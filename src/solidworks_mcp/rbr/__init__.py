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
from . import (apiref, batch, bodies as bodies_mod, cavity, context, derive, dialogs, equations, export_check,
               fastpath,
               features,
               flow, geom, guard, health, image, instance, jobs, journal, library, offset_loop, param, query,
               render, scope,
               script_tool, section, sketch_datum, solidcam, speed, sweep, tools)

__all__ = ["register"]

RBR_VERSION = "1.10.0"

INSTRUCTIONS = """

RBR extension (solidworks-mcp-rbr)
- Batch work goes in ONE run_script call (sw, model, ext, fm, asm/part, comp(), feature(), select(), segments(), W(), args, MM, DEG; set `result`). Prefer it over many granular calls on big parts. Items from Get* collections are late-bound and misbehave (GetType() -> "'int' object is not callable", Select4 type mismatch): take them from segments()/relations() or W() them first.
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
- server_health warns when open documents or SLDWORKS.exe memory run high and says how many documents close_hidden_documents would close; close_hidden_documents(keep=[...], dry_run=True) lists them, without dry_run closes the invisible, saved, unowned ones. Its lock_holder is null when nothing runs; owners lists live server processes only.
- check_containment(envelope, keepout) measures how much of each body sits inside another part's envelope body (master-model parts sharing an origin).
- Flow Simulation on a saved part: flow_create_project(name, template) -> flow_add_boundary_condition(kind, faces, value) (an internal flow needs an inlet and a pressure condition on the inner faces of its lids) -> flow_add_goal(kind, faces) -> optionally flow_set_conditions -> flow_solve (detaches like run_script; get_job) -> flow_goals. Check each goal against a hand estimate (pressure drop, drag) as you would a mass.
- More script names: vec(*doubles) / pt(x,y,z) (COM-ready VARIANTs), ui_unlocked() (lift the UI freeze for InsertSketch/EditSketch), tree_before() + features_added(before) + rename_new(before, name), combine(op, main, tools), mirror_body(body, plane, merge=True), whats_wrong(), new_part(), activate(title), rays(bodies, points, dirs), export(path, fmt), split_segment / trim_to_intersection (in the sketch being edited).
- Parametric building on SolidWorks objects, no tool bodies: set_global / link_dimension / feature_dimensions; sketch_circles (also centred on a profile's mean line at a station) / sketch_rect; convert_entities, add_relation; offset_face_loop (a wall-thick contour inside a face, also where SolidWorks' own offset refuses); extrude_toward / cut_toward (explicit direction, depth / through_all / up_to, also one region of a sketch); plane_normal_to_line; mirror_features. Drive every size and position by a global variable.
- A cavity following a curved skin, section by section: sketch_intersection(plane=..., body/faces) -> sketch_trim_region(sketch, keep_box) (one closed contour) -> sketch_offset_chain(sketch, wall_mm) (inside, original to construction) -> loft_cut([sections]) or cut_offset_from_surface(sketch, face, wall_mm, toward) ending a wall short of the skin; split_line_project(sketch, faces) cuts the skin patch such a cut ends on. Check region_area_mm2 and the removed volume against a hand estimate.
- Multibody parts: body_properties (mass, volume, area, centre, box, material per body), rename_body, show_bodies(visible), delete_bodies(keep=False), move_copy_body(translate_mm, rotate, copies), set_feature_scope(feature, bodies) (a cut or boss acting on some bodies only), save_bodies(bodies, path) (to a new part), insert_part(path) (another part as bodies). A material per body is not settable through the API (SolidWorks ignores it): bodies carry the part's material.
- Working in the middle of a big part: edit_context(freeze_to=..., rollback_before=...) limits rebuilds to the features between the two bars (set_dimension 10 s -> 3-4 s on 205 features); new features land above the rollback bar under the names you give; save_part is refused until edit_context(end=True). get_status and server_health show an open context.
- Seeing: screenshot renders off-screen and returns the picture in the reply - no window needed, works for a document that is not active or not visible (document='title'), bodies=[...] limits it. section_screenshot(point_mm, normal) shows a cut; section_profile(point_mm, normal) gives the exact contours with wall_mm per segment - measure walls with it, not by eye.
- Checked changes: every building tool answers `change` (each body's volume before / after, faces added / removed, whats_wrong, new under-defined sketches) and takes expect={'volume': 'decrease', 'bodies': 'same', 'changed_bodies': [...]}: a call that does otherwise fails and what it added is removed. Cuts, bosses and holes take bodies=[names] to act on those bodies only - no tool body + combine.
- Picking: find_faces / find_edges describe the entity (surface, normal, parallel_to, box, feature, body; expect = how many must match) and return `ref` - a short reference that names the same face or edge after any rebuild, where '#N' renumbers. Pass a ref wherever a face or an edge is taken (on=, face=, up_to=, edges=, datum_x=, the face= of *_on_face tools).
- Sketches that follow the model: sketch_rect / sketch_circles / add_sketch take datum_x / datum_y (an edge, a planar face seen edge-on, a vertex '@x,y,z') - positions are then measured from that geometry, not from the origin. For a sketch that exists: fully_define_sketch(sketch, datum_x, datum_y) moves its origin dimensions to the datums and defines what is still free; add_dimension adds one dimension (also to a model edge or vertex); sketch_fillet rounds a corner with a radius dimension.
- Routes: add_sketch3d(points_mm, bend_radius_mm) + sweep_cut(path, diameter_mm) / sweep_boss - a channel or a rod along a route in the existing body, one feature, the bend radius a dimension. Many at once: read_sketches([names]) (no sketch opened, no rebuild), set_dimensions({name: value}) (one rebuild).
- run_script(rollback_on_error=False) keeps what the script finished before an error (`kept`); the default removes exactly what the script created (by feature ID - a renamed feature stays) and restores changed equations.
- Derived parts: derive_part(body, path) gives a body its own part file that follows the master; list_external_refs shows what a part takes from other files and whether each link is alive; update_external_refs loads the sources, rebuilds and reports each body's volume before / after.
- A call that answers DIALOG_BLOCKING ran into a modal SolidWorks dialog: the reply holds its text and buttons. dialog_respond(button) presses one ('yes' / 'no' / 'ok' / 'cancel' or the caption); the interrupted call is NOT cancelled and goes on then - never send it again. dialog_list shows the open dialogs at any time. Read the text before answering; when it asks about the user's data (save changes?), ask the user.
- api_search(query) finds a method, property or constant of the installed SolidWorks API with its parameters - use it before a run_script call you are not sure of. sw_instances lists the running SolidWorks processes and the one this server drives; only one SolidWorks may run on a machine - never start a second one.
- SolidCAM (in the same SolidWorks; never start another SolidWorks or SolidCAM): solidcam_status -> solidcam_new_part(path, machine) (makes the zero SolidCAM_Zero on the top face at the bottom-left corner, then SolidCAM's Part Data panel waits for the user to pick it) or solidcam_open(path) -> solidcam_stock -> solidcam_add_operation(template, geometry) -> solidcam_calculate -> solidcam_gcode (says whether the program cuts) -> solidcam_save / solidcam_close. The API cannot define job geometry (chains, faces): a template job on the target alone posts no moves.
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


SW_WINDOW_MAXIMIZED = 1  # swWindowState_e, read from swconst.tlb of SW 2025
SW_WINDOW_MINIMIZED = 2
SW_DONT_REBUILD_ON_ACTIVATE = 2  # swRebuildOnActivation_e


def _patch_screenshot():
    """SaveAs3 grabs the graphics buffer: a minimised SolidWorks, a minimised
    document window or a never-shown document yields a solid one-colour frame
    (1126 bytes) that looks like a result. Restore the windows, front the
    document, redraw, retake once - and FAIL LOUD (file deleted) when the
    picture still comes out blank, instead of handing the blank back."""
    from ..session import SolidWorksSession
    original = SolidWorksSession.screenshot
    if getattr(original, "_rbr_blank_checked", False):
        return

    def _front_and_redraw(self) -> None:
        model = self._require_model()
        sw = self._sw
        if not sw.Visible:
            sw.Visible = True
        if int(sw.FrameState) == SW_WINDOW_MINIMIZED:
            sw.FrameState = SW_WINDOW_MAXIMIZED
        sw.ActivateDoc3(model.GetTitle(), False, SW_DONT_REBUILD_ON_ACTIVATE, 0)
        frame = binding_wrap(self, sw.Frame(), "IFrame")
        if frame is not None:
            try:
                for window in frame.ModelWindows or ():
                    win = binding_wrap(self, window, "IModelWindow")
                    doc = binding_wrap(self, win.ModelDoc, "IModelDoc2")
                    if doc is not None and doc.GetTitle() == model.GetTitle():
                        win.RestoreFromMinimize()
            except Exception:  # noqa: BLE001 - not every build exposes the model windows
                pass
        model.GraphicsRedraw2()

    def binding_wrap(self, obj, interface):
        from .. import binding
        try:
            return binding.wrap(obj, getattr(self._mod, interface))
        except Exception:  # noqa: BLE001
            return None

    def screenshot(self, path: str, view: str = "iso", zoom_mm: list | None = None,
                   show_planes: bool = False, from_dir: list | None = None, _orig=original):
        try:
            _front_and_redraw(self)
        except Exception:  # noqa: BLE001 - fronting is best-effort; the capture may still work
            pass
        result = _orig(self, path, view, zoom_mm, show_planes, from_dir)
        blank = image.is_blank_file(path) if str(path).lower().endswith(".png") else None
        if blank:
            try:
                _front_and_redraw(self)
            except Exception:  # noqa: BLE001
                pass
            result = _orig(self, path, view, zoom_mm, show_planes, from_dir)
            blank = image.is_blank_file(path)
        if blank:
            try:
                os.remove(path)
            except OSError:
                pass
            raise SolidWorksError(
                "The screenshot came out as one solid colour twice, although the SolidWorks "
                "window was restored and the document fronted and redrawn: the graphics buffer "
                "is empty (a covered/remote session, or the document never drew). Nothing was "
                "written. Bring the SolidWorks window to the screen and retake.")
        if isinstance(result, dict) and blank is not None:
            result["blank"] = False
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
    instance.apply()  # SWMCP_SW_PID: which SolidWorks process, should a stray second one be running
    fastpath.apply()
    speed.apply()
    guard.apply()    # before the journal, so the journal times the guard too
    journal.apply()
    context.apply()
    _patch_new_documents()
    _patch_shell_check_result()
    _patch_screenshot()
    _patch_export()
    _patch_set_equations()

    mcp, worker, session, call, com_error = (server.mcp, server._worker, server._session,
                                             server._call, server._COM_ERROR)
    query.install(session)
    try:
        mcp._mcp_server.instructions = (mcp._mcp_server.instructions or "") + INSTRUCTIONS
    except AttributeError:
        pass

    upstream_call = call
    detached = set()   # calls answered with DIALOG_BLOCKING that still wait on the COM thread

    async def _blocking_dialogs() -> list:
        """The modal dialogs SolidWorks waits behind, read off the event loop (pure Win32)."""
        if DIALOG_WATCH_S <= 0:
            return []
        pid = guard.state().get("sw_pid")   # the instance this server drives, not another one's dialogs
        try:
            found = await asyncio.get_running_loop().run_in_executor(
                None, lambda: dialogs.blocking(pids=[pid] if pid else None))
        except Exception:  # noqa: BLE001 - the watch never breaks a call
            return []
        return dialogs.questions(found)

    async def _watched(awaitable, tool: str | None = None):
        """Await a call; when SolidWorks shows a modal dialog meanwhile, answer
        DIALOG_BLOCKING within seconds instead of waiting for the client's timeout.

        The dialog has to be seen on two looks in a row (a message box that a tool
        itself answers, or one flashing by, is not reported). The call is not
        cancelled - it cannot be: the COM thread waits inside SolidWorks."""
        task = asyncio.ensure_future(awaitable)
        if DIALOG_WATCH_S <= 0:
            return await task
        started = asyncio.get_running_loop().time()
        seen = 0
        while True:
            done, _pending = await asyncio.wait({task}, timeout=DIALOG_WATCH_S)
            if done:
                return task.result()
            found = await _blocking_dialogs()
            seen = seen + 1 if found else 0
            if seen >= 2 and not task.done():
                detached.add(task)
                task.add_done_callback(detached.discard)
                return dialogs.blocking_reply(found, tool, asyncio.get_running_loop().time() - started)

    def _tool_of(fn) -> str:
        return journal.tool_name(fn)

    def _in_request(fn):
        """`fn` with the change report / body scope / expectations of the tool call in progress."""
        request = scope.REQUEST.get()
        if request is None:
            return fn
        return scope.runner(session, fn, request["tool"], request.get("bodies"), request.get("expect"))

    async def call(fn, *args, **kwargs):  # noqa: F811 - replaces server._call for upstream's tools too
        return await _watched(upstream_call(_in_request(fn), *args, **kwargs), _tool_of(fn))

    server._call = call

    if worker is not None and not getattr(worker, "_rbr_fail_fast", False):
        original_call = worker.call

        async def call_fail_fast(fn):
            busy = guard.busy_error()  # no queueing behind a call that has run for long
            if busy:
                found = await _blocking_dialogs()
                if found:  # ... because a dialog is open: say that, it is the way out
                    raise SolidWorksError(dialogs.blocking_reply(found, guard.state().get("tool"))["error"]
                                          + " dialog_respond(button=...) answers it.")
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
            found = await _blocking_dialogs()
            if found:
                return dialogs.blocking_reply(found, guard.state().get("tool"))
            return {"ok": False, "error": busy}
        tool = _tool_of(fn)
        fn = _in_request(fn)
        fut = worker.submit(lambda: session.run_guarded(fn, *args))

        async def finish():
            try:
                return await asyncio.wait_for(asyncio.wrap_future(fut), limit)
            except asyncio.TimeoutError:
                return {"ok": False, "error": f"SolidWorks did not finish within {limit:.0f}s: the work is still "
                                              "running (the COM thread stays busy); server_health shows it."}
            except SolidWorksError as exc:
                return {"ok": False, "error": str(exc)}
            except com_error as exc:
                info = getattr(exc, "excepinfo", None)
                desc = info[2] if info and len(info) > 2 and info[2] else getattr(exc, "strerror", None)
                return {"ok": False, "error": f"SolidWorks COM error: {desc or exc}"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        return await _watched(finish(), tool)

    def _local(fn, *args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # --- scripts ---------------------------------------------------------------

    @mcp.tool()
    async def run_script(code: str, timeout_s: float = 120.0, rollback_on_error: bool = True) -> dict:
        """Run a Python script against SolidWorks in ONE call: batch work that would take many tool calls.

        Use it for repetitive or bulk operations (delete/suppress/move many
        components, rename, bulk set_dimension, export loops, reading data
        across a part or assembly) instead of calling granular tools one by one.
        The script runs on the server's COM thread with the UI frozen and
        CommandInProgress set; if it raises, the features it CREATED (told by
        feature ID: a renamed one is not new) and the equations it added or
        changed are rolled back. rollback_on_error=False keeps what the script
        finished before the error instead: the reply is ok false with the error
        and `kept` {features, equations}, so the failing step can be fixed and
        the rest run without redoing the first steps. The full code is written
        to the journal.

        Namespace: sw/swApp/swapp (ISldWorks, early-bound), mod (typelib wrapper
        module, e.g. mod.IComponent2), wrap(obj, mod.IFace2) to early-bind a raw
        dispatch, model (current or active IModelDoc2), active, ext
        (IModelDocExtension), fm (IFeatureManager), selmgr (ISelectionMgr), asm
        (IAssemblyDoc) or part (IPartDoc), comp(name) -> IComponent2 by
        list_components name, feature(name) -> IFeature, features(type=None) ->
        all top-level IFeature in one call (do not walk FirstFeature/GetNextFeature),
        select(objs, append=False) -> count selected (components, entities and sketch
        segments alike, raw or wrapped), api(obj, contains='') -> the
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
        new_part() / activate(title) return the IModelDoc2 (raw NewDocument from a script gets
        an empty template path; ActivateDoc3 answers a tuple); rays(bodies, points, dirs) casts
        RayIntersections with the bodies as the VARIANT array it needs; export(path, fmt)
        rebuilds first and checks the file; split_segment(seg, point_mm) and
        trim_to_intersection(seg, pick_mm) edit the sketch being edited (not offset or
        converted curves: SolidWorks keeps those whole). The parametric tools are here too:
        set_global, link_dimension, feature_dimensions, sketch_circles, sketch_rect,
        convert_entities, add_relation, offset_face_loop, extrude_toward, cut_toward,
        plane_normal_to_line, mirror_features; and find_faces / find_edges / entity(ref),
        section_profile, read_sketches, set_dimensions, add_sketch3d, sweep_cut, sweep_boss,
        fully_define_sketch, add_dimension, sketch_fillet, sketch_intersection,
        sketch_trim_region, sketch_offset_chain, split_line_project, loft_cut,
        cut_offset_from_surface (keyword arguments as the tools of those names).
        Raw API calls that mislead, and what to call instead: IBody2.GetBodyBox /
        IPartDoc.GetPartBox run loose round curved faces -> body_box(body) (tight, from
        extreme points); model.SaveAs3 to STL / STEP right after adding features can
        write an empty file -> export(path); asm.AddComponent5(path, ..., x, y, z) centres
        the component's BOX at x, y, z -> insert_component(path, x_mm, y_mm, z_mm) puts
        its origin there; sw.NewDocument with the default template preference gets a
        file name without its folder -> new_part() / new_assembly().
        IModeler.CreateBodyFromBox3(data) - only for helper calculations, never as a tool body in
        a part - reads data = [x, y, z, dx, dy, dz, width, length, height] (m): (x, y, z) is the
        CENTRE OF THE BOTTOM FACE, (dx, dy, dz) the direction of the HEIGHT; then width runs
        along Z, length along Y, height along that direction (verified on SW 2025).
        Sketch geometry comes early-bound through segments(sketch=None, construction=False)
        -> ISketchSegment list of the active or named sketch, and relations(sketch=None)
        -> its ISketchRelation list: use them instead of raw GetSketchSegments()/
        GetRelations(), whose late-bound items resolve methods as properties
        (seg.GetType() dies with "'int' object is not callable") and reject Select4's
        arguments. Any other Get* collection item: W(x, 'IFace2') re-binds it (name the
        interface - this SolidWorks exposes no type info on them); call(obj, 'Name',
        *args) reads or invokes a member of a still-raw dispatch either way it is exposed.
        COM rules: methods with out-parameters return a tuple (retval, out1, out2...):
        unpack it, do not call methods on it; objects from Get* calls are often raw
        dispatches: W(x) / segments() / wrap(x, mod.IFeature) before using them;
        lengths in metres.
        The result also gives `features` {before, after, added} when the tree changed.
        Example (delete 3 components with their mates in one step):
            n = select([comp(c) for c in ["Bolt-1", "Bolt-2", "Nut-1"]])
            result = {"selected": n, "deleted": ext.DeleteSelection2(1)}
        timeout_s: how long to wait for the reply (5..1800, capped by SWMCP_SYNC_WAIT_S,
        50 s). The script is NOT killed at the timeout: the reply detaches as
        {job_id, state: running} and get_job(job_id) returns the result when done -
        never resubmit the script because the reply detached.
        """
        return await _run_detachable(code, None, None, "run_script", timeout_s, rollback_on_error)

    async def _run_detachable(code: str, args: dict | None, name: str | None,
                              tool: str, timeout_s: float, rollback_on_error: bool = True) -> dict:
        """Submit as a job, answer within the client's window, detach when it runs longer."""
        if worker is None:
            return {"ok": False, "error": "SolidWorks tools only work on Windows with SolidWorks."}
        busy = guard.busy_error()
        if busy:
            return {"ok": False, "error": busy}
        try:
            job = jobs.submit(worker, session, code, args, name, tool, rollback_on_error=rollback_on_error)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        wait_s = max(5.0, min(float(timeout_s), jobs.SYNC_WAIT_S))
        record = await _watched(jobs.wait(job["job_id"], wait_s), tool)
        if isinstance(record, dict) and record.get("dialog_blocking"):
            return {**record, "job_id": job["job_id"],
                    "hint": record["hint"] + f" get_job('{job['job_id']}') returns the script's result."}
        return jobs.answer(record)

    @mcp.tool()
    async def run_saved_script(name: str, args: dict | None = None, timeout_s: float = 120.0,
                               rollback_on_error: bool = True) -> dict:
        """Run a script from the library (list_scripts) by name; `args` reaches it as the dict `args`.
        Detaches like run_script: a long run answers {job_id, state: running}, get_job finishes it.
        rollback_on_error as in run_script."""
        try:
            code = library.code_of(name)
        except SolidWorksError as exc:
            return {"ok": False, "error": str(exc)}
        return await _run_detachable(code, args or {}, name, "run_saved_script", timeout_s, rollback_on_error)

    @mcp.tool()
    async def run_script_async(code: str, args: dict | None = None, rollback_on_error: bool = True) -> dict:
        """Start a run_script job in the background and answer {job_id} at once.

        Same namespace and guarantees as run_script; the job queues behind other
        calls on the one COM thread and runs to completion whatever the client's
        reply timeout. get_job(job_id) returns {state, elapsed_s, result, error};
        the record is kept 24 h. Use this for anything expected to pass ~50 s.
        """
        def start():
            job = jobs.submit(worker, session, code, args, None, "run_script_async",
                              rollback_on_error=rollback_on_error)
            return {"ok": True, "job_id": job["job_id"], "state": job["state"],
                    "hint": f"get_job('{job['job_id']}') returns the result when done."}
        return _local(start)

    @mcp.tool()
    async def run_saved_script_async(name: str, args: dict | None = None,
                                     rollback_on_error: bool = True) -> dict:
        """run_saved_script as a background job: {job_id} at once, get_job for the result."""
        def start():
            code = library.code_of(name)
            job = jobs.submit(worker, session, code, args or {}, name, "run_saved_script_async",
                              rollback_on_error=rollback_on_error)
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
        and its age, queued calls, who holds the cross-process lock at this moment (null when
        nothing runs), the documents owned by LIVE server processes (records of processes that
        exited are dropped), the last faults and errors; plus SLDWORKS.exe memory and (when the
        COM thread is free) the open document count, with a warning past 30 documents - and how
        many of them close_hidden_documents would close - or 70% of RAM. Use it when a call is
        slow or SolidWorks seems stuck."""
        def build():
            st = guard.state()
            last = journal.read(last_n=5, errors_only=True, entries=True)
            return {"ok": True, "version": RBR_VERSION, "server": st,
                    "queued": worker._queue.qsize() if worker is not None else 0,
                    "lock_holder": guard._lock.held_by(), "owners": guard.prune_owners(),
                    "edit_context": context.seen(),
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
        closable = None
        if documents is not None and documents > health.DOCS_WARN:
            try:  # the suggestion with its number: what close_hidden_documents would close now
                fut = worker.submit(lambda: session.run_guarded(health.count_closable, session))
                closable = await asyncio.wait_for(asyncio.wrap_future(fut), 5.0)
                report["close_hidden_documents_would_close"] = closable
            except Exception:  # noqa: BLE001 - health never hangs on SolidWorks
                closable = None
        warn = health.warnings_for(documents, report.get("sldworks_memory"), closable)
        if warn:
            report["warnings"] = warn
        return report

    @mcp.tool()
    async def sw_instances() -> dict:
        """The SolidWorks processes running on this machine - pid, memory (working set and
        private MB), whether each is registered for automation - and which one this server is
        attached to. Answers without calling into SolidWorks. Only ONE SolidWorks may run on
        a machine: never start a second one, for no purpose. More than one in the list (a
        process left behind by a crash) comes with a warning - tell the user and close
        nothing yourself."""
        return _local(instance.instances, guard.state().get("sw_pid"))

    @mcp.tool()
    async def close_hidden_documents(keep: list | None = None, dry_run: bool = False) -> dict:
        """Close every open document that is invisible, saved and not owned by another live
        server process. Assemblies silently open their parts; past a few dozen documents every
        call crawls (server_health warns and says how many would close). keep lists titles to
        spare; the active document, the current model, visible and unsaved documents always
        stay. dry_run=True only lists what would be closed (would_close) and why the rest
        stays."""
        return await _timed(health.close_hidden_documents, session, keep, dry_run, timeout_s=120)

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

    # --- working mid-tree (1.10.0) ---------------------------------------------------

    @mcp.tool()
    async def edit_context(freeze_to: str | None = None, rollback_before: str | None = None,
                           end: bool = False) -> dict:
        """Work in the middle of a big part without rebuilding all of it: freeze the tree
        down to and including `freeze_to` (SolidWorks' freeze bar) and/or roll it back to just
        before `rollback_before` (both list_features names). Rebuilds then cover only the
        features in between: on a 205-feature part set_dimension took 10 s, 3-4 s inside such
        a context. New features land above the rollback bar and are named correctly there.
        While rolled back, mass properties describe the rolled-back part and save_part is
        refused. end=True moves both bars back (the rolled-back features rebuild once) and
        reports failing_features. With no arguments: where the bars are now. The state also
        shows in get_status and server_health."""
        return await _timed(context.edit_context, session, freeze_to, rollback_before, end, timeout_s=600)

    # --- pictures (1.10.0): rendered off-screen, and returned in the reply ------------------

    def _with_image(result: dict, inline: bool):
        """The result, followed by the picture itself as MCP image content."""
        if not (inline and isinstance(result, dict) and result.get("ok") and result.get("path")):
            return result
        try:
            from mcp.server.fastmcp import Image
            data = image.inline_png(result["path"])
            if data is None:
                result["inline"] = "left out: the file is too large to send inline; read it from `path`"
                return result
            return [result, Image(data=data, format="png")]
        except Exception as exc:  # noqa: BLE001 - the file is there: the picture is a convenience
            result["inline"] = f"left out: {type(exc).__name__}: {exc}"
            return result

    try:
        mcp.remove_tool("screenshot")  # upstream's: window capture only, answers a path
    except Exception:  # noqa: BLE001 - an mcp without remove_tool: ours is registered under its own name
        pass

    @mcp.tool(structured_output=False)
    async def screenshot(path: str | None = None, view: str = "iso", zoom_mm: list | None = None,
                         show_planes: bool = False, from_dir: list | None = None, mode: str = "auto",
                         width: int = 1200, height: int = 800, inline: bool = True,
                         document: str | None = None, bodies: list | str | None = None):
        """A picture of the current part or assembly, returned in the reply (and saved as PNG).

        view: 'iso' (default), 'front', 'back', 'left', 'right', 'top', 'bottom'; or from_dir =
        [x, y, z], the direction to look from (the model's +y stays up). zoom_mm = [[x1, y1, z1],
        [x2, y2, z2]]: the region to fill the image (model mm).
        mode 'render' (the default 'auto' picks it when numpy is installed) draws the solids
        off-screen from SolidWorks' display tessellation: it needs no window, so it also works
        for a document that is not active or not visible (document='title' renders any open
        document without making it current), does not change the user's view and does not hang
        on big assemblies; bodies limits a part to the named bodies. Smooth shading, one colour
        per body or component, outlines; no sketches, planes or appearances.
        mode 'window' captures the SolidWorks window instead (true appearances; show_planes
        keeps planes and axes): it fronts the document and changes its view; jpg/tif paths too.
        path: where to save (default: a file under the server's home folder). inline=False
        answers the path only. width, height: the picture's size in pixels (render)."""
        want = str(mode).lower()
        if want not in ("auto", "render", "window"):
            return {"ok": False, "error": f"Unknown mode '{mode}': use 'auto', 'render' or 'window'."}
        if want == "auto":
            wants_window = show_planes or (path is not None and not str(path).lower().endswith(".png"))
            want = "window" if (wants_window or not render.available()) else "render"
        if want == "render":
            result = await _timed(render.render_view, session, path, view, zoom_mm, from_dir, width, height,
                                  document, bodies, timeout_s=120)
        else:
            if document is not None or bodies is not None:
                return {"ok": False, "error": "document= and bodies= need mode='render'; the window capture "
                                              "shows the current document as it is on screen."}
            target = path or render.default_path("window", "shot")
            result = await _timed(session.screenshot, target, view, zoom_mm, show_planes, from_dir, timeout_s=120)
            if isinstance(result, dict) and result.get("ok"):
                result["mode"] = "window"
        return _with_image(result, inline)

    server._rbr_screenshot = screenshot

    @mcp.tool(structured_output=False)
    async def section_screenshot(point_mm: list, normal: str | list = "z", zoom_mm: list | None = None,
                                 path: str | None = None, from_dir: list | None = None, width: int = 1200,
                                 height: int = 800, inline: bool = True, document: str | None = None,
                                 bodies: list | str | None = None):
        """A picture of the part or assembly cut by a plane, returned in the reply: the plane
        through point_mm [x, y, z] with `normal` ('x', '-y', 'z' or [nx, ny, nz]); what lies
        on the normal's side is left out and the cut faces are painted red. Looks straight at
        the cut (the picture's right and up are the plane's u_axis and v_axis, the same frame
        as section_profile); from_dir = [x, y, z] looks from elsewhere on the normal's side,
        e.g. [1, 1, 1]. zoom_mm = [[x1, y1, z1], [x2, y2, z2]] fills the image with a detail.
        Rendered off-screen (see screenshot): nothing changes in SolidWorks, no section view is
        switched on, and document='title' cuts any open document. For measuring, use
        section_profile."""
        result = await _timed(section.section_view, session, point_mm, normal, zoom_mm, path, from_dir, width,
                              height, document, bodies, timeout_s=120)
        return _with_image(result, inline)

    @mcp.tool()
    async def section_profile(point_mm: list, normal: str | list = "z", bodies: list | str | None = None,
                              document: str | None = None) -> dict:
        """The exact contours of a part's section by the plane through point_mm with `normal`
        ('x', '-y', 'z' or [nx, ny, nz]), in mm - for measuring walls. Per body and region:
        area, and each contour (outer / inner) as its segments in order: line, arc or circle
        (centre, radius), spline (points), with start and end both in model mm and in the
        plane's own (u, v). wall_mm on a segment is the thickness of the material there,
        across it from the segment's middle; min_wall_mm the thinnest. Taken from copies of
        the bodies cut by SolidWorks itself: exact geometry, and the part is not touched (no
        feature, no rebuild). bodies: body names (default all visible); document: an open part
        by title without making it current."""
        return await _timed(section.section_profile, session, point_mm, normal, bodies, document, timeout_s=300)

    # --- picking by description (1.10.0) --------------------------------------------------

    @mcp.tool()
    async def find_faces(surface: str | None = None, normal: str | list | None = None,
                         parallel_to: str | list | None = None, within_mm: list | None = None,
                         at_mm: list | None = None, area_mm2: float | list | None = None,
                         radius_mm: float | list | None = None, feature: str | None = None,
                         body: str | list | None = None, expect: int | list | None = None,
                         limit: int = 30) -> dict:
        """Find faces of the current part by what they are - no ray, no '#N' index. Every
        filter given must hold: surface 'plane' / 'cylinder' / 'cone' / 'sphere' / 'torus' /
        'spline' / ...; normal '+z' / '-x' / [x, y, z] (planar faces facing that way);
        parallel_to 'front' / 'top' / 'right' (planes parallel to it, cylinders with their axis
        across it) or an axis 'x' / [x, y, z] (cylinders along it, planes containing it);
        within_mm [[x1, y1, z1], [x2, y2, z2]] (the face's box inside it); at_mm [x, y, z] (a
        point on the face); area_mm2 and radius_mm a value or [min, max] (None = open);
        feature (the list_features name that made the face); body (list_bodies names).
        expect: how many must match, a count or [min, max] - otherwise the call fails and
        shows what matched, so a wrong pick stops here. Each face comes back with surface,
        area, box, normal or radius + axis, the feature that made it, and `ref`: a reference
        that names the same face after rebuilds and in later sessions, where '#N' renumbers.
        Pass it wherever a face is taken: on= / face= / up_to= / faces=, and the face= of
        the *_on_face tools (the point then only places the sketch)."""
        return await call(query.find_faces, session, surface, normal, parallel_to, within_mm, at_mm, area_mm2,
                          radius_mm, feature, body, expect, limit)

    @mcp.tool()
    async def find_edges(kind: str | None = None, parallel_to: str | list | None = None,
                         length_mm: float | list | None = None, radius_mm: float | list | None = None,
                         within_mm: list | None = None, at_mm: list | None = None, face: str | None = None,
                         between: list | None = None, feature: str | None = None,
                         body: str | list | None = None, expect: int | list | None = None,
                         limit: int = 30) -> dict:
        """Find edges of the current part by what they are: kind 'line' / 'circle' / 'arc' /
        'ellipse' / 'spline' / 'curve' (anything else); parallel_to an axis 'x' / [x, y, z]
        (lines along it, circles and arcs about it); length_mm and radius_mm a value or
        [min, max]; within_mm a box holding both ends; at_mm a point on the edge; face (the
        edges of that face: a `ref`, '#N' or '+z'); between [face, face] (the edge two faces
        share); feature (edges of the faces a feature made); body. expect: the count that
        must match (or [min, max]), else the call fails. Each edge comes back with type,
        length, ends, direction or centre + radius + axis, and `ref`, taken by add_fillet /
        add_chamfer edges= ('ref:..., ref:...') and convert_entities edges=[...] in place of
        list_edges indices, which renumber on every rebuild."""
        return await call(query.find_edges, session, kind, parallel_to, length_mm, radius_mm, within_mm, at_mm,
                          face, between, feature, body, expect, limit)

    # --- many at once (1.10.0) ---------------------------------------------------------

    @mcp.tool()
    async def read_sketches(names: list | str | None = None, dimensions: bool = True) -> dict:
        """Read several sketches of the current part in ONE call (names: list_features names,
        2D or 3D, also sketches a feature absorbed; None = every sketch): per sketch its
        lines, arcs, circles and splines in MODEL mm (construction marked), its dimensions and
        whether it is fully defined - read_sketch's content, without opening a sketch and
        without a rebuild. 15 sketches of a 205-feature part take about a second
        (read_sketch: ~14 s each). A name that is no sketch comes back in `missing`."""
        return await call(batch.read_sketches, session, names, dimensions)

    @mcp.tool()
    async def set_dimensions(values: dict) -> dict:
        """Set several driving dimensions, then rebuild ONCE: values = {'D1@Boss': 12,
        'x@SK_A': -3.5, 'D1@Angle1': 30} (mm; degrees for angles; a point's coordinate takes a
        sign, as in set_dimension). Every value is read back after the rebuild; when SolidWorks
        did not take one (an equation drives it, a size of 0) all of them are put back and the
        error names it. Returns old and new values, failing_features and mass properties."""
        return await call(batch.set_dimensions, session, values)

    # --- a channel or a rod along a route (1.10.0) ---------------------------------------

    @mcp.tool()
    async def add_sketch3d(points_mm: list, bend_radius_mm: float | None = None, fillets: list | None = None,
                           name: str | None = None, vars: dict | None = None) -> dict:
        """A 3D sketch of straight runs through points_mm = [[x, y, z], ...] (model mm) in the
        current part - a route for sweep_cut / sweep_boss. bend_radius_mm rounds every corner,
        or fillets = one radius per interior corner (0 = sharp). The rounding is a real sketch
        fillet: its radius is a dimension (`radius`, or `r<corner>` when they differ) that
        set_dimension changes, the runs staying where they are; vars links it to a global
        variable ({'radius': 'HOSE_R'}). Refuses a radius that does not fit between two
        corners. Returns the sketch's name, its dimensions and the route's length."""
        return await call(sweep.add_sketch3d, session, points_mm, bend_radius_mm, fillets, name, vars)

    @mcp.tool()
    async def sweep_cut(path: str, diameter_mm: float | None = None, profile: str | None = None,
                        name: str | None = None) -> dict:
        """Cut a channel along a sketch of the current part (2D or 3D, e.g. add_sketch3d's): a
        round profile of diameter_mm, perpendicular to the path - its diameter comes back as
        dimensions.diameter - or a closed profile sketch that crosses the path. One feature in
        the existing body. A cut that removes nothing is rolled back; removed_mm3 is to check
        against pi d^2 / 4 x length."""
        return await call(sweep.sweep_cut, session, path, diameter_mm, profile, name)

    @mcp.tool()
    async def sweep_boss(path: str, diameter_mm: float | None = None, profile: str | None = None,
                         name: str | None = None, merge: bool = True) -> dict:
        """Add material along a sketch of the current part: a round rod of diameter_mm or a
        profile sketch swept along the path; merge=False keeps it a separate body."""
        return await call(sweep.sweep_boss, session, path, diameter_mm, profile, name, merge)

    # --- parametric building on SolidWorks objects ---------------------------------

    @mcp.tool()
    async def set_global(name: str, value: float | str) -> dict:
        """Add or change a global variable: a number (mm / deg) or an expression of other
        variables ('WALL * 2'). Rebuilds; failing_features names what broke."""
        return await call(param.set_global, session, name, value)

    @mcp.tool()
    async def link_dimension(dimension: str, expression: str) -> dict:
        """Drive a dimension by global variables: dimension is its full name ('D1@Plane5',
        'diameter1@SK_BOSS' - feature_dimensions lists them), expression e.g. 'WALL',
        'HOLE_DEPTH + 2', 'abs(POS_1)'; bare variable names are quoted for you."""
        return await call(param.link_dimension, session, dimension, expression)

    @mcp.tool()
    async def feature_dimensions(feature: str | None = None) -> dict:
        """Dimensions of a feature and of the sketches it absorbed (or of every feature):
        full name for link_dimension, value, unit, driving, and the equation driving it."""
        return await call(param.feature_dimensions, session, feature)

    @mcp.tool()
    async def sketch_circles(on: str, circles: list, name: str | None = None,
                             profile_face: str | None = None, datum_x: str | list | None = None,
                             datum_y: str | list | None = None) -> dict:
        """A fully defined sketch of circles on a plane ('front'/'top'/'right' or a tree plane
        such as 'Plane5') or a face ('+x', '#5', '@x,y,z').

        circles: [{"diameter_mm": 3.2, "center_mm": [x, y, z], "vars": {"diameter": "PIN_D",
        "X": "PIN_X", "Z": "POS_1"}}] - or a centre on the profile's MEAN LINE:
        {"diameter_mm": 10, "midline": {"axis": "Z", "station_mm": -120}, "vars":
        {"diameter": "BOSS_D", "station": "POS_1"}}: a construction chord across
        profile_face's outer loop (converted) at that station, the centre at its midpoint, so
        the hole follows the profile. vars keys: diameter, station, x / y (sketch axes) or X / Y /
        Z (model axes); positions are linked as abs(expr). datum_x / datum_y: measure the x / y
        positions from model geometry instead of the origin (see sketch_rect). Then
        extrude_toward / cut_toward."""
        return await call(param.sketch_circles, session, on, circles, name, profile_face, datum_x, datum_y)

    @mcp.tool()
    async def sketch_rect(on: str, corner_mm: list, width_mm: float, height_mm: float,
                          name: str | None = None, vars: dict | None = None,
                          datum_x: str | list | None = None, datum_y: str | list | None = None) -> dict:
        """A fully defined rectangle on a plane or face: corner_mm (model space), width along
        the sketch x, height along its y (negative: the other way). Dimensions width, height,
        x, y (corner from the origin); vars links them, also by model axis ('X': 'SLOT_X').
        datum_x / datum_y: what x / y are measured from instead of the origin, so the
        rectangle follows the model - an edge ('ref:...' from find_edges, 'edge:5'), a planar
        face seen edge-on ('+x', 'ref:...') or a vertex ('@x,y,z'); the datum for x must run
        across the sketch's x axis. A corner lying on the datum gets a relation, not a
        dimension."""
        return await call(param.sketch_rect, session, on, corner_mm, width_mm, height_mm, name, vars,
                          datum_x, datum_y)

    # --- sketches placed from the model (1.10.0) ------------------------------------------

    try:
        mcp.remove_tool("add_sketch")  # upstream's: dimensions from the origin only
    except Exception:  # noqa: BLE001 - an mcp without remove_tool: upstream's stays
        pass

    @mcp.tool()
    async def add_sketch(plane: str, start_mm: list, segments: list, name: str | None = None,
                         datum_x: str | list | None = None, datum_y: str | list | None = None) -> dict:
        """Draw a sketch of lines, arcs and splines on a plane, fully defined, to extrude or cut.

        plane: "front", "top", "right" or a plane by name (add_plane). Points are
        [u, v] in the plane's own axes, which the result gives (origin_mm + u
        x_axis + v y_axis): front is (x, y), top (x, -z), right (-z, y).
        The outline starts at start_mm; each segment runs on from where the last
        ended: {"line": [u, v]}; {"arc": [u, v], "through": [u, v]} (an arc through
        a middle point); {"arc": [u, v], "tangent": true} (an arc flowing on from
        the segment before); {"spline": [[u, v], ..., [u, v]]} (through the points,
        ending at the last). "tangent": true on a line checks that it flows on.
        End on start_mm to close the outline. Segments that meet smoothly get a
        tangent relation, the rest is dimensioned from the origin: x<i>/y<i> for
        the i-th point given (points_mm lists them; start_mm is 0), r<k> for the
        radius of segment k. datum_x / datum_y: measure the x<i> / y<i> from model
        geometry instead - an edge ('ref:...', 'edge:5'), a planar face seen edge-on
        ('+x') or a vertex ('@x,y,z') - so the outline follows the model; the names
        stay. Then extrude_sketch / cut_sketch the returned sketch.
        """
        return await call(sketch_datum.add_sketch, session, plane, start_mm, segments, name, datum_x, datum_y)

    server._rbr_add_sketch = add_sketch

    @mcp.tool()
    async def fully_define_sketch(sketch: str, datum_x: str | list | None = None,
                                  datum_y: str | list | None = None,
                                  replace_origin_dimensions: bool = True) -> dict:
        """Make an existing sketch fully defined and measured from the model, not the origin.
        datum_x / datum_y: the model entity its x / y positions are measured from - an edge
        ('ref:...' from find_edges, 'edge:5' from list_edges), a planar face seen edge-on
        ('+x', '#5', 'ref:...') or a vertex ('@x,y,z'). Every dimension or relation that ties
        a sketch point to the origin is replaced by one from the datum, under the same name
        (x, y2, ...), so set_dimension and equations keep working; the sketch then follows
        that geometry. What is still free afterwards (a sketch a person drew) gets relations
        and dimensions from SolidWorks' Fully Define Sketch, measured from the datums.
        Returns replaced, kept (with the reason), dimensions_added, origin_dimensions (those
        still from the origin) and fully_defined. A dimension an equation drives is kept."""
        return await call(sketch_datum.fully_define_sketch, session, sketch, datum_x, datum_y,
                          replace_origin_dimensions)

    @mcp.tool()
    async def add_dimension(sketch: str, entities: list | str, value_mm: float | None = None,
                            direction: str = "auto", name: str | None = None, var: str | None = None) -> dict:
        """Add one dimension to an existing sketch that is not yet fully defined - between its
        own entities, or from one of them to the model. entities: one or two of '#3' (a
        segment, read_sketch order), '#3.start' / '#3.end' / '#3.center', 'origin', and of the
        model 'ref:...' (find_edges / find_faces), 'edge:5' (list_edges), '@x,y,z' (the vertex
        there). One line gives its length, one circle its diameter, one arc its radius; two
        entities their distance (direction 'horizontal' / 'vertical' along the sketch axes), two
        lines that are not parallel their angle. value_mm (degrees for an angle) sets it and
        moves the geometry; name names it; var links it to a global variable. A dimension
        that would over-define the sketch is refused and removed."""
        return await call(sketch_datum.add_dimension, session, sketch, entities, value_mm, direction, name, var)

    @mcp.tool()
    async def sketch_fillet(sketch: str, corners: list | str, radius_mm: float, name: str | None = None,
                            var: str | None = None) -> dict:
        """Round corners of an existing sketch (a real sketch fillet): corners '#3.end' (a
        segment's end, read_sketch order), ['#2', '#3'] (where two segments meet), [x, y, z]
        model mm at the corner, or a list of those. The corner stays a virtual sharp, so the
        dimensions on it hold; the radius is one dimension for all corners given
        (dimensions.radius), changed by set_dimension, linked by var."""
        return await call(sketch_datum.sketch_fillet, session, sketch, corners, radius_mm, name, var)

    @mcp.tool()
    async def convert_entities(sketch: str | None = None, on: str | None = None, face: str | None = None,
                               edges: list | None = None, construction: bool = True) -> dict:
        """Convert a face's outer loop (face='+x' / '#5' / '@x,y,z') or edges (list_edges
        indices) into an existing sketch (sketch=) or a new one on a plane or face (on=).
        construction=True keeps them as reference geometry. Returns their segment indices."""
        return await call(param.convert_entities, session, sketch, on, face, edges, construction)

    @mcp.tool()
    async def add_relation(sketch: str, kind: str, entities: list) -> dict:
        """Add a sketch relation: kind coincident / on_edge (point onto a curve) / midpoint
        (point, line) / horizontal / vertical / tangent / parallel / perpendicular / concentric /
        collinear / coradial / at_intersection / fixed. entities: '#3' (segment, read_sketch
        order), '#3.start' / '#3.end' / '#3.center', 'origin'. Says whether it is fully defined."""
        return await call(param.add_relation, session, sketch, kind, entities)

    @mcp.tool()
    async def offset_face_loop(face: str, distance_mm: float, inward: bool = True,
                               link_to: str | None = None, name: str | None = None) -> dict:
        """A closed contour distance_mm inside (or outside) a planar face's outer edge loop, as a
        new sketch on the face - also where SolidWorks' own closed Offset Entities refuses
        because the material is thinner than two offsets (a thin edge). Each edge is
        converted (construction) and offset on its own, a collapsed offset is dropped, and the
        contour is the sketch REGION the offsets close: no trimming, so the offsets stay linked.
        link_to: a global variable driving every offset dimension. Returns region_point_mm:
        pass it to cut_toward(sketch, toward, depth_mm, region_point_mm=...)."""
        return await call(offset_loop.offset_face_loop, session, face, distance_mm, inward, link_to, name)

    @mcp.tool()
    async def extrude_toward(sketch: str, toward: list, depth_mm: float | None = None,
                             through_all: bool = False, up_to: str | list | None = None,
                             merge: bool = True, region_point_mm: list | None = None,
                             name: str | None = None) -> dict:
        """Boss-extrude a sketch toward a model direction [x, y, z] (where the material goes; no
        Dir/Flip guessing): depth_mm, through_all, or up_to a face ('+x', '#5', [x, y, z] on it).
        region_point_mm extrudes only the closed region of the sketch around that point.
        merge=False keeps a separate body. The depth comes back as dimensions.depth."""
        return await call(features.extrude, session, sketch, toward, depth_mm, through_all, up_to, merge,
                          region_point_mm, name)

    @mcp.tool()
    async def cut_toward(sketch: str, toward: list, depth_mm: float | None = None, through_all: bool = False,
                         up_to: str | list | None = None, region_point_mm: list | None = None,
                         name: str | None = None) -> dict:
        """Cut-extrude a sketch toward a model direction [x, y, z] (into the material): depth_mm,
        through_all, or up_to a face. region_point_mm cuts only the closed region around that
        point (e.g. offset_face_loop's contour). A cut that removes nothing is rolled back."""
        return await call(features.cut, session, sketch, toward, depth_mm, through_all, up_to,
                          region_point_mm, name)

    @mcp.tool()
    async def plane_normal_to_line(sketch: str, segment: int, at: str = "start", name: str | None = None) -> dict:
        """A reference plane perpendicular to line #segment of a sketch, through its start or
        end point - to sketch a hole whose axis is that line (a hinge axis at an angle)."""
        return await call(param.plane_normal_to_line, session, sketch, segment, at, name)

    @mcp.tool()
    async def mirror_features(features: list, plane: str, name: str = "Mirror") -> dict:
        """Mirror features (list_features names, not bodies) about 'front' / 'top' / 'right' or
        any plane of the part. The copies follow their seeds' dimensions."""
        return await call(param.mirror_features, session, features, plane, name)

    @mcp.tool()
    async def mirror_body(body: str, plane: str, merge: bool = True) -> dict:
        """Mirror one solid body (its list_bodies name) about a plane; merge joins the copy
        through combine_bodies (SolidWorks' own merge welded broken geometry) and warns when
        the volume did not roughly double."""
        return await call(geom.mirror_body, session, body, plane, merge)

    # --- solid bodies (1.8.0) ---------------------------------------------------------

    @mcp.tool()
    async def body_properties(bodies: list | str | None = None) -> dict:
        """Per solid body of the current part (all, or the named ones): volume, surface area,
        mass and centre of mass (by the material), bounding box, faces, visibility and material."""
        return await call(bodies_mod.body_properties, session, bodies)

    @mcp.tool()
    async def rename_body(body: str, new_name: str) -> dict:
        """Rename a solid body (its list_bodies name); the name survives rebuilds. The name is
        read back: when SolidWorks keeps the old one the call fails and says so (from a script,
        body.Name = ... can fail silently - use this tool, also as session method)."""
        return await call(bodies_mod.rename_body, session, body, new_name)

    @mcp.tool()
    async def show_bodies(bodies: list | str, visible: bool = True) -> dict:
        """Show (visible=True) or hide solid bodies by name."""
        return await call(bodies_mod.show_bodies, session, bodies, visible)

    @mcp.tool()
    async def delete_bodies(bodies: list | str, keep: bool = False, name: str | None = None) -> dict:
        """Body-Delete/Keep feature: delete the named bodies, or with keep=True keep only them.
        Undo with delete_feature."""
        return await call(bodies_mod.delete_bodies, session, bodies, keep, name)

    @mcp.tool()
    async def move_copy_body(bodies: list | str, translate_mm: list | None = None, rotate: dict | None = None,
                             copies: int = 0, name: str | None = None) -> dict:
        """Body-Move/Copy feature: translate_mm [dx, dy, dz] and/or rotate {axis: 'x'|'y'|'z',
        angle_deg, about_mm: [x, y, z]}; copies > 0 keeps the originals and adds that many
        copies, each a step further. Returns the new bodies."""
        return await call(bodies_mod.move_copy_body, session, bodies, translate_mm, rotate, copies, name)

    @mcp.tool()
    async def set_feature_scope(feature: str, bodies: list | str | None = None) -> dict:
        """Limit an existing cut / boss / revolve / hole (a list_features name) to the named
        bodies (Feature Scope); bodies=None makes it act on all bodies again. Returns each
        body's volume before and after."""
        return await call(bodies_mod.set_feature_scope, session, feature, bodies)

    @mcp.tool()
    async def save_bodies(bodies: list | str, path: str, close: bool = True) -> dict:
        """Save solid bodies to a new part file (linked to this one). Save this part first."""
        return await call(bodies_mod.save_bodies, session, bodies, path, close)

    @mcp.tool()
    async def insert_part(path: str, config: str = "", name: str | None = None, material: bool = False) -> dict:
        """Insert another part's solid bodies into the current part (linked Insert Part feature);
        the new bodies get unique names (name, name_2, ...). Position them with move_copy_body."""
        return await call(bodies_mod.insert_part, session, path, config, name, material)

    # --- derived parts (1.10.0) ---------------------------------------------------------

    @mcp.tool()
    async def derive_part(body: list | str, path: str, close: bool = False) -> dict:
        """Give a body of the current (saved) part its own part file that keeps following it
        ("Insert into New Part"): for a lid, a cover or a print section modelled as a body of
        the master. body: a list_bodies name (or several). The new part starts with one
        feature holding an external reference to the body; a later change of the body arrives
        there with update_external_refs (or when the part is opened). Returns the new part's
        references and its volume against the body's. close=False (default) leaves the new
        part open in the background; this part stays current."""
        return await _timed(derive.derive_part, session, body, path, close, timeout_s=300)

    @mcp.tool()
    async def list_external_refs(document: str | None = None) -> dict:
        """What the current document (or an open one by title) takes from other files: per
        external reference the feature holding it, the file and entity it points to, its
        status (in context / out of context / locked / broken / dangling), whether the file
        exists and is loaded, and whether that source has unsaved changes."""
        return await call(derive.list_external_refs, session, document)

    @mcp.tool()
    async def update_external_refs(load_sources: bool = True) -> dict:
        """Bring the current part up to date with the files it references (a derived part
        after its master changed): loads each referenced file that is not open, rebuilds,
        and returns each body's volume before and after (`changed`), the references' status
        and failing_features. A locked or broken reference does not update and is named."""
        return await _timed(derive.update_external_refs, session, load_sources, timeout_s=600)

    # --- SolidCAM (1.8.0) -------------------------------------------------------------

    @mcp.tool()
    async def solidcam_status() -> dict:
        """SolidCAM running in this SolidWorks: version, machines, operation templates and the
        open CAM part (machine, zeros, geometries, stock, operations, tools)."""
        return await _timed(solidcam.status, session, timeout_s=60)

    @mcp.tool()
    async def solidcam_new_part(path: str, machine: str, up: str = "+z", inch: bool = False) -> dict:
        """Start a SolidCAM milling part (.prz) for the current saved part on a machine
        (solidcam_status lists them). Makes the zero first: coordinate system SolidCAM_Zero on
        the top face (`up`) at the bottom-left corner. SolidCAM then shows its Part Data panel,
        which the user confirms (the result says how); afterwards solidcam_status shows the part."""
        return await _timed(solidcam.new_part, session, path, machine, up, inch, timeout_s=120)

    @mcp.tool()
    async def solidcam_zero(up: str = "+z") -> dict:
        """Make (or remake) the SolidWorks coordinate system SolidCAM_Zero in the current part:
        on the top face (`up`) at the bottom-left corner of the part's box."""
        return await call(solidcam.make_zero, session, up)

    @mcp.tool()
    async def solidcam_open(path: str) -> dict:
        """Open an existing SolidCAM part (.prz)."""
        return await _timed(solidcam.open_part, session, path, timeout_s=180)

    @mcp.tool()
    async def solidcam_add_zero(position: str = "top_corner", coordinate_system: str | None = None) -> dict:
        """Add a machine zero to the open CAM part: 'top_corner', 'top_center', 'cad_origin',
        or a SolidWorks coordinate system by name."""
        return await _timed(solidcam.add_zero, session, position, coordinate_system, timeout_s=60)

    @mcp.tool()
    async def solidcam_stock(offsets_mm: list | None = None, name: str = "Stock") -> dict:
        """Box stock around the model: offsets_mm [x+, y+, z+, x-, y-, z-], or SolidCAM's defaults."""
        return await _timed(solidcam.stock_box, session, offsets_mm, name, timeout_s=120)

    @mcp.tool()
    async def solidcam_add_operation(template: str, geometry: str | None = None, home: str | None = None) -> dict:
        """Add a job from an operation template (solidcam_status lists them) on a geometry of the
        CAM part (default the first) at a zero (default the first). The API cannot create job
        geometry: define chains / faces in SolidCAM, then name them here."""
        return await _timed(solidcam.add_operation, session, template, geometry, home, timeout_s=120)

    @mcp.tool()
    async def solidcam_calculate(operations: list | None = None, force: bool = False) -> dict:
        """Calculate toolpaths of all operations or the named ones; lists any left uncalculated."""
        return await _timed(solidcam.calculate, session, operations, force, timeout_s=1200)

    @mcp.tool()
    async def solidcam_gcode(operation: str | None = None, path: str | None = None) -> dict:
        """Post-process to G-code (whole part into its G-codes folder, or one operation into path)
        and read it back: lines, cutting moves, and a warning when a program does not cut."""
        return await _timed(solidcam.gcode, session, operation, path, timeout_s=600)

    @mcp.tool()
    async def solidcam_save(path: str | None = None) -> dict:
        """Save the open CAM part (or save it as path)."""
        return await _timed(solidcam.save, session, path, timeout_s=120)

    @mcp.tool()
    async def solidcam_close(close_model: bool = True) -> dict:
        """Close the open CAM part (and its model)."""
        return await _timed(solidcam.close, session, close_model, timeout_s=120)

    # --- cavity along a skin (1.7.0) ---------------------------------------------------

    @mcp.tool()
    async def sketch_intersection(sketch: str | None = None, plane: str | None = None, faces: list | str | None = None,
                                  body: str | None = None, name: str | None = None, detach: bool = True) -> dict:
        """The curves where a sketch plane cuts faces or a body, as segments of a 2D sketch on that
        plane: an existing sketch (sketch=) or a new one on plane= ('front', 'Plane5'). faces:
        selectors ('#5', '+x', [x, y, z] mm on the face); default all faces of body (a list_bodies
        name) or of every solid body. SolidWorks' 2D IntersectCurves creates nothing; this goes
        through a 3D sketch (Sketch3DIntersections) and Convert Entities. detach=True frees the
        curves for trimming (the helper 3D sketch is deleted, the curves stop following the skin);
        detach=False keeps them linked (hidden helper) but untrimmable. Returns the segments, the
        bounding box and region_area_mm2 of the largest closed contour."""
        return await _timed(cavity.sketch_intersection, session, sketch, plane, faces, body, name, detach,
                            timeout_s=300)

    @mcp.tool()
    async def sketch_trim_region(sketch: str, keep_box: dict | list, max_trims: int = 80) -> dict:
        """Trim a sketch's curves (e.g. sketch_intersection's) to what lies inside keep_box and close
        the cut with straight lines on its sides: ONE closed contour. keep_box in MODEL mm:
        {'x': [-114, 114]} (any of x/y/z, None for an open side) or two corners [[x1, y1, z1],
        [x2, y2, z2]]. The curve keeps its part inside the box, a side line its part inside the
        curve. Refuses (ok=false) when the result is not one region. Returns region_area_mm2,
        region_point_mm and the segments."""
        return await _timed(cavity.sketch_trim_region, session, sketch, keep_box, max_trims, timeout_s=300)

    @mcp.tool()
    async def sketch_offset_chain(sketch: str, distance_mm: float, inward: bool = True,
                                  original_to_construction: bool = True, link_to: str | None = None) -> dict:
        """Offset a sketch's closed contour(s) (SketchOffset2) distance_mm into the material
        (inward) or away from it, the side checked against the contour (a wrong-side offset is
        redone the other way), and make the original construction geometry. A hollow contour -
        an outline with holes - is taken loop by loop: the outline to its inside, each hole to
        its outside. One offset dimension per loop comes back (offset_dimensions); link_to: a
        global variable to drive them. wall_mm gives the measured min/max distance.
        SolidWorks' limit: where the contour is narrower than twice the distance (a wedge's
        tip, a thin web) no offset exists and the call is refused - use a smaller wall, cut
        the thin end off first (sketch_trim_region), or offset_face_loop on the face."""
        return await _timed(cavity.sketch_offset_chain, session, sketch, distance_mm, inward,
                            original_to_construction, link_to, timeout_s=300)

    @mcp.tool()
    async def split_line_project(sketch: str, faces: list | str, directional: bool = False, flip: bool = False,
                                 name: str | None = None) -> dict:
        """Split faces along a sketch projected onto them (Split Line, projection), e.g. a cavity
        contour onto the skin. faces: selectors ('#5', '+z', [x, y, z] mm on the face).
        directional=True projects one way (flip: the other). The new feature and the face count
        before/after prove it (SolidWorks answers None either way)."""
        return await _timed(cavity.split_line_project, session, sketch, faces, directional, flip, name,
                            timeout_s=300)

    @mcp.tool()
    async def loft_cut(sketches: list, guides: list | None = None, closed: bool = False,
                       name: str = "Loft-Cut") -> dict:
        """Cut a loft through existing sketches in the given order (each one closed contour, e.g.
        sections from sketch_intersection -> sketch_trim_region -> sketch_offset_chain); guides:
        guide-curve sketches. Returns removed_mm3; a loft that removes nothing is rolled back."""
        return await _timed(cavity.loft_cut, session, sketches, guides, closed, name, timeout_s=300)

    @mcp.tool()
    async def cut_offset_from_surface(sketch: str, face: str | list, offset_mm: float, toward: list,
                                      offset_reverse: bool = False, translate: bool = False,
                                      region_point_mm: list | None = None, name: str | None = None) -> dict:
        """Cut a sketch toward [x, y, z] ending offset_mm short of a face (end condition 'offset
        from surface'): a pocket keeping a wall under a curved skin - also a patch a split line
        cut out. face: '#5' / '+z' or [x, y, z] mm on it. offset_reverse: end beyond the face;
        translate: a flat end parallel-shifted instead of the face's offset shape.
        region_point_mm: one region of the sketch. The offset is dimensions.offset."""
        return await _timed(cavity.cut_offset_from_surface, session, sketch, face, offset_mm, toward,
                            offset_reverse, translate, region_point_mm, name, timeout_s=300)

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
    async def dialog_list() -> dict:
        """The dialogs SolidWorks shows right now, read without SolidWorks' help (so it answers
        while every other tool waits): title, message text, buttons, check boxes, and whether
        the dialog is modal - blocking SolidWorks until it is answered. A call that runs into a
        modal dialog answers DIALOG_BLOCKING after a few seconds with the same description; it
        is not cancelled and goes on when the dialog is answered. A progress window is marked
        progress and is not a question."""
        def build():
            pid = guard.state().get("sw_pid")
            found = dialogs.find(pids=[pid] if pid else None)
            return {"ok": True, "count": len(found), "blocking": len(dialogs.questions(found)),
                    "dialogs": [dialogs.public(d) for d in found], "server": guard.state()}
        return _local(build)

    @mcp.tool()
    async def dialog_respond(button: str | int, hwnd: int | None = None, title: str | None = None) -> dict:
        """Press a button of a SolidWorks dialog: its caption as dialog_list shows it ('Да',
        'Отмена', 'Rebuild'), its control id, or a language-neutral name for the standard
        ones - 'ok', 'cancel', 'yes', 'no', 'retry', 'ignore', 'abort', 'close', 'continue'.
        With several dialogs open name one by hwnd or part of its title; otherwise the one
        modal dialog is taken. Returns whether it closed and which dialogs remain (a second
        one often follows). What the answer does is SolidWorks' business: 'no' to "save
        changes?" discards them - read the text first, and ask the user when it is their
        data."""
        def press():
            pid = guard.state().get("sw_pid")
            result = dialogs.respond(button, hwnd=hwnd, title=title, pids=[pid] if pid else None)
            journal.record("dialog_respond", {"button": button, "hwnd": hwnd, "title": title}, result)
            return result
        return _local(press)

    @mcp.tool()
    async def release_document(title: str | None = None) -> dict:
        """Free a document (or all, title=None) that another server process holds after changing it,
        so this one may change it too. Only when you know the other session is done with it."""
        return _local(guard.release_document, title)

    @mcp.tool()
    async def api_search(query: str, kind: str = "any", interface: str | None = None, limit: int = 30) -> dict:
        """Find a method, property or constant of the INSTALLED SolidWorks API by name or words
        (case-blind): api_search('InsertCutSwept') gives each method's parameters in call
        order and its help line; 'IFeatureManager.EditFreeze' or interface= narrows to one
        interface; kind 'method' / 'property' / 'constant' - constants are the swconst enum
        values (api_search('freeze', kind='constant') -> swMoveFreezeBarTo_e.swMoveFreezeBarToTop
        = 4). Use it before writing a run_script call you are not sure of; api(obj) inside a
        script lists one object's members."""
        return await call(apiref.api_search, session, query, kind, interface, limit)

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

    _extend_builders(mcp)


# How often a waiting call looks for a modal SolidWorks dialog (s); 0 switches the watch off.
DIALOG_WATCH_S = float(os.environ.get("SWMCP_DIALOG_WATCH_S", "1.5"))

BODIES_DOC = (" bodies=[names]: the feature acts on these bodies only (list_bodies names), the others "
              "stay as they are.")
EXPECT_DOC = (" expect={'volume': 'decrease', 'bodies': 'same', ...}: the call fails and what it added is "
              "removed when it does otherwise (keys: volume, volume_mm3 [low, high], bodies, changed_bodies, "
              "no_errors, fully_defined). The result's `change` gives each body's volume before and after.")


def _extend_builders(mcp) -> None:
    """Give every building tool expect= (and the cuts, bosses and holes bodies=): the tool is
    registered again under its name with those parameters added; scope.runner does the work."""
    import inspect
    try:
        registered = list(mcp._tool_manager.list_tools())
    except Exception:  # noqa: BLE001 - an mcp without a tool manager: the tools stay as they are
        return
    for tool in registered:
        name, original = tool.name, tool.fn
        if not scope.is_builder(name) or getattr(original, "_rbr_extended", False):
            continue
        signature = inspect.signature(original)
        extra = []
        if name in scope.SCOPED and "bodies" not in signature.parameters:
            extra.append(inspect.Parameter("bodies", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None,
                                           annotation=list | str | None))
        if "expect" not in signature.parameters:
            extra.append(inspect.Parameter("expect", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None,
                                           annotation=dict | None))
        extended = signature.replace(parameters=[*signature.parameters.values(), *extra])
        added = {p.name for p in extra}

        def make(tool_name, fn, sig, own):
            async def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                request = {"tool": tool_name,
                           "bodies": bound.arguments.pop("bodies") if "bodies" in own else None,
                           "expect": bound.arguments.pop("expect") if "expect" in own else None}
                token = scope.REQUEST.set(request)
                try:
                    return await fn(*bound.args, **bound.kwargs)
                finally:
                    scope.REQUEST.reset(token)

            wrapper.__name__ = tool_name
            wrapper.__qualname__ = tool_name
            wrapper.__doc__ = fn.__doc__
            wrapper.__signature__ = sig
            wrapper.__wrapped_tool__ = fn
            wrapper._rbr_extended = True
            return wrapper

        description = (tool.description or "") + (BODIES_DOC if "bodies" in added else "") + EXPECT_DOC
        try:
            mcp.remove_tool(name)
            mcp.add_tool(make(name, original, extended, added), name=name, description=description)
        except Exception:  # noqa: BLE001 - keep the tool as it was rather than lose it
            if name not in {t.name for t in mcp._tool_manager.list_tools()}:
                mcp.add_tool(original, name=name, description=tool.description)
