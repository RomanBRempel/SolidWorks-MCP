"""Sketches placed from the model's geometry, not from the origin (S-08).

The building tools dimension every sketch point from the part's origin. That
is fully defined, but it is not how a part is designed: a pocket belongs
"8 mm from this wall", and when the wall moves the pocket must follow. Here:

- datum_x / datum_y on sketch_rect, sketch_circles and add_sketch, and on
  fully_define_sketch for a sketch that exists: the entity the horizontal /
  the vertical positions are measured from - an edge, a planar face seen
  edge-on, or a vertex of the model. The dimensions keep their names
  ('x', 'y1', ...); only what they are measured from changes.
- add_dimension: one dimension between sketch entities and / or model geometry.
- sketch_fillet: a corner of an existing sketch rounded, the radius a dimension.
- fully_define_sketch: replace the origin dimensions by datum dimensions, then
  let SolidWorks add what is still missing (ISketchManager.FullyDefineSketch).

Verified on SOLIDWORKS 2025 SP3: an origin dimension is a DISTANCE relation
between the sketch point and the origin as an external point; deleting it
through the relation manager and dimensioning the same point to a model edge
leaves the sketch fully defined, and the sketch then follows the edge (box
80 -> 90 wide: a rectangle 70 mm from the far edge moved by 10).
"""

import math
import re

from .. import binding
from ..constants import SW_CONSTRAINED_CORNER_KEEP, SW_FULLY_CONSTRAINED
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m
from . import param, query

# swConstraintType_e
SW_CONSTRAINT_DISTANCE = 1
SW_CONSTRAINT_ANGLE = 2
SW_CONSTRAINT_RADIUS = 3
SW_CONSTRAINT_DIAMETER = 23
SW_CONSTRAINT_COINCIDENT = 9
SW_CONSTRAINT_HORIZONTAL_POINTS = 25
SW_CONSTRAINT_VERTICAL_POINTS = 26
# swDimensionType_e
SW_DIM_HORIZONTAL = 11
SW_DIM_VERTICAL = 12
# swAutodimMark_e, swAutodimScheme_e
SW_AUTODIM_MARK_HORIZONTAL_DATUM = 2
SW_AUTODIM_MARK_VERTICAL_DATUM = 4
SW_AUTODIM_BASELINE = 1
SW_FULLY_DEFINE_ALL_RELATIONS = 1023   # every swSketchFullyDefineRelationType_e bit
SW_OBJECT_SAME = 1                     # swObjectEquality_e
SW_UNDER_CONSTRAINED = 2
STATUS = {1: "unknown", 2: "under defined", 3: "fully defined", 4: "over defined", 5: "no solution",
          6: "invalid solution", 7: "auto-solve off"}

ACROSS = 0.03     # |cos| below this: perpendicular
ALONG = 0.9995    # |cos| above this: parallel
ON_DATUM_M = 1e-9
VERTEX_TOLERANCE_MM = 0.01


# --- pure parts (unit-tested) --------------------------------------------------------------------


def unit(vector) -> tuple:
    length = math.sqrt(sum(float(c) ** 2 for c in vector))
    if length < 1e-15:
        raise SolidWorksError("A direction needs a non-zero vector.")
    return tuple(float(c) / length for c in vector)


def dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def edge_is_datum_for(direction, measure_axis) -> bool:
    """Can a straight edge running along `direction` be the datum of positions measured
    along `measure_axis` (both model unit vectors)? It must run across that axis: then
    every point of it has the same position along it. Pure."""
    return abs(dot(unit(direction), unit(measure_axis))) < ACROSS


def face_is_datum_for(normal, measure_axis) -> bool:
    """Can a planar face be the datum of positions along `measure_axis`? Its normal must
    run along that axis (the face is seen edge-on in the sketch). Pure."""
    return abs(dot(unit(normal), unit(measure_axis))) > ALONG


def parse_point(token) -> list | None:
    """[x, y, z] from '@x,y,z' or a list of three numbers, else None; pure."""
    if isinstance(token, (list, tuple)):
        if len(token) == 3 and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in token):
            if not all(math.isfinite(c) for c in token):
                raise SolidWorksError(f"A point needs three finite numbers (got {list(token)!r}).")
            return [float(c) for c in token]
        return None
    text = str(token).strip()
    if not text.startswith("@"):
        return None
    try:
        values = [float(c) for c in text[1:].split(",")]
    except ValueError:
        values = []
    if len(values) != 3 or not all(math.isfinite(c) for c in values):
        raise SolidWorksError(f"A point is '@x,y,z' in model mm (got {token!r}).")
    return values


def parse_edge_index(token) -> int | None:
    """5 from 'edge:5' / 'edge#5' / 'edge 5' (a list_edges index), else None; pure."""
    match = re.fullmatch(r"edge\s*[:#]?\s*(\d+)", str(token).strip().lower())
    return int(match.group(1)) if match else None


def origin_axis(kind: int, dim_type: int | None) -> str | None:
    """Which position ('x', 'y' or 'xy') a relation to the origin fixes; None when it is
    not one this module replaces. Pure. A horizontal dimension or 'on the origin's
    vertical line' fixes x; coincident with the origin fixes both."""
    if kind == SW_CONSTRAINT_DISTANCE:
        return {SW_DIM_HORIZONTAL: "x", SW_DIM_VERTICAL: "y"}.get(dim_type)
    return {SW_CONSTRAINT_VERTICAL_POINTS: "x", SW_CONSTRAINT_HORIZONTAL_POINTS: "y",
            SW_CONSTRAINT_COINCIDENT: "xy"}.get(kind)


def describe_status(code: int) -> str:
    return STATUS.get(int(code), f"status {code}")


# --- datums ---------------------------------------------------------------------------------------


class Datum:
    """A model entity positions are measured from: `entity` (selectable), `kind`
    ('edge' / 'vertex'), `at` (its position along the measured sketch axis, m)."""

    def __init__(self, token, entity, kind: str, at: float, label: str):
        self.token, self.entity, self.kind, self.at, self.label = token, entity, kind, at, label


def _edge_line(session, edge):
    """(start, end) of a straight edge in model m, or None when it is not straight."""
    mod = session._mod
    curve = binding.wrap(edge.GetCurve(), mod.ICurve)
    if curve is None or not curve.IsLine():
        return None
    params = edge.GetCurveParams2()
    return [float(c) for c in params[0:3]], [float(c) for c in params[3:6]]


def _vertex_at(session, point_mm):
    """The model vertex at a point (any solid body), with its position (m)."""
    mod = session._mod
    best = None
    for body in session._part_bodies():
        for raw in body.GetVertices() or ():
            vertex = binding.wrap(raw, mod.IVertex)
            at = [float(c) for c in vertex.GetPoint()]
            off = math.dist([c * 1000.0 for c in at], point_mm)
            if best is None or off < best[0]:
                best = (off, vertex, at)
    if best is None or best[0] > VERTEX_TOLERANCE_MM:
        near = f" (the nearest is {best[0]:.3f} mm away)" if best else ""
        raise SolidWorksError(f"No model vertex at {list(point_mm)} mm{near}: a point datum is a corner of the "
                              "model; for a position along an edge give the edge.")
    return best[1], best[2]


def _kind_of_ref(session, token):
    for kind in ("edge", "face", "vertex"):
        try:
            return kind, query.entity(session, token, kind)
        except SolidWorksError as exc:
            if "does not name" not in str(exc):
                raise
    raise SolidWorksError(f"The reference {token} names no edge, face or vertex.")


def resolve_datum(session, sketch, token, axis: str) -> Datum | None:
    """The datum behind a token, checked against the sketch axis it measures along
    ('x': horizontal positions, 'y': vertical). None for no token or 'origin'.

    Tokens: 'ref:...' (find_edges / find_faces), 'edge:5' (a list_edges index), a face
    selector ('+x', '-y:inner', '#5'), '@x,y,z' or [x, y, z] (the model vertex there)."""
    if token is None or (isinstance(token, str) and token.strip().lower() in ("", "origin")):
        return None
    mod = session._mod
    _origin, u_dir, v_dir = param._sketch_axes(session, sketch)
    measure = u_dir if axis == "x" else v_dir
    normal = [u_dir[1] * v_dir[2] - u_dir[2] * v_dir[1], u_dir[2] * v_dir[0] - u_dir[0] * v_dir[2],
              u_dir[0] * v_dir[1] - u_dir[1] * v_dir[0]]
    name = f"datum_{axis}"

    def position(point_m) -> float:
        local = session._sketch_coords(sketch, *point_m)
        return local[0] if axis == "x" else local[1]

    def from_edge(edge, label):
        line = _edge_line(session, edge)
        if line is None:
            raise SolidWorksError(f"{name} {label} is not a straight edge: positions are measured from a straight "
                                  "edge, a planar face or a vertex.")
        start, end = line
        if not edge_is_datum_for([b - a for a, b in zip(start, end)], measure):
            raise SolidWorksError(
                f"{name} {label} runs along the sketch's {axis} axis: a datum for {axis} positions must run "
                f"across it (or give it as datum_{'y' if axis == 'x' else 'x'}).")
        return Datum(token, edge, "edge", position(start), label)

    point = parse_point(token)
    if point is not None:
        vertex, at = _vertex_at(session, point)
        return Datum(token, vertex, "vertex", position(at), f"the vertex at {point}")
    index = parse_edge_index(token)
    if index is not None:
        edges = list(session._solid_body().GetEdges() or ())
        if not 0 <= index < len(edges):
            raise SolidWorksError(f"{name}: no edge {index} (list_edges shows 0..{len(edges) - 1}).")
        return from_edge(binding.wrap(edges[index], mod.IEdge), f"edge {index}")
    face = None
    if query.is_ref(token):
        kind, obj = _kind_of_ref(session, token)
        if kind == "edge":
            return from_edge(obj, f"the edge {token}")
        if kind == "vertex":
            return Datum(token, obj, "vertex", position([float(c) for c in obj.GetPoint()]), f"the vertex {token}")
        face = obj
    else:
        from . import tools as rbr_tools
        face = binding.wrap(rbr_tools._face_for(session, str(token).strip()), mod.IFace2)
    surface = binding.wrap(face.GetSurface(), mod.ISurface)
    if surface is None or not surface.IsPlane() or not face_is_datum_for(face.Normal, measure):
        raise SolidWorksError(
            f"{name} {token!r} is not a planar face across the sketch's {axis} axis: the face must be seen "
            "edge-on in the sketch, its normal along that axis. Give such a face, an edge or a vertex.")
    best = None
    for raw in face.GetEdges() or ():
        edge = binding.wrap(raw, mod.IEdge)
        line = _edge_line(session, edge)
        if line is None:
            continue
        start, end = line
        along = [b - a for a, b in zip(start, end)]
        # prefer an edge lying in the sketch's direction of view (a line in the sketch), near its plane
        score = (abs(dot(unit(along), normal)), abs(session._sketch_coords(sketch, *start)[2]))
        if best is None or score < best[0]:
            best = (score, edge, start)
    if best is None:
        raise SolidWorksError(f"{name} {token!r}: the face has no straight edge to measure from; give an edge "
                              "or a vertex.")
    return Datum(token, best[1], "edge", position(best[2]), f"the face {token}")


# --- dimensions in the open sketch ------------------------------------------------------------------


def _select(session, *entities) -> None:
    model = session._require_model()
    model.ClearSelection2(True)
    for k, entity in enumerate(entities):
        # sketch points and segments select themselves; a model edge, vertex or face is an IEntity
        target = entity if hasattr(entity, "Select4") else binding.wrap(entity, session._mod.IEntity)
        if not target.Select4(k > 0, None):
            raise SolidWorksError("Could not select an entity for the dimension.")


def position_dimension(session, sketch, point, datum: Datum, role: str, horizontal: bool) -> str | None:
    """Place `point` (ISketchPoint) from the datum along the sketch's x (horizontal) or
    y: a dimension named `role`, or a relation when the point lies on the datum (SolidWorks
    has no 0 mm dimension). Returns the dimension's full name, or None for the relation."""
    model = session._require_model()
    coord = point.X if horizontal else point.Y
    if abs(coord - datum.at) <= ON_DATUM_M:
        if datum.kind == "edge":
            param._relate(session, sketch, "coincident", point, datum.entity)
        else:
            param._relate(session, sketch, "vertical_points" if horizontal else "horizontal_points",
                          point, datum.entity)
        return None
    _select(session, point, datum.entity)
    text = (point.X + param._TEXT_OFFSET_M, point.Y + param._TEXT_OFFSET_M, 0.0)
    display = (model.AddHorizontalDimension2 if horizontal else model.AddVerticalDimension2)(*text)
    if display is None:
        _select(session, point, datum.entity)
        display = model.AddDimension2(*text)
    model.ClearSelection2(True)
    if display is None:
        raise SolidWorksError(f"SolidWorks refused the dimension '{role}' from {datum.label}.")
    return param._named(session, display, role)


def _is_origin(session, definition, origin) -> bool:
    """Is a relation's definition entity the part's origin point?

    GetEntities gives the origin, a model vertex and any other external point alike (a
    point with an id outside the sketch, at its projected place); GetDefinitionEntities2
    gives the object itself - the origin's sketch point, an IVertex, an IEdge - and
    ISldWorks.IsSame answers 1 for the origin only (verified on SW 2025 SP3: a vertex
    datum at the origin's projection answers 0)."""
    try:
        return int(session._sw.IsSame(definition, origin._oleobj_)) == SW_OBJECT_SAME
    except Exception:  # noqa: BLE001 - not comparable: not the origin
        return False


def origin_relations(session, sketch) -> list:
    """The relations of the open sketch that tie one of its points to the origin:
    [{relation, point, axis ('x' / 'y' / 'xy'), dimension (IDimension or None), name}]."""
    mod = session._mod
    manager = binding.wrap(sketch.RelationManager, mod.ISketchRelationManager)
    origin = session._origin_point()
    found = []
    for raw in manager.GetRelations(0) or ():
        relation = binding.wrap(raw, mod.ISketchRelation)
        kind = int(relation.GetRelationType())
        if kind not in (SW_CONSTRAINT_DISTANCE, SW_CONSTRAINT_COINCIDENT, SW_CONSTRAINT_HORIZONTAL_POINTS,
                        SW_CONSTRAINT_VERTICAL_POINTS):
            continue
        entities = list(relation.GetEntities() or ())
        types = list(relation.GetEntitiesType() or ())
        if len(entities) != 2 or any(int(t) != 2 for t in types):   # 2: a point
            continue
        try:
            definitions = list(relation.GetDefinitionEntities2() or ())
        except Exception:  # noqa: BLE001
            continue
        if len(definitions) != 2:
            continue
        points = [binding.wrap(e, mod.ISketchPoint) for e in entities]
        flags = [_is_origin(session, d, origin) for d in definitions]
        if sum(flags) != 1:
            continue
        dimension, dim_type = None, None
        if kind == SW_CONSTRAINT_DISTANCE:
            display = binding.wrap(relation.GetDisplayDimension(), mod.IDisplayDimension)
            if display is None:
                continue
            dimension = binding.wrap(display.GetDimension2(0), mod.IDimension)
            dim_type = int(display.Type2)
        axis = origin_axis(kind, dim_type)
        if axis is None:
            continue
        found.append({"relation": relation, "point": points[flags.index(False)], "axis": axis,
                      "dimension": dimension, "name": dimension.Name if dimension is not None else None,
                      "manager": manager})
    return found


def _driven_by_equation(session) -> dict:
    return {e["name"].lower(): e["equation"] for e in session._equation_entries()
            if e.get("name") and "@" in e["name"]}


def redatum(session, sketch, feature_name: str, datum_x: Datum | None, datum_y: Datum | None) -> dict:
    """In the open sketch: every position measured from the origin is measured from the
    datum instead (x positions from datum_x, y from datum_y), under the same dimension
    name. Returns {replaced: [...], kept: [...]}."""
    replaced, kept = [], []
    if datum_x is None and datum_y is None:
        return {"replaced": replaced, "kept": kept}
    equations = _driven_by_equation(session)
    origin = session._origin_point()
    counter = {"x": 0, "y": 0}
    for entry in origin_relations(session, sketch):
        axes = [a for a in entry["axis"] if (datum_x if a == "x" else datum_y) is not None]
        name = entry["name"]
        if not axes:
            continue
        full = f"{name}@{feature_name}".lower() if name else None
        if full and full in equations:
            kept.append({"dimension": f"{name}@{feature_name}", "why": f"driven by the equation {equations[full]}: "
                         "delete or re-link it first (delete_equation), its meaning would change"})
            continue
        point = entry["point"]
        old = round(m_to_mm(entry["dimension"].SystemValue), 6) if entry["dimension"] is not None else 0.0
        if not entry["manager"].DeleteRelation(entry["relation"]):
            kept.append({"dimension": f"{name}@{feature_name}" if name else entry["axis"],
                         "why": "SolidWorks would not delete the relation"})
            continue
        for axis in entry["axis"]:
            datum = datum_x if axis == "x" else datum_y
            horizontal = axis == "x"
            if datum is None:   # the other half of 'at the origin' stays on the origin's line
                param._relate(session, sketch, "vertical_points" if horizontal else "horizontal_points",
                              point, origin)
                continue
            counter[axis] += 1
            role = name if (name and len(entry["axis"]) == 1) else f"{axis}_datum{counter[axis]}"
            made = position_dimension(session, sketch, point, datum, role, horizontal)
            row = {"axis": axis, "from": datum.label, "was_mm": old}
            if made is None:
                row["relation"] = "on the datum"
            else:
                dim = binding.wrap(session._require_model().Parameter(made), session._mod.IDimension)
                row.update(dimension=made, value_mm=round(m_to_mm(dim.SystemValue), 6) if dim is not None else None)
            replaced.append(row)
    return {"replaced": replaced, "kept": kept}


def _status(sketch) -> int:
    return int(sketch.GetConstrainedStatus())


def _result_tail(session, sketch, feature) -> dict:
    code = _status(sketch)
    return {"fully_defined": code == SW_FULLY_CONSTRAINED, "status": describe_status(code),
            "origin_dimensions": [f"{e['name']}@{feature.Name}" for e in origin_relations(session, sketch)
                                  if e["name"]]}


# --- tools ----------------------------------------------------------------------------------------


def redatum_sketch(session, sketch: str, datum_x=None, datum_y=None) -> dict:
    """Open the named sketch, move its origin dimensions to the datums, close it."""
    sk, open_sketch, feature = param._edit_sketch(session, sketch)
    try:
        dx = resolve_datum(session, open_sketch, datum_x, "x")
        dy = resolve_datum(session, open_sketch, datum_y, "y")
        report = redatum(session, open_sketch, feature.Name, dx, dy)
        report.update(_result_tail(session, open_sketch, feature))
    finally:
        param._close(session, sk)
    return report


def fully_define_sketch(session, sketch: str, datum_x=None, datum_y=None,
                        replace_origin_dimensions: bool = True) -> dict:
    """Make an existing sketch fully defined, its positions measured from model geometry.

    1. With datum_x / datum_y and replace_origin_dimensions: each dimension (or relation)
       from the origin becomes one from the datum, under its old name.
    2. What is still free gets relations and dimensions from SolidWorks' own Fully Define
       Sketch, measured from the datums (the origin where none is given).
    Returns what was replaced, the dimensions added, and whether it is fully defined."""
    model = session._require_model()
    mod = session._mod
    sk, open_sketch, feature = param._edit_sketch(session, sketch)
    try:
        dx = resolve_datum(session, open_sketch, datum_x, "x")
        dy = resolve_datum(session, open_sketch, datum_y, "y")
        before_names = {d["name"] for d in session._dimension_entries(feature)}
        status_before = _status(open_sketch)
        report = {"replaced": [], "kept": []}
        if replace_origin_dimensions:
            report = redatum(session, open_sketch, feature.Name, dx, dy)
        auto = None
        if _status(open_sketch) == SW_UNDER_CONSTRAINED:
            origin = session._origin_point()
            model.ClearSelection2(True)
            manager = binding.wrap(model.SelectionManager, mod.ISelectionMgr)
            horizontal = dx.entity if dx is not None else origin
            vertical = dy.entity if dy is not None else origin
            same = horizontal is vertical
            picks = [(horizontal, SW_AUTODIM_MARK_HORIZONTAL_DATUM | (SW_AUTODIM_MARK_VERTICAL_DATUM if same else 0))]
            if not same:
                picks.append((vertical, SW_AUTODIM_MARK_VERTICAL_DATUM))
            for k, (entity, mark) in enumerate(picks):
                data = binding.wrap(manager.CreateSelectData(), mod.ISelectData)
                data.Mark = mark
                if not binding.wrap(entity, mod.IEntity).Select4(k > 0, data):
                    raise SolidWorksError("Could not select the datum for Fully Define Sketch.")
            auto = sk.FullyDefineSketch(True, True, SW_FULLY_DEFINE_ALL_RELATIONS, True, SW_AUTODIM_BASELINE, None,
                                        SW_AUTODIM_BASELINE, None, 1, -1)
            model.ClearSelection2(True)
        added = [d["name"] for d in session._dimension_entries(feature) if d["name"] not in before_names]
        tail = _result_tail(session, open_sketch, feature)
    finally:
        param._close(session, sk)
    result = {"ok": True, "sketch": feature.Name, "was": describe_status(status_before), **tail,
              "replaced": report["replaced"], "kept": report["kept"], "dimensions_added": added}
    if auto is not None:
        result["fully_define_code"] = int(auto)
    if not result["fully_defined"]:
        result["hint"] = ("Still not fully defined: read_sketch shows the geometry; add_relation / add_dimension "
                          "fix what is free (a spline's shape, a construction line).")
    return result


def _model_entity(session, sketch, token):
    """A model entity for add_dimension: (entity, label), or None when the token is a
    sketch token ('#3', '#3.start', 'origin')."""
    mod = session._mod
    point = parse_point(token)
    if point is not None:
        return _vertex_at(session, point)[0], f"the vertex at {point}"
    index = parse_edge_index(token)
    if index is not None:
        edges = list(session._solid_body().GetEdges() or ())
        if not 0 <= index < len(edges):
            raise SolidWorksError(f"No edge {index} (list_edges shows 0..{len(edges) - 1}).")
        return binding.wrap(edges[index], mod.IEdge), f"edge {index}"
    if query.is_ref(token):
        kind, obj = _kind_of_ref(session, token)
        return obj, f"the {kind} {token}"
    return None


def add_dimension(session, sketch: str, entities: list | str, value_mm: float | None = None,
                  direction: str = "auto", name: str | None = None, var: str | None = None) -> dict:
    """One dimension in an existing sketch, between its entities and / or the model's.

    entities: one or two of '#3' (segment, read_sketch order), '#3.start' / '#3.end' /
    '#3.center', 'origin', and of the model: 'ref:...' (an edge, vertex or face from
    find_edges / find_faces), 'edge:5' (list_edges index), '@x,y,z' (the vertex there).
    One line: its length; one arc or circle: its radius (direction='diameter' for the
    diameter); two entities: their distance, or the angle between two lines that are not
    parallel. direction 'auto' / 'horizontal' / 'vertical' (along the sketch's x / y).
    value_mm (degrees for an angle) sets it - the geometry moves; without it the
    dimension takes the size as drawn. name names it; var links it to a global variable.
    Refused when the sketch is already fully defined (the dimension would be driven)."""
    want = str(direction).lower()
    if want not in ("auto", "horizontal", "vertical", "diameter", "radius"):
        raise SolidWorksError("direction is 'auto', 'horizontal', 'vertical', 'diameter' or 'radius'.")
    tokens = [entities] if isinstance(entities, str) else list(entities)
    if len(tokens) == 3 and parse_point(tokens) is not None:
        tokens = [tokens]
    if not 1 <= len(tokens) <= 2:
        raise SolidWorksError("entities takes one or two entities.")
    model = session._require_model()
    mod = session._mod
    closed = binding.wrap(session._sketch_by_name(sketch).GetSpecificFeature2(), mod.ISketch)
    if closed is not None and _status(closed) == SW_FULLY_CONSTRAINED:   # before the sketch is opened
        raise SolidWorksError(
            f"'{sketch}' is fully defined: another dimension would over-define it. To measure a "
            "position from model geometry instead of the origin use fully_define_sketch(sketch, datum_x, "
            "datum_y), or delete a dimension first.")
    sk, open_sketch, feature = param._edit_sketch(session, sketch)
    made = None
    try:
        picked, labels = [], []
        for token in tokens:
            found = _model_entity(session, open_sketch, token)
            if found is None:
                picked.append(param._entity(session, open_sketch, token))
                labels.append(str(token))
            else:
                picked.append(found[0])
                labels.append(found[1])
        before = {d["name"] for d in session._dimension_entries(feature)}
        _select(session, *picked)
        first = picked[0]
        at = (getattr(first, "X", 0.0) + param._TEXT_OFFSET_M, getattr(first, "Y", 0.0) + param._TEXT_OFFSET_M, 0.0)
        if len(picked) == 1 and hasattr(first, "GetType") and int(first.GetType()) == 1:
            arc = binding.wrap(first, mod.ISketchArc)
            centre = binding.wrap(arc.GetCenterPoint2(), mod.ISketchPoint)
            off = arc.GetRadius() + param._TEXT_OFFSET_M
            at = (centre.X + off, centre.Y + off, 0.0)
            full_circle = bool(arc.IsCircle())
            use_diameter = want == "diameter" or (want == "auto" and full_circle)
            display = (model.AddDiameterDimension2 if use_diameter else model.AddRadialDimension2)(*at)
        elif want == "horizontal":
            display = model.AddHorizontalDimension2(*at)
        elif want == "vertical":
            display = model.AddVerticalDimension2(*at)
        else:
            display = model.AddDimension2(*at)
        model.ClearSelection2(True)
        if display is None:
            raise SolidWorksError(f"SolidWorks refused a dimension between {' and '.join(labels)}.")
        shown = binding.wrap(display, mod.IDisplayDimension)
        dim = binding.wrap(shown.GetDimension2(0), mod.IDimension)
        if name:
            dim.Name = name
        made = dim.GetNameForSelection()
        code = _status(open_sketch)
        if int(dim.DrivenState) == 1 or code not in (SW_UNDER_CONSTRAINED, SW_FULLY_CONSTRAINED):   # swDimensionDriven
            raise SolidWorksError(
                f"The dimension between {' and '.join(labels)} over-defines the sketch ({describe_status(code)}): "
                "what it measures is already fixed by other dimensions or relations.")
        unit_name, to_system, from_system = session._dimension_unit(dim)
        if value_mm is not None:
            dim.SystemValue = to_system(float(value_mm))
            if abs(abs(dim.SystemValue) - abs(to_system(float(value_mm)))) > 1e-9:
                raise SolidWorksError(f"SolidWorks did not take {value_mm} for the new dimension.")
            code = _status(open_sketch)
            if code not in (SW_UNDER_CONSTRAINED, SW_FULLY_CONSTRAINED):
                raise SolidWorksError(f"With {value_mm} the sketch has {describe_status(code)}.")
        value = round(from_system(dim.SystemValue), 6)
        tail = _result_tail(session, open_sketch, feature)
        extra = [d["name"] for d in session._dimension_entries(feature) if d["name"] not in before]
    except Exception:
        if made is not None:   # leave the sketch as it was
            try:
                model.ClearSelection2(True)
                if model.Extension.SelectByID2(made, "DIMENSION", 0, 0, 0, False, 0, None, 0):
                    model.EditDelete()
            except Exception:  # noqa: BLE001 - the guard rolls back what it can
                pass
        raise
    finally:
        param._close(session, sk)
    result = {"ok": True, "sketch": feature.Name, "dimension": made, f"value_{unit_name}": value,
              "between": labels, **tail}
    if len(extra) > 1:
        result["dimensions_added"] = extra
    if var:
        result["equations"] = param._link_all(session, [(made, str(var))])
    return result


def sketch_fillet(session, sketch: str, corners: list | str, radius_mm: float, name: str | None = None,
                  var: str | None = None) -> dict:
    """Round corners of an existing sketch with a sketch fillet of radius_mm: the two
    segments stay where they are (their corner becomes a virtual sharp, so its dimensions
    hold) and the radius is a dimension - one for all corners given, tied by equal
    relations. corners: '#3.end' / '#3.start' (a segment's end, read_sketch order), a pair
    ['#2', '#3'] (where two segments meet), or [x, y, z] model mm at the corner; a list of
    those for several. name names the radius dimension (default 'radius' or 'r<n>'); var
    links it to a global variable."""
    if radius_mm is None or float(radius_mm) <= 0:
        raise SolidWorksError(f"radius_mm must be > 0 (got {radius_mm}).")
    items = _corner_items(corners)
    model = session._require_model()
    mod = session._mod
    sk, open_sketch, feature = param._edit_sketch(session, sketch)
    try:
        points = [_corner_point(session, open_sketch, item) for item in items]
        ids = {tuple(p.GetID()) for p in points}
        if len(ids) != len(points):
            raise SolidWorksError("The same corner is given twice.")
        radii_before = {d.GetNameForSelection() for d in session._radius_dimensions(open_sketch)}
        arcs_before = session._arc_count(open_sketch)
        model.ClearSelection2(True)
        if not all(point.Select4(k > 0, None) for k, point in enumerate(points)):
            raise SolidWorksError("Could not select the corner(s) to round.")
        if sk.CreateFillet(mm_to_m(float(radius_mm)), SW_CONSTRAINED_CORNER_KEEP) is None:
            raise SolidWorksError(f"SolidWorks could not round the corner(s) with R{float(radius_mm):g}: the radius "
                                  "does not fit between the neighbouring segments, or the point is no corner.")
        rounded = session._arc_count(open_sketch) - arcs_before
        new = [d for d in session._radius_dimensions(open_sketch) if d.GetNameForSelection() not in radii_before]
        if rounded != len(points) or len(new) != 1:
            raise SolidWorksError(f"Rounding gave {rounded} arc(s) and {len(new)} radius dimension(s) for "
                                  f"{len(points)} corner(s).")
        role = name or ("radius" if not radii_before else f"r{len(radii_before) + 1}")
        new[0].Name = role
        made = new[0].GetNameForSelection()
        model.ClearSelection2(True)
        tail = _result_tail(session, open_sketch, feature)
    finally:
        param._close(session, sk)
    result = {"ok": True, "sketch": feature.Name, "corners": len(points), "radius_mm": float(radius_mm),
              "dimensions": {"radius": made}, **tail}
    if var:
        result["equations"] = param._link_all(session, [(made, str(var))])
    return result


def _corner_items(corners) -> list:
    """The corners as a list of items: a token, a pair of segment tokens or a point; pure."""
    if isinstance(corners, str):
        return [corners]
    corners = list(corners)
    if not corners:
        raise SolidWorksError("corners is empty.")
    if parse_point(corners) is not None:
        return [corners]
    if len(corners) == 2 and all(isinstance(c, str) and re.fullmatch(r"#?\d+", c.strip()) for c in corners):
        return [corners]
    return corners


def _segment_points(session, segment) -> list:
    mod = session._mod
    kind = int(segment.GetType())
    if kind == 0:
        owner = binding.wrap(segment, mod.ISketchLine)
    elif kind == 1:
        owner = binding.wrap(segment, mod.ISketchArc)
    else:
        return []
    return [binding.wrap(owner.GetStartPoint2(), mod.ISketchPoint), binding.wrap(owner.GetEndPoint2(), mod.ISketchPoint)]


def _corner_point(session, sketch, item):
    """The sketch point of a corner given as '#3.end', ['#2', '#3'] or a model point."""
    point = parse_point(item) if not isinstance(item, str) or item.strip().startswith("@") else None
    segments = param._segments(session, sketch)
    if point is not None:
        u, v, off = param._uv(session, sketch, point)
        if abs(off) > 0.05:
            raise SolidWorksError(f"The corner {list(point)} is {off:.3f} mm off the sketch plane.")
        best = None
        for segment in segments:
            for candidate in _segment_points(session, segment):
                gap = math.dist((candidate.X, candidate.Y), (u, v))
                if best is None or gap < best[0]:
                    best = (gap, candidate)
        if best is None or m_to_mm(best[0]) > VERTEX_TOLERANCE_MM:
            raise SolidWorksError(f"No sketch corner at {list(point)} mm (read_sketch shows the segment ends).")
        return best[1]
    if isinstance(item, (list, tuple)):
        if len(item) != 2:
            raise SolidWorksError(f"A corner is '#3.end', a pair of segments ['#2', '#3'] or [x, y, z] (got {item!r}).")
        first, second = (param._entity(session, sketch, token) for token in item)
        for a in _segment_points(session, first):
            for b in _segment_points(session, second):
                if math.dist((a.X, a.Y), (b.X, b.Y)) <= 1e-9:
                    return a
        raise SolidWorksError(f"The segments {item[0]} and {item[1]} do not meet in a corner.")
    text = str(item).strip()
    if not re.fullmatch(r"#?\d+\.(start|end)", text):
        raise SolidWorksError(f"A corner is '#3.end', a pair of segments ['#2', '#3'] or [x, y, z] (got {item!r}).")
    return param._entity(session, sketch, text)


def add_sketch(session, plane: str, start_mm: list, segments: list, name: str | None = None,
               datum_x=None, datum_y=None) -> dict:
    """Upstream's add_sketch; with datum_x / datum_y its points are then placed from those
    model entities instead of the origin (same dimension names)."""
    result = session.add_sketch(plane, start_mm, segments, name)
    if datum_x is None and datum_y is None:
        return result
    report = redatum_sketch(session, result["sketch"], datum_x, datum_y)
    if not report["fully_defined"]:
        raise SolidWorksError(f"With the datums the sketch came out {report['status']}; nothing was kept. "
                              f"Kept at the origin: {report['kept']}.")
    result["datums"] = {"replaced": report["replaced"], "kept": report["kept"]}
    result["origin_dimensions"] = report["origin_dimensions"]
    return result
