"""rebuild_chain: carry a change of a master part through the parts derived from it.

A derived part (derive_part, "Insert into New Part") takes its body from its
master only while the master is open, and a part derived from THAT one only
after the first has been rebuilt: fuselage -> sections, wing -> roots -> ribs
were opened, rebuilt and saved by hand, one by one, in the right order.

rebuild_chain(root) finds the parts that reference root - directly or through
other parts - among the part files of a folder, without opening anything
(ISldWorks.GetDocumentDependencies2 reads a closed file's references), puts
them in dependency order, and for each: loads it without a window if it is
not open, rebuilds it (its sources are open by then), reads every body's
volume before and after, saves it, and closes it again if it was loaded here.
No document is activated: the user's current document stays in front.

Verified on SOLIDWORKS 2025 SP3 (2026-11-20): GetDocumentDependencies2 on a
closed derived part answers [name, path] pairs with its master's path.
"""

import os
import time

from .. import binding
from ..errors import SolidWorksError
from . import assembly, bodies as bodies_mod, derive, guard

SW_DOC_PART = 1
SW_SAVE_SILENT = 1
MAX_FILES = 400


# --- pure parts (unit-tested) --------------------------------------------------------------------


def key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def paths_of(dependencies) -> list:
    """The paths in GetDocumentDependencies2's answer ([name, path, name, path, ...]); None = none."""
    items = list(dependencies or ())
    return [str(items[i]) for i in range(1, len(items), 2) if items[i]]


def dependents(graph: dict, root: str) -> set:
    """The files that reference `root` directly or through others. graph: {file: [files it references]}."""
    found, grew = set(), True
    while grew:
        grew = False
        for file, sources in graph.items():
            if file != root and file not in found and any(s == root or s in found for s in sources):
                found.add(file)
                grew = True
    return found


def in_order(graph: dict, members) -> list:
    """`members` with every file after the members it references (sources first); a cycle
    is refused. Ties keep the alphabetical order, so the result is the same every time."""
    members = sorted(set(members))
    placed, out = set(), []
    while len(out) < len(members):
        ready = [m for m in members if m not in placed
                 and all(s in placed or s not in members for s in graph.get(m, ()) if s != m)]
        if not ready:
            left = [m for m in members if m not in placed]
            raise SolidWorksError("The parts reference each other in a circle: "
                                  + ", ".join(os.path.basename(m) for m in left) + ".")
        for m in ready:
            placed.add(m)
            out.append(m)
    return out


def objections(chain: list, open_docs: dict, owners: dict, own_pid: int, save: bool) -> list:
    """Why the chain must not be run as asked; pure. open_docs: {path key: {title, modified}};
    owners: guard.owners() (title -> {pid}). A document another live server process works
    on is not touched; unsaved changes this server did not make are not saved over."""
    out = []
    for path in chain:
        doc = open_docs.get(path)
        if doc is None:
            continue
        owner = (owners.get(doc.get("title")) or {}).get("pid")
        if owner not in (None, own_pid):
            out.append(f"'{doc.get('title')}' is in use by server process {owner}")
        elif save and doc.get("modified") and owner != own_pid:
            out.append(f"'{doc.get('title')}' has unsaved changes this server did not make (save it yourself, or "
                       "pass save=False)")
    return out


def summary(rows: list) -> dict:
    """Totals over the per-part rows; pure."""
    changed = [r["part"] for r in rows if r.get("changed")]
    failed = [r["part"] for r in rows if r.get("error") or r.get("failing_features") or r.get("rebuild_ok") is False]
    return {"count": len(rows), "changed": changed, "unchanged": [r["part"] for r in rows
                                                                 if not r.get("changed") and r["part"] not in failed],
            "failed": failed, "saved": [r["part"] for r in rows if r.get("saved")]}


# --- SolidWorks ---------------------------------------------------------------------------------


def _root_path(session, root) -> str:
    if root is None:
        path = session._require_model().GetPathName()
        if not path:
            raise SolidWorksError("The current document was never saved: derived parts reference a file.")
        return path
    text = str(root)
    if os.path.isfile(text):
        return os.path.abspath(text)
    wanted = text.lower()
    for doc in session._open_documents():
        title = doc.GetTitle()
        if title.lower() == wanted or os.path.splitext(title)[0].lower() == wanted:
            if not doc.GetPathName():
                raise SolidWorksError(f"'{title}' was never saved: derived parts reference a file.")
            return doc.GetPathName()
    raise SolidWorksError(f"root '{root}' is neither a file nor the title of an open document.")


def _candidates(folders) -> list:
    files = []
    for folder in folders:
        if not os.path.isdir(folder):
            raise SolidWorksError(f"No folder '{folder}'.")
        files += [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                  if name.lower().endswith(".sldprt") and not name.startswith("~$")]
    if len(files) > MAX_FILES:
        raise SolidWorksError(f"{len(files)} part files to look through (limit {MAX_FILES}): name the folder(s) "
                              "that hold the derived parts.")
    return files


def _graph(session, files: list) -> dict:
    graph = {}
    for path in files:
        try:
            deps = session._sw.GetDocumentDependencies2(path, False, False, False)
        except Exception:  # noqa: BLE001 - a file SolidWorks cannot read is not part of a chain
            deps = None
        graph[key(path)] = [key(p) for p in paths_of(deps)]
    return graph


def _open_docs(session) -> dict:
    out = {}
    for doc in session._open_documents():
        try:
            path = doc.GetPathName()
            if path:
                out[key(path)] = {"title": doc.GetTitle(), "modified": bool(doc.GetSaveFlag()), "doc": doc}
        except Exception:  # noqa: BLE001
            continue
    return out


def _front_title(session) -> str | None:
    try:
        active = binding.wrap(session._sw.ActiveDoc, session._mod.IModelDoc2)
        return None if active is None else active.GetTitle()
    except Exception:  # noqa: BLE001
        return None


def _volumes(session) -> dict:
    return {body.Name: round(bodies_mod._volume_mm3(body), 3) for body in bodies_mod._all_bodies(session)}


def rebuild_chain(session, root=None, folder=None, save: bool = True, close_opened: bool = True,
                  dry_run: bool = False) -> dict:
    """Rebuild root and every part derived from it, sources first; see the module text."""
    root_path = _root_path(session, root)
    root_key = key(root_path)
    if folder is None:
        folders = [os.path.dirname(root_path)]
    else:
        folders = [os.path.abspath(f) for f in ([folder] if isinstance(folder, str) else list(folder))]
    files = _candidates(folders)
    names = {key(p): p for p in files}
    names.setdefault(root_key, root_path)
    t0 = time.perf_counter()
    graph = _graph(session, files)
    scan = {"files": len(files), "seconds": round(time.perf_counter() - t0, 1)}
    chain = in_order(graph, dependents(graph, root_key))
    order = [root_key] + chain
    open_docs = _open_docs(session)
    plan = [{"part": os.path.basename(names[k]), "path": names[k], "open": k in open_docs,
             "references": [os.path.basename(names.get(s, s)) for s in graph.get(k, ()) if s in order]}
            for k in order]
    if not chain:
        return {"ok": True, "root": os.path.basename(root_path), "chain": [], "parts": [],
                "note": f"No part file in {', '.join(folders)} references '{os.path.basename(root_path)}': "
                        "nothing to rebuild. Pass folder= when the derived parts live elsewhere."}
    blocked = objections(order, open_docs, guard.prune_owners(), os.getpid(), bool(save))
    if blocked:
        raise SolidWorksError("rebuild_chain was not started: " + "; ".join(blocked) + ". Nothing was changed.")
    if dry_run:
        return {"ok": True, "dry_run": True, "root": os.path.basename(root_path),
                "chain": [p["part"] for p in plan[1:]], "parts": plan,
                "would_load": [p["part"] for p in plan if not p["open"]], "scan": scan}

    mod = session._mod
    rows, loaded = [], []
    front = _front_title(session)
    try:
        for k in order:
            path = names[k]
            row = {"part": os.path.basename(path), "path": path}
            rows.append(row)
            started = time.perf_counter()
            try:
                here = k not in open_docs and assembly.load_hidden(session, path, SW_DOC_PART)
                doc = binding.wrap(session._sw.GetOpenDocumentByName(path), mod.IModelDoc2)
                if doc is None:
                    raise SolidWorksError("SolidWorks did not hand the document over after loading it.")
                if here:
                    loaded.append(doc.GetTitle())
                    row["loaded_here"] = True
                with derive.as_current(session, doc):
                    before = _volumes(session)
                    row["rebuild_ok"] = bool(doc.ForceRebuild3(False))
                    after = _volumes(session)
                    row["bodies"] = derive.volume_changes(before, after)
                    row["changed"] = set(before) != set(after) or any(
                        abs(c.get("delta_mm3") or 0.0) > 1e-3 for c in row["bodies"])
                    failing = session._failing_features()
                    if failing:
                        row["failing_features"] = failing
                    if k != root_key:
                        refs = derive._refs(session, doc)
                        note = derive.verdict(refs)
                        if note:
                            row["warning"] = note
                    if save and bool(doc.GetSaveFlag()):
                        got = doc.Save3(SW_SAVE_SILENT, 0, 0)
                        ok = got[0] if isinstance(got, tuple) else got
                        if not ok:
                            raise SolidWorksError(f"SolidWorks did not save it (answer {got}).")
                        row["saved"] = True
            except SolidWorksError as exc:
                row["error"] = str(exc)
                row["seconds"] = round(time.perf_counter() - started, 1)
                break   # what comes after would rebuild from a part that did not
            row["seconds"] = round(time.perf_counter() - started, 1)
    finally:
        closed = []
        if close_opened:
            for title in reversed(loaded):
                try:
                    session._sw.CloseDoc(title)
                    closed.append(title)
                except Exception:  # noqa: BLE001
                    pass
        # closing a document lets SolidWorks bring any other window forward: put back the one
        # that was in front when the call started
        if front and _front_title(session) != front:
            try:
                session._sw.ActivateDoc3(front, False, assembly.SW_DONT_REBUILD_ACTIVE_DOC, 0)
            except Exception:  # noqa: BLE001 - the chain is done; the front window is a courtesy
                pass
    totals = summary(rows)
    result = {"ok": not totals["failed"], "root": os.path.basename(root_path),
              "chain": [os.path.basename(names[k]) for k in chain], "parts": rows, **totals, "scan": scan}
    if closed:
        result["closed_again"] = closed
    if len(rows) < len(order):
        result["not_reached"] = [os.path.basename(names[k]) for k in order[len(rows):]]
    if totals["failed"]:
        result["error"] = ("The chain stopped or rebuilt with errors at: " + ", ".join(totals["failed"])
                           + " (see parts). Parts before it are rebuilt"
                           + (" and saved." if save else "."))
    return result
