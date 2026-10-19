"""Guard for following upstream: rbr patches these SolidWorksSession methods, so a
change to any of them upstream must be looked at before merging.

When this fails in an upstream-sync PR: read the upstream change, adapt
rbr/fastpath.py or rbr/journal.py if needed, then refresh the hashes with
    python tests/rbr/test_upstream_contract.py --update
"""

import hashlib
import inspect
import json
import os
import sys

CONTRACT = os.path.join(os.path.dirname(__file__), "upstream_contract.json")
# patched by rbr, or relied on by it
WATCHED = ["_iter_features", "_history_snapshot", "_roll_back", "_remove_features_since", "_history",
           "run_guarded", "new_part", "new_assembly", "_pick_planar_face", "_body_faces", "_solid_body",
           "_component_by_name", "_require_model", "_require_part", "_require_assembly", "_quiet",
           "connect", "add_shell", "_fillet_refusal", "list_edges", "_edge_entry", "_largest_rounds", "_persist_refs", "_uniform_fillet", "_face_edges", "_feature_edges", "_box_corners", "_MAX_FILLET_TRIALS", "_thin_walls", "_face_triangles", "_reattach_if_gone", "_command_in_progress", "_quiet_ui", "_wake_all"]


def _original(name):
    from solidworks_mcp.session import SolidWorksSession
    from solidworks_mcp.rbr import fastpath, guard, journal, speed
    fn = (fastpath._ORIGINAL.get(name) or speed._ORIGINAL.get(name) or guard._ORIGINAL.get(name)
          or journal._ORIGINAL.get(name)
          or getattr(SolidWorksSession, name, None))
    return getattr(fn, "__wrapped__", fn)  # new_part/new_assembly carry the original


def current_hashes() -> dict:
    out = {}
    for name in WATCHED:
        fn = _original(name)
        if fn is not None and not callable(fn):  # a constant: hash its value
            out[name] = hashlib.sha1(repr(fn).encode()).hexdigest()[:12]
            continue
        out[name] = None if fn is None else hashlib.sha1(inspect.getsource(fn).replace("\r\n", "\n").encode()).hexdigest()[:12]
    return out


def test_upstream_methods_unchanged():
    with open(CONTRACT, encoding="utf-8") as fh:
        expected = json.load(fh)
    now = current_hashes()
    missing = [n for n, h in now.items() if h is None]
    assert not missing, f"Upstream removed/renamed: {missing}"
    changed = [n for n in WATCHED if expected.get(n) != now[n]]
    assert not changed, f"Upstream changed methods rbr depends on: {changed}. See this file's docstring."


if __name__ == "__main__" and "--update" in sys.argv:
    sys.path.insert(0, os.path.dirname(__file__))
    import conftest  # noqa: F401 - stubs pywin32 off Windows
    with open(CONTRACT, "w", encoding="utf-8") as fh:
        json.dump(current_hashes(), fh, indent=2)
    print("updated", CONTRACT)
