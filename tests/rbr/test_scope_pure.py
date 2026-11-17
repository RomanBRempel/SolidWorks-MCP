"""scope: which tools are builders, pairing bodies across a call, the change block and expect (pure)."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import scope


def body(volume, centre=(0.0, 0.0, 0.0), faces=6):
    return {"volume_mm3": volume, "centre_mm": list(centre), "faces": faces}


def change(delta=0.0, before=1, after=1, rows=(), **extra):
    """The smallest change block check_expect reads."""
    return {"volume_mm3": {"delta": delta}, "body_count": {"before": before, "after": after},
            "bodies": list(rows), **extra}


# --- is_builder -----------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["add_box", "cut_profile", "extrude_sketch", "sweep_cut", "loft_cut",
                                  "mirror_body", "combine_bodies", "split_body"])
def test_tools_with_a_building_prefix_are_builders(name):
    assert scope.is_builder(name)


def test_builders_without_a_prefix_are_listed_by_name():
    assert all(scope.is_builder(name) for name in scope.BUILDERS_EXTRA)
    assert {"move_copy_body", "delete_bodies", "insert_part", "set_feature_scope"} <= scope.BUILDERS_EXTRA


def test_tools_that_only_look_like_builders_are_not():
    for name in ("add_configuration", "add_mate", "add_relation", "add_sketch", "add_plane", "add_sketch3d",
                 "split_line_project", "add_dimension"):
        assert name in scope.NOT_BUILDERS and not scope.is_builder(name)


def test_every_scoped_tool_is_a_builder():
    assert all(scope.is_builder(name) for name in scope.SCOPED)
    assert not scope.SCOPED & scope.NOT_BUILDERS


@pytest.mark.parametrize("name", ["list_faces", "set_dimension", "get_status", "rebuild", "sketch_rect",
                                  "find_faces", "addbox", ""])
def test_readers_and_setters_are_not_builders(name):
    assert not scope.is_builder(name)


# --- pair_bodies ----------------------------------------------------------------------------

def test_bodies_that_kept_their_name_pair_with_themselves():
    before = {"A": body(10), "B": body(20, (50, 0, 0))}
    assert scope.pair_bodies(before, dict(before)) == [("A", "A"), ("B", "B")]


def test_a_renamed_body_pairs_with_the_nearest_centre():
    before = {"Boss1": body(1000, (0, 0, 0)), "Boss2": body(1000, (100, 0, 0))}
    after = {"Cut1": body(900, (99, 0, 0)), "Cut2": body(900, (1, 0, 0))}  # listed the other way round
    assert sorted(scope.pair_bodies(before, after)) == [("Boss1", "Cut2"), ("Boss2", "Cut1")]


def test_same_names_pair_before_the_nearest_centre_is_asked():
    # 'A' moved far away, and the new body sits where 'A' was: the name still wins
    before = {"A": body(10, (0, 0, 0)), "B": body(10, (5, 0, 0))}
    after = {"A": body(10, (500, 0, 0)), "Cut1": body(10, (0, 0, 0))}
    assert scope.pair_bodies(before, after) == [("A", "A"), ("B", "Cut1")]


def test_leftover_bodies_appeared_or_went():
    before = {"A": body(10), "Gone": body(5, (30, 0, 0))}
    assert scope.pair_bodies(before, {"A": body(10)}) == [("A", "A"), ("Gone", None)]
    assert scope.pair_bodies({"A": body(10)}, {"A": body(10), "New": body(5, (30, 0, 0))}) == [("A", "A"),
                                                                                             (None, "New")]
    assert scope.pair_bodies({}, {}) == []


def test_a_split_pairs_the_old_body_with_the_nearer_half():
    before = {"Block": body(1000, (10, 0, 0))}
    after = {"Split1": body(700, (7, 0, 0)), "Split2": body(300, (17, 0, 0))}
    assert scope.pair_bodies(before, after) == [("Block", "Split1"), (None, "Split2")]


# --- change_report --------------------------------------------------------------------------

def test_change_report_of_a_cut_that_renamed_its_body():
    before = {"bodies": {"Boss1": body(1000.0, (5, 5, 5), faces=6), "Other": body(200.0, (50, 0, 0), faces=6)}}
    after = {"bodies": {"Cut1": body(899.99951, (5, 5, 5.2), faces=11), "Other": body(200.0, (50, 0, 0), faces=6)}}
    report = scope.change_report(before, after)
    assert report["bodies"] == [
        {"body": "Other", "before_mm3": 200.0, "after_mm3": 200.0, "delta_mm3": 0.0},
        {"body": "Cut1", "was": "Boss1", "before_mm3": 1000.0, "after_mm3": 900.0, "delta_mm3": -100.0}]
    assert report["bodies_added"] == [] and report["bodies_removed"] == []
    assert report["body_count"] == {"before": 2, "after": 2}
    assert report["volume_mm3"] == {"before": 1200.0, "after": 1100.0, "delta": -100.0}
    assert report["faces"] == {"before": 12, "after": 17, "added": 5, "removed": 0}


def test_change_report_names_bodies_that_appeared_and_went():
    before = {"bodies": {"A": body(100.0, faces=6), "Tool": body(40.0, (90, 0, 0), faces=8)}}
    after = {"bodies": {"A": body(100.0, faces=6)}}
    gone = scope.change_report(before, after)
    assert gone["bodies_removed"] == ["Tool"] and gone["bodies_added"] == []
    assert gone["bodies"][-1] == {"body": None, "was": "Tool", "before_mm3": 40.0, "after_mm3": None,
                                  "delta_mm3": -40.0}
    assert gone["faces"] == {"before": 14, "after": 6, "added": 0, "removed": 8}
    came = scope.change_report(after, before)
    assert came["bodies_added"] == ["Tool"] and came["bodies_removed"] == []
    assert came["bodies"][-1] == {"body": "Tool", "before_mm3": None, "after_mm3": 40.0, "delta_mm3": 40.0}
    assert came["body_count"] == {"before": 1, "after": 2}
    assert came["volume_mm3"]["delta"] == 40.0


def test_change_report_feeds_check_expect():
    before = {"bodies": {"A": body(100.0), "B": body(50.0, (40, 0, 0))}}
    after = {"bodies": {"Cut1": body(90.0, (0, 0, 1)), "B": body(50.0, (40, 0, 0))}}
    report = scope.change_report(before, after)
    assert scope.check_expect({"volume": "decrease", "volume_mm3": [-11, -9], "bodies": "same",
                               "changed_bodies": ["A"]}, report) == []
    assert len(scope.check_expect({"volume": "increase", "bodies": "+1", "changed_bodies": ["B"]}, report)) == 3


# --- check_expect ---------------------------------------------------------------------------

def test_expect_volume_decrease_increase_unchanged():
    assert scope.check_expect({"volume": "decrease"}, change(-5.0)) == []
    assert scope.check_expect({"volume": "increase"}, change(5.0)) == []
    assert scope.check_expect({"volume": "unchanged"}, change(0.0)) == []
    assert scope.check_expect({"volume": "Decrease"}, change(-5.0)) == []  # case-blind
    (failed,) = scope.check_expect({"volume": "decrease"}, change(5.0))
    assert "decrease" in failed and "+5.000" in failed
    assert len(scope.check_expect({"volume": "increase"}, change(-5.0))) == 1
    assert len(scope.check_expect({"volume": "unchanged"}, change(-5.0))) == 1


def test_expect_volume_ignores_numeric_noise():
    noise = scope.VOLUME_TOL_MM3 / 2
    assert scope.check_expect({"volume": "unchanged"}, change(-noise)) == []
    assert len(scope.check_expect({"volume": "decrease"}, change(-noise))) == 1
    assert len(scope.check_expect({"volume": "increase"}, change(noise))) == 1


def test_expect_volume_takes_only_its_three_words():
    with pytest.raises(SolidWorksError, match="'decrease', 'increase' or 'unchanged'"):
        scope.check_expect({"volume": "smaller"}, change(-5.0))


def test_expect_volume_mm3_is_a_range_for_the_change():
    assert scope.check_expect({"volume_mm3": [-120, -80]}, change(-100.0)) == []
    assert scope.check_expect({"volume_mm3": [-80, -120]}, change(-100.0)) == []  # either order
    assert scope.check_expect({"volume_mm3": [-100, -100]}, change(-100.0004)) == []  # the ends are inside
    (failed,) = scope.check_expect({"volume_mm3": [-120, -80]}, change(-50.0))
    assert "[-120, -80]" in failed and "-50.000" in failed
    assert len(scope.check_expect({"volume_mm3": [10, 20]}, change(25.0))) == 1


def test_expect_bodies_same_relative_and_absolute():
    assert scope.check_expect({"bodies": "same"}, change(before=2, after=2)) == []
    assert scope.check_expect({"bodies": "+1"}, change(before=2, after=3)) == []
    assert scope.check_expect({"bodies": "-2"}, change(before=3, after=1)) == []
    assert scope.check_expect({"bodies": 3}, change(before=1, after=3)) == []
    assert scope.check_expect({"bodies": "3"}, change(before=1, after=3)) == []
    (failed,) = scope.check_expect({"bodies": "same"}, change(before=2, after=3))
    assert "2 bodies were expected" in failed and "there are 3" in failed
    assert len(scope.check_expect({"bodies": "+1"}, change(before=2, after=2))) == 1
    assert len(scope.check_expect({"bodies": "-2"}, change(before=3, after=2))) == 1
    assert len(scope.check_expect({"bodies": 3}, change(before=3, after=2))) == 1


def test_expect_changed_bodies_goes_by_the_names_before_the_call():
    rows = [{"body": "Cut1", "was": "Boss1", "before_mm3": 100.0, "after_mm3": 90.0, "delta_mm3": -10.0},
            {"body": "Other", "before_mm3": 50.0, "after_mm3": 50.0, "delta_mm3": 0.0}]
    assert scope.check_expect({"changed_bodies": ["Boss1"]}, change(rows=rows)) == []
    assert scope.check_expect({"changed_bodies": "Boss1"}, change(rows=rows)) == []  # one name as a string
    (failed,) = scope.check_expect({"changed_bodies": ["Cut1"]}, change(rows=rows))  # its name after the call
    assert "['Cut1']" in failed and "['Boss1']" in failed
    assert len(scope.check_expect({"changed_bodies": ["Boss1", "Other"]}, change(rows=rows))) == 1
    assert len(scope.check_expect({"changed_bodies": ["Other"]}, change(rows=rows))) == 1


def test_expect_changed_bodies_says_when_none_changed():
    rows = [{"body": "A", "before_mm3": 50.0, "after_mm3": 50.0, "delta_mm3": 0.0}]
    (failed,) = scope.check_expect({"changed_bodies": ["A"]}, change(rows=rows))
    assert failed.endswith("none did")


def test_expect_no_errors_and_fully_defined():
    clean = change(whats_wrong=[], new_under_defined_sketches=[])
    assert scope.check_expect({"no_errors": True, "fully_defined": True}, clean) == []
    broken = change(whats_wrong=[{"feature": "Cut1", "error": "dangling"}], new_under_defined_sketches=["Sketch7"])
    failed = scope.check_expect({"no_errors": True, "fully_defined": True}, broken)
    assert len(failed) == 2 and "Cut1" in failed[0] and "Sketch7" in failed[1]
    assert scope.check_expect({"no_errors": False, "fully_defined": False}, broken) == []  # not asked for


def test_every_unmet_expectation_is_reported():
    rows = [{"body": "A", "before_mm3": 50.0, "after_mm3": 60.0, "delta_mm3": 10.0}]
    failed = scope.check_expect({"volume": "decrease", "volume_mm3": [-20, -5], "bodies": "+1",
                                 "changed_bodies": ["B"], "no_errors": True, "fully_defined": True},
                                change(10.0, rows=rows, whats_wrong=["x"], new_under_defined_sketches=["S"]))
    assert len(failed) == len(scope.EXPECT_KEYS) == 6


def test_expect_with_an_unknown_key_is_refused():
    with pytest.raises(SolidWorksError, match="Unknown expect key") as err:
        scope.check_expect({"volume": "decrease", "faces": 3}, change(-5.0))
    assert "'faces'" in str(err.value) and all(key in str(err.value) for key in scope.EXPECT_KEYS)


@pytest.mark.parametrize("expect", ["decrease", ["volume"], None, 5])
def test_expect_must_be_a_dict(expect):
    with pytest.raises(SolidWorksError, match="expect is a dict"):
        scope.check_expect(expect, change())


def test_an_empty_expect_asks_for_nothing():
    assert scope.check_expect({}, change(123.0, before=1, after=4)) == []


@pytest.mark.parametrize("bodies", ["more", "one", "+", "1.5"])
def test_expect_bodies_that_is_no_count_is_refused(bodies):
    with pytest.raises(SolidWorksError):
        scope.check_expect({"bodies": bodies}, change())


@pytest.mark.parametrize("volume_mm3", [-100, [-100], [-120, -100, -80], ["a", "b"]])
def test_expect_volume_mm3_that_is_no_pair_of_numbers_is_refused(volume_mm3):
    with pytest.raises(SolidWorksError):
        scope.check_expect({"volume_mm3": volume_mm3}, change(-100.0))
