"""API reference built into the server (S-16): find a method, property or constant by name.

The answer comes from what this SolidWorks really has, not from a help page of
another release: the interfaces are the makepy wrappers generated from the
installed sldworks.tlb (the same ones every tool calls through), the constants
are read from the swconst.tlb next to SLDWORKS.exe. About 14 000 members and
9 000 enum values; the index is built once per server process (under a second)
and searched in memory.

api_search('InsertCutSwept')            -> the methods with their parameters, in call order
api_search('freeze', kind='constant')   -> swMoveFreezeBarTo_e.swMoveFreezeBarToTop = 4 ...
api_search('IFeatureManager.EditFreeze')-> one member of one interface
"""

import inspect
import os
import re

from ..errors import SolidWorksError

SWCONST_TLB_GUID = "{4687F359-55D0-4CD3-B6CF-2EB42C11F989}"

_index = {"members": None, "constants": None, "source": {}}


def _doc_of(fn) -> str:
    """makepy writes the type library's help string as the method's docstring."""
    return (getattr(fn, "__doc__", None) or "").strip()


def _signature(fn) -> str:
    try:
        params = [name for name in inspect.signature(fn).parameters if name != "self"]
    except (TypeError, ValueError):
        return "(...)"
    return "(" + ", ".join(params) + ")"


def build_members(module) -> list:
    """[{interface, member, kind, signature, doc}] for every interface of a makepy module."""
    rows = []
    for name, cls in vars(module).items():
        if not inspect.isclass(cls) or not hasattr(cls, "CLSID") or name.startswith("_"):
            continue
        for member, fn in vars(cls).items():
            if member.startswith("_") or not inspect.isfunction(fn):
                continue
            rows.append({"interface": name, "member": member, "kind": "method",
                         "signature": _signature(fn), "doc": _doc_of(fn)})
        readable = getattr(cls, "_prop_map_get_", None) or {}
        writable = getattr(cls, "_prop_map_put_", None) or {}
        for member in sorted(set(readable) | set(writable)):
            access = ("get" if member in readable else "") + ("/set" if member in writable else "")
            rows.append({"interface": name, "member": member, "kind": "property",
                         "signature": access.strip("/"), "doc": ""})
    return rows


def build_constants(typelib) -> list:
    """[{interface: enum name, member, kind: 'constant', signature: '= value', doc, value}]
    from a loaded type library (pythoncom ITypeLib)."""
    import pythoncom
    rows = []
    for i in range(typelib.GetTypeInfoCount()):
        info = typelib.GetTypeInfo(i)
        attr = info.GetTypeAttr()
        if attr.typekind != pythoncom.TKIND_ENUM:
            continue
        enum = typelib.GetDocumentation(i)[0]
        for v in range(attr.cVars):
            desc = info.GetVarDesc(v)
            rows.append({"interface": enum, "member": info.GetNames(desc.memid)[0], "kind": "constant",
                         "signature": f"= {desc.value}", "doc": "", "value": desc.value})
    return rows


def _load_swconst(session):
    import pythoncom
    try:
        folder = os.path.dirname(str(session._sw.GetExecutablePath()))
        for candidate in (os.path.join(folder, "swconst.tlb"), os.path.join(str(session._sw.GetExecutablePath()),
                                                                             "swconst.tlb")):
            if os.path.isfile(candidate):
                return pythoncom.LoadTypeLib(candidate), candidate
    except Exception:  # noqa: BLE001
        pass
    major = int(str(session._revision()).split(".")[0])
    return pythoncom.LoadRegTypeLib(SWCONST_TLB_GUID, major, 0), f"registry {SWCONST_TLB_GUID} {major}.0"


def rank(rows: list, query: str, kind: str = "any", interface: str | None = None, limit: int = 30) -> list:
    """The rows matching every word of `query` (case-blind; 'Interface.Member' narrows to an
    interface), best first: the exact name, then a name that starts with it, then one that
    contains it, then a match in the help text only. Pure."""
    text = str(query or "").strip()
    if "." in text and interface is None:
        interface, text = text.split(".", 1)
    words = [w for w in re.split(r"[\s,]+", text.lower()) if w]
    if not words and not interface:
        raise SolidWorksError("Give a name or words to search for, e.g. 'InsertCutSwept' or 'freeze bar'.")
    wanted_kind = str(kind or "any").lower()
    if wanted_kind not in ("any", "method", "property", "constant"):
        raise SolidWorksError(f"kind is 'any', 'method', 'property' or 'constant' (got {kind!r}).")
    iface = interface.lower() if interface else None
    scored = []
    for row in rows:
        if wanted_kind != "any" and row["kind"] != wanted_kind:
            continue
        owner = row["interface"].lower()
        if iface and owner != iface and owner != "i" + iface:
            continue
        name = row["member"].lower()
        hay = f"{name} {owner} {row['doc'].lower()}"
        if not all(w in hay for w in words):
            continue
        joined = "".join(words)
        score = (0 if name == joined else 1 if name.startswith(joined) else 2 if joined in name
                 else 3 if all(w in name for w in words) else 4 if all(w in f"{name} {owner}" for w in words) else 5)
        scored.append((score, len(name), row["interface"], row["member"], row))
    scored.sort(key=lambda item: item[:4])
    return [item[4] for item in scored[:max(1, int(limit))]]


def api_search(session, query: str, kind: str = "any", interface: str | None = None, limit: int = 30) -> dict:
    """Search the installed SolidWorks API (see the module doc)."""
    session._ensure()
    if _index["members"] is None:
        _index["members"] = build_members(session._mod)
        _index["source"]["interfaces"] = getattr(session._mod, "__file__", None)
    if _index["constants"] is None:
        try:
            typelib, where = _load_swconst(session)
            _index["constants"] = build_constants(typelib)
            _index["source"]["constants"] = where
        except Exception as exc:  # noqa: BLE001 - the interfaces still answer
            _index["constants"] = []
            _index["source"]["constants"] = f"not loaded: {exc}"
    rows = _index["members"] + _index["constants"]
    found = rank(rows, query, kind, interface, limit)
    results = []
    for row in found:
        entry = {"name": f"{row['interface']}.{row['member']}", "kind": row["kind"]}
        if row["kind"] == "method":
            entry["signature"] = f"{row['member']}{row['signature']}"
        elif row["kind"] == "property":
            entry["access"] = row["signature"]
        else:
            entry["value"] = row["value"]
        if row["doc"]:
            entry["doc"] = row["doc"]
        results.append(entry)
    return {"ok": True, "query": query, "count": len(results), "results": results,
            "indexed": {"members": len(_index["members"]), "constants": len(_index["constants"])},
            "revision": session._revision(),
            "note": None if results else "Nothing matches: try fewer or other words (names are English, "
                                         "e.g. 'rollback', 'tess', 'persist')."}
