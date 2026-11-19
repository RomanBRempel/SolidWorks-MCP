"""FeatureManager folders for the components of an assembly.

What works was found by trying every documented way on SolidWorks 2025 SP3
(2026-11-19, a test assembly of cubes, with the UI live and with it frozen the
way a tool call freezes it - the results were the same):

- IFeatureManager.InsertFeatureTreeFolder2(swFeatureTreeFolder_Containing = 2)
  with the components selected makes a folder that holds them.
- InsertFeatureTreeFolder2(swFeatureTreeFolder_EmptyBefore = 1) makes an EMPTY
  folder before the first selected item, whatever else is selected. Scripts
  that passed 1 and expected a filled folder got an empty one.
- IAssemblyDoc.ReorderComponents(components, folder, where) moves components
  INTO the folder only with where = 3 (swReorderComponents_LastInFolder) or
  4 (FirstInFolder). With 1 (After) or 2 (Before) the components land next to
  the folder - and the call answers True all the same.
- IFeatureManager.MoveToFolder answers False for components (it moves features).

ReorderComponents wants its components as VARIANT(VT_ARRAY | VT_DISPATCH).

No call here is trusted on its return value: the folder is read back, through
IFeatureFolder.GetFeatures and through the tree control (ITreeControlItem), and
a folder that does not hold what was asked for is an error.

The tree control writes more than the name: a status mark goes in front -
'(f) Folder 1' (in the installation's language) for a folder whose components
are all fixed, '(-)' for floating ones - and a component reads
'(f) Bolt<3> (Default) <<Default>_Display State 1>'. A tree item is therefore
matched by the feature it stands for, not by its text (comparing the text with
the name found no folder at all in the live run of 2026-11-19, and the check
was skipped in silence).

Measured on an assembly of 70 components, ten of them into one folder: 1.7 s
a call with the UI frozen as a tool call freezes it, 3.4 s inside
ui_unlocked() - a script has no reason to unfreeze the UI around tree_folder.
(4.7 s and 18 s while every name was looked up in a listing of its own.)
"""

import re

import pythoncom
import win32com.client

from .. import binding
from ..errors import SolidWorksError

SW_FOLDER_CONTAINING = 2          # swFeatureTreeFolderType_e
SW_REORDER_LAST_IN_FOLDER = 3     # swReorderComponentsWhere_e
SW_FM_PANE_BOTTOM = 1             # the pane GetFeatureTreeRootItem2 reads (the one scripts used)
FOLDER_TYPE = "FtrFolder"


def tree_label(component_name: str) -> str:
    """How the tree control writes a component's instance name: 'Bolt-3' -> 'Bolt<3>'; pure."""
    return re.sub(r"-(\d+)$", r"<\1>", component_name)


def missing_from(wanted: list, inside: list) -> list:
    """The wanted component names a folder does not hold (compared case-blind); pure."""
    have = {str(n).lower() for n in inside}
    return [n for n in wanted if str(n).lower() not in have]


def shows(text: str, label: str) -> bool:
    """Does a tree item's text show `label` as a whole word? '(f) Bolt<3> (Default)' shows
    'Bolt<3>' and not 'olt<3>'; a status mark before it and a configuration after it do not
    count; pure, case-blind."""
    return re.search(r"(?:^|[\s)])" + re.escape(str(label).lower()) + r"(?:$|[\s(])", str(text).lower()) is not None


def missing_in_tree(wanted: list, items: list) -> list:
    """The wanted components no tree item of the folder shows ('Bolt<3>' inside the item text); pure."""
    return [n for n in wanted if not any(shows(t, tree_label(n)) for t in items)]


def _folder_feature(session, asm, name: str):
    feat = binding.wrap(asm.FeatureByName(name), session._mod.IFeature)
    if feat is not None and feat.GetTypeName2() != FOLDER_TYPE:
        raise SolidWorksError(f"'{name}' is already the name of a feature that is not a folder "
                              f"({feat.GetTypeName2()}); choose another name.")
    return feat


def folder_components(session, folder) -> list:
    """Names of what a folder feature holds, as IFeatureFolder reports them."""
    spec = binding.wrap(folder.GetSpecificFeature2(), session._mod.IFeatureFolder)
    if spec is None:
        return []
    return [binding.wrap(f, session._mod.IFeature).Name for f in (spec.GetFeatures() or ())]


def _is_folder_item(mod, item, name: str) -> bool:
    """Is this tree item the folder feature called `name`? The text only narrows it down (it
    carries a status mark: '(f) Folder 1'); the feature behind the item decides."""
    if not shows(item.Text or "", name):
        return False
    try:
        feat = binding.wrap(item.Object, mod.IFeature)
        return feat is not None and feat.Name == name and feat.GetTypeName2() == FOLDER_TYPE
    except (pythoncom.com_error, AttributeError):
        return False   # an item that stands for something else (a component of that name)


def resolve_components(session, asm, components: list) -> list:
    """[(name, component)] for the names given, without repeats, from ONE listing of the assembly.

    session._component_by_name lists and wraps every component of the assembly for each
    name it looks up: 0.3 s a name at 70 components (2.9 of the 4.7 s a folder of ten took).
    A name that is not an exact instance name (a part name alone, a wrong one) still goes
    to it - its rules and its words for the error are upstream's."""
    listed = {}
    for comp in session._components(asm):
        listed.setdefault(comp.Name2.lower(), comp)
    out, seen = [], set()
    for wanted in components:
        if "/" in str(wanted):
            raise SolidWorksError(f"'{wanted}' is inside a sub-assembly: a folder of this assembly holds only "
                                  "its own top-level components.")
        comp = listed.get(str(wanted).strip().lower()) or session._component_by_name(asm, str(wanted))
        name = comp.Name2
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append((name, comp))
    return out


def tree_items(session, model, name: str) -> list | None:
    """Texts of the tree items under the folder called `name`, read from the tree control
    (ITreeControlItem); None when the tree control cannot be read.

    Inside a tool call the feature tree is switched off, and a switched-off tree
    is not brought up to date: it is switched on for the read and put back."""
    mod = session._mod
    fm = binding.wrap(model.FeatureManager, mod.IFeatureManager)
    tree_on = bool(fm.EnableFeatureTree)
    try:
        if not tree_on:
            fm.EnableFeatureTree = True
        fm.UpdateFeatureTree()
        root = binding.wrap(fm.GetFeatureTreeRootItem2(SW_FM_PANE_BOTTOM), mod.ITreeControlItem)
        item = binding.wrap(root.GetFirstChild(), mod.ITreeControlItem) if root is not None else None
        while item is not None:
            if _is_folder_item(mod, item, name):
                kids = []
                child = binding.wrap(item.GetFirstChild(), mod.ITreeControlItem)
                while child is not None:
                    kids.append(child.Text)
                    child = binding.wrap(child.GetNext(), mod.ITreeControlItem)
                return kids
            item = binding.wrap(item.GetNext(), mod.ITreeControlItem)
        return None
    except pythoncom.com_error:
        return None
    finally:
        if not tree_on:
            try:
                fm.EnableFeatureTree = False
            except pythoncom.com_error:
                pass


def _select(session, model, comps) -> int:
    model.ClearSelection2(True)
    data = binding.wrap(model.SelectionManager, session._mod.ISelectionMgr).CreateSelectData()
    return sum(1 for c in comps if c.Select4(True, data, False))


def _delete_folder(model, name: str) -> None:
    """Remove a folder this call made and could not fill (its contents, if any, stay in the tree)."""
    try:
        model.ClearSelection2(True)
        if model.Extension.SelectByID2(name, "FTRFOLDER", 0, 0, 0, False, 0, None, 0):
            model.EditDelete()
        model.ClearSelection2(True)
    except pythoncom.com_error:
        pass


def tree_folder(session, name: str, components: list) -> dict:
    """Put top-level components of the current assembly into the FeatureManager folder `name`.

    The folder is created (holding the components) when there is none of that
    name; an existing folder of that name gets the components added at its end.
    The result is read back from the folder and from the tree control: every
    named component must be inside, or the call fails - and a folder it had
    just made is removed again, so a failed call leaves no empty folder.
    """
    name = str(name or "").strip()
    if not name:
        raise SolidWorksError("Give the folder a name.")
    if not components or isinstance(components, str):
        raise SolidWorksError("components: a list of component names (list_components), at least one.")
    asm = session._require_assembly()
    model = session._model
    mod = session._mod
    resolved = resolve_components(session, asm, components)
    wanted = [n for n, _ in resolved]
    comps = [c for _, c in resolved]
    folder = _folder_feature(session, asm, name)
    created = folder is None
    fm = binding.wrap(model.FeatureManager, mod.IFeatureManager)
    if created:
        selected = _select(session, model, comps)
        if selected != len(comps):
            model.ClearSelection2(True)
            raise SolidWorksError(f"Only {selected} of {len(comps)} components could be selected; no folder made.")
        made = binding.wrap(fm.InsertFeatureTreeFolder2(SW_FOLDER_CONTAINING), mod.IFeature)
        model.ClearSelection2(True)
        if made is None:
            raise SolidWorksError("SolidWorks made no folder for these components (InsertFeatureTreeFolder2 "
                                  "returned nothing). Components of a pattern or of a sub-assembly cannot be "
                                  "put into a folder of their own.")
        made.Name = name
        folder = _folder_feature(session, asm, name)
        if folder is None:
            raise SolidWorksError(f"The new folder could not be named '{name}' (it is called '{made.Name}').")
    inside = folder_components(session, folder)
    moved = missing_from(wanted, inside)
    if moved:
        array = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH,
                                        [c._oleobj_ for n, c in resolved if n in moved])
        asm.ReorderComponents(array, folder, SW_REORDER_LAST_IN_FOLDER)
        inside = folder_components(session, folder)
    left_out = missing_from(wanted, inside)
    items = tree_items(session, model, name)
    not_shown = missing_in_tree(wanted, items) if items is not None else []
    if left_out or not_shown or not inside:
        if created and not inside:
            _delete_folder(model, name)
        problem = (f"not in the folder: {left_out}" if left_out else
                   f"the tree control does not show them inside: {not_shown}" if not_shown else "the folder is empty")
        raise SolidWorksError(
            f"Folder '{name}' was not filled as asked - {problem}. The folder holds {inside}"
            + (f", the tree shows {len(items)} item(s) under it" if items is not None else "")
            + (". The empty folder was removed again." if created and not inside else ".")
            + " Components of a pattern, or driven by a pattern, cannot be moved.")
    return {"ok": True, "folder": name, "created": created, "count": len(inside), "components": inside,
            "added": [n for n in wanted if created or n in moved],
            "tree_items": None if items is None else len(items)}


def list_tree_folders(session) -> dict:
    """The folders of the current assembly's tree and what each holds."""
    session._require_assembly()
    out = []
    for feat in session._iter_features():
        if feat.GetTypeName2() == FOLDER_TYPE and "EndTag" not in feat.Name:
            inside = folder_components(session, feat)
            out.append({"folder": feat.Name, "count": len(inside), "components": inside})
    return {"ok": True, "count": len(out), "folders": out}
