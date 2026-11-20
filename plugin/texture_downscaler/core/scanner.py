"""Find the winning texture for every virtual path across a prioritized list of sources.

A *source* is anything that can list textures (a loose mod folder today; a BSA archive later).
Sources are given in ascending priority order, exactly like MO2's left pane: later wins.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .dds import DdsInfo, read_dds_info
from .texture_types import TextureType, classify

ProgressFn = Callable[[int, int, str], None]  # (done, total, message)
CancelFn = Callable[[], bool]


@dataclass(slots=True)
class SourceFile:
    """A texture file provided by a source."""

    virtual_path: str  # e.g. "textures/armor/iron/cuirass.dds" (original casing, forward slashes)
    abs_path: Path
    size: int
    mtime: float


class TextureSource(Protocol):
    name: str

    def iter_textures(self) -> Iterator[SourceFile]: ...


@dataclass(slots=True)
class LooseModSource:
    """Loose `.dds` files under a mod folder's `textures/` directory."""

    name: str
    root: Path

    def iter_textures(self) -> Iterator[SourceFile]:
        try:
            textures_dir = next(
                (Path(e.path) for e in os.scandir(self.root) if e.is_dir() and e.name.lower() == "textures"),
                None,
            )
        except OSError:
            return
        if textures_dir is None:
            return
        yield from self._walk(textures_dir, textures_dir.name)

    def _walk(self, directory: Path, rel: str) -> Iterator[SourceFile]:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            entry_rel = f"{rel}/{entry.name}"
            try:
                if entry.is_dir():
                    yield from self._walk(Path(entry.path), entry_rel)
                elif entry.name.lower().endswith(".dds"):
                    st = entry.stat()
                    yield SourceFile(entry_rel, Path(entry.path), st.st_size, st.st_mtime)
            except OSError:
                continue


@dataclass(slots=True)
class TextureEntry:
    """The winning version of one virtual texture path, plus the user's choices for it."""

    virtual_path: str
    mod: str
    abs_path: Path
    size: int
    mtime: float
    info: DdsInfo
    ttype: TextureType
    provider_count: int = 1  # how many sources provide this path
    target: int | None = None  # requested max dimension; None = leave as is
    done_target: int | None = None  # already downscaled to this in the output mod
    status: str = ""

    @property
    def key(self) -> str:
        return self.virtual_path.lower()

    @property
    def max_dim(self) -> int:
        return self.info.max_dim


def scan(
    sources: list[TextureSource],
    progress: ProgressFn | None = None,
    cancelled: CancelFn | None = None,
    workers: int = 8,
) -> list[TextureEntry]:
    """Scan sources (ascending priority) and return one entry per winning texture."""
    progress = progress or (lambda *_: None)
    cancelled = cancelled or (lambda: False)

    winners: dict[str, tuple[str, SourceFile]] = {}
    counts: dict[str, int] = {}
    for i, source in enumerate(sources):
        if cancelled():
            return []
        progress(i, len(sources), f"Listing files: {source.name}")
        for f in source.iter_textures():
            key = f.virtual_path.lower()
            winners[key] = (source.name, f)
            counts[key] = counts.get(key, 0) + 1

    items = list(winners.items())
    total = len(items)
    entries: list[TextureEntry] = []

    def read(item: tuple[str, tuple[str, SourceFile]]) -> TextureEntry | None:
        if cancelled():
            return None
        key, (mod, f) = item
        info = read_dds_info(f.abs_path)
        if info is None:
            return None
        return TextureEntry(
            virtual_path=f.virtual_path,
            mod=mod,
            abs_path=f.abs_path,
            size=f.size,
            mtime=f.mtime,
            info=info,
            ttype=classify(f.virtual_path, info.is_cubemap),
            provider_count=counts[key],
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, entry in enumerate(pool.map(read, items), 1):
            if entry is not None:
                entries.append(entry)
            if n % 500 == 0 or n == total:
                progress(n, total, f"Reading texture headers ({n}/{total})")
            if cancelled():
                return []

    entries.sort(key=lambda e: (e.info.width * e.info.height, e.size), reverse=True)
    return entries
