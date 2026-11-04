"""A cavity that follows an outer skin: intersection, trimmed region, inward
offset, split line, loft cut, cut offset from a surface.

The chain these tools serve: a plane cuts the skin -> the intersection curves
in a sketch on that plane -> trimmed to a frame into one closed contour ->
that contour offset inward by the wall -> a loft cut through such sections,
or a cut ending a wall's thickness short of the skin.

SolidWorks facts these tools rest on (verified live on SW 2025 SP3, 04.11.2026):
- ISketchManager.IntersectCurves in a 2D sketch answers None and creates
  nothing, whatever is selected (faces, the body, plane + faces; early or
  late bound; graphics, feature tree and DisplayWhenAdded on).
- IModelDoc2.Sketch3DIntersections in an open 3D sketch with the plane and the
  faces selected does create the curves (one closed spline for a loft
  section). SketchUseEdge3 then carries them into the 2D sketch on the plane,
  same length, with an On Edge relation to the 3D sketch.
- A segment with an On Edge relation refuses split and trim (sketch_edit.py);
  deleting that relation frees it.
"""

import contextlib
import math
import time

from .. import binding
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m
from . import offset_loop
from . import sketch_edit as se

SW_SEL_DATUMPLANES = 4   # swSelectType_e
SW_SEL_FACES = 2
SW_CONSTRAINT_USEEDGE = se.SW_CONSTRAINT_USEEDGE
SW_SKETCH_LINE = 0
SW_SKETCH_SPLINE = 3
_SAMPLES = 96            # points per curve for inside tests and boxes
_TESS_M = 5e-5           # chord / length tolerance of curve tessellation, m


# --- pure helpers (unit-tested) ------------------------------------------------------


def box_bounds(keep_box) -> list:
    """keep_box -> [(axis, low, high)] with None for an open side; pure.

    keep_box: {"x": [lo, hi], "y": [lo, hi], "z": [lo, hi]} in model mm, any
    axis or side may be left out or None; or [[x1, y1, z1], [x2, y2, z2]]
    corners (None for an open side)."""
    out = []
    if isinstance(keep_box, dict):
        for axis, key in enumerate("xyz"):
            pair = keep_box.get(key)
            if pair is None:
                continue
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise SolidWorksError(f"keep_box['{key}'] must be [low, high] mm (None for an open side).")
            out.append(_bound(axis, pair[0], pair[1]))
    elif isinstance(keep_box, (list, tuple)) and len(keep_box) == 2 and all(
            isinstance(c, (list, tuple)) and len(c) == 3 for c in keep_box):
        for axis in range(3):
            if keep_box[0][axis] is None and keep_box[1][axis] is None:
                continue
            out.append(_bound(axis, keep_box[0][axis], keep_box[1][axis]))
    else:
        raise SolidWorksError("keep_box: {'x': [low, high], 'y': [...], 'z': [...]} in model mm, or two "
                              "corners [[x1, y1, z1], [x2, y2, z2]]; None for an open side.")
    if not out:
        raise SolidWorksError("keep_box limits nothing: give at least one side.")
    return out


def _bound(axis: int, a, b) -> tuple:
    lo = None if a is None else float(a)
    hi = None if b is None else float(b)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    if lo is not None and hi is not None and hi - lo <= 0:
        raise SolidWorksError(f"keep_box along {'xyz'[axis]} is empty ([{lo}, {hi}]).")
    return axis, lo, hi


def in_box(point_mm, bounds, tol_mm: float = 1e-4) -> bool:
    """Is the model point inside (or on) the box; pure."""
    for axis, lo, hi in bounds:
        c = point_mm[axis]
        if lo is not None and c < lo - tol_mm:
            return False
        if hi is not None and c > hi + tol_mm:
            return False
    return True


def outside_runs(flags) -> list:
    """Index ranges [(i, j)] of consecutive True flags (an outside run), the
    first and last joined for a closed curve is NOT assumed; pure."""
    runs, start = [], None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(flags) - 1))
    return runs


# --- SolidWorks plumbing -------------------------------------------------------------


@contextlib.contextmanager
def _unlocked(session):
    """Lift CommandInProgress and the view freeze around sketch open/close:
    InsertSketch / Insert3DSketch / EditSketch refuse to open a sketch while
    the server's UI freeze is on (run_script's ui_unlocked does the same)."""
    sw = session._sw
    model = session._require_model()
    was_busy = bool(sw.CommandInProgress)
    if was_busy:
        sw.CommandInProgress = False
    view = binding.wrap(model.ActiveView, session._mod.IModelView)
    was_still = view is not None and not view.EnableGraphicsUpdate
    if was_still:
        view.EnableGraphicsUpdate = True
    try:
        yield
    finally:
        if was_still:
            view.EnableGraphicsUpdate = False
        if was_busy:
            sw.CommandInProgress = True


def _ui(session, call, *args):
    """Run one sketch open/close call with the UI freeze lifted.

    Only these calls need it. Everything else inside an open sketch must run
    WITH CommandInProgress set: without it every COM call there costs ~20 x
    more (64 points on 5 segments: 15 s against 0.7 s, measured)."""
    return call(*args)


def _segments(session, sketch) -> list:
    return [binding.wrap(s, session._mod.ISketchSegment) for s in (sketch.GetSketchSegments() or ())]


def _new_feature(session, names_before: set, type_name: str):
    return next((f for f in session._iter_features()
                 if f.Name not in names_before and f.GetTypeName2() == type_name), None)


def _sketch_plane_feature(session, sketch_feature):
    """The plane (IFeature) or face (IFace2) the sketch lies on, to select."""
    sketch = binding.wrap(sketch_feature.GetSpecificFeature2(), session._mod.ISketch)
    try:
        ref = sketch.GetReferenceEntity(0)
    except Exception:  # noqa: BLE001
        ref = None
    obj, kind = (ref if isinstance(ref, tuple) else (ref, None))
    if kind is not None and int(kind) == SW_SEL_FACES and obj is not None:
        return ("face", binding.wrap(obj, session._mod.IFace2))
    for raw in sketch_feature.GetParents() or ():
        parent = binding.wrap(raw, session._mod.IFeature)
        if parent.GetTypeName2() == "RefPlane":
            return ("plane", parent)
    raise SolidWorksError(f"'{sketch_feature.Name}' lies neither on a plane nor on a face this tool can select.")


def _select_entity(session, obj, append: bool) -> bool:
    return bool(binding.wrap(obj, session._mod.IEntity).Select4(append, None))


def _select_target(session, target, append: bool) -> bool:
    kind, obj = target
    if kind == "plane":
        return bool(obj.Select2(append, 0))
    return _select_entity(session, obj, append)


def _faces(session, faces, body) -> list:
    """IFace2 list: selectors ('#5', '+x', '@x,y,z', [x, y, z]) or every face of
    the named body / of all solid bodies."""
    from . import features as rbr_features
    if faces not in (None, "all", []):
        items = faces if isinstance(faces, (list, tuple)) and not (
            len(faces) == 3 and all(isinstance(c, (int, float)) for c in faces)) else [faces]
        return [binding.wrap(rbr_features._face_of(session, f), session._mod.IFace2) for f in items]
    bodies = session._part_bodies()
    if body is not None:
        bodies = [b for b in bodies if b.Name == body]
        if not bodies:
            raise SolidWorksError(f"No solid body '{body}' (list_bodies names them: "
                                  f"{', '.join(b.Name for b in session._part_bodies())}).")
    out = [binding.wrap(f, session._mod.IFace2) for b in bodies for f in (b.GetFaces() or ())]
    if not out:
        raise SolidWorksError("The part has no solid body faces to intersect.")
    return out


def _sample_seg(session, seg, count: int = _SAMPLES) -> list:
    """Sketch-space points (m) along a segment, ends included."""
    kind = int(seg.GetType())
    mod = session._mod
    if kind == SW_SKETCH_LINE:
        line = binding.wrap(seg, mod.ISketchLine)
        a = binding.wrap(line.GetStartPoint2(), mod.ISketchPoint)
        b = binding.wrap(line.GetEndPoint2(), mod.ISketchPoint)
        return [(a.X + (b.X - a.X) * k / count, a.Y + (b.Y - a.Y) * k / count) for k in range(count + 1)]
    curve = binding.wrap(seg.GetCurve(), mod.ICurve)
    params = curve.GetEndParams()
    t0, t1 = float(params[1]), float(params[2])
    # Few COM calls: inside an open sketch each one costs ~0.1 s (64 Evaluate2
    # per segment made a 6-trim region take 100 s). Two tessellated halves
    # (a closed curve tessellates to nothing from a point back to itself).
    ends = [curve.Evaluate2(t, 0) for t in (t0, (t0 + t1) / 2.0, t1)]
    polyline = []
    for a, b in ((ends[0], ends[1]), (ends[1], ends[2])):
        flat = list(curve.GetTessPts(_TESS_M, _TESS_M, offset_loop._vec(a[:3]), offset_loop._vec(b[:3])) or ())
        half = [(float(flat[k]), float(flat[k + 1])) for k in range(0, len(flat) - 2, 3)] or [
            (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))]
        polyline += half if not polyline else half[1:]
    return resample(polyline, count)


def resample(polyline, count: int) -> list:
    """count + 1 points evenly spaced by length along a polyline; pure."""
    if len(polyline) < 2:
        return list(polyline) * (count + 1) if polyline else []
    cum = [0.0]
    for p, q in zip(polyline, polyline[1:]):
        cum.append(cum[-1] + math.dist(p, q))
    total = cum[-1]
    if total == 0:
        return [polyline[0]] * (count + 1)
    out, j = [], 0
    for k in range(count + 1):
        s = total * k / count
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        span = cum[j + 1] - cum[j]
        f = 0.0 if span == 0 else (s - cum[j]) / span
        p, q = polyline[j], polyline[j + 1]
        out.append((p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f))
    return out



def _bbox(points_mm) -> dict:
    lo = [min(p[i] for p in points_mm) for i in range(3)]
    hi = [max(p[i] for p in points_mm) for i in range(3)]
    return {"min_mm": [round(c, 3) for c in lo], "max_mm": [round(c, 3) for c in hi]}


def _detach(session, sketch, segments) -> int:
    """Delete the On Edge relations that lock converted segments; how many."""
    manager = binding.wrap(sketch.RelationManager, session._mod.ISketchRelationManager)
    gone = 0
    for seg in segments:
        for raw in seg.GetRelations() or ():
            rel = binding.wrap(raw, session._mod.ISketchRelation)
            if int(rel.GetRelationType()) == SW_CONSTRAINT_USEEDGE:
                if manager.DeleteRelation(rel):
                    gone += 1
    return gone


def _describe(session, sketch) -> dict:
    """Segments, regions, bounding box and the largest closed contour (area, a
    point inside it for region_point_mm) of a sketch - points are moved to
    model space in Python: one COM transform per point costs seconds."""
    from . import param
    segs = [s for s in _segments(session, sketch) if not bool(s.ConstructionGeometry)]
    origin, u_axis, v_axis = param._sketch_axes(session, sketch)
    rows, pieces = [], []
    for s in segs:
        sample = [(m_to_mm(p[0]), m_to_mm(p[1])) for p in _sample_seg(session, s, 48)]
        pieces.append(sample)
        rows.append({"type": {0: "line", 1: "arc", 3: "spline"}.get(int(s.GetType()), f"type {s.GetType()}"),
                     "length_mm": round(m_to_mm(s.GetLength()), 3),
                     "closed": int(s.GetType()) != SW_SKETCH_LINE and math.dist(sample[0], sample[-1]) < 1e-4})
    out = {"segments": rows, "regions": len(sketch.GetSketchRegions() or ())}
    pts = [_model_of(origin, u_axis, v_axis, p) for piece in pieces for p in piece]
    if pts:
        out["bounding_box_mm"] = _bbox(pts)
    loops = rings(pieces, 0.01)
    out["closed_contours"] = len(loops)
    if loops:
        biggest = max(loops, key=offset_loop.polygon_area)
        out["region_area_mm2"] = round(offset_loop.polygon_area(biggest), 3)
        point = offset_loop.interior_point(biggest)
        if point is not None:
            out["region_point_mm"] = [round(c, 4) for c in _model_of(origin, u_axis, v_axis, point)]
    return out


# --- 1. sketch_intersection ----------------------------------------------------------


def box_crosses_plane(box_mm, origin_mm, normal, tol_mm: float = 0.01) -> bool:
    """Does an axis-aligned box [x1, y1, z1, x2, y2, z2] reach the plane; pure."""
    lo, hi = box_mm[:3], box_mm[3:]
    ds = [sum((c[i] - origin_mm[i]) * normal[i] for i in range(3))
          for c in ((x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2]))]
    return min(ds) <= tol_mm and max(ds) >= -tol_mm


def _face_box_mm(face) -> list:
    return [m_to_mm(c) for c in face.GetBox()]


def _target_plane(session, target):
    """(origin mm, unit normal) of the plane or planar face to intersect with,
    or None when it cannot be read cheaply (then every face is tried).
    Read from IRefPlane.Transform: its rotation rows are the plane's x, y and
    normal; opening a sketch to ask would cost a rebuild."""
    kind, obj = target
    try:
        if kind == "plane":
            ref = binding.wrap(obj.GetSpecificFeature2(), session._mod.IRefPlane)
            data = list(binding.wrap(ref.Transform, session._mod.IMathTransform).ArrayData)
            normal, origin = data[6:9], [m_to_mm(c) for c in data[9:12]]
        else:
            face = binding.wrap(obj, session._mod.IFace2)
            normal = list(face.Normal)
            box = face.GetBox()
            centre = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
            origin = [m_to_mm(c) for c in face.GetClosestPointOn(*centre)[:3]]
    except Exception:  # noqa: BLE001 - only a speed-up; all faces still work
        return None
    length = math.sqrt(sum(c * c for c in normal))
    if length < 1e-9:
        return None
    return origin, [c / length for c in normal]


def sketch_intersection(session, sketch: str | None = None, plane: str | None = None, faces=None,
                        body: str | None = None, name: str | None = None, detach: bool = True) -> dict:
    """Intersection curves of faces (or a whole body) with a sketch plane, as
    sketch segments of a 2D sketch on that plane.

    sketch: an existing sketch to add them to, or plane: a plane ('front',
    'Plane5') for a new sketch. faces: selectors ('#5', '+x', [x, y, z] mm on
    the face); default every face of `body` (a list_bodies name) or of all
    solid bodies. The curves are made in a helper 3D sketch
    (Sketch3DIntersections: the 2D IntersectCurves creates nothing) and
    converted into the sketch. detach=True (default) drops their link to the
    helper, which is then deleted: the curves can be trimmed (sketch_trim_region),
    but no longer follow a changed skin. detach=False keeps the helper (hidden)
    and the link; such curves refuse trimming.
    """
    if (sketch is None) == (plane is None):
        raise SolidWorksError("Give sketch (an existing sketch) or plane (for a new one), not both.")
    model = session._require_model()
    session._require_part()
    mod = session._mod
    sm = binding.wrap(model.SketchManager, mod.ISketchManager)
    if sketch is not None:
        target_feature = session._sketch_by_name(sketch)
        target = _sketch_plane_feature(session, target_feature)
    else:
        plane_feature, _label = session._named_plane(plane)
        target_feature, target = None, ("plane", plane_feature)
    timing = {}
    t0 = time.perf_counter()
    face_list = _faces(session, faces, body)
    total_faces = len(face_list)
    frame = _target_plane(session, target)
    if frame is not None:
        face_list = [f for f in face_list if box_crosses_plane(_face_box_mm(f), *frame)]
        if not face_list:
            raise SolidWorksError(f"None of the {total_faces} faces reaches the plane: no intersection. Check "
                                  "the plane's offset and side.")
    timing["faces"] = time.perf_counter() - t0

    names_before = {f.Name for f in session._iter_features()}
    model.ClearSelection2(True)
    _ui(session, sm.Insert3DSketch, True)
    helper_sketch = binding.wrap(model.GetActiveSketch2(), mod.ISketch)
    if helper_sketch is None:
        raise SolidWorksError("SolidWorks did not open a 3D sketch.")
    try:
        model.ClearSelection2(True)
        if not _select_target(session, target, False):
            raise SolidWorksError("Could not select the sketch plane.")
        picked = sum(1 for f in face_list if _select_entity(session, f, True))
        if not picked:
            raise SolidWorksError("Could not select any of the faces.")
        model.Sketch3DIntersections()
        made3 = _segments(session, helper_sketch)
    finally:
        model.ClearSelection2(True)
        _ui(session, sm.Insert3DSketch, True)
    helper = _new_feature(session, names_before, "3DProfileFeature")
    timing["sketch3d"] = time.perf_counter() - t0 - sum(timing.values())
    if not made3:
        raise SolidWorksError(
            f"The plane does not cut the {picked} face(s): no intersection curves. Check that the "
            "plane passes through the body (its offset, which side).")
    if helper is None:
        raise SolidWorksError("The helper 3D sketch is not in the tree.")
    helper_sketch = binding.wrap(helper.GetSpecificFeature2(), mod.ISketch)
    segs3 = _segments(session, helper_sketch)

    names_mid = {f.Name for f in session._iter_features()}
    if target_feature is not None:
        model.ClearSelection2(True)
        if not target_feature.Select2(False, 0):
            raise SolidWorksError(f"Could not select the sketch '{sketch}'.")
        _ui(session, model.EditSketch)
    else:
        model.ClearSelection2(True)
        _select_target(session, target, False)
        _ui(session, sm.InsertSketch, True)
    sk2 = binding.wrap(model.GetActiveSketch2(), mod.ISketch)
    if sk2 is None:
        raise SolidWorksError("Could not open the 2D sketch on the plane.")
    try:
        before = {se._seg_key(s) for s in _segments(session, sk2)}
        model.ClearSelection2(True)
        for s in segs3:
            s.Select4(True, None)
        if not sm.SketchUseEdge3(False, False):
            raise SolidWorksError("Converting the intersection curves into the sketch (SketchUseEdge3) failed.")
        made = [s for s in _segments(session, sk2) if se._seg_key(s) not in before]
        freed = _detach(session, sk2, made) if detach else 0
    finally:
        model.ClearSelection2(True)
        _ui(session, sm.InsertSketch, True)
    feature2 = target_feature or _new_feature(session, names_mid, "ProfileFeature")
    if feature2 is None:
        raise SolidWorksError("The 2D sketch is not in the tree.")
    timing["convert"] = time.perf_counter() - t0 - sum(timing.values())

    helper_name = helper.Name
    if detach:
        model.ClearSelection2(True)
        helper.Select2(False, 0)
        model.EditDelete()
        model.ClearSelection2(True)
        if any(f.Name == helper_name for f in session._iter_features()):
            raise SolidWorksError(f"The helper 3D sketch '{helper_name}' could not be deleted.")
        helper_name = None
    else:
        model.ClearSelection2(True)
        helper.Select2(False, 0)
        model.BlankSketch()
        model.ClearSelection2(True)
    if name and target_feature is None:
        feature2.Name = name
        if feature2.Name != name:
            raise SolidWorksError(f"The sketch could not be named '{name}' (taken?).")
    model.EditRebuild3()  # only what changed: a forced rebuild of a big part costs seconds
    timing["cleanup"] = time.perf_counter() - t0 - sum(timing.values())
    final = binding.wrap(session._sketch_by_name(feature2.Name).GetSpecificFeature2(), mod.ISketch)
    result = {"ok": True, "sketch": feature2.Name, "curves": len(made), "faces": picked,
              "faces_of_body": total_faces, "detached": bool(detach), "relations_removed": freed,
              "helper_3d_sketch": helper_name}
    result.update(_describe(session, final))
    timing["check"] = time.perf_counter() - t0 - sum(timing.values())
    result["timing_s"] = {k: round(v, 2) for k, v in timing.items()}
    return result


# --- 2. sketch_trim_region -----------------------------------------------------------


def rings(pieces, tol: float) -> list:
    """Chain polylines into closed rings (several loops allowed); pure.

    A piece that closes on itself is a ring; open pieces are joined end to end,
    reversed as needed. Pieces that never close are left out."""
    rest = [list(p) for p in pieces if len(p) >= 2]
    out = []
    while rest:
        ring = rest.pop(0)
        grown = True
        while math.dist(ring[0], ring[-1]) > tol and grown:
            grown = False
            for i, piece in enumerate(rest):
                if math.dist(piece[0], ring[-1]) <= tol:
                    ring.extend(piece[1:])
                elif math.dist(piece[-1], ring[-1]) <= tol:
                    ring.extend(reversed(piece[:-1]))
                else:
                    continue
                rest.pop(i)
                grown = True
                break
        if math.dist(ring[0], ring[-1]) <= tol and len(ring) >= 4:
            out.append(ring[:-1])
    return out


def inside_rings(point, ring_list) -> bool:
    """Even-odd over all rings: inside an outline, outside its holes; pure."""
    return sum(1 for r in ring_list if offset_loop.point_in_polygon(point, r)) % 2 == 1


def frame_line(origin_mm, u_axis, v_axis, axis: int, value_mm: float, span_uv):
    """The line in sketch (u, v) mm where the model coordinate `axis` equals
    value_mm, long enough to cross the span ((u0, v0), (u1, v1)) with room to
    spare; None when the sketch plane is parallel to that bound; pure."""
    a, b = u_axis[axis], v_axis[axis]
    n2 = a * a + b * b
    if n2 < 1e-12:
        return None
    k = (value_mm - origin_mm[axis]) / n2
    p0 = (a * k, b * k)
    d = (-b / math.sqrt(n2), a / math.sqrt(n2))
    (u0, v0), (u1, v1) = span_uv
    corners = [(u0, v0), (u0, v1), (u1, v0), (u1, v1)]
    ts = [(c[0] - p0[0]) * d[0] + (c[1] - p0[1]) * d[1] for c in corners]
    room = 0.1 * math.dist((u0, v0), (u1, v1)) + 5.0
    t0, t1 = min(ts) - room, max(ts) + room
    return (p0[0] + d[0] * t0, p0[1] + d[1] * t0), (p0[0] + d[0] * t1, p0[1] + d[1] * t1)


def _model_of(origin, u_axis, v_axis, uv_mm) -> list:
    return [origin[i] + uv_mm[0] * u_axis[i] + uv_mm[1] * v_axis[i] for i in range(3)]


def _open_for_edit(session, feature):
    model = session._require_model()
    model.ClearSelection2(True)
    if not feature.Select2(False, 0):
        raise SolidWorksError(f"Could not select the sketch '{feature.Name}'.")
    _ui(session, model.EditSketch)
    sketch = binding.wrap(model.GetActiveSketch2(), session._mod.ISketch)
    if sketch is None:
        raise SolidWorksError(f"Could not edit the sketch '{feature.Name}'.")
    return binding.wrap(model.SketchManager, session._mod.ISketchManager), sketch


def _outside_pick(session, sk, origin, u_axis, v_axis, bounds, ring_list):
    """(segment, (u, v) mm) in the middle of the longest stretch of a segment
    that lies outside the kept region, or None when nothing does."""
    def on_frame(p_model) -> bool:
        return any(value is not None and abs(p_model[axis] - value) < 1e-3
                   for axis, lo, hi in bounds for value in (lo, hi))

    for seg in [s for s in _segments(session, sk) if not bool(s.ConstructionGeometry)]:
        pts = [(m_to_mm(p[0]), m_to_mm(p[1])) for p in _sample_seg(session, seg, 64)][1:-1]
        if not pts:
            continue
        models = [_model_of(origin, u_axis, v_axis, p) for p in pts]
        frame_piece = int(seg.GetType()) == SW_SKETCH_LINE and all(on_frame(m) for m in models)
        flags = [not (in_box(m, bounds, 1e-3) and (inside_rings(p, ring_list) if frame_piece else True))
                 for p, m in zip(pts, models)]
        runs = outside_runs(flags)
        if runs:
            i, j = max(runs, key=lambda r: r[1] - r[0])
            return seg, pts[(i + j) // 2]
    return None


def sketch_trim_region(session, sketch: str, keep_box, max_trims: int = 80) -> dict:
    """Trim a sketch's curves (e.g. sketch_intersection's) to the part inside
    keep_box, closing the cut with straight lines on the box's sides: one
    closed contour, ready for sketch_offset_chain, a loft or a cut.

    keep_box: {'x': [low, high], 'y': [...], 'z': [...]} in MODEL mm, any side
    None or left out (open). A side meets the sketch plane in a line; the curve
    keeps what lies inside the box, the line what lies inside the curve.
    Converted curves still linked to their source are freed first (On Edge).
    """
    from . import param
    bounds = box_bounds(keep_box)
    model = session._require_model()
    session._require_part()
    feature = session._sketch_by_name(sketch)
    trims, lines, skipped = 0, [], []
    timing = {"pick": 0.0, "trim": 0.0}
    t0 = time.perf_counter()
    sm, sk = _open_for_edit(session, feature)
    timing["open"] = time.perf_counter() - t0
    try:
        curves = [s for s in _segments(session, sk) if not bool(s.ConstructionGeometry)]
        if not curves:
            raise SolidWorksError(f"'{sketch}' has no (non-construction) curves to trim.")
        freed = _detach(session, sk, curves)
        origin, u_axis, v_axis = param._sketch_axes(session, sk)
        pieces = [[(m_to_mm(p[0]), m_to_mm(p[1])) for p in _sample_seg(session, s)] for s in curves]
        ring_list = rings(pieces, 0.01)
        if not ring_list:
            raise SolidWorksError(f"The curves of '{sketch}' close no loop: no region to keep.")
        us = [p[0] for r in ring_list for p in r]
        vs = [p[1] for r in ring_list for p in r]
        span = ((min(us), min(vs)), (max(us), max(vs)))
        for axis, lo, hi in bounds:
            for value in (lo, hi):
                if value is None:
                    continue
                label = f"{'xyz'[axis]}={value:g}"
                ends = frame_line(origin, u_axis, v_axis, axis, value, span)
                if ends is None:
                    if not in_box(origin, [(axis, lo, hi)], 1e-3):
                        raise SolidWorksError(f"The sketch plane lies outside keep_box ({'xyz'[axis]} = "
                                              f"{origin[axis]:.3f} mm, kept {lo}..{hi}).")
                    skipped.append(label)
                    continue
                a, b = ends
                if sm.CreateLine(mm_to_m(a[0]), mm_to_m(a[1]), 0.0, mm_to_m(b[0]), mm_to_m(b[1]), 0.0) is None:
                    raise SolidWorksError(f"Could not draw the frame line {label} mm.")
                lines.append(label)
        model.ClearSelection2(True)
        timing["frame"] = time.perf_counter() - t0 - timing["open"]
        while True:
            t1 = time.perf_counter()
            pick = _outside_pick(session, sk, origin, u_axis, v_axis, bounds, ring_list)
            timing["pick"] += time.perf_counter() - t1
            if pick is None:
                break
            if trims >= max_trims:
                raise SolidWorksError(f"Still pieces outside after {trims} trims: the contour is too "
                                      "intricate for one call (raise max_trims), or a trim keeps failing.")
            seg, p = pick
            t1 = time.perf_counter()
            se.trim_to_intersection(session, seg, [p[0], p[1]], space="sketch")
            timing["trim"] += time.perf_counter() - t1
            trims += 1
    finally:
        t1 = time.perf_counter()
        model.ClearSelection2(True)
        _ui(session, sm.InsertSketch, True)
        timing["close"] = time.perf_counter() - t1
    t1 = time.perf_counter()
    model.EditRebuild3()
    timing["rebuild"] = time.perf_counter() - t1
    t1 = time.perf_counter()
    feature = session._sketch_by_name(sketch)
    final = binding.wrap(feature.GetSpecificFeature2(), session._mod.ISketch)
    result = {"ok": True, "sketch": feature.Name, "trims": trims, "frame_lines": lines,
              "parallel_sides_skipped": skipped, "relations_removed": freed}
    result.update(_describe(session, final))
    timing["check"] = time.perf_counter() - t1
    result["timing_s"] = {k: round(v, 2) for k, v in timing.items()}
    if result.get("regions") != 1:
        result["ok"] = False
        result["error"] = (f"The trimmed sketch has {result.get('regions')} regions instead of one closed "
                           "contour: keep_box may cut the outline into several pieces.")
    return result


# --- 3. sketch_offset_chain ----------------------------------------------------------


def _distance_to_ring(point, ring) -> float:
    return min(offset_loop._segment_distance(point, a, b) for a, b in zip(ring, ring[1:] + ring[:1]))


def sketch_offset_chain(session, sketch: str, distance_mm: float, inward: bool = True,
                        original_to_construction: bool = True, link_to: str | None = None) -> dict:
    """Offset the closed contour of a sketch as one chain (SketchOffset2) by
    distance_mm to the inside (or outside) - a wall's thickness - and turn the
    original contour into construction geometry, so the offset is the profile.

    The side is checked against the contour, not guessed from the sign: an
    offset on the wrong side is deleted and redone the other way. The offset
    gets one dimension; link_to names a global variable to drive it.
    """
    from . import param
    if distance_mm <= 0:
        raise SolidWorksError(f"distance_mm must be > 0 (got {distance_mm:g}); inward picks the side.")
    model = session._require_model()
    session._require_part()
    mod = session._mod
    if link_to:
        known = {(e.get("name") or "").lower() for e in session._equation_entries() if e.get("global")}
        if link_to.lower() not in known:
            raise SolidWorksError(f'No global variable "{link_to}": add it first (set_global).')
    feature = session._sketch_by_name(sketch)
    d_m = mm_to_m(distance_mm)
    sm, sk = _open_for_edit(session, feature)
    made, dims_before = [], {e["name"] for e in session._dimension_entries(feature)}
    try:
        chain = [s for s in _segments(session, sk) if not bool(s.ConstructionGeometry)]
        if not chain:
            raise SolidWorksError(f"'{sketch}' has no (non-construction) contour to offset.")
        locked = [se.locked_by(session, s) for s in chain]
        pieces = [[(m_to_mm(p[0]), m_to_mm(p[1])) for p in _sample_seg(session, s)] for s in chain]
        loops = rings(pieces, 0.01)
        if len(loops) != 1 or sum(len(p) for p in pieces) - len(pieces) > len(loops[0]) + 1:
            raise SolidWorksError(f"'{sketch}' is not one closed contour ({len(loops)} closed loops of "
                                  f"{len(chain)} segments): trim it first (sketch_trim_region).")
        polygon = loops[0]
        keys_before = {se._seg_key(s) for s in _segments(session, sk)}
        sign = 1.0
        for _attempt in range(2):
            model.ClearSelection2(True)
            picked = sum(1 for s in chain if s.Select4(True, None))
            if picked != len(chain):
                raise SolidWorksError("Could not select the whole contour.")
            sm.SketchOffset2(sign * d_m, False, True, 0, 0, True)
            model.ClearSelection2(True)
            made = [s for s in _segments(session, sk) if se._seg_key(s) not in keys_before]
            if not made:
                sign = -sign
                continue
            mids = [(m_to_mm(p[0]), m_to_mm(p[1])) for s in made for p in _sample_seg(session, s, 8)[1:-1]]
            if all(offset_loop.point_in_polygon(p, polygon) == inward for p in mids):
                break
            offset_loop._delete(model, made)
            made = []
            sign = -sign
        if not made:
            raise SolidWorksError(
                f"SolidWorks could not offset the contour {distance_mm:g} mm to the "
                f"{'inside' if inward else 'outside'}: the contour may be narrower than twice that "
                "somewhere, or a spline folds over itself at that distance (try a smaller wall).")
        new_pieces = [[(m_to_mm(p[0]), m_to_mm(p[1])) for p in _sample_seg(session, s)] for s in made]
        gaps = [_distance_to_ring(p, polygon) for piece in new_pieces for p in piece[1:-1]]
        if original_to_construction:
            for s in chain:
                s.ConstructionGeometry = True
    finally:
        model.ClearSelection2(True)
        _ui(session, sm.InsertSketch, True)
    model.EditRebuild3()
    feature = session._sketch_by_name(sketch)
    new_dims = [e["name"] for e in session._dimension_entries(feature) if e["name"] not in dims_before]
    equations = []
    if link_to and new_dims:
        equations = [f'"{new_dims[0]}" = "{link_to}"']
        session.set_equations(equations)
    final = binding.wrap(feature.GetSpecificFeature2(), mod.ISketch)
    result = {"ok": True, "sketch": feature.Name, "side": "inside" if inward else "outside",
              "offset_segments": len(made), "offset_dimension": new_dims[0] if new_dims else None,
              "equations": equations, "original_area_mm2": round(offset_loop.polygon_area(polygon), 3),
              "wall_mm": {"min": round(min(gaps), 4), "max": round(max(gaps), 4)} if gaps else None,
              "original_locked": [r for r in locked if r]}
    result.update(_describe(session, final))
    if gaps and (min(gaps) < distance_mm * 0.98 or max(gaps) > distance_mm * 1.02 + 0.05):
        result["warning"] = (f"The offset lies {min(gaps):.3f}..{max(gaps):.3f} mm from the contour, not "
                             f"{distance_mm:g} mm everywhere: check corners (a sharp concave corner rounds).")
    return result


# --- 4. split_line_project -----------------------------------------------------------

MARK_SPLIT_SKETCH = 4   # verified: the sketch with mark 4, the faces with mark 1
MARK_SPLIT_FACES = 1


def _select_marked(session, model, obj, mark: int, append: bool) -> bool:
    smg = binding.wrap(model.SelectionManager, session._mod.ISelectionMgr)
    data = binding.wrap(smg.CreateSelectData(), session._mod.ISelectData)
    data.Mark = mark
    return bool(binding.wrap(obj, session._mod.IEntity).Select4(append, data))


def _face_count(session) -> int:
    return sum(len(b.GetFaces() or ()) for b in session._part_bodies())


def split_line_project(session, sketch: str, faces, directional: bool = False, flip: bool = False,
                       name: str | None = None) -> dict:
    """Project a sketch onto faces and split them along it (Split Line,
    projection): e.g. the cavity contour onto the skin, so a cut can end
    offset from just that patch of it.

    faces: selectors ('#5', '+z', [x, y, z] mm on the face). directional=True
    projects one way only (flip: the other way). Verified on SW 2025:
    InsertSplitLineProject needs the sketch with selection mark 4 and the faces
    with mark 1 (other marks: nothing, and it answers None even when it works),
    so the new feature and the face count are the proof.
    """
    if faces in (None, "all", []):
        raise SolidWorksError("Name the faces to split (selectors such as '#5' or [x, y, z] on the face).")
    model = session._require_model()
    session._require_part()
    feature = session._sketch_by_name(sketch)
    face_list = _faces(session, faces, None)
    before_faces = _face_count(session)
    names_before = {f.Name for f in session._iter_features()}
    model.ClearSelection2(True)
    if not feature.Select2(False, MARK_SPLIT_SKETCH):
        raise SolidWorksError(f"Could not select the sketch '{sketch}'.")
    picked = sum(1 for f in face_list if _select_marked(session, model, f, MARK_SPLIT_FACES, True))
    if picked != len(face_list):
        raise SolidWorksError(f"Could select only {picked} of the {len(face_list)} faces.")
    binding.wrap(model, session._mod.IModelDoc2).InsertSplitLineProject(bool(directional), bool(flip))
    model.ClearSelection2(True)
    made = [f for f in session._iter_features() if f.Name not in names_before]
    if not made:
        from . import geom
        raise SolidWorksError(
            f"SolidWorks made no split line of '{sketch}' on the {picked} face(s): the projected "
            "contour must cross or lie on them (projection runs along the sketch normal"
            f"{', one way: try flip' if directional else ''}). {geom.whats_wrong(session) or ''}".strip())
    split = made[-1]
    if name:
        split.Name = name
    model.EditRebuild3()
    after_faces = _face_count(session)
    result = {"ok": True, "feature": split.Name, "faces_selected": picked,
              "faces_before": before_faces, "faces_after": after_faces}
    if after_faces <= before_faces:
        result["warning"] = "The split line was made but the face count did not grow: check it in SolidWorks."
    return result


# --- 5. loft_cut ---------------------------------------------------------------------

MARK_LOFT_PROFILE = 1
MARK_LOFT_GUIDE = 2


def loft_cut(session, sketches: list, guides: list | None = None, closed: bool = False,
             name: str = "Loft-Cut") -> dict:
    """Cut a loft through existing closed sketches, in the order given
    (InsertCutBlend; profiles mark 1, guide curves mark 2). The volume must
    drop, otherwise the cut is refused and rolled back."""
    if not sketches or len(sketches) < 2:
        raise SolidWorksError("A loft cut needs at least two profile sketches, in order.")
    model = session._require_model()
    session._require_part()
    volume_before = float(session.get_mass_properties()["mass_properties"].get("volume_mm3") or 0.0)
    profiles = [session._sketch_by_name(s) for s in sketches]
    guide_feats = [session._sketch_by_name(g) for g in guides or ()]
    model.ClearSelection2(True)
    for feat in profiles:
        if not feat.Select2(True, MARK_LOFT_PROFILE):
            raise SolidWorksError(f"Could not select the profile '{feat.Name}'.")
    for feat in guide_feats:
        if not feat.Select2(True, MARK_LOFT_GUIDE):
            raise SolidWorksError(f"Could not select the guide '{feat.Name}'.")
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    made = fm.InsertCutBlend(bool(closed), False, False, 1.0, 0, 0, False, 0.0, 0.0, 0, True, True)
    model.ClearSelection2(True)
    if made is None:
        from . import geom
        raise SolidWorksError(
            "SolidWorks refused the loft cut: each profile must be ONE closed contour (construction "
            "geometry aside), the profiles in order, and they must not twist "
            f"(start points). {geom.whats_wrong(session) or ''}".strip())
    made = binding.wrap(made, session._mod.IFeature)
    result = session._finish_feature(made, name, profiles=[f.Name for f in profiles],
                                     guides=[f.Name for f in guide_feats])
    volume_after = float((result.get("mass_properties") or {}).get("volume_mm3") or 0.0)
    removed = volume_before - volume_after
    if removed <= 1e-6 * max(volume_before, 1.0):
        raise SolidWorksError("The loft cut removed no material: do the profiles lie inside the body? Rolled back.")
    result["removed_mm3"] = round(removed, 3)
    return result


# --- 6. cut_offset_from_surface -------------------------------------------------------


def cut_offset_from_surface(session, sketch: str, face, offset_mm: float, toward,
                            offset_reverse: bool = False, translate: bool = False,
                            region_point_mm=None, name: str | None = None) -> dict:
    """Cut a sketch toward a model direction, ending offset_mm short of a face
    (end condition 'offset from surface', 5) - a pocket that keeps a wall of
    offset_mm under a curved skin; the face may be a patch a split line made.

    face: '#5' / '+z' style selector or [x, y, z] mm on it. offset_reverse=True
    puts the end offset_mm beyond the face instead. translate=True moves a
    copy of the face (a flat end) instead of offsetting its shape.
    region_point_mm picks one region of the sketch. The offset is a dimension
    ('offset': D1@<cut>) for set_dimension / link_dimension.
    """
    from . import features
    try:
        return features.cut(session, sketch, toward, up_to=face, region_point_mm=region_point_mm, name=name,
                            offset_mm=offset_mm, offset_reverse=offset_reverse, translate=translate)
    except SolidWorksError as exc:
        if translate or "refused" not in str(exc):
            raise
        faces_hit = faces_ahead(session, sketch, toward)
        if len(faces_hit) <= 1:
            raise
        listing = ", ".join(f"{h['rays']} rays on a face of {h['area_mm2']:g} mm^2 (hit at {h['ahead']})"
                            for h in faces_hit)
        raise SolidWorksError(
            f"{exc} Seen along {list(toward)} the profile meets {len(faces_hit)} faces ({listing}): an "
            "offset-from-surface end needs ONE face that covers the whole profile. Trim the profile to "
            "the part in front of one face (one cut per face), end on a flat copy (translate=True), or "
            "build the cavity from sections with loft_cut.") from exc


def faces_ahead(session, sketch: str, toward, grid: int = 7) -> list:
    """Which faces rays from inside a sketch's contour, along `toward`, hit
    first: [{rays, area_mm2, ahead}] (ahead: how far along
    toward, mm). Only for diagnosing a refused end condition."""
    from . import param
    model = session._require_model()
    mod = session._mod
    feature = session._sketch_by_name(sketch)
    sk = binding.wrap(feature.GetSpecificFeature2(), mod.ISketch)
    segs = [s for s in _segments(session, sk) if not bool(s.ConstructionGeometry)]
    loops = rings([[(m_to_mm(p[0]), m_to_mm(p[1])) for p in _sample_seg(session, s, 48)] for s in segs], 0.01)
    if not loops:
        return []
    ring = max(loops, key=offset_loop.polygon_area)
    origin, u_axis, v_axis = param._sketch_axes(session, sk)
    length = math.sqrt(sum(float(c) ** 2 for c in toward)) or 1.0
    d = [float(c) / length for c in toward]
    us, vs = [p[0] for p in ring], [p[1] for p in ring]
    ext = binding.wrap(model.Extension, mod.IModelDocExtension)
    smg = binding.wrap(model.SelectionManager, mod.ISelectionMgr)
    found = {}
    try:
        for i in range(1, grid + 1):
            for j in range(1, grid + 1):
                uv = (min(us) + (max(us) - min(us)) * i / (grid + 1), min(vs) + (max(vs) - min(vs)) * j / (grid + 1))
                if not offset_loop.point_in_polygon(uv, ring):
                    continue
                # just off the plane: a face lying in it (a cavity floor) is not ahead
                start = [c + 0.05 * k for c, k in zip(_model_of(origin, u_axis, v_axis, uv), d)]
                model.ClearSelection2(True)
                if not ext.SelectByRay(*(mm_to_m(c) for c in start), *d, mm_to_m(0.5), SW_SEL_FACES, False, 0, 0):
                    continue
                face = binding.wrap(smg.GetSelectedObject6(1, -1), mod.IFace2)
                hit = [m_to_mm(c) for c in list(smg.GetSelectionPoint2(1, -1))[:3]]
                along = sum((h - s0) * c for h, s0, c in zip(hit, start, d))
                key = round(face.GetArea() * 1e6, 1)
                entry = found.setdefault(key, {"rays": 0, "area_mm2": key, "along": []})
                entry["rays"] += 1
                entry["along"].append(along)
    finally:
        model.ClearSelection2(True)
    out = []
    for entry in sorted(found.values(), key=lambda e: -e["rays"]):
        out.append({"rays": entry["rays"], "area_mm2": entry["area_mm2"],
                    "ahead": f"{min(entry['along']):.1f}..{max(entry['along']):.1f} mm ahead"})
    return out
