"""Edit context: work in the middle of a big tree without rebuilding all of it (S-01),
and name what a call created, not "the last feature in the tree" (S-02).

edit_context(freeze_to, rollback_before) puts SolidWorks' freeze bar after one
feature and the rollback bar before another: a rebuild then covers only the
features between the two bars. Measured on a 205-feature part (SW 2025 SP3):
ForceRebuild3 8.6 s -> 4.5 s frozen -> 2.3 s frozen and rolled back.

The rolled-back state broke two assumptions of the code around it:

- the upstream snapshot taken before every modifying call reads every feature
  name when the rollback bar is not at the end, and it ran BEFORE
  CommandInProgress was set: 20.7 s per call on that part (16 s of it in
  GetFeatures, 10 ms per returned object). guard.py now sets the flag first.
- "the feature just created" was looked up as the last one in the tree (the
  last plane, the last axis, the last history entry). Below the rollback bar
  the tree goes on, so the name went to a rolled-back feature: add_plane
  renamed a plane further down, twice, and the new one stayed "PlaneN". The
  look-ups here stop at the rollback bar.

Values from swconst.tlb of SOLIDWORKS 2025 (rev 33).
"""

import functools
import time

from .. import binding
from ..errors import SolidWorksError
from ..session import SolidWorksSession
from .fastpath import live_count

SW_DOC_PART = 1
SW_USER_ENABLE_FREEZE_BAR = 461       # swUserPreferenceToggle_e
SW_ROLLBACK_TO_END = 1                # swMoveRollbackBarTo_e
SW_ROLLBACK_BEFORE_FEATURE = 3
SW_FREEZE_AFTER_FEATURE = 3           # swMoveFreezeBarTo_e
SW_FREEZE_TO_TOP = 4

_ORIGINAL = {}
_seen = {}                 # title -> last state read, for server_health (no SolidWorks needed)
_toggle_enabled_here = False


# --- the rollback bar ---------------------------------------------------------------------


def live(session, feats) -> list:
    """`feats` without what is rolled back."""
    feats = list(feats)
    return feats[:live_count(feats)]


# --- S-02: "the newest feature" stops at the rollback bar -------------------------------------


def _last_ref_plane(self):
    """The most recently created reference plane: the last one above the rollback bar."""
    planes = live(self, self._ref_planes())
    return planes[-1] if planes else None


def _last_feature_name(self) -> str:
    """Upstream's default pattern seed (the most recent body-modifying feature),
    read in one call and never from below the rollback bar."""
    seed = None
    for feat in live(self, self._iter_features()):
        try:
            tname = feat.GetTypeName2() or ""
        except Exception:  # noqa: BLE001
            tname = ""
        if tname and not tname.endswith("Folder") and tname not in self._NON_SEED_TYPES:
            seed = feat
    if seed is None:
        raise SolidWorksError("No patternable feature found; pass feature_name explicitly.")
    return seed.Name


def _history(self) -> list:
    """Upstream's history. Inside a tool that takes `_history()[-1]` for the
    feature it just made (see _NEWEST_IS_LAST) the rolled-back features are
    moved to the front, so the last entry is the newest one above the bar."""
    history = _without_freeze_bar(_ORIGINAL["_history"](self))
    if getattr(self, "_rbr_newest_last", 0):
        count = live_count(history)
        if count < len(history):
            return history[count:] + history[:count]
    return history


FREEZE_BAR_TYPE = "LockFeature"


def _without_freeze_bar(history: list) -> list:
    """The history without the freeze bar, which SolidWorks lists as a feature
    of its own ('LockFeature', named 'Freeze***' in the UI language) right
    after the last frozen feature: it is nothing a person modelled, cannot be
    selected or deleted, and looked like a feature the call had added."""
    if not history:
        return history
    try:
        if not history[0].IsFrozen():
            return history
        lo, hi = 0, len(history)       # the first feature that is not frozen
        while lo < hi:
            mid = (lo + hi) // 2
            if history[mid].IsFrozen():
                lo = mid + 1
            else:
                hi = mid
        if lo < len(history) and history[lo].GetTypeName2() == FREEZE_BAR_TYPE:
            return history[:lo] + history[lo + 1:]
        return [f for f in history if f.GetTypeName2() != FREEZE_BAR_TYPE]
    except Exception:  # noqa: BLE001 - a build without IsFrozen: nothing is frozen there
        return history


# upstream methods that read the feature they just created as "the last one"
_NEWEST_IS_LAST = ("_model_axis", "cut_offset_pocket")


def _newest_last(original):
    def wrapper(self, *args, **kwargs):
        self._rbr_newest_last = getattr(self, "_rbr_newest_last", 0) + 1
        try:
            return original(self, *args, **kwargs)
        finally:
            self._rbr_newest_last -= 1

    functools.update_wrapper(wrapper, original)
    return wrapper


# --- state --------------------------------------------------------------------------------


def _fm(session, model):
    return binding.wrap(model.FeatureManager, session._mod.IFeatureManager)


def state(session, model=None) -> dict | None:
    """Where the freeze and rollback bars of the current part are, or None when
    it is not a part. Cheap: two calls when nothing is frozen or rolled back."""
    model = model if model is not None else session._model
    if model is None:
        return None
    try:
        if int(model.GetType()) != SW_DOC_PART:
            return None
        title = model.GetTitle()
    except Exception:  # noqa: BLE001
        return None
    out = {"frozen_to": None, "rolled_back_before": None, "rolled_back": 0}
    try:
        frozen = binding.wrap(_fm(session, model).GetFreezeLocation(), session._mod.IFeature)
        out["frozen_to"] = frozen.Name if frozen is not None else None
    except Exception:  # noqa: BLE001 - older builds: no freeze bar
        pass
    try:
        last = binding.wrap(model.FeatureByPositionReverse(0), session._mod.IFeature)
        if last is not None and last.IsRolledBack():
            feats = list(session._iter_features())
            count = live_count(feats)
            out["rolled_back"] = len(feats) - count
            out["rolled_back_before"] = feats[count].Name if count < len(feats) else None
    except Exception:  # noqa: BLE001
        pass
    out["active"] = bool(out["frozen_to"] or out["rolled_back"])
    _seen[title] = {**out, "seen": time.strftime("%H:%M:%S")}
    if not out["active"]:
        _seen.pop(title, None)
    return out


def seen() -> dict:
    """Edit contexts as last read, per document title (for server_health)."""
    return {title: dict(entry) for title, entry in _seen.items()}


def forget(title: str | None) -> None:
    _seen.pop(title, None)


# --- the tool -------------------------------------------------------------------------------


def _index_of(names: list, name: str, what: str) -> int:
    if name not in names:
        close = [n for n in names if n.lower() == str(name).lower()]
        hint = f" Did you mean '{close[0]}'?" if close else ""
        raise SolidWorksError(f"No feature '{name}' in the part's history to {what}.{hint} "
                              "list_features shows the names.")
    return names.index(name)


def _enable_freeze_bar(session, fm) -> None:
    """Switch on the 'Enable Freeze bar' option - with the feature tree enabled.

    Inside a tool call the tree is switched off (upstream's quiet UI); the
    option switched on then gives no freeze bar and EditFreeze answers 1.
    Switched on with EnableFeatureTree = True it works, also with the tree
    off again right after (verified in every order, SW 2025 SP3)."""
    global _toggle_enabled_here
    sw = session._sw
    if sw.GetUserPreferenceToggle(SW_USER_ENABLE_FREEZE_BAR):
        return
    tree_on = bool(fm.EnableFeatureTree)
    if not tree_on:
        fm.EnableFeatureTree = True
    try:
        sw.SetUserPreferenceToggle(SW_USER_ENABLE_FREEZE_BAR, True)
    finally:
        if not tree_on:
            fm.EnableFeatureTree = False
    _toggle_enabled_here = True


def _freeze(session, fm, location: int, name: str) -> None:
    """EditFreeze, once more with the feature tree enabled when it answers an error."""
    if fm.EditFreeze(location, name, True) == 0:
        return
    tree_on = bool(fm.EnableFeatureTree)
    if not tree_on:
        fm.EnableFeatureTree = True
    try:
        code = fm.EditFreeze(location, name, True)
    finally:
        if not tree_on:
            fm.EnableFeatureTree = False
    if code != 0:
        raise SolidWorksError(
            f"SolidWorks refused to move the freeze bar{' after ' + repr(name) if name else ''} (code {code}): "
            "name a feature of the tree's top level, not a sketch absorbed by another feature.")


def edit_context(session, freeze_to: str | None = None, rollback_before: str | None = None,
                 end: bool = False) -> dict:
    """Limit rebuilds to a stretch of the tree, or end that (see the module doc)."""
    global _toggle_enabled_here
    model = session._require_model()
    session._require_part()
    if end and (freeze_to or rollback_before):
        raise SolidWorksError("Give end=True alone, or freeze_to / rollback_before.")
    fm = _fm(session, model)
    before = state(session, model) or {}
    if not end and not freeze_to and not rollback_before:
        return {"ok": True, "document": model.GetTitle(), **before}

    t0 = time.perf_counter()
    if end:
        if before.get("rolled_back"):
            if not fm.EditRollback(SW_ROLLBACK_TO_END, ""):
                raise SolidWorksError("SolidWorks refused to move the rollback bar to the end.")
        if before.get("frozen_to"):
            _freeze(session, fm, SW_FREEZE_TO_TOP, "")
        if _toggle_enabled_here:
            try:
                session._sw.SetUserPreferenceToggle(SW_USER_ENABLE_FREEZE_BAR, False)
            except Exception:  # noqa: BLE001 - an option left on harms nothing
                pass
            _toggle_enabled_here = False
        now = state(session, model) or {}
        if now.get("rolled_back") or now.get("frozen_to"):
            raise SolidWorksError(f"The tree is still {'rolled back' if now.get('rolled_back') else 'frozen'} "
                                  f"after ending the edit context: {now}.")
        from . import geom
        return {"ok": True, "document": model.GetTitle(), "ended": True, "was": before, **now,
                "failing_features": session._failing_features(), "whats_wrong": geom.whats_wrong(session),
                "elapsed_s": round(time.perf_counter() - t0, 2)}

    names = [f.Name for f in session._history()]
    freeze_at = _index_of(names, freeze_to, "freeze up to") if freeze_to else None
    roll_at = _index_of(names, rollback_before, "roll back before") if rollback_before else None
    bar_now = names.index(before["rolled_back_before"]) if before.get("rolled_back_before") in names else len(names)
    frozen_now = names.index(before["frozen_to"]) if before.get("frozen_to") in names else -1
    bar_then = roll_at if roll_at is not None else bar_now
    frozen_then = freeze_at if freeze_at is not None else frozen_now
    if frozen_then >= bar_then:
        raise SolidWorksError(
            f"The freeze bar (after '{names[frozen_then]}') must stay above the rollback bar "
            f"(before '{names[bar_then]}'): freeze_to has to come earlier in the tree than rollback_before."
            if bar_then < len(names) else f"'{names[frozen_then]}' is the last feature: nothing would be left to edit.")

    def freeze():
        _enable_freeze_bar(session, fm)
        _freeze(session, fm, SW_FREEZE_AFTER_FEATURE, freeze_to)

    def roll():
        if not fm.EditRollback(SW_ROLLBACK_BEFORE_FEATURE, rollback_before):
            raise SolidWorksError(f"SolidWorks refused to roll back before '{rollback_before}'.")

    # the freeze bar cannot pass the rollback bar: whichever move keeps them apart goes first
    steps = []
    if freeze_at is not None:
        steps.append(freeze)
    if roll_at is not None:
        steps.insert(0 if (freeze_at is not None and freeze_at >= bar_now) else len(steps), roll)
    try:
        for step in steps:
            step()
        now = state(session, model) or {}
        if freeze_to and now.get("frozen_to") != freeze_to:
            raise SolidWorksError(
                f"SolidWorks did not freeze up to '{freeze_to}' (the freeze bar is at {now.get('frozen_to')!r}): "
                "name a feature of the tree's top level, not a sketch absorbed by another feature.")
        if rollback_before and now.get("rolled_back_before") != rollback_before:
            raise SolidWorksError(f"SolidWorks did not roll back before '{rollback_before}' (the bar is before "
                                  f"{now.get('rolled_back_before')!r}).")
    except Exception:
        if not before.get("active"):  # a half-entered context is worse than none
            try:
                fm.EditRollback(SW_ROLLBACK_TO_END, "")
                fm.EditFreeze(SW_FREEZE_TO_TOP, "", True)
                state(session, model)
            except Exception:  # noqa: BLE001 - the original error is what matters
                pass
        raise
    live_from = names.index(now["frozen_to"]) + 1 if now.get("frozen_to") in names else 0
    live_to = names.index(now["rolled_back_before"]) if now.get("rolled_back_before") in names else len(names)
    return {"ok": True, "document": model.GetTitle(), **now,
            "frozen": live_from, "editable": live_to - live_from, "features": len(names),
            "editable_from": names[live_from] if live_from < live_to else None,
            "editable_to": names[live_to - 1] if live_from < live_to else None,
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "note": "Rebuilds now cover only the editable features; mass properties and the bounding box "
                    "describe the rolled-back part. save_part is refused until edit_context(end=True)."}


# --- patches --------------------------------------------------------------------------------


def _get_status(self) -> dict:
    result = _ORIGINAL["get_status"](self)
    try:
        context = state(self)
    except Exception:  # noqa: BLE001 - the status never fails on this
        context = None
    if isinstance(result, dict) and context and context.get("active"):
        result["edit_context"] = context
    return result


def _save_part(self, path: str) -> dict:
    context = state(self)
    if context and context.get("rolled_back"):
        raise SolidWorksError(
            f"{context['rolled_back']} feature(s) are rolled back (from '{context['rolled_back_before']}' on): "
            "saving now would store the part in that state. Call edit_context(end=True) first.")
    result = _ORIGINAL["save_part"](self, path)
    if isinstance(result, dict) and context and context.get("frozen_to"):
        result["edit_context"] = context
        result["warning"] = (f"Saved with the freeze bar after '{context['frozen_to']}': the features above it "
                             "do not rebuild until edit_context(end=True).")
    return result


def apply() -> None:
    if _ORIGINAL:
        return
    for name, fn in (("_last_ref_plane", _last_ref_plane), ("_last_feature_name", _last_feature_name),
                     ("_history", _history), ("get_status", _get_status), ("save_part", _save_part)):
        _ORIGINAL[name] = getattr(SolidWorksSession, name)
        functools.update_wrapper(fn, _ORIGINAL[name])
        setattr(SolidWorksSession, name, fn)
    for name in _NEWEST_IS_LAST:
        _ORIGINAL[name] = getattr(SolidWorksSession, name)
        setattr(SolidWorksSession, name, _newest_last(_ORIGINAL[name]))


def unapply() -> None:
    for name, fn in _ORIGINAL.items():
        setattr(SolidWorksSession, name, fn)
    _ORIGINAL.clear()
