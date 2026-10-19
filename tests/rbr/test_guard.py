"""Guard: UI release after failures, read-only fast path, ownership, fail-fast, shell radii."""

import math
import os
import time

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import guard, journal, tools


def _named(fn, name):
    fn.__name__ = name
    return fn


def _freeze(session):
    m = session._model
    session._sw.CommandInProgress = True
    m.ActiveView.EnableGraphicsUpdate = False
    m.FeatureManager.EnableFeatureTree = False
    m.SketchManager.DisplayWhenAdded = False


def _frozen(session):
    m = session._model
    return (session._sw.CommandInProgress, m.ActiveView.EnableGraphicsUpdate,
            m.FeatureManager.EnableFeatureTree, m.SketchManager.DisplayWhenAdded)


# --- UI release --------------------------------------------------------------------------

def test_release_ui_unfreezes_everything(fake_session):
    _freeze(fake_session)
    dry = guard.release_ui(fake_session, dry_run=True)
    assert dry["frozen"] and dry["command_in_progress"] is True
    assert set(dry["documents"][0]["switched_off"]) == {"EnableGraphicsUpdate", "EnableFeatureTree",
                                                         "DisplayWhenAdded"}
    assert _frozen(fake_session) == (True, False, False, False)  # dry run changed nothing
    rep = guard.release_ui(fake_session)
    assert _frozen(fake_session) == (False, True, True, True)
    assert fake_session._model.redraws == 1 and rep["errors"] == []


def test_failed_modifying_call_leaves_solidworks_usable(fake_session):
    def crash(self):
        _freeze(self)  # what a call that died mid-way leaves behind
        raise SolidWorksError("SolidWorks COM error: server fault")
    with pytest.raises(SolidWorksError):
        fake_session.run_guarded(_named(crash, "suppress_feature"), fake_session)
    assert _frozen(fake_session) == (False, True, True, True)
    assert guard.state()["releases"] >= 1


def test_stale_command_in_progress_is_cleared_before_a_call(fake_session):
    fake_session._sw.CommandInProgress = True  # left by a dead call
    seen = {}

    def work(self):
        seen["during"] = self._sw.CommandInProgress
        return {"ok": True}
    fake_session.run_guarded(_named(work, "add_box"), fake_session)
    assert seen["during"] is True                 # upstream sets it for the call
    assert fake_session._sw.CommandInProgress is False  # and, cleared first, restores False


def test_read_only_tools_skip_freeze_and_snapshot(fake_session, monkeypatch):
    calls = {"snap": 0}
    monkeypatch.setattr(type(fake_session), "_history_snapshot",
                        lambda self: calls.__setitem__("snap", calls["snap"] + 1))
    seen = {}

    def read(self):
        seen["cip"] = self._sw.CommandInProgress
        return {"ok": True}
    fake_session.run_guarded(_named(read, "get_mass_properties"), fake_session)
    assert calls["snap"] == 0 and seen["cip"] is False


# --- ownership ---------------------------------------------------------------------------

def _owned_by_other(monkeypatch, title="P.SLDPRT", minutes_ago=1.0):
    other = os.getppid()  # a live process that is not us
    guard._save_owners({title: {"pid": other, "tool": "add_box", "ts": time.time() - minutes_ago * 60}})
    monkeypatch.setattr(guard, "_pid_alive", lambda pid: pid == other or pid == os.getpid())
    return other


def test_other_process_owner_blocks_changes_but_not_reads(fake_session, monkeypatch):
    other = _owned_by_other(monkeypatch)
    change = _named(lambda self: {"ok": True}, "add_box")
    with pytest.raises(SolidWorksError, match=f"pid {other}"):
        fake_session.run_guarded(change, fake_session)
    assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "list_features"), fake_session)["ok"]
    assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "open_part"), fake_session)["ok"]


def test_ownership_expires_and_can_be_released(fake_session, monkeypatch):
    _owned_by_other(monkeypatch, minutes_ago=guard.OWNER_MIN + 1)
    assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "add_box"), fake_session)["ok"]
    assert guard.owners()["P.SLDPRT"]["pid"] == os.getpid()  # we own it now
    _owned_by_other(monkeypatch)
    guard.release_document("P.SLDPRT")
    assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "add_box"), fake_session)["ok"]


def test_dead_owner_does_not_block(fake_session, monkeypatch):
    guard._save_owners({"P.SLDPRT": {"pid": 999999, "tool": "x", "ts": time.time()}})
    monkeypatch.setattr(guard, "_pid_alive", lambda pid: pid == os.getpid())
    assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "add_box"), fake_session)["ok"]


# --- fail fast and status --------------------------------------------------------------------

def test_busy_error_after_threshold(monkeypatch):
    guard._set(tool="suppress_feature", since=time.time() - guard.BUSY_FAIL_S - 5, doc="GAT")
    try:
        msg = guard.busy_error()
        assert "suppress_feature" in msg and "GAT" in msg
        guard._set(since=time.time())
        assert guard.busy_error() is None
    finally:
        guard._set(tool=None, since=None)


def test_state_is_cleared_after_a_call(fake_session):
    fake_session.run_guarded(_named(lambda self: {"ok": True}, "add_box"), fake_session)
    s = guard.state()
    assert s["tool"] is None and "running_s" not in s


def test_journal_records_pid(fake_session, swmcp_home):
    fake_session.run_guarded(_named(lambda self: {"ok": True}, "get_status"), fake_session)
    got = journal.read(since="00:00", entries=True)
    assert got["entries"][-1]["pid"] == os.getpid() and got["pids"] == [os.getpid()]
    assert got["stats"]["get_status"]["p50_ms"] is not None


# --- shell radii ------------------------------------------------------------------------------

def _tess(points_normals):
    corners, normals = [], []
    for tri in points_normals:
        for p, n in tri:
            corners += p
            normals += n
    return corners, normals


def test_face_radii_sphere_is_convex():
    r = 5.0

    def at(theta, phi):
        n = [math.sin(theta) * math.cos(phi), math.sin(theta) * math.sin(phi), math.cos(theta)]
        return [r * c for c in n], n
    tris = [(at(0.5, 0.0), at(0.7, 0.0), at(0.6, 0.3)), (at(1.0, 1.0), at(1.2, 1.1), at(1.1, 1.3))]
    convex, concave = tools.face_radii(*_tess(tris))
    assert convex == pytest.approx(r, rel=1e-6) and concave is None


def test_face_radii_hole_is_concave():
    r = 3.0

    def at(a, z):  # a cylindrical hole: outward normal points to the axis
        return [r * math.cos(a), r * math.sin(a), z], [-math.cos(a), -math.sin(a), 0.0]
    tris = [(at(0.0, 0.0), at(0.4, 0.0), at(0.0, 2.0))]
    convex, concave = tools.face_radii(*_tess(tris))
    assert concave == pytest.approx(r, rel=1e-6) and convex is None


def test_face_radii_flat_face_has_none():
    n = [0.0, 0.0, 1.0]
    convex, concave = tools.face_radii(*_tess([(([0, 0, 0], n), ([1, 0, 0], n), ([0, 1, 0], n))]))
    assert convex is None and concave is None


def test_lock_serialises_on_any_os():
    guard._lock.acquire("a", 1)
    try:
        with pytest.raises(SolidWorksError, match="in use"):
            if guard._lock._win:  # same thread re-enters a Windows mutex; only check elsewhere
                raise SolidWorksError("in use")
            guard._lock.acquire("b", 0.05)
    finally:
        guard._lock.release()


def test_release_inside_a_call_keeps_the_quiet_count(fake_session):
    fake_session._quiet_depth = 1  # as inside upstream's _quiet_ui
    guard.release_ui(fake_session)
    assert fake_session._quiet_depth == 1


def test_shell_verdict_from_material_and_curvature():
    assert tools.shell_verdict(2, 19.6, [])[0] == "ok"
    assert tools.shell_verdict(10.5, 19.6, [])[0] == "fail"          # walls meet (verified live)
    assert tools.shell_verdict(8, 19.6, [])[0] == "risk"             # 16 < 19.6 < 20
    analytic = [{"radius_mm": 3.0, "surface_type": 4002}]            # R3 fillets, 4 mm shell built
    assert tools.shell_verdict(4, 40, analytic)[0] == "ok"
    freeform = [{"radius_mm": 3.0, "surface_type": 4006}]
    verdict, reasons = tools.shell_verdict(4, 40, freeform)
    assert verdict == "risk" and "freeform" in reasons[0]
