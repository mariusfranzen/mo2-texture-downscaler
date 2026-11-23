"""Background threads so MO2's UI stays responsive."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..core import nexus, output
from ..core.downscaler import JobResult, run_jobs
from ..core.header_cache import HeaderCache
from ..core.i18n import tr
from ..core.nexus import AuthorCache, NexusRef
from ..core.output import Manifest, Plan
from ..core.scanner import TextureEntry, TextureSource, scan


class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(list)  # list[TextureEntry]
    failed = pyqtSignal(str)

    def __init__(self, sources: list[TextureSource], cache_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.sources = sources
        self.cache_path = cache_path
        self.cancel = False

    def run(self):
        try:
            cache = HeaderCache.load(self.cache_path) if self.cache_path else HeaderCache()
            entries = scan(self.sources, self.progress.emit, lambda: self.cancel, cache=cache)
            if not self.cancel:
                cache.save()
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
            self.progress.emit(0, 0, tr("run.removing", count=len(self.plan.remove)))
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


class AuthorWorker(QThread):
    """Looks up Nexus uploaders for mods that aren't in the author cache yet."""

    found = pyqtSignal(str, str)  # NexusRef.key, uploader ("" = not found)
    progress = pyqtSignal(int, int)
    finished_msg = pyqtSignal(str)

    def __init__(
        self,
        refs: list[NexusRef],
        cache: AuthorCache,
        api_key_provider: Callable[[], str | None],
        parent=None,
    ):
        super().__init__(parent)
        self.refs = refs
        self.cache = cache
        self.api_key_provider = api_key_provider
        self.cancel = False

    def run(self):
        try:
            api_key = self.api_key_provider()
            if not api_key:
                self.finished_msg.emit(tr("authors.no_key"))
                return
            total = len(self.refs)
            count = [0]

            def on_result(ref: NexusRef, author: str) -> None:
                count[0] += 1
                self.found.emit(ref.key, author)
                self.progress.emit(count[0], total)

            msg = nexus.fetch_missing(self.refs, self.cache, api_key, on_result, lambda: self.cancel)
            self.finished_msg.emit(msg)
        except Exception:
            self.finished_msg.emit(tr("authors.failed") + "\n" + traceback.format_exc(limit=3))
