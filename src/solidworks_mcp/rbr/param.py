"""Parametric building blocks on SolidWorks objects (B1, B2, B3, B6, B7).

The TORUK rev2 rule: everything rests on faces, edges, planes and sketches;
sizes and positions are global variables or equations; no inserted tool
bodies. These tools make that possible through the server:

B1  set_global, link_dimension, feature_dimensions (full names: D1@PL_X550).
B2  sketch_circles / sketch_rect: a sketch on a plane or face, fully defined
    by dimensions from the origin, each optionally driven by a variable.
B3  convert_entities, add_relation, and the midline centre: a circle centred
    on the middle of a construction chord between the two sides of a face
    profile at a station - the centre of a hole on the profile's mean line,
    tied by relations so it follows the profile.
B6  plane_normal_to_line: a plane perpendicular to a sketch line through
    one of its end points (a hinge hole axis at 23.8 deg).
B7  mirror_features: upstream add_mirror with feature names and any plane.

Values from swconst.tlb of SOLIDWORKS 2025. Pure helpers are unit-tested.
"""

import math
import re

from .. import binding
from ..constants import SW_FULLY_CONSTRAINED
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m

# swConstraintType_e
RELATIONS = {"horizontal": 4, "vertical": 5, "tangent": 6, "parallel": 7, "perpendicular": 8,
             "coincident": 9, "on_edge": 9, "concentric": 10, "midpoint": 12, "at_intersection": 13,
             "fixed": 17, "horizontal_points": 25, "vertical_points": 26, "collinear": 27, "coradial": 28}
# swRefPlaneReferenceConstraints_e
REF_PLANE_PERPENDICULAR = 2
REF_PLANE_COINCIDENT = 4
_TEXT_OFFSET_M = 0.002


# --- pure helpers ------------------------------------------------------------------


def quote_names(expression, names) -> str:
    """Quote the global-variable names an expression uses bare; pure, unit-tested.

    'TIP_WALL * 2' -> '"TIP_WALL" * 2' when TIP_WALL is a known name; a number
    stays a number; already quoted names and function names (abs, sin) stay."""
    text = str(expression).strip()
    if not names:
        return text
    known = {n.lower(): n for n in names}
    parts = re.split(r'("[^"]*")', text)  # leave quoted runs alone
    out = []
    for part in parts:
        if part.startswith('"'):
            out.append(part)
            continue
        out.append(re.sub(r"[A-Za-z_Ѐ-ӿ][\wЀ-ӿ]*",
                          lambda m: f'"{known[m.group(0).lower()]}"' if m.group(0).lower() in known else m.group(0),
                          part))
    return "".join(out)


def axis_role(u_axis, v_axis) -> dict:
    """Which model axis ('X', 'Y', 'Z') each sketch axis runs along; pure.

    u_axis / v_axis: the sketch's x and y directions in model space. An axis
    not along a model axis maps to None (its dimension has no model meaning)."""
    def name(vector):
        for i, label in enumerate("XYZ"):
            if abs(abs(vector[i]) - 1.0) < 1e-6:
                return label
        return None
    return {"x": name(u_axis), "y": name(v_axis)}


def station_dimension_axis(chord_dir_sketch) -> str:
    """The sketch dimension that places a chord running along chord_dir_sketch:
    a vertical chord is placed by its x (horizontal dimension) and vice versa;
    pure, unit-tested."""
    du, dv = chord_dir_sketch
    if abs(du) < 1e-9 and abs(dv) > 0:
        return "x"
    if abs(dv) < 1e-9 and abs(du) > 0:
        return "y"
    raise SolidWorksError("The station chord does not run along a sketch axis: give a station axis "
                          "that lies in the sketch plane along one of its axes.")


# --- B1: variables and dimensions -------------------------------------------------


def _globals(session) -> list:
    return [e["name"] for e in session._equation_entries() if e.get("global") and e.get("name")]


def set_global(session, name: str, value) -> dict:
    """Add or change a global variable: a number (mm or deg as the document's
    units) or an expression of other variables ('TIP_WALL * 2'). Rebuilds."""
    if not re.match(r"^[A-Za-z_Ѐ-ӿ][\wЀ-ӿ]*$", str(name)):
        raise SolidWorksError(f"A global variable name is letters, digits and _ (got {name!r}).")
    rhs = value if isinstance(value, (int, float)) else quote_names(value, [g for g in _globals(session) if g != name])
    result = session.set_equations([f'"{name}" = {rhs}'])
    entry = next(e for e in session._equation_entries() if (e.get("name") or "").lower() == name.lower())
    return {"ok": True, "name": name, "equation": entry["equation"], "value": entry["value"],
            "rebuild_ok": result["rebuild_ok"], "failing_features": result["failing_features"]}


def link_dimension(session, dimension: str, expression) -> dict:
    """Drive a dimension ('D1@PL_X550', 'diameter@SK_BOSS') by an expression of
    global variables ('TIP_WALL', 'INS_DEPTH + 2', 'abs(INS_Z1)'). Bare known
    variable names are quoted for SolidWorks. Rebuilds and reads the value back."""
    model = session._require_model()
    if model.Parameter(dimension) is None:
        raise SolidWorksError(f"No dimension '{dimension}' (feature_dimensions lists the full names).")
    rhs = quote_names(expression, _globals(session))
    result = session.set_equations([f'"{dimension}" = {rhs}'])
    dim = binding.wrap(model.Parameter(dimension), session._mod.IDimension)
    unit, _to, from_system = session._dimension_unit(dim)
    return {"ok": True, "dimension": dimension, "equation": f'"{dimension}" = {rhs}',
            f"value_{unit}": round(from_system(dim.SystemValue), 6),
            "rebuild_ok": result["rebuild_ok"], "failing_features": result["failing_features"]}


def feature_dimensions(session, feature: str | None = None) -> dict:
    """Every dimension of a feature AND of the sketches it absorbed (or of all
    features), with its full name for link_dimension and the equation driving it."""
    session._require_part()
    driven = {}
    for e in session._equation_entries():
        if e.get("name") and "@" in e["name"]:
            driven[e["name"].lower()] = e["equation"]
    rows, seen, found = [], set(), feature is None
    for feat in session._iter_features():
        family = [feat, *session._sub_features(feat)]
        if feature is not None and feature not in [f.Name for f in family]:
            continue
        found = True
        for member in family:
            for entry in session._dimension_entries(member):
                if entry["name"] in seen:
                    continue
                seen.add(entry["name"])
                entry["equation"] = driven.get(entry["name"].lower())
                rows.append(entry)
    if not found:
        raise SolidWorksError(f"No feature '{feature}' (list_features shows them).")
    return {"ok": True, "feature": feature, "count": len(rows), "dimensions": rows}


# --- sketch plumbing ----------------------------------------------------------------


def _open_sketch_on(session, on: str):
    """Select a plane (front/top/right or a tree plane) or a face ('+x', '#5',
    '@x,y,z') and open a sketch there. Returns (sk, sketch, feature)."""
    from . import features as rbr_features
    model = session._require_model()
    mod = session._mod
    model.ClearSelection2(True)
    target = None
    try:
        plane, _label = session._named_plane(on)
        target = plane
        ok = plane.Select2(False, 0)
    except SolidWorksError:
        face = rbr_features._face_of(session, on)
        ok = binding.wrap(face, mod.IEntity).Select4(False, None)
        target = face
    if not ok or target is None:
        raise SolidWorksError(f"Could not select '{on}' to sketch on.")
    names_before = {f.Name for f in session._iter_features()}
    sk = binding.wrap(model.SketchManager, mod.ISketchManager)
    sk.InsertSketch(True)
    sketch = binding.wrap(model.GetActiveSketch2(), mod.ISketch)
    feature = next((f for f in session._iter_features()
                    if f.Name not in names_before and f.GetTypeName2() == "ProfileFeature"), None)
    if sketch is None or feature is None:
        raise SolidWorksError(f"No sketch opened on '{on}'.")
    return sk, sketch, feature


def _edit_sketch(session, name: str):
    """Open an existing sketch for editing. Returns (sk, sketch, feature)."""
    model = session._require_model()
    feature = session._sketch_by_name(name)
    model.ClearSelection2(True)
    if not feature.Select2(False, 0):
        raise SolidWorksError(f"Could not select the sketch '{name}'.")
    model.EditSketch()
    sketch = binding.wrap(model.GetActiveSketch2(), session._mod.ISketch)
    if sketch is None:
        raise SolidWorksError(f"Could not edit the sketch '{name}'.")
    return binding.wrap(model.SketchManager, session._mod.ISketchManager), sketch, feature


def _close(session, sk) -> None:
    session._require_model().ClearSelection2(True)
    sk.InsertSketch(True)


def _segments(session, sketch) -> list:
    return [binding.wrap(s, session._mod.ISketchSegment) for s in (sketch.GetSketchSegments() or ())]


def _key(segment) -> tuple:
    return int(segment.GetType()), tuple(segment.GetID())


def _sketch_axes(session, sketch) -> tuple:
    """(origin, u, v) of the sketch in model space (m)."""
    mod = session._mod
    to_model = binding.wrap(binding.wrap(sketch.ModelToSketchTransform, mod.IMathTransform).Inverse(),
                            mod.IMathTransform)
    o = session._to_model_mm(to_model, 0.0, 0.0, 0.0)
    u = [b - a for a, b in zip(o, session._to_model_mm(to_model, 0.001, 0.0, 0.0))]
    v = [b - a for a, b in zip(o, session._to_model_mm(to_model, 0.0, 0.001, 0.0))]
    norm = lambda w: [c / (math.sqrt(sum(x * x for x in w)) or 1.0) for c in w]  # noqa: E731
    return o, norm(u), norm(v)


def _uv(session, sketch, point_mm) -> tuple:
    local = session._sketch_coords(sketch, *(mm_to_m(float(c)) for c in point_mm))
    return local[0], local[1], m_to_mm(local[2])


def _relate(session, sketch, kind: str, *entities) -> None:
    import pythoncom
    import win32com.client
    if kind not in RELATIONS:
        raise SolidWorksError(f"Unknown relation '{kind}'; use one of {', '.join(sorted(RELATIONS))}.")
    manager = binding.wrap(sketch.RelationManager, session._mod.ISketchRelationManager)
    array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [e._oleobj_ for e in entities])
    if manager.AddRelation(array, RELATIONS[kind]) is None:
        raise SolidWorksError(f"SolidWorks refused the {kind} relation.")


def _dimension_point(session, point, role: str, horizontal: bool) -> str:
    """A horizontal/vertical dimension of a sketch point from the origin, named role."""
    model = session._require_model()
    origin = session._origin_point()
    model.ClearSelection2(True)
    if not (point.Select4(False, None) and origin.Select4(True, None)):
        raise SolidWorksError(f"Could not select the point and the origin for '{role}'.")
    add = model.AddHorizontalDimension2 if horizontal else model.AddVerticalDimension2
    display = add(point.X + _TEXT_OFFSET_M, point.Y + _TEXT_OFFSET_M, 0.0)
    if display is None:
        raise SolidWorksError(f"SolidWorks refused the dimension '{role}'.")
    return _named(session, display, role)


def _dimension_diameter(session, circle, role: str) -> str:
    model = session._require_model()
    model.ClearSelection2(True)
    if not binding.wrap(circle, session._mod.ISketchSegment).Select4(False, None):
        raise SolidWorksError(f"Could not select the circle for '{role}'.")
    arc = binding.wrap(circle, session._mod.ISketchArc)
    centre = binding.wrap(arc.GetCenterPoint2(), session._mod.ISketchPoint)
    off = arc.GetRadius() + _TEXT_OFFSET_M
    display = model.AddDiameterDimension2(centre.X + off, centre.Y + off, 0.0)
    if display is None:
        raise SolidWorksError(f"SolidWorks refused the diameter '{role}'.")
    return _named(session, display, role)


def _named(session, display, role: str) -> str:
    dim = binding.wrap(binding.wrap(display, session._mod.IDisplayDimension).GetDimension2(0), session._mod.IDimension)
    dim.Name = role
    return dim.GetNameForSelection()


def _link_all(session, links: list) -> list:
    """[(dimension full name, expression)] -> equations set in one rebuild."""
    if not links:
        return []
    names = _globals(session)
    equations = [f'"{dim}" = {quote_names(expr, names)}' for dim, expr in links]
    session.set_equations(equations)
    return equations


# --- B3: convert, relate, midline ---------------------------------------------------


def _convert(session, sketch, sk, entities, construction: bool) -> list:
    """Convert faces/edges (IEntity objects) into the open sketch; the new segments."""
    model = session._require_model()
    before = {_key(s) for s in _segments(session, sketch)}
    model.ClearSelection2(True)
    picked = sum(1 for e in entities if binding.wrap(e, session._mod.IEntity).Select4(True, None))
    if picked != len(entities) or not sk.SketchUseEdge3(False, False):
        raise SolidWorksError("Converting the edges (SketchUseEdge3) failed.")
    made = [s for s in _segments(session, sketch) if _key(s) not in before]
    if construction:
        for s in made:
            s.ConstructionGeometry = True
    model.ClearSelection2(True)
    return made


def _outer_loop_edges(session, face_selector: str) -> list:
    from . import features as rbr_features
    from . import offset_loop
    face = binding.wrap(rbr_features._face_of(session, face_selector), session._mod.IFace2)
    return offset_loop._loop_edges(session, face)


def convert_entities(session, sketch: str | None = None, on: str | None = None, face: str | None = None,
                     edges: list | None = None, construction: bool = True) -> dict:
    """Convert a face's outer loop (face='+x' / '#5' / '@x,y,z') or edges
    (list_edges indices) into a sketch - an existing one (sketch=) or a new one
    on a plane/face (on=). construction=True keeps them as reference geometry."""
    if (sketch is None) == (on is None):
        raise SolidWorksError("Give sketch= (an existing sketch) or on= (a plane or face for a new one).")
    if (face is None) == (edges is None):
        raise SolidWorksError("Give face= (its outer loop) or edges= (list_edges indices).")
    if face is not None:
        entities = _outer_loop_edges(session, face)
    else:
        body = session._solid_body()
        all_edges = [binding.wrap(e, session._mod.IEdge) for e in (body.GetEdges() or ())]
        try:
            entities = [all_edges[int(str(i).lstrip("#"))] for i in edges]
        except (ValueError, IndexError):
            raise SolidWorksError(f"edges must be list_edges indices 0..{len(all_edges) - 1}.") from None
    sk, open_sketch, feature = _open_sketch_on(session, on) if on is not None else _edit_sketch(session, sketch)
    try:
        made = _convert(session, open_sketch, sk, entities, construction)
        indices = [i for i, s in enumerate(_segments(session, open_sketch)) if _key(s) in {_key(m) for m in made}]
    finally:
        _close(session, sk)
    return {"ok": True, "sketch": feature.Name, "converted": len(made), "segment_indices": indices,
            "construction": construction}


def _entity(session, sketch, token):
    """'#3' a segment; '#3.start' / '#3.end' / '#3.center' a point; 'origin'."""
    text = str(token).strip()
    if text == "origin":
        return session._origin_point()
    match = re.match(r"^#?(\d+)(?:\.(start|end|center))?$", text)
    if not match:
        raise SolidWorksError(f"An entity is '#3', '#3.start', '#3.end', '#3.center' or 'origin' (got {token!r}).")
    segments = _segments(session, sketch)
    index = int(match.group(1))
    if index >= len(segments):
        raise SolidWorksError(f"No segment #{index}: the sketch has {len(segments)} (read_sketch order).")
    seg = segments[index]
    if not match.group(2):
        return seg
    mod = session._mod
    kind = int(seg.GetType())
    owner = binding.wrap(seg, mod.ISketchLine if kind == 0 else mod.ISketchArc if kind == 1 else mod.ISketchSpline)
    if match.group(2) == "center":
        if kind != 1:
            raise SolidWorksError(f"#{index} is not an arc or circle: it has no center.")
        return binding.wrap(owner.GetCenterPoint2(), mod.ISketchPoint)
    if kind == 3:
        pts = list(owner.GetPoints2() or ())
        return binding.wrap(pts[0] if match.group(2) == "start" else pts[-1], mod.ISketchPoint)
    return binding.wrap(owner.GetStartPoint2() if match.group(2) == "start" else owner.GetEndPoint2(), mod.ISketchPoint)


def add_relation(session, sketch: str, kind: str, entities: list) -> dict:
    """Add a relation between sketch entities of an existing sketch: kind one of
    coincident / on_edge (a point onto a curve) / midpoint (point, line) /
    horizontal / vertical / tangent / parallel / perpendicular / concentric /
    collinear / coradial / at_intersection / fixed. entities: '#3' (segment, in
    read_sketch order), '#3.start' / '#3.end' / '#3.center' (its points), 'origin'."""
    sk, open_sketch, feature = _edit_sketch(session, sketch)
    try:
        objects = [_entity(session, open_sketch, t) for t in entities]
        _relate(session, open_sketch, kind, *objects)
        status = open_sketch.GetConstrainedStatus()
    finally:
        _close(session, sk)
    return {"ok": True, "sketch": feature.Name, "relation": kind, "entities": list(entities),
            "fully_defined": status == SW_FULLY_CONSTRAINED}


def _curve_hits(session, curve_segments, a_uv, b_uv) -> list:
    """Where the straight chord a-b (sketch m) crosses the given segments:
    [(t along a-b, segment, (u, v))], sorted by t."""
    from . import offset_loop
    hits = []
    for seg in curve_segments:
        poly = offset_loop._sample(session, seg, 400)
        for (p, q) in zip(poly, poly[1:]):
            hit = _segment_cross(a_uv, b_uv, p, q)
            if hit is not None:
                hits.append((hit[0], seg, hit[1]))
    return sorted(hits, key=lambda h: h[0])


def _segment_cross(a, b, p, q):
    """(t, point) where a-b crosses p-q, or None; pure."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (q[0] - p[0], q[1] - p[1])
    denom = r[0] * s[1] - r[1] * s[0]
    if abs(denom) < 1e-18:
        return None
    t = ((p[0] - a[0]) * s[1] - (p[1] - a[1]) * s[0]) / denom
    w = ((p[0] - a[0]) * r[1] - (p[1] - a[1]) * r[0]) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= w <= 1.0:
        return t, (a[0] + t * r[0], a[1] + t * r[1])
    return None


def _midline_centre(session, sketch, sk, profile, station_axis: str, station_mm: float, role: str):
    """A construction chord across the profile at model `station_axis` =
    station_mm, its ends on the profile curves, placed by a dimension `role`
    from the origin; returns (chord line, its dimension name, centre guess uv)."""
    axis = "XYZ".index(station_axis.upper())
    origin, u_dir, v_dir = _sketch_axes(session, sketch)
    normal = [u_dir[1] * v_dir[2] - u_dir[2] * v_dir[1], u_dir[2] * v_dir[0] - u_dir[0] * v_dir[2],
              u_dir[0] * v_dir[1] - u_dir[1] * v_dir[0]]
    if abs(normal[axis]) > 1e-6:
        raise SolidWorksError(f"The station axis {station_axis} is the sketch's normal: it must lie in the sketch plane.")
    # the chord runs across the station: in the plane, perpendicular to the station axis
    e = [1.0 if i == axis else 0.0 for i in range(3)]
    along = [normal[1] * e[2] - normal[2] * e[1], normal[2] * e[0] - normal[0] * e[2],
             normal[0] * e[1] - normal[1] * e[0]]
    centre_model = list(origin)
    centre_model[axis] = station_mm
    far = 5000.0
    a_mm = [c - far * d for c, d in zip(centre_model, along)]
    b_mm = [c + far * d for c, d in zip(centre_model, along)]
    a_uv, b_uv = _uv(session, sketch, a_mm)[:2], _uv(session, sketch, b_mm)[:2]
    hits = _curve_hits(session, profile, a_uv, b_uv)
    if len(hits) < 2:
        raise SolidWorksError(f"The station {station_axis} = {station_mm:g} mm does not cross the profile twice "
                              f"({len(hits)} crossing): is it inside the face?")
    (_, seg_lo, p_lo), (_, seg_hi, p_hi) = hits[0], hits[-1]
    dim_axis = station_dimension_axis((b_uv[0] - a_uv[0], b_uv[1] - a_uv[1]))
    sk.AddToDB = True
    try:
        chord = sk.CreateLine(p_lo[0], p_lo[1], 0.0, p_hi[0], p_hi[1], 0.0)
    finally:
        sk.AddToDB = False
    if chord is None:
        raise SolidWorksError("Could not draw the station chord.")
    chord_seg = binding.wrap(chord, session._mod.ISketchSegment)
    chord_seg.ConstructionGeometry = True
    line = binding.wrap(chord, session._mod.ISketchLine)
    start = binding.wrap(line.GetStartPoint2(), session._mod.ISketchPoint)
    end = binding.wrap(line.GetEndPoint2(), session._mod.ISketchPoint)
    _relate(session, sketch, "coincident", start, seg_lo)
    _relate(session, sketch, "coincident", end, seg_hi)
    _relate(session, sketch, "vertical" if dim_axis == "x" else "horizontal", chord_seg)
    dim = _dimension_point(session, start, role, horizontal=(dim_axis == "x"))
    guess = ((p_lo[0] + p_hi[0]) / 2, (p_lo[1] + p_hi[1]) / 2)
    return chord_seg, dim, guess, (abs(m_to_mm(p_hi[0] - p_lo[0])) + abs(m_to_mm(p_hi[1] - p_lo[1])))


# --- B2: circles and rectangles -----------------------------------------------------


def sketch_circles(session, on: str, circles: list, name: str | None = None,
                   profile_face: str | None = None) -> dict:
    """A fully defined sketch of circles on a plane or face, each dimension
    optionally driven by a variable.

    circles: [{"diameter_mm": 10.7, "center_mm": [x, y, z], "vars": {...}}, ...]
    or a centre on the profile's mean line: {"diameter_mm": 10.7, "midline":
    {"axis": "Z", "station_mm": -382}, "vars": {"diameter": "INS_BOSS_D",
    "station": "INS_Z1"}} - a construction chord across profile_face's outer
    loop (converted as construction) at that station, the circle's centre at
    its midpoint. vars keys: 'diameter', 'station', 'x' / 'y' (sketch axes) or
    'X' / 'Y' / 'Z' (the model axis a position dimension runs along). Position
    links are wrapped in abs(): a dimension from the origin is a distance.
    """
    if not circles:
        raise SolidWorksError("circles is empty.")
    sk, sketch, feature = _open_sketch_on(session, on)
    dims_out, links, profile = [], [], None
    try:
        origin, u_dir, v_dir = _sketch_axes(session, sketch)
        roles_axis = axis_role(u_dir, v_dir)
        for k, spec in enumerate(circles):
            d_mm = float(spec.get("diameter_mm") or 0)
            if d_mm <= 0:
                raise SolidWorksError(f"Circle {k}: diameter_mm must be > 0.")
            variables = dict(spec.get("vars") or {})
            suffix = "" if len(circles) == 1 else str(k + 1)
            roles = {}
            if "midline" in spec:
                if profile is None:
                    face_sel = profile_face or on
                    profile = _convert(session, sketch, sk, _outer_loop_edges(session, face_sel), True)
                mid = spec["midline"]
                chord, station_dim, guess, chord_mm = _midline_centre(
                    session, sketch, sk, profile, str(mid.get("axis", "Z")), float(mid["station_mm"]),
                    f"station{suffix}")
                circle = session._draw_circle(sk, guess[0], guess[1], mm_to_m(d_mm / 2))
                centre = binding.wrap(binding.wrap(circle, session._mod.ISketchArc).GetCenterPoint2(),
                                      session._mod.ISketchPoint)
                _relate(session, sketch, "midpoint", centre, chord)
                roles["station"] = station_dim
                roles["chord_mm"] = round(chord_mm, 3)
            else:
                u, v, off = _uv(session, sketch, spec["center_mm"])
                if abs(off) > 0.05:
                    raise SolidWorksError(f"Circle {k}: center_mm {spec['center_mm']} is {off:.3f} mm off the sketch plane.")
                circle = session._draw_circle(sk, u, v, mm_to_m(d_mm / 2))
                centre = binding.wrap(binding.wrap(circle, session._mod.ISketchArc).GetCenterPoint2(),
                                      session._mod.ISketchPoint)
                for axis_name, horizontal, coord in (("x", True, u), ("y", False, v)):
                    if abs(coord) <= 1e-9:
                        _relate(session, sketch, "vertical_points" if horizontal else "horizontal_points",
                                centre, session._origin_point())
                        continue
                    roles[axis_name] = _dimension_point(session, centre, f"{axis_name}{suffix}", horizontal)
            roles["diameter"] = _dimension_diameter(session, circle, f"diameter{suffix}")
            for key, expr in variables.items():
                target = key
                if key in ("X", "Y", "Z"):
                    target = next((r for r, a in roles_axis.items() if a == key), None)
                    if target is None:
                        raise SolidWorksError(f"Circle {k}: the sketch has no axis along model {key}.")
                if target not in roles or target == "chord_mm":
                    raise SolidWorksError(f"Circle {k}: no dimension '{key}' to link (has: "
                                          f"{', '.join(r for r in roles if r != 'chord_mm')}).")
                links.append((roles[target], f"abs({expr})" if target in ("x", "y", "station") else str(expr)))
            dims_out.append(roles)
        status = sketch.GetConstrainedStatus()
    finally:
        _close(session, sk)
    if name:
        old = feature.Name
        feature.Name = name
        if feature.Name != name:
            raise SolidWorksError(f"The sketch could not be named '{name}': is the name taken?")
        dims_out = [{r: (d.replace(f"@{old}", f"@{name}") if isinstance(d, str) else d) for r, d in roles.items()}
                    for roles in dims_out]
        links = [(d.replace(f"@{old}", f"@{name}"), e) for d, e in links]
    equations = _link_all(session, links)
    return {"ok": True, "sketch": feature.Name, "circles": dims_out, "equations": equations,
            "fully_defined": status == SW_FULLY_CONSTRAINED, "axes": roles_axis}


def sketch_rect(session, on: str, corner_mm: list, width_mm: float, height_mm: float,
                name: str | None = None, vars: dict | None = None) -> dict:
    """A fully defined rectangle on a plane or face: corner_mm (model space) is
    one corner, width along the sketch x, height along the sketch y (negative:
    the other way). Dimensions: width, height, x and y of the corner from the
    origin. vars links them ('width': 'SLOT_W', 'X': 'SLOT_X', ...)."""
    if not width_mm or not height_mm:
        raise SolidWorksError("width_mm and height_mm must not be 0.")
    sk, sketch, feature = _open_sketch_on(session, on)
    links = []
    try:
        origin, u_dir, v_dir = _sketch_axes(session, sketch)
        roles_axis = axis_role(u_dir, v_dir)
        u, v, off = _uv(session, sketch, corner_mm)
        if abs(off) > 0.05:
            raise SolidWorksError(f"corner_mm is {off:.3f} mm off the sketch plane.")
        w, h = mm_to_m(width_mm), mm_to_m(height_mm)
        sk.AddToDB = True
        try:
            made = sk.CreateCornerRectangle(u, v, 0.0, u + w, v + h, 0.0)
        finally:
            sk.AddToDB = False
        lines = [binding.wrap(s, session._mod.ISketchSegment) for s in (made or ())]
        if len(lines) != 4:
            raise SolidWorksError("CreateCornerRectangle did not draw four lines.")
        mod = session._mod

        def ends(seg):
            line = binding.wrap(seg, mod.ISketchLine)
            return (binding.wrap(line.GetStartPoint2(), mod.ISketchPoint),
                    binding.wrap(line.GetEndPoint2(), mod.ISketchPoint))

        points = {}
        for seg in lines:
            for p in ends(seg):
                points[tuple(p.GetID())] = p
        corner = min(points.values(), key=lambda p: math.dist((p.X, p.Y), (u, v)))
        opposite = max(points.values(), key=lambda p: math.dist((p.X, p.Y), (u, v)))
        roles = {}
        model = session._require_model()
        for role, horizontal in (("width", True), ("height", False)):
            model.ClearSelection2(True)
            corner.Select4(False, None)
            opposite.Select4(True, None)
            add = model.AddHorizontalDimension2 if horizontal else model.AddVerticalDimension2
            display = add(opposite.X + _TEXT_OFFSET_M, opposite.Y + _TEXT_OFFSET_M, 0.0)
            if display is None:
                raise SolidWorksError(f"SolidWorks refused the {role} dimension.")
            roles[role] = _named(session, display, role)
        for axis_name, horizontal, coord in (("x", True, u), ("y", False, v)):
            if abs(coord) <= 1e-9:
                _relate(session, sketch, "vertical_points" if horizontal else "horizontal_points",
                        corner, session._origin_point())
            else:
                roles[axis_name] = _dimension_point(session, corner, axis_name, horizontal)
        for key, expr in (vars or {}).items():
            target = key
            if key in ("X", "Y", "Z"):
                target = next((r for r, a in roles_axis.items() if a == key), None)
            if target not in roles:
                raise SolidWorksError(f"No dimension '{key}' to link (has: {', '.join(roles)}).")
            links.append((roles[target], f"abs({expr})" if target in ("x", "y") else str(expr)))
        status = sketch.GetConstrainedStatus()
    finally:
        _close(session, sk)
    if name:
        old = feature.Name
        feature.Name = name
        roles = {r: d.replace(f"@{old}", f"@{name}") for r, d in roles.items()}
        links = [(d.replace(f"@{old}", f"@{name}"), e) for d, e in links]
    equations = _link_all(session, links)
    return {"ok": True, "sketch": feature.Name, "dimensions": roles, "equations": equations,
            "fully_defined": status == SW_FULLY_CONSTRAINED, "axes": roles_axis}


# --- B6: plane normal to a sketch line ----------------------------------------------


def plane_normal_to_line(session, sketch: str, segment: int, at: str = "start", name: str | None = None) -> dict:
    """A reference plane perpendicular to a line of a sketch, through its start
    or end point: the plane to sketch a hole on whose axis is that line (a
    hinge axis at an angle). The plane follows the line when it moves."""
    if at not in ("start", "end"):
        raise SolidWorksError("at is 'start' or 'end' (the line's point the plane passes through).")
    model = session._require_model()
    feature = session._sketch_by_name(sketch)
    shape = binding.wrap(feature.GetSpecificFeature2(), session._mod.ISketch)
    segments = _segments(session, shape)
    if not 0 <= int(segment) < len(segments) or int(segments[int(segment)].GetType()) != 0:
        raise SolidWorksError(f"#{segment} is not a line of '{sketch}' (read_sketch lists its segments).")
    line_seg = segments[int(segment)]
    line = binding.wrap(line_seg, session._mod.ISketchLine)
    point = binding.wrap(line.GetStartPoint2() if at == "start" else line.GetEndPoint2(), session._mod.ISketchPoint)
    sm = binding.wrap(model.SelectionManager, session._mod.ISelectionMgr)
    model.ClearSelection2(True)
    first = binding.wrap(sm.CreateSelectData(), session._mod.ISelectData)
    first.Mark = 0
    second = binding.wrap(sm.CreateSelectData(), session._mod.ISelectData)
    second.Mark = 1
    if not line_seg.Select4(False, first) or not point.Select4(True, second):
        raise SolidWorksError("Could not select the line and its point for the plane.")
    planes_before = {p.Name for p in session._ref_planes()}
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    made = fm.InsertRefPlane(REF_PLANE_PERPENDICULAR, 0.0, REF_PLANE_COINCIDENT, 0.0, 0, 0.0)
    model.ClearSelection2(True)
    if made is None:
        raise SolidWorksError("SolidWorks refused a plane perpendicular to the line through its point.")
    plane = next((p for p in session._ref_planes() if p.Name not in planes_before), None)
    if plane is None:
        raise SolidWorksError("The new plane is not in the tree.")
    if name:
        plane.Name = name
        if plane.Name != name:
            raise SolidWorksError(f"The plane could not be named '{name}' (it is '{plane.Name}').")
    return {"ok": True, "plane": plane.Name, "sketch": sketch, "segment": int(segment), "through": at,
            **session._plane_frame(plane)}


# --- B7 ------------------------------------------------------------------------------


def mirror_features(session, features: list, plane: str, name: str = "Mirror") -> dict:
    """Mirror features (list_features names) about a plane: 'front' / 'top' /
    'right' or any plane of the part ('PL_MID'). The copies follow their seeds."""
    if not features:
        raise SolidWorksError("features is empty: name the features to mirror (mirror_body mirrors a body).")
    return session.add_mirror(plane, 0.0, list(features), name)
