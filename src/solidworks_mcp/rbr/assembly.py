"""insert_component without a window per inserted file (the cause of the crashes of 2026-11-18/19).

AddComponent5 gives None for a document that is not loaded, so upstream opens
each component's file first - the ordinary way, which gives every inserted
file a document WINDOW that nothing closes again. Measured on SolidWorks 2025
SP3 (2026-11-19):

    a document window                     about 250 GDI objects of SLDWORKS.exe
    the same file loaded without a window 0
    a file opened into a window and closed 37-45 objects never returned
    upstream insert_component, new file   +251, and +45 for good after closing all

Windows gives one process 10 000 GDI objects and SolidWorks starts with 2 000:
thirty-odd document windows fill it. Then SolidWorks cannot create a device
context - its journal fills with 'the requested resource is unavailable'
message boxes, AddComponent5 fails with error 61704 ('internal application
error'), and it dies in its own drawing code (mfc140u <- uiBaseView_c::setupDC
<- uiModelView_c::OnMouseMove). Windows logs that as a GDIObjectLeak report
(Application log, Windows Error Reporting, event 1001), not as an Application
Error 1000.

Here the file is loaded with ISldWorks.DocumentVisible(False, type): no
window, no GDI object, the assembly stays the active document, and SolidWorks
unloads the file together with the assembly. 16 inserts cost 0 GDI objects
and left 0 behind (against +2005 and +365).
"""

import functools
import os

import pythoncom

from .. import binding
from ..constants import (SW_ADD_COMPONENT_CURRENT_CONFIG, SW_DOC_ASSEMBLY, SW_DOC_PART,
                         SW_DONT_REBUILD_ACTIVE_DOC, SW_OPEN_DOC_SILENT)
from ..errors import SolidWorksError
from ..session import SolidWorksSession

_ORIGINAL = {}


def com_text(exc) -> str:
    """SolidWorks' own words for a COM error: its description and code, else the system's; pure."""
    info = getattr(exc, "excepinfo", None)
    if info and len(info) > 2 and info[2]:
        text = str(info[2]).strip()
        return f"{text} (code {info[0]})" if info[0] else text
    return str(getattr(exc, "strerror", None) or exc)


def load_hidden(session, abs_path: str, doc_type: int) -> bool:
    """Load a document without a window, unless it is open already; True when it was loaded here.

    DocumentVisible is a setting of the whole SolidWorks session: it is put
    back in `finally`, or the user's next File > Open would show nothing."""
    sw = session._sw
    if sw.GetOpenDocumentByName(abs_path) is not None:
        return False
    try:
        title = session._model.GetTitle() if session._model is not None else None
    except Exception:  # noqa: BLE001 - the current document was closed meanwhile: nothing to put back
        title = None
    sw.DocumentVisible(False, doc_type)
    try:
        result = sw.OpenDoc6(abs_path, doc_type, SW_OPEN_DOC_SILENT, "", 0, 0)
    finally:
        sw.DocumentVisible(True, doc_type)
    doc, errors = (result[0], result[1]) if isinstance(result, tuple) else (result, 0)
    if doc is None:
        raise SolidWorksError(f"SolidWorks could not load '{abs_path}' (swFileLoadError_e {errors}); "
                              "nothing was inserted.")
    if title is not None:
        active = binding.wrap(sw.ActiveDoc, session._mod.IModelDoc2)
        if active is None or active.GetTitle() != title:
            sw.ActivateDoc3(title, False, SW_DONT_REBUILD_ACTIVE_DOC, 0)
    return True


NATIVE = {".sldprt": SW_DOC_PART, ".sldasm": SW_DOC_ASSEMBLY}


def open_hidden(session, path: str) -> dict:
    """Load a part or an assembly WITHOUT a document window and make it the current document
    of the tools (no GDI objects; SolidWorks' active document stays in front).

    A document open already becomes current as it is (visible or not). Only native files:
    an import needs a window of its own."""
    abs_path = os.path.abspath(path)
    doc_type = NATIVE.get(os.path.splitext(abs_path)[1].lower())
    if doc_type is None:
        raise SolidWorksError("visible=False opens native .sldprt / .sldasm files only; an import "
                              "(STEP, IGES, Parasolid) needs a window.")
    if not os.path.isfile(abs_path):
        raise SolidWorksError(f"No such file: {abs_path}")
    sw = session._ensure()
    loaded = load_hidden(session, abs_path, doc_type)
    model = binding.wrap(sw.GetOpenDocumentByName(abs_path), session._mod.IModelDoc2)
    if model is None:
        raise SolidWorksError(f"SolidWorks loaded '{abs_path}' but does not hand it out by name.")
    session._model = model
    return {"ok": True, "title": model.GetTitle(), "path": abs_path, "visible": bool(model.Visible),
            "loaded": loaded,
            "note": "Loaded without a window: the tools work on it as the current document; SolidWorks' own "
                    "active window is unchanged. close_part closes it; screenshot needs mode='render'."}


def _with_visible(name: str, original):
    """open_part / open_assembly with visible=False: the file is loaded without a window.
    allow_large (upstream: an import over 30 MB) applies to the ordinary open only - a
    windowless open takes native files, never imports."""

    @functools.wraps(original)
    def opener(self, path: str, visible: bool = True, allow_large: bool = False) -> dict:
        if visible:
            return original(self, path, allow_large=allow_large)
        return open_hidden(self, path)

    opener._rbr_visible = True
    return opener


HIDDEN_HINT = ("The document was loaded without a window (visible=False): SolidWorks sketches only in a "
               "document with a window. Changing dimensions, equations, suppression and reading work without "
               "one; to sketch, open the file again with visible=True (it costs about 250 GDI objects).")


def _windowless_require_model(original):
    """Upstream makes the current document SolidWorks' active one before a writing tool runs;
    for a document loaded without a window that gives it one (measured: +443 GDI objects for
    a cylinder and a dimension). Such a document stays windowless: what works without a
    window (dimensions, equations, features that do not sketch) runs, a sketch fails with
    HIDDEN_HINT (see _with_hidden_hint)."""

    @functools.wraps(original)
    def require_model(self):
        model = self._model
        if model is not None:
            try:
                hidden = not bool(model.Visible)
            except Exception:  # noqa: BLE001 - cannot tell: upstream's way
                hidden = False
            if hidden:
                # not stilled either: without a window nothing redraws, and waking the feature
                # tree of a windowless document closed during the call faults inside SolidWorks
                # (RPC_E_SERVERFAULT, measured) where a windowed one only answers an RPC error
                return model
        return original(self)

    require_model._rbr_windowless = True
    return require_model


def _with_hidden_hint(original):
    """run_guarded: a failure in a windowless current document says why it may have failed."""

    @functools.wraps(original)
    def run_guarded(self, fn, *args, **kwargs):
        try:
            return original(self, fn, *args, **kwargs)
        except SolidWorksError as exc:
            try:
                hidden = self._model is not None and not bool(self._model.Visible)
            except Exception:  # noqa: BLE001
                hidden = False
            if hidden and HIDDEN_HINT not in str(exc):
                raise SolidWorksError(f"{exc} {HIDDEN_HINT}") from exc
            raise

    run_guarded._rbr_hidden_hint = True
    return run_guarded


def insert_component(self, path: str, x_mm: float = 0.0, y_mm: float = 0.0,
                     z_mm: float = 0.0, fixed: bool | None = None) -> dict:
    """Upstream's insert_component with the component's file loaded invisibly.

    Besides that: a refusal of AddComponent5 (a COM error that is not a lost
    link, such as 61704) comes back as an error that names it, with the
    assembly as it was; and the assembly is rebuilt after the transform is
    written, so the tree and the graphics show the component where it is."""
    abs_path = os.path.abspath(path)
    doc_type = {".sldprt": SW_DOC_PART, ".sldasm": SW_DOC_ASSEMBLY}.get(os.path.splitext(abs_path)[1].lower())
    if doc_type is None:
        raise SolidWorksError(f"insert_component takes a .sldprt or a .sldasm (got '{path}').")
    asm = self._require_assembly()
    if not os.path.isfile(abs_path):
        raise SolidWorksError(f"Part not found: {abs_path}")
    if fixed is None:
        fixed = not self._component_dispatches(asm)

    load_hidden(self, abs_path, doc_type)
    try:
        raw = asm.AddComponent5(abs_path, SW_ADD_COMPONENT_CURRENT_CONFIG, "", False, "", 0.0, 0.0, 0.0)
    except pythoncom.com_error as exc:
        if exc.hresult in self._GONE_HRESULTS:
            raise  # the link itself: run_guarded tells a crash from a closed document
        raise SolidWorksError(
            f"SolidWorks refused to insert '{abs_path}': {com_text(exc)}. The assembly is as it was. "
            "server_health shows whether SolidWorks is short of window resources (sldworks_gui: GDI objects) "
            "or memory; if it is, save, close document windows and restart SolidWorks before inserting more."
        ) from exc
    comp = binding.wrap(raw, self._mod.IComponent2)
    if comp is None:
        raise SolidWorksError(
            f"AddComponent5 returned None for '{abs_path}'. Is it a valid SolidWorks part, and could "
            "SolidWorks load it?")
    self._apply_transform(comp, x_mm, y_mm, z_mm, 0.0, 0.0, 0.0)
    if fixed:
        self._fix_component(asm, comp)
    try:
        self._model.EditRebuild3()
    except pythoncom.com_error as exc:
        if exc.hresult in self._GONE_HRESULTS:
            raise
    return {"ok": True, "component": self._component_entry(comp)}


def apply() -> None:
    if "insert_component" not in _ORIGINAL:
        _ORIGINAL["insert_component"] = SolidWorksSession.insert_component
        functools.update_wrapper(insert_component, _ORIGINAL["insert_component"])  # keeps doc and __wrapped__
        SolidWorksSession.insert_component = insert_component
    for name in ("open_part", "open_assembly"):   # also when insert_component was installed before
        current = getattr(SolidWorksSession, name)
        if not getattr(current, "_rbr_visible", False):
            _ORIGINAL[name] = current
            setattr(SolidWorksSession, name, _with_visible(name, current))
    if not getattr(SolidWorksSession._require_model, "_rbr_windowless", False):
        _ORIGINAL["_require_model"] = SolidWorksSession._require_model
        SolidWorksSession._require_model = _windowless_require_model(SolidWorksSession._require_model)
    if not getattr(SolidWorksSession.run_guarded, "_rbr_hidden_hint", False):
        _ORIGINAL["run_guarded"] = SolidWorksSession.run_guarded
        SolidWorksSession.run_guarded = _with_hidden_hint(SolidWorksSession.run_guarded)


def unapply() -> None:
    """Restore upstream's insert_component (test teardown)."""
    if _ORIGINAL:
        if "insert_component" in _ORIGINAL:
            SolidWorksSession.insert_component = _ORIGINAL["insert_component"]
        for name in ("open_part", "open_assembly", "_require_model", "run_guarded"):
            if name in _ORIGINAL:
                setattr(SolidWorksSession, name, _ORIGINAL[name])
        _ORIGINAL.clear()
