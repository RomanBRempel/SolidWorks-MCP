"""RBR tools: units, custom properties, configurations, flat-face DXF, BOM and mass budget,
and tool_source (the code any tool runs).

Plain functions taking the SolidWorksSession; registered as MCP tools in
rbr/__init__.py and run through session.run_guarded like every upstream tool.
Constant values were read from swconst.tlb of SOLIDWORKS 2025 (rev 33).
"""

import csv
import inspect
import os

from .. import binding
from ..errors import SolidWorksError
from ..session import SolidWorksSession

SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2

# swUserPreferenceIntegerValue_e
SW_UNIT_SYSTEM = 263
SW_UNITS_LINEAR = 47
SW_UNITS_LINEAR_DECIMALS = 49
SW_UNITS_MASSPROP_LENGTH = 258
SW_UNITS_MASSPROP_MASS = 259
SW_UNITS_MASSPROP_DECIMALS = 261
SW_NO_OPTION = 0
SW_UNIT_SYSTEMS = {"CGS": 1, "MKS": 2, "IPS": 3, "CUSTOM": 4, "MMGS": 5}
SW_MASS_UNITS = {"mg": 1, "g": 2, "kg": 3, "lb": 4}
SW_LENGTH_MM = 0

SW_CUSTOM_TEXT = 30
SW_CUSTOM_REPLACE = 2
SW_EXPORT_SELECTED_FACES = 2


# --- helpers ----------------------------------------------------------------------


def _model(session):
    return session._require_model()


def _ext(session, model):
    return binding.wrap(model.Extension, session._mod.IModelDocExtension)


def _tuple(value):
    if value is None:
        return ()
    return tuple(value) if isinstance(value, (list, tuple)) else (value,)


def _cpm(session, model, config: str):
    mgr = _ext(session, model).CustomPropertyManager(config or "")
    return binding.wrap(mgr, session._mod.ICustomPropertyManager)


def _read_props(session, model, config: str = "", only=None) -> dict:
    cpm = _cpm(session, model, config)
    out = {}
    for name in _tuple(cpm.GetNames()):
        if only and name not in only:
            continue
        try:
            got = cpm.Get6(name, False)
        except Exception:  # noqa: BLE001 - older releases: Get5
            got = cpm.Get5(name, False)
        got = _tuple(got)
        # (retval, value, resolved, wasResolved, linkToProp) - value/resolved positions
        value = got[1] if len(got) > 1 else None
        resolved = got[2] if len(got) > 2 else value
        out[name] = {"value": value, "resolved": resolved} if value != resolved else resolved
    return out


# --- units ------------------------------------------------------------------------


def set_units(session, system: str = "MMGS", decimals: int = 2, mass_unit: str = "g") -> dict:
    """Set the current document's units; MMGS = mm, g, s. Read back after setting."""
    key = system.upper()
    if key not in SW_UNIT_SYSTEMS:
        raise SolidWorksError(f"Unknown unit system '{system}'; use one of {', '.join(SW_UNIT_SYSTEMS)}.")
    if mass_unit not in SW_MASS_UNITS:
        raise SolidWorksError(f"Unknown mass unit '{mass_unit}'; use one of {', '.join(SW_MASS_UNITS)}.")
    model = _model(session)
    ext = _ext(session, model)
    # Order matters: changing a single unit after the system turns it into Custom
    # (verified on SW 2025), so decimals go first and the system last.
    ext.SetUserPreferenceInteger(SW_UNITS_LINEAR_DECIMALS, SW_NO_OPTION, int(decimals))
    ext.SetUserPreferenceInteger(SW_UNITS_MASSPROP_DECIMALS, SW_NO_OPTION, int(decimals))
    if key == "MMGS" and mass_unit != "g":
        key = "CUSTOM"
    if key == "CUSTOM":
        ext.SetUserPreferenceInteger(SW_UNIT_SYSTEM, SW_NO_OPTION, SW_UNIT_SYSTEMS["CUSTOM"])
        ext.SetUserPreferenceInteger(SW_UNITS_LINEAR, SW_NO_OPTION, SW_LENGTH_MM)
        ext.SetUserPreferenceInteger(SW_UNITS_MASSPROP_LENGTH, SW_NO_OPTION, SW_LENGTH_MM)
        ext.SetUserPreferenceInteger(SW_UNITS_MASSPROP_MASS, SW_NO_OPTION, SW_MASS_UNITS[mass_unit])
    else:
        ext.SetUserPreferenceInteger(SW_UNIT_SYSTEM, SW_NO_OPTION, SW_UNIT_SYSTEMS[key])
    back = {v: k for k, v in SW_UNIT_SYSTEMS.items()}
    system_now = ext.GetUserPreferenceInteger(SW_UNIT_SYSTEM, SW_NO_OPTION)
    if system_now != SW_UNIT_SYSTEMS[key]:
        raise SolidWorksError(f"SolidWorks kept unit system {back.get(system_now, system_now)}, not {key}.")
    model.SetSaveFlag()
    return {"ok": True, "document": model.GetTitle(), "unit_system": back.get(system_now, system_now),
            "linear_mm": ext.GetUserPreferenceInteger(SW_UNITS_LINEAR, SW_NO_OPTION) == SW_LENGTH_MM,
            "decimals": ext.GetUserPreferenceInteger(SW_UNITS_LINEAR_DECIMALS, SW_NO_OPTION)}


# --- custom properties ------------------------------------------------------------


def get_custom_properties(session, config: str = "") -> dict:
    """Custom properties of the current document: file level (config='') or one configuration's."""
    model = _model(session)
    props = _read_props(session, model, config)
    return {"ok": True, "document": model.GetTitle(), "config": config, "count": len(props),
            "properties": props}


def set_custom_properties(session, properties: dict, config: str = "", delete: list | None = None) -> dict:
    """Add or replace text custom properties ({name: value}); delete removes names. Read back."""
    if not isinstance(properties, dict):
        raise SolidWorksError("properties must be an object {name: value}.")
    model = _model(session)
    cpm = _cpm(session, model, config)
    for name, value in properties.items():  # Add3 -> 0 = added or changed; checked by the read-back below
        cpm.Add3(str(name), SW_CUSTOM_TEXT, "" if value is None else str(value), SW_CUSTOM_REPLACE)
    for name in delete or []:
        cpm.Delete2(str(name))
    now = _read_props(session, model, config)
    missing = [n for n, v in properties.items()
               if n not in now or str((now[n]["value"] if isinstance(now[n], dict) else now[n])) != str(v)]
    if missing:
        raise SolidWorksError(f"SolidWorks did not store: {', '.join(map(str, missing))}.")
    model.SetSaveFlag()
    return {"ok": True, "document": model.GetTitle(), "config": config, "properties": now}


# --- configurations ---------------------------------------------------------------


def _active_config(session, model) -> str | None:
    try:
        mgr = binding.wrap(model.ConfigurationManager, session._mod.IConfigurationManager)
        cfg = binding.wrap(mgr.ActiveConfiguration, session._mod.IConfiguration)
        return cfg.Name if cfg is not None else None
    except Exception:  # noqa: BLE001
        return None


def list_configurations(session) -> dict:
    """Configurations of the current document and which is active."""
    model = _model(session)
    names = list(_tuple(model.GetConfigurationNames()))
    return {"ok": True, "document": model.GetTitle(), "active": _active_config(session, model),
            "count": len(names), "configurations": names}


def activate_configuration(session, name: str) -> dict:
    model = _model(session)
    if name not in _tuple(model.GetConfigurationNames()):
        raise SolidWorksError(f"No configuration '{name}'; there are: {', '.join(_tuple(model.GetConfigurationNames()))}.")
    if _active_config(session, model) == name:
        return {"ok": True, "active": name, "changed": False}
    if not model.ShowConfiguration2(name):
        raise SolidWorksError(f"SolidWorks would not activate configuration '{name}'.")
    return {"ok": True, "active": _active_config(session, model)}


def add_configuration(session, name: str, description: str = "") -> dict:
    """Add a configuration (derived from the active one) and make it active."""
    model = _model(session)
    if name in _tuple(model.GetConfigurationNames()):
        raise SolidWorksError(f"Configuration '{name}' already exists.")
    cfg = model.AddConfiguration3(name, description, "", 0)
    if not cfg:
        raise SolidWorksError(f"SolidWorks could not add configuration '{name}'.")
    return {"ok": True, "added": name, "active": _active_config(session, model),
            "configurations": list(_tuple(model.GetConfigurationNames()))}


# --- flat face to DXF -------------------------------------------------------------

_DIRS = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}


def _face_for(session, face: str):
    body = session._solid_body()
    faces = session._body_faces(body)
    if face.startswith("#"):
        try:
            return faces[int(face[1:])]
        except (ValueError, IndexError):
            raise SolidWorksError(f"No face {face}; list_faces shows indices 0..{len(faces) - 1}.")
    direction, _, side = face.partition(":")
    if direction not in _DIRS:
        raise SolidWorksError("face: '#index' from list_faces, or a direction such as '+z' / '-z:inner'.")
    picked = session._pick_planar_face(faces, _DIRS[direction], side or "outer")
    if not picked or picked[0] is None:
        raise SolidWorksError(f"No planar face facing {direction}.")
    return picked[0]


def export_face_dxf(session, path: str, face: str = "+z") -> dict:
    """Export one planar face (outline + holes, 1:1, mm) to DXF/DWG for milling or laser.

    face: '#index' from list_faces or a direction ('+z', '-z:inner'). The part
    must be saved (ExportToDWG2 needs its file).
    """
    if not path.lower().endswith((".dxf", ".dwg")):
        raise SolidWorksError("path must end in .dxf or .dwg.")
    part = session._require_part()
    model = _model(session)
    model_path = model.GetPathName()
    if not model_path:
        raise SolidWorksError("Save the part first (save_part): DXF export reads it from its file.")
    target = _face_for(session, face)
    entity = binding.wrap(target, session._mod.IEntity)
    model.ClearSelection2(True)
    if not entity.Select4(False, None):
        raise SolidWorksError("Could not select the face.")
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    import pythoncom
    from win32com.client import VARIANT
    empty = VARIANT(pythoncom.VT_DISPATCH, None)
    ok = part.ExportToDWG2(path, model_path, SW_EXPORT_SELECTED_FACES, True, empty, False, False, 0, empty)
    model.ClearSelection2(True)
    if not ok or not os.path.isfile(path):
        raise SolidWorksError("SolidWorks did not write the DXF.")
    f2 = binding.wrap(target, session._mod.IFace2)
    return {"ok": True, "path": path, "bytes": os.path.getsize(path), "face_area_mm2": round(f2.GetArea() * 1e6, 3)}


# --- BOM and mass budget ----------------------------------------------------------


def _doc_mass_kg(session, doc) -> float | None:
    try:
        ext = binding.wrap(doc.Extension, session._mod.IModelDocExtension)
        props = _tuple(ext.GetMassProperties2(1, 0, False))
        if props and isinstance(props[0], (list, tuple)):
            props = _tuple(props[0])
        return float(props[5]) if len(props) > 5 else None
    except Exception:  # noqa: BLE001
        return None


def assembly_bom(session, csv_path: str | None = None, top_level_only: bool = False,
                 properties: list | None = None) -> dict:
    """Bill of materials of the current assembly: one row per part file + configuration,
    with quantity, mass each and total (g), material and custom properties.

    top_level_only counts only the assembly's own components (sub-assemblies as
    rows); otherwise parts are counted through all sub-assemblies. Suppressed
    and excluded-from-BOM components are skipped. csv_path writes ';'-separated
    UTF-8 with BOM for Excel.
    """
    asm = session._require_assembly()
    comps = _tuple(asm.GetComponents(bool(top_level_only)))
    rows, docs_mass, skipped = {}, {}, 0
    for raw in comps:
        comp = binding.wrap(raw, session._mod.IComponent2)
        try:
            if comp.IsSuppressed() or comp.ExcludeFromBOM:
                skipped += 1
                continue
        except Exception:  # noqa: BLE001
            pass
        doc = binding.wrap(comp.GetModelDoc2(), session._mod.IModelDoc2)
        if doc is None:
            skipped += 1
            continue
        kind = int(doc.GetType())
        if not top_level_only and kind == SW_DOC_ASSEMBLY:
            continue  # its parts are listed themselves
        path = doc.GetPathName() or doc.GetTitle()
        config = comp.ReferencedConfiguration or ""
        key = (path, config)
        if key not in rows:
            if path not in docs_mass:
                docs_mass[path] = _doc_mass_kg(session, doc)
            material = ""
            if kind == SW_DOC_PART:
                try:
                    material = binding.wrap(doc, session._mod.IPartDoc).GetMaterialPropertyName2(config, "")[0] or ""
                except Exception:  # noqa: BLE001
                    material = ""
            props = _read_props(session, doc, "", properties)
            props.update(_read_props(session, doc, config, properties) if config else {})
            mass_g = docs_mass[path] * 1000 if docs_mass[path] is not None else None
            rows[key] = {"file": os.path.basename(path), "config": config,
                         "type": "assembly" if kind == SW_DOC_ASSEMBLY else "part",
                         "qty": 0, "mass_g": round(mass_g, 2) if mass_g is not None else None,
                         "material": material, "properties": props, "path": path}
        rows[key]["qty"] += 1
    items = sorted(rows.values(), key=lambda r: (r["file"].lower(), r["config"]))
    total = 0.0
    for r in items:
        r["total_g"] = round(r["mass_g"] * r["qty"], 2) if r["mass_g"] is not None else None
        total += r["total_g"] or 0.0
    assembly_mass = _doc_mass_kg(session, _model(session))
    res = {"ok": True, "assembly": _model(session).GetTitle(), "rows": len(items),
           "pieces": sum(r["qty"] for r in items), "skipped": skipped,
           "sum_of_rows_g": round(total, 2),
           "assembly_mass_g": round(assembly_mass * 1000, 2) if assembly_mass is not None else None,
           "items": items}
    if csv_path:
        keys = sorted({k for r in items for k in r["properties"]})
        os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["file", "config", "type", "qty", "mass_g", "total_g", "material", *keys, "path"])
            for r in items:
                vals = [r["properties"].get(k, "") for k in keys]
                vals = [v.get("resolved") if isinstance(v, dict) else v for v in vals]
                w.writerow([r["file"], r["config"], r["type"], r["qty"], r["mass_g"], r["total_g"],
                            r["material"], *vals, r["path"]])
        res["csv"] = csv_path
    return res


# --- transparency -----------------------------------------------------------------


def tool_source(session, name: str) -> dict:
    """The Python code a tool runs: the MCP wrapper and the session method behind it."""
    from .. import server
    from . import script_tool
    from . import tools as rbr_tools
    out = {"ok": True, "name": name}
    wrapper = getattr(server, name, None) or getattr(server, "_rbr_" + name, None)
    target = (getattr(SolidWorksSession, name, None) or getattr(rbr_tools, name, None)
              or getattr(script_tool, name, None))
    if wrapper is None and target is None:
        raise SolidWorksError(f"No tool or session method '{name}'.")
    for key, obj in (("wrapper", wrapper), ("implementation", target)):
        if obj is None:
            continue
        fn = getattr(obj, "fn", obj)  # FastMCP may wrap the function
        try:
            src = inspect.getsource(fn)
            out[key] = {"file": inspect.getsourcefile(fn), "line": inspect.getsourcelines(fn)[1],
                        "source": src[:30000]}
        except (OSError, TypeError):
            out[key] = {"source": None}
    return out


# --- shell feasibility -------------------------------------------------------------------


def face_radii(corners_mm, normals) -> tuple:
    """(min convex radius, min concave radius) in mm of a tessellated face, None where flat.

    Uses each triangle edge's two vertex normals: along an arc of radius R the
    normals turn by |dn| = |dp| / R (exact for a circle), and dn . dp > 0 where
    the surface bulges outward (convex), < 0 where it is hollow (concave).
    corners_mm: flat [x, y, z] * 3 per triangle; normals: unit outward vertex normals, same layout.
    """
    convex = concave = None
    n_tri = min(len(corners_mm), len(normals)) // 9
    for t in range(n_tri):
        pts = [corners_mm[9 * t + 3 * k:9 * t + 3 * k + 3] for k in range(3)]
        nrm = [normals[9 * t + 3 * k:9 * t + 3 * k + 3] for k in range(3)]
        for i, j in ((0, 1), (1, 2), (2, 0)):
            dp = [b - a for a, b in zip(pts[i], pts[j])]
            dn = [b - a for a, b in zip(nrm[i], nrm[j])]
            len_dn = sum(c * c for c in dn) ** 0.5
            if len_dn < 1e-6:
                continue
            len_dp = sum(c * c for c in dp) ** 0.5
            radius = len_dp / len_dn
            if sum(a * b for a, b in zip(dn, dp)) > 0:
                convex = radius if convex is None else min(convex, radius)
            else:
                concave = radius if concave is None else min(concave, radius)
    return convex, concave


FREEFORM_SURFACES = {4006, 4008, 4010}  # B-spline, offset, revolved spline: no exact offset to fall back on


def shell_verdict(thickness_mm: float, thinnest_mm, radius_rows: list) -> tuple:
    """(verdict, reasons) from the measured material and curvature. Pure, unit-tested.

    Verified on SOLIDWORKS 2025: SolidWorks trims an offset that folds over on
    analytic faces and extrusions (a 4 mm shell over R3 fillets and a 6.5 mm one
    over a spline outline of R5.6 both built), but where the material is thinner
    than twice the wall the cavity vanishes: a 10.5 mm shell on a 19.6 mm wide
    part 'succeeded' and removed nothing. So the material decides; a freeform
    convex radius under the thickness only adds risk.
    """
    reasons, verdict = [], "ok"
    if thinnest_mm is not None and thinnest_mm < 2 * thickness_mm:
        verdict = "fail"
        reasons.append(f"material only {thinnest_mm:.2f} mm thick somewhere: under 2 x {thickness_mm} mm the "
                       "inner walls meet and that region gets no cavity (the shell may build and remove nothing)")
    elif thinnest_mm is not None and thinnest_mm < 2.5 * thickness_mm:
        verdict = "risk"
        reasons.append(f"material {thinnest_mm:.2f} mm thick somewhere: less than {2.5 * thickness_mm:.1f} mm "
                       "leaves a slit-thin cavity")
    freeform = [r for r in radius_rows if r.get("surface_type") in FREEFORM_SURFACES
                and r["radius_mm"] < thickness_mm * 1.05]
    if freeform:
        verdict = "fail" if verdict == "fail" else "risk"
        reasons.append(f"{len(freeform)} freeform face(s) bulge with a radius under the thickness "
                       f"(smallest {freeform[0]['radius_mm']} mm): their offset folds over and SolidWorks may "
                       "compute for long and fail")
    return verdict, reasons


def shell_check(session, thickness_mm: float, outward: bool = False, top: int = 15,
                budget_s: float = 12.0, samples: int = 3000) -> dict:
    """Can a shell of thickness_mm be built on the current part? Checked before trying it.

    Measures (1) the material thickness straight through the part, by rays from
    points over every face: under 2 x thickness the walls meet; (2) the smallest
    convex radius (concave for outward) of every curved face from its
    tessellation: on freeform faces under the thickness the offset folds over.
    After add_shell, also compare the volume: a shell that removed nothing failed.
    The material check runs first with at most `samples` rays; the curvature
    pass stops when budget_s is used up and the result says `partial`.
    """
    import time as _time
    t0 = _time.perf_counter()
    if thickness_mm <= 0:
        raise SolidWorksError("thickness_mm must be > 0.")
    part = session._require_part()
    bodies = _tuple(part.GetBodies2(0, True))
    if not bodies:
        raise SolidWorksError("No solid body.")
    mod = session._mod
    walls = None
    saved_cap = type(session)._MAX_WALL_SAMPLES
    try:
        type(session)._MAX_WALL_SAMPLES = max(100, int(samples))  # upstream's 100 000 took > 60 s on a loft
        body0 = binding.wrap(bodies[0], mod.IBody2)
        triangles = [(index, *tri) for index, face in enumerate(session._body_faces(body0))
                     for tri in session._face_triangles(binding.wrap(face, mod.IFace2))]
        walls = session._thin_walls(body0, triangles, 2.5 * thickness_mm)
    except Exception as exc:  # noqa: BLE001 - the radius check still stands
        walls = {"error": str(exc)}
    finally:
        type(session)._MAX_WALL_SAMPLES = saved_cap
    walls_s = round(_time.perf_counter() - t0, 2)
    kind = "concave" if outward else "convex"
    rows, faces_seen, curved, partial = [], 0, 0, False
    for b_index, raw_body in enumerate(bodies):
        body = binding.wrap(raw_body, mod.IBody2)
        for f_index, raw_face in enumerate(_tuple(body.GetFaces())):
            if _time.perf_counter() - t0 > budget_s:
                partial = True
                break
            faces_seen += 1
            face = binding.wrap(raw_face, mod.IFace2)
            surface = binding.wrap(face.GetSurface(), mod.ISurface)
            if surface is not None and surface.IsPlane():
                continue
            curved += 1
            corners = [v * 1000.0 for v in _tuple(face.GetTessTriangles(True))]
            normals = list(_tuple(face.GetTessNorms()))
            convex, concave = face_radii(corners, normals)
            radius = concave if outward else convex
            if radius is None:
                continue
            box = _tuple(face.GetBox())
            centre = [round((box[i] + box[i + 3]) / 2 * 1000, 2) for i in range(3)] if len(box) >= 6 else None
            rows.append({"body": b_index, "face": f"#{f_index}", "radius_mm": round(radius, 3),
                         "area_mm2": round(face.GetArea() * 1e6, 1), "centre_mm": centre,
                         "surface_type": int(surface.Identity()) if surface is not None else None})
    rows.sort(key=lambda r: r["radius_mm"])
    thinnest = walls.get("thinnest_mm") if isinstance(walls, dict) else None
    verdict, reasons = shell_verdict(thickness_mm, thinnest, rows)
    min_r = rows[0]["radius_mm"] if rows else None
    max_t = [v for v in (thinnest / 2.5 if thinnest else None,
                         min_r / 1.05 if min_r and any(r.get("surface_type") in FREEFORM_SURFACES for r in rows)
                         else None) if v]
    return {"ok": True, "thickness_mm": thickness_mm, "direction": "outward" if outward else "inward",
            "verdict": verdict, "reasons": reasons,
            "thinnest_material_mm": thinnest, "thin_faces": (walls or {}).get("faces", [])[:max(1, int(top))],
            f"min_{kind}_radius_mm": min_r, "max_safe_thickness_mm": round(min(max_t), 3) if max_t else None,
            "faces_checked": faces_seen, "curved_faces": curved, "partial": partial,
            "timing_s": {"material": walls_s, "total": round(_time.perf_counter() - t0, 2)},
            "radius_faces": rows[:max(1, int(top))],
            "after_shell": "compare volume_mm3 before/after add_shell: unchanged means the shell did nothing"}
