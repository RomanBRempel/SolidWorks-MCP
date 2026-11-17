"""Pure parts around undo and housekeeping: what a script made, external references, live owners,
health warnings, loops of an offset chain, the SolidWorks instance asked for, dialogs as a tool reply."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import cavity, derive, dialogs, guard, health, instance, script_tool


# --- script_tool.made_since -----------------------------------------------------------------

def state(ids, equations=()):
    return {"title": "P.SLDPRT", "ids": dict(ids), "equations": None if equations is None else list(equations)}


def test_nothing_made_when_the_two_pictures_agree():
    before = state({1: "Base", 2: "Boss"}, ['"w" = 10'])
    assert script_tool.made_since(before, state({1: "Base", 2: "Boss"}, ['"w" = 10'])) == {
        "created": [], "renamed": [], "equations_added": [], "equations_lost": []}


def test_created_features_are_new_by_id():
    before = state({1: "Base", 2: "Boss"})
    made = script_tool.made_since(before, state({1: "Base", 2: "Boss", 7: "Sketch5", 8: "Cut1"}))
    assert made["created"] == ["Sketch5", "Cut1"] and made["renamed"] == []


def test_renamed_feature_is_not_a_created_one():
    before = state({1: "Base", 2: "Boss"})
    made = script_tool.made_since(before, state({1: "Base", 2: "Lid"}))
    assert made["created"] == [] and made["renamed"] == [{"was": "Boss", "now": "Lid"}]


def test_new_feature_that_took_an_old_name_is_still_created():
    # the script renamed 'Boss' away and made a new feature under that name
    before = state({1: "Base", 2: "Boss"})
    made = script_tool.made_since(before, state({1: "Base", 2: "Boss_old", 9: "Boss"}))
    assert made["created"] == ["Boss"] and made["renamed"] == [{"was": "Boss", "now": "Boss_old"}]


def test_deleted_feature_is_neither_created_nor_renamed():
    made = script_tool.made_since(state({1: "Base", 2: "Boss"}), state({1: "Base"}))
    assert made["created"] == [] and made["renamed"] == []


def test_equations_added_and_lost():
    before = state({}, ['"w" = 10', '"h" = 5'])
    made = script_tool.made_since(before, state({}, ['"w" = 10', '"h" = 5', '"d" = 3']))
    assert made["equations_added"] == ['"d" = 3'] and made["equations_lost"] == []
    made = script_tool.made_since(before, state({}, ['"h" = 5']))
    assert made["equations_added"] == [] and made["equations_lost"] == ['"w" = 10']


def test_changed_equation_is_both_added_and_lost():
    before = state({}, ['"w" = 10', '"h" = 5'])
    made = script_tool.made_since(before, state({}, ['"w" = 12', '"h" = 5']))
    assert made["equations_added"] == ['"w" = 12'] and made["equations_lost"] == ['"w" = 10']


def test_reordered_equations_are_no_change():
    made = script_tool.made_since(state({}, ['"w" = 10', '"h" = 5']), state({}, ['"h" = 5', '"w" = 10']))
    assert made["equations_added"] == [] and made["equations_lost"] == []


@pytest.mark.parametrize("old, new", [(None, ['"w" = 10']), (['"w" = 10'], None), (None, None)])
def test_unreadable_equations_on_either_side_report_no_change(old, new):
    made = script_tool.made_since(state({1: "Base"}, old), state({1: "Base", 2: "Cut"}, new))
    assert made["equations_added"] == [] and made["equations_lost"] == []
    assert made["created"] == ["Cut"]  # the tree is still compared


def test_pictures_without_an_equations_entry_report_no_change():
    made = script_tool.made_since({"ids": {1: "Base"}}, {"ids": {1: "Base"}})
    assert made == {"created": [], "renamed": [], "equations_added": [], "equations_lost": []}


# --- derive.rows_from -----------------------------------------------------------------------

def arrays(n=2, **override):
    """ListExternalFileReferences2's nine parallel arrays for n references."""
    columns = {"files": [f"m{i}.sldprt" for i in range(n)], "components": [""] * n,
               "features": [f"Stock-m{i}-1" for i in range(n)], "kinds": ["Body"] * n, "statuses": [3] * n,
               "entities": [f"Body{i}" for i in range(n)], "feature_components": [""] * n, "options": [0] * n,
               "configurations": ["Default"] * n}
    columns.update(override)
    return tuple(columns.values())


def test_no_references_give_no_rows():
    assert derive.rows_from(None) == []
    assert derive.rows_from(()) == []
    assert derive.rows_from((None,) * 9) == []
    assert derive.rows_from(arrays(0)) == []


def test_parallel_arrays_become_one_row_per_reference():
    assert derive.rows_from(arrays(2)) == [
        {"feature": "Stock-m0-1", "file": "m0.sldprt", "status": "in context", "kind": "Body", "entity": "Body0",
         "configuration": "Default"},
        {"feature": "Stock-m1-1", "file": "m1.sldprt", "status": "in context", "kind": "Body", "entity": "Body1",
         "configuration": "Default"}]


def test_arrays_may_come_as_lists_or_tuples():
    as_tuples = tuple(tuple(column) for column in arrays(2))
    assert derive.rows_from(as_tuples) == derive.rows_from([list(column) for column in arrays(2)])


def test_status_names():
    rows = derive.rows_from(arrays(6, statuses=[0, 1, 3, 4, 5, 2]))
    assert [r["status"] for r in rows] == ["broken", "locked", "in context", "out of context", "dangling", "status 2"]
    assert set(derive.DEAD) == {"broken", "locked", "dangling"}


def test_empty_configuration_name_is_none():
    rows = derive.rows_from(arrays(2, configurations=["", None]))
    assert [r["configuration"] for r in rows] == [None, None]


def test_arrays_of_unequal_length_are_tolerated():
    rows = derive.rows_from(arrays(3, entities=["Body0"], configurations=[], kinds=None))
    assert len(rows) == 3
    assert [r["feature"] for r in rows] == ["Stock-m0-1", "Stock-m1-1", "Stock-m2-1"]
    assert [r["entity"] for r in rows] == ["Body0", None, None]
    assert all(r["kind"] is None and r["configuration"] is None for r in rows)


def test_fewer_than_nine_arrays_are_tolerated():
    rows = derive.rows_from(arrays(2)[:5])
    assert len(rows) == 2
    assert rows[0]["feature"] == "Stock-m0-1" and rows[0]["status"] == "in context"
    assert rows[0]["entity"] is None and rows[0]["configuration"] is None


def test_a_short_feature_column_does_not_lose_references():
    rows = derive.rows_from(arrays(2, features=["Stock-m0-1"]))
    assert [(r["file"], r["feature"]) for r in rows] == [("m0.sldprt", "Stock-m0-1"), ("m1.sldprt", None)]


# --- derive.verdict, volume_changes ---------------------------------------------------------

def test_verdict_is_none_while_every_reference_can_update():
    rows = derive.rows_from(arrays(2, statuses=[3, 4]))
    assert derive.verdict(rows) is None
    assert derive.verdict([{**r, "file_exists": True} for r in rows]) is None
    assert derive.verdict([]) is None


def test_verdict_names_dead_references():
    rows = derive.rows_from(arrays(3, statuses=[0, 3, 5]))
    text = derive.verdict(rows)
    assert text == "2 reference(s) are broken, dangling and do not update (Stock-m0-1, Stock-m2-1)"


def test_verdict_names_missing_files_once_each():
    rows = [{**r, "file_exists": False} for r in derive.rows_from(arrays(2, files=["gone.sldprt", "gone.sldprt"]))]
    assert derive.verdict(rows) == "2 referenced file(s) are missing (gone.sldprt)"


def test_verdict_joins_both_reasons():
    rows = derive.rows_from(arrays(2, statuses=[1, 3]))
    rows[1]["file_exists"] = False
    text = derive.verdict(rows)
    assert text.startswith("1 reference(s) are locked and do not update (Stock-m0-1); ")
    assert text.endswith("1 referenced file(s) are missing (m1.sldprt)")


def test_volume_changes_match_bodies_by_name():
    changes = derive.volume_changes({"Lid": 1000.0, "Base": 500.0}, {"Lid": 1100.0004, "Base": 500.0})
    assert changes == [{"body": "Lid", "before_mm3": 1000.0, "after_mm3": 1100.0004, "delta_mm3": 100.0},
                       {"body": "Base", "before_mm3": 500.0, "after_mm3": 500.0, "delta_mm3": 0.0}]


def test_volume_changes_keep_bodies_that_came_or_went_without_a_delta():
    changes = derive.volume_changes({"Old": 10.0, "Stays": 5.0}, {"Stays": 4.0, "New": 7.0})
    assert changes == [{"body": "Old", "before_mm3": 10.0, "after_mm3": None},
                       {"body": "Stays", "before_mm3": 5.0, "after_mm3": 4.0, "delta_mm3": -1.0},
                       {"body": "New", "before_mm3": None, "after_mm3": 7.0}]
    assert derive.volume_changes({}, {}) == []


# --- guard.live_entries ---------------------------------------------------------------------

def test_records_of_dead_processes_are_dropped():
    data = {"A.SLDPRT": {"pid": 10, "tool": "add_box"}, "B.SLDPRT": {"pid": 11, "tool": "cut_profile"},
            "C.SLDASM": {"pid": 10, "tool": "add_mate"}}
    kept, dropped = guard.live_entries(data, lambda pid: pid == 10)
    assert kept == {"A.SLDPRT": data["A.SLDPRT"], "C.SLDASM": data["C.SLDASM"]}
    assert dropped == ["B.SLDPRT"]
    assert kept["A.SLDPRT"] is data["A.SLDPRT"]  # the records themselves, untouched


def test_each_pid_is_asked_once():
    asked = []
    data = {f"P{i}.SLDPRT": {"pid": pid} for i, pid in enumerate([10, 11, 10, 10, 11, 12])}
    kept, dropped = guard.live_entries(data, lambda pid: asked.append(pid) or pid != 11)
    assert asked == [10, 11, 12]
    assert list(kept) == ["P0.SLDPRT", "P2.SLDPRT", "P3.SLDPRT", "P5.SLDPRT"]
    assert dropped == ["P1.SLDPRT", "P4.SLDPRT"]


@pytest.mark.parametrize("entry", [{"tool": "add_box"}, {}, None, {"pid": None}, {"pid": "abc"}, {"pid": [10]}])
def test_record_without_a_usable_pid_is_dropped_unasked(entry):
    asked = []
    kept, dropped = guard.live_entries({"Old.SLDPRT": entry}, lambda pid: asked.append(pid) or True)
    assert kept == {} and dropped == ["Old.SLDPRT"] and asked == []


def test_pid_stored_as_text_still_counts():
    kept, dropped = guard.live_entries({"A.SLDPRT": {"pid": "10"}}, lambda pid: pid == 10)
    assert list(kept) == ["A.SLDPRT"] and dropped == []


def test_truthy_answers_of_alive_count_as_alive():
    kept, dropped = guard.live_entries({"A": {"pid": 1}, "B": {"pid": 2}}, lambda pid: "yes" if pid == 1 else None)
    assert list(kept) == ["A"] and dropped == ["B"]
    assert guard.live_entries({}, lambda pid: True) == ({}, [])


# --- health.warnings_for(closable) ----------------------------------------------------------

def test_document_warning_says_how_many_would_close_now(monkeypatch):
    monkeypatch.setattr(health, "DOCS_WARN", 30)
    (warn,) = health.warnings_for(74, None, closable=41)
    assert "74 documents are open (> 30)" in warn and warn.endswith("the invisible, saved ones - 41 of them now.")
    (none_closable,) = health.warnings_for(74, None, closable=0)
    assert none_closable.endswith(" - 0 of them now.")  # nothing to gain: said, not hidden


def test_document_warning_without_a_count_makes_no_promise(monkeypatch):
    monkeypatch.setattr(health, "DOCS_WARN", 30)
    (warn,) = health.warnings_for(74, None)
    assert "of them now" not in warn and warn.endswith("the invisible, saved ones.")
    assert health.warnings_for(74, None, closable=None) == [warn]


def test_closable_alone_raises_no_warning(monkeypatch):
    monkeypatch.setattr(health, "DOCS_WARN", 30)
    assert health.warnings_for(30, None, closable=25) == []  # at the threshold: not past it
    assert health.warnings_for(None, None, closable=25) == []
    assert len(health.warnings_for(31, None, closable=25)) == 1


def test_memory_warning_does_not_mention_closable(monkeypatch):
    monkeypatch.setattr(health, "DOCS_WARN", 30)
    monkeypatch.setattr(health, "MEMORY_WARN_PCT", 70.0)
    warn = health.warnings_for(5, {"working_set_mb": 30000, "memory_pct": 85.5}, closable=3)
    assert len(warn) == 1 and "85.5% of RAM, > 70%" in warn[0] and "of them now" not in warn[0]
    assert health.warnings_for(5, {"working_set_mb": 30000}, closable=3) == []  # no percentage: nothing to judge
    assert health.warnings_for(5, {"working_set_mb": 30000, "memory_pct": 70.0}) == []


# --- cavity.loop_depths, assign_to_loops ----------------------------------------------------

def square(low, high):
    return [(low, low), (high, low), (high, high), (low, high)]


OUTLINE, HOLE, ISLAND, APART = square(0.0, 30.0), square(5.0, 25.0), square(10.0, 20.0), square(50.0, 60.0)


def test_depths_of_an_outline_a_hole_and_an_island():
    assert cavity.loop_depths([OUTLINE, HOLE, ISLAND]) == [0, 1, 2]
    assert cavity.loop_depths([ISLAND, OUTLINE, HOLE]) == [2, 0, 1]  # whatever the order


def test_loops_side_by_side_are_all_outer():
    assert cavity.loop_depths([OUTLINE, APART]) == [0, 0]
    assert cavity.loop_depths([OUTLINE, HOLE, APART, ISLAND]) == [0, 1, 0, 2]
    assert cavity.loop_depths([OUTLINE]) == [0]
    assert cavity.loop_depths([]) == []


def test_two_holes_in_one_outline_have_the_same_depth():
    assert cavity.loop_depths([OUTLINE, square(2.0, 8.0), square(12.0, 18.0)]) == [0, 1, 1]


def test_depth_does_not_depend_on_the_winding():
    assert cavity.loop_depths([OUTLINE[::-1], HOLE, ISLAND[::-1]]) == [0, 1, 2]


def test_pieces_go_to_the_loop_their_middle_lies_on():
    loops = [OUTLINE, HOLE, ISLAND]
    pieces = [[(0.0, 0.0), (15.0, 0.0), (30.0, 0.0)],        # the outline's bottom edge
              [(25.0, 5.0), (25.0, 15.0), (25.0, 25.0)],     # the hole's right edge
              [(10.0, 20.0), (20.0, 20.0)],                  # the island's top edge: two points, the middle is its end
              [(30.0, 28.0), (30.0, 29.0), (30.0, 30.0), (29.0, 30.0), (28.0, 30.0)]]  # round the outline's corner
    assert cavity.assign_to_loops(pieces, loops) == [0, 1, 2, 0]


def test_piece_on_no_loop_is_none():
    loops = [OUTLINE, HOLE]
    stray = [(40.0, 40.0), (41.0, 40.0), (42.0, 40.0)]            # never closed into a loop
    between = [(2.0, 10.0), (2.0, 15.0), (2.0, 20.0)]             # in the material, on neither contour
    assert cavity.assign_to_loops([stray, between, [(0.0, 0.0), (0.0, 15.0), (0.0, 30.0)]], loops) == [None, None, 0]
    assert cavity.assign_to_loops([stray], []) == [None]
    assert cavity.assign_to_loops([], loops) == []


def test_piece_within_the_tolerance_of_a_loop_belongs_to_it():
    near = [(0.0, 0.01), (15.0, 0.01), (30.0, 0.01)]
    far = [(0.0, 0.05), (15.0, 0.05), (30.0, 0.05)]
    assert cavity.assign_to_loops([near, far], [OUTLINE]) == [0, None]
    assert cavity.assign_to_loops([far], [OUTLINE], tol=0.1) == [0]


def test_material_lies_inside_even_and_outside_odd_depths():
    loops = [OUTLINE, HOLE, ISLAND]
    depths = cavity.loop_depths(loops)
    owners = cavity.assign_to_loops([[(15.0, 0.0)], [(15.0, 5.0)], [(15.0, 10.0)]], loops)
    assert [depths[o] % 2 for o in owners] == [0, 1, 0]


# --- instance.pid_of, wanted_pid ------------------------------------------------------------

@pytest.mark.parametrize("name, pid", [("SolidWorks_PID_9816", 9816), ("!SolidWorks_PID_9816", 9816),
                                       ("solidworks_pid_12", 12), ("  SolidWorks_PID_7  ", 7)])
def test_pid_from_a_running_object_table_name(name, pid):
    assert instance.pid_of(name) == pid


@pytest.mark.parametrize("name", ["SolidWorks_PID_", "SolidWorks_PID_12x", "SolidWorks_PID_-5", "xSolidWorks_PID_12",
                                  "SldWorks.Application", "a part file.SLDPRT", "", None, 9816])
def test_other_names_carry_no_pid(name):
    assert instance.pid_of(name) is None


def test_wanted_pid_from_the_given_environment():
    assert instance.wanted_pid({instance.PID_ENV: "1234"}) == 1234
    assert instance.wanted_pid({instance.PID_ENV: "  1234 "}) == 1234
    assert instance.wanted_pid({}) is None
    assert instance.wanted_pid({instance.PID_ENV: ""}) is None
    assert instance.wanted_pid({instance.PID_ENV: "   "}) is None
    assert instance.wanted_pid({"OTHER": "5"}) is None


@pytest.mark.parametrize("value", ["abc", "12a", "-5", "12.5", "0x10", "1 2", "SolidWorks_PID_12"])
def test_wanted_pid_that_is_no_number_is_refused(value):
    with pytest.raises(SolidWorksError, match="SWMCP_SW_PID must be the process id") as err:
        instance.wanted_pid({instance.PID_ENV: value})
    assert repr(value) in str(err.value)


def test_wanted_pid_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(instance.PID_ENV, "4321")
    assert instance.wanted_pid() == 4321
    monkeypatch.delenv(instance.PID_ENV)
    assert instance.wanted_pid() is None
    monkeypatch.setenv(instance.PID_ENV, "first")
    with pytest.raises(SolidWorksError):
        instance.wanted_pid()


# --- dialogs.questions, public, blocking_reply ----------------------------------------------

def box(**fields):
    base = {"hwnd": 101, "pid": 7, "title": "CAD", "class": "#32770", "text": "Save changes to Part1?", "modal": True,
            "visible": True, "enabled": True, "owner": 1, "style": 0, "ex_style": 0, "edits": [], "checkboxes": [],
            "buttons": [{"id": 6, "text": "Yes", "enabled": True, "default": True, "hwnd": 201},
                        {"id": 7, "text": "No", "enabled": True, "default": False, "hwnd": 202},
                        {"id": 2, "text": "Cancel", "enabled": True, "default": False, "hwnd": 203}]}
    return {**base, **fields}


def test_questions_are_modal_dialogs_without_a_progress_bar():
    question, modeless, working = box(), box(hwnd=102, modal=False), box(hwnd=103, progress=True)
    assert dialogs.questions([question, modeless, working]) == [question]
    assert dialogs.questions([modeless, working]) == []
    assert dialogs.questions([]) == []
    assert dialogs.questions([{"hwnd": 5}]) == []  # not classified: not known to be modal


def test_questions_keep_their_order():
    first, second = box(hwnd=1), box(hwnd=2)
    assert dialogs.questions([first, box(hwnd=9, modal=False), second]) == [first, second]


def test_public_shows_what_the_dialog_says_and_what_can_be_pressed():
    assert dialogs.public(box()) == {"hwnd": 101, "title": "CAD", "text": "Save changes to Part1?",
                                     "buttons": ["Yes", "No", "Cancel"], "modal": True, "default_button": "Yes"}


def test_public_leaves_out_window_internals():
    shown = dialogs.public(box())
    assert not {"pid", "class", "style", "ex_style", "owner", "visible", "enabled"} & set(shown)
    assert all(isinstance(b, str) for b in shown["buttons"])  # captions only: no control handles


def test_public_lists_only_enabled_buttons_and_names_a_captionless_one_by_id():
    buttons = [{"id": 6, "text": "Yes", "enabled": False, "default": False},
               {"id": 7, "text": "", "enabled": True, "default": False},
               {"id": 2, "text": "Cancel"}]  # no 'enabled' entry: taken as enabled
    shown = dialogs.public(box(buttons=buttons))
    assert shown["buttons"] == ["id 7", "Cancel"] and "default_button" not in shown


def test_public_copies_checkboxes_and_edits_without_handles():
    shown = dialogs.public(box(checkboxes=[{"id": 9, "text": "Don't show again", "checked": False, "hwnd": 301}],
                               edits=[{"id": 10, "text": "Part2", "hwnd": 302}]))
    assert shown["checkboxes"] == [{"id": 9, "text": "Don't show again", "checked": False}]
    assert shown["edits"] == [{"id": 10, "text": "Part2"}]
    assert "checkboxes" not in dialogs.public(box()) and "edits" not in dialogs.public(box())


def test_public_carries_the_flags_only_when_set():
    plain = dialogs.public(box(progress=False, text_unreadable=False, hung=False))
    assert not {"progress", "text_unreadable", "hung"} & set(plain)
    flagged = dialogs.public(box(progress=True, text_unreadable=True, hung=True))
    assert flagged["progress"] is True and flagged["text_unreadable"] is True and flagged["hung"] is True


def test_public_of_a_bare_window():
    assert dialogs.public({"hwnd": 5, "title": "Bare"}) == {"hwnd": 5, "title": "Bare", "text": None, "buttons": [],
                                                           "modal": False}


def test_public_leaves_the_dialog_alone():
    dialog = box(checkboxes=[{"id": 9, "text": "x", "checked": True, "hwnd": 301}])
    dialogs.public(dialog)
    assert dialog["checkboxes"][0]["hwnd"] == 301 and dialog["buttons"][0]["hwnd"] == 201


def test_blocking_reply_of_a_waiting_tool():
    reply = dialogs.blocking_reply([box()], tool="save_part", waiting_s=12.4)
    assert reply["ok"] is False and reply["dialog_blocking"] is True
    assert reply["error"] == ('DIALOG_BLOCKING: SolidWorks shows a dialog "CAD": "Save changes to Part1?" '
                              "[Yes | No | Cancel]. 'save_part' has been waiting for 12 s behind it.")
    assert reply["dialogs"] == [dialogs.public(box())]
    assert "dialog_respond" in reply["hint"] and "NOT cancelled" in reply["hint"]


def test_blocking_reply_without_a_tool_or_a_wait():
    reply = dialogs.blocking_reply([box()])
    assert reply["error"].endswith("[Yes | No | Cancel]. SolidWorks answers nothing until it is closed.")
    just_started = dialogs.blocking_reply([box()], tool="rebuild")
    assert just_started["error"].endswith("'rebuild' has been waiting behind it.")
    assert dialogs.blocking_reply([box()], tool="rebuild", waiting_s=0)["error"] == just_started["error"]


def test_blocking_reply_shows_every_dialog():
    reply = dialogs.blocking_reply([box(), box(hwnd=102, title="Second", buttons=[])], tool="rebuild", waiting_s=3)
    assert [d["hwnd"] for d in reply["dialogs"]] == [101, 102]
    assert '(+1 more: "Second")' in reply["error"]
