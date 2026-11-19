"""Live checks of the 1.11.0 crash fixes that scripts/live_crash_scenario.py does not cover.

Run it after the scenario (it uses the scenario's cubes), with the server's own interpreter,
in a process of its own, never through MCP:

    <uv tools>\\solidworks-mcp\\Scripts\\python.exe scripts\\live_crash_extras.py

Like the scenario it needs the ONE running SolidWorks with NO document open, refuses to start
otherwise, and closes all documents at its end - unless one that is not its own is open by then.

What it asserts:
- with SWMCP_GDI_REFUSE_PCT lowered (a child process: the variable is read at import),
  new_part / new_assembly / open_part / open_assembly are refused, also from a script, and no
  document appears; a heavy call's reply carries the GDI hint;
- close_hidden_documents: a saved document with a window stays without windows=True and goes
  with it; a part an open assembly uses loses only its window and stays loaded;
- tree_folder: components that are not neighbours, adding to an existing folder, a component
  that is inside already, a name that does not exist (the folder is left as it was), a name
  taken by a feature that is no folder; the tree control read with the tree switched off;
- a message box SolidWorks raises INSIDE a COM call (ISldWorks.SendMsgToUser2 in a child
  process) is found, its text read (MSAA or UI Automation - both are tried and reported), and
  answered with dialog_respond's code;
- health.crash_evidence reads SolidWorks' own journals without failing.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MODE = sys.argv[1] if len(sys.argv) > 1 else ""
ROOT = os.path.join(tempfile.gettempdir(), "swmcp_crash_scenario")
os.environ["SWMCP_HOME"] = os.path.join(ROOT, "home")
MARKER = "swmcp live check 1.11.0: a message box raised inside a COM call"

from solidworks_mcp.errors import SolidWorksError  # noqa: E402
from solidworks_mcp.rbr import (assembly, context, dialogs, fastpath, guard, health, journal,  # noqa: E402
                                script_tool, speed, tree)
from solidworks_mcp.session import SolidWorksSession  # noqa: E402

failures = []


def check(ok, what, detail=""):
    print(("ok   " if ok else "FAIL ") + what + (f" - {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(what)


for module in (fastpath, speed, guard, journal, context, assembly):
    module.apply()
session = SolidWorksSession()
session.connect()
sw = session._sw
pid = int(sw.GetProcessID())
paths = [os.path.join(ROOT, f"cube_{i + 1}.sldprt") for i in range(7)]


def call(fn, *args):
    """A guarded call the way a tool makes it; a refusal comes back as {ok: False, error}."""
    try:
        return session.run_guarded(fn, *args)
    except SolidWorksError as exc:
        return {"ok": False, "error": str(exc)}


def run(code, **args):
    return call(script_tool.run_script, session, code, args)


def gdi():
    return int((health.gui_resources(pid) or {}).get("gdi") or 0)


def documents():
    return session.list_documents()["documents"]


# --- child: a message box inside a COM call ------------------------------------------------------
if MODE == "--msgbox":
    print("answered", sw.SendMsgToUser2(MARKER, 2, 2), flush=True)   # information, OK: blocks until pressed
    sys.exit(0)

# --- child: the GDI limits lowered through the environment ---------------------------------------
if MODE == "--refuse":
    check(health.GDI_REFUSE_PCT == 1.0 and health.GDI_WARN_PCT == 1.0, "the limits come from the environment")
    for name, args in (("new_part", ()), ("new_assembly", ()), ("open_part", (paths[0],)),
                       ("open_assembly", (os.path.join(ROOT, "none.sldasm"),))):
        reply = call(getattr(session, name), *args)
        check(not reply.get("ok") and "GDI objects" in reply.get("error", "") and "refused" in reply["error"],
              f"{name} is refused past the limit", reply.get("error", str(reply))[:150])
    reply = run("new_part()\nresult = 1")
    check(not reply.get("ok") and "GDI objects" in str(reply.get("error")),
          "new_part() inside a script is refused too", str(reply.get("error"))[:120])
    check(int(sw.GetDocumentCount()) == 0, "no document was opened by the refused calls")
    reply = run("result = 1")
    check(reply.get("ok") and "GDI objects" in str(reply.get("hint")) and reply.get("gdi_objects"),
          "a heavy call's reply carries the GDI hint", str(reply.get("gdi_objects")))
    check(any("GDI objects" in w for w in health.warnings_for(0, None, None, health.sldworks_gui(pid))),
          "server_health would warn")
    guard.release_ui(session)
    sys.exit(1 if failures else 0)

# --- the parent ----------------------------------------------------------------------------------
if int(sw.GetDocumentCount()):
    sys.exit("SolidWorks has documents open: this check closes all documents and will not run.")
if not all(os.path.isfile(p) for p in paths):
    sys.exit(f"Run scripts/live_crash_scenario.py first: its cubes are expected in {ROOT}")
own_titles = set()
start = gdi()
print(f"SolidWorks pid {pid}, GDI objects {start}", flush=True)


def title_of(path):
    return next(d["title"] for d in documents() if os.path.normcase(d["path"]) == os.path.normcase(path))


# 1. the refusal, in a child with the limits lowered
child = subprocess.run([sys.executable, os.path.abspath(__file__), "--refuse"], capture_output=True, text=True,
                       encoding="utf-8", timeout=300,
                       env={**os.environ, "SWMCP_GDI_REFUSE_PCT": "1", "SWMCP_GDI_WARN_PCT": "1"})
print("".join("     | " + line + "\n" for line in child.stdout.splitlines()), end="", flush=True)
check(child.returncode == 0, "window makers are refused past SWMCP_GDI_REFUSE_PCT", child.stderr[-300:])

# 2. close_hidden_documents and document windows
call(session.open_part, paths[0])
call(session.open_part, paths[1])
with_windows = gdi()
first, second = title_of(paths[0]), title_of(paths[1])
dry = call(health.close_hidden_documents, session, None, True)
check(dry.get("would_close") == [] and {"title": first, "why": "visible"} in dry.get("kept", []),
      "without windows=True a saved document with a window stays", str(dry)[:200])
dry = call(health.close_hidden_documents, session, None, True, True)
check(dry.get("would_close") == [first], "windows=True (dry run) names it", str(dry)[:200])
real = call(health.close_hidden_documents, session, None, False, True)
left = [d["title"] for d in documents()]
check(real.get("closed") == [first] and left == [second], "windows=True closes it; the current document stays",
      f"{real} / open: {left}"[:200])
print(f"     GDI objects: {start} with no document, {with_windows} with two windows, {gdi()} after closing one",
      flush=True)
call(session.close_part)
call(session.open_part, paths[2])
used = title_of(paths[2])
built = run("new_assembly()\nr = insert_component(args['p'], fixed=True)\n"
            "result = {'title': model.GetTitle(), 'name': r['component']['name']}", p=paths[2])
check(built.get("ok"), "an assembly that uses a part with a window of its own", str(built.get("error")))
own_titles.add(built["result"]["title"])
before = gdi()
real = call(health.close_hidden_documents, session, None, False, True)
state = {d["title"]: d["visible"] for d in documents()}
loaded = run("c = comp(args['n'])\nresult = {'loaded': c.GetModelDoc2() is not None, "
             "'suppression': c.GetSuppression2()}", n=built["result"]["name"])
check(real.get("closed") == [used] and state.get(used) is False and (loaded.get("result") or {}).get("loaded"),
      "a part the open assembly uses loses its window and stays loaded",
      f"closed {real.get('closed')}, visible {state}, component {loaded.get('result')}, GDI {before} -> {gdi()}")

# 3. tree_folder beyond the scenario
made = run("""
names = []
for index, path in enumerate(args["paths"]):
    for copy in range(2):
        r = insert_component(path, x_mm=20.0 * index, y_mm=20.0 + 20.0 * copy, z_mm=0.0, fixed=True)
        names.append(r["component"]["name"])
result = names
""", paths=paths[:4])
names = made.get("result") or []
check(made.get("ok") and len(names) == 8, "eight more components", str(made.get("error") or names))
reply = call(tree.tree_folder, session, "Mixed", [names[0], names[3]])
check(reply.get("ok") and reply.get("created") and reply.get("count") == 2 and reply.get("tree_items") == 2,
      "a folder of two components that are not neighbours (tree control read inside the call)", str(reply)[:250])
reply = call(tree.tree_folder, session, "Mixed", [names[6]])
check(reply.get("ok") and reply.get("created") is False and reply.get("added") == [names[6]]
      and reply.get("count") == 3, "a component is added to the existing folder", str(reply)[:250])
reply = call(tree.tree_folder, session, "Mixed", [names[0], names[7]])
check(reply.get("ok") and reply.get("added") == [names[7]] and reply.get("count") == 4,
      "one inside already and one new: only the new one is added", str(reply)[:250])
reply = call(tree.tree_folder, session, "Mixed", ["no_such_part-9"])
check(not reply.get("ok"), "a component that does not exist is an error", str(reply.get("error"))[:120])
reply = call(tree.tree_folder, session, names[1], [names[2]])
check(not reply.get("ok") and "not a folder" in str(reply.get("error")),
      "a name taken by a feature that is no folder is an error", str(reply.get("error"))[:120])
reply = call(tree.tree_folder, session, "Empty", ["no_such_part-9"])
listed = call(tree.list_tree_folders, session)
check(not reply.get("ok") and [(f["folder"], f["count"]) for f in listed.get("folders", [])] == [("Mixed", 4)],
      "after the failed calls there is one folder, with its four components", str(listed.get("folders"))[:250])
items = tree.tree_items(session, session._model, "Mixed")
check(items is not None and len(items) == 4, "the tree control shows four items under it", str(items))

# 4. a message box SolidWorks raises inside a COM call
box = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--msgbox"], stdout=subprocess.PIPE, text=True,
                       encoding="utf-8")
found, deadline = None, time.time() + 20
while found is None and time.time() < deadline and box.poll() is None:
    time.sleep(0.5)
    found = next((d for d in dialogs.blocking(pids=[pid])), None)
if found is None:
    check(False, "the message box raised inside a COM call is found", f"child exit {box.poll()}")
else:
    by_uia = dialogs.uia_text(found["hwnd"])
    print(f"     dialog: {json.dumps(dialogs.public(found), ensure_ascii=False)}", flush=True)
    check(MARKER in (found.get("text") or ""), "its text is read",
          f"through {found.get('text_source') or 'a control'}")
    check(MARKER in by_uia, "UI Automation alone reads the text too", by_uia[:120])
    pressed = dialogs.respond("ok", hwnd=found["hwnd"], pids=[pid])
    check(pressed.get("ok"), "it is answered", str(pressed)[:200])
try:
    out = box.communicate(timeout=20)[0]
    check("answered" in out, "the blocked COM call returns after the answer", out.strip())
except subprocess.TimeoutExpired:
    box.kill()
    check(False, "the blocked COM call returns after the answer", "the child was still waiting after 20 s")

# 5. SolidWorks' own journals
try:
    evidence = health.crash_evidence()
    check(True, "crash_evidence reads SolidWorks' journals",
          "nothing recorded" if evidence is None else json.dumps(evidence, ensure_ascii=False)[:300])
except Exception as exc:  # noqa: BLE001
    check(False, "crash_evidence reads SolidWorks' journals", repr(exc))

# --- leave SolidWorks as it was found --------------------------------------------------------------
inside = os.path.normcase(ROOT) + os.sep
foreign = [d["title"] for d in documents()
           if (not os.path.normcase(os.path.abspath(d["path"])).startswith(inside) if d["path"]
               else d["title"] not in own_titles)]
if foreign:
    check(False, "only this check's documents are open at the end", f"{foreign} - nothing was closed")
else:
    sw.CloseAllDocuments(True)
guard.release_ui(session)
print(json.dumps({"ok": not failures, "failed": failures, "gdi_start": start, "gdi_end": gdi(),
                  "documents_open": int(sw.GetDocumentCount())}, ensure_ascii=False), flush=True)
sys.exit(1 if failures else 0)
