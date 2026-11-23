"""rebuild_chain: who depends on the master and in what order; export_static_body's switching (pure parts)."""

import os

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import chain, derive

K = chain.key
FUS, F1, F2, WING, ROOT, RIB, LONE = (K(f"C:/proj/{n}.SLDPRT") for n in
                                       ("fuselage", "F1", "F2", "wing", "root", "rib", "bracket"))
GRAPH = {FUS: [], F1: [FUS], F2: [FUS], WING: [], ROOT: [WING], RIB: [ROOT, WING], LONE: []}


def test_paths_come_from_the_name_path_pairs():
    assert chain.paths_of(None) == []
    assert chain.paths_of(("t_src", "D:\\t\\t_src.SLDPRT", "pin", "D:\\t\\pin.SLDPRT")) == [
        "D:\\t\\t_src.SLDPRT", "D:\\t\\pin.SLDPRT"]
    assert chain.paths_of(("odd",)) == []


def test_keys_ignore_case_and_slashes():
    assert chain.key("C:/Proj/A.sldprt") == chain.key("c:\\proj\\a.SLDPRT")


def test_dependents_follow_the_references_through_other_parts():
    assert chain.dependents(GRAPH, FUS) == {F1, F2}
    assert chain.dependents(GRAPH, WING) == {ROOT, RIB}            # the rib also through the root
    assert chain.dependents(GRAPH, ROOT) == {RIB}
    assert chain.dependents(GRAPH, LONE) == set() and chain.dependents(GRAPH, RIB) == set()


def test_the_order_puts_sources_first_and_is_the_same_every_time():
    assert chain.in_order(GRAPH, {RIB, ROOT}) == [ROOT, RIB]
    assert chain.in_order(GRAPH, {F2, F1}) == [F1, F2]               # no dependency between them: by name
    assert chain.in_order(GRAPH, set()) == []
    # a reference to a part outside the set (the master itself) does not hold a member back
    assert chain.in_order(GRAPH, {ROOT}) == [ROOT]


def test_a_circle_of_references_is_refused():
    a, b = K("C:/p/a.SLDPRT"), K("C:/p/b.SLDPRT")
    with pytest.raises(SolidWorksError, match="in a circle"):
        chain.in_order({a: [b], b: [a]}, {a, b})


def test_objections_protect_other_processes_and_unsaved_work():
    docs = {FUS: {"title": "fuselage.SLDPRT", "modified": True}, F1: {"title": "F1.SLDPRT", "modified": True},
            F2: {"title": "F2.SLDPRT", "modified": False}}
    owners = {"fuselage.SLDPRT": {"pid": 10}, "F2.SLDPRT": {"pid": 77}}
    got = chain.objections([FUS, F1, F2], docs, owners, 10, True)
    assert len(got) == 2
    assert "F1.SLDPRT" in got[0] and "unsaved changes this server did not make" in got[0]
    assert "F2.SLDPRT" in got[1] and "server process 77" in got[1]
    # without saving, somebody's unsaved work is only rebuilt in memory: allowed
    assert chain.objections([FUS, F1], docs, owners, 10, False) == []
    # a part that is not open is nobody's
    assert chain.objections([K("C:/proj/closed.SLDPRT")], docs, owners, 10, True) == []


def test_the_summary_sorts_the_parts():
    rows = [{"part": "fuselage", "changed": False, "rebuild_ok": True},
            {"part": "F1", "changed": True, "rebuild_ok": True, "saved": True},
            {"part": "F2", "changed": True, "rebuild_ok": True, "failing_features": [{"name": "Cut1"}]},
            {"part": "F3", "error": "SolidWorks did not save it"}]
    rows.append({"part": "frozen", "skipped": "its references to the chain are broken"})
    got = chain.summary(rows)
    assert got == {"count": 5, "changed": ["F1", "F2"], "unchanged": ["fuselage"], "failed": ["F2", "F3"],
                   "saved": ["F1"], "skipped": ["frozen"]}
    assert "parts" not in got        # the reply's `parts` is the table itself


def test_candidates_are_the_part_files_of_the_folders(tmp_path):
    for name in ("b.SLDPRT", "a.sldprt", "~$a.sldprt", "asm.SLDASM", "notes.txt"):
        (tmp_path / name).write_text("x")
    assert [os.path.basename(p) for p in chain._candidates([str(tmp_path)])] == ["a.sldprt", "b.SLDPRT"]
    with pytest.raises(SolidWorksError, match="No folder"):
        chain._candidates([str(tmp_path / "missing")])


def test_too_many_files_ask_for_a_narrower_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(chain, "MAX_FILES", 2)
    for i in range(3):
        (tmp_path / f"p{i}.SLDPRT").write_text("x")
    with pytest.raises(SolidWorksError, match="limit 2"):
        chain._candidates([str(tmp_path)])


# --- export_static_body: which features to switch -----------------------------------------------


STATES = {"Boss": False, "DEL_lid": False, "DEL_core": True, "Fillet": True}


def test_only_features_not_yet_in_the_wanted_state_are_switched():
    assert derive.state_changes(STATES, ["DEL_lid", "DEL_core"], ["Fillet", "Boss"]) == (["DEL_lid"], ["Fillet"])
    assert derive.state_changes(STATES, "DEL_lid", None) == (["DEL_lid"], [])
    assert derive.state_changes(STATES, None, None) == ([], [])
    assert derive.state_changes(STATES, ["DEL_lid", "DEL_lid"], []) == (["DEL_lid"], [])


def test_unknown_and_contradictory_features_are_refused():
    with pytest.raises(SolidWorksError, match="No feature named 'Nope'"):
        derive.state_changes(STATES, ["Nope"], None)
    with pytest.raises(SolidWorksError, match="both to suppress and to unsuppress"):
        derive.state_changes(STATES, ["Boss"], ["Boss"])


def test_a_part_follows_only_through_a_live_link_to_the_chain():
    refs = [{"file": "C:/proj/fuselage.SLDPRT", "status": "broken"}, {"file": "C:/other/x.SLDPRT", "status": "in context"}]
    assert chain.follows(refs, [FUS, F1]) is False                    # what export_static_body leaves
    assert chain.follows([{"file": "c:/proj/FUSELAGE.sldprt", "status": "in context"}], [FUS]) is True
    assert chain.follows([{"file": "C:/proj/fuselage.SLDPRT", "status": "out of context"}], [FUS]) is True
    assert chain.follows([{"file": "C:/proj/fuselage.SLDPRT", "status": "locked"}], [FUS]) is False
    assert chain.follows([], [FUS]) is False and chain.follows([{"file": None, "status": "in context"}], [FUS]) is False


# --- O-60: references whose stored path leads nowhere ----------------------------------------------


def test_resolve_keeps_a_stored_path_that_exists():
    by_name = chain.names_of([r"D:\w\a.sldprt"])
    assert chain.resolve(r"E:\old\a.sldprt", r"D:\w\b.sldprt", by_name, exists=lambda p: True) == \
        (chain.key(r"E:\old\a.sldprt"), False)


def test_resolve_finds_a_moved_file_by_name_beside_the_referrer_first():
    files = [r"D:\w\a.sldprt", r"D:\other\a.sldprt", r"D:\w\b.sldprt"]
    by_name = chain.names_of(files)
    assert chain.resolve(r"I:\gone\A.SLDPRT", r"D:\w\b.sldprt", by_name, exists=lambda p: False) == \
        (chain.key(r"D:\w\a.sldprt"), True)


def test_resolve_takes_the_only_file_of_that_name_elsewhere_and_refuses_to_guess():
    by_name = chain.names_of([r"D:\x\a.sldprt"])
    assert chain.resolve(r"C:\gone\a.sldprt", r"D:\w\b.sldprt", by_name, exists=lambda p: False) == \
        (chain.key(r"D:\x\a.sldprt"), True)
    two = chain.names_of([r"D:\x\a.sldprt", r"D:\y\a.sldprt"])
    assert chain.resolve(r"C:\gone\a.sldprt", r"D:\w\b.sldprt", two, exists=lambda p: False) == \
        (chain.key(r"C:\gone\a.sldprt"), False)


def test_a_moved_chain_is_found_whole():
    """master -> a -> b moved together: b stores a's old path; it must still follow the master."""
    files = [r"D:\new\m.sldprt", r"D:\new\a.sldprt", r"D:\new\b.sldprt"]
    by_name = chain.names_of(files)
    stored = {r"D:\new\a.sldprt": [r"C:\old\m.sldprt"], r"D:\new\b.sldprt": [r"C:\old\a.sldprt"],
              r"D:\new\m.sldprt": []}
    graph = {chain.key(f): [chain.resolve(p, f, by_name, exists=lambda p: False)[0] for p in stored[f]]
             for f in files}
    root = chain.key(r"D:\new\m.sldprt")
    assert chain.in_order(graph, chain.dependents(graph, root)) == [chain.key(r"D:\new\a.sldprt"),
                                                                     chain.key(r"D:\new\b.sldprt")]


def test_follows_uses_the_resolver():
    refs = [{"file": r"C:\old\a.sldprt", "status": "ok"}]
    members = [chain.key(r"D:\new\a.sldprt")]
    assert not chain.follows(refs, members)
    assert chain.follows(refs, members, lambda p: chain.key(r"D:\new\a.sldprt"))
