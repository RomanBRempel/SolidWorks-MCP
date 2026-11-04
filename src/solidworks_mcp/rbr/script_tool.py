"""run_script: execute a whole batch of SolidWorks API work in ONE MCP call.

Runs on the server's single COM thread inside SolidWorksSession.run_guarded, so
the UI stays frozen (no redraw, no feature-tree update), CommandInProgress is
set, and a failed script rolls back what it added to the current part.

Every run is written to the journal with its full code (rbr/journal.py), and
the result carries the feature-tree change, so what the script did is visible.
"""

import contextlib
import json
import time
import traceback

import pythoncom
import win32com.client

from .. import binding
from ..errors import SolidWorksError
from . import geom, journal

SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_DOC_DRAWING = 3
MAX_OUT = 20000


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def api(obj, contains: str = "") -> dict:
    """What an early-bound SolidWorks object offers: its interface, methods and properties.

    Use it before guessing a name (SW 2025 lacks some members the online help lists,
    e.g. ILoftFeatureData.StartConstraint). A raw dispatch has no list: wrap() it first.
    """
    cls = type(obj)
    props = set(getattr(cls, "_prop_map_get_", {}) or {}) | set(getattr(cls, "_prop_map_put_", {}) or {})
    methods = {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n, None))} - props
    needle = contains.lower()
    pick = (lambda names: sorted(n for n in names if needle in n.lower()))
    return {"interface": cls.__name__, "methods": pick(methods), "properties": pick(props),
            "note": None if props or methods else "raw dispatch: wrap(obj, mod.IXxx) first"}


def namespace(session, out: list, args: dict | None = None) -> dict:
    """The names a script sees (documented in the run_script tool)."""
    sw = session._ensure()
    mod = session._mod
    active = binding.wrap(sw.ActiveDoc, mod.IModelDoc2)
    model = session._model if session._model is not None else active
    if model is not None and session._quiet_depth:
        try:
            session._quiet(model)
        except Exception:  # noqa: BLE001
            pass

    def _print(*items, sep=" ", end="\n", **_):
        out.append(sep.join(str(a) for a in items) + end)

    ns = {
        "sw": sw, "swApp": sw, "mod": mod, "wrap": binding.wrap,
        "model": model, "active": active, "session": session,
        "SolidWorksError": SolidWorksError, "print": _print, "result": None,
        "MM": 0.001, "DEG": 3.141592653589793 / 180.0, "args": dict(args or {}),
    }
    if model is not None:
        ns["ext"] = binding.wrap(model.Extension, mod.IModelDocExtension)
        ns["selmgr"] = binding.wrap(model.SelectionManager, mod.ISelectionMgr)
        ns["fm"] = binding.wrap(model.FeatureManager, mod.IFeatureManager)
        doc_type = int(model.GetType())
        if doc_type == SW_DOC_ASSEMBLY:
            ns["asm"] = binding.wrap(model, mod.IAssemblyDoc)
        elif doc_type == SW_DOC_PART:
            ns["part"] = binding.wrap(model, mod.IPartDoc)

    def comp(name):
        """Component of the current assembly by list_components name."""
        if "asm" not in ns:
            raise SolidWorksError("comp() needs an assembly as the current document.")
        return session._component_by_name(ns["asm"], name)

    def select(objs, append=False):
        """Select components/entities (Select4 with the doc's SelectData); returns the count."""
        if not append:
            model.ClearSelection2(True)
        data = ns["selmgr"].CreateSelectData()
        return sum(1 for o in objs if o.Select4(True, data, False))

    def feature(name):
        """A feature of the current document by its tree name (early-bound IFeature)."""
        for f in session._iter_features():
            if f.Name == name:
                return f
        raise SolidWorksError(f"No feature named '{name}'.")

    def features(type_name: str | None = None):
        """Top-level features of the current document (one COM call for the list), optionally of one type."""
        out_ = list(session._iter_features())
        return [f for f in out_ if type_name is None or f.GetTypeName2() == type_name]

    def vec(*values):
        """A VARIANT(VT_ARRAY|VT_R8) for CreateSpline2, CreatePoint and friends.

        A plain Python list reaches those APIs as None or garbage (1e-311);
        this is the marshalling they actually need. Nested lists flatten.
        """
        flat = []
        for v in values:
            if isinstance(v, (list, tuple)):
                flat.extend(float(x) for x in v)
            else:
                flat.append(float(v))
        return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)

    def pt(x, y, z):
        """An IMathPoint at (x, y, z) metres, via MathUtility with the right VARIANT."""
        mu = binding.wrap(sw.GetMathUtility(), mod.IMathUtility)
        return binding.wrap(mu.CreatePoint(vec(x, y, z)), mod.IMathPoint)

    @contextlib.contextmanager
    def ui_unlocked():
        """Temporarily lift CommandInProgress and the graphics freeze.

        InsertSketch/EditSketch refuse to open a sketch while the script's UI
        freeze is on; wrap just those calls, the freeze comes back after.
        """
        was_busy = bool(sw.CommandInProgress)
        if was_busy:
            sw.CommandInProgress = False
        view = binding.wrap(model.ActiveView, mod.IModelView) if model is not None else None
        was_still = view is not None and not view.EnableGraphicsUpdate
        if was_still:
            view.EnableGraphicsUpdate = True
        try:
            yield
        finally:
            if was_still:
                view.EnableGraphicsUpdate = False
            if was_busy:
                sw.CommandInProgress = True

    def tree_before():
        """A snapshot for features_added / rename_new, taken before the risky call."""
        return journal.tree_state(session)

    def features_added(before):
        """Names of the features appended since the tree_before() snapshot."""
        return journal.tree_added(session, before, journal.tree_state(session))

    def rename_new(before, name):
        """Rename the newest feature added since `before` (a tree_before() snapshot).

        Setting .Name on what InsertRefPlane itself returned fails with a type
        mismatch; the feature re-fetched from the tree renames fine.
        """
        added = features_added(before)
        if not added:
            raise SolidWorksError("No feature was added since the snapshot; nothing to rename.")
        feat = next((f for f in session._iter_features() if f.Name == added[-1]), None)
        if feat is None:
            raise SolidWorksError(f"The added feature '{added[-1]}' is not in the tree any more.")
        feat.Name = name
        return {"renamed": added[-1], "to": name}

    def combine(operation, main, tools=None, name="Combine"):
        """Join/subtract/intersect bodies through the proven combine_bodies path
        ('add'/'subtract'/'common'); InsertCombineFeature called raw from a script fails."""
        return session.combine_bodies(operation, main, tools, name)

    def new_part():
        """A new part (MMGS) that becomes the current document; returns its IModelDoc2.

        swApp.NewDocument(GetUserPreferenceStringValue(8), 0, 0, 0) from a script
        answers None (the template path comes back empty); session.new_part reads
        and checks the default template and falls back without a modal dialog.
        """
        session.new_part()
        return session._model

    def activate(title):
        """Make an open document current by its title; returns its IModelDoc2.

        Raw ActivateDoc3 answers a (doc, errors) tuple - calling methods on that
        tuple fails - and does not update the server's current document; this
        goes through session.activate_document (title with or without the
        extension) and hands back the early-bound model.
        """
        session.activate_document(title)
        return session._model

    ns.update(comp=comp, select=select, feature=feature, features=features, api=api, swapp=sw,
              vec=vec, pt=pt, ui_unlocked=ui_unlocked, tree_before=tree_before,
              features_added=features_added, rename_new=rename_new, combine=combine,
              new_part=new_part, activate=activate,
              mirror_body=lambda body, plane, merge=True: geom.mirror_body(session, body, plane, merge),
              whats_wrong=lambda: geom.whats_wrong(session))
    try:
        import numpy as np  # optional: present when installed with `--with numpy`
        ns["np"] = np
    except ImportError:
        pass
    return ns


def run_script(session, code: str, args: dict | None = None) -> dict:
    out = []
    ns = namespace(session, out, args)
    before = journal.tree_state(session)
    t0 = time.perf_counter()
    try:
        exec(compile(code, "<run_script>", "exec"), ns)  # noqa: S102 - the point of the tool
    except SolidWorksError:
        raise
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=6)
        stdout = "".join(out)[-4000:]
        raise SolidWorksError(f"Script failed: {type(exc).__name__}: {exc}\n{tb}\nstdout:\n{stdout}")
    elapsed = round(time.perf_counter() - t0, 3)
    after = journal.tree_state(session)
    stdout = "".join(out)
    res = {
        "ok": True,
        "elapsed_s": elapsed,
        "result": _jsonable(ns.get("result")),
        "stdout": stdout[-MAX_OUT:],
        "stdout_truncated": len(stdout) > MAX_OUT,
    }
    if before and after and before["title"] == after["title"] and before["count"] != after["count"]:
        res["features"] = {"before": before["count"], "after": after["count"],
                           "added": journal.tree_added(session, before, after)}
    return res
