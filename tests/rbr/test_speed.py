"""Speed patches: budgeted fillet diagnosis, list_edges filtering before the expensive calls."""

import types

import pytest

from solidworks_mcp.session import SolidWorksSession
from solidworks_mcp.rbr import speed

from conftest import FakeMod


class Edge:
    curve_calls = 0

    def __init__(self, start, end):
        self.start, self.end = start, end
        self.selected = False

    def GetCurveParams2(self):
        return [c / 1000 for c in self.start] + [c / 1000 for c in self.end] + [0.0, 1.0]

    def GetCurve(self):
        Edge.curve_calls += 1
        length = sum((a - b) ** 2 for a, b in zip(self.start, self.end)) ** 0.5 / 1000
        return types.SimpleNamespace(Identity=lambda: 3001, GetLength3=lambda a, b: length)

    def Select4(self, *a):
        self.selected = True
        return True


def _fake(edges, bad=(), slow=0.0):
    """A session-like object: edges in `bad` refuse any fillet they are part of."""
    import time
    model = types.SimpleNamespace(ClearSelection2=lambda *a: [setattr(e, "selected", False) for e in edges],
                                  Extension=types.SimpleNamespace(
                                      GetObjectByPersistReference3=lambda ref: (edges[ref],),
                                      DeleteSelection2=lambda *a: True))

    def uniform(radius, chain=False):
        time.sleep(slow)
        if any(e.selected and i in bad for i, e in enumerate(edges)):
            return None
        return types.SimpleNamespace(Select2=lambda *a: True)

    fake = SolidWorksSession()           # the real helpers (_edge_entry, _axis_of, _box_corners ...)
    fake._model, fake._mod = model, FakeMod()
    fake._solid_body = lambda: types.SimpleNamespace(GetEdges=lambda: edges)
    fake._persist_refs = lambda es: [edges.index(e) for e in es]
    fake._uniform_fillet = uniform
    fake._largest_rounds = lambda refs, index_of, rounds, r: ""
    return fake


def _edges(n):
    return [Edge((i, 0, 0), (i, 10, 0)) for i in range(n)]


def test_fillet_diagnosis_names_the_failing_edge():
    edges = _edges(6)
    msg = speed._fillet_refusal(_fake(edges, bad={3}), 2.0, edges, True)
    assert "edge(s) 3 (10 mm) fail even alone" in msg and 'edges="0,1,2,4,5"' in msg
    assert "not checked" not in msg


def test_fillet_diagnosis_stops_at_the_budget(monkeypatch):
    monkeypatch.setattr(speed, "FILLET_DIAG_S", 0.05)
    edges = _edges(40)
    msg = speed._fillet_refusal(_fake(edges, bad={39}, slow=0.01), 2.0, edges, True)
    assert "not checked within 0.05 s" in msg


def test_list_edges_filters_before_reading_curves(monkeypatch):
    edges = [Edge((0, 0, 0), (0, 10, 0)), Edge((100, 0, 0), (100, 10, 0)), Edge((5, 5, 0), (5, 8, 0))]
    fake = _fake(edges)
    fake._require_model = lambda: None
    Edge.curve_calls = 0
    got = speed.list_edges(fake, within_mm=[[-1, -1, -1], [20, 20, 1]])
    assert [e["index"] for e in got["edges"]] == [0, 2]
    assert Edge.curve_calls == 2   # the far edge never had its curve read
    got = speed.list_edges(fake, within_mm=[[-1, -1, -1], [20, 20, 1]], min_length_mm=5)
    assert [e["index"] for e in got["edges"]] == [0]


def test_script_api_lists_members_of_a_wrapper_class():
    from solidworks_mcp.rbr import script_tool

    class IFake:
        _prop_map_get_ = {"Name": 1, "Visible": 2}
        _prop_map_put_ = {"Visible": 2}

        def GetBox(self):
            pass

        def Select4(self):
            pass

    got = script_tool.api(IFake())
    assert got["interface"] == "IFake" and got["properties"] == ["Name", "Visible"]
    assert got["methods"] == ["GetBox", "Select4"]
    assert script_tool.api(IFake(), "sel")["methods"] == ["Select4"]
