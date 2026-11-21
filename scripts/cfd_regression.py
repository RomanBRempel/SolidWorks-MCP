"""Local regression of the Flow tools against a reference run (needs SolidWorks with Flow Simulation).

Everything about the reference comes from the private settings (rbr/private.py, key
`reference`): the model, the finished run's folder, the case name, the free stream,
what the run gave and how far a repeat may be off. Nothing of it is in this file.

What it does, on a file copy of the reference model in the private sandbox_dir:
1. the case file is made from the reference run's folder if it is not there yet (read only),
   (the copy is of reference.carrier_model when that key is set: a model file whose
   reference project still has the reference setup),
2. a project is made from the case (flow_create_from_case) and solved with the case held
   (flow_solve(case=...)), `--runs` times (default 2),
3. checks: the cell count is the reference's; each goal is within the private tolerance of
   the reference - by its last value AND by its averaged value; the repeats agree with each
   other within --repeat-tolerance (default 0.1 %). The private key reference.compare
   ('averaged' | 'value' | 'both', default 'both') says which of the two decides; the
   other is shown as [info].

Run it with the server's interpreter, from the repository:

    python scripts/cfd_regression.py [--runs 2] [--cpus N] [--keep]

Exit code 0: all checks passed; 1: a check failed; 2: it could not run. One solver runs at
a time: do not start it while another Flow calculation is running on this PC.
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from solidworks_mcp import server  # noqa: E402
from solidworks_mcp.rbr import flow_case, private, register  # noqa: E402

_loop = asyncio.new_event_loop()
CHECKS = []


def call(tool: str, **args) -> dict:
    result = _loop.run_until_complete(server.mcp.call_tool(tool, args))
    blocks = result[0] if isinstance(result, tuple) else result
    text = next((b.text for b in blocks if type(b).__name__ == "TextContent"), "")
    try:
        return json.loads(text)
    except ValueError:
        return {"ok": False, "error": text}


def check(label: str, passed: bool, detail="", gate: bool = True) -> bool:
    """A check that counts (gate) or one that is only shown: [info] with ok / off."""
    if gate:
        CHECKS.append((label, bool(passed)))
        mark = "PASS" if passed else "FAIL"
    else:
        mark = "info: ok" if passed else "info: off"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail != "" else ""), flush=True)
    return bool(passed)


def within(value, expected, tolerance: dict, goal: str):
    """(passed, text) of a goal value against the reference by the private tolerance keys
    '<goal>_rel' (a fraction) or '<goal>_abs'; without a key the value is only shown."""
    if value is None or expected is None:
        return False, "no value"
    delta = value - expected
    pct = delta / abs(expected) * 100.0 if expected else float("nan")
    if f"{goal}_rel" in tolerance:
        limit = float(tolerance[f"{goal}_rel"])
        return abs(delta) <= limit * abs(expected), f"{value:.6g} vs {expected:.6g}: {pct:+.3f} % (limit {limit * 100:g} %)"
    if f"{goal}_abs" in tolerance:
        limit = float(tolerance[f"{goal}_abs"])
        return abs(delta) <= limit, f"{value:.6g} vs {expected:.6g}: {delta:+.4g} (limit {limit:g})"
    return True, f"{value:.6g} vs {expected:.6g}: {pct:+.3f} % (no tolerance set)"


def solve(case: str, cpus) -> dict:
    args = {"timeout_s": 5, "case": case}
    if cpus:
        args["cpus"] = cpus
    started = call("flow_solve", **args)
    job = started.get("job_id")
    if not job:
        return started
    t0 = time.time()
    while True:
        record = call("get_job", job_id=job, wait_s=45)
        if record.get("state") != "running" or time.time() - t0 > 4 * 3600:
            return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--cpus", type=int, default=None, help="solver threads (default: Flow's choice)")
    parser.add_argument("--repeat-tolerance", type=float, default=0.001, help="between repeats, a fraction")
    parser.add_argument("--keep", action="store_true", help="leave the copy open and its projects in place")
    args = parser.parse_args()
    try:
        settings = private.load()
        ref = private.need("reference", settings)
        sandbox = private.need("sandbox_dir", settings)
        cases = private.need("cases_dir", settings)
        for key in ("model", "results_dir", "case_name", "velocity_mps", "expected"):
            private.need(f"reference.{key}", settings)
    except private.PrivateError as exc:
        print("cannot run:", exc)
        return 2
    register(server)
    tolerance = ref.get("tolerance") or {}
    # which goal value decides: 'averaged' (over the solver's analysis interval), 'value' (the
    # last iteration) or 'both'. The parallel solver is not bit-repeatable and a goal that
    # still swings is sampled at a random phase by its last value, so 'value' may fail a
    # tight tolerance on a correct setup; the other kind is still shown.
    decides = str(ref.get("compare") or "both").lower()
    gate = {"value": decides in ("value", "both"), "averaged": decides in ("averaged", "both")}
    print(f"goal values that decide: {decides}", flush=True)
    expected = ref["expected"]
    case_path = os.path.join(cases, ref["case_name"] + ".json")
    if not os.path.isfile(case_path):
        made = call("flow_case_from_project", name=ref["case_name"], results_dir=ref["results_dir"],
                    note="made by the regression from the reference run's folder")
        if not check("the case file was made from the reference run", made.get("ok"), made.get("error", "")):
            return 2
    case = flow_case.load_case(case_path)
    reference_goals = (case.get("expect") or {}).get("goals") or {}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    # the carrier: a model file whose reference project still has the reference setup (the
    # project in the model itself may have been changed since the reference run)
    source = ref.get("carrier_model") or ref["model"]
    print("copying", "reference.carrier_model" if ref.get("carrier_model") else "reference.model", flush=True)
    extension = os.path.splitext(source)[1]
    copy = os.path.join(sandbox, f"regression_{stamp}{extension}")
    shutil.copy2(source, copy)
    opened = call("open_part", path=copy)
    if not check("the copy of the reference model is open", opened.get("ok"), opened.get("error", "")):
        return 2

    runs = []
    try:
        for i in range(1, max(1, args.runs) + 1):
            name = f"REG_{stamp[-6:]}_{i}"
            made = call("flow_create_from_case", case=ref["case_name"], name=name, velocity_mps=ref["velocity_mps"])
            if not check(f"run {i}: project from the case, settings verified equal", made.get("ok") and made.get("verified"),
                         made.get("error", "")):
                break
            t0 = time.time()
            record = solve(ref["case_name"], args.cpus)
            result = record.get("result") or {}
            run = result.get("run") or {}
            if not check(f"run {i}: solved", record.get("state") == "done", record.get("error", "")):
                break
            print(f"     {time.time() - t0:.0f} s; result: {run.get('result')}; registry: {run.get('registry')}", flush=True)
            with open(run["result"], encoding="utf-8") as fh:
                stored = json.load(fh)
            runs.append(stored)
            check(f"run {i}: cells = the reference's", stored.get("cells") == expected.get("cells_total"),
                  f"{stored.get('cells')} vs {expected.get('cells_total')}")
            check(f"run {i}: the run is the case (settings and mesh)",
                  (stored.get("case") or {}).get("settings_match") and (stored.get("case") or {}).get("mesh_matches"),
                  (stored.get("case") or {}).get("differences", ""))
            for goal, numbers in stored.get("goals", {}).items():
                if goal in expected:
                    passed, text = within(numbers.get("value"), expected[goal], tolerance, goal)
                    check(f"run {i}: {goal} last value against the reference", passed, text, gate["value"])
                if goal in reference_goals:
                    passed, text = within(numbers.get("averaged"), reference_goals[goal].get("averaged"), tolerance, goal)
                    check(f"run {i}: {goal} averaged against the reference's averaged", passed, text,
                          gate["averaged"])
        for i in range(1, len(runs)):
            a, b = runs[0], runs[i]
            check(f"repeat {i + 1}: the same cells as run 1", a.get("cells") == b.get("cells"), f"{a.get('cells')} / {b.get('cells')}")
            check(f"repeat {i + 1}: the same settings fingerprint as run 1",
                  a["fingerprint"]["settings"] == b["fingerprint"]["settings"])
            for goal, numbers in a.get("goals", {}).items():
                other = (b.get("goals") or {}).get(goal) or {}
                for which in ("value", "averaged"):
                    va, vb = numbers.get(which), other.get(which)
                    if va is None or vb is None:
                        continue
                    if f"{goal}_abs" in tolerance:
                        passed, text = abs(vb - va) <= float(tolerance[f"{goal}_abs"]), f"{va:.6g} / {vb:.6g}: {vb - va:+.4g}"
                    else:
                        pct = (vb - va) / abs(va) * 100.0 if va else float("nan")
                        passed, text = abs(vb - va) <= args.repeat_tolerance * abs(va), f"{va:.6g} / {vb:.6g}: {pct:+.3f} %"
                    check(f"repeat {i + 1}: {goal} {which} against run 1", passed, text, gate[which])
    finally:
        if not args.keep:
            call("close_part", save=False)
        try:
            call("release_ui")
        except Exception:  # noqa: BLE001
            pass
    failed = [label for label, passed in CHECKS if not passed]
    print(f"=== {len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
