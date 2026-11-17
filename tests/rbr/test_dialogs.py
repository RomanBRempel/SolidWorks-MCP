"""Dialog detection: classify / match_button / summary on plain dicts, and one real MessageBox."""

import os
import subprocess
import sys
import time

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import dialogs

WS_VISIBLE, WS_DISABLED, WS_CAPTION, WS_SYSMENU = 0x10000000, 0x08000000, 0x00C00000, 0x00080000
FRAME_STYLE = 0x04000000 | WS_CAPTION | WS_SYSMENU | 0x00040000 | WS_VISIBLE     # an overlapped main window
DIALOG_STYLE = dialogs.WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_VISIBLE


def win(hwnd, cls="#32770", title="", visible=True, enabled=True, owner=0, style=DIALOG_STYLE, ex_style=0, pid=100):
    style = (style | WS_VISIBLE) if visible else (style & ~WS_VISIBLE)
    style = (style & ~WS_DISABLED) if enabled else (style | WS_DISABLED)
    return {"hwnd": hwnd, "pid": pid, "title": title, "class": cls, "visible": visible, "enabled": enabled,
            "owner": owner, "style": style, "ex_style": ex_style}


def frame(hwnd=1, enabled=True, pid=100):
    return win(hwnd, cls="Afx:frame", title="CAD - [Part1]", enabled=enabled, style=FRAME_STYLE, pid=pid)


# --- classify (pure) ------------------------------------------------------------------------

def test_message_box_owned_by_a_disabled_frame_is_modal():
    found = dialogs.classify([frame(enabled=False), win(2, title="CAD", owner=1)])
    assert [(d["hwnd"], d["modal"]) for d in found] == [(2, True)]
    assert found[0]["title"] == "CAD" and found[0]["owner"] == 1  # the window's own fields are kept


def test_modeless_popup_of_an_enabled_frame_is_a_candidate_but_not_modal():
    popup = win(2, cls="Afx:popup", title="Measure", owner=1)
    found = dialogs.classify([frame(), popup])
    assert [(d["hwnd"], d["modal"]) for d in found] == [(2, False)]


def test_invisible_dialog_is_ignored():
    assert dialogs.classify([frame(enabled=False), win(2, visible=False, owner=1)]) == []


def test_tool_windows_tooltips_and_helpers_are_ignored():
    windows = [frame(enabled=False),
               win(2, cls="Afx:toolbar", title="Toolbar", owner=1, enabled=False, ex_style=dialogs.WS_EX_TOOLWINDOW),
               win(8, cls="Afx:helper", title="Helper", ex_style=dialogs.WS_EX_TOOLWINDOW),  # unowned tool window
               win(3, cls="tooltips_class32", owner=1, style=dialogs.WS_POPUP),
               win(4, cls="IME", title="Default IME", owner=1),
               win(5, cls="MSCTFIME UI", title="MSCTFIME UI", owner=1),
               win(6, cls="Afx:splash", style=dialogs.WS_POPUP),            # unowned, no caption
               win(7, cls="Afx:flyout", owner=1, style=dialogs.WS_POPUP)]   # owned, but no caption or frame
    assert dialogs.classify(windows) == []
    floating = win(2, cls="Afx:toolbar", title="Toolbar", owner=1, ex_style=dialogs.WS_EX_TOOLWINDOW)
    assert dialogs.classify([frame(), floating]) == []
    # an MFC frame disables its floating panes with itself: behind a message box they stay out
    disabled = dict(floating, enabled=False)
    assert [d["hwnd"] for d in dialogs.classify([win(3, owner=1), disabled, frame(enabled=False)])] == [3]


def test_tool_window_left_enabled_over_its_disabled_owner_is_the_dialog():
    box = win(2, cls="Afx:box", title="CAD", owner=1, ex_style=dialogs.WS_EX_TOOLWINDOW)
    assert [(d["hwnd"], d["modal"]) for d in dialogs.classify([box, frame(enabled=False)])] == [(2, True)]
    bare = win(2, cls="Afx:box", owner=1, style=dialogs.WS_POPUP, ex_style=dialogs.WS_EX_TOOLWINDOW)
    assert dialogs.classify([bare, frame(enabled=False)]) == []          # no caption: a flyout, not a dialog
    orphan = win(2, cls="Palette", owner=7, ex_style=dialogs.WS_EX_TOOLWINDOW)   # its owner is not shown
    assert dialogs.classify([orphan, win(7, visible=False), frame(enabled=False)]) == []


def test_owned_popup_needs_a_caption_or_a_dialog_frame():
    thin = win(2, cls="Custom", owner=1, style=dialogs.WS_POPUP, ex_style=dialogs.WS_EX_DLGMODALFRAME)
    assert [d["hwnd"] for d in dialogs.classify([frame(), thin])] == [2]
    overlapped = win(3, cls="Custom", owner=1, style=WS_CAPTION)  # not a popup: a second document frame
    assert dialogs.classify([frame(), overlapped]) == []


def test_modal_dialogs_come_first_in_z_order():
    windows = [win(5, title="modeless", owner=9, pid=200), frame(9, pid=200),     # another process, not blocked
               win(4, title="tool", cls="Afx:popup", owner=1),                    # owner disabled: modal too
               win(2, title="question", owner=1),
               frame(enabled=False)]
    found = dialogs.classify(windows)
    assert [(d["hwnd"], d["modal"]) for d in found] == [(4, True), (2, True), (5, False)]


def test_only_the_top_of_a_modal_chain_is_modal():
    # frame -> dialog A -> message box B: A is disabled while B is up and cannot be answered yet
    windows = [win(3, title="B", owner=2), win(2, title="A", owner=1, enabled=False), frame(enabled=False)]
    assert [(d["title"], d["modal"]) for d in dialogs.classify(windows)] == [("B", True), ("A", False)]


def test_without_a_visible_owner_the_main_frame_decides():
    hidden_owner = win(7, cls="HiddenOwner", visible=False, style=dialogs.WS_POPUP)
    boxes = [win(2, title="unowned"), win(3, title="hidden owner", owner=7)]
    assert all(d["modal"] for d in dialogs.classify([*boxes, hidden_owner, frame(enabled=False)]))
    assert not any(d["modal"] for d in dialogs.classify([*boxes, hidden_owner, frame()]))
    assert not any(d["modal"] for d in dialogs.classify(boxes))  # no frame at all: a bare MessageBoxW(0, ...)
    other = dialogs.classify([*boxes, frame(enabled=False, pid=200)])  # a disabled frame of another process
    assert not any(d["modal"] for d in other)


def test_classify_leaves_its_input_alone():
    windows = [frame(enabled=False), win(2, owner=1)]
    dialogs.classify(windows)
    assert all("modal" not in w for w in windows)


# --- match_button (pure) --------------------------------------------------------------------

def russian():
    return [{"id": 6, "text": "Да", "enabled": True, "default": True},
            {"id": 7, "text": "Нет", "enabled": True, "default": False},
            {"id": 2, "text": "Отмена", "enabled": True, "default": False}]


def test_button_by_control_id():
    assert dialogs.match_button(russian(), 7)["text"] == "Нет"
    assert dialogs.match_button(russian(), "2")["text"] == "Отмена"
    assert dialogs.match_button(russian(), " 6 ")["text"] == "Да"


def test_button_by_caption_ignores_case_accelerators_and_dots():
    buttons = [{"id": 101, "text": "Save As...", "enabled": True}, {"id": 102, "text": "Don't Save", "enabled": True},
               {"id": 2, "text": "Отмена", "enabled": True}]
    assert dialogs.match_button(buttons, "save as")["id"] == 101
    assert dialogs.match_button(buttons, " &Save as... ")["id"] == 101
    assert dialogs.match_button(buttons, "DON'T SAVE")["id"] == 102
    assert dialogs.match_button(buttons, "отмена")["id"] == 2
    raw = [{"id": 5, "text": "&Browse…", "enabled": True}]  # a caption that kept its marks
    assert dialogs.match_button(raw, "browse")["id"] == 5


def test_standard_names_work_under_russian_captions():
    assert dialogs.match_button(russian(), "yes")["id"] == 6
    assert dialogs.match_button(russian(), "No")["id"] == 7
    assert dialogs.match_button(russian(), "CANCEL")["id"] == 2
    assert dialogs.match_button([{"id": 1, "text": "ОК", "enabled": True}], "ok")["id"] == 1


def test_exact_caption_wins_over_a_standard_name():
    buttons = [{"id": 1, "text": "Yes", "enabled": True}, {"id": 6, "text": "Maybe", "enabled": True}]
    assert dialogs.match_button(buttons, "yes")["id"] == 1


def test_ambiguous_caption_is_an_error():
    buttons = [{"id": 10, "text": "&Apply", "enabled": True}, {"id": 11, "text": "Apply...", "enabled": True}]
    with pytest.raises(SolidWorksError, match="2 buttons match") as err:
        dialogs.match_button(buttons, "apply")
    assert "id 10" in str(err.value) and "id 11" in str(err.value)
    assert dialogs.match_button(buttons, 11)["text"] == "Apply..."


def test_missing_button_lists_the_available_ones():
    for wanted in ("retry", "Сохранить", 99, "99"):
        with pytest.raises(SolidWorksError, match="No button") as err:
            dialogs.match_button(russian(), wanted)
        assert '"Да" (id 6), "Нет" (id 7), "Отмена" (id 2)' in str(err.value)
    with pytest.raises(SolidWorksError, match="no buttons"):
        dialogs.match_button([], "ok")
    with pytest.raises(SolidWorksError, match="Name the button"):
        dialogs.match_button(russian(), "")


def test_disabled_button_is_refused():
    buttons = russian()
    buttons[0]["enabled"] = False
    for wanted in (6, "да", "yes"):
        with pytest.raises(SolidWorksError, match="disabled"):
            dialogs.match_button(buttons, wanted)


# --- summary (pure) -------------------------------------------------------------------------

def test_summary_names_title_text_and_buttons():
    box = {"title": "SOLIDWORKS", "text": "Save changes to Part1?", "buttons": russian()}
    assert dialogs.summary([box]) == ('SolidWorks shows a dialog "SOLIDWORKS": "Save changes to Part1?" '
                                      "[Да | Нет | Отмена]")


def test_summary_is_one_line_and_bounded():
    box = {"title": "Rebuild\nerrors", "text": "line one\r\n\r\n  line two\n" + "x" * 1000, "buttons": []}
    line = dialogs.summary([box, {"title": "Second"}, {"title": "Third"}])
    assert "\n" not in line and "\r" not in line
    assert line.startswith('SolidWorks shows a dialog "Rebuild errors": "line one line two xxx')
    assert line.endswith('…" (+2 more: "Second", "Third")')
    assert len(line) < dialogs.TEXT_LIMIT + 100


def test_summary_without_text_or_dialogs():
    assert dialogs.summary([]) == ""
    drawn = {"title": "Task", "text": "", "text_unreadable": True, "buttons": [{"id": 1, "text": "OK"}]}
    assert dialogs.summary([drawn]) == 'SolidWorks shows a dialog "Task" (its text cannot be read) [OK]'
    assert dialogs.summary([{"title": "Bare", "text": "", "buttons": []}]) == 'SolidWorks shows a dialog "Bare"'


# --- off Windows ----------------------------------------------------------------------------

def test_off_windows_nothing_is_found_and_answering_is_refused(monkeypatch):
    monkeypatch.setattr(dialogs.os, "name", "posix")
    monkeypatch.setattr(dialogs, "_user32", None)
    assert dialogs.windows_of([1]) == []
    assert dialogs.find(pids=[1]) == [] and dialogs.blocking(pids=[1]) == []
    with pytest.raises(SolidWorksError, match="only on Windows"):
        dialogs.describe(1)
    with pytest.raises(SolidWorksError, match="only on Windows"):
        dialogs.respond("ok", pids=[1])


# --- the real thing: a MessageBox of a child process ----------------------------------------

PROBE = ("import ctypes; "
         "print(ctypes.windll.user32.MessageBoxW(0, 'probe text', 'rbr-dialog-probe', 3), flush=True)")

# A frame window and a Retry/Cancel box owned by it: what a modal dialog of an application looks like.
OWNED_PROBE = """
import ctypes, ctypes.wintypes as wt
u = ctypes.WinDLL("user32")
u.CreateWindowExW.restype = wt.HWND
u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD] + [ctypes.c_int] * 4 + [wt.HWND] * 4
u.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
frame = u.CreateWindowExW(0, "Static", "rbr-probe-frame", 0x10CF0000, 40, 40, 320, 200, None, None, None, None)
print(u.MessageBoxW(frame, "owned text", "rbr-owned-probe", 0x35), flush=True)
"""

# TaskDialog with OK / Yes / No / Cancel; prints the HRESULT and the pressed id.
TASK_PROBE = """
import ctypes, ctypes.wintypes as wt
show = ctypes.WinDLL("comctl32").TaskDialog
show.argtypes = [wt.HWND, wt.HINSTANCE] + [wt.LPCWSTR] * 3 + [ctypes.c_int, wt.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
pressed = ctypes.c_int(0)
print(show(None, None, "rbr-task-probe", "Main instruction", "Content", 0xF, None, ctypes.byref(pressed)),
      pressed.value, flush=True)
"""


def _probe(code=PROBE):
    # The interpreter itself, not the venv launcher: the launcher's child would own the window.
    python = getattr(sys, "_base_executable", None) or sys.executable
    return subprocess.Popen([python, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _await_dialog(child, seconds=10.0, optional=False):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = dialogs.find(pids=[child.pid])
        if found:
            return found
        if child.poll() is not None:
            if optional:
                pytest.skip("this interpreter cannot show the probe dialog")
            raise AssertionError(f"the probe exited early: {child.stderr.read()}")
        time.sleep(0.1)
    raise AssertionError("the probe's message box never showed up")


def _swallow_bm_click(monkeypatch):
    """A button that takes the click and does nothing: the fallback has to close the dialog."""
    send = dialogs._send
    monkeypatch.setattr(dialogs, "_send", lambda u, hwnd, msg, *a, **kw:
                        0 if msg == dialogs.BM_CLICK else send(u, hwnd, msg, *a, **kw))


@pytest.mark.skipif(os.name != "nt", reason="needs the Windows API")
def test_finds_describes_and_answers_a_real_message_box():
    child = _probe()
    try:
        found = _await_dialog(child)
        assert len(found) == 1
        box = found[0]
        assert box["title"] == "rbr-dialog-probe" and box["class"] == "#32770"
        assert box["text"] == "probe text" and "text_unreadable" not in box
        assert box["pid"] == child.pid and box["visible"] and box["enabled"]
        assert box["modal"] is False  # MessageBoxW(0, ...) has no owner to disable
        assert dialogs.blocking(pids=[child.pid]) == []
        assert sorted(b["id"] for b in box["buttons"]) == [2, 6, 7]  # Cancel, Yes, No in any UI language
        assert all(b["enabled"] and b["text"] and "&" not in b["text"] for b in box["buttons"])
        assert [b["id"] for b in box["buttons"] if b["default"]] == [6]
        assert box["edits"] == [] and box["checkboxes"] == []
        assert dialogs.summary(found).startswith('SolidWorks shows a dialog "rbr-dialog-probe": "probe text" [')

        with pytest.raises(SolidWorksError, match="does not belong"):
            dialogs.respond("no", hwnd=box["hwnd"], pids=[os.getpid()])
        with pytest.raises(SolidWorksError, match="No button"):
            dialogs.respond("retry", title="dialog-probe", pids=[child.pid])
        assert child.poll() is None  # the refusals pressed nothing

        with pytest.raises(SolidWorksError, match="No dialog with 'elsewhere' in the title"):
            dialogs.respond("no", title="elsewhere", pids=[child.pid])
        assert child.poll() is None

        report = dialogs.respond("no", pids=[child.pid])  # the only dialog of that process
        assert report["ok"] and report["closed"] and "hint" not in report
        assert report["dialog"]["title"] == "rbr-dialog-probe" and report["dialog"]["text"] == "probe text"
        assert report["pressed"]["id"] == 7 and report["remaining"] == []
        out, _ = child.communicate(timeout=10)
        assert out.strip() == "7"  # IDNO
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


@pytest.mark.skipif(os.name != "nt", reason="needs the Windows API")
def test_blocking_sees_a_box_over_its_disabled_owner_and_wm_command_answers_it(monkeypatch):
    child = _probe(OWNED_PROBE)
    try:
        _await_dialog(child)
        found = dialogs.blocking(pids=[child.pid])
        assert [(d["title"], d["modal"], d["text"]) for d in found] == [("rbr-owned-probe", True, "owned text")]
        assert sorted(b["id"] for b in found[0]["buttons"]) == [2, 4]  # Cancel, Retry
        owner = next(w for w in dialogs.windows_of([child.pid]) if w["hwnd"] == found[0]["owner"])
        assert owner["title"] == "rbr-probe-frame" and owner["visible"] and not owner["enabled"]

        _swallow_bm_click(monkeypatch)
        report = dialogs.respond("cancel", pids=[child.pid])  # the only modal dialog, by its standard name
        assert report["closed"] and report["method"] == "WM_COMMAND" and report["pressed"]["id"] == 2
        out, _ = child.communicate(timeout=10)
        assert out.strip() == "2"  # IDCANCEL
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()


@pytest.mark.skipif(os.name != "nt", reason="needs the Windows API")
@pytest.mark.parametrize("swallow, method", [(False, "BM_CLICK"), (True, "TDM_CLICK_BUTTON")])
def test_task_dialog_is_read_through_msaa_and_its_buttons_pressed(monkeypatch, swallow, method):
    child = _probe(TASK_PROBE)
    try:
        box = _await_dialog(child, optional=True)[0]
        assert box["title"] == "rbr-task-probe" and box["task_dialog"]
        # the text is drawn, no control holds it: it comes through MSAA (pythoncom)
        assert box["text"] == "Main instruction" + chr(10) + "Content" and "text_unreadable" not in box
        assert sorted(b["id"] for b in box["buttons"]) == [1, 2, 6, 7]  # ids restored from the captions
        if swallow:
            _swallow_bm_click(monkeypatch)
        report = dialogs.respond("no", hwnd=box["hwnd"], pids=[child.pid])
        assert report["closed"] and report["method"] == method and report["pressed"]["id"] == 7
        out, _ = child.communicate(timeout=10)
        assert out.split() == ["0", "7"]  # S_OK, IDNO
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()
