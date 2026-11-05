"""Solid bodies of a multibody part: properties, rename, hide/show, delete/keep,
move/copy, feature scope, save bodies to a part of their own and insert a part
as bodies (1.8.0).

Verified on SOLIDWORKS 2025 SP3 before writing:
- IBody2.Name is writable and the new name survives a rebuild.
- IFeatureManager.InsertMoveCopyBody2 moves or copies the bodies selected with
  mark 1 (translation in m, rotation through a point in radians); copies are
  named '<feature>[n]', a moved body takes the feature's name. Measured: the
  angles named RotAngleX/Y/Z turn about Z, Y, X, and with a rotation the
  translation is ignored - so a turn and a shift become two features.
- InsertDeleteBody2(keep) deletes the selected bodies, or keeps only them.
- IBody2.HideBody(True/False) hides and shows; IBody2.Visible reads it back.
- IExtrudeFeatureData2: AccessSelections, FeatureScope=True, AutoSelect=False,
  FeatureScopeBodies=[bodies], ModifyDefinition limits an existing cut or boss
  to those bodies (a slot through two bodies, scoped to one, restored the other).
- IMassProperty.AddBodies([bodies]) gives the mass and centre of mass of just
  those bodies; its Volume and SurfaceArea read 0 then, so those come from
  IBody2.GetMassProperties.
- IPartDoc.SaveToFile3 with the bodies selected writes a new part (the source
  must be saved, its path is the reference) and opens it.
  IFeatureManager.CreateSaveBodyFeature returned None in every form tried.
- IPartDoc.InsertPart3(path, 1 = solid bodies, config) inserts a part as a
  'Stock' feature; two inserts of one file give two bodies of the same name,
  so the new bodies are renamed apart.
- A material per body (IBody2.SetMaterialProperty) answers NoError and changes
  nothing: neither the name read back nor the mass. Bodies here carry the
  part's material; body_properties reports what the body really has.
"""

import math
import os

import pythoncom
import win32com.client

from .. import binding
from ..errors import SolidWorksError
from . import geom

MARK_BODY = 1          # InsertMoveCopyBody2 / InsertDeleteBody2 read bodies with mark 1 or 0
INSERT_SOLIDS = 1      # swInsertPartImportSolids
INSERT_MATERIAL = 524288  # swInsertPartImportMaterial
_AXES = {"x": 0, "y": 1, "z": 2}


# --- pure helpers (unit-tested) ------------------------------------------------------


def rotation_args(rotate) -> tuple:
    """(RotPointX, Y, Z [m], RotAngleX, Y, Z [rad]) for InsertMoveCopyBody2.

    rotate: None, or {"axis": "x"|"y"|"z", "angle_deg": a, "about_mm": [x, y, z]}
    (about_mm defaults to the origin). One axis per call, right-hand rule."""
    if not rotate:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if not isinstance(rotate, dict):
        raise SolidWorksError("rotate must be {axis: 'x'|'y'|'z', angle_deg: a, about_mm: [x, y, z]}.")
    axis = str(rotate.get("axis", "")).lower()
    if axis not in _AXES:
        raise SolidWorksError(f"rotate.axis must be 'x', 'y' or 'z' (got {rotate.get('axis')!r}).")
    angle = float(rotate.get("angle_deg", 0.0))
    if angle == 0.0:
        raise SolidWorksError("rotate.angle_deg must not be 0.")
    about = rotate.get("about_mm") or [0.0, 0.0, 0.0]
    if len(about) != 3:
        raise SolidWorksError(f"rotate.about_mm must be [x, y, z] (got {about}).")
    # The three angles are named RotAngleX/Y/Z but turn about Z, Y and X in that
    # order (measured on SW 2025: a quarter turn in the first moved (0,10,-55) to (-10,0,-55)).
    angles = [0.0, 0.0, 0.0]
    angles[{"z": 0, "y": 1, "x": 2}[axis]] = math.radians(angle)
    return tuple(float(c) / 1000.0 for c in about) + tuple(angles)


def moved_point(point, translate_mm, rotate) -> list:
    """Where InsertMoveCopyBody2 takes a point (mm): rotated about rotate's axis
    through about_mm, then translated."""
    p = [float(c) for c in point]
    if rotate:
        axis = _AXES[str(rotate["axis"]).lower()]
        about = [float(c) for c in (rotate.get("about_mm") or [0.0, 0.0, 0.0])]
        a = math.radians(float(rotate["angle_deg"]))
        i, j = [k for k in range(3) if k != axis]
        if axis == 1:  # right-hand rule about Y turns Z towards X
            i, j = j, i
        u, v = p[i] - about[i], p[j] - about[j]
        p[i] = about[i] + u * math.cos(a) - v * math.sin(a)
        p[j] = about[j] + u * math.sin(a) + v * math.cos(a)
    move = translate_mm or [0.0, 0.0, 0.0]
    return [p[k] + float(move[k]) for k in range(3)]


def match_names(expected: dict, found: dict, tol_mm: float = 0.01) -> dict:
    """{new name: old name} pairing bodies whose centre landed where expected;
    expected {old: [x, y, z]}, found {new: [x, y, z]}. Ambiguous ones stay out."""
    out = {}
    for old, target in expected.items():
        near = [new for new, p in found.items()
                if max(abs(a - b) for a, b in zip(p, target)) <= tol_mm and new not in out]
        if len(near) == 1:
            out[near[0]] = old
    return out


def unique_name(wanted: str, taken) -> str:
    """wanted, or wanted_2, wanted_3 ... - the first not in taken."""
    taken = set(taken)
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}_{n}" in taken:
        n += 1
    return f"{wanted}_{n}"


def as_names(bodies) -> list:
    """A body name or a list of them -> list of names; empty refused."""
    if isinstance(bodies, str):
        bodies = [bodies]
    names = [str(b) for b in (bodies or [])]
    if not names:
        raise SolidWorksError("Name at least one body (list_bodies shows them).")
    return names


# --- COM helpers -----------------------------------------------------------------------


def _all_bodies(session) -> list:
    """Every solid body of the current part, hidden ones too."""
    session._require_part()
    return [binding.wrap(b, session._mod.IBody2) for b in (session._part_bodies() or [])]


def _resolve(session, names) -> list:
    bodies = _all_bodies(session)
    out = []
    for name in names:
        found = [b for b in bodies if b.Name == name]
        if not found:
            raise SolidWorksError(f"No body '{name}' (there are: {', '.join(b.Name for b in bodies) or 'none'}).")
        if len(found) > 1:
            raise SolidWorksError(f"{len(found)} bodies are named '{name}': rename_body one of them first.")
        out.append(found[0])
    return out


def _variant(bodies):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                                   [getattr(b, "_oleobj_", b) for b in bodies])


def _select(session, model, bodies, mark: int = MARK_BODY) -> None:
    model.ClearSelection2(True)
    sm = binding.wrap(model.SelectionManager, session._mod.ISelectionMgr)
    for body in bodies:
        data = binding.wrap(sm.CreateSelectData(), session._mod.ISelectData)
        data.Mark = mark
        if not body.Select2(True, data):
            raise SolidWorksError(f"Cannot select the body '{body.Name}'.")


def _volume_mm3(body) -> float:
    return float(body.GetMassProperties(1.0)[3]) * 1e9


def _names(session) -> list:
    return [b.Name for b in _all_bodies(session)]


def _summary(session) -> list:
    return [{"name": b.Name, "volume_mm3": round(_volume_mm3(b), 3), "visible": bool(b.Visible)}
            for b in _all_bodies(session)]


def _new_feature_result(session, feature, name: str | None, **extra) -> dict:
    feature = binding.wrap(feature, session._mod.IFeature)
    result = session._finish_feature(feature, name or feature.Name, **extra)
    result["bodies"] = _summary(session)
    return result


# --- properties -------------------------------------------------------------------------


def body_properties(session, bodies=None) -> dict:
    """Per solid body: volume, area, mass and centre of mass (by the part's material),
    box, face count, visibility and material."""
    model = session._require_model()
    targets = _all_bodies(session) if not bodies else _resolve(session, as_names(bodies))
    ext = binding.wrap(model.Extension, session._mod.IModelDocExtension)
    part = binding.wrap(model, session._mod.IPartDoc)
    try:
        cfg = model.ConfigurationManager.ActiveConfiguration.Name
    except Exception:  # noqa: BLE001
        cfg = ""
    part_material = part.GetMaterialPropertyName2(cfg)
    part_material = part_material[0] if isinstance(part_material, (list, tuple)) else part_material
    rows = []
    for body in targets:
        mp_body = body.GetMassProperties(1.0)  # density 1: [cx, cy, cz, volume, area, mass, ...] in m
        mass = binding.wrap(ext.CreateMassProperty(), session._mod.IMassProperty)
        mass.UseSystemUnits = True
        mass_kg = com = None
        if mass.AddBodies(_variant([body])):
            mass_kg = float(mass.Mass)
            com = [round(float(c) * 1000.0, 4) for c in mass.CenterOfMass]
        material = None
        try:
            own = body.GetMaterialPropertyName(cfg)
            own = own[0] if isinstance(own, (list, tuple)) else own
            material = own or None
        except Exception:  # noqa: BLE001
            pass
        volume = float(mp_body[3]) * 1e9
        rows.append({
            "name": body.Name,
            "visible": bool(body.Visible),
            "volume_mm3": round(volume, 3),
            "surface_area_mm2": round(float(mp_body[4]) * 1e6, 3),
            "mass_g": round(mass_kg * 1000.0, 3) if mass_kg is not None else None,
            "density_kg_m3": round(mass_kg / (volume * 1e-9), 2) if mass_kg and volume else None,
            "center_of_mass_mm": com or [round(float(c) * 1000.0, 4) for c in mp_body[:3]],
            "bounding_box_mm": session._body_box(body),
            "faces": int(body.GetFaceCount()),
            "material": material or part_material or None,
            "material_from": "body" if material else "part",
        })
    total_mass = sum(r["mass_g"] or 0.0 for r in rows)
    return {"ok": True, "count": len(rows), "bodies": rows,
            "total_volume_mm3": round(sum(r["volume_mm3"] for r in rows), 3),
            "total_mass_g": round(total_mass, 3)}


# --- rename, hide/show ------------------------------------------------------------------


def rename_body(session, body: str, new_name: str) -> dict:
    """Rename a solid body; the name stays after rebuilds."""
    new_name = str(new_name or "").strip()
    if not new_name:
        raise SolidWorksError("new_name must not be empty.")
    target = _resolve(session, [body])[0]
    if new_name != body and new_name in _names(session):
        raise SolidWorksError(f"A body is already named '{new_name}'.")
    target.Name = new_name
    model = session._require_model()
    model.ForceRebuild3(False)
    if new_name not in _names(session):
        raise SolidWorksError(f"SolidWorks kept the old name of '{body}'.")
    model.SetSaveFlag()
    return {"ok": True, "renamed": body, "name": new_name, "bodies": _summary(session)}


def show_bodies(session, bodies, visible: bool = True) -> dict:
    """Hide (visible=False) or show solid bodies; read back through IBody2.Visible."""
    targets = _resolve(session, as_names(bodies))
    for body in targets:
        body.HideBody(not visible)
    session._require_model().GraphicsRedraw2()
    wrong = [b.Name for b in _resolve(session, [t.Name for t in targets]) if bool(b.Visible) != bool(visible)]
    if wrong:
        raise SolidWorksError(f"SolidWorks did not {'show' if visible else 'hide'}: {', '.join(wrong)}.")
    return {"ok": True, "visible": bool(visible), "changed": [t.Name for t in targets], "bodies": _summary(session)}


# --- delete / keep, move / copy -------------------------------------------------------


def delete_bodies(session, bodies, keep: bool = False, name: str | None = None) -> dict:
    """Body-Delete/Keep feature: delete the named bodies, or (keep=True) keep only them."""
    model = session._require_model()
    targets = _resolve(session, as_names(bodies))
    before = _names(session)
    _select(session, model, targets)
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    feat = fm.InsertDeleteBody2(bool(keep))
    model.ClearSelection2(True)
    if feat is None:
        raise geom.feature_refusal(session, "InsertDeleteBody2")
    result = _new_feature_result(session, feat, name)
    after = [b["name"] for b in result["bodies"]]
    result["removed"] = [n for n in before if n not in after]
    if not result["removed"]:
        raise SolidWorksError("The Delete/Keep feature removed no body. Rolled back.")
    return result


def _move_once(session, model, targets, move, rotate, copies, name):
    """One Body-Move/Copy feature: a translation OR a rotation (SolidWorks drops
    the translation of a feature that also rotates)."""
    rot = rotation_args(rotate)
    before = _names(session)
    volumes = sum(_volume_mm3(b) for b in targets)
    centres = {b.Name: [float(c) * 1000.0 for c in b.GetMassProperties(1.0)[:3]] for b in targets}
    _select(session, model, targets)
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    feat = fm.InsertMoveCopyBody2(move[0] / 1000.0, move[1] / 1000.0, move[2] / 1000.0, 0.0,
                                  *rot, copies > 0, max(copies, 1))
    model.ClearSelection2(True)
    if feat is None:
        raise geom.feature_refusal(session, "InsertMoveCopyBody2")
    result = _new_feature_result(session, feat, name)
    if not copies:
        # SolidWorks names a moved body after the Move feature: give each its own name back,
        # pairing them by where their centre of mass had to land.
        expected = {old: moved_point(c, move, rotate) for old, c in centres.items()}
        found = {b.Name: [float(c) * 1000.0 for c in b.GetMassProperties(1.0)[:3]]
                 for b in _all_bodies(session) if b.Name not in before or b.Name in centres}
        pairs = match_names(expected, found)
        for body in _all_bodies(session):
            old = pairs.get(body.Name)
            if old and old != body.Name:
                try:
                    body.Name = old
                except Exception:  # noqa: BLE001
                    pass
        model.ForceRebuild3(False)
        result["bodies"] = _summary(session)
        lost = [n for n in centres if n not in _names(session)]
        if lost:
            result["warning"] = (f"{', '.join(lost)} did not land where expected, or lost its name: "
                                 "check bodies and their boxes.")
    else:
        added = sum(b["volume_mm3"] for b in result["bodies"] if b["name"] not in before)
        if added < 0.99 * volumes * copies:
            result["warning"] = (f"The copies hold {added:.1f} mm^3, expected {volumes * copies:.1f}: "
                                 "copies that overlap the originals may have merged.")
    result["new_bodies"] = [b["name"] for b in result["bodies"] if b["name"] not in before]
    return result


def move_copy_body(session, bodies, translate_mm=None, rotate=None, copies: int = 0,
                   name: str | None = None) -> dict:
    """Body-Move/Copy: rotate {axis, angle_deg, about_mm} and/or translate_mm
    [dx, dy, dz] (a turn and a shift become two features, turn first); copies > 0
    leaves the originals and makes that many copies, each one step further."""
    model = session._require_model()
    names = as_names(bodies)
    targets = _resolve(session, names)
    move = [float(c) for c in (translate_mm or [0.0, 0.0, 0.0])]
    if len(move) != 3:
        raise SolidWorksError(f"translate_mm must be [dx, dy, dz] (got {translate_mm}).")
    rotation_args(rotate)  # validates
    if not any(move) and not rotate:
        raise SolidWorksError("Give translate_mm and/or rotate: the bodies would not move.")
    copies = int(copies or 0)
    if copies < 0:
        raise SolidWorksError("copies must be >= 0.")
    if copies and rotate and any(move):
        raise SolidWorksError("Copies take a turn or a shift, not both: make the copies first, then move them.")
    steps = []
    if rotate:
        steps.append(_move_once(session, model, targets, [0.0, 0.0, 0.0], rotate, copies, name))
    if any(move):
        if steps:
            targets = _resolve(session, names)
        steps.append(_move_once(session, model, targets, move, None, copies,
                                f"{name}-shift" if (name and steps) else name))
    result = steps[-1]
    result["features"] = [s["feature"] for s in steps]
    result["translate_mm"], result["rotate"], result["copies"] = move, rotate or None, copies
    result["new_bodies"] = sorted({n for s in steps for n in s["new_bodies"]} & {b["name"] for b in result["bodies"]})
    warnings = [s["warning"] for s in steps if s.get("warning")]
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


# --- feature scope ----------------------------------------------------------------------

_SCOPE_DATA = ("IExtrudeFeatureData2", "IRevolveFeatureData2", "ISweepFeatureData", "ILoftFeatureData",
               "IWizardHoleFeatureData2", "ISimpleHoleFeatureData2", "IBoundaryBossFeatureData")


def _scope_data(session, feature):
    raw = feature.GetDefinition()
    if raw is None:
        raise SolidWorksError(f"'{feature.Name}' has no definition to scope.")
    for iface in _SCOPE_DATA:
        cls = getattr(session._mod, iface, None)
        if cls is None:
            continue
        data = binding.wrap(raw, cls)
        try:
            data.FeatureScope  # noqa: B018 - probes the interface
            return data
        except Exception:  # noqa: BLE001
            continue
    raise SolidWorksError(f"'{feature.Name}' ({feature.GetTypeName2()}) has no feature scope "
                          "(extrudes, cuts, revolves, sweeps, lofts and holes do).")


def set_feature_scope(session, feature: str, bodies=None) -> dict:
    """Limit a cut or boss to the named bodies (Feature Scope); bodies=None
    lets it act on all bodies again. Returns the bodies' volumes before and after."""
    model = session._require_model()
    session._require_part()
    feat = model.FeatureByName(feature) if hasattr(model, "FeatureByName") else None
    if feat is None:
        feat = binding.wrap(binding.wrap(model, session._mod.IPartDoc).FeatureByName(feature), session._mod.IFeature)
    if feat is None:
        raise SolidWorksError(f"No feature '{feature}' (list_features shows them).")
    feat = binding.wrap(feat, session._mod.IFeature)
    targets = _resolve(session, as_names(bodies)) if bodies else []
    scope_names = [t.Name for t in targets]  # read now: the wrappers go stale after ModifyDefinition
    before = {b["name"]: b["volume_mm3"] for b in _summary(session)}
    data = _scope_data(session, feat)
    if not data.AccessSelections(model, None):
        raise SolidWorksError(f"Cannot open '{feature}' for editing.")
    try:
        if targets:
            data.FeatureScope = True
            data.AutoSelect = False
            data.FeatureScopeBodies = _variant(targets)
        else:
            data.FeatureScope = False
            data.AutoSelect = True
        ok = feat.ModifyDefinition(data, model, None)
    except Exception:
        data.ReleaseSelectionAccess()
        raise
    if not ok:
        data.ReleaseSelectionAccess()
        raise geom.feature_refusal(session, f"ModifyDefinition of '{feature}' with the new scope")
    rebuilt = bool(model.ForceRebuild3(False))
    after = {b["name"]: b["volume_mm3"] for b in _summary(session)}
    return {"ok": True, "feature": feature, "scope": scope_names or "all bodies",
            "rebuild_ok": rebuilt, "volumes_before_mm3": before, "volumes_after_mm3": after,
            "mass_properties": session.get_mass_properties()["mass_properties"]}


# --- save out / insert ------------------------------------------------------------------


def save_bodies(session, bodies, path: str, close: bool = True) -> dict:
    """Save the named bodies to a new part file (linked to this part, like
    Insert into New Part). This part must be saved first."""
    model = session._require_model()
    part = binding.wrap(model, session._mod.IPartDoc)
    if not model.GetPathName():
        raise SolidWorksError("Save this part first (save_part): the new part references it by path.")
    path = os.path.abspath(path)
    if not path.lower().endswith(".sldprt"):
        path += ".SLDPRT"
    if os.path.exists(path):
        raise SolidWorksError(f"{path} exists; give a new file name.")
    targets = _resolve(session, as_names(bodies))
    source_title = model.GetTitle()
    _select(session, model, targets, mark=0)
    got = part.SaveToFile3(path, 1, 0, False, "")
    model.ClearSelection2(True)
    ok = got[0] if isinstance(got, tuple) else got
    if not ok or not os.path.exists(path):
        raise SolidWorksError(f"SolidWorks did not write {path} (answer {got}).")
    out = {"ok": True, "path": path, "bodies": [t.Name for t in targets], "size_bytes": os.path.getsize(path)}
    new_title = os.path.splitext(os.path.basename(path))[0]
    if close:
        try:
            session._sw.CloseDoc(new_title)
        except Exception:  # noqa: BLE001
            pass
        out["closed"] = True
    session._sw.ActivateDoc3(source_title, False, 0, 0)
    return out


def insert_part(session, path: str, config: str = "", name: str | None = None,
                material: bool = False) -> dict:
    """Insert another part's solid bodies into this one (an Insert Part feature,
    linked to the file). The new bodies get unique names (name, name_2 ...)."""
    model = session._require_model()
    part = binding.wrap(model, session._mod.IPartDoc)
    session._require_part()
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise SolidWorksError(f"No file {path}.")
    if model.GetPathName() and os.path.normcase(model.GetPathName()) == os.path.normcase(path):
        raise SolidWorksError("A part cannot be inserted into itself.")
    before = _all_bodies(session)
    before_ids = [b._oleobj_ for b in before]
    count_before = len(before)
    feat = part.InsertPart3(path, INSERT_SOLIDS | (INSERT_MATERIAL if material else 0), config or "")
    if feat is None:
        raise geom.feature_refusal(session, f"InsertPart3 of {path}")
    feat = binding.wrap(feat, session._mod.IFeature)
    model.ForceRebuild3(False)
    bodies_now = _all_bodies(session)
    new = bodies_now[count_before:] if len(bodies_now) > count_before else []
    del before_ids
    taken = [b.Name for b in bodies_now[:count_before]]
    base = name or os.path.splitext(os.path.basename(path))[0]
    renamed = []
    for body in new:
        wanted = unique_name(base, taken)
        try:
            body.Name = wanted
        except Exception:  # noqa: BLE001
            wanted = body.Name
        taken.append(wanted)
        renamed.append(wanted)
    result = session._finish_feature(feat, feat.Name, inserted=path)
    result["new_bodies"] = renamed
    result["bodies"] = _summary(session)
    if not renamed:
        raise SolidWorksError(f"{path} brought no solid body. Rolled back.")
    return result
