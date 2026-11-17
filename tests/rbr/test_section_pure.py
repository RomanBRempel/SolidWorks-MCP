"""section: the plane's frame, arcs as polylines, chaining a loop's edges, area and inside (pure)."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import section

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]  # counter-clockwise


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def length_of(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


# --- plane_frame ----------------------------------------------------------------------------

@pytest.mark.parametrize("normal, expected", [
    ("+x", (1.0, 0.0, 0.0)), ("x", (1.0, 0.0, 0.0)), ("-y", (0.0, -1.0, 0.0)), ("z", (0.0, 0.0, 1.0)),
    (" -Z ", (0.0, 0.0, -1.0)), ([0, 0, 5], (0.0, 0.0, 1.0)), ((3, 0, -4), (0.6, 0.0, -0.8))])
def test_normal_from_an_axis_name_or_a_vector(normal, expected):
    assert section.plane_frame([0, 0, 0], normal)["normal"] == pytest.approx(expected)


@pytest.mark.parametrize("normal", ["+x", "-x", "+y", "-y", "+z", "-z", [1, 1, 1], [1, -2, 3], [0, 1e-3, -1],
                                    [-5, 0.2, 0]])
def test_frame_is_orthonormal_and_right_handed(normal):
    frame = section.plane_frame([1, 2, 3], normal)
    u, v, n = frame["u"], frame["v"], frame["normal"]
    for axis in (u, v, n):
        assert math.sqrt(dot(axis, axis)) == pytest.approx(1.0)
    assert dot(u, v) == pytest.approx(0.0, abs=1e-12)
    assert dot(u, n) == pytest.approx(0.0, abs=1e-12)
    assert dot(v, n) == pytest.approx(0.0, abs=1e-12)
    assert cross(u, v) == pytest.approx(n, abs=1e-12)  # right and up, seen from the normal's side


def test_frame_keeps_the_point_as_its_origin():
    frame = section.plane_frame([1, 2.5, -3], "z")
    assert frame["origin"] == (1.0, 2.5, -3.0)
    assert all(isinstance(c, float) for c in frame["origin"])


def test_frame_looks_at_the_cut_with_the_models_y_up():
    front = section.plane_frame([0, 0, 0], "+z")
    assert (front["u"], front["v"]) == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    right = section.plane_frame([0, 0, 0], "+x")  # seen from +x the model's -z runs to the right
    assert right["u"] == pytest.approx((0.0, 0.0, -1.0)) and right["v"] == pytest.approx((0.0, 1.0, 0.0))
    top = section.plane_frame([0, 0, 0], "+y")    # as SolidWorks' top view: x to the right, -z up
    assert top["u"] == pytest.approx((1.0, 0.0, 0.0)) and top["v"] == pytest.approx((0.0, 0.0, -1.0))


@pytest.mark.parametrize("point, normal", [([0, 0], "z"), ([0, 0, 0, 0], "z"), ([0, 0, 0], "w"), ([0, 0, 0], ""),
                                           ([0, 0, 0], [0, 0, 0]), ([0, 0, 0], [1, 2]), ([0, 0, 0], 5),
                                           ([0, 0, 0], ["a", "b", "c"])])
def test_frame_refuses_a_bad_point_or_normal(point, normal):
    with pytest.raises(SolidWorksError):
        section.plane_frame(point, normal)


def test_uv_and_model_coordinates_round_trip():
    frame = section.plane_frame([10, 20, 30], [1, 2, 2])
    for uv in ((0.0, 0.0), (3.0, -4.0), (-12.5, 7.25)):
        point = section.to_model(frame, uv)
        assert dot([p - o for p, o in zip(point, frame["origin"])], frame["normal"]) == pytest.approx(0.0, abs=1e-9)
        assert section.to_uv(frame, point) == pytest.approx(uv)
    assert section.to_uv(frame, [10, 20, 30]) == pytest.approx((0.0, 0.0))


# --- arc_points -----------------------------------------------------------------------------

def test_quarter_arc_runs_from_start_through_mid_to_end():
    mid = (math.cos(math.pi / 4), math.sin(math.pi / 4))
    points = section.arc_points((0, 0), 1.0, (1, 0), mid, (0, 1))
    assert points[0] == pytest.approx((1, 0)) and points[-1] == pytest.approx((0, 1))
    assert all(math.hypot(*p) == pytest.approx(1.0) for p in points)
    assert all(p[0] >= -1e-12 and p[1] >= -1e-12 for p in points)  # stays in the first quadrant
    assert length_of(points) == pytest.approx(math.pi / 2, rel=1e-3)


def test_arc_goes_the_way_its_mid_point_lies():
    # same ends, the mid point on the other side: three quarters of the circle, clockwise
    mid = (-math.cos(math.pi / 4), -math.sin(math.pi / 4))
    points = section.arc_points((0, 0), 1.0, (1, 0), mid, (0, 1))
    assert points[0] == pytest.approx((1, 0)) and points[-1] == pytest.approx((0, 1))
    assert length_of(points) == pytest.approx(1.5 * math.pi, rel=1e-3)
    assert min(math.dist(p, mid) for p in points) < 0.05
    assert points[1][1] < 0  # leaves the start downwards


def test_arc_across_the_pi_seam():
    start = (math.cos(math.radians(170)), math.sin(math.radians(170)))
    end = (math.cos(math.radians(190)), math.sin(math.radians(190)))
    points = section.arc_points((0, 0), 1.0, start, (-1, 0), end)
    assert length_of(points) == pytest.approx(math.radians(20), rel=1e-3)
    assert all(p[0] < 0 for p in points)


def test_arc_off_the_origin_keeps_its_centre_and_radius():
    centre = (5.0, -3.0)
    points = section.arc_points(centre, 2.0, (7, -3), (5, -1), (3, -3))
    assert points[0] == pytest.approx((7, -3)) and points[-1] == pytest.approx((3, -3))
    assert all(math.dist(p, centre) == pytest.approx(2.0) for p in points)
    assert max(p[1] for p in points) == pytest.approx(-1.0, abs=1e-3)


def test_start_equal_to_end_gives_the_full_circle():
    points = section.arc_points((0, 0), 2.0, (2, 0), (2, 0), (2, 0))
    assert points[0] == pytest.approx(points[-1])
    assert length_of(points) == pytest.approx(4 * math.pi, rel=1e-3)
    assert abs(section.polygon_area(points[:-1])) == pytest.approx(4 * math.pi, rel=1e-3)


@pytest.mark.parametrize("radius, chord", [(1.0, section.CHORD_MM), (50.0, section.CHORD_MM), (10.0, 0.5), (3.0, 0.01)])
def test_arc_polyline_stays_within_the_chord_error(radius, chord):
    points = section.arc_points((0, 0), radius, (radius, 0), (0, radius), (-radius, 0), chord)
    sagitta = max(radius - math.hypot((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(points, points[1:]))
    assert 0 < sagitta <= chord * (1 + 1e-6)
    assert len(points) >= 3


def test_coarser_chord_gives_fewer_points():
    fine = section.arc_points((0, 0), 10.0, (10, 0), (0, 10), (-10, 0), 0.001)
    coarse = section.arc_points((0, 0), 10.0, (10, 0), (0, 10), (-10, 0), 0.1)
    assert len(coarse) < len(fine)


def test_arc_smaller_than_the_chord_still_has_points():
    points = section.arc_points((0, 0), 0.0005, (0.0005, 0), (0, 0.0005), (-0.0005, 0))
    assert len(points) >= 3 and points[0] == pytest.approx((0.0005, 0)) and points[-1] == pytest.approx((-0.0005, 0))


# --- chain ----------------------------------------------------------------------------------

def test_chain_orders_edges_head_to_tail_reversing_where_needed():
    edges = [[(0, 0), (10, 0)],          # 0
             [(0, 10), (0, 0)],          # 1: the closing edge
             [(10, 10), (10, 0)],        # 2: runs against the loop
             [(10, 10), (0, 10)]]        # 3
    ordered, order, flipped = section.chain(edges)
    assert order == [0, 2, 3, 1] and flipped == [False, True, False, False]
    assert ordered == [[(0, 0), (10, 0)], [(10, 0), (10, 10)], [(10, 10), (0, 10)], [(0, 10), (0, 0)]]
    assert all(a[-1] == b[0] for a, b in zip(ordered, ordered[1:] + ordered[:1]))


def test_chain_keeps_intermediate_points_of_a_reversed_edge():
    edges = [[(0, 0), (10, 0)], [(0, 0), (2, 6), (8, 6), (10, 0)]]  # two edges closing one loop
    ordered, order, flipped = section.chain(edges)
    assert order == [0, 1] and flipped == [False, True]
    assert ordered[1] == [(10, 0), (8, 6), (2, 6), (0, 0)]


def test_chain_turns_a_first_edge_that_ran_against_the_loop():
    edges = [[(10, 0), (20, 0)], [(0, 0), (10, 0)]]  # the second edge joins the FIRST one's start
    ordered, order, flipped = section.chain(edges)
    assert order == [0, 1] and flipped == [True, True]
    assert ordered == [[(20, 0), (10, 0)], [(10, 0), (0, 0)]]


def test_chain_of_one_edge_and_of_nothing():
    circle = [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 0)]
    assert section.chain([circle]) == ([circle], [0], [False])
    assert section.chain([]) == ([], [], [])


def test_chain_leaves_its_input_alone():
    edges = [[(0, 0), (10, 0)], [(10, 10), (10, 0)], [(10, 10), (0, 0)]]
    copy = [list(e) for e in edges]
    section.chain(edges)
    assert edges == copy


def test_chained_loop_gives_the_polygon_section_profile_builds():
    edges = [[(10, 10), (0, 10)], [(0, 0), (10, 0)], [(0, 10), (0, 0)], [(10, 0), (10, 10)]]
    ordered, _, _ = section.chain(edges)
    polygon = [p for line in ordered for p in line[:-1]]
    assert len(polygon) == 4 and abs(section.polygon_area(polygon)) == pytest.approx(100.0)


# --- polygon_area, inside -------------------------------------------------------------------

def test_area_is_signed_counter_clockwise_positive():
    assert section.polygon_area(SQUARE) == pytest.approx(100.0)
    assert section.polygon_area(SQUARE[::-1]) == pytest.approx(-100.0)
    assert section.polygon_area([(0, 0), (4, 0), (0, 3)]) == pytest.approx(6.0)
    assert section.polygon_area([(1, 1), (5, 1)]) == pytest.approx(0.0)  # no area between two points


def test_area_does_not_depend_on_where_the_polygon_lies_or_starts():
    moved = [(x + 1000.0, y - 500.0) for x, y in SQUARE]
    assert section.polygon_area(moved) == pytest.approx(100.0)
    assert section.polygon_area(SQUARE[2:] + SQUARE[:2]) == pytest.approx(100.0)
    assert section.polygon_area(tuple(SQUARE)) == pytest.approx(100.0)  # a tuple of points works as well


def test_inside_a_convex_polygon():
    assert section.inside(SQUARE, (5, 5)) and section.inside(SQUARE[::-1], (5, 5))
    assert section.inside(SQUARE, (0.001, 9.999))
    for point in ((-1, 5), (11, 5), (5, -1), (5, 11), (20, 20)):
        assert not section.inside(SQUARE, point)


def test_inside_a_concave_polygon():
    u_shape = [(0, 0), (10, 0), (10, 10), (8, 10), (8, 2), (2, 2), (2, 10), (0, 10)]
    assert section.inside(u_shape, (1, 5)) and section.inside(u_shape, (9, 5)) and section.inside(u_shape, (5, 1))
    assert not section.inside(u_shape, (5, 5))  # between the legs
    assert not section.inside(u_shape, (5, 11))


# --- measure_walls (numpy) ------------------------------------------------------------------

def _loop(polygon, outer):
    closed = list(polygon) + [polygon[0]]
    return {"outer": outer, "polygon": list(polygon),
            "segments": [{"type": "line", "polyline": [a, b]} for a, b in zip(closed, closed[1:])]}


def test_hollow_section_measures_the_wall_between_outer_and_inner_contour():
    pytest.importorskip("numpy")
    hole = [(2.0, 2.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0)]
    loops = [_loop(SQUARE, True), _loop(hole[::-1], False)]
    section.measure_walls(loops)
    walls = [seg["wall_mm"] for lp in loops for seg in lp["segments"]]
    assert walls == pytest.approx([2.0] * 8)
    assert loops[0]["segments"][0]["wall_at"] == pytest.approx((5.0, 0.0))  # the middle of the segment


def test_u_section_measures_the_wall_between_two_sides_of_one_contour():
    pytest.importorskip("numpy")
    u_shape = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (8.0, 10.0), (8.0, 2.0), (2.0, 2.0), (2.0, 10.0), (0.0, 10.0)]
    loops = [_loop(u_shape, True)]
    section.measure_walls(loops)
    walls = [seg["wall_mm"] for seg in loops[0]["segments"]]
    #        bottom  right  leg top  inner  floor  inner  leg top  left
    assert walls == pytest.approx([2.0, 2.0, 10.0, 2.0, 2.0, 2.0, 10.0, 2.0])


def test_wall_at_is_the_nearest_boundary_along_the_direction():
    np = pytest.importorskip("numpy")
    starts = np.asarray([(0.0, 2.0), (0.0, 7.0), (20.0, 0.0)])
    ends = np.asarray([(10.0, 2.0), (10.0, 7.0), (20.0, 10.0)])
    assert section.wall_at((5.0, 0.0), (0.0, 1.0), starts, ends) == pytest.approx(2.0)
    assert section.wall_at((5.0, 3.0), (0.0, 1.0), starts, ends) == pytest.approx(4.0)   # what lies behind is not hit
    assert section.wall_at((5.0, 3.0), (1.0, 0.0), starts, ends) == pytest.approx(15.0)
    assert section.wall_at((5.0, 8.0), (0.0, 1.0), starts, ends) is None
