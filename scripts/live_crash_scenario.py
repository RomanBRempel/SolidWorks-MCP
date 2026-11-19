"""Live check of the 1.11.0 crash fixes: a cube, an assembly of 70 components, 6 tree folders,
ReorderComponents, CloseAllDocuments inside a script, the same again - and SolidWorks still there.

Run it with the server's own interpreter, in a process of its own, never through MCP:

    <uv tools>\\solidworks-mcp\\Scripts\\python.exe scripts\\live_crash_scenario.py [folder]

It needs the ONE running SolidWorks with NO document open and refuses to start otherwise: it
calls CloseAllDocuments. It works only on files it creates in `folder` (default: a
swmcp_crash_scenario folder in the temp directory) and on its own journal folder inside it.
Never start a second SolidWorks for it.

What it asserts (exit code 1 and a FAIL line otherwise):
- 70 inserts open no document window: one visible document (the assembly), and SolidWorks'
  GDI object count grows by less than one window's worth (upstream's insert_component took
  about 250 per inserted file and ran the process into its 10 000 limit);
- 6 folders hold exactly the components asked for, read back from the folder features and
  from the tree control;
- raw ReorderComponents moves a component INTO a folder with where=3 and not with where=2;
- a script that calls swApp.CloseAllDocuments(True) and then builds a new assembly, using
  model / asm / ui_unlocked() afterwards, runs to its end;
- SolidWorks is the same process at the end.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(),
                                                                            "swmcp_crash_scenario")
os.makedirs(ROOT, exist_ok=True)
os.environ["SWMCP_HOME"] = os.path.join(ROOT, "home")  # own journal and ownership records

from solidworks_mcp.rbr import (assembly, context, fastpath, guard, health, journal,  # noqa: E402
                                script_tool, speed, tree)
from solidworks_mcp.session import SolidWorksSession  # noqa: E402

PARTS = 7           # different cubes
COPIES = 10         # of each: 70 components, inserted round-robin so no group is contiguous
FOLDERS = 6
failures = []


def check(ok, what, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f" - {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(what)


def gdi(pid):
    counts = health.gui_resources(pid) or {}
    return int(counts.get("gdi") or 0)


for module in (fastpath, speed, guard, journal, context, assembly):
    module.apply()
session = SolidWorksSession()
session.connect()
sw = session._sw
pid = int(sw.GetProcessID())
if int(sw.GetDocumentCount()):
    sys.exit("SolidWorks has documents open: this scenario closes all documents and will not run. "
             "Close them yourself and start it again.")


def run(code, **args):
    return session.run_guarded(script_tool.run_script, session, code, args)


own_titles = set()   # the unsaved assemblies this scenario made


def foreign_documents():
    """Open documents that are not this scenario's: saved outside its folder, or unsaved and
    not made here. Someone else opened them while it ran - CloseAllDocuments must not run."""
    inside = os.path.normcase(ROOT) + os.sep
    return [d["title"] for d in session.list_documents()["documents"]
            if (not os.path.normcase(os.path.abspath(d["path"])).startswith(inside) if d["path"]
                else d["title"] not in own_titles)]


paths = [os.path.join(ROOT, f"cube_{i + 1}.sldprt") for i in range(PARTS)]
start = gdi(pid)
print(f"SolidWorks pid {pid}, GDI objects {start}", flush=True)

# --- the cubes -------------------------------------------------------------------------------
for i, path in enumerate(paths):
    if not os.path.isfile(path):
        session.run_guarded(session.new_part)
        session.run_guarded(session.add_box, 10.0 + i, 10.0, 10.0)
        session.run_guarded(session.save_part, path)
        session.run_guarded(session.close_part)
check(all(os.path.isfile(p) for p in paths), f"{PARTS} cube parts on disk")

BUILD = """
import time
new_assembly()
names = []
t0 = time.perf_counter()
for copy in range(args["copies"]):
    for index, path in enumerate(args["paths"]):
        r = insert_component(path, x_mm=20.0 * index, y_mm=20.0 * copy, z_mm=0.0, fixed=True)
        names.append(r["component"]["name"])
# Transform2 on a FIXED component right after the insert, with no rebuild in between
mu = wrap(swApp.GetMathUtility(), mod.IMathUtility)
c = comp(names[-1])
c.Transform2 = mu.CreateTransform(vec(1, 0, 0, 0, 1, 0, 0, 0, 1, 0.5, 0.5, 0.0, 1.0, 0, 0, 0))
moved = [round(v, 4) for v in wrap(c.Transform2, mod.IMathTransform).ArrayData[9:12]]
result = {"title": model.GetTitle(), "names": names, "insert_s": round(time.perf_counter() - t0, 1),
          "fixed_moved_to_m": moved, "components": len(asm.GetComponents(True) or ())}
"""

FOLDERS_CODE = """
out = {}
for index in range(args["folders"]):
    group = [n for n in args["names"] if n.startswith("cube_%d-" % (index + 1))]
    with ui_unlocked():          # as the script of 2026-11-19 did around its folder calls
        out["Folder %d" % (index + 1)] = tree_folder("Folder %d" % (index + 1), group)["count"]
result = {"made": out, "folders": tree_folders()["folders"]}
"""

REORDER = """
import pythoncom, win32com.client
def into(name, folder, where):
    arr = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [comp(name)._oleobj_])
    answered = asm.ReorderComponents(arr, feature(folder), where)
    inside = {f["folder"]: f["components"] for f in tree_folders()["folders"]}
    return {"answered": answered, "inside": name in inside[folder]}
loose = args["loose"]
result = {"where_2": into(loose[0], "Folder 1", 2), "where_3": into(loose[1], "Folder 1", 3)}
"""

CLOSE_AND_REBUILD = """
old = model.GetTitle()
swApp.CloseAllDocuments(True)
left = swApp.GetDocumentCount()
""" + BUILD + """
with ui_unlocked():
    after = model.GetTitle()
result.update(closed=old, left_open=left, new_title=after, fm_ok=fm is not None)
"""


def build_and_file(label, code):
    before = gdi(pid)
    built = run(code, paths=paths, copies=COPIES)
    check(built.get("ok") and built["result"]["components"] == PARTS * COPIES,
          f"{label}: {PARTS * COPIES} components inserted", f"{built.get('result', built)}"[:200])
    data = built["result"]
    own_titles.add(data["title"])
    grown = gdi(pid) - before
    visible = [d["title"] for d in session.list_documents()["documents"] if d["visible"]]
    check(visible == [data["title"]], f"{label}: the assembly is the only document with a window", str(visible))
    check(grown < 2 * health.GDI_PER_WINDOW, f"{label}: GDI objects grew by {grown} for 70 inserts "
                                             f"(one new assembly window is about {health.GDI_PER_WINDOW})")
    print(f"     inserts took {data['insert_s']} s; a fixed component written with Transform2 reads back at "
          f"{data['fixed_moved_to_m']} m", flush=True)
    filed = run(FOLDERS_CODE, names=data["names"], folders=FOLDERS)
    folders = {f["folder"]: f["components"] for f in (filed.get("result") or {}).get("folders", [])}
    check(filed.get("ok") and len(folders) == FOLDERS and all(len(v) == COPIES for v in folders.values()),
          f"{label}: {FOLDERS} folders with {COPIES} components each", str({k: len(v) for k, v in folders.items()}))
    for index in range(FOLDERS):
        name = f"Folder {index + 1}"
        items = tree.tree_items(session, session._model, name)
        check(items is not None and len(items) == COPIES, f"{label}: the tree control shows {COPIES} under '{name}'",
              str(None if items is None else len(items)))
    return data


first = build_and_file("first assembly", BUILD)
loose = [n for n in first["names"] if n.startswith(f"cube_{PARTS}-")][:2]   # the part left out of the folders
moved = run(REORDER, loose=loose)
res = moved.get("result") or {}
check(moved.get("ok") and res.get("where_3", {}).get("inside") is True,
      "ReorderComponents(where=3) puts a component into the folder", str(res.get("where_3")))
check(moved.get("ok") and res.get("where_2", {}).get("inside") is False,
      "ReorderComponents(where=2) answers True and leaves it outside", str(res.get("where_2")))
after_insert = run("r = insert_component(args['path'], 0, 0, 50.0)\nresult = r['component']['name']", path=paths[0])
check(after_insert.get("ok"), "an insert after the folder work succeeds", str(after_insert.get("result")))

foreign = foreign_documents()
if foreign:
    sys.exit(f"Documents that are not this scenario's were opened while it ran: {foreign}. It stops here "
             "and closes nothing; its own documents are the unsaved assemblies and the cubes in " + ROOT)
second = run(CLOSE_AND_REBUILD, paths=paths, copies=COPIES)
data = second.get("result") or {}
if data.get("title"):
    own_titles.add(data["title"])
check(second.get("ok") and data.get("left_open") == 0 and data.get("components") == PARTS * COPIES,
      "a script that calls CloseAllDocuments goes on with the new assembly (model, asm, fm, ui_unlocked)",
      f"{second.get('error') or data}"[:300])
if second.get("ok"):
    filed = run(FOLDERS_CODE, names=data["names"], folders=FOLDERS)
    check(filed.get("ok") and all(v == COPIES for v in filed["result"]["made"].values()),
          "folders in the second assembly are filled", str((filed.get("result") or {}).get("made")))

check(health.process_alive(pid) and int(sw.GetProcessID()) == pid, "SolidWorks is the same process, alive")
end = gdi(pid)
load = health.sldworks_gui(pid) or {}
print(f"GDI objects: {start} at the start, {end} at the end ({load.get('gdi_pct')}% of the quota)", flush=True)

# --- leave SolidWorks as it was found: no documents, UI released --------------------------------
time.sleep(0.5)
foreign = foreign_documents()
if foreign:
    check(False, "only this scenario's documents are open at the end", f"{foreign} - nothing was closed")
else:
    sw.CloseAllDocuments(True)   # the unsaved assemblies and the cubes, nothing else
guard.release_ui(session)
print(json.dumps({"ok": not failures, "failed": failures, "gdi_start": start, "gdi_end": gdi(pid),
                  "documents_open": int(sw.GetDocumentCount()), "folder": ROOT}, ensure_ascii=False), flush=True)
sys.exit(1 if failures else 0)
