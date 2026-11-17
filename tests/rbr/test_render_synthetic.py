"""render: the view axes, the rasteriser and the PNG writer on a synthetic mesh (a cube as triangles)."""

import math
import os
import re
import struct
import types
import zlib

import pytest

np = pytest.importorskip("numpy")  # the renderer is numpy; without it the server only captures the window

from solidworks_mcp.errors import SolidWorksError  # noqa: E402
from solidworks_mcp.rbr import render  # noqa: E402

COLOUR = (0.5, 0.6, 0.7)
WHITE = (255, 255, 255)


def cube(size=10.0, origin=(0.0, 0.0, 0.0)):
    """(points, normals) of a cube: 12 triangles wound counter-clockwise seen from outside,
    every vertex with its face's outward normal - what SolidWorks' tessellation gives."""
    s = size
    faces = [((0, 0, s), (s, 0, s), (s, s, s), (0, s, s), (0, 0, 1)),
             ((0, 0, 0), (0, s, 0), (s, s, 0), (s, 0, 0), (0, 0, -1)),
             ((s, 0, 0), (s, s, 0), (s, s, s), (s, 0, s), (1, 0, 0)),
             ((0, 0, 0), (0, 0, s), (0, s, s), (0, s, 0), (-1, 0, 0)),
             ((0, s, 0), (0, s, s), (s, s, s), (s, s, 0), (0, 1, 0)),
             ((0, 0, 0), (s, 0, 0), (s, 0, s), (0, 0, s), (0, -1, 0))]
    points, normals = [], []
    for a, b, c, d, normal in faces:
        points += [(a, b, c), (a, c, d)]
        normals += [(normal,) * 3, (normal,) * 3]
    moved = np.asarray(points, dtype=np.float64) + np.asarray(origin, dtype=np.float64)
    return moved, np.asarray(normals, dtype=np.float64)


def shoot(view="front", width=100, height=100, points=None, normals="cube", colour=COLOUR, group_ids=None, **kw):
    """rasterise() of the cube (or the given triangles) seen from a named view or a direction."""
    cube_points, cube_normals = cube()
    points = cube_points if points is None else points
    normals = (cube_normals if len(points) == 12 else None) if isinstance(normals, str) else normals
    colours = np.tile(np.asarray(colour, dtype=np.float64), (len(points), 1))
    group_ids = np.zeros(len(points), dtype=np.int64) if group_ids is None else group_ids
    axes = render.view_axes(render.VIEW_DIRS[view] if isinstance(view, str) else view)
    return render.rasterise(points, normals, colours, group_ids, axes, width, height, **kw)


def shaded(colour, normal_towards_viewer=(0.0, 0.0, 1.0)):
    """The pixel colour of a face with that screen-space normal, as the module shades it."""
    light = np.asarray(render.LIGHT) / np.linalg.norm(render.LIGHT)
    shade = 0.38 + 0.62 * abs(float(np.dot(normal_towards_viewer, light)))
    return tuple(int(v) for v in np.clip(np.rint(np.asarray(colour) * shade * 255.0), 0, 255))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


# --- view_axes ------------------------------------------------------------------------------

@pytest.mark.parametrize("from_dir", [*render.VIEW_DIRS.values(), (1, -2, 3), (-0.2, 0.9, 0.1), (0, 5, 1e-6),
                                      (3, 0, 0)])
def test_view_axes_are_orthonormal_and_right_handed(from_dir):
    x, y, z = render.view_axes(from_dir)
    for axis in (x, y, z):
        assert math.hypot(*axis) == pytest.approx(1.0)
    assert sum(a * b for a, b in zip(x, y)) == pytest.approx(0.0, abs=1e-12)
    assert sum(a * b for a, b in zip(y, z)) == pytest.approx(0.0, abs=1e-12)
    assert cross(x, y) == pytest.approx(z, abs=1e-12)
    length = math.hypot(*from_dir)
    assert z == pytest.approx([c / length for c in from_dir])  # z points at the viewer


def test_front_and_back_views_keep_y_up():
    assert render.view_axes(render.VIEW_DIRS["front"]) == ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])
    x, y, _ = render.view_axes(render.VIEW_DIRS["back"])
    assert x == pytest.approx([-1.0, 0.0, 0.0]) and y == pytest.approx([0.0, 1.0, 0.0])


def test_side_views_keep_y_up():
    x, y, _ = render.view_axes(render.VIEW_DIRS["right"])
    assert x == pytest.approx([0.0, 0.0, -1.0]) and y == pytest.approx([0.0, 1.0, 0.0])
    x, y, _ = render.view_axes(render.VIEW_DIRS["left"])
    assert x == pytest.approx([0.0, 0.0, 1.0]) and y == pytest.approx([0.0, 1.0, 0.0])


def test_top_and_bottom_views_are_solidworks_own():
    x, y, _ = render.view_axes(render.VIEW_DIRS["top"])        # x to the right, -z up
    assert x == pytest.approx([1.0, 0.0, 0.0]) and y == pytest.approx([0.0, 0.0, -1.0])
    x, y, _ = render.view_axes(render.VIEW_DIRS["bottom"])     # x to the right, +z up
    assert x == pytest.approx([1.0, 0.0, 0.0]) and y == pytest.approx([0.0, 0.0, 1.0])


def test_iso_view_has_the_models_y_pointing_up_the_screen():
    x, y, z = render.view_axes(render.VIEW_DIRS["iso"])
    assert y[1] > 0.8 and x[1] == pytest.approx(0.0)
    assert z == pytest.approx([1 / math.sqrt(3)] * 3)


def test_view_names():
    assert set(render.VIEW_DIRS) == {"iso", "front", "back", "right", "left", "top", "bottom"}
    for name, opposite in (("front", "back"), ("right", "left"), ("top", "bottom")):
        assert render.VIEW_DIRS[name] == tuple(-c for c in render.VIEW_DIRS[opposite])


@pytest.mark.parametrize("from_dir", [(0, 0, 0), (1, 0), (1, 0, 0, 0), ()])
def test_view_axes_refuse_a_zero_or_misshapen_direction(from_dir):
    with pytest.raises(SolidWorksError, match="from_dir needs a direction"):
        render.view_axes(from_dir)


# --- small helpers --------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 2, 3, 8, 40])
def test_grid_is_the_barycentric_lattice_of_a_subdivided_triangle(n):
    weights = render._grid(n)
    assert weights.shape == ((n + 1) * (n + 2) // 2, 3)
    assert np.allclose(weights.sum(axis=1), 1.0) and weights.min() >= 0.0
    rows = {tuple(row) for row in weights.tolist()}
    assert {(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)} <= rows and len(rows) == len(weights)


def test_mesh_turns_flat_metres_into_triangles_in_mm():
    tris = (0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02, 0.0,
            0.0, 0.0, 0.005, 0.01, 0.0, 0.005, 0.0, 0.02, 0.005)
    norms = (0.0, 0.0, 1.0) * 6
    points, normals = render._mesh(tris, norms)
    assert points.shape == (2, 3, 3) and normals.shape == (2, 3, 3)
    assert points[0].tolist() == [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 20.0, 0.0]]
    assert points[1][:, 2].tolist() == [5.0, 5.0, 5.0]
    assert np.all(normals == (0.0, 0.0, 1.0))  # directions: not scaled


def test_mesh_without_triangles_or_with_mismatched_normals():
    assert render._mesh(None, None) is None and render._mesh((), ()) is None
    tris = (0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02, 0.0)
    points, normals = render._mesh(tris, None)
    assert points.shape == (1, 3, 3) and normals is None
    assert render._mesh(tris, (0.0, 0.0, 1.0))[1] is None  # one normal for three vertices: unusable


def test_rgb_takes_a_colour_in_zero_to_one_or_falls_back():
    fallback = (0.1, 0.2, 0.3)
    assert render._rgb((0.2, 0.4, 0.6, 1.0, 1.0, 0.5, 0.3, 0.0, 0.0), fallback) == (0.2, 0.4, 0.6)
    assert render._rgb([1, 0, 0], fallback) == (1.0, 0.0, 0.0)
    for bad in ((-1.0, -1.0, -1.0), (0.5, 0.5, 1.5), (0.5, 0.5), None, "red", ()):
        assert render._rgb(bad, fallback) == fallback
    assert render._rgb(None, None) is None


def test_raw_leaves_a_plain_object_alone():
    plain = types.SimpleNamespace(GetFaces=lambda: ())
    assert render._raw(plain) is plain


def test_numpy_is_reported_available():
    assert render.available() is True
    render._need_numpy()  # no refusal


# --- rasterise: the picture -----------------------------------------------------------------

@pytest.mark.parametrize("width, height", [(100, 100), (120, 80), (64, 96), (33, 17)])
def test_image_has_the_requested_size_and_is_not_blank(width, height):
    image, info = shoot("iso", width, height)
    assert image.shape == (height, width, 3) and image.dtype == np.uint8
    assert (image != 255).any()
    assert tuple(image[0, 0]) == WHITE and tuple(image[-1, -1]) == WHITE  # the margin stays background
    assert info["covered_pct"] > 20


@pytest.mark.parametrize("view", sorted(render.VIEW_DIRS))
def test_every_named_view_draws_the_cube(view):
    image, info = shoot(view)
    assert info["triangles"] == 12
    assert info["drawn"] == (6 if view == "iso" else 2)  # back faces are never seen
    assert info["covered_pct"] > 50 and (image != 255).any()


def test_front_view_fills_the_image_up_to_the_margin():
    image, info = shoot("front", 100, 100)
    assert info["covered_pct"] == pytest.approx(100 * 0.92 ** 2, abs=3.0)
    assert info["mm_per_px"] == pytest.approx(10.0 / 92.0, rel=1e-3)
    assert tuple(image[50, 50]) == shaded(COLOUR)
    assert tuple(image[2, 50]) == WHITE and tuple(image[50, 2]) == WHITE
    assert tuple(image[50, 97]) == WHITE and tuple(image[97, 50]) == WHITE


def test_wide_image_fits_the_model_by_its_height():
    image, info = shoot("front", 200, 100)
    assert info["mm_per_px"] == pytest.approx(10.0 / 92.0, rel=1e-3)
    assert tuple(image[50, 100]) == shaded(COLOUR)
    assert tuple(image[50, 20]) == WHITE and tuple(image[50, 180]) == WHITE  # space left and right
    drawn = np.nonzero((image != 255).any(axis=2))
    assert 200 - drawn[1].max() - 1 == pytest.approx(drawn[1].min(), abs=2)  # centred


def test_face_is_shaded_by_its_direction_to_the_light():
    image, _ = shoot("iso", 200, 200)
    axes = np.asarray(render.view_axes(render.VIEW_DIRS["iso"]))
    seen = {tuple(pixel) for pixel in image.reshape(-1, 3).tolist()}
    for normal in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):  # the three faces towards the viewer
        assert shaded(COLOUR, axes @ np.asarray(normal, dtype=np.float64)) in seen
    top, right = shaded(COLOUR, axes @ np.asarray((0.0, 1.0, 0.0))), shaded(COLOUR, axes @ np.asarray((1.0, 0.0, 0.0)))
    assert top != right


def test_silhouette_is_outlined():
    image, _ = shoot("front", 100, 100)
    face = shaded(COLOUR)
    darker = (image.astype(int).sum(axis=2) < sum(face) * 0.5)
    assert darker.any()
    rows, cols = np.nonzero(darker)
    assert rows.min() <= 5 and rows.max() >= 94 and cols.min() <= 5 and cols.max() >= 94  # all the way round
    assert not darker[20:80, 20:80].any()  # and nothing inside the flat face


def test_small_triangles_are_sampled_and_long_ones_filled_to_the_same_picture():
    small, sampled = shoot("front", 16, 16)     # the cube's diagonal is 21 px long
    large, filled = shoot("front", 200, 200)    # ... and 260 px here
    assert sampled["filled"] == 0 and sampled["samples"] > 0
    assert filled["filled"] == 2 and filled["samples"] == 0
    assert sampled["covered_pct"] == pytest.approx(filled["covered_pct"], abs=8.0)
    assert tuple(small[8, 8]) == tuple(large[100, 100]) == shaded(COLOUR)
    assert tuple(small[0, 0]) == tuple(large[0, 0]) == WHITE


def test_winding_does_not_matter_when_vertex_normals_are_given():
    points, normals = cube()
    flipped = points[:, ::-1, :].copy()  # every triangle wound the other way
    image, info = shoot("iso", points=flipped, normals=normals[:, ::-1, :].copy())
    reference, expected = shoot("iso")
    assert info["drawn"] == 6 and info["covered_pct"] == expected["covered_pct"]
    # the same picture; only pixels exactly on an edge two faces share may go to the other face
    assert (image != reference).any(axis=2).mean() < 0.01


def test_without_vertex_normals_the_winding_tells_front_from_back():
    points, _ = cube()
    image, info = shoot("front", points=points, normals=None)
    assert info["drawn"] == 2 and tuple(image[50, 50]) == shaded(COLOUR)
    _, inside_out = shoot("front", points=points[:, ::-1, :].copy(), normals=None)
    assert inside_out["drawn"] == 2  # now the far face is the one that 'faces' the viewer


def test_nearer_surface_hides_the_farther_one():
    near, far = cube(10.0, (0, 0, 20)), cube(10.0, (0, 0, 0))
    points = np.concatenate([far[0], near[0]])
    normals = np.concatenate([far[1], near[1]])
    colours = np.concatenate([np.tile((1.0, 0.0, 0.0), (12, 1)), np.tile((0.0, 0.0, 1.0), (12, 1))])
    groups = np.repeat([0, 1], 12)
    axes = render.view_axes(render.VIEW_DIRS["front"])
    image, _ = render.rasterise(points, normals, colours, groups, axes, 100, 100)
    assert tuple(image[50, 50]) == shaded((0.0, 0.0, 1.0))  # the near, blue cube
    image, _ = render.rasterise(points, normals, colours, groups, render.view_axes(render.VIEW_DIRS["back"]), 100, 100)
    assert tuple(image[50, 50]) == shaded((1.0, 0.0, 0.0))  # from behind the red one is in front


def test_bodies_are_told_apart_by_a_line_between_them():
    left, right = cube(10.0, (0, 0, 0)), cube(10.0, (10, 0, 0))  # two cubes face to face: one flat front
    points, normals = np.concatenate([left[0], right[0]]), np.concatenate([left[1], right[1]])
    one_body, _ = shoot("front", 200, 100, points=points, normals=normals)
    two_bodies, _ = shoot("front", 200, 100, points=points, normals=normals, group_ids=np.repeat([0, 1], 12))
    face = shaded(COLOUR)
    assert all(tuple(pixel) == face for pixel in one_body[50, 60:140].tolist())  # nothing at the seam
    seam = [tuple(pixel) for pixel in two_bodies[50, 95:106].tolist()]
    assert any(sum(pixel) < sum(face) * 0.5 for pixel in seam)
    assert tuple(two_bodies[50, 60]) == face and tuple(two_bodies[50, 140]) == face


# --- rasterise: zoom and clip ---------------------------------------------------------------

def test_zoom_box_fills_the_image_with_that_region():
    _, whole = shoot("front", 100, 100)
    image, zoomed = shoot("front", 100, 100, box=((0, 0, 0), (5, 5, 10)))
    assert zoomed["mm_per_px"] == pytest.approx(whole["mm_per_px"] / 2, rel=1e-3)
    assert tuple(image[50, 50]) == shaded(COLOUR)
    assert tuple(image[50, 99]) == shaded(COLOUR) and tuple(image[0, 50]) == shaded(COLOUR)  # the cube runs on
    assert zoomed["covered_pct"] > whole["covered_pct"]


def test_zoom_box_corners_may_come_in_any_order():
    first, _ = shoot("front", 80, 80, box=((0, 0, 0), (5, 5, 10)))
    second, _ = shoot("front", 80, 80, box=((5, 5, 10), (0, 0, 0)))
    assert np.array_equal(first, second)


def test_zoom_off_the_model_gives_an_empty_picture():
    image, info = shoot("front", 60, 40, box=((100, 100, 0), (110, 110, 10)))
    assert image.shape == (40, 60, 3) and (image == 255).all()
    assert info["covered_pct"] == 0 and info["drawn"] == 0


def test_clip_cuts_away_the_normals_side_and_paints_the_cut():
    image, info = shoot("front", 100, 100, clip=((0, 0, 5), (0, 0, 1)))
    cut = tuple(int(v) for v in np.rint((0.6 * np.asarray(render.CUT_COLOUR) + 0.4 * np.asarray(COLOUR)) * 255.0))
    assert tuple(image[50, 50]) == cut  # the inside of the far wall, seen through the cut
    assert tuple(image[20, 30]) == cut and tuple(image[75, 70]) == cut
    assert tuple(image[1, 1]) == WHITE
    assert info["covered_pct"] == pytest.approx(100 * 0.92 ** 2, abs=4.0)


def test_clip_plane_in_front_of_the_model_changes_nothing_inside():
    image, _ = shoot("front", 100, 100, clip=((0, 0, 20), (0, 0, 1)))
    assert tuple(image[50, 50]) == shaded(COLOUR)


def test_clip_plane_behind_the_model_leaves_nothing():
    image, info = shoot("front", 100, 100, clip=((0, 0, -5), (0, 0, 1)))
    assert (image == 255).all() and info["covered_pct"] == 0


def test_clipped_half_seen_from_the_side_is_framed_without_what_was_cut_away():
    # cut at x = 5 with the normal +x, looked at from +z: the half x > 5 is gone
    image, half = shoot("front", 100, 100, clip=((5, 0, 0), (1, 0, 0)))
    _, whole = shoot("front", 100, 100)
    assert half["mm_per_px"] == pytest.approx(whole["mm_per_px"], rel=1e-3)  # 5 x 10 mm: the height still decides
    assert half["covered_pct"] == pytest.approx(whole["covered_pct"] / 2, abs=3.0)
    drawn = np.nonzero((image != 255).any(axis=2))
    assert drawn[1].min() == pytest.approx(27, abs=2) and drawn[1].max() == pytest.approx(73, abs=2)  # centred
    assert tuple(image[50, 50]) == shaded(COLOUR)  # the front face of the half that is left
    assert tuple(image[50, 15]) == WHITE and tuple(image[50, 85]) == WHITE


# --- png_bytes, downsample ------------------------------------------------------------------

def read_png(data):
    """(width, height, rows) of a PNG as png_bytes writes it: 8-bit RGB, no filter, not interlaced."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, at = [], 8
    while at < len(data):
        (length,) = struct.unpack(">I", data[at:at + 4])
        kind, body = data[at + 4:at + 8], data[at + 8:at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length:at + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks.append((kind, body))
        at += 12 + length
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, colour_type, compression, filtering, interlace) == (8, 2, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    stride = 1 + 3 * width
    assert len(raw) == height * stride and all(raw[r * stride] == 0 for r in range(height))
    return width, height, [raw[r * stride + 1:(r + 1) * stride] for r in range(height)]


def test_png_holds_the_image_row_by_row():
    image = np.arange(4 * 5 * 3, dtype=np.uint8).reshape(4, 5, 3)
    width, height, rows = read_png(render.png_bytes(image))
    assert (width, height) == (5, 4)
    assert [list(row) for row in rows] == image.reshape(4, 15).tolist()


def test_png_of_a_rendered_picture_round_trips():
    image, _ = shoot("iso", 48, 32)
    width, height, rows = read_png(render.png_bytes(image))
    assert (width, height) == (48, 32)
    assert b"".join(rows) == image.tobytes()


def test_downsample_averages_blocks():
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:2, :2] = 255                      # one block white
    image[2:, 2:] = (10, 20, 30)             # one block a flat colour
    image[0, 2] = (255, 0, 0)                # one pixel of four red
    small = render.downsample(image, 2)
    assert small.shape == (2, 2, 3) and small.dtype == np.uint8
    assert small[0, 0].tolist() == [255, 255, 255] and small[1, 1].tolist() == [10, 20, 30]
    assert small[0, 1].tolist() == [64, 0, 0] and small[1, 0].tolist() == [0, 0, 0]


def test_downsample_by_one_is_the_image_itself_and_odd_edges_are_dropped():
    image = np.full((5, 7, 3), 200, dtype=np.uint8)
    assert render.downsample(image, 1) is image
    assert render.downsample(image, 2).shape == (2, 3, 3)
    assert render.downsample(image, 3).shape == (1, 2, 3)
    assert (render.downsample(image, 2) == 200).all()


# --- draw -----------------------------------------------------------------------------------

def group(name, rgb, origin=(0.0, 0.0, 0.0)):
    points, normals = cube(10.0, origin)
    return {"name": name, "rgb": rgb, "points": points, "normals": normals}


def test_draw_gives_the_requested_size_after_supersampling():
    image, info = render.draw([group("A", COLOUR)], render.VIEW_DIRS["front"], 90, 60)
    assert image.shape == (60, 90, 3) and image.dtype == np.uint8
    assert info["mm_per_px"] == pytest.approx(10.0 / (0.92 * 60), rel=1e-3)  # in pixels of the picture returned
    assert info["triangles"] == 12 and info["drawn"] == 2
    assert tuple(image[30, 45]) == shaded(COLOUR) and tuple(image[0, 0]) == WHITE


def test_draw_paints_each_group_in_its_own_colour():
    red, blue = (0.9, 0.2, 0.2), (0.2, 0.2, 0.9)
    image, info = render.draw([group("A", red), group("B", blue, (10, 0, 0))], render.VIEW_DIRS["front"], 200, 100)
    assert info["triangles"] == 24
    assert tuple(image[50, 60]) == shaded(red) and tuple(image[50, 140]) == shaded(blue)


def test_draw_with_zoom_and_clip():
    zoomed, info = render.draw([group("A", COLOUR)], render.VIEW_DIRS["front"], 80, 80, zoom_mm=((0, 0, 0), (5, 5, 10)))
    assert info["mm_per_px"] == pytest.approx(5.0 / (0.92 * 80), rel=1e-3)
    cut, _ = render.draw([group("A", COLOUR)], (0, 0, 1), 80, 80, clip=((0, 0, 5), (0, 0, 1)))
    expected = tuple(int(v) for v in np.rint((0.6 * np.asarray(render.CUT_COLOUR) + 0.4 * np.asarray(COLOUR)) * 255))
    assert tuple(cut[40, 40]) == expected and tuple(zoomed[40, 40]) == shaded(COLOUR)


def test_draw_from_a_zero_direction_is_refused():
    with pytest.raises(SolidWorksError, match="from_dir"):
        render.draw([group("A", COLOUR)], (0, 0, 0), 80, 80)


# --- the tool's own checks (before any document is touched) ---------------------------------

def test_default_path_is_a_png_in_the_shots_folder_of_the_home(swmcp_home):
    path = render.default_path("Bracket v2 (copy).SLDPRT")
    assert os.path.dirname(path) == os.path.join(str(swmcp_home), "shots") and os.path.isdir(os.path.dirname(path))
    assert re.fullmatch(r"Bracket_v2__copy_-view-\d{6}\.png", os.path.basename(path))
    section_shot = os.path.basename(render.default_path("A-1_b.c.SLDASM", "section"))
    assert re.fullmatch(r"A-1_b\.c-section-\d{6}\.png", section_shot)
    long_title = "x" * 200 + ".SLDPRT"
    assert os.path.basename(render.default_path(long_title)).startswith("x" * 60 + "-view-")


def test_unknown_view_and_malformed_zoom_are_refused_before_the_document_is_asked():
    with pytest.raises(SolidWorksError, match="Unknown view 'sideways'"):
        render.render_view(None, view="sideways")
    for zoom in ([[0, 0, 0]], [[0, 0], [1, 1]], [[0, 0, 0], [1, 1, 1], [2, 2, 2]]):
        with pytest.raises(SolidWorksError, match="zoom_mm needs two corners"):
            render.render_view(None, zoom_mm=zoom)


def test_no_current_document_is_a_named_error():
    with pytest.raises(SolidWorksError, match="No current document"):
        render._document(types.SimpleNamespace(_model=None), None)
    model = object()
    assert render._document(types.SimpleNamespace(_model=model), None) is model
