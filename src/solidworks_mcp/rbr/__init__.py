"""RBR extension of solidworks-mcp, kept out of the upstream files.

Nothing in the upstream package is edited: this subpackage patches
SolidWorksSession at start-up and registers extra tools on the same FastMCP
server, so `git merge upstream/main` stays conflict-free.

Start it with `python -m solidworks_mcp.rbr` (or the solidworks-mcp-rbr script)
instead of `solidworks-mcp`.
"""

import asyncio
import functools
import os

from ..errors import SolidWorksError
from . import fastpath, guard, journal, library, script_tool, tools

__all__ = ["register"]

RBR_VERSION = "1.2.0"

INSTRUCTIONS = """

RBR extension (solidworks-mcp-rbr)
- Batch work goes in ONE run_script call (sw, model, ext, fm, asm/part, comp(), feature(), select(), args, MM, DEG; set `result`). Prefer it over many granular calls on big parts.
- Proven scripts: list_scripts / get_script / save_script / run_saved_script(name, args).
- Every call is journalled with its arguments and full script code: get_journal. tool_source(name) shows the code any tool runs.
- Telemetry: get_journal(since='HH:MM', entries=False) gives per-tool stats from the journal file without touching SolidWorks; never read the journal through run_script. server_health shows what the server is doing right now, also while a call runs.
- If SolidWorks looks frozen while idle (no redraw, tree greyed), call release_ui. A document another server process changed in the last minutes is read-only for you: release_document frees it.
- An error saying SolidWorks failed inside a call (RPC_E_SERVERFAULT) means its own crash: changes are refused until SolidWorks is restarted. Tell the user, and do not retry the same operation on the same geometry with the same inputs.
- Before a shell on a curved/lofted body: shell_check(thickness_mm). A thickness above the smallest convex radius makes the shell fail, sometimes after a long computation.
- New parts and assemblies are switched to MMGS (mm, g) at creation. Also: set_units, get/set_custom_properties, list/activate/add_configuration, export_face_dxf (flat part -> DXF 1:1 for milling), assembly_bom (BOM + mass budget, CSV).
"""


def _patch_new_documents():
    """New parts/assemblies start in MMGS (RBR rule); SWMCP_NEW_DOC_UNITS=off disables it."""
    from ..session import SolidWorksSession
    if getattr(SolidWorksSession, "_rbr_units_patched", False):
        return
    if os.environ.get("SWMCP_NEW_DOC_UNITS", "MMGS").lower() == "off":
        return
    system = os.environ.get("SWMCP_NEW_DOC_UNITS", "MMGS")
    for name in ("new_part", "new_assembly"):
        original = getattr(SolidWorksSession, name)

        def patched(self, *a, _orig=original, **kw):
            res = _orig(self, *a, **kw)
            if isinstance(res, dict) and res.get("ok"):
                try:
                    res["units"] = tools.set_units(self, system)["unit_system"]
                except Exception as exc:  # noqa: BLE001 - the document exists; say what failed
                    res["units_error"] = str(exc)
            return res

        functools.update_wrapper(patched, original)  # keeps name, doc and __wrapped__
        setattr(SolidWorksSession, name, patched)
    SolidWorksSession._rbr_units_patched = True


def _patch_shell_check_result():
    """add_shell that removes no material failed (verified: a 10.5 mm shell on a 19.6 mm wide part
    reported ok with the volume unchanged). Refuse it, so run_guarded rolls the feature back."""
    from ..session import SolidWorksSession
    original = SolidWorksSession.add_shell
    if getattr(original, "_rbr_checked", False):
        return

    def add_shell(self, thickness_mm, open_face="+z", _orig=original):
        before = None
        try:
            ext = self._require_model().Extension
            from .. import binding as _b
            mp = _b.wrap(_b.wrap(ext, self._mod.IModelDocExtension).CreateMassProperty(), self._mod.IMassProperty)
            mp.UseSystemUnits = True
            before = mp.Volume * 1e9
        except Exception:  # noqa: BLE001 - no reference: keep upstream's behaviour
            before = None
        res = _orig(self, thickness_mm, open_face)
        after = (res.get("mass_properties") or {}).get("volume_mm3") if isinstance(res, dict) else None
        if before and after is not None and abs(after - before) <= 1e-6 * before:
            raise SolidWorksError(
                f"The shell removed no material (volume stays {after:.1f} mm^3): somewhere the part is "
                f"thinner than 2 x {thickness_mm} mm, or the offset failed. Rolled back; run shell_check.")
        if isinstance(res, dict) and before:
            res["removed_mm3"] = round(before - (after or before), 3)
        return res

    functools.update_wrapper(add_shell, original)
    add_shell._rbr_checked = True
    SolidWorksSession.add_shell = add_shell


def register(server) -> None:
    """Patch the session and add the RBR tools to `server.mcp` (the upstream FastMCP)."""
    fastpath.apply()
    guard.apply()    # before the journal, so the journal times the guard too
    journal.apply()
    _patch_new_documents()
    _patch_shell_check_result()

    mcp, worker, session, call, com_error = (server.mcp, server._worker, server._session,
                                             server._call, server._COM_ERROR)
    try:
        mcp._mcp_server.instructions = (mcp._mcp_server.instructions or "") + INSTRUCTIONS
    except AttributeError:
        pass

    if worker is not None and not getattr(worker, "_rbr_fail_fast", False):
        original_call = worker.call

        async def call_fail_fast(fn):
            busy = guard.busy_error()  # no queueing behind a call that has run for long
            if busy:
                raise SolidWorksError(busy)
            return await original_call(fn)

        worker.call = call_fail_fast
        worker._rbr_fail_fast = True

    async def _timed(fn, *args, timeout_s: float = 120.0):
        """Like server._call, with a per-call timeout (scripts may run long)."""
        if worker is None:
            return {"ok": False, "error": "SolidWorks tools only work on Windows with SolidWorks."}
        limit = max(5.0, min(float(timeout_s), 1800.0))
        busy = None if fn is guard.release_ui else guard.busy_error()
        if busy:
            return {"ok": False, "error": busy}
        fut = worker.submit(lambda: session.run_guarded(fn, *args))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), limit)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"SolidWorks did not finish within {limit:.0f}s; a modal dialog may be "
                                          "open, or the work is still running (the COM thread stays busy)."}
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        except com_error as exc:
            info = getattr(exc, "excepinfo", None)
            desc = info[2] if info and len(info) > 2 and info[2] else getattr(exc, "strerror", None)
            return {"ok": False, "error": f"SolidWorks COM error: {desc or exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _local(fn, *args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- scripts ---------------------------------------------------------------

    @mcp.tool()
    async def run_script(code: str, timeout_s: float = 120.0) -> dict:
        """Run a Python script against SolidWorks in ONE call: batch work that would take many tool calls.

        Use it for repetitive or bulk operations (delete/suppress/move many
        components, rename, bulk set_dimension, export loops, reading data
        across a part or assembly) instead of calling granular tools one by one.
        The script runs on the server's COM thread with the UI frozen and
        CommandInProgress set; if it raises, what it added to the current part
        is rolled back. The full code is written to the journal.

        Namespace: sw/swApp (ISldWorks, early-bound), mod (typelib wrapper
        module, e.g. mod.IComponent2), wrap(obj, mod.IFace2) to early-bind,
        model (current or active IModelDoc2), active, ext (IModelDocExtension),
        fm (IFeatureManager), selmgr (ISelectionMgr), asm (IAssemblyDoc) or
        part (IPartDoc), comp(name) -> IComponent2 by list_components name,
        feature(name) -> IFeature by tree name, select(objs, append=False) ->
        count selected, session (the server's SolidWorksSession: its methods
        are the tools), MM = 0.001 (API lengths are metres), DEG, args (dict),
        print(...) -> returned as stdout; set `result = ...` to return JSON data.
        The result also gives `features` {before, after, added} when the tree changed.
        Example (delete 3 components with their mates in one step):
            n = select([comp(c) for c in ["Bolt-1", "Bolt-2", "Nut-1"]])
            result = {"selected": n, "deleted": ext.DeleteSelection2(1)}
        timeout_s: answer after this many seconds at most (5..1800).
        """
        return await _timed(script_tool.run_script, session, code, timeout_s=timeout_s)

    @mcp.tool()
    async def run_saved_script(name: str, args: dict | None = None, timeout_s: float = 120.0) -> dict:
        """Run a script from the library (list_scripts) by name; `args` reaches it as the dict `args`."""
        try:
            code = library.code_of(name)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        return await _timed(script_tool.run_script, session, code, args or {}, timeout_s=timeout_s)

    @mcp.tool()
    async def list_scripts() -> dict:
        """Saved scripts (built-in and the user's folder) with their one-line descriptions."""
        return _local(library.list_scripts)

    @mcp.tool()
    async def get_script(name: str) -> dict:
        """The code of a saved script."""
        return _local(library.get_script, name)

    @mcp.tool()
    async def save_script(name: str, code: str, description: str = "") -> dict:
        """Save a proven run_script script to the user's library under `name` (replaces one of that name).

        description becomes its docstring when the code has none. Read `args`
        for parameters so one script serves many parts.
        """
        return _local(library.save_script, name, code, description)

    # --- transparency ----------------------------------------------------------

    @mcp.tool()
    async def get_journal(last_n: int = 20, tool: str | None = None, date: str | None = None,
                          errors_only: bool = False, since: str | None = None, entries: bool = True) -> dict:
        """Telemetry and history of tool calls, read from the journal file (never from SolidWorks).

        Each entry: time, pid, tool, arguments (full script code), document, ms,
        ok/error, feature-tree change. stats per tool: calls, total_s, p50/p95/max
        ms, errors - over all matching calls. since 'HH:MM' limits to calls from
        then; date 'YYYY-MM-DD' (today by default); entries=False for stats only.
        """
        return _local(journal.read, last_n, tool, date, errors_only, since, entries)

    @mcp.tool()
    async def server_health() -> dict:
        """What the server is doing now, answered without waiting for SolidWorks: the running call
        and its age, queued calls, the cross-process lock holder, document owners, the last faults
        and errors. Use it when a call is slow or SolidWorks seems stuck."""
        def build():
            st = guard.state()
            last = journal.read(last_n=5, errors_only=True, entries=True)
            return {"ok": True, "version": RBR_VERSION, "server": st,
                    "queued": worker._queue.qsize() if worker is not None else 0,
                    "lock_holder": guard._lock.holder(), "owners": guard.owners(),
                    "recent_errors": [{k: e.get(k) for k in ("ts", "pid", "tool", "doc", "error")}
                                      for e in last.get("entries", [])]}
        return _local(build)

    @mcp.tool()
    async def release_ui(dry_run: bool = False, clear_quarantine: bool = False) -> dict:
        """Unfreeze SolidWorks: clear CommandInProgress and switch the view, feature tree and
        sketch display back on in every open document. Use when SolidWorks is idle but does not
        redraw or respond after a failed call. dry_run only reports what is switched off.
        After a SolidWorks crash inside a call (RPC_E_SERVERFAULT) changes are quarantined until
        SolidWorks restarts; clear_quarantine=True lifts that without a restart (only when the
        user confirms SolidWorks is fine)."""
        return await _timed(guard.release_ui, session, dry_run, True, clear_quarantine, timeout_s=30)

    @mcp.tool()
    async def release_document(title: str | None = None) -> dict:
        """Free a document (or all, title=None) that another server process holds after changing it,
        so this one may change it too. Only when you know the other session is done with it."""
        return _local(guard.release_document, title)

    @mcp.tool()
    async def tool_source(name: str) -> dict:
        """The Python code a tool runs (its MCP wrapper and the session method), to see exactly
        which SolidWorks API calls it makes."""
        return _local(tools.tool_source, session, name)

    # --- units, properties, configurations ------------------------------------------

    @mcp.tool()
    async def set_units(system: str = "MMGS", decimals: int = 2, mass_unit: str = "g") -> dict:
        """Set the current document's units: MMGS (mm, g, s), MKS, CGS, IPS; decimals for
        lengths and mass properties. New documents get MMGS automatically."""
        return await call(tools.set_units, session, system, decimals, mass_unit)

    @mcp.tool()
    async def get_custom_properties(config: str = "") -> dict:
        """Custom properties of the current document; config='' is the file level,
        a configuration name gives that configuration's."""
        return await call(tools.get_custom_properties, session, config)

    @mcp.tool()
    async def set_custom_properties(properties: dict, config: str = "", delete: list | None = None) -> dict:
        """Add or replace text custom properties {name: value} (e.g. part code, name, material);
        delete lists names to remove. Read back and verified; the document is marked modified."""
        return await call(tools.set_custom_properties, session, properties, config, delete)

    @mcp.tool()
    async def list_configurations() -> dict:
        """Configurations of the current document and the active one."""
        return await call(tools.list_configurations, session)

    @mcp.tool()
    async def activate_configuration(name: str) -> dict:
        """Make a configuration of the current document active."""
        return await call(tools.activate_configuration, session, name)

    @mcp.tool()
    async def add_configuration(name: str, description: str = "") -> dict:
        """Add a configuration derived from the active one; it becomes active."""
        return await call(tools.add_configuration, session, name, description)

    @mcp.tool()
    async def shell_check(thickness_mm: float, outward: bool = False, top: int = 15,
                          budget_s: float = 12.0, samples: int = 3000) -> dict:
        """Check BEFORE a shell (or before unsuppressing one) whether thickness_mm can work on the
        current part: the smallest convex radius (inward shell) or concave radius (outward) must
        exceed the thickness. Returns verdict ok/risk/fail, the limiting faces (index, radius, area,
        centre) and max_safe_thickness_mm. Read-only; stays within budget_s (default 12 s) using
        `samples` rays (3000), and says `partial` when a big body did not fit the budget. A failing
        shell on a lofted body can crash SolidWorks' shell computation and leave the part bodiless."""
        return await call(tools.shell_check, session, thickness_mm, outward, top, budget_s, samples)

    # --- manufacturing ---------------------------------------------------------

    @mcp.tool()
    async def export_face_dxf(path: str, face: str = "+z") -> dict:
        """Export one planar face of the current (saved) part to DXF/DWG at 1:1 in mm: outline
        and holes, for milling FR4/aluminium sheet or laser cutting.

        face: '#index' from list_faces, or a direction such as '+z' (outermost)
        or '-z:inner'. Returns the file size and the face area to check against.
        """
        return await call(tools.export_face_dxf, session, path, face)

    @mcp.tool()
    async def assembly_bom(csv_path: str | None = None, top_level_only: bool = False,
                           properties: list | None = None) -> dict:
        """BOM and mass budget of the current assembly: per part file + configuration the
        quantity, mass each and total (g), material and custom properties.

        Parts are counted through all sub-assemblies unless top_level_only.
        properties limits which custom properties are read (all by default).
        csv_path writes a ';'-separated UTF-8 CSV for Excel. sum_of_rows_g vs
        assembly_mass_g shows mass the BOM misses (e.g. excluded components).
        """
        return await call(tools.assembly_bom, session, csv_path, top_level_only, properties)

    # The upstream run_script is not upstream; keep tool_source able to find ours.
    server._rbr_run_script = run_script
