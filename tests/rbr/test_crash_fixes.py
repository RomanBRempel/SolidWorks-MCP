"""1.11.0: what kept SolidWorks from running out of window resources and dying, on fakes.

The measurements behind the numbers (GDI objects per document window, what a
folder call really moves) were taken on a live SolidWorks 2025 SP3 and are
written down in rbr/assembly.py, rbr/health.py and rbr/tree.py; the live
scenario is scripts/live_crash_scenario.py.
"""

import types

import pytest
import pythoncom

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import assembly, guard, health, script_tool, tree


def com_error(hresult, description=None, code=0):
    info = (code, "SOLIDWORKS", description, None, 0, 0) if description else None
    try:
        exc = pythoncom.com_error(hresult, "text", info, None)
    except TypeError:  # the stub off Windows
        exc = pythoncom.com_error("text")
    for name, value in (("hresult", hresult), ("excepinfo", info), ("strerror", "text")):
        if getattr(exc, name, None) != value:
            try:
                setattr(exc, name, value)
            except (AttributeError, TypeError):
                pass
    return exc


# --- the script namespace: document names are looked up when read -----------------------------

def test_namespace_resolves_live_names_at_every_read_also_inside_functions():
    box = {"doc": "first"}
    ns = script_tool.Namespace({"model": lambda: box["doc"]})
    ns["seen"] = []
    code = ("seen.append(model)\n"
            "def later():\n"
            "    return model\n"
            "box['doc'] = 'second'\n"
            "seen.append(model)\n"
            "seen.append(later())\n")
    ns["box"] = box
    exec(compile(code, "<t>", "exec"), ns)  # noqa: S102
    assert ns["seen"] == ["first", "second", "second"]


def test_namespace_keeps_what_the_script_assigns_and_reports_missing_names():
    ns = script_tool.Namespace({"model": lambda: "live", "asm": lambda: (_ for _ in ()).throw(KeyError("asm"))})
    exec("a = model\nmodel = 'mine'\nb = model\n", ns)  # noqa: S102
    assert (dict.__getitem__(ns, "a"), dict.__getitem__(ns, "b")) == ("live", "mine")
    assert "model" in ns and ns.get("nothing") is None
    with pytest.raises(NameError):
        exec("asm.AddComponent5()", ns)  # noqa: S102 - not an assembly: an undefined name, as before


def test_script_follows_the_document_it_switched_to(fake_session):
    from conftest import FakeModel
    other = FakeModel(["A"], title="Other.SLDPRT")
    fake_session.other = other
    res = script_tool.run_script(
        fake_session, "a = model.GetTitle()\nsession._model = session.other\nresult = [a, model.GetTitle()]")
    assert res["result"] == ["P.SLDPRT", "Other.SLDPRT"]


def test_closed_current_document_is_dropped_and_the_active_one_taken(fake_session):
    from conftest import FakeModel
    dead = fake_session._model
    alive = FakeModel(["A"], title="Alive.SLDPRT")
    fake_session._sw.ActiveDoc = alive

    def gone():
        raise com_error(script_tool.RPC_E_DISCONNECTED)

    dead.GetTitle = gone
    current, live = script_tool.live_documents(fake_session)
    assert current() is alive and fake_session._model is None
    assert live["part"]() is alive
    with pytest.raises(KeyError):
        live["asm"]()


def test_stale_object_in_a_script_is_explained_not_raised_as_a_fault(fake_session):
    fake_session.boom = lambda: (_ for _ in ()).throw(com_error(script_tool.RPC_E_DISCONNECTED))
    with pytest.raises(SolidWorksError) as err:
        script_tool.run_script(fake_session, "session.boom()")
    text = str(err.value)
    assert "its document was closed" in text and "session.boom()" not in text.split("\n")[0]
    faults_before = guard.state()["faults"]
    with pytest.raises(SolidWorksError):
        fake_session.run_guarded(script_tool.run_script, fake_session, "session.boom()")
    assert guard.state()["faults"] == faults_before  # the script's mistake, not SolidWorks' fault


def test_close_all_documents_objections():
    docs = [{"title": "Mine", "modified": True}, {"title": "Saved", "modified": False},
            {"title": "Users", "modified": True}, {"title": "Theirs", "modified": False}]
    owners = {"Mine": {"pid": 7}, "Theirs": {"pid": 9}}
    blocked = script_tool.close_all_objections(docs, owners, own_pid=7)
    assert len(blocked) == 2 and "'Users'" in blocked[0] and "process 9" in blocked[1]
    assert script_tool.close_all_objections(docs[:2], owners, own_pid=7) == []


def test_script_with_close_all_is_refused_before_it_runs(fake_session, monkeypatch):
    doc = fake_session._model
    doc.GetSaveFlag = lambda: True
    monkeypatch.setattr(type(fake_session), "_open_documents", lambda self: [doc])
    doc.ran = False
    with pytest.raises(SolidWorksError, match="CloseAllDocuments"):
        script_tool.run_script(fake_session, "model.ran = True\nswApp.CloseAllDocuments(True)")
    assert doc.ran is False


# --- window resources ---------------------------------------------------------------------------

def test_gui_load_and_refusal():
    load = health.gui_load({"gdi": 9500, "user": 3000, "gdi_peak": 9600, "user_peak": 3100}, 10000)
    assert load["gdi_pct"] == 95.0 and load["windows_left"] == 2
    assert "9500 of the 10000 GDI objects" in health.gui_refusal(load)
    assert health.gui_refusal(health.gui_load({"gdi": 5000, "user": 1}, 10000)) is None
    assert health.gui_refusal(None) is None and health.gui_load(None) is None


def test_gdi_warning_comes_first_and_only_past_the_threshold():
    warn = health.warnings_for(10, None, None, health.gui_load({"gdi": 7600, "user": 1}, 10000))
    assert len(warn) == 1 and "7600 of 10000 GDI objects" in warn[0] and "9 more document windows" in warn[0]
    assert health.warnings_for(10, None, None, health.gui_load({"gdi": 3000, "user": 1}, 10000)) == []


def test_load_hints_for_a_heavy_call():
    hints = guard.load_hints(56, health.gui_load({"gdi": 9100, "user": 1}, 10000))
    assert len(hints) == 2 and "56 documents" in hints[0] and "close_hidden_documents" in hints[0]
    assert "GDI" in hints[1]
    assert guard.load_hints(30, None) == [] and guard.load_hints(None, None) == []


def test_window_makers_are_refused_when_no_window_fits(fake_session, monkeypatch):
    monkeypatch.setattr(health, "sldworks_gui", lambda pid=None: health.gui_load({"gdi": 9900, "user": 1}, 10000))
    with pytest.raises(SolidWorksError, match="new_part refused.*GDI objects"):
        fake_session.new_part()
    with pytest.raises(SolidWorksError, match="open_assembly refused"):
        fake_session.open_assembly("x.sldasm")


JOURNAL_END = """Set Part = swApp.ActiveDoc
swApp.ActivateDoc2 "Part9.SLDPRT", False, longstatus
' MSGBOX
' Запрашиваемый ресурс недоступен.

' MSGBOX
' Запрашиваемый ресурс недоступен.

Part.GraphicsRedraw2

'The stack trace for this error is:
'Build: d250516.003.P9T sw2025_SP03
'{mfc140u:7FF80E442972:00292972}\tmfc140u:Unknown
'{sldappu:45B7E2A8:0047E2A8}\tsldappu:uiBaseView_c::setupDC
'{slduiu:4951A9F7:004DA9F7}\tslduiu:uiModelView_c::OnMouseMove
'{mfc140u:7FF80E44849E:0029849E}\tmfc140u:Unknown
'{USER32:7FF877FA52E6:000152E6}\tUSER32:CallWindowProcW
"""


def test_journal_evidence_reads_the_crash_stack_and_the_resource_messages():
    found = health.journal_evidence(JOURNAL_END)
    assert found["resource_messages"] == 2
    assert found["stack"][:3] == ["mfc140u:Unknown", "sldappu:uiBaseView_c::setupDC",
                                  "slduiu:uiModelView_c::OnMouseMove"]
    assert found["last_call"] == "Part.GraphicsRedraw2"
    assert health.journal_evidence("Part.ClearSelection2 True\n") == {
        "stack": [], "resource_messages": 0, "last_call": "Part.ClearSelection2 True"}


def test_lost_message_names_the_last_call_and_the_cause():
    last = {"tool": "run_script", "doc": "Asm6", "at": "06:24:16", "ok": False, "error": "61704 internal error"}
    text = health.lost_message(44252, last, {**health.journal_evidence(JOURNAL_END), "journal": "swxJRNL.BAK"})
    assert text.startswith("SolidWorks crashed or was closed: its process (pid 44252) is gone")
    assert "'run_script' on 'Asm6' at 06:24:16" in text and "61704" in text
    assert "ran out of GDI objects" in text and "uiBaseView_c::setupDC" in text
    assert "Start SolidWorks and call again" in text
    again = health.lost_message(44252, None, None, new_pid=8036, during="get_status")
    assert "noticed in 'get_status'" in again and "pid 8036" in again


def test_connect_failure_after_a_crash_says_so(fake_session, monkeypatch):
    from solidworks_mcp.session import SolidWorksSession
    guard._set(sw_pid=999999, last_call={"tool": "insert_component", "doc": "Asm1", "at": "06:25:37",
                                         "ts": 1.0, "ok": False, "error": "x"})
    monkeypatch.setattr(guard, "_gone_for_good", lambda pid: True)
    monkeypatch.setattr(health, "crash_evidence", lambda since=None: None)

    def no_solidworks(self):
        raise SolidWorksError("No running SolidWorks found. (COM error: operation unavailable)")

    monkeypatch.setitem(guard._ORIGINAL, "connect", no_solidworks)
    try:
        with pytest.raises(SolidWorksError) as err:
            SolidWorksSession.connect(fake_session)
        assert "SolidWorks crashed or was closed" in str(err.value)
        assert "'insert_component' on 'Asm1' at 06:25:37" in str(err.value)
        assert "operation unavailable" not in str(err.value)
    finally:
        guard._set(sw_pid=None, last_call=None, lost=None)


def test_finished_calls_are_remembered_and_a_restart_is_told_once(fake_session):
    try:
        fake_session.run_guarded(lambda: {"ok": True})
        assert guard.state()["last_call"]["ok"] is True
        guard._set(lost="SolidWorks crashed or was closed: its process (pid 1) is gone.")
        first = fake_session.run_guarded(lambda: {"ok": True})
        second = fake_session.run_guarded(lambda: {"ok": True})
        assert "crashed or was closed" in first["solidworks_lost"] and "solidworks_lost" not in second
    finally:
        guard._set(last_call=None, lost=None)


# --- insert_component: the file is loaded without a window ----------------------------------------

class _App:
    def __init__(self, open_already=False, opens=True):
        self.calls, self.open_already, self.opens, self.ActiveDoc = [], open_already, opens, None

    def GetOpenDocumentByName(self, path):
        return object() if self.open_already else None

    def DocumentVisible(self, visible, doc_type):
        self.calls.append(("visible", visible, doc_type))

    def OpenDoc6(self, path, doc_type, options, config, errors, warnings):
        self.calls.append(("open", path, options))
        if self.opens is None:
            raise com_error(-2147352567, "boom", 1)
        return (object(), 0, 0) if self.opens else (None, 2, 0)

    def ActivateDoc3(self, title, *a):
        self.calls.append(("activate", title))


def _host(app, title="Asm1"):
    doc = types.SimpleNamespace(GetTitle=lambda: title)
    app.ActiveDoc = doc
    return types.SimpleNamespace(_sw=app, _model=doc, _mod=types.SimpleNamespace(IModelDoc2=lambda o: o))


def test_load_hidden_switches_visibility_off_and_always_back_on():
    app = _App()
    assert assembly.load_hidden(_host(app), r"C:\p\a.sldprt", 1) is True
    assert app.calls == [("visible", False, 1), ("open", r"C:\p\a.sldprt", 1), ("visible", True, 1)]
    failing = _App(opens=None)
    with pytest.raises(pythoncom.com_error):
        assembly.load_hidden(_host(failing), r"C:\p\a.sldprt", 1)
    assert failing.calls[-1] == ("visible", True, 1)


def test_load_hidden_skips_an_open_document_and_refuses_a_file_that_does_not_load():
    app = _App(open_already=True)
    assert assembly.load_hidden(_host(app), r"C:\p\a.sldprt", 1) is False and app.calls == []
    with pytest.raises(SolidWorksError, match="could not load.*swFileLoadError_e 2"):
        assembly.load_hidden(_host(_App(opens=False)), r"C:\p\a.sldprt", 1)


def test_load_hidden_reactivates_the_assembly_only_when_it_lost_the_front():
    app = _App()
    host = _host(app)
    assembly.load_hidden(host, r"C:\p\a.sldprt", 1)
    assert not [c for c in app.calls if c[0] == "activate"]
    app.calls.clear()
    real_open = app.OpenDoc6

    def steals_front(*a):
        app.ActiveDoc = types.SimpleNamespace(GetTitle=lambda: "a.sldprt")
        return real_open(*a)

    app.OpenDoc6 = steals_front
    assembly.load_hidden(host, r"C:\p\a.sldprt", 1)
    assert ("activate", "Asm1") in app.calls


def test_com_text_prefers_solidworks_own_words():
    assert assembly.com_text(com_error(-2147352567, "Internal application error.", 61704)) == \
        "Internal application error. (code 61704)"
    assert assembly.com_text(com_error(-2147417848)) == "text"


def test_insert_component_patch_keeps_the_upstream_original_reachable():
    from solidworks_mcp.session import SolidWorksSession
    upstream = SolidWorksSession.insert_component
    assembly.apply()
    try:
        assert SolidWorksSession.insert_component is assembly.insert_component
        assert SolidWorksSession.insert_component.__wrapped__ is upstream
    finally:
        assembly.unapply()
    assert SolidWorksSession.insert_component is upstream


# --- tree folders ------------------------------------------------------------------------------------

def test_tree_label_and_missing_checks():
    assert tree.tree_label("Nut_M6-12") == "Nut_M6<12>"
    assert tree.tree_label("Plain") == "Plain"
    assert tree.missing_from(["A-1", "B-1"], ["a-1"]) == ["B-1"]
    items = ["(f) A<1> (Default) <<Default>_Display State 1>", "(-) B<2> (Default)"]
    assert tree.missing_in_tree(["A-1", "B-2"], items) == []
    assert tree.missing_in_tree(["A-1", "B-1"], items) == ["B-1"]
    # a whole word: 'A<1>' is not shown by 'AA<1>', nor 'B<2>' by 'B<21>'
    assert tree.missing_in_tree(["A-1", "B-2"], ["(f) AA<1> (Default)", "B<21>"]) == ["A-1", "B-2"]
    assert tree.shows("(ф) Folder 1", "Folder 1") and tree.shows("Folder 1", "folder 1")
    assert not tree.shows("(ф) Folder 10", "Folder 1") and not tree.shows("Old Folder 1x", "Folder 1")


def test_tree_folder_argument_refusals(fake_session, monkeypatch):
    monkeypatch.setattr(type(fake_session), "_require_assembly", lambda self: self._model, raising=False)
    monkeypatch.setattr(type(fake_session), "_components", lambda self, asm: [])
    with pytest.raises(SolidWorksError, match="name"):
        tree.tree_folder(fake_session, "  ", ["A-1"])
    with pytest.raises(SolidWorksError, match="list of component names"):
        tree.tree_folder(fake_session, "F", "A-1")
    with pytest.raises(SolidWorksError, match="sub-assembly"):
        tree.tree_folder(fake_session, "F", ["Sub-1/A-1"])


class _Comp:
    def __init__(self, name):
        self.Name2, self._oleobj_ = name, name

    def Select4(self, *a):
        return True


class _FolderAsm:
    """An assembly whose folders behave as SolidWorks 2025 does (see rbr/tree.py)."""

    def __init__(self, comps, containing_works=True, reorder_works=True):
        self.comps = {c.Name2: c for c in comps}
        self.folders = {}
        self.containing_works, self.reorder_works = containing_works, reorder_works
        self.selected, self.deleted = [], []
        self.reorder_calls = []

    def FeatureByName(self, name):
        if name not in self.folders:
            return None
        asm = self
        spec = types.SimpleNamespace(GetFeatures=lambda: [types.SimpleNamespace(Name=n) for n in asm.folders[name]])
        return types.SimpleNamespace(Name=name, GetTypeName2=lambda: "FtrFolder", GetSpecificFeature2=lambda: spec)

    def ReorderComponents(self, array, folder, where):
        self.reorder_calls.append(where)
        if self.reorder_works and where in (3, 4):
            self.folders[folder.Name] += [n for n in self.pending if n not in self.folders[folder.Name]]
        return True  # as SolidWorks does, whatever happened


def _folder_session(monkeypatch, fake_session, asm, tree_shows=True):
    model = fake_session._model
    monkeypatch.setattr(type(fake_session), "_require_assembly", lambda self: asm, raising=False)
    monkeypatch.setattr(type(fake_session), "_components", lambda self, a: list(asm.comps.values()))
    monkeypatch.setattr(type(fake_session), "_component_by_name", lambda self, a, name: asm.comps[name])
    monkeypatch.setattr(tree, "_select", lambda session, m, comps: (asm.selected.clear(), asm.selected.extend(
        c.Name2 for c in comps), len(comps))[2])
    monkeypatch.setattr(tree, "tree_items", lambda session, m, name: [
        tree.tree_label(n) for n in asm.folders.get(name, [])] if tree_shows else [])
    monkeypatch.setattr(tree, "_delete_folder", lambda m, name: asm.deleted.append(asm.folders.pop(name, None)))
    monkeypatch.setattr(tree.win32com.client, "VARIANT", lambda kind, items: setattr(asm, "pending", list(items)),
                        raising=False)

    def insert(kind):
        made = types.SimpleNamespace(Name="Folder1")
        asm.folders["Folder1"] = list(asm.selected) if (kind == 2 and asm.containing_works) else []

        class Made:
            @property
            def Name(self):
                return made.Name

            @Name.setter
            def Name(self, value):
                asm.folders[value] = asm.folders.pop(made.Name)
                made.Name = value
        return Made()

    model.FeatureManager.InsertFeatureTreeFolder2 = insert
    return fake_session


def test_tree_folder_creates_a_filled_folder(fake_session, monkeypatch):
    asm = _FolderAsm([_Comp("A-1"), _Comp("B-1"), _Comp("C-1")])
    s = _folder_session(monkeypatch, fake_session, asm)
    res = tree.tree_folder(s, "Fasteners", ["A-1", "B-1"])
    assert res == {"ok": True, "folder": "Fasteners", "created": True, "count": 2, "components": ["A-1", "B-1"],
                   "added": ["A-1", "B-1"], "tree_items": 2}
    assert asm.reorder_calls == []


def test_tree_folder_adds_to_an_existing_folder_at_its_end(fake_session, monkeypatch):
    asm = _FolderAsm([_Comp("A-1"), _Comp("B-1"), _Comp("C-1")])
    asm.folders["Fasteners"] = ["A-1"]
    s = _folder_session(monkeypatch, fake_session, asm)
    res = tree.tree_folder(s, "Fasteners", ["A-1", "C-1"])
    assert res["created"] is False and res["components"] == ["A-1", "C-1"] and res["added"] == ["C-1"]
    assert asm.reorder_calls == [tree.SW_REORDER_LAST_IN_FOLDER]


def test_tree_folder_fails_loud_and_removes_the_empty_folder_it_made(fake_session, monkeypatch):
    asm = _FolderAsm([_Comp("A-1")], containing_works=False, reorder_works=False)
    s = _folder_session(monkeypatch, fake_session, asm)
    with pytest.raises(SolidWorksError, match="was not filled as asked.*removed again"):
        tree.tree_folder(s, "Fasteners", ["A-1"])
    assert asm.deleted == [[]] and "Fasteners" not in asm.folders


def test_tree_folder_trusts_the_tree_control_over_the_folder_feature(fake_session, monkeypatch):
    asm = _FolderAsm([_Comp("A-1")])
    s = _folder_session(monkeypatch, fake_session, asm, tree_shows=False)
    with pytest.raises(SolidWorksError, match="tree control does not show"):
        tree.tree_folder(s, "Fasteners", ["A-1"])


def test_components_are_resolved_from_one_listing(fake_session, monkeypatch):
    """Upstream's lookup wraps every component for every name (0.3 s a name at 70 components):
    exact instance names come from one listing, anything else still goes to upstream."""
    comps, listings, asked = [_Comp("A-1"), _Comp("A-2"), _Comp("B-1")], [], []
    monkeypatch.setattr(type(fake_session), "_components", lambda self, a: listings.append(1) or comps)
    monkeypatch.setattr(type(fake_session), "_component_by_name",
                        lambda self, a, name: asked.append(name) or comps[2])
    found = tree.resolve_components(fake_session, object(), ["a-2", "A-1", "A-2", "B"])
    assert [n for n, _ in found] == ["A-2", "A-1", "B-1"]      # no repeats, the order asked for
    assert listings == [1] and asked == ["B"]                   # a part name alone: upstream's rules


class _Item:
    """A tree control item: its text carries a status mark, the feature behind it has the name."""

    def __init__(self, text, obj=None, kids=()):
        self.Text, self.Object, self.kids, self.next = text, obj, list(kids), None
        for a, b in zip(self.kids, self.kids[1:]):
            a.next = b

    def GetFirstChild(self):
        return self.kids[0] if self.kids else None

    def GetNext(self):
        return self.next


def _feature(name, kind="FtrFolder"):
    return types.SimpleNamespace(Name=name, GetTypeName2=lambda: kind)


def test_tree_items_finds_the_folder_behind_a_status_mark(fake_session):
    """Live, 2026-11-19: a folder of fixed components reads '(f) Folder A' in the tree control;
    looking for the bare name found nothing and the tree check was skipped."""
    root = _Item("Assem1 (Default)", kids=[
        _Item("Annotations", _feature("Annotations", "DetailCabinet")),
        _Item("(f) Folder A<1> (Default)", types.SimpleNamespace(Name2="Folder A-1")),   # a component, not a folder
        _Item("(f) Folder A", _feature("Folder A"), kids=[_Item("(f) cube_1<1> (Default)"),
                                                          _Item("(f) cube_4<1> (Default)")]),
        _Item("(-) Folder AB", _feature("Folder AB"), kids=[_Item("(-) cube_2<1> (Default)")]),
    ])
    fm = fake_session._model.FeatureManager
    fm.EnableFeatureTree, fm.UpdateFeatureTree, fm.GetFeatureTreeRootItem2 = True, lambda: None, lambda pane: root
    items = tree.tree_items(fake_session, fake_session._model, "Folder A")
    assert items == ["(f) cube_1<1> (Default)", "(f) cube_4<1> (Default)"]
    assert tree.missing_in_tree(["cube_1-1", "cube_4-1"], items) == []
    assert tree.tree_items(fake_session, fake_session._model, "Folder AB") == ["(-) cube_2<1> (Default)"]
    assert tree.tree_items(fake_session, fake_session._model, "Folder") is None


# --- dialogs: how a drawn text was read is part of the reply ------------------------------------

def test_dialog_reply_says_how_a_drawn_text_was_read():
    from solidworks_mcp.rbr import dialogs
    window = {"hwnd": 7, "title": "SOLIDWORKS", "text": "Save changes?", "buttons": [], "modal": True}
    assert "text_source" not in dialogs.public(window)   # a control held the text: nothing to say
    assert dialogs.public({**window, "text_source": "uia"})["text_source"] == "uia"


# --- reading a dialog must not take COM away from the thread that reads it ----------------------

needs_windows = pytest.mark.skipif(__import__("os").name != "nt", reason="COM apartments are Windows")


@needs_windows
def test_reading_a_dialog_leaves_com_on_the_thread_that_holds_the_session():
    """Live, 2026-11-19: pythoncom.CoInitialize() is a no-op on the thread that imported
    pythoncom first and CoUninitialize() is not, so drawn_text() bracketed by the two tore that
    thread's apartment down; the SolidWorks objects it held answered 0x800401FD ever after."""
    from solidworks_mcp.rbr import apartment, dialogs, instance
    pythoncom.CoInitialize()   # the state a thread with a session is in
    assert apartment.initialised()
    with apartment.com():
        assert apartment.initialised()
    assert apartment.initialised()
    assert dialogs.drawn_text(0) == ""          # no such window: nothing read - and nothing taken
    assert apartment.initialised()
    dialogs.uia_text(0, timeout_s=5.0)          # its helper thread has an apartment of its own
    assert apartment.initialised()
    instance.instances()                        # reads the running object table
    assert apartment.initialised()


@needs_windows
def test_a_thread_without_com_gets_it_for_the_block_only():
    import threading
    from solidworks_mcp.rbr import apartment
    seen = []

    def work():
        seen.append(apartment.initialised())
        with apartment.com():
            seen.append(apartment.initialised())
        seen.append(apartment.initialised())

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(10)
    assert seen == [False, True, False]
