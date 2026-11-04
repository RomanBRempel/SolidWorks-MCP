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
