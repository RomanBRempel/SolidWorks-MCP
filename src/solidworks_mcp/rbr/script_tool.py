"""run_script: execute a whole batch of SolidWorks API work in ONE MCP call.

Runs on the server's single COM thread inside SolidWorksSession.run_guarded, so
the UI stays frozen (no redraw, no feature-tree update), CommandInProgress is
set, and a failed script rolls back what it added to the current part.

Every run is written to the journal with its full code (rbr/journal.py), and
the result carries the feature-tree change, so what the script did is visible.
"""

import contextlib
import json
import time
import traceback

import pythoncom
import win32com.client

from .. import binding
from ..constants import RAY_HIT_WIDTH, SW_RAY_NORMALS_ENTRY_EXIT, SW_RELATIONS_ALL
from ..errors import SolidWorksError
# `features` is aliased: namespace() has a local helper of the same name, which
# would shadow the module inside the lambdas that need features_tools.extrude/cut.
from . import features as features_tools
from . import geom, journal, offset_loop, param, sketch_edit

# Base class of every makepy-generated wrapper; absent in the test stubs of win32com.
_DISPATCH_BASE = getattr(win32com.client, "DispatchBaseClass", None)

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


def api(obj, contains: str = "") -> dict:
    """What an early-bound SolidWorks object offers: its interface, methods and properties.

    Use it before guessing a name (SW 2025 lacks some members the online help lists,
    e.g. ILoftFeatureData.StartConstraint). A raw dispatch has no list: wrap() it first.
    """
    cls = type(obj)
    props = set(getattr(cls, "_prop_map_get_", {}) or {}) | set(getattr(cls, "_prop_map_put_", {}) or {})
    methods = {n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n, None))} - props
    needle = contains.lower()
    pick = (lambda names: sorted(n for n in names if needle in n.lower()))
    return {"interface": cls.__name__, "methods": pick(methods), "properties": pick(props),
            "note": None if props or methods else "raw dispatch: wrap(obj, mod.IXxx) first"}


class Namespace(dict):
    """The globals of a script, with the document names looked up when they are READ.

    `model`, `active`, `ext`, `fm`, `selmgr`, `asm` and `part` used to be a
    snapshot taken before the script ran. A script that closed, reloaded or
    opened documents went on with objects of documents that were gone, and its
    next use of one - also inside a helper such as ui_unlocked() - died with
    RPC_E_DISCONNECTED (2026-11-19, after swApp.CloseAllDocuments). Now each
    of those names is resolved at the moment of use: `live` maps the name to a
    function returning the object for the document that is current then (it
    raises KeyError where the name does not apply, e.g. `asm` in a part, which
    Python reports as an undefined name, as before). A script that assigns to
    such a name itself keeps its own value from then on.

    exec() honours __getitem__ of a dict SUBCLASS for global and name lookups
    (the fast path is taken for exact dicts only); tests/rbr pins that.
    """

    def __init__(self, live: dict):
        super().__init__()
        self._live = dict(live)

    def __getitem__(self, key):
        resolver = self._live.get(key)
        if resolver is not None:
            return resolver()
        return dict.__getitem__(self, key)

    def __setitem__(self, key, value):
        self._live.pop(key, None)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if self._live.pop(key, None) is None:
            dict.__delitem__(self, key)

    def __contains__(self, key):
        return key in self._live or dict.__contains__(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def live_documents(session):
    """(current, resolvers) for a script namespace.

    current() is the document the tools work on NOW: the server's current one
    while it still answers, else SolidWorks' active one. A current document
    that was closed behind the server's back (by the script itself) is dropped
    on the way, once, instead of failing every later use."""
    sw, mod = session._sw, session._mod

    def active():
        return binding.wrap(sw.ActiveDoc, mod.IModelDoc2)

    def current():
        held = session._model
        if held is not None:
            try:
                held.GetTitle()
                return held
            except pythoncom.com_error as exc:
                if getattr(exc, "hresult", None) not in session._GONE_HRESULTS:
                    raise
                session._model = None
        return active()

    def of(interface, attribute=None, doc_type=None):
        def resolve():
            doc = current()
            if doc is None or (doc_type is not None and int(doc.GetType()) != doc_type):
                raise KeyError(interface)
            return binding.wrap(getattr(doc, attribute) if attribute else doc, getattr(mod, interface))
        return resolve

    return current, {
        "model": current, "active": active,
        "ext": of("IModelDocExtension", "Extension"), "selmgr": of("ISelectionMgr", "SelectionManager"),
        "fm": of("IFeatureManager", "FeatureManager"),
        "asm": of("IAssemblyDoc", doc_type=SW_DOC_ASSEMBLY), "part": of("IPartDoc", doc_type=SW_DOC_PART),
    }


def namespace(session, out: list, args: dict | None = None) -> dict:
    """The names a script sees (documented in the run_script tool)."""
    sw = session._ensure()
    mod = session._mod
    current, live = live_documents(session)
    first = current()
    if first is not None and session._quiet_depth:
        try:
            session._quiet(first)
        except Exception:  # noqa: BLE001
            pass
    del first  # nothing below may hold a document: every helper asks current()

    def _print(*items, sep=" ", end="\n", **_):
        out.append(sep.join(str(a) for a in items) + end)

    ns = Namespace(live)
    ns.update({
        "sw": sw, "swApp": sw, "mod": mod, "wrap": binding.wrap, "session": session,
        "SolidWorksError": SolidWorksError, "print": _print, "result": None,
        "MM": 0.001, "DEG": 3.141592653589793 / 180.0, "args": dict(args or {}),
    })

    def comp(name):
        """Component of the current assembly by list_components name."""
        try:
            assembly = live["asm"]()
        except KeyError:
            raise SolidWorksError("comp() needs an assembly as the current document.") from None
        return session._component_by_name(assembly, name)

    def W(obj, iface=None):
        """Early-bind a raw or late-bound dispatch (or a list of them) so the API behaves.

        Items out of Get* collections (GetSketchSegments, GetRelations, GetFaces,
        GetEdges, ...) come back late-bound: on those, methods resolve as
        properties (seg.GetType() dies with "'int' object is not callable") and
        Select4 rejects its arguments with a type mismatch. W(x, 'ISketchSegment')
        (or W(x, mod.ISketchSegment)) re-wraps them as the generated class. Name
        the interface: SolidWorks 2025 exposes no type info on these objects, so
        bare W(x) can only pass through what is already bound, and is loud
        otherwise. api(obj) lists what a wrapped object really offers.
        """
        if obj is None:
            return None
        if isinstance(obj, (list, tuple)):
            return [W(x, iface) for x in obj]
        if isinstance(iface, str):
            cls = getattr(mod, iface, None)
            if cls is None:
                raise SolidWorksError(f"The type library has no interface named '{iface}' (see mod).")
            iface = cls
        if iface is not None:
            return binding.wrap(obj, iface)
        if _DISPATCH_BASE is not None and isinstance(obj, _DISPATCH_BASE):
            return obj  # already early-bound
        oleobj = getattr(obj, "_oleobj_", obj)
        name = None
        try:
            name = oleobj.GetTypeInfo().GetDocumentation(-1)[0]
        except Exception:  # noqa: BLE001 - some SW objects expose no type info
            pass
        cls = getattr(mod, name, None) or getattr(mod, f"I{name}", None) if name else None
        if cls is None:
            raise SolidWorksError(
                "The object exposes no usable type info"
                + (f" ('{name}' is not in the type library)" if name else "")
                + ": name the interface, W(x, 'ISketchSegment') or wrap(x, mod.ISketchSegment).")
        return binding.wrap(obj, cls)

    def call(obj, name, *call_args):
        """Read or invoke a member of a still-raw dispatch, method or property alike.

        Late binding resolves some methods as properties (obj.GetType is already
        the int); call(obj, 'GetType') works either way. Prefer W(obj) and the
        documented API when the interface is known.
        """
        attr = getattr(obj, name)
        return attr(*call_args) if callable(attr) else attr

    def _sketch_of(which):
        """An early-bound ISketch: None = the sketch being edited, a name, or the object."""
        if which is None:
            return sketch_edit.active_sketch(session)[2]
        if isinstance(which, str):
            sk = binding.wrap(feature(which).GetSpecificFeature2(), mod.ISketch)
            if sk is None:
                raise SolidWorksError(f"'{which}' is not a sketch.")
            return sk
        return binding.wrap(which, mod.ISketch)

    def segments(sketch=None, construction=False):
        """Early-bound ISketchSegment list of a sketch (default: the one being edited).

        sketch: None = the active sketch (loud when none is being edited); a
        sketch's tree name (works without editing it); or an ISketch. By default
        construction geometry is left out; construction=True returns only it,
        construction=None everything. Unlike raw GetSketchSegments() items these
        behave as documented: GetType(), GetID(), Select4(append, data).
        """
        segs = [binding.wrap(s, mod.ISketchSegment)
                for s in (_sketch_of(sketch).GetSketchSegments() or ())]
        if construction is None:
            return segs
        return [s for s in segs if bool(s.ConstructionGeometry) == bool(construction)]

    def relations(sketch=None):
        """Early-bound ISketchRelation list of a sketch (default: the one being edited)."""
        manager = binding.wrap(_sketch_of(sketch).RelationManager, mod.ISketchRelationManager)
        return [binding.wrap(r, mod.ISketchRelation)
                for r in (manager.GetRelations(SW_RELATIONS_ALL) or ())]

    def select(objs, append=False):
        """Select components, entities or sketch segments; returns the count.

        Uses Select4 with the document's SelectData and either signature
        (components take three arguments, entities and segments two); raw
        dispatches are early-bound first, so items straight out of a Get*
        collection select fine.
        """
        if not append:
            current().ClearSelection2(True)
        data = live["selmgr"]().CreateSelectData()
        count = 0
        for o in objs:
            try:
                o = W(o)
            except SolidWorksError:
                pass  # no type info: try it as it came
            try:
                ok = o.Select4(True, data, False)
            except TypeError:
                ok = o.Select4(True, data)
            count += 1 if ok else 0
        return count

    def feature(name):
        """A feature of the current document by its tree name (early-bound IFeature)."""
        for f in session._iter_features():
            if f.Name == name:
                return f
        raise SolidWorksError(f"No feature named '{name}'.")

    def features(type_name: str | None = None):
        """Top-level features of the current document (one COM call for the list), optionally of one type."""
        out_ = list(session._iter_features())
        return [f for f in out_ if type_name is None or f.GetTypeName2() == type_name]

    def vec(*values):
        """A VARIANT(VT_ARRAY|VT_R8) for CreateSpline2, CreatePoint and friends.

        A plain Python list reaches those APIs as None or garbage (1e-311);
        this is the marshalling they actually need. Nested lists flatten.
        """
        flat = []
        for v in values:
            if isinstance(v, (list, tuple)):
                flat.extend(float(x) for x in v)
            else:
                flat.append(float(v))
        return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat)

    def pt(x, y, z):
        """An IMathPoint at (x, y, z) metres, via MathUtility with the right VARIANT."""
        mu = binding.wrap(sw.GetMathUtility(), mod.IMathUtility)
        return binding.wrap(mu.CreatePoint(vec(x, y, z)), mod.IMathPoint)

    @contextlib.contextmanager
    def ui_unlocked():
        """Temporarily lift CommandInProgress and the graphics freeze.

        InsertSketch/EditSketch refuse to open a sketch while the script's UI
        freeze is on; wrap just those calls, the freeze comes back after.
        """
        was_busy = bool(sw.CommandInProgress)
        if was_busy:
            sw.CommandInProgress = False
        doc = current()
        view = binding.wrap(doc.ActiveView, mod.IModelView) if doc is not None else None
        was_still = view is not None and not view.EnableGraphicsUpdate
        if was_still:
            view.EnableGraphicsUpdate = True
        try:
            yield
        finally:
            if was_still:
                try:
                    view.EnableGraphicsUpdate = False
                except pythoncom.com_error:
                    pass  # the block closed that document: nothing left to keep still
            if was_busy:
                sw.CommandInProgress = True

    def tree_before():
        """A snapshot for features_added / rename_new, taken before the risky call."""
        return journal.tree_state(session, names=True)

    def features_added(before):
        """Names of the features added since the tree_before() snapshot, in tree order -
        also when they landed mid-tree, above the rollback bar."""
        return journal.tree_added(session, before, journal.tree_state(session, names=True))

    def rename_new(before, name):
        """Rename the newest feature added since `before` (a tree_before() snapshot).

        Setting .Name on what InsertRefPlane itself returned fails with a type
        mismatch; the feature re-fetched from the tree renames fine.
        """
        added = features_added(before)
        if not added:
            raise SolidWorksError("No feature was added since the snapshot; nothing to rename.")
        feat = next((f for f in session._iter_features() if f.Name == added[-1]), None)
        if feat is None:
            raise SolidWorksError(f"The added feature '{added[-1]}' is not in the tree any more.")
        feat.Name = name
        return {"renamed": added[-1], "to": name}

    def combine(operation, main, tools=None, name="Combine"):
        """Join/subtract/intersect bodies through the proven combine_bodies path
        ('add'/'subtract'/'common'); InsertCombineFeature called raw from a script fails."""
        return session.combine_bodies(operation, main, tools, name)

    def new_part():
        """A new part (MMGS) that becomes the current document; returns its IModelDoc2.

        swApp.NewDocument(GetUserPreferenceStringValue(8), 0, 0, 0) from a script
        answers None (the template path comes back empty); session.new_part reads
        and checks the default template and falls back without a modal dialog.
        """
        session.new_part()
        return session._model

    def activate(title):
        """Make an open document current by its title; returns its IModelDoc2.

        Raw ActivateDoc3 answers a (doc, errors) tuple - calling methods on that
        tuple fails - and does not update the server's current document; this
        goes through session.activate_document (title with or without the
        extension) and hands back the early-bound model.
        """
        session.activate_document(title)
        return session._model

    def rays(bodies, points, directions, options=SW_RAY_NORMALS_ENTRY_EXIT):
        """Cast rays against solid bodies and return every hit.

        bodies: IBody2 objects or body names; None = every solid body of the
        current part. points / directions are METRES in model space, nested
        [[x, y, z], ...] or flat, one direction per point. RayIntersections
        wants the bodies as VARIANT(VT_ARRAY | VT_DISPATCH) - a plain Python
        list dies with 'Memory is locked' - and the doubles as VT_ARRAY | VT_R8;
        this does both. options: swRayPtsOpts_e (default: normals + entry/exit).
        Returns [{ray, body, type, point_m, normal}]; type bit 32 = the ray
        LEAVES the body there (SW_RAY_HIT_EXIT).
        """
        target = session._require_model()
        if bodies is None:
            chosen = geom._solid_bodies(session, target)
        else:
            chosen = [geom.resolve_body(session, target, b)
                      for b in (bodies if isinstance(bodies, (list, tuple)) else [bodies])]
        if not chosen:
            raise SolidWorksError("No solid body to cast rays against.")

        def _flat(values, what):
            flat_ = []
            for v in values:
                if isinstance(v, (list, tuple)):
                    flat_.extend(float(c) for c in v)
                else:
                    flat_.append(float(v))
            if not flat_ or len(flat_) % 3:
                raise SolidWorksError(f"{what} must be [x, y, z] triples in metres (got {len(flat_)} values).")
            return flat_

        pts, dirs = _flat(points, "points"), _flat(directions, "directions")
        if len(pts) != len(dirs):
            raise SolidWorksError(f"{len(pts) // 3} point(s) against {len(dirs) // 3} direction(s).")
        dispatches = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                                             [b._oleobj_ for b in chosen])
        count = int(target.RayIntersections(dispatches, vec(pts), vec(dirs), int(options), 0.0, 0.0) or 0)
        rows = target.GetRayIntersectionsPoints() if count else ()
        hits = []
        for h in range(count):
            row = rows[h * RAY_HIT_WIDTH:(h + 1) * RAY_HIT_WIDTH]
            body_index = int(row[0])
            hits.append({"ray": int(row[1]),
                         "body": chosen[body_index].Name if 0 <= body_index < len(chosen) else body_index,
                         "type": int(row[2]), "point_m": [float(c) for c in row[3:6]],
                         "normal": [float(c) for c in row[6:9]]})
        return hits

    from . import batch, cavity, query, section, sketch_datum, sweep, tree   # here: they import this package's parts

    def body_box(body=None):
        """A body's TIGHT box in mm ({min_mm, max_mm, size_mm}) from its extreme points; body:
        an IBody2, a list_bodies name, or None for the first solid body. IBody2.GetBodyBox
        and IPartDoc.GetPartBox run loose round curved faces (10.004 for a 10 mm loft)."""
        target = session._solid_body() if body is None else geom.resolve_body(session, session._require_model(), body)
        return session._body_box(target)

    def new_assembly():
        """A new assembly that becomes the current document; returns its IModelDoc2 (raw
        NewDocument gets a template name without its folder on some installations)."""
        session.new_assembly()
        return session._model

    ns.update(
        body_box=body_box, new_assembly=new_assembly,
        # the ways of filling a tree folder that answer True and move nothing: see rbr/tree.py
        tree_folder=lambda name, components: tree.tree_folder(session, name, components),
        tree_folders=lambda: tree.list_tree_folders(session),
        # AddComponent5's own x, y, z centre the component's BOX there; this puts its ORIGIN there
        insert_component=lambda path, x_mm=0.0, y_mm=0.0, z_mm=0.0, fixed=None: session.insert_component(
            path, x_mm, y_mm, z_mm, fixed),
        find_faces=lambda **kw: query.find_faces(session, **kw),
        find_edges=lambda **kw: query.find_edges(session, **kw),
        entity=lambda ref, kind=None: query.entity(session, ref, kind),
        section_profile=lambda point_mm, normal="z", bodies=None: section.section_profile(
            session, point_mm, normal, bodies),
        read_sketches=lambda names=None, dimensions=True: batch.read_sketches(session, names, dimensions),
        set_dimensions=lambda values: batch.set_dimensions(session, values),
        add_sketch3d=lambda points_mm, **kw: sweep.add_sketch3d(session, points_mm, **kw),
        sweep_cut=lambda path, **kw: sweep.sweep_cut(session, path, **kw),
        sweep_boss=lambda path, **kw: sweep.sweep_boss(session, path, **kw),
        fully_define_sketch=lambda sketch, **kw: sketch_datum.fully_define_sketch(session, sketch, **kw),
        add_dimension=lambda sketch, entities, **kw: sketch_datum.add_dimension(session, sketch, entities, **kw),
        sketch_fillet=lambda sketch, corners, radius_mm, **kw: sketch_datum.sketch_fillet(
            session, sketch, corners, radius_mm, **kw),
        sketch_intersection=lambda **kw: cavity.sketch_intersection(session, **kw),
        sketch_trim_region=lambda sketch, keep_box, max_trims=80: cavity.sketch_trim_region(
            session, sketch, keep_box, max_trims),
        sketch_offset_chain=lambda sketch, distance_mm, **kw: cavity.sketch_offset_chain(
            session, sketch, distance_mm, **kw),
        split_line_project=lambda sketch, faces, **kw: cavity.split_line_project(session, sketch, faces, **kw),
        loft_cut=lambda sketches, **kw: cavity.loft_cut(session, sketches, **kw),
        cut_offset_from_surface=lambda sketch, face, offset_mm, toward, **kw: cavity.cut_offset_from_surface(
            session, sketch, face, offset_mm, toward, **kw))
    ns.update(comp=comp, select=select, feature=feature, features=features, api=api, swapp=sw,
              W=W, call=call, segments=segments, relations=relations,
              vec=vec, pt=pt, ui_unlocked=ui_unlocked, tree_before=tree_before,
              features_added=features_added, rename_new=rename_new, combine=combine,
              new_part=new_part, activate=activate, rays=rays,
              export=lambda path, file_format=None, **kw: session.export(path, file_format, **kw),
              split_segment=lambda segment, point_mm, space="model": sketch_edit.split_segment(
                  session, segment, point_mm, space),
              trim_to_intersection=lambda segment, pick_mm, space="model": sketch_edit.trim_to_intersection(
                  session, segment, pick_mm, space),
              set_global=lambda name, value: param.set_global(session, name, value),
              link_dimension=lambda dimension, expression: param.link_dimension(session, dimension, expression),
              feature_dimensions=lambda feature=None: param.feature_dimensions(session, feature),
              sketch_circles=lambda on, circles, name=None, profile_face=None, **kw: param.sketch_circles(
                  session, on, circles, name, profile_face, **kw),
              sketch_rect=lambda on, corner_mm, width_mm, height_mm, name=None, vars=None, **kw: param.sketch_rect(
                  session, on, corner_mm, width_mm, height_mm, name, vars, **kw),
              convert_entities=lambda **kw: param.convert_entities(session, **kw),
              add_relation=lambda sketch, kind, entities: param.add_relation(session, sketch, kind, entities),
              offset_face_loop=lambda face, distance_mm, inward=True, link_to=None, name=None:
                  offset_loop.offset_face_loop(session, face, distance_mm, inward, link_to, name),
              extrude_toward=lambda sketch, toward, **kw: features_tools.extrude(session, sketch, toward, **kw),
              cut_toward=lambda sketch, toward, **kw: features_tools.cut(session, sketch, toward, **kw),
              plane_normal_to_line=lambda sketch, segment, at="start", name=None: param.plane_normal_to_line(
                  session, sketch, segment, at, name),
              mirror_features=lambda feats, plane, name="Mirror": param.mirror_features(session, feats, plane, name),
              mirror_body=lambda body, plane, merge=True: geom.mirror_body(session, body, plane, merge),
              whats_wrong=lambda: geom.whats_wrong(session))
    try:
        import numpy as np  # optional: present when installed with `--with numpy`
        ns["np"] = np
    except ImportError:
        pass
    return ns


# --- what a script made, and undoing exactly that (S-11) ------------------------------------------


def made_state(session) -> dict | None:
    """What exists before a script runs: the current part's features by ID and its
    equations. None when there is no part (nothing can be undone then).

    Features are told apart by IFeature.GetID, not by name: a script that renames an
    existing feature has not created one, and "every name that was not there before"
    used to delete it on failure."""
    try:
        model = session._model
        if model is None or int(model.GetType()) != 1:  # swDocPART
            return None
        state = {"title": model.GetTitle(), "ids": {int(f.GetID()): f.Name for f in session._history()}}
    except Exception:  # noqa: BLE001 - no reliable picture: the caller falls back to the name snapshot
        return None
    try:
        state["equations"] = [e["equation"] for e in session._equation_entries()]
    except Exception:  # noqa: BLE001
        state["equations"] = None
    return state


def made_since(before: dict, now: dict) -> dict:
    """What a script did to the tree and the equations, from two made_state pictures; pure.
    created: feature names new by ID; renamed: [{was, now}]; equations_added /
    equations_lost: texts that appeared / are gone (a changed equation is both)."""
    created = [name for fid, name in now["ids"].items() if fid not in before["ids"]]
    renamed = [{"was": before["ids"][fid], "now": name} for fid, name in now["ids"].items()
               if fid in before["ids"] and before["ids"][fid] != name]
    old, new = before.get("equations"), now.get("equations")
    added = [t for t in new if t not in old] if old is not None and new is not None else []
    lost = [t for t in old if t not in new] if old is not None and new is not None else []
    return {"created": created, "renamed": renamed, "equations_added": added, "equations_lost": lost}


def _close_open_sketch(session) -> str | None:
    """Leave a sketch the script left open (every later tool would draw into it)."""
    try:
        model = session._model
        sketch = model.GetActiveSketch2() if model is not None else None
        if sketch is None:
            return None
        name = None
        try:
            name = binding.wrap(sketch, session._mod.IFeature).Name
        except Exception:  # noqa: BLE001
            pass
        model.ClearSelection2(True)
        binding.wrap(model.SketchManager, session._mod.ISketchManager).InsertSketch(True)
        return name or "a sketch"
    except Exception:  # noqa: BLE001
        return None


def _undo_equations(session, before: list, change: dict) -> dict:
    """Put the equations back as they were: delete the added ones, restore the lost ones."""
    report = {"removed": [], "restored": [], "failed": []}
    eqmgr = session._equation_mgr()
    lost_names = {session._equation_lhs(t).lower() for t in change["equations_lost"]}
    for text in change["equations_added"]:
        try:
            if session._equation_lhs(text).lower() in lost_names:
                continue  # a changed equation: set back below, in place
            index = next((i for i in range(eqmgr.GetCount()) if eqmgr.Equation(i) == text), None)
            if index is not None and eqmgr.Delete(index) >= 0:
                report["removed"].append(text)
            else:
                report["failed"].append(text)
        except Exception:  # noqa: BLE001
            report["failed"].append(text)
    if change["equations_lost"]:
        try:
            session.set_equations(list(change["equations_lost"]))
            report["restored"] = list(change["equations_lost"])
        except Exception as exc:  # noqa: BLE001
            report["failed"] += [f"{t} ({exc})" for t in change["equations_lost"]]
    elif report["removed"]:
        try:
            session._require_model().ForceRebuild3(False)
        except Exception:  # noqa: BLE001
            pass
    return report


def after_failure(session, before: dict | None, rollback: bool) -> dict:
    """After a script raised: undo what it created (rollback) or report what stays."""
    report = {}
    closed = _close_open_sketch(session)
    if closed:
        report["closed_open_sketch"] = closed
    now = made_state(session)
    if before is None or now is None or now["title"] != before["title"]:
        report["rolled_back"] = False
        report["note"] = ("Nothing was undone: the script started without a current part, or switched "
                          "documents.")
        return report
    change = made_since(before, now)
    if change["renamed"]:
        report["renamed"] = change["renamed"]
    if not rollback:
        report["rolled_back"] = False
        report["kept"] = {"features": change["created"], "equations": change["equations_added"]}
        if change["equations_lost"]:
            report["kept"]["equations_changed_or_deleted"] = change["equations_lost"]
        return report
    report["rolled_back"] = True
    left = []
    if change["created"]:
        keep = {name for fid, name in now["ids"].items() if fid in before["ids"]}
        left = list(session._remove_features_since(keep))
    report["removed"] = {"features": [n for n in change["created"] if n not in left]}
    if left:
        report["could_not_remove"] = left
    if before.get("equations") is not None and (change["equations_added"] or change["equations_lost"]):
        report["removed"]["equations"] = _undo_equations(session, before["equations"], change)
    return report


RPC_E_DISCONNECTED = -2147417848
STALE_HINT = ("an object the script was holding is gone: its document was closed, reloaded or replaced earlier "
              "in the script (CloseDoc, CloseAllDocuments, ReloadOrReplace, close_part). model, active, part, asm, "
              "ext, fm and selmgr always mean the document that is current now - use them, or fetch your own "
              "references again (wrap(swApp.ActiveDoc, mod.IModelDoc2), activate(title)) after closing documents")


def _stale_reference(session, exc) -> bool:
    """RPC_E_DISCONNECTED while SolidWorks itself still answers: one object died, not the link.
    The script's mistake, to be told as such - not a fault that distrusts the whole session."""
    if getattr(exc, "hresult", None) != RPC_E_DISCONNECTED:
        return False
    try:
        session._sw.CommandInProgress
    except Exception:  # noqa: BLE001 - the link is dead: a real fault
        return False
    try:  # a current document that went the same way is dropped, so the clean-up works on what exists
        if session._model is not None:
            session._model.GetTitle()
    except Exception:  # noqa: BLE001
        session._model = None
    return True


def close_all_objections(documents: list, owners: dict, own_pid: int) -> list:
    """Why CloseAllDocuments must not run: the open documents it would take from someone else; pure.

    documents: [{title, modified}]; owners: guard.owners() (title -> {pid}). A document of
    another live server process, and unsaved changes this server did not make (the user's own
    work in the same SolidWorks), are not a script's to throw away."""
    out = []
    for doc in documents:
        title = doc.get("title")
        owner = (owners.get(title) or {}).get("pid")
        if owner not in (None, own_pid):
            out.append(f"'{title}' (in use by server process {owner})")
        elif doc.get("modified") and owner != own_pid:
            out.append(f"'{title}' (unsaved changes this server did not make)")
    return out


def _check_close_all(session, code: str) -> None:
    """A script that calls CloseAllDocuments is refused BEFORE it runs when that would discard
    someone else's documents (see close_all_objections)."""
    if "CloseAllDocuments" not in code:
        return
    import os
    from . import guard
    try:
        documents = [{"title": d.GetTitle(), "modified": bool(d.GetSaveFlag())} for d in session._open_documents()]
        blocked = close_all_objections(documents, guard.prune_owners(), os.getpid())
    except pythoncom.com_error:
        return
    if blocked:
        shown = ", ".join(blocked[:8]) + (f" and {len(blocked) - 8} more" if len(blocked) > 8 else "")
        raise SolidWorksError(
            f"The script calls CloseAllDocuments, which would close {len(blocked)} document(s) that are not "
            f"this server's to discard: {shown}. Nothing was run. Close the documents you mean one by one "
            "(swApp.CloseDoc(title), or activate(title) + session.close_part()), or use "
            "close_hidden_documents(); CloseAllDocuments is accepted when only saved documents and this "
            "server's own ones are open.")


def _fault(exc) -> bool:
    """A COM error after which SolidWorks' state cannot be trusted: the guard must see it."""
    from ..session import SolidWorksSession
    from . import guard
    hresult = getattr(exc, "hresult", None)
    return hresult is not None and (hresult in guard.FAULTS or hresult in SolidWorksSession._GONE_HRESULTS)


def run_script(session, code: str, args: dict | None = None, rollback_on_error: bool = True) -> dict:
    """Run `code` in the script namespace. When it raises:

    rollback_on_error=True (default): the features the script CREATED - told by feature
    ID, so a renamed feature is not one of them - and the equations it added or changed
    are undone, and the error is raised (the caller sees ok false).
    rollback_on_error=False: nothing is undone; the reply is {ok: false, error, kept:
    {features, equations}} - the finished steps stay, the script can be continued."""
    out = []
    _check_close_all(session, code)
    ns = namespace(session, out, args)
    before = journal.tree_state(session, names=True)
    made_before = made_state(session)
    t0 = time.perf_counter()
    try:
        exec(compile(code, "<run_script>", "exec"), ns)  # noqa: S102 - the point of the tool
    except Exception as exc:  # noqa: BLE001
        stale = _stale_reference(session, exc)
        if not stale and _fault(exc):
            raise
        if isinstance(exc, SolidWorksError):
            message = str(exc)
        else:
            # the script's own frames: the exec frame of this module says nothing
            frames = exc.__traceback__.tb_next if exc.__traceback__ is not None else None
            tb = "".join(traceback.format_exception(type(exc), exc, frames, limit=6))
            message = f"Script failed: {type(exc).__name__}: {exc}\n{tb}"
            if stale:
                message = f"Script failed: {STALE_HINT}.\n{tb}"
        stdout = "".join(out)[-4000:]
        try:
            report = after_failure(session, made_before, bool(rollback_on_error))
        except Exception as undo_exc:  # noqa: BLE001 - the script's own error is what matters
            report = {"rolled_back": False, "note": f"Undoing failed: {type(undo_exc).__name__}: {undo_exc}"}
        if not rollback_on_error:
            return {"ok": False, "error": message, "elapsed_s": round(time.perf_counter() - t0, 3),
                    "stdout": stdout, **report,
                    "hint": "rollback_on_error=False: what the script finished before the error is kept "
                            "(see kept); fix the failing step and run the rest."}
        undone = report.get("removed") or {}
        tail = ""
        if undone.get("features") or undone.get("equations"):
            tail = f"\nRolled back: {undone}."
        if report.get("could_not_remove"):
            tail += f"\nCould not remove: {report['could_not_remove']}."
        if report.get("closed_open_sketch"):
            tail += f"\nLeft the sketch the script had open: {report['closed_open_sketch']}."
        raise SolidWorksError(f"{message}{tail}\nstdout:\n{stdout}") from None
    elapsed = round(time.perf_counter() - t0, 3)
    after = journal.tree_state(session, names=True)
    stdout = "".join(out)
    res = {
        "ok": True,
        "elapsed_s": elapsed,
        "result": _jsonable(ns.get("result")),
        "stdout": stdout[-MAX_OUT:],
        "stdout_truncated": len(stdout) > MAX_OUT,
    }
    if before and after and before["title"] == after["title"]:
        added = journal.tree_added(session, before, after)
        if added or before["count"] != after["count"]:
            res["features"] = {"before": before["count"], "after": after["count"], "added": added}
            gone = set(after.get("names") or ())
            removed = [n for n in before.get("names") or () if n not in gone]
            if removed:
                res["features"]["removed"] = removed[:50]
    return res
