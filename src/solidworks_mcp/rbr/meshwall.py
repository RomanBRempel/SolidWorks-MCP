"""Material thickness from a tessellation, in pure Python: no SolidWorks calls.

Why: IModelDoc2.RayIntersections took 29 s for 2560 rays on a 120-face lofted
body (GAT-27 v5, SW 2025), past an MCP client's timeout. The display
tessellation of the same body (4632 triangles) is read in 0.5 s; casting
against it here, only as far as the thickness that matters, takes well under
a second. The tessellation is within SolidWorks' chord tolerance of the
surface, so thicknesses are good to about a tenth of a millimetre on a body
of this size.

Each ray starts just inside the material at a sample point and runs along the
inward normal up to max_mm; the first triangle it meets is where the material
ends. Triangles are bucketed in a uniform grid with cells of max_mm, so a ray
tests only the few cells its segment crosses.
"""

import math
from collections import defaultdict

EPS = 1e-9


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def ray_triangle(origin, direction, a, b, c):
    """Distance along `direction` (unit) to triangle abc, or None (Moller-Trumbore, both sides)."""
    e1, e2 = _sub(b, a), _sub(c, a)
    p = _cross(direction, e2)
    det = _dot(e1, p)
    if -EPS < det < EPS:
        return None
    inv = 1.0 / det
    s = _sub(origin, a)
    u = _dot(s, p) * inv
    if u < 0.0 or u > 1.0:
        return None
    q = _cross(s, e1)
    v = _dot(direction, q) * inv
    if v < 0.0 or u + v > 1.0:
        return None
    t = _dot(e2, q) * inv
    return t if t > EPS else None


class Grid:
    """Triangles bucketed by the cells their bounding boxes touch."""

    def __init__(self, triangles, cell_mm):
        self.cell = max(cell_mm, 1e-3)
        self.tris = triangles  # [(face_index, a, b, c), ...]
        self.cells = defaultdict(list)
        for n, (_, a, b, c) in enumerate(triangles):
            lo = [math.floor(min(a[i], b[i], c[i]) / self.cell) for i in range(3)]
            hi = [math.floor(max(a[i], b[i], c[i]) / self.cell) for i in range(3)]
            for x in range(lo[0], hi[0] + 1):
                for y in range(lo[1], hi[1] + 1):
                    for z in range(lo[2], hi[2] + 1):
                        self.cells[(x, y, z)].append(n)

    def candidates(self, start, end):
        lo = [math.floor(min(start[i], end[i]) / self.cell) for i in range(3)]
        hi = [math.floor(max(start[i], end[i]) / self.cell) for i in range(3)]
        seen = set()
        for x in range(lo[0], hi[0] + 1):
            for y in range(lo[1], hi[1] + 1):
                for z in range(lo[2], hi[2] + 1):
                    for n in self.cells.get((x, y, z), ()):
                        if n not in seen:
                            seen.add(n)
                            yield n


def thickness(grid, point, inward, max_mm, skip_face=None):
    """Distance through the material from `point` along unit `inward`, capped at max_mm (None = thicker)."""
    origin = tuple(p + 1e-4 * d for p, d in zip(point, inward))
    end = tuple(p + max_mm * d for p, d in zip(origin, inward))
    best = None
    for n in grid.candidates(origin, end):
        face, a, b, c = grid.tris[n]
        t = ray_triangle(origin, inward, a, b, c)
        if t is not None and t <= max_mm and (best is None or t < best):
            best = t
    return best


def wall_survey(triangles, samples, max_mm):
    """Thickness at each sample, capped at max_mm.

    triangles: [(face_index, a, b, c), ...] in mm; samples: [(face_index, point, outward_unit_normal)].
    Returns per-face {count, thin (under max_mm), thinnest, median of the thin ones, at} and the totals.
    """
    grid = Grid(triangles, max_mm)
    per = {}
    thin_total = 0
    for face, point, normal in samples:
        inward = tuple(-n for n in normal)
        t = thickness(grid, point, inward, max_mm)
        entry = per.setdefault(face, {"count": 0, "thin": [], "at": None, "thinnest": None})
        entry["count"] += 1
        if t is not None:
            entry["thin"].append(t)
            thin_total += 1
            if entry["thinnest"] is None or t < entry["thinnest"]:
                entry["thinnest"], entry["at"] = t, [round(c, 2) for c in point]
    faces = []
    for face, e in per.items():
        if not e["thin"]:
            continue
        values = sorted(e["thin"])
        faces.append({"index": face, "thinnest_mm": round(e["thinnest"], 3),
                      "median_thin_mm": round(values[len(values) // 2], 3),
                      "thin_share": round(len(values) / e["count"], 3), "at_mm": e["at"]})
    faces.sort(key=lambda f: f["thinnest_mm"])
    return {"samples": len(samples), "thin_samples": thin_total, "faces": faces,
            "thinnest_mm": faces[0]["thinnest_mm"] if faces else None}
