"""The user's private settings: what belongs to their own work and never to this repository.

One JSON file outside the source tree, read locally and only on demand:

    %LOCALAPPDATA%\\solidworks-mcp\\private\\cfd_private.json
    (the folder is overridden by the environment variable SWMCP_PRIVATE_DIR)

Keys the server knows (all optional):
- templates_dir: the shared folder of Flow project templates (.fwp) and its
  templates_manifest.json,
- cases_dir: where Flow case files are written and looked up by name,
- runs_registry: the CSV every finished solve is appended to,
- sandbox_dir: where a regression may make file copies and solve them,
- compare_runs_dir: the folder whose project folders flow_compare takes by name,
- flow_folder_slot: 'k/M' - this PC takes only the Flow folder numbers n with n % M == k
  (two PCs sharing a synced folder: '0/2' on one, '1/2' on the other),
- reference: {model, carrier_model, project_name, results_dir, case_name, velocity_mps,
  expected: {cells_total, <goal>: value}, tolerance: {<goal>_rel | <goal>_abs},
  reduction: {q_pa, S_m2, c_m, z_cg_m}} - the reference run of the regression,
- leak_denylist: strings that must never reach a commit, a PR or a report.

Nothing from this file is written to the journal or returned by a tool except the
paths a tool was asked to use; reports name the keys, not the values.
"""

import json
import os

FILE_NAME = "cfd_private.json"
ENV_DIR = "SWMCP_PRIVATE_DIR"
KEYS = ("templates_dir", "cases_dir", "runs_registry", "sandbox_dir", "compare_runs_dir", "flow_folder_slot",
        "reference", "leak_denylist")


class PrivateError(ValueError):
    pass


def private_dir() -> str:
    override = os.environ.get(ENV_DIR, "").strip()
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "solidworks-mcp", "private")


def private_path() -> str:
    return os.path.join(private_dir(), FILE_NAME)


def load(path: str | None = None) -> dict:
    """The private settings; {} when there is no file. A file that is not a JSON object is an error."""
    path = path or private_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PrivateError(f"The private settings file ({FILE_NAME} in the private folder) cannot be read: "
                           f"{type(exc).__name__}.") from None
    if not isinstance(data, dict):
        raise PrivateError(f"The private settings file ({FILE_NAME}) must hold a JSON object.")
    return data


def get(key: str, default=None, settings: dict | None = None):
    """A value by dotted key ('reference.expected.cells_total'); default when it is not set."""
    node = load() if settings is None else settings
    for part in str(key).split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def need(key: str, settings: dict | None = None):
    """A value that a tool cannot work without; names the key (never a value) when it is missing."""
    value = get(key, None, settings)
    if value in (None, "", [], {}):
        raise PrivateError(f"The private settings have no '{key}': set it in {FILE_NAME} in the private folder "
                           f"(%LOCALAPPDATA%\\solidworks-mcp\\private, or the folder named by {ENV_DIR}).")
    return value


def status(settings: dict | None = None) -> dict:
    """For server_health: is the file there, and which of the known keys are set - no values."""
    try:
        data = load() if settings is None else settings
    except PrivateError as exc:
        return {"present": True, "readable": False, "error": str(exc)}
    present = bool(data) or os.path.isfile(private_path())
    return {"present": present, "readable": True,
            "keys_set": [k for k in KEYS if data.get(k) not in (None, "", [], {})],
            "keys_missing": [k for k in KEYS if data.get(k) in (None, "", [], {})]}
