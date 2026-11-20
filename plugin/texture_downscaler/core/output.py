"""Output mod bookkeeping: the manifest of what was downscaled, and planning a run.

The manifest (`texture_downscaler.json` in the output mod root) records, per virtual path, which
source file was downscaled and to what size. It lets a later run skip textures that are already
done, redo ones whose source changed, and remove outputs the user no longer wants.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from .downscaler import Job, target_size
from .scanner import TextureEntry

MANIFEST_NAME = "texture_downscaler.json"
MANIFEST_VERSION = 1


class RunMode(StrEnum):
    SKIP_DONE = "skip"  # keep existing outputs that are still valid
    REBUILD = "rebuild"  # wipe previous outputs and start fresh


@dataclass(slots=True)
class ManifestEntry:
    path: str  # virtual path, original casing
    mod: str
    source_size: int
    source_mtime: float
    target: int
    width: int
    height: int


@dataclass(slots=True)
class Manifest:
    entries: dict[str, ManifestEntry] = field(default_factory=dict)  # key = lowercase path

    @classmethod
    def load(cls, mod_dir: Path) -> Manifest:
        path = mod_dir / MANIFEST_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        entries = {}
        for key, raw in data.get("textures", {}).items():
            try:
                entries[key] = ManifestEntry(**raw)
            except TypeError:
                continue
        return cls(entries)

    def save(self, mod_dir: Path) -> None:
        mod_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": MANIFEST_VERSION,
            "textures": {k: asdict(v) for k, v in sorted(self.entries.items())},
        }
        tmp = mod_dir / (MANIFEST_NAME + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(mod_dir / MANIFEST_NAME)


def is_current(m: ManifestEntry, entry: TextureEntry, output_dir: Path) -> bool:
    """True if the output for `entry` exists and was made from the same source file."""
    return (
        m.mod == entry.mod
        and m.source_size == entry.size
        and abs(m.source_mtime - entry.mtime) < 2.0
        and (output_dir / m.path).is_file()
    )


def apply_manifest(entries: list[TextureEntry], manifest: Manifest, output_dir: Path) -> None:
    """Pre-fill targets/status of scanned entries from a previous run (skip mode)."""
    for entry in entries:
        m = manifest.entries.get(entry.key)
        if m is None:
            continue
        entry.target = m.target
        if is_current(m, entry, output_dir):
            entry.done_target = m.target
            entry.status = f"Done ({m.width}x{m.height})"
        else:
            entry.status = "Source changed - will redo"


@dataclass(slots=True)
class Plan:
    jobs: list[Job]
    remove: list[str]  # manifest keys whose output files should be deleted
    skipped: int  # already up to date

    @property
    def is_empty(self) -> bool:
        return not self.jobs and not self.remove


def make_plan(entries: list[TextureEntry], manifest: Manifest, output_dir: Path, mode: RunMode) -> Plan:
    by_key = {e.key: e for e in entries}
    jobs: list[Job] = []
    remove: list[str] = []
    skipped = 0

    if mode is RunMode.REBUILD:
        remove = list(manifest.entries)

    for entry in entries:
        wanted = entry.target is not None and entry.target < entry.max_dim
        m = manifest.entries.get(entry.key)
        if not wanted:
            if m is not None and mode is RunMode.SKIP_DONE:
                remove.append(entry.key)
            continue
        if (
            mode is RunMode.SKIP_DONE
            and m is not None
            and m.target == entry.target
            and is_current(m, entry, output_dir)
        ):
            skipped += 1
            continue
        w, h = target_size(entry.info, entry.target)
        jobs.append(
            Job(
                key=entry.key,
                source=entry.abs_path,
                dest=output_dir / entry.virtual_path,
                width=w,
                height=h,
                mipmaps=entry.info.mip_count > 1,
            )
        )

    # Outputs whose source texture is no longer provided by any enabled mod.
    if mode is RunMode.SKIP_DONE:
        remove.extend(k for k in manifest.entries if k not in by_key)

    return Plan(jobs=jobs, remove=remove, skipped=skipped)


def remove_outputs(keys: list[str], manifest: Manifest, output_dir: Path) -> None:
    for key in keys:
        m = manifest.entries.pop(key, None)
        if m is None:
            continue
        try:
            (output_dir / m.path).unlink(missing_ok=True)
        except OSError:
            pass
    _prune_empty_dirs(output_dir / "textures")


def record(manifest: Manifest, entry: TextureEntry, job: Job) -> None:
    manifest.entries[entry.key] = ManifestEntry(
        path=entry.virtual_path,
        mod=entry.mod,
        source_size=entry.size,
        source_mtime=entry.mtime,
        target=entry.target or job.width,
        width=job.width,
        height=job.height,
    )


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()  # only succeeds when empty
        except OSError:
            pass
