"""SolidCAM (the SolidCAM add-in inside the running SolidWorks) through its
automation API 2.0, scautom2.dll (1.8.0).

What the API does, verified on SolidCAM 2025 SP2 HF1 in SOLIDWORKS 2025 SP3:
- Launcher (CLSID D7E018F6-...) .Item(0) is the IApplication of the SolidCAM
  that runs as solidcam.exe under the user's SLDWORKS.exe. Never start a
  SolidWorks or SolidCAM of our own: everything goes through the one instance.
- IApplication: version, machines, operation templates, OpenPart, Part.
- IPart: machine, Homes (Create(TopCorner) / CreateFromCAD(cs)), Geometries,
  CreateStockBox(Default), CreateTarget, Operations (Calculate,
  GenerateGCode), Tools, Save/Close. G-code lands in <part folder>\\G-codes.
- ITemplate.CreateOperation(geometry, sub_machine, home) adds a job from an
  operation template; the job machines only what its geometry holds. The API
  cannot DEFINE a geometry (a chain, a face set): with only the target
  geometry a pocket or face-mill template posts a program without motion
  (positions read 142857, SolidCAM's 'no point'). gcode() reports that.
- CreateNewMillingPart(path, cs, machine, inch) opens SolidCAM's 'Milling
  Part Data' panel and waits for the user: the CAD coordinate system passed in
  is NOT taken as the part zero. In the panel: 'Ноль детали' -> 'Выберите
  систему координат' -> SolidCAM_Zero -> OK three times.
- The old API (scautom.dll, ISolidCAM / IAutomation) crashed the MCP server
  process when created in it: not used.

The user's convention: the part zero sits on the TOP face at the bottom-left
corner (min X, min Y, max Z of the part in its machining orientation).
"""

import os
import re

import pythoncom
import win32com.client
from win32com.client import gencache

from .. import binding
from ..errors import SolidWorksError

TLB_GUID = "{8991AA70-721C-4478-B002-184CB55C37BF}"  # SolidCAM 2.0 Type Library (scautom2.dll)
LAUNCHER_CLSID = "{D7E018F6-718D-4ED0-966A-94D6DEDE3B7D}"
ZERO_NAME = "SolidCAM_Zero"
HOME_ORIGIN = {"top_corner": 0, "top_center": 1, "cad_origin": 2}  # eHomeOrigin
DEFINE_BY_SOLID = 0  # eDefineBy
NO_POINT = "142857"  # SolidCAM's placeholder coordinate: a job without toolpath
_MOTION = re.compile(r"\bG(?:0?[123]|8[1-9])\b")  # feed moves, arcs and drilling cycles

_sc = None


# --- pure helpers (unit-tested) ------------------------------------------------------


def zero_corner(box: dict, up: str = "+z") -> list:
    """The part zero for a bounding box {min_mm, max_mm}: on the top face (the
    side `up` points to), at the bottom-left corner - min of the two other axes."""
    axis = {"x": 0, "y": 1, "z": 2}.get(up[-1:].lower())
    if axis is None or up[:1] not in "+-" or len(up) != 2:
        raise SolidWorksError(f"up must be '+z', '-z', '+x', '-x', '+y' or '-y' (got {up!r}).")
    lo, hi = list(box["min_mm"]), list(box["max_mm"])
    point = list(lo)
    point[axis] = hi[axis] if up[0] == "+" else lo[axis]
    return [round(c, 6) for c in point]


def gcode_check(text: str) -> dict:
    """Lines, motion blocks (G1/G2/G3) and 'no point' placeholders in a G-code program."""
    lines = text.splitlines()
    motion = sum(1 for line in lines if _MOTION.search(line.upper()))
    placeholders = sum(1 for line in lines if NO_POINT in line)
    return {"lines": len(lines), "motion_blocks": motion, "placeholder_positions": placeholders,
            "has_toolpath": motion > 0}


# --- connection -------------------------------------------------------------------------


def _module():
    global _sc
    if _sc is None:
        try:
            _sc = gencache.EnsureModule(TLB_GUID, 0, 1, 0)
        except pythoncom.com_error as exc:
            raise SolidWorksError(f"SolidCAM's automation library (scautom2.dll) is not registered: {exc}")
        if _sc is None:
            raise SolidWorksError("Could not load SolidCAM's automation library (scautom2.dll).")
    return _sc


def _wrap(obj, iface: str):
    if obj is None:
        return None
    return getattr(_module(), iface)(getattr(obj, "_oleobj_", obj))


def _app(session=None):
    sc = _module()
    try:
        launcher = sc.ILauncher(win32com.client.Dispatch(LAUNCHER_CLSID)._oleobj_)
    except pythoncom.com_error as exc:
        raise SolidWorksError(f"SolidCAM's launcher is not available: {exc}")
    if launcher.Count < 1:
        raise SolidWorksError("SolidCAM is not running in SolidWorks: open its task pane tab once, "
                              "or switch the SolidCAM add-in on (Tools > Add-ins).")
    return _wrap(launcher.Item(0), "IApplication")


def _part(app, required: bool = True):
    part = app.Part
    if part is None:
        if required:
            raise SolidWorksError("No SolidCAM part is open (solidcam_open, or solidcam_new_part and confirm "
                                  "its Part Data panel).")
        return None
    return _wrap(part, "IPart")


def _check(app, what: str):
    try:
        code, text = app.LastError, app.LastErrorDescription
    except Exception:  # noqa: BLE001
        return
    if code:
        raise SolidWorksError(f"SolidCAM refused {what}: {text} (error {code}).")


def _items(collection, iface: str) -> list:
    return [_wrap(collection.Item(i), iface) for i in range(collection.Count)]


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _operation_row(op) -> dict:
    tool = _safe(lambda: _wrap(op.Tool, "ITool"))
    return {"name": _safe(lambda: op.Name), "id": _safe(lambda: op.ID), "type": _safe(lambda: str(op.Type)),
            "calculated": _safe(lambda: bool(op.Calculated)), "suppressed": _safe(lambda: bool(op.Suppressed)),
            "geometry": _safe(lambda: _wrap(op.Geometry, "IGeometry").Name),
            "tool": _safe(lambda: tool.Name) if tool is not None else None}


def _part_info(part) -> dict:
    return {
        "name": _safe(lambda: part.Name), "path": _safe(lambda: part.path), "folder": _safe(lambda: part.Folder),
        "machine": _safe(lambda: _wrap(part.machine, "IMachine").Name),
        "homes": [_safe(lambda h=h: h.Name) for h in _safe(lambda: _items(part.Homes, "IHome"), [])],
        "geometries": [_safe(lambda g=g: g.Name) for g in _safe(lambda: _items(part.Geometries, "IGeometry"), [])],
        "stock": _safe(lambda: part.StockName) or None, "target": _safe(lambda: part.TargetName) or None,
        "operations": [_operation_row(o) for o in _safe(lambda: _items(part.Operations, "IOperation"), [])],
        "tools": [_safe(lambda t=t: t.Name) for t in _safe(lambda: _items(part.Tools, "ITool"), [])],
        "total_machining_time": _safe(lambda: str(part.TotalMachiningTime)),
    }


# --- tools ------------------------------------------------------------------------------


def status(session) -> dict:
    """SolidCAM version, its machines and operation templates, and the open CAM part."""
    app = _app(session)
    machines = _safe(lambda: _items(app.Machines, "IMachine"), [])
    templates = _safe(lambda: _items(app.Templates, "ITemplate"), [])
    part = _part(app, required=False)
    return {"ok": True, "version": _safe(lambda: f"{app.Version} {app.Build}"), "pid": _safe(lambda: app.pid),
            "machines": sorted({_safe(lambda m=m: m.Name) for m in machines} - {None}),
            "templates": [_safe(lambda t=t: t.Name) for t in templates],
            "post_processor_dir": _safe(lambda: app.GppPath), "tables_dir": _safe(lambda: app.TablesPath),
            "part": _part_info(part) if part is not None else None}


def make_zero(session, up: str = "+z", name: str = ZERO_NAME) -> dict:
    """A SolidWorks coordinate system for the SolidCAM part zero in the current
    part: on the top face (`up`), at the bottom-left corner of the part's box,
    axes along the model's. An existing one of that name is replaced."""
    model = session._require_model()
    session._require_part()
    box = session.get_bounding_box()["bounding_box_mm"]
    point = zero_corner(box, up)
    part = binding.wrap(model, session._mod.IPartDoc)
    for old in (name, f"{name}_point"):  # remake: the coordinate system, then its point sketch
        if part.FeatureByName(old) is not None:
            session.delete_feature(old, with_children=True)
    sm = binding.wrap(model.SketchManager, session._mod.ISketchManager)
    sm.Insert3DSketch(True)
    try:
        sm.AddToDB = True  # without it the point snaps to the origin (verified)
        pt = sm.CreatePoint(point[0] / 1000.0, point[1] / 1000.0, point[2] / 1000.0)
    finally:
        sm.AddToDB = False
        sm.Insert3DSketch(True)
    if pt is None:
        raise SolidWorksError("Could not place the zero point.")
    try:
        binding.wrap(model.FeatureByPositionReverse(0), session._mod.IFeature).Name = f"{name}_point"
    except Exception:  # noqa: BLE001
        pass
    pt = binding.wrap(pt, session._mod.ISketchPoint)
    model.ClearSelection2(True)
    data = binding.wrap(binding.wrap(model.SelectionManager, session._mod.ISelectionMgr).CreateSelectData(),
                        session._mod.ISelectData)
    data.Mark = 1
    if not pt.Select4(False, data):
        raise SolidWorksError("Could not select the zero point.")
    fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
    cs = fm.InsertCoordinateSystem(False, False, False)
    model.ClearSelection2(True)
    if cs is None:
        raise SolidWorksError("SolidWorks refused the coordinate system.")
    last = binding.wrap(model.FeatureByPositionReverse(0), session._mod.IFeature)
    try:
        last.Name = name
    except Exception:  # noqa: BLE001
        pass
    ext = binding.wrap(model.Extension, session._mod.IModelDocExtension)
    xform = ext.GetCoordinateSystemTransformByName(name)
    data = list(binding.wrap(xform, session._mod.IMathTransform).ArrayData) if xform else None
    origin = [round(c * 1000.0, 4) for c in data[9:12]] if data else None
    if origin is None or max(abs(a - b) for a, b in zip(origin, point)) > 1e-3:
        raise SolidWorksError(f"The coordinate system landed at {origin}, not at {point}.")
    if up != "+z":
        note = (f"Axes follow the model's; with up={up!r} turn Z in SolidCAM's zero panel, or pick "
                "'Выберите поверхность' and the top face there.")
    else:
        note = None
    return {"ok": True, "coordinate_system": name, "origin_mm": origin, "part_box_mm": box, "note": note}


def new_part(session, path: str, machine: str, up: str = "+z", inch: bool = False) -> dict:
    """Start a SolidCAM milling part for the current (saved) SolidWorks part:
    the zero coordinate system first, then SolidCAM's Part Data panel."""
    model = session._require_model()
    if not model.GetPathName():
        raise SolidWorksError("Save the SolidWorks part first (save_part).")
    app = _app(session)
    names = {_safe(lambda m=m: m.Name) for m in _safe(lambda: _items(app.Machines, "IMachine"), [])}
    if machine not in names:
        raise SolidWorksError(f"No SolidCAM machine '{machine}' (there are: {', '.join(sorted(n for n in names if n))}).")
    path = os.path.abspath(path)
    if not path.lower().endswith(".prz"):
        path += ".prz"
    zero = make_zero(session, up)
    model.Save3(1, 0, 0)
    session._sw.CommandInProgress = False
    app.CreateNewMillingPart(path, ZERO_NAME, machine, bool(inch))
    session._model = None  # SolidCAM replaces the open document with its CAM assembly
    _check(app, "the new part")
    part = _part(app, required=False)
    out = {"ok": True, "path": path, "machine": machine, "zero": zero}
    if part is None:
        out["waiting_for_user"] = True
        out["next"] = ("SolidCAM shows its 'Milling Part Data' panel: Ноль детали -> Выберите систему координат -> "
                       f"{ZERO_NAME} -> OK, OK, then OK in Part Data. Then call solidcam_part.")
    else:
        out["part"] = _part_info(part)
    return out


def open_part(session, path: str) -> dict:
    """Open an existing SolidCAM part (.prz)."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise SolidWorksError(f"No file {path}.")
    app = _app(session)
    session._sw.CommandInProgress = False
    app.OpenPart(path)
    session._model = None
    _check(app, f"opening {path}")
    return {"ok": True, "part": _part_info(_part(app))}


def part_info(session) -> dict:
    """The open SolidCAM part: machine, zeros, geometries, stock, target, operations, tools."""
    return {"ok": True, "part": _part_info(_part(_app(session)))}


def add_zero(session, position: str = "top_corner", coordinate_system: str | None = None) -> dict:
    """Add a machine zero (home) to the open CAM part: 'top_corner' (the user's usual),
    'top_center', 'cad_origin', or from a SolidWorks coordinate system by name."""
    app = _app(session)
    part = _part(app)
    homes = part.Homes
    before = _part_info(part)["homes"]
    if coordinate_system:
        homes.CreateFromCAD(coordinate_system)
    else:
        if position not in HOME_ORIGIN:
            raise SolidWorksError(f"position must be one of {sorted(HOME_ORIGIN)} (got {position!r}).")
        homes.Create(HOME_ORIGIN[position])
    _check(app, "the zero")
    after = _part_info(part)["homes"]
    out = {"ok": True, "homes": after, "added": [h for h in after if h not in before]}
    if not out["added"]:
        out["note"] = "SolidCAM added no zero: the part already has one at that position (verified with top_corner)."
    return out


def stock_box(session, offsets_mm: list | None = None, name: str = "Stock") -> dict:
    """A box stock around the model: offsets_mm = [x+, y+, z+, x-, y-, z-] (default
    SolidCAM's own default offsets)."""
    app = _app(session)
    part = _part(app)
    if offsets_mm is None:
        ok = part.CreateStockBoxDefault(name, True, DEFINE_BY_SOLID, False, False, 0.01)
    else:
        if len(offsets_mm) != 6:
            raise SolidWorksError("offsets_mm must be [x+, y+, z+, x-, y-, z-].")
        ok = part.CreateStockBox(name, *[float(v) for v in offsets_mm], True, DEFINE_BY_SOLID, False, False, 0.01)
    _check(app, "the stock")
    if not ok:
        raise SolidWorksError("SolidCAM did not create the stock.")
    return {"ok": True, "stock": part.StockName}


def add_operation(session, template: str, geometry: str | None = None, home: str | None = None) -> dict:
    """Add a job from an operation template (solidcam_status lists them) on a
    geometry of the part (default: the first, usually the target model)."""
    app = _app(session)
    part = _part(app)
    tmpl = _safe(lambda: app.Templates.GetByName(template))
    if tmpl is None:
        raise SolidWorksError(f"No operation template '{template}' (solidcam_status lists them).")
    geoms = _items(part.Geometries, "IGeometry")
    geom = geoms[0] if geometry is None and geoms else next((g for g in geoms if g.Name == geometry), None)
    if geom is None:
        raise SolidWorksError(f"No geometry '{geometry}' (there are: {', '.join(g.Name for g in geoms) or 'none'}).")
    homes = _items(part.Homes, "IHome")
    hm = homes[0] if home is None and homes else next((h for h in homes if h.Name == home), None)
    if hm is None:
        raise SolidWorksError(f"No zero '{home}' (there are: {', '.join(h.Name for h in homes) or 'none'}).")
    sub = _wrap(part.machine, "IMachine").SubMachines.Item(0)
    before = {o["name"] for o in _part_info(part)["operations"]}
    _wrap(tmpl, "ITemplate").CreateOperation(geom, sub, hm)
    _check(app, f"the operation from '{template}'")
    ops = _part_info(part)["operations"]
    new = [o for o in ops if o["name"] not in before]
    if not new:
        raise SolidWorksError(f"SolidCAM added no operation from '{template}'.")
    return {"ok": True, "added": new, "operations": ops}


def calculate(session, operations: list | None = None, force: bool = False) -> dict:
    """Calculate the toolpaths of all operations (or the named ones)."""
    app = _app(session)
    part = _part(app)
    ops = part.Operations
    if operations:
        for name in operations:
            op = _safe(lambda n=name: ops.GetByName(n))
            if op is None:
                raise SolidWorksError(f"No operation '{name}'.")
            _wrap(op, "IOperation").Calculate(bool(force))
    else:
        ops.Calculate(bool(force))
    _check(app, "the calculation")
    rows = _part_info(part)["operations"]
    return {"ok": True, "operations": rows,
            "not_calculated": [r["name"] for r in rows if not r["calculated"] and not r["suppressed"]]}


def gcode(session, operation: str | None = None, path: str | None = None) -> dict:
    """Post-process to G-code: the whole part (into <part folder>\\G-codes) or
    one operation (into path). Reads the program back and says whether it moves."""
    app = _app(session)
    part = _part(app)
    folder = os.path.join(os.path.dirname(part.path), "G-codes")
    before = {f: os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder)} if os.path.isdir(folder) else {}
    if operation:
        op = _safe(lambda: part.Operations.GetByName(operation))
        if op is None:
            raise SolidWorksError(f"No operation '{operation}'.")
        target = os.path.abspath(path or os.path.join(folder, f"{operation}.NC"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _wrap(op, "IOperation").GenerateGCode(target)
        files = [target]
    else:
        part.Operations.GenerateGCode()
        files = [os.path.join(folder, f) for f in (os.listdir(folder) if os.path.isdir(folder) else [])
                 if before.get(f) != os.path.getmtime(os.path.join(folder, f))]
    _check(app, "the G-code")
    out = []
    for f in files:
        if not os.path.isfile(f):
            continue
        with open(f, encoding="latin-1") as fh:
            text = fh.read()
        out.append({"path": f, "size_bytes": os.path.getsize(f), **gcode_check(text)})
    if not out:
        raise SolidWorksError("SolidCAM wrote no G-code file.")
    result = {"ok": True, "files": out}
    if not all(f["has_toolpath"] for f in out):
        result["warning"] = ("A program has no cutting moves (positions 142857 = no point): the jobs' geometry holds "
                             "nothing to machine. Define the job geometry (a chain or faces) in SolidCAM.")
    return result


def save(session, path: str | None = None) -> dict:
    """Save the CAM part (in place, or as path)."""
    app = _app(session)
    part = _part(app)
    if path:
        part.SaveAs(os.path.abspath(path), True, False)
    else:
        part.SaveTo(os.path.dirname(part.path))
    _check(app, "saving")
    return {"ok": True, "path": part.path}


def close(session, close_model: bool = True) -> dict:
    """Close the CAM part (and its model)."""
    app = _app(session)
    part = _part(app)
    name = part.Name
    part.Close2(bool(close_model))
    session._model = None
    _check(app, "closing")
    return {"ok": True, "closed": name}
