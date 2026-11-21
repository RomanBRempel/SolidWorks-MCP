"""Pure tests of rbr/flow_templates.py: one shared template folder, checked against its manifest."""

import json
import os

import pytest

from solidworks_mcp.rbr import flow_templates as ft


def shared(tmp_path, files=None, manifest="auto", tail=b""):
    folder = tmp_path / "shared"
    folder.mkdir()
    files = files or {"air.fwp": b"AAAA"}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    if manifest == "auto":
        manifest = {"note": "n", "templates": [{"file": n, "md5": ft.md5_of(str(folder / n)), "status": "ok"}
                                              for n in files]}
    if manifest is not None:
        (folder / ft.MANIFEST).write_bytes(json.dumps(manifest).encode("utf-8") + tail)
    return str(folder)


def local(tmp_path, names=("air.fwp", "water.fwp")):
    folder = tmp_path / "local" / "Flow" / "template"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"LOCAL-" + name.encode())
    return (str(tmp_path / "local"),)


def test_a_shared_template_is_found_by_name_and_its_md5_checked(tmp_path):
    folder = shared(tmp_path)
    got = ft.resolve("air", folder, search=local(tmp_path))
    assert got["source"] == "shared" and got["path"] == os.path.join(folder, "air.fwp")
    assert got["md5"] == got["manifest"]["md5"] and got["warnings"] == []
    assert ft.resolve("AIR.FWP", folder)["path"] == got["path"]


def test_a_changed_shared_template_is_refused(tmp_path):
    folder = shared(tmp_path)
    with open(os.path.join(folder, "air.fwp"), "wb") as fh:
        fh.write(b"BBBB")
    with pytest.raises(ft.TemplateError, match="does not match"):
        ft.resolve("air", folder)


def test_a_shared_template_missing_from_the_manifest_is_refused(tmp_path):
    folder = shared(tmp_path, {"air.fwp": b"A", "new.fwp": b"N"},
                    manifest={"templates": [{"file": "air.fwp", "md5": "0cc175b9c0f1b6a831c399e269772661"}]})
    with pytest.raises(ft.TemplateError, match="not in templates_manifest.json"):
        ft.resolve("new", folder)


def test_local_templates_are_not_used_when_a_shared_folder_is_set(tmp_path):
    folder = shared(tmp_path)
    search = local(tmp_path)
    with pytest.raises(ft.TemplateError, match="PC-local templates are not used unless allow_local=True"):
        ft.resolve("water", folder, search=search)
    got = ft.resolve("water", folder, allow_local=True, search=search)
    assert got["source"] == "local" and "PC-local template" in got["warnings"][0]
    # the shared one wins even with allow_local
    assert ft.resolve("air", folder, allow_local=True, search=search)["source"] == "shared"


def test_without_a_shared_folder_the_local_ones_work_as_before(tmp_path):
    search = local(tmp_path)
    got = ft.resolve("water", None, search=search)
    assert got["source"] == "local" and got["path"].endswith("water.fwp") and got["warnings"] == []
    with pytest.raises(ft.TemplateError, match="found: air, water"):
        ft.resolve("nothing", None, search=search)
    with pytest.raises(ft.TemplateError, match="template is empty"):
        ft.resolve(" ", None, search=search)


def test_a_path_outside_the_shared_folder_needs_allow_local(tmp_path):
    folder = shared(tmp_path)
    outside = os.path.join(local(tmp_path)[0], "Flow", "template", "air.fwp")
    with pytest.raises(ft.TemplateError, match="outside the shared templates folder"):
        ft.resolve(outside, folder)
    assert ft.resolve(outside, folder, allow_local=True)["source"] == "local"
    assert ft.resolve(os.path.join(folder, "air.fwp"), folder)["source"] == "shared"
    assert ft.resolve(outside, None)["source"] == "local"


def test_stray_bytes_after_the_manifest_are_read_past_and_reported(tmp_path):
    folder = shared(tmp_path, tail=b"}\x9f\x00garbage")
    got = ft.resolve("air", folder)
    assert got["source"] == "shared" and "stray bytes" in got["warnings"][0]


def test_check_folder_and_record(tmp_path):
    folder = shared(tmp_path, {"air.fwp": b"A", "new.fwp": b"N"},
                    manifest={"templates": [{"file": "air.fwp", "md5": "wrong"}, {"file": "gone.fwp", "md5": "x"}]})
    state = ft.check_folder(folder)
    assert state["templates"] == 2 and state["mismatch"] == ["air.fwp"] and state["unlisted"] == ["new.fwp"]
    assert state["missing"] == ["gone.fwp"] and state["ok"] == []
    entry = ft.record(folder, "new.fwp", source="made from a reference project", status="reference")
    assert entry["md5"] == ft.md5_of(os.path.join(folder, "new.fwp"))
    ft.record(folder, "air.fwp")
    state = ft.check_folder(folder)
    assert sorted(state["ok"]) == ["air.fwp", "new.fwp"] and state["mismatch"] == [] and state["unlisted"] == []
    with pytest.raises(ft.TemplateError, match="No 'nope.fwp'"):
        ft.record(folder, "nope.fwp")
