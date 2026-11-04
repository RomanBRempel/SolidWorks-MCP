"""param: the pure helpers behind variables, linked dimensions and midline centres."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import param

NAMES = ["WALL", "POS_1", "POCKET_DEPTH", "Толщина"]


@pytest.mark.parametrize("expr, quoted", [
    ("WALL", '"WALL"'),
    ("wall * 2", '"WALL" * 2'),             # SolidWorks compares names case-blind
    ('"WALL" + POCKET_DEPTH', '"WALL" + "POCKET_DEPTH"'),  # quoted runs stay as they are
    ("abs(POS_1)", 'abs("POS_1")'),               # function names are not variables
    ("12.5", "12.5"),
    ("Толщина / 2", '"Толщина" / 2'),               # Cyrillic names too
    ("UNKNOWN + 1", "UNKNOWN + 1"),                  # unknown names left for SolidWorks to refuse
])
def test_quote_names(expr, quoted):
    assert param.quote_names(expr, NAMES) == quoted


def test_quote_names_without_known_names_is_identity():
    assert param.quote_names(" X + 1 ", []) == "X + 1"


def test_axis_role_maps_sketch_axes_to_model_axes():
    # the Right plane: sketch x runs along model -Z, y along +Y
    assert param.axis_role([0, 0, -1], [0, 1, 0]) == {"x": "Z", "y": "Y"}
    # a turned plane: its x runs along no model axis
    assert param.axis_role([0.7071, 0.7071, 0], [0, 0, 1]) == {"x": None, "y": "Z"}


def test_station_dimension_axis():
    assert param.station_dimension_axis((0.0, 2.0)) == "x"   # a vertical chord is placed by x
    assert param.station_dimension_axis((-3.0, 0.0)) == "y"
    with pytest.raises(SolidWorksError, match="sketch axis"):
        param.station_dimension_axis((1.0, 1.0))


def test_segment_cross():
    t, p = param._segment_cross((0, -1), (0, 1), (-1, 0), (1, 0))
    assert t == pytest.approx(0.5) and p == pytest.approx((0, 0))
    assert param._segment_cross((0, -1), (0, 1), (1, 0), (2, 0)) is None   # misses
    assert param._segment_cross((0, 0), (1, 0), (0, 1), (1, 1)) is None   # parallel
