"""What a building call did, and to which bodies (S-05, S-10).

Every building tool (a cut, a boss, a hole, a combine ...) runs through
`runner` here, inside the same guarded call as the tool itself:

- the result gains `change`: each body's volume before and after (bodies are
  matched across the call although SolidWorks renames a body after the last
  feature that touched it), bodies that appeared or went, the face count,
  SolidWorks' own rebuild errors and any new sketch that is not fully defined;
- `bodies=[...]` limits a cut or boss to the named bodies (Feature Scope), so a
  multibody part needs no tool body + Combine. The scope is written into the
  feature right after it is created; a body outside the scope that changed
  anyway fails the call;
- `expect={...}` states what the call must do; when it does not, the call
  fails and the guard removes what it added - the tree is as it was.

expect keys: volume 'decrease' | 'increase' | 'unchanged'; volume_mm3 [low,
high] (the total volume's change); bodies 'same' | '+1' | '-2' | 3 (count
after); changed_bodies [names] (exactly these change, by their names before
the call); no_errors true; fully_defined true.
"""

import contextvars
import math

from .. import binding
from ..errors import SolidWorksError
from . import bodies as bodies_mod
from . import geom, journal

SW_DOC_PART = 1
SW_FULLY_CONSTRAINED = 3  # swConstrainedStatus_e
VOLUME_TOL_MM3 = 1e-3

REQUEST = contextvars.ContextVar("rbr_scope_request", default=None)

# cuts and bosses that take a Feature Scope: these tools gain bodies=
SCOPED = frozenset({
    "cut_sketch", "extrude_sketch", "cut_profile", "cut_profile_on_face", "cut_profile_through_plane",
    "cut_slot", "cut_offset_pocket", "add_hole", "add_hole_on_face", "add_counterbore_hole", "add_hole_wizard",
    "add_boss_on_face", "add_extruded_profile_on_face", "add_extruded_profile_on_plane", "add_extruded_slot",
    "add_revolved_profile", "add_swept_profile", "cut_toward", "extrude_toward", "cut_offset_from_surface",
    "loft_cut", "sweep_cut", "sweep_boss",
})
# tools that build geometry: these gain expect= and the change report
BUILDER_PREFIXES = ("add_", "cut_", "extrude_", "sweep_", "loft_", "mirror_", "combine_", "split_")
BUILDERS_EXTRA = frozenset({"move_copy_body", "delete_bodies", "insert_part", "set_feature_scope"})
NOT_BUILDERS = frozenset({"add_configuration", "add_mate", "add_relation", "add_sketch", "add_plane",
                          "add_sketch3d", "split_line_project", "add_dimension"})

EXPECT_KEYS = ("volume", "volume_mm3", "bodies", "changed_bodies", "no_errors", "fully_defined")


def is_builder(name: str) -> bool:
    return (name in BUILDERS_EXTRA or name in SCOPED or name.startswith(BUILDER_PREFIXES)) and name not in NOT_BUILDERS


# --- pure parts (unit-tested) ----------------------------------------------------------------


def pair_bodies(before: dict, after: dict) -> list:
    """Match bodies across a call: [(name before | None, name after | None)].

    before / after: {name: {"volume_mm3", "centre_mm"}}. Same names pair first;
    the rest by nearest centre of mass (a cut renames its body, and moves the
    centre only a little); what is left over appeared or went."""
    pairs = [(name, name) for name in before if name in after]
    old = [n for n in before if n not in after]
    new = [n for n in after if n not in before]
    while old and new:
        distance, a, b = min((math.dist(before[a]["centre_mm"], after[b]["centre_mm"]), a, b)
                             for a in old for b in new)
        pairs.append((a, b))
        old.remove(a)
        new.remove(b)
    return pairs + [(a, None) for a in old] + [(None, b) for b in new]


def change_report(before: dict, after: dict) -> dict:
    """The `change` block from two snapshots; pure."""
    rows, added, removed = [], [], []
    for a, b in pair_bodies(before["bodies"], after["bodies"]):
        if a is None:
            added.append(b)
            rows.append({"body": b, "before_mm3": None, "after_mm3": round(after["bodies"][b]["volume_mm3"], 3),
                         "delta_mm3": round(after["bodies"][b]["volume_mm3"], 3)})
        elif b is None:
            removed.append(a)
            rows.append({"body": None, "was": a, "before_mm3": round(before["bodies"][a]["volume_mm3"], 3),
                         "after_mm3": None, "delta_mm3": round(-before["bodies"][a]["volume_mm3"], 3)})
        else:
            v0, v1 = before["bodies"][a]["volume_mm3"], after["bodies"][b]["volume_mm3"]
            row = {"body": b, "before_mm3": round(v0, 3), "after_mm3": round(v1, 3), "delta_mm3": round(v1 - v0, 3)}
            if a != b:
                row["was"] = a
            rows.append(row)
    total0 = sum(b["volume_mm3"] for b in before["bodies"].values())
    total1 = sum(b["volume_mm3"] for b in after["bodies"].values())
    faces0 = sum(b["faces"] for b in before["bodies"].values())
    faces1 = sum(b["faces"] for b in after["bodies"].values())
    return {"bodies": rows, "bodies_added": added, "bodies_removed": removed,
            "body_count": {"before": len(before["bodies"]), "after": len(after["bodies"])},
            "volume_mm3": {"before": round(total0, 3), "after": round(total1, 3), "delta": round(total1 - total0, 3)},
            "faces": {"before": faces0, "after": faces1, "added": max(0, faces1 - faces0),
                      "removed": max(0, faces0 - faces1)}}


def check_expect(expect: dict, change: dict) -> list:
    """The expectations a change does not meet, as readable sentences; pure."""
    if not isinstance(expect, dict):
        raise SolidWorksError(f"expect is a dict such as {{'volume': 'decrease', 'bodies': 'same'}} (got {expect!r}).")
    unknown = [k for k in expect if k not in EXPECT_KEYS]
    if unknown:
        raise SolidWorksError(f"Unknown expect key(s) {unknown}: use {', '.join(EXPECT_KEYS)}.")
    failed = []
    delta = change["volume_mm3"]["delta"]
    want = expect.get("volume")
    if want is not None:
        ok = {"decrease": delta < -VOLUME_TOL_MM3, "increase": delta > VOLUME_TOL_MM3,
              "unchanged": abs(delta) <= VOLUME_TOL_MM3}.get(str(want).lower())
        if ok is None:
            raise SolidWorksError(f"expect volume is 'decrease', 'increase' or 'unchanged' (got {want!r}).")
        if not ok:
            failed.append(f"volume was to {want}, it changed by {delta:+.3f} mm^3")
    if expect.get("volume_mm3") is not None:
        try:
            low, high = sorted(float(v) for v in expect["volume_mm3"])
        except (TypeError, ValueError):
            raise SolidWorksError("expect volume_mm3 is [low, high], the range the volume change must lie in, "
                                  f"e.g. [-2100, -1900] (got {expect['volume_mm3']!r}).") from None
        if not low - VOLUME_TOL_MM3 <= delta <= high + VOLUME_TOL_MM3:
            failed.append(f"the volume change was to lie in [{low:g}, {high:g}] mm^3, it is {delta:+.3f}")
    want = expect.get("bodies")
    if want is not None:
        count0, count1 = change["body_count"]["before"], change["body_count"]["after"]
        text = str(want).strip().lower()
        try:
            target = count0 if text == "same" else count0 + int(text) if text[:1] in "+-" else int(text)
        except ValueError:
            raise SolidWorksError("expect bodies is 'same', a count such as 2, or a change such as '+1' / '-1' "
                                  f"(got {want!r}).") from None
        if count1 != target:
            failed.append(f"{target} bodies were expected after the call, there are {count1} (before: {count0})")
    if expect.get("changed_bodies") is not None:
        named = expect["changed_bodies"]   # [] says: no body may change
        wanted = set() if isinstance(named, (list, tuple)) and not named else set(bodies_mod.as_names(named))
        changed = {row.get("was", row["body"]) for row in change["bodies"] if abs(row["delta_mm3"]) > VOLUME_TOL_MM3}
        if changed != wanted:
            failed.append(f"bodies {sorted(wanted)} were to change, {sorted(str(c) for c in changed) or 'none'} did")
    if expect.get("no_errors") and change.get("whats_wrong"):
        failed.append(f"SolidWorks reports rebuild errors: {change['whats_wrong']}")
    if expect.get("fully_defined") and change.get("new_under_defined_sketches"):
        failed.append(f"sketches left not fully defined: {change['new_under_defined_sketches']}")
    return failed


# --- SolidWorks side ----------------------------------------------------------------------------


def snapshot(session):
    """{bodies: {name: {volume_mm3, centre_mm, faces}}} of the current part, or None."""
    model = session._model
    try:
        if model is None or int(model.GetType()) != SW_DOC_PART:
            return None
        out = {}
        for body in session._part_bodies():
            props = body.GetMassProperties(1.0)  # centre xyz (m), volume (m^3), area ...
            name, k = body.Name, 2
            while name in out:  # two bodies of one name: keep both in the count
                name, k = f"{body.Name} ({k})", k + 1
            out[name] = {"volume_mm3": float(props[3]) * 1e9, "centre_mm": [float(c) * 1000.0 for c in props[0:3]],
                         "faces": int(body.GetFaceCount())}
        return {"bodies": out}
    except Exception:  # noqa: BLE001 - the report never breaks the tool
        return None


def _sketch_states(session, names) -> list:
    """'Sketch (under defined)' for the named features and the sketches they absorbed."""
    mod = session._mod
    part = binding.wrap(session._model, mod.IPartDoc)
    out, seen = [], set()
    for name in names:
        feat = binding.wrap(part.FeatureByName(name), mod.IFeature) if name else None
        if feat is None:
            continue
        for candidate in [feat, *session._sub_features(feat)]:
            try:
                if candidate.GetTypeName2() != "ProfileFeature" or candidate.Name in seen:
                    continue
                seen.add(candidate.Name)
                status = binding.wrap(candidate.GetSpecificFeature2(), mod.ISketch).GetConstrainedStatus()
                if status != SW_FULLY_CONSTRAINED:
                    out.append(candidate.Name)
            except Exception:  # noqa: BLE001
                continue
    return out


def _body_refs(session, names) -> list:
    """Persistent references of the named bodies, taken before the feature exists: once
    the cut has touched a body SolidWorks names it after the cut."""
    ext = binding.wrap(session._model.Extension, session._mod.IModelDocExtension)
    targets = bodies_mod._resolve(session, names)
    return [(t.Name, bytes(ext.GetPersistReference3(t)), [float(c) for c in t.GetMassProperties(1.0)[0:3]])
            for t in targets]


def _bodies_now(session, refs) -> list:
    """The bodies behind the references taken before the call, as they are now."""
    mod = session._mod
    ext = binding.wrap(session._model.Extension, mod.IModelDocExtension)
    present = bodies_mod._all_bodies(session)
    out = []
    for name, ref, centre in refs:
        body = None
        try:
            found = ext.GetObjectByPersistReference3(ref)
            found = found[0] if isinstance(found, tuple) else found
            body = binding.wrap(found, mod.IBody2)
            body.Name  # noqa: B018 - a dead reference raises here
        except Exception:  # noqa: BLE001
            body = None
        if body is None and present:  # by position: the body whose centre of mass is nearest
            body = min(present, key=lambda b: math.dist(centre, [float(c) for c in b.GetMassProperties(1.0)[0:3]]))
        if body is None:
            raise SolidWorksError(f"The body '{name}' is gone after the feature was built: it cannot be scoped.")
        out.append(body)
    return out


def _apply_scope(session, feature_name: str, refs) -> None:
    """Write the feature's scope: only the bodies behind `refs`."""
    model = session._require_model()
    mod = session._mod
    feat = binding.wrap(binding.wrap(model, mod.IPartDoc).FeatureByName(feature_name), mod.IFeature)
    if feat is None:
        raise SolidWorksError(f"The new feature '{feature_name}' is not in the tree: its scope cannot be set.")
    data = bodies_mod._scope_data(session, feat)
    if not data.AccessSelections(model, None):
        raise SolidWorksError(f"Cannot open '{feature_name}' to set its scope.")
    try:
        data.FeatureScope = True
        data.AutoSelect = False
        data.FeatureScopeBodies = bodies_mod._variant(_bodies_now(session, refs))
        ok = feat.ModifyDefinition(data, model, None)
    except Exception:
        data.ReleaseSelectionAccess()
        raise
    if not ok:
        data.ReleaseSelectionAccess()
        raise geom.feature_refusal(session, f"ModifyDefinition of '{feature_name}' with bodies {[r[0] for r in refs]}")
    model.ForceRebuild3(False)


def runner(session, fn, tool: str, bodies=None, expect=None):
    """`fn` with the change report, the body scope and the expectations around it."""
    def run(*args, **kwargs):
        if bodies is not None and tool not in SCOPED:
            raise SolidWorksError(f"'{tool}' takes no bodies=: only cuts, bosses and holes have a feature scope.")
        if expect is not None:
            check_expect(expect, {"volume_mm3": {"delta": 0.0}, "body_count": {"before": 0, "after": 0},
                                  "bodies": []})  # refuses a malformed expect before anything is built
        before = snapshot(session)
        refs = None
        if bodies is not None:
            if before is None:
                raise SolidWorksError("bodies= needs a part as the current document.")
            names = bodies_mod.as_names(bodies)
            refs = _body_refs(session, names)
        result = fn(*args, **kwargs)
        if not isinstance(result, dict) or not result.get("ok", True) or before is None:
            return result
        feature = result.get("feature")
        if refs is not None:
            if not feature:
                raise SolidWorksError(f"'{tool}' names no feature in its result: its scope cannot be set.")
            _apply_scope(session, feature, refs)
        after = snapshot(session)
        if after is None:
            return result
        change = change_report(before, after)
        change["whats_wrong"] = geom.whats_wrong(session)
        change["new_under_defined_sketches"] = _sketch_states(session, [feature, result.get("sketch")])
        if refs is not None:
            wanted = {r[0] for r in refs}
            touched = {row.get("was", row["body"]) for row in change["bodies"]
                       if abs(row["delta_mm3"]) > VOLUME_TOL_MM3 and row["before_mm3"] is not None}
            outside = sorted(str(t) for t in touched - wanted)
            if outside:
                raise SolidWorksError(f"'{feature}' was scoped to {sorted(wanted)}, yet {outside} changed too. "
                                      "Rolled back.")
            change["scope"] = sorted(wanted)
            try:
                result["mass_properties"] = session.get_mass_properties()["mass_properties"]
            except Exception:  # noqa: BLE001
                pass
        if expect is not None:
            failed = check_expect(expect, change)
            if failed:
                raise SolidWorksError(f"expect not met: {'; '.join(failed)}. Nothing was kept: the features "
                                      f"this call added are removed. change: {change['bodies']}")
            change["expect"] = "met"
        result["change"] = change
        return result

    run.__name__ = journal.tool_name(fn)
    run.__wrapped__ = fn
    return run
