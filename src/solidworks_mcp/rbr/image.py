"""A blank-frame detector for screenshots, with no imaging dependency.

SolidWorks' SaveAs3 grabs the graphics buffer: with the window minimised or
fully covered the file is a solid white rectangle that *looks* like a result.
is_blank_png decodes the PNG with the standard library alone (zlib + the five
PNG filters) and answers whether every pixel is one colour.

Only what SolidWorks writes is supported: 8-bit greyscale/RGB/RGBA without
interlacing. Anything else answers None ("cannot tell") rather than guessing.
"""

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}  # colour type -> samples per pixel (8-bit only)


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


def is_blank_png(data: bytes) -> bool | None:
    """True when every pixel of the PNG is the same colour; None when undecodable."""
    if not data.startswith(_SIGNATURE):
        return None
    width = height = None
    bpp = None
    idat = bytearray()
    for kind, payload in _chunks(data):
        if kind == b"IHDR":
            if len(payload) < 13:
                return None
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", payload[:13])
            if depth != 8 or interlace != 0 or colour not in _CHANNELS:
                return None
            bpp = _CHANNELS[colour]
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    if not width or not height or bpp is None or not idat:
        return None
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    pixels = _unfilter(raw, width, height, bpp)
    if pixels is None:
        return None
    first = pixels[:bpp]
    return all(pixels[i:i + bpp] == first for i in range(0, len(pixels), bpp))


def is_blank_file(path: str) -> bool | None:
    """is_blank_png for a file on disk; non-PNG and unreadable files answer None."""
    try:
        with open(path, "rb") as fh:
            return is_blank_png(fh.read())
    except OSError:
        return None
