"""sketch_datum: what can be a datum, the datum and corner tokens, origin relations; context.bar_is_before (pure)."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import context, sketch_datum

X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)


def turned(degrees):
    """A unit vector in the xy plane, `degrees` from x."""
    return (math.cos(math.radians(degrees)), math.sin(math.radians(degrees)), 0.0)


# --- unit, dot ------------------------------------------------------------------------------

def test_unit_and_dot():
    assert sketch_datum.unit([0, 3, 4]) == pytest.approx((0.0, 0.6, 0.8))
    assert sketch_datum.unit(("2", 0, 0)) == (1.0, 0.0, 0.0)  # anything float() takes
    assert sketch_datum.dot((1, 2, 3), (4, -5, 6)) == 12
    assert sketch_datum.dot(X, Y) == 0.0


def test_unit_refuses_a_zero_vector():
    with pytest.raises(SolidWorksError, match="non-zero vector"):
        sketch_datum.unit([0, 0, 0])


# --- what can be a datum --------------------------------------------------------------------

def test_edge_across_the_measured_axis_is_a_datum():
    assert sketch_datum.edge_is_datum_for(Y, X)
    assert sketch_datum.edge_is_datum_for(Z, X)
    assert sketch_datum.edge_is_datum_for((0, -25, 0), (3, 0, 0))  # lengths and sense do not matter
    assert sketch_datum.edge_is_datum_for((0, 1, 1), X)


def test_edge_along_or_oblique_to_the_measured_axis_is_no_datum():
    assert not sketch_datum.edge_is_datum_for(X, X)
    assert not sketch_datum.edge_is_datum_for((-1, 0, 0), X)
    assert not sketch_datum.edge_is_datum_for(turned(45), X)
    assert not sketch_datum.edge_is_datum_for(turned(80), X)


def test_edge_within_a_degree_of_perpendicular_still_counts():
    assert sketch_datum.edge_is_datum_for(turned(89), X)
    assert sketch_datum.edge_is_datum_for(turned(91), X)
    assert not sketch_datum.edge_is_datum_for(turned(87), X)


def test_face_whose_normal_runs_along_the_measured_axis_is_a_datum():
    assert sketch_datum.face_is_datum_for(X, X)
    assert sketch_datum.face_is_datum_for((-1, 0, 0), X)  # either side of the wall
    assert sketch_datum.face_is_datum_for((0, 0, 7), (0, 0, 0.5))
    assert sketch_datum.face_is_datum_for(turned(1), X)


def test_face_seen_from_the_front_or_tilted_is_no_datum():
    assert not sketch_datum.face_is_datum_for(Y, X)
    assert not sketch_datum.face_is_datum_for(Z, X)
    assert not sketch_datum.face_is_datum_for(turned(45), X)
    assert not sketch_datum.face_is_datum_for(turned(3), X)


def test_datum_checks_refuse_a_zero_vector():
    with pytest.raises(SolidWorksError):
        sketch_datum.edge_is_datum_for((0, 0, 0), X)
    with pytest.raises(SolidWorksError):
        sketch_datum.face_is_datum_for(X, (0, 0, 0))


# --- parse_point ----------------------------------------------------------------------------

def test_point_from_at_text():
    assert sketch_datum.parse_point("@1,2,3") == [1.0, 2.0, 3.0]
    assert sketch_datum.parse_point("  @1.5, -2 ,3e1 ") == [1.5, -2.0, 30.0]
    assert sketch_datum.parse_point("@0,0,0") == [0.0, 0.0, 0.0]


def test_point_from_three_numbers():
    assert sketch_datum.parse_point([1, 2.5, -3]) == [1.0, 2.5, -3.0]
    assert sketch_datum.parse_point((0, 0, 0)) == [0.0, 0.0, 0.0]
    assert all(isinstance(c, float) for c in sketch_datum.parse_point([1, 2, 3]))


@pytest.mark.parametrize("token", [[True, 2, 3], [1, 2, False], [1, 2], [1, 2, 3, 4], ["1", 2, 3], [None, 0, 0],
                                   ["#2", "#3"], []])
def test_list_that_is_not_three_numbers_is_no_point(token):
    assert sketch_datum.parse_point(token) is None


@pytest.mark.parametrize("token", ["@1,2", "@1,2,3,4", "@a,b,c", "@", "@1;2;3", "@1,,3"])
def test_bad_at_text_is_refused(token):
    with pytest.raises(SolidWorksError, match="A point is '@x,y,z'"):
        sketch_datum.parse_point(token)


@pytest.mark.parametrize("token", ["+x", "-y:inner", "#5", "edge:5", "ref:0123456789ab", "origin", "1,2,3", "", None,
                                   5])
def test_other_tokens_are_no_point(token):
    assert sketch_datum.parse_point(token) is None


# --- parse_edge_index -----------------------------------------------------------------------

@pytest.mark.parametrize("token, index", [("edge:5", 5), ("edge#5", 5), ("edge 5", 5), ("Edge: 12", 12),
                                          ("  EDGE # 0 ", 0), ("edge5", 5), ("edge:007", 7)])
def test_edge_index_from_an_edge_token(token, index):
    assert sketch_datum.parse_edge_index(token) == index


@pytest.mark.parametrize("token", ["5", "#5", 5, "edge", "edge:", "edge:-1", "edge:1.5", "edge:5,6", "edges:5",
                                   "face:5", "ref:0123456789ab", "+x", "", None])
def test_other_tokens_are_no_edge_index(token):
    assert sketch_datum.parse_edge_index(token) is None


# --- origin_axis, describe_status -----------------------------------------------------------

def test_distance_to_the_origin_fixes_the_axis_it_is_measured_along():
    distance = sketch_datum.SW_CONSTRAINT_DISTANCE
    assert sketch_datum.origin_axis(distance, sketch_datum.SW_DIM_HORIZONTAL) == "x"
    assert sketch_datum.origin_axis(distance, sketch_datum.SW_DIM_VERTICAL) == "y"
    assert sketch_datum.origin_axis(distance, None) is None
    assert sketch_datum.origin_axis(distance, 1) is None  # an aligned distance: not one this module replaces


def test_relations_to_the_origin_fix_x_y_or_both():
    assert sketch_datum.origin_axis(sketch_datum.SW_CONSTRAINT_VERTICAL_POINTS, None) == "x"
    assert sketch_datum.origin_axis(sketch_datum.SW_CONSTRAINT_HORIZONTAL_POINTS, None) == "y"
    assert sketch_datum.origin_axis(sketch_datum.SW_CONSTRAINT_COINCIDENT, None) == "xy"
    # the dimension type only matters for a distance
    assert sketch_datum.origin_axis(sketch_datum.SW_CONSTRAINT_COINCIDENT, sketch_datum.SW_DIM_VERTICAL) == "xy"


@pytest.mark.parametrize("kind", [sketch_datum.SW_CONSTRAINT_ANGLE, sketch_datum.SW_CONSTRAINT_RADIUS,
                                  sketch_datum.SW_CONSTRAINT_DIAMETER, 0, 999])
def test_other_relations_fix_no_position(kind):
    assert sketch_datum.origin_axis(kind, sketch_datum.SW_DIM_HORIZONTAL) is None


def test_status_names():
    assert sketch_datum.describe_status(sketch_datum.SW_FULLY_CONSTRAINED) == "fully defined"
    assert sketch_datum.describe_status(sketch_datum.SW_UNDER_CONSTRAINED) == "under defined"
    assert sketch_datum.describe_status(4) == "over defined"
    assert sketch_datum.describe_status("3") == "fully defined"
    assert sketch_datum.describe_status(42) == "status 42"


# --- _corner_items --------------------------------------------------------------------------

def test_one_corner_token_is_one_item():
    assert sketch_datum._corner_items("#3.end") == ["#3.end"]
    assert sketch_datum._corner_items("@10,0,5") == ["@10,0,5"]


def test_pair_of_segment_tokens_is_one_corner():
    assert sketch_datum._corner_items(["#2", "#3"]) == [["#2", "#3"]]
    assert sketch_datum._corner_items(("#2", "#3")) == [["#2", "#3"]]
    assert sketch_datum._corner_items(["2", " #3 "]) == [["2", " #3 "]]


def test_model_point_is_one_corner():
    assert sketch_datum._corner_items([10, 0, 5]) == [[10, 0, 5]]
    assert sketch_datum._corner_items((10.0, 0.5, -5)) == [[10.0, 0.5, -5]]


def test_list_of_several_corners_stays_a_list():
    several = ["#1.end", ["#2", "#3"], [10, 0, 5], "@1,2,3"]
    assert sketch_datum._corner_items(several) == several
    assert sketch_datum._corner_items(["#1.end", "#3.start"]) == ["#1.end", "#3.start"]  # two ends, not a pair
    assert sketch_datum._corner_items(["#1", "#2", "#3"]) == ["#1", "#2", "#3"]
    assert sketch_datum._corner_items([["#2", "#3"], ["#4", "#5"]]) == [["#2", "#3"], ["#4", "#5"]]
    assert sketch_datum._corner_items([[10, 0, 5]]) == [[10, 0, 5]]
    assert sketch_datum._corner_items([[10, 0, 5], [20, 0, 5]]) == [[10, 0, 5], [20, 0, 5]]


def test_no_corner_is_refused():
    with pytest.raises(SolidWorksError, match="corners is empty"):
        sketch_datum._corner_items([])
    with pytest.raises(SolidWorksError, match="corners is empty"):
        sketch_datum._corner_items(())


# --- context.bar_is_before ------------------------------------------------------------------

NAMES = ["Base", "Sketch2", "Boss", "Sketch3", "3DSketch1", "Sweep", "Fillet"]
KINDS = ["Extrusion", "ProfileFeature", "Extrusion", "ProfileFeature", "3DProfileFeature", "Sweep", "Fillet"]


def test_bar_exactly_before_the_feature():
    for wanted, name in enumerate(NAMES):
        assert context.bar_is_before(NAMES, KINDS, wanted, name)


def test_bar_before_the_sketch_the_feature_absorbed():
    assert context.bar_is_before(NAMES, KINDS, NAMES.index("Boss"), "Sketch2")


def test_bar_before_several_absorbed_sketches_2d_and_3d():
    sweep = NAMES.index("Sweep")
    assert context.bar_is_before(NAMES, KINDS, sweep, "3DSketch1")
    assert context.bar_is_before(NAMES, KINDS, sweep, "Sketch3")


def test_bar_is_not_there_when_a_real_feature_lies_in_between():
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Boss"), "Base")
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Sweep"), "Boss")
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Sweep"), "Sketch2")  # Boss lies in between
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Fillet"), "Sweep")


def test_bar_after_the_feature_is_not_before_it():
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Boss"), "Sketch3")
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Sketch2"), "Boss")
    assert not context.bar_is_before(NAMES, KINDS, 0, "Fillet")


@pytest.mark.parametrize("bar_name", ["Nope", "boss", "", None])
def test_unknown_bar_name_is_never_the_place(bar_name):
    assert not context.bar_is_before(NAMES, KINDS, NAMES.index("Boss"), bar_name)
