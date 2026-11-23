"""SOLIDWORKS Flow Simulation: a project, its boundary conditions and goals, the solve, the goals' values.

Flow is driven through its NIK API (NIKCommonApi2, from the Flow Simulation API
SDK). That library runs in the calling process and crashes it outright on some
calls (an access violation reading a computational-domain parameter), so every
NIK call runs in a separate process, rbr/flow_worker.py: a crash there fails the
one tool call with a readable error and leaves this server running.

Faces are picked here, in the server's own SolidWorks session (the selection is
SolidWorks' own, so the worker sees it), with the same selectors as the other
tools: '#5' (list_faces index), '+x' / '+x:inner', or 'all'.

Verified on SOLIDWORKS Flow Simulation 2025 SP3 (2026-11-02): the SDK sample
model and a shelled box (water, 1 m/s inlet, environment-pressure outlet) solved
in 7-13 s; the classic FloWorks.App API could not set outlets, pressures or goal
kinds and its Solve refused silently, which is why this uses NIK.
"""

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

from .. import binding
from ..errors import SolidWorksError
from . import flow_case, flow_templates, private, safefile

SW_DOC_PART = 1

TEMPLATE_DIRS = (r"C:\ProgramData\COSMOS Applications",)

# nikBoundaryConditionTypes_e -> the nikParametersTypes_e its value goes into (None: no value)
BOUNDARY_CONDITIONS = {
    "inlet_mass_flow": (0, 17),
    "inlet_volume_flow": (1, 18),
    "inlet_velocity": (2, 19),
    "inlet_mach": (3, 20),
    "inlet_mass_flux": (4, 62),
    "outlet_mass_flow": (5, 17),
    "outlet_volume_flow": (6, 18),
    "outlet_velocity": (7, 19),
    "outlet_mach": (8, 20),
    "total_pressure": (9, 25),
    "static_pressure": (10, 24),
    "environment_pressure": (11, 9),
    "real_wall": (12, None),
    "real_external_wall": (13, None),
    "ideal_wall": (14, None),
}
BC_UNITS = {17: "kg/s", 18: "m^3/s", 19: "m/s", 20: "Mach", 62: "kg/(m^2*s)", 25: "Pa", 24: "Pa", 9: "Pa"}

# nikGoalParameters_e
GOALS = {
    "static_pressure": 3, "temperature": 4, "density": 5, "mass_flow": 6, "heat_flux": 8,
    "normal_force": 9, "force_x": 10, "force_y": 11, "force_z": 12, "volume_flow": 17,
    "total_pressure": 18, "dynamic_pressure": 19, "shear_force": 20, "velocity": 21,
    "moment_x": 22, "moment_y": 23, "moment_z": 24, "normal_force_x": 34, "normal_force_y": 35,
    "normal_force_z": 36, "force": 37, "shear_force_x": 38, "shear_force_y": 39,
    "shear_force_z": 40, "velocity_x": 41, "velocity_y": 42, "velocity_z": 43,
    "wall_temperature": 140, "total_temperature": 164,
}
# nikGoalValueCalculateTypes_e
GOAL_VALUES = {"min": 0, "max": 1, "average": 2, "bulk_average": 3}
# integrals over the faces: Flow takes no min/max/average for them (verified: volume flow refused 'average')
INTEGRAL_GOALS = {6, 9, 10, 11, 12, 17, 20, 22, 23, 24, 34, 35, 36, 37, 38, 39, 40}

# FWUnitsEnum -> the SI unit Flow reports values in
UNITS = {4: "kg/m^3", 7: "N", 8: "W", 10: "m", 12: "kg/s", 14: "Pa", 18: "W/m^2", 19: "m/s",
         20: "K", 22: "s", 26: "m^3", 29: "s", 33: "Mach", 35: "m^3/s", 39: "N*m", 42: ""}

WORKER_TIMEOUT_S = 300.0
SOLVE_TIMEOUT_S = 4 * 3600.0
# A template's own level is whatever its author left (the external-air one here: 3, which
# gave a lift 25 % low against level 5); a project is made at this level unless told otherwise.
DEFAULT_MESH_LEVEL = 5
DOMAIN_KEYS = ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")


# --- pure helpers (unit-tested) --------------------------------------------------------


def _choice(table: dict, key: str, what: str):
    try:
        return table[str(key).strip().lower()]
    except KeyError:
        raise SolidWorksError(f"Unknown {what} '{key}'. Use one of: {', '.join(table)}.") from None


def boundary_condition(kind: str):
    """'inlet_velocity' -> (nik type, nik value parameter or None)."""
    return _choice(BOUNDARY_CONDITIONS, kind, "boundary condition")


def goal(kind: str, value: str = "average") -> tuple:
    """('static_pressure', 'max') -> (nik goal parameter, nik value-to-calculate); an integral
    goal (a force, a flow rate) has no value to calculate: (parameter, None)."""
    parameter = _choice(GOALS, kind, "goal")
    calculate = _choice(GOAL_VALUES, value, "goal value")
    if parameter in INTEGRAL_GOALS:
        if str(value).strip().lower() != "average":
            raise SolidWorksError(f"A {kind} goal is an integral over the faces: it takes no '{value}'.")
        return parameter, None
    return parameter, calculate


def template_path(template: str, search=TEMPLATE_DIRS) -> str:
    """A .fwp by full path, or by name in Flow's template folders ('internal_water')."""
    text = str(template or "").strip()
    if not text:
        raise SolidWorksError("template is empty: name a Flow project template (.fwp), e.g. 'internal_water'.")
    if os.path.isabs(text):
        if os.path.isfile(text):
            return text
        raise SolidWorksError(f"No template file '{text}'.")
    name = text if text.lower().endswith(".fwp") else text + ".fwp"
    found = []
    for root in search:
        for folder, _, files in os.walk(root):
            found += [os.path.join(folder, f) for f in files if f.lower().endswith(".fwp")]
    for path in found:
        if os.path.basename(path).lower() == name.lower():
            return path
    names = ", ".join(sorted({os.path.splitext(os.path.basename(p))[0] for p in found})) or "none"
    raise SolidWorksError(
        f"No Flow template '{text}' (found: {names}). Make one in Flow Simulation (Wizard, then "
        "Tools > Flow Simulation > Project > Create Template) or pass the .fwp path.")


def unit(unit_id) -> str:
    return UNITS.get(unit_id, f"unit {unit_id}")


DOMAIN_HELP = ("domain_m is the computational domain in metres, model coordinates: "
               "{'x': [min, max], 'y': [min, max], 'z': [min, max]} (leave an axis out, or give null for "
               "a side, to keep it) or [xmin, xmax, ymin, ymax, zmin, zmax].")


def domain_values(domain_m) -> dict:
    """{'xmin': -1.375, ...} for the sides that were given; {} for None. Refuses a shape it
    does not know and a side pair that is not min < max."""
    if domain_m is None:
        return {}
    out = {}
    try:
        if isinstance(domain_m, dict):
            for key, value in domain_m.items():
                k = str(key).strip().lower()
                if k in ("x", "y", "z"):
                    low, high = value
                    out[k + "min"], out[k + "max"] = low, high
                elif k in DOMAIN_KEYS:
                    out[k] = value
                else:
                    raise ValueError(key)
        else:
            values = list(domain_m)
            if len(values) != 6:
                raise ValueError(len(values))
            out = dict(zip(DOMAIN_KEYS, values))
        out = {k: float(v) for k, v in out.items() if v is not None}
    except (TypeError, ValueError):
        raise SolidWorksError(DOMAIN_HELP) from None
    for axis in "xyz":
        low, high = out.get(axis + "min"), out.get(axis + "max")
        if low is not None and high is not None and not low < high:
            raise SolidWorksError(f"domain_m: {axis}min ({low}) must be below {axis}max ({high}). {DOMAIN_HELP}")
    if not out:
        raise SolidWorksError(DOMAIN_HELP)
    return out


def min_gap(min_gap_mm) -> float | None:
    """Minimum gap size in metres for Flow, from mm."""
    if min_gap_mm is None:
        return None
    try:
        value = float(min_gap_mm)
    except (TypeError, ValueError):
        value = 0.0
    if not value > 0:
        raise SolidWorksError("min_gap_mm is the mesh's minimum gap size in mm, above 0.")
    return value / 1000.0


def worker_answer(returncode: int, stdout: str, stderr: str) -> dict:
    """The worker's one-line JSON reply, or a readable error when it died without one."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict):
                return reply
    if returncode in (-1073741819, 3221225477):  # 0xC0000005
        cause = "an access violation inside the Flow API library"
    else:
        cause = f"exit code {returncode}"
    tail = " ".join((stderr or "").strip().splitlines()[-3:])
    raise SolidWorksError(f"The Flow worker process died ({cause}) without an answer; SolidWorks itself is "
                          f"not affected. {tail}".strip())


# --- calling the worker ----------------------------------------------------------------


_silent_holds = 0   # COM thread only: calls and running solves that need Flow silent


def silent_on(session) -> None:
    """Flow's own add-in API in silent mode: its Yes/No questions (reset the computational
    domain? adjust the mesh?) would otherwise wait for a click in SolidWorks while the
    worker waits for them. NIK has no such switch; the classic FloWorks.App API does.

    Counted: a solve holds it from its start to its end while other Flow calls come and
    go, and only the last release switches the questions back on. COM thread only."""
    global _silent_holds
    _silent_holds += 1
    try:
        import win32com.client
        win32com.client.Dispatch(session._sw.GetAddInObject("FloWorks.App")).GetAPI().SetSilent()
    except Exception:  # noqa: BLE001 - no add-in object: the worker names the problem
        pass


def silent_off(session) -> None:
    global _silent_holds
    _silent_holds = max(0, _silent_holds - 1)
    if _silent_holds:
        return
    try:
        import win32com.client
        win32com.client.Dispatch(session._sw.GetAddInObject("FloWorks.App")).GetAPI().ResetSilent()
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def _silent(session):
    silent_on(session)
    try:
        yield
    finally:
        silent_off(session)


def _call(session, request: dict, timeout_s: float = None) -> dict:
    with _silent(session):
        return _run_worker(request, WORKER_TIMEOUT_S if timeout_s is None else timeout_s)


WORKER = [sys.executable, "-m", "solidworks_mcp.rbr.flow_worker"]


def _worker_env() -> dict:
    """The worker must import THIS package (a server run from a source tree, a test harness)."""
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.environ.get("PYTHONPATH", "")
    return dict(os.environ, PYTHONPATH=src + (os.pathsep + path if path else ""))


def _run_worker(request: dict, timeout_s: float) -> dict:
    try:
        run = subprocess.run(WORKER, input=json.dumps(request), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout_s, env=_worker_env(),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise SolidWorksError(f"Flow did not finish within {timeout_s:.0f} s; the calculation may still be "
                              "running in SolidWorks (its solver monitor shows it).") from None
    reply = worker_answer(run.returncode, run.stdout, run.stderr)
    if not reply.get("ok"):
        raise SolidWorksError(reply.get("error") or "The Flow worker failed without a reason.")
    return reply


def _document(session):
    """The current part, which Flow needs saved: its project lives next to the file."""
    model = session._require_model()
    path = model.GetPathName()
    if not path:
        raise SolidWorksError("Save the document first (save_part / save_assembly): a Flow project is stored "
                              "next to the file.")
    return model, path


def computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""


def server_version() -> str:
    from . import RBR_VERSION
    return RBR_VERSION


def _request(session, op: str, **kw) -> dict:
    model, path = _document(session)
    pid = int(session._sw.GetProcessID())
    return {"op": op, "pid": pid, "document": path, "computer": computer_name(), **kw}


def _select_faces(session, faces) -> int:
    """Select the faces named by selectors; returns how many."""
    if isinstance(faces, str):
        faces = [faces]
    if not faces:
        raise SolidWorksError("faces is empty: name faces as '#5' (list_faces), '+x' / '+x:inner', or 'all'.")
    model = session._require_model()
    if int(model.GetType()) != SW_DOC_PART:
        raise SolidWorksError("Faces are picked on a part; on an assembly select them in SolidWorks and use "
                              "faces=['selection'].")
    body = session._solid_body()
    all_faces = session._body_faces(body)
    chosen = []
    for token in faces:
        text = str(token).strip()
        if text.lower() == "all":
            chosen += all_faces
            continue
        index = session._face_index(text)
        if index is not None:
            if not 0 <= index < len(all_faces):
                raise SolidWorksError(f"No face '{text}': the body has faces #0..#{len(all_faces) - 1}.")
            chosen.append(all_faces[index])
            continue
        normal, side = session._parse_face_selector(text)
        face = session._planar_face_by_normal(body, normal, side)
        if face is None:
            raise SolidWorksError(f"No planar face facing '{text}'.")
        chosen.append(face)
    model.ClearSelection2(True)
    count = 0
    for face in chosen:
        if binding.wrap(face, session._mod.IEntity).Select4(True, None):
            count += 1
    if not count:
        raise SolidWorksError(f"Could not select the faces {list(faces)}.")
    return count


def _with_selection(session, faces, request: dict) -> dict:
    if faces in (["selection"], "selection"):
        return _call(session, request, WORKER_TIMEOUT_S)
    selected = _select_faces(session, faces)
    try:
        reply = _call(session, request, WORKER_TIMEOUT_S)
    finally:
        session._require_model().ClearSelection2(True)
    reply["faces_selected"] = selected
    return reply


# --- tools ------------------------------------------------------------------------------


def _mesh_level(mesh_level) -> int | None:
    if mesh_level is None:
        return None
    try:
        level = int(mesh_level)
    except (TypeError, ValueError):
        level = 0
    if not 1 <= level <= 7 or level != mesh_level:
        raise SolidWorksError("mesh_level is Flow's automatic mesh level, a whole number 1 (coarse) to 7 (fine).")
    return level


def _apply(session, velocity_mps=None, pressure_pa=None, temperature_k=None, mesh_level=None,
           domain_m=None, min_gap_mm=None) -> dict:
    """Set what was given on the active project: {set: {...}}. The domain goes in a worker
    call of its own, after the rest (see flow_worker: it has crashed the worker before)."""
    if velocity_mps is not None and len(velocity_mps) != 3:
        raise SolidWorksError("velocity_mps is [vx, vy, vz] in m/s.")
    level = _mesh_level(mesh_level)
    domain = domain_values(domain_m)
    gap = min_gap(min_gap_mm)
    done = {}
    reply = {}
    if any(v is not None for v in (velocity_mps, pressure_pa, temperature_k, level, gap)):
        reply = _call(session, _request(session, "set_conditions", velocity=velocity_mps, pressure=pressure_pa,
                                        temperature=temperature_k, mesh_level=level, min_gap=gap))
        done.update(reply.get("set") or {})
    if domain:
        try:
            reply = _call(session, _request(session, "set_domain", domain=domain))
        except SolidWorksError as exc:
            raise SolidWorksError(f"{exc}" + (f" Already set: {done}." if done else "")) from None
        done["domain_m"] = reply.get("domain_m")
    return {"project": reply.get("project"), "set": done}


def shared_template(template: str, allow_local: bool = False) -> dict:
    """The template's file by the rules of flow_templates: the private templates_dir when
    there is one (MD5 held against its manifest), Flow's own folders otherwise."""
    try:
        return flow_templates.resolve(template, private.get("templates_dir"), allow_local, TEMPLATE_DIRS)
    except flow_templates.TemplateError as exc:
        raise SolidWorksError(str(exc)) from None


def flow_create_project(session, name: str, template: str = "internal_water", mesh_level: int = DEFAULT_MESH_LEVEL,
                        domain_m=None, min_gap_mm: float | None = None, allow_local: bool = False) -> dict:
    level = _mesh_level(mesh_level)
    domain_values(domain_m), min_gap(min_gap_mm)   # a bad value refuses before anything is made
    found = shared_template(template, allow_local)
    reply = _call(session, _request(session, "create_project", name=str(name), template=found["path"],
                                    mesh_level=level, slot=private.get("flow_folder_slot")))
    reply["template_source"], reply["template_md5"] = found["source"], found["md5"]
    if found["warnings"]:
        reply["warnings"] = found["warnings"]
    if domain_m is not None or min_gap_mm is not None:
        try:
            reply["set"] = _apply(session, domain_m=domain_m, min_gap_mm=min_gap_mm)["set"]
        except SolidWorksError as exc:
            raise SolidWorksError(f"Project '{reply.get('project')}' was created (mesh level {level}), but: "
                                  f"{exc}") from None
    return reply


def flow_add_boundary_condition(session, kind: str, faces, value: float | None = None, name: str | None = None) -> dict:
    bc_type, value_param = boundary_condition(kind)
    if value_param is None and value is not None:
        raise SolidWorksError(f"A {kind} takes no value.")
    if value_param is not None and value is None:
        raise SolidWorksError(f"A {kind} needs a value in {BC_UNITS[value_param]}.")
    request = _request(session, "add_boundary_condition", bc_type=bc_type, value_param=value_param,
                       value=value, name=name)
    reply = _with_selection(session, faces, request)
    if value_param is not None:
        reply["value_unit"] = BC_UNITS[value_param]
    return reply


def flow_add_goal(session, kind: str, faces, value: str = "average", name: str | None = None) -> dict:
    parameter, calculate = goal(kind, value)
    return _with_selection(session, faces, _request(session, "add_goal", parameter=parameter,
                                                     calculate=calculate, name=name))


def flow_set_conditions(session, velocity_mps: list | None = None, pressure_pa: float | None = None,
                        temperature_k: float | None = None, mesh_level: int | None = None,
                        domain_m=None, min_gap_mm: float | None = None) -> dict:
    if all(v is None for v in (velocity_mps, pressure_pa, temperature_k, mesh_level, domain_m, min_gap_mm)):
        raise SolidWorksError("Nothing to set: give velocity_mps, pressure_pa, temperature_k, mesh_level, "
                              "domain_m and/or min_gap_mm.")
    return _apply(session, velocity_mps, pressure_pa, temperature_k, mesh_level, domain_m, min_gap_mm)


def flow_activate(session, name: str) -> dict:
    return _call(session, _request(session, "activate", name=str(name)))


def flow_clone(session, name: str, new_name: str, velocity_mps: list | None = None,
               pressure_pa: float | None = None, temperature_k: float | None = None,
               mesh_level: int | None = None, domain_m=None, min_gap_mm: float | None = None,
               copy_results: bool = False) -> dict:
    if not str(new_name or "").strip():
        raise SolidWorksError("new_name is empty: name the copy.")
    if velocity_mps is not None and len(velocity_mps) != 3:
        raise SolidWorksError("velocity_mps is [vx, vy, vz] in m/s.")
    _mesh_level(mesh_level), domain_values(domain_m), min_gap(min_gap_mm)
    reply = _call(session, _request(session, "clone", name=str(name), new_name=str(new_name).strip(),
                                    copy_results=bool(copy_results), slot=private.get("flow_folder_slot")))
    if any(v is not None for v in (velocity_mps, pressure_pa, temperature_k, mesh_level, domain_m, min_gap_mm)):
        try:
            reply["set"] = _apply(session, velocity_mps, pressure_pa, temperature_k, mesh_level, domain_m,
                                  min_gap_mm)["set"]
        except SolidWorksError as exc:
            raise SolidWorksError(f"Project '{reply.get('project')}' was cloned and is active, but: {exc}") from None
    return reply


def flow_delete(session, name: str, force: bool = False) -> dict:
    return _call(session, _request(session, "delete", name=str(name), force=bool(force)))


def flow_prepare(session, force: bool = False) -> dict:
    """Have Flow write the active project's solver settings file (no mesh, no solve)."""
    return _call(session, _request(session, "prepare", force=bool(force)))


def _with_units(reply: dict) -> dict:
    for item in reply.get("goals", []) + reply.get("service_goals", []):
        item["unit"] = unit(item.pop("unit_id", None))
    return reply


# --- the solve: started on the COM thread, waited for off it ---------------------------------------


def geometry_of(session) -> dict | None:
    """What was calculated, apart from its file: volume, area, box, face and body counts of
    the current document, and a fingerprint of them (9 significant digits) - the same for a
    file copy, different after any change of shape. None when it cannot be measured."""
    try:
        props = session.get_mass_properties()
        faces = bodies = 0
        try:
            for body in session._part_bodies():
                bodies += 1
                faces += len(session._body_faces(body))
        except Exception:  # noqa: BLE001 - an assembly: the mass properties still tell
            faces = bodies = None
        box = props.get("bounding_box_mm") or {}
        out = {"volume_mm3": props.get("volume_mm3"), "area_mm2": props.get("surface_area_mm2"),
               "bounding_box_mm": box, "faces": faces, "bodies": bodies}
        out["fingerprint"] = geometry_fingerprint(out)
        return out
    except Exception:  # noqa: BLE001 - a courtesy to the record, never a reason to refuse a solve
        return None


def geometry_fingerprint(geometry: dict) -> str:
    def flat(value):
        if isinstance(value, dict):
            return [flat(value[k]) for k in sorted(value)]
        if isinstance(value, (list, tuple)):
            return [flat(v) for v in value]
        if isinstance(value, float):
            return f"{value:.9g}"
        return value

    text = json.dumps(flat({k: v for k, v in geometry.items() if k != "fingerprint"}), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve_case(case) -> tuple:
    """(case dict, path) of a case given by path or by name in the private cases_dir; (None, None) for None."""
    if case in (None, ""):
        return None, None
    text = str(case)
    path = text if os.path.isabs(text) else None
    if path is None:
        folder = private.get("cases_dir")
        if not folder:
            raise SolidWorksError("A case by name needs the private settings' cases_dir; or give the file's path.")
        name = text if text.lower().endswith(".json") else text + ".json"
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            have = sorted(os.path.splitext(n)[0] for n in os.listdir(folder) if n.lower().endswith(".json")) \
                if os.path.isdir(folder) else []
            raise SolidWorksError(f"No case '{text}' in the cases folder (it has: {', '.join(have) or 'none'}).")
    try:
        return flow_case.load_case(path), path
    except flow_case.CaseError as exc:
        raise SolidWorksError(str(exc)) from None


def solve_begin(session, force: bool = False, case=None, cpus: int | None = None, record: bool = True) -> dict:
    """On the COM thread: save (Flow solves the project of the file on disk), switch Flow's
    questions off for the run, and return the worker request. solve_end undoes the switch.

    force: solve although the project's folder holds another model's / PC's calculation.
    case: hold the project's solver settings against this case before meshing and refuse
    when they differ; cpus: the number of solver threads (None: Flow's own choice)."""
    model, _path = _document(session)
    loaded, case_path = resolve_case(case)
    if cpus is not None and (int(cpus) != cpus or not 1 <= int(cpus) <= 256):
        raise SolidWorksError("cpus is the number of solver threads, a whole number from 1.")
    request = _request(session, "solve", force=bool(force), server=server_version(), cpus=cpus,
                       case_path=case_path, record=bool(record))
    request["geometry"] = geometry_of(session) if record else None
    model.Save3(1, 0, 0)
    silent_on(session)
    return request


def solve_end(session) -> None:
    """On the COM thread, when the solve's worker is gone."""
    silent_off(session)


def solve_run(request: dict, on_event=None, timeout_s: float = SOLVE_TIMEOUT_S) -> dict:
    """Run the solve in the worker process and wait for it - on ANY thread, never the COM
    one: SolidWorks stays free for other calls meanwhile (verified: 6-70 ms per API call
    and a part built in another document while a 136 s solve ran).

    on_event(dict) gets the worker's {"event": "started", project, directory} line and
    {"event": "process", pid}."""
    proc = subprocess.Popen(WORKER, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_worker_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if on_event:
        on_event({"event": "process", "pid": proc.pid})
    proc.stdin.write(json.dumps(request).encode("utf-8"))
    proc.stdin.close()
    errors = []
    drain = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
    drain.start()
    lines = []

    def read():
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace")
            lines.append(line)
            text = line.strip()
            if on_event and text.startswith("{") and '"event"' in text:
                try:
                    on_event(json.loads(text))
                except Exception:  # noqa: BLE001 - progress is a courtesy
                    pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise SolidWorksError(f"The Flow solve did not finish within {timeout_s:.0f} s; it may still be running "
                              "in SolidWorks (flow_status shows the solver's own progress).") from None
    reader.join(10)
    drain.join(10)
    stderr = (errors[0] if errors else b"").decode("utf-8", "replace")
    reply = worker_answer(proc.returncode, "".join(lines), stderr)
    if not reply.get("ok"):
        raise SolidWorksError(reply.get("error") or "The Flow worker failed without a reason.")
    return _with_units(reply)


def record_run(directory: str | None, request: dict) -> dict:
    """After a solve, off the COM thread: write result.json into the project's folder and
    append the run to the registry (private runs_registry, or runs.csv beside the model).
    Returns {result, registry, fingerprint, case, reduced} for the solve's answer; a
    failure to write is reported there, never raised - the calculation is done."""
    out = {}
    if not directory:
        return out
    try:
        case = flow_case.load_case(request["case_path"]) if request.get("case_path") else None
        extra = {"server": request.get("server"), "threads_asked": request.get("cpus"),
                 "case_file": None if not request.get("case_path") else
                 {"name": os.path.basename(request["case_path"]), "md5": flow_templates.md5_of(request["case_path"])}}
        record = flow_case.result_record(directory, case, request.get("geometry"), extra)
        if not case:
            try:
                record["case_match"] = flow_case.match_case(record, private.get("cases_dir"))
            except Exception:  # noqa: BLE001 - the registry line says 'none' then
                record["case_match"] = None
        reduction = private.get("reference.reduction")
        velocity = flow_case.velocity_of(flow_case.read_config(flow_case.config_path(directory)))
        if isinstance(reduction, dict) and velocity:
            for which in ("value", "averaged"):
                reduced = flow_case.reduce_forces(record["goals"], velocity, reduction.get("q_pa"),
                                                  reduction.get("S_m2"), reduction.get("c_m"),
                                                  reduction.get("z_cg_m", 0.0), which)
                if reduced:
                    record.setdefault("reduced", {})[which] = reduced
        out["result"] = flow_case.save_json(os.path.join(directory, RESULT_FILE), record)
        out["fingerprint"] = record["fingerprint"]
        if record.get("case"):
            out["case"] = record["case"]
        if record.get("reduced"):
            out["reduced"] = record["reduced"]
        registry = private.get("runs_registry") or os.path.join(os.path.dirname(request["document"]), "runs.csv")
        out["registry"] = flow_case.register_run(registry, record)
    except Exception as exc:  # noqa: BLE001
        out["record_error"] = f"{type(exc).__name__}: {exc}"
    return out


RESULT_FILE = "result.json"


def flow_solve(session, timeout_s: float = SOLVE_TIMEOUT_S, force: bool = False, case=None,
               cpus: int | None = None) -> dict:
    """The whole solve in one blocking call (scripts and tests); the MCP tool runs
    solve_begin / solve_run / solve_end itself so that the COM thread is free meanwhile."""
    request = solve_begin(session, force, case, cpus)
    seen = {}
    try:
        reply = solve_run(request, lambda e: seen.update(e) if e.get("event") == "started" else None,
                          max(60.0, min(float(timeout_s), SOLVE_TIMEOUT_S)))
        if request.get("record"):
            reply["run"] = record_run(seen.get("directory"), request)
        return reply
    finally:
        solve_end(session)


def flow_goals(session, service: bool = False) -> dict:
    return _with_units(_call(session, _request(session, "goals", service=bool(service)), WORKER_TIMEOUT_S))


def flow_info(session) -> dict:
    """Projects, the active one's features, and its mesh level, minimum gap, domain and last
    mesh size. The settings come from a second worker call: if reading them kills the
    worker, the rest still answers and says so."""
    reply = _call(session, _request(session, "info"), WORKER_TIMEOUT_S)
    if reply.get("active_project"):
        try:
            settings = _call(session, _request(session, "settings"), WORKER_TIMEOUT_S)
            reply["mesh_level"] = settings.get("mesh_level")
            reply["min_gap_mm"] = settings.get("min_gap_mm")
            reply["min_gap"] = "automatic (switched off in the project)" if settings.get("min_gap_mm") is None \
                else "manual"
            reply["domain_m"] = settings.get("domain_m")
        except SolidWorksError as exc:
            reply["settings_error"] = str(exc)
        progress = read_progress(reply.get("directory"))
        if progress:
            reply["last_run"] = {k: progress[k] for k in ("phase", "iteration", "travels", "cells", "finished_at")
                                 if progress.get(k) is not None}
    return reply


# --- progress: the solver's own files, no SolidWorks ------------------------------------------------


def progress_of(info: dict) -> dict:
    """What <n>.info.json (written by the solver every few iterations) says; pure.

    percent is the least-converged goal's progress, as Flow's monitor shows it: 0 until
    the solver has enough iterations to judge (about one travel), then up to 100."""
    tel = info.get("telemetry") or {}
    mesh = info.get("mesh") or {}
    goals = []
    for item in info.get("goals") or ():
        g = item.get("goal") if isinstance(item, dict) else None
        if not isinstance(g, dict):
            continue
        goals.append({"name": g.get("name"), "value": g.get("value"), "quantity": g.get("unit"),
                      "progress_pct": g.get("progress"), "delta": g.get("delta"), "criteria": g.get("criteria"),
                      "in_convergence": g.get("use_in_convergence")})
    judged = [g["progress_pct"] for g in goals if g["in_convergence"] and g["progress_pct"] is not None]
    iteration = tel.get("iteration")
    if info.get("finished"):
        phase = "finished"
    elif iteration:
        phase = "solving"
    elif mesh.get("cells_total"):
        phase = "meshed"
    else:
        phase = "meshing"
    out = {"phase": phase, "project": info.get("project_name"), "document": info.get("model_name"),
           "iteration": iteration, "travels": tel.get("travel"),
           "iterations_per_travel": tel.get("iterations_per_travel"), "cells": mesh.get("cells_total"),
           "cells_fluid": mesh.get("cells_fluid"), "goals": goals,
           "percent": 100.0 if info.get("finished") else (min(judged) if judged else None),
           "cpu_time_s": tel.get("cpu_time_solver_total"), "cpu_time_left_s": tel.get("cpu_time_left"),
           "finished_at": tel.get("last_iteration_finished") if info.get("finished") else None}
    return {k: v for k, v in out.items() if v is not None or k in ("iteration", "percent")}


def read_progress(directory: str | None) -> dict | None:
    """progress_of the project folder's <n>.info.json; None when there is none (yet)."""
    if not directory:
        return None
    try:
        names = [n for n in os.listdir(directory) if n.lower().endswith(".info.json")]
        if not names:
            return None
        path = max((os.path.join(directory, n) for n in names), key=os.path.getmtime)
        data = safefile.read_json(path)
        if not isinstance(data, dict):
            return None
        out = progress_of(data)
        out["info_age_s"] = round(max(0.0, time.time() - os.path.getmtime(path)), 1)
        return out
    except (OSError, ValueError):
        return None   # mid-write: the next look reads it


def pid_alive(pid) -> bool:
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def status_of(record: dict, progress: dict | None, worker_alive: bool, started_after: bool) -> dict:
    """flow_status' answer from a job record and the solver's progress; pure.

    started_after: the progress file was written after this job started (an older file
    is the previous run's and says nothing about this one)."""
    out = {"ok": True, "job_id": record.get("job_id"), "state": record.get("state"),
           "document": record.get("document"), "project": record.get("project"),
           "submitted": record.get("submitted")}
    if record.get("state") == "running" and not worker_alive:
        out["state"] = "lost"
        out["note"] = ("The server process that started this solve is gone; the solver itself may have "
                       "finished - see phase and goals, and flow_goals once the document is current.")
    if progress and started_after:
        out.update({k: v for k, v in progress.items() if k not in ("project", "document")})
    elif record.get("state") == "running":
        out["phase"] = "preparing"
    if record.get("state") == "failed":
        out["ok"] = False
        out["error"] = record.get("error")
    if record.get("state") == "done" and isinstance(record.get("result"), dict):
        out["result"] = {k: v for k, v in record["result"].items() if k != "ok"}
    for key in ("elapsed_s", "directory"):
        if record.get(key) is not None:
            out[key] = record[key]
    return out


def flow_status(job_id: str | None = None) -> dict:
    """Where a flow_solve stands, read from its job record and the solver's own progress
    file - no SolidWorks call, so it answers while SolidWorks is busy with anything."""
    import datetime as _dt

    from . import jobs
    if job_id:
        record = jobs.get(str(job_id))
        if record.get("tool") != "flow_solve":
            raise SolidWorksError(f"Job '{job_id}' is a {record.get('tool')}, not a flow_solve.")
    else:
        record = jobs.latest("flow_solve")
        if record is None:
            raise SolidWorksError("No flow_solve job in the last 24 h: start one with flow_solve.")
    progress = read_progress(record.get("directory"))
    started_after = False
    if progress:
        try:
            started = _dt.datetime.fromisoformat(record["submitted"]).timestamp()
            started_after = time.time() - progress["info_age_s"] >= started - 2.0
        except (KeyError, ValueError):
            started_after = True
    alive = record.get("state") != "running" or pid_alive(record.get("worker_pid") or record.get("pid"))
    return status_of(record, progress, alive, started_after)


def running_solves() -> list:
    """The flow_solve jobs still running, for server_health: [{job_id, document, project, iteration}]."""
    from . import jobs
    out = []
    for full in jobs.running("flow_solve"):
        if not pid_alive(full.get("worker_pid") or full.get("pid")):
            continue
        progress = read_progress(full.get("directory")) or {}
        out.append({"job_id": full["job_id"], "document": full.get("document"), "project": full.get("project"),
                    "since": full.get("submitted"), "phase": progress.get("phase"),
                    "iteration": progress.get("iteration"), "percent": progress.get("percent")})
    return out


# --- the mesh as it is, and as the solver gets it -------------------------------------------------------
#
# No API writes a control plane's stretch ratio or the thin-channel refinement level, and
# none reads them from the project (verified on Flow Simulation 2025: NIK lists two mesh
# parameters in the automatic mode - level and gap; the classic add-in API's
# GlobalMeshSettings.GetParameter answers E_FAIL for every parameter type, in both
# modes). They follow from switches of the automatic mesh that a project inherits from
# the template or the project it was made from. What can be done, and is done here:
# - read: the classic API's base-mesh grid lines (live), and the solver's settings file,
#   which Flow writes on demand without meshing (UpdateConfigAndDataFiles) - the truth;
# - write: level, gap (also switching it on and off, which NIK cannot), domain, and
#   the automatic / manual switch (manual keeps the present mesh as it is);
# - hold: compare the file with a specification and say what differs.
# A project with other hidden switches is not repaired - it is made anew from a project
# or a template that has the right ones (flow_create_from_case).


def ensure_addin(session):
    """Flow's add-in object, loading the add-in when it is not on."""
    import win32com.client
    obj = None
    try:
        obj = session._sw.GetAddInObject("FloWorks.App")
    except Exception:  # noqa: BLE001
        obj = None
    if obj is None:
        for name in ("{D08D09B9-5944-48DF-9269-936C312A2FEE}", "FW03.dll"):
            try:
                session._sw.LoadAddIn(name)
                obj = session._sw.GetAddInObject("FloWorks.App")
            except Exception:  # noqa: BLE001
                obj = None
            if obj is not None:
                break
    if obj is None:
        raise SolidWorksError("The Flow Simulation add-in is not on and would not load: switch it on in "
                              "SolidWorks (Tools > Add-Ins > SOLIDWORKS Flow Simulation).")
    return win32com.client.Dispatch(obj)


def _classic_project(session, name: str | None = None):
    """The classic add-in API's project object: the active one, or by name. COM thread."""
    model, _path = _document(session)
    try:
        doc = ensure_addin(session).GetAPI().GetDocument(model)
        if name is None:
            project = doc.IActiveProject
        else:
            project = next((p for p in (doc.Projects or ()) if str(p.ProjectName).lower() == str(name).lower()),
                           None)
    except SolidWorksError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SolidWorksError(f"Flow's add-in API did not answer ({type(exc).__name__}): is the document a "
                              "Flow document with a project?") from None
    if project is None:
        raise SolidWorksError("The document has no active Flow project" if name is None
                              else f"The document has no Flow project '{name}'.")
    return project


def live_mesh(session, name: str | None = None) -> dict:
    """What the add-in tells of the project's mesh now: {automatic, min_gap, domain_m, base_mesh}."""
    project = _classic_project(session, name)
    out = {"project": project.ProjectName, "automatic": bool(project.AutomaticMeshDefinition)}
    custom = bool(project.CustomMinimalGapSize)
    out["min_gap_mm"] = round(float(project.MinimalGapSize) * 1000.0, 9)
    out["min_gap"] = "manual" if custom else "automatic (switched off in the project; the value is Flow's own)"
    out["domain_m"] = {key: float(getattr(project, "ComputationalDomain" + key[0].upper() + key[1:]))
                       for key in DOMAIN_KEYS}
    try:
        planes = project.GetBasicMeshPlanes()
        base = {}
        for axis, lines in zip("xyz", planes[:3]):
            runs = flow_case.intervals_of(lines)
            base[axis] = {"cells": len(lines) - 1,
                          "intervals": [{k: (round(v, 9) if isinstance(v, float) else v) for k, v in r.items()}
                                        for r in runs]}
        out["base_mesh"] = base
        out["base_mesh_even"] = all(r["ratio"] == 1.0 for a in base.values() for r in a["intervals"])
    except Exception as exc:  # noqa: BLE001
        out["base_mesh_error"] = f"{type(exc).__name__}"
    return out


def flow_mesh_get(session, prepare: bool = True, force: bool = False) -> dict:
    """The active project's mesh: live (add-in) and as the solver gets it (settings file)."""
    live = live_mesh(session)
    settings = _call(session, _request(session, "settings"))
    reply = {"ok": True, "project": live["project"], "mesh_level": settings.get("mesh_level"), "live": live,
             "directory": settings.get("directory")}
    info = _call(session, _request(session, "info"))
    reply["local_meshes"] = [f["name"] for f in info.get("features", []) if f.get("type") == "local_mesh"]
    path = None
    if prepare:
        try:
            path = _call(session, _request(session, "prepare", force=bool(force))).get("config")
            reply["solver_file"] = "written now from the project"
        except SolidWorksError as exc:
            reply["solver_file"] = f"not written: {exc}"
    if path is None:
        path = flow_case.config_path(settings.get("directory"))
        if path and "solver_file" not in reply:
            reply["solver_file"] = "as left by the last preparation or solve (prepare=False)"
    if path:
        flat = flow_case.read_config(path)
        reply["solver"] = flow_case.mesh_of(flat)
        reply["fingerprint"] = {"settings": flow_case.fingerprint(flat), "mesh": flow_case.fingerprint(flat, "mesh")}
    return reply


def mesh_spec(spec) -> dict:
    """A mesh specification as flow_mesh_set takes it, normalised: {mesh_level, min_gap_mm
    (number, or None for automatic), domain_m, automatic, expect: solver keys to hold}.
    Accepts flow_mesh_get's 'solver' block or a case's 'mesh' block as they are."""
    if not isinstance(spec, dict) or not spec:
        raise SolidWorksError("spec is an object: {mesh_level, min_gap_mm, domain_m, automatic} and/or the "
                              "'solver' block of flow_mesh_get / the 'mesh' block of a case.")
    known = {"mesh_level", "min_gap_mm", "domain_m", "automatic", "result_resolution", "min_gap_m", "min_wall_m",
             "cells", "planes", "refinement"}
    unknown = sorted(set(spec) - known)
    if unknown:
        raise SolidWorksError(f"Unknown mesh setting(s) {unknown}. Known: {sorted(known)}.")
    out = {"expect": {}}
    if spec.get("mesh_level") is not None:
        out["mesh_level"] = _mesh_level(spec["mesh_level"])
    elif spec.get("result_resolution") is not None:
        # the solver file counts the level from 0 (a level-5 project has ResultResolution 4)
        out["mesh_level"] = _mesh_level(int(spec["result_resolution"]) + 1)
    if "min_gap_mm" in spec:
        out["min_gap_mm"] = None if spec["min_gap_mm"] in (None, "auto", "automatic") else \
            min_gap(spec["min_gap_mm"]) * 1000.0
    elif spec.get("min_gap_m") is not None:
        out["min_gap_mm"] = min_gap(float(spec["min_gap_m"]) * 1000.0) * 1000.0
    if spec.get("domain_m") is not None:
        out["domain_m"] = domain_values(spec["domain_m"])
    if spec.get("automatic") is not None:
        out["automatic"] = bool(spec["automatic"])
    for key in ("cells", "planes", "refinement", "min_wall_m"):
        if spec.get(key) is not None:
            out["expect"][key] = spec[key]
    return out


def mesh_mismatch(solver: dict, expect: dict) -> list:
    """Where the solver's mesh (mesh_of) differs from what a specification expects:
    [{key, have, want}]. Numbers are compared to 1e-9 relative."""
    def same(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b

    found = []
    if "cells" in expect and not same(solver.get("cells"), expect["cells"]):
        found.append({"key": "cells", "have": solver.get("cells"), "want": expect["cells"]})
    if "min_wall_m" in expect and not same(solver.get("min_wall_m"), expect["min_wall_m"]):
        found.append({"key": "min_wall_m", "have": solver.get("min_wall_m"), "want": expect["min_wall_m"]})
    for axis, want in (expect.get("planes") or {}).items():
        have = (solver.get("planes") or {}).get(axis) or {}
        for key in ("points_m", "cells", "ratios", "manual"):
            if key in want and not same(have.get(key), want[key]):
                found.append({"key": f"planes.{axis}.{key}", "have": have.get(key), "want": want[key]})
    for key, want in (expect.get("refinement") or {}).items():
        have = (solver.get("refinement") or {}).get(key)
        if key != "ToleranceCriteriaValue" and not same(have, want):
            found.append({"key": f"refinement.{key}", "have": have, "want": want})
    return found


def flow_mesh_set(session, spec: dict, force: bool = False) -> dict:
    """Write what an API writes (level, gap, domain, automatic / manual), then read the
    solver's settings file back and hold it against the rest of the specification."""
    wanted = mesh_spec(spec)
    done = {}
    project = _classic_project(session)
    name = project.ProjectName
    if "automatic" in wanted:
        project.AutomaticMeshDefinition = wanted["automatic"]
        if bool(project.AutomaticMeshDefinition) != wanted["automatic"]:
            raise SolidWorksError("Flow did not switch the mesh definition between automatic and manual.")
        done["automatic"] = wanted["automatic"]
    if "min_gap_mm" in wanted:
        if wanted["min_gap_mm"] is None:
            project.CustomMinimalGapSize = False
        else:
            project.CustomMinimalGapSize = True
            project.MinimalGapSize = wanted["min_gap_mm"] / 1000.0
        on = bool(project.CustomMinimalGapSize)
        if on != (wanted["min_gap_mm"] is not None) or (
                on and abs(float(project.MinimalGapSize) * 1000.0 - wanted["min_gap_mm"]) > 1e-9):
            raise SolidWorksError(f"Flow did not take the minimum gap size (it reads back "
                                  f"{'on' if on else 'off'}, {float(project.MinimalGapSize) * 1000.0} mm)."
                                  + (f" Already set: {done}." if done else ""))
        done["min_gap_mm"] = wanted["min_gap_mm"]
    project = None
    if wanted.get("mesh_level") is not None or wanted.get("domain_m"):
        try:
            done.update(_apply(session, mesh_level=wanted.get("mesh_level"),
                               domain_m=wanted.get("domain_m"))["set"])
        except SolidWorksError as exc:
            raise SolidWorksError(f"{exc}" + (f" Already set: {done}." if done else "")) from None
    reply = {"ok": True, "project": name, "set": done}
    got = flow_mesh_get(session, True, force)
    reply.update({k: got[k] for k in ("mesh_level", "live", "solver", "fingerprint", "solver_file") if k in got})
    if wanted["expect"]:
        if "solver" not in got:
            raise SolidWorksError(f"The settings were written ({done}) but the solver's settings file could not "
                                  f"be read back to check the rest: {got.get('solver_file')}")
        found = mesh_mismatch(got["solver"], wanted["expect"])
        reply["verified"] = not found
        if found:
            reply["differences"] = found
            shown = "; ".join(f"{d['key']}: {d['have']} (wanted {d['want']})" for d in found[:8])
            raise SolidWorksError(
                f"The mesh of '{name}' is not the specified one in {len(found)} place(s): {shown}"
                f"{' ...' if len(found) > 8 else ''}. Written: {done or 'nothing'}. What remains is decided by "
                "switches of the automatic mesh that no API writes; they come with the project or template a "
                "project is made from - make the project with flow_create_from_case (a clone of the reference "
                "project, or a template made from it).")
    return reply


# --- cases --------------------------------------------------------------------------------------------


def _case_folder() -> str:
    folder = private.get("cases_dir")
    if not folder:
        raise SolidWorksError("No folder for case files: set cases_dir in the private settings, or give a path.")
    return folder


def flow_case_from_results(name: str, results_dir: str, path: str | None = None, note: str | None = None,
                           template: str | None = None) -> dict:
    """A case file from a FINISHED run's folder - nothing is opened and nothing in that
    folder is written. Pure but for the files: no SolidWorks."""
    config = flow_case.config_path(results_dir)
    if not config:
        raise SolidWorksError("That folder holds no solver settings file (.xmlconfig): it is not a Flow "
                              "project folder, or the project was never prepared.")
    try:
        flat = flow_case.read_config(config)
        case = flow_case.make_case(flat, name, results_dir, note)
    except flow_case.CaseError as exc:
        raise SolidWorksError(str(exc)) from None
    case["goals"] = flow_case.goals_of(flat)
    return _save_case(case, name, path, template)


def _save_case(case: dict, name: str, path: str | None, template: str | None) -> dict:
    if template:
        try:
            found = flow_templates.resolve(template, private.get("templates_dir"))
        except flow_templates.TemplateError as exc:
            raise SolidWorksError(str(exc)) from None
        case["template"] = {"file": os.path.basename(found["path"]), "md5": found["md5"],
                            "source": found["source"]}
    if path is None:
        folder = _case_folder()
        path = os.path.join(folder, name if name.lower().endswith(".json") else name + ".json")
    if os.path.exists(path):
        folder, stem = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
        raise SolidWorksError(f"The case '{stem}' exists and a case is never overwritten: use the next version, "
                              f"'{flow_case.next_version(folder, stem)}'.")
    flow_case.save_json(path, case)
    return {"ok": True, "case": case["case"], "path": path, "md5": flow_templates.md5_of(path),
            "fingerprint": case["fingerprint"], "expect": case["expect"], "mesh": case["mesh"],
            "conditions": case["conditions"], "goals": case.get("goals"), "template": case.get("template"),
            "source": {k: case["source"].get(k) for k in ("project", "model", "computer", "cad", "solver")}}


def flow_case_from_project(session, name: str, path: str | None = None, note: str | None = None,
                           template: str | None = None) -> dict:
    """A case file from the ACTIVE project of the current document: the solver settings of
    its finished run when it has one (as solved - nothing is rewritten), else as Flow
    writes them now."""
    if not str(name or "").strip():
        raise SolidWorksError("name is empty: name the case, e.g. 'CASE-a_v01'.")
    settings = _call(session, _request(session, "settings"))
    folder = settings.get("directory")
    holder = flow_case.owner(folder) if folder else None
    if not (holder and holder.get("finished")):
        _call(session, _request(session, "prepare"))
    reply = flow_case_from_results(str(name).strip(), folder, path, note, template)
    reply["from"] = "the finished run's settings" if holder and holder.get("finished") else \
        "the project as it stands (not solved yet: no expected cells or goals)"
    return reply


def case_velocity(case: dict, alpha_deg: float | None, velocity_mps) -> list | None:
    """The free stream of a run of this case: as given, or the case's speed turned to
    alpha_deg (flow along -Z, lift along +Y: vy = V sin a, vz = -V cos a); None = the case's own."""
    import math
    if velocity_mps is not None:
        if alpha_deg is not None:
            raise SolidWorksError("Give alpha_deg or velocity_mps, not both.")
        if len(velocity_mps) != 3:
            raise SolidWorksError("velocity_mps is [vx, vy, vz] in m/s.")
        return [float(v) for v in velocity_mps]
    if alpha_deg is None:
        return None
    own = flow_case.velocity_of(flow_case.case_settings(case))
    if own is None:
        raise SolidWorksError("The case has no free-stream velocity to turn: give velocity_mps.")
    speed = math.hypot(float(own[1]), float(own[2]))
    a = math.radians(float(alpha_deg))
    return [float(own[0]), speed * math.sin(a), -speed * math.cos(a)]


def flow_create_from_case(session, case, name: str, alpha_deg: float | None = None, velocity_mps=None,
                          clone_of: str | None = None, template: str | None = None) -> dict:
    """A project that calculates the way the case does, checked before it is handed back."""
    loaded, case_path = resolve_case(case)
    if loaded is None:
        raise SolidWorksError("case is empty: name a case file (path, or a name in the cases folder).")
    if not str(name or "").strip():
        raise SolidWorksError("name is empty: name the new project.")
    name = str(name).strip()
    velocity = case_velocity(loaded, alpha_deg, velocity_mps)
    info = _call(session, _request(session, "info"))
    projects = info.get("projects") or []
    if any(p.lower() == name.lower() for p in projects):
        raise SolidWorksError(f"The document already has a Flow project '{name}'.")
    source = clone_of or (loaded.get("source") or {}).get("project")
    made_by = None
    if source and any(p.lower() == str(source).lower() for p in projects):
        reply = _call(session, _request(session, "clone", name=source, new_name=name, copy_results=False,
                                        slot=private.get("flow_folder_slot")))
        made_by = {"cloned_from": reply.get("cloned_from")}
    else:
        if clone_of:
            raise SolidWorksError(f"The document has no Flow project '{clone_of}' to clone "
                                  f"(it has: {', '.join(projects) or 'none'}).")
        wanted = template or (loaded.get("template") or {}).get("file")
        if not wanted:
            raise SolidWorksError(
                f"The document has no project '{source}' to clone and the case names no template. The hidden "
                "switches of the mesh travel only with a project or a template: in Flow, on the reference "
                "project, use Flow Simulation > Project > Create Template, put the .fwp into the shared "
                "templates folder, record it (flow_template_record) and pass template=<its name>.")
        try:
            found = flow_templates.resolve(wanted, private.get("templates_dir"))
        except flow_templates.TemplateError as exc:
            raise SolidWorksError(str(exc)) from None
        md5 = (loaded.get("template") or {}).get("md5")
        if md5 and not template and md5 != found["md5"]:
            raise SolidWorksError(f"Template '{wanted}' is not the file the case was made with (MD5 {found['md5']}, "
                                  f"the case says {md5}).")
        mesh = loaded.get("mesh") or {}
        level = None if mesh.get("result_resolution") is None else int(mesh["result_resolution"]) + 1
        reply = _call(session, _request(session, "create_project", name=name, template=found["path"],
                                        mesh_level=level, slot=private.get("flow_folder_slot")))
        made_by = {"template": os.path.basename(found["path"]), "template_md5": found["md5"]}
        if reply.get("template_items_removed"):
            made_by["template_items_removed"] = reply["template_items_removed"]
        spec = {k: mesh[k] for k in ("min_gap_m", "domain_m") if mesh.get(k) is not None}
        if spec:
            flow_mesh_set(session, spec)
    try:
        if velocity is not None:
            _apply(session, velocity_mps=velocity)
        config = _call(session, _request(session, "prepare")).get("config")
        flat = flow_case.read_config(config)
        exact = flow_case.same_run(flat, loaded)
        found = flow_case.check_case(flat, loaded, "all", exact)
    except (SolidWorksError, flow_case.CaseError) as exc:
        _discard(session, name)
        raise SolidWorksError(f"{exc} The new project '{name}' was removed.") from None
    if found and made_by.get("template"):
        # goals belong to the geometry: a template brings none that bind here
        found = [d for d in found if not d["key"].startswith(("Features_Surface_Goal", "ConvergenceOptions[0]/GoalsInfo"))]
    if found:
        _discard(session, name)
        shown = "; ".join(f"{d['key']}: {d['have']} (case: {d['want']})" for d in found[:10])
        raise SolidWorksError(f"The project made for case '{loaded.get('case')}' differs from it in {len(found)} "
                              f"setting(s): {shown}{' ...' if len(found) > 10 else ''}. It was removed; nothing "
                              "was solved.")
    out = {"ok": True, "project": name, **made_by, "case": loaded.get("case"), "case_path": case_path,
           "verified": True,
           "same_run": exact, "directory": os.path.dirname(config),
           "velocity_mps": flow_case.velocity_of(flat),
           "fingerprint": {"settings": flow_case.fingerprint(flat), "mesh": flow_case.fingerprint(flat, "mesh")},
           "cells_expected": (loaded.get("expect") or {}).get("cells") if exact else None}
    if made_by.get("template"):
        out["note"] = ("Made from a template: add this model's goals (flow_add_goal) - the case lists the "
                       "reference's under 'goals'.")
        out["case_goals"] = loaded.get("goals")
    return out


def _discard(session, name: str) -> None:
    try:
        _call(session, _request(session, "delete", name=name, force=True))
    except SolidWorksError:
        pass


# --- comparing runs --------------------------------------------------------------------------------------


def _run_folder(run: str) -> str:
    text = str(run or "").strip()
    if not text:
        raise SolidWorksError("A run is a Flow project folder (path, or a folder name in the private "
                              "compare_runs_dir), or its result.json.")
    if os.path.basename(text).lower() == RESULT_FILE:
        text = os.path.dirname(text)
    if not os.path.isabs(text):
        root = private.get("compare_runs_dir")
        if not root:
            raise SolidWorksError("A run by folder name needs compare_runs_dir in the private settings; or give "
                                  "the folder's path.")
        text = os.path.join(root, text)
    if not flow_case.config_path(text):
        raise SolidWorksError(f"'{os.path.basename(text)}' is not a Flow project folder with a prepared "
                              "calculation.")
    return text


def flow_compare(run_a: str, run_b: str, force: bool = False) -> dict:
    """Two finished runs: comparable (the same way of calculating)? and the differences of
    their goals. No SolidWorks; nothing is written."""
    a, b = _run_folder(run_a), _run_folder(run_b)
    try:
        flat_a, flat_b = (flow_case.read_config(flow_case.config_path(f)) for f in (a, b))
        rec_a, rec_b = flow_case.result_record(a), flow_case.result_record(b)
    except flow_case.CaseError as exc:
        raise SolidWorksError(str(exc)) from None
    pseudo = {"settings": {k[len(flow_case.ROOT):]: v for k, v in flow_case.stable(flat_a).items()
                           if k.startswith(flow_case.ROOT)}}
    same_stream = flow_case.same_run(flat_b, pseudo)
    found = flow_case.check_case(flat_b, pseudo, "all", same_stream)
    # two geometries bind their goals to other faces: that is not a way of calculating
    found = [d for d in found if "Faces_Keys" not in d["key"]]
    reply = {"ok": True, "a": {"folder": a, "project": rec_a.get("project"), "model": rec_a.get("model"),
                   "cells": rec_a.get("cells"), "fingerprint": rec_a["fingerprint"]},
             "b": {"folder": b, "project": rec_b.get("project"), "model": rec_b.get("model"),
                   "cells": rec_b.get("cells"), "fingerprint": rec_b["fingerprint"]},
             "same_run": same_stream, "comparable": not found}
    if found:
        reply["differences"] = [{"key": d["key"], "b": d["have"], "a": d["want"]} for d in found[:40]]
        if not force:
            shown = "; ".join(f"{d['key']}: {d['want']} | {d['have']}" for d in found[:8])
            raise SolidWorksError(f"The runs are not comparable - {len(found)} setting(s) differ (a | b): {shown}"
                                  f"{' ...' if len(found) > 8 else ''}. force=True compares the goals anyway.")
        reply["warning"] = (f"NOT comparable: {len(found)} setting(s) differ (differences). The deltas below mix "
                            "the effect of the settings with the effect looked for.")
    reply["goals"] = flow_case.compare_runs(rec_a, rec_b)["goals"]
    reduction = private.get("reference.reduction")
    if isinstance(reduction, dict):
        coefficients = {}
        for which in ("value", "averaged"):
            ra, rb = (flow_case.reduce_forces(rec["goals"], flow_case.velocity_of(flat), reduction.get("q_pa"),
                                              reduction.get("S_m2"), reduction.get("c_m"),
                                              reduction.get("z_cg_m", 0.0), which)
                      for rec, flat in ((rec_a, flat_a), (rec_b, flat_b)))
            if ra and rb:
                coefficients[which] = {k: {"a": ra[k], "b": rb[k], "delta": rb[k] - ra[k]}
                                       for k in ("CL", "CD", "Cm") if k in ra and k in rb}
        if coefficients:
            reply["coefficients"] = coefficients
    return reply


def flow_template_record(file_name: str, source: str | None = None, status: str | None = None) -> dict:
    """Enter a template of the shared folder into its manifest with the file's MD5."""
    folder = private.get("templates_dir")
    if not folder:
        raise SolidWorksError("No shared templates folder: set templates_dir in the private settings.")
    name = file_name if str(file_name).lower().endswith(".fwp") else f"{file_name}.fwp"
    try:
        entry = flow_templates.record(folder, name, source=source, status=status,
                                      date=time.strftime("%Y-%m-%d %H:%M"), computer=computer_name())
        return {"ok": True, "entry": entry, "folder_state": flow_templates.check_folder(folder)}
    except flow_templates.TemplateError as exc:
        raise SolidWorksError(str(exc)) from None


def health() -> dict:
    """For server_health: the private settings (which keys, never values), the shared
    templates against their manifest, this PC. No SolidWorks."""
    out = {"private_settings": private.status(), "computer_cores": os.cpu_count()}
    try:
        folder = private.get("templates_dir")
        if folder:
            state = flow_templates.check_folder(folder) if os.path.isdir(folder) else {"error": "not reachable"}
            out["shared_templates"] = state
        out["folder_slot"] = private.get("flow_folder_slot")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
