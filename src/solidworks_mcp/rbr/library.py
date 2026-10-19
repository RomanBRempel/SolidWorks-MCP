"""Library of saved run_script scripts: proven batch jobs are kept and reused by name.

Two folders, the user's first (a name there overrides a built-in one):
- user:     SWMCP_SCRIPTS, or %LOCALAPPDATA%\\solidworks-mcp\\scripts
            (point SWMCP_SCRIPTS at a shared/git folder to share with the team)
- built-in: solidworks_mcp/rbr/lib/*.py, versioned with this fork

A script is a .py file; its module docstring is its description. It sees the
run_script namespace plus `args` (the dict passed to run_saved_script).
"""

import ast
import os
import re

from ..errors import SolidWorksError
from . import journal

_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "lib")


def user_dir() -> str:
    path = os.environ.get("SWMCP_SCRIPTS") or os.path.join(journal.home(), "scripts")
    os.makedirs(path, exist_ok=True)
    return path


def _check_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise SolidWorksError("Script name: 1-64 characters of letters, digits, '_' or '-'.")
    return name


def _describe(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = ast.get_docstring(ast.parse(fh.read())) or ""
    except (OSError, SyntaxError, ValueError):
        return ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _all() -> dict:
    found = {}
    for origin, folder in (("builtin", BUILTIN_DIR), ("user", user_dir())):
        if not os.path.isdir(folder):
            continue
        for f in sorted(os.listdir(folder)):
            if f.endswith(".py") and not f.startswith("_"):
                found[f[:-3]] = {"name": f[:-3], "origin": origin, "path": os.path.join(folder, f)}
    return found


def list_scripts() -> dict:
    items = [{**v, "description": _describe(v["path"])} for v in _all().values()]
    return {"ok": True, "count": len(items), "user_dir": user_dir(), "scripts": items}


def get_script(name: str) -> dict:
    entry = _all().get(_check_name(name))
    if entry is None:
        raise SolidWorksError(f"No saved script '{name}'. list_scripts shows them.")
    with open(entry["path"], encoding="utf-8") as fh:
        code = fh.read()
    return {"ok": True, **entry, "description": _describe(entry["path"]), "code": code}


def save_script(name: str, code: str, description: str = "") -> dict:
    _check_name(name)
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SolidWorksError(f"The script does not parse: {exc}")
    if description and not ast.get_docstring(tree):
        code = '"""' + description.replace('"""', "'''") + '"""\n' + code
    path = os.path.join(user_dir(), name + ".py")
    existed = os.path.isfile(path)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(code if code.endswith("\n") else code + "\n")
    return {"ok": True, "name": name, "path": path, "replaced": existed}


def code_of(name: str) -> str:
    return get_script(name)["code"]
