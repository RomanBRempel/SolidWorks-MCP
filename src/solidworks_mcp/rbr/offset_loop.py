"""offset_face_loop (A3): a closed contour a wall's thickness inside a face,
also where SolidWorks' own closed offset refuses.

Why SolidWorks refuses: Offset Entities on the whole loop of a profile's end
face fails when the material is anywhere thinner than two offsets (0.5 mm at
a thin edge against 2 x 3 mm) - the true offset folds over itself.

What works (verified on SW 2025, 04.11.2026):
- ISketchManager.SketchOffset2 on ONE preselected segment offsets it, with its
  own dimension (IModelDoc2.SketchOffsetEntities2 answers False and does
  nothing for sketch entities).
- The offsets then cross each other, and SolidWorks makes sketch REGIONS of
  everything that closes - also of crossing, untrimmed curves. The region
  bounded by offsets alone is the cavity contour.
- Offset (and converted) curves cannot be split or trimmed: SolidWorks keeps
  them whole for their relation. So nothing is trimmed: the tails stay, the
  offset relations stay, and every offset dimension can follow a global
  variable - a changed wall rebuilds the contour.
- FeatureCut4 / FeatureExtrusion3 take that region when it alone is selected
  through ISelectData with Mark = 4 (features.py does that).

Pure helpers (chain_polylines, interior_point, polygon_area) are unit-tested.
"""

import math

from .. import binding
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m
from . import sketch_edit as se

MARK_PROFILE = 4          # ISelectData.Mark a region needs for FeatureCut4 / FeatureExtrusion3
_WALL_TOLERANCE = 0.02    # an edge is "at the wall" within 2 % of the distance
_TESS_M = 1e-4            # chord and length tolerance for edge tessellation


# --- pure geometry -----------------------------------------------------------------


def point_in_polygon(point, polygon) -> bool:
    """Even-odd rule; pure, unit-tested."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def chain_polylines(pieces, tol: float) -> list:
    """Join polylines [[(x, y), ...], ...] end to end into one closed ring,
    reversing pieces as needed; pure, unit-tested. Raises when they do not chain."""
    if not pieces:
        return []
    rest = [list(p) for p in pieces if p]
    ring = rest.pop(0)
    while rest:
        tail = ring[-1]
        for i, piece in enumerate(rest):
            if math.dist(piece[0], tail) <= tol:
                ring.extend(piece[1:])
                rest.pop(i)
                break
            if math.dist(piece[-1], tail) <= tol:
                ring.extend(reversed(piece[:-1]))
                rest.pop(i)
                break
        else:
            raise SolidWorksError("The region's edges do not chain into one loop.")
    if len(ring) > 1 and math.dist(ring[0], ring[-1]) <= tol:
        ring.pop()
    return ring


def polygon_area(polygon) -> float:
    """Absolute shoelace area; pure, unit-tested."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def interior_point(polygon, grid: int = 24):
    """A point well inside the polygon (the grid point farthest from its
    boundary samples); pure, unit-tested. None for a degenerate polygon."""
    if len(polygon) < 3:
        return None
    xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
    best, best_gap = None, -1.0
    for i in range(1, grid):
        for j in range(1, grid):
            p = (min(xs) + (max(xs) - min(xs)) * i / grid, min(ys) + (max(ys) - min(ys)) * j / grid)
            if not point_in_polygon(p, polygon):
                continue
            gap = min(_segment_distance(p, a, b) for a, b in zip(polygon, polygon[1:] + polygon[:1]))
            if gap > best_gap:
                best, best_gap = p, gap
    return best


def _segment_distance(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


# --- SolidWorks side ---------------------------------------------------------------


def _ends_m(session, segment) -> tuple:
    """((x1, y1), (x2, y2)) of an open sketch segment, sketch space, m."""
    kind = int(segment.GetType())
    mod = session._mod
    if kind in (0, 1):
        owner = binding.wrap(segment, mod.ISketchLine if kind == 0 else mod.ISketchArc)
        a = binding.wrap(owner.GetStartPoint2(), mod.ISketchPoint)
        b = binding.wrap(owner.GetEndPoint2(), mod.ISketchPoint)
        if a is None or b is None:
            raise SolidWorksError("A full circle has no ends.")
    elif kind == 3:
        pts = [binding.wrap(p, mod.ISketchPoint)
               for p in (binding.wrap(segment, mod.ISketchSpline).GetPoints2() or ())]
        if not pts:
            raise SolidWorksError("A spline without points.")
        a, b = pts[0], pts[-1]
    else:
        raise SolidWorksError(f"A sketch segment of type {kind} is not handled here.")
    return (a.X, a.Y), (b.X, b.Y)


def _vec(values):
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def _edge_polyline_model(session, edge) -> list:
    """Tessellated points (model space, m) of a region edge, start to end."""
    params = list(edge.GetCurveParams2() or ())
    curve = binding.wrap(edge.GetCurve(), session._mod.ICurve)
    if curve is None or len(params) < 6:
        raise SolidWorksError("A sketch-region edge has no curve to follow.")
    flat = list(curve.GetTessPts(_TESS_M, _TESS_M, _vec(params[0:3]), _vec(params[3:6])) or ())
    if len(flat) < 6:
        return [tuple(params[0:3]), tuple(params[3:6])]
    return [tuple(flat[k:k + 3]) for k in range(0, len(flat) - 2, 3)]


def _to_sketch(session, sketch, point_m) -> tuple:
    local = session._sketch_coords(sketch, *point_m)
    return float(local[0]), float(local[1])


def region_polygons(session, sketch) -> list:
    """Every region of a (closed) sketch: {index, region, polygon (sketch m),
    edges: [sketch-space polylines]}."""
    out = []
    for index, raw in enumerate(sketch.GetSketchRegions() or ()):
        region = binding.wrap(raw, session._mod.ISketchRegion)
        edges = []
        for raw_edge in region.GetEdges() or ():
            model_pts = _edge_polyline_model(session, binding.wrap(raw_edge, session._mod.IEdge))
            edges.append([_to_sketch(session, sketch, p) for p in model_pts])
        try:
            polygon = chain_polylines(edges, 1e-6)
        except SolidWorksError:
            polygon = [p for edge in edges for p in edge]
        out.append({"index": index, "region": region, "polygon": polygon, "edges": edges})
    return out


def _sketch_segments(session, sketch) -> list:
    return [binding.wrap(s, session._mod.ISketchSegment) for s in (sketch.GetSketchSegments() or ())]


def _distance_to_curves(session, segments, point) -> float:
    """min distance (m) from a sketch point to the segments' curves. The ICurve
    is fetched afresh each time: one kept across sketch edits is disconnected
    ('the object invoked has disconnected from its clients')."""
    best = math.inf
    for seg in segments:
        curve = binding.wrap(seg.GetCurve(), session._mod.ICurve)
        near = curve.GetClosestPointOn(point[0], point[1], 0.0)
        best = min(best, math.dist(point, (near[0], near[1])))
    return best


def wall_region(session, sketch, original_segments, d_m: float):
    """The region bounded by offset curves alone: each of its edges lies d_m
    from the original loop. The largest such region, or None."""
    found = []
    for entry in region_polygons(session, sketch):
        at_wall = True
        for edge in entry["edges"]:
            inner = edge[1:-1] or edge
            for p in inner[:: max(1, len(inner) // 6)]:
                if abs(_distance_to_curves(session, original_segments, p) - d_m) > _WALL_TOLERANCE * d_m:
                    at_wall = False
                    break
            if not at_wall:
                break
        if at_wall and len(entry["polygon"]) >= 3:
            found.append(entry)
    if not found:
        return None
    return max(found, key=lambda e: polygon_area(e["polygon"]))


def region_at(session, sketch, point_sketch_m):
    """The smallest region of a sketch that contains a sketch-space point, or None."""
    inside = [e for e in region_polygons(session, sketch) if point_in_polygon(point_sketch_m, e["polygon"])]
    return min(inside, key=lambda e: polygon_area(e["polygon"])) if inside else None


def _loop_edges(session, face):
    mod = session._mod
    for raw in face.GetLoops() or ():
        loop = binding.wrap(raw, mod.ILoop2)
        if bool(loop.IsOuter()):
            edges = [binding.wrap(e, mod.IEdge) for e in (loop.GetEdges() or ())]
            if edges:
                return edges
    raise SolidWorksError("The face has no outer loop to offset.")


def _sample(session, segment, count: int = 8) -> list:
    """Points (sketch m) along a sketch segment, from its curve's tessellation."""
    curve = binding.wrap(segment.GetCurve(), session._mod.ICurve)
    (a, b) = _ends_m(session, segment)
    flat = list(curve.GetTessPts(_TESS_M, _TESS_M, _vec([*a, 0.0]), _vec([*b, 0.0])) or ())
    pts = [(flat[k], flat[k + 1]) for k in range(0, len(flat) - 2, 3)] or [a, b]
    step = max(1, len(pts) // count)
    return pts[::step] + [pts[-1]]


def _newest_dimension(session, feature) -> str | None:
    names = [e["name"] for e in session._dimension_entries(feature)]
    return names[-1] if names else None


def _delete(model, segments) -> None:
    model.ClearSelection2(True)
    if any([s.Select4(True, None) for s in segments]):
        model.EditDelete()
    model.ClearSelection2(True)


def offset_face_loop(session, face: str, distance_mm: float, inward: bool = True,
                     link_to: str | None = None, name: str | None = None) -> dict:
    """A closed contour distance_mm inside (or outside) a planar face's outer edge
    loop, as a new sketch on that face, its offsets dimensioned - also where
    SolidWorks' own closed offset refuses because the material is thinner than
    two offsets.

    face: '#index' from list_faces or a direction such as '+x' / '-z:inner'.
    The loop's edges are converted (construction), each is offset on its own,
    and an offset lying wholly nearer the loop than distance_mm (a collapsed
    thin edge) is deleted. The contour is the sketch REGION the offsets
    close between their crossings; region_point_mm (model space) picks it for
    cut / extrude. link_to names an existing global variable that drives every
    offset dimension.
    """
    from . import tools as rbr_tools
    if distance_mm <= 0:
        raise SolidWorksError(f"distance_mm must be > 0 (got {distance_mm:g}).")
    model = session._require_model()
    session._require_part()
    mod = session._mod
    d_m = mm_to_m(distance_mm)
    if link_to:
        known = {(e.get("name") or "").lower() for e in session._equation_entries() if e.get("global")}
        if link_to.lower() not in known:
            raise SolidWorksError(f'No global variable "{link_to}": add it first (set_global).')
    target = rbr_tools._face_for(session, face)
    edges = _loop_edges(session, binding.wrap(target, mod.IFace2))

    names_before = {f.Name for f in session._iter_features()}
    sk = binding.wrap(model.SketchManager, mod.ISketchManager)
    model.ClearSelection2(True)
    if not binding.wrap(target, mod.IEntity).Select4(False, None):
        raise SolidWorksError("Could not select the face.")
    sk.InsertSketch(True)
    sketch = binding.wrap(model.GetActiveSketch2(), mod.ISketch)
    feature = next((f for f in session._iter_features()
                    if f.Name not in names_before and f.GetTypeName2() == "ProfileFeature"), None)
    if sketch is None or feature is None:
        raise SolidWorksError("No sketch opened on the face.")
    dims, dropped = [], 0
    try:
        before = {se._seg_key(s) for s in _sketch_segments(session, sketch)}
        model.ClearSelection2(True)
        picked = sum(1 for e in edges if binding.wrap(e, mod.IEntity).Select4(True, None))
        if picked != len(edges) or not sk.SketchUseEdge3(False, False):
            raise SolidWorksError("Converting the face's edges (SketchUseEdge3) failed.")
        originals = [s for s in _sketch_segments(session, sketch) if se._seg_key(s) not in before]
        polygon = [p for s in originals for p in _sample(session, s, 16)]
        curves = originals

        sign = 1.0
        for seg in originals:
            made = None
            for _attempt in range(2):
                made = _offset_one(session, model, sk, sketch, seg, sign * d_m, polygon, inward)
                if made is not None:
                    break
                sign = -sign  # the side flips with the segment's direction; keep the last good sign
            if made is None:
                raise SolidWorksError(f"SolidWorks could not offset a {m_to_mm(seg.GetLength()):.3f} mm "
                                      f"segment of the loop to the {'inside' if inward else 'outside'}.")
            if all(_distance_to_curves(session, curves, p) < d_m * (1 - _WALL_TOLERANCE)
                   for s in made for p in _sample(session, s)):
                _delete(model, made)  # collapsed: lies nearer the loop than the wall everywhere
                dropped += 1
                continue
            dims.append(_newest_dimension(session, feature))
        for seg in originals:
            seg.ConstructionGeometry = True
    finally:
        model.ClearSelection2(True)
        sk.InsertSketch(True)

    closed_sketch = binding.wrap(feature.GetSpecificFeature2(), mod.ISketch)
    region = wall_region(session, closed_sketch, curves_of(session, closed_sketch), d_m)
    if name:
        old = feature.Name
        feature.Name = name
        if feature.Name != name:
            raise SolidWorksError(f"The sketch could not be named '{name}' (it is '{feature.Name}'): name taken?")
        dims = [d.replace(f"@{old}", f"@{name}") if d else d for d in dims]
    equations = []
    if link_to and dims:
        equations = [f'"{dim}" = "{link_to}"' for dim in dims if dim]
        session.set_equations(equations)
    result = {"ok": region is not None, "sketch": feature.Name, "offsets": len(dims),
              "dropped_offsets": dropped, "offset_dimensions": dims, "equations": equations}
    if region is None:
        result["error"] = (f"The offsets close no region {distance_mm:g} mm inside the face: the face is "
                           "too small for that wall, or a concave corner leaves a gap.")
        return result
    point = interior_point(region["polygon"])
    inv = _sketch_to_model(session, closed_sketch, point)
    result.update(region_index=region["index"],
                  region_area_mm2=round(polygon_area(region["polygon"]) * 1e6, 3),
                  region_point_mm=[round(m_to_mm(c), 4) for c in inv],
                  hint="cut_region(sketch, region_point_mm, depth_mm=..., toward=[...]) cuts the cavity.")
    return result


def curves_of(session, sketch) -> list:
    """The construction segments of a closed sketch: the converted loop."""
    return [s for s in _sketch_segments(session, sketch) if bool(s.ConstructionGeometry)]


def _sketch_to_model(session, sketch, point_m) -> list:
    mod = session._mod
    xform = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, mod.IMathTransform).Inverse(),
                         mod.IMathTransform)
    mu = binding.wrap(session._sw.GetMathUtility(), mod.IMathUtility)
    p = binding.wrap(mu.CreatePoint(_vec([point_m[0], point_m[1], 0.0])), mod.IMathPoint)
    return list(binding.wrap(p.MultiplyTransform(xform), mod.IMathPoint).ArrayData)


def _offset_one(session, model, sk, sketch, seg, d_m, polygon, inward):
    """Offset one segment with its dimension; the new segments, or None when
    the offset is refused or lands on the wrong side (then deleted)."""
    before = {se._seg_key(s) for s in _sketch_segments(session, sketch)}
    model.ClearSelection2(True)
    if not seg.Select4(False, None):
        raise SolidWorksError("Could not select a loop segment to offset.")
    sk.SketchOffset2(d_m, False, False, 0, 0, True)
    new = [s for s in _sketch_segments(session, sketch) if se._seg_key(s) not in before]
    if not new:
        return None
    mid = _sample(session, new[0], 2)[1]
    if point_in_polygon(mid, polygon) != inward:
        _delete(model, new)
        return None
    return new
