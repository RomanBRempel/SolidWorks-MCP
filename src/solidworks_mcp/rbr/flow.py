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
import threading
import time

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


def flow_create_project(session, name: str, template: str = "internal_water", mesh_level: int = DEFAULT_MESH_LEVEL,
                        domain_m=None, min_gap_mm: float | None = None) -> dict:
    level = _mesh_level(mesh_level)
    domain_values(domain_m), min_gap(min_gap_mm)   # a bad value refuses before anything is made
    reply = _call(session, _request(session, "create_project", name=str(name), template=template_path(template),
                                    mesh_level=level))
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
                                    copy_results=bool(copy_results)))
    if any(v is not None for v in (velocity_mps, pressure_pa, temperature_k, mesh_level, domain_m, min_gap_mm)):
        try:
            reply["set"] = _apply(session, velocity_mps, pressure_pa, temperature_k, mesh_level, domain_m,
                                  min_gap_mm)["set"]
        except SolidWorksError as exc:
            raise SolidWorksError(f"Project '{reply.get('project')}' was cloned and is active, but: {exc}") from None
    return reply


def flow_delete(session, name: str) -> dict:
    return _call(session, _request(session, "delete", name=str(name)))


def _with_units(reply: dict) -> dict:
    for item in reply.get("goals", []) + reply.get("service_goals", []):
        item["unit"] = unit(item.pop("unit_id", None))
    return reply


# --- the solve: started on the COM thread, waited for off it ---------------------------------------


def solve_begin(session) -> dict:
    """On the COM thread: save (Flow solves the project of the file on disk), switch Flow's
    questions off for the run, and return the worker request. solve_end undoes the switch."""
    model, _path = _document(session)
    request = _request(session, "solve")
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


def flow_solve(session, timeout_s: float = SOLVE_TIMEOUT_S) -> dict:
    """The whole solve in one blocking call (scripts and tests); the MCP tool runs
    solve_begin / solve_run / solve_end itself so that the COM thread is free meanwhile."""
    request = solve_begin(session)
    try:
        return solve_run(request, None, max(60.0, min(float(timeout_s), SOLVE_TIMEOUT_S)))
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
        with open(path, encoding="utf-8") as fh:
            out = progress_of(json.load(fh))
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
