"""Live tests of rebuild_chain against a running SolidWorks (marked solidworks; skipped without it).

O-60: a chain whose files were made elsewhere (another PC, another drive letter, moved) stores
reference paths that no longer exist; SolidWorks finds those files by name, and so must the
chain - otherwise the parts past the first level were never rebuilt."""

import os
import shutil

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import assembly, chain, derive

pytestmark = pytest.mark.solidworks


@pytest.fixture(scope="module", autouse=True)
def _rbr_patches():
    assembly.apply()
    yield
    assembly.unapply()


def _close_under(sw, folder):
    for doc in list(sw._open_documents()):
        if (doc.GetPathName() or "").lower().startswith(str(folder).lower()):
            sw._sw.CloseDoc(doc.GetPathName())
    sw._model = None


def _volume_on_disk(sw, path):
    sw.open_part(path, visible=False)
    try:
        return sw.get_mass_properties()["mass_properties"]["volume_mm3"]
    finally:
        sw._sw.CloseDoc(sw._model.GetPathName())
        sw._model = None


def test_a_moved_chain_is_rebuilt_to_its_last_part(sw, tmp_path):
    made, moved = tmp_path / "made", tmp_path / "moved"
    made.mkdir()
    moved.mkdir()
    names = ["c60_master.sldprt", "c60_a.sldprt", "c60_b.sldprt"]
    master, a, b = (str(made / n) for n in names)
    sw.new_part()
    sw.add_box(40.0, 20.0, 10.0)
    sw.save_part(master)
    derive.derive_part(sw, sw._part_bodies()[0].Name, a, close=True)
    sw.open_part(a)
    derive.derive_part(sw, sw._part_bodies()[0].Name, b, close=True)
    _close_under(sw, made)
    for n in names:
        shutil.copy2(made / n, moved / n)
        os.remove(made / n)   # the stored paths now lead nowhere
    sw.open_part(str(moved / names[0]))
    try:
        sw.run_guarded(sw.set_dimension, "D1@BlockExtrude", 25.0)
        sw._model.Save3(1, 0, 0)
        result = sw.run_guarded(chain.rebuild_chain, sw, None)
    finally:
        _close_under(sw, moved)
    assert [p.lower() for p in result["chain"]] == ["c60_a.sldprt", "c60_b.sldprt"]
    assert sorted(p.lower() for p in result["saved"]) == ["c60_a.sldprt", "c60_b.sldprt"]
    assert any(r["part"].lower() == "c60_b.sldprt" for r in result["relinked"])
    assert _volume_on_disk(sw, str(moved / names[2])) == pytest.approx(40 * 20 * 25, rel=1e-6)


def test_bodies_of_a_windowless_part_are_not_saved_to_a_new_part(sw, tmp_path):
    """SaveToFile3 on a part loaded without a window killed SolidWorks (2026-11-23)."""
    path = str(tmp_path / "c60_hidden.sldprt")
    sw.new_part()
    sw.add_box(10.0, 10.0, 10.0)
    sw.save_part(path)
    sw.close_part()
    sw.open_part(path, visible=False)
    try:
        with pytest.raises(SolidWorksError, match="without a window"):
            derive.derive_part(sw, sw._part_bodies()[0].Name, str(tmp_path / "c60_new.sldprt"))
        assert not os.path.exists(tmp_path / "c60_new.sldprt")
    finally:
        _close_under(sw, tmp_path)
