"""Main window: scan results table, filters, bulk target tools, and the downscale run."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..core import output
from ..core.downscaler import JobResult, estimated_size
from ..core.output import Manifest, RunMode
from ..core.scanner import TextureEntry, TextureSource
from ..core.texture_types import TextureType
from .table_model import (
    COL_MOD,
    COL_PATH,
    COL_RES,
    COL_STATUS,
    COL_TARGET,
    COL_TYPE,
    ENTRY_ROLE,
    TARGET_CHOICES,
    TextureFilterProxy,
    TextureTableModel,
    fmt_bytes,
    fmt_dim,
)
from .workers import DownscaleWorker, ScanWorker

HIDE_SMALLER_CHOICES = [0, 512, 1024, 2048, 4096, 8192]
DEFAULT_TYPE_LIMITS: dict[TextureType, int | None] = {t: None for t in TextureType}


def _size_combo(choices: list[int], none_label: str | None = None, default: int | None = None) -> QComboBox:
    combo = QComboBox()
    if none_label is not None:
        combo.addItem(none_label, None)
    for c in choices:
        combo.addItem(f"{fmt_dim(c)}  ({c} px)", c)
    if default is not None:
        combo.setCurrentIndex(combo.findData(default))
    return combo


class TargetDelegate(QStyledItemDelegate):
    """Combo box editor for the Target column, offering only sizes smaller than the texture."""

    def createEditor(self, parent, option, index):
        entry: TextureEntry = index.data(ENTRY_ROLE)
        combo = QComboBox(parent)
        combo.addItem("- keep original", None)
        for c in TARGET_CHOICES:
            if c < entry.max_dim:
                combo.addItem(fmt_dim(c), c)
        return combo

    def setEditorData(self, editor, index):
        i = editor.findData(index.data(Qt.ItemDataRole.EditRole))
        editor.setCurrentIndex(max(0, i))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.ItemDataRole.EditRole)


class MainDialog(QDialog):
    def __init__(
        self,
        *,
        sources: list[TextureSource],
        output_dir: Path,
        output_name: str,
        mode: RunMode,
        workers: int,
        ensure_output_mod: Callable[[], Path | None],
        on_output_written: Callable[[], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Texture Downscaler")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1500, 850)

        self.sources = sources
        self.output_dir = output_dir
        self.output_name = output_name
        self.mode = mode
        self.workers = workers
        self.ensure_output_mod = ensure_output_mod
        self.on_output_written = on_output_written
        self.manifest = Manifest.load(output_dir)
        self.scan_worker: ScanWorker | None = None

        self.model = TextureTableModel(self)
        self.proxy = TextureFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.model.dataChanged.connect(self._update_summary)
        self.model.modelReset.connect(self._update_summary)

        self._build_ui()
        self._start_scan()

    # ================================================================================== UI ====

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- scan status line
        status_row = QHBoxLayout()
        self.scan_label = QLabel("Scanning...")
        self.scan_bar = QProgressBar()
        self.scan_bar.setMaximumWidth(300)
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self._start_scan)
        status_row.addWidget(self.scan_label, 1)
        status_row.addWidget(self.scan_bar)
        status_row.addWidget(self.rescan_btn)
        root.addLayout(status_row)

        if self.mode is RunMode.REBUILD and self.manifest.entries:
            warn = QLabel(
                f"<b>Rebuild from scratch:</b> the {len(self.manifest.entries)} textures previously written to "
                f"'{self.output_name}' will be deleted when you click Downscale."
            )
            warn.setWordWrap(True)
            root.addWidget(warn)

        # --- filters
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by path or mod (space-separated terms)...")
        self.search.setClearButtonEnabled(True)
        self.mod_combo = QComboBox()
        self.mod_combo.setMinimumWidth(220)
        self.mod_combo.addItem("All mods", None)
        self.type_combo = QComboBox()
        self.type_combo.addItem("All types", None)
        for t in TextureType:
            self.type_combo.addItem(t.value, t)
        self.hide_combo = QComboBox()
        for d in HIDE_SMALLER_CHOICES:
            self.hide_combo.addItem("Show all sizes" if d == 0 else f"Hide smaller than {fmt_dim(d)}", d)
        self.hide_combo.setCurrentIndex(self.hide_combo.findData(2048))
        self.only_marked = QCheckBox("Only marked")
        self.hidden_mods: set[str] = set()
        self.unhide_btn = QPushButton()
        self.unhide_btn.setToolTip("Show all mods you hid via right-click again")
        self.unhide_btn.clicked.connect(self._unhide_all_mods)
        self.unhide_btn.setVisible(False)

        filters.addWidget(self.search, 3)
        filters.addWidget(QLabel("Mod:"))
        filters.addWidget(self.mod_combo, 2)
        filters.addWidget(QLabel("Type:"))
        filters.addWidget(self.type_combo)
        filters.addWidget(self.hide_combo)
        filters.addWidget(self.only_marked)
        filters.addWidget(self.unhide_btn)
        root.addLayout(filters)

        self.search.textChanged.connect(self._apply_filters)
        for combo in (self.mod_combo, self.type_combo, self.hide_combo):
            combo.currentIndexChanged.connect(self._apply_filters)
        self.only_marked.toggled.connect(self._apply_filters)

        # --- table + bulk panel
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.table.setItemDelegateForColumn(COL_TARGET, TargetDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for col, width in (
            (COL_PATH, 480),
            (COL_MOD, 230),
            (COL_TYPE, 120),
            (COL_RES, 100),
            (COL_STATUS, 150),
        ):
            self.table.setColumnWidth(col, width)
        self.table.sortByColumn(COL_RES, Qt.SortOrder.DescendingOrder)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        splitter.addWidget(self.table)
        splitter.addWidget(self._build_bulk_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([1150, 350])
        root.addWidget(splitter, 1)

        # --- bottom bar
        bottom = QHBoxLayout()
        self.summary = QLabel()
        self.run_btn = QPushButton("Downscale...")
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._run)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(QLabel(f"Output mod: <b>{self.output_name}</b>"))
        bottom.addWidget(self.run_btn)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

    def _build_bulk_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)

        hint = QLabel(
            "Bulk actions apply to the textures <b>currently shown</b> by the filters. "
            "Use the Mod / search filters to target a single mod or folder."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Size rule
        rule_box = QGroupBox("Size rule")
        rule = QFormLayout(rule_box)
        self.rule_above = _size_combo(TARGET_CHOICES, default=2048)
        self.rule_target = _size_combo(TARGET_CHOICES, default=2048)
        rule.addRow("Textures larger than:", self.rule_above)
        rule.addRow("Downscale to:", self.rule_target)
        btn = QPushButton("Apply to shown")
        btn.clicked.connect(self._apply_size_rule)
        rule.addRow(btn)
        layout.addWidget(rule_box)

        # Per type limits
        type_box = QGroupBox("Max size per texture type")
        tf = QFormLayout(type_box)
        self.type_limit_combos: dict[TextureType, QComboBox] = {}
        for t in TextureType:
            combo = _size_combo(TARGET_CHOICES, none_label="No limit")
            self.type_limit_combos[t] = combo
            tf.addRow(f"{t.value}:", combo)
        btn = QPushButton("Apply to shown")
        btn.clicked.connect(self._apply_type_limits)
        tf.addRow(btn)
        layout.addWidget(type_box)

        # Selection
        sel_box = QGroupBox("Selected rows")
        sl = QVBoxLayout(sel_box)
        row = QHBoxLayout()
        self.sel_target = _size_combo(TARGET_CHOICES, default=2048)
        set_btn = QPushButton("Set")
        set_btn.clicked.connect(lambda: self._set_selected(self.sel_target.currentData()))
        row.addWidget(self.sel_target, 1)
        row.addWidget(set_btn)
        sl.addLayout(row)
        keep_btn = QPushButton("Keep original (clear target)")
        keep_btn.clicked.connect(lambda: self._set_selected(None))
        sl.addWidget(keep_btn)
        tip = QLabel("Tip: double-click a Target cell, or right-click rows for more options.")
        tip.setWordWrap(True)
        sl.addWidget(tip)
        layout.addWidget(sel_box)

        clear_btn = QPushButton("Clear targets of all shown textures")
        clear_btn.clicked.connect(lambda: self._set_rows(self._visible_rows(), None))
        layout.addWidget(clear_btn)
        layout.addStretch(1)
        return panel

    # ============================================================================ scanning ====

    def _start_scan(self) -> None:
        if self.scan_worker is not None and self.scan_worker.isRunning():
            return
        self.rescan_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.scan_bar.setVisible(True)
        self.scan_bar.setRange(0, 0)
        self.scan_label.setText(f"Scanning {len(self.sources)} enabled mods...")
        self.model.set_entries([])
        worker = ScanWorker(self.sources, self)
        worker.progress.connect(self._on_scan_progress)
        worker.done.connect(self._on_scan_done)
        worker.failed.connect(self._on_scan_failed)
        self.scan_worker = worker
        worker.start()

    def _on_scan_progress(self, done: int, total: int, msg: str) -> None:
        self.scan_bar.setRange(0, max(total, 1))
        self.scan_bar.setValue(done)
        self.scan_label.setText(msg)

    def _on_scan_done(self, entries: list[TextureEntry]) -> None:
        if self.mode is RunMode.SKIP_DONE:
            output.apply_manifest(entries, self.manifest, self.output_dir)
        self.model.set_entries(entries)
        self.table.sortByColumn(
            self.table.horizontalHeader().sortIndicatorSection(),
            self.table.horizontalHeader().sortIndicatorOrder(),
        )
        current_mod = self.mod_combo.currentData()
        self.mod_combo.blockSignals(True)
        self.mod_combo.clear()
        self.mod_combo.addItem("All mods", None)
        for mod in sorted({e.mod for e in entries}, key=str.lower):
            self.mod_combo.addItem(mod, mod)
        self.mod_combo.setCurrentIndex(max(0, self.mod_combo.findData(current_mod)))
        self.mod_combo.blockSignals(False)
        self._apply_filters()

        self.scan_bar.setVisible(False)
        self.rescan_btn.setEnabled(True)
        self.run_btn.setEnabled(True)
        self.scan_label.setText(
            f"Found {len(entries)} textures (winning files only) in {len(self.sources)} enabled mods."
        )

    def _on_scan_failed(self, tb: str) -> None:
        self.scan_bar.setVisible(False)
        self.rescan_btn.setEnabled(True)
        self.scan_label.setText("Scan failed.")
        QMessageBox.critical(self, "Scan failed", tb)

    # ============================================================================ filters ====

    def _apply_filters(self) -> None:
        self.proxy.set_filters(
            min_dim=self.hide_combo.currentData() or 0,
            mod=self.mod_combo.currentData(),
            hidden_mods=frozenset(self.hidden_mods),
            ttype=self.type_combo.currentData(),
            text=self.search.text(),
            only_targeted=self.only_marked.isChecked(),
        )
        self._update_summary()

    def _visible_rows(self) -> list[int]:
        return [self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in range(self.proxy.rowCount())]

    def _selected_rows(self) -> list[int]:
        return sorted({self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()})

    def _update_summary(self, *_args) -> None:
        entries = self.model.entries
        marked = [e for e in entries if e.target is not None]
        before = sum(e.size for e in marked)
        after = sum(estimated_size(e.size, e.info, e.target) for e in marked)
        text = f"Showing {self.proxy.rowCount()} of {len(entries)} textures  |  {len(marked)} marked"
        if marked:
            text += f"  |  {fmt_bytes(before)} -> ~{fmt_bytes(after)} (saves ~{fmt_bytes(before - after)})"
        self.summary.setText(text)

    # ======================================================================= bulk actions ====

    def _set_rows(self, rows: list[int], target: int | None) -> None:
        self.model.set_target(rows, target)
        if self.only_marked.isChecked():
            self._apply_filters()
        self._update_summary()

    def _set_selected(self, target: int | None) -> None:
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Nothing selected", "Select one or more rows in the table first.")
            return
        self._set_rows(rows, target)

    def _apply_size_rule(self) -> None:
        above, target = self.rule_above.currentData(), self.rule_target.currentData()
        rows = [r for r in self._visible_rows() if self.model.entries[r].max_dim > above]
        self._set_rows(rows, target)
        self.scan_label.setText(
            f"Size rule: {len(rows)} shown textures larger than {fmt_dim(above)} set to {fmt_dim(target)}."
        )

    def _apply_type_limits(self) -> None:
        count = 0
        for t, combo in self.type_limit_combos.items():
            limit = combo.currentData()
            if limit is None:
                continue
            rows = [
                r
                for r in self._visible_rows()
                if self.model.entries[r].ttype is t and self.model.entries[r].max_dim > limit
            ]
            count += len(rows)
            self.model.set_target(rows, limit)
        if self.only_marked.isChecked():
            self._apply_filters()
        self._update_summary()
        self.scan_label.setText(f"Type limits applied to {count} shown textures.")

    def _context_menu(self, pos) -> None:
        index: QModelIndex = self.table.indexAt(pos)
        if not index.isValid():
            return
        entry: TextureEntry = index.data(ENTRY_ROLE)
        rows = self._selected_rows()
        menu = QMenu(self)

        target_menu = menu.addMenu(f"Set target for {len(rows)} selected")
        for c in TARGET_CHOICES:
            act = QAction(fmt_dim(c), target_menu)
            act.triggered.connect(lambda _=False, c=c: self._set_selected(c))
            target_menu.addAction(act)
        menu.addAction("Keep original (clear target)", lambda: self._set_selected(None))
        menu.addSeparator()

        selected_mods = sorted({self.model.entries[r].mod for r in rows} | {entry.mod}, key=str.lower)
        hide_label = (
            f'Hide mod "{selected_mods[0]}"' if len(selected_mods) == 1 else f"Hide {len(selected_mods)} mods"
        )
        menu.addAction(hide_label, lambda: self._hide_mods(selected_mods))
        if self.hidden_mods:
            menu.addAction(f"Unhide all mods ({len(self.hidden_mods)})", self._unhide_all_mods)
        menu.addAction(f'Show only mod "{entry.mod}"', lambda: self._filter_mod(entry.mod))
        folder = entry.virtual_path.rsplit("/", 1)[0] + "/"
        menu.addAction(f'Show only folder "{folder}"', lambda: self._filter_folder(folder))
        menu.addAction("Reset filters", self._reset_filters)
        menu.addSeparator()
        menu.addAction("Open containing folder", lambda: os.startfile(entry.abs_path.parent))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _filter_mod(self, mod: str) -> None:
        self.mod_combo.setCurrentIndex(max(0, self.mod_combo.findData(mod)))

    def _hide_mods(self, mods: list[str]) -> None:
        self.hidden_mods.update(mods)
        if self.mod_combo.currentData() in self.hidden_mods:
            self.mod_combo.blockSignals(True)
            self.mod_combo.setCurrentIndex(0)
            self.mod_combo.blockSignals(False)
        self._update_unhide_button()
        self._apply_filters()

    def _unhide_all_mods(self) -> None:
        self.hidden_mods.clear()
        self._update_unhide_button()
        self._apply_filters()

    def _update_unhide_button(self) -> None:
        n = len(self.hidden_mods)
        self.unhide_btn.setText(f"Unhide {n} mod{'s' if n != 1 else ''}")
        self.unhide_btn.setToolTip("Hidden: " + ", ".join(sorted(self.hidden_mods, key=str.lower)))
        self.unhide_btn.setVisible(n > 0)

    def _filter_folder(self, folder: str) -> None:
        self.search.setText(folder.lower())

    def _reset_filters(self) -> None:
        self.search.clear()
        self.mod_combo.setCurrentIndex(0)
        self.type_combo.setCurrentIndex(0)
        self.only_marked.setChecked(False)
        self._unhide_all_mods()

    # ================================================================================ run ====

    def _run(self) -> None:
        entries = self.model.entries
        plan = output.make_plan(entries, self.manifest, self.output_dir, self.mode)
        if plan.is_empty:
            msg = "Nothing to do."
            if plan.skipped:
                msg += f" All {plan.skipped} marked textures are already downscaled."
            else:
                msg += " Mark some textures with a target size first."
            QMessageBox.information(self, "Texture Downscaler", msg)
            return

        lines = [f"Downscale <b>{len(plan.jobs)}</b> textures into the mod <b>{self.output_name}</b>."]
        if plan.skipped:
            lines.append(f"Skip {plan.skipped} textures that are already done.")
        if plan.remove:
            lines.append(
                f"Delete {len(plan.remove)} previously downscaled textures (no longer wanted, "
                f"source changed/removed, or rebuild)."
            )
        lines.append("<br>Original mod files are not modified.")
        if (
            QMessageBox.question(self, "Start downscaling?", "<br>".join(lines))
            != QMessageBox.StandardButton.Yes
        ):
            return

        output_dir = self.ensure_output_mod()
        if output_dir is None:
            return
        self.output_dir = output_dir

        progress = QProgressDialog("Downscaling textures...", "Cancel", 0, max(1, len(plan.jobs)), self)
        progress.setWindowTitle("Texture Downscaler")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = DownscaleWorker(
            plan, {e.key: e for e in entries}, self.manifest, self.output_dir, self.workers, self
        )
        worker.progress.connect(
            lambda n, total, name: (
                progress.setMaximum(max(1, total)),
                progress.setValue(n),
                progress.setLabelText(f"Downscaling textures... {n}/{total}\n{name}"),
            )
        )
        progress.canceled.connect(lambda: setattr(worker, "cancel", True))
        worker.done.connect(lambda results: self._on_run_done(plan, results, progress))
        worker.failed.connect(
            lambda tb: (progress.close(), QMessageBox.critical(self, "Downscale failed", tb))
        )
        worker.start()

    def _on_run_done(self, plan: output.Plan, results: list[JobResult], progress: QProgressDialog) -> None:
        progress.close()
        by_key = {e.key: e for e in self.model.entries}
        for key in plan.remove:
            if key in by_key:
                by_key[key].done_target = None
                by_key[key].status = ""
        failed: list[JobResult] = []
        cancelled = 0
        for r in results:
            e = by_key[r.job.key]
            if r.ok:
                e.done_target = e.target
                e.status = f"Done ({r.job.width}x{r.job.height})"
            elif r.message == "Cancelled":
                cancelled += 1
            else:
                e.status = f"Failed: {r.message}"
                failed.append(r)
        self.model.refresh_rows()
        self.mode = RunMode.SKIP_DONE  # outputs on disk now match the manifest
        self.on_output_written()

        ok = len(results) - len(failed) - cancelled
        msg = f"Downscaled {ok} textures."
        if cancelled:
            msg += f"\n{cancelled} were cancelled."
        if failed:
            msg += f"\n{len(failed)} failed (see the Status column)."
        msg += (
            f"\n\nMake sure '{self.output_name}' is enabled and placed at the bottom of your mod list "
            "so it overrides the original textures."
        )
        box = QMessageBox(
            QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information,
            "Texture Downscaler",
            msg,
            parent=self,
        )
        if failed:
            box.setDetailedText("\n".join(f"{r.job.source}: {r.message}" for r in failed[:500]))
        box.exec()

    # ============================================================================= close ====

    def done(self, result: int) -> None:  # covers Close button, Esc and the window X
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.cancel = True
            self.scan_worker.wait(10000)
        super().done(result)
