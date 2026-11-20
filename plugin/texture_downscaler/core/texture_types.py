"""Classify Skyrim textures by their filename suffix."""

from __future__ import annotations

from enum import StrEnum


class TextureType(StrEnum):
    DIFFUSE = "Diffuse"
    NORMAL = "Normal"
    SPECULAR = "Specular / Mask"
    GLOW = "Glow"
    PARALLAX = "Parallax / Height"
    ENVIRONMENT = "Environment / Cubemap"


# Suffix (lowercase, without extension) -> type. Longest suffixes are checked first.
_SUFFIXES: dict[str, TextureType] = {
    "_msn": TextureType.NORMAL,
    "_n": TextureType.NORMAL,
    "_s": TextureType.SPECULAR,
    "_m": TextureType.SPECULAR,
    "_em": TextureType.SPECULAR,  # environment mask
    "_sk": TextureType.SPECULAR,  # skin tint / subsurface
    "_b": TextureType.SPECULAR,  # back light
    "_rmaos": TextureType.SPECULAR,  # PBR (Community Shaders / True PBR)
    "_cnr": TextureType.SPECULAR,  # PBR coat/normal/roughness
    "_g": TextureType.GLOW,
    "_p": TextureType.PARALLAX,
    "_e": TextureType.ENVIRONMENT,
}
_ORDERED = sorted(_SUFFIXES.items(), key=lambda kv: len(kv[0]), reverse=True)


def classify(virtual_path: str, is_cubemap: bool = False) -> TextureType:
    if is_cubemap:
        return TextureType.ENVIRONMENT
    name = virtual_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".dds"):
        name = name[:-4]
    for suffix, ttype in _ORDERED:
        if name.endswith(suffix):
            return ttype
    if "/cubemaps/" in virtual_path.lower().replace("\\", "/"):
        return TextureType.ENVIRONMENT
    return TextureType.DIFFUSE
