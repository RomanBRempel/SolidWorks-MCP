"""cavity: the pure geometry behind trim-to-box, chaining and sampling."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import cavity


def test_box_bounds_dict_and_corners():
    assert cavity.box_bounds({"x": [114, -114]}) == [(0, -114.0, 114.0)]
    assert cavity.box_bounds({"x": [None, 10], "z": [-5, 5]}) == [(0, None, 10.0), (2, -5.0, 5.0)]
    assert cavity.box_bounds([[-1, None, 0], [1, None, 2]]) == [(0, -1.0, 1.0), (2, 0.0, 2.0)]


@pytest.mark.parametrize("bad", [{}, {"x": [1]}, {"x": [3, 3]}, [[0, 0, 0]], "x", 5])
def test_box_bounds_refuses(bad):
    with pytest.raises(SolidWorksError):
        cavity.box_bounds(bad)


def test_in_box_open_sides_and_tolerance():
    bounds = cavity.box_bounds({"x": [-114, 114], "y": [None, 0]})
    assert cavity.in_box([0, -500, 7], bounds)
    assert cavity.in_box([114.00001, 0, 0], bounds)
    assert not cavity.in_box([114.1, 0, 0], bounds)
    assert not cavity.in_box([0, 0.1, 0], bounds)


def test_outside_runs():
    assert cavity.outside_runs([False, True, True, False, True]) == [(1, 2), (4, 4)]
    assert cavity.outside_runs([False, False]) == []
    assert cavity.outside_runs([True, True]) == [(0, 1)]


def _circle(r, n=64, cx=0.0, cy=0.0):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n + 1)]


def test_rings_one_closed_piece_and_split_pieces():
    whole = _circle(10)
    assert len(cavity.rings([whole], 1e-6)) == 1
    top, bottom = whole[:33], whole[32:]
    rings = cavity.rings([top, list(reversed(bottom))], 1e-6)
    assert len(rings) == 1 and len(rings[0]) == 64


def test_rings_leave_open_pieces_out():
    assert cavity.rings([[(0, 0), (1, 0), (1, 1)]], 1e-6) == []


def test_inside_rings_even_odd():
    outer, hole = cavity.rings([_circle(10)], 1e-6)[0], cavity.rings([_circle(3)], 1e-6)[0]
    assert cavity.inside_rings((5, 0), [outer, hole])
    assert not cavity.inside_rings((0, 0), [outer, hole])
    assert not cavity.inside_rings((20, 0), [outer, hole])


def test_frame_line_on_a_z_plane():
    # sketch on a plane z = 150 with u = x, v = y: x = 114 is the vertical line u = 114
    (a, b) = cavity.frame_line([0, 0, 150], [1, 0, 0], [0, 1, 0], 0, 114.0, ((-140, -130), (160, 170)))
    assert a[0] == pytest.approx(114) and b[0] == pytest.approx(114)
    assert min(a[1], b[1]) < -130 and max(a[1], b[1]) > 170


def test_frame_line_on_a_tilted_plane_and_parallel_side():
    s = math.sqrt(0.5)
    ends = cavity.frame_line([0, 0, 0], [s, 0, s], [0, 1, 0], 0, 10.0, ((-50, -50), (50, 50)))
    for u, v in ends:
        assert u * s == pytest.approx(10.0)  # model x of the line's points
    assert cavity.frame_line([0, 0, 150], [1, 0, 0], [0, 1, 0], 2, 100.0, ((0, 0), (1, 1))) is None


def test_resample_even_by_length():
    pts = cavity.resample([(0, 0), (1, 0), (1, 3)], 4)
    assert len(pts) == 5
    assert pts[0] == (0, 0) and pts[-1] == pytest.approx((1, 3))
    assert pts[1] == pytest.approx((1, 0))  # a quarter of 4 is the corner
    assert cavity.resample([(2, 2), (2, 2)], 3) == [(2, 2)] * 4
