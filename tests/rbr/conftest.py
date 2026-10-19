"""Fixtures for the RBR extension tests: fake COM objects, so they run on any OS.

Run alone (also on Linux):  pytest tests/rbr --confcutdir=tests/rbr
On Windows they also run with the whole suite.
"""

import sys
import types

if "pythoncom" not in sys.modules:  # Linux/macOS: stub just enough for imports
    try:
        import pythoncom  # noqa: F401
    except ImportError:
        pc = types.ModuleType("pythoncom")
        pc.com_error = type("com_error", (Exception,), {})
        pc.VT_DISPATCH = 9
        pc.CoInitialize = pc.CoUninitialize = lambda *a: None
        sys.modules["pythoncom"] = pc
        w = types.ModuleType("win32com")
        wc = types.ModuleType("win32com.client")
        wc.VARIANT = lambda *a: None
        wc.gencache = types.SimpleNamespace(EnsureModule=lambda *a: None)
        wc.dynamic = types.SimpleNamespace(DumbDispatch=lambda *a: None)
        wc.GetActiveObject = lambda *a: None
        w.client = wc
        sys.modules["win32com"] = w
        sys.modules["win32com.client"] = wc
        sys.modules["win32gui"] = types.ModuleType("win32gui")

import pytest  # noqa: E402


class Ident:
    """Stand-in for a makepy wrapper class: wrap(obj, Ident) returns obj itself."""

    def __new__(cls, obj):
        return obj


class FakeMod:
    def __getattr__(self, name):
        return Ident


class FakeFeature:
    def __init__(self, name, type_name="Extrusion", model=None):
        self.Name = name
        self.type_name = type_name
        self.model = model

    def GetTypeName2(self):
        return self.type_name

    def GetErrorCode2(self):
        return (0, False)

    def IsSuppressed2(self, *a):
        return (False,)

    def IsRolledBack(self):
        return bool(getattr(self.model, "rolled_back", False)) and self is self.model.features[-1]

    def Select2(self, *a):
        self.model.selected.append(self)
        return True


class FakeFeatureManager:
    def __init__(self, model):
        self.model = model
        self.get_features_calls = 0

    def GetFeatures(self, top):
        self.get_features_calls += 1
        return tuple(self.model.features)

    def GetFeatureCount(self, top):
        return len(self.model.features)


class FakeModel:
    """A part whose tree starts with folders, planes and the origin."""

    def __init__(self, names=(), title="P.SLDPRT"):
        self.title = title
        self.features = [FakeFeature("Notes", "CommentsFolder", self), FakeFeature("Front", "RefPlane", self),
                         FakeFeature("Origin", "OriginProfileFeature", self)]
        self.features += [FakeFeature(n, model=self) for n in names]
        self.FeatureManager = FakeFeatureManager(self)
        self.selected = []
        self.Extension = types.SimpleNamespace(DeleteSelection2=self._delete)
        self.SelectionManager = types.SimpleNamespace(CreateSelectData=lambda: None)

    def _delete(self, *a):
        for f in self.selected:
            self.features.remove(f)
        self.selected = []
        return True

    def GetType(self):
        return 1

    def GetTitle(self):
        return self.title

    def FeatureByPositionReverse(self, i):
        return self.features[-1 - i] if i < len(self.features) else None

    def FirstFeature(self):
        return self.features[0] if self.features else None

    def ClearSelection2(self, *a):
        self.selected = []

    def ForceRebuild3(self, *a):
        return True

    def add(self, name):
        self.features.append(FakeFeature(name, model=self))


@pytest.fixture
def fake_session(monkeypatch):
    from solidworks_mcp.session import SolidWorksSession
    from solidworks_mcp.rbr import fastpath, journal
    fastpath.apply()
    journal.apply()
    s = SolidWorksSession()
    s._mod = FakeMod()
    s._model = FakeModel(["Sketch1", "Boss1"])
    monkeypatch.setattr(SolidWorksSession, "_require_model", lambda self: self._model)
    monkeypatch.setattr(SolidWorksSession, "_require_part", lambda self: self._model)
    monkeypatch.setattr(SolidWorksSession, "_reattach_if_gone", lambda self: None)
    monkeypatch.setattr(SolidWorksSession, "_command_in_progress", lambda self, flag: False)
    import contextlib
    monkeypatch.setattr(SolidWorksSession, "_quiet_ui", lambda self: contextlib.nullcontext())
    return s


@pytest.fixture
def swmcp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SWMCP_HOME", str(tmp_path))
    monkeypatch.delenv("SWMCP_SCRIPTS", raising=False)
    return tmp_path
