"""Guard: keep SolidWorks usable when a call fails, and keep clients from tripping over each other.

Wraps SolidWorksSession.run_guarded (inside the journal wrapper) on the COM thread:

1. UI release. Upstream's run_guarded sets CommandInProgress and switches the
   view/feature tree off for the call, and restores them in `finally`. After a
   COM fault (RPC_E_SERVERFAULT from a 49 s shell on 2026-10-19) that restore
   fails too and SolidWorks stays frozen while idle. Every failed modifying call
   is followed by release_ui(): each flag reset on its own, every open document.
   A CommandInProgress left set by a dead call is cleared before the next one.
2. Read-only tools (READ_ONLY) skip the snapshot, CommandInProgress and the UI
   freeze/redraw: ~0.5 s less per call on a heavy surface part.
3. One call at a time across processes: a named mutex (Local\\solidworks-mcp-rbr)
   around each call, so two servers never interleave in one SolidWorks.
4. Document ownership: the process that last modified a document owns it for
   SWMCP_OWNER_MIN minutes (10); a modifying call from another live process is
   refused with who holds it. Reads are always allowed. release_document frees it.
5. Status outside the COM queue: state() gives the running call and its age,
   used by server_health and by the fail-fast check before queueing.

Limit: a single server process shared by several chat sessions is one owner;
ownership only separates processes. The journal records the pid to show which.
"""

import json
import os
import threading
import time

from .. import binding
from ..errors import SolidWorksError
from ..session import SolidWorksSession
from . import journal

# Tools that only read: no snapshot, no CommandInProgress, no UI freeze or redraw.
READ_ONLY = frozenset({
    "get_status", "list_documents", "get_mass_properties", "get_bounding_box", "list_faces", "list_edges",
    "list_bodies", "list_features", "list_dimensions", "read_sketch", "list_equations", "list_components",
    "get_assembly_bounding_box", "get_custom_properties", "list_configurations", "shell_check",
    "release_ui",
})

# Tools that switch the current document: ownership is checked on the document they act on, not before.
SWITCHERS = frozenset({"new_part", "new_assembly", "open_part", "open_assembly", "activate_document"})

# COM HRESULTs after which SolidWorks' state cannot be trusted
FAULTS = {
    -2147417851: "RPC_E_SERVERFAULT",
    -2147417848: "RPC_E_DISCONNECTED",
    -2147023174: "RPC_S_SERVER_UNAVAILABLE",
    -2147023170: "RPC_S_CALL_FAILED",
    -2147418111: "RPC_E_CALL_REJECTED",
}

MUTEX_NAME = "Local\\solidworks-mcp-rbr"
LOCK_WAIT_S = float(os.environ.get("SWMCP_LOCK_WAIT_S", "30"))
OWNER_MIN = float(os.environ.get("SWMCP_OWNER_MIN", "10"))
BUSY_FAIL_S = float(os.environ.get("SWMCP_BUSY_FAIL_S", "20"))

_ORIGINAL = {}
_state_lock = threading.Lock()
_state = {"tool": None, "since": None, "doc": None, "calls": 0, "faults": 0, "releases": 0,
          "last_fault": None, "last_release": None}


# --- status (any thread) -----------------------------------------------------------------

def state() -> dict:
    with _state_lock:
        s = dict(_state)
    if s["since"] is not None:
        s["running_s"] = round(time.time() - s["since"], 1)
    s["pid"] = os.getpid()
    return s


def busy_error() -> str | None:
    """A readable refusal when the COM thread has been busy longer than BUSY_FAIL_S."""
    s = state()
    if s["tool"] and s.get("running_s", 0) > BUSY_FAIL_S:
        return (f"SolidWorks is busy: '{s['tool']}' on '{s['doc']}' has been running {s['running_s']:.0f}s. "
                "Try again later; server_health shows when it is free. If SolidWorks itself is computing "
                "(a shell, a loft), it cannot be interrupted from here.")
    return None


def _set(**kw):
    with _state_lock:
        _state.update(kw)


# --- cross-process lock -----------------------------------------------------------------

class _ProcessLock:
    """A named Windows mutex (owned by the COM thread that takes it); a thread lock elsewhere."""

    def __init__(self):
        self._handle = None
        self._local = threading.Lock()
        try:
            import win32event  # noqa: F401
            self._win = True
        except ImportError:
            self._win = False

    def _holder_path(self):
        return os.path.join(journal.home(), "lock.json")

    def holder(self) -> dict | None:
        try:
            with open(self._holder_path(), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def acquire(self, tool: str, timeout_s: float) -> None:
        if self._win:
            import win32event
            if self._handle is None:
                self._handle = win32event.CreateMutex(None, False, MUTEX_NAME)
            rc = win32event.WaitForSingleObject(self._handle, int(timeout_s * 1000))
            # WAIT_OBJECT_0 = 0, WAIT_ABANDONED = 0x80 (the holder died: the mutex is ours now)
            if rc not in (0, 0x80):
                h = self.holder() or {}
                raise SolidWorksError(
                    f"SolidWorks is in use by another server process (pid {h.get('pid')}, "
                    f"'{h.get('tool')}' since {h.get('since')}); waited {timeout_s:.0f}s.")
        elif not self._local.acquire(timeout=timeout_s):
            raise SolidWorksError(f"SolidWorks is in use; waited {timeout_s:.0f}s.")
        try:
            with open(self._holder_path(), "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "tool": tool, "since": time.strftime("%H:%M:%S")}, fh)
        except OSError:
            pass

    def release(self) -> None:
        if self._win:
            import win32event
            if self._handle is not None:
                try:
                    win32event.ReleaseMutex(self._handle)
                except Exception:  # noqa: BLE001 - not ours (never acquired): nothing to release
                    pass
        else:
            try:
                self._local.release()
            except RuntimeError:
                pass


_lock = _ProcessLock()


# --- document ownership --------------------------------------------------------------------

def _owners_path() -> str:
    return os.path.join(journal.home(), "owners.json")


def owners() -> dict:
    try:
        with open(_owners_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_owners(data: dict) -> None:
    tmp = _owners_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, _owners_path())


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        import psutil  # optional
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        try:
            import win32api
            handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            win32api.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - no such process
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def check_owner(title: str | None, tool: str) -> None:
    if not title or OWNER_MIN <= 0:
        return
    entry = owners().get(title)
    if not entry or entry.get("pid") == os.getpid():
        return
    age_min = (time.time() - float(entry.get("ts", 0))) / 60
    if age_min <= OWNER_MIN and _pid_alive(int(entry.get("pid", 0))):
        raise SolidWorksError(
            f"'{title}' is being modified by another server process (pid {entry['pid']}, last "
            f"'{entry.get('tool')}' {age_min:.1f} min ago). Reading is allowed; to change it, wait "
            f"{OWNER_MIN:.0f} min after its last change or call release_document('{title}').")


def claim(title: str | None, tool: str) -> None:
    if not title:
        return
    try:
        data = owners()
        data[title] = {"pid": os.getpid(), "tool": tool, "ts": time.time(),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_owners(data)
    except OSError:
        pass


def release_document(title: str | None = None) -> dict:
    data = owners()
    if title is None:
        freed = sorted(data)
        data = {}
    else:
        freed = [title] if data.pop(title, None) is not None else []
    _save_owners(data)
    return {"ok": True, "released": freed, "owners": data}


# --- UI release ---------------------------------------------------------------------------

def _documents(session):
    docs = session._sw.GetDocuments() or ()
    return [binding.wrap(d, session._mod.IModelDoc2) for d in docs]


def release_ui(session, dry_run: bool = False, _retry: bool = True) -> dict:
    """Clear CommandInProgress and switch drawing back on in every open document.

    Each setting is reset on its own, so one failure does not leave the rest.
    dry_run reports what is switched off without changing it.
    """
    report = {"ok": True, "command_in_progress": None, "documents": [], "errors": []}
    if session._sw is None:
        _ORIGINAL.get("connect", SolidWorksSession.connect)(session)
    try:
        report["command_in_progress"] = bool(session._sw.CommandInProgress)
        if report["command_in_progress"] and not dry_run:
            session._sw.CommandInProgress = False
    except Exception as exc:  # noqa: BLE001
        if _retry and getattr(exc, "hresult", None) in FAULTS:  # the link died: attach to the running SolidWorks
            session._sw = session._model = None
            _ORIGINAL.get("connect", SolidWorksSession.connect)(session)
            return release_ui(session, dry_run, _retry=False)
        report["errors"].append(f"CommandInProgress: {exc}")
    mod = session._mod
    for doc in _documents(session):
        entry = {"title": None, "switched_off": []}
        try:
            entry["title"] = doc.GetTitle()
            view = binding.wrap(doc.ActiveView, mod.IModelView)
            fm = binding.wrap(doc.FeatureManager, mod.IFeatureManager)
            sk = binding.wrap(doc.SketchManager, mod.ISketchManager)
            for target, prop in ((view, "EnableGraphicsUpdate"), (fm, "EnableFeatureTree"),
                                 (fm, "EnableFeatureTreeWindow"), (sk, "DisplayWhenAdded")):
                if target is None:
                    continue
                try:
                    if not getattr(target, prop):
                        entry["switched_off"].append(prop)
                        if not dry_run:
                            setattr(target, prop, True)
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append(f"{entry['title']}.{prop}: {exc}")
            if entry["switched_off"] and not dry_run:
                doc.GraphicsRedraw2()
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"{entry['title']}: {exc}")
        report["documents"].append(entry)
    if not dry_run:
        # Never touch _quiet_depth: when this runs inside a guarded call (a script, or
        # connect() from _ensure) upstream's _quiet_ui still has to count itself out.
        if not session._quiet_depth:
            session._to_wake = []
        _set(releases=_state["releases"] + 1, last_release=time.strftime("%H:%M:%S"))
    report["dry_run"] = dry_run
    report["frozen"] = bool(report["command_in_progress"]) or any(d["switched_off"] for d in report["documents"])
    return report


def _clear_stale_flag(session) -> None:
    """A CommandInProgress still set while we hold the lock was left by a dead call."""
    try:
        if session._sw is not None and session._sw.CommandInProgress:
            session._sw.CommandInProgress = False
    except Exception:  # noqa: BLE001 - _reattach_if_gone in the call deals with a dead link
        pass


def _title(session):
    try:
        return session._model.GetTitle() if session._model is not None else None
    except Exception:  # noqa: BLE001
        return None


def _run_guarded(self, fn, *args, **kwargs):
    tool = journal.tool_name(fn)
    read_only = tool in READ_ONLY
    _lock.acquire(tool, LOCK_WAIT_S)
    _set(tool=tool, since=time.time(), doc=_title(self), calls=_state["calls"] + 1)
    try:
        if read_only:
            self._reattach_if_gone()
            return fn(*args, **kwargs)
        if tool not in SWITCHERS:
            check_owner(_title(self), tool)
        _clear_stale_flag(self)
        try:
            result = _ORIGINAL["run_guarded"](self, fn, *args, **kwargs)
        except Exception as exc:
            hresult = getattr(exc, "hresult", None)
            if hresult in FAULTS:
                _set(faults=_state["faults"] + 1, last_fault=f"{time.strftime('%H:%M:%S')} {tool}: {FAULTS[hresult]}")
            try:
                release_ui(self)
            except Exception:  # noqa: BLE001 - the original error is what the caller needs
                pass
            raise
        claim(_title(self), tool)
        return result
    finally:
        _set(tool=None, since=None)
        _lock.release()


_releasing = threading.local()


def _connect(self):
    """Attach as upstream does, then clear whatever a previous server or a crash left frozen."""
    result = _ORIGINAL["connect"](self)
    if not getattr(_releasing, "on", False):
        _releasing.on = True
        try:
            report = release_ui(self)
            if isinstance(result, dict) and report.get("frozen"):
                result["released_ui"] = True
        except Exception:  # noqa: BLE001 - connecting must not fail on the clean-up
            pass
        finally:
            _releasing.on = False
    return result


def apply() -> None:
    """Install before journal.apply(), so the journal wraps (and times) the guard."""
    if not _ORIGINAL:
        _ORIGINAL["run_guarded"] = SolidWorksSession.run_guarded
        _ORIGINAL["connect"] = SolidWorksSession.connect
        SolidWorksSession.run_guarded = _run_guarded
        SolidWorksSession.connect = _connect
