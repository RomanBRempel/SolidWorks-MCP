"""Modal dialogs of SolidWorks: see them without COM, describe them, press a button.

While SLDWORKS.exe shows a message box ("save changes?", rebuild errors, a
missing file) every COM call into it waits until the client gives up. The
dialog itself is an ordinary top-level window, so it can be found, read and
answered through the Windows API alone, from any thread, while the COM thread
is stuck.

Everything here is ctypes over user32 (no psutil). Enumerating and
classifying the windows sends no message at all; describing a dialog sends only
reads (WM_GETTEXT, WM_GETTEXTLENGTH, BM_GETCHECK, WM_GETDLGCODE), each with a
timeout, so a hung window never hangs the caller. Only respond() changes
anything: it presses one button of one dialog.

SolidWorks' own message boxes are TaskDialogs (verified on SW 2025 SP3 with
ISldWorks.SendMsgToUser2): the question is drawn inside a "DirectUIHWND" pane
and no control holds it. It is read through MSAA instead - IAccessible of the
pane, the children with the role "static text" - which needs pythoncom
(present wherever the server runs); without it the text stays unreadable and
the buttons are still there.

classify, match_button and summary are pure and unit-tested; the Win32 round
trip was verified against message boxes and a TaskDialog of child processes
(found, read, answered by BM_CLICK and by the fallbacks; a window that pumps
no messages is described within a second). Measured on SolidWorks 2025 with
340 top-level windows: one blocking() poll takes about 30 ms, half of it
finding the process. Its main frame is an unowned "Afx:..." window without
WS_CAPTION. A real SolidWorks message box (Yes / No, raised on purpose) was
found as modal within one poll, read, and answered by BM_CLICK in 0.08 s; the
script waiting behind it went on with the answer.
"""

import ctypes
import ctypes.wintypes as wt
import os
import time

from ..errors import SolidWorksError
from . import health

READ_TIMEOUT_MS = 500     # one WM_GETTEXT; a second timeout in a row stops the reads of that dialog
CLICK_TIMEOUT_MS = 1000   # BM_CLICK returns when the button's handler does
CLICK_SETTLE_S = 0.5      # how long BM_CLICK gets before WM_COMMAND is tried
TEXT_LIMIT = 300          # the message text inside summary()

# window styles
WS_POPUP = 0x80000000
WS_DLGFRAME = 0x00400000          # set by WS_CAPTION too (WS_CAPTION = WS_BORDER | WS_DLGFRAME)
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GW_OWNER = 4
GWL_STYLE, GWL_EXSTYLE = -16, -20

# messages
WM_GETTEXT, WM_GETTEXTLENGTH, WM_GETDLGCODE, WM_COMMAND = 0x000D, 0x000E, 0x0087, 0x0111
BM_GETCHECK, BM_CLICK, BN_CLICKED = 0x00F0, 0x00F5, 0
TDM_CLICK_BUTTON = 0x0400 + 102
SMTO_ABORTIFHUNG = 0x0002
DLGC_DEFPUSHBUTTON, DLGC_RADIOBUTTON = 0x0010, 0x0040

# control styles
BS_TYPEMASK = 0x0F
_BS_DEFAULT = {0x1, 0xD, 0xF}                 # DEFPUSHBUTTON, DEFSPLITBUTTON, DEFCOMMANDLINK
_BS_CHECK = {0x2, 0x3, 0x5, 0x6}              # CHECKBOX, AUTOCHECKBOX, 3STATE, AUTO3STATE
_BS_RADIO = {0x4, 0x9}
_BS_GROUP = 0x7
_BS_OWNERDRAW = 0xB
SS_TYPEMASK = 0x1F
_SS_NO_TEXT = {0x3, 0x4, 0x5, 0x6, 0x7, 0x8, 0x9, 0xE, 0xF, 0x10, 0x11, 0x12}  # icons, bitmaps, frames
SS_NOPREFIX = 0x80
ES_PASSWORD = 0x20

DIALOG_CLASS = "#32770"
PROGRESS_CLASS = "msctls_progress32"
OBJID_CLIENT = -4
ROLE_STATIC_TEXT = 41                 # ROLE_SYSTEM_STATICTEXT
ACC_COUNT, ACC_CHILD, ACC_NAME, ACC_ROLE = -5001, -5002, -5003, -5006   # IAccessible dispids
ACC_LIMIT = 80                        # elements looked at in one pane
TASK_DIALOG_PANE = "DirectUIHWND"
# never a dialog: tooltips, IME helpers, menus, drop-down lists, window shadows
_NOT_DIALOG_CLASSES = {"tooltips_class32", "ime", "msctfime ui", "#32768", "combolbox", "sysshadow",
                       "auto-suggest dropdown"}

# language-neutral names of the standard buttons (the captions follow the UI language)
STANDARD_IDS = {"ok": 1, "cancel": 2, "abort": 3, "retry": 4, "ignore": 5, "yes": 6, "no": 7, "close": 8,
                "continue": 11}


# --- user32, bound once (any thread, no COM) -------------------------------------------------

_user32 = None
_ENUMPROC = None


def _api():
    """user32 with 64-bit-safe prototypes; SolidWorksError off Windows."""
    global _user32, _ENUMPROC
    if _user32 is not None:
        return _user32
    if os.name != "nt":
        raise SolidWorksError("Dialogs can be inspected and answered only on Windows.")
    u = ctypes.WinDLL("user32", use_last_error=True)  # own instance: the prototypes stay private
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    lresult = ctypes.c_ssize_t
    for name, restype, argtypes in (
        ("EnumWindows", wt.BOOL, [proc, wt.LPARAM]),
        ("EnumChildWindows", wt.BOOL, [wt.HWND, proc, wt.LPARAM]),
        ("GetWindowThreadProcessId", wt.DWORD, [wt.HWND, ctypes.POINTER(wt.DWORD)]),
        ("InternalGetWindowText", ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
        ("GetClassNameW", ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
        ("IsWindow", wt.BOOL, [wt.HWND]),
        ("IsWindowVisible", wt.BOOL, [wt.HWND]),
        ("IsWindowEnabled", wt.BOOL, [wt.HWND]),
        ("GetWindow", wt.HWND, [wt.HWND, wt.UINT]),
        ("GetWindowLongW", wt.LONG, [wt.HWND, ctypes.c_int]),
        ("GetDlgCtrlID", ctypes.c_int, [wt.HWND]),
        ("SendMessageTimeoutW", lresult, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT,
                                          ctypes.POINTER(ctypes.c_size_t)]),
        ("PostMessageW", wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
        ("MB_GetString", wt.LPCWSTR, [wt.UINT]),
    ):
        fn = getattr(u, name, None)
        if fn is not None:  # MB_GetString is an old, optional export
            fn.restype, fn.argtypes = restype, argtypes
    _ENUMPROC = proc
    _user32 = u
    return u


def _enumerate(call, *head) -> list:
    """The window handles EnumWindows / EnumChildWindows walks, in z order."""
    found = []

    def visit(hwnd, _lparam):
        found.append(int(hwnd or 0))
        return True

    call(*head, _ENUMPROC(visit), 0)
    return [h for h in found if h]


def _class(u, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if u.GetClassNameW(hwnd, buf, 256) else ""


def _caption(u, hwnd: int) -> str:
    """The text kept in the window itself; sends no message, so it cannot hang."""
    buf = ctypes.create_unicode_buffer(1024)
    return buf.value if u.InternalGetWindowText(hwnd, buf, 1024) else ""


def _style(u, hwnd: int, index: int = GWL_STYLE) -> int:
    return int(u.GetWindowLongW(hwnd, index)) & 0xFFFFFFFF


def _send(u, hwnd: int, msg: int, wparam: int = 0, lparam=0, timeout_ms: int = READ_TIMEOUT_MS):
    """SendMessageTimeoutW(SMTO_ABORTIFHUNG): the message's result, or None when it was not answered."""
    result = ctypes.c_size_t(0)
    done = u.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
    return int(result.value) if done else None


class _Reader:
    """Control texts via WM_GETTEXT; after two unanswered reads it stops asking (a hung window)."""

    def __init__(self, u):
        self.u = u
        self.misses = 0

    @property
    def hung(self) -> bool:
        return self.misses >= 2

    def ask(self, hwnd: int, msg: int, wparam: int = 0, lparam=0):
        if self.hung:
            return None
        answer = _send(self.u, hwnd, msg, wparam, lparam)
        self.misses = 0 if answer is not None else self.misses + 1
        return answer

    def text(self, hwnd: int) -> str:
        length = self.ask(hwnd, WM_GETTEXTLENGTH)
        if length is None:
            return _caption(self.u, hwnd)
        if length <= 0:
            return ""
        size = min(int(length), 65535) + 1
        buf = ctypes.create_unicode_buffer(size)
        if self.ask(hwnd, WM_GETTEXT, size, ctypes.cast(buf, ctypes.c_void_p).value) is None:
            return _caption(self.u, hwnd)
        return buf.value


def _window(u, hwnd: int, pid: int) -> dict:
    return {"hwnd": int(hwnd), "pid": int(pid), "title": _caption(u, hwnd), "class": _class(u, hwnd),
            "visible": bool(u.IsWindowVisible(hwnd)), "enabled": bool(u.IsWindowEnabled(hwnd)),
            "owner": int(u.GetWindow(hwnd, GW_OWNER) or 0),
            "style": _style(u, hwnd, GWL_STYLE), "ex_style": _style(u, hwnd, GWL_EXSTYLE)}


def _pid_of(u, hwnd: int) -> int:
    pid = wt.DWORD(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def windows_of(pids: list[int]) -> list[dict]:
    """Every top-level window of those processes, in z order; no message is sent. [] off Windows."""
    if os.name != "nt" or not pids:
        return []
    u = _api()
    wanted = {int(p) for p in pids}
    out = []
    for hwnd in _enumerate(u.EnumWindows):
        pid = _pid_of(u, hwnd)
        if pid in wanted:
            out.append(_window(u, hwnd, pid))
    return out


# --- one dialog in full ------------------------------------------------------------------------

def _plain(caption: str) -> str:
    """A caption without its '&' accelerator marks ('&&' is a literal ampersand)."""
    return (caption or "").replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def _kind(class_name: str) -> str | None:
    """button / static / edit for the standard controls and their WinForms twins, else None."""
    name = class_name.lower()
    for kind in ("button", "static", "edit"):
        if name == kind or f".{kind}." in name:  # WindowsForms10.BUTTON.app.0.xxxx
            return kind
    if name.startswith("richedit"):
        return "edit"
    return None


def _standard_captions(u) -> dict:
    """The system's own captions of the standard buttons in the UI language -> their ids."""
    out = {}
    try:
        for ident in STANDARD_IDS.values():
            caption = _plain(u.MB_GetString(ident - 1) or "").casefold()  # the strings are indexed by id - 1
            if caption:
                out.setdefault(caption, ident)
    except Exception:  # noqa: BLE001 - without the export the buttons keep id 0 and answer to their captions
        pass
    return out


def drawn_text(pane_hwnd: int) -> str:
    """The text a TaskDialog draws in its pane (main instruction, content, footer), read
    through MSAA; "" when it cannot be read. Call only for a window that still answers."""
    if os.name != "nt":
        return ""
    try:
        import pythoncom
    except ImportError:
        return ""
    initialised = False
    try:
        pythoncom.CoInitialize()   # this may be a pool thread that never used COM
        initialised = True
    except Exception:  # noqa: BLE001
        pass
    try:
        iid = (ctypes.c_byte * 16).from_buffer_copy(bytes(pythoncom.IID_IDispatch))
        pointer = ctypes.c_void_p()
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(wt.HWND(int(pane_hwnd)), ctypes.c_long(OBJID_CLIENT),
                                                        ctypes.byref(iid), ctypes.byref(pointer))
        if not pointer.value:
            return ""
        root = pythoncom.ObjectFromAddress(pointer.value, pythoncom.IID_IDispatch)
        get = pythoncom.DISPATCH_PROPERTYGET
        lines, seen = [], [0]

        def read(obj, dispid, *args):
            try:
                return obj.Invoke(dispid, 0, get, True, *args)
            except Exception:  # noqa: BLE001 - an element without that property
                return None

        def walk(obj, depth: int) -> None:
            for index in range(1, int(read(obj, ACC_COUNT) or 0) + 1):
                if seen[0] >= ACC_LIMIT:
                    return
                seen[0] += 1
                child = read(obj, ACC_CHILD, index)
                if child is not None and not isinstance(child, int):
                    name, role = read(child, ACC_NAME, 0), read(child, ACC_ROLE, 0)
                    if depth < 3:
                        walk(child, depth + 1)
                else:
                    name, role = read(obj, ACC_NAME, index), read(obj, ACC_ROLE, index)
                if role == ROLE_STATIC_TEXT and name and str(name).strip():
                    lines.append(str(name).strip())

        walk(root, 0)
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - reading is best-effort: the buttons still work
        return ""
    finally:
        if initialised:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


UIA_TEXT_TYPES = {50020, 50004, 50030}   # Text, Edit, Document
UIA_TIMEOUT_S = 2.0
UIA_LIMIT = 120                          # elements looked at under one window
_CLSID_UIA = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_IID_UIA = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"


def _uia_read(hwnd: int, out: list) -> None:
    """Collect the names of the text elements UI Automation shows under `hwnd` into `out`.

    Raw vtable calls (pywin32 has no UI Automation client, comtypes is not a
    dependency): IUIAutomation.ElementFromHandle, .CreateTrueCondition,
    IUIAutomationElement.FindAll(descendants), then each element's control type
    and name. Own thread, own apartment."""
    import pythoncom
    pythoncom.CoInitialize()
    held = []

    def method(pointer, index, *argtypes):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])

    def release(pointer):
        if pointer:
            method(pointer, 2)(pointer)

    try:
        clsid = (ctypes.c_byte * 16).from_buffer_copy(bytes(pythoncom.MakeIID(_CLSID_UIA)))
        iid = (ctypes.c_byte * 16).from_buffer_copy(bytes(pythoncom.MakeIID(_IID_UIA)))
        uia = ctypes.c_void_p()
        ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(uia))
        held.append(uia)
        root, always, found = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        if method(uia, 6, wt.HWND, ctypes.POINTER(ctypes.c_void_p))(uia, wt.HWND(int(hwnd)), ctypes.byref(root)) \
                or not root:
            return
        held.append(root)
        method(uia, 21, ctypes.POINTER(ctypes.c_void_p))(uia, ctypes.byref(always))
        held.append(always)
        if method(root, 6, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                root, 4, always, ctypes.byref(found)) or not found:   # TreeScope_Descendants
            return
        held.append(found)
        count = ctypes.c_int(0)
        method(found, 3, ctypes.POINTER(ctypes.c_int))(found, ctypes.byref(count))
        for index in range(min(count.value, UIA_LIMIT)):
            element = ctypes.c_void_p()
            if method(found, 4, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(found, index, ctypes.byref(element)) \
                    or not element:
                continue
            try:
                kind = ctypes.c_int(0)
                method(element, 21, ctypes.POINTER(ctypes.c_int))(element, ctypes.byref(kind))
                if kind.value not in UIA_TEXT_TYPES:
                    continue
                name = ctypes.c_void_p()
                method(element, 23, ctypes.POINTER(ctypes.c_void_p))(element, ctypes.byref(name))
                if name.value:
                    text = ctypes.wstring_at(name.value).strip()
                    ctypes.windll.oleaut32.SysFreeString(name)
                    if text and text not in out:
                        out.append(text)
            finally:
                release(element)
    except Exception:  # noqa: BLE001 - best effort, like every other way of reading the text
        pass
    finally:
        for pointer in reversed(held):
            try:
                release(pointer)
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()


def uia_text(hwnd: int, timeout_s: float = UIA_TIMEOUT_S) -> str:
    """The text of a window as UI Automation exposes it; "" when it cannot be read in time.

    The second way into a drawn dialog (a TaskDialog, a WPF window). MSAA's
    IAccessible is an ordinary COM object of SolidWorks' UI thread, and a call
    into that thread is turned away while it is busy inside the call that
    raised the dialog; UI Automation talks to the window over its own channel.
    It runs on a helper thread and is given up on after timeout_s, so a window
    that answers nothing costs the caller no more than that."""
    if os.name != "nt":
        return ""
    import threading
    lines: list = []
    worker = threading.Thread(target=_uia_read, args=(int(hwnd), lines), daemon=True, name="swmcp-uia")
    try:
        worker.start()
        worker.join(timeout_s)
    except Exception:  # noqa: BLE001
        return ""
    return "" if worker.is_alive() else "\n".join(lines)


def describe(hwnd: int) -> dict:
    """Title, message text, buttons, check boxes and edit fields of one dialog window.

    Reads only (WM_GETTEXT and friends, each with a timeout). The text of a
    TaskDialog is drawn, not held by a control: it comes back empty with
    text_unreadable True, the buttons are still real and pressable. Their
    control ids are all 0 there; the standard ones get their ids back from the
    captions (OK / Yes / No / Cancel ... in the UI language).
    """
    u = _api()
    hwnd = int(hwnd)
    if not u.IsWindow(hwnd):
        raise SolidWorksError(f"Window {hwnd} no longer exists.")
    reader = _Reader(u)
    out = {"hwnd": hwnd, "title": reader.text(hwnd) or _caption(u, hwnd), "class": _class(u, hwnd),
           "text": "", "buttons": [], "edits": [], "checkboxes": []}
    labels = []
    drawn = out["class"].startswith("HwndWrapper")  # WPF: no child windows at all
    pane = None
    for child in _enumerate(u.EnumChildWindows, hwnd):
        try:
            class_name = _class(u, child)
            if class_name == TASK_DIALOG_PANE:
                drawn = True
                pane = pane or int(child)
            if class_name.lower() == PROGRESS_CLASS and u.IsWindowVisible(child):
                out["progress"] = True  # work in progress with a Cancel button, not a question
            kind = _kind(class_name)
            if kind is None or not u.IsWindowVisible(child):
                continue
            style = _style(u, child)
            ident = int(u.GetDlgCtrlID(child))
            if kind == "static":
                if style & SS_TYPEMASK in _SS_NO_TEXT:
                    continue
                label = reader.text(child)
                label = (label if style & SS_NOPREFIX else _plain(label)).strip()
                if label:
                    labels.append(label)
            elif kind == "edit":
                out["edits"].append({"id": ident, "text": "" if style & ES_PASSWORD else reader.text(child)})
            else:
                sort = style & BS_TYPEMASK
                code = (reader.ask(child, WM_GETDLGCODE) or 0) if sort == _BS_OWNERDRAW else 0
                if sort == _BS_GROUP or sort in _BS_RADIO or code & DLGC_RADIOBUTTON:
                    continue
                caption = _plain(reader.text(child))
                if sort in _BS_CHECK:
                    out["checkboxes"].append({"id": ident, "text": caption,
                                              "checked": reader.ask(child, BM_GETCHECK) == 1})
                else:
                    out["buttons"].append({"id": ident, "text": caption, "enabled": bool(u.IsWindowEnabled(child)),
                                           "default": sort in _BS_DEFAULT or bool(code & DLGC_DEFPUSHBUTTON),
                                           "hwnd": int(child)})
        except Exception:  # noqa: BLE001 - a control that vanished mid-walk must not lose the dialog
            continue
    out["text"] = "\n".join(labels)
    if drawn:
        out["task_dialog"] = True
        if out["buttons"] and not any(b["id"] for b in out["buttons"]):
            known = _standard_captions(u)
            for b in out["buttons"]:
                b["id"] = known.get(b["text"].casefold(), 0)
        if not out["text"] and pane is not None and not reader.hung:
            out["text"] = drawn_text(pane)
            if out["text"]:
                out["text_source"] = "msaa"
    if not out["text"] and not reader.hung and (drawn or not out["edits"]):
        # nothing a control holds and nothing MSAA gave (its calls are turned away while
        # SolidWorks is busy inside the call that raised the dialog): UI Automation
        captions = {b["text"] for b in out["buttons"]} | {c["text"] for c in out["checkboxes"]} | {out["title"]}
        lines = [line for line in uia_text(hwnd).split("\n") if line and line not in captions]
        if lines:
            out["text"] = "\n".join(lines)
            out["text_source"] = "uia"
    if drawn and not out["text"]:
        out["text_unreadable"] = True
    if reader.hung:
        out["hung"] = True  # the window stopped answering: texts are what the window structure holds
    return out


# --- which windows are dialogs, and which block ------------------------------------------------

def _never_dialog(win: dict) -> bool:
    return (not win.get("visible") or bool(int(win.get("ex_style") or 0) & WS_EX_NOACTIVATE)
            or str(win.get("class") or "").lower() in _NOT_DIALOG_CLASSES)


def _framed_popup(win: dict) -> bool:
    """An owned WS_POPUP with a caption or a dialog frame."""
    style, ex_style = int(win.get("style") or 0), int(win.get("ex_style") or 0)
    return (bool(win.get("owner")) and bool(style & WS_POPUP)
            and bool(style & WS_DLGFRAME or ex_style & WS_EX_DLGMODALFRAME))


def _is_tool(win: dict) -> bool:
    return bool(int(win.get("ex_style") or 0) & WS_EX_TOOLWINDOW)


def classify(windows: list[dict]) -> list[dict]:
    """The dialogs among the windows of windows_of, each with "modal": bool, modal ones first. Pure.

    A candidate is visible and either a "#32770" or an owned WS_POPUP with a
    caption / dialog frame; tool windows, tooltips, IME helpers, menus and
    invisible windows are not. It is modal when it is enabled itself and its
    owner is visible and disabled - that is how Windows marks a modal owner.
    Without a visible owner the process's main frames decide (visible, unowned,
    not dialogs themselves). In a chain frame -> dialog A -> message box B only
    B is modal: A is disabled and cannot be answered before B.

    One tool window does count: a framed popup that is enabled while its own
    owner is disabled. An MFC frame disables its floating panes together with
    itself, so what stays enabled there is the window being waited for.
    """
    by_hwnd = {w.get("hwnd"): w for w in windows}

    def owner_blocked(w) -> bool | None:
        """Is the owner disabled? None when there is no visible owner to ask."""
        owner = by_hwnd.get(w.get("owner")) if w.get("owner") else None
        if owner is None or not owner.get("visible"):
            return None
        return not owner.get("enabled")

    candidates = []
    for w in windows:
        if _never_dialog(w):
            continue
        if _is_tool(w):
            if _framed_popup(w) and w.get("enabled") and owner_blocked(w):
                candidates.append(w)
        elif str(w.get("class") or "") == DIALOG_CLASS or _framed_popup(w):
            candidates.append(w)
    chosen = {id(w) for w in candidates}
    frames: dict = {}
    for w in windows:
        if not _never_dialog(w) and not _is_tool(w) and not w.get("owner") and id(w) not in chosen:
            frames.setdefault(w.get("pid"), []).append(w)
    out = []
    for w in candidates:
        blocked = owner_blocked(w)
        if blocked is None:
            blocked = any(not f.get("enabled") for f in frames.get(w.get("pid"), []))
        out.append({**w, "modal": bool(w.get("enabled") and blocked)})
    out.sort(key=lambda w: not w["modal"])  # stable: z order inside each group
    return out


def find(image: str = "SLDWORKS.exe", pids: list[int] | None = None, modal_only: bool = False) -> list[dict]:
    """The open dialogs of the application, described; modal ones first. [] off Windows.

    pids default to every process whose executable is image. A window that
    cannot be described (it closed meanwhile) is skipped, never raised.
    """
    if os.name != "nt":
        return []
    if pids is None:
        pids = health.find_processes(image)
    out = []
    for win in classify(windows_of(list(pids))):
        if modal_only and not win["modal"]:
            continue
        try:
            out.append({**win, **describe(win["hwnd"])})
        except Exception:  # noqa: BLE001 - one bad window must not hide the others
            continue
    return out


def blocking(image: str = "SLDWORKS.exe", pids: list[int] | None = None) -> list[dict]:
    """The modal dialogs only. Cheap enough to poll: no message is sent unless one is open."""
    return find(image=image, pids=pids, modal_only=True)


def questions(dialogs: list[dict]) -> list[dict]:
    """The dialogs that wait for an answer: modal, and not a progress window (a bar and a
    Cancel button while SolidWorks works looks the same to Windows). Pure."""
    return [d for d in dialogs if d.get("modal") and not d.get("progress")]


def public(dialog: dict) -> dict:
    """A dialog as a tool reply shows it: what it says and what can be pressed; pure."""
    out = {"hwnd": dialog.get("hwnd"), "title": dialog.get("title"), "text": dialog.get("text"),
           "buttons": [b.get("text") or f"id {b.get('id')}" for b in dialog.get("buttons") or ()
                       if b.get("enabled", True)],
           "modal": bool(dialog.get("modal"))}
    default = next((b.get("text") for b in dialog.get("buttons") or ()
                    if b.get("default") and b.get("enabled", True)), None)
    if default:
        out["default_button"] = default
    for key in ("checkboxes", "edits"):
        if dialog.get(key):
            out[key] = [{k: v for k, v in item.items() if k != "hwnd"} for item in dialog[key]]
    for key in ("progress", "text_unreadable", "hung"):
        if dialog.get(key):
            out[key] = True
    return out


def blocking_reply(dialogs: list[dict], tool: str | None = None, waiting_s: float | None = None) -> dict:
    """The reply of a call that is waiting behind a dialog (DIALOG_BLOCKING); pure."""
    waited = f" for {waiting_s:.0f} s" if waiting_s else ""
    what = f"'{tool}' has been waiting{waited} behind it" if tool else "SolidWorks answers nothing until it is closed"
    return {"ok": False, "dialog_blocking": True,
            "error": f"DIALOG_BLOCKING: {summary(dialogs)}. {what}.",
            "dialogs": [public(d) for d in dialogs],
            "hint": "dialog_respond(button=...) presses one of its buttons (dialog_list shows the dialog again). "
                    "The waiting call is NOT cancelled: it goes on as soon as the dialog is answered - do not "
                    "send it again; get_journal(last_n=3) shows how it ended."}


# --- answering ----------------------------------------------------------------------------------

def _normal(caption) -> str:
    text = _plain(str(caption))
    while text.endswith("...") or text.endswith("…"):
        text = text[:-3 if text.endswith("...") else -1].rstrip()
    return text.casefold()


def _button_list(buttons: list[dict]) -> str:
    if not buttons:
        return "the dialog has no buttons"
    return ", ".join(f'"{b.get("text", "")}" (id {b.get("id")})' + ("" if b.get("enabled", True) else " [disabled]")
                     for b in buttons)


def match_button(buttons: list[dict], wanted) -> dict:
    """The one button `wanted` names: a control id, a caption, or a standard name. Pure.

    A caption matches case-blind without '&', outer spaces and a trailing
    '...'. "ok", "cancel", "yes", "no", "retry", "ignore", "abort", "close" and
    "continue" name the standard ids whatever the UI language shows; an exact
    caption wins over such a name. No match, several matches and a disabled
    button are errors that list the buttons.
    """
    if isinstance(wanted, bool) or wanted is None or str(wanted).strip() == "":
        raise SolidWorksError(f"Name the button to press: a caption or a control id. Buttons: {_button_list(buttons)}.")
    hits = []
    if isinstance(wanted, int) or str(wanted).strip().isdigit():
        hits = [b for b in buttons if b.get("id") == int(str(wanted).strip())]
    if not hits and not isinstance(wanted, int):
        key = _normal(wanted)
        hits = [b for b in buttons if _normal(b.get("text", "")) == key]
        if not hits and key in STANDARD_IDS:
            hits = [b for b in buttons if b.get("id") == STANDARD_IDS[key]]
    if not hits:
        raise SolidWorksError(f"No button {wanted!r} in the dialog. Buttons: {_button_list(buttons)}.")
    if len(hits) > 1:
        raise SolidWorksError(f"{len(hits)} buttons match {wanted!r}; name one by its control id or full caption. "
                              f"Buttons: {_button_list(buttons)}.")
    if not hits[0].get("enabled", True):
        raise SolidWorksError(f'The button "{hits[0].get("text", "")}" (id {hits[0].get("id")}) is disabled. '
                              f"Buttons: {_button_list(buttons)}.")
    return hits[0]


def _dialog_list(dialogs: list[dict]) -> str:
    return "; ".join(f'"{d.get("title", "")}" (hwnd {d.get("hwnd")}{", modal" if d.get("modal") else ""})'
                     for d in dialogs)


def _pick(u, hwnd, title, image: str, pids: list[int]) -> dict:
    """The dialog respond() answers: by handle, by title, or the only one (modal ones go first)."""
    if hwnd is not None:
        hwnd = int(hwnd)
        if not u.IsWindow(hwnd):
            raise SolidWorksError(f"Window {hwnd} no longer exists.")
        if _pid_of(u, hwnd) not in {int(p) for p in pids}:
            raise SolidWorksError(f"Window {hwnd} does not belong to {image}.")
        return describe(hwnd)
    dialogs = find(image=image, pids=pids)
    if title is not None:
        key = str(title).casefold()
        hits = [d for d in dialogs if key in str(d.get("title", "")).casefold()]
        if len(hits) > 1 and any(d["modal"] for d in hits):
            hits = [d for d in hits if d["modal"]]
        what = f"with {title!r} in the title"
    else:
        hits = [d for d in dialogs if d["modal"]] or dialogs  # nothing modal: a lone dialog is still the one meant
        what = "is open"
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SolidWorksError(f"No dialog {what}. Open dialogs: {_dialog_list(dialogs) or 'none'}.")
    raise SolidWorksError(f"{len(hits)} dialogs {what.replace('is open', 'are open')}; pass hwnd or title. "
                          f"They are: {_dialog_list(hits)}.")


def _gone(u, hwnd: int) -> bool:
    return not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd)


def _wait_gone(u, hwnd: int, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if _gone(u, hwnd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _face(dialog: dict) -> tuple:
    """What a dialog shows; a press that changed it (a wizard page) has worked and is not repeated."""
    return (dialog.get("title"), dialog.get("text"),
            [(b.get("hwnd"), b.get("text"), b.get("enabled")) for b in dialog.get("buttons") or []],
            [(c.get("id"), c.get("checked")) for c in dialog.get("checkboxes") or []])


def respond(button, hwnd: int | None = None, title: str | None = None, image: str = "SLDWORKS.exe",
            pids: list[int] | None = None, wait_s: float = 3.0) -> dict:
    """Press one button of one dialog and wait for the dialog to close.

    The dialog: hwnd, else a case-blind part of its title, else the only modal
    one, else the only one open (none or several is an error that lists them;
    a handle outside the application's processes is refused). The press is
    BM_CLICK to the button. When the click came back and the dialog stands
    unchanged, WM_COMMAND (BN_CLICKED) goes to the dialog, TDM_CLICK_BUTTON to
    a TaskDialog; a click still being handled, or one that changed the dialog,
    is not repeated. A dialog that does not close is not an error: closed is
    False with a hint, and remaining lists what is open now (a second dialog
    often follows).
    """
    u = _api()
    if pids is None:
        pids = health.find_processes(image)
    pids = [int(p) for p in pids]
    dialog = _pick(u, hwnd, title, image, pids)
    target = match_button(dialog["buttons"], button)
    box, ident = int(dialog["hwnd"]), int(target["id"])
    method = "BM_CLICK"
    taken = _send(u, target["hwnd"], BM_CLICK, timeout_ms=CLICK_TIMEOUT_MS) is not None
    settle = min(CLICK_SETTLE_S, wait_s) if taken else wait_s
    closed = _wait_gone(u, box, settle)
    if not closed and taken:
        # An unanswered click is left alone: its handler is still running, likely inside the next dialog.
        try:
            untouched = _face(describe(box)) == _face(dialog)
        except Exception:  # noqa: BLE001 - it closed while we looked
            untouched = False
        if untouched:  # the click returned and nothing happened: a button ignores it while its dialog is inactive
            if not dialog.get("task_dialog"):
                method = "WM_COMMAND"
                u.PostMessageW(box, WM_COMMAND, (BN_CLICKED << 16) | (ident & 0xFFFF), target["hwnd"])
            elif ident:  # a custom TaskDialog button has no id we can read
                method = "TDM_CLICK_BUTTON"
                u.PostMessageW(box, TDM_CLICK_BUTTON, ident, 0)
        closed = _wait_gone(u, box, max(wait_s - settle, settle))
    remaining = []
    try:
        remaining = [{"hwnd": w["hwnd"], "title": w["title"], "modal": w["modal"]}
                     for w in classify(windows_of(pids)) if not (closed and w["hwnd"] == box)]
    except Exception:  # noqa: BLE001 - the press is done; the follow-up list is a courtesy
        pass
    out = {"ok": True, "closed": closed,
           "dialog": {"hwnd": box, "title": dialog.get("title", ""), "text": dialog.get("text", "")},
           "pressed": {"id": ident, "text": target.get("text", "")}, "method": method, "remaining": remaining}
    if not closed and not taken:
        out["hint"] = ("The window did not take the click in time: the application is busy or already handling "
                       "it (a second dialog, see remaining). The click may still land later - look again with "
                       "find() before pressing anything more.")
    elif not closed:
        out["hint"] = (f"The dialog is still open {wait_s:g} s after the press: this button may not close it, or "
                       "the application is acting on the answer. Look again with find() before pressing more.")
    return out


def summary(dialogs: list[dict]) -> str:
    """One line for an error message: the first dialog's title, text and buttons; "" for none. Pure.

    'SolidWorks shows a dialog "SOLIDWORKS": "Save changes to Part1?" [Да | Нет | Отмена]'
    """
    if not dialogs:
        return ""
    first = dialogs[0]
    line = f'SolidWorks shows a dialog "{" ".join(str(first.get("title") or "").split())}"'
    text = " ".join(str(first.get("text") or "").split())
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT - 1].rstrip() + "…"
    if text:
        line += f': "{text}"'
    elif first.get("text_unreadable"):
        line += " (its text cannot be read)"
    buttons = [str(b.get("text") or b.get("id")) for b in first.get("buttons") or []]
    if buttons:
        line += f" [{' | '.join(buttons)}]"
    if len(dialogs) > 1:
        others = ", ".join(f'"{" ".join(str(d.get("title") or "").split())}"' for d in dialogs[1:])
        line += f" (+{len(dialogs) - 1} more: {others})"
    return line
