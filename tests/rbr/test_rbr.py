"""RBR extension: fast paths keep upstream's contract, the journal records, the library stores."""

import json
import os

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.session import SolidWorksSession
from solidworks_mcp.rbr import fastpath, journal, library, script_tool


# --- fastpath ---------------------------------------------------------------------

def test_iter_features_uses_one_call_and_keeps_order(fake_session):
    names = [f.Name for f in fake_session._iter_features()]
    assert names == ["Notes", "Front", "Origin", "Sketch1", "Boss1"]
    assert fake_session._model.FeatureManager.get_features_calls == 1


def test_history_still_starts_after_origin(fake_session):
    assert [f.Name for f in fake_session._history()] == ["Sketch1", "Boss1"]


def test_snapshot_is_constant_size(fake_session):
    snap = fake_session._history_snapshot()
    assert snap == ("fast", "P.SLDPRT", "Boss1")


def test_rollback_removes_only_what_the_call_added(fake_session):
    snap = fake_session._history_snapshot()
    fake_session._model.add("Sketch2")
    fake_session._model.add("Cut1")
    left = fake_session._roll_back(snap)
    assert left == []
    assert [f.Name for f in fake_session._history()] == ["Sketch1", "Boss1"]


def test_rollback_noop_when_nothing_added(fake_session):
    snap = fake_session._history_snapshot()
    assert fake_session._roll_back(snap) == []
    assert len(fake_session._history()) == 2


def test_rollback_bar_mid_tree_uses_upstream_snapshot(fake_session):
    fake_session._model.rolled_back = True
    snap = fake_session._history_snapshot()
    assert snap[0] == "P.SLDPRT" and snap[1] == {"Sketch1", "Boss1"}  # the upstream shape
    fake_session._model.add("Extra")
    fake_session._roll_back(snap)
    assert [f.Name for f in fake_session._history()] == ["Sketch1", "Boss1"]


def test_rollback_other_document_does_nothing(fake_session):
    snap = fake_session._history_snapshot()
    fake_session._model.title = "Other.SLDPRT"
    fake_session._model.add("X")
    assert fake_session._roll_back(snap) == []
    assert fake_session._history()[-1].Name == "X"


def test_failed_call_is_rolled_back_end_to_end(fake_session, swmcp_home):
    def bad(self):
        self._model.add("Sketch9")
        raise SolidWorksError("refused")
    with pytest.raises(SolidWorksError):
        fake_session.run_guarded(bad, fake_session)
    assert [f.Name for f in fake_session._history()] == ["Sketch1", "Boss1"]


# --- journal ----------------------------------------------------------------------

def _entries(home):
    folder = os.path.join(home, "journal")
    (name,) = os.listdir(folder)
    with open(os.path.join(folder, name), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def test_journal_records_call_args_and_added_features(fake_session, swmcp_home):
    def add_two(self, depth_mm, name="Boss"):
        self._model.add("Sketch2")
        self._model.add(name)
        return {"ok": True}
    add_two.__name__ = "add_two"
    fake_session.run_guarded(add_two, fake_session, 5.0, name="Boss2")
    (e,) = _entries(swmcp_home)
    assert e["tool"] == "add_two" and e["ok"] is True
    assert e["args"] == [5.0] and e["kwargs"] == {"name": "Boss2"}  # session itself left out
    assert e["features"] == {"before": 5, "after": 7, "added": ["Sketch2", "Boss2"]}
    assert e["doc"] == "P.SLDPRT" and e["ms"] >= 0


def test_journal_records_errors(fake_session, swmcp_home):
    def bad(self):
        raise SolidWorksError("no face +q")
    with pytest.raises(SolidWorksError):
        fake_session.run_guarded(bad, fake_session)
    (e,) = _entries(swmcp_home)
    assert e["ok"] is False and "no face +q" in e["error"]


def test_journal_keeps_script_code(fake_session, swmcp_home):
    code = "result = 1 + 1"
    fake_session._ensure = lambda: _FakeSw(fake_session._model)
    res = fake_session.run_guarded(script_tool.run_script, fake_session, code)
    assert res["ok"] and res["result"] == 2
    (e,) = _entries(swmcp_home)
    assert e["tool"] == "run_script" and e["code"] == code


def test_journal_read_filters_and_stats(fake_session, swmcp_home):
    ok = lambda self: {"ok": True}  # noqa: E731
    ok.__name__ = "list_bodies"
    for _ in range(3):
        fake_session.run_guarded(ok, fake_session)
    got = journal.read(last_n=2, tool="list_bodies")
    assert got["total"] == 3 and got["count"] == 2 and got["stats"]["list_bodies"]["calls"] == 3


# --- run_script -------------------------------------------------------------------

class _FakeSw:
    def __init__(self, model):
        self.ActiveDoc = model


def test_script_reports_tree_change_and_args(fake_session, swmcp_home):
    fake_session._ensure = lambda: _FakeSw(fake_session._model)
    code = "model.add(args['n'])\nprint('hi')\nresult = [f.Name for f in session._history()]"
    res = script_tool.run_script(fake_session, code, {"n": "Cut7"})
    assert res["result"][-1] == "Cut7" and res["stdout"] == "hi\n"
    assert res["features"]["added"] == ["Cut7"]


def test_script_error_is_readable_and_rolled_back(fake_session, swmcp_home):
    fake_session._ensure = lambda: _FakeSw(fake_session._model)
    with pytest.raises(SolidWorksError, match="ZeroDivisionError"):
        fake_session.run_guarded(script_tool.run_script, fake_session, "model.add('Tmp')\n1/0")
    assert fake_session._history()[-1].Name == "Boss1"


# --- library ----------------------------------------------------------------------

def test_library_save_list_get_and_override(swmcp_home):
    assert "tree_summary" in {s["name"] for s in library.list_scripts()["scripts"]}
    library.save_script("tree_summary", "result = 'mine'", "My version")
    got = library.get_script("tree_summary")
    assert got["origin"] == "user" and got["description"] == "My version"
    assert library.code_of("tree_summary").endswith("result = 'mine'\n")


def test_library_rejects_bad_names_and_syntax(swmcp_home):
    with pytest.raises(SolidWorksError):
        library.save_script("../evil", "x = 1")
    with pytest.raises(SolidWorksError, match="does not parse"):
        library.save_script("ok_name", "def (")


def test_builtin_scripts_parse():
    import ast
    for s in os.listdir(library.BUILTIN_DIR):
        if s.endswith(".py"):
            with open(os.path.join(library.BUILTIN_DIR, s), encoding="utf-8") as fh:
                assert ast.get_docstring(ast.parse(fh.read())) or s.startswith("_"), s


# --- registration -------------------------------------------------------------------

def test_register_adds_tools_without_touching_upstream(swmcp_home):
    import asyncio
    from solidworks_mcp import server
    from solidworks_mcp.rbr import register
    before = {t.name for t in asyncio.run(server.mcp.list_tools())}
    register(server)
    after = {t.name for t in asyncio.run(server.mcp.list_tools())}
    new = after - before
    assert {"run_script", "run_saved_script", "list_scripts", "get_script", "save_script", "get_journal",
            "tool_source", "set_units", "get_custom_properties", "set_custom_properties",
            "list_configurations", "activate_configuration", "add_configuration", "export_face_dxf",
            "assembly_bom", "server_health", "release_ui", "release_document", "shell_check"} <= new
    assert before <= after  # every upstream tool still there


def test_tool_source_shows_code():
    from solidworks_mcp.rbr import tools
    got = tools.tool_source(None, "add_box")
    assert "def add_box" in got["implementation"]["source"]
    assert "def add_box" in got["wrapper"]["source"]
