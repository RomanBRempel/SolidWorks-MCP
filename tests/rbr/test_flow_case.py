"""Pure tests of rbr/flow_case.py: the solver settings file, cases, fingerprints, results, the registry.

Everything is synthetic: a settings file written by the test, shaped like the real one
(value attributes, index attributes, size lists, a raw '<' inside a value).
"""

import csv
import json
import os

import pytest

from solidworks_mcp.rbr import flow_case as fc


def config(project="P1", uuid="11111111-aaaa", model="box.SLDPRT", computer="PC-A", velocity=(0, 1.5, -20.0),
           ratio_x="2.5 1 1 0.4 1", cells_x="10 20 20 12 1", nx=62, thin=2, gap="0.01", extra=""):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<settings product="FLOEFD" version ="1">
<CADVersionInfo value="CAD 2025 SP3.0"/>
<JobOwnerName value="{computer}\\someone"/>
<MinimalGapSize value="{gap}"/>
<MinimalWallThickness value="{gap}"/>
<PostConfigName value="Default"/>
<PostModelName value="{model}"/>
<ProjectName value="{project}"/>
<ProjectUUID value="{uuid}"/>
<ResultResolution value="4"/>
<WarningCommentFormat32 value="P>Pmax, T<Tmin"/>
<ComputationalDomainSize size="6"> -1.5 -0.9 -2 1.5 1.1 0.8 </ComputationalDomainSize>
<AmbientParameter index="0">
<GUID value="AAAA-{uuid}"/>
<ExcelParameterCollection index="0">
<ExcelParameter index="0">
<ExcelParameterType value="0"/>
<ExcelParameterValue value="101325"/>
</ExcelParameter>
<ExcelParameter index="1">
<ExcelParameterType value="100"/>
<ExcelParameterValue value="{velocity[0]}"/>
</ExcelParameter>
<ExcelParameter index="2">
<ExcelParameterType value="101"/>
<ExcelParameterValue value="{velocity[1]}"/>
</ExcelParameter>
<ExcelParameter index="3">
<ExcelParameterType value="102"/>
<ExcelParameterValue value="{velocity[2]}"/>
</ExcelParameter>
</ExcelParameterCollection>
</AmbientParameter>
<BaseMeshSettings index="0">
<CaptureLevel value="3"/>
<CellPerGap value="10"/>
<CurvatureRefinementCriteria value="0.30000000000000004"/>
<EnableThinChannelRefinement value="1"/>
<MaxThinChannelRefinementLevel value="{thin}"/>
<Nx value="{nx}"/>
<Ny value="40"/>
<Nz value="70"/>
<ToleranceCriteriaRefinementLevel value="4"/>
<ToleranceCriteriaValue value="9.3e-05"/>
<ControlPoints index="0">
<Manual size="5"> 0 0 0 0 0 </Manual>
<NumOfCells size="5"> {cells_x} </NumOfCells>
<Points size="5"> -1.5 -0.6 0 0.6 1.5 </Points>
<Ratio size="5"> {ratio_x} </Ratio>
</ControlPoints>
<ControlPoints index="1">
<NumOfCells size="3"> 20 20 1 </NumOfCells>
<Points size="3"> -0.9 0 1.1 </Points>
<Ratio size="3"> 1 1 1 </Ratio>
</ControlPoints>
<ControlPoints index="2">
<NumOfCells size="3"> 35 35 1 </NumOfCells>
<Points size="3"> -2 -0.5 0.8 </Points>
<Ratio size="3"> 1 1 1 </Ratio>
</ControlPoints>
</BaseMeshSettings>
<ConvergenceOptions index="0">
<RefMaxCells value="13750000"/>
<GoalsInfo index="0">
<GoalInfo index="0">
<CriteriaPercentage value="3"/>
<GUID value="goal-{uuid}"/>
</GoalInfo>
</GoalsInfo>
</ConvergenceOptions>
<Features_Surface_Goal index="0">
<Surface_Goal index="0">
<GUID value="goal-{uuid}"/>
<Goal_Type value="9"/>
<Goal_Use_In_Convergence value="1"/>
<Name value="F1"/>
<Faces_Keys size="3"> 7 8 9 </Faces_Keys>
</Surface_Goal>
</Features_Surface_Goal>
{extra}
</settings>
'''


def folder_with(tmp_path, name="7", text=None, info=None, goals=None):
    folder = tmp_path / name
    folder.mkdir()
    (folder / f"{name}.xmlconfig").write_text(text or config(), encoding="utf-8")
    if info is not None:
        (folder / f"{name}.info.json").write_text(json.dumps(info), encoding="utf-8")
    if goals:
        (folder / "Goals.DAT").mkdir()
        for goal, rows in goals.items():
            head = "Iteration\tCPUTime\tPhysTime\tTravels\tValue\tAvValue\tMinValue\tMaxValue\tDelta\tCriteria\n"
            body = "".join("\t".join(str(v) for v in row) + "\n" for row in rows)
            (folder / "Goals.DAT" / f"{goal}.txt").write_text(head + body, encoding="utf-8")
    return str(folder)


INFO = {"model_name": "box.SLDPRT", "project_name": "P1", "finished": True,
        "mesh": {"cells_total": 12345, "cells_fluid": 12000},
        "telemetry": {"iteration": 3, "travel": 1.01, "cpu_time_solver_total": 9},
        "system_info": {"computer_name": "PC-A", "number_of_cores": 8, "product_name": "Flow X / SOLVER y"}}
GOALS = {"F1": [(1, 1, 0, 0.3, 50.0, 50.0, 50.0, 50.0, 0, 0), (2, 2, 0, 0.6, 21.0, 35.5, 21.0, 50.0, 29, 0),
                (3, 3, 0, 1.01, 21.4, 21.2, 21.0, 21.4, 0.4, 1.2)],
         "Serv Press": [(1, 1, 0, 0.3, 1.0, 1.0, 1.0, 1.0, 0, 0)]}


# --- the settings file --------------------------------------------------------------------------------


def test_parse_reads_values_indexes_lists_and_raw_angle_brackets():
    flat = fc.parse_config(config())
    assert flat["settings/MinimalGapSize"] == "0.01"
    assert flat["settings/BaseMeshSettings[0]/Nx"] == "62"
    assert flat["settings/BaseMeshSettings[0]/ControlPoints[0]/Ratio"] == "2.5 1 1 0.4 1"
    assert flat["settings/ComputationalDomainSize@size"] == "6"
    assert flat["settings/WarningCommentFormat32"] == "P>Pmax, T<Tmin"
    # a container's own text is layout, not a value; the root's attributes are not settings
    assert "settings/BaseMeshSettings[0]" not in flat and "settings/ConvergenceOptions[0]/GoalsInfo[0]/GoalInfo[0]" not in flat
    assert not any(k.startswith("settings@") for k in flat)


def test_read_config_refuses_a_file_that_is_not_one(tmp_path):
    other = tmp_path / "x.xmlconfig"
    other.write_text("<nothing/>", encoding="utf-8")
    with pytest.raises(fc.CaseError, match="not a Flow solver settings file"):
        fc.read_config(str(other))
    with pytest.raises(fc.CaseError, match="Cannot read"):
        fc.read_config(str(tmp_path / "missing.xmlconfig"))


def test_mesh_of_gives_planes_cells_ratios_and_levels():
    mesh = fc.mesh_of(fc.parse_config(config()))
    assert mesh["cells"] == [62, 40, 70]
    assert mesh["min_gap_m"] == 0.01 and mesh["result_resolution"] == 4
    assert mesh["domain_m"] == {"xmin": -1.5, "ymin": -0.9, "zmin": -2, "xmax": 1.5, "ymax": 1.1, "zmax": 0.8}
    assert mesh["planes"]["x"] == {"points_m": [-1.5, -0.6, 0, 0.6, 1.5], "cells": [10, 20, 20, 12, 1],
                                   "ratios": [2.5, 1, 1, 0.4, 1], "manual": [0, 0, 0, 0, 0]}
    assert mesh["refinement"]["MaxThinChannelRefinementLevel"] == 2
    assert mesh["refinement"]["CurvatureRefinementCriteria"] == pytest.approx(0.3)


def test_conditions_and_velocity():
    flat = fc.parse_config(config(velocity=(0, 1.5, -20.0)))
    assert fc.conditions_of(flat) == {"pressure_pa": 101325, "velocity_x_mps": 0, "velocity_y_mps": 1.5,
                                      "velocity_z_mps": -20.0}
    assert fc.velocity_of(flat) == [0, 1.5, -20.0]


# --- fingerprints and differences -----------------------------------------------------------------------


def test_fingerprint_ignores_names_ids_paths_and_who_ran_it():
    a = fc.parse_config(config())
    b = fc.parse_config(config(project="Other", uuid="2222-bbbb", model="copy of box.SLDPRT", computer="PC-B"))
    assert fc.fingerprint(a) == fc.fingerprint(b)
    assert fc.differences(a, b) == []


def test_fingerprint_sees_a_mesh_setting_and_names_it():
    a = fc.parse_config(config())
    b = fc.parse_config(config(ratio_x="1 1 1 1 1", thin=5))
    assert fc.fingerprint(a) != fc.fingerprint(b) and fc.fingerprint(a, "mesh") != fc.fingerprint(b, "mesh")
    keys = [d["key"] for d in fc.differences(b, a, "mesh")]
    assert keys == ["BaseMeshSettings[0]/ControlPoints[0]/Ratio", "BaseMeshSettings[0]/MaxThinChannelRefinementLevel"]


def test_fingerprint_reads_numbers_as_numbers():
    a = fc.parse_config(config(gap="0.01"))
    b = fc.parse_config(config(gap="0.010000000000000000208"))
    assert fc.fingerprint(a) == fc.fingerprint(b)


def test_the_velocity_changes_the_settings_fingerprint_but_not_the_mesh_one():
    a = fc.parse_config(config())
    b = fc.parse_config(config(velocity=(0, 2.8, -19.8)))
    assert fc.fingerprint(a) != fc.fingerprint(b)
    assert fc.fingerprint(a, "mesh") == fc.fingerprint(b, "mesh")


# --- cases ---------------------------------------------------------------------------------------------


def test_case_round_trip_and_an_equal_run_matches(tmp_path):
    folder = folder_with(tmp_path, info=INFO, goals=GOALS)
    flat = fc.read_config(fc.config_path(folder))
    case = fc.make_case(flat, "CASE-1", folder)
    path = fc.save_json(str(tmp_path / "cases" / "CASE-1.json"), case)
    loaded = fc.load_case(path)
    assert loaded["expect"]["cells"] == 12345 and loaded["expect"]["goals"]["F1"]["value"] == 21.4
    assert loaded["fingerprint"]["settings"] == fc.fingerprint(flat)
    again = fc.parse_config(config(project="Copy", uuid="3333", model="box2.SLDPRT", computer="PC-B"))
    assert fc.same_run(again, loaded) is True
    assert fc.check_case(again, loaded, "all") == []


def test_check_case_holds_the_derived_base_mesh_only_for_the_cases_own_free_stream(tmp_path):
    case = fc.make_case(fc.parse_config(config()), "CASE-1")
    # the same free stream, other cell counts: not comparable
    same_stream = fc.parse_config(config(cells_x="12 18 18 14 1", nx=60))
    assert [d["key"] for d in fc.check_case(same_stream, case, "mesh")] == [
        "BaseMeshSettings[0]/ControlPoints[0]/NumOfCells", "BaseMeshSettings[0]/Nx"]
    # another angle of attack derives another base mesh: that is not a difference of the way of calculating
    other_stream = fc.parse_config(config(velocity=(0, 2.8, -19.8), cells_x="12 18 18 14 1", nx=60,
                                          ratio_x="2.2 1 1 0.45 1"))
    assert fc.same_run(other_stream, case) is False
    assert fc.check_case(other_stream, case, "all") == []
    # ... but an even base mesh against a stretched one is, at any angle
    even = fc.parse_config(config(velocity=(0, 2.8, -19.8), ratio_x="1 1 1 1 1", thin=5))
    found = fc.check_case(even, case, "mesh")
    assert found[0]["have"].startswith("even") and found[0]["want"].startswith("stretched")
    assert found[1]["key"] == "BaseMeshSettings[0]/MaxThinChannelRefinementLevel"


def test_load_case_refuses_other_json(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{\"hello\": 1}", encoding="utf-8")
    with pytest.raises(fc.CaseError, match="not a Flow case file"):
        fc.load_case(str(path))


# --- whose folder ---------------------------------------------------------------------------------------


def test_owner_reads_the_solver_summary_and_the_settings(tmp_path):
    folder = folder_with(tmp_path, info=INFO)
    assert fc.owner(folder) == {"model": "box.SLDPRT", "project": "P1", "project_uuid": "11111111-aaaa",
                                "computer": "PC-A", "cells": 12345, "finished": True, "has_results": True}
    empty = tmp_path / "9"
    empty.mkdir()
    assert fc.owner(str(empty)) is None


def test_foreign_names_another_model_another_project_another_computer():
    holder = {"model": "box.SLDPRT", "project": "P1", "computer": "PC-A"}
    assert fc.foreign(holder, r"D:\w\box.SLDPRT", "pc-a", "p1") is None
    assert "another model" in fc.foreign(holder, r"D:\w\box_copy.SLDPRT", "PC-A", "P1")
    assert "another project" in fc.foreign(holder, r"D:\w\box.SLDPRT", "PC-A", "P2")
    assert "another computer" in fc.foreign(holder, r"D:\w\box.SLDPRT", "PC-B", "P1")
    assert fc.foreign(None, "box.SLDPRT", "PC-B") is None


# --- results and the registry ---------------------------------------------------------------------------


def test_goal_stats_takes_the_last_row_and_skips_service_goals(tmp_path):
    folder = folder_with(tmp_path, info=INFO, goals=GOALS)
    stats = fc.goal_stats(folder)
    assert list(stats) == ["F1"]
    assert stats["F1"] == {"value": 21.4, "averaged": 21.2, "min": 21.0, "max": 21.4, "delta": 0.4,
                           "criteria": 1.2, "iterations": 3, "first_iteration_value": 50.0}


def test_result_record_against_a_case_and_the_registry_line(tmp_path):
    reference = folder_with(tmp_path, "1", info=INFO, goals=GOALS)
    case = fc.make_case(fc.read_config(fc.config_path(reference)), "CASE-1", reference)
    run = folder_with(tmp_path, "2", text=config(project="P2", uuid="4444", ratio_x="1 1 1 1 1"),
                      info=dict(INFO, project_name="P2"), goals=GOALS)
    record = fc.result_record(run, case, geometry={"fingerprint": "abc", "volume_m3": 1.0}, extra={"server": "x.y"})
    assert record["cells"] == 12345 and record["goals"]["F1"]["averaged"] == 21.2
    assert record["case"]["mesh_matches"] is False and record["case"]["same_free_stream"] is True
    assert record["case"]["differences"][0]["key"] == "BaseMeshSettings[0]/ControlPoints[0]/Ratio"
    assert record["fingerprint"]["geometry"] == "abc"
    registry = str(tmp_path / "reg" / "runs.csv")
    fc.register_run(registry, record)
    fc.register_run(registry, fc.result_record(reference, case))
    with open(registry, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert [r["project"] for r in rows] == ["P2", "P1"]
    assert [r["comparable"] for r in rows] == ["NO", "yes"]
    assert rows[0]["goals"].startswith("F1=21.4 (avg 21.2)") and rows[0]["server"] == "x.y"
    assert os.path.getsize(registry) > 0


def test_result_record_needs_a_prepared_calculation(tmp_path):
    empty = tmp_path / "5"
    empty.mkdir()
    with pytest.raises(fc.CaseError, match="no prepared calculation"):
        fc.result_record(str(empty))


# --- the live base mesh and the reduction ---------------------------------------------------------------


def _lines(start, runs):
    """Grid lines from (cells, first step, growth factor) runs."""
    out, x = [start], start
    for cells, step, factor in runs:
        for _ in range(cells):
            x += step
            out.append(x)
            step *= factor
    return out


def test_intervals_of_finds_stretched_and_even_runs():
    lines = _lines(-1.0, [(10, 0.05, 0.9), (20, 0.02, 1.0), (8, 0.025, 1.0), (10, 0.021, 1.1)])
    runs = fc.intervals_of(lines)
    assert [r["cells"] for r in runs] == [10, 20, 8, 10]
    assert runs[0]["ratio"] == pytest.approx(0.9 ** -9, rel=1e-3) and runs[1]["ratio"] == 1.0
    assert runs[3]["ratio"] == pytest.approx(1.1 ** -9, rel=1e-3)
    assert runs[0]["from_m"] == -1.0 and runs[-1]["to_m"] == pytest.approx(lines[-1])
    assert fc.intervals_of([0.0, 1.0]) == [{"from_m": 0.0, "to_m": 1.0, "cells": 1, "first_m": 1.0,
                                           "last_m": 1.0, "ratio": 1.0}]
    assert fc.intervals_of([]) == []


def test_reduce_forces_follows_the_free_stream_angle():
    goals = {"FY_all": {"value": 20.0, "averaged": 21.0}, "FZ_all": {"value": -0.5, "averaged": -0.5},
             "MX_all": {"value": 8.0, "averaged": 8.0}}
    out = fc.reduce_forces(goals, [0.0, 0.0, -20.0], q_pa=240.0, S_m2=0.5, c_m=0.4, z_cg_m=0.3)
    assert out["alpha_deg"] == 0.0 and out["L"] == 20.0 and out["D"] == 0.5
    assert out["CL"] == pytest.approx(20.0 / 120.0) and out["CD"] == pytest.approx(0.5 / 120.0)
    assert out["Cm"] == pytest.approx(-(8.0 - 0.3 * 20.0) / (240.0 * 0.5 * 0.4))
    tilted = fc.reduce_forces(goals, [0.0, 20.0, -20.0], 240.0, 0.5, 0.4, which="averaged")
    assert tilted["alpha_deg"] == 45.0
    assert tilted["L"] == pytest.approx((21.0 - 0.5) * 0.5 ** 0.5) and tilted["D"] == pytest.approx((21.0 + 0.5) * 0.5 ** 0.5)
    assert fc.reduce_forces({"FY": {"value": 1.0}}, [0, 0, -1], 1.0, 1.0, 1.0) is None


def test_compare_runs_gives_the_goal_deltas_and_whether_the_settings_agree():
    a = {"fingerprint": {"settings": "s1", "mesh": "m1"}, "goals": {"F": {"value": 20.0, "averaged": 20.0}}}
    b = {"fingerprint": {"settings": "s2", "mesh": "m1"}, "goals": {"F": {"value": 21.0, "averaged": 20.1}}}
    out = fc.compare_runs(a, b)
    assert out["same_settings"] is False and out["same_mesh"] is True and out["same_geometry"] is None
    assert out["goals"]["F"]["value"]["delta_pct"] == 5.0 and out["goals"]["F"]["averaged"]["delta_pct"] == 0.5
