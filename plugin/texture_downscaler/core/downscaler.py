"""Resize DDS textures with texconv.exe (Microsoft DirectXTex)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .dds import DdsInfo

TEXCONV_PATH = Path(__file__).resolve().parent.parent / "bin" / "texconv.exe"
_BLOCK_COMPRESSED_PREFIX = "BC"


def target_size(info: DdsInfo, target: int) -> tuple[int, int]:
    """Scale (width, height) so the larger side equals `target`, keeping aspect ratio.

    Returns the original size if it is already at or below the target.
    """
    largest = info.max_dim
    if target >= largest:
        return info.width, info.height
    minimum = 4 if info.format.startswith(_BLOCK_COMPRESSED_PREFIX) else 1
    width = max(minimum, info.width * target // largest)
    height = max(minimum, info.height * target // largest)
    return width, height


def estimated_size(file_size: int, info: DdsInfo, target: int | None) -> int:
    """Rough output file size: scales with pixel count (mip/header overhead is proportional)."""
    if target is None or target >= info.max_dim:
        return file_size
    w, h = target_size(info, target)
    return int(file_size * (w * h) / (info.width * info.height))


@dataclass(slots=True)
class Job:
    key: str
    source: Path
    dest: Path  # full output file path; file name must equal source file name
    width: int
    height: int
    mipmaps: bool


@dataclass(slots=True)
class JobResult:
    job: Job
    ok: bool
    message: str = ""


def build_command(job: Job, texconv: Path = TEXCONV_PATH) -> list[str]:
    # No -f: texconv keeps the source pixel format (BC1/3/5/7, sRGB flag, etc.).
    return [
        str(texconv),
        "-nologo",
        "-y",
        "-w",
        str(job.width),
        "-h",
        str(job.height),
        "-m",
        "0" if job.mipmaps else "1",
        "-o",
        str(job.dest.parent),
        str(job.source),
    ]


def run_job(job: Job, texconv: Path = TEXCONV_PATH) -> JobResult:
    if job.source.name.lower() != job.dest.name.lower():
        return JobResult(job, False, "Destination file name must match source file name")
    try:
        job.dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            build_command(job, texconv),
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return JobResult(job, False, str(e))
    if proc.returncode != 0 or not job.dest.exists():
        output = (proc.stdout + proc.stderr).strip().splitlines()
        return JobResult(job, False, output[-1] if output else f"texconv exit code {proc.returncode}")
    return JobResult(job, True)


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def run_jobs(
    jobs: list[Job],
    progress: Callable[[int, int, JobResult], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    workers: int = 0,
    texconv: Path = TEXCONV_PATH,
) -> list[JobResult]:
    """Run jobs in parallel. Jobs not yet started when cancelled are skipped."""
    cancelled = cancelled or (lambda: False)
    results: list[JobResult] = []

    def guarded(job: Job) -> JobResult:
        if cancelled():
            return JobResult(job, False, "Cancelled")
        return run_job(job, texconv)

    with ThreadPoolExecutor(max_workers=workers or default_workers()) as pool:
        futures = [pool.submit(guarded, job) for job in jobs]
        for n, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            if progress:
                progress(n, len(jobs), result)
    return results
