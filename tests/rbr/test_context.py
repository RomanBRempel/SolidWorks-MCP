"""Edit context (freeze and rollback bars) and naming what a call created mid-tree."""

import json
import os

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import context, guard, journal, script_tool

from conftest import FakeModel, FakeSw


@pytest.fixture
def part(fake_session, monkeypatch):
    """A part with the history A..E, the context patches applied over a stub save and status."""
    from solidworks_mcp.session import SolidWorksSession
    model = FakeModel(["A", "B", "C", "D", "E"])
    fake_session._model = model
    fake_session._sw = FakeSw(model)
    fake_session.saved = []
    monkeypatch.setattr(SolidWorksSession, "save_part", lambda self, path: self.saved.append(path) or {"ok": True})
    monkeypatch.setattr(SolidWorksSession, "get_status", lambda self: {"ok": True})
    context.apply()
    yield fake_session
    context.unapply()
    context._seen.clear()
    context._toggle_enabled_here = False


def _names(session):
    return [f.Name for f in session._history()]


# --- S-01 ---------------------------------------------------------------------------------


def test_edit_context_freezes_down_to_one_feature_and_rolls_back_before_another(part):
    res = context.edit_context(part, freeze_to="B", rollback_before="E")
    assert res["frozen_to"] == "B" and res["rolled_back_before"] == "E" and res["rolled_back"] == 1
    assert (res["frozen"], res["editable"], res["features"]) == (2, 2, 5)
    assert (res["editable_from"], res["editable_to"]) == ("C", "D")
    assert _names(part) == ["A", "B", "C", "D", "E"]  # the freeze bar is not a feature of the history
    assert context.seen()["P.SLDPRT"]["frozen_to"] == "B"


def test_edit_context_end_leaves_nothing_frozen_or_rolled_back(part):
    context.edit_context(part, freeze_to="B", rollback_before="E")
    res = context.edit_context(part, end=True)
    assert res["ended"] and not res["active"] and res["failing_features"] == []
    assert res["was"]["frozen_to"] == "B"
    assert not any(f.IsRolledBack() or f.IsFrozen() for f in part._model.features)
    assert part._sw.toggles[461] is False  # the option this server switched on is off again
    assert context.seen() == {}


def test_edit_context_without_arguments_reports_the_bars(part):
    assert context.edit_context(part)["active"] is False
    context.edit_context(part, rollback_before="D")
    res = context.edit_context(part)
    assert res["rolled_back_before"] == "D" and res["rolled_back"] == 2 and res["frozen_to"] is None


def test_freeze_must_come_before_the_rollback_bar(part):
    with pytest.raises(SolidWorksError, match="earlier in the tree"):
        context.edit_context(part, freeze_to="D", rollback_before="C")
    assert not any(f.IsRolledBack() or f.IsFrozen() for f in part._model.features)
    with pytest.raises(SolidWorksError, match="No feature 'Nope'"):
        context.edit_context(part, rollback_before="Nope")


def test_moving_an_existing_context_keeps_the_bars_apart(part):
    context.edit_context(part, freeze_to="A", rollback_before="C")
    # the new freeze position lies below the current rollback bar: the bar has to move first
    res = context.edit_context(part, freeze_to="C", rollback_before="E")
    assert res["frozen_to"] == "C" and res["rolled_back_before"] == "E"
    fm = part._model.FeatureManager
    assert fm.rollback_calls[-1] == (3, "E")
    assert [c for c in fm.freeze_calls if c[1] == "C"]


def test_freeze_bar_option_is_switched_on_while_the_feature_tree_is_enabled(part):
    """Switched on with the tree off (as it is inside a tool call) the option gives no
    freeze bar and EditFreeze answers 1 - seen live on SW 2025 SP3."""
    fm = part._model.FeatureManager
    fm.EnableFeatureTree = False
    seen = []
    original = part._sw.SetUserPreferenceToggle

    def toggle(key, value):
        seen.append((key, value, fm.EnableFeatureTree))
        original(key, value)

    part._sw.SetUserPreferenceToggle = toggle
    context.edit_context(part, freeze_to="B")
    assert seen == [(461, True, True)]
    assert fm.EnableFeatureTree is False  # and the tree is off again for the rest of the call


def test_failed_entry_does_not_leave_half_a_context(part):
    part._model.FeatureManager.EditRollback = lambda location, name: location == 1  # refuses to roll back
    with pytest.raises(SolidWorksError, match="refused to roll back"):
        context.edit_context(part, freeze_to="B", rollback_before="E")
    assert part._model.FeatureManager.GetFreezeLocation() is None


def test_save_part_is_refused_while_rolled_back(part):
    context.edit_context(part, freeze_to="A", rollback_before="D")
    with pytest.raises(SolidWorksError, match="2 feature.s. are rolled back .from 'D' on."):
        part.save_part("x.sldprt")
    assert part.saved == []
    context.edit_context(part, rollback_before="E")
    context.edit_context(part, end=True)
    assert part.save_part("x.sldprt") == {"ok": True} and part.saved == ["x.sldprt"]


def test_save_part_with_only_the_freeze_bar_warns(part):
    context.edit_context(part, freeze_to="B")
    res = part.save_part("x.sldprt")
    assert res["ok"] and "freeze bar after 'B'" in res["warning"]


def test_status_shows_the_edit_context(part):
    assert "edit_context" not in part.get_status()
    context.edit_context(part, rollback_before="C")
    assert part.get_status()["edit_context"]["rolled_back_before"] == "C"


# --- S-02 ---------------------------------------------------------------------------------


def test_new_plane_is_found_above_the_rollback_bar_not_at_the_end_of_the_tree(part):
    model = part._model
    model.features[-1].type_name = "RefPlane"  # E: a plane further down the tree
    context.edit_context(part, rollback_before="D")
    model.add("Plane9", "RefPlane")  # what InsertRefPlane does: it lands above the bar
    assert part._last_ref_plane().Name == "Plane9"  # upstream took E and renamed it
    assert _names(part) == ["A", "B", "C", "Plane9", "D", "E"]


def test_default_pattern_seed_is_not_taken_from_below_the_rollback_bar(part):
    context.edit_context(part, rollback_before="D")
    assert part._last_feature_name() == "C"


def test_tools_that_take_the_last_history_entry_get_the_newest_live_feature(part):
    context.edit_context(part, rollback_before="D")
    part._model.add("Sketch9")
    assert part._history()[-1].Name == "E"  # the tree's real order for everyone else
    seen = []

    def tool(self):
        seen.append(self._history()[-1].Name)

    context._newest_last(tool)(part)
    assert seen == ["Sketch9"]
    assert part._history()[-1].Name == "E"


def test_script_helpers_name_what_was_added_mid_tree(part):
    context.edit_context(part, rollback_before="D")
    before = journal.tree_state(part, names=True)
    part._model.add("Плоскость7", "RefPlane")
    after = journal.tree_state(part, names=True)
    assert journal.tree_added(part, before, after) == ["Плоскость7"]


def test_journal_names_the_feature_added_above_the_bar(part, swmcp_home):
    context.edit_context(part, rollback_before="D")

    def add_plane(self):
        self._model.add("PL_NEW", "RefPlane")
        return {"ok": True}

    part.run_guarded(add_plane, part)
    folder = os.path.join(swmcp_home, "journal")
    (name,) = os.listdir(folder)
    with open(os.path.join(folder, name), encoding="utf-8") as fh:
        entry = [json.loads(line) for line in fh][-1]
    assert entry["features"]["added"] == ["PL_NEW"] and entry["features"]["rolled_back"] is True


def test_failed_call_in_a_rolled_back_part_removes_only_its_own_features(part):
    context.edit_context(part, rollback_before="D")

    def bad(self):
        self._model.add("Sketch9")
        self._model.add("Cut9")
        raise SolidWorksError("refused")

    with pytest.raises(SolidWorksError):
        part.run_guarded(bad, part)
    assert _names(part) == ["A", "B", "C", "D", "E"]


# --- guard --------------------------------------------------------------------------------


def test_command_in_progress_is_set_before_the_snapshot(part, monkeypatch):
    """The snapshot of a rolled-back part reads the tree: 20.7 s without the flag, 0.3 s with it."""
    seen = {}
    original = type(part)._history_snapshot

    def snapshot(self):
        seen["flag"] = self._sw.CommandInProgress
        return original(self)

    monkeypatch.setattr(type(part), "_history_snapshot", snapshot)

    def work(self):
        return {"ok": True}

    work.__name__ = "add_box"
    part.run_guarded(work, part)
    assert seen["flag"] is True and part._sw.CommandInProgress is False


def test_a_plain_refusal_does_not_walk_every_document(part, monkeypatch):
    calls = []
    monkeypatch.setattr(guard, "release_ui", lambda session, *a, **kw: calls.append(1) or {})

    def refuse(self):
        raise SolidWorksError("no such face")

    refuse.__name__ = "add_hole"
    with pytest.raises(SolidWorksError):
        part.run_guarded(refuse, part)
    assert calls == []  # nothing was left switched off: the cheap check is enough

    def crash(self):
        self._model.FeatureManager.EnableFeatureTree = False  # a call that died mid-way
        raise SolidWorksError("refused")

    crash.__name__ = "add_hole"
    with pytest.raises(SolidWorksError):
        part.run_guarded(crash, part)
    assert calls == [1]


def test_run_script_reports_features_added_and_removed_by_name(part):
    part._ensure = lambda: part._sw
    res = script_tool.run_script(part, "model.add('X1')\nmodel.features.remove(model.features[3])")
    assert res["features"]["added"] == ["X1"] and res["features"]["removed"] == ["A"]
