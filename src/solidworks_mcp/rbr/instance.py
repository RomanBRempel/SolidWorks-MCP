"""Which SolidWorks this server drives, when more than one is running (S-15).

Upstream attaches with GetActiveObject("SldWorks.Application"): whichever
instance registered first. Every SolidWorks process also registers itself in
the Running Object Table as "SolidWorks_PID_<pid>" (seen on SW 2025 SP3 next
to the open documents' file monikers), so one instance can be picked by its
process id:

- SWMCP_SW_PID=<pid> in the server's environment: attach to that instance
  instead of the first one. That is all a server needs to work in a second
  SolidWorks of its own while the user's stays untouched.
- instances(): the running instances with their memory, and which one this
  server holds - no COM call into SolidWorks, so it answers while one is busy.

Starting the second instance is not done from here: it takes a licence check,
~half a minute and a few hundred MB, and belongs to whoever owns the machine.
"""

import os
import re

from .. import binding
from ..errors import SolidWorksError
from . import health

PID_ENV = "SWMCP_SW_PID"
ROT_NAME = re.compile(r"^SolidWorks_PID_(\d+)$", re.IGNORECASE)

_ORIGINAL = {}


def pid_of(display_name: str) -> int | None:
    """The process id in a ROT display name 'SolidWorks_PID_9816', else None; pure."""
    match = ROT_NAME.match(str(display_name or "").lstrip("!").strip())
    return int(match.group(1)) if match else None


def wanted_pid(environ=None) -> int | None:
    """The instance the environment asks for (SWMCP_SW_PID), or None; pure. A value that
    is not a process id is refused rather than ignored."""
    value = (os.environ if environ is None else environ).get(PID_ENV, "").strip()
    if not value:
        return None
    if not (value.isascii() and value.isdigit()):
        raise SolidWorksError(f"{PID_ENV} must be the process id of a running SolidWorks (got {value!r}).")
    return int(value)


def _rot_monikers() -> dict:
    """{pid: (running object table, moniker)} of the SolidWorks instances registered there."""
    import pythoncom
    table = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    found = {}
    for moniker in table:
        try:
            pid = pid_of(moniker.GetDisplayName(context, None))
        except Exception:  # noqa: BLE001 - a moniker without a display name is not ours
            continue
        if pid is not None:
            found[pid] = (table, moniker)
    return found


def instances(attached_pid: int | None = None) -> dict:
    """The SolidWorks processes running on this machine: pid, memory, whether it is
    registered for automation, and which one this server is attached to."""
    registered = {}
    try:
        import pythoncom
        pythoncom.CoInitialize()
        try:
            registered = _rot_monikers()
        finally:
            pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001 - the process list still stands
        registered = {}
    rows = []
    for pid in sorted(set(health.find_processes()) | set(registered)):
        memory = health.process_memory(pid) or {}
        rows.append({"pid": pid,
                     "working_set_mb": round(memory.get("working_set_bytes", 0) / 1048576) if memory else None,
                     "private_mb": round(memory.get("private_bytes", 0) / 1048576) if memory else None,
                     "registered": pid in registered, "attached": pid == attached_pid})
    total = health.total_memory() or {}
    out = {"ok": True, "count": len(rows), "instances": rows, "attached_pid": attached_pid,
           "requested_pid": None, "ram_mb": round(total.get("total_bytes", 0) / 1048576) or None,
           "system_load_pct": total.get("system_load_pct")}
    try:
        out["requested_pid"] = wanted_pid()
    except SolidWorksError as exc:
        out["warning"] = str(exc)
    if len(rows) > 1 and out["requested_pid"] is None:
        out["note"] = (f"{len(rows)} SolidWorks instances are running and {PID_ENV} is not set: the server "
                       "attaches to the one that registered first.")
    return out


def connect_to(pid: int):
    """An early-bound ISldWorks of the instance with that process id (as binding.connect
    gives for the first one). Must run on the COM thread."""
    import win32com.client
    registered = _rot_monikers()
    if pid not in registered:
        running = sorted(registered) or "none"
        raise SolidWorksError(
            f"No SolidWorks with process id {pid} is registered for automation (running: {running}). "
            f"Start it first, or clear {PID_ENV} to attach to the first instance.")
    table, moniker = registered[pid]
    import pythoncom
    raw = win32com.client.Dispatch(table.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
    revision = win32com.client.dynamic.DumbDispatch(raw._oleobj_).RevisionNumber
    binding._mod = binding._load_module(revision)
    return binding.wrap(raw, binding._mod.ISldWorks)


def _connect():
    pid = wanted_pid()
    if pid is None:
        return _ORIGINAL["connect"]()
    return connect_to(pid)


def apply() -> None:
    """binding.connect honours SWMCP_SW_PID (once)."""
    if _ORIGINAL:
        return
    _ORIGINAL["connect"] = binding.connect
    binding.connect = _connect


def unapply() -> None:
    if _ORIGINAL:
        binding.connect = _ORIGINAL["connect"]
        _ORIGINAL.clear()
