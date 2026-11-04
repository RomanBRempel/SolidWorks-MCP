"""Parametric building blocks on SolidWorks objects (B1, B2, B3, B6, B7).

The rule these tools serve: everything rests on faces, edges, planes and sketches;
sizes and positions are global variables or equations; no inserted tool
bodies. These tools make that possible through the server:

B1  set_global, link_dimension, feature_dimensions (full names: D1@Plane5).
B2  sketch_circles / sketch_rect: a sketch on a plane or face, fully defined
    by dimensions from the origin, each optionally driven by a variable.
B3  convert_entities, add_relation, and the midline centre: a circle centred
    on the middle of a construction chord between the two sides of a face
    profile at a station - the centre of a hole on the profile's mean line,
    tied by relations so it follows the profile.
B6  plane_normal_to_line: a plane perpendicular to a sketch line through
    one of its end points (a hole whose axis is tilted).
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

    'WALL * 2' -> '"WALL" * 2' when WALL is a known name; a number
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
    units) or an expression of other variables ('WALL * 2'). Rebuilds."""
    if not re.match(r"^[A-Za-z_Ѐ-ӿ][\wЀ-ӿ]*$", str(name)):
        raise SolidWorksError(f"A global variable name is letters, digits and _ (got {name!r}).")
    rhs = value if isinstance(value, (int, float)) else quote_names(value, [g for g in _globals(session) if g != name])
    result = session.set_equations([f'"{name}" = {rhs}'])
    entry = next(e for e in session._equation_entries() if (e.get("name") or "").lower() == name.lower())
    return {"ok": True, "name": name, "equation": entry["equation"], "value": entry["value"],
            "rebuild_ok": result["rebuild_ok"], "failing_features": result["failing_features"]}


def link_dimension(session, dimension: str, expression) -> dict:
    """Drive a dimension ('D1@Plane5', 'diameter@SK_BOSS') by an expression of
    global variables ('WALL', 'HOLE_DEPTH + 2', 'abs(POS_1)'). Bare known
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
