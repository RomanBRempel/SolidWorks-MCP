"""run_script: execute a whole batch of SolidWorks API work in ONE MCP call.

Runs on the server's single COM thread inside SolidWorksSession.run_guarded, so
the UI stays frozen (no redraw, no feature-tree update), CommandInProgress is
set, and a failed script rolls back what it added to the current part.

Every run is written to the journal with its full code (rbr/journal.py), and
the result carries the feature-tree change, so what the script did is visible.
"""

import json
import time
import traceback

from .. import binding
from ..errors import SolidWorksError
from . import journal

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

    ns.update(comp=comp, select=select, feature=feature)
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
