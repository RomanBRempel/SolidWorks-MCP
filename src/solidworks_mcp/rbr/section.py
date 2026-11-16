"""Sections (S-04): the exact outline of a cut through a part, and a picture of it.

section_profile cuts COPIES of the bodies (IBody2.Copy + Operations2 with a box
whose one face lies in the section plane) and reads the faces the cut leaves in
that plane: their loops are the section's contours, with SolidWorks' own
curves - lines, arcs with centre and radius, splines as points - so a wall is
measured on real geometry, not on a picture or a mesh. The part is not
changed: no feature, no selection, no rebuild.

Wall thickness is measured where it is defined: at the middle of each contour
segment, across the material along the segment's normal, to the next boundary
of the same region. A hollow section gives the wall between the outer and the
inner contour; a U-shaped one (a shell cut through its open side) the wall
between the two sides of one contour.

section_view is render.py's picture with everything on the normal's side of
the plane left out and the cut painted.
"""

import math
import time

from .. import binding
from ..errors import SolidWorksError
from . import fastpath, geom, render

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

SWBODY_CUT = 15902                # swBodyOperationType_e (session._BODY_OPERATIONS 'subtract')
CURVE_LINE, CURVE_CIRCLE = 3001, 3002   # ICurve.Identity
CHORD_MM = 0.001                  # polyline accuracy of arcs and splines
_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


# --- pure geometry (unit-tested) ---------------------------------------------------------------


def parse_normal(normal) -> tuple:
    """A unit vector from 'x' / '-y' / '+z' or [nx, ny, nz]; pure."""
    if isinstance(normal, str):
        key = normal.strip().lower()
        sign = -1.0 if key.startswith("-") else 1.0
        axis = key.lstrip("+-")
        if axis not in _AXES:
            raise SolidWorksError(f"normal is 'x', 'y', 'z' (with a sign) or [nx, ny, nz] (got {normal!r}).")
        return tuple(sign * c for c in _AXES[axis])
    try:
        vector = [float(c) for c in normal]
    except (TypeError, ValueError):
        raise SolidWorksError(f"normal is 'x', 'y', 'z' (with a sign) or [nx, ny, nz] (got {normal!r}).") from None
    length = math.sqrt(sum(c * c for c in vector)) if len(vector) == 3 else 0.0
    if not length:
        raise SolidWorksError(f"normal needs three components that are not all zero (got {normal!r}).")
    return tuple(c / length for c in vector)


def plane_frame(point_mm, normal) -> dict:
    """The section plane's frame: origin (the point), unit normal, and u, v - the
    picture's right and up when looking at the cut from the normal's side; pure."""
    if len(point_mm) != 3:
        raise SolidWorksError(f"point_mm needs [x, y, z] (got {point_mm!r}).")
    n = parse_normal(normal)
    u, v, _ = render.view_axes(n)
    return {"origin": tuple(float(c) for c in point_mm), "normal": n, "u": tuple(u), "v": tuple(v)}


def to_uv(frame: dict, point_mm) -> tuple:
    d = [p - o for p, o in zip(point_mm, frame["origin"])]
    return (sum(a * b for a, b in zip(d, frame["u"])), sum(a * b for a, b in zip(d, frame["v"])))


def to_model(frame: dict, uv) -> list:
    return [o + uv[0] * a + uv[1] * b for o, a, b in zip(frame["origin"], frame["u"], frame["v"])]


def arc_points(centre, radius: float, start, mid, end, chord: float = CHORD_MM) -> list:
    """Points (2D) along the arc from start through mid to end, the chord error below `chord`;
    start == end gives the full circle. Pure."""
    a0 = math.atan2(start[1] - centre[1], start[0] - centre[0])
    full = math.dist(start, end) < 1e-9
    if full:
        sweep = 2 * math.pi
    else:
        am = math.atan2(mid[1] - centre[1], mid[0] - centre[0])
        a1 = math.atan2(end[1] - centre[1], end[0] - centre[0])
        ccw = (a1 - a0) % (2 * math.pi)
        sweep = ccw if (am - a0) % (2 * math.pi) <= ccw else ccw - 2 * math.pi
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - chord / radius))) if radius > chord else math.pi / 8
    count = max(2, int(math.ceil(abs(sweep) / max(step, 1e-6))))
    return [(centre[0] + radius * math.cos(a0 + sweep * k / count),
             centre[1] + radius * math.sin(a0 + sweep * k / count)) for k in range(count + 1)]


def chain(polylines: list, tolerance: float = 1e-4) -> tuple:
    """Order a loop's edge polylines head to tail (reversing where needed).
    Returns (ordered polylines, order, reversed flags); pure."""
    if not polylines:
        return [], [], []
    left = list(range(1, len(polylines)))
    order, flipped, out = [0], [False], [list(polylines[0])]
    while left:
        tail = out[-1][-1]
        best = min(((min(math.dist(tail, polylines[i][0]), math.dist(tail, polylines[i][-1])), i) for i in left))
        i = best[1]
        left.remove(i)
        reverse = math.dist(tail, polylines[i][-1]) < math.dist(tail, polylines[i][0])
        out.append(list(reversed(polylines[i])) if reverse else list(polylines[i]))
        order.append(i)
        flipped.append(reverse)
    if len(out) > 1 and math.dist(out[0][0], out[1][0]) < math.dist(out[0][-1], out[1][0]) - tolerance:
        out[0].reverse()  # the first edge ran against the loop
        flipped[0] = True
    return out, order, flipped


def polygon_area(points) -> float:
    """Signed area of a closed polyline (counter-clockwise positive); pure."""
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(points, list(points[1:]) + [points[0]]))


def inside(polygon, point) -> bool:
    """Even-odd point-in-polygon; pure."""
    x, y = point
    hit = False
    for (x1, y1), (x2, y2) in zip(polygon, list(polygon[1:]) + [polygon[0]]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def wall_at(point, direction, starts, ends):
    """Distance from `point` along `direction` to the nearest boundary segment
    (arrays of segment starts and ends), or None; numpy."""
    d = np.asarray(direction, dtype=np.float64)
    e = ends - starts
    den = d[0] * e[:, 1] - d[1] * e[:, 0]
    rel = starts - np.asarray(point, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (rel[:, 0] * e[:, 1] - rel[:, 1] * e[:, 0]) / den
        s = (rel[:, 0] * d[1] - rel[:, 1] * d[0]) / den
    ok = (np.abs(den) > 1e-15) & (t > 1e-6) & (s >= -1e-9) & (s <= 1 + 1e-9)
    return float(t[ok].min()) if ok.any() else None


def measure_walls(loops: list) -> None:
    """Add wall_mm to every segment of a region's loops (see the module doc) and
    return nothing; loops: [{outer, polygon, segments: [{polyline}]}]. Needs numpy."""
    if np is None:
        return
    outer = [lp["polygon"] for lp in loops if lp["outer"]]
    holes = [lp["polygon"] for lp in loops if not lp["outer"]]
    points = [p for lp in loops for p in lp["polygon"]]
    starts, ends = [], []
    for lp in loops:
        poly = lp["polygon"]
        starts += poly
        ends += list(poly[1:]) + [poly[0]]
    starts, ends = np.asarray(starts, dtype=np.float64), np.asarray(ends, dtype=np.float64)
    if not len(points):
        return

    def in_material(q) -> bool:
        return any(inside(o, q) for o in outer) and not any(inside(h, q) for h in holes)

    for lp in loops:
        for seg in lp["segments"]:
            line = seg["polyline"]
            picks = [len(line) // 2] if seg["type"] == "line" or len(line) < 5 else \
                [len(line) // 4, len(line) // 2, 3 * len(line) // 4]
            best = None
            for k in picks:
                if seg["type"] == "line":
                    a, b = line[0], line[-1]
                    p = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                else:
                    a, b = line[max(0, k - 1)], line[min(len(line) - 1, k + 1)]
                    p = line[k]
                length = math.dist(a, b)
                if length < 1e-12:
                    continue
                nx, ny = -(b[1] - a[1]) / length, (b[0] - a[0]) / length
                for sx, sy in ((nx, ny), (-nx, -ny)):
                    if in_material((p[0] + sx * 1e-3, p[1] + sy * 1e-3)):
                        t = wall_at(p, (sx, sy), starts, ends)
                        if t is not None and (best is None or t < best[0]):
                            best = (t, p)
                        break
            if best is not None:
                seg["wall_mm"] = round(best[0], 4)
                seg["wall_at"] = best[1]


# --- SolidWorks side ----------------------------------------------------------------------------


def _vec(values):
    import pythoncom
    import win32com.client
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def _bodies(session, model, bodies) -> list:
    if int(model.GetType()) != render.SW_DOC_PART:
        raise SolidWorksError("section_profile reads a part; open the part of the assembly you want to cut "
                              "(section_screenshot draws assemblies too).")
    if bodies is None:
        part = binding.wrap(model, session._mod.IPartDoc)
        found = part.GetBodies2(render.SW_BODY_SOLID, True) or ()
        if not isinstance(found, (list, tuple)):
            found = [found]
        out = [binding.wrap(b, session._mod.IBody2) for b in found]
    else:
        out = [geom.resolve_body(session, model, b) for b in ([bodies] if isinstance(bodies, str) else bodies)]
    if not out:
        raise SolidWorksError(f"'{model.GetTitle()}' has no visible solid body to cut.")
    return out


def _edge_segment(session, raw_edge, frame: dict) -> dict:
    """One edge of a section loop: its type and data in the plane's (u, v), mm."""
    mod = session._mod
    edge = fastpath._bind(raw_edge, mod.IEdge)
    curve = binding.wrap(edge.GetCurve(), mod.ICurve)
    params = edge.GetCurveParams2()
    start = to_uv(frame, [c * 1000.0 for c in params[0:3]])
    end = to_uv(frame, [c * 1000.0 for c in params[3:6]])
    kind = int(curve.Identity())
    length = float(curve.GetLength3(params[6], params[7])) * 1000.0
    seg = {"length_mm": round(length, 4)}
    if kind == CURVE_LINE:
        seg.update(type="line", polyline=[start, end])
    elif kind == CURVE_CIRCLE:
        cp = curve.CircleParams
        centre = to_uv(frame, [c * 1000.0 for c in cp[0:3]])
        radius = float(cp[6]) * 1000.0
        full = math.dist(start, end) < 1e-7
        mid = start
        if not full:
            middle = curve.Evaluate2((params[6] + params[7]) / 2.0, 0)
            mid = to_uv(frame, [c * 1000.0 for c in middle[0:3]])
        seg.update(type="circle" if full else "arc", centre_mm=[round(c, 5) for c in centre],
                   radius_mm=round(radius, 5), polyline=arc_points(centre, radius, start, mid, end))
    else:
        flat = list(curve.GetTessPts(CHORD_MM / 1000.0, 0.002, _vec(params[0:3]), _vec(params[3:6])) or ())
        pts = [to_uv(frame, [c * 1000.0 for c in flat[k:k + 3]]) for k in range(0, len(flat) - 2, 3)]
        if len(pts) < 2:
            pts = [start, end]
        seg.update(type="spline", polyline=pts, points=len(pts))
    return seg


def _section_faces(session, piece, frame: dict) -> list:
    """The faces of a cut piece that lie in the section plane, facing the normal."""
    mod = session._mod
    origin_m = [c / 1000.0 for c in frame["origin"]]
    out = []
    for raw_face in render._raw(piece).GetFaces() or ():
        face = fastpath._bind(raw_face, mod.IFace2)
        surface = binding.wrap(face.GetSurface(), mod.ISurface)
        if surface is None or not surface.IsPlane():
            continue
        pp = surface.PlaneParams  # normal xyz, root point xyz (m)
        if abs(abs(sum(a * b for a, b in zip(pp[0:3], frame["normal"]))) - 1.0) > 1e-9:
            continue
        if abs(sum((p - o) * n for p, o, n in zip(pp[3:6], origin_m, frame["normal"]))) > 1e-7:
            continue
        out.append(face)
    return out


def _region(session, face, frame: dict) -> dict:
    mod = session._mod
    loops = []
    for raw_loop in render._raw(face).GetLoops() or ():
        loop = fastpath._bind(raw_loop, mod.ILoop2)
        segments = [_edge_segment(session, e, frame) for e in (render._raw(loop).GetEdges() or ())]
        ordered, order, flipped = chain([s["polyline"] for s in segments])
        segments = [segments[i] for i in order]
        polygon = []
        for seg, line in zip(segments, ordered):
            seg["polyline"] = line
            polygon += line[:-1]
        loops.append({"outer": bool(loop.IsOuter()), "polygon": polygon, "segments": segments})
    measure_walls(loops)
    area = float(face.GetArea()) * 1e6
    out_loops, walls = [], []
    for lp in loops:
        us, vs = [p[0] for p in lp["polygon"]], [p[1] for p in lp["polygon"]]
        entries = []
        for seg in lp["segments"]:
            line = seg["polyline"]
            entry = {"type": seg["type"], "start_mm": [round(c, 4) for c in to_model(frame, line[0])],
                     "end_mm": [round(c, 4) for c in to_model(frame, line[-1])],
                     "start_uv": [round(c, 4) for c in line[0]], "end_uv": [round(c, 4) for c in line[-1]],
                     "length_mm": seg["length_mm"]}
            if "radius_mm" in seg:
                entry.update(radius_mm=seg["radius_mm"], centre_uv=seg["centre_mm"],
                             centre_mm=[round(c, 4) for c in to_model(frame, seg["centre_mm"])])
            if seg["type"] == "spline":
                step = max(1, len(line) // 24)
                entry["points_uv"] = [[round(c, 4) for c in p] for p in line[::step]]
            if "wall_mm" in seg:
                entry["wall_mm"] = seg["wall_mm"]
                walls.append((seg["wall_mm"], seg["wall_at"]))
            entries.append(entry)
        out_loops.append({"outer": lp["outer"], "segments": entries,
                          "length_mm": round(sum(s["length_mm"] for s in lp["segments"]), 4),
                          "area_mm2": round(abs(polygon_area(lp["polygon"])), 4),
                          "box_uv": [[round(min(us), 4), round(min(vs), 4)], [round(max(us), 4), round(max(vs), 4)]]})
    region = {"area_mm2": round(area, 4), "contours": out_loops}
    if walls:
        thinnest = min(walls, key=lambda w: w[0])
        region["min_wall_mm"] = thinnest[0]
        region["min_wall_at_mm"] = [round(c, 4) for c in to_model(frame, thinnest[1])]
        region["max_wall_mm"] = max(w[0] for w in walls)
    return region


def section_profile(session, point_mm, normal, bodies=None, document: str | None = None) -> dict:
    """The contours of the section of a part by the plane through point_mm with the given normal."""
    t0 = time.perf_counter()
    frame = plane_frame(point_mm, normal)
    model = render._document(session, document)
    mod = session._mod
    modeler = binding.wrap(session._sw.GetModeler(), mod.IModeler)
    origin_m = [c / 1000.0 for c in frame["origin"]]
    n = frame["normal"]
    out = []
    for body in _bodies(session, model, bodies):
        box = body.GetBodyBox()
        corners = [(box[i], box[j], box[k]) for i in (0, 3) for j in (1, 4) for k in (2, 5)]
        sides = [sum((c - o) * a for c, o, a in zip(corner, origin_m, n)) for corner in corners]
        if min(sides) > 1e-9 or max(sides) < -1e-9:
            continue  # the plane misses this body
        centre = [(box[i] + box[i + 3]) / 2 for i in range(3)]
        offset = sum((c - o) * a for c, o, a in zip(centre, origin_m, n))
        foot = [c - offset * a for c, a in zip(centre, n)]  # the body's middle, dropped onto the plane
        size = 2.0 * math.dist(box[0:3], box[3:6]) + 0.01
        tool = modeler.CreateBodyFromBox3(_vec([*foot, *n, size, size, size]))
        if tool is None:
            raise SolidWorksError("SolidWorks made no cutting box (IModeler.CreateBodyFromBox3).")
        copy = binding.wrap(geom._temp_copy(body), mod.IBody2)
        pieces = [binding.wrap(p, mod.IBody2) for p in geom._pieces(copy.Operations2(SWBODY_CUT, tool))]
        regions = [_region(session, face, frame) for piece in pieces for face in _section_faces(session, piece, frame)]
        if regions:
            entry = {"body": body.Name, "regions": regions,
                     "area_mm2": round(sum(r["area_mm2"] for r in regions), 4)}
            walls = [r["min_wall_mm"] for r in regions if "min_wall_mm" in r]
            if walls:
                entry["min_wall_mm"] = min(walls)
            out.append(entry)
    if not out:
        raise SolidWorksError(
            f"The plane through {list(point_mm)} with normal {[round(c, 6) for c in n]} cuts no solid body of "
            f"'{model.GetTitle()}' (get_bounding_box shows where the part lies).")
    result = {"ok": True, "document": model.GetTitle(),
              "plane": {"point_mm": list(frame["origin"]), "normal": [round(c, 9) for c in n],
                        "u_axis": [round(c, 9) for c in frame["u"]], "v_axis": [round(c, 9) for c in frame["v"]]},
              "bodies": out, "area_mm2": round(sum(b["area_mm2"] for b in out), 4),
              "note": "uv: millimetres in the plane from point_mm along u_axis and v_axis (right and up in "
                      "section_screenshot). wall_mm: the material's thickness at the middle of a segment, "
                      "measured along its normal.",
              "elapsed_s": round(time.perf_counter() - t0, 2)}
    walls = [b["min_wall_mm"] for b in out if "min_wall_mm" in b]
    if walls:
        result["min_wall_mm"] = min(walls)
    return result


def section_view(session, point_mm, normal, zoom_mm=None, path: str | None = None, from_dir=None,
                 width: int = 1200, height: int = 800, document: str | None = None, bodies=None) -> dict:
    """A picture of the part or assembly cut by the plane: what lies on the normal's side is
    left out, the cut is painted. Looks at the cut along the normal unless from_dir is given."""
    import os
    render._need_numpy()
    t0 = time.perf_counter()
    frame = plane_frame(point_mm, normal)
    model = render._document(session, document)
    groups = render.collect(session, model, bodies)
    look = tuple(from_dir) if from_dir is not None else frame["normal"]
    if sum(a * b for a, b in zip(look, frame["normal"])) <= 0:
        raise SolidWorksError("from_dir looks at the cut from behind: give a direction on the normal's side "
                              "(or flip the normal to see the other half).")
    width, height = max(64, min(int(width), 4000)), max(64, min(int(height), 4000))
    image, info = render.draw(groups, look, width, height, zoom_mm, clip=(frame["origin"], frame["normal"]))
    out = os.path.abspath(path) if path else render.default_path(model.GetTitle(), "section")
    if not out.lower().endswith(".png"):
        raise SolidWorksError("The section picture is a PNG: give a path ending in .png.")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    data = render.png_bytes(image)
    with open(out, "wb") as fh:
        fh.write(data)
    if info["covered_pct"] <= 0:
        raise SolidWorksError("The section picture came out empty: the plane leaves nothing on the far side "
                              "of the normal, or zoom_mm lies off the model.")
    return {"ok": True, "path": out, "format": "png", "bytes": len(data), "mode": "render",
            "document": model.GetTitle(),
            "plane": {"point_mm": list(frame["origin"]), "normal": [round(c, 9) for c in frame["normal"]],
                      "u_axis": [round(c, 9) for c in frame["u"]], "v_axis": [round(c, 9) for c in frame["v"]]},
            "size_px": [int(image.shape[1]), int(image.shape[0])], "groups": len(groups), **info,
            "elapsed_s": round(time.perf_counter() - t0, 2)}
