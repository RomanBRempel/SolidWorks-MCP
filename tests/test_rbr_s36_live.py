"""Live tests of S-38 ... S-41 against a running SolidWorks (marked solidworks; skipped without it):
a document opened without a window, the documents a failed script leaves, batch globals
and links with one rebuild, a loft along guide curves, rays against an assembly component."""

import os

import pytest

from solidworks_mcp.errors import SolidWorksError
from solidworks_mcp.rbr import assembly, cavity, health, param, script_tool

pytestmark = pytest.mark.solidworks


@pytest.fixture(scope="module", autouse=True)
def _visible_option():
    assembly.apply()   # open_part / open_assembly(visible=False), as the server installs it
    yield
    assembly.unapply()


@pytest.fixture(scope="module")
def block(sw, tmp_path_factory):
    """A saved 40 x 20 x 10 mm block."""
    path = str(tmp_path_factory.mktemp("s36") / "s36_block.sldprt")
    sw.new_part()
    sw.add_box(40.0, 20.0, 10.0)
    sw.save_part(path)
    sw.close_part()
    return path


def _titles(sw):
    return {d.GetTitle(): bool(d.Visible) for d in sw._open_documents()}


def _gdi():
    load = health.sldworks_gui()
    return load["gdi"] if load else None


def test_open_part_without_a_window_costs_no_gdi_objects(sw, block):
    before = _gdi()
    result = sw.open_part(block, visible=False)
    try:
        if before is not None:   # measured 2026-11-23: +0 hidden, +238 into a window (+40 kept after closing)
            assert _gdi() - before < 50
        assert result["visible"] is False
        assert _titles(sw)[result["title"]] is False
        # (mass properties of a hidden part hold about 160 GDI objects until it is closed)
        assert sw.get_mass_properties()["mass_properties"]["volume_mm3"] == pytest.approx(8000.0, rel=1e-6)
    finally:
        sw.close_part()
    assert result["title"] not in _titles(sw)


def test_open_hidden_refuses_an_import(sw, tmp_path):
    with pytest.raises(SolidWorksError, match="native"):
        sw.open_part(str(tmp_path / "x.step"), visible=False)


def test_a_failed_script_closes_the_documents_it_opened(sw, block):
    sw.new_part()
    try:
        current = sw._model.GetTitle()
        code = f"open_doc({block!r}, visible=False)\nraise ValueError('on purpose')"
        with pytest.raises(SolidWorksError) as caught:
            script_tool.run_script(sw, code)
        assert "on purpose" in str(caught.value) and "Closed the documents" in str(caught.value)
        assert os.path.basename(block).lower() not in {t.lower() for t in _titles(sw)}
        assert sw._model.GetTitle() == current
    finally:
        sw.close_part()


def test_set_globals_and_link_dimensions_in_one_rebuild_each(part):
    made = part.add_box(40.0, 20.0, 10.0)
    dims = made["dimensions"]
    out = param.set_globals(part, {"S36_W": 30, "S36_H": "S36_W / 2", "S36_D": 8})
    assert out["ok"] and out["rebuilds"] == 1 and [g["name"] for g in out["globals"]] == ["S36_W", "S36_H", "S36_D"]
    links = {dims["width"]: "S36_W", dims["height"]: "S36_H", dims["depth"]: "S36_D"}
    linked = param.link_dimensions(part, links)
    assert linked["ok"] and linked["count"] == 3 and linked["rebuild_ok"]
    assert part.get_mass_properties()["mass_properties"]["volume_mm3"] == pytest.approx(30 * 15 * 8, rel=1e-6)
    param.set_globals(part, {"S36_W": 50})
    assert part.get_mass_properties()["mass_properties"]["volume_mm3"] == pytest.approx(50 * 25 * 8, rel=1e-6)
    with pytest.raises(SolidWorksError, match="nothing was changed"):
        param.link_dimensions(part, {"D1@NoSuchFeature": "S36_W"})


def test_loft_boss_along_a_guide_curve(part):
    p0 = param.sketch_rect(part, "front", [-10.0, -10.0, 0.0], 20.0, 20.0, name="S36_P0")
    plane = part.add_plane("front", 40.0, name="S36_PL")
    p1 = param.sketch_rect(part, plane["plane"] if "plane" in plane else "S36_PL", [-10.0, -10.0, 40.0], 20.0, 20.0,
                           name="S36_P1")
    straight = 20.0 * 20.0 * 40.0
    # the guide on the right plane (u = -z, v = y) from the top edge of one square to the other's,
    # bulging up to y = 18 halfway
    part.add_sketch("right", [0.0, 10.0], [{"spline": [[-20.0, 18.0], [-40.0, 10.0]]}], name="S36_G")
    out = cavity.loft_boss(part, [p0["sketch"] if "sketch" in p0 else "S36_P0",
                                  p1["sketch"] if "sketch" in p1 else "S36_P1"], ["S36_G"], name="S36_Loft")
    assert out["guides"] == ["S36_G"]
    assert out["added_mm3"] > straight * 1.05   # the guide bulges it out: more than the straight prism


def test_rays_against_an_assembly_component(sw, block, tmp_path):
    sw.new_assembly()
    try:
        sw.insert_component(block, 100.0, 0.0, 0.0)
        comps = sw.list_components()["components"]
        name = comps[0]["name"]
        code = (f"hits = rays(None, [[0.120, 0.010, 0.050]], [[0, 0, -1]], component={name!r})\n"
                "result = hits")
        hits = script_tool.run_script(sw, code)["result"]
        # the block spans x 100..140, y 0..20, z 0..10 in the assembly: the ray enters at z = 10 and leaves at 0
        zs = sorted(round(h["point_m"][2] * 1000, 6) for h in hits)
        assert zs == [0.0, 10.0]
        assert all(round(h["point_m"][0] * 1000, 6) == 120.0 for h in hits)
        top = next(h for h in hits if round(h["point_m"][2] * 1000) == 10)
        assert top["normal"] == pytest.approx([0, 0, 1], abs=1e-9) and top["component"] == name
    finally:
        sw.close_part()
