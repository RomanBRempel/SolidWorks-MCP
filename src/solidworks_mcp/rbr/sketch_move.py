"""sketch_move: shift sketch geometry by a vector, as ONE move, and check where it landed.

Moving a constrained shape point by point (ISketchPoint.SetCoords on each corner
of a rectangle) moves it twice: the first point drags the others along through
the relations, the next SetCoords shifts them again (14 mm instead of 7,
journal 2026-11-20). SolidWorks' own Move Entities
(IModelDocExtension.MoveOrCopy) takes the whole selection at once.

Probed on SW 2025 (2026-11-20, a 10 x 8 rectangle):
- held by relations only: MoveOrCopy shifts it by exactly the vector;
- placed by dimensions (x, y from the origin - what sketch_rect makes): MoveOrCopy
  answers nothing and moves nothing; the dimensions hold it.
So when the plain move falls short, the sketch's driving dimensions are released
(DrivenState = driven), the move is made, and they are made driving again: each
then keeps what it measures now. The dimensions that place the selection come
out changed by the vector, every other one as it was - without having to work
out which is which.

A dimension set by an equation cannot be moved that way (the equation puts it
back at the next rebuild): the move is undone and the equation named.

Every point of the selection is compared before and after; a shortfall undoes
the move and is refused with what held it.
"""

import math

from .. import binding
from ..constants import SKETCH_STATUSES, SW_DIMENSION_DRIVING, SW_FULLY_CONSTRAINED, SW_SKETCH_ARC, SW_SKETCH_LINE
from ..errors import SolidWorksError
from ..units import m_to_mm, mm_to_m
from . import batch, param, sketch_datum

SW_DIMENSION_DRIVEN = 1
TOLERANCE_M = 1e-7          # 0.0001 mm: what counts as "landed there"
PLANE_TOLERANCE_MM = 1e-3   # how far a vector may leave the sketch plane
FILTER_KEYS = ("type", "construction", "within_mm")
FILTER_HELP = ("filter picks the segments to move: leave it out for the whole sketch; a list of segments "
               "['#0', '#3'] (read_sketch order); or {'type': 'line' | 'arc' | 'circle' | 'spline' (or a list), "
               "'construction': true | false, 'within_mm': [[x, y, z], [x, y, z]]} - a model-space box that "
               "holds every point of the segment; the keys given must all match.")


# --- pure parts (unit-tested) -------------------------------------------------------------------


def _vector(value, what: str) -> list:
    try:
        out = [float(c) for c in value]
    except (TypeError, ValueError):
        out = []
    if len(out) != 3 or not all(math.isfinite(c) for c in out):
        raise SolidWorksError(f"{what} is [x, y, z] in model mm (got {value!r}).")
    return out


def segment_points(entry: dict) -> list:
    """The model points (mm) a read_sketches segment entry carries."""
    return [entry[key] for key in ("start_mm", "end_mm", "centre_mm") if entry.get(key) is not None]


def choose(entries: list, selection) -> list:
    """Indices of the segments `selection` picks among read_sketches entries (see FILTER_HELP)."""
    count = len(entries)
    if selection is None or selection == "all":
        picked = list(range(count))
    elif isinstance(selection, dict):
        unknown = [k for k in selection if k not in FILTER_KEYS]
        if unknown:
            raise SolidWorksError(f"Unknown filter key(s) {unknown}. {FILTER_HELP}")
        kinds = selection.get("type")
        if isinstance(kinds, str):
            kinds = [kinds]
        kinds = None if kinds is None else {str(k).strip().lower() for k in kinds}
        box = selection.get("within_mm")
        if box is not None:
            try:
                low, high = _vector(box[0], "within_mm[0]"), _vector(box[1], "within_mm[1]")
            except (IndexError, TypeError):
                raise SolidWorksError(f"within_mm is [[x, y, z], [x, y, z]]. {FILTER_HELP}") from None
            low, high = [min(a, b) - 1e-6 for a, b in zip(low, high)], [max(a, b) + 1e-6 for a, b in zip(low, high)]
        picked = []
        for i, entry in enumerate(entries):
            if kinds is not None and entry.get("type") not in kinds:
                continue
            if "construction" in selection and bool(entry.get("construction")) != bool(selection["construction"]):
                continue
            if box is not None:
                points = segment_points(entry)
                if not points or not all(low[k] <= p[k] <= high[k] for p in points for k in range(3)):
                    continue
            picked.append(i)
    else:
        tokens = [selection] if isinstance(selection, str) else list(selection)
        picked = []
        for token in tokens:
            text = str(token).strip().lstrip("#")
            if not text.isdigit():
                raise SolidWorksError(f"'{token}' is not a segment. {FILTER_HELP}")
            if int(text) >= count:
                raise SolidWorksError(f"No segment #{text}: the sketch has {count} (read_sketch order).")
            if int(text) not in picked:
                picked.append(int(text))
    if not picked:
        raise SolidWorksError("The filter picks no segment of the sketch. " + FILTER_HELP)
    return picked


def shortfall(before: dict, after: dict, ids, du: float, dv: float, tolerance: float = TOLERANCE_M) -> list:
    """The selected points that are not at before + (du, dv): [(id, missing_u, missing_v)]."""
    out = []
    for key in ids:
        if key not in before or key not in after:
            out.append((key, du, dv))
            continue
        miss_u = before[key][0] + du - after[key][0]
        miss_v = before[key][1] + dv - after[key][1]
        if abs(miss_u) > tolerance or abs(miss_v) > tolerance:
            out.append((key, miss_u, miss_v))
    return out


def bystanders(before: dict, after: dict, ids, tolerance: float = TOLERANCE_M) -> int:
    """How many points OUTSIDE the selection moved (dragged along by relations)."""
    chosen = set(ids)
    return sum(1 for key, was in before.items()
               if key not in chosen and key in after
               and (abs(after[key][0] - was[0]) > tolerance or abs(after[key][1] - was[1]) > tolerance))


# --- SolidWorks ---------------------------------------------------------------------------------


def _point_key(point) -> tuple:
    return tuple(int(c) for c in point.GetID())


def _all_points(session, sketch) -> dict:
    mod = session._mod
    out = {}
    for raw in sketch.GetSketchPoints2() or ():
        point = binding.wrap(raw, mod.ISketchPoint)
        out[_point_key(point)] = (float(point.X), float(point.Y))
    return out


def _points_of(session, segment) -> list:
    """The sketch points that belong to a segment: ends, an arc's or circle's centre, a spline's points."""
    mod = session._mod
    kind = int(segment.GetType())
    raws = []
    if kind == SW_SKETCH_LINE:
        line = binding.wrap(segment, mod.ISketchLine)
        raws = [line.GetStartPoint2(), line.GetEndPoint2()]
    elif kind == SW_SKETCH_ARC:
        arc = binding.wrap(segment, mod.ISketchArc)
        raws = [arc.GetCenterPoint2()] if arc.IsCircle() else [arc.GetStartPoint2(), arc.GetEndPoint2(),
                                                                arc.GetCenterPoint2()]
    else:
        try:
            raws = list(binding.wrap(segment, mod.ISketchSpline).GetPoints2() or ())
        except Exception:  # noqa: BLE001 - an ellipse, text: moved, but no points to check it by
            raws = []
    return [binding.wrap(r, mod.ISketchPoint) for r in raws if r is not None]


def _dimensions(session, feature) -> list:
    """[(name, IDimension)] of the sketch's display dimensions."""
    mod = session._mod
    out = []
    display = feature.GetFirstDisplayDimension()
    while display is not None:
        shown = binding.wrap(display, mod.IDisplayDimension)
        dim = binding.wrap(shown.GetDimension2(0), mod.IDimension)
        out.append((dim.GetNameForSelection(), dim))
        display = feature.GetNextDisplayDimension(display)
    return out


def _move(session, segments, du: float, dv: float, keep_relations: bool) -> None:
    model = session._require_model()
    mod = session._mod
    model.ClearSelection2(True)
    for k, segment in enumerate(segments):
        if not segment.Select4(k > 0, None):
            model.ClearSelection2(True)
            raise SolidWorksError("Could not select the segments to move.")
    binding.wrap(model.Extension, mod.IModelDocExtension).MoveOrCopy(
        False, 1, bool(keep_relations), 0.0, 0.0, 0.0, float(du), float(dv), 0.0)
    model.ClearSelection2(True)


def _in_sketch(session, sketch, delta_mm: list) -> tuple:
    """A model vector (mm) as (du, dv) in the sketch (m); refused when it leaves the plane."""
    a = session._sketch_coords(sketch, 0.0, 0.0, 0.0)
    b = session._sketch_coords(sketch, *(mm_to_m(c) for c in delta_mm))
    off = m_to_mm(b[2] - a[2])
    if abs(off) > PLANE_TOLERANCE_MM:
        raise SolidWorksError(f"The move {delta_mm} mm leaves the sketch plane by {off:.4f} mm: a sketch moves "
                              "within its plane (move the plane, or give a vector in it).")
    return b[0] - a[0], b[1] - a[1]


def _put_back(session, sketch, before: dict) -> bool:
    """Set every sketch point back to its coordinates in `before`. A point set drags its
    neighbours through the relations, so the pass is repeated until all are home (they
    were a solution of the relations, so they are one again)."""
    mod = session._mod
    for _ in range(8):
        points = {}
        for raw in sketch.GetSketchPoints2() or ():
            point = binding.wrap(raw, mod.ISketchPoint)
            points[_point_key(point)] = point
        off = [key for key, point in points.items() if key in before
               and (abs(point.X - before[key][0]) > TOLERANCE_M or abs(point.Y - before[key][1]) > TOLERANCE_M)]
        if not off:
            return True
        for key in off:
            points[key].SetCoords(before[key][0], before[key][1], 0.0)
    return False


def sketch_move(session, sketch: str, delta_mm=None, target_mm=None, filter=None,  # noqa: A002 - the tool's word
                anchor: str | None = None, break_relations: bool = False) -> dict:
    """Move segments of an existing 2D sketch by a vector, in one move; see the module text."""
    if (delta_mm is None) == (target_mm is None):
        raise SolidWorksError("Give delta_mm [dx, dy, dz] (the shift) or target_mm [x, y, z] (where the anchor "
                              "point goes), one of them; model mm.")
    session._require_model()
    session._require_part()
    mod = session._mod
    sk, open_sketch, feature = param._edit_sketch(session, sketch)
    try:
        if feature.GetTypeName2() != "ProfileFeature":
            raise SolidWorksError(f"'{feature.Name}' is a 3D sketch; sketch_move moves 2D sketches.")
        transform = tuple(binding.wrap(binding.wrap(open_sketch.ModelToSketchTransform, mod.IMathTransform).Inverse(),
                                       mod.IMathTransform).ArrayData)
        raws = list(open_sketch.GetSketchSegments() or ())
        entries = [batch._segment(session, raw, transform) for raw in raws]
        picked = choose(entries, filter)
        segments = [binding.wrap(raws[i], mod.ISketchSegment) for i in picked]
        chosen_points = {}
        for segment in segments:
            for point in _points_of(session, segment):
                chosen_points[_point_key(point)] = point
        if anchor is not None:
            anchor_point = param._entity(session, open_sketch, anchor)
            if not hasattr(anchor_point, "SetCoords"):
                raise SolidWorksError(f"anchor is a point: '#3.start', '#3.end' or '#3.center' (got {anchor!r}).")
            anchor_point = binding.wrap(anchor_point, mod.ISketchPoint)
        elif chosen_points:
            anchor_point = next(iter(chosen_points.values()))
        else:
            anchor_point = None
        anchor_was = None if anchor_point is None else batch.to_model(
            transform, (anchor_point.X, anchor_point.Y, anchor_point.Z))
        if target_mm is not None:
            if anchor_was is None:
                raise SolidWorksError("target_mm needs a point to bring there: give anchor ('#3.start').")
            delta = [t - a for t, a in zip(_vector(target_mm, "target_mm"), anchor_was)]
        else:
            delta = _vector(delta_mm, "delta_mm")
        du, dv = _in_sketch(session, open_sketch, delta)
        if abs(du) <= TOLERANCE_M and abs(dv) <= TOLERANCE_M:
            raise SolidWorksError("The move is zero: the selection is already there.")

        before = _all_points(session, open_sketch)
        ids = list(chosen_points)
        dims = _dimensions(session, feature)
        values_before = {name: float(dim.SystemValue) for name, dim in dims}
        driving = [(name, dim) for name, dim in dims if dim.DrivenState == SW_DIMENSION_DRIVING]
        by_equation = sketch_datum._driven_by_equation(session)
        # (a dimension an equation sets does not report itself as driving: look at them all)
        tied = {name: by_equation[name.lower()] for name, _dim in dims if name.lower() in by_equation}

        def rest() -> tuple:
            """What the first selected point still lacks of the move, and the points that are off."""
            now = _all_points(session, open_sketch)
            missing = shortfall(before, now, ids, du, dv)
            if not ids:
                return 0.0, 0.0, missing, now
            key = ids[0]
            return before[key][0] + du - now[key][0], before[key][1] + dv - now[key][1], missing, now

        def release(on: bool) -> None:
            for name, dim in driving:
                if name not in tied:       # an equation's dimension is not ours to free
                    dim.DrivenState = SW_DIMENSION_DRIVEN if on else SW_DIMENSION_DRIVING

        how = "free"
        _move(session, segments, du, dv, True)
        rest_u, rest_v, missing, after = rest()
        if missing and driving:
            # dimensions hold the selection: release them, move the rest of the way, lock them again
            # (from where it started: a first move that went part of the way may have bent the shape)
            how = "dimensions"
            release(True)
            try:
                _put_back(session, open_sketch, before)
                _move(session, segments, du, dv, True)
            finally:
                release(False)
            rest_u, rest_v, missing, after = rest()
        if missing and break_relations:
            how = "relations to the rest broken"
            release(True)
            try:
                _put_back(session, open_sketch, before)
                _move(session, segments, du, dv, False)
            finally:
                release(False)
            rest_u, rest_v, missing, after = rest()
        changed = {}
        for name, dim in dims:
            now_value = float(dim.SystemValue)
            if abs(now_value - values_before[name]) > 1e-9:
                changed[name] = (values_before[name], now_value, dim)
        if missing:
            # back to where it was: geometry first (dimensions free), then the dimensions' values
            release(True)
            try:
                home = _put_back(session, open_sketch, before)
            finally:
                release(False)
            for name, dim in driving:
                if name not in tied and abs(float(dim.SystemValue) - values_before[name]) > 1e-12:
                    dim.SystemValue = values_before[name]
            worst = max(missing, key=lambda m: abs(m[1]) + abs(m[2]))
            why = (f"{len(missing)} of {len(ids)} point(s) would be off, the worst by "
                   f"({m_to_mm(worst[1]):.4f}, {m_to_mm(worst[2]):.4f}) mm in the sketch.")
            if tied:
                listed = "; ".join(f"{name}: {equation}" for name, equation in tied.items())
                why += (f" Dimension(s) of this sketch are set by an equation ({listed}) and hold the geometry: "
                        "change the equation or its global variable instead (set_equation / set_global).")
            else:
                why += (" A relation to geometry outside the selection holds it (coincident with the origin or an "
                        "edge, fixed, a converted edge): move the whole sketch, remove that relation, or pass "
                        "break_relations=True to cut the relations between the moved and the rest.")
            raise SolidWorksError("SolidWorks could not move the selection by the whole vector: " + why
                                  + (" The sketch is back as it was." if home else
                                     " The sketch could NOT be put back exactly: read_sketch shows where it is."))
        dragged = bystanders(before, after, ids)
        anchor_now = None if anchor_point is None else batch.to_model(
            transform, (anchor_point.X, anchor_point.Y, anchor_point.Z))
    finally:
        param._close(session, sk)
    rows = {}
    for name, (was, now_value, dim) in changed.items():
        unit, _to_system, from_system = session._dimension_unit(dim)
        rows[name] = {"was": round(from_system(was), 6), "now": round(from_system(now_value), 6), "unit": unit}
    # the status as the rebuild left it (inside the edit it lags behind a re-locked dimension)
    status = int(binding.wrap(feature.GetSpecificFeature2(), mod.ISketch).GetConstrainedStatus())
    result = {"ok": True, "sketch": feature.Name, "segments_moved": len(picked), "points_checked": len(ids),
              "delta_mm": [round(c, 6) for c in delta], "moved_by": how, "dimensions_changed": rows,
              "points_dragged_along": dragged, "fully_defined": status == SW_FULLY_CONSTRAINED,
              "status": SKETCH_STATUSES.get(status, f"status {status}")}
    if anchor_was is not None:
        result["anchor"] = {"was_mm": anchor_was, "now_mm": anchor_now}
        final = _anchor_after(session, feature, anchor_point)
        if final is not None and math.dist(final, anchor_now) > 1e-3:
            raise SolidWorksError(
                f"After the rebuild the moved point is at {final} mm, not {anchor_now}: something outside the sketch "
                f"(an equation, a design table) set it back. Dimensions touched: {rows or 'none'}.")
    if dragged:
        result["note"] = (f"{dragged} point(s) outside the selection moved along: relations tie them to what was "
                          "moved.")
    if not ids:
        result["note"] = "The selection has no points to check by (text, an ellipse): the move was made unchecked."
    return result


def _anchor_after(session, feature, anchor_point) -> list | None:
    """The anchor point's model position (mm) read from the closed, rebuilt sketch."""
    mod = session._mod
    try:
        key = _point_key(anchor_point)
        sketch = binding.wrap(feature.GetSpecificFeature2(), mod.ISketch)
        transform = tuple(binding.wrap(binding.wrap(sketch.ModelToSketchTransform, mod.IMathTransform).Inverse(),
                                       mod.IMathTransform).ArrayData)
        for raw in sketch.GetSketchPoints2() or ():
            point = binding.wrap(raw, mod.ISketchPoint)
            if _point_key(point) == key:
                return batch.to_model(transform, (point.X, point.Y, point.Z))
    except Exception:  # noqa: BLE001 - the check inside the sketch already passed
        return None
    return None
