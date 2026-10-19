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
