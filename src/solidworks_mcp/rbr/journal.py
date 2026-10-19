"""Journal: one JSON line per tool call, so every change made in SolidWorks is traceable.

Wraps SolidWorksSession.run_guarded (the choke point every tool and run_script
passes through, on the COM thread). Per call it records: time, tool, arguments
(full code for scripts), document, duration, ok/error, and the feature tree
change (count before/after, names of features added at the end).

Files: %LOCALAPPDATA%\\solidworks-mcp\\journal\\YYYY-MM-DD.jsonl
(override the folder with SWMCP_HOME). Read back with the get_journal tool.
"""

import datetime as _dt
import json
import os
import threading
import time

from .. import binding
from ..session import SolidWorksSession

MAX_ARG_CHARS = 4000  # per argument; a script's code is kept whole up to 64 KB
MAX_CODE_CHARS = 65536

_ORIGINAL = {}
_lock = threading.Lock()


def home() -> str:
    base = os.environ.get("SWMCP_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "solidworks-mcp")
    os.makedirs(base, exist_ok=True)
    return base


def journal_dir() -> str:
    path = os.path.join(home(), "journal")
    os.makedirs(path, exist_ok=True)
    return path


def _clip(value, limit=MAX_ARG_CHARS):
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:  # noqa: BLE001
        text = json.dumps(repr(value), ensure_ascii=False)
    if len(text) <= limit:
        return json.loads(text)
    return text[:limit] + f"... [{len(text)} chars]"


def tool_name(fn) -> str:
    name = getattr(fn, "__name__", None) or repr(fn)
    return name


def describe_args(fn, args, kwargs) -> dict:
    name = tool_name(fn)
    out = {}
    if name == "run_script" and len(args) >= 2:  # (session, code[, args])
        code = args[1]
        out["code"] = code if len(code) <= MAX_CODE_CHARS else code[:MAX_CODE_CHARS] + "\n# ... clipped"
        rest = args[2:]
    else:
        rest = args
    rest = [a for a in rest if not isinstance(a, SolidWorksSession)]  # rbr tools pass the session
    if rest:
        out["args"] = [_clip(a) for a in rest]
    if kwargs:
        out["kwargs"] = {k: _clip(v) for k, v in kwargs.items()}
    return out


def tree_state(session):
    """(title, top-level feature count, last feature name) of the current document; cheap."""
    model = session._model
    if model is None:
        return None
    try:
        fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
        last = model.FeatureByPositionReverse(0)
        return {"title": model.GetTitle(), "count": int(fm.GetFeatureCount(True)),
                "last": last.Name if last is not None else None}
    except Exception:  # noqa: BLE001 - the journal never breaks a call
        return None


def tree_added(session, before, after) -> list:
    """Names of the features appended since `before` (same document only)."""
    if not before or not after or before["title"] != after["title"]:
        return []
    grown = after["count"] - before["count"]
    if grown <= 0:
        return []
    names = []
    try:
        for i in range(min(grown, 50)):
            feat = session._model.FeatureByPositionReverse(i)
            if feat is None or feat.Name == before["last"]:
                break
            names.append(feat.Name)
    except Exception:  # noqa: BLE001
        pass
    return list(reversed(names))


def write(entry: dict) -> None:
    path = os.path.join(journal_dir(), _dt.date.today().isoformat() + ".jsonl")
    line = json.dumps(entry, ensure_ascii=False, default=repr)
    with _lock, open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _run_guarded(self, fn, *args, **kwargs):
    t0 = time.perf_counter()
    entry = {"ts": _dt.datetime.now().isoformat(timespec="milliseconds"), "tool": tool_name(fn),
             **describe_args(fn, args, kwargs)}
    before = tree_state(self)
    try:
        result = _ORIGINAL["run_guarded"](self, fn, *args, **kwargs)
    except Exception as exc:
        entry.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:2000])
        raise
    else:
        ok = result.get("ok", True) if isinstance(result, dict) else True
        entry["ok"] = ok
        if isinstance(result, dict) and not ok:
            entry["error"] = str(result.get("error", ""))[:2000]
        return result
    finally:
        after = tree_state(self)
        entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        entry["doc"] = (after or before or {}).get("title")
        if before and after and before["title"] == after["title"]:
            entry["features"] = {"before": before["count"], "after": after["count"]}
            added = tree_added(self, before, after)
            if added:
                entry["features"]["added"] = added
        try:
            write(entry)
        except Exception:  # noqa: BLE001 - a full disk must not fail modelling
            pass


def read(last_n: int = 20, tool: str | None = None, date: str | None = None,
         errors_only: bool = False) -> dict:
    """The last entries of a day's journal (today by default), newest last."""
    day = date or _dt.date.today().isoformat()
    path = os.path.join(journal_dir(), day + ".jsonl")
    if not os.path.isfile(path):
        days = sorted(f[:-6] for f in os.listdir(journal_dir()) if f.endswith(".jsonl"))
        return {"ok": True, "date": day, "count": 0, "entries": [], "days": days[-10:], "path": path}
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if tool and e.get("tool") != tool:
                continue
            if errors_only and e.get("ok", True):
                continue
            entries.append(e)
    total = len(entries)
    entries = entries[-max(1, int(last_n)):]
    stats = {}
    for e in entries:
        s = stats.setdefault(e.get("tool"), {"calls": 0, "ms": 0.0, "errors": 0})
        s["calls"] += 1
        s["ms"] = round(s["ms"] + float(e.get("ms") or 0), 1)
        s["errors"] += 0 if e.get("ok", True) else 1
    return {"ok": True, "date": day, "path": path, "total": total, "count": len(entries),
            "stats": stats, "entries": entries}


def apply() -> None:
    if not _ORIGINAL:
        _ORIGINAL["run_guarded"] = SolidWorksSession.run_guarded
        SolidWorksSession.run_guarded = _run_guarded
