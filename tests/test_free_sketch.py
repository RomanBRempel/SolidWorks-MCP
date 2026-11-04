"""Pure unit tests for free sketches: building a chain of lines, arcs and splines,
and planning the relations and dimensions that fully define it (no SolidWorks).

The plan must remove every degree of freedom exactly once: one constraint too
few leaves the sketch under-defined, one too many over-defines it and
SolidWorks flags it. Relations go only where the input already holds them, so
nothing moves; the dimensions measure from the origin.
"""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.free_sketch import build_chain, plan_chain

STADIUM = [{"line": [40, 0]}, {"arc": [40, 20], "tangent": True}, {"line": [0, 20]}, {"arc": [0, 0], "tangent": True}]


def kinds(plan, kind):
    return [r for r in plan.relations if r[0] == kind]


def test_a_tangent_arc_turns_the_way_the_end_point_lies():
    chain = build_chain([0, 0], STADIUM)
    assert chain.closed and chain.user_points == [0, 1, 2, 3]
    (_, s1, e1, c1, d1), (_, s2, e2, c2, d2) = [e for e in chain.entities if e[0] == "arc"]
    assert chain.points[c1] == pytest.approx((40, 10)) and d1 == 1, "left turn from +x up to (40, 20): counterclockwise"
    assert chain.points[c2] == pytest.approx((0, 10)) and d2 == 1
    assert e2 == 0, "the last arc closes the chain on the start point"


def test_a_tangent_arc_can_turn_right():
    chain = build_chain([0, 0], [{"line": [40, 0]}, {"arc": [40, -20], "tangent": True}])
    _, _, _, centre, direction = chain.entities[1]
    assert chain.points[centre] == pytest.approx((40, -10)) and direction == -1
    assert not chain.closed


def test_a_three_point_arc_goes_through_its_middle_point():
    chain = build_chain([10, 0], [{"arc": [-10, 0], "through": [0, 10]}, {"line": [10, 0]}])
    _, _, _, centre, direction = chain.entities[0]
    assert chain.points[centre] == pytest.approx((0, 0)) and direction == 1
    assert chain.closed


def test_a_spline_adds_its_through_points():
    chain = build_chain([0, 0], [{"line": [40, 0]}, {"spline": [[45, 10], [30, 20]]}, {"line": [0, 0]}])
    assert [chain.points[i] for i in chain.user_points] == [(0, 0), (40, 0), (45, 10), (30, 20)]
    assert chain.entities[1] == ("spline", [1, 2, 3])


def test_a_tangent_line_lands_on_the_tangent():
    """Within a micrometre the end is put on the tangent, as asked."""
    chain = build_chain([0, 0], [{"line": [40, 0]}, {"arc": [40, 20], "tangent": True},
                                 {"line": [10, 20.0004], "tangent": True}])
    assert chain.points[chain.user_points[3]] == pytest.approx((10, 20), abs=1e-12)


@pytest.mark.parametrize("segments,match", [
    ([{"arc": [10, 10], "tangent": True}], "first segment"),
    ([{"line": [40, 0]}, {"arc": [60, 0], "tangent": True}], "straight"),
    ([{"line": [40, 0]}, {"arc": [60, 0], "through": [50, 0]}], "in one line"),
    ([{"line": [40, 0]}, {"arc": [40, 20], "tangent": True}, {"line": [10, 21], "tangent": True}],
     r"does not continue.*\(-1, 0\)"),
    ([{"line": [0, 0]}], "zero length"),
    ([{"curve": [10, 0]}], "line, arc or spline"),
    ([{"line": [40, 0]}, {"arc": [40, 20]}], "through.*tangent"),
    ([{"spline": [[10, 5], [20, 0]]}, {"arc": [30, 10], "tangent": True}], "spline"),
    ([], "at least one segment"),
    ([{"line": [40, 0]}, {"line": [40, 20]}, {"line": [40, 0]}], "twice"),
], ids=["first tangent", "straight arc", "collinear arc", "off the tangent", "zero length", "unknown",
        "arc without how", "tangent after spline", "empty", "crosses itself"])
def test_a_chain_refuses_what_it_cannot_draw(segments, match):
    with pytest.raises(SolidWorksError, match=match):
        build_chain([0, 0], segments)


def test_a_stadium_needs_only_its_length_and_height():
    """Tangent where the arcs meet the sides, sides horizontal, start on the
    origin: two dimensions are left, and changing either keeps the shape."""
    plan = plan_chain(build_chain([0, 0], STADIUM))
    assert len(kinds(plan, "tangent")) == 4
    assert len(kinds(plan, "horizontal")) == 2
    assert kinds(plan, "at_origin") == [("at_origin", 0)]
    assert [(d[0], d[2]) for d in plan.dimensions] == [("x", 40), ("y", 20)]
    assert plan.fully_defined


def test_a_free_arc_gets_a_radius():
    """An arc that is tangent to nothing needs its radius dimensioned."""
    chain = build_chain([0, 0], [{"line": [40, 0]}, {"arc": [40, 20], "through": [45, 10]}, {"line": [0, 20]},
                                 {"line": [0, 0]}])
    plan = plan_chain(chain)
    assert not kinds(plan, "tangent")
    assert ("radius", 1, pytest.approx(12.5)) in [(d[0], d[1], d[2]) for d in plan.dimensions]
    assert plan.fully_defined


def test_concentric_arcs_whose_chords_share_a_bisector_get_a_radius():
    """A bent slot: two concentric arcs joined by lines on one straight line.
    Both chords then share one bisector, so the arcs' ends barely hold their
    shared centre (a rank residual of 2e-6, from the inputs' rounding), and
    SolidWorks saw it free. A radius holds it instead of the last coordinate."""
    chain = build_chain([97.8839, 9.2771], [{"line": [99.4276, 13.9118]},
                                            {"arc": [90.3697, -13.2832], "through": [107.31, 1.01]},
                                            {"line": [91.9134, -8.6485]},
                                            {"arc": [97.8839, 9.2771], "through": [102.51, 1.01]}])
    plan = plan_chain(chain)
    assert [d[0] for d in plan.dimensions].count("radius") == 1 and len(plan.dimensions) == 8
    assert plan.fully_defined


def test_a_relation_that_follows_from_others_is_left_out():
    """A straight continuation is collinear with the line before it; its own
    horizontal relation would then over-define the sketch."""
    plan = plan_chain(build_chain([0, 0], [{"line": [20, 0]}, {"line": [40, 0], "tangent": True},
                                           {"line": [40, 10]}, {"line": [0, 10]}, {"line": [0, 0]}]))
    assert len(kinds(plan, "collinear")) == 1
    assert len(kinds(plan, "horizontal")) == 2, "the first side and the top, not the continued side"
    assert plan.fully_defined


def test_spline_points_are_dimensioned():
    plan = plan_chain(build_chain([0, 0], [{"line": [40, 0]}, {"spline": [[45, 10], [30, 20]]}, {"line": [0, 0]}]))
    dimensioned = {(d[0], d[1]) for d in plan.dimensions}
    assert {("x", 2), ("y", 2), ("x", 3), ("y", 3)} <= dimensioned
    assert plan.fully_defined


def test_the_plan_anchors_to_where_the_model_origin_projects():
    """SolidWorks ties origin relations and dimensions to the PROJECTED model
    origin, which on a plane that does not contain it sits away from the
    sketch's (0, 0): with origin_mm given, 'at zero' must mean 'at that
    projection', or a u = 0 point gets tied to the projection and jumps."""
    chain = build_chain([0, 20], [{"line": [15, 35]}])
    plain = plan_chain(chain)
    assert kinds(plain, "origin_x") == [("origin_x", 0)], "u = 0 anchors to the origin's vertical"
    shifted = plan_chain(chain, origin_mm=(7.0, 0.0))
    assert not kinds(shifted, "origin_x") and not kinds(shifted, "at_origin")
    assert ("x", 0, pytest.approx(-7.0)) in [(d[0], d[1], d[2]) for d in shifted.dimensions]
    assert shifted.fully_defined


def test_a_point_on_the_projected_origin_is_tied_there():
    chain = build_chain([5, 3], [{"line": [25, 3]}])
    plan = plan_chain(chain, origin_mm=(5.0, 3.0))
    assert kinds(plan, "at_origin") == [("at_origin", 0)]
    assert kinds(plan, "horizontal") == [("horizontal", 0)]
    assert plan.fully_defined


def test_points_on_the_axes_get_relations_not_zero_dimensions():
    plan = plan_chain(build_chain([0, 10], [{"line": [30, 10]}, {"line": [30, 0]}, {"line": [0, 10]}]))
    assert ("origin_x", 0) in plan.relations and ("origin_y", 2) in plan.relations
    assert all(abs(d[2]) > 0 for d in plan.dimensions)
    assert plan.fully_defined


def test_a_plan_removes_every_degree_of_freedom_once():
    """Whatever the chain, the constraints add up to two per point."""
    chain = build_chain([5, 5], [{"line": [45, 5]}, {"arc": [55, 15], "tangent": True},
                                 {"arc": [65, 25], "tangent": True}, {"spline": [[50, 40], [20, 35]]},
                                 {"arc": [5, 5], "through": [8, 22]}])
    plan = plan_chain(chain)
    removed = sum(2 if r[0] == "at_origin" else 1 for r in plan.relations) + len(plan.dimensions)
    inherent = sum(1 for e in chain.entities if e[0] == "arc")
    assert removed + inherent == 2 * len(chain.points)
    assert plan.fully_defined


def test_roles_follow_the_points_and_segments_given():
    chain = build_chain([0, 0], [{"line": [40, 0]}, {"arc": [40, 20], "through": [45, 10]}, {"line": [0, 20]},
                                 {"line": [0, 0]}])
    plan = plan_chain(chain)
    assert {plan.role(d) for d in plan.dimensions} == {"x1", "x2", "y2", "r1"}


def test_an_arc_round_a_given_centre_goes_the_short_way():
    left = build_chain([10, 0], [{"arc": [0, 10], "center": [0, 0]}, {"line": [10, 0]}])
    _, _, _, centre, direction = left.entities[0]
    assert left.points[centre] == (0.0, 0.0) and direction == 1
    right = build_chain([0, 10], [{"arc": [10, 0], "center": [0, 0]}, {"line": [0, 10]}])
    assert right.entities[0][4] == -1


def test_an_arc_end_a_hair_off_its_circle_is_put_on_it():
    """An 8 degree wedge with its end rounded to 3 decimals lies 0.07 um inside R30."""
    chain = build_chain([30, 0], [{"arc": [29.708, 4.175], "center": [0, 0]}, {"line": [0, 0]}, {"line": [30, 0]}])
    assert math.hypot(*chain.points[1]) == pytest.approx(30, abs=1e-12)


@pytest.mark.parametrize("segments,match", [
    ([{"arc": [0, 11], "center": [0, 0]}], "not on one circle"),
    ([{"arc": [-10, 0], "center": [0, 0]}], "half circle"),
    ([{"line": [20, 0]}, {"arc": [20, 10], "center": [20, 5], "tangent": True}], "center or tangent"),
], ids=["off the circle", "half circle", "both"])
def test_an_arc_round_a_centre_refuses_what_is_unclear(segments, match):
    with pytest.raises(SolidWorksError, match=match):
        build_chain([10, 0], segments)


def test_centres_closer_than_solidworks_tells_apart_are_one_point():
    """Through points rounded to 5 decimals put the two centres of a ring 2 um
    off the origin and 0.02 um apart; SolidWorks merges such points, so the
    chain does too, and a centre that close to the origin goes onto it."""
    c, d = 7.07107, 14.14214  # 10 and 20 times cos 45, rounded
    chain = build_chain([10, 0], [{"arc": [0, 10], "through": [c, c]}, {"line": [0, 20]},
                                  {"arc": [20, 0], "through": [d, d]}, {"line": [10, 0]}])
    centres = {e[3] for e in chain.entities if e[0] == "arc"}
    assert len(centres) == 1 and chain.points[centres.pop()] == (0.0, 0.0)
    plan = plan_chain(chain)
    assert ("at_origin", 4) in plan.relations and plan.fully_defined
