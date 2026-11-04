"""features: the direction flag and end condition are decided, not searched."""

import pytest

from solidworks_mcp.constants import SW_END_COND_BLIND, SW_END_COND_THROUGH_ALL, SW_END_COND_UP_TO_SURFACE
from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import features

UP = [0.0, 0.0, 1.0]


@pytest.mark.parametrize("toward, cut, expected", [
    ([0, 0, 1], False, False),   # boss along the normal: SolidWorks' own direction
    ([0, 0, -1], False, True),   # boss against it: reversed
    ([0, 0, -1], True, False),   # cut against the normal: SolidWorks' own direction
    ([0, 0, 1], True, True),     # cut along it: reversed
    ([0.3, 0.2, -5], True, False),  # only the sign along the normal counts
])
def test_reverse_flag(toward, cut, expected):
    assert features.reverse_flag(toward, UP, cut) is expected


def test_reverse_flag_refuses_in_plane_and_bad_input():
    with pytest.raises(SolidWorksError, match="lies in the sketch plane"):
        features.reverse_flag([1, 0, 0], UP, True)
    with pytest.raises(SolidWorksError, match=r"\[x, y, z\]"):
        features.reverse_flag([1, 0], UP, True)


def test_end_condition():
    assert features.end_condition(12.5, False, None) == (SW_END_COND_BLIND, pytest.approx(0.0125))
    assert features.end_condition(None, True, None) == (SW_END_COND_THROUGH_ALL, 0.0)
    assert features.end_condition(None, False, "+z") == (SW_END_COND_UP_TO_SURFACE, 0.0)


def test_end_condition_wants_exactly_one():
    with pytest.raises(SolidWorksError, match="exactly one"):
        features.end_condition(None, False, None)
    with pytest.raises(SolidWorksError, match="exactly one"):
        features.end_condition(5, True, None)
    with pytest.raises(SolidWorksError, match="> 0"):
        features.end_condition(-1, False, None)


def test_end_condition_offset_from_surface():
    assert features.end_condition(None, False, "+z", 6.0) == (features.SW_END_COND_OFFSET_FROM_SURFACE,
                                                               pytest.approx(0.006))
    with pytest.raises(SolidWorksError, match="needs up_to"):
        features.end_condition(None, False, None, 6.0)
    with pytest.raises(SolidWorksError, match="> 0"):
        features.end_condition(None, False, "+z", 0.0)
