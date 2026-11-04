"""geom.resolve_body: names and COM objects both land on a body of the part."""

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import geom


class _Ident:
    def __new__(cls, obj):
        return obj


class _Mod:
    def __getattr__(self, name):
        return _Ident


class FakeBody:
    """IBody2 stand-in: unhashable, like the real COM wrapper."""

    __hash__ = None

    def __init__(self, name):
        self.Name = name

    def GetMassProperties(self, density):
        return (0.0, 0.0, 0.0, 1e-6)


class FakePart:
    def __init__(self, bodies):
        self._bodies = bodies

    def GetBodies2(self, kind, visible):
        return list(self._bodies)


class FakeSession:
    def __init__(self, bodies):
        self._mod = _Mod()
        self._part = FakePart(bodies)


@pytest.fixture
def setup(monkeypatch):
    bodies = [FakeBody("Тело1"), FakeBody("MirrorBody")]
    session = FakeSession(bodies)
    monkeypatch.setattr(geom, "_solid_bodies", lambda s, doc: list(bodies))
    return session, bodies


def test_resolve_body_by_name(setup):
    session, bodies = setup
    assert geom.resolve_body(session, None, "Тело1") is bodies[0]


def test_resolve_body_unknown_name_lists_bodies(setup):
    session, _ = setup
    with pytest.raises(SolidWorksError, match="Тело1, MirrorBody"):
        geom.resolve_body(session, None, "Тело9")


def test_resolve_body_takes_the_object_itself(setup):
    session, bodies = setup
    # the defect: `body in {Name: body}` hashed the IBody2 and crashed
    resolved = geom.resolve_body(session, None, bodies[1])
    assert resolved.Name == "MirrorBody"


def test_resolve_body_object_from_another_part_is_refused(setup):
    session, _ = setup
    with pytest.raises(SolidWorksError, match="not a solid body of the current part"):
        geom.resolve_body(session, None, FakeBody("Чужое"))


def test_resolve_body_garbage_is_refused(setup):
    session, _ = setup
    with pytest.raises(SolidWorksError, match="body name or an IBody2"):
        geom.resolve_body(session, None, 3.14)
