"""A channel or a rod along a route in the part being worked on (S-06).

upstream's add_swept_pipe draws its path on the Front plane of a new part. A
route through an existing body (a hose channel, a wire duct) needs a path in
space and a cut:

add_sketch3d   a 3D sketch of straight runs through the given points, the
               corners rounded by sketch fillets whose radius is a dimension
               (one per group of equal radii), everything else fixed - so the
               radius is the route's one parameter, changed by set_dimension.
sweep_cut      a swept cut along a sketch: a circular profile of diameter_mm
               (the diameter is the feature's own dimension) or a profile sketch.
sweep_boss     the same as material.

Both sweeps take bodies=[...] and expect={...} through scope.runner.
Signatures read from the SOLIDWORKS 2025 type library (InsertCutSwept5 /
InsertProtrusionSwept4); constants from swconst.tlb.
"""

import math

from .. import binding
from ..constants import (SW_CONSTRAINED_CORNER_KEEP, SW_CONSTRAINT_RADIUS, SW_FULLY_CONSTRAINED,
                         SW_RELATIONS_ALL)
from ..errors import SolidWorksError
from ..units import mm_to_m
from . import geom, param

MARK_PROFILE, MARK_PATH = 1, 4     # the sweep reads its profile and its path by these selection marks
SKETCH_TYPES = ("ProfileFeature", "3DProfileFeature")


# --- pure geometry (unit-tested) ---------------------------------------------------------------


def corner_radii(count: int, bend_radius_mm=None, fillets=None) -> list:
    """One radius per interior corner (0 = sharp) from bend_radius_mm (all corners) or
    fillets (a list, one per corner); pure."""
    if bend_radius_mm is not None and fillets is not None:
        raise SolidWorksError("Give bend_radius_mm (every corner) or fillets (one radius per corner), not both.")
    if fillets is not None:
        radii = [float(r or 0.0) for r in fillets]
        if len(radii) != count:
            raise SolidWorksError(f"fillets needs one radius per interior corner: {count} (got {len(radii)}).")
    else:
        radii = [float(bend_radius_mm or 0.0)] * count
    if any(r < 0 for r in radii):
        raise SolidWorksError("A fillet radius cannot be negative (0 leaves the corner sharp).")
    return radii


def route_check(points_mm: list, radii: list) -> dict:
    """Does every fillet fit between its neighbours? Returns the route's length and each
    corner's angle; refuses a radius whose tangent lengths overrun a run. Pure."""
    if len(points_mm) < 2 or any(len(p) != 3 for p in points_mm):
        raise SolidWorksError("points_mm needs at least two [x, y, z] points.")
    runs = [math.dist(a, b) for a, b in zip(points_mm, points_mm[1:])]
    if any(run < 1e-6 for run in runs):
        raise SolidWorksError("Two consecutive route points coincide.")
    tangents, angles, arcs = [], [], []
    for i, radius in enumerate(radii):
        a, b, c = points_mm[i], points_mm[i + 1], points_mm[i + 2]
        u = [(q - p) / runs[i] for p, q in zip(a, b)]
        v = [(q - p) / runs[i + 1] for p, q in zip(b, c)]
        turn = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v)))))  # 0 = straight on
        angles.append(round(math.degrees(turn), 4))
        if radius and turn < 1e-6:
            raise SolidWorksError(f"Corner {i + 1} is straight (the route does not turn there): give it radius 0.")
        if radius and abs(turn - math.pi) < 1e-6:
            raise SolidWorksError(f"The route doubles back on itself at corner {i + 1}.")
        tangents.append(radius * math.tan(turn / 2) if radius else 0.0)
        arcs.append(radius * turn if radius else 0.0)
    for i, run in enumerate(runs):
        used = (tangents[i - 1] if i > 0 else 0.0) + (tangents[i] if i < len(tangents) else 0.0)
        if used > run + 1e-9:
            raise SolidWorksError(
                f"The fillets do not fit on run {i + 1} ({run:.3f} mm long): their tangent lengths need "
                f"{used:.3f} mm. Use a smaller radius or move the points apart.")
    length = sum(runs) - 2 * sum(tangents) + sum(arcs)
    return {"length_mm": round(length, 4), "corner_angles_deg": angles,
            "straight_mm": [round(run - (tangents[i - 1] if i > 0 else 0.0)
                                  - (tangents[i] if i < len(tangents) else 0.0), 4) for i, run in enumerate(runs)]}


def radius_groups(radii: list) -> list:
    """[(radius, [corner indices])] for the rounded corners, equal radii together; pure."""
    groups = {}
    for i, radius in enumerate(radii):
        if radius > 0:
            groups.setdefault(round(radius, 9), []).append(i)
    return sorted(groups.items(), key=lambda item: item[1][0])


# --- 3D sketch ----------------------------------------------------------------------------------


def _radius_dimensions(session, sketch) -> list:
    mod = session._mod
    relations = binding.wrap(sketch.RelationManager, mod.ISketchRelationManager)
    found = []
    for raw in relations.GetRelations(SW_RELATIONS_ALL) or ():
        relation = binding.wrap(raw, mod.ISketchRelation)
        try:
            if relation.GetRelationType() != SW_CONSTRAINT_RADIUS:
                continue
            display = binding.wrap(relation.GetDisplayDimension(), mod.IDisplayDimension)
            found.append(binding.wrap(display.GetDimension2(0), mod.IDimension))
        except Exception:  # noqa: BLE001
            continue
    return found


def add_sketch3d(session, points_mm: list, bend_radius_mm=None, fillets=None, name: str | None = None,
                 vars: dict | None = None) -> dict:
    """A 3D sketch through points_mm, its corners rounded (see the module doc)."""
    model = session._require_model()
    session._require_part()
    mod = session._mod
    points = [[float(c) for c in p] for p in points_mm]
    radii = corner_radii(max(0, len(points) - 2), bend_radius_mm, fillets)
    route = route_check(points, radii)
    names_before = {f.Name for f in session._iter_features()}
    sk = binding.wrap(model.SketchManager, mod.ISketchManager)
    model.ClearSelection2(True)
    sk.Insert3DSketch(True)
    dimensions, fully_defined = {}, False
    try:
        sketch = binding.wrap(model.GetActiveSketch2(), mod.ISketch)
        if sketch is None:
            raise SolidWorksError("SolidWorks opened no 3D sketch.")
        lines = []
        sk.AddToDB = True
        try:
            for a, b in zip(points, points[1:]):
                line = sk.CreateLine(*(mm_to_m(c) for c in a), *(mm_to_m(c) for c in b))
                if line is None:
                    raise SolidWorksError(f"SolidWorks drew no line from {a} to {b}.")
                lines.append(binding.wrap(line, mod.ISketchLine))
        finally:
            sk.AddToDB = False
        ends = [(binding.wrap(line.GetStartPoint2(), mod.ISketchPoint),
                 binding.wrap(line.GetEndPoint2(), mod.ISketchPoint)) for line in lines]
        # the straight runs are fixed where they lie; a fixed line's end points still slide
        # along it, which is what lets a fillet's radius change
        for line in lines:
            param._relate(session, sketch, "fixed", binding.wrap(line, mod.ISketchSegment))
        groups = radius_groups(radii)
        for radius, corners in groups:
            before = {d.GetNameForSelection() for d in _radius_dimensions(session, sketch)}
            model.ClearSelection2(True)
            if not all(ends[i][1].Select4(k > 0, None) for k, i in enumerate(corners)):
                raise SolidWorksError(f"Could not select corner(s) {[c + 1 for c in corners]} to round.")
            if sk.CreateFillet(mm_to_m(radius), SW_CONSTRAINED_CORNER_KEEP) is None:
                raise SolidWorksError(f"SolidWorks could not round corner(s) {[c + 1 for c in corners]} "
                                      f"with R{radius:g}.")
            new = [d for d in _radius_dimensions(session, sketch) if d.GetNameForSelection() not in before]
            if len(new) != 1:
                raise SolidWorksError(f"Rounding corner(s) {[c + 1 for c in corners]} gave {len(new)} radius "
                                      "dimension(s); one was expected.")
            role = "radius" if len(groups) == 1 else f"r{corners[0] + 1}"
            new[0].Name = role
            dimensions[role] = new[0].GetNameForSelection()
        model.ClearSelection2(True)
        for point in (ends[0][0], ends[-1][1]):  # the route's two ends
            try:
                param._relate(session, sketch, "fixed", point)
            except SolidWorksError:
                pass  # already held by the fixed line and a fillet's tangency
        fully_defined = sketch.GetConstrainedStatus() == SW_FULLY_CONSTRAINED
    finally:
        model.ClearSelection2(True)
        sk.Insert3DSketch(True)
    new_names = [f for f in session._iter_features()
                 if f.Name not in names_before and f.GetTypeName2() == "3DProfileFeature"]
    if len(new_names) != 1:
        raise SolidWorksError(f"Could not identify the 3D sketch just drawn ({len(new_names)} new 3D sketches).")
    feature = new_names[0]
    if name:
        old = feature.Name
        feature.Name = name
        if feature.Name != name:
            raise SolidWorksError(f"The sketch could not be named '{name}' (it is '{feature.Name}'): is the name taken?")
        dimensions = {role: dim.replace(f"@{old}", f"@{name}") for role, dim in dimensions.items()}
    equations = []
    if vars:
        unknown = [k for k in vars if k not in dimensions]
        if unknown:
            raise SolidWorksError(f"No dimension {unknown} to link (has: {', '.join(dimensions) or 'none'}).")
        equations = param._link_all(session, [(dimensions[k], str(v)) for k, v in vars.items()])
    return {"ok": True, "sketch": feature.Name, "points_mm": points, "fillets_mm": radii,
            "dimensions": dimensions, "equations": equations, "fully_defined": fully_defined, **route}


# --- sweeps -------------------------------------------------------------------------------------


def _sketch_feature(session, name: str, what: str):
    mod = session._mod
    part = binding.wrap(session._require_model(), mod.IPartDoc)
    feat = binding.wrap(part.FeatureByName(name), mod.IFeature)
    if feat is None or feat.GetTypeName2() not in SKETCH_TYPES:
        raise SolidWorksError(f"No sketch '{name}' for the sweep's {what} (a 2D or 3D sketch of the part; "
                              "list_features shows them).")
    return feat


def _sweep(session, cut: bool, path: str, diameter_mm, profile, name, merge: bool) -> dict:
    model = session._require_model()
    session._require_part()
    mod = session._mod
    if (diameter_mm is None) == (profile is None):
        raise SolidWorksError("Give diameter_mm (a round profile) or profile (a sketch), one of them.")
    if diameter_mm is not None and float(diameter_mm) <= 0:
        raise SolidWorksError(f"diameter_mm must be > 0 (got {diameter_mm}).")
    path_feat = _sketch_feature(session, path, "path")
    profile_feat = _sketch_feature(session, profile, "profile") if profile is not None else None
    volume_before = None
    if cut:
        bodies = session._part_bodies()
        if not bodies:
            raise SolidWorksError("The part has no solid body to cut.")
        volume_before = sum(float(b.GetMassProperties(1.0)[3]) for b in bodies) * 1e9
    model.ClearSelection2(True)
    if profile_feat is not None and not profile_feat.Select2(False, MARK_PROFILE):
        raise SolidWorksError(f"Could not select the profile '{profile}'.")
    if not path_feat.Select2(profile_feat is not None, MARK_PATH):
        raise SolidWorksError(f"Could not select the path '{path}'.")
    fm = binding.wrap(model.FeatureManager, mod.IFeatureManager)
    round_profile = diameter_mm is not None
    diameter = mm_to_m(float(diameter_mm)) if round_profile else 0.0
    if cut:
        made = fm.InsertCutSwept5(
            False, True, 0, False,      # Propagate, Alignment, TwistCtrlOption, KeepTangency
            False, 0, 0,                # BAdvancedSmoothing, Start/EndMatchingType
            False, 0.0, 0.0, 0,         # IsThinBody, Thickness1, Thickness2, ThinType
            0, True, True,              # PathAlign, UseFeatScope, UseAutoSelect
            0.0, True,                  # TwistAngle, BMergeSmoothFaces
            False, False, False,        # assembly feature scope (parts: unused)
            round_profile, diameter, 0)  # CircularProfile, CircularProfileDiameter, Direction
    else:
        made = fm.InsertProtrusionSwept4(
            False, True, 0, False, False, 0, 0, False, 0.0, 0.0, 0, 0,
            bool(merge), True, True, 0.0, True, round_profile, diameter, 0)
    model.ClearSelection2(True)
    kind = "swept cut" if cut else "sweep"
    if made is None:
        refusal = geom.feature_refusal(session, f"the {kind} along '{path}'")
        raise SolidWorksError(
            f"{refusal} Check that the path is one connected, tangent chain"
            + (", that it runs through the body" if cut else "")
            + (f" and that R >= {float(diameter_mm) / 2:g} mm at every bend (a round profile cannot turn tighter "
               "than its own radius)." if round_profile else " and that the profile is closed and crosses the path."))
    feature = binding.wrap(made, mod.IFeature)
    result = session._finish_feature(feature, name or ("SweepCut" if cut else "Sweep"), path=path)
    dims = session._dimension_entries(feature)
    if round_profile and dims:
        result["dimensions"] = {"diameter": dims[0]["name"]}
    elif profile is not None:
        result["profile"] = profile
    if cut:
        volume_after = (result.get("mass_properties") or {}).get("volume_mm3")
        if volume_after is not None and volume_before - volume_after <= 1e-6:
            raise SolidWorksError(f"The swept cut along '{path}' removed no material: the path does not run "
                                  "through a body. Rolled back.")
        if volume_after is not None:
            result["removed_mm3"] = round(volume_before - volume_after, 3)
    return result


def sweep_cut(session, path: str, diameter_mm=None, profile: str | None = None, name: str | None = None) -> dict:
    """Cut a channel along a sketch: a round profile of diameter_mm, or a profile sketch."""
    return _sweep(session, True, path, diameter_mm, profile, name, True)


def sweep_boss(session, path: str, diameter_mm=None, profile: str | None = None, name: str | None = None,
               merge: bool = True) -> dict:
    """Add material along a sketch: a round profile of diameter_mm, or a profile sketch."""
    return _sweep(session, False, path, diameter_mm, profile, name, merge)
