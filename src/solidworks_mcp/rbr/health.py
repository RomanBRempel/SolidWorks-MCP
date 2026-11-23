"""Load watching: SolidWorks' memory and open documents, and a way to shed them.

A long session piles up invisible documents (assemblies open their parts) and
SLDWORKS.exe memory grows until every call crawls. server_health gains the
numbers and a warning; close_hidden_documents closes what is invisible, saved
and unowned, which brings call times back to seconds.

The memory numbers come from the Windows API via ctypes (no psutil needed) and
never touch SolidWorks, so they are safe while a call runs.
"""

import ctypes
import ctypes.wintypes as wt
import os

from ..errors import SolidWorksError
from .. import binding
from . import guard

DOCS_WARN = int(os.environ.get("SWMCP_DOCS_WARN", "30"))
MEMORY_WARN_PCT = float(os.environ.get("SWMCP_MEMORY_WARN_PCT", "70"))


# --- Windows process numbers (any thread, no COM) -------------------------------------------

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


def total_memory() -> dict | None:
    """Physical memory of the machine: total bytes and the system-wide load %."""
    if os.name != "nt":
        return None
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {"total_bytes": int(status.ullTotalPhys), "system_load_pct": int(status.dwMemoryLoad)}


def _image_name(handle) -> str:
    size = wt.DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return os.path.basename(buf.value)
    return ""


def find_processes(image: str = "SLDWORKS.exe") -> list:
    """The pids whose executable is `image` (case-blind); [] off Windows."""
    if os.name != "nt":
        return []
    count = 4096
    pids = (wt.DWORD * count)()
    returned = wt.DWORD()
    if not ctypes.windll.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(returned)):
        return []
    wanted = image.lower()
    found = []
    for pid in pids[: returned.value // ctypes.sizeof(wt.DWORD)]:
        if not pid:
            continue
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            if _image_name(handle).lower() == wanted:
                found.append(int(pid))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    return found


def process_memory(pid: int) -> dict | None:
    """Working set and private bytes of one process."""
    if os.name != "nt":
        return None
    handle = ctypes.windll.kernel32.OpenProcess(0x1010, False, pid)  # QUERY_LIMITED | VM_READ
    if not handle:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(counters)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return {"working_set_bytes": int(counters.WorkingSetSize), "private_bytes": int(counters.PrivateUsage)}
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def sldworks_memory() -> dict | None:
    """SLDWORKS.exe memory against the machine's RAM, without touching COM."""
    pids = find_processes()
    if not pids:
        return None
    total = total_memory()
    out = {"pids": pids}
    working = 0
    for pid in pids:
        mem = process_memory(pid)
        if mem:
            working += mem["working_set_bytes"]
    out["working_set_mb"] = round(working / 1024 / 1024)
    if total and total.get("total_bytes"):
        out["memory_pct"] = round(100.0 * working / total["total_bytes"], 1)
        out["system_load_pct"] = total["system_load_pct"]
    return out


# --- window resources (GDI / USER objects): what SolidWorks really runs out of ----------------
#
# A Windows process may hold GDIProcessHandleQuota GDI objects (10 000 unless the registry says
# otherwise). SLDWORKS.exe starts with about 2 000; every document WINDOW takes about 250 more
# (251 measured on SW 2025 SP3, 0 for a document loaded without a window), and opening a file
# into a window leaves 37-45 behind for good after it is closed. At the quota SolidWorks cannot
# create a device context: its message boxes say 'the requested resource is unavailable', API
# calls fail with error 61704 and it then dies in its own drawing code - Windows records a
# GDIObjectLeak report (Application log, Windows Error Reporting, event 1001), not an
# Application Error 1000. That, not memory, ended the sessions of 2026-11-18 and 11-19.

GDI_WARN_PCT = float(os.environ.get("SWMCP_GDI_WARN_PCT", "70"))
GDI_REFUSE_PCT = float(os.environ.get("SWMCP_GDI_REFUSE_PCT", "80"))
GDI_PER_WINDOW = 250
_QUOTA_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows"


def gui_quota(name: str = "GDIProcessHandleQuota") -> int:
    """The per-process GDI (or USER) object quota of this machine; 10 000 when it cannot be read."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _QUOTA_KEY) as key:
            value = int(winreg.QueryValueEx(key, name)[0])
        return value if value > 0 else 10000
    except Exception:  # noqa: BLE001 - not Windows, or the value is absent
        return 10000


def gui_resources(pid: int) -> dict | None:
    """GDI and USER objects of one process, now and at their peak (GetGuiResources); no COM."""
    if os.name != "nt":
        return None
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        read = ctypes.windll.user32.GetGuiResources
        gdi, user = int(read(handle, 0)), int(read(handle, 1))
        if not gdi and not user:
            return None
        return {"gdi": gdi, "user": user, "gdi_peak": int(read(handle, 2)), "user_peak": int(read(handle, 4))}
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def gui_load(counts: dict | None, quota: int = 10000) -> dict | None:
    """`counts` with the quota, the share of it in use and the windows that still fit; pure."""
    if not counts:
        return None
    gdi = int(counts.get("gdi") or 0)
    return {**counts, "gdi_quota": int(quota), "gdi_pct": round(100.0 * gdi / quota, 1),
            "windows_left": max(0, (int(quota) - gdi) // GDI_PER_WINDOW)}


def sldworks_gui(pid: int | None = None) -> dict | None:
    """Window resources of SolidWorks: of `pid`, else of the SLDWORKS.exe that holds the most."""
    quota = gui_quota()
    best = None
    for candidate in ([pid] if pid else find_processes()):
        load = gui_load(gui_resources(candidate), quota)
        if load and (best is None or load["gdi"] > best["gdi"]):
            best = {"pid": int(candidate), **load}
    return best


def gui_refusal(load: dict | None, what: str = "open another document window") -> str | None:
    """Why a window must not be opened now, or None while there is room; pure."""
    if not load or load.get("gdi_pct", 0) < GDI_REFUSE_PCT:
        return None
    return (f"SolidWorks holds {load['gdi']} of the {load['gdi_quota']} GDI objects Windows allows one process "
            f"({load['gdi_pct']}%): it cannot {what} without failing ('the requested resource is unavailable', "
            "then a crash). Save the work, close document windows (close_hidden_documents takes the invisible "
            "ones; visible ones close with activate_document + close_part), and restart SolidWorks if the "
            "number stays high - closed windows do not give everything back. open_part / open_assembly with "
            "visible=False load a file without a window (no GDI objects). server_health shows the count.")


def process_alive(pid: int | None, image: str = "SLDWORKS.exe") -> bool:
    """Is `pid` a running process with that executable? (Windows hands a dead pid to the next process.)"""
    if not pid or os.name != "nt":
        return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = wt.DWORD(0)
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
            return False  # 259 = STILL_ACTIVE
        return _image_name(handle).lower() == image.lower()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


# --- what SolidWorks itself wrote down when it died -----------------------------------------------

RESOURCE_MESSAGES = ("ресурс недоступен", "resource was unavailable", "resource is unavailable")
JOURNAL_TAIL_BYTES = 12000


def journal_evidence(text: str) -> dict:
    """What the end of a SolidWorks journal (swxJRNL.swj) says about how the session ended; pure.

    stack: the crash stack SolidWorks appends ('module:function', innermost first, system
    frames it could not name left out); resource_messages: how many message boxes about an
    unavailable resource precede it (GDI objects exhausted); last_call: the last API line."""
    lines = text.splitlines()
    stack, in_stack = [], False
    for line in lines:
        if "stack trace for this error" in line:
            stack, in_stack = [], True
            continue
        if in_stack and line.startswith("'{") and "\t" in line:
            frame = line.split("\t", 1)[1].strip()
            if not frame.endswith(":Unknown") or not stack:
                stack.append(frame)
    resource = sum(1 for line in lines if any(m in line.lower() for m in RESOURCE_MESSAGES))
    last_call = next((line.strip() for line in reversed(lines)
                      if line.strip() and not line.lstrip().startswith("'")), None)
    return {"stack": stack[:6], "resource_messages": resource, "last_call": last_call}


def crash_evidence(since: float | None = None) -> dict | None:
    """The journal of the SolidWorks session that just ended, when it records a crash or a
    resource shortage: {journal, written, stack, resource_messages, last_call}; else None.

    Looks at swxJRNL.swj and swxJRNL.BAK (the session before the running one) under
    %APPDATA%\\SolidWorks, newest first, no older than `since` (a time.time() value)."""
    base = os.path.join(os.environ.get("APPDATA") or "", "SolidWorks")
    found = []
    try:
        for folder in os.listdir(base):
            for name in ("swxJRNL.swj", "swxJRNL.BAK"):
                path = os.path.join(base, folder, name)
                if os.path.isfile(path) and os.path.getsize(path):
                    found.append((os.path.getmtime(path), path))
    except OSError:
        return None
    for written, path in sorted(found, reverse=True):
        if since is not None and written < since - 5:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, os.path.getsize(path) - JOURNAL_TAIL_BYTES))
                text = fh.read().decode("mbcs" if os.name == "nt" else "latin-1", errors="replace")
        except OSError:
            continue
        evidence = journal_evidence(text)
        if evidence["stack"] or evidence["resource_messages"]:
            import datetime
            return {"journal": path, "written": datetime.datetime.fromtimestamp(written).isoformat(timespec="seconds"),
                    **evidence}
    return None


def lost_message(old_pid, last_call: dict | None, evidence: dict | None, new_pid=None,
                 during: str | None = None) -> str:
    """The error a caller gets when the SolidWorks this server drove is gone; pure.

    Says that SolidWorks crashed (or was closed), which call was the last one, and what
    SolidWorks' own journal recorded - instead of the COM text 'operation unavailable'."""
    parts = [f"SolidWorks crashed or was closed: its process (pid {old_pid}) is gone"
             + (f", noticed in '{during}'" if during else "") + "."]
    if last_call:
        ended = "ok" if last_call.get("ok", True) else f"failed: {str(last_call.get('error') or '')[:300]}"
        parts.append(f"Last call before that: '{last_call.get('tool')}' on '{last_call.get('doc')}' at "
                     f"{last_call.get('at')} ({ended}).")
    if evidence:
        if evidence.get("resource_messages"):
            parts.append(f"SolidWorks' journal ends with {evidence['resource_messages']} 'resource unavailable' "
                         "message(s): it ran out of GDI objects (too many document windows).")
        if evidence.get("stack"):
            parts.append("It recorded a crash in " + " <- ".join(evidence["stack"][:4]) + f" ({evidence['journal']}).")
    parts.append("Unsaved work in it is lost; files on disk are as last saved.")
    parts.append(f"A new SolidWorks (pid {new_pid}) is running and this server is attached to it: reopen the "
                 "documents and go on." if new_pid else
                 "Start SolidWorks and call again: the server attaches by itself.")
    return " ".join(parts)


def warnings_for(documents: int | None, memory: dict | None, closable: int | None = None,
                 gui: dict | None = None) -> list:
    """The health warnings the numbers justify; pure, unit-tested. closable: how many of
    the documents close_hidden_documents would close now (None: not counted). gui: the
    window resources (sldworks_gui)."""
    warn = []
    if gui and gui.get("gdi_pct", 0) >= GDI_WARN_PCT:
        warn.append(f"SolidWorks holds {gui['gdi']} of {gui['gdi_quota']} GDI objects ({gui['gdi_pct']}%, "
                    f"room for about {gui.get('windows_left', 0)} more document windows): at the limit it "
                    "fails with 'resource unavailable' and crashes. Close document windows; if the number "
                    "stays high, save and restart SolidWorks.")
    if documents is not None and documents > DOCS_WARN:
        would = "" if closable is None else f" - {closable} of them now"
        warn.append(f"{documents} documents are open (> {DOCS_WARN}): every modelling call rebuilds "
                    f"slower; close_hidden_documents closes the invisible, saved ones{would}.")
    if memory:
        pct = memory.get("memory_pct")
        if pct is not None and pct > MEMORY_WARN_PCT:
            warn.append(f"SLDWORKS.exe holds {memory.get('working_set_mb')} MB "
                        f"({pct}% of RAM, > {MEMORY_WARN_PCT:.0f}%): save and restart SolidWorks "
                        "before it degrades or crashes.")
    return warn


# --- shedding documents (COM thread, via the worker) ----------------------------------------

def count_closable(session) -> int:
    """How many documents close_hidden_documents would close now (a quick read for
    server_health: title, visibility and save flag of each document)."""
    return len(close_hidden_documents(session, None, True)["would_close"])


def close_hidden_documents(session, keep: list | None = None, dry_run: bool = False,
                           windows: bool = False) -> dict:
    """Close every open document that is invisible, saved and not owned elsewhere.

    Assemblies silently open their parts; after a day the count passes 70 and
    everything crawls. keep lists titles to spare (the active document, the
    current model, dirty and visible documents are always spared). dry_run only
    lists what would be closed. windows=True also closes the saved documents
    that have a WINDOW (each holds about 250 GDI objects of SolidWorks; a part
    an open assembly uses stays loaded, only its window goes) - the active and
    the current document, unsaved ones and those owned elsewhere still stay.
    """
    sw = session._ensure()
    spare = {str(k).lower() for k in (keep or [])}
    active_title = None
    try:
        active = binding.wrap(sw.ActiveDoc, session._mod.IModelDoc2)
        active_title = active.GetTitle() if active is not None else None
    except Exception:  # noqa: BLE001
        pass
    current_title = None
    try:
        current_title = session._model.GetTitle() if session._model is not None else None
    except Exception:  # noqa: BLE001
        pass
    own = guard.prune_owners()
    closed, kept = [], []
    for doc in guard._documents(session):
        try:
            title = doc.GetTitle()
            why = None
            if title == active_title or title == current_title:
                why = "active"
            elif title.lower() in spare or os.path.splitext(title)[0].lower() in spare:
                why = "keep"
            elif bool(doc.Visible) and not windows:
                why = "visible"
            elif bool(doc.GetSaveFlag()):
                why = "unsaved changes"
            else:
                owner = own.get(title)
                if owner and owner.get("pid") not in (None, os.getpid()):
                    why = f"owned by pid {owner['pid']}"   # prune_owners left only live processes
            if why:
                kept.append({"title": title, "why": why})
            else:
                if not dry_run:
                    sw.CloseDoc(title)
                closed.append(title)
        except Exception as exc:  # noqa: BLE001 - one stubborn document must not stop the sweep
            kept.append({"title": locals().get("title"), "why": f"error: {exc}"})
    remaining = None
    try:
        remaining = int(sw.GetDocumentCount())
    except Exception:  # noqa: BLE001
        pass
    if not closed and not kept:
        raise SolidWorksError("No open documents.")
    if dry_run:
        return {"ok": True, "dry_run": True, "would_close": closed, "kept": kept, "open_now": remaining}
    return {"ok": True, "closed": closed, "kept": kept, "open_now": remaining}
