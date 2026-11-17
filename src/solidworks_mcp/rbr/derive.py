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
