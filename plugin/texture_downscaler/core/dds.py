"""Minimal DDS header parsing (no pixel decoding)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

DDS_MAGIC = b"DDS "
HEADER_READ_SIZE = 4 + 124 + 20  # magic + DDS_HEADER + DDS_HEADER_DXT10

DDPF_ALPHAPIXELS = 0x1
DDPF_FOURCC = 0x4
DDPF_RGB = 0x40
DDPF_LUMINANCE = 0x20000
DDSCAPS2_CUBEMAP = 0x200
DDS_RESOURCE_MISC_TEXTURECUBE = 0x4

DXGI_NAMES: dict[int, str] = {
    2: "RGBA32F",
    10: "RGBA16F",
    24: "RGB10A2",
    28: "RGBA8",
    29: "RGBA8_SRGB",
    49: "RG8",
    56: "R16",
    61: "R8",
    65: "A8",
    71: "BC1",
    72: "BC1_SRGB",
    74: "BC2",
    75: "BC2_SRGB",
    77: "BC3",
    78: "BC3_SRGB",
    80: "BC4",
    81: "BC4_SNORM",
    83: "BC5",
    84: "BC5_SNORM",
    85: "B5G6R5",
    86: "B5G5R5A1",
    87: "BGRA8",
    88: "BGRX8",
    91: "BGRA8_SRGB",
    93: "BGRX8_SRGB",
    95: "BC6H_UF16",
    96: "BC6H_SF16",
    98: "BC7",
    99: "BC7_SRGB",
}

FOURCC_NAMES: dict[bytes, str] = {
    b"DXT1": "BC1",
    b"DXT2": "BC2",
    b"DXT3": "BC2",
    b"DXT4": "BC3",
    b"DXT5": "BC3",
    b"ATI1": "BC4",
    b"BC4U": "BC4",
    b"BC4S": "BC4_SNORM",
    b"ATI2": "BC5",
    b"BC5U": "BC5",
    b"BC5S": "BC5_SNORM",
}


@dataclass(frozen=True, slots=True)
class DdsInfo:
    width: int
    height: int
    mip_count: int
    format: str
    is_cubemap: bool

    @property
    def max_dim(self) -> int:
        return max(self.width, self.height)


def parse_dds_header(data: bytes) -> DdsInfo | None:
    """Parse the first bytes of a DDS file. Returns None if it is not a valid DDS."""
    if len(data) < 128 or data[:4] != DDS_MAGIC:
        return None

    height, width = struct.unpack_from("<II", data, 12)
    mip_count = struct.unpack_from("<I", data, 28)[0] or 1
    pf_flags = struct.unpack_from("<I", data, 80)[0]
    fourcc = data[84:88]
    caps2 = struct.unpack_from("<I", data, 112)[0]
    is_cubemap = bool(caps2 & DDSCAPS2_CUBEMAP)

    if width == 0 or height == 0:
        return None

    if pf_flags & DDPF_FOURCC:
        if fourcc == b"DX10":
            if len(data) < 148:
                return None
            dxgi, _dim, misc = struct.unpack_from("<III", data, 128)
            fmt = DXGI_NAMES.get(dxgi, f"DXGI_{dxgi}")
            is_cubemap = is_cubemap or bool(misc & DDS_RESOURCE_MISC_TEXTURECUBE)
        else:
            fmt = FOURCC_NAMES.get(fourcc, fourcc.decode("ascii", errors="replace").strip("\x00 "))
    else:
        bits = struct.unpack_from("<I", data, 88)[0]
        if pf_flags & DDPF_LUMINANCE:
            fmt = f"L{bits}"
        elif pf_flags & DDPF_ALPHAPIXELS:
            fmt = f"RGBA{bits}bit"
        else:
            fmt = f"RGB{bits}bit"

    return DdsInfo(width=width, height=height, mip_count=mip_count, format=fmt, is_cubemap=is_cubemap)


def read_dds_info(path: Path | str) -> DdsInfo | None:
    try:
        with open(path, "rb") as f:
            return parse_dds_header(f.read(HEADER_READ_SIZE))
    except OSError:
        return None
