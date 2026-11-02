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
