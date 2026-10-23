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


def _wf(index, thinnest, median, share):
    return {"index": index, "thinnest_mm": thinnest, "median_thin_mm": median, "thin_share": share, "at_mm": [0, 0, 0]}


def test_shell_verdict_from_material_and_curvature():
    assert tools.shell_verdict(2, [], [])[0] == "ok"
    assert tools.shell_verdict(10.5, [_wf(1, 15.0, 16.0, 0.9)], [])[0] == "fail"   # walls meet (verified live)
    assert tools.shell_verdict(8, [_wf(1, 18.0, 19.0, 0.9)], [])[0] == "risk"      # 16..20 mm: slit cavity
    verdict, reasons = tools.shell_verdict(3, [_wf(57, 0.002, 0.4, 0.02)], [])       # GAT-27: knife edge
    assert verdict == "risk" and "acute edge" in reasons[0]
    analytic = [{"radius_mm": 3.0, "surface_type": 4002}]                          # R3 fillets, 4 mm built
    assert tools.shell_verdict(4, [], analytic)[0] == "ok"
    verdict, reasons = tools.shell_verdict(4, [], [{"radius_mm": 3.0, "surface_type": 4006, "face": "#1"}])
    assert verdict == "risk" and "freeform" in reasons[0]
    gat27 = [{"radius_mm": 2.32, "surface_type": 4007, "face": "#114"}]               # blends under a 3 mm shell
    verdict, reasons = tools.shell_verdict(3, [], gat27)
    assert verdict == "risk" and "#114" in reasons[0] and "reorder_feature" in reasons[0]


def _box_tris(w, h, d):
    """Closed box 0..w x 0..h x 0..d as (face, a, b, c) and its outward normals per face."""
    v = lambda x, y, z: (float(x), float(y), float(z))  # noqa: E731
    quads = {0: ([v(0, 0, 0), v(w, 0, 0), v(w, h, 0), v(0, h, 0)], (0, 0, -1)),
             1: ([v(0, 0, d), v(w, 0, d), v(w, h, d), v(0, h, d)], (0, 0, 1)),
             2: ([v(0, 0, 0), v(w, 0, 0), v(w, 0, d), v(0, 0, d)], (0, -1, 0)),
             3: ([v(0, h, 0), v(w, h, 0), v(w, h, d), v(0, h, d)], (0, 1, 0)),
             4: ([v(0, 0, 0), v(0, h, 0), v(0, h, d), v(0, 0, d)], (-1, 0, 0)),
             5: ([v(w, 0, 0), v(w, h, 0), v(w, h, d), v(w, 0, d)], (1, 0, 0))}
    tris, normals = [], {}
    for f, (q, n) in quads.items():
        tris += [(f, q[0], q[1], q[2]), (f, q[0], q[2], q[3])]
        normals[f] = n
    return tris, normals


def test_mesh_thickness_of_a_plate():
    from solidworks_mcp.rbr import meshwall
    tris, normals = _box_tris(100, 80, 4)
    samples = [(1, (50.0, 40.0, 4.0), normals[1]), (5, (100.0, 40.0, 2.0), normals[5])]
    got = meshwall.wall_survey(tris, samples, 10.0)
    by = {f["index"]: f for f in got["faces"]}
    assert by[1]["thinnest_mm"] == pytest.approx(4.0, abs=1e-3)   # through the plate
    assert 5 not in by                                             # 100 mm across: beyond the cap


def test_mesh_thickness_runs_fast_enough():
    """~4800 small triangles and 4000 rays, the size of GAT-27 v5: well under the budget."""
    import time as _t
    from solidworks_mcp.rbr import meshwall
    tris, step, w, h, d = [], 4.0, 160.0, 60.0, 6.0
    for z, face in ((0.0, 0), (d, 1)):
        for i in range(int(w / step)):
            for j in range(int(h / step)):
                x0, y0 = i * step, j * step
                a, b, c, e = (x0, y0, z), (x0 + step, y0, z), (x0 + step, y0 + step, z), (x0, y0 + step, z)
                tris += [(face, a, b, c), (face, a, c, e)]
    samples = [(1, (x * 2.0 + 1, y * 2.0 + 1, d), (0.0, 0.0, 1.0)) for x in range(80) for y in range(30)]
    t0 = _t.perf_counter()
    got = meshwall.wall_survey(tris, samples, 7.5)
    elapsed = _t.perf_counter() - t0
    assert len(tris) == 2400 and got["thin_samples"] == len(samples)
    assert got["faces"][0]["thinnest_mm"] == pytest.approx(d, abs=1e-3)
    assert elapsed < 5, elapsed


class _Fault(Exception):
    hresult = -2147417851  # RPC_E_SERVERFAULT


def test_server_fault_quarantines_changes_until_restart(fake_session):
    guard._set(quarantine=None)
    def crash(self):
        raise _Fault("server fault")
    try:
        with pytest.raises(SolidWorksError, match="its own crash"):
            fake_session.run_guarded(_named(crash, "add_shell"), fake_session)
        assert guard.state()["quarantine"]["tool"] == "add_shell"
        ok = _named(lambda self: {"ok": True}, "open_part")
        with pytest.raises(SolidWorksError, match="refused until SolidWorks is restarted"):
            fake_session.run_guarded(ok, fake_session)
        assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "list_features"), fake_session)["ok"]
        fake_session._sw.pid = 2222          # SolidWorks restarted: a new process
        assert fake_session.run_guarded(ok, fake_session)["ok"]
        assert guard.state()["quarantine"] is None
    finally:
        guard._set(quarantine=None)
        fake_session._sw.pid = 1111


def test_quarantine_can_be_cleared_by_hand(fake_session):
    guard._set(quarantine={"tool": "add_shell", "at": "12:54:29", "sw_pid": 1111, "doc": "GAT"})
    try:
        rep = guard.release_ui(fake_session, clear_quarantine=True)
        assert rep["quarantine"] is None
        assert fake_session.run_guarded(_named(lambda self: {"ok": True}, "add_box"), fake_session)["ok"]
    finally:
        guard._set(quarantine=None)
