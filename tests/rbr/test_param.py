"""param: the pure helpers behind variables, linked dimensions and midline centres."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import param

NAMES = ["TIP_WALL", "INS_Z1", "CAV_DEPTH", "Толщина"]


@pytest.mark.parametrize("expr, quoted", [
    ("TIP_WALL", '"TIP_WALL"'),
    ("tip_wall * 2", '"TIP_WALL" * 2'),             # SolidWorks compares names case-blind
    ('"TIP_WALL" + CAV_DEPTH', '"TIP_WALL" + "CAV_DEPTH"'),  # quoted runs stay as they are
    ("abs(INS_Z1)", 'abs("INS_Z1")'),               # function names are not variables
    ("10.15", "10.15"),
    ("Толщина / 2", '"Толщина" / 2'),               # Cyrillic names too
    ("UNKNOWN + 1", "UNKNOWN + 1"),                  # unknown names left for SolidWorks to refuse
])
def test_quote_names(expr, quoted):
    assert param.quote_names(expr, NAMES) == quoted


def test_quote_names_without_known_names_is_identity():
    assert param.quote_names(" X + 1 ", []) == "X + 1"
