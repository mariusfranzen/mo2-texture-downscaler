"""Qt table model + filter proxy for scanned textures."""

from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor, QFont

from ..core.downscaler import estimated_size, target_size
from ..core.scanner import TextureEntry
from ..core.texture_types import TextureType

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 2

TARGET_CHOICES: list[int] = [8192, 4096, 2048, 1024, 512, 256, 128]

COL_PATH, COL_MOD, COL_TYPE, COL_RES, COL_FORMAT, COL_SIZE, COL_TARGET, COL_NEW, COL_STATUS = range(9)
HEADERS = ["Texture", "Mod", "Type", "Resolution", "Format", "Size", "Target", "New size", "Status"]


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_dim(n: int) -> str:
    return f"{n // 1024}K" if n >= 1024 and n % 1024 == 0 else str(n)


class TextureTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[TextureEntry] = []
        self._bold = QFont()
        self._bold.setBold(True)
        self._target_brush = QBrush(QColor(70, 140, 255, 45))

    # ---- data access -------------------------------------------------------------------------

    def set_entries(self, entries: list[TextureEntry]) -> None:
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()

    def set_target(self, rows: list[int], target: int | None) -> int:
        """Set target for model rows; targets >= current size are treated as 'keep'.

        Returns how many rows changed.
        """
        changed = 0
        for row in rows:
            entry = self.entries[row]
            new = target if target is not None and target < entry.max_dim else None
            if entry.target != new:
                entry.target = new
                changed += 1
        if rows and changed:
            self.dataChanged.emit(self.index(min(rows), 0), self.index(max(rows), len(HEADERS) - 1))
        return changed

    def refresh_rows(self) -> None:
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, len(HEADERS) - 1))

    # ---- QAbstractTableModel -----------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        return None

    def flags(self, index):
        base = super().flags(index)
        if index.column() == COL_TARGET:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(e, col)
        if role == SORT_ROLE:
            return self._sort_key(e, col)
        if role == ENTRY_ROLE:
            return e
        if role == Qt.ItemDataRole.EditRole and col == COL_TARGET:
            return e.target
        if role == Qt.ItemDataRole.BackgroundRole and e.target is not None:
            return self._target_brush
        if role == Qt.ItemDataRole.FontRole and col == COL_TARGET and e.target is not None:
            return self._bold
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_PATH:
                return f"{e.virtual_path}\n{e.abs_path}"
            if col == COL_MOD and e.provider_count > 1:
                return f"Winning file. Provided by {e.provider_count} mods in total."
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_RES, COL_SIZE, COL_TARGET, COL_NEW):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.column() != COL_TARGET or role != Qt.ItemDataRole.EditRole:
            return False
        self.set_target([index.row()], value)
        return True

    @staticmethod
    def _display(e: TextureEntry, col: int) -> str:
        if col == COL_PATH:
            return e.virtual_path
        if col == COL_MOD:
            return e.mod
        if col == COL_TYPE:
            return e.ttype.value
        if col == COL_RES:
            return f"{e.info.width} x {e.info.height}"
        if col == COL_FORMAT:
            return e.info.format
        if col == COL_SIZE:
            return fmt_bytes(e.size)
        if col == COL_TARGET:
            if e.target is None:
                return "-"
            w, h = target_size(e.info, e.target)
            return f"{w} x {h}"
        if col == COL_NEW:
            return fmt_bytes(estimated_size(e.size, e.info, e.target)) if e.target is not None else ""
        if col == COL_STATUS:
            return e.status
        return ""

    @staticmethod
    def _sort_key(e: TextureEntry, col: int):
        if col == COL_RES:
            return (e.info.width * e.info.height, e.size)
        if col == COL_SIZE:
            return e.size
        if col == COL_TARGET:
            return e.target or 0
        if col == COL_NEW:
            return estimated_size(e.size, e.info, e.target)
        return TextureTableModel._display(e, col).lower()


class TextureFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.min_dim = 0
        self.mod: str | None = None
        self.hidden_mods: frozenset[str] = frozenset()
        self.ttype: TextureType | None = None
        self.text_terms: list[str] = []
        self.only_targeted = False
        self.hide_done = False
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(False)

    def set_filters(
        self,
        *,
        min_dim: int,
        mod: str | None,
        hidden_mods: frozenset[str] = frozenset(),
        ttype: TextureType | None,
        text: str,
        only_targeted: bool,
        hide_done: bool = False,
    ) -> None:
        self.min_dim = min_dim
        self.mod = mod
        self.hidden_mods = hidden_mods
        self.ttype = ttype
        self.text_terms = [t for t in text.lower().replace("\\", "/").split() if t]
        self.only_targeted = only_targeted
        self.hide_done = hide_done
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        e: TextureEntry = self.sourceModel().entries[source_row]
        if e.max_dim < self.min_dim:
            return False
        if self.mod is not None and e.mod != self.mod:
            return False
        if e.mod in self.hidden_mods:
            return False
        if self.ttype is not None and e.ttype is not self.ttype:
            return False
        if self.only_targeted and e.target is None:
            return False
        # Done = output exists for the current target; a changed target makes it pending again.
        if self.hide_done and e.done_target is not None and e.target == e.done_target:
            return False
        if self.text_terms:
            haystack = e.key + " " + e.mod.lower()
            if not all(t in haystack for t in self.text_terms):
                return False
        return True

    def lessThan(self, left, right):
        return left.data(SORT_ROLE) < right.data(SORT_ROLE)
