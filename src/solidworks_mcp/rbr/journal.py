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

# The guard (applied underneath) reads the tree state for us inside its lock and
# with CommandInProgress set: outside it each read cost ~0.15 s on a 200-feature
# part and raced the other server processes.
GUARDED = False
_notes = threading.local()


def note(session, when: str) -> None:
    """Called by the guard at the start ('before') and end ('after') of a call."""
    if getattr(_notes, "open", False):
        setattr(_notes, when, tree_state(session))


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


def tree_state(session, names=None):
    """{title, top-level feature count, last feature name, rolled} of the current document; cheap.

    `names` (the top-level feature names in tree order) is what tells WHICH
    features a call added when they did not land at the end of the tree: below
    the rollback bar the tree goes on, and reading "the last N features" then
    names rolled-back ones (a script renamed an existing plane that way).
    names=None lists them only while the part is rolled back, True always.
    """
    model = session._model
    if model is None:
        return None
    try:
        fm = binding.wrap(model.FeatureManager, session._mod.IFeatureManager)
        last = binding.wrap(model.FeatureByPositionReverse(0), session._mod.IFeature)
        state = {"title": model.GetTitle(), "count": int(fm.GetFeatureCount(True)),
                 "last": last.Name if last is not None else None}
    except Exception:  # noqa: BLE001 - the journal never breaks a call
        return None
    try:
        if fm.GetFreezeLocation() is not None:
            state["count"] -= 1  # the freeze bar is listed as a feature of its own
    except Exception:  # noqa: BLE001 - no freeze bar in this build or document type
        pass
    try:
        state["rolled"] = bool(last.IsRolledBack()) if last is not None else False
        if names:
            state["names"] = [f.Name for f in session._iter_features()]
        elif state["rolled"]:
            from .fastpath import live_count
            feats = list(session._iter_features())
            live = live_count(feats)
            state.update(live=live, below=feats[live].Name if live < len(feats) else None)
    except Exception:  # noqa: BLE001 - an assembly, a drawing: no rollback bar to mind
        state.setdefault("rolled", False)
    return state


def tree_added(session, before, after) -> list:
    """Names of the features added since `before` (same document only)."""
    if not before or not after or before["title"] != after["title"]:
        return []
    if "names" in before and "names" in after:
        old = set(before["names"])
        return [n for n in after["names"] if n not in old][:50]
    if before.get("rolled") or after.get("rolled"):
        # new features land right above the rollback bar; with the bar moved by the
        # call itself, positions say nothing
        grown = after.get("live", 0) - before.get("live", 0)
        if not (before.get("rolled") and after.get("rolled")) or before.get("below") != after.get("below") \
                or grown <= 0:
            return []
        try:
            feats = list(session._iter_features())
            return [f.Name for f in feats[after["live"] - grown:after["live"]]][:50]
        except Exception:  # noqa: BLE001
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


def record(tool: str, args: dict, result=None) -> None:
    """Journal a call that did not go through run_guarded (it never touched COM), such
    as pressing a dialog button: what was asked and how it ended."""
    entry = {"ts": _dt.datetime.now().isoformat(timespec="milliseconds"), "pid": os.getpid(), "tool": tool,
             "args": args}
    ok = result.get("ok", True) if isinstance(result, dict) else True
    entry["ok"] = ok
    if isinstance(result, dict):
        if not ok:
            entry["error"] = str(result.get("error", ""))[:2000]
        entry["result"] = _clip({k: v for k, v in result.items()
                                 if k in ("closed", "pressed", "dialog", "method", "dialogs")})
    try:
        write(entry)
    except Exception:  # noqa: BLE001 - a full disk must not fail the call
        pass


def _run_guarded(self, fn, *args, **kwargs):
    t0 = time.perf_counter()
    entry = {"ts": _dt.datetime.now().isoformat(timespec="milliseconds"), "pid": os.getpid(), "tool": tool_name(fn),
             **describe_args(fn, args, kwargs)}
    nested = getattr(_notes, "open", False)
    _notes.open, _notes.before, _notes.after = True, None, None
    before = None if GUARDED else tree_state(self)
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
        before = before or _notes.before
        after = _notes.after or tree_state(self)
        _notes.open = nested
        entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        entry["doc"] = (after or before or {}).get("title")
        if before and after and before["title"] == after["title"]:
            entry["features"] = {"before": before["count"], "after": after["count"]}
            if after.get("rolled"):
                entry["features"]["rolled_back"] = True
            added = tree_added(self, before, after)
            if added:
                entry["features"]["added"] = added
        try:
            write(entry)
        except Exception:  # noqa: BLE001 - a full disk must not fail modelling
            pass


def _pct(values, q):
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1) if v else None


def read(last_n: int = 20, tool: str | None = None, date: str | None = None,
         errors_only: bool = False, since: str | None = None, entries: bool = True) -> dict:
    """Journal of a day (today by default), newest last; reads the file only, never SolidWorks.

    since: 'HH:MM[:SS]' or an ISO time - only calls from then on. Stats (calls,
    total s, p50/p95/max ms, errors per tool) cover every matching call; the
    entries list is the last last_n of them (entries=False leaves it out).
    """
    day = date or _dt.date.today().isoformat()
    path = os.path.join(journal_dir(), day + ".jsonl")
    if not os.path.isfile(path):
        days = sorted(f[:-6] for f in os.listdir(journal_dir()) if f.endswith(".jsonl"))
        return {"ok": True, "date": day, "count": 0, "entries": [], "days": days[-10:], "path": path}
    if since and "T" not in since:
        since = f"{day}T{since}"
    found = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if since and e.get("ts", "") < since:
                continue
            if tool and e.get("tool") != tool:
                continue
            if errors_only and e.get("ok", True):
                continue
            found.append(e)
    per = {}
    for e in found:
        per.setdefault(e.get("tool"), []).append(e)
    stats = {}
    for name, items in sorted(per.items(), key=lambda kv: -sum(float(x.get("ms") or 0) for x in kv[1])):
        ms = [float(x.get("ms") or 0) for x in items]
        stats[name] = {"calls": len(items), "total_s": round(sum(ms) / 1000, 1), "p50_ms": _pct(ms, 0.5),
                       "p95_ms": _pct(ms, 0.95), "max_ms": round(max(ms), 1),
                       "errors": sum(1 for x in items if not x.get("ok", True))}
    out = {"ok": True, "date": day, "path": path, "total": len(found), "stats": stats,
           "pids": sorted({e.get("pid") for e in found if e.get("pid")})}
    if found:
        out["span"] = [found[0].get("ts"), found[-1].get("ts")]
        out["busy_s"] = round(sum(float(e.get("ms") or 0) for e in found) / 1000, 1)
    if entries:
        out["entries"] = found[-max(1, int(last_n)):]
        out["count"] = len(out["entries"])
    return out


def apply() -> None:
    if not _ORIGINAL:
        _ORIGINAL["run_guarded"] = SolidWorksSession.run_guarded
        SolidWorksSession.run_guarded = _run_guarded


def unapply() -> None:
    """Restore run_guarded as it was before apply() (test teardown)."""
    if _ORIGINAL:
        SolidWorksSession.run_guarded = _ORIGINAL["run_guarded"]
        _ORIGINAL.clear()
