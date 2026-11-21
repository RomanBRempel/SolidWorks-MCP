"""Flow Simulation tools, the parts that need no SolidWorks: names to NIK numbers, templates,
the worker's reply (and its crash), units, and a Flow solve run as a background job."""

import concurrent.futures
import json
import time

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import flow, flow_worker, jobs


def test_boundary_conditions_carry_their_value_parameter():
    assert flow.boundary_condition("inlet_velocity") == (2, 19)
    assert flow.boundary_condition(" Environment_Pressure ") == (11, 9)
    assert flow.boundary_condition("real_wall") == (12, None)


def test_an_unknown_kind_names_the_choices():
    with pytest.raises(SolidWorksError, match="Unknown boundary condition 'inlet'.*inlet_velocity"):
        flow.boundary_condition("inlet")
    with pytest.raises(SolidWorksError, match="Unknown goal value 'mean'.*average"):
        flow.goal("force_x", "mean")


def test_goals_map_to_nik_parameters():
    assert flow.goal("static_pressure", "max") == (3, 1)
    assert flow.goal("temperature") == (4, 2)


def test_an_integral_goal_takes_no_value_to_calculate():
    assert flow.goal("force_x") == (10, None)
    assert flow.goal("volume_flow") == (17, None)
    with pytest.raises(SolidWorksError, match="volume_flow goal is an integral"):
        flow.goal("volume_flow", "max")


def test_template_by_name_or_path(tmp_path):
    folder = tmp_path / "Flow Simulation 2025" / "template"
    folder.mkdir(parents=True)
    (folder / "internal_water.fwp").write_bytes(b"x")
    (folder / "External_Air.fwp").write_bytes(b"x")
    assert flow.template_path("external_air", search=(str(tmp_path),)).endswith("External_Air.fwp")
    assert flow.template_path(str(folder / "internal_water.fwp")) == str(folder / "internal_water.fwp")
    with pytest.raises(SolidWorksError, match=r"No Flow template 'air' \(found: External_Air, internal_water\)"):
        flow.template_path("air", search=(str(tmp_path),))


def test_the_worker_reply_is_its_last_json_line():
    out = "noise from a library\n" + json.dumps({"ok": True, "project": "p"}) + "\n"
    assert flow.worker_answer(0, out, "") == {"ok": True, "project": "p"}


def test_a_worker_that_died_says_so_and_spares_solidworks():
    with pytest.raises(SolidWorksError, match="access violation inside the Flow API.*SolidWorks itself is not affected"):
        flow.worker_answer(-1073741819, "", "Windows fatal exception: access violation")
    with pytest.raises(SolidWorksError, match="exit code 3"):
        flow.worker_answer(3, "", "")


def test_goal_units_are_named():
    reply = flow._with_units({"goals": [{"name": "F", "value": 1.0, "unit_id": 7}],
                              "service_goals": [{"name": "s", "value": 0.0, "unit_id": 999}]})
    assert reply["goals"][0] == {"name": "F", "value": 1.0, "unit": "N"}
    assert reply["service_goals"][0]["unit"] == "unit 999"


def test_flow_log_warnings_since_an_offset(tmp_path, monkeypatch):
    log = tmp_path / "ngp.log"
    log.write_text("warning 07:00 [FW03.dll] old reason\n", encoding="utf-8")
    monkeypatch.setattr(flow_worker, "LOG", str(log))
    offset = flow_worker.log_offset()
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("07:01 [vwr09inc.dll] loaded\nwarning 07:02 [FW03.dll] No pressure condition.\n"
                 "warning 07:03 [FW03.dll] No pressure condition.\n")
    assert flow_worker.log_warnings(offset) == ["No pressure condition."]


class _Worker:
    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def submit(self, fn):
        return self.pool.submit(fn)


class _Session:
    def run_guarded(self, fn, *args):
        return fn(*args)


def test_a_function_runs_as_a_job(swmcp_home):
    worker = _Worker()
    try:
        job = jobs.submit(worker, _Session(), None, None, None, "flow_solve",
                          call=lambda session: {"ok": True, "goals": [{"name": "p", "value": 2.0}]})
        deadline = time.time() + 5
        while jobs.get(job["job_id"])["state"] != "done" and time.time() < deadline:
            time.sleep(0.01)
        answer = jobs.answer(jobs.get(job["job_id"]))
        assert answer["goals"] == [{"name": "p", "value": 2.0}] and answer["job_id"] == job["job_id"]
    finally:
        worker.pool.shutdown(wait=True)


# --- 1.12: domain, gap, mesh level, progress, status ------------------------------------------------


def test_domain_as_axes_or_as_six_numbers():
    assert flow.domain_values(None) == {}
    assert flow.domain_values({"x": [-1.375, 1.375], "Y": [-0.85, 1.05]}) == {
        "xmin": -1.375, "xmax": 1.375, "ymin": -0.85, "ymax": 1.05}
    assert flow.domain_values([-1.375, 1.375, -0.85, 1.05, -2.155, 0.833]) == {
        "xmin": -1.375, "xmax": 1.375, "ymin": -0.85, "ymax": 1.05, "zmin": -2.155, "zmax": 0.833}
    assert flow.domain_values([None, 0.3, None, None, None, None]) == {"xmax": 0.3}     # null keeps a side
    assert flow.domain_values({"zmin": -2, "x": [None, 1]}) == {"zmin": -2.0, "xmax": 1.0}


@pytest.mark.parametrize("bad", [[1, 2, 3], {"w": [0, 1]}, {"x": 5}, "big", {"x": ["a", 1]}, {}, [None] * 6])
def test_a_domain_of_an_unknown_shape_is_refused_with_the_format(bad):
    with pytest.raises(SolidWorksError, match="domain_m is the computational domain"):
        flow.domain_values(bad)


def test_a_domain_side_pair_must_be_min_below_max():
    with pytest.raises(SolidWorksError, match="xmin .* must be below xmax"):
        flow.domain_values({"x": [0.5, 0.1]})
    with pytest.raises(SolidWorksError, match="zmin"):
        flow.domain_values([0, 1, 0, 1, 2, 2])


def test_the_gap_goes_to_flow_in_metres():
    assert flow.min_gap(None) is None and flow.min_gap(10) == pytest.approx(0.01)
    for bad in (0, -1, "thin"):
        with pytest.raises(SolidWorksError, match="min_gap_mm"):
            flow.min_gap(bad)


def test_the_mesh_level_is_a_whole_number_1_to_7():
    assert flow._mesh_level(None) is None and flow._mesh_level(5) == 5 and flow._mesh_level(7.0) == 7
    for bad in (0, 8, 4.5, "5"):
        with pytest.raises(SolidWorksError, match="mesh_level"):
            flow._mesh_level(bad)
    assert flow.DEFAULT_MESH_LEVEL == 5


INFO = {"model_name": "cube.SLDPRT", "project_name": "P2", "finished": False,
        "mesh": {"cells_total": 89216, "cells_fluid": 89216},
        "telemetry": {"iteration": 52, "travel": 0.58, "iterations_per_travel": 89, "cpu_time_solver_total": 40,
                      "cpu_time_left": 61, "last_iteration_finished": "18:18:10+0200"},
        "goals": [{"goal": {"name": "FX", "unit": "force", "value": 0.175, "progress": 0.0, "criteria": 0.0,
                            "delta": 0.068, "use_in_convergence": True}},
                  {"goal": {"name": "FY", "unit": "force", "value": 1.0, "progress": 40.0, "criteria": 0.1,
                            "delta": 0.2, "use_in_convergence": True}},
                  {"goal": {"name": "T", "unit": "temperature", "value": 293.2, "progress": 100.0,
                            "use_in_convergence": False}}]}


def test_progress_from_the_solvers_info_file():
    p = flow.progress_of(INFO)
    assert p["phase"] == "solving" and p["iteration"] == 52 and p["cells"] == 89216 and p["travels"] == 0.58
    assert p["percent"] == 0.0                      # the least-converged goal that counts
    assert p["goals"][0] == {"name": "FX", "value": 0.175, "quantity": "force", "progress_pct": 0.0,
                             "delta": 0.068, "criteria": 0.0, "in_convergence": True}
    assert p["cpu_time_left_s"] == 61 and "finished_at" not in p


def test_progress_phases():
    assert flow.progress_of({})["phase"] == "meshing"
    assert flow.progress_of({"mesh": {"cells_total": 10}, "telemetry": {"iteration": 0}})["phase"] == "meshed"
    done = flow.progress_of({**INFO, "finished": True})
    assert done["phase"] == "finished" and done["percent"] == 100.0 and done["finished_at"] == "18:18:10+0200"
    nothing_judged = flow.progress_of({"telemetry": {"iteration": 3}, "goals": []})
    assert nothing_judged["phase"] == "solving" and nothing_judged["percent"] is None


def test_read_progress_takes_the_folders_info_file(tmp_path):
    assert flow.read_progress(None) is None and flow.read_progress(str(tmp_path)) is None
    (tmp_path / "7.info.json").write_text(json.dumps(INFO), encoding="utf-8")
    got = flow.read_progress(str(tmp_path))
    assert got["iteration"] == 52 and got["info_age_s"] < 60
    (tmp_path / "7.info.json").write_text("{half", encoding="utf-8")      # caught mid-write
    assert flow.read_progress(str(tmp_path)) is None


def test_status_while_running_done_failed_and_lost():
    record = {"job_id": "j1", "state": "running", "document": "cube.SLDPRT", "project": "P2",
              "submitted": "2026-11-20T18:17:21.478", "directory": "D:/1"}
    running = flow.status_of(record, flow.progress_of(INFO), True, True)
    assert running["state"] == "running" and running["phase"] == "solving" and running["iteration"] == 52
    assert running["project"] == "P2" and running["directory"] == "D:/1"
    # a progress file older than this job belongs to the run before: nothing is told from it
    assert flow.status_of(record, flow.progress_of(INFO), True, False)["phase"] == "preparing"
    assert flow.status_of(record, None, True, False)["phase"] == "preparing"
    lost = flow.status_of(record, flow.progress_of({**INFO, "finished": True}), False, True)
    assert lost["state"] == "lost" and lost["phase"] == "finished" and "flow_goals" in lost["note"]
    done = flow.status_of({**record, "state": "done", "elapsed_s": 107.1,
                           "result": {"ok": True, "goals": [{"name": "FX", "value": 0.2, "unit": "N"}]}},
                          flow.progress_of({**INFO, "finished": True}), True, True)
    assert done["ok"] and done["result"] == {"goals": [{"name": "FX", "value": 0.2, "unit": "N"}]}
    assert done["elapsed_s"] == 107.1 and done["percent"] == 100.0
    failed = flow.status_of({**record, "state": "failed", "error": "Flow did not solve"}, None, True, False)
    assert failed["ok"] is False and failed["error"] == "Flow did not solve"


def test_flow_status_reads_the_job_and_the_folder(swmcp_home, tmp_path):
    import threading
    folder = tmp_path / "proj"
    folder.mkdir()
    gate = threading.Event()

    def run(job_id):
        jobs.update(job_id, directory=str(folder), project="P2")
        gate.wait(5)
        return {"ok": True, "goals": []}

    job = jobs.submit_thread("flow_solve", run, {"document": "cube.SLDPRT"})
    try:
        deadline = time.time() + 5
        while "directory" not in jobs.get(job["job_id"]) and time.time() < deadline:
            time.sleep(0.01)
        assert flow.flow_status(job["job_id"])["phase"] == "preparing"
        (folder / "1.info.json").write_text(json.dumps(INFO), encoding="utf-8")
        status = flow.flow_status()                                   # no id: the newest solve
        assert status["job_id"] == job["job_id"] and status["iteration"] == 52 and status["state"] == "running"
        assert [s["job_id"] for s in flow.running_solves()] == [job["job_id"]]
    finally:
        gate.set()
    with pytest.raises(SolidWorksError, match="No job"):
        flow.flow_status("nope")


def test_flow_status_refuses_a_job_of_another_tool(swmcp_home):
    other = jobs.submit_thread("something_else", lambda job_id: {"ok": True})
    with pytest.raises(SolidWorksError, match="not a flow_solve"):
        flow.flow_status(other["job_id"])


def test_silent_mode_is_counted_so_a_solve_keeps_it_through_other_calls(monkeypatch):
    import sys
    import types
    calls = []

    class _Api:
        def SetSilent(self):
            calls.append("set")

        def ResetSilent(self):
            calls.append("reset")

    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda _obj: type("AddIn", (), {"GetAPI": lambda self: _Api()})()
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(sys.modules["win32com"], "client", client, raising=False)
    session = type("S", (), {"_sw": type("Sw", (), {"GetAddInObject": lambda self, name: object()})()})()
    monkeypatch.setattr(flow, "_silent_holds", 0)
    flow.silent_on(session)            # the solve
    flow.silent_on(session)            # a flow_info meanwhile
    flow.silent_off(session)
    assert calls == ["set", "set"]     # still silent: the solve holds it
    flow.silent_off(session)
    assert calls == ["set", "set", "reset"]
    flow.silent_off(session)           # one release too many never goes negative
    assert flow._silent_holds == 0


def test_the_worker_knows_the_new_operations():
    for op in ("set_domain", "settings", "activate", "clone", "delete"):
        assert op in flow_worker.OPS and callable(getattr(flow_worker.Flow, op))
    assert flow_worker.DOMAIN == {"xmin": 1, "xmax": 2, "ymin": 3, "ymax": 4, "zmin": 5, "zmax": 6}
    assert flow_worker.P_GAP == 8 and flow_worker.P_MESH_LEVEL == 7


# --- 1.12.1: a template that brings another model's goals; a project that cannot be finished ------


class _Item:
    """A Flow project item: `references` None = it sits on no geometry at all (not face-bound)."""

    def __init__(self, project, name, kind=12, references=()):
        self.project, self.Name, self.Type, self.references = project, name, kind, references

    def GetInterface(self, name):
        return self if name == "ITopologyBasedFeature" and self.references is not None else None

    def GetReferencesNames(self):
        return tuple(self.references)

    def Delete(self):
        self.project.items.remove(self)


class _Enum:
    def __init__(self, items):
        self.items = list(items)

    def Next(self):
        return self.items.pop(0) if self.items else None


class _Project:
    def __init__(self, name):
        self.Name, self.items = name, []
        self.GlobalMeshSettings = object()

    def EnumFeatures(self):
        return _Enum(self.items) if self.items else None

    def GetLastRebuildError(self):
        return "Cannot find the bodies this item refers to." if any(i.references == () for i in self.items) else ""


class _Config:
    def __init__(self, doc):
        self.doc = doc

    def RemoveProject(self, name):
        self.doc.projects = [p for p in self.doc.projects if p.Name != name]
        if self.doc.active is not None and self.doc.active.Name == name:
            self.doc.active = None
        return True

    def ActivateProject(self, name, _window):
        self.doc.active = next(p for p in self.doc.projects if p.Name == name)
        return self.doc.active


class _FlowDoc:
    """Rebuilds only while no item is left without references - like Flow with foreign goals."""

    def __init__(self, template_items=(), never_rebuilds=False):
        self.projects, self.template_items, self.never_rebuilds = [_Project("P1")], template_items, never_rebuilds
        self.active = self.projects[0]
        self.ActiveConfiguration = _Config(self)

    def GetProjectNames(self):
        return tuple(p.Name for p in self.projects)

    def GetActiveProject(self):
        return self.active

    def CreateProjectFromTemplate(self, _template, name):
        project = _Project(name)
        project.items = [_Item(project, n, kind, refs) for n, kind, refs in self.template_items]
        self.projects.append(project)
        self.active = project
        return project

    def Rebuild(self, *_flags):
        return not self.never_rebuilds and all(i.references != () for i in self.active.items)


def _worker(doc):
    worker = object.__new__(flow_worker.Flow)
    worker.doc = doc
    worker.wrap = lambda obj, _iface: obj
    worker.set = lambda owner, kind, value, label, integer=False: None
    return worker


def test_a_template_without_items_makes_the_project_as_before():
    doc = _FlowDoc()
    reply = _worker(doc).create_project({"name": "T", "template": "air.fwp", "mesh_level": 5})
    assert reply["project"] == "T" and reply["features"] == [] and reply["mesh_level"] == 5
    assert "template_items_removed" not in reply and doc.active.Name == "T"


def test_goals_a_template_brought_from_another_model_are_removed_and_named():
    doc = _FlowDoc([("FY_all", 12, ()), ("FZ_all", 12, ()), ("Inlet", 2, ())])
    reply = _worker(doc).create_project({"name": "T", "template": "air_gap.fwp"})
    assert reply["template_items_removed"] == [{"name": "FY_all", "type": "surface_goal"},
                                               {"name": "FZ_all", "type": "surface_goal"},
                                               {"name": "Inlet", "type": "boundary_condition"}]
    assert reply["features"] == [] and "flow_add_goal" in reply["note"] and doc.active.Name == "T"


def test_only_the_items_that_hold_nothing_go_when_others_are_bound():
    doc = _FlowDoc([("FY_all", 12, ()), ("Wall", 2, ("Face<1>",)), ("Global", 99, None)])
    reply = _worker(doc).create_project({"name": "T", "template": "air_gap.fwp"})
    assert [d["name"] for d in reply["template_items_removed"]] == ["FY_all"]
    assert [f["name"] for f in reply["features"]] == ["Wall", "Global"]   # a bound one and one that is not face-bound


def test_a_project_that_cannot_be_finished_is_not_left_in_the_document():
    doc = _FlowDoc(never_rebuilds=True)
    with pytest.raises(flow_worker.Refused, match="does not rebuild.*not kept; the document is as it was"):
        _worker(doc).create_project({"name": "T", "template": "internal_water.fwp"})
    assert doc.GetProjectNames() == ("P1",) and doc.active.Name == "P1"      # the earlier project is active again


def test_removing_the_templates_items_to_no_avail_says_so_and_discards():
    doc = _FlowDoc([("FY_all", 12, ())], never_rebuilds=True)
    with pytest.raises(flow_worker.Refused, match=r"Removed from it first, to no avail: \['FY_all'\].*not kept"):
        _worker(doc).create_project({"name": "T", "template": "air_gap.fwp"})
    assert doc.GetProjectNames() == ("P1",) and doc.active.Name == "P1"


def test_an_existing_project_name_is_refused_before_anything_is_made():
    doc = _FlowDoc()
    with pytest.raises(flow_worker.Refused, match="already has a Flow project 'P1'"):
        _worker(doc).create_project({"name": "P1", "template": "air.fwp"})
    assert doc.GetProjectNames() == ("P1",)
