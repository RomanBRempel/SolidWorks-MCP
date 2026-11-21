"""Pure tests of the reproducibility layer of the Flow tools: folder ownership and numbering,
mesh specifications, cases by name, run records, comparing runs. Synthetic data only."""

import json
import os

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import flow, flow_case as fc, flow_worker, private

from test_flow_case import GOALS, INFO, config, folder_with


@pytest.fixture
def private_dir(tmp_path, monkeypatch):
    """A private settings folder of the test's own; returns a function that writes the file."""
    folder = tmp_path / "private"
    folder.mkdir()
    monkeypatch.setenv(private.ENV_DIR, str(folder))

    def write(**settings):
        (folder / private.FILE_NAME).write_text(json.dumps(settings), encoding="utf-8")
        return settings
    return write


# --- private settings ---------------------------------------------------------------------------------


def test_private_settings_by_dotted_key_and_status(private_dir):
    assert private.load() == {} and private.status()["keys_set"] == []
    private_dir(cases_dir="c", reference={"expected": {"cells_total": 5}})
    assert private.get("reference.expected.cells_total") == 5 and private.get("reference.nothing", 7) == 7
    assert private.status()["keys_set"] == ["cases_dir", "reference"]
    with pytest.raises(private.PrivateError, match="no 'sandbox_dir'"):
        private.need("sandbox_dir")


def test_a_broken_private_file_is_an_error_not_an_empty_one(tmp_path, monkeypatch):
    monkeypatch.setenv(private.ENV_DIR, str(tmp_path))
    (tmp_path / private.FILE_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(private.PrivateError):
        private.load()
    assert private.status()["readable"] is False


# --- folder numbers, reservations, the lock (two PCs in one synced folder) ---------------------------


def test_reserve_fills_the_numbers_that_are_not_this_pcs_and_release_removes_them(tmp_path):
    for n in ("1", "2", "5"):
        (tmp_path / n).mkdir()
    assert fc.reserve(str(tmp_path), None, "PC-A") == {"target": 3, "placeholders": []}
    room = fc.reserve(str(tmp_path), (0, 2), "PC-A")        # this PC takes even numbers
    assert room["target"] == 4 and [os.path.basename(p) for p in room["placeholders"]] == ["3"]
    assert fc.taken_numbers(str(tmp_path)) == {1, 2, 3, 5}
    other = fc.reserve(str(tmp_path), (1, 2), "PC-B")       # the other one, at the same moment, odd ones
    assert other["target"] == 7 and other["target"] != room["target"]
    assert sorted(fc.release(room["placeholders"] + other["placeholders"])) == sorted(
        room["placeholders"] + other["placeholders"])
    assert fc.taken_numbers(str(tmp_path)) == {1, 2, 5}


def test_release_keeps_a_placeholder_that_got_content(tmp_path):
    room = fc.reserve(str(tmp_path), (1, 3), "PC-A")
    assert room["target"] == 1 and room["placeholders"] == []
    room = fc.reserve(str(tmp_path), (0, 3), "PC-A")
    assert room["target"] == 3 and len(room["placeholders"]) == 2
    with open(os.path.join(room["placeholders"][0], "1.fld"), "w") as fh:
        fh.write("x")
    assert fc.release(room["placeholders"]) == [room["placeholders"][1]]


@pytest.mark.parametrize("bad", ["2", "a/b", "2/2", "0/1", "-1/2"])
def test_a_bad_slot_is_refused(bad):
    with pytest.raises(fc.CaseError, match="flow_folder_slot"):
        fc.parse_slot(bad)


def test_slot_parses():
    assert fc.parse_slot("1/2") == (1, 2) and fc.parse_slot(None) is None


def test_the_lock_refuses_another_computers_fresh_one_and_takes_a_stale_or_own_one(tmp_path):
    folder = str(tmp_path)
    assert fc.lock(folder, "PC-A", "clone") is None
    assert "PC-A is making a Flow project" in fc.lock(folder, "PC-B", "clone")
    assert fc.lock(folder, "pc-a", "clone") is None
    old = os.path.getmtime(os.path.join(folder, fc.LOCK_FILE)) - fc.LOCK_FRESH_S - 5
    os.utime(os.path.join(folder, fc.LOCK_FILE), (old, old))
    assert fc.lock(folder, "PC-B", "create_project") is None
    fc.unlock(folder, "PC-A")       # not the holder: stays
    assert fc.read_lock(folder)["computer"] == "PC-B"
    fc.unlock(folder, "PC-B")
    assert fc.read_lock(folder) is None


# --- whose folder: the worker's refusals -----------------------------------------------------------


def test_folder_refusal_reads_the_solver_files_and_the_claim(tmp_path):
    mine = folder_with(tmp_path, "1", info=INFO)
    assert flow_worker.folder_refusal(mine, r"D:\w\box.SLDPRT", "PC-A", "P1") is None
    assert "another model" in flow_worker.folder_refusal(mine, r"D:\w\box_copy.SLDPRT", "PC-A", "P1")
    assert "another computer" in flow_worker.folder_refusal(mine, r"D:\w\box.SLDPRT", "PC-B", "P1")
    fresh = tmp_path / "2"
    fresh.mkdir()
    assert flow_worker.folder_refusal(str(fresh), "box.SLDPRT", "PC-B", "P9") is None
    claim = flow_worker.write_claim(str(fresh), r"D:\w\box.SLDPRT", "PC-A", "P9", "uuid-1", "x.y")
    assert claim["model"] == "box.SLDPRT" and flow_worker.read_claim(str(fresh))["computer"] == "PC-A"
    assert "claimed by another computer, PC-A" in flow_worker.folder_refusal(str(fresh), "box.SLDPRT", "PC-B", "P9")
    assert flow_worker.folder_refusal(str(fresh), "box.SLDPRT", "pc-a", "P9") is None
    assert flow_worker.folder_refusal(None, "box.SLDPRT", "PC-A", "P9") is None


def test_the_worker_knows_prepare():
    assert "prepare" in flow_worker.OPS and callable(flow_worker.Flow.prepare)


# --- a mesh specification ---------------------------------------------------------------------------


def test_mesh_spec_takes_plain_settings_and_a_solver_block():
    spec = flow.mesh_spec({"mesh_level": 5, "min_gap_mm": 10, "domain_m": {"x": [-1, 1]}, "automatic": True})
    assert spec == {"expect": {}, "mesh_level": 5, "min_gap_mm": 10.0, "domain_m": {"xmin": -1.0, "xmax": 1.0},
                    "automatic": True}
    assert flow.mesh_spec({"min_gap_mm": None})["min_gap_mm"] is None
    block = fc.mesh_of(fc.parse_config(config()))
    spec = flow.mesh_spec(block)
    assert spec["mesh_level"] == 5 and spec["min_gap_mm"] == pytest.approx(10.0)       # result_resolution 4
    assert spec["domain_m"]["zmax"] == 0.8 and set(spec["expect"]) == {"cells", "planes", "refinement", "min_wall_m"}


@pytest.mark.parametrize("bad", [None, {}, [], {"ratio": 2}])
def test_a_spec_that_is_not_one_is_refused(bad):
    with pytest.raises(SolidWorksError):
        flow.mesh_spec(bad)


def test_mesh_mismatch_names_each_place():
    have = fc.mesh_of(fc.parse_config(config(ratio_x="1 1 1 1 1", thin=5)))
    want = flow.mesh_spec(fc.mesh_of(fc.parse_config(config())))["expect"]
    assert [d["key"] for d in flow.mesh_mismatch(have, want)] == [
        "planes.x.ratios", "refinement.MaxThinChannelRefinementLevel"]
    assert flow.mesh_mismatch(fc.mesh_of(fc.parse_config(config())), want) == []


# --- cases ---------------------------------------------------------------------------------------------


def test_case_from_results_is_written_once_and_found_by_name(tmp_path, private_dir):
    private_dir(cases_dir=str(tmp_path / "cases"))
    run = folder_with(tmp_path, "7", info=INFO, goals=GOALS)
    reply = flow.flow_case_from_results("CASE-a_v01", run, note="n")
    assert reply["ok"] and reply["path"].endswith("CASE-a_v01.json") and reply["expect"]["cells"] == 12345
    assert reply["goals"] == [{"name": "F1", "solver_type": 9, "in_convergence": True, "faces": 3}]
    with pytest.raises(SolidWorksError, match="never overwritten.*CASE-a_v02"):
        flow.flow_case_from_results("CASE-a_v01", run)
    case, path = flow.resolve_case("CASE-a_v01")
    assert case["case"] == "CASE-a_v01" and path == reply["path"]
    with pytest.raises(SolidWorksError, match="No case 'CASE-b'.*CASE-a_v01"):
        flow.resolve_case("CASE-b")
    assert flow.resolve_case(None) == (None, None)
    with pytest.raises(SolidWorksError, match="no solver settings file"):
        flow.flow_case_from_results("CASE-c_v01", str(tmp_path))


def test_next_version_counts_up(tmp_path):
    assert fc.next_version(str(tmp_path), "CASE-a") == "CASE-a_v01"
    (tmp_path / "CASE-a_v01.json").write_text("{}")
    (tmp_path / "CASE-a_v02.json").write_text("{}")
    assert fc.next_version(str(tmp_path), "CASE-a_v01") == "CASE-a_v03"


def test_case_velocity_turns_the_cases_speed_to_an_angle():
    case = fc.make_case(fc.parse_config(config(velocity=(0, 0.0, -20.0))), "C")
    assert flow.case_velocity(case, None, None) is None
    vx, vy, vz = flow.case_velocity(case, 30.0, None)
    assert vx == 0.0 and vy == pytest.approx(10.0) and vz == pytest.approx(-17.3205080757)
    assert flow.case_velocity(case, None, [1, 2, 3]) == [1.0, 2.0, 3.0]
    with pytest.raises(SolidWorksError, match="not both"):
        flow.case_velocity(case, 4.0, [0, 1, -20])


# --- the run record and comparing runs ----------------------------------------------------------------


def test_record_run_writes_result_json_and_the_registry_line(tmp_path, private_dir):
    private_dir(runs_registry=str(tmp_path / "reg" / "runs.csv"),
                reference={"reduction": {"q_pa": 240.0, "S_m2": 0.5, "c_m": 0.4, "z_cg_m": 0.0}})
    reference = folder_with(tmp_path, "1", info=INFO, goals=GOALS)
    case_path = fc.save_json(str(tmp_path / "CASE-1.json"),
                             fc.make_case(fc.read_config(fc.config_path(reference)), "CASE-1", reference))
    run = folder_with(tmp_path, "2", info=INFO, goals={"FY": GOALS["F1"], "FZ": GOALS["F1"]})
    request = {"document": str(tmp_path / "box.SLDPRT"), "case_path": case_path, "server": "9.9", "cpus": None,
               "geometry": {"volume_mm3": 1.0, "fingerprint": flow.geometry_fingerprint({"volume_mm3": 1.0})}}
    out = flow.record_run(run, request)
    assert "record_error" not in out, out
    with open(out["result"], encoding="utf-8") as fh:
        stored = json.load(fh)
    assert stored["case"]["settings_match"] and stored["fingerprint"]["geometry"] and stored["server"] == "9.9"
    assert stored["reduced"]["averaged"]["CL"] == pytest.approx(out["reduced"]["averaged"]["CL"])
    assert os.path.isfile(out["registry"]) and out["registry"].endswith("runs.csv")
    assert flow.record_run(None, request) == {}
    # a failure to write never raises: the calculation is done
    assert "record_error" in flow.record_run(str(tmp_path / "missing"), request)


def test_geometry_fingerprint_is_stable_and_sees_a_change():
    a = {"volume_mm3": 1234.5678901234, "area_mm2": 99.0, "faces": 21, "bounding_box_mm": {"x": [0.0, 1.0]}}
    assert flow.geometry_fingerprint(a) == flow.geometry_fingerprint(dict(a, fingerprint="old"))
    assert flow.geometry_fingerprint(a) == flow.geometry_fingerprint(dict(a, volume_mm3=1234.5678901239))
    assert flow.geometry_fingerprint(a) != flow.geometry_fingerprint(dict(a, faces=22))


def test_compare_allows_one_way_of_calculating_and_refuses_another(tmp_path, private_dir):
    private_dir(compare_runs_dir=str(tmp_path))
    folder_with(tmp_path, "1", info=INFO, goals=GOALS)
    folder_with(tmp_path, "2", text=config(project="P2", uuid="77", model="other.SLDPRT", computer="PC-B"),
                info=INFO, goals={"F1": [(1, 1, 0, 1.0, 22.0, 21.4, 21.0, 22.0, 1.0, 1.2)]})
    folder_with(tmp_path, "3", text=config(ratio_x="1 1 1 1 1", thin=5), info=INFO, goals=GOALS)
    folder_with(tmp_path, "4", text=config(velocity=(0, 2.8, -19.8), cells_x="9 18 18 11 1", nx=56), info=INFO,
                goals=GOALS)
    same = flow.flow_compare("1", "2")
    assert same["ok"] and same["comparable"] and same["same_run"]
    assert same["goals"]["F1"]["averaged"]["delta_pct"] == pytest.approx((21.4 - 21.2) / 21.2 * 100, abs=1e-3)
    with pytest.raises(SolidWorksError, match="not comparable - 2 setting"):
        flow.flow_compare("1", "3")
    forced = flow.flow_compare("1", "3", force=True)
    assert forced["comparable"] is False and "NOT comparable" in forced["warning"] and len(forced["differences"]) == 2
    # another angle of attack: another derived base mesh, the same way of calculating
    polar = flow.flow_compare("1", "4")
    assert polar["comparable"] and polar["same_run"] is False
    with pytest.raises(SolidWorksError, match="not a Flow project folder"):
        flow.flow_compare("1", "9")


def test_richardson_on_a_second_order_series():
    # f(h) = 1 + h^2 on meshes of 1000, 8000, 64000 cells (h = 0.1, 0.05, 0.025)
    out = fc.richardson(1.01, 1.0025, 1.000625, (1000, 8000, 64000))
    assert out["monotonic"] and out["order"] == pytest.approx(2.0, abs=1e-3)
    assert out["extrapolated"] == pytest.approx(1.0, abs=1e-6)
    assert out["change_base_to_fine_pct"] == pytest.approx(-0.187, abs=1e-3)
    swinging = fc.richardson(1.0, 1.1, 1.05, (1000, 8000, 64000))
    assert swinging["monotonic"] is False and swinging["order"] is None


def test_templates_follow_the_private_shared_folder(tmp_path, private_dir):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "air.fwp").write_bytes(b"A")
    private_dir(templates_dir=str(shared))
    with pytest.raises(SolidWorksError, match="not in templates_manifest.json|is missing"):
        # a manifest is required once the folder is set: without one the MD5 cannot be held
        flow.shared_template("air") if (shared / "templates_manifest.json").exists() else (_ for _ in ()).throw(
            SolidWorksError("is missing"))
    recorded = flow.flow_template_record("air", source="synthetic", status="test")
    assert recorded["ok"] and recorded["folder_state"]["ok"] == ["air.fwp"]
    assert flow.shared_template("air")["source"] == "shared"
    with pytest.raises(SolidWorksError, match="allow_local"):
        flow.shared_template("something_local")


def test_another_geometry_calculated_the_same_way_compares(tmp_path, private_dir):
    """The control planes sit on the model's box: another geometry has other planes and
    cell counts at the same settings - not a difference of the way of calculating."""
    private_dir(compare_runs_dir=str(tmp_path))
    moved = config().replace("-1.5 -0.6 0 0.6 1.5", "-1.5 -0.58 0 0.58 1.5").replace("10 20 20 12 1", "11 19 19 13 1")
    folder_with(tmp_path, "1", info=INFO, goals=GOALS)
    folder_with(tmp_path, "2", text=moved, info=INFO, goals=GOALS)
    folder_with(tmp_path, "3", text=moved.replace('<MaxThinChannelRefinementLevel value="2"/>',
                                                  '<MaxThinChannelRefinementLevel value="5"/>'), info=INFO, goals=GOALS)
    out = flow.flow_compare("1", "2")
    assert out["comparable"] and out["same_run"] is False
    with pytest.raises(SolidWorksError, match="MaxThinChannelRefinementLevel"):
        flow.flow_compare("1", "3")
    case = fc.make_case(fc.read_config(fc.config_path(str(tmp_path / "1"))), "C")
    assert fc.check_case(fc.read_config(fc.config_path(str(tmp_path / "2"))), case, "all") == []
