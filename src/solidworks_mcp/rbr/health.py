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


def warnings_for(documents: int | None, memory: dict | None) -> list:
    """The health warnings the numbers justify; pure, unit-tested."""
    warn = []
    if documents is not None and documents > DOCS_WARN:
        warn.append(f"{documents} documents are open (> {DOCS_WARN}): every modelling call rebuilds "
                    "slower; close_hidden_documents closes the invisible, saved ones.")
    if memory:
        pct = memory.get("memory_pct")
        if pct is not None and pct > MEMORY_WARN_PCT:
            warn.append(f"SLDWORKS.exe holds {memory.get('working_set_mb')} MB "
                        f"({pct}% of RAM, > {MEMORY_WARN_PCT:.0f}%): save and restart SolidWorks "
                        "before it degrades or crashes.")
    return warn


# --- shedding documents (COM thread, via the worker) ----------------------------------------

def close_hidden_documents(session, keep: list | None = None) -> dict:
    """Close every open document that is invisible, saved and not owned elsewhere.

    Assemblies silently open their parts; after a day the count passes 70 and
    everything crawls. keep lists titles to spare (the active document, the
    current model, dirty and visible documents are always spared).
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
    own = guard.owners()
    closed, kept = [], []
    for doc in guard._documents(session):
        try:
            title = doc.GetTitle()
            why = None
            if title == active_title or title == current_title:
                why = "active"
            elif title.lower() in spare or os.path.splitext(title)[0].lower() in spare:
                why = "keep"
            elif bool(doc.Visible):
                why = "visible"
            elif bool(doc.GetSaveFlag()):
                why = "unsaved changes"
            else:
                owner = own.get(title)
                if owner and owner.get("pid") not in (None, os.getpid()) and guard._pid_alive(int(owner["pid"])):
                    why = f"owned by pid {owner['pid']}"
            if why:
                kept.append({"title": title, "why": why})
            else:
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
    return {"ok": True, "closed": closed, "kept": kept, "open_now": remaining}
