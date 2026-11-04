"""Speed patches for SolidWorksSession, applied at start-up (no upstream file edited).

Measured on a 157-feature part (SW 2025, rev 33.3), out-of-process COM:

- `_iter_features` walked the tree with FirstFeature/GetNextFeature, one RPC
  per feature: 200-1100 ms depending on SolidWorks' UI state.
  IFeatureManager.GetFeatures(True) returns the same top-level features in the
  same order (verified) in one call: 130-150 ms.
- `_history_snapshot` ran before EVERY tool call (also read-only ones) and read
  every feature name: 860-1190 ms. It now stores (title, last feature name):
  1-5 ms. The full name set is only built when the rollback bar is above the
  last feature (IFeature.IsRolledBack), where new features land mid-tree.
- `_roll_back` (only on a failed call) deletes what follows the remembered last
  feature, so it needs no name set taken in advance.

Each patch keeps the original's contract; tests/rbr/test_rbr.py pins it.
"""

from .. import binding
from ..session import SolidWorksSession

SW_DOC_PART = 1

_ORIGINAL = {}


def _iter_features(self):
    """Yield each top-level feature as a wrapped IFeature, in tree order (one RPC)."""
    fm = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
    feats = None
    try:
        feats = fm.GetFeatures(True)
    except Exception:  # noqa: BLE001 - fall back to the walk the upstream code uses
        feats = None
    if feats is None:
        yield from _ORIGINAL["_iter_features"](self)
        return
    if not isinstance(feats, (list, tuple)):
        feats = [feats]
    cls = self._mod.IFeature
    for f in feats:
        yield binding.wrap(f, cls)


def _rolling_back(self, model) -> bool:
    """True when the rollback bar is above the last feature (new features then land mid-tree).

    IFeatureManager has no IsRollingBack in SW 2025; the last feature reports IsRolledBack.
    """
    try:
        last = binding.wrap(model.FeatureByPositionReverse(0), self._mod.IFeature)
        return last is not None and bool(last.IsRolledBack())
    except Exception:  # noqa: BLE001 - unknown: be safe, take the slow snapshot
        return True


def _history_snapshot(self):
    """('fast', title, last top-level feature name), or the upstream
    (title, set of names) when the rollback bar is not at the end."""
    model = self._model
    if model is None or int(model.GetType()) != SW_DOC_PART:
        return None
    if _rolling_back(self, model):
        return _ORIGINAL["_history_snapshot"](self)
    last = model.FeatureByPositionReverse(0)
    return ("fast", model.GetTitle(), last.Name if last is not None else None)


def _roll_back(self, snapshot) -> list:
    if snapshot is None or snapshot[0] != "fast":
        return _ORIGINAL["_roll_back"](self, snapshot)
    _, title, last = snapshot
    if self._model is None or self._model.GetTitle() != title:
        return []
    names = [f.Name for f in self._iter_features()]
    if last not in names:
        return []  # the call removed the feature we anchored on: nothing safe to undo
    added = set(names[names.index(last) + 1:])
    if not added:
        return []
    keep = {f.Name for f in self._history()} - added
    return self._remove_features_since(keep)


SW_DELETE_ABSORBED = 2  # swDeleteSelectionOptions_e, as session.py


def _sub_names(self, feat) -> list:
    """Names of the features a feature absorbed (its sketches)."""
    out = []
    try:
        sub = feat.GetFirstSubFeature()
        while sub is not None:
            sub = binding.wrap(sub, self._mod.IFeature)
            out.append(sub.Name)
            sub = sub.GetNextSubFeature()
    except Exception:  # noqa: BLE001 - unknown: treat as owning nothing old
        return out
    return out


def _remove_features_since(self, before: set) -> list:
    """Delete what the history gained since `before`, newest first; returns the
    names that could not be removed.

    Upstream deletes each with its absorbed features. That also deleted a sketch
    that existed BEFORE the call when the call's new feature had consumed it: a
    split line made from a person's sketch, then a failing cut in the same call,
    took the sketch with it (seen live 04.11.2026). A feature that absorbed an
    older feature is deleted alone, which hands the sketch back to the tree; a
    second pass removes what was new among its absorbed features.
    """
    model = self._require_model()
    extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
    for _pass in range(2):
        new = [f for f in self._history() if f.Name not in before]
        if not new:
            break
        # names and absorbed features read up front: a feature deleted along with
        # another is a dead COM object, even its Name
        plan = [(feat, feat.Name, _sub_names(self, feat)) for feat in reversed(new)]
        gone = set()
        for feat, name, subs in plan:
            if name in gone:
                continue  # went with the feature that absorbed it
            option = 0 if any(n in before for n in subs) else SW_DELETE_ABSORBED
            if option:
                gone.update(subs)
            model.ClearSelection2(True)
            if feat.Select2(False, 0):
                extension.DeleteSelection2(option)
    model.ForceRebuild3(False)
    return [f.Name for f in self._history() if f.Name not in before]


def apply() -> list:
    """Install the patches once; returns the names patched."""
    if _ORIGINAL:
        return sorted(_ORIGINAL)
    for name, fn in (("_iter_features", _iter_features), ("_history_snapshot", _history_snapshot),
                     ("_roll_back", _roll_back), ("_remove_features_since", _remove_features_since)):
        _ORIGINAL[name] = getattr(SolidWorksSession, name)
        setattr(SolidWorksSession, name, fn)
    return sorted(_ORIGINAL)


def unapply() -> None:
    """Restore the upstream methods (test teardown); apply() works again after."""
    for name, fn in _ORIGINAL.items():
        setattr(SolidWorksSession, name, fn)
    _ORIGINAL.clear()
