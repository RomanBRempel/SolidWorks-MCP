"""The Flow Simulation worker: NIK API calls in a process of their own (see rbr/flow.py).

Reads one JSON request on stdin, writes one JSON line on stdout:
{"ok": true, ...} or {"ok": false, "error": "..."}. Started by flow._run_worker
as `python -m solidworks_mcp.rbr.flow_worker`; never imported by the server.

NIK traps, all met on Flow Simulation 2025 SP3:
- reading or setting a computational-domain parameter is an access violation in
  this process; the domain is only ever reset to automatic (Rebuild(True, ...)),
- some parameters' values cannot be read (they raise); only their Type is read
  while looking one up,
- Solve and Rebuild return False without a reason; Flow writes it to
  %TEMP%\\ngp.log ("warning ... [FW03.dll] ..."), which is quoted back.
"""

import json
import os
import sys
import time
import traceback
import winreg

NIK_TYPELIB = ("{CB182AE6-7258-43F3-B058-25ACFBD58535}", 0, 1, 0)
FEATURE_TYPES = {2: "boundary_condition", 3: "surface_source", 4: "volume_source", 5: "rotating_region",
                 8: "solid_material", 11: "fluid_subdomain", 12: "surface_goal", 13: "volume_goal",
                 14: "local_mesh"}
BOUNDARY_CONDITION, SURFACE_GOAL = 2, 12
P_BC_TYPE, P_MESH_LEVEL = 41, 7
P_PRESSURE, P_TEMPERATURE, P_VX, P_VY, P_VZ = 9, 10, 11, 12, 13
NO_ERROR = 0
LOG = os.path.join(os.environ.get("TEMP", ""), "ngp.log")


class Refused(Exception):
    pass


def product_key() -> str:
    """The newest installed 'Srac\\COSMOS_FloWorks <year>' with a Path: what LoadProductAPI takes."""
    found = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Srac") as root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            name = winreg.EnumKey(root, i)
            if not name.startswith("COSMOS_FloWorks "):
                continue
            try:
                with winreg.OpenKey(root, name) as key:
                    if winreg.QueryValueEx(key, "Path")[0]:
                        found.append(name)
            except OSError:
                continue
    if not found:
        raise Refused("SOLIDWORKS Flow Simulation is not installed (no HKLM\\SOFTWARE\\Srac\\COSMOS_FloWorks key).")
    return "Srac\\" + max(found)


def log_offset() -> int:
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def log_warnings(offset: int) -> list:
    """What Flow wrote to ngp.log since `offset`: the reasons Solve/Rebuild do not return."""
    try:
        with open(LOG, "rb") as fh:
            fh.seek(offset)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if "[FW" in line and ("warning" in line.lower() or "error" in line.lower()):
            message = line.split("] ", 1)[-1].strip()
            if message not in out:
                out.append(message)
    return out


class Flow:
    def __init__(self, request: dict):
        import win32com.client
        from win32com.client import gencache

        self.nik = gencache.EnsureModule(*NIK_TYPELIB)
        if self.nik is None:
            raise Refused("The Flow Simulation API (NIKCommonApi2) is not registered: install the Flow "
                          "Simulation API SDK from <Flow folder>\\API.")
        # kept for the worker's lifetime: releasing it unloads the product API, and every
        # object it handed out then answers RPC_E_DISCONNECTED
        self.base = self.nik.IBaseApiObject(win32com.client.Dispatch("NIKCommonApi2.BaseApiObject")._oleobj_)
        if not self.base.LoadProductAPI(product_key()):
            raise Refused("The Flow Simulation API would not load for this Flow installation.")
        attached = self.base.Attach2RunningObject2(int(request["pid"]))
        if attached is None:
            raise Refused("The Flow Simulation API could not attach to SolidWorks; is the Flow Simulation "
                          "add-in on (Tools > Add-Ins)?")
        self.app = self.nik.IInteractiveApplication(attached._oleobj_)
        active = self.app.ActiveDocument
        if active is None:
            raise Refused("Flow sees no active document in SolidWorks.")
        self.doc = self.nik.IModelDoc(active._oleobj_)
        want = os.path.normcase(os.path.abspath(request["document"]))
        have = os.path.normcase(os.path.abspath(self.doc.GetPathName() or ""))
        if want != have:
            raise Refused(f"Flow's active document is '{have}', not '{want}'.")

    def wrap(self, obj, iface):
        return getattr(self.nik, iface)(obj._oleobj_)

    def project(self):
        proj = self.doc.GetActiveProject()
        if proj is None:
            raise Refused("The document has no Flow project: call flow_create_project first.")
        return self.wrap(proj, "IFDAProject")

    def features(self, proj) -> list:
        out = []
        e = proj.EnumFeatures()  # None for a project with no features yet
        f = e.Next() if e is not None else None
        while f is not None:
            f = self.wrap(f, "IFeature")
            out.append({"name": f.Name, "type": FEATURE_TYPES.get(f.Type, f"type {f.Type}")})
            f = e.Next()
        return out

    def parameter(self, owner, kind: int):
        """The parameter of `owner` with nikParametersTypes_e `kind` (only Types are read on the way)."""
        pf = owner.GetInterface("IParametrizedFeature")
        if pf is None:
            raise Refused("This Flow item has no parameters.")
        e = self.wrap(pf, "IParametrizedFeature").EnumParameters()
        p = e.Next() if e is not None else None
        while p is not None:
            p = self.wrap(p, "IParameter")
            if p.Type == kind:
                return p
            p = e.Next()
        raise Refused(f"This Flow item has no parameter {kind}.")

    def set(self, owner, kind: int, value, label: str, integer: bool = False):
        p = self.parameter(owner, kind)
        code = p.SetLongValue(int(value)) if integer else p.SetValue(float(value))
        if code != NO_ERROR:
            raise Refused(f"Flow refused {label} = {value} (error {code}).")

    def rebuild_error(self, proj, offset: int, what: str) -> str:
        reasons = [r for r in [proj.GetLastRebuildError()] + log_warnings(offset) if r]
        return f"{what}: " + ("; ".join(reasons) if reasons else "Flow gave no reason (see its Messages pane).")

    # --- operations -----------------------------------------------------------------

    def create_project(self, request: dict) -> dict:
        offset = log_offset()
        if request["name"] in (self.doc.GetProjectNames() or ()):
            raise Refused(f"The document already has a Flow project '{request['name']}'.")
        proj = self.doc.CreateProjectFromTemplate(request["template"], request["name"])
        if proj is None:
            raise Refused(self.rebuild_error(self.project(), offset, "Flow did not create the project")
                          if self.doc.GetActiveProject() is not None else
                          f"Flow did not create the project from '{request['template']}'.")
        proj = self.wrap(proj, "IFDAProject")
        if not self.doc.Rebuild(True, True, False, True, True):  # reset the template's domain to this model
            raise Refused(self.rebuild_error(proj, offset, "The new project does not rebuild"))
        return {"project": proj.Name, "template": request["template"], "features": self.features(proj)}

    def add_boundary_condition(self, request: dict) -> dict:
        proj = self.project()
        f = self.wrap(proj.CreateTemporaryFeature(BOUNDARY_CONDITION), "IFeature")
        self.set(f, P_BC_TYPE, request["bc_type"], "the boundary condition type", integer=True)
        if request.get("value_param") is not None:
            self.set(f, request["value_param"], request["value"], "the boundary condition value")
        return self._place(proj, f, request.get("name"))

    def add_goal(self, request: dict) -> dict:
        proj = self.project()
        f = self.wrap(proj.CreateTemporaryFeature(SURFACE_GOAL), "IFeature")
        g = self.wrap(f.GetInterface("IParameterGoal"), "IParameterGoal")
        if g.SetParameter(request["parameter"]) != NO_ERROR:
            raise Refused(f"Flow refused goal parameter {request['parameter']}.")
        if request.get("calculate") is not None and g.SetValueToCalculate(request["calculate"]) != NO_ERROR:
            raise Refused("Flow refused that value to calculate (min/max/average/bulk_average) for this goal.")
        return self._place(proj, f, request.get("name"))

    def _place(self, proj, f, name) -> dict:
        topo = self.wrap(f.GetInterface("ITopologyBasedFeature"), "ITopologyBasedFeature")
        taken = topo.UpdateReferenciesFromSelection(0)
        if not taken:
            raise Refused("Flow took none of the selected faces (a face must touch the fluid).")
        if name:
            f.SetName(name)  # answers False even when the name is taken: read it back instead
        if not proj.AddTemporaryFeature(f):
            raise Refused("Flow did not add the item to the project.")
        reply = {"faces": int(taken)}
        if name and f.Name != name:
            f.SetName(name)
            if f.Name != name:
                reply["name_refused"] = f"Flow kept its own name instead of '{name}' (in use, or not allowed)."
        reply.update(name=f.Name, features=self.features(proj))
        return reply

    def set_conditions(self, request: dict) -> dict:
        proj = self.project()
        initial = proj.GlobalInitialConditions
        done = {}
        if request.get("velocity") is not None:
            for kind, value in zip((P_VX, P_VY, P_VZ), request["velocity"]):
                self.set(initial, kind, value, "the velocity")
            done["velocity_mps"] = list(request["velocity"])
        if request.get("pressure") is not None:
            self.set(initial, P_PRESSURE, request["pressure"], "the pressure")
            done["pressure_pa"] = request["pressure"]
        if request.get("temperature") is not None:
            self.set(initial, P_TEMPERATURE, request["temperature"], "the temperature")
            done["temperature_k"] = request["temperature"]
        if request.get("mesh_level") is not None:
            self.set(proj.GlobalMeshSettings, P_MESH_LEVEL, request["mesh_level"], "the mesh level", integer=True)
            done["mesh_level"] = request["mesh_level"]
        return {"project": proj.Name, "set": done}

    def solve(self, request: dict) -> dict:
        proj = self.project()
        offset = log_offset()
        if not self.doc.Rebuild(False, False, False, True, True):
            raise Refused(self.rebuild_error(proj, offset, "The project does not rebuild"))
        t0 = time.time()
        if not proj.Solve(True, True, True):
            raise Refused(self.rebuild_error(proj, offset, "Flow did not solve"))
        reply = self.goals({"service": False}, proj)
        reply["solve_s"] = round(time.time() - t0, 1)
        loaded = self.load_in_solidworks(proj)
        if loaded:
            reply["results_loaded"] = loaded
        warnings = log_warnings(offset)
        if warnings:
            reply["warnings"] = warnings
        return reply

    def load_in_solidworks(self, proj) -> str | None:
        """Load the final result file into Flow's Results in SolidWorks, so plots can be made
        there; Solve alone leaves them unloaded. The final file is the one with the most
        iterations (r_000000.fld is the initial state)."""
        files = proj.EnumFDAResultFiles()
        best = None
        f = files.Next() if files is not None else None
        while f is not None:
            f = self.wrap(f, "IFDAResultFileInfo")
            path, iterations = f.GetPathToFDAResultFile(), f.GetIterationsNumber()
            if path and (best is None or iterations > best[0]):
                best = (iterations, path)
            f = files.Next()
        if best is not None and proj.LoadResultsInCad(best[1]):
            return best[1]
        return None

    def goals(self, request: dict, proj=None) -> dict:
        proj = proj or self.project()
        results = proj.LoadResults(True)
        if results is None:
            raise Refused(f"Project '{proj.Name}' has no results yet: call flow_solve.")
        enum = self.wrap(results, "IFDAResultHandler").GetGoalsCalculationResults()
        user, service = [], []
        g = enum.Next() if enum is not None else None
        while g is not None:
            g = self.wrap(g, "IFDAGoalCalculationResults")
            item = {"name": g.GetGoalName(), "value": g.GetLastCalculatedValue(), "unit_id": g.GetUnit()}
            (service if g.IsServiceGoal() else user).append(item)
            g = enum.Next()
        reply = {"project": proj.Name, "goals": user}
        if request.get("service"):
            reply["service_goals"] = service
        return reply

    def info(self, request: dict) -> dict:
        reply = {"document": os.path.basename(self.doc.GetPathName()),
                 "projects": list(self.doc.GetProjectNames() or ())}
        proj = self.doc.GetActiveProject()
        if proj is not None:
            proj = self.wrap(proj, "IFDAProject")
            reply.update(active_project=proj.Name, features=self.features(proj))
        return reply


OPS = ("create_project", "add_boundary_condition", "add_goal", "set_conditions", "solve", "goals", "info")


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if request.get("op") not in OPS:
            raise Refused(f"Unknown Flow operation {request.get('op')!r}.")
        flow = Flow(request)
        reply = {"ok": True, **getattr(flow, request["op"])(request)}
    except Refused as exc:
        reply = {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - one readable line back to the server
        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(limit=4)}
    # ASCII escapes: the pipe's code page (cp1251 here) must not decide how names arrive
    sys.stdout.write(json.dumps(reply, default=repr) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
