"""A blank-frame detector for screenshots, with no imaging dependency.

SolidWorks' SaveAs3 grabs the graphics buffer: with the view stilled, the
window minimised or fully covered the file is a solid white rectangle that
*looks* like a result - and SolidWorks saves that one as a 1-BIT greyscale PNG
(1126 bytes, verified live), since two colours suffice for it. is_blank_png
decodes the PNG with the standard library alone (zlib + the five PNG filters)
and answers whether every pixel is one colour.

Supported: 8-bit greyscale/RGB/RGBA, and 1/2/4-bit greyscale or palette,
without interlacing. Anything else answers None ("cannot tell"), not a guess.
"""

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}  # colour type -> samples per pixel (8-bit)
_INDEXED = {0, 3}  # greyscale and palette: one sample, any bit depth


def _chunks(data: bytes):
    pos = len(_SIGNATURE)
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos + 8:pos + 8 + length]
        pos += 12 + length  # header + payload + CRC


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray | None:
    """Reverse the per-scanline PNG filters; None when the stream is malformed."""
    stride = width * bpp
    if len(raw) < height * (stride + 1):
        return None
    out = bytearray(height * stride)
    prior = bytearray(stride)
    pos = 0
    for row in range(height):
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:    # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prior[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prior[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = prior[i]
                up_left = prior[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pred = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            return None
        out[row * stride:(row + 1) * stride] = line
        prior = line
    return out


def _bits_blank(rows: bytearray, width: int, height: int, depth: int, stride: int) -> bool:
    """Every sub-byte pixel one value? Rows first (cheap), then the first row's bits."""
    first_row = rows[:stride]
    if any(rows[r * stride:(r + 1) * stride] != first_row for r in range(1, height)):
        return False
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    seen = set()
    for x in range(width):
        byte = first_row[x // per_byte]
        shift = 8 - depth * (x % per_byte + 1)
        seen.add((byte >> shift) & mask)
        if len(seen) > 1:
            return False
    return True


def is_blank_png(data: bytes) -> bool | None:
    """True when every pixel of the PNG is the same colour; None when undecodable."""
    if not data.startswith(_SIGNATURE):
        return None
    width = height = None
    depth = colour = None
    idat = bytearray()
    for kind, payload in _chunks(data):
        if kind == b"IHDR":
            if len(payload) < 13:
                return None
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", payload[:13])
            if interlace != 0:
                return None
            if not ((depth == 8 and colour in _CHANNELS) or (depth in (1, 2, 4) and colour in _INDEXED)):
                return None
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    if not width or not height or depth is None or not idat:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    if depth == 8:
        bpp = _CHANNELS[colour] if colour in _CHANNELS else 1
        pixels = _unfilter(raw, width, height, bpp)
        if pixels is None:
            return None
        first = pixels[:bpp]
        return all(pixels[i:i + bpp] == first for i in range(0, len(pixels), bpp))
    # 1/2/4-bit greyscale or palette: filters work on BYTES (bpp = 1); the
    # blank 1126-byte SolidWorks frame is exactly such a 1-bit file
    stride = (width * depth + 7) // 8
    rows = _unfilter_bytes(raw, stride, height)
    if rows is None:
        return None
    return _bits_blank(rows, width, height, depth, stride)


def _unfilter_bytes(raw: bytes, stride: int, height: int) -> bytearray | None:
    """_unfilter for sub-byte depths: one 'pixel' = one byte of packed bits."""
    return _unfilter(raw, stride, height, 1)


def is_blank_file(path: str) -> bool | None:
    """is_blank_png for a file on disk; non-PNG and unreadable files answer None."""
    try:
        with open(path, "rb") as fh:
            return is_blank_png(fh.read())
    except OSError:
        return None
