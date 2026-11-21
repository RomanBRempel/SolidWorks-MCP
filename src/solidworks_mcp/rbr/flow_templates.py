"""Flow project templates from ONE shared folder, checked against its manifest.

A template (.fwp) carries far more than its name says - the analysis type and fluid,
but also the domain, the gap and the hidden switches of the automatic mesh of the
project it was made from. Two PCs each with "the same" template in their own
ProgramData folder calculated differently. So, when the private settings name a
shared templates_dir:

- a template is looked up there and nowhere else,
- its MD5 must be the one templates_manifest.json records for it,
- a PC-local template (Flow's own folders) is used only when asked for explicitly
  (allow_local=True), and the answer says so.

Without a templates_dir the old behaviour stays: Flow's own template folders.

The manifest: {"templates": [{"file": "name.fwp", "md5": "...", "source": ..., "date": ...,
"status": ...}, ...]}. Pure: files in, dicts out.
"""

import hashlib
import json
import os

MANIFEST = "templates_manifest.json"
LOCAL_DIRS = (r"C:\ProgramData\COSMOS Applications",)


class TemplateError(ValueError):
    pass


def md5_of(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(folder: str) -> tuple:
    """(manifest dict, warnings). A missing manifest is {} with a warning; bytes after the
    JSON object (a file overwritten in place by a shorter one) are tolerated and reported."""
    path = os.path.join(folder, MANIFEST)
    if not os.path.isfile(path):
        return {}, [f"{MANIFEST} is missing in the shared templates folder: MD5s cannot be checked."]
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8-sig", "replace")
    warnings = []
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data, end = json.JSONDecoder().raw_decode(text.lstrip())
        except ValueError as exc:
            raise TemplateError(f"{MANIFEST} in the shared templates folder is not JSON: {exc}.") from None
        if text.lstrip()[end:].strip():
            warnings.append(f"{MANIFEST} has stray bytes after its JSON object (it was overwritten in place); "
                            "it was read up to the object's end - rewrite the file.")
    if not isinstance(data, dict):
        raise TemplateError(f"{MANIFEST} must hold a JSON object with a 'templates' list.")
    return data, warnings


def entries(manifest: dict) -> dict:
    """{file name (lower case): entry}."""
    out = {}
    for item in manifest.get("templates") or ():
        if isinstance(item, dict) and item.get("file"):
            out[str(item["file"]).lower()] = item
    return out


def _fwp(name: str) -> str:
    return name if name.lower().endswith(".fwp") else name + ".fwp"


def shared_names(folder: str) -> list:
    try:
        return sorted(n for n in os.listdir(folder) if n.lower().endswith(".fwp"))
    except OSError:
        return []


def find_local(name: str, search=LOCAL_DIRS) -> tuple:
    """(path or None, the local template names found)."""
    found = []
    for root in search:
        for folder, _, files in os.walk(root):
            found += [os.path.join(folder, f) for f in files if f.lower().endswith(".fwp")]
    wanted = _fwp(name).lower()
    path = next((p for p in found if os.path.basename(p).lower() == wanted), None)
    return path, sorted({os.path.splitext(os.path.basename(p))[0] for p in found})


def resolve(template: str, shared_dir: str | None, allow_local: bool = False, search=LOCAL_DIRS) -> dict:
    """Where a template comes from: {path, source: 'shared' | 'local', md5, manifest: entry | None,
    warnings: [...]}. Refuses what the rules above refuse, naming what exists."""
    text = str(template or "").strip()
    if not text:
        raise TemplateError("template is empty: name a Flow project template (.fwp).")
    warnings = []
    if os.path.isabs(text):
        if not os.path.isfile(text):
            raise TemplateError("No template file at the given path.")
        inside = bool(shared_dir) and os.path.normcase(os.path.abspath(os.path.dirname(text))) == \
            os.path.normcase(os.path.abspath(shared_dir))
        if not inside:
            if shared_dir and not allow_local:
                raise TemplateError("That template file is outside the shared templates folder: templates are "
                                    "taken from the shared folder only (allow_local=True overrides, at the price "
                                    "of a setup that other PCs do not have).")
            if shared_dir:
                warnings.append("A template from outside the shared templates folder was used (allow_local).")
            return {"path": text, "source": "local", "md5": md5_of(text), "manifest": None, "warnings": warnings}
        name = os.path.basename(text)
    else:
        name = _fwp(text)
    if not shared_dir:
        path, names = find_local(name, search)
        if path is None:
            raise TemplateError(
                f"No Flow template '{text}' (found: {', '.join(names) or 'none'}). Make one in Flow Simulation "
                "(Wizard, then Tools > Flow Simulation > Project > Create Template) or pass the .fwp path.")
        return {"path": path, "source": "local", "md5": md5_of(path), "manifest": None, "warnings": warnings}
    if not os.path.isdir(shared_dir):
        raise TemplateError("The shared templates folder named by the private settings (templates_dir) is not "
                            "reachable.")
    manifest, manifest_warnings = load_manifest(shared_dir)
    warnings += manifest_warnings
    have = {n.lower(): n for n in shared_names(shared_dir)}
    if name.lower() in have:
        path = os.path.join(shared_dir, have[name.lower()])
        digest = md5_of(path)
        entry = entries(manifest).get(name.lower())
        if entry is None:
            if manifest:
                raise TemplateError(f"Template '{have[name.lower()]}' is in the shared folder but not in "
                                    f"{MANIFEST}: add it there with its MD5 ({digest}) before using it.")
        elif str(entry.get("md5", "")).lower() != digest:
            raise TemplateError(f"Template '{have[name.lower()]}' does not match {MANIFEST}: the file's MD5 is "
                                f"{digest}, the manifest says {entry.get('md5')}. The file was changed or is not "
                                "fully synced; do not calculate with it.")
        return {"path": path, "source": "shared", "md5": digest, "manifest": entry, "warnings": warnings}
    if allow_local:
        path, names = find_local(name, search)
        if path is not None:
            warnings.append(f"The PC-local template '{os.path.basename(path)}' was used (allow_local): it is not "
                            "in the shared folder, another PC may have a different file under this name.")
            return {"path": path, "source": "local", "md5": md5_of(path), "manifest": None, "warnings": warnings}
    raise TemplateError(
        f"No template '{text}' in the shared templates folder (it has: "
        f"{', '.join(os.path.splitext(n)[0] for n in have.values()) or 'none'}). PC-local templates are not "
        "used unless allow_local=True.")


def check_folder(shared_dir: str) -> dict:
    """For server_health: every shared template against the manifest -
    {templates: n, ok: [...], mismatch: [...], unlisted: [...], missing: [...], warnings: [...]}."""
    manifest, warnings = load_manifest(shared_dir)
    listed = entries(manifest)
    out = {"templates": 0, "ok": [], "mismatch": [], "unlisted": [], "missing": [], "warnings": warnings}
    present = {n.lower(): n for n in shared_names(shared_dir)}
    out["templates"] = len(present)
    for low, name in present.items():
        entry = listed.get(low)
        if entry is None:
            out["unlisted"].append(name)
        elif str(entry.get("md5", "")).lower() == md5_of(os.path.join(shared_dir, name)):
            out["ok"].append(name)
        else:
            out["mismatch"].append(name)
    out["missing"] = [item["file"] for low, item in listed.items() if low not in present]
    return out


def record(shared_dir: str, file_name: str, **fields) -> dict:
    """Add or update a template's manifest entry with the file's current MD5; returns the entry."""
    path = os.path.join(shared_dir, file_name)
    if not os.path.isfile(path):
        raise TemplateError(f"No '{file_name}' in the shared templates folder.")
    manifest, _ = load_manifest(shared_dir)
    items = [i for i in (manifest.get("templates") or []) if isinstance(i, dict)]
    entry = next((i for i in items if str(i.get("file", "")).lower() == file_name.lower()), None)
    if entry is None:
        entry = {"file": file_name}
        items.append(entry)
    entry.update({k: v for k, v in fields.items() if v is not None})
    entry["md5"] = md5_of(path)
    manifest["templates"] = items
    tmp = os.path.join(shared_dir, MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, os.path.join(shared_dir, MANIFEST))
    return entry
