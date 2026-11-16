"""Off-screen rendering: a picture of a part or assembly that needs no SolidWorks window (S-03, S-04).

SolidWorks' own screenshot (SaveAs3 to PNG) grabs the graphics buffer of the
ACTIVE window: a document that is open but invisible (a part an assembly
loaded), not active, or behind a minimised SolidWorks gives one solid colour;
the capture fronts the document and changes the user's view on the way, and on
a 47-component assembly it hung for 130 s.

Here the picture is made from the display tessellation SolidWorks already
holds (IFace2 / IComponent2 GetTessTriangles + GetTessNorms): read, projected
and shaded in numpy, written as a PNG with zlib. Nothing is activated, redrawn
or zoomed in SolidWorks, so it also works on a document that is not the
current one.

The rasteriser has no per-pixel Python loop. Small and medium triangles are
SAMPLED: every triangle of a size class gets the same barycentric grid, dense
enough that neighbouring samples are less than a pixel apart, and all samples
of a class go through the z-buffer in one numpy.maximum.at. Only triangles
longer than MAX_GRID pixels are filled one by one over their bounding box
(planar faces: few of them). Shading is smooth (the vertex normals SolidWorks
gives), with outlines where the depth, the body or the surface direction jumps.

A section (clip) is the same picture with the samples in front of a plane
dropped; looking along the plane's normal, a pixel whose nearest surface then
faces away from the viewer is the inside of a solid: it is painted as the cut.
"""

import math
import os
import struct
import time
import zlib

from .. import binding
from ..errors import SolidWorksError
from . import fastpath, journal

try:  # optional, as for run_script's `np`; without it only the window capture works
    import numpy as np
except ImportError:  # pragma: no cover - the uv tool is installed with numpy
    np = None

SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2
SW_BODY_SOLID = 0
SW_COMPONENT_SUPPRESSED = 0

VIEW_DIRS = {"iso": (1.0, 1.0, 1.0), "front": (0.0, 0.0, 1.0), "back": (0.0, 0.0, -1.0),
             "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0), "top": (0.0, 1.0, 0.0),
             "bottom": (0.0, -1.0, 0.0)}
PALETTE = [(0.62, 0.70, 0.82), (0.86, 0.66, 0.42), (0.55, 0.76, 0.60), (0.82, 0.56, 0.58), (0.68, 0.62, 0.84),
           (0.80, 0.78, 0.50), (0.50, 0.74, 0.78), (0.78, 0.60, 0.74)]
BACKGROUND = (255, 255, 255)
CUT_COLOUR = (0.86, 0.36, 0.30)
MAX_GRID = 40          # triangles longer than this many pixels are filled, not sampled
CHUNK_SAMPLES = 3_000_000
LIGHT = (-0.35, 0.45, 0.82)


def available() -> bool:
    return np is not None


def _need_numpy():
    if np is None:
        raise SolidWorksError("The off-screen renderer needs numpy (install the server with `--with numpy`); "
                              "mode='window' captures the SolidWorks window instead.")


# --- reading the tessellation -------------------------------------------------------------


def _raw(obj):
    """`obj` with collection results left as raw dispatches (see fastpath._raw_feature_manager):
    what GetFaces / GetLoops / GetEdges / GetSketchSegments return then costs no call per item."""
    if not hasattr(obj, "_oleobj_"):
        return obj  # not a COM wrapper (a test double): its results are plain already
    cls = fastpath._raw_feature_manager(type(obj))
    try:
        view = cls.__new__(cls)  # around the same dispatch: no QueryInterface
        view.__dict__["_oleobj_"] = obj._oleobj_
        return view
    except Exception:  # noqa: BLE001
        return cls(obj._oleobj_)


def _mesh(tris, norms):
    """(n, 3, 3) vertex and normal arrays in mm from SolidWorks' flat float tuples, or None."""
    if not tris:
        return None
    points = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3) * 1000.0
    normals = None
    if norms and len(norms) == len(tris):
        normals = np.asarray(norms, dtype=np.float64).reshape(-1, 3, 3)
    return points, normals


def _rgb(values, fallback):
    try:
        r, g, b = (float(c) for c in values[:3])
        if 0.0 <= min(r, g, b) and max(r, g, b) <= 1.0:
            return (r, g, b)
    except Exception:  # noqa: BLE001
        pass
    return fallback


def _body_mesh(session, body):
    """One body's faces as a mesh, read face by face (the faces unwrapped: no call per face
    beyond the two that fetch its triangles and normals)."""
    mod = session._mod
    points, normals = [], []
    for raw_face in _raw(body).GetFaces() or ():
        face = fastpath._bind(raw_face, mod.IFace2)
        mesh = _mesh(face.GetTessTriangles(True), face.GetTessNorms())
        if mesh is None:
            continue
        points.append(mesh[0])
        normals.append(mesh[1] if mesh[1] is not None else np.zeros_like(mesh[0]))
    if not points:
        return None
    return np.concatenate(points), np.concatenate(normals)


def _part_groups(session, model, bodies=None) -> list:
    mod = session._mod
    part = binding.wrap(model, mod.IPartDoc)
    found = part.GetBodies2(SW_BODY_SOLID, bodies is None) or ()
    if not isinstance(found, (list, tuple)):
        found = [found]
    wanted = None if bodies is None else {str(b) for b in ([bodies] if isinstance(bodies, str) else bodies)}
    try:
        base = _rgb(part.MaterialPropertyValues, PALETTE[0])
    except Exception:  # noqa: BLE001
        base = PALETTE[0]
    groups = []
    for k, dispatch in enumerate(found):
        body = binding.wrap(dispatch, mod.IBody2)
        name = body.Name
        if wanted is not None and name not in wanted:
            continue
        mesh = _body_mesh(session, body)
        if mesh is None:
            continue
        try:
            colour = _rgb(body.MaterialPropertyValues2, None)
        except Exception:  # noqa: BLE001
            colour = None
        if colour is None:  # bodies of one part: the part's colour, shifted so they tell apart
            colour = base if len(found) == 1 else PALETTE[k % len(PALETTE)]
        groups.append({"name": name, "rgb": colour, "points": mesh[0], "normals": mesh[1]})
    if wanted is not None and len(groups) != len(wanted):
        have = {g["name"] for g in groups}
        raise SolidWorksError(f"No solid body {sorted(wanted - have)} to draw (list_bodies shows them).")
    return groups


def _assembly_groups(session, model) -> list:
    mod = session._mod
    asm = binding.wrap(model, mod.IAssemblyDoc)
    comps = _raw(asm).GetComponents(False) or ()
    groups = []
    meshes = {}  # (file, configuration) -> mesh in the part's own frame: instances share it
    for k, raw_comp in enumerate(comps):
        comp = fastpath._bind(raw_comp, mod.IComponent2)
        try:
            if comp.GetSuppression2() == SW_COMPONENT_SUPPRESSED or comp.IsHidden(False):
                continue
            key = (comp.GetPathName(), comp.ReferencedConfiguration)
            if key not in meshes:
                # IComponent2.GetTessTriangles answers nothing here (SW 2025 SP3): the bodies do
                found = _raw(comp).GetBodies2(SW_BODY_SOLID) or ()
                if not isinstance(found, (list, tuple)):
                    found = [found]
                parts = [m for m in (_body_mesh(session, fastpath._bind(b, mod.IBody2)) for b in found)
                         if m is not None]
                meshes[key] = (np.concatenate([p[0] for p in parts]),
                               np.concatenate([p[1] for p in parts])) if parts else None
            mesh = meshes[key]
        except Exception:  # noqa: BLE001 - a component without display data
            continue
        if mesh is None:
            continue  # a sub-assembly, or a part without solids: its parts come on their own
        points, normals = mesh
        transform = comp.Transform2
        if transform is not None:
            data = binding.wrap(transform, mod.IMathTransform).ArrayData
            rotation = np.asarray(data[:9], dtype=np.float64).reshape(3, 3)  # rows: the part's x, y, z in the assembly
            shift = np.asarray(data[9:12], dtype=np.float64) * 1000.0
            scale = float(data[12]) or 1.0
            points = points @ rotation * scale + shift
            if normals is not None:
                normals = normals @ rotation
        try:
            colour = _rgb(comp.GetModelMaterialPropertyValues(""), None)
        except Exception:  # noqa: BLE001
            colour = None
        groups.append({"name": comp.Name2, "rgb": colour or PALETTE[k % len(PALETTE)], "file": key,
                       "points": points, "normals": normals if normals is not None else np.zeros_like(points)})
    files = sorted({g["file"] for g in groups})
    if len(files) > 1 and len({g["rgb"] for g in groups}) == 1:
        # every part in SolidWorks' default colour: one colour per part file tells them apart
        for g in groups:
            g["rgb"] = PALETTE[files.index(g["file"]) % len(PALETTE)]
    return groups


def collect(session, model, bodies=None) -> list:
    """The document's visible solids as triangle groups [{name, rgb, points, normals}] in mm."""
    _need_numpy()
    kind = int(model.GetType())
    if kind == SW_DOC_PART:
        groups = _part_groups(session, model, bodies)
    elif kind == SW_DOC_ASSEMBLY:
        groups = _assembly_groups(session, model)
    else:
        raise SolidWorksError("Only a part or an assembly can be rendered.")
    if not groups:
        raise SolidWorksError(
            f"'{model.GetTitle()}' has nothing to draw: no visible solid body with display data. A part whose "
            "last features delete or split away every body has none; in an assembly check that components "
            "are not all hidden or suppressed.")
    return groups


# --- the view -------------------------------------------------------------------------------


def view_axes(from_dir):
    """Screen x, y, z (z towards the viewer) in model axes for a view from from_dir, with the
    model's +y up (looking along y: -z or +z up, as SolidWorks' top and bottom views)."""
    if len(from_dir) != 3 or not math.hypot(*from_dir):
        raise SolidWorksError(f"from_dir needs a direction [x, y, z] that is not zero (got {from_dir}).")
    length = math.hypot(*from_dir)
    z = [c / length for c in from_dir]
    up = [0.0, 1.0, 0.0] if abs(z[1]) < 1 - 1e-9 else [0.0, 0.0, -1.0 if z[1] > 0 else 1.0]
    along = sum(u * c for u, c in zip(up, z))
    y = [u - along * c for u, c in zip(up, z)]
    norm = math.hypot(*y)
    y = [c / norm for c in y]
    x = [y[1] * z[2] - y[2] * z[1], y[2] * z[0] - y[0] * z[2], y[0] * z[1] - y[1] * z[0]]
    return x, y, z


def _grid(n: int):
    """Barycentric weights of an n-subdivided triangle: ((n + 1)(n + 2) / 2, 3)."""
    rows = []
    for i in range(n + 1):
        for j in range(n + 1 - i):
            rows.append((i / n, j / n, (n - i - j) / n))
    return np.asarray(rows, dtype=np.float64)


_GRID_STEPS = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 40)


def rasterise(points, normals, colours, group_ids, axes, width: int, height: int, box=None,
              clip=None, margin: float = 0.04):
    """Draw triangles (n, 3, 3 mm) into an (height, width, 3) uint8 image.

    axes: screen (x, y, z) in model space. box: (low, high) mm corners of the region to fill
    the image; default everything. clip: (point, normal) - what lies on the normal's side of
    the plane is cut away, and the cut is painted where a solid shows its inside.
    Returns (image, info)."""
    _need_numpy()
    basis = np.asarray(axes, dtype=np.float64)                 # rows: screen x, y, z
    screen = points @ basis.T                                  # (n, 3, 3) in mm along the screen axes
    if box is not None:
        corners = np.asarray([[box[i][k] for k, i in enumerate(pick)]
                              for pick in np.ndindex(2, 2, 2)], dtype=np.float64) @ basis.T
        low, high = corners.min(0), corners.max(0)
    else:
        flat = screen.reshape(-1, 3)
        if clip is not None:
            # what is cut away does not count: its vertices are dropped onto the plane, which
            # still contains every cut edge
            model_flat = points.reshape(-1, 3)
            plane_n = np.asarray(clip[1], dtype=np.float64)
            ahead = np.maximum((model_flat - np.asarray(clip[0])) @ plane_n, 0.0)
            flat = (model_flat - ahead[:, None] * plane_n) @ basis.T
        low, high = flat.min(0), flat.max(0)
    span = np.maximum(high[:2] - low[:2], 1e-9)
    scale = min(width * (1 - 2 * margin) / span[0], height * (1 - 2 * margin) / span[1])
    centre = (low[:2] + high[:2]) / 2
    px = (screen[..., 0] - centre[0]) * scale + width / 2
    py = height / 2 - (screen[..., 1] - centre[1]) * scale
    pz = screen[..., 2] * scale                                # depth in pixels too: one threshold for edges
    P = np.stack([px, py, pz], axis=-1)                        # (n, 3, 3)

    # geometric normal of each triangle in screen space (y is up there)
    e1, e2 = screen[:, 1] - screen[:, 0], screen[:, 2] - screen[:, 0]
    face_n = np.cross(e1, e2)
    area2 = np.linalg.norm(face_n, axis=1)
    ok = area2 > 1e-12
    vertex_n = normals @ basis.T if normals is not None else None
    if vertex_n is not None:
        lengths = np.linalg.norm(vertex_n, axis=2, keepdims=True)
        flat_n = (face_n / np.maximum(area2, 1e-300)[:, None])[:, None, :]
        vertex_n = np.where(lengths > 1e-9, vertex_n / np.maximum(lengths, 1e-300), flat_n)
        # SolidWorks' vertex normals point out of the body: orient the triangle's own normal by them
        outward = np.einsum("nc,nc->n", face_n, vertex_n.mean(1)) >= 0
        face_n = np.where(outward[:, None], face_n, -face_n)
    else:
        vertex_n = np.repeat((face_n / np.maximum(area2, 1e-300)[:, None])[:, None, :], 3, axis=1)

    # on screen: keep what touches the image
    lo, hi = P.min(1), P.max(1)
    ok &= (hi[:, 0] >= 0) & (lo[:, 0] <= width - 1) & (hi[:, 1] >= 0) & (lo[:, 1] <= height - 1)
    if clip is None:
        ok &= face_n[:, 2] > 0                                  # closed bodies: back faces are never seen
    index = np.nonzero(ok)[0]

    zbuf = np.full(width * height, -np.inf)
    owner = np.full(width * height, -1, dtype=np.int64)
    nbuf = np.zeros((width * height, 3))
    clip_point = np.asarray(clip[0], dtype=np.float64) if clip is not None else None
    clip_normal = np.asarray(clip[1], dtype=np.float64) if clip is not None else None

    edge = np.maximum.reduce([np.linalg.norm(P[index][:, a, :2] - P[index][:, b, :2], axis=1)
                              for a, b in ((0, 1), (1, 2), (2, 0))]) if len(index) else np.zeros(0)
    steps = np.asarray(_GRID_STEPS)
    need = np.ceil(edge * 1.45).astype(np.int64)               # samples closer than 0.7 px along the longest edge
    cls = np.searchsorted(steps, need)                         # the class whose grid is at least that fine
    samples = 0
    for c, n in enumerate(_GRID_STEPS):
        members = index[cls == c]
        if not len(members):
            continue
        weights = _grid(n)
        per = max(1, CHUNK_SAMPLES // len(weights))
        for start in range(0, len(members), per):
            chunk = members[start:start + per]
            pts = np.einsum("mk,nkc->nmc", weights, P[chunk]).reshape(-1, 3)
            tri = np.repeat(chunk, len(weights))
            keep = np.ones(len(pts), dtype=bool)
            if clip is not None:
                model_pts = np.einsum("mk,nkc->nmc", weights, points[chunk]).reshape(-1, 3)
                keep &= (model_pts - clip_point) @ clip_normal <= 1e-9
            ix = np.rint(pts[:, 0]).astype(np.int64)
            iy = np.rint(pts[:, 1]).astype(np.int64)
            keep &= (ix >= 0) & (ix < width) & (iy >= 0) & (iy < height)
            if not keep.any():
                continue
            pix = iy[keep] * width + ix[keep]
            depth = pts[keep, 2]
            tri = tri[keep]
            samples += len(pix)
            np.maximum.at(zbuf, pix, depth)
            win = depth >= zbuf[pix]
            owner[pix[win]] = tri[win]
            nrm = np.einsum("mk,nkc->nmc", weights, vertex_n[chunk]).reshape(-1, 3)[keep][win]
            nbuf[pix[win]] = nrm

    big = index[cls >= len(_GRID_STEPS)]
    for t in big:                                              # long triangles: fill the bounding box
        (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = P[t]
        xa, xb = max(0, int(math.floor(min(x0, x1, x2)))), min(width - 1, int(math.ceil(max(x0, x1, x2))))
        ya, yb = max(0, int(math.floor(min(y0, y1, y2)))), min(height - 1, int(math.ceil(max(y0, y1, y2))))
        if xa > xb or ya > yb:
            continue
        det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(det) < 1e-12:
            continue
        xs, ys = np.meshgrid(np.arange(xa, xb + 1), np.arange(ya, yb + 1))
        w0 = ((y1 - y2) * (xs - x2) + (x2 - x1) * (ys - y2)) / det
        w1 = ((y2 - y0) * (xs - x2) + (x0 - x2) * (ys - y2)) / det
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if clip is not None:
            a, b, c = points[t]
            model_pts = w0[..., None] * a + w1[..., None] * b + w2[..., None] * c
            inside &= (model_pts - clip_point) @ clip_normal <= 1e-9
        if not inside.any():
            continue
        depth = w0 * z0 + w1 * z1 + w2 * z2
        pix = (ys * width + xs)[inside]
        depth = depth[inside]
        win = depth > zbuf[pix]
        if not win.any():
            continue
        pix = pix[win]
        zbuf[pix] = depth[win]
        owner[pix] = t
        vn = vertex_n[t]
        nbuf[pix] = (w0[inside][win][:, None] * vn[0] + w1[inside][win][:, None] * vn[1]
                     + w2[inside][win][:, None] * vn[2])

    drawn = owner >= 0
    image = np.empty((width * height, 3), dtype=np.float64)
    image[:] = np.asarray(BACKGROUND, dtype=np.float64) / 255.0
    if drawn.any():
        normal = nbuf[drawn]
        normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
        light = np.asarray(LIGHT) / np.linalg.norm(LIGHT)
        facing = face_n[owner[drawn], 2] > 0
        shade = 0.38 + 0.62 * np.abs(normal @ light)
        colour = colours[owner[drawn]] * shade[:, None]
        if clip is not None:                                   # the inside of a solid, seen through the cut
            colour[~facing] = 0.6 * np.asarray(CUT_COLOUR) + 0.4 * colours[owner[drawn]][~facing]
        image[drawn] = colour

    # outlines: where the body, the depth or the surface direction jumps between neighbours
    z2d = zbuf.reshape(height, width)
    g2d = np.where(drawn, group_ids[np.maximum(owner, 0)], -1).reshape(height, width)
    n2d = nbuf.reshape(height, width, 3)
    cut2d = np.zeros((height, width), dtype=bool)
    if clip is not None and drawn.any():
        inside_flag = np.zeros(width * height, dtype=bool)
        inside_flag[drawn] = ~facing
        cut2d = inside_flag.reshape(height, width)
    line = np.zeros((height, width), dtype=bool)
    for dy, dx in ((0, 1), (1, 0)):
        a = (slice(0, height - dy), slice(0, width - dx))
        b = (slice(dy, height), slice(dx, width))
        both = (g2d[a] >= 0) & (g2d[b] >= 0)
        in_cut = cut2d[a] & cut2d[b]  # the cut is one flat face: what shows behind it draws no line
        jump = g2d[a] != g2d[b]
        with np.errstate(invalid="ignore"):
            jump |= both & (np.abs(z2d[a] - z2d[b]) > 6.0) & ~in_cut
        jump |= both & (np.einsum("ijc,ijc->ij", n2d[a], n2d[b]) < 0.80) & ~in_cut
        jump |= cut2d[a] != cut2d[b]
        line[a] |= jump
    image = image.reshape(height, width, 3)
    image[line] *= 0.25
    out = np.clip(np.rint(image * 255.0), 0, 255).astype(np.uint8)
    info = {"triangles": int(len(points)), "drawn": int(len(index)), "samples": int(samples),
            "filled": int(len(big)), "covered_pct": round(100.0 * float(drawn.mean()), 1),
            "mm_per_px": round(1.0 / scale, 5)}
    return out, info


def png_bytes(image) -> bytes:
    """An (h, w, 3) uint8 array as a PNG (zlib only)."""
    height, width, _ = image.shape
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), image.reshape(height, width * 3)], axis=1).tobytes()

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def downsample(image, factor: int):
    """Average factor x factor blocks (anti-aliasing after rendering larger)."""
    if factor <= 1:
        return image
    height, width, _ = image.shape
    height, width = height - height % factor, width - width % factor
    blocks = image[:height, :width].reshape(height // factor, factor, width // factor, factor, 3).astype(np.float64)
    return np.rint(blocks.mean(axis=(1, 3))).astype(np.uint8)


def draw(groups, from_dir, width: int = 1200, height: int = 800, zoom_mm=None, clip=None):
    """Triangle groups -> (image, info). Renders at twice the size and averages down while the
    triangle count allows it."""
    points = np.concatenate([g["points"] for g in groups])
    normals = np.concatenate([g["normals"] for g in groups])
    colours = np.concatenate([np.tile(np.asarray(g["rgb"], dtype=np.float64), (len(g["points"]), 1)) for g in groups])
    group_ids = np.concatenate([np.full(len(g["points"]), k, dtype=np.int64) for k, g in enumerate(groups)])
    factor = 2 if len(points) <= 400_000 else 1
    axes = view_axes(from_dir)
    image, info = rasterise(points, normals, colours, group_ids, axes, width * factor, height * factor,
                            box=zoom_mm, clip=clip)
    info["mm_per_px"] = round(info["mm_per_px"] * factor, 5)
    return downsample(image, factor), info


# --- the tool -------------------------------------------------------------------------------


def _document(session, title):
    """The current document, or an open one by title - without activating it."""
    if title is None:
        if session._model is None:
            raise SolidWorksError("No current document. Open or create one, or name an open document.")
        return session._model
    from . import geom
    session._ensure()  # the first call of a fresh server: attach before listing documents
    return geom._doc_by_title(session, title)


def default_path(title: str, tag: str = "view") -> str:
    folder = os.path.join(journal.home(), "shots")
    os.makedirs(folder, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in os.path.splitext(title)[0])[:60]
    return os.path.join(folder, f"{safe}-{tag}-{time.strftime('%H%M%S')}.png")


def render_view(session, path: str | None = None, view: str = "iso", zoom_mm=None, from_dir=None,
                width: int = 1200, height: int = 800, document: str | None = None, bodies=None) -> dict:
    """Render a part or assembly to a PNG without touching the SolidWorks window."""
    _need_numpy()
    t0 = time.perf_counter()
    key = str(view).lower()
    if from_dir is None and key not in VIEW_DIRS:
        raise SolidWorksError(f"Unknown view '{view}'. Use {', '.join(VIEW_DIRS)}, or from_dir=[x, y, z].")
    if zoom_mm is not None and (len(zoom_mm) != 2 or any(len(corner) != 3 for corner in zoom_mm)):
        raise SolidWorksError(f"zoom_mm needs two corners [[x1, y1, z1], [x2, y2, z2]] (got {zoom_mm}).")
    width, height = max(64, min(int(width), 4000)), max(64, min(int(height), 4000))
    model = _document(session, document)
    groups = collect(session, model, bodies)
    t_read = time.perf_counter() - t0
    image, info = draw(groups, from_dir if from_dir is not None else VIEW_DIRS[key], width, height, zoom_mm)
    out = os.path.abspath(path) if path else default_path(model.GetTitle())
    if not out.lower().endswith(".png"):
        raise SolidWorksError("The off-screen renderer writes PNG: give a path ending in .png.")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    data = png_bytes(image)
    with open(out, "wb") as fh:
        fh.write(data)
    if info["covered_pct"] <= 0:
        raise SolidWorksError("The render came out empty: zoom_mm lies off the model, or nothing is visible.")
    return {"ok": True, "path": out, "format": "png", "bytes": len(data), "mode": "render",
            "document": model.GetTitle(), "view": "from_dir" if from_dir is not None else key,
            "size_px": [int(image.shape[1]), int(image.shape[0])], "groups": len(groups), **info,
            "read_s": round(t_read, 2), "elapsed_s": round(time.perf_counter() - t0, 2)}
