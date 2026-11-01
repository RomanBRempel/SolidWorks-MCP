"""The PNG blank detector: real (tiny) PNGs built in the test, no imaging library."""

import struct
import zlib

from solidworks_mcp.rbr import image


def _png(width, height, rows, colour=2, depth=8, filters=None):
    """A valid PNG from raw pixel rows; rows[r] is bytes of one scanline."""
    def chunk(kind, payload):
        data = kind + payload
        return struct.pack(">I", len(payload)) + data + struct.pack(">I", zlib.crc32(data))

    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    filters = filters or [0] * height
    raw = b"".join(bytes([filters[r]]) + rows[r] for r in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_uniform_rgb_is_blank():
    white = bytes([255, 255, 255] * 4)
    assert image.is_blank_png(_png(4, 3, [white, white, white])) is True


def test_one_differing_pixel_is_not_blank():
    white = bytes([255, 255, 255] * 4)
    spot = bytes([255, 255, 255, 250, 255, 255, 255, 255, 255, 255, 255, 255])
    assert image.is_blank_png(_png(4, 3, [white, spot, white])) is False


def test_filters_are_undone_before_judging():
    # rows stored with Sub/Up/Paeth filters still decode to one colour
    grey = bytes([128, 128, 128] * 5)
    deltas_sub = bytes([128, 128, 128] + [0] * 12)       # Sub: first pixel raw, rest delta 0
    deltas_up = bytes([0] * 15)                          # Up: identical to the row above
    png = _png(5, 3, [grey, deltas_up, deltas_sub], filters=[0, 2, 1])
    assert image.is_blank_png(png) is True


def test_rgba_and_greyscale_work():
    assert image.is_blank_png(_png(2, 2, [bytes([7, 7]), bytes([7, 7])], colour=0)) is True
    rgba = bytes([1, 2, 3, 4] * 2)
    assert image.is_blank_png(_png(2, 2, [rgba, rgba], colour=6)) is True


def test_undecodable_answers_none():
    assert image.is_blank_png(b"not a png") is None
    white = bytes([255, 255, 255] * 2)
    png16 = _png(2, 1, [white * 2], depth=16)
    assert image.is_blank_png(png16) is None
    short = bytearray(_png(4, 1, [bytes([0, 0, 0] * 4)]))
    short[20:24] = struct.pack(">I", 4)  # header now claims 4 rows, the data holds 1
    assert image.is_blank_png(bytes(short)) is None


def test_is_blank_file_reads_from_disk(tmp_path):
    path = tmp_path / "shot.png"
    white = bytes([255, 255, 255] * 4)
    path.write_bytes(_png(4, 1, [white]))
    assert image.is_blank_file(str(path)) is True
    assert image.is_blank_file(str(tmp_path / "missing.png")) is None
