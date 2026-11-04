"""offset_loop: the pure geometry behind the region-based offset contour."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import offset_loop


SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def test_point_in_polygon():
    assert offset_loop.point_in_polygon((0.5, 0.5), SQUARE)
    assert not offset_loop.point_in_polygon((1.5, 0.5), SQUARE)


def test_polygon_area_either_orientation():
    assert offset_loop.polygon_area(SQUARE) == pytest.approx(1.0)
    assert offset_loop.polygon_area(list(reversed(SQUARE))) == pytest.approx(1.0)


def test_chain_polylines_joins_and_reverses():
    # a lens made of two arcs-as-polylines, the second given backwards
    upper = [(0.0, 0.0), (0.5, 0.4), (1.0, 0.0)]
    lower = [(0.0, 0.0), (0.5, -0.3), (1.0, 0.0)]  # starts where upper starts: must be reversed
    ring = offset_loop.chain_polylines([upper, lower], 1e-9)
    assert len(ring) == 4  # closing point dropped
    assert offset_loop.polygon_area(ring) == pytest.approx(0.35)


def test_chain_polylines_refuses_a_gap():
    with pytest.raises(SolidWorksError, match="do not chain"):
        offset_loop.chain_polylines([[(0, 0), (1, 0)], [(2, 0), (3, 0)]], 1e-9)


def test_interior_point_of_a_thin_lens_is_inside_and_central():
    # a lens 72 mm long and 8 mm tall, like a cavity behind a thin edge
    upper = [(x / 10, 4 * math.sin(math.pi * x / 720)) for x in range(0, 721, 40)]
    lower = [(x / 10, -4 * math.sin(math.pi * x / 720)) for x in range(720, -1, -40)]
    ring = offset_loop.chain_polylines([upper, lower], 1e-9)
    p = offset_loop.interior_point(ring)
    assert offset_loop.point_in_polygon(p, ring)
    assert 20 < p[0] < 52 and abs(p[1]) < 2


def test_interior_point_of_an_l_shape_avoids_the_notch():
    ell = [(0, 0), (10, 0), (10, 2), (2, 2), (2, 10), (0, 10)]
    p = offset_loop.interior_point(ell)
    assert offset_loop.point_in_polygon(p, ell)


def test_interior_point_degenerate():
    assert offset_loop.interior_point([(0, 0), (1, 1)]) is None
