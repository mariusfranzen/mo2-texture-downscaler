"""Persistent cache of DDS header info, keyed by file path + size + mtime.

Opening ~60k files to read their headers is the slow part of a scan (seconds, mostly per-file
open overhead on Windows). Size and mtime come for free from the directory listing, so files
that haven't changed are answered from this cache without being opened.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .dds import DdsInfo

CACHE_VERSION = 1
_MTIME_TOLERANCE = 0.001  # JSON float round-trips are exact, but be lenient anyway


class HeaderCache:
    def __init__(self, path: Path | None = None):
        self.path = path
        # key (lowercase abs path) -> [size, mtime, width, height, mips, format, is_cubemap]
        self._old: dict[str, list] = {}
        self._new: dict[str, list] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @classmethod
    def load(cls, path: Path) -> HeaderCache:
        cache = cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                cache._old = data.get("files", {})
        except (OSError, ValueError, AttributeError):
            pass
        return cache

    def get(self, file: Path, size: int, mtime: float) -> DdsInfo | None:
        key = str(file).lower()
        row = self._old.get(key)
        if row is None or row[0] != size or abs(row[1] - mtime) > _MTIME_TOLERANCE:
            self.misses += 1
            return None
        self.hits += 1
        with self._lock:
            self._new[key] = row
        return DdsInfo(width=row[2], height=row[3], mip_count=row[4], format=row[5], is_cubemap=row[6])

    def put(self, file: Path, size: int, mtime: float, info: DdsInfo) -> None:
        row = [size, mtime, info.width, info.height, info.mip_count, info.format, info.is_cubemap]
        with self._lock:
            self._new[str(file).lower()] = row

    def save(self) -> None:
        """Write only entries used in this scan, so files that disappeared are dropped."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": CACHE_VERSION, "files": self._new}, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            pass  # a cache is optional
