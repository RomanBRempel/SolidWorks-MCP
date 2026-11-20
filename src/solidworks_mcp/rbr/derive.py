"""Derived parts: a body of one part living on as its own part file (S-12).

A lid, a cover, a print section is modelled as a body of the master part and
then needs its own file - for a drawing, a BOM line, an export - that keeps
following the master. SolidWorks' "Insert into New Part" does that: the new
part starts with one feature (type SavedExtBody, "Stock-<master>-1") holding
an external reference to the body.

- derive_part(body, path): make that part and check it against the body.
- list_external_refs(): what the current part takes from other files, and
  whether each reference is alive (in context / out of context / locked /
  broken / dangling) and its file loaded.
- update_external_refs(): load the referenced files, rebuild, and report each
  body's volume before and after - the proof that a change in the master
  arrived.

Verified on SOLIDWORKS 2025 SP3: IModelDocExtension.ListExternalFileReferences2()
answers nine parallel arrays (model path, component path, feature, data type,
status, referenced entity, feature component, configuration option and name);
status 3 = in context for a body saved from an open master.
"""

import contextlib
import os

from .. import binding
from ..errors import SolidWorksError
from . import bodies as bodies_mod

SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_OPEN_SILENT = 1                      # swOpenDocOptions_Silent
STATUS = {0: "broken", 1: "locked", 3: "in context", 4: "out of context", 5: "dangling"}   # swExternalReferenceStatus_e
DEAD = ("broken", "locked", "dangling")


# --- pure parts (unit-tested) --------------------------------------------------------------------


def rows_from(arrays) -> list:
    """ListExternalFileReferences2's parallel arrays as one dict per reference; pure.
    Tolerates None (no references) and arrays of unequal length."""
    if not arrays:
        return []
    columns = [list(a) if isinstance(a, (list, tuple)) else [] for a in arrays[:9]]
    columns += [[] for _ in range(9 - len(columns))]
    count = max((len(c) for c in columns[:5]), default=0)

    def at(column, i, default=None):
        return column[i] if i < len(column) else default

    rows = []
    for i in range(count):
        code = at(columns[4], i)
        rows.append({"feature": at(columns[2], i), "file": at(columns[0], i),
                     "status": STATUS.get(code, f"status {code}"), "kind": at(columns[3], i),
                     "entity": at(columns[5], i), "configuration": at(columns[8], i) or None})
    return rows


def verdict(rows: list) -> str | None:
    """Why the references will not all update, or None; pure."""
    dead = [r for r in rows if r["status"] in DEAD]
    missing = [r for r in rows if r.get("file_exists") is False]
    notes = []
    if dead:
        notes.append(f"{len(dead)} reference(s) are {', '.join(sorted({r['status'] for r in dead}))} and do not "
                     f"update ({', '.join(str(r['feature']) for r in dead)})")
    if missing:
        notes.append(f"{len(missing)} referenced file(s) are missing ({', '.join(sorted({str(r['file']) for r in missing}))})")
    return "; ".join(notes) or None


def volume_changes(before: dict, after: dict) -> list:
    """Per body: volume before, after and the change; pure. Bodies are matched by name."""
    out = []
    for name in list(dict.fromkeys([*before, *after])):
        b, a = before.get(name), after.get(name)
        row = {"body": name, "before_mm3": b, "after_mm3": a}
        if a is not None and b is not None:
            row["delta_mm3"] = round(a - b, 3)
        out.append(row)
    return out


# --- reading ----------------------------------------------------------------------------------------


@contextlib.contextmanager
def as_current(session, doc):
    """The session's helpers read `session._model`: point them at `doc` for a while. Nothing
    is activated in SolidWorks, so the document in front stays in front."""
    held = session._model
    session._model = doc
    try:
        yield
    finally:
        session._model = held


def _open_by_path(session) -> dict:
    out = {}
    for doc in session._open_documents():
        try:
            path = doc.GetPathName()
            if path:
                out[os.path.normcase(os.path.abspath(path))] = doc
        except Exception:  # noqa: BLE001
            continue
    return out


def _document(session, document: str | None):
    if document is None:
        return session._require_model()
    wanted = str(document).lower()
    for doc in session._open_documents():
        title = doc.GetTitle()
        if title.lower() == wanted or os.path.splitext(title)[0].lower() == wanted:
            return doc
    raise SolidWorksError(f"No open document '{document}' (list_documents shows the titles).")


def _refs(session, model) -> list:
    ext = binding.wrap(model.Extension, session._mod.IModelDocExtension)
    if not int(ext.ListExternalFileReferencesCount() or 0):
        return []
    rows = rows_from(ext.ListExternalFileReferences2())
    loaded = _open_by_path(session)
    for row in rows:
        path = row.get("file")
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(path))
        row["file_exists"] = os.path.isfile(path)
        doc = loaded.get(key)
        row["loaded"] = doc is not None
        if doc is not None:
            try:
                row["source_unsaved_changes"] = bool(doc.GetSaveFlag())
            except Exception:  # noqa: BLE001
                pass
    return rows


def _volumes(session) -> dict:
    out = {}
    for body in bodies_mod._all_bodies(session):
        try:
            out[body.Name] = round(bodies_mod._volume_mm3(body), 3)
        except Exception:  # noqa: BLE001
            out[body.Name] = None
    return out


def list_external_refs(session, document: str | None = None) -> dict:
    """The external references of the current document (or an open one by title): per
    reference the feature that holds it, the file and entity it points to, its status
    and whether that file exists and is loaded."""
    model = _document(session, document)
    rows = _refs(session, model)
    result = {"ok": True, "document": model.GetTitle(), "count": len(rows), "references": rows}
    note = verdict(rows)
    if note:
        result["warning"] = note
    return result


# --- making and updating ---------------------------------------------------------------------------


def derive_part(session, body, path: str, close: bool = False) -> dict:
    """Save a body (or several) of the current part to a new part file that stays linked
    to it ("Insert into New Part"): the new part's first feature holds an external
    reference to the body, so a later change of the body arrives there on rebuild
    (update_external_refs). The current part must be saved. Returns the new part's
    references and volume against the body's; close=True closes the new part again,
    otherwise it stays open in the background and this part stays current."""
    model = session._require_model()
    source_title = model.GetTitle()
    names = bodies_mod.as_names(body)
    source = {name: volume for name, volume in _volumes(session).items() if name in names}
    made = bodies_mod.save_bodies(session, names, path, False)
    new_path = made["path"]
    derived = _open_by_path(session).get(os.path.normcase(new_path))
    result = {"ok": True, "path": new_path, "bodies": made["bodies"], "source": source_title,
              "size_bytes": made["size_bytes"]}
    if derived is None:
        result["warning"] = "The new part was written but is not open: open_part it to check its references."
        return result
    title = derived.GetTitle()
    try:
        session.activate_document(title)
        rows = _refs(session, session._model)
        volumes = _volumes(session)
        result.update(title=title, references=rows, volume_mm3=round(sum(v or 0.0 for v in volumes.values()), 3),
                      source_volume_mm3=round(sum(v or 0.0 for v in source.values()), 3))
        note = verdict(rows)
        if not rows:
            note = "The new part holds no external reference: it will not follow this part."
        if note:
            result["warning"] = note
        if abs(result["volume_mm3"] - result["source_volume_mm3"]) > 1e-3 * max(1.0, result["source_volume_mm3"]):
            result["warning"] = ((result.get("warning") + "; ") if result.get("warning") else "") + \
                "the derived volume differs from the source body's"
        if close:
            session._sw.CloseDoc(title)
            result["closed"] = True
    finally:
        session.activate_document(source_title)
    return result


def update_external_refs(session, load_sources: bool = True) -> dict:
    """Bring the current part up to date with the files it references: load each
    referenced file that is not open (load_sources), rebuild, and report the references'
    status and each body's volume before and after. A reference that is locked or broken
    stays as it is and is named. Sources loaded here stay open in the background."""
    model = session._require_model()
    session._require_part()
    title = model.GetTitle()
    rows = _refs(session, model)
    if not rows:
        raise SolidWorksError(f"'{title}' has no external references (list_external_refs).")
    before = _volumes(session)
    opened, failed = [], []
    if load_sources:
        for path in dict.fromkeys(r["file"] for r in rows if r.get("file") and not r.get("loaded")):
            if not os.path.isfile(path):
                failed.append({"file": path, "why": "the file is missing"})
                continue
            kind = SW_DOC_ASSEMBLY if path.lower().endswith(".sldasm") else SW_DOC_PART
            try:
                got = session._sw.OpenDoc6(path, kind, SW_OPEN_SILENT, "", 0, 0)
                doc = got[0] if isinstance(got, tuple) else got
                if doc is None:
                    failed.append({"file": path, "why": f"SolidWorks did not open it ({got[1:] if isinstance(got, tuple) else got})"})
                else:
                    opened.append(path)
            except Exception as exc:  # noqa: BLE001
                failed.append({"file": path, "why": str(exc)})
        if opened:
            session.activate_document(title)   # OpenDoc6 fronted the source
            model = session._require_model()
    rebuilt = bool(model.ForceRebuild3(False))
    after = _volumes(session)
    rows = _refs(session, model)
    changes = volume_changes(before, after)
    result = {"ok": True, "document": title, "rebuild_ok": rebuilt, "references": rows,
              "bodies": changes, "changed": any(abs(c.get("delta_mm3") or 0.0) > 1e-3 for c in changes)
              or set(before) != set(after),
              "failing_features": session._failing_features()}
    if opened:
        result["sources_loaded"] = opened
    if failed:
        result["sources_not_loaded"] = failed
    note = verdict(rows)
    unsaved = [r["file"] for r in rows if r.get("source_unsaved_changes")]
    if unsaved:
        note = ((note + "; ") if note else "") + ("the source has unsaved changes: this part now follows what is "
                                                  "in memory - save the source too")
    if note:
        result["warning"] = note
    return result


# --- a static copy of a body --------------------------------------------------------------------


def state_changes(states: dict, suppress, unsuppress) -> tuple:
    """(to suppress, to unsuppress) among the named features, given {name: suppressed now};
    pure. A feature already in the wanted state is left out; an unknown name, or one named
    in both lists, is refused."""
    off = [suppress] if isinstance(suppress, str) else list(suppress or ())
    on = [unsuppress] if isinstance(unsuppress, str) else list(unsuppress or ())
    unknown = [n for n in [*off, *on] if n not in states]
    if unknown:
        raise SolidWorksError(f"No feature named {', '.join(repr(n) for n in unknown)} (list_features shows them).")
    both = [n for n in off if n in on]
    if both:
        raise SolidWorksError(f"{', '.join(both)}: named both to suppress and to unsuppress.")
    return [n for n in dict.fromkeys(off) if not states[n]], [n for n in dict.fromkeys(on) if states[n]]


def _suppression(session) -> dict:
    return {feat.Name: bool(feat.IsSuppressed()) for feat in session._history()}


def _set_suppressed(session, names: list, on: bool) -> None:
    """Suppress / unsuppress the named features in one SolidWorks operation."""
    if not names:
        return
    model = session._require_model()
    wanted = set(names)
    feats = [feat for feat in session._history() if feat.Name in wanted]
    model.ClearSelection2(True)
    for feat in feats:
        if not feat.Select2(True, 0):
            model.ClearSelection2(True)
            raise SolidWorksError(f"Could not select feature '{feat.Name}'.")
    done = model.EditSuppress2() if on else model.EditUnsuppress2()
    model.ClearSelection2(True)
    if not done:
        raise SolidWorksError(f"SolidWorks did not {'suppress' if on else 'unsuppress'} {', '.join(names)}.")


def export_static_body(session, body, path: str, suppress=None, unsuppress=None) -> dict:
    """A body (or several) of the current part as a part file of its own WITHOUT a link back:
    a plain solid that no later change of this part reaches - for a print, a hand-over, a
    frozen revision. One call for what took six: the features named in `suppress` /
    `unsuppress` are switched for the moment (a body that a later feature deletes or
    merges exists only with that feature suppressed), the body is saved to `path`, the new
    part's external references are broken, it is saved and closed, and this part is put
    back exactly as it was - the same features suppressed, the same bodies and volumes,
    and saved again only if it had no unsaved changes before.
    The current part must have been saved once (SolidWorks writes the new part from it)."""
    model = session._require_model()
    session._require_part()
    title = model.GetTitle()
    if not model.GetPathName():
        raise SolidWorksError("Save this part first (save_part): SolidWorks writes the new part from its file.")
    names = bodies_mod.as_names(body)
    target = os.path.abspath(path)
    if not target.lower().endswith(".sldprt"):
        target += ".SLDPRT"
    if os.path.exists(target):
        raise SolidWorksError(f"{target} exists; give a new file name.")
    was_modified = bool(model.GetSaveFlag())
    states = _suppression(session)
    volumes_before = _volumes(session)
    to_suppress, to_unsuppress = state_changes(states, suppress, unsuppress)
    result = {"ok": True, "source": title}
    switched = False
    try:
        if to_suppress or to_unsuppress:
            switched = True
            _set_suppressed(session, to_suppress, True)
            _set_suppressed(session, to_unsuppress, False)
            model.ForceRebuild3(False)
        available = _volumes(session)
        missing = [n for n in names if n not in available]
        if missing:
            raise SolidWorksError(
                f"No body {', '.join(repr(n) for n in missing)} in this state of the part (bodies now: "
                f"{', '.join(available) or 'none'}). Name in suppress / unsuppress the features that must be "
                "switched for the body to exist.")
        source_volume = round(sum(available[n] or 0.0 for n in names), 3)
        # the new part is loaded without a window of its own (a window costs SolidWorks about
        # 250 GDI objects) and is never activated: this part stays in front throughout
        session._sw.DocumentVisible(False, SW_DOC_PART)
        try:
            made = bodies_mod.save_bodies(session, names, target, False)
        finally:
            session._sw.DocumentVisible(True, SW_DOC_PART)
        new_doc = _open_by_path(session).get(os.path.normcase(made["path"]))
        if new_doc is None:
            raise SolidWorksError(f"SolidWorks wrote {made['path']} but did not keep it open: its link to this "
                                  "part could not be broken. Delete the file and try again.")
        new_title = new_doc.GetTitle()
        try:
            with as_current(session, new_doc):
                ext = binding.wrap(new_doc.Extension, session._mod.IModelDocExtension)
                ext.BreakAllExternalFileReferences2(False)
                new_doc.ForceRebuild3(False)
                refs = _refs(session, new_doc)
                alive = [r for r in refs if r["status"] not in DEAD]
                volumes = _volumes(session)
                got = new_doc.Save3(1, 0, 0)
            if not (got[0] if isinstance(got, tuple) else got):
                raise SolidWorksError(f"SolidWorks did not save {made['path']} (answer {got}).")
            result.update(path=made["path"], bodies=made["bodies"], size_bytes=os.path.getsize(made["path"]),
                          volume_mm3=round(sum(v or 0.0 for v in volumes.values()), 3),
                          source_volume_mm3=source_volume, references_broken=len(refs) - len(alive),
                          static=not alive)
            if alive:
                result["warning"] = (f"{len(alive)} external reference(s) are still alive "
                                     f"({', '.join(str(r['feature']) for r in alive)}): the part still follows "
                                     "this one.")
            if abs(result["volume_mm3"] - source_volume) > 1e-3 * max(1.0, source_volume):
                result["warning"] = ((result.get("warning") + "; ") if result.get("warning") else "") + \
                    "the exported volume differs from the body's"
        finally:
            try:
                session._sw.CloseDoc(new_title)
            except Exception:  # noqa: BLE001
                pass
    finally:
        if switched:
            model = session._require_model()
            now = _suppression(session)
            _set_suppressed(session, [n for n, was in states.items() if was and now.get(n) is False], True)
            _set_suppressed(session, [n for n, was in states.items() if not was and now.get(n) is True], False)
            model.ForceRebuild3(False)
    model = session._require_model()
    left = {n: s for n, s in _suppression(session).items() if states.get(n) != s}
    volumes_after = _volumes(session)
    same = not left and volumes_after == volumes_before
    source = {"unchanged": same, "had_unsaved_changes": was_modified}
    if left:
        source["suppression_differs"] = left
    if volumes_after != volumes_before:
        source["bodies"] = volume_changes(volumes_before, volumes_after)
    if switched and same and not was_modified:
        got = model.Save3(1, 0, 0)      # it was clean before: it is clean again, with the same content
        source["saved_again"] = bool(got[0] if isinstance(got, tuple) else got)
    result["source_part"] = source
    if not same:
        result["ok"] = False
        result["error"] = ("The body was exported, but this part is not as it was (source_part shows the "
                           "difference): check it before saving.")
    return result
