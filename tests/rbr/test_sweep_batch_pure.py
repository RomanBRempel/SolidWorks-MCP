"""sweep: corner radii and the route check; batch: sketch points in model space, set_dimensions input (pure)."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import batch, sweep

L_ROUTE = [[0, 0, 0], [10, 0, 0], [10, 10, 0]]
Z_ROUTE = [[0, 0, 0], [10, 0, 0], [10, 10, 0], [20, 10, 0]]


# --- corner_radii ---------------------------------------------------------------------------

def test_bend_radius_rounds_every_corner():
    assert sweep.corner_radii(3, bend_radius_mm=2.5) == [2.5, 2.5, 2.5]
    assert sweep.corner_radii(2, bend_radius_mm=4) == [4.0, 4.0]


def test_no_radius_leaves_every_corner_sharp():
    assert sweep.corner_radii(2) == [0.0, 0.0]
    assert sweep.corner_radii(0) == []
    assert sweep.corner_radii(0, bend_radius_mm=3) == []  # a straight run has no corner to round


def test_fillets_give_one_radius_per_corner():
    assert sweep.corner_radii(3, fillets=[1, 0, 2.5]) == [1.0, 0.0, 2.5]
    assert sweep.corner_radii(2, fillets=[None, 3]) == [0.0, 3.0]  # None: a sharp corner


def test_fillets_must_match_the_corner_count():
    with pytest.raises(SolidWorksError, match=r"one radius per interior corner: 2 \(got 3\)"):
        sweep.corner_radii(2, fillets=[1, 2, 3])
    with pytest.raises(SolidWorksError, match="one radius per interior corner"):
        sweep.corner_radii(2, fillets=[])


def test_bend_radius_and_fillets_together_are_refused():
    with pytest.raises(SolidWorksError, match="not both"):
        sweep.corner_radii(2, bend_radius_mm=2, fillets=[1, 1])


def test_negative_radius_is_refused():
    with pytest.raises(SolidWorksError, match="cannot be negative"):
        sweep.corner_radii(2, bend_radius_mm=-1)
    with pytest.raises(SolidWorksError, match="cannot be negative"):
        sweep.corner_radii(2, fillets=[1, -0.5])


# --- route_check ----------------------------------------------------------------------------

def test_sharp_route_is_as_long_as_its_runs():
    route = sweep.route_check(L_ROUTE, [0.0])
    assert route == {"length_mm": 20.0, "corner_angles_deg": [90.0], "straight_mm": [10.0, 10.0]}


def test_fillet_shortens_the_route_by_its_tangents_and_adds_its_arc():
    route = sweep.route_check(L_ROUTE, [2.0])
    assert route["length_mm"] == pytest.approx(20 - 2 * 2 + 2 * math.pi / 2, abs=1e-4)
    assert route["corner_angles_deg"] == [90.0]
    assert route["straight_mm"] == [8.0, 8.0]


def test_corner_angle_is_the_turn_from_straight_on():
    assert sweep.route_check([[0, 0, 0], [10, 0, 0], [20, 0, 0]], [0.0])["corner_angles_deg"] == [0.0]
    assert sweep.route_check([[0, 0, 0], [10, 0, 0], [20, 10, 0]], [0.0])["corner_angles_deg"] == [45.0]
    assert sweep.route_check([[0, 0, 0], [10, 0, 0], [0, 10, 0]], [0.0])["corner_angles_deg"] == [135.0]


def test_route_in_space():
    points = [[0, 0, 0], [0, 0, 30], [0, 20, 30], [40, 20, 30]]
    route = sweep.route_check(points, [5.0, 5.0])
    assert route["corner_angles_deg"] == [90.0, 90.0]
    assert route["straight_mm"] == [25.0, 10.0, 35.0]
    assert route["length_mm"] == pytest.approx(90 - 4 * 5 + 2 * 5 * math.pi / 2, abs=1e-4)


def test_shallow_bend_takes_a_short_tangent():
    points = [[0, 0, 0], [10, 0, 0], [20, 10, 0]]  # a 45 degree turn
    route = sweep.route_check(points, [4.0])
    tangent = 4.0 * math.tan(math.radians(22.5))
    assert route["straight_mm"] == pytest.approx([10 - tangent, math.hypot(10, 10) - tangent], abs=1e-4)
    assert route["length_mm"] == pytest.approx(10 + math.hypot(10, 10) - 2 * tangent + 4.0 * math.pi / 4, abs=1e-4)


def test_two_point_route_has_no_corner():
    assert sweep.route_check([[0, 0, 0], [3, 4, 0]], []) == {"length_mm": 5.0, "corner_angles_deg": [],
                                                           "straight_mm": [5.0]}


def test_fillets_may_use_a_run_up_exactly():
    route = sweep.route_check(Z_ROUTE, [5.0, 5.0])
    assert route["straight_mm"] == [5.0, 0.0, 5.0]
    assert route["length_mm"] == pytest.approx(10 + 2 * 5 * math.pi / 2, abs=1e-4)


def test_fillets_that_overrun_a_run_are_refused():
    with pytest.raises(SolidWorksError, match=r"do not fit on run 2 \(10\.000 mm long\).*need 12\.000 mm"):
        sweep.route_check(Z_ROUTE, [6.0, 6.0])
    with pytest.raises(SolidWorksError, match="do not fit on run 1"):
        sweep.route_check(L_ROUTE, [10.5])


def test_one_big_fillet_fits_where_two_do_not():
    assert sweep.route_check(Z_ROUTE, [9.0, 1.0])["straight_mm"] == [1.0, 0.0, 9.0]
    with pytest.raises(SolidWorksError, match="do not fit on run 2"):
        sweep.route_check(Z_ROUTE, [9.0, 1.5])


def test_straight_corner_cannot_be_rounded():
    points = [[0, 0, 0], [10, 0, 0], [20, 0, 0], [20, 10, 0]]
    with pytest.raises(SolidWorksError, match="Corner 1 is straight"):
        sweep.route_check(points, [2.0, 2.0])
    assert sweep.route_check(points, [0.0, 2.0])["corner_angles_deg"] == [0.0, 90.0]


def test_route_doubling_back_cannot_be_rounded():
    with pytest.raises(SolidWorksError, match="doubles back on itself at corner 1"):
        sweep.route_check([[0, 0, 0], [10, 0, 0], [5, 0, 0]], [1.0])


@pytest.mark.parametrize("points", [[], [[0, 0, 0]], [[0, 0], [10, 0]], [[0, 0, 0], [10, 0, 0, 0]]])
def test_route_needs_two_points_in_space(points):
    with pytest.raises(SolidWorksError, match="at least two"):
        sweep.route_check(points, [])


def test_coincident_points_are_refused():
    with pytest.raises(SolidWorksError, match="coincide"):
        sweep.route_check([[0, 0, 0], [10, 0, 0], [10, 0, 0], [10, 10, 0]], [0.0, 0.0])


def test_corner_radii_feed_route_check():
    radii = sweep.corner_radii(len(Z_ROUTE) - 2, bend_radius_mm=3)
    route = sweep.route_check(Z_ROUTE, radii)
    assert route["straight_mm"] == [7.0, 4.0, 7.0] and route["corner_angles_deg"] == [90.0, 90.0]


# --- radius_groups --------------------------------------------------------------------------

def test_equal_radii_share_a_group_and_sharp_corners_have_none():
    assert sweep.radius_groups([2.0, 0.0, 3.0, 2.0]) == [(2.0, [0, 3]), (3.0, [2])]
    assert sweep.radius_groups([0.0, 0.0]) == []
    assert sweep.radius_groups([]) == []


def test_groups_come_in_the_order_of_their_first_corner():
    assert sweep.radius_groups([5.0, 1.0, 5.0, 1.0, 3.0]) == [(5.0, [0, 2]), (1.0, [1, 3]), (3.0, [4])]


def test_radii_equal_to_nine_places_are_one_dimension():
    assert sweep.radius_groups([2.0, 2.0 + 1e-12]) == [(2.0, [0, 1])]
    assert len(sweep.radius_groups([2.0, 2.001])) == 2


# --- batch.to_model -------------------------------------------------------------------------

# a sketch on the Top plane: its x is the model's x, its y the model's -z, its z the model's y
TOP = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0)


def test_3d_sketch_points_are_model_points_in_mm():
    assert batch.to_model(None, (0.01, -0.0025, 0.1)) == [10.0, -2.5, 100.0]
    assert batch.to_model(None, (0.0123456789, 0.0, 0.0)) == [12.3457, 0.0, 0.0]  # four decimals


def test_identity_transform_only_converts_to_mm():
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    assert batch.to_model(identity, (0.005, 0.002, 0.0)) == [5.0, 2.0, 0.0]


def test_rotation_rows_are_the_sketch_axes_in_the_model():
    transform = TOP + (0.0, 0.0, 0.0, 1.0)
    assert batch.to_model(transform, (0.005, 0.0, 0.0)) == [5.0, 0.0, 0.0]
    assert batch.to_model(transform, (0.0, 0.002, 0.0)) == [0.0, 0.0, -2.0]
    assert batch.to_model(transform, (0.0, 0.0, 0.001)) == [0.0, 1.0, 0.0]


def test_shift_is_added_after_the_rotation():
    transform = TOP + (0.01, 0.02, 0.03, 1.0)
    assert batch.to_model(transform, (0.005, 0.002, 0.0)) == [15.0, 20.0, 28.0]


def test_scale_applies_to_the_point_not_to_the_shift():
    transform = TOP + (0.01, 0.02, 0.03, 2.0)
    assert batch.to_model(transform, (0.005, 0.002, 0.0)) == [20.0, 20.0, 26.0]
    unscaled = TOP + (0.01, 0.02, 0.03, 0.0)  # a scale of 0 means none was stored
    assert batch.to_model(unscaled, (0.005, 0.002, 0.0)) == [15.0, 20.0, 28.0]


def test_to_model_never_answers_minus_zero():
    values = batch.to_model(None, (-0.0, -1e-9, 0.0)) + batch.to_model(TOP + (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
    for value in values:
        assert value == 0.0 and math.copysign(1.0, value) == 1.0


# --- batch.dimension_targets ----------------------------------------------------------------

def test_dimension_targets_are_name_value_pairs_in_order():
    assert batch.dimension_targets({"D1@Boss": 12, "x@SK_A": 3.5, "angle@Sketch2": -30}) == [
        ("D1@Boss", 12.0), ("x@SK_A", 3.5), ("angle@Sketch2", -30.0)]
    targets = batch.dimension_targets({"D1@Boss": 12})
    assert isinstance(targets[0][1], float)


@pytest.mark.parametrize("values", [{}, None, [("D1@Boss", 12)], "D1@Boss=12", 12])
def test_dimension_targets_need_a_filled_dict(values):
    with pytest.raises(SolidWorksError, match="set_dimensions takes"):
        batch.dimension_targets(values)


@pytest.mark.parametrize("value", ["12", None, True, False, [12], float("nan"), float("inf"), -float("inf")])
def test_dimension_value_must_be_a_finite_number(value):
    with pytest.raises(SolidWorksError, match="'D1@Boss' must be a number"):
        batch.dimension_targets({"x@SK": 1.0, "D1@Boss": value})
