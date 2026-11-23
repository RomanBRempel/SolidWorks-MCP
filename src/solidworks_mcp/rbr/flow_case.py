"""Reproducible Flow Simulation runs: what the solver was actually given, kept and compared.

Everything here is pure (files in, dicts out; no SolidWorks), so it is unit-tested
and usable on a machine without Flow.

The ground truth of a run is the solver's own settings file, <folder>/<n>.xmlconfig:
Flow writes it from the project when a calculation is prepared, and it carries what
no API reports in one place - the base mesh (cells per axis, control planes with
their cell counts and stretch ratios), the refinement levels, the gap and wall
sizes, the domain, the ambient conditions, the goals' convergence criteria. Two
runs are comparable when these agree; a project made on another PC from that PC's
template agreed in name only (measured: a uniform base mesh and a thin-channel
level of 5 instead of stretched planes and 2 moved a lift force by 4 %).

- read_config: the file as a flat {path: value} map. It is not well-formed XML
  (raw '<' inside attribute values), so it is tokenised here.
- mesh_of / conditions_of: the parts people read.
- fingerprint: a hash of every setting that is not a name, a path or an id.
- make_case / check_case: a case file - the settings of a reference run, to hold
  later runs against - and the differences of a run from it.
- owner / foreign: whose results a project folder holds. A file copy of a model
  keeps pointing at the original's result folders, and a folder synced between
  PCs can be written by both.
- goal_stats, result_record, register_run: the run's result.json and one line in
  the run registry.
"""

import csv
import hashlib
import json
import os
import re
import time

from . import safefile

SCHEMA = 1

_TAG = re.compile(r'<(/?)([A-Za-z_][\w.\-]*)((?:\s+[\w.\-]+\s*=\s*"[^"]*")*)\s*(/?)>', re.S)
_ATTR = re.compile(r'([\w.\-]+)\s*=\s*"([^"]*)"', re.S)

# Not settings: names, ids, paths, who ran it, message texts. A project's own UUIDs change
# with every clone; face keys and body ids change with every file copy of the model.
VOLATILE = re.compile(
    r"(^|/)(JobOwnerName|JobProjectName|PostModelName|PostConfigName|ProjectName|ProjectUUID|PEP|"
    r"NewCalcStartCptName|CADVersionInfo|LicenseType|EducationalVersion|PerformanceMonitoring)$"
    r"|(^|/)WarningCommentFormat\d*$"
    r"|(^|/)ExportProjectToNastran"
    r"|(^|/)(GUID|UUID|FeatureUUID|FeatureRealUUID|Faces_Keys|Excluded_Faces_Keys|BodiesIDs|"
    r"DisabledBodiesIDs|InsulatorBodiesIDs|RefinementGoalConvergenceGuids)(@size)?$"
    r"|(^|/)(Name|FeatureNAME)$"
    r"|(^|/)RefMaxCells$")      # the refinement cell budget follows the PC's memory

ROOT = "settings/"
MESH = ROOT + "BaseMeshSettings[0]/"
AXES = ("x", "y", "z")
# ComputationalDomainSize is "xmin ymin zmin xmax ymax zmax"
DOMAIN_ORDER = ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")
MESH_SCALARS = (
    "Nx", "Ny", "Nz", "CaptureLevel", "CellPerGap", "ChannelRefinementMethod", "EnableThinChannelRefinement",
    "MaxThinChannelRefinementLevel", "EnableMaximumChannelsThickness", "MaximumChannelsThickness",
    "EnableMinimumChannelsThickness", "MinimumChannelsThickness", "IgnoreThinChannelsHeight",
    "CurvatureRefinementCriteria", "CurvatureUnrefinementCriteria", "ToleranceCriteriaRefinementLevel",
    "ToleranceCriteriaValue", "RefinementLevel", "RefineAllCells", "AllCellsRefinementLevel",
    "RefineAllFluidCells", "FluidCellsRefinementLevel", "RefineAllPartialCells", "PartialCellsRefinementLevel",
    "RefineAllSolidCells", "SolidCellsRefinementLevel")
GLOBAL_MESH_KEYS = ("ResultResolution", "MinimalGapSize", "MinimalWallThickness", "ManualSolverRefinement",
                    "SolverRefinementLevel", "UsingBooleanOpInMesher")


class CaseError(ValueError):
    """A file that is not there, not readable, or not what it should be."""


# --- the solver's settings file ---------------------------------------------------------------------


def parse_config(text: str) -> dict:
    """{path: value} of an xmlconfig's text. A path is the element names from the root,
    'settings/BaseMeshSettings[0]/Nx'; an element's index attribute becomes '[n]', its
    value attribute or its text the value, any other attribute 'path@name'."""
    out = {}
    stack = []
    opened = None       # (path, where its text starts) of the element just opened
    for m in _TAG.finditer(text):
        closing, name, attrs, single = m.group(1), m.group(2), dict(_ATTR.findall(m.group(3))), m.group(4)
        if closing:
            if opened and opened[0] == "/".join(stack):
                body = " ".join(text[opened[1]:m.start()].split())
                if body:
                    out[opened[0]] = body
            opened = None
            if stack:
                stack.pop()
            continue
        opened = None       # the parent has a child: its text is layout, not a value
        label = name + (f"[{attrs['index']}]" if "index" in attrs else "")
        path = "/".join(stack + [label])
        for key, value in attrs.items():
            if key == "index" or not stack:
                continue
            target = path if key == "value" else f"{path}@{key}"
            n = 1
            while target in out:        # the same element twice without an index: number it
                n += 1
                target = (path if key == "value" else f"{path}@{key}") + f"#{n}"
            out[target] = value
        if not single:
            stack.append(label)
            opened = ("/".join(stack), m.end())
    return out


def read_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            flat = parse_config(fh.read())
    except OSError as exc:
        raise CaseError(f"Cannot read '{path}': {exc.strerror or exc}.") from None
    if not any(k.startswith(ROOT) for k in flat):
        raise CaseError(f"'{path}' is not a Flow solver settings file (no <settings> element).")
    return flat


def config_path(folder: str) -> str | None:
    """<folder>/<n>.xmlconfig of a project folder; None when the project was never prepared."""
    if not folder or not os.path.isdir(folder):
        return None
    own = os.path.join(folder, os.path.basename(os.path.normpath(folder)) + ".xmlconfig")
    if os.path.isfile(own):
        return own
    found = [n for n in os.listdir(folder) if n.lower().endswith(".xmlconfig")]
    return os.path.join(folder, found[0]) if len(found) == 1 else None


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return text
    return int(value) if value.is_integer() and "." not in str(text) and "e" not in str(text).lower() else value


def _numbers(text) -> list:
    return [_number(t) for t in str(text or "").split()]


def mesh_of(flat: dict) -> dict:
    """The mesh the solver was given: {level, min_gap_m, min_wall_m, domain_m, cells: [nx, ny, nz],
    planes: {x: {points_m, cells, ratios}, ...}, refinement: {...}}.

    planes: the control planes of an axis cut it into intervals; interval i runs from
    points_m[i] to points_m[i + 1] with cells[i] cells and ratios[i] the size of its first
    cell over its last (1 = even; the last entry of cells and ratios belongs to no interval)."""
    def g(key):
        return flat.get(ROOT + key)

    domain = _numbers(g("ComputationalDomainSize"))
    out = {
        "result_resolution": _number(g("ResultResolution")),
        "min_gap_m": _number(g("MinimalGapSize")),
        "min_wall_m": _number(g("MinimalWallThickness")),
        "domain_m": dict(zip(DOMAIN_ORDER, domain)) if len(domain) == 6 else None,
        "cells": [_number(flat.get(MESH + n)) for n in ("Nx", "Ny", "Nz")],
        "planes": {},
        "refinement": {k: _number(flat.get(MESH + k)) for k in MESH_SCALARS[3:] if MESH + k in flat},
    }
    for i, axis in enumerate(AXES):
        base = f"{MESH}ControlPoints[{i}]/"
        if base + "Points" not in flat:
            continue
        out["planes"][axis] = {"points_m": _numbers(flat.get(base + "Points")),
                               "cells": _numbers(flat.get(base + "NumOfCells")),
                               "ratios": _numbers(flat.get(base + "Ratio")),
                               "manual": _numbers(flat.get(base + "Manual"))}
    for key in GLOBAL_MESH_KEYS[3:]:
        if ROOT + key in flat:
            out["refinement"][key] = _number(g(key))
    return out


def intervals_of(lines, tolerance: float = 1e-6) -> list:
    """The base mesh's grid lines along one axis -> its intervals between control planes:
    [{from_m, to_m, cells, first_m, last_m, ratio}], ratio = first cell / last cell (1 = even).

    Flow reports the lines, not the planes; an interval is a run of cells whose size grows
    by one constant factor, so a new run starts where the factor changes."""
    lines = [float(v) for v in lines or ()]
    steps = [b - a for a, b in zip(lines, lines[1:])]
    if not steps:
        return []
    runs, start, factor = [], 0, None
    for i in range(1, len(steps)):
        q = steps[i] / steps[i - 1]
        if factor is None and i - start == 1:
            factor = q
            # one cell is not a trend: it belongs to the next run only if that run keeps this factor
            continue
        if abs(q - factor) > tolerance:
            cut = i
            # a run of two cells whose "factor" was really the jump between two runs
            if i - start == 2 and abs(q - 1.0) <= tolerance and abs(factor - 1.0) > tolerance:
                cut = start + 1
            runs.append((start, cut))
            start, factor = cut, (q if cut != i else None)
    runs.append((start, len(steps)))
    out = []
    for a, b in runs:
        out.append({"from_m": lines[a], "to_m": lines[b], "cells": b - a, "first_m": steps[a],
                    "last_m": steps[b - 1], "ratio": round(steps[a] / steps[b - 1], 4)})
    return out


def mesh_keys(flat: dict) -> dict:
    """The flat entries that decide the mesh (what check_case(scope='mesh') compares)."""
    keep = {k: v for k, v in flat.items() if k.startswith(MESH)}
    for key in GLOBAL_MESH_KEYS + ("ComputationalDomainSize",):
        if ROOT + key in flat:
            keep[ROOT + key] = flat[ROOT + key]
    return keep


def stable(flat: dict) -> dict:
    """The settings without names, ids, paths and message texts."""
    return {k: v for k, v in flat.items() if not VOLATILE.search(k)}


def _canonical(value: str) -> str:
    """Numbers compared as numbers: '0.30000000000000004' and '0.3' are one setting."""
    parts = []
    for token in str(value).split():
        try:
            parts.append(repr(round(float(token), 12)))
        except ValueError:
            parts.append(token)
    return " ".join(parts)


def fingerprint(flat: dict, scope: str = "all") -> str:
    """sha256 over the stable settings ('all') or the mesh ones ('mesh'), 16 hex digits."""
    chosen = stable(flat) if scope == "all" else stable(mesh_keys(flat))
    digest = hashlib.sha256()
    for key in sorted(chosen):
        digest.update(f"{key}={_canonical(chosen[key])}\n".encode("utf-8"))
    return digest.hexdigest()[:16]


def differences(have: dict, want: dict, scope: str = "all", limit: int = 60) -> list:
    """[{key, have, want}] between two flat settings maps, names and ids aside."""
    a, b = (stable(have), stable(want)) if scope == "all" else (stable(mesh_keys(have)), stable(mesh_keys(want)))
    out = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None or _canonical(va) != _canonical(vb):
            out.append({"key": key[len(ROOT):] if key.startswith(ROOT) else key, "have": va, "want": vb})
    return out[:limit] if limit else out


def environment_of(flat: dict) -> dict:
    """Who and what made the settings file: {cad, owner, model, configuration, project, project_uuid}."""
    def g(key):
        return flat.get(ROOT + key)

    owner = g("JobOwnerName") or ""
    return {"cad": g("CADVersionInfo"), "computer": owner.split("\\")[0] or None, "owner": owner or None,
            "model": g("PostModelName"), "configuration": g("PostConfigName"), "project": g("ProjectName"),
            "project_uuid": g("ProjectUUID")}


# --- whose results a folder holds ----------------------------------------------------------------------


def read_info(folder: str, attempts: int = 1, wait_s: float = 0.5) -> dict:
    """<folder>/<n>.info.json (the solver's progress and summary file); {} when there is none.

    Bytes after the JSON (NUL padding, the tail of a longer older version) are ignored;
    a file caught mid-write is read again up to `attempts` times."""
    if not folder or not os.path.isdir(folder):
        return {}
    for attempt in range(max(1, int(attempts))):
        if attempt:
            time.sleep(wait_s)
        try:
            names = [n for n in os.listdir(folder) if n.lower().endswith(".info.json")]
            if not names:
                return {}
            path = max((os.path.join(folder, n) for n in names), key=os.path.getmtime)
            data = safefile.read_json(path)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


_INFO_CELLS = re.compile(r"^\s*Total cells:\s*([\d \t]+?)\s*$", re.M)
_INFO_FLUID = re.compile(r"^\s*Fluid cells:\s*([\d \t]+?)\s*$", re.M)
_INFO_ITER = re.compile(r"^\s*Iterations:\s*(\d+)\s*$", re.M)
_INFO_TRAVELS = re.compile(r"^\s*Travels:\s*([-+0-9.,eE]+)\s*$", re.M)


def parse_info_text(text: str) -> dict:
    """cells, cells_fluid, iterations, travels from the solver's plain-text <n>.info; pure."""
    out = {}
    for key, rx, cast in (("cells", _INFO_CELLS, int), ("cells_fluid", _INFO_FLUID, int),
                          ("iterations", _INFO_ITER, int), ("travels", _INFO_TRAVELS, float)):
        m = rx.search(text or "")
        if m:
            try:
                out[key] = cast(re.sub(r"\s", "", m.group(1)).replace(",", "."))
            except ValueError:
                pass
    return out


def read_info_text(folder: str) -> dict:
    """parse_info_text of <folder>/<n>.info (the newest); {} when there is none."""
    try:
        names = [n for n in os.listdir(folder or "") if n.lower().endswith(".info")]
        if not names:
            return {}
        path = max((os.path.join(folder, n) for n in names), key=os.path.getmtime)
        return parse_info_text(safefile.read_text(path, "utf-8-sig"))
    except OSError:
        return {}


def run_size(folder: str, info: dict | None = None, goals: dict | None = None) -> dict:
    """{cells, cells_fluid, iterations, travels, source}: from <n>.info.json, else from the
    plain-text <n>.info, the iterations at last from Goals.DAT - a registry line always
    says how big the run was and how long it ran when the solver left any trace of it."""
    info = read_info(folder) if info is None else info
    mesh = info.get("mesh") or {}
    tel = info.get("telemetry") or {}
    out = {"cells": mesh.get("cells_total"), "cells_fluid": mesh.get("cells_fluid"),
           "iterations": tel.get("iteration"), "travels": tel.get("travel")}
    source = {k: "info.json" for k, v in out.items() if v is not None}
    if any(v is None for v in out.values()):
        for k, v in read_info_text(folder).items():
            if out.get(k) is None:
                out[k], source[k] = v, "info"
    if out["iterations"] is None:
        goals = goal_stats(folder) if goals is None else goals
        its = [g.get("iterations") for g in goals.values() if isinstance(g, dict) and g.get("iterations")]
        if its:
            out["iterations"], source["iterations"] = max(its), "Goals.DAT"
    out["source"] = source
    return out


def owner(folder: str) -> dict | None:
    """Whose calculation a project folder holds: {model, project, project_uuid, computer, cells,
    finished, source}; None for a folder without a prepared calculation."""
    info = read_info(folder)
    path = config_path(folder)
    env = {}
    if path:
        try:
            env = environment_of(read_config(path))
        except CaseError:
            env = {}
    if not info and not env:
        return None
    system = info.get("system_info") or {}
    return {"model": info.get("model_name") or env.get("model"),
            "project": info.get("project_name") or env.get("project"),
            "project_uuid": env.get("project_uuid"),
            "computer": system.get("computer_name") or env.get("computer"),
            "cells": (info.get("mesh") or {}).get("cells_total"),
            "finished": info.get("finished"),
            "has_results": bool(info.get("finished")) or any(
                n.lower().endswith(".fld") and not n.lower().startswith("r_0") for n in os.listdir(folder))}


def foreign(holder: dict | None, document: str, computer: str | None, project: str | None = None) -> str | None:
    """Why a project folder is not this document's to write into, or None when it is.

    A file copy of a model carries the original's Flow projects and they keep the
    original's folders (next to the file: the same folders when the copy sits beside it);
    a folder on a synced drive may hold another PC's run."""
    if not holder:
        return None
    theirs = os.path.basename(str(holder.get("model") or "")).lower()
    mine = os.path.basename(str(document or "")).lower()
    if theirs and mine and theirs != mine:
        return (f"the folder holds the calculation of another model, '{holder.get('model')}' (project "
                f"'{holder.get('project')}'): this document is a copy that shares its Flow folders")
    if project and holder.get("project") and str(holder["project"]).lower() != str(project).lower():
        return (f"the folder holds the calculation of another project, '{holder.get('project')}'")
    if computer and holder.get("computer") and str(holder["computer"]).lower() != str(computer).lower():
        return (f"the folder holds a calculation made on another computer, {holder.get('computer')} "
                f"(this one is {computer})")
    return None


# --- a case file ---------------------------------------------------------------------------------------


# The solver's numbering of the ambient parameters (its own, not an API enum). Named from
# runs whose conditions were set to known values; the rest are reported as 'parameter_<n>'.
AMBIENT = {0: "pressure_pa", 1: "temperature_k", 100: "velocity_x_mps", 101: "velocity_y_mps",
           102: "velocity_z_mps"}
VELOCITY_TYPES = (100, 101, 102)
_AMBIENT_VALUE = re.compile(r"^settings/AmbientParameter\[0\]/ExcelParameterCollection\[0\]/"
                            r"ExcelParameter\[(\d+)\]/ExcelParameterValue$")


def ambient_keys(flat: dict) -> dict:
    """{parameter type: the flat key of its value} of the ambient (free-stream) conditions."""
    out = {}
    for key in flat:
        m = _AMBIENT_VALUE.match(key)
        if m:
            kind = flat.get(key[:-len("ExcelParameterValue")] + "ExcelParameterType")
            try:
                out[int(kind)] = key
            except (TypeError, ValueError):
                continue
    return out


def conditions_of(flat: dict) -> dict:
    """The ambient (free-stream) conditions as the solver has them: {pressure_pa, temperature_k,
    velocity_x_mps, ..., parameter_<type>: value}."""
    out = {}
    for kind, key in sorted(ambient_keys(flat).items()):
        out[AMBIENT.get(kind, f"parameter_{kind}")] = _number(flat[key])
    return out


def velocity_of(flat: dict) -> list | None:
    keys = ambient_keys(flat)
    if not all(k in keys for k in VELOCITY_TYPES):
        return None
    return [_number(flat[keys[k]]) for k in VELOCITY_TYPES]


# In the automatic mode Flow derives the base mesh from the level, the gap, the domain, the
# model's box and the free stream: these entries follow from the rest, and differ rightly
# between two angles of attack or two geometries calculated the same way.
DERIVED = re.compile(r"^BaseMeshSettings\[0\]/(Nx|Ny|Nz|ToleranceCriteriaValue|"
                     r"ControlPoints\[\d\]/(Points|NumOfCells|Ratio))$")


def even_base_mesh(flat: dict) -> bool | None:
    """True when every interval of the base mesh has stretch ratio 1 (Flow's 'uniform mesh');
    None when the file has no control planes."""
    ratios = [r for i in range(3) for r in _numbers(flat.get(f"{MESH}ControlPoints[{i}]/Ratio"))]
    return all(float(r) == 1.0 for r in ratios) if ratios else None


def make_case(flat: dict, name: str, folder: str | None = None, note: str | None = None) -> dict:
    """A case from a reference run's settings: what to hold later runs against.

    settings is every stable entry (so a later difference can be named), mesh and
    conditions are its readable parts, expect what the reference run gave."""
    info = read_info(folder) if folder else {}
    stats = goal_stats(folder) if folder else {}
    return {
        "schema": SCHEMA,
        "case": str(name),
        "note": note,
        "made": time.strftime("%Y-%m-%d %H:%M"),
        "source": {**environment_of(flat), "folder": folder,
                   "solver": (info.get("system_info") or {}).get("product_name")},
        "fingerprint": {"settings": fingerprint(flat), "mesh": fingerprint(flat, "mesh")},
        "expect": {"cells": (info.get("mesh") or {}).get("cells_total"),
                   "iterations": (info.get("telemetry") or {}).get("iteration"),
                   "goals": stats},
        "mesh": mesh_of(flat),
        "conditions": conditions_of(flat),
        "settings": {k[len(ROOT):]: v for k, v in sorted(stable(flat).items()) if k.startswith(ROOT)},
    }


def load_case(path: str) -> dict:
    try:
        case = safefile.read_json(path)
    except OSError as exc:
        raise CaseError(f"Cannot read the case file '{path}': {exc.strerror or exc}.") from None
    except ValueError as exc:
        raise CaseError(f"The case file '{path}' is not JSON: {exc}.") from None
    if not isinstance(case, dict) or not isinstance(case.get("settings"), dict) or not case.get("case"):
        raise CaseError(f"'{path}' is not a Flow case file (it needs 'case' and 'settings').")
    if int(case.get("schema") or 0) > SCHEMA:
        raise CaseError(f"The case file '{path}' is of a newer kind (schema {case.get('schema')}) than this "
                        f"server reads ({SCHEMA}).")
    return case


def save_json(path: str, data: dict) -> str:
    safefile.write_json(path, data)
    return path


def case_settings(case: dict) -> dict:
    return {ROOT + k: v for k, v in case["settings"].items()}


def same_box(flat: dict, other: dict, tolerance: float = 1e-9) -> bool:
    """True when both settings have the same control-plane positions on every axis: the
    planes sit on the domain's faces and on the model's box, so with equal domains they
    differ exactly when the geometry does."""
    for i in range(3):
        key = f"{MESH}ControlPoints[{i}]/Points"
        mine, theirs = _numbers(flat.get(key)), _numbers(other.get(key))
        if len(mine) != len(theirs) or any(abs(float(p) - float(q)) > tolerance for p, q in zip(mine, theirs)):
            return False
    return True


def same_run(flat: dict, case: dict, tolerance: float = 1e-9) -> bool:
    """True when the run has the case's own free stream AND the case's model box: then its
    derived base mesh must be the case's to the last plane. Another angle of attack, or
    another geometry calculated the same way, derives another - and is not held to it."""
    want = case_settings(case)
    mine, theirs = velocity_of(flat), velocity_of(want)
    if mine is None or theirs is None:
        return False
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(mine, theirs)) and same_box(flat, want)


def check_case(flat: dict, case: dict, scope: str = "mesh", exact: bool | None = None) -> list:
    """The differences of a run's settings from a case, [{key, have, want}]; [] = comparable.

    scope 'mesh': the mesh and the domain; 'all': every stable setting.
    exact: hold the derived base mesh (cells per axis, control planes, their ratios) to the
    case's too - right for the case's own free stream and geometry, and the default then.
    Otherwise the free-stream velocity and the derived entries are left out, and only
    whether the base mesh is even or stretched is held to the case."""
    want = case_settings(case)
    if exact is None:
        exact = same_run(flat, case)
    found = differences(flat, want, scope, limit=0)
    if exact:
        return found
    velocity = {k[len(ROOT):] for kind, k in ambient_keys(want).items() if kind in VELOCITY_TYPES}
    velocity |= {k[len(ROOT):] for kind, k in ambient_keys(flat).items() if kind in VELOCITY_TYPES}
    found = [d for d in found if d["key"] not in velocity and not DERIVED.match(d["key"])]
    mine, theirs = even_base_mesh(flat), even_base_mesh(want)
    if mine is not None and theirs is not None and mine != theirs:
        word = {True: "even (ratio 1 everywhere)", False: "stretched towards the domain's faces"}
        found.insert(0, {"key": "BaseMeshSettings[0]/ControlPoints (base mesh)", "have": word[mine],
                         "want": word[theirs]})
    return found


# --- results --------------------------------------------------------------------------------------------

GOAL_COLUMNS = ("Iteration", "CPUTime", "PhysTime", "Travels", "Value", "AvValue", "MinValue", "MaxValue",
                "Delta", "Criteria")


def goal_history(path: str) -> list:
    """Rows of a Goals.DAT/<goal>.txt: [{iteration, value, averaged, min, max, delta, criteria, travels}]."""
    rows = []
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            header = None
            for line in fh:
                parts = line.rstrip("\r\n").split("\t")
                if header is None:
                    header = {name.strip(): i for i, name in enumerate(parts)}
                    continue
                try:
                    get = lambda name: float(parts[header[name]].replace(",", "."))  # noqa: E731
                    rows.append({"iteration": int(get("Iteration")), "travels": get("Travels"),
                                 "value": get("Value"), "averaged": get("AvValue"), "min": get("MinValue"),
                                 "max": get("MaxValue"), "delta": get("Delta"), "criteria": get("Criteria")})
                except (KeyError, ValueError, IndexError):
                    continue
    except OSError:
        return []
    return rows


def goal_stats(folder: str | None) -> dict:
    """{goal: {value, averaged, min, max, delta, criteria, iterations}} from <folder>/Goals.DAT.

    value is the last iteration's; averaged, min, max and delta are over the solver's
    analysis interval. A goal that still swings is sampled at a random phase by 'value' -
    two runs of the same settings agree in 'averaged' an order of magnitude better."""
    out = {}
    goals = os.path.join(folder or "", "Goals.DAT")
    if not os.path.isdir(goals):
        return out
    for name in sorted(os.listdir(goals)):
        stem, ext = os.path.splitext(name)
        if ext.lower() != ".txt" or stem.lower().startswith("serv ") or stem.lower() == "global_parameters":
            continue
        rows = goal_history(os.path.join(goals, name))
        if not rows:
            continue
        last = rows[-1]
        out[stem] = {"value": last["value"], "averaged": last["averaged"], "min": last["min"],
                     "max": last["max"], "delta": last["delta"], "criteria": last["criteria"],
                     "iterations": last["iteration"], "first_iteration_value": rows[0]["value"]}
    return out


def result_record(folder: str, case: dict | None = None, geometry: dict | None = None,
                  extra: dict | None = None) -> dict:
    """What a finished run was: settings and mesh fingerprints, the mesh size, the goals,
    who ran it on what - and, against a case, whether it is comparable with it."""
    path = config_path(folder)
    if not path:
        raise CaseError(f"'{folder}' holds no prepared calculation (no .xmlconfig).")
    flat = read_config(path)
    info = read_info(folder, attempts=6)
    system = info.get("system_info") or {}
    telemetry = info.get("telemetry") or {}
    goals = goal_stats(folder)
    size = run_size(folder, info, goals)
    record = {
        "schema": SCHEMA,
        "written": time.strftime("%Y-%m-%d %H:%M:%S"),
        "folder": folder,
        **{k: v for k, v in environment_of(flat).items() if k != "owner"},
        "solver": system.get("product_name"),
        "cores": system.get("number_of_cores"),
        "finished": info.get("finished"),
        "cells": size["cells"],
        "cells_fluid": size["cells_fluid"],
        "iterations": size["iterations"],
        "travels": size["travels"],
        "size_source": size["source"],
        "cpu_time_s": telemetry.get("cpu_time_solver_total"),
        "fingerprint": {"settings": fingerprint(flat), "mesh": fingerprint(flat, "mesh")},
        "mesh": mesh_of(flat),
        "conditions": conditions_of(flat),
        "goals": goals,
    }
    if geometry:
        record["geometry"] = geometry
        record["fingerprint"]["geometry"] = geometry.get("fingerprint")
    if case:
        exact = same_run(flat, case)
        mesh_diff = check_case(flat, case, "mesh", exact)
        all_diff = check_case(flat, case, "all", exact)
        record["case"] = {"name": case.get("case"), "fingerprint": case.get("fingerprint"),
                          "same_run": exact,
                          "mesh_matches": not mesh_diff, "settings_match": not all_diff,
                          "differences": (mesh_diff or all_diff)[:40],
                          "expected_cells": (case.get("expect") or {}).get("cells") if exact else None}
    if extra:
        record.update(extra)
    return record


def reduce_forces(goals: dict, velocity, q_pa: float, S_m2: float, c_m: float, z_cg_m: float = 0.0,
                  which: str = "value") -> dict | None:
    """Lift, drag and the pitching moment coefficient from the force goals of an external
    flow that runs along -Z with the lift along +Y (the free stream gives the angle of
    attack, alpha = atan2(vy, -vz)):

        L = FY cos a + FZ sin a      D = FY sin a - FZ cos a
        CL = L / (q S)   CD = D / (q S)   Cm = -(MX - z_cg FY) / (q S c)

    goals: {name: {value, averaged, ...}} (goal_stats) - the goals whose names start with
    FY, FZ and MX are taken; which: 'value' (last iteration) or 'averaged'. None when a
    force goal or the free stream is missing."""
    import math

    def pick(prefix):
        for name, g in goals.items():
            if name.upper().startswith(prefix):
                return g.get(which) if isinstance(g, dict) else g
        return None

    fy, fz, mx = pick("FY"), pick("FZ"), pick("MX")
    if fy is None or fz is None or not velocity or len(velocity) != 3 or not q_pa or not S_m2:
        return None
    alpha = math.atan2(float(velocity[1]), -float(velocity[2]))
    lift = fy * math.cos(alpha) + fz * math.sin(alpha)
    drag = fy * math.sin(alpha) - fz * math.cos(alpha)
    out = {"alpha_deg": round(math.degrees(alpha), 4), "L": lift, "D": drag,
           "CL": lift / (q_pa * S_m2), "CD": drag / (q_pa * S_m2)}
    if mx is not None and c_m:
        out["Cm"] = -(mx - float(z_cg_m or 0.0) * fy) / (q_pa * S_m2 * c_m)
    return out


def compare_runs(a: dict, b: dict, scope: str = "all") -> dict:
    """Two result records (result_record): are they the same way of calculating, and how far
    apart are their goals. comparable is False when the settings differ in anything but the
    free stream and the base mesh derived from it; differences names what."""
    fa, fb = (a.get("fingerprint") or {}), (b.get("fingerprint") or {})
    same = fa.get("settings") == fb.get("settings")
    out = {"same_settings": same, "same_mesh": fa.get("mesh") == fb.get("mesh"),
           "same_geometry": (fa.get("geometry") == fb.get("geometry")) if fa.get("geometry") and fb.get("geometry")
           else None}
    deltas = {}
    for name, ga in (a.get("goals") or {}).items():
        gb = (b.get("goals") or {}).get(name)
        if not gb:
            continue
        row = {}
        for which in ("value", "averaged"):
            va, vb = ga.get(which), gb.get(which)
            if va is None or vb is None:
                continue
            row[which] = {"a": va, "b": vb, "delta": vb - va,
                          "delta_pct": None if not va else round((vb - va) / abs(va) * 100.0, 4)}
        deltas[name] = row
    out["goals"] = deltas
    return out


REGISTRY_COLUMNS = ("written", "computer", "model", "project", "case", "comparable", "cells", "iterations",
                    "travels", "fingerprint_settings", "fingerprint_mesh", "fingerprint_geometry", "goals",
                    "folder", "solver", "cad", "server")


NO_CASE = "none"


def match_case(record: dict, cases_dir: str | None) -> dict | None:
    """The case in cases_dir whose fingerprint a run has (settings first, else mesh):
    {name, settings: bool}; None when no case matches or there is no folder."""
    prints = record.get("fingerprint") or {}
    if not cases_dir or not os.path.isdir(cases_dir) or not prints.get("settings"):
        return None
    mesh_only = None
    for name in sorted(os.listdir(cases_dir)):
        if not name.lower().endswith(".json"):
            continue
        try:
            case = load_case(os.path.join(cases_dir, name))
        except CaseError:
            continue
        theirs = case.get("fingerprint") or {}
        if theirs.get("settings") == prints["settings"]:
            return {"name": case.get("case"), "settings": True}
        if mesh_only is None and prints.get("mesh") and theirs.get("mesh") == prints["mesh"]:
            mesh_only = {"name": case.get("case"), "settings": False}
    return mesh_only


def registry_row(record: dict) -> dict:
    case = record.get("case") or {}
    prints = record.get("fingerprint") or {}
    goals = "; ".join(f"{name}={g.get('value'):.6g} (avg {g.get('averaged'):.6g})"
                      for name, g in (record.get("goals") or {}).items()
                      if g.get("value") is not None and g.get("averaged") is not None)
    matched = record.get("case_match") or {}
    if case:
        name = case.get("name") or "?"
        comparable = "yes" if case.get("settings_match") else ("mesh only" if case.get("mesh_matches") else "NO")
    elif matched.get("name"):
        name = matched["name"]
        comparable = "yes (fingerprint)" if matched.get("settings") else "mesh only (fingerprint)"
    else:
        name, comparable = NO_CASE, "n/a"
    return {"written": record.get("written"), "computer": record.get("computer"), "model": record.get("model"),
            "project": record.get("project"), "case": name, "comparable": comparable,
            "cells": record.get("cells"), "iterations": record.get("iterations"),
            "travels": None if record.get("travels") is None else round(record["travels"], 4),
            "fingerprint_settings": prints.get("settings"), "fingerprint_mesh": prints.get("mesh"),
            "fingerprint_geometry": prints.get("geometry") or "", "goals": goals, "folder": record.get("folder"),
            "solver": record.get("solver"), "cad": record.get("cad"), "server": record.get("server") or ""}


def register_run(registry: str, record: dict) -> str:
    """Append the run to the registry CSV (made with its header when new). One line per run;
    lines are only ever added, so two PCs writing the same synced file lose nothing but order."""
    safefile.append_csv_row(registry, REGISTRY_COLUMNS, registry_row(record))
    return registry


def read_registry(registry: str) -> list:
    """The registry's rows as dicts; a garbage tail (NULs, a torn line) is skipped."""
    if not os.path.isfile(registry):
        return []
    return safefile.csv_rows(safefile.read_text(registry))[1]


# --- folders next to the model: numbers, reservations, a lock -----------------------------------------
#
# Flow names a new project's folder with the first number that is free next to the model
# file (measured: with 1, 2, 15, 16 present the next projects got 3, 4, 5). Two PCs that
# share the folder through a file-sync service therefore pick the SAME number until the
# other's folder has arrived, and one calculation overwrites the other. A PC with a slot
# "k/M" takes only numbers n with n % M == k: before a project is made, the free numbers
# below the next such n are filled with placeholder folders, and removed again afterwards.

RESERVED_FILE = "mcp_reserved.json"
LOCK_FILE = "mcp_flow.lock"
LOCK_FRESH_S = 180.0


def parse_slot(slot) -> tuple | None:
    """'1/2' -> (1, 2): this PC takes the folder numbers n with n % 2 == 1. None for None."""
    if slot in (None, ""):
        return None
    try:
        k, m = (int(part) for part in str(slot).split("/"))
    except ValueError:
        raise CaseError("flow_folder_slot is 'k/M' (e.g. '0/2' on one PC and '1/2' on the other).") from None
    if not (m >= 2 and 0 <= k < m):
        raise CaseError("flow_folder_slot 'k/M' needs M >= 2 and 0 <= k < M.")
    return k, m


def taken_numbers(folder: str) -> set:
    try:
        return {int(n) for n in os.listdir(folder) if n.isdigit() and os.path.isdir(os.path.join(folder, n))}
    except OSError:
        return set()


def reserve(folder: str, slot: tuple | None, computer: str | None) -> dict:
    """Make Flow's next folder number one of this PC's: {target, placeholders: [paths]}.
    Without a slot nothing is reserved and target is just the first free number."""
    taken = taken_numbers(folder)
    n = 1
    made = []
    while True:
        if n not in taken:
            if slot is None or n % slot[1] == slot[0]:
                return {"target": n, "placeholders": made}
            path = os.path.join(folder, str(n))
            os.makedirs(path, exist_ok=True)
            safefile.write_json(os.path.join(path, RESERVED_FILE),
                                {"computer": computer, "reserved": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "why": "keeps this number from a PC it does not belong to"})
            made.append(path)
        n += 1


def release(placeholders) -> list:
    """Remove the placeholder folders made by reserve() that still hold nothing but the marker."""
    removed = []
    for path in placeholders or ():
        try:
            if sorted(os.listdir(path)) in ([RESERVED_FILE], ["desktop.ini", RESERVED_FILE], []):
                for name in os.listdir(path):
                    os.remove(os.path.join(path, name))
                os.rmdir(path)
                removed.append(path)
        except OSError:
            continue
    return removed


def read_lock(folder: str) -> dict | None:
    try:
        path = os.path.join(folder, LOCK_FILE)
        data = safefile.read_json(path)
        if isinstance(data, dict):
            data["age_s"] = max(0.0, time.time() - os.path.getmtime(path))
            return data
    except (OSError, ValueError):
        pass
    return None


def lock(folder: str, computer: str | None, doing: str) -> str | None:
    """Take the folder's lock; returns why not when another computer holds a fresh one.
    The lock is advice between PCs that see each other's files within a sync delay - the
    slot scheme, not the lock, is what keeps two PCs' folders apart."""
    held = read_lock(folder)
    if held and held.get("age_s", 1e9) < LOCK_FRESH_S and computer and held.get("computer") \
            and str(held["computer"]).lower() != str(computer).lower():
        return (f"{held.get('computer')} is making a Flow project in this folder right now "
                f"({held.get('doing')}, {held['age_s']:.0f} s ago): wait until its folder has synced")
    try:
        safefile.write_json(os.path.join(folder, LOCK_FILE),
                            {"computer": computer, "doing": doing, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    except OSError:
        pass
    return None


def unlock(folder: str, computer: str | None) -> None:
    held = read_lock(folder)
    if held and str(held.get("computer") or "").lower() == str(computer or "").lower():
        try:
            os.remove(os.path.join(folder, LOCK_FILE))
        except OSError:
            pass


# --- goals, versions, grid convergence -------------------------------------------------------------


def goals_of(flat: dict) -> list:
    """The surface goals as the solver has them: [{name, solver_type, in_convergence, faces}]."""
    out = []
    i = 0
    while True:
        base = f"{ROOT}Features_Surface_Goal[0]/Surface_Goal[{i}]/"
        if base + "Goal_Type" not in flat:
            return out
        out.append({"name": flat.get(base + "Name"), "solver_type": _number(flat.get(base + "Goal_Type")),
                    "in_convergence": flat.get(base + "Goal_Use_In_Convergence") == "1",
                    "faces": _number(flat.get(base + "Faces_Keys@size"))})
        i += 1


def next_version(folder: str, name: str) -> str:
    """'CASE-a_v01' when the file exists -> 'CASE-a_v02': a case is never overwritten."""
    m = re.match(r"^(.*_v)(\d+)$", name)
    stem, number, width = (m.group(1), int(m.group(2)), len(m.group(2))) if m else (name + "_v", 1, 2)
    while os.path.isfile(os.path.join(folder, f"{stem}{number:0{width}d}.json")):
        number += 1
    return f"{stem}{number:0{width}d}"


def richardson(coarse: float, base: float, fine: float, cells: tuple) -> dict:
    """Grid convergence of one quantity on three meshes (cell counts coarse < base < fine):
    the observed order p, the extrapolated value and the fine-grid convergence index.
    {change_base_to_fine_pct, order, extrapolated, gci_fine_pct, monotonic}; order and the
    rest are None when the three values do not converge monotonically."""
    import math
    n1, n2, n3 = (float(c) for c in cells)
    r21, r32 = (n2 / n1) ** (1.0 / 3.0), (n3 / n2) ** (1.0 / 3.0)
    e21, e32 = base - coarse, fine - base
    out = {"change_base_to_fine_pct": None if not base else round(e32 / abs(base) * 100.0, 4),
           "refinement_ratios": [round(r21, 4), round(r32, 4)], "monotonic": e21 * e32 > 0,
           "order": None, "extrapolated": None, "gci_fine_pct": None}
    if not out["monotonic"] or e32 == 0:
        return out
    # equal ratios give p directly; unequal ones are solved by a few fixed-point steps
    p = math.log(abs(e21 / e32)) / math.log((r21 + r32) / 2.0)
    for _ in range(50):
        q = math.log((r32 ** p - 1.0) / (r21 ** p - 1.0)) if p > 0 and r21 ** p != 1.0 else 0.0
        p_next = abs(math.log(abs(e21 / e32)) + q) / math.log(r32)
        if abs(p_next - p) < 1e-10:
            break
        p = p_next
    if not p > 0:
        return out
    out["order"] = round(p, 4)
    out["extrapolated"] = fine + e32 / (r32 ** p - 1.0)
    out["gci_fine_pct"] = None if not fine else round(1.25 * abs(e32 / fine) / (r32 ** p - 1.0) * 100.0, 4)
    return out
