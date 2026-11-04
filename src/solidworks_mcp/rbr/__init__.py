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
from . import (equations, export_check, fastpath, flow, geom, guard, health, image, jobs, journal,
               library, script_tool, speed, tools)

__all__ = ["register"]

RBR_VERSION = "1.5.1"

INSTRUCTIONS = """

RBR extension (solidworks-mcp-rbr)
- Batch work goes in ONE run_script call (sw, model, ext, fm, asm/part, comp(), feature(), select(), args, MM, DEG; set `result`). Prefer it over many granular calls on big parts.
- Proven scripts: list_scripts / get_script / save_script / run_saved_script(name, args).
- Every call is journalled with its arguments and full script code: get_journal. tool_source(name) shows the code any tool runs.
- Telemetry: get_journal(since='HH:MM', entries=False) gives per-tool stats from the journal file without touching SolidWorks; never read the journal through run_script. server_health shows what the server is doing right now, also while a call runs.
- If SolidWorks looks frozen while idle (no redraw, tree greyed), call release_ui. A document another server process changed in the last minutes is read-only for you: release_document frees it.
- An error saying SolidWorks failed inside a call (RPC_E_SERVERFAULT) means its own crash: changes are refused until SolidWorks is restarted. Tell the user, and do not retry the same operation on the same geometry with the same inputs.
- Before a shell on a curved/lofted body: shell_check(thickness_mm).
- On a big part every modelling call rebuilds the whole tree (3-10 s each on 100+ features). Prefer one run_script doing several sketch/cut/dimension steps over many separate calls; rebuild_profile shows which features make rebuilds slow (suppress them while iterating). Use the library's tree_summary instead of writing your own feature walk. A refused fillet is diagnosed within 15 s; pass the edges it names, not the whole set again. A thickness above the smallest convex radius makes the shell fail, sometimes after a long computation.
- New parts and assemblies are switched to MMGS (mm, g) at creation. Also: set_units, get/set_custom_properties, list/activate/add_configuration, export_face_dxf (flat part -> DXF 1:1 for milling), assembly_bom (BOM + mass budget, CSV).
- A script that may run long: run_script answers {job_id, state: running} after ~50 s instead of dying, run_script_async / run_saved_script_async answer {job_id} at once; the work keeps running and get_job(job_id) returns its result (kept 24 h), list_jobs the recent ones. Never resubmit a script because the reply detached.
- After editing the linked global-variables file: sync_equation_file() adds the new names, updates the changed ones and names what the file no longer has, with a reason for every refused line.
- server_health warns when open documents or SLDWORKS.exe memory run high; close_hidden_documents(keep=[...]) closes the invisible, saved, unowned ones.
- check_containment(envelope, keepout) measures how much of each body sits inside another part's envelope body (master-model parts sharing an origin).
- Flow Simulation on a saved part: flow_create_project(name, template) -> flow_add_boundary_condition(kind, faces, value) (an internal flow needs an inlet and a pressure condition on the inner faces of its lids) -> flow_add_goal(kind, faces) -> optionally flow_set_conditions -> flow_solve (detaches like run_script; get_job) -> flow_goals. Check each goal against a hand estimate (pressure drop, drag) as you would a mass.
- More script names: vec(*doubles) / pt(x,y,z) (COM-ready VARIANTs), ui_unlocked() (lift the UI freeze for InsertSketch/EditSketch), tree_before() + features_added(before) + rename_new(before, name), combine(op, main, tools), mirror_body(body, plane, merge=True), whats_wrong().
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


def _patch_screenshot():
    """SaveAs3 grabs the graphics buffer: a minimised or covered SolidWorks yields a solid
    white frame that looks like a result. Front the document inside SolidWorks first, and
    flag the capture with blank=true when every pixel still came out one colour."""
    from ..session import SolidWorksSession
    original = SolidWorksSession.screenshot
    if getattr(original, "_rbr_blank_checked", False):
        return

    def screenshot(self, path: str, view: str = "iso", zoom_mm: list | None = None,
                   show_planes: bool = False, from_dir: list | None = None, _orig=original):
        try:
            model = self._require_model()
            self._sw.ActivateDoc3(model.GetTitle(), False, 2, 0)
            model.GraphicsRedraw2()
        except Exception:  # noqa: BLE001 - fronting is best-effort; the capture may still work
            pass
        result = _orig(self, path, view, zoom_mm, show_planes, from_dir)
        blank = image.is_blank_file(path) if str(path).lower().endswith(".png") else None
        if blank:
            try:
                self._require_model().GraphicsRedraw2()
            except Exception:  # noqa: BLE001
                pass
            result = _orig(self, path, view, zoom_mm, show_planes, from_dir)
            blank = image.is_blank_file(path)
        if isinstance(result, dict) and blank is not None:
            result["blank"] = bool(blank)
            if blank:
                result["hint"] = ("Every pixel is one colour: the SolidWorks window is probably "
                                  "minimised or fully covered, so the graphics buffer was empty. "
                                  "Restore/uncover the SolidWorks window and retake.")
        return result

    functools.update_wrapper(screenshot, original)
    screenshot._rbr_blank_checked = True
    SolidWorksSession.screenshot = screenshot


def _patch_export():
    """A derived part saved to STEP right after its features were added gave an
    EMPTY 4.5 KB file; ForceRebuild3 first, and the file is read back: a STEP
    with no solid (while the part has bodies) or an STL with 0 triangles fails
    loud instead of looking like a result."""
    from ..session import SolidWorksSession
    original = SolidWorksSession.export
    if getattr(original, "_rbr_verified", False):
        return

    def export(self, path, file_format=None, quality: str = "fine", deviation_mm=None,
               angle_deg=None, per_component: bool = False, _orig=original):
        model = self._require_model()
        try:
            model.ForceRebuild3(False)
        except Exception:  # noqa: BLE001 - a drawing refuses; the export may still be fine
            pass
        result = _orig(self, path, file_format, quality, deviation_mm, angle_deg, per_component)
        fmt = (result or {}).get("format", "")
        if fmt in ("step", "stp", "stl"):
            try:
                bodies = len(self._part_bodies()) if int(model.GetType()) == 1 else None
            except Exception:  # noqa: BLE001
                bodies = None
            for file in result.get("files") or [result.get("path")]:
                verdict = export_check.check(fmt, file, bodies)
                if not verdict.get("ok"):
                    raise SolidWorksError(f"{verdict['problem']} ({file})")
                for key in ("solids_in_file", "triangles_in_file"):
                    if key in verdict:
                        result[key] = verdict[key]
        return result

    functools.update_wrapper(export, original)
    export._rbr_verified = True
    SolidWorksSession.export = export


def _patch_set_equations():
    """A refused list names only the equation text; say which position failed too."""
    from ..session import SolidWorksSession
    original = SolidWorksSession.set_equations
    if getattr(original, "_rbr_indexed", False):
        return

    def set_equations(self, equations: list, _orig=original) -> dict:
        try:
            return _orig(self, equations)
        except SolidWorksError as exc:
            text = str(exc)
            if isinstance(equations, (list, tuple)):
                for i, equation in enumerate(equations):
                    if str(equation) and str(equation) in text:
                        raise SolidWorksError(
                            f"Equation {i + 1} of {len(equations)}: {text} "
                            "(the whole list was rolled back)") from exc
            raise

    functools.update_wrapper(set_equations, original)
    set_equations._rbr_indexed = True
    SolidWorksSession.set_equations = set_equations


def register(server) -> None:
    """Patch the session and add the RBR tools to `server.mcp` (the upstream FastMCP)."""
    fastpath.apply()
    speed.apply()
    guard.apply()    # before the journal, so the journal times the guard too
    journal.apply()
    _patch_new_documents()
    _patch_shell_check_result()
    _patch_screenshot()
    _patch_export()
    _patch_set_equations()

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

        Namespace: sw/swApp/swapp (ISldWorks, early-bound), mod (typelib wrapper
        module, e.g. mod.IComponent2), wrap(obj, mod.IFace2) to early-bind a raw
        dispatch, model (current or active IModelDoc2), active, ext
        (IModelDocExtension), fm (IFeatureManager), selmgr (ISelectionMgr), asm
        (IAssemblyDoc) or part (IPartDoc), comp(name) -> IComponent2 by
        list_components name, feature(name) -> IFeature, features(type=None) ->
        all top-level IFeature in one call (do not walk FirstFeature/GetNextFeature),
        select(objs, append=False) -> count selected, api(obj, contains='') -> the
        methods and properties an object really has in this SolidWorks (check before
        guessing names), session (the server's SolidWorksSession: its methods are the
        tools), MM = 0.001 (API lengths are metres), DEG, args (dict), np (numpy, if
        installed), print(...) -> stdout; set `result = ...` to return JSON data.
        More helpers: vec(*doubles) and pt(x, y, z) build the VARIANTs CreateSpline2 /
        CreatePoint really take (a plain list arrives as None or 1e-311); ui_unlocked()
        lifts the UI freeze around InsertSketch/EditSketch; tree_before() then
        features_added(before) / rename_new(before, name) name and rename what a call
        added (renaming what InsertRefPlane returned fails with a type mismatch);
        combine(op, main, tools) joins bodies the proven way; mirror_body(body, plane,
        merge=True) mirrors without SolidWorks' broken Merge and welds afterwards;
        whats_wrong() lists SolidWorks' rebuild errors when an Insert* returned None.
        COM rules: methods with out-parameters return a tuple (retval, out1, out2...):
        unpack it, do not call methods on it; objects from Get* calls are often raw
        dispatches: wrap(x, mod.IFeature) etc. before using them; lengths in metres.
        The result also gives `features` {before, after, added} when the tree changed.
        Example (delete 3 components with their mates in one step):
            n = select([comp(c) for c in ["Bolt-1", "Bolt-2", "Nut-1"]])
            result = {"selected": n, "deleted": ext.DeleteSelection2(1)}
        timeout_s: how long to wait for the reply (5..1800, capped by SWMCP_SYNC_WAIT_S,
        50 s). The script is NOT killed at the timeout: the reply detaches as
        {job_id, state: running} and get_job(job_id) returns the result when done -
        never resubmit the script because the reply detached.
        """
        return await _run_detachable(code, None, None, "run_script", timeout_s)

    async def _run_detachable(code: str, args: dict | None, name: str | None,
                              tool: str, timeout_s: float) -> dict:
        """Submit as a job, answer within the client's window, detach when it runs longer."""
        if worker is None:
            return {"ok": False, "error": "SolidWorks tools only work on Windows with SolidWorks."}
        busy = guard.busy_error()
        if busy:
            return {"ok": False, "error": busy}
        try:
            job = jobs.submit(worker, session, code, args, name, tool)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        wait_s = max(5.0, min(float(timeout_s), jobs.SYNC_WAIT_S))
        return jobs.answer(await jobs.wait(job["job_id"], wait_s))

    @mcp.tool()
    async def run_saved_script(name: str, args: dict | None = None, timeout_s: float = 120.0) -> dict:
        """Run a script from the library (list_scripts) by name; `args` reaches it as the dict `args`.
        Detaches like run_script: a long run answers {job_id, state: running}, get_job finishes it."""
        try:
            code = library.code_of(name)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        return await _run_detachable(code, args or {}, name, "run_saved_script", timeout_s)

    @mcp.tool()
    async def run_script_async(code: str, args: dict | None = None) -> dict:
        """Start a run_script job in the background and answer {job_id} at once.

        Same namespace and guarantees as run_script; the job queues behind other
        calls on the one COM thread and runs to completion whatever the client's
        reply timeout. get_job(job_id) returns {state, elapsed_s, result, error};
        the record is kept 24 h. Use this for anything expected to pass ~50 s.
        """
        def start():
            job = jobs.submit(worker, session, code, args, None, "run_script_async")
            return {"ok": True, "job_id": job["job_id"], "state": job["state"],
                    "hint": f"get_job('{job['job_id']}') returns the result when done."}
        return _local(start)

    @mcp.tool()
    async def run_saved_script_async(name: str, args: dict | None = None) -> dict:
        """run_saved_script as a background job: {job_id} at once, get_job for the result."""
        def start():
            code = library.code_of(name)
            job = jobs.submit(worker, session, code, args or {}, name, "run_saved_script_async")
            return {"ok": True, "job_id": job["job_id"], "state": job["state"], "script": name,
                    "hint": f"get_job('{job['job_id']}') returns the result when done."}
        return _local(start)

    @mcp.tool()
    async def get_job(job_id: str) -> dict:
        """A background job's record: state (queued|running|done|failed), elapsed_s, the
        script's full result once done, error and traceback when failed, features_added.
        Works across server processes and restarts; records live 24 h."""
        return _local(jobs.get, job_id)

    @mcp.tool()
    async def list_jobs(last_n: int = 10) -> dict:
        """The most recent background jobs (all server processes), oldest first."""
        return _local(jobs.list_jobs, last_n)

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
        and errors; plus SLDWORKS.exe memory and (when the COM thread is free) the open document
        count, with a warning past 30 documents or 70% of RAM. Use it when a call is slow or
        SolidWorks seems stuck."""
        def build():
            st = guard.state()
            last = journal.read(last_n=5, errors_only=True, entries=True)
            return {"ok": True, "version": RBR_VERSION, "server": st,
                    "queued": worker._queue.qsize() if worker is not None else 0,
                    "lock_holder": guard._lock.holder(), "owners": guard.owners(),
                    "sldworks_memory": health.sldworks_memory(),
                    "recent_errors": [{k: e.get(k) for k in ("ts", "pid", "tool", "doc", "error")}
                                      for e in last.get("entries", [])]}
        report = _local(build)
        if not report.get("ok"):
            return report
        documents = None  # a quick COM probe, only while nothing runs; never waited on for long
        if worker is not None and guard.state().get("tool") is None and session._sw is not None:
            try:
                fut = worker.submit(lambda: int(session._sw.GetDocumentCount()))
                documents = await asyncio.wait_for(asyncio.wrap_future(fut), 3.0)
            except Exception:  # noqa: BLE001 - health never hangs on SolidWorks
                documents = None
        report["documents_open"] = documents
        warn = health.warnings_for(documents, report.get("sldworks_memory"))
        if warn:
            report["warnings"] = warn
        return report

    @mcp.tool()
    async def close_hidden_documents(keep: list | None = None) -> dict:
        """Close every open document that is invisible, saved and not owned by another server
        process. Assemblies silently open their parts; past a few dozen documents every call
        crawls (server_health warns). keep lists titles to spare; the active document, the
        current model, visible and unsaved documents always stay."""
        return await _timed(health.close_hidden_documents, session, keep, timeout_s=120)

    @mcp.tool()
    async def sync_equation_file(path: str | None = None) -> dict:
        """Re-read the current document's linked equation file (or `path`): add the global
        variables the file gained, update the changed ones, and name those the file no longer
        has. Dimension equations ("D1@...") are left alone. Every refused line comes back with
        its reason (duplicate name, a broken leftover equation - deleted and retried by itself -
        or a reference nothing resolves). SolidWorks' own update only refreshes existing names."""
        return await _timed(equations.sync, session, path, timeout_s=300)

    @mcp.tool()
    async def check_containment(envelope: str, keepout: str | None = None) -> dict:
        """How much of each solid body of `keepout` (an open part's title; the current part by
        default) sits inside the bodies of the open part `envelope`: per body its volume, %
        inside and the mm^3 sticking out, via intersections of copies (the parts are untouched).
        Both parts must share their origin, as master-model parts built from one skeleton do."""
        return await _timed(geom.check_containment, session, envelope, keepout, timeout_s=600)

    # --- Flow Simulation ---------------------------------------------------------------

    @mcp.tool()
    async def flow_create_project(name: str, template: str = "internal_water") -> dict:
        """Create a Flow Simulation project on the current SAVED part or assembly from a project
        template (.fwp: a name in Flow's template folder, or a full path), and fit its
        computational domain to the model. The template fixes the analysis type and the fluid:
        'internal_water' ships with Flow; for external flow or air, make a template once in
        Flow (Wizard, then Create Template). Then: flow_add_boundary_condition, flow_add_goal,
        flow_set_conditions, flow_solve. Needs the Flow Simulation add-in and its API SDK."""
        return await _timed(flow.flow_create_project, session, name, template, timeout_s=330)

    @mcp.tool()
    async def flow_add_boundary_condition(kind: str, faces: list | str, value: float | None = None,
                                          name: str | None = None) -> dict:
        """Add a boundary condition on faces that touch the fluid (for an internal flow: the
        inner faces of the lids that close the openings, e.g. '+x:inner').
        kind: inlet_velocity (m/s), inlet_mass_flow (kg/s), inlet_volume_flow (m^3/s),
        inlet_mach, inlet_mass_flux, outlet_velocity / outlet_mass_flow / outlet_volume_flow /
        outlet_mach, static_pressure / total_pressure / environment_pressure (Pa), or
        real_wall / real_external_wall / ideal_wall (no value). Values are SI.
        faces: '#5' (list_faces index), '+x' / '+x:inner', 'all', or ['selection'] for what is
        selected in SolidWorks. An internal flow needs at least one pressure condition."""
        return await _timed(flow.flow_add_boundary_condition, session, kind, faces, value, name, timeout_s=330)

    @mcp.tool()
    async def flow_add_goal(kind: str, faces: list | str, value: str = "average", name: str | None = None) -> dict:
        """Add a surface goal on faces (same selectors as flow_add_boundary_condition; 'all' for
        the whole body, e.g. the drag of an external flow). kind: static_pressure,
        total_pressure, dynamic_pressure, temperature, density, velocity, velocity_x/y/z,
        mass_flow, volume_flow, heat_flux, force, force_x/y/z, normal_force(_x/y/z),
        shear_force(_x/y/z), moment_x/y/z, wall_temperature, total_temperature.
        value: average (default), min, max, bulk_average. Goals are what flow_solve reports."""
        return await _timed(flow.flow_add_goal, session, kind, faces, value, name, timeout_s=330)

    @mcp.tool()
    async def flow_set_conditions(velocity_mps: list | None = None, pressure_pa: float | None = None,
                                  temperature_k: float | None = None, mesh_level: int | None = None) -> dict:
        """Set the active project's initial/ambient conditions - for an external flow the free
        stream: velocity_mps [vx, vy, vz], pressure_pa, temperature_k - and/or its automatic
        mesh level 1 (coarse, fast) .. 7 (fine). Leave out what should stay."""
        return await _timed(flow.flow_set_conditions, session, velocity_mps, pressure_pa, temperature_k,
                            mesh_level, timeout_s=330)

    @mcp.tool()
    async def flow_solve(timeout_s: float = 120.0) -> dict:
        """Save the document, mesh and solve the active Flow project, and return its goals
        {name, value, unit} (SI). The results are then loaded into SolidWorks (Flow's Results
        node, `results_loaded`), where the user can make cut or surface plots. A run that outlasts timeout_s (max 50 s of waiting) answers
        {job_id, state: running}: get_job(job_id) returns the goals when it is done - never start
        it again because the reply detached. A refused solve names Flow's reason (e.g. no
        pressure condition on an internal flow)."""
        if worker is None:
            return {"ok": False, "error": "SolidWorks tools only work on Windows with SolidWorks."}
        busy = guard.busy_error()
        if busy:
            return {"ok": False, "error": busy}
        try:
            job = jobs.submit(worker, session, None, None, None, "flow_solve", call=flow.flow_solve)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        wait_s = max(5.0, min(float(timeout_s), jobs.SYNC_WAIT_S))
        return jobs.answer(await jobs.wait(job["job_id"], wait_s))

    @mcp.tool()
    async def flow_goals(service: bool = False) -> dict:
        """The active Flow project's goal values from its last solve: {name, value, unit} (SI).
        service=True adds Flow's own convergence goals."""
        return await _timed(flow.flow_goals, session, service, timeout_s=330)

    @mcp.tool()
    async def flow_info() -> dict:
        """The current document's Flow projects, the active one and its boundary conditions and goals."""
        return await _timed(flow.flow_info, session, timeout_s=330)

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
                          budget_s: float = 12.0, samples: int = 8000) -> dict:
        """Check BEFORE a shell (or before unsuppressing one) whether thickness_mm can work on the
        current part: the smallest convex radius (inward shell) or concave radius (outward) must
        exceed the thickness. Returns verdict ok/risk/fail, the limiting faces (index, radius, area,
        centre) and max_safe_thickness_mm. Read-only; stays within budget_s (default 12 s) using
        `samples` rays (8000) cast in Python against the tessellation, and says `partial` when a big body did not fit the budget. A failing
        shell on a lofted body can crash SolidWorks' shell computation and leave the part bodiless."""
        return await call(tools.shell_check, session, thickness_mm, outward, top, budget_s, samples)

    @mcp.tool()
    async def rebuild_profile(top: int = 15) -> dict:
        """Rebuild the current part once and list the features that take the longest
        (seconds and share of the total). Use it when modelling calls get slow: suppress or
        move the slowest (fillets, shells, lofts) while iterating on the rest."""
        return await _timed(speed.rebuild_profile, session, top, timeout_s=300)

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
