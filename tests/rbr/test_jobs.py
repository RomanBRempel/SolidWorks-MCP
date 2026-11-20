"""The background-job scheduler, with no SolidWorks: a fake worker thread and session."""

import asyncio
import concurrent.futures
import json
import os
import threading
import time

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import jobs


class FakeWorker:
    """Runs submitted callables on one background thread, like ComWorker."""

    def __init__(self):
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def submit(self, fn):
        return self._pool.submit(fn)


class FakeSession:
    """run_guarded that interprets the 'code' itself instead of touching COM."""

    def __init__(self, delay_s=0.0, fail=False, result=None):
        self.delay_s = delay_s
        self.fail = fail
        self.result = result or {"ok": True, "result": 42,
                                 "features": {"before": 1, "after": 2, "added": ["F1"]}}
        self.ran = threading.Event()

    def run_guarded(self, fn, *args, **kwargs):
        self.ran.set()
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise SolidWorksError("the script broke")
        return dict(self.result)


@pytest.fixture
def worker():
    w = FakeWorker()
    yield w
    w._pool.shutdown(wait=True)


@pytest.fixture(autouse=True)
def fresh_registry(swmcp_home):
    jobs._jobs.clear()
    jobs._futures.clear()
    yield
    jobs._jobs.clear()
    jobs._futures.clear()


def _wait_state(job_id, state, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs.get(job_id).get("state") == state:
            return jobs.get(job_id)
        time.sleep(0.01)
    raise AssertionError(f"job never reached {state}: {jobs.get(job_id)}")


def test_job_runs_to_done_and_persists(worker, swmcp_home):
    job = jobs.submit(worker, FakeSession(), "result = 42")
    record = _wait_state(job["job_id"], "done")
    assert record["result"]["result"] == 42
    assert record["features_added"] == ["F1"]
    assert record["elapsed_s"] >= 0
    path = os.path.join(str(swmcp_home), "jobs", job["job_id"] + ".json")
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["state"] == "done"


def test_get_reads_the_file_of_another_process(worker, swmcp_home):
    job = jobs.submit(worker, FakeSession(), "x")
    _wait_state(job["job_id"], "done")
    jobs._jobs.clear()  # as if this were a different server process
    jobs._futures.clear()
    record = jobs.get(job["job_id"])
    assert record["ok"] and record["state"] == "done"


def test_unknown_job_is_a_named_error(swmcp_home):
    with pytest.raises(SolidWorksError, match="No job 'nope'"):
        jobs.get("nope")


def test_failure_is_recorded_not_raised(worker, swmcp_home):
    job = jobs.submit(worker, FakeSession(fail=True), "x")
    record = _wait_state(job["job_id"], "failed")
    assert "the script broke" in record["error"]
    assert jobs.answer(record)["ok"] is False


def test_sync_wait_detaches_then_finishes(worker, swmcp_home):
    session = FakeSession(delay_s=0.5)
    job = jobs.submit(worker, session, "x")

    async def ask(wait_s):
        return await jobs.wait(job["job_id"], wait_s)

    early = asyncio.run(ask(0.05))
    assert early["state"] in ("queued", "running")
    detached = jobs.answer(early)
    assert detached["ok"] and detached["job_id"] == job["job_id"] and "state" in detached
    late = asyncio.run(ask(5.0))
    assert late["state"] == "done"
    assert jobs.answer(late)["result"] == 42


def test_list_jobs_newest_and_capped(worker, swmcp_home):
    ids = [jobs.submit(worker, FakeSession(), "x")["job_id"] for _ in range(3)]
    for job_id in ids:
        _wait_state(job_id, "done")
    listing = jobs.list_jobs(last_n=2)
    assert listing["count"] == 2
    assert [j["state"] for j in listing["jobs"]] == ["done", "done"]


def test_cleanup_drops_records_older_than_24h(worker, swmcp_home):
    job = jobs.submit(worker, FakeSession(), "x")
    _wait_state(job["job_id"], "done")
    path = os.path.join(str(swmcp_home), "jobs", job["job_id"] + ".json")
    old = time.time() - 25 * 3600
    os.utime(path, (old, old))
    jobs.cleanup()
    assert not os.path.exists(path)
    with pytest.raises(SolidWorksError):
        jobs.get(job["job_id"])


def test_submit_without_worker_refuses():
    with pytest.raises(SolidWorksError, match="Windows"):
        jobs.submit(None, FakeSession(), "x")


# --- thread jobs: work that waits on another process, off the COM worker -------------------------


def test_a_thread_job_runs_without_a_worker_and_persists(swmcp_home):
    gate = threading.Event()

    def run(job_id):
        jobs.update(job_id, directory="D:/flow/1", project="P1")
        gate.wait(5)
        return {"ok": True, "goals": [{"name": "FX", "value": 0.15}]}

    job = jobs.submit_thread("flow_solve", run, {"document": "cube.SLDPRT"})
    assert job["state"] == "running" and job["document"] == "cube.SLDPRT"
    deadline = time.time() + 5
    while "directory" not in jobs.get(job["job_id"]) and time.time() < deadline:
        time.sleep(0.01)
    on_disk = json.loads((swmcp_home / "jobs" / f"{job['job_id']}.json").read_text(encoding="utf-8"))
    assert on_disk["directory"] == "D:/flow/1" and on_disk["project"] == "P1" and on_disk["state"] == "running"
    assert [j["job_id"] for j in jobs.running("flow_solve")] == [job["job_id"]]
    gate.set()
    done = _wait_state(job["job_id"], "done")
    assert jobs.answer(done)["goals"][0]["name"] == "FX" and done["directory"] == "D:/flow/1"
    assert jobs.running("flow_solve") == []


def test_a_thread_job_that_raises_ends_failed(swmcp_home):
    def run(job_id):
        raise SolidWorksError("Flow did not solve: no pressure condition")

    job = jobs.submit_thread("flow_solve", run)
    failed = _wait_state(job["job_id"], "failed")
    assert "no pressure condition" in failed["error"] and jobs.answer(failed)["ok"] is False


def test_latest_names_the_newest_job_of_a_tool_also_for_another_process(swmcp_home):
    assert jobs.latest("flow_solve") is None
    first = jobs.submit_thread("flow_solve", lambda job_id: {"ok": True})
    second = jobs.submit_thread("flow_solve", lambda job_id: {"ok": True, "n": 2})
    _wait_state(first["job_id"], "done")
    _wait_state(second["job_id"], "done")
    assert jobs.latest("flow_solve")["job_id"] == second["job_id"]
    jobs._jobs.clear()   # as another server process sees it: the files only
    assert jobs.latest("flow_solve")["result"]["n"] == 2
    assert jobs.latest("other_tool") is None


def test_wait_polls_the_file_of_a_job_another_process_runs(swmcp_home):
    """get_job(wait_s) answered such a job at once: there is no future to wait on here."""
    folder = swmcp_home / "jobs"
    folder.mkdir(exist_ok=True)
    record = {"job_id": "abc123abc123", "state": "running", "tool": "run_script_async", "pid": 1}
    (folder / "abc123abc123.json").write_text(json.dumps(record), encoding="utf-8")

    def finish():
        time.sleep(0.4)
        (folder / "abc123abc123.json").write_text(json.dumps({**record, "state": "done", "result": {"ok": True}}),
                                                  encoding="utf-8")

    threading.Thread(target=finish, daemon=True).start()
    t0 = time.monotonic()
    got = asyncio.run(jobs.wait("abc123abc123", 5.0))
    assert got["state"] == "done" and 0.3 <= time.monotonic() - t0 < 3.0
    t0 = time.monotonic()
    assert asyncio.run(jobs.wait("abc123abc123", 5.0))["state"] == "done"   # finished: at once
    assert time.monotonic() - t0 < 0.3


def test_wait_keeps_waiting_until_its_time_is_up(swmcp_home):
    folder = swmcp_home / "jobs"
    folder.mkdir(exist_ok=True)
    (folder / "def456def456.json").write_text(json.dumps({"job_id": "def456def456", "state": "running"}),
                                              encoding="utf-8")
    t0 = time.monotonic()
    got = asyncio.run(jobs.wait("def456def456", 0.7))
    assert got["state"] == "running" and 0.6 <= time.monotonic() - t0 < 2.0


def test_a_wait_that_runs_out_does_not_cancel_a_queued_job(worker, swmcp_home):
    """The COM worker skips a job whose future was cancelled before it started, and
    asyncio.wait_for cancels what it waits on: a queued script was dropped when the
    reply window closed before its turn. Now it runs."""
    blocker = FakeSession(delay_s=0.4)
    queued = FakeSession()
    jobs.submit(worker, blocker, "first")
    job = jobs.submit(worker, queued, "second")
    early = asyncio.run(jobs.wait(job["job_id"], 0.05))
    assert early["state"] == "queued"
    assert _wait_state(job["job_id"], "done")["result"]["result"] == 42 and queued.ran.is_set()
