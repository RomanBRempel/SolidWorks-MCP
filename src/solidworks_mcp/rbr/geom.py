"""Body geometry helpers born from work on real parts.

- mirror_body: InsertMirrorFeature2 with Merge=True can weld a broken result
  (a mirrored half lost most of its volume in the weld): mirror WITHOUT merging, then join
  through the same InsertCombineFeature path combine_bodies uses, and check the
  volume roughly doubled.
- check_containment: how much of each body of one part sits inside another
  part's body, via IBody2::Operations2 intersections on copies. Both parts are
  read in their own coordinates, so the master-model parts being compared must
  share their origin (they do when both were built from one skeleton).
- whats_wrong: the rebuild errors SolidWorks knows but a bare `None` from an
  Insert* call never shows.
"""

import os

from .. import binding
from ..errors import SolidWorksError
from . import guard

from ..constants import SW_FEATURE_SCOPE_ALL_BODIES

SWBODY_INTERSECT = 15901  # swBodyOperationType_e, as session._BODY_OPERATIONS
MARK_MIRROR_PLANE = 2
MARK_MIRROR_BODY = 256


def _doc_by_title(session, title: str | None):
    """An open document by its title (with or without extension); None = the current one."""
    if title is None:
        return session._require_model()
    for doc in guard._documents(session):
        try:
            full = doc.GetTitle()
        except Exception:  # noqa: BLE001
            continue
        if title in (full, os.path.splitext(full)[0]):
            return doc
    raise SolidWorksError(f"No open document '{title}' (list_documents shows them).")


def _solid_bodies(session, doc) -> list:
    part = binding.wrap(doc, session._mod.IPartDoc)
    try:
        raw = part.GetBodies2(0, True) or ()
    except Exception as exc:  # noqa: BLE001
        raise SolidWorksError(f"'{doc.GetTitle()}' is not a part with solid bodies: {exc}")
    return [binding.wrap(b, session._mod.IBody2) for b in raw]


def _volume_mm3(body) -> float:
    return float(body.GetMassProperties(1.0)[3]) * 1e9


def _temp_copy(body):
    for name, args in (("Copy2", (False,)), ("Copy", ())):
        method = getattr(body, name, None)
        if method is None:
            continue
        try:
            copy = method(*args)
        except Exception:  # noqa: BLE001
            continue
        if copy is not None:
            return copy
    raise SolidWorksError("This SolidWorks exposes neither IBody2::Copy2 nor Copy.")


def _pieces(result) -> list:
    """Operations2 returns the result bodies; with an out-parameter it is (bodies, err)."""
    if isinstance(result, tuple):
        bodies = result[0]
    else:
        bodies = result
    if bodies is None:
        return []
    try:
        return list(bodies)
    except TypeError:
        return [bodies]


def whats_wrong(session) -> list:
    """SolidWorks' own rebuild errors: [{feature, error}], newest knowledge it has."""
    model = session._require_model()
    ext = binding.wrap(model.Extension, session._mod.IModelDocExtension)
    try:
        result = ext.GetWhatsWrong()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(result, tuple):
        return []
    arrays = [list(part) if part is not None else [] for part in result if not isinstance(part, bool)]
    if not arrays or not arrays[0]:
        return []
    features, *rest = arrays
    out = []
    for i, feat in enumerate(features):
        entry = {"feature": None}
        try:
            entry["feature"] = binding.wrap(feat, session._mod.IFeature).Name
        except Exception:  # noqa: BLE001
            pass
        for j, extra in enumerate(rest):
            if i < len(extra):
                entry["error" if j == 0 else "warning"] = extra[i]
        out.append(entry)
    return out


def feature_refusal(session, call: str) -> SolidWorksError:
    """A readable error for an Insert* that answered None, carrying what SolidWorks knows."""
    wrong = whats_wrong(session)
    last = None
    try:
        feat = session._require_model().FeatureByPositionReverse(0)
        if feat is not None:
            feat = binding.wrap(feat, session._mod.IFeature)
            code = feat.GetErrorCode2()
            code = code[0] if isinstance(code, tuple) else code
            last = {"feature": feat.Name, "error_code": int(code)}
    except Exception:  # noqa: BLE001
        pass
    detail = f" Rebuild errors: {wrong}." if wrong else ""
    tail = f" Last feature: {last}." if last else ""
    return SolidWorksError(f"SolidWorks refused {call} (returned None).{detail}{tail}")


def resolve_body(session, model, body):
    """One of the part's solid bodies, by name or as the IBody2 (or raw dispatch)
    itself. IBody2 is not hashable and two wrappers of one body do not compare
    equal, so bodies are matched by NAME, never used as dict keys or with `in`."""
    bodies = _solid_bodies(session, model)
    if isinstance(body, str):
        found = [b for b in bodies if b.Name == body]
        if not found:
            raise SolidWorksError(f"No body '{body}' (there are: {', '.join(b.Name for b in bodies) or 'none'}).")
        return found[0]
    wrapped = binding.wrap(body, session._mod.IBody2)
    try:
        name = wrapped.Name
    except Exception as exc:  # noqa: BLE001
        raise SolidWorksError(f"body must be a body name or an IBody2 (got {body!r}): {exc}") from None
    if not any(b.Name == name for b in bodies):
        raise SolidWorksError(f"The body '{name}' is not a solid body of the current part "
                              f"(there are: {', '.join(b.Name for b in bodies) or 'none'}).")
    return wrapped


def mirror_body(session, body, plane: str, merge: bool = True, name: str = "MirrorBody") -> dict:
    """Mirror one solid body (its name or the IBody2) about a plane ('front' /
    'top' / 'right' or a plane of the part); merge joins the copy back through
    the combine_bodies path (InsertMirrorFeature2's own Merge gave broken geometry)."""
    model = session._require_model()
    session._require_part()
    target = resolve_body(session, model, body)
    body_name = target.Name
    plane_feat, plane_label = session._named_plane(plane)
    before = {b.Name for b in _solid_bodies(session, model)}
    volume_before = _volume_mm3(target)
    model.ClearSelection2(True)
    if not plane_feat.Select2(True, MARK_MIRROR_PLANE):
        raise SolidWorksError(f"Cannot select the {plane_label}.")
    # IBody2::Select2 takes an ISelectData (an integer mark raises "Python
    # instance can not be converted to a COM object"): build one, as add_mirror does.
    select_data = binding.wrap(binding.wrap(model.SelectionManager, session._mod.ISelectionMgr)
                               .CreateSelectData(), session._mod.ISelectData)
    select_data.Mark = MARK_MIRROR_BODY
    if not target.Select2(True, select_data):
        raise SolidWorksError(f"Cannot select the body '{body_name}'.")
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    # BMirrorBody=True, BGeometryPattern=False, BMerge=False (see module doc), BKnit=False;
    # SW 2025's InsertMirrorFeature2 requires the ScopeOptions argument (type mismatch without it)
    feat = fm.InsertMirrorFeature2(True, False, False, False, SW_FEATURE_SCOPE_ALL_BODIES)
    if feat is None:
        raise feature_refusal(session, f"InsertMirrorFeature2 of '{body_name}' about '{plane}'")
    feat = binding.wrap(feat, session._mod.IFeature)
    new_names = [b.Name for b in _solid_bodies(session, model) if b.Name not in before]
    out = {"ok": True, "feature": feat.Name, "mirrored": body_name, "plane": plane,
           "new_bodies": new_names, "volume_before_mm3": round(volume_before, 3)}
    if merge:
        if not new_names:
            raise SolidWorksError("The mirror added no body to merge; the tree has the mirror feature.")
        combined = session.combine_bodies("add", body_name, new_names)
        out["merged"] = True
        out["mass_properties"] = combined.get("mass_properties")
        volume_after = (combined.get("mass_properties") or {}).get("volume_mm3")
        if volume_after is not None and volume_after < 1.5 * volume_before:
            out["warning"] = (f"After the merge the volume is {volume_after:.1f} mm^3 against "
                              f"{volume_before:.1f} before the mirror: the halves may overlap or "
                              "the weld lost material; check list_bodies and the shape.")
    else:
        out["merged"] = False
    return out


def check_containment(session, envelope: str, keepout: str | None = None) -> dict:
    """How much of each solid body of `keepout` (default: the current part) lies
    inside the bodies of `envelope`: per body the volume, % inside and the mm^3
    outside. Runs on copies via IBody2::Operations2, so the parts are untouched."""
    keep_doc = _doc_by_title(session, keepout)
    env_doc = _doc_by_title(session, envelope)
    env_bodies = _solid_bodies(session, env_doc)
    if not env_bodies:
        raise SolidWorksError(f"'{env_doc.GetTitle()}' has no solid body to contain anything.")
    rows = []
    for body in _solid_bodies(session, keep_doc):
        volume = _volume_mm3(body)
        inside = 0.0
        for env in env_bodies:
            body_copy = binding.wrap(_temp_copy(body), session._mod.IBody2)
            env_copy = _temp_copy(env)
            pieces = _pieces(body_copy.Operations2(SWBODY_INTERSECT, env_copy))
            for piece in pieces:
                try:
                    inside += _volume_mm3(binding.wrap(piece, session._mod.IBody2))
                except Exception:  # noqa: BLE001 - a sliver piece without mass stays uncounted
                    continue
        inside = min(inside, volume)
        rows.append({"body": body.Name, "volume_mm3": round(volume, 3),
                     "inside_pct": round(100.0 * inside / volume, 2) if volume else None,
                     "outside_mm3": round(volume - inside, 3)})
    worst = min((r["inside_pct"] for r in rows if r["inside_pct"] is not None), default=None)
    return {"ok": True, "keepout": keep_doc.GetTitle(), "envelope": env_doc.GetTitle(),
            "bodies": rows, "worst_inside_pct": worst,
            "note": "Both parts are read in their own coordinates: the comparison only means "
                    "something when they share their origin (master-model parts do)."}
