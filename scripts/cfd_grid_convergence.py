"""Grid convergence of the reference case: three automatic mesh levels, Richardson extrapolation.

Local, needs SolidWorks with Flow Simulation; everything about the reference comes from
the private settings (rbr/private.py, key `reference`), nothing of it is in this file.

On a file copy of the reference model (reference.carrier_model when set) in sandbox_dir:
a project is made from the case, then cloned to the coarser and the finer mesh level
(the clones keep every other setting, the hidden mesh switches included), the three are
solved one after another, and for each force coefficient (private reference.reduction)
the change base -> fine, the observed order, the extrapolated value and the fine-grid
convergence index are printed. Three solves: the fine one takes several times the base.

    python scripts/cfd_grid_convergence.py [--levels 4 5 6] [--which averaged]

Exit code 0: CL and CD change by no more than --limit (default 2 %) from base to fine;
1: they change by more (the base mesh is not converged: decide which level to keep);
2: it could not run.
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from cfd_regression import call, solve  # noqa: E402

from solidworks_mcp import server  # noqa: E402
from solidworks_mcp.rbr import flow_case, private, register  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--levels", type=int, nargs=3, default=None, metavar=("COARSE", "BASE", "FINE"))
    parser.add_argument("--which", choices=("averaged", "value"), default="averaged")
    parser.add_argument("--limit", type=float, default=2.0, help="allowed change base -> fine, percent")
    args = parser.parse_args()
    try:
        settings = private.load()
        ref = private.need("reference", settings)
        sandbox = private.need("sandbox_dir", settings)
        reduction = private.need("reference.reduction", settings)
        case = flow_case.load_case(os.path.join(private.need("cases_dir", settings), ref["case_name"] + ".json"))
    except (private.PrivateError, flow_case.CaseError) as exc:
        print("cannot run:", exc)
        return 2
    register(server)
    base_level = int(case["mesh"]["result_resolution"]) + 1
    levels = args.levels or [base_level - 1, base_level, base_level + 1]
    source = ref.get("carrier_model") or ref["model"]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    copy = os.path.join(sandbox, f"gridconv_{stamp}{os.path.splitext(source)[1]}")
    shutil.copy2(source, copy)
    if not call("open_part", path=copy).get("ok"):
        print("cannot run: the copy did not open")
        return 2
    rows = []
    try:
        first = f"GC_{stamp[-6:]}_L{base_level}"
        made = call("flow_create_from_case", case=ref["case_name"], name=first, velocity_mps=ref["velocity_mps"])
        if not made.get("ok"):
            print("cannot run:", made.get("error"))
            return 2
        for level in levels:
            name = f"GC_{stamp[-6:]}_L{level}"
            if level != base_level:
                cloned = call("flow_clone", name=first, new_name=name, mesh_level=level)
                if not cloned.get("ok"):
                    print("cannot run:", cloned.get("error"))
                    return 2
            else:
                call("flow_activate", name=first)
            t0 = time.time()
            record = solve(ref["case_name"] if level == base_level else None, None)
            run = (record.get("result") or {}).get("run") or {}
            if record.get("state") != "done" or not run.get("result"):
                print(f"level {level}: not solved - {record.get('error')}")
                return 2
            with open(run["result"], encoding="utf-8") as fh:
                stored = json.load(fh)
            reduced = (stored.get("reduced") or {}).get(args.which) or {}
            rows.append({"level": level, "cells": stored.get("cells"), **reduced})
            print(f"level {level}: {stored.get('cells')} cells, {time.time() - t0:.0f} s, "
                  + ", ".join(f"{k} {reduced[k]:.5f}" for k in ("CL", "CD", "Cm") if k in reduced), flush=True)
    finally:
        call("close_part", save=False)
        try:
            call("release_ui")
        except Exception:  # noqa: BLE001
            pass
    cells = tuple(r["cells"] for r in rows)
    worst = 0.0
    for key in ("CL", "CD", "Cm"):
        if not all(key in r for r in rows):
            continue
        out = flow_case.richardson(rows[0][key], rows[1][key], rows[2][key], cells)
        print(f"{key}: base -> fine {out['change_base_to_fine_pct']} %, order {out['order']}, "
              f"extrapolated {out['extrapolated']}, GCI(fine) {out['gci_fine_pct']} %, monotonic {out['monotonic']}")
        if key in ("CL", "CD") and out["change_base_to_fine_pct"] is not None:
            worst = max(worst, abs(out["change_base_to_fine_pct"]))
    print(f"=== largest change of CL / CD from base to fine: {worst:.3f} % (limit {args.limit} %)")
    return 0 if worst <= args.limit else 1


if __name__ == "__main__":
    sys.exit(main())
