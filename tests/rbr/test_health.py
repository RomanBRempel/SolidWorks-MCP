"""Health warnings and close_hidden_documents, on fake documents."""

import types

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import health


class _Ident:
    def __new__(cls, obj):
        return obj


class FakeMod:
    def __getattr__(self, name):
        return _Ident


# --- warnings (pure) ------------------------------------------------------------------------

def test_no_warnings_inside_the_thresholds():
    assert health.warnings_for(10, {"working_set_mb": 1000, "memory_pct": 20.0}) == []
    assert health.warnings_for(None, None) == []


def test_document_and_memory_warnings():
    warn = health.warnings_for(74, {"working_set_mb": 30000, "memory_pct": 85.5})
    assert len(warn) == 2
    assert "74 documents" in warn[0] and "close_hidden_documents" in warn[0]
    assert "85.5%" in warn[1] and "restart SolidWorks" in warn[1]


# --- close_hidden_documents -----------------------------------------------------------------

class Doc:
    def __init__(self, title, visible=False, dirty=False):
        self.title = title
        self.Visible = visible
        self.dirty = dirty

    def GetTitle(self):
        return self.title

    def GetSaveFlag(self):
        return self.dirty


class Sw:
    def __init__(self, docs, active=None):
        self.docs = list(docs)
        self.ActiveDoc = active

    def GetDocuments(self):
        return tuple(self.docs)

    def CloseDoc(self, title):
        self.docs = [d for d in self.docs if d.GetTitle() != title]

    def GetDocumentCount(self):
        return len(self.docs)


def _session(sw, current=None):
    return types.SimpleNamespace(_sw=sw, _mod=FakeMod(), _model=current,
                                 _ensure=lambda: sw)


def test_closes_only_invisible_saved_unowned(swmcp_home):
    active = Doc("Active.SLDASM", visible=True)
    hidden = Doc("Hidden1.SLDPRT")
    dirty = Doc("Edited.SLDPRT", dirty=True)
    visible = Doc("Shown.SLDPRT", visible=True)
    kept_name = Doc("Keep me.SLDPRT")
    sw = Sw([active, hidden, dirty, visible, kept_name], active=active)
    report = health.close_hidden_documents(_session(sw, current=active), keep=["keep me"])
    assert report["closed"] == ["Hidden1.SLDPRT"]
    reasons = {k["title"]: k["why"] for k in report["kept"]}
    assert reasons == {"Active.SLDASM": "active", "Edited.SLDPRT": "unsaved changes",
                       "Shown.SLDPRT": "visible", "Keep me.SLDPRT": "keep"}
    assert report["open_now"] == 4


def test_spares_a_document_owned_by_a_live_process(swmcp_home, monkeypatch):
    from solidworks_mcp.rbr import guard
    hidden = Doc("Theirs.SLDPRT")
    sw = Sw([hidden])
    monkeypatch.setattr(guard, "owners", lambda: {"Theirs.SLDPRT": {"pid": 4242, "tool": "x"}})
    monkeypatch.setattr(guard, "_pid_alive", lambda pid: True)
    report = health.close_hidden_documents(_session(sw))
    assert report["closed"] == []
    assert report["kept"][0]["why"] == "owned by pid 4242"


def test_no_documents_is_a_named_error(swmcp_home):
    with pytest.raises(SolidWorksError, match="No open documents"):
        health.close_hidden_documents(_session(Sw([])))


def test_current_model_is_never_closed(swmcp_home):
    working = Doc("Working.SLDPRT")  # invisible and saved, but it is the session's model
    sw = Sw([working, Doc("Other.SLDPRT")])
    report = health.close_hidden_documents(_session(sw, current=working))
    assert report["closed"] == ["Other.SLDPRT"]
    assert {k["title"] for k in report["kept"]} == {"Working.SLDPRT"}
