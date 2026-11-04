"""sketch_edit: the pure pieces (resolve, snapshot diff, point mapping)."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import sketch_edit


class FakeSegment:
    def __init__(self, kind, seg_id, length, construction=False):
        self._kind = kind
        self._id = seg_id
        self._len = length
        self.ConstructionGeometry = construction

    def GetType(self):
        return self._kind

    def GetID(self):
        return self._id

    def GetLength(self):
        return self._len


class FakeSketch:
    def __init__(self, segments):
        self._segments = segments

    def GetSketchSegments(self):
        return list(self._segments)


class _Ident:
    def __new__(cls, obj):
        return obj


class _Mod:
    def __getattr__(self, name):
        return _Ident


class FakeSession:
    def __init__(self):
        self._mod = _Mod()


@pytest.fixture
def session():
    return FakeSession()


def _sketch():
    return FakeSketch([
        FakeSegment(0, (0, 1), 0.05),
        FakeSegment(3, (0, 2), 0.107),
        FakeSegment(0, (0, 3), 0.02, construction=True),
    ])


def test_snapshot_skips_construction(session):
    snap = sketch_edit.snapshot(session, _sketch())
    assert len(snap) == 2
    assert snap[(0, (0, 1))] == 0.05


def test_resolve_segment_by_index_and_hash(session):
    sketch = _sketch()
    assert sketch_edit.resolve_segment(session, sketch, 1).GetLength() == 0.107
    assert sketch_edit.resolve_segment(session, sketch, "#0").GetLength() == 0.05


def test_resolve_segment_passes_com_object_through(session):
    seg = FakeSegment(3, (9, 9), 1.0)
    assert sketch_edit.resolve_segment(session, _sketch(), seg) is seg


def test_resolve_segment_out_of_range(session):
    with pytest.raises(SolidWorksError, match="No segment 5"):
        sketch_edit.resolve_segment(session, _sketch(), 5)
    with pytest.raises(SolidWorksError, match="index"):
        sketch_edit.resolve_segment(session, _sketch(), "spline")


def test_diff_lengths_reports_removed_added_changed():
    before = {"a": 0.05, "b": 0.10, "c": 0.02}
    after = {"a": 0.05, "b": 0.07, "d": 0.03}
    change = sketch_edit.diff_lengths(before, after)
    assert change["removed"] == ["c"]
    assert change["added"] == ["d"]
    assert change["changed"] == ["b"]
    assert change["length_before_m"] == pytest.approx(0.17)
    assert change["length_after_m"] == pytest.approx(0.15)


def test_diff_lengths_identical_is_quiet():
    snap = {"a": 0.05}
    change = sketch_edit.diff_lengths(snap, dict(snap))
    assert not (change["removed"] or change["added"] or change["changed"])


def test_sketch_uv_rejects_bad_space_and_shape(session):
    with pytest.raises(SolidWorksError, match="'model' or 'sketch'"):
        sketch_edit._sketch_uv_m(session, object(), [0, 0], "face")
    with pytest.raises(SolidWorksError, match=r"\[u, v\]"):
        sketch_edit._sketch_uv_m(session, object(), [0, 0, 0], "sketch")
    with pytest.raises(SolidWorksError, match=r"\[x, y, z\]"):
        sketch_edit._sketch_uv_m(session, object(), [0, 0], "model")


def test_sketch_uv_sketch_space_is_mm_to_m(session):
    assert sketch_edit._sketch_uv_m(session, object(), [10, -2.5], "sketch") == (0.010, -0.0025)
