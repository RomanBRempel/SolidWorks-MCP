"""sync_equation_file: bring the document's global variables up to the linked equation file.

SolidWorks' own UpdateValuesFromExternalEquationFile only refreshes values of
names that already exist in the document; a line added to the file never
arrives, and EquationMgr.Add2 answers a bare -1 when it refuses. This module
reads the file itself, adds the new global variables, updates the changed ones,
reports the names that are gone from the file, and turns every refusal into a
named reason.

Dimension equations ("D1@Sketch1" = ...) in the file are left alone: the file
drives the global variables, the model drives its own dimensions.

Known Add2 refusals seen in practice (each diagnosed here):
- the name is already set by another equation (SolidWorks compares case-blind),
  including a *broken* leftover from a deleted feature: the broken entry must be
  deleted first, then Add2 succeeds - sync does that by itself and reports it;
- the expression references a name that neither the globals nor the model's
  dimensions know (e.g. the dimension was re-created under a new name);
- the text is not "name" = expression.
"""

import os
import re

from ..errors import SolidWorksError

_LINE = re.compile(r'^\s*"(?P<name>[^"]+)"\s*=\s*(?P<rhs>.+?)\s*$')
_QUOTED = re.compile(r'"([^"]+)"')


def parse_equation_file(text: str) -> tuple[list, list]:
    """The equations of a linked file, in file order; pure, unit-tested.

    Returns (entries, ignored): entries are {line, name, rhs, raw, dimension};
    ignored are the lines that are neither blank, a comment ('...) nor an equation.
    """
    entries, ignored = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿").strip()
        if not line or line.startswith("'"):
            continue
        match = _LINE.match(line)
        if not match:
            ignored.append({"line": lineno, "text": raw.strip()[:200]})
            continue
        name = match.group("name").strip()
        entries.append({"line": lineno, "name": name, "rhs": match.group("rhs"),
                        "raw": f'"{name}" = {match.group("rhs")}', "dimension": "@" in name})
    return entries, ignored


def _normalize(expression: str) -> str:
    """Whitespace-blind form for comparing what SolidWorks stores with the file's text."""
    return re.sub(r"\s+", "", expression or "").lower()


def plan(file_entries: list, doc_entries: list) -> dict:
    """What sync will do, decided without SolidWorks; pure, unit-tested.

    doc_entries is session._equation_entries() ({index, equation, name, global, ...}).
    Returns add / update / unchanged (file entries), skipped_dimensions (names),
    and missing_in_file: the document's global variables the file no longer has.
    """
    doc_globals = {e["name"].lower(): e for e in doc_entries if e.get("global") and e.get("name")}
    # a broken equation (its names are gone) is replaced wholesale: Add2 after deleting it,
    # never SetEquation in place - so it counts as absent here
    live = {k: e for k, e in doc_globals.items() if not e.get("broken")}
    add, update, unchanged, skipped = [], [], [], []
    seen = set()
    for entry in file_entries:
        if entry["dimension"]:
            skipped.append(entry["name"])
            continue
        seen.add(entry["name"].lower())
        current = live.get(entry["name"].lower())
        if current is None:
            add.append(entry)
        elif _normalize(current["equation"]) == _normalize(entry["raw"]):
            unchanged.append(entry)
        else:
            update.append({**entry, "index": current["index"], "was": current["equation"]})
    missing = sorted(e["name"] for k, e in doc_globals.items() if k not in seen)
    return {"add": add, "update": update, "unchanged": unchanged,
            "skipped_dimensions": skipped, "missing_in_file": missing}


def refusal_reason(entry: dict, doc_entries: list, known_parameter) -> str:
    """Why SolidWorks most likely refused this equation; pure, unit-tested.

    known_parameter(name) says whether the model resolves a dimension name
    (model.Parameter(name) is not None); pass `lambda n: False` when unknown.
    """
    name = entry["name"]
    same = [e for e in doc_entries if (e.get("name") or "").lower() == name.lower()]
    if same and any(e.get("broken") for e in same):
        return (f"'{name}' is already set by a broken equation (index {same[0]['index']}, "
                f"{same[0]['equation']!r}) left over from a deleted feature: delete it first "
                "(sync retries that by itself).")
    if same:
        return f"'{name}' is already set by equation index {same[0]['index']}: {same[0]['equation']!r}."
    globals_known = {(e.get("name") or "").lower() for e in doc_entries if e.get("global")}
    unknown = [ref for ref in _QUOTED.findall(entry["rhs"])
               if ref.lower() not in globals_known and ref.lower() != name.lower() and not known_parameter(ref)]
    if unknown:
        return (f"the expression references {', '.join(repr(u) for u in unknown)}, which neither the "
                "global variables nor the model's dimensions resolve (a re-created dimension gets a "
                "new name: list_dimensions shows the live ones).")
    if "@" in name:
        return (f"'{name}' is a dimension equation: the dimension must exist and not be driven "
                "by another equation; after re-creating a dimension in the same sketch, delete the "
                "old equation entry first, rebuild, then add.")
    return "SolidWorks gave no reason (Add2 returned -1); check the expression against list_equations."


def _read_file(path: str) -> str:
    for encoding in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            with open(path, encoding=encoding) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise SolidWorksError(f"Cannot read the equation file {path}: {exc}")
    raise SolidWorksError(f"Cannot decode the equation file {path}.")


def sync(session, path: str | None = None) -> dict:
    """Apply the linked (or given) equation file's global variables to the current document."""
    model = session._require_model()
    eqmgr = session._equation_mgr()
    file_path = path or str(getattr(eqmgr, "FilePath", "") or "")
    if not file_path:
        raise SolidWorksError("The document has no linked equation file; pass path= explicitly.")
    if not os.path.isfile(file_path):
        raise SolidWorksError(f"No equation file at {file_path}.")
    entries, ignored = parse_equation_file(_read_file(file_path))
    doc_entries = session._equation_entries()
    todo = plan(entries, doc_entries)
    eqmgr.AutomaticSolveOrder = True

    def parameter_known(name: str) -> bool:
        try:
            return model.Parameter(name) is not None
        except Exception:  # noqa: BLE001
            return False

    added, updated, failed, repaired = [], [], [], []
    # updates first: repairing a broken duplicate below deletes an equation, which
    # would shift the indices the update plan still points at
    for entry in todo["update"]:
        eqmgr.SetEquation(entry["index"], entry["raw"])
        if session._same_equation(eqmgr.Equation(entry["index"]), entry["raw"]):
            updated.append({"equation": entry["raw"], "index": entry["index"], "was": entry["was"]})
        else:  # a refusal keeps the old text silently
            failed.append({"equation": entry["raw"], "line": entry["line"],
                           "reason": refusal_reason(entry, doc_entries, parameter_known)
                           + " (SetEquation kept the old text)"})
    for entry in todo["add"]:
        index = eqmgr.Add2(eqmgr.GetCount(), entry["raw"], False)
        if index < 0:
            # a broken equation with the same name blocks Add2: delete it and retry once
            lhs = re.compile(r'\s*"' + re.escape(entry["name"]) + r'"\s*=', re.IGNORECASE)
            stale = [e for e in doc_entries
                     if e.get("broken") and ((e.get("name") or "").lower() == entry["name"].lower()
                                             or lhs.match(e.get("equation") or ""))]
            if stale and eqmgr.Delete(stale[0]["index"]) >= 0:
                doc_entries = session._equation_entries()
                index = eqmgr.Add2(eqmgr.GetCount(), entry["raw"], False)
                if index >= 0:
                    repaired.append({"name": entry["name"], "deleted": stale[0]["equation"]})
        if index < 0:
            failed.append({"equation": entry["raw"], "line": entry["line"],
                           "reason": refusal_reason(entry, doc_entries, parameter_known)})
        else:
            added.append({"equation": entry["raw"], "index": index})
    rebuilt_ok = bool(model.ForceRebuild3(False))
    out = {"ok": not failed, "file": file_path,
           "added": added, "updated": updated, "unchanged": len(todo["unchanged"]),
           "skipped_dimensions": todo["skipped_dimensions"],
           "missing_in_file": todo["missing_in_file"],
           "rebuild_ok": rebuilt_ok, "failing_features": session._failing_features()}
    if repaired:
        out["repaired"] = repaired
    if failed:
        out["failed"] = failed
        out["error"] = f"{len(failed)} equation(s) refused; each carries its reason."
    if ignored:
        out["ignored_lines"] = ignored
    return out
