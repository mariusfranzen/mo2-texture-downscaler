"""Background threads so MO2's UI stays responsive."""

from __future__ import annotations

import traceback
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..core import output
from ..core.downscaler import JobResult, run_jobs
from ..core.output import Manifest, Plan
from ..core.scanner import TextureEntry, TextureSource, scan


class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(list)  # list[TextureEntry]
    failed = pyqtSignal(str)

    def __init__(self, sources: list[TextureSource], parent=None):
        super().__init__(parent)
        self.sources = sources
        self.cancel = False

    def run(self):
        try:
            entries = scan(self.sources, self.progress.emit, lambda: self.cancel)
        except Exception:
            self.failed.emit(traceback.format_exc())
            return
        if not self.cancel:
            self.done.emit(entries)


class DownscaleWorker(QThread):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(list)  # list[JobResult]
    failed = pyqtSignal(str)

    def __init__(
        self,
        plan: Plan,
        entries_by_key: dict[str, TextureEntry],
        manifest: Manifest,
        output_dir: Path,
        workers: int,
        parent=None,
    ):
        super().__init__(parent)
        self.plan = plan
        self.entries_by_key = entries_by_key
        self.manifest = manifest
        self.output_dir = output_dir
        self.workers = workers
        self.cancel = False

    def run(self):
        try:
            self._run()
        except Exception:
            self.failed.emit(traceback.format_exc())

    def _run(self):
        if self.plan.remove:
            self.progress.emit(0, 0, f"Removing {len(self.plan.remove)} previous outputs...")
            output.remove_outputs(self.plan.remove, self.manifest, self.output_dir)
            self.manifest.save(self.output_dir)

        def on_progress(n: int, total: int, result: JobResult) -> None:
            if result.ok:
                output.record(self.manifest, self.entries_by_key[result.job.key], result.job)
            if n % 50 == 0:  # checkpoint so a crash/cancel doesn't lose bookkeeping
                self.manifest.save(self.output_dir)
            self.progress.emit(n, total, result.job.source.name)

        results = run_jobs(
            self.plan.jobs,
            progress=on_progress,
            cancelled=lambda: self.cancel,
            workers=self.workers,
        )
        self.manifest.save(self.output_dir)
        self.done.emit(results)
