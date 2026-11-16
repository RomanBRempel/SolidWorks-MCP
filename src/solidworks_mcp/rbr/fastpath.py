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

Measured on a 205-feature part with 47 documents open (2026-11-16, 1.10.0):

- GetFeatures(True) itself is one call, but win32com turned each of the 222
  returned objects into a wrapper with a QueryInterface and two GetTypeInfo
  calls: 0.85 s per walk with CommandInProgress set, 5-8 s without. The array
  is now read raw and wrapped locally: no call per feature.
- with the rollback bar inside the tree the snapshot fell back to the full
  name set on every modifying call. It now remembers the two features the bar
  lies between ('bar' snapshot): what a failed call added sits between them.
- a failed call that had added nothing still ran ForceRebuild3 (8-9 s there).

Each patch keeps the original's contract; tests/rbr/test_rbr.py pins it.
"""

from .. import binding
from ..session import SolidWorksSession

SW_DOC_PART = 1

_ORIGINAL = {}
_RAW_CLASSES = {}


def _raw_feature_manager(cls):
    """`cls` (the generated IFeatureManager) with results left as raw dispatches:
    the generated method still makes the call, by its own dispid."""
    raw = _RAW_CLASSES.get(cls)
    if raw is None:
        class RawFeatureManager(cls):
            def _get_good_object_(self, obj, obUserName=None, resultCLSID=None):
                return obj

        raw = _RAW_CLASSES[cls] = RawFeatureManager
    return raw


def _iter_features(self):
    """Yield each top-level feature as a wrapped IFeature, in tree order (one RPC)."""
    fm = binding.wrap(self._model.FeatureManager, self._mod.IFeatureManager)
    feats = None
    try:
        feats = _raw_feature_manager(self._mod.IFeatureManager)(getattr(fm, "_oleobj_", fm)).GetFeatures(True)
    except Exception:  # noqa: BLE001 - fall back to the plain call, then to upstream's walk
        try:
            feats = fm.GetFeatures(True)
        except Exception:  # noqa: BLE001
            feats = None
    if feats is None:
        yield from _ORIGINAL["_iter_features"](self)
        return
    if not isinstance(feats, (list, tuple)):
        feats = [feats]
    cls = self._mod.IFeature
    for f in feats:
        yield _bind(f, cls)


def _bind(raw, cls):
    """`cls` around a raw feature dispatch WITHOUT the QueryInterface that
    DispatchBaseClass.__init__ makes for each object (3 ms each: 0.7 s per walk
    of 222 features). A feature's own dispatch is IFeature: Name, GetTypeName2,
    IsRolledBack, GetID, GetErrorCode2, IsSuppressed2 and IsFrozen answered the
    same on all 222 features both ways, sub-features, sketches, dimensions and
    Select2 included (SW 2025 SP3, 2026-11-16)."""
    if type(raw).__name__ != "PyIDispatch":
        return binding.wrap(raw, cls)
    try:
        obj = cls.__new__(cls)
        obj.__dict__["_oleobj_"] = raw
        return obj
    except Exception:  # noqa: BLE001 - a class that cannot be made bare: the usual way
        return binding.wrap(raw, cls)


def live_count(feats) -> int:
    """How many of `feats` (tree order) lie above the rollback bar.

    Everything below the bar reports IsRolledBack, so the boundary is found by
    bisection: one call when nothing is rolled back, ~log2(n) otherwise."""
    if not feats:
        return 0
    try:
        if not feats[-1].IsRolledBack():
            return len(feats)
        lo, hi = 0, len(feats) - 1   # invariant: feats[hi] is rolled back
        while lo < hi:
            mid = (lo + hi) // 2
            if feats[mid].IsRolledBack():
                hi = mid
            else:
                lo = mid + 1
        return lo
    except Exception:  # noqa: BLE001 - unknown: behave as if nothing were rolled back
        return len(feats)


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
    """('fast', title, last top-level feature name); with the rollback bar inside
    the tree ('bar', title, feature above the bar, feature below it, feature
    count); upstream's (title, set of names) when neither can be read."""
    model = self._model
    if model is None or int(model.GetType()) != SW_DOC_PART:
        return None
    if _rolling_back(self, model):
        try:
            feats = list(self._iter_features())
            count = live_count(feats)
            if 0 < count < len(feats):
                return ("bar", model.GetTitle(), feats[count - 1].Name, feats[count].Name, len(feats))
        except Exception:  # noqa: BLE001
            pass
        return _ORIGINAL["_history_snapshot"](self)
    last = model.FeatureByPositionReverse(0)
    return ("fast", model.GetTitle(), last.Name if last is not None else None)


def _roll_back(self, snapshot) -> list:
    if snapshot is None or snapshot[0] not in ("fast", "bar"):
        return _ORIGINAL["_roll_back"](self, snapshot)
    title = snapshot[1]
    if self._model is None or self._model.GetTitle() != title:
        return []
    feats = list(self._iter_features())
    names = [f.Name for f in feats]
    if snapshot[0] == "bar":
        _, _, above, below, total = snapshot
        count = live_count(feats)
        if above not in names or count >= len(feats) or names[count] != below:
            return []  # the call moved the rollback bar: nothing can be told apart safely
        added = names[names.index(above) + 1:count]
        if len(added) != len(feats) - total:
            return []  # the call also removed or reordered features: leave the tree alone
    else:
        last = snapshot[2]
        if last not in names:
            return []  # the call removed the feature we anchored on: nothing safe to undo
        added = names[names.index(last) + 1:]
    if not added:
        return []
    keep = {f.Name for f in self._history()} - set(added)
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

    Nothing new to remove: no rebuild either (it cost 8-9 s per refused call on
    a 205-feature part).
    """
    model = self._require_model()
    extension = binding.wrap(model.Extension, self._mod.IModelDocExtension)
    removed = False
    for _pass in range(2):
        new = [f for f in self._history() if f.Name not in before]
        if not new:
            if not removed:
                return []
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
                removed = True
    if removed:
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
