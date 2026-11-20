"""sketch_move: picking the segments and checking where the points landed (pure parts)."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import sketch_move as sm

ENTRIES = [
    {"type": "line", "construction": False, "start_mm": [0, 0, 20], "end_mm": [10, 0, 20]},
    {"type": "line", "construction": True, "start_mm": [10, 0, 20], "end_mm": [10, 8, 20]},
    {"type": "circle", "construction": False, "centre_mm": [45, 10, 20], "radius_mm": 3},
    {"type": "arc", "construction": False, "start_mm": [30, 0, 20], "end_mm": [34, 4, 20], "centre_mm": [30, 4, 20]},
    {"type": "spline", "construction": False, "length_mm": 12.0},
]


def test_no_filter_takes_the_whole_sketch():
    assert sm.choose(ENTRIES, None) == [0, 1, 2, 3, 4] and sm.choose(ENTRIES, "all") == [0, 1, 2, 3, 4]


def test_segments_by_token():
    assert sm.choose(ENTRIES, ["#2", "0", "#2"]) == [2, 0]
    assert sm.choose(ENTRIES, "#3") == [3]
    with pytest.raises(SolidWorksError, match="No segment #9"):
        sm.choose(ENTRIES, ["#9"])
    with pytest.raises(SolidWorksError, match="is not a segment"):
        sm.choose(ENTRIES, ["#3.start"])


def test_filter_by_type_and_construction():
    assert sm.choose(ENTRIES, {"type": "line"}) == [0, 1]
    assert sm.choose(ENTRIES, {"type": ["circle", "ARC"]}) == [2, 3]
    assert sm.choose(ENTRIES, {"type": "line", "construction": False}) == [0]
    assert sm.choose(ENTRIES, {"construction": True}) == [1]


def test_filter_by_box_needs_every_point_inside():
    assert sm.choose(ENTRIES, {"within_mm": [[40, 0, 20], [50, 20, 20]]}) == [2]
    assert sm.choose(ENTRIES, {"within_mm": [[12, 10, 20], [-1, -1, 20]]}) == [0, 1]      # corners in any order
    with pytest.raises(SolidWorksError, match="picks no segment"):
        sm.choose(ENTRIES, {"within_mm": [[100, 100, 0], [110, 110, 0]]})
    # a segment without points to test (a spline's entry has only its length here) is never "inside"
    assert 4 not in sm.choose(ENTRIES, {"within_mm": [[-100, -100, 0], [100, 100, 40]]})


@pytest.mark.parametrize("bad", [{"colour": "red"}, {"within_mm": [1, 2]}, {"within_mm": [[0, 0], [1, 1]]}])
def test_a_filter_it_does_not_know_is_refused_with_the_format(bad):
    with pytest.raises(SolidWorksError, match="filter picks the segments|within_mm"):
        sm.choose(ENTRIES, bad)


def test_segment_points_are_what_the_entry_carries():
    assert sm.segment_points(ENTRIES[0]) == [[0, 0, 20], [10, 0, 20]]
    assert sm.segment_points(ENTRIES[2]) == [[45, 10, 20]]
    assert sm.segment_points(ENTRIES[4]) == []


def test_shortfall_names_the_points_that_did_not_arrive():
    before = {(1,): (0.0, 0.0), (2,): (0.010, 0.0), (3,): (0.010, 0.008)}
    moved = {(1,): (0.007, 0.0), (2,): (0.017, 0.0), (3,): (0.010, 0.008)}
    assert sm.shortfall(before, moved, [(1,), (2,)], 0.007, 0.0) == []
    off = sm.shortfall(before, moved, [(1,), (3,)], 0.007, 0.0)
    assert [key for key, *_ in off] == [(3,)] and off[0][1] == pytest.approx(0.007) and off[0][2] == 0.0
    # the rectangle that went twice as far: every point is off by minus the vector
    twice = {key: (u + 0.014, v) for key, (u, v) in before.items()}
    assert all(miss == pytest.approx(-0.007) for _key, miss, _ in sm.shortfall(before, twice, list(before), 0.007, 0.0))
    assert sm.shortfall(before, {}, [(1,)], 0.007, 0.0) == [((1,), 0.007, 0.0)]            # a point that is gone


def test_shortfall_tolerates_the_solvers_noise():
    before, after = {(1,): (0.0, 0.0)}, {(1,): (0.007 + 5e-8, -5e-8)}
    assert sm.shortfall(before, after, [(1,)], 0.007, 0.0) == []
    assert sm.shortfall(before, {(1,): (0.007 + 5e-7, 0.0)}, [(1,)], 0.007, 0.0) != []


def test_bystanders_counts_points_outside_the_selection_that_moved():
    before = {(1,): (0.0, 0.0), (2,): (0.010, 0.0), (3,): (0.010, 0.008)}
    after = {(1,): (0.007, 0.0), (2,): (0.017, 0.0), (3,): (0.010, 0.008)}
    assert sm.bystanders(before, after, [(1,)]) == 1 and sm.bystanders(before, after, [(1,), (2,)]) == 0


def test_a_vector_is_three_finite_numbers():
    assert sm._vector([1, "2", 3.5], "delta_mm") == [1.0, 2.0, 3.5]
    for bad in ([1, 2], "abc", None, [1, 2, float("nan")], [1, 2, "x"]):
        with pytest.raises(SolidWorksError, match="delta_mm is"):
            sm._vector(bad, "delta_mm")
