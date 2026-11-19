"""Guard: keep SolidWorks usable when a call fails, and keep clients from tripping over each other.

Wraps SolidWorksSession.run_guarded (inside the journal wrapper) on the COM thread:

1. UI release. Upstream's run_guarded sets CommandInProgress and switches the
   view/feature tree off for the call, and restores them in `finally`. After a
   COM fault (RPC_E_SERVERFAULT from a 49 s shell on 2026-10-19) that restore
   fails too and SolidWorks stays frozen while idle. Every failed modifying call
   is followed by release_ui(): each flag reset on its own, every open document.
   A CommandInProgress left set by a dead call is cleared before the next one.
2. Read-only tools (READ_ONLY) skip the snapshot and the UI freeze/redraw:
   ~0.5 s less per call on a heavy surface part. CommandInProgress IS set for
   them (restored after): it makes out-of-process COM calls ~100x faster, and
   the heavy reads (list_features, read_sketch, list_equations) make thousands.
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

import pythoncom

from .. import binding
from ..errors import SolidWorksError
from ..session import SolidWorksSession
from . import journal

# Tools that only read: no snapshot, no UI freeze or redraw (CommandInProgress is still set).
READ_ONLY = frozenset({
    "get_status", "list_documents", "get_mass_properties", "get_bounding_box", "list_faces", "list_edges",
    "list_bodies", "list_features", "list_dimensions", "read_sketch", "list_equations", "list_components",
    "get_assembly_bounding_box", "get_custom_properties", "list_configurations", "shell_check",
    "release_ui", "flow_goals", "flow_info", "render_view", "section_view", "section_profile",
    "read_sketches", "find_faces", "find_edges", "list_external_refs", "api_search", "count_closable",
})

# Tools that switch the current document: ownership is checked on the document they act on, not before.
SWITCHERS = frozenset({"new_part", "new_assembly", "open_part", "open_assembly", "activate_document"})

# COM HRESULTs after which SolidWorks' state cannot be trusted
FAULTS = {
    -2147417851: "RPC_E_SERVERFAULT",
    -2147417848: "RPC_E_DISCONNECTED",
    -2147023174: "RPC_S_SERVER_UNAVAILABLE",
    -2147023170: "RPC_S_CALL_FAILED",
    -2147023169: "RPC_S_CALL_FAILED_DNE",
    -2147023179: "RPC_S_UNKNOWN_IF",  # the stub went with a restarted SolidWorks (journal 2026-11-02/03)
    -2147418111: "RPC_E_CALL_REJECTED",
}

MUTEX_NAME = "Local\\solidworks-mcp-rbr"
LOCK_WAIT_S = float(os.environ.get("SWMCP_LOCK_WAIT_S", "30"))
OWNER_MIN = float(os.environ.get("SWMCP_OWNER_MIN", "10"))
BUSY_FAIL_S = float(os.environ.get("SWMCP_BUSY_FAIL_S", "20"))

_ORIGINAL = {}
_state_lock = threading.Lock()
_state = {"tool": None, "since": None, "doc": None, "calls": 0, "faults": 0, "releases": 0,
          "last_fault": None, "last_release": None, "quarantine": None, "sw_pid": None,
          # last_call: the last FINISHED call {tool, doc, at, ts, ok, error}; lost: what to tell the
          # next caller after SolidWorks went away and a new one was attached (said once)
          "last_call": None, "lost": None}


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
        """What the holder file says: the last process that took the lock (it may be done)."""
        try:
            with open(self._holder_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def held_by(self) -> dict | None:
        """Who holds the lock at this moment, or None when it is free.

        The holder file alone is not the answer: it kept naming the last caller
        while nothing ran (server_health showed a lock_holder for minutes of idle
        time, and one of a process that had exited). The mutex itself is asked: if
        it can be taken without waiting, nobody holds it. Call this from a thread
        other than the COM thread (a mutex is re-entrant for its owner)."""
        if self._win:
            import win32event
            try:
                if self._handle is None:
                    self._handle = win32event.CreateMutex(None, False, MUTEX_NAME)
                rc = win32event.WaitForSingleObject(self._handle, 0)
            except Exception:  # noqa: BLE001 - unknown: say what the file says
                return self.holder()
            if rc in (0, 0x80):
                try:
                    win32event.ReleaseMutex(self._handle)
                except Exception:  # noqa: BLE001
                    pass
                return None
            return self.holder() or {"pid": None, "tool": None, "since": None}
        if self._local.acquire(blocking=False):
            self._local.release()
            return None
        return self.holder()

    def _forget_holder(self) -> None:
        """Empty the holder file when it names this process (the call is over)."""
        try:
            if (self.holder() or {}).get("pid") == os.getpid():
                os.remove(self._holder_path())
        except OSError:
            pass

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
        self._forget_holder()
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

_owners_lock = threading.Lock()   # claim() runs on the COM thread, server_health prunes on another


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


def live_entries(data: dict, alive) -> tuple:
    """(entries whose process is alive, titles dropped); pure. `alive` is pid -> bool."""
    kept, dropped, verdicts = {}, [], {}
    for title, entry in data.items():
        try:
            pid = int((entry or {}).get("pid"))
        except (TypeError, ValueError):
            dropped.append(title)
            continue
        if pid not in verdicts:
            verdicts[pid] = bool(alive(pid))
        if verdicts[pid]:
            kept[title] = entry
        else:
            dropped.append(title)
    return kept, dropped


def prune_owners() -> dict:
    """Drop the ownership records of processes that are gone and return the rest.

    The file only ever grew: a month of sessions left 130 records, nearly all of
    server processes that had exited (the oldest two weeks old). Ownership
    protects a document from another LIVE server process; a dead one protects
    nothing and only hides the live ones in server_health."""
    with _owners_lock:
        data = owners()
        kept, dropped = live_entries(data, _server_alive)
        if dropped:
            try:
                _save_owners(kept)
            except OSError:
                pass
    return kept


_SERVER_IMAGES = ("python", "solidworks-mcp")


def _server_alive(pid: int) -> bool:
    """Is `pid` a live server process? Alive, and - where the image name can be read -
    a Python or solidworks-mcp executable (Windows reuses the pids of dead processes)."""
    if pid == os.getpid():
        return True
    if not _pid_alive(pid):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        import ctypes.wintypes as wt
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return True
            return os.path.basename(buf.value).lower().startswith(_SERVER_IMAGES)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - cannot tell: keep the record
        return True


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
        with _owners_lock:
            data, _dropped = live_entries(owners(), _server_alive)   # the dead go whenever the file is written
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


def release_ui(session, dry_run: bool = False, _retry: bool = True, clear_quarantine: bool = False) -> dict:
    """Clear CommandInProgress and switch drawing back on in every open document.

    Each setting is reset on its own, so one failure does not leave the rest.
    dry_run reports what is switched off without changing it.
    """
    report = {"ok": True, "command_in_progress": None, "documents": [], "errors": []}
    if session._sw is None:
        _ORIGINAL.get("connect", SolidWorksSession.connect)(session)
    flagged = None
    try:
        report["command_in_progress"] = flagged = bool(session._sw.CommandInProgress)
        # The flag stays SET while the documents are walked and is cleared at the end: with it
        # the walk's ~10 COM calls per document are fast, without it they took 0.2 s per
        # document - 11.5-12.3 s with 56 documents open (measured 2026-11-16), on the first
        # call of every server process and after a refused call that left the UI switched off.
        if not flagged:
            session._sw.CommandInProgress = True
    except Exception as exc:  # noqa: BLE001
        if _retry and getattr(exc, "hresult", None) in FAULTS:  # the link died: attach to the running SolidWorks
            session._sw = session._model = None
            _ORIGINAL.get("connect", SolidWorksSession.connect)(session)
            return release_ui(session, dry_run, _retry=False, clear_quarantine=clear_quarantine)
        report["errors"].append(f"CommandInProgress: {exc}")
    try:
        _release_documents(session, report, dry_run)
    finally:
        try:
            session._sw.CommandInProgress = bool(flagged) if dry_run else False
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"CommandInProgress: {exc}")
    if not dry_run:
        # Never touch _quiet_depth: when this runs inside a guarded call (a script, or
        # connect() from _ensure) upstream's _quiet_ui still has to count itself out.
        if not session._quiet_depth:
            session._to_wake = []
        _set(releases=_state["releases"] + 1, last_release=time.strftime("%H:%M:%S"))
    report["dry_run"] = dry_run
    if clear_quarantine and not dry_run:
        _set(quarantine=None)
    report["quarantine"] = _state.get("quarantine")
    report["frozen"] = bool(report["command_in_progress"]) or any(d["switched_off"] for d in report["documents"])
    return report


def _release_documents(session, report: dict, dry_run: bool) -> None:
    """Switch drawing back on in every open document (each setting on its own)."""
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


def _left_frozen(session) -> bool:
    """After a refused call: is CommandInProgress or the current document's
    drawing still switched off? Unknown counts as yes."""
    try:
        if session._sw is None:
            return False
        if session._sw.CommandInProgress or session._to_wake:
            return True
        doc = session._model
        if doc is None:
            return False
        mod = session._mod
        view = binding.wrap(doc.ActiveView, mod.IModelView)
        fm = binding.wrap(doc.FeatureManager, mod.IFeatureManager)
        sk = binding.wrap(doc.SketchManager, mod.ISketchManager)
        return not all(getattr(target, prop) for target, prop in (
            (view, "EnableGraphicsUpdate"), (fm, "EnableFeatureTree"), (fm, "EnableFeatureTreeWindow"),
            (sk, "DisplayWhenAdded")) if target is not None)
    except Exception:  # noqa: BLE001
        return True


def _clear_stale_flag(session) -> None:
    """A CommandInProgress still set while we hold the lock was left by a dead call."""
    try:
        if session._sw is not None and session._sw.CommandInProgress:
            session._sw.CommandInProgress = False
    except Exception:  # noqa: BLE001 - _reattach_if_gone in the call deals with a dead link
        pass


QUARANTINE_FAULTS = {-2147417851}  # RPC_E_SERVERFAULT: SolidWorks failed inside the call


def _sw_pid(session):
    try:
        return int(session._sw.GetProcessID())
    except Exception:  # noqa: BLE001
        return None


def _check_quarantine(session, tool: str) -> None:
    """After a SERVERFAULT, refuse changes until SolidWorks is a new process (or it is cleared)."""
    q = _state.get("quarantine")
    if not q:
        return
    try:
        session._reattach_if_gone()  # a restarted SolidWorks: attach to it first
    except Exception:  # noqa: BLE001
        pass
    pid = _sw_pid(session)
    if pid is not None and pid != q.get("sw_pid"):
        _set(quarantine=None)
        return
    raise SolidWorksError(
        f"SolidWorks failed inside '{q['tool']}' at {q['at']} (RPC_E_SERVERFAULT) and its state is not "
        "trustworthy: changes are refused until SolidWorks is restarted (reading still works). Files on disk "
        "are as last saved. After the restart this clears by itself; release_ui(clear_quarantine=True) "
        "clears it now. Do not repeat the same operation on the same geometry without changing its inputs.")


def _title(session):
    try:
        return session._model.GetTitle() if session._model is not None else None
    except Exception:  # noqa: BLE001
        return None


# --- SolidWorks gone: say so, with the last call (not 'operation unavailable') -------------------

def _lost_text(old_pid, new_pid=None) -> str:
    """The readable account of a SolidWorks that is gone: last call, and its own journal's end."""
    from . import health
    last = _state.get("last_call")
    evidence = None
    try:
        evidence = health.crash_evidence(since=last.get("ts")) if last else None
    except Exception:  # noqa: BLE001 - the account must not fail on its footnote
        pass
    return health.lost_message(old_pid, last, evidence, new_pid, during=_state.get("tool"))


def _gone_for_good(pid) -> bool:
    from . import health
    return bool(pid) and os.name == "nt" and not health.process_alive(pid)


def _remember(tool: str, started: str, ok: bool, error=None) -> None:
    _set(last_call={"tool": tool, "doc": _state.get("doc"), "at": started, "ts": time.time(), "ok": bool(ok),
                    "error": None if ok else str(error)[:400]})


def _take_lost() -> str | None:
    with _state_lock:
        note, _state["lost"] = _state.get("lost"), None
    return note


def _explain_lost_link(self):
    """Upstream's account of a link that died mid-call, made specific when the process is gone."""
    previous = _state.get("sw_pid")
    lost = _ORIGINAL["explain"](self)
    if lost is not None and _gone_for_good(previous):
        new_pid = _sw_pid(self) if self._sw is not None else None
        _take_lost()  # said right here
        return SolidWorksError(_lost_text(previous, new_pid if new_pid != previous else None))
    return lost


def _run_guarded(self, fn, *args, **kwargs):
    """The guarded call, remembered as the last one, with the news of a lost SolidWorks attached."""
    tool = journal.tool_name(fn)
    started = time.strftime("%H:%M:%S")
    try:
        result = _guarded(self, fn, *args, **kwargs)
    except Exception as exc:
        _remember(tool, started, False, f"{type(exc).__name__}: {exc}")
        note = _take_lost()
        if note and isinstance(exc, SolidWorksError) and note not in str(exc):
            raise SolidWorksError(f"{exc} [{note}]") from exc
        raise
    ok = result.get("ok", True) if isinstance(result, dict) else True
    _remember(tool, started, ok, None if ok else result.get("error"))
    note = _take_lost()
    if note and isinstance(result, dict):
        result["solidworks_lost"] = note
    return result


def _guarded(self, fn, *args, **kwargs):
    tool = journal.tool_name(fn)
    read_only = tool in READ_ONLY
    _lock.acquire(tool, LOCK_WAIT_S)
    _set(tool=tool, since=time.time(), doc=_title(self), calls=_state["calls"] + 1)
    try:
        if read_only:
            self._reattach_if_gone()
            busy = None
            try:
                # CommandInProgress makes out-of-process COM calls ~100x faster
                # (upstream's pinned measurement), and a read that walks the tree
                # makes thousands of them: list_features spent 16-21 s without it
                # (journal through 2026-11-03); a live A/B on a 93-feature part
                # measured 4.1 s -> 0.3 s. Snapshot and UI freeze stay skipped.
                busy = self._command_in_progress(True)
                journal.note(self, "before")
                try:
                    return fn(*args, **kwargs)
                finally:
                    journal.note(self, "after")
            except pythoncom.com_error as exc:
                # the link died during the read itself (the probe above had just passed)
                if getattr(exc, "hresult", None) in SolidWorksSession._GONE_HRESULTS:
                    _set(faults=_state["faults"] + 1,
                         last_fault=f"{time.strftime('%H:%M:%S')} {tool}: {FAULTS.get(exc.hresult, exc.hresult)}")
                    lost = self._explain_lost_link()
                    if lost is not None:
                        raise lost from exc
                raise
            finally:
                if busy is not None:
                    try:
                        self._command_in_progress(busy)
                    except Exception:  # noqa: BLE001 - the link died during the read: nothing to restore
                        pass
        _check_quarantine(self, tool)
        if tool not in SWITCHERS:
            check_owner(_title(self), tool)
        _clear_stale_flag(self)
        # CommandInProgress BEFORE upstream's snapshot: with the rollback bar inside the
        # tree that snapshot reads every feature name, 20.7 s without the flag on a
        # 205-feature part against 0.3 s with it (measured 2026-11-16).
        try:
            flagged = self._command_in_progress(True)
        except Exception:  # noqa: BLE001 - a dead link: upstream reattaches and sets it itself
            flagged = None
        try:
            journal.note(self, "before")
            try:
                result = _ORIGINAL["run_guarded"](self, fn, *args, **kwargs)
            finally:
                journal.note(self, "after")
                try:
                    self._command_in_progress(flagged)
                except Exception:  # noqa: BLE001 - the link died in the call
                    pass
        except Exception as exc:
            # a lost link arrives as a SolidWorksError raised from the COM error
            hresult = getattr(exc, "hresult", None) or getattr(exc.__cause__, "hresult", None)
            if hresult in FAULTS:
                _set(faults=_state["faults"] + 1, last_fault=f"{time.strftime('%H:%M:%S')} {tool}: {FAULTS[hresult]}")
            try:
                # A refusal (a tool's own SolidWorksError) leaves upstream's restore intact:
                # the full release walks every open document, 5-7 s with 48 of them open,
                # so it runs only when something really is still switched off.
                if not isinstance(exc, SolidWorksError) or hresult in FAULTS or _left_frozen(self):
                    release_ui(self)
            except Exception:  # noqa: BLE001 - the original error is what the caller needs
                pass
            if hresult in QUARANTINE_FAULTS:
                _set(quarantine={"tool": tool, "at": time.strftime("%H:%M:%S"), "sw_pid": _sw_pid(self),
                                 "doc": _state.get("doc")})
                raise SolidWorksError(
                    f"SolidWorks failed inside '{tool}' (RPC_E_SERVERFAULT, its own crash, not a refusal). "
                    "The UI was released, but SolidWorks' state is not trustworthy: further changes are refused "
                    "until it is restarted; save nothing from it. Do not repeat this operation on this geometry "
                    "without changing its inputs.") from exc
            raise
        claim(_title(self), tool)
        if tool in HEAVY and isinstance(result, dict):
            _note_load(self, result)
        return result
    finally:
        _set(tool=None, since=None)
        _lock.release()


DOCS_WARN = int(os.environ.get("SWMCP_DOCS_WARN", "30"))

# Tools that load documents or build on many of them: their reply carries the load warnings.
HEAVY = SWITCHERS | {"insert_component", "insert_part", "run_script", "derive_part", "make_drawing", "export",
                     "tree_folder", "rebuild"}
# Session methods that open a document WINDOW (about 250 GDI objects each), also from a script.
WINDOW_MAKERS = ("new_part", "new_assembly", "open_part", "open_assembly")


def load_hints(documents: int | None, gui: dict | None) -> list:
    """The warnings a heavy call's reply carries; pure. documents: open documents; gui: health.sldworks_gui."""
    from . import health
    hints = []
    if documents is not None and documents > DOCS_WARN:
        hints.append(f"{documents} documents are open (> {DOCS_WARN}): calls slow down with every one. "
                     "close_hidden_documents(dry_run=True) shows which saved, unowned ones it would close; "
                     "close_hidden_documents() closes them (windows=True takes saved document windows too).")
    if gui and gui.get("gdi_pct", 0) >= health.GDI_WARN_PCT:
        hints.append(f"SolidWorks holds {gui['gdi']} of {gui['gdi_quota']} GDI objects ({gui['gdi_pct']}%): about "
                     f"{gui.get('windows_left', 0)} more document windows fit before it fails and crashes. "
                     "Close document windows now; save and restart SolidWorks if the number stays high.")
    return hints


def _note_load(session, result: dict) -> None:
    """After a heavy call: say so when too many documents are open (one COM call) or SolidWorks
    is running out of window resources (no COM call)."""
    from . import health
    count = gui = None
    try:
        count = int(session._sw.GetDocumentCount())
    except Exception:  # noqa: BLE001
        pass
    try:
        gui = health.sldworks_gui(_state.get("sw_pid") if not _gone_for_good(_state.get("sw_pid")) else None)
    except Exception:  # noqa: BLE001
        pass
    hints = load_hints(count, gui)
    if not hints:
        return
    if count is not None and count > DOCS_WARN:
        result["documents_open"] = count
    if gui and gui.get("gdi_pct", 0) >= health.GDI_WARN_PCT:
        result["gdi_objects"] = {k: gui[k] for k in ("gdi", "gdi_quota", "gdi_pct", "windows_left")}
    result["hint"] = " ".join(([str(result["hint"])] if result.get("hint") else []) + hints)


def _window_guard(name: str, original):
    """`original` (a session method that opens a document window), refused when SolidWorks has
    no GDI objects left for one: the refusal costs nothing, the window would cost the session."""
    import functools

    @functools.wraps(original)
    def guarded(self, *args, **kwargs):
        from . import health
        try:
            pid = _state.get("sw_pid")
            refusal = health.gui_refusal(health.sldworks_gui(None if _gone_for_good(pid) else pid))
        except Exception:  # noqa: BLE001 - the check never stands in the way
            refusal = None
        if refusal:
            raise SolidWorksError(f"{name} refused. {refusal}")
        return original(self, *args, **kwargs)

    guarded._rbr_window_guard = True
    return guarded


_releasing = threading.local()


def _connect(self):
    """Attach as upstream does, then clear whatever a previous server or a crash left frozen.
    When the SolidWorks attached before is gone, say so - with the last call - instead of
    passing on 'no running SolidWorks' / 'operation unavailable'."""
    previous = _state.get("sw_pid")
    try:
        result = _ORIGINAL["connect"](self)
    except SolidWorksError as exc:
        if _gone_for_good(previous):
            raise SolidWorksError(_lost_text(previous)) from exc
        raise
    pid = _sw_pid(self)
    if previous and pid and pid != previous and _gone_for_good(previous):
        _set(lost=_lost_text(previous, pid))   # told to the caller of the call in progress
    _set(sw_pid=pid)   # for the checks that must not call into a busy SolidWorks
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
        _ORIGINAL["explain"] = SolidWorksSession._explain_lost_link
        SolidWorksSession.run_guarded = _run_guarded
        SolidWorksSession.connect = _connect
        SolidWorksSession._explain_lost_link = _explain_lost_link
        for name in WINDOW_MAKERS:
            _ORIGINAL[name] = getattr(SolidWorksSession, name)
            if not getattr(_ORIGINAL[name], "_rbr_window_guard", False):
                setattr(SolidWorksSession, name, _window_guard(name, _ORIGINAL[name]))
        journal.GUARDED = True


def unapply() -> None:
    """Restore the upstream methods (test teardown); apply() works again after."""
    if _ORIGINAL:
        SolidWorksSession.run_guarded = _ORIGINAL["run_guarded"]
        SolidWorksSession.connect = _ORIGINAL["connect"]
        SolidWorksSession._explain_lost_link = _ORIGINAL["explain"]
        for name in WINDOW_MAKERS:
            if getattr(getattr(SolidWorksSession, name), "_rbr_window_guard", False):
                setattr(SolidWorksSession, name, _ORIGINAL[name])  # not when another patch wraps it by now
        _ORIGINAL.clear()
        journal.GUARDED = False
