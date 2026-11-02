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
import json
import os
import subprocess
import sys

from .. import binding
from ..errors import SolidWorksError

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


@contextlib.contextmanager
def _silent(session):
    """Flow's own add-in API in silent mode: its Yes/No questions (reset the computational
    domain? adjust the mesh?) would otherwise wait for a click in SolidWorks while the
    worker waits for them. NIK has no such switch; the classic FloWorks.App API does."""
    api = None
    try:
        import win32com.client
        api = win32com.client.Dispatch(session._sw.GetAddInObject("FloWorks.App")).GetAPI()
        api.SetSilent()
    except Exception:  # noqa: BLE001 - no add-in object: the worker names the problem
        api = None
    try:
        yield
    finally:
        if api is not None:
            try:
                api.ResetSilent()
            except Exception:  # noqa: BLE001
                pass


def _call(session, request: dict, timeout_s: float = None) -> dict:
    with _silent(session):
        return _run_worker(request, WORKER_TIMEOUT_S if timeout_s is None else timeout_s)


def _run_worker(request: dict, timeout_s: float) -> dict:
    try:
        run = subprocess.run([sys.executable, "-m", "solidworks_mcp.rbr.flow_worker"],
                             input=json.dumps(request), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout_s,
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


def _request(session, op: str, **kw) -> dict:
    model, path = _document(session)
    pid = int(session._sw.GetProcessID())
    return {"op": op, "pid": pid, "document": path, **kw}


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


def flow_create_project(session, name: str, template: str = "internal_water") -> dict:
    return _call(session, _request(session, "create_project", name=str(name), template=template_path(template)),
                       WORKER_TIMEOUT_S)


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
                   temperature_k: float | None = None, mesh_level: int | None = None) -> dict:
    if velocity_mps is not None and len(velocity_mps) != 3:
        raise SolidWorksError("velocity_mps is [vx, vy, vz] in m/s.")
    if mesh_level is not None and not 1 <= int(mesh_level) <= 7:
        raise SolidWorksError("mesh_level is Flow's automatic mesh level, 1 (coarse) to 7 (fine).")
    return _call(session, _request(session, "set_conditions", velocity=velocity_mps, pressure=pressure_pa,
                                temperature=temperature_k,
                                mesh_level=None if mesh_level is None else int(mesh_level)), WORKER_TIMEOUT_S)


def _with_units(reply: dict) -> dict:
    for item in reply.get("goals", []) + reply.get("service_goals", []):
        item["unit"] = unit(item.pop("unit_id", None))
    return reply


def flow_solve(session, timeout_s: float = SOLVE_TIMEOUT_S) -> dict:
    session._require_model().Save3(1, 0, 0)  # Flow solves the project of the file on disk
    return _with_units(_call(session, _request(session, "solve"), max(60.0, min(float(timeout_s), SOLVE_TIMEOUT_S))))


def flow_goals(session, service: bool = False) -> dict:
    return _with_units(_call(session, _request(session, "goals", service=bool(service)), WORKER_TIMEOUT_S))


def flow_info(session) -> dict:
    return _call(session, _request(session, "info"), WORKER_TIMEOUT_S)
