"""Did an export really carry the geometry? Cheap, dependency-free file checks.

A derived part (insert part + mirror + delete body) saved to STEP right after
its features were added gave a 4.5 KB file with NO solid in it; after
ForceRebuild3 the same SaveAs3 wrote the real thing. SaveAs3 returns success
either way, so the file itself is read back: a STEP names each solid as
MANIFOLD_SOLID_BREP, an STL carries its triangle count in the header (binary)
or one 'facet' per triangle (ASCII).
"""

import os
import re
import struct

_STEP_SOLID = re.compile(rb"MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS", re.IGNORECASE)
_MIN_BYTES = 128  # below this no format holds any geometry


def step_solids(data: bytes) -> int:
    """How many solids a STEP file holds (0 = an empty shell of a file)."""
    return len(_STEP_SOLID.findall(data))


def stl_triangles(data: bytes) -> int | None:
    """Triangle count of an STL (binary header, or ASCII facets); None = not an STL."""
    if len(data) < 15:
        return None
    head = data[:80].lstrip()
    if head[:5].lower() == b"solid" and b"facet" in data[:4096].lower():
        return data.lower().count(b"facet normal")
    if len(data) >= 84:
        (count,) = struct.unpack("<I", data[80:84])
        # a real binary STL is exactly 84 + 50 * count bytes
        if 84 + 50 * count == len(data):
            return count
    return None


def check(fmt: str, path: str, model_bodies: int | None) -> dict:
    """{ok, problem, detail...} for the file just exported; pure given the bytes."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {"ok": False, "problem": f"The exported file cannot be read back: {exc}"}
    out = {"ok": True, "bytes": size}
    if size < _MIN_BYTES:
        return {"ok": False, "bytes": size,
                "problem": f"The exported file is {size} bytes: no geometry fits in that."}
    fmt = (fmt or "").lower()
    if fmt in ("step", "stp") or fmt == "stl":
        with open(path, "rb") as fh:
            data = fh.read()
        if fmt in ("step", "stp"):
            solids = step_solids(data)
            out["solids_in_file"] = solids
            if solids == 0 and (model_bodies or 0) > 0:
                out.update(ok=False, problem=(
                    f"The STEP holds NO solid while the part has {model_bodies} body(ies): "
                    "SolidWorks exported a stale (unrebuilt) state. The rebuild-first export "
                    "should prevent this; if it persists, rebuild and save the part, then export."))
        else:
            triangles = stl_triangles(data)
            out["triangles_in_file"] = triangles
            if triangles == 0:
                out.update(ok=False, problem="The STL holds 0 triangles: the export carried no geometry.")
    return out
