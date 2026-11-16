"""Reading many sketches and writing many dimensions in one call (S-07).

read_sketch on a 222-feature part took ~14 s per sketch (208 s for 15): it
walked the whole tree with every sub-feature to find the sketch, and turned
every point into model space through three COM objects (MathUtility,
MathPoint, MultiplyTransform). read_sketches finds each sketch with one
FeatureByName, reads the sketch's transform once and maps the points in
Python; the segments come as raw dispatches (no call per object to wrap
them). No sketch is opened for editing, nothing is rebuilt, and a 3D sketch
reads the same way.

set_dimension rebuilds after every value (and once more per check): five
dimensions were five rebuilds of the whole tree. set_dimensions writes them
all, rebuilds once, reads every value back, and puts all of them back when
SolidWorks did not take one.
"""

import math
import time

from .. import binding
from ..constants import (SKETCH_STATUSES, SW_DIMENSION_DRIVING, SW_FULLY_CONSTRAINED, SW_SKETCH_ARC,
                         SW_SKETCH_LINE, SW_SKETCH_SPLINE, SW_SKETCH_TEXT)
from ..errors import SolidWorksError
from . import fastpath, render

SKETCH_TYPES = ("ProfileFeature", "3DProfileFeature")
_SEGMENT_NAMES = {SW_SKETCH_SPLINE: "spline", SW_SKETCH_TEXT: "text"}


# --- pure parts (unit-tested) -------------------------------------------------------------------


def to_model(transform, point) -> list:
    """A sketch point (m) in model space (mm). transform: IMathTransform.ArrayData of the
    sketch-to-model transform - rotation rows [0:9] (the sketch's x, y, z in the model),
    shift [9:12], scale [12]; None for a 3D sketch, whose points are model points. Pure."""
    if transform is None:
        return [round(c * 1000.0, 4) + 0.0 for c in point]
    r, shift, scale = transform[0:9], transform[9:12], transform[12] or 1.0
    return [round((sum(point[k] * r[3 * k + axis] for k in range(3)) * scale + shift[axis]) * 1000.0, 4) + 0.0
            for axis in range(3)]


def dimension_targets(values: dict) -> list:
    """[(name, value)] from {name: value}, refusing an empty or malformed request; pure."""
    if not isinstance(values, dict) or not values:
        raise SolidWorksError("set_dimensions takes {dimension: value, ...}, e.g. {'D1@Boss': 12, 'x@SK_A': 3.5}.")
    out = []
    for name, value in values.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise SolidWorksError(f"The value for '{name}' must be a number (mm or degrees), got {value!r}.")
        out.append((str(name), float(value)))
    return out


# --- sketches -----------------------------------------------------------------------------------


def _sketch_feature(session, part, name: str):
    mod = session._mod
    feat = binding.wrap(part.FeatureByName(name), mod.IFeature)
    if feat is None or feat.GetTypeName2() not in SKETCH_TYPES:
        return None
    return feat


def _point(session, raw, transform) -> list:
    p = fastpath._bind(raw, session._mod.ISketchPoint)
    return to_model(transform, (p.X, p.Y, p.Z))


def _segment(session, raw, transform) -> dict:
    mod = session._mod
    segment = binding.wrap(raw, mod.ISketchSegment)
    kind = int(segment.GetType())
    entry = {"construction": bool(segment.ConstructionGeometry)}
    if kind == SW_SKETCH_LINE:
        line = render._raw(binding.wrap(raw, mod.ISketchLine))
        entry.update(type="line", start_mm=_point(session, line.GetStartPoint2(), transform),
                     end_mm=_point(session, line.GetEndPoint2(), transform))
    elif kind == SW_SKETCH_ARC:
        arc = binding.wrap(raw, mod.ISketchArc)
        raw_arc = render._raw(arc)
        circle = bool(arc.IsCircle())
        entry.update(type="circle" if circle else "arc",
                     centre_mm=_point(session, raw_arc.GetCenterPoint2(), transform),
                     radius_mm=round(arc.GetRadius() * 1000.0, 4))
        if not circle:
            entry.update(start_mm=_point(session, raw_arc.GetStartPoint2(), transform),
                         end_mm=_point(session, raw_arc.GetEndPoint2(), transform))
    else:
        entry.update(type=_SEGMENT_NAMES.get(kind, f"segment type {kind}"),
                     length_mm=round(segment.GetLength() * 1000.0, 4))
        if kind == SW_SKETCH_SPLINE:
            points = render._raw(binding.wrap(raw, mod.ISketchSpline)).GetPoints2() or ()
            if points:
                entry.update(start_mm=_point(session, points[0], transform),
                             end_mm=_point(session, points[-1], transform), points=len(points))
    return entry


def _read_one(session, part, name: str, dimensions: bool) -> dict:
    mod = session._mod
    feature = _sketch_feature(session, part, name)
    if feature is None:
        return {"name": name, "ok": False, "error": f"No sketch '{name}' in the part."}
    sketch = binding.wrap(feature.GetSpecificFeature2(), mod.ISketch)
    transform = None
    is_3d = feature.GetTypeName2() == "3DProfileFeature"
    if not is_3d:
        to_model_transform = binding.wrap(
            binding.wrap(sketch.ModelToSketchTransform, mod.IMathTransform).Inverse(), mod.IMathTransform)
        transform = tuple(to_model_transform.ArrayData)
    segments = [_segment(session, raw, transform) for raw in (render._raw(sketch).GetSketchSegments() or ())]
    status = sketch.GetConstrainedStatus()
    out = {"name": feature.Name, "ok": True, "kind": "3d" if is_3d else "2d",
           "fully_defined": status == SW_FULLY_CONSTRAINED,
           "status": SKETCH_STATUSES.get(status, f"status {status}"), "segments": segments}
    if dimensions:
        out["dimensions"] = session._dimension_entries(feature)
    return out


def read_sketches(session, names=None, dimensions: bool = True) -> dict:
    """Several sketches of the current part read back in model coordinates (see the module doc)."""
    model = session._require_model()
    session._require_part()
    part = binding.wrap(model, session._mod.IPartDoc)
    t0 = time.perf_counter()
    if names is None:
        wanted = [f.Name for f in session._iter_features() if f.GetTypeName2() in SKETCH_TYPES]
        for feat in session._history():  # and the sketches features absorbed
            wanted += [s.Name for s in session._sub_features(feat) if s.GetTypeName2() in SKETCH_TYPES]
        wanted = list(dict.fromkeys(wanted))
    else:
        wanted = [names] if isinstance(names, str) else [str(n) for n in names]
    if not wanted:
        raise SolidWorksError("No sketch to read: the part has none.")
    sketches = []
    for name in wanted:
        try:
            sketches.append(_read_one(session, part, name, dimensions))
        except SolidWorksError as exc:
            sketches.append({"name": name, "ok": False, "error": str(exc)})
    missing = [s["name"] for s in sketches if not s["ok"]]
    if missing and len(missing) == len(sketches):
        raise SolidWorksError(f"None of the sketches {missing} is in the part (list_features shows them).")
    return {"ok": True, "count": len(sketches) - len(missing), "sketches": sketches, "missing": missing,
            "segments": sum(len(s.get("segments", ())) for s in sketches),
            "elapsed_s": round(time.perf_counter() - t0, 2)}


# --- dimensions ---------------------------------------------------------------------------------


def set_dimensions(session, values: dict) -> dict:
    """Set several driving dimensions and rebuild ONCE (see the module doc)."""
    model = session._require_model()
    targets = dimension_targets(values)
    plan = []
    for name, value in targets:
        dim = session._dimension(name)
        unit, to_system, from_system = session._dimension_unit(dim)
        coordinate = session._coordinate_of(name)
        if coordinate is None and unit == "mm" and value < 0:
            raise SolidWorksError(f"'{name}' is a size and cannot be negative (got {value:g}); only a point's "
                                  "coordinate from the origin takes a sign. Nothing was changed.")
        if dim.DrivenState != SW_DIMENSION_DRIVING:
            raise SolidWorksError(f"'{name}' is a reference (driven) dimension: it follows the geometry. "
                                  "Nothing was changed.")
        original = dim.SystemValue
        old = from_system(original) if coordinate is None else coordinate()
        target = session._joint_angle(dim, unit, value) if coordinate is None else value
        crosses = coordinate is not None and (value < 0) != (old < 0)
        write = to_system(-abs(value) if crosses else abs(value) if coordinate is not None else target)
        plan.append({"name": name, "dim": dim, "unit": unit, "from": from_system, "coordinate": coordinate,
                     "original": original, "old": old, "target": target, "value": value, "write": write})

    def read(entry) -> float:
        if entry["coordinate"] is None:
            return entry["from"](entry["dim"].SystemValue)
        return session._coordinate_of(entry["name"])()

    def write_all(key: str) -> None:
        for entry in plan:
            for _ in range(2):  # SolidWorks now and then ignores a write (upstream: 3 in 80)
                entry["dim"].SystemValue = entry[key]
                if abs(abs(entry["dim"].SystemValue) - abs(entry[key])) < 1e-9:
                    break

    t0 = time.perf_counter()
    write_all("write")
    rebuilt_ok = bool(model.ForceRebuild3(False))
    refused = [e for e in plan if abs(read(e) - e["target"]) >= 1e-6]
    if refused:
        write_all("original")
        model.ForceRebuild3(False)
        reasons = "; ".join(f"'{e['name']}' stays at {e['old']:g} {e['unit']} "
                            f"({session._why_not_taken(e['name'], e['dim'])})" for e in refused)
        raise SolidWorksError(f"SolidWorks did not take {len(refused)} of {len(plan)} value(s): {reasons}. "
                              "All dimensions are back at their old values.")
    rows = [{"dimension": e["name"], f"old_value_{e['unit']}": round(e["old"], 6),
             f"new_value_{e['unit']}": round(read(e) + e["value"] - e["target"], 6)} for e in plan]
    return {"ok": True, "count": len(rows), "dimensions": rows, "rebuilds": 1, "rebuild_ok": rebuilt_ok,
            "rebuild_s": round(time.perf_counter() - t0, 2), "failing_features": session._failing_features(),
            "mass_properties": session.get_mass_properties()["mass_properties"]}
