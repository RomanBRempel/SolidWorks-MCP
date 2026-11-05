"""Pure helpers of rbr.bodies: rotation arguments (in SolidWorks' measured
Z, Y, X angle order), where a moved point lands, name pairing and unique names."""

import math

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import bodies


def test_no_rotation_is_all_zero():
    assert bodies.rotation_args(None) == (0.0,) * 6


@pytest.mark.parametrize("axis, slot", [("z", 3), ("y", 4), ("x", 5)])
def test_angle_goes_to_the_measured_slot(axis, slot):
    args = bodies.rotation_args({"axis": axis, "angle_deg": 90, "about_mm": [100, 0, 0]})
    assert args[:3] == (0.1, 0.0, 0.0)
    assert args[slot] == pytest.approx(math.pi / 2)
    assert sum(abs(a) for a in args[3:]) == pytest.approx(math.pi / 2)


@pytest.mark.parametrize("bad", [{"axis": "w", "angle_deg": 5}, {"axis": "x", "angle_deg": 0},
                                 {"axis": "x", "angle_deg": 5, "about_mm": [1, 2]}, [1, 2, 3]])
def test_bad_rotation_refused(bad):
    with pytest.raises(SolidWorksError):
        bodies.rotation_args(bad)


# Measured live on SW 2025 (centre of mass of a 10 mm cube, mm).
@pytest.mark.parametrize("start, rotate, move, landed", [
    ([0, 10, -55], {"axis": "z", "angle_deg": 90, "about_mm": [100, 0, 0]}, None, [90, -100, -55]),
    ([90, -100, -55], {"axis": "x", "angle_deg": -90}, None, [90, -55, 100]),
    ([90, -55, 100], {"axis": "y", "angle_deg": 90}, [1, 2, 3], [101, -53, -87]),
    ([0, 10, -55], None, [0, 0, 10], [0, 10, -45]),
])
def test_moved_point_matches_solidworks(start, rotate, move, landed):
    assert bodies.moved_point(start, move, rotate) == pytest.approx(landed, abs=1e-9)


def test_match_names_pairs_by_landing_point():
    expected = {"Cube": [1, 2, 3], "Tab": [10, 0, 0]}
    found = {"Body-Move/Copy4": [10, 0, 0.004], "Body-Move/Copy4[2]": [1, 2, 3]}
    assert bodies.match_names(expected, found) == {"Body-Move/Copy4": "Tab", "Body-Move/Copy4[2]": "Cube"}


def test_match_names_leaves_ambiguous_out():
    assert bodies.match_names({"A": [0, 0, 0]}, {"x": [0, 0, 0], "y": [0, 0, 0]}) == {}


def test_unique_name():
    assert bodies.unique_name("Cube", []) == "Cube"
    assert bodies.unique_name("Cube", ["Cube", "Cube_2"]) == "Cube_3"


def test_as_names():
    assert bodies.as_names("Tab") == ["Tab"]
    assert bodies.as_names(["A", "B"]) == ["A", "B"]
    with pytest.raises(SolidWorksError):
        bodies.as_names([])
