"""Pure tests of S-36 ... S-41: safe writes on a synced drive, solver files with a garbage
tail, a registry line that always says case / cells / iterations, the GDI threshold, the
documents a failed script leaves, batch globals and links, a component's transform."""

import csv
import json
import os

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import flow_case as fc
from solidworks_mcp.rbr import health, param, safefile, script_tool, xform

# --- safe writes -----------------------------------------------------------------------------


def test_write_bytes_lands_intact_and_leaves_no_part_files(tmp_path):
    target = tmp_path / "deep" / "f.bin"
    data = os.urandom(70_000)
    assert safefile.write_bytes(str(target), data) == str(target)
    assert target.read_bytes() == data
    assert [p.name for p in target.parent.iterdir()] == ["f.bin"]


def test_write_replaces_an_existing_file(tmp_path):
    target = tmp_path / "f.json"
    target.write_text("old, and longer than the new content" * 10)
    safefile.write_json(str(target), {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_a_copy_that_comes_out_different_is_made_again(tmp_path, monkeypatch):
    import shutil
    real = shutil.copyfile
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        real(src, dst)
        if calls["n"] == 1:   # the first copy is torn (as a sync client may leave it)
            with open(dst, "r+b") as fh:
                fh.truncate(3)
        return dst

    monkeypatch.setattr(safefile.shutil, "copyfile", flaky)
    safefile.write_text(str(tmp_path / "x.txt"), "hello world")
    assert (tmp_path / "x.txt").read_text() == "hello world" and calls["n"] == 2


def test_a_copy_that_never_comes_out_right_is_an_error_and_the_old_file_stays(tmp_path, monkeypatch):
    target = tmp_path / "x.txt"
    target.write_text("old")

    def torn(src, dst, *a, **k):
        with open(dst, "wb") as fh:
            fh.write(b"t")
        return dst

    monkeypatch.setattr(safefile.shutil, "copyfile", torn)
    with pytest.raises(safefile.SafeWriteError, match="intact"):
        safefile.write_text(str(target), "new content")
    assert target.read_text() == "old"


def test_a_locked_target_is_waited_for(tmp_path, monkeypatch):
    real = os.replace
    calls = {"n": 0}

    def busy(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "in use")
        return real(src, dst)

    monkeypatch.setattr(safefile.os, "replace", busy)
    monkeypatch.setattr(safefile, "BUSY_WAIT_S", (0.0, 0.0, 0.0, 0.0))
    safefile.write_text(str(tmp_path / "y.txt"), "z")
    assert (tmp_path / "y.txt").read_text() == "z"


# --- reading files with a garbage tail ------------------------------------------------------------


@pytest.mark.parametrize("tail", [b"\x00" * 512, b'\r\n    "x": 1\r\n}\r\n}', b"garbage", b"\r\n\x00\x00{"])
def test_read_json_ignores_what_follows_the_value(tmp_path, tail):
    path = tmp_path / "7.info.json"
    path.write_bytes(b'\xef\xbb\xbf{"mesh": {"cells_total": 861}, "telemetry": {"iteration": 19}}' + tail)
    assert safefile.read_json(str(path))["mesh"]["cells_total"] == 861


def test_read_json_of_a_file_cut_short_is_a_value_error(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"mesh": {"cells_')
    with pytest.raises(ValueError):
        safefile.read_json(str(path))


def test_read_info_tolerates_the_tail_and_run_size_falls_back(tmp_path):
    folder = tmp_path / "3"
    folder.mkdir()
    (folder / "3.info.json").write_bytes(b'{"mesh": {"cells_total": 500}, "telemetry": {"iteration": 40}}\x00\x00junk')
    assert fc.read_info(str(folder))["mesh"]["cells_total"] == 500
    size = fc.run_size(str(folder))
    assert (size["cells"], size["iterations"]) == (500, 40) and size["source"]["cells"] == "info.json"


INFO_TEXT = """Model: m.SLDPRT
Status: Finished

Number of cells:
    Total cells: 861049
    Fluid cells: 861 049
    Solid cells: 84893

Iterations: 191
Travels: 1.00383
"""


def test_parse_info_text():
    assert fc.parse_info_text(INFO_TEXT) == {"cells": 861049, "cells_fluid": 861049, "iterations": 191,
                                             "travels": 1.00383}


def test_run_size_from_the_text_summary_and_from_goals_dat(tmp_path):
    folder = tmp_path / "4"
    (folder / "Goals.DAT").mkdir(parents=True)
    (folder / "4.info.json").write_text('{"mesh": {"cells_')     # caught mid-write
    (folder / "4.info").write_text(INFO_TEXT.replace("Iterations: 191\n", ""), encoding="utf-8")
    (folder / "Goals.DAT" / "F1.txt").write_text(
        "Iteration\tCPUTime\tPhysTime\tTravels\tValue\tAvValue\tMinValue\tMaxValue\tDelta\tCriteria\n"
        "1\t0\t0\t0.1\t5\t5\t5\t5\t0\t1\n77\t0\t0\t0.4\t6\t6\t6\t6\t0\t1\n")
    size = fc.run_size(str(folder))
    assert size["cells"] == 861049 and size["source"]["cells"] == "info"
    assert size["iterations"] == 77 and size["source"]["iterations"] == "Goals.DAT"


# --- the registry ----------------------------------------------------------------------------------


def test_registry_row_always_names_a_case():
    record = {"cells": 1, "iterations": 2, "goals": {}}
    assert fc.registry_row(record)["case"] == fc.NO_CASE and fc.registry_row(record)["comparable"] == "n/a"
    matched = fc.registry_row({**record, "case_match": {"name": "CASE-9", "settings": True}})
    assert (matched["case"], matched["comparable"]) == ("CASE-9", "yes (fingerprint)")
    named = fc.registry_row({**record, "case": {"name": "CASE-1", "settings_match": True}})
    assert (named["case"], named["comparable"]) == ("CASE-1", "yes")


def test_match_case_by_fingerprint(tmp_path):
    for name, prints in (("a.json", {"settings": "S1", "mesh": "M1"}), ("b.json", {"settings": "S2", "mesh": "M2"})):
        (tmp_path / name).write_text(json.dumps({"case": name[0].upper(), "settings": {}, "fingerprint": prints}))
    (tmp_path / "broken.json").write_text("{")
    assert fc.match_case({"fingerprint": {"settings": "S2", "mesh": "M2"}}, str(tmp_path)) == {"name": "B",
                                                                                                "settings": True}
    assert fc.match_case({"fingerprint": {"settings": "S9", "mesh": "M1"}}, str(tmp_path)) == {"name": "A",
                                                                                                "settings": False}
    assert fc.match_case({"fingerprint": {"settings": "S9", "mesh": "M9"}}, str(tmp_path)) is None
    assert fc.match_case({"fingerprint": {"settings": "S1"}}, None) is None


def test_register_run_drops_a_garbage_tail_and_keeps_the_old_rows(tmp_path):
    registry = tmp_path / "runs.csv"
    fc.register_run(str(registry), {"written": "t1", "cells": 10, "iterations": 5, "goals": {}})
    with open(registry, "ab") as fh:
        fh.write(b"half;a;line\r\n" + b"\x00" * 300)
    fc.register_run(str(registry), {"written": "t2", "cells": 20, "iterations": 6, "goals": {}})
    rows = fc.read_registry(str(registry))
    assert [(r["written"], r["case"], r["cells"], r["iterations"]) for r in rows] == [
        ("t1", "none", "10", "5"), ("t2", "none", "20", "6")]
    raw = registry.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\x00" not in raw
    with open(registry, encoding="utf-8-sig", newline="") as fh:
        assert len(list(csv.DictReader(fh, delimiter=";"))) == 2


def test_append_csv_row_keeps_an_older_header_and_adds_new_columns(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text("a;b\r\n1;2\r\n", encoding="utf-8-sig")
    safefile.append_csv_row(str(path), ["a", "b", "c"], {"a": 3, "b": 4, "c": 5})
    header, rows = safefile.csv_rows(safefile.read_text(str(path)))
    assert header == ["a", "b", "c"] and rows == [{"a": "1", "b": "2", "c": ""}, {"a": "3", "b": "4", "c": "5"}]


# --- GDI and documents -----------------------------------------------------------------------------


def test_a_window_is_refused_from_80_percent_of_the_gdi_quota():
    assert health.GDI_REFUSE_PCT == 80.0
    assert health.gui_refusal({"gdi": 7900, "gdi_quota": 10000, "gdi_pct": 79.0}) is None
    refusal = health.gui_refusal({"gdi": 8000, "gdi_quota": 10000, "gdi_pct": 80.0})
    assert refusal and "visible=False" in refusal


def test_left_open_closes_what_the_script_opened_and_keeps_unsaved_work():
    before = {"docs": {"A": False, "B": True}, "current": "A"}
    now = {"docs": {"A": False, "B": True, "C": False, "D": True, "E": False}, "current": "C"}
    assert script_tool.left_open(before, now) == (["C", "E"], ["D"])
    assert script_tool.left_open(None, now) == ([], [])


# --- batch globals and links -----------------------------------------------------------------------


def test_global_equations_quote_the_batchs_own_names():
    out = param.global_equations({"WALL": 2, "H": "wall * 10", "D": "H + OLD"}, ["OLD"])
    assert out == ['"WALL" = 2', '"H" = "WALL" * 10', '"D" = "H" + "OLD"']
    assert param.global_equations([["A", 1.5]], []) == ['"A" = 1.5']


def test_global_equations_refuse_bad_names_and_doubles():
    with pytest.raises(SolidWorksError, match="letters"):
        param.global_equations({"2X": 1}, [])
    with pytest.raises(SolidWorksError, match="twice"):
        param.global_equations([["A", 1], ["a", 2]], [])
    with pytest.raises(SolidWorksError, match="nothing"):
        param.global_equations({}, [])


def test_link_equations():
    out = param.link_equations([{"dimension": "D1@Plane5", "expression": "WALL"}, ["D2@Sk", "abs(POS) + 1"]],
                               ["WALL", "POS"])
    assert out == ['"D1@Plane5" = "WALL"', '"D2@Sk" = abs("POS") + 1']


# --- a component's transform -----------------------------------------------------------------------

ROT_Z90 = [0, 1, 0, -1, 0, 0, 0, 0, 1, 0.1, 0.2, 0.3, 1, 0, 0, 0]   # rows: x -> +y, y -> -x


def test_transform_round_trip_in_both_readings():
    p = [0.5, -0.25, 2.0]
    for convention in ("row", "col"):
        moved = xform.apply(ROT_Z90, p, convention)
        back = xform.invert(ROT_Z90, moved, convention)
        assert back == pytest.approx(p)
        d = xform.rotate(ROT_Z90, [1, 0, 0], convention)
        assert xform.unrotate(ROT_Z90, d, convention) == pytest.approx([1, 0, 0])
    assert xform.apply(ROT_Z90, [1, 0, 0], "row") == pytest.approx([0.1, 1.2, 0.3])
    assert xform.apply(ROT_Z90, [1, 0, 0], "col") == pytest.approx([0.1, 0.2 - 1, 0.3])


def test_convention_is_told_by_the_probe():
    probe = [0.123, -0.456, 0.789]
    assert xform.convention_of(ROT_Z90, probe, xform.apply(ROT_Z90, probe, "col")) == "col"
    assert xform.convention_of(ROT_Z90, probe, xform.apply(ROT_Z90, probe, "row")) == "row"
    with pytest.raises(ValueError):
        xform.convention_of(ROT_Z90, probe, [9, 9, 9])
