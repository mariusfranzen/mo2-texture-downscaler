"""Build tiny synthetic DDS files for tests."""

from __future__ import annotations

import struct
from pathlib import Path

DDSD_CAPS, DDSD_HEIGHT, DDSD_WIDTH, DDSD_PIXELFORMAT, DDSD_MIPMAPCOUNT, DDSD_LINEARSIZE = (
    0x1,
    0x2,
    0x4,
    0x1000,
    0x20000,
    0x80000,
)


def dds_header(
    width: int,
    height: int,
    *,
    mips: int = 1,
    fourcc: bytes | None = None,
    dxgi: int | None = None,
    cubemap: bool = False,
) -> bytes:
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
    if dxgi is not None:
        fourcc = b"DX10"
    if fourcc:
        pf = struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    else:  # uncompressed A8R8G8B8
        pf = struct.pack(
            "<II4sIIIII", 32, 0x41, b"\0\0\0\0", 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000
        )
    caps2 = 0xFE00 if cubemap else 0
    header = struct.pack("<7I", 124, flags, height, width, 0, 0, mips) + b"\0" * 44 + pf
    header += struct.pack("<5I", 0x1000, caps2, 0, 0, 0)
    assert len(header) == 124
    data = b"DDS " + header
    if dxgi is not None:
        data += struct.pack("<5I", dxgi, 3, 0x4 if cubemap else 0, 1, 0)
    return data


def write_rgba_dds(path: Path, width: int, height: int) -> Path:
    """A real, valid uncompressed 32-bit DDS (no mips) that texconv can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = bytes(
        (x * 7 % 256, y * 5 % 256, 128, 255)[c] for y in range(height) for x in range(width) for c in range(4)
    )
    path.write_bytes(dds_header(width, height) + pixels)
    return path


def write_fake_dds(path: Path, width: int, height: int, **kw) -> Path:
    """Header only - enough for the scanner, not for texconv."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dds_header(width, height, **kw))
    return path
