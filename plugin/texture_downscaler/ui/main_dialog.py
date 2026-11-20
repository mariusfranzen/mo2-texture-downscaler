"""Main window: scan results table, filters, bulk target tools, and the downscale run."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from PyQt6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QModelIndex, Qt, QTimer
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import output
from ..core.downscaler import JobResult, estimated_size
from ..core.nexus import UNKNOWN, AuthorCache, NexusRef
from ..core.output import Manifest, RunMode
from ..core.scanner import TextureEntry, TextureSource
from ..core.texture_types import TextureType
from .table_model import (
    COL_AUTHOR,
    COL_MOD,
    COL_PATH,
    COL_RES,
    COL_STATUS,
    COL_TARGET,
    COL_TYPE,
    ENTRY_ROLE,
    HEADERS,
    TARGET_CHOICES,
    TextureFilterProxy,
    TextureTableModel,
    fmt_bytes,
    fmt_dim,
)
from .workers import AuthorWorker, DownscaleWorker, ScanWorker

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
        combo.addItem("- keep original size", None)
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
        hidden_mods: Iterable[str] = (),
        on_hidden_mods_changed: Callable[[set[str]], None] | None = None,
        header_cache_path: Path | None = None,
        sort_state: dict | None = None,
        on_sort_changed: Callable[[dict], None] | None = None,
        show_authors: bool = True,
        mod_refs: dict[str, NexusRef | None] | None = None,
        author_cache_path: Path | None = None,
        api_key_provider: Callable[[], str | None] | None = None,
        hidden_authors: Iterable[str] = (),
        on_hidden_authors_changed: Callable[[set[str]], None] | None = None,
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
        self.on_hidden_mods_changed = on_hidden_mods_changed
        self.header_cache_path = header_cache_path
        self.on_sort_changed = on_sort_changed
        self._sort_state = dict(sort_state or {})
        self._initial_hidden_mods = set(hidden_mods)
        self.hidden_authors: set[str] = set(hidden_authors)
        self._saved_hidden_authors = set(hidden_authors)
        self.on_hidden_authors_changed = on_hidden_authors_changed
        self.mod_refs = mod_refs or {}
        self.author_cache_path = author_cache_path
        self.api_key_provider = api_key_provider
        self.show_authors = show_authors
        self.author_worker: AuthorWorker | None = None
        self._pending_authors: dict[str, str] = {}
        self._mods_by_ref: dict[str, list[str]] = {}
        self._author_timer = QTimer(self)
        self._author_timer.setInterval(1000)
        self._author_timer.timeout.connect(self._flush_authors)
        self.manifest = Manifest.load(output_dir)
        self.scan_worker: ScanWorker | None = None

        self.model = TextureTableModel(self)
        self.proxy = TextureFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.model.dataChanged.connect(self._update_summary)
        self.model.modelReset.connect(self._update_summary)

        self._build_ui()
        self._start_scan()
        if show_authors:
            self._start_author_lookup()
        else:
            self.table.setColumnHidden(COL_AUTHOR, True)

    # ================================================================================== UI ====

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- scan status line
        status_row = QHBoxLayout()
        self.scan_label = QLabel("Scanning...")
        self.scan_bar = QProgressBar()
        self.scan_bar.setMaximumWidth(300)
        self.author_label = QLabel()
        self.author_label.setVisible(False)
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self._start_scan)
        status_row.addWidget(self.scan_label, 1)
        status_row.addWidget(self.scan_bar)
        status_row.addWidget(self.author_label)
        status_row.addWidget(QLabel("Sort: click a column header, then by:"))
        self.secondary_combo = QComboBox()
        self.secondary_combo.addItem("(nothing)", None)
        for col, name in enumerate(HEADERS):
            self.secondary_combo.addItem(name, col)
        self.secondary_combo.setToolTip("Secondary sort column. Tip: Ctrl+click a column header to set it.")
        self.secondary_order = QPushButton()
        self.secondary_order.setCheckable(True)
        self.secondary_order.setFixedWidth(110)
        self.secondary_order.setToolTip("Direction of the secondary sort")
        self.secondary_combo.currentIndexChanged.connect(self._apply_secondary_sort)
        self.secondary_order.toggled.connect(self._apply_secondary_sort)
        status_row.addWidget(self.secondary_combo)
        status_row.addWidget(self.secondary_order)
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
        self.only_marked.setToolTip("Show only textures marked for downscaling")
        self.hide_done = QCheckBox("Hide already downscaled")
        self.hide_done.setToolTip(
            "Hide textures that were already downscaled to the size they are marked for"
        )
        self.hidden_mods: set[str] = set(self._initial_hidden_mods)
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
        filters.addWidget(self.hide_done)
        filters.addWidget(self.unhide_btn)
        root.addLayout(filters)

        self.search.textChanged.connect(self._apply_filters)
        for combo in (self.mod_combo, self.type_combo, self.hide_combo):
            combo.currentIndexChanged.connect(self._apply_filters)
        self.only_marked.toggled.connect(self._apply_filters)
        self.hide_done.toggled.connect(self._apply_filters)
        self._update_unhide_button()

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
            (COL_AUTHOR, 130),
            (COL_TYPE, 120),
            (COL_RES, 100),
            (COL_STATUS, 150),
        ):
            self.table.setColumnWidth(col, width)
        self._restore_sort()
        self.table.horizontalHeader().viewport().installEventFilter(self)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._save_sort)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        splitter.addWidget(self.table)
        splitter.addWidget(self._build_side_panel())
        self.table.selectionModel().selectionChanged.connect(self._update_selection_label)
        self.model.dataChanged.connect(self._update_selection_label)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([1150, 350])
        root.addWidget(splitter, 1)

        # --- bottom bar
        bottom = QHBoxLayout()
        self.summary = QLabel()
        self.run_btn = QPushButton("Downscale marked textures...")
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._run)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(QLabel(f"Output mod: <b>{self.output_name}</b>"))
        bottom.addWidget(self.run_btn)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

    def _build_side_panel(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_basic_tab(), "Basic")
        tabs.addTab(self._build_batch_tab(), "Batch")
        tabs.setMinimumWidth(300)
        return tabs

    def _build_basic_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        steps = QLabel(
            "<b>1.</b> Select textures in the table (Ctrl / Shift-click, Ctrl+A for all shown).<br>"
            "<b>2.</b> Choose a size and click <b>Mark for downscaling</b>. Marked rows turn blue.<br>"
            "<b>3.</b> Click <b>Downscale marked textures</b> at the bottom. Nothing is written until then."
        )
        steps.setWordWrap(True)
        layout.addWidget(steps)

        box = QGroupBox("Selected textures")
        bl = QVBoxLayout(box)
        self.selection_label = QLabel("No textures selected.")
        self.selection_label.setWordWrap(True)
        bl.addWidget(self.selection_label)
        form = QFormLayout()
        self.sel_target = _size_combo(TARGET_CHOICES, default=2048)
        form.addRow("Downscale to:", self.sel_target)
        bl.addLayout(form)
        mark_btn = QPushButton("Mark for downscaling")
        mark_btn.clicked.connect(lambda: self._set_selected(self.sel_target.currentData()))
        bl.addWidget(mark_btn)
        keep_btn = QPushButton("Unmark (keep original size)")
        keep_btn.clicked.connect(lambda: self._set_selected(None))
        bl.addWidget(keep_btn)
        layout.addWidget(box)

        tip = QLabel(
            "Tips: double-click a <i>Downscale to</i> cell to change one texture. Right-click rows to hide a mod, "
            "deselect a texture type, or filter to a mod/folder. Textures already at or below the "
            "chosen size are left alone."
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch(1)
        return page

    def _build_batch_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel(
            "Batch actions apply to <b>all textures currently shown</b> by the filters, not only the "
            "selected ones. Use the Mod / Type / search filters or hide mods to narrow them down."
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
        btn = QPushButton("Mark shown for downscaling")
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
        btn = QPushButton("Mark shown for downscaling")
        btn.clicked.connect(self._apply_type_limits)
        tf.addRow(btn)
        layout.addWidget(type_box)

        clear_btn = QPushButton("Unmark all shown (keep original size)")
        clear_btn.clicked.connect(lambda: self._set_rows(self._visible_rows(), None))
        layout.addWidget(clear_btn)
        layout.addStretch(1)
        return page

    def _update_selection_label(self, *_args) -> None:
        rows = self._selected_rows()
        if not rows:
            self.selection_label.setText("No textures selected.")
            return
        entries = [self.model.entries[r] for r in rows]
        mods = {e.mod for e in entries}
        marked = sum(e.target is not None for e in entries)
        self.selection_label.setText(
            f"<b>{len(rows)}</b> selected from {len(mods)} mod{'s' if len(mods) != 1 else ''}"
            f" ({marked} already marked), {fmt_bytes(sum(e.size for e in entries))}."
        )

    # ============================================================================= sorting ====

    def _apply_secondary_sort(self, *_args) -> None:
        desc = self.secondary_order.isChecked()
        self.secondary_order.setText("Descending ▼" if desc else "Ascending ▲")
        self.proxy.set_secondary_sort(self.secondary_combo.currentData(), desc)
        self._save_sort()

    def _restore_sort(self) -> None:
        """Apply the sort saved from the last session (defaults: resolution, largest first)."""
        state = self._sort_state
        # Columns are saved by name so adding columns doesn't scramble a saved sort.
        col = HEADERS.index(state["column"]) if state.get("column") in HEADERS else COL_RES
        sec = HEADERS.index(state["secondary"]) if state.get("secondary") in HEADERS else None
        self.secondary_combo.blockSignals(True)
        self.secondary_order.blockSignals(True)
        self.secondary_combo.setCurrentIndex(max(0, self.secondary_combo.findData(sec)))
        self.secondary_order.setChecked(bool(state.get("secondary_desc", False)))
        self.secondary_combo.blockSignals(False)
        self.secondary_order.blockSignals(False)
        desc = self.secondary_order.isChecked()
        self.secondary_order.setText("Descending ▼" if desc else "Ascending ▲")
        self.proxy.secondary_col, self.proxy.secondary_desc = sec, desc
        order = Qt.SortOrder.DescendingOrder if state.get("descending", True) else Qt.SortOrder.AscendingOrder
        self.table.sortByColumn(col, order)

    def _save_sort(self, *_args) -> None:
        header = self.table.horizontalHeader()
        col = header.sortIndicatorSection()
        sec = self.secondary_combo.currentData()
        state = {
            "column": HEADERS[col] if 0 <= col < len(HEADERS) else None,
            "descending": header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder,
            "secondary": HEADERS[sec] if sec is not None else None,
            "secondary_desc": self.secondary_order.isChecked(),
        }
        if state != self._sort_state:
            self._sort_state = state
            if self.on_sort_changed is not None:
                self.on_sort_changed(dict(state))

    def set_secondary_column(self, col: int) -> None:
        """Ctrl+click on a header: make it the secondary sort, or flip its direction."""
        if self.secondary_combo.currentData() == col:
            self.secondary_order.toggle()
        else:
            self.secondary_combo.setCurrentIndex(self.secondary_combo.findData(col))

    def eventFilter(self, obj, event) -> bool:
        header = self.table.horizontalHeader()
        if (
            obj is header.viewport()
            and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
            and event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if event.type() == QEvent.Type.MouseButtonRelease:
                col = header.logicalIndexAt(event.position().toPoint())
                if col >= 0:
                    self.set_secondary_column(col)
            return True  # swallow so the primary sort doesn't change
        return super().eventFilter(obj, event)

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
        worker = ScanWorker(self.sources, self.header_cache_path, self)
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
            hidden_authors=frozenset(self.hidden_authors),
            ttype=self.type_combo.currentData(),
            text=self.search.text(),
            only_targeted=self.only_marked.isChecked(),
            hide_done=self.hide_done.isChecked(),
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
            text += (
                f"  |  VRAM if all loaded: {fmt_bytes(before)} -> ~{fmt_bytes(after)}"
                f" (-{fmt_bytes(before - after)})  |  output mod adds ~{fmt_bytes(after)} on disk"
            )
        self.summary.setText(text)
        self.summary.setToolTip(
            "VRAM: a compressed texture takes about as much video memory as its file size. The saving "
            "is an upper bound - the game only loads textures for what is around you.\n"
            "Disk: the original files stay in their mods; the downscaled copies are added to the "
            "output mod, so disk usage goes up by the new size."
        )
        self.run_btn.setText(
            f"Downscale {len(marked)} marked texture{'s' if len(marked) != 1 else ''}..."
            if marked
            else "Downscale marked textures..."
        )

    # ======================================================================= bulk actions ====

    def _set_rows(self, rows: list[int], target: int | None) -> None:
        self.model.set_target(rows, target)
        if self.only_marked.isChecked() or self.hide_done.isChecked():
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
            f"Size rule: marked {len(rows)} shown textures larger than {fmt_dim(above)} for downscaling to {fmt_dim(target)}."
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
        if self.only_marked.isChecked() or self.hide_done.isChecked():
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

        target_menu = menu.addMenu(f"Mark {len(rows)} selected for downscaling to")
        for c in TARGET_CHOICES:
            act = QAction(fmt_dim(c), target_menu)
            act.triggered.connect(lambda _=False, c=c: self._set_selected(c))
            target_menu.addAction(act)
        menu.addAction("Unmark (keep original size)", lambda: self._set_selected(None))
        type_counts: dict[TextureType, int] = {}
        for r in rows:
            t = self.model.entries[r].ttype
            type_counts[t] = type_counts.get(t, 0) + 1
        if len(type_counts) > 1:
            deselect_menu = menu.addMenu("Deselect by type")
            only_menu = menu.addMenu("Keep only type selected")
            for t in TextureType:
                if t in type_counts:
                    label = f"{t.value} ({type_counts[t]})"
                    deselect_menu.addAction(label, lambda t=t: self._deselect_where(lambda e: e.ttype is t))
                    only_menu.addAction(label, lambda t=t: self._deselect_where(lambda e: e.ttype is not t))
        menu.addSeparator()

        selected_mods = sorted({self.model.entries[r].mod for r in rows} | {entry.mod}, key=str.lower)
        hide_label = (
            f'Hide mod "{selected_mods[0]}"' if len(selected_mods) == 1 else f"Hide {len(selected_mods)} mods"
        )
        menu.addAction(hide_label, lambda: self._hide_mods(selected_mods))
        selected_authors = sorted(
            {a for m in selected_mods if (a := self.model.mod_authors.get(m))}, key=str.lower
        )
        if selected_authors:
            author_label = (
                f'Hide author "{selected_authors[0]}"'
                if len(selected_authors) == 1
                else f"Hide {len(selected_authors)} authors"
            )
            menu.addAction(author_label, lambda: self._hide_authors(selected_authors))
        if self.hidden_mods or self.hidden_authors:
            menu.addAction(f"Unhide all ({self._hidden_summary()})", self._unhide_all_mods)
        menu.addAction(f'Show only mod "{entry.mod}"', lambda: self._filter_mod(entry.mod))
        folder = entry.virtual_path.rsplit("/", 1)[0] + "/"
        menu.addAction(f'Show only folder "{folder}"', lambda: self._filter_folder(folder))
        menu.addAction("Reset filters", self._reset_filters)
        menu.addSeparator()
        menu.addAction("Open containing folder", lambda: os.startfile(entry.abs_path.parent))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _deselect_where(self, predicate: Callable[[TextureEntry], bool]) -> None:
        """Remove selected rows whose entry matches `predicate` from the table selection."""
        sel_model = self.table.selectionModel()
        last_col = self.proxy.columnCount() - 1
        proxy_rows = sorted(
            i.row()
            for i in sel_model.selectedRows()
            if predicate(self.model.entries[self.proxy.mapToSource(i).row()])
        )
        selection = QItemSelection()
        start = prev = None
        for row in [*proxy_rows, None]:  # merge contiguous rows into ranges
            if row is not None and prev is not None and row == prev + 1:
                prev = row
                continue
            if start is not None:
                selection.select(self.proxy.index(start, 0), self.proxy.index(prev, last_col))
            start = prev = row
        sel_model.select(
            selection, QItemSelectionModel.SelectionFlag.Deselect | QItemSelectionModel.SelectionFlag.Rows
        )

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

    def _hide_authors(self, authors: list[str]) -> None:
        self.hidden_authors.update(authors)
        self._update_unhide_button()
        self._apply_filters()

    def _unhide_all_mods(self) -> None:
        """Unhide all hidden mods and authors."""
        self.hidden_mods.clear()
        self.hidden_authors.clear()
        self._update_unhide_button()
        self._apply_filters()

    def _hidden_summary(self) -> str:
        parts = []
        if n := len(self.hidden_mods):
            parts.append(f"{n} mod{'s' if n != 1 else ''}")
        if n := len(self.hidden_authors):
            parts.append(f"{n} author{'s' if n != 1 else ''}")
        return ", ".join(parts)

    def _update_unhide_button(self) -> None:
        if self.on_hidden_mods_changed is not None and self.hidden_mods != self._initial_hidden_mods:
            self._initial_hidden_mods = set(self.hidden_mods)
            self.on_hidden_mods_changed(set(self.hidden_mods))
        if self.on_hidden_authors_changed is not None and self.hidden_authors != self._saved_hidden_authors:
            self._saved_hidden_authors = set(self.hidden_authors)
            self.on_hidden_authors_changed(set(self.hidden_authors))
        self.unhide_btn.setText(f"Unhide {self._hidden_summary()}")
        tooltip = []
        if self.hidden_mods:
            tooltip.append("Hidden mods: " + ", ".join(sorted(self.hidden_mods, key=str.lower)))
        if self.hidden_authors:
            tooltip.append("Hidden authors: " + ", ".join(sorted(self.hidden_authors, key=str.lower)))
        self.unhide_btn.setToolTip("\n".join(tooltip))
        self.unhide_btn.setVisible(bool(self.hidden_mods or self.hidden_authors))

    # ============================================================================ authors ====

    def _start_author_lookup(self) -> None:
        """Fill the Author column from the cache, then look up the rest on Nexus in the background."""
        cache = AuthorCache.load(self.author_cache_path) if self.author_cache_path else AuthorCache()
        authors: dict[str, str] = {}
        missing: list[NexusRef] = []
        for mod, ref in self.mod_refs.items():
            if ref is None:
                authors[mod] = UNKNOWN
                continue
            self._mods_by_ref.setdefault(ref.key, []).append(mod)
            cached = cache.get(ref)
            if cached is None:
                missing.append(ref)
            else:
                authors[mod] = cached or UNKNOWN
        self.model.set_mod_authors(authors)
        missing = list(dict.fromkeys(missing))
        if not missing:
            return
        if self.api_key_provider is None:
            self.model.set_mod_authors({m: UNKNOWN for r in missing for m in self._mods_by_ref[r.key]})
            return

        self.author_label.setText(f"Looking up {len(missing)} mod authors on Nexus...")
        self.author_label.setVisible(True)
        worker = AuthorWorker(missing, cache, self.api_key_provider, self)
        worker.found.connect(self._on_author_found)
        worker.progress.connect(
            lambda n, total: self.author_label.setText(f"Looking up mod authors on Nexus: {n}/{total}")
        )
        worker.finished_msg.connect(self._on_authors_finished)
        self._author_timer.start()
        self.author_worker = worker
        worker.start()

    def _on_author_found(self, ref_key: str, author: str) -> None:
        for mod in self._mods_by_ref.get(ref_key, []):
            self._pending_authors[mod] = author or UNKNOWN

    def _flush_authors(self) -> None:
        """Apply looked-up authors in batches (re-filtering 60k rows per result would be slow)."""
        if not self._pending_authors:
            return
        self.model.set_mod_authors(self._pending_authors)
        self._pending_authors = {}
        if self.hidden_authors or self.search.text():
            self._apply_filters()

    def _on_authors_finished(self, msg: str) -> None:
        self._author_timer.stop()
        self._flush_authors()
        unresolved = {
            m: UNKNOWN for mods in self._mods_by_ref.values() for m in mods if m not in self.model.mod_authors
        }
        if unresolved:
            self.model.set_mod_authors(unresolved)
        self.author_label.setText(msg.splitlines()[0] if msg else "")
        self.author_label.setToolTip(msg)
        self.author_label.setVisible(bool(msg))

    def _filter_folder(self, folder: str) -> None:
        self.search.setText(folder.lower())

    def _reset_filters(self) -> None:
        self.search.clear()
        self.mod_combo.setCurrentIndex(0)
        self.type_combo.setCurrentIndex(0)
        self.only_marked.setChecked(False)
        self.hide_done.setChecked(False)
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
                msg += " Mark some textures for downscaling first."
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
        self._apply_filters()  # newly done rows may now be hidden
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
        if self.author_worker is not None and self.author_worker.isRunning():
            self.author_worker.cancel = True
            self.author_worker.wait(20000)  # finishes the current request, then saves the cache
        super().done(result)
