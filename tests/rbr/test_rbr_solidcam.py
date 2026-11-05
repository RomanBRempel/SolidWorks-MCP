"""Pure helpers of rbr.solidcam: the part zero corner and the G-code check."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import solidcam

BOX = {"min_mm": [0, 0, 0], "max_mm": [100, 60, 20]}


def test_zero_on_top_bottom_left():
    assert solidcam.zero_corner(BOX) == [0, 0, 20]


def test_zero_other_up_directions():
    assert solidcam.zero_corner(BOX, "+y") == [0, 60, 0]
    assert solidcam.zero_corner(BOX, "-z") == [0, 0, 0]


@pytest.mark.parametrize("bad", ["z", "+w", "up", "++z"])
def test_zero_bad_direction(bad):
    with pytest.raises(SolidWorksError):
        solidcam.zero_corner(BOX, bad)


# The program SolidCAM 2025 posted for template jobs on the target alone: no moves.
EMPTY = """%
O1000 (SC_TEST_CAM2)
N110 M06 T2 ()
N114 G00 G54 G90 X142857. Y142857. S3500 M03
N116 G43 H2 Z20.
N118 M05
%"""

CUTS = """%
N10 G00 X0 Y0
N20 G01 Z-2. F300
N30 G1 X50.
N40 G03 X60. Y10. R10.
%"""


def test_gcode_without_moves_is_flagged():
    got = solidcam.gcode_check(EMPTY)
    assert got["has_toolpath"] is False
    assert got["placeholder_positions"] == 1


def test_drill_cycle_counts_as_cutting():
    assert solidcam.gcode_check("N10 G83 X10. Y10. Z-12. R2. Q2. F100\nN20 G80")["has_toolpath"] is True


def test_gcode_with_moves():
    got = solidcam.gcode_check(CUTS)
    assert got["has_toolpath"] is True
    assert got["motion_blocks"] == 3
