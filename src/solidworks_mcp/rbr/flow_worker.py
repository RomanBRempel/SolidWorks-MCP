"""The Flow Simulation worker: NIK API calls in a process of their own (see rbr/flow.py).

Reads one JSON request on stdin, writes one JSON line on stdout:
{"ok": true, ...} or {"ok": false, "error": "..."}. Started by flow._run_worker
as `python -m solidworks_mcp.rbr.flow_worker`; never imported by the server.

A solve writes one extra line BEFORE it starts, {"event": "started", ...}, with
the project's folder: the server reads the solver's progress from there while
this process waits inside Solve.

NIK traps, all met on Flow Simulation 2025 SP3:
- touching a computational-domain parameter was an access violation in this
  process on 2026-11-02 and works on 2026-11-20 (read and set, same build), so
  the domain is read and set in a worker call of its own: a crash there costs
  that one answer, not the rest,
- the minimum gap size is a parameter of the global mesh only in a project that
  has it switched on (Global Mesh > Minimum gap size); a project made from a
  template without it has no such parameter at all,
- a template carries the goals and boundary conditions of the project it was
  made from, bound to THAT model's faces. On another model they find nothing
  ("cannot find the bodies this item refers to") and the new project does not
  rebuild: create_project removes them and says which,
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

try:
    import winreg
except ImportError:  # not Windows: the pure parts are still importable (tests)
    winreg = None

from . import flow_case

NIK_TYPELIB = ("{CB182AE6-7258-43F3-B058-25ACFBD58535}", 0, 1, 0)
FEATURE_TYPES = {2: "boundary_condition", 3: "surface_source", 4: "volume_source", 5: "rotating_region",
                 8: "solid_material", 11: "fluid_subdomain", 12: "surface_goal", 13: "volume_goal",
                 14: "local_mesh"}
BOUNDARY_CONDITION, SURFACE_GOAL = 2, 12
P_BC_TYPE, P_MESH_LEVEL, P_GAP = 41, 7, 8
DOMAIN = {"xmin": 1, "xmax": 2, "ymin": 3, "ymax": 4, "zmin": 5, "zmax": 6}  # nikComputationalDomain*
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
            have = list(self.doc.GetProjectNames() or ())
            if have:
                raise Refused(f"No Flow project is active in the document: flow_activate one of {', '.join(have)}.")
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

    def read(self, owner, kind: int, integer: bool = False):
        """A parameter's value; None when the item has no such parameter or will not tell."""
        try:
            p = self.parameter(owner, kind)
            code, value = p.GetLongValue() if integer else p.GetValue()
        except Exception:  # noqa: BLE001 - Refused (no such parameter), or a read that raises
            return None
        return value if code == NO_ERROR else None

    def config(self):
        conf = self.doc.ActiveConfiguration
        if conf is None:
            raise Refused("Flow sees no active configuration in the document.")
        return self.wrap(conf, "IModelConfiguration")

    def names(self) -> list:
        return list(self.doc.GetProjectNames() or ())

    def named(self, name: str) -> str:
        """The project's exact name (matched without case), or a refusal listing what exists."""
        have = self.names()
        for n in have:
            if n == name:
                return n
        for n in have:
            if n.lower() == str(name).lower():
                return n
        raise Refused(f"The document has no Flow project '{name}' (it has: {', '.join(have) or 'none'}).")

    def active_name(self) -> str | None:
        active = self.doc.GetActiveProject()
        return None if active is None else self.wrap(active, "IFDAProject").Name

    def folder(self, proj) -> str | None:
        try:
            return self.wrap(proj.ProjectFiles, "IProjectFiles").ProjectDirectory or None
        except Exception:  # noqa: BLE001
            return None

    # --- whose folder (a file copy of a model shares the original's Flow folders; a synced
    #     folder is written by more than one PC) ----------------------------------------------

    def refusal(self, proj, request: dict, doing: str) -> str | None:
        """Why this project's folder is not ours to `doing` (solve into / delete), or None."""
        folder = self.folder(proj)
        reason = folder_refusal(folder, request.get("document"), request.get("computer"), proj.Name)
        if reason is None:
            return None
        return (f"Refused to {doing} project '{proj.Name}': {reason}. flow_clone the project and work on the "
                "clone (a clone gets a folder of its own), or repeat with force=True to go ahead.")

    def claim(self, proj, request: dict) -> dict | None:
        folder = self.folder(proj)
        if not folder:
            return None
        try:
            uuid = proj.UUID
        except Exception:  # noqa: BLE001
            uuid = None
        return write_claim(folder, request.get("document"), request.get("computer"), proj.Name, uuid,
                           request.get("server"))

    def make_room(self, request: dict) -> dict:
        """Before a project is made: take the folder's lock and, when this PC has a slot,
        fill the free numbers that are not its own, so Flow's next folder number is."""
        folder = os.path.dirname(request.get("document") or "")
        if not folder:
            return {"placeholders": [], "folder": ""}
        reason = flow_case.lock(folder, request.get("computer"), str(request.get("op")))
        if reason:
            raise Refused(reason + ".")
        try:
            room = flow_case.reserve(folder, flow_case.parse_slot(request.get("slot")), request.get("computer"))
        except flow_case.CaseError as exc:
            flow_case.unlock(folder, request.get("computer"))
            raise Refused(str(exc)) from None
        room["folder"] = folder
        return room

    def room_done(self, request: dict, room: dict) -> None:
        flow_case.release(room.get("placeholders"))
        if room.get("folder"):
            flow_case.unlock(room["folder"], request.get("computer"))

    def taken(self, proj, request: dict) -> str | None:
        """A project that was just made must sit in a folder nobody else calculated in."""
        folder = self.folder(proj)
        holder = flow_case.owner(folder) if folder else None
        other = read_claim(folder) if folder else None
        mine = str(request.get("computer") or "").lower()
        if holder is not None:
            return (f"Flow gave the new project the folder '{os.path.basename(folder)}', which already holds a "
                    f"calculation (project '{holder.get('project')}' of '{holder.get('model')}', made on "
                    f"{holder.get('computer')})")
        if other and str(other.get("computer") or "").lower() not in ("", mine):
            return (f"Flow gave the new project the folder '{os.path.basename(folder)}', which "
                    f"{other.get('computer')} claimed at {other.get('claimed')}")
        return None

    def rebuild_error(self, proj, offset: int, what: str) -> str:
        reasons = [r for r in [proj.GetLastRebuildError()] + log_warnings(offset) if r]
        return f"{what}: " + ("; ".join(reasons) if reasons else "Flow gave no reason (see its Messages pane).")

    # --- operations -----------------------------------------------------------------

    def face_bound(self, proj) -> list:
        """[(IFeature, reference names)] of the project's items that sit on geometry."""
        out = []
        e = proj.EnumFeatures()
        f = e.Next() if e is not None else None
        while f is not None:
            f = self.wrap(f, "IFeature")
            try:
                topo = f.GetInterface("ITopologyBasedFeature")
            except Exception:  # noqa: BLE001
                topo = None
            if topo is not None:
                try:
                    names = list(self.wrap(topo, "ITopologyBasedFeature").GetReferencesNames() or ())
                except Exception:  # noqa: BLE001 - an item whose references cannot even be read
                    names = []
                out.append((f, [str(n) for n in names]))
            f = e.Next()
        return out

    def drop_unbound(self, proj) -> list:
        """Delete the items a template brought along that hold no geometry of THIS model -
        or, when every one of them claims references and the project still does not
        rebuild, all of them: they came with the template, not from this model."""
        items = self.face_bound(proj)
        loose = [(f, names) for f, names in items if not names] or items
        dropped = []
        for f, names in loose:
            entry = {"name": f.Name, "type": FEATURE_TYPES.get(f.Type, f"type {f.Type}")}
            try:
                f.Delete()
            except Exception as exc:  # noqa: BLE001
                entry["not_removed"] = str(exc)
            dropped.append(entry)
        left = {x["name"] for x in self.features(proj)}
        for entry in dropped:
            if entry["name"] in left and "not_removed" not in entry:
                entry["not_removed"] = "Flow kept it"
        return dropped

    def discard(self, name: str, activate: str | None) -> bool:
        """Remove a project that could not be finished and put the earlier one back; True when gone."""
        try:
            self.config().RemoveProject(name)
        except Exception:  # noqa: BLE001
            pass
        gone = name not in self.names()
        try:
            if activate and activate in self.names() and self.active_name() != activate:
                self.config().ActivateProject(activate, False)
        except Exception:  # noqa: BLE001
            pass
        return gone

    def create_project(self, request: dict) -> dict:
        offset = log_offset()
        if request["name"] in (self.doc.GetProjectNames() or ()):
            raise Refused(f"The document already has a Flow project '{request['name']}'.")
        active_before = self.active_name()
        room = self.make_room(request)
        try:
            proj = self.doc.CreateProjectFromTemplate(request["template"], request["name"])
        finally:
            self.room_done(request, room)
        if proj is None:
            raise Refused(self.rebuild_error(self.project(), offset, "Flow did not create the project")
                          if self.doc.GetActiveProject() is not None else
                          f"Flow did not create the project from '{request['template']}'.")
        proj = self.wrap(proj, "IFDAProject")
        rebuilt = self.doc.Rebuild(True, True, False, True, True)  # reset the template's domain to this model
        dropped = []
        if not rebuilt:
            # the template's own goals and conditions sit on faces of the model it was made from
            first_reason = self.rebuild_error(proj, offset, "The new project does not rebuild")
            dropped = self.drop_unbound(proj)
            if dropped:
                offset = log_offset()
                rebuilt = self.doc.Rebuild(True, True, False, True, True)
        if not rebuilt:
            reason = self.rebuild_error(proj, offset, "The new project does not rebuild") if dropped else first_reason
            gone = self.discard(request["name"], active_before)
            raise Refused(reason + (f" Removed from it first, to no avail: {[d['name'] for d in dropped]}." if dropped
                                    else "")
                          + (" The project was not kept; the document is as it was." if gone else
                             f" The half-made project '{request['name']}' could not be removed: flow_delete it."))
        taken = self.taken(proj, request)
        if taken:
            raise Refused(f"{taken}: another PC or a copy of this model works in the same folder. The project "
                          f"'{request['name']}' was left in the document unsolved - flow_delete(name, force=True) "
                          "it only if that folder's results are not needed.")
        self.claim(proj, request)
        reply = {"project": proj.Name, "template": request["template"], "directory": self.folder(proj),
                 "features": self.features(proj)}
        if dropped:
            reply["template_items_removed"] = dropped
            reply["note"] = ("The template brought goals / conditions bound to the model it was made from; they "
                             "found no faces here and were removed. Add this model's own with flow_add_goal / "
                             "flow_add_boundary_condition.")
        if request.get("mesh_level") is not None:
            self.set(proj.GlobalMeshSettings, P_MESH_LEVEL, request["mesh_level"], "the mesh level", integer=True)
            reply["mesh_level"] = request["mesh_level"]
        return reply

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
        if request.get("min_gap") is not None:
            try:
                self.set(proj.GlobalMeshSettings, P_GAP, request["min_gap"], "the minimum gap size")
            except Refused as exc:
                if "no parameter" not in str(exc):
                    raise
                raise Refused(
                    f"Project '{proj.Name}' has no minimum gap size to set: it is switched off in the project "
                    "(it came from a template without it), and the Flow API cannot switch it on. Turn it on "
                    "once in Flow (Global Mesh > Minimum gap size) and save that project as a template "
                    "(Flow Simulation > Project > Create Template) for flow_create_project, or flow_clone a "
                    "project that has it." + (f" Already set: {done}." if done else "")) from None
            done["min_gap_mm"] = request["min_gap"] * 1000.0
        return {"project": proj.Name, "set": done}

    def set_domain(self, request: dict) -> dict:
        """The computational domain's faces, metres; in a call of its own (see the module text)."""
        proj = self.project()
        settings = proj.ComputationalDomainSettings
        for key, value in request["domain"].items():
            self.set(settings, DOMAIN[key], value, f"the domain's {key}")
        return {"project": proj.Name, "domain_m": self.domain(proj)}

    def domain(self, proj) -> dict:
        settings = proj.ComputationalDomainSettings
        return {key: self.read(settings, kind) for key, kind in DOMAIN.items()}

    def settings(self, request: dict) -> dict:
        """Mesh level, minimum gap and domain of the active project (read-only)."""
        proj = self.project()
        mesh = proj.GlobalMeshSettings
        gap = self.read(mesh, P_GAP)
        try:
            uuid = proj.UUID
        except Exception:  # noqa: BLE001
            uuid = None
        return {"project": proj.Name, "project_uuid": uuid, "directory": self.folder(proj),
                "mesh_level": self.read(mesh, P_MESH_LEVEL, integer=True),
                "min_gap_mm": None if gap is None else gap * 1000.0, "domain_m": self.domain(proj)}

    def prepare(self, request: dict) -> dict:
        """Write the solver's settings file from the project as it stands, without meshing
        or solving: what flow_mesh_get and the check against a case read."""
        proj = self.project()
        refusal = self.refusal(proj, request, "prepare")
        if refusal and not request.get("force"):
            raise Refused(refusal)
        offset = log_offset()
        if request.get("rebuild", True) and not self.doc.Rebuild(False, False, False, True, True):
            raise Refused(self.rebuild_error(proj, offset, "The project does not rebuild"))
        proj.UpdateConfigAndDataFiles()
        folder = self.folder(proj)
        return {"project": proj.Name, "directory": folder, "config": flow_case.config_path(folder)}

    # --- projects ---------------------------------------------------------------------

    def activate(self, request: dict) -> dict:
        name = self.named(request["name"])
        self.config().ActivateProject(name, False)
        if self.active_name() != name:
            raise Refused(f"Flow did not activate project '{name}'.")
        return {"project": name, "projects": self.names(), "features": self.features(self.project())}

    def clone(self, request: dict) -> dict:
        source, new = self.named(request["name"]), request["new_name"]
        if any(n.lower() == new.lower() for n in self.names()):
            raise Refused(f"The document already has a Flow project '{new}'.")
        offset = log_offset()
        room = self.make_room(request)
        try:
            self.config().CloneProject(source, new, bool(request.get("copy_results")))
        finally:
            self.room_done(request, room)
        if new not in self.names():
            reasons = "; ".join(log_warnings(offset))
            raise Refused(f"Flow did not clone '{source}' to '{new}'." + (f" {reasons}" if reasons else ""))
        if self.active_name() != new:
            self.config().ActivateProject(new, False)
        if self.active_name() != new:
            raise Refused(f"'{new}' was cloned from '{source}' but did not become the active project.")
        proj = self.project()
        taken = self.taken(proj, request)
        if taken:
            raise Refused(f"{taken}: another PC or a copy of this model works in the same folder. The clone "
                          f"'{new}' was left in the document unsolved - flow_delete(name, force=True) it only "
                          "if that folder's results are not needed, or clone again once the folders are synced.")
        self.claim(proj, request)
        return {"project": new, "cloned_from": source, "projects": self.names(), "directory": self.folder(proj),
                "features": self.features(proj)}

    def delete(self, request: dict) -> dict:
        name = self.named(request["name"])
        if not request.get("force"):
            # the folder is only known of the active project: look, and put the earlier one back
            before = self.active_name()
            if before != name:
                self.config().ActivateProject(name, False)
            refusal = self.refusal(self.project(), request, "delete") if self.active_name() == name else None
            if refusal:
                if before and before != name:
                    self.config().ActivateProject(before, False)
                raise Refused(refusal + " Deleting a project deletes its folder.")
        removed = self.config().RemoveProject(name)
        if not removed or name in self.names():
            raise Refused(f"Flow did not delete project '{name}'.")
        left = self.names()
        if left and self.active_name() is None:
            # deleting the active project leaves none active, and every Flow tool works on
            # the active one: the first that is left takes over
            self.config().ActivateProject(left[0], False)
        return {"deleted": name, "projects": left, "active_project": self.active_name()}

    def solve(self, request: dict) -> dict:
        proj = self.project()
        refusal = self.refusal(proj, request, "solve")
        if refusal and not request.get("force"):
            raise Refused(refusal + " Solving overwrites the results in the folder.")
        offset = log_offset()
        if not self.doc.Rebuild(False, False, False, True, True):
            raise Refused(self.rebuild_error(proj, offset, "The project does not rebuild"))
        self.claim(proj, request)
        if request.get("case_path"):
            self.hold_to_case(proj, request)
        emit({"event": "started", "project": proj.Name, "directory": self.folder(proj)})
        t0 = time.time()
        if request.get("cpus"):
            params = self.wrap(proj.CreateSolveParameters(), "ISolveParameters")
            params.BuildMesh, params.Recalculate, params.CloseMonitor = True, True, True
            params.cpus = int(request["cpus"])
            solved = proj.Solve3(params)
        else:
            solved = proj.Solve(True, True, True)
        if not solved:
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

    def hold_to_case(self, proj, request: dict) -> None:
        """Before any meshing: the settings Flow would hand the solver, against the case.
        A difference refuses the solve and names itself - a run that is not comparable is
        not worth its hour."""
        case = flow_case.load_case(request["case_path"])
        proj.UpdateConfigAndDataFiles()
        path = flow_case.config_path(self.folder(proj))
        if not path:
            raise Refused("Flow wrote no solver settings file for the project, so it cannot be held against "
                          "the case.")
        found = flow_case.check_case(flow_case.read_config(path), case, "all")
        if found and not request.get("force_case"):
            shown = "; ".join(f"{d['key']}: {d['have']} (case: {d['want']})" for d in found[:12])
            raise Refused(f"Project '{proj.Name}' is not the case '{case.get('case')}' - {len(found)} setting(s) "
                          f"differ: {shown}{' ...' if len(found) > 12 else ''}. Nothing was meshed or solved. "
                          "flow_mesh_get shows the mesh; make the project from the case (flow_create_from_case).")

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
            folder = self.folder(proj)
            if folder:
                reply["directory"] = folder
            last = self.last_results(proj)
            if last:
                reply["last_results"] = last
        return reply

    def last_results(self, proj) -> dict | None:
        """Cells and iterations of the newest result file (None before the first solve)."""
        try:
            files = proj.EnumFDAResultFiles()
            best = None
            f = files.Next() if files is not None else None
            while f is not None:
                f = self.wrap(f, "IFDAResultFileInfo")
                iterations = f.GetIterationsNumber()
                if best is None or iterations > best["iterations"]:
                    best = {"iterations": iterations, "cells": f.GetMeshCellsCount(),
                            "file": f.GetPathToFDAResultFile()}
                f = files.Next()
            return best
        except Exception:  # noqa: BLE001 - a courtesy: the project's own data is the answer
            return None


OPS = ("create_project", "add_boundary_condition", "add_goal", "set_conditions", "set_domain", "settings",
       "activate", "clone", "delete", "solve", "goals", "info", "prepare")

CLAIM_FILE = "mcp_owner.json"


def read_claim(folder: str | None) -> dict | None:
    """Who made the project of a folder (written by this server at create / clone / solve)."""
    try:
        with open(os.path.join(folder or "", CLAIM_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_claim(folder: str, document, computer, project, uuid=None, server=None) -> dict | None:
    claim = {"computer": computer, "model": os.path.basename(str(document or "")), "project": project,
             "project_uuid": uuid, "claimed": time.strftime("%Y-%m-%d %H:%M:%S"), "server": server}
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, CLAIM_FILE), "w", encoding="utf-8") as fh:
            json.dump(claim, fh, ensure_ascii=False, indent=1)
    except OSError:
        return None
    return claim


def folder_refusal(folder, document, computer, project) -> str | None:
    """Why a project folder is not this document's on this PC: the solver's own files say so
    (another model, another project, another computer), or another PC's claim does."""
    if not folder:
        return None
    reason = flow_case.foreign(flow_case.owner(folder), document, computer, project)
    if reason:
        return reason
    claim = read_claim(folder)
    if claim and computer and claim.get("computer") and str(claim["computer"]).lower() != str(computer).lower():
        return f"the folder was claimed by another computer, {claim.get('computer')}, at {claim.get('claimed')}"
    return None


def emit(line: dict) -> None:
    """One JSON line to the server, at once. ASCII escapes: the pipe's code page (cp1251
    here) must not decide how names arrive."""
    sys.stdout.write(json.dumps(line, default=repr) + "\n")
    sys.stdout.flush()


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
    emit(reply)


if __name__ == "__main__":
    main()
