"""export_check: empty STEP/STL files fail loud, real ones pass."""

import struct

from solidworks_mcp.rbr import export_check


def _binary_stl(triangles: int) -> bytes:
    return b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * (50 * triangles)


def test_step_solids_counts_breps():
    data = b"ISO-10303-21; #12=MANIFOLD_SOLID_BREP('body',#5); #44=manifold_solid_brep('b2',#9);"
    assert export_check.step_solids(data) == 2
    assert export_check.step_solids(b"ISO-10303-21; HEADER; nothing here") == 0


def test_stl_triangles_binary_and_ascii():
    assert export_check.stl_triangles(_binary_stl(7)) == 7
    ascii_stl = b"solid part\n" + b"facet normal 0 0 1\nendfacet\n" * 3 + b"endsolid part\n"
    assert export_check.stl_triangles(ascii_stl) == 3
    assert export_check.stl_triangles(b"not an stl at all, but long enough to look at") is None


def test_check_refuses_empty_step_with_bodies(tmp_path):
    f = tmp_path / "part.step"
    f.write_bytes(b"ISO-10303-21;" + b" " * 5000 + b"END-ISO-10303-21;")
    verdict = export_check.check("step", str(f), model_bodies=1)
    assert not verdict["ok"]
    assert "NO solid" in verdict["problem"]


def test_check_accepts_real_step(tmp_path):
    f = tmp_path / "part.step"
    f.write_bytes(b"ISO-10303-21;" + b"#1=MANIFOLD_SOLID_BREP('b',#2);" + b" " * 500)
    verdict = export_check.check("step", str(f), model_bodies=1)
    assert verdict["ok"] and verdict["solids_in_file"] == 1


def test_check_refuses_empty_stl(tmp_path):
    f = tmp_path / "part.stl"
    f.write_bytes(_binary_stl(0) + b"\0" * 200)  # padded: still 0 triangles by ASCII/binary reading
    f.write_bytes(_binary_stl(0))
    verdict = export_check.check("stl", str(f), model_bodies=1)
    assert not verdict["ok"]


def test_check_refuses_tiny_files(tmp_path):
    f = tmp_path / "part.step"
    f.write_bytes(b"almost nothing")
    verdict = export_check.check("step", str(f), model_bodies=1)
    assert not verdict["ok"] and "bytes" in verdict["problem"]


def test_check_passes_other_formats_on_size_alone(tmp_path):
    f = tmp_path / "part.x_t"
    f.write_bytes(b"x" * 4096)
    assert export_check.check("x_t", str(f), model_bodies=1)["ok"]
