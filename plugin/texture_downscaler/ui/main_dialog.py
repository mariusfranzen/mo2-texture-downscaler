"""Main window: scan results table, filters, bulk target tools, and the downscale run."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from PyQt6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QModelIndex, Qt, QTimer
from PyQt6.QtGui import QAction, QIcon
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

from .. import ICON_PATH
from ..core import output
from ..core.downscaler import JobResult, estimated_size
from ..core.i18n import tr
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
    column_title,
    fmt_bytes,
    fmt_dim,
    type_name,
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
        combo.addItem(tr("editor.keep"), None)
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
        authors_enabled: bool = True,
        auto_fetch_authors: bool = True,
        mod_refs: dict[str, NexusRef | None] | None = None,
        author_cache_path: Path | None = None,
        hidden_authors: Iterable[str] = (),
        on_hidden_authors_changed: Callable[[set[str]], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Texture Downscaler")
        if ICON_PATH.is_file():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
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
        self.auto_fetch_authors = auto_fetch_authors
        self.authors_enabled = authors_enabled
        self._author_queue: list[NexusRef] = []
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
        self.model.authors_enabled = authors_enabled
        if authors_enabled:
            self._init_authors()
        else:
            self.table.setColumnHidden(COL_AUTHOR, True)

    # ================================================================================== UI ====

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- scan status line
        status_row = QHBoxLayout()
        self.scan_label = QLabel(tr("scan.scanning", count=len(self.sources)))
        self.scan_bar = QProgressBar()
        self.scan_bar.setMaximumWidth(300)
        self.author_label = QLabel()
        self.author_label.setVisible(False)
        self.rescan_btn = QPushButton(tr("scan.rescan"))
        self.rescan_btn.clicked.connect(self._start_scan)
        status_row.addWidget(self.scan_label, 1)
        status_row.addWidget(self.scan_bar)
        status_row.addWidget(self.author_label)
        status_row.addWidget(QLabel(tr("sort.label")))
        self.secondary_combo = QComboBox()
        self.secondary_combo.addItem(tr("sort.nothing"), None)
        for col in range(len(HEADERS)):
            if col != COL_AUTHOR or self.authors_enabled:
                self.secondary_combo.addItem(column_title(col), col)
        self.secondary_combo.setToolTip(tr("sort.tip_secondary"))
        self.secondary_order = QPushButton()
        self.secondary_order.setCheckable(True)
        self.secondary_order.setFixedWidth(110)
        self.secondary_order.setToolTip(tr("sort.tip_direction"))
        self.secondary_combo.currentIndexChanged.connect(self._apply_secondary_sort)
        self.secondary_order.toggled.connect(self._apply_secondary_sort)
        status_row.addWidget(self.secondary_combo)
        status_row.addWidget(self.secondary_order)
        status_row.addWidget(self.rescan_btn)
        root.addLayout(status_row)

        if self.mode is RunMode.REBUILD and self.manifest.entries:
            warn = QLabel(tr("rebuild_warning", count=len(self.manifest.entries), mod=self.output_name))
            warn.setWordWrap(True)
            root.addWidget(warn)

        # --- filters
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("filter.search"))
        self.search.setClearButtonEnabled(True)
        self.mod_combo = QComboBox()
        self.mod_combo.setMinimumWidth(220)
        self.mod_combo.addItem(tr("filter.all_mods"), None)
        self.type_combo = QComboBox()
        self.type_combo.addItem(tr("filter.all_types"), None)
        for t in TextureType:
            self.type_combo.addItem(type_name(t), t)
        self.hide_combo = QComboBox()
        for d in HIDE_SMALLER_CHOICES:
            self.hide_combo.addItem(
                tr("filter.all_sizes") if d == 0 else tr("filter.hide_smaller", size=fmt_dim(d)), d
            )
        self.hide_combo.setCurrentIndex(self.hide_combo.findData(2048))
        self.only_marked = QCheckBox(tr("filter.only_marked"))
        self.only_marked.setToolTip(tr("filter.tip_only_marked"))
        self.hide_done = QCheckBox(tr("filter.hide_done"))
        self.hide_done.setToolTip(tr("filter.tip_hide_done"))
        self.hidden_mods: set[str] = set(self._initial_hidden_mods)
        self.unhide_btn = QPushButton()
        self.unhide_btn.setToolTip(tr("filter.tip_unhide"))
        self.unhide_btn.clicked.connect(self._unhide_all_mods)
        self.unhide_btn.setVisible(False)

        filters.addWidget(self.search, 3)
        filters.addWidget(QLabel(tr("filter.mod")))
        filters.addWidget(self.mod_combo, 2)
        filters.addWidget(QLabel(tr("filter.type")))
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
        self.table.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.horizontalHeader().customContextMenuRequested.connect(self._header_context_menu)
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
        self.run_btn = QPushButton(tr("bottom.downscale_none"))
        self.run_btn.setDefault(True)
        self.run_btn.clicked.connect(self._run)
        close_btn = QPushButton(tr("bottom.close"))
        close_btn.clicked.connect(self.close)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(QLabel(tr("bottom.output_mod", mod=self.output_name)))
        bottom.addWidget(self.run_btn)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

    def _build_side_panel(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._build_basic_tab(), tr("tab.basic"))
        tabs.addTab(self._build_batch_tab(), tr("tab.batch"))
        tabs.setMinimumWidth(300)
        return tabs

    def _build_basic_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        steps = QLabel(tr("basic.steps"))
        steps.setWordWrap(True)
        layout.addWidget(steps)

        box = QGroupBox(tr("basic.selected_box"))
        bl = QVBoxLayout(box)
        self.selection_label = QLabel(tr("basic.none_selected"))
        self.selection_label.setWordWrap(True)
        bl.addWidget(self.selection_label)
        form = QFormLayout()
        self.sel_target = _size_combo(TARGET_CHOICES, default=2048)
        form.addRow(tr("basic.downscale_to"), self.sel_target)
        bl.addLayout(form)
        mark_btn = QPushButton(tr("basic.mark"))
        mark_btn.clicked.connect(lambda: self._set_selected(self.sel_target.currentData()))
        bl.addWidget(mark_btn)
        keep_btn = QPushButton(tr("basic.unmark"))
        keep_btn.clicked.connect(lambda: self._set_selected(None))
        bl.addWidget(keep_btn)
        layout.addWidget(box)

        tip = QLabel(tr("basic.tips"))
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch(1)
        return page

    def _build_batch_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        hint = QLabel(tr("batch.hint"))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Size rule
        rule_box = QGroupBox(tr("batch.size_rule"))
        rule = QFormLayout(rule_box)
        self.rule_above = _size_combo(TARGET_CHOICES, default=2048)
        self.rule_target = _size_combo(TARGET_CHOICES, default=2048)
        rule.addRow(tr("batch.larger_than"), self.rule_above)
        rule.addRow(tr("basic.downscale_to"), self.rule_target)
        btn = QPushButton(tr("batch.mark_shown"))
        btn.clicked.connect(self._apply_size_rule)
        rule.addRow(btn)
        layout.addWidget(rule_box)

        # Per type limits
        type_box = QGroupBox(tr("batch.type_limits"))
        tf = QFormLayout(type_box)
        self.type_limit_combos: dict[TextureType, QComboBox] = {}
        for t in TextureType:
            combo = _size_combo(TARGET_CHOICES, none_label=tr("batch.no_limit"))
            self.type_limit_combos[t] = combo
            tf.addRow(f"{type_name(t)}:", combo)
        btn = QPushButton(tr("batch.mark_shown"))
        btn.clicked.connect(self._apply_type_limits)
        tf.addRow(btn)
        layout.addWidget(type_box)

        clear_btn = QPushButton(tr("batch.unmark_shown"))
        clear_btn.clicked.connect(lambda: self._set_rows(self._visible_rows(), None))
        layout.addWidget(clear_btn)
        layout.addStretch(1)
        return page

    def _update_selection_label(self, *_args) -> None:
        rows = self._selected_rows()
        if not rows:
            self.selection_label.setText(tr("basic.none_selected"))
            return
        entries = [self.model.entries[r] for r in rows]
        mods = {e.mod for e in entries}
        marked = sum(e.target is not None for e in entries)
        self.selection_label.setText(
            tr(
                "basic.selection",
                count=len(rows),
                mods=tr("count.mods", count=len(mods)),
                marked=marked,
                size=fmt_bytes(sum(e.size for e in entries)),
            )
        )

    # ============================================================================= sorting ====

    def _apply_secondary_sort(self, *_args) -> None:
        desc = self.secondary_order.isChecked()
        self.secondary_order.setText(tr("sort.descending") if desc else tr("sort.ascending"))
        self.proxy.set_secondary_sort(self.secondary_combo.currentData(), desc)
        self._save_sort()

    def _restore_sort(self) -> None:
        """Apply the sort saved from the last session (defaults: resolution, largest first)."""
        state = self._sort_state
        # Columns are saved by name so adding columns doesn't scramble a saved sort.
        col = HEADERS.index(state["column"]) if state.get("column") in HEADERS else COL_RES
        sec = HEADERS.index(state["secondary"]) if state.get("secondary") in HEADERS else None
        if not self.authors_enabled:  # don't sort by a hidden column
            col = COL_RES if col == COL_AUTHOR else col
            sec = None if sec == COL_AUTHOR else sec
        self.secondary_combo.blockSignals(True)
        self.secondary_order.blockSignals(True)
        self.secondary_combo.setCurrentIndex(max(0, self.secondary_combo.findData(sec)))
        self.secondary_order.setChecked(bool(state.get("secondary_desc", False)))
        self.secondary_combo.blockSignals(False)
        self.secondary_order.blockSignals(False)
        desc = self.secondary_order.isChecked()
        self.secondary_order.setText(tr("sort.descending") if desc else tr("sort.ascending"))
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
        self.scan_label.setText(tr("scan.scanning", count=len(self.sources)))
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
        self.mod_combo.addItem(tr("filter.all_mods"), None)
        for mod in sorted({e.mod for e in entries}, key=str.lower):
            self.mod_combo.addItem(mod, mod)
        self.mod_combo.setCurrentIndex(max(0, self.mod_combo.findData(current_mod)))
        self.mod_combo.blockSignals(False)
        self._apply_filters()

        self.scan_bar.setVisible(False)
        self.rescan_btn.setEnabled(True)
        self.run_btn.setEnabled(True)
        self.scan_label.setText(
            tr(
                "scan.found",
                textures=tr("count.textures", count=len(entries)),
                mods=tr("count.enabled_mods", count=len(self.sources)),
            )
        )

    def _on_scan_failed(self, tb: str) -> None:
        self.scan_bar.setVisible(False)
        self.rescan_btn.setEnabled(True)
        self.scan_label.setText(tr("scan.failed"))
        QMessageBox.critical(self, tr("scan.failed_title"), tb)

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
        text = tr("summary.showing", shown=self.proxy.rowCount(), total=len(entries), marked=len(marked))
        if marked:
            text += tr(
                "summary.sizes",
                before=fmt_bytes(before),
                after=fmt_bytes(after),
                saved=fmt_bytes(before - after),
            )
        self.summary.setText(text)
        self.summary.setToolTip(tr("summary.tip"))
        self.run_btn.setText(
            tr("bottom.downscale", count=len(marked)) if marked else tr("bottom.downscale_none")
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
            QMessageBox.information(self, tr("nothing_selected.title"), tr("nothing_selected.text"))
            return
        self._set_rows(rows, target)

    def _apply_size_rule(self) -> None:
        above, target = self.rule_above.currentData(), self.rule_target.currentData()
        rows = [r for r in self._visible_rows() if self.model.entries[r].max_dim > above]
        self._set_rows(rows, target)
        self.scan_label.setText(
            tr("batch.size_rule_done", count=len(rows), above=fmt_dim(above), target=fmt_dim(target))
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
        self.scan_label.setText(tr("batch.type_limits_done", count=count))

    def _context_menu(self, pos) -> None:
        index: QModelIndex = self.table.indexAt(pos)
        if not index.isValid():
            return
        entry: TextureEntry = index.data(ENTRY_ROLE)
        rows = self._selected_rows()
        menu = QMenu(self)

        target_menu = menu.addMenu(tr("menu.mark_selected", count=len(rows)))
        for c in TARGET_CHOICES:
            act = QAction(fmt_dim(c), target_menu)
            act.triggered.connect(lambda _=False, c=c: self._set_selected(c))
            target_menu.addAction(act)
        menu.addAction(tr("menu.unmark"), lambda: self._set_selected(None))
        type_counts: dict[TextureType, int] = {}
        for r in rows:
            t = self.model.entries[r].ttype
            type_counts[t] = type_counts.get(t, 0) + 1
        if len(type_counts) > 1:
            deselect_menu = menu.addMenu(tr("menu.deselect_type"))
            only_menu = menu.addMenu(tr("menu.keep_only_type"))
            for t in TextureType:
                if t in type_counts:
                    label = tr("menu.type_count", type=type_name(t), count=type_counts[t])
                    deselect_menu.addAction(label, lambda t=t: self._deselect_where(lambda e: e.ttype is t))
                    only_menu.addAction(label, lambda t=t: self._deselect_where(lambda e: e.ttype is not t))
        menu.addSeparator()

        selected_mods = sorted({self.model.entries[r].mod for r in rows} | {entry.mod}, key=str.lower)
        hide_label = (
            tr("menu.hide_mod", mod=selected_mods[0])
            if len(selected_mods) == 1
            else tr("menu.hide_mods", count=len(selected_mods))
        )
        menu.addAction(hide_label, lambda: self._hide_mods(selected_mods))
        selected_authors = sorted(
            {self.model.mod_authors.get(m, UNKNOWN) for m in selected_mods}, key=str.lower
        )
        if self.authors_enabled and selected_authors:
            author_label = (
                tr("menu.hide_author", author=self._author_text(selected_authors[0]))
                if len(selected_authors) == 1
                else tr("menu.hide_authors", count=len(selected_authors))
            )
            menu.addAction(author_label, lambda: self._hide_authors(selected_authors))
        if self.hidden_mods or self.hidden_authors:
            menu.addAction(tr("menu.unhide_all", what=self._hidden_summary()), self._unhide_all_mods)
        self._add_fetch_actions(menu, selected_mods)
        menu.addAction(tr("menu.show_only_mod", mod=entry.mod), lambda: self._filter_mod(entry.mod))
        folder = entry.virtual_path.rsplit("/", 1)[0] + "/"
        menu.addAction(tr("menu.show_only_folder", folder=folder), lambda: self._filter_folder(folder))
        menu.addAction(tr("menu.reset_filters"), self._reset_filters)
        menu.addSeparator()
        menu.addAction(tr("menu.open_folder"), lambda: os.startfile(entry.abs_path.parent))
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
            parts.append(tr("count.mods", count=n))
        if n := len(self.hidden_authors):
            parts.append(tr("count.authors", count=n))
        return ", ".join(parts)

    @staticmethod
    def _author_text(author: str) -> str:
        return tr("table.author_unknown") if author == UNKNOWN else author

    def _update_unhide_button(self) -> None:
        if self.on_hidden_mods_changed is not None and self.hidden_mods != self._initial_hidden_mods:
            self._initial_hidden_mods = set(self.hidden_mods)
            self.on_hidden_mods_changed(set(self.hidden_mods))
        if self.on_hidden_authors_changed is not None and self.hidden_authors != self._saved_hidden_authors:
            self._saved_hidden_authors = set(self.hidden_authors)
            self.on_hidden_authors_changed(set(self.hidden_authors))
        self.unhide_btn.setText(tr("hidden.unhide", what=self._hidden_summary()))
        tooltip = []
        if self.hidden_mods:
            tooltip.append(tr("hidden.tip_mods", list=", ".join(sorted(self.hidden_mods, key=str.lower))))
        if self.hidden_authors:
            tooltip.append(
                tr(
                    "hidden.tip_authors",
                    list=", ".join(
                        sorted((self._author_text(a) for a in self.hidden_authors), key=str.lower)
                    ),
                )
            )
        self.unhide_btn.setToolTip("\n".join(tooltip))
        self.unhide_btn.setVisible(bool(self.hidden_mods or self.hidden_authors))

    # ============================================================================ authors ====

    def _header_context_menu(self, pos) -> None:
        header = self.table.horizontalHeader()
        if header.logicalIndexAt(pos) != COL_AUTHOR:
            return
        menu = QMenu(self)
        self._add_fetch_actions(menu, [])
        if menu.actions():
            menu.exec(header.mapToGlobal(pos))

    def _add_fetch_actions(self, menu: QMenu, selected_mods: list[str]) -> None:
        """'Fetch author' entries; no-op entries are hidden rather than shown disabled."""
        if not self.authors_enabled or not self.mod_refs:
            return
        selected = self._unfetched_refs(selected_mods)
        everything = self._unfetched_refs(None)
        if not selected and not everything:
            return
        menu.addSeparator()
        if selected:
            n = len({m for m in selected_mods if self.mod_refs.get(m) in selected})
            menu.addAction(tr("menu.fetch_selected", count=n), lambda: self.fetch_authors(selected_mods))
        if everything:
            menu.addAction(tr("menu.fetch_all", count=len(everything)), lambda: self.fetch_authors(None))

    def _init_authors(self) -> None:
        """Show cached authors; mods never looked up show "Unknown" until fetched."""
        self._author_cache = (
            AuthorCache.load(self.author_cache_path) if self.author_cache_path else AuthorCache()
        )
        authors: dict[str, str] = {}
        for mod, ref in self.mod_refs.items():
            if ref is None:
                authors[mod] = UNKNOWN
                continue
            self._mods_by_ref.setdefault(ref.key, []).append(mod)
            cached = self._author_cache.get(ref)
            if cached is not None:
                authors[mod] = cached or UNKNOWN
        self.model.set_mod_authors(authors)
        if self.auto_fetch_authors:
            self.fetch_authors(None, quiet=True)

    def _unfetched_refs(self, mods: list[str] | None) -> list[NexusRef]:
        """Nexus pages (of `mods`, or of all mods) that aren't cached and aren't being fetched."""
        names = self.mod_refs if mods is None else mods
        queued = {r.key for r in self._author_queue}
        refs = [
            ref
            for m in names
            if (ref := self.mod_refs.get(m)) is not None
            and self._author_cache.get(ref) is None
            and ref.key not in queued
            and m not in self.model.fetching_mods  # already being looked up
        ]
        return list(dict.fromkeys(refs))

    def fetch_authors(self, mods: list[str] | None, quiet: bool = False) -> None:
        """Look up uploaders on Nexus for `mods` (None = all mods) in the background."""
        refs = self._unfetched_refs(mods)
        if not refs:
            if not quiet:
                self.author_label.setText(tr("authors.nothing_left"))
                self.author_label.setVisible(True)
            return
        self._author_queue.extend(refs)
        self.model.set_fetching({m for r in refs for m in self._mods_by_ref.get(r.key, [])}, True)
        if self.author_worker is None or not self.author_worker.isRunning():
            self._start_author_worker()

    def _start_author_worker(self) -> None:
        refs, self._author_queue = self._author_queue, []
        self.author_label.setText(tr("authors.looking_up", count=len(refs)))
        self.author_label.setVisible(True)
        worker = AuthorWorker(refs, self._author_cache, self)
        worker.failed.connect(self._on_authors_failed)
        worker.found.connect(self._on_author_found)
        worker.progress.connect(
            lambda n, total: self.author_label.setText(tr("authors.progress", done=n, total=total))
        )
        worker.finished_msg.connect(lambda msg, refs=refs: self._on_authors_finished(msg, refs))
        self._author_timer.start()
        self.author_worker = worker
        worker.start()

    def _on_authors_failed(self, message: str, details: str) -> None:
        """Tell the user a lookup failed instead of failing silently (found authors are kept)."""
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            tr("authors.error_title"),
            tr("authors.error_text", message=message),
            parent=self,
        )
        if details:
            box.setDetailedText(details)
        box.open()  # non-blocking: the worker's finished signal still updates the status line

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

    def _on_authors_finished(self, msg: str, refs: list[NexusRef]) -> None:
        self._author_timer.stop()
        self._flush_authors()
        # Pages that weren't resolved (cancelled, rate limit, offline) go back to "Unknown".
        self.model.set_fetching({m for r in refs for m in self._mods_by_ref.get(r.key, [])}, False)
        self.author_label.setText(msg.splitlines()[0] if msg else "")
        self.author_label.setToolTip(msg)
        self.author_label.setVisible(bool(msg))
        if self._author_queue and not (self.author_worker and self.author_worker.cancel):
            self._start_author_worker()

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
            msg = tr("run.nothing_all_done", count=plan.skipped) if plan.skipped else tr("run.nothing")
            QMessageBox.information(self, "Texture Downscaler", msg)
            return

        lines = [tr("run.confirm_jobs", count=len(plan.jobs), mod=self.output_name)]
        if plan.skipped:
            lines.append(tr("run.confirm_skip", count=plan.skipped))
        if plan.remove:
            lines.append(tr("run.confirm_remove", count=len(plan.remove)))
        lines.append(tr("run.confirm_originals"))
        if (
            QMessageBox.question(self, tr("run.confirm_title"), "<br>".join(lines))
            != QMessageBox.StandardButton.Yes
        ):
            return

        output_dir = self.ensure_output_mod()
        if output_dir is None:
            return
        self.output_dir = output_dir

        progress = QProgressDialog(tr("run.progress"), tr("run.cancel"), 0, max(1, len(plan.jobs)), self)
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
                progress.setLabelText(tr("run.progress_n", done=n, total=total, name=name)),
            )
        )
        progress.canceled.connect(lambda: setattr(worker, "cancel", True))
        worker.done.connect(lambda results: self._on_run_done(plan, results, progress))
        worker.failed.connect(
            lambda tb: (progress.close(), QMessageBox.critical(self, tr("run.failed_title"), tb))
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
                e.status = tr("status.done", width=r.job.width, height=r.job.height)
            elif r.message == "Cancelled":
                cancelled += 1
            else:
                e.status = tr("status.failed", message=r.message)
                failed.append(r)
        self.model.refresh_rows()
        self._apply_filters()  # newly done rows may now be hidden
        self.mode = RunMode.SKIP_DONE  # outputs on disk now match the manifest
        self.on_output_written()

        ok = len(results) - len(failed) - cancelled
        msg = tr("run.result", count=ok)
        if cancelled:
            msg += "\n" + tr("run.result_cancelled", count=cancelled)
        if failed:
            msg += "\n" + tr("run.result_failed", count=len(failed))
        msg += "\n\n" + tr("run.result_hint", mod=self.output_name)
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
