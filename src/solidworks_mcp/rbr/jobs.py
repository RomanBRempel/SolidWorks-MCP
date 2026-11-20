"""Background jobs: a script that outlives the client's reply window keeps running.

The cloud bridge cuts a tool reply at 60 s, while a real script (a whole part
rebuilt, an export loop) can take minutes. A job is submitted to the same COM
worker queue as every other call, so it still runs one-at-a-time under the
guard and the journal; only the MCP reply detaches from it.

run_script_async / run_saved_script_async return {job_id} at once; get_job and
list_jobs read the state. The synchronous run_script submits a job too and
waits up to SWMCP_SYNC_WAIT_S (50 s): a script that finishes in time answers as
before, a longer one answers {job_id, state: running} instead of dying with a
timeout.

Every job is written to <SWMCP_HOME>/jobs/<job_id>.json on submit, start and
finish, so any server process (and a restarted one) can answer get_job; files
older than KEEP_H (24 h) are removed on the next submit/list.
"""

import asyncio
import datetime as _dt
import json
import os
import threading
import time
import traceback
import uuid

from ..errors import SolidWorksError
from . import journal, script_tool

KEEP_H = 24.0
SYNC_WAIT_S = float(os.environ.get("SWMCP_SYNC_WAIT_S", "50"))
MAX_LIST = 200

_lock = threading.Lock()
_jobs: dict = {}     # job_id -> record (this process; the file mirrors it)
_futures: dict = {}  # job_id -> concurrent.futures.Future


def jobs_dir() -> str:
    path = os.path.join(journal.home(), "jobs")
    os.makedirs(path, exist_ok=True)
    return path


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="milliseconds")


def _path(job_id: str) -> str:
    return os.path.join(jobs_dir(), job_id + ".json")


def _write(job: dict) -> None:
    """Persist atomically; a full disk must not fail the job itself."""
    try:
        tmp = _path(job["job_id"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job, fh, ensure_ascii=False, default=repr)
        os.replace(tmp, _path(job["job_id"]))
    except OSError:
        pass


def cleanup() -> None:
    """Drop job files older than KEEP_H hours (and their memory records)."""
    cutoff = time.time() - KEEP_H * 3600
    try:
        names = os.listdir(jobs_dir())
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(jobs_dir(), name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                with _lock:
                    _jobs.pop(name[:-5], None)
                    _futures.pop(name[:-5], None)
        except OSError:
            continue


def submit(worker, session, code: str | None, args: dict | None = None,
           name: str | None = None, tool: str = "run_script_async", call=None,
           rollback_on_error: bool = True) -> dict:
    """Queue `code` on the COM worker as a job; returns the job record at once.

    `call` (a function of the session, e.g. a Flow solve) runs instead of a
    script, under the same guard, journal and job record."""
    if worker is None:
        raise SolidWorksError("SolidWorks tools only work on Windows with SolidWorks.")
    cleanup()
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "state": "queued", "tool": tool, "script": name,
           "pid": os.getpid(), "submitted": _now(), "code_chars": len(code or "")}
    with _lock:
        _jobs[job_id] = job
    _write(job)

    def run():
        job.update(state="running", started=_now())
        _write(job)
        t0 = time.perf_counter()
        end = {}   # the final fields: written to the file BEFORE memory shows them, so whoever
        try:       # sees the job done (get reads memory first) finds its file complete too
            if call is not None:
                result = session.run_guarded(call, session)
            else:
                result = session.run_guarded(script_tool.run_script, session, code, args or {},
                                             bool(rollback_on_error))
        except SolidWorksError as exc:
            end.update(state="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - the job must end in a readable record
            end.update(state="failed", error=f"{type(exc).__name__}: {exc}",
                       traceback=traceback.format_exc(limit=6))
        else:
            failed = isinstance(result, dict) and not result.get("ok", True)
            end.update(state="failed" if failed else "done", result=result)
            if failed:
                end["error"] = str(result.get("error", "failed"))
            if isinstance(result, dict):
                added = (result.get("features") or {}).get("added")
                if added:
                    end["features_added"] = added
        finally:
            end.update(finished=_now(), elapsed_s=round(time.perf_counter() - t0, 3))
            _write({**job, **end})
            job.update(end)
        return job

    fut = worker.submit(run)
    with _lock:
        _futures[job_id] = fut
    return dict(job)


def submit_thread(tool: str, run, extra: dict | None = None) -> dict:
    """A job that runs in a thread of its own and NOT on the COM worker: work that waits
    on another process (a Flow solve), during which SolidWorks must stay usable.

    `run(job_id)` returns the result dict or raises; it may call update(job_id, ...) to
    add fields (the solver's folder) while it runs. Same record, file and get/wait as
    the COM jobs."""
    import concurrent.futures
    cleanup()
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "state": "running", "tool": tool, "script": None, "pid": os.getpid(),
           "submitted": _now(), "started": _now(), **(extra or {})}
    fut = concurrent.futures.Future()
    with _lock:
        _jobs[job_id] = job
        _futures[job_id] = fut
    _write(job)
    try:  # latest(tool) finds it without reading every record
        with open(os.path.join(jobs_dir(), f"latest-{tool}.txt"), "w", encoding="utf-8") as fh:
            fh.write(job_id)
    except OSError:
        pass

    def body():
        t0 = time.perf_counter()
        end = {}
        try:
            result = run(job_id)
        except SolidWorksError as exc:
            end.update(state="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - the job must end in a readable record
            end.update(state="failed", error=f"{type(exc).__name__}: {exc}",
                       traceback=traceback.format_exc(limit=6))
        else:
            failed = isinstance(result, dict) and not result.get("ok", True)
            end.update(state="failed" if failed else "done", result=result)
            if failed:
                end["error"] = str(result.get("error", "failed"))
        finally:
            end.update(finished=_now(), elapsed_s=round(time.perf_counter() - t0, 3))
            _write({**job, **end})
            job.update(end)
            fut.set_result(job)

    threading.Thread(target=body, name=f"job-{job_id}", daemon=True).start()
    return dict(job)


def update(job_id: str, **fields) -> None:
    """Add fields to a running job's record (memory and file)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        _write(dict(job))   # under the lock: whoever sees the field in memory finds it in the file too


def latest(tool: str) -> dict | None:
    """The newest thread job of `tool` over all server processes (None when there is none
    within KEEP_H): the one submit_thread last noted in latest-<tool>.txt."""
    try:
        with open(os.path.join(jobs_dir(), f"latest-{tool}.txt"), encoding="utf-8") as fh:
            job_id = fh.read().strip()
        return get(job_id) if job_id else None
    except (OSError, SolidWorksError):
        return None


def running(tool: str) -> list:
    """The jobs of `tool` still running: this process' own, and the newest one of the
    machine when another server process runs it. Memory and one small file: for server_health."""
    with _lock:
        out = [dict(j) for j in _jobs.values() if j.get("tool") == tool and j.get("state") == "running"]
    newest = latest(tool)
    if newest and newest.get("state") == "running" and all(newest["job_id"] != j["job_id"] for j in out):
        out.append(newest)
    return out


def get(job_id: str) -> dict:
    """The job record: this process' memory first, then the file another process wrote."""
    with _lock:
        job = _jobs.get(job_id)
    if job is not None:
        return {"ok": True, **job}
    try:
        with open(_path(job_id), encoding="utf-8") as fh:
            return {"ok": True, **json.load(fh)}
    except (OSError, ValueError):
        raise SolidWorksError(f"No job '{job_id}': it never existed, or its record is older than {KEEP_H:.0f} h.")


def list_jobs(last_n: int = 10) -> dict:
    """The newest jobs over all server processes, oldest first."""
    cleanup()
    records = []
    try:
        names = [n for n in os.listdir(jobs_dir()) if n.endswith(".json")]
    except OSError:
        names = []
    for name in names:
        try:
            with open(os.path.join(jobs_dir(), name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        records.append({k: rec.get(k) for k in ("job_id", "state", "tool", "script", "pid",
                                                "submitted", "finished", "elapsed_s", "error")})
    records.sort(key=lambda r: r.get("submitted") or "")
    records = records[-max(1, min(int(last_n), MAX_LIST)):]
    return {"ok": True, "count": len(records), "jobs": records}


POLL_S = 0.5


def finished(record: dict) -> bool:
    return record.get("state") in ("done", "failed")


async def wait(job_id: str, wait_s: float) -> dict:
    """Wait up to wait_s for the job to finish; returns its record either way.

    A job of this process is awaited on its future. A job of ANOTHER server process (the
    record came from its file) is polled until its file says done or failed: get_job used
    to answer such a job at once, whatever wait it was given."""
    with _lock:
        fut = _futures.get(job_id)
    if wait_s <= 0:
        return get(job_id)
    if fut is not None:
        try:
            # shield: the timeout must not cancel the job's own future
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), wait_s)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001 - the record carries the error
            pass
        return get(job_id)
    deadline = time.monotonic() + wait_s
    record = get(job_id)
    while not finished(record) and time.monotonic() < deadline:
        await asyncio.sleep(min(POLL_S, max(0.0, deadline - time.monotonic())))
        record = get(job_id)
    return record


def answer(record: dict) -> dict:
    """The tool reply for a job record: the script's own result once done,
    the failure once failed, the handle while it still runs."""
    state = record.get("state")
    if state == "done":
        result = record.get("result")
        if isinstance(result, dict):
            return {**result, "job_id": record["job_id"]}
        return {"ok": True, "job_id": record["job_id"], "result": result}
    if state == "failed":
        if isinstance(record.get("result"), dict):  # a script that kept its finished steps says which
            return {**record["result"], "ok": False, "job_id": record["job_id"]}
        out = {"ok": False, "job_id": record["job_id"], "error": record.get("error", "failed")}
        if record.get("traceback"):
            out["traceback"] = record["traceback"]
        return out
    return {"ok": True, "job_id": record["job_id"], "state": state,
            "hint": f"Still {state}; get_job('{record['job_id']}') returns the result when done."}
