"""Qt table model + filter proxy for scanned textures."""

from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PyQt6.QtGui import QBrush, QColor, QFont

from ..core.downscaler import estimated_size, target_size
from ..core.nexus import UNKNOWN
from ..core.scanner import TextureEntry
from ..core.texture_types import TextureType

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 2

TARGET_CHOICES: list[int] = [8192, 4096, 2048, 1024, 512, 256, 128]

(
    COL_PATH,
    COL_MOD,
    COL_AUTHOR,
    COL_TYPE,
    COL_RES,
    COL_FORMAT,
    COL_SIZE,
    COL_TARGET,
    COL_NEW,
    COL_STATUS,
) = range(10)
HEADERS = [
    "Texture",
    "Mod",
    "Author",
    "Type",
    "Resolution",
    "Format",
    "Size",
    "Downscale to",
    "New size",
    "Status",
]


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
        # mod name -> Nexus uploader, or UNKNOWN when it isn't a Nexus mod / not found.
        # Mods missing here haven't been looked up yet (also shown as UNKNOWN).
        self.mod_authors: dict[str, str] = {}
        self.fetching_mods: set[str] = set()  # lookup queued or running -> shown as "..."
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

    def reorder(self, order: list[int]) -> None:
        """Rearrange rows so new row i is old row order[i], keeping selections/persistent indexes."""
        self.layoutAboutToBeChanged.emit()
        new_pos = [0] * len(order)
        for new, old in enumerate(order):
            new_pos[old] = new
        self.entries = [self.entries[i] for i in order]
        old_indexes = self.persistentIndexList()
        self.changePersistentIndexList(
            old_indexes, [self.index(new_pos[i.row()], i.column()) for i in old_indexes]
        )
        self.layoutChanged.emit()

    def author(self, e: TextureEntry) -> str:
        return self.mod_authors.get(e.mod, UNKNOWN)

    def set_mod_authors(self, authors: dict[str, str]) -> None:
        self.mod_authors.update(authors)
        self.fetching_mods.difference_update(authors)
        self.refresh_authors()

    def set_fetching(self, mods: set[str], fetching: bool) -> None:
        if fetching:
            self.fetching_mods |= mods
        else:
            self.fetching_mods -= mods
        self.refresh_authors()

    def refresh_authors(self) -> None:
        if self.entries:
            self.dataChanged.emit(self.index(0, COL_AUTHOR), self.index(len(self.entries) - 1, COL_AUTHOR))

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
            if col == COL_AUTHOR:
                if e.mod in self.fetching_mods:
                    return "Looking up the uploader on Nexus Mods..."
                if e.mod not in self.mod_authors:
                    return "Not looked up yet. Right-click > Fetch author to look it up on Nexus."
                if self.mod_authors[e.mod] == UNKNOWN:
                    return "Not a Nexus mod, or not found on Nexus"
                return "Nexus uploader"
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_RES, COL_SIZE, COL_TARGET, COL_NEW):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.column() != COL_TARGET or role != Qt.ItemDataRole.EditRole:
            return False
        self.set_target([index.row()], value)
        return True

    def _display(self, e: TextureEntry, col: int) -> str:
        if col == COL_PATH:
            return e.virtual_path
        if col == COL_MOD:
            return e.mod
        if col == COL_AUTHOR:
            return "..." if e.mod in self.fetching_mods else self.author(e)
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

    def sort_key(self, e: TextureEntry, col: int):
        if col == COL_RES:
            return e.info.width * e.info.height
        if col == COL_SIZE:
            return e.size
        if col == COL_TARGET:
            return e.target or 0
        if col == COL_NEW:
            return estimated_size(e.size, e.info, e.target)
        return self._display(e, col).lower()


class TextureFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.min_dim = 0
        self.mod: str | None = None
        self.hidden_mods: frozenset[str] = frozenset()
        self.hidden_authors: frozenset[str] = frozenset()
        self.ttype: TextureType | None = None
        self.text_terms: list[str] = []
        self.only_targeted = False
        self.hide_done = False
        self.secondary_col: int | None = None
        self.secondary_desc = False
        self.sort_col = -1
        self.sort_order = Qt.SortOrder.AscendingOrder
        self.setDynamicSortFilter(False)

    def set_filters(
        self,
        *,
        min_dim: int,
        mod: str | None,
        hidden_mods: frozenset[str] = frozenset(),
        hidden_authors: frozenset[str] = frozenset(),
        ttype: TextureType | None,
        text: str,
        only_targeted: bool,
        hide_done: bool = False,
    ) -> None:
        self.min_dim = min_dim
        self.mod = mod
        self.hidden_mods = hidden_mods
        self.hidden_authors = hidden_authors
        self.ttype = ttype
        self.text_terms = [t for t in text.lower().replace("\\", "/").split() if t]
        self.only_targeted = only_targeted
        self.hide_done = hide_done
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model: TextureTableModel = self.sourceModel()
        e: TextureEntry = model.entries[source_row]
        if e.max_dim < self.min_dim:
            return False
        if self.mod is not None and e.mod != self.mod:
            return False
        if e.mod in self.hidden_mods:
            return False
        if self.hidden_authors and model.author(e) in self.hidden_authors:
            return False
        if self.ttype is not None and e.ttype is not self.ttype:
            return False
        if self.only_targeted and e.target is None:
            return False
        # Done = output exists for the current target; a changed target makes it pending again.
        if self.hide_done and e.done_target is not None and e.target == e.done_target:
            return False
        if self.text_terms:
            haystack = f"{e.key} {e.mod.lower()} {model.author(e).lower()}"
            if not all(t in haystack for t in self.text_terms):
                return False
        return True

    def set_secondary_sort(self, col: int | None, descending: bool) -> None:
        self.secondary_col = col
        self.secondary_desc = descending
        if self.sort_col >= 0:
            self.sort(self.sort_col, self.sort_order)
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(HEADERS) - 1)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        text = super().headerData(section, orientation, role)
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and section == self.secondary_col
            and section != self.sort_col
        ):
            return f"{text}  (2{'▼' if self.secondary_desc else '▲'})"
        return text

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """Sort by reordering the source model in Python instead of Qt's sort.

        Qt's proxy sort calls lessThan ~1M times for 60k rows; every call crosses into Python,
        which takes seconds. Sorting the list in Python takes ~0.1 s, and the proxy (left
        unsorted) then just keeps source order for the filtered rows.
        """
        self.sort_col, self.sort_order = column, order
        model: TextureTableModel = self.sourceModel()
        if column < 0 or not model.entries:
            return
        key = model.sort_key
        entries = model.entries
        order_idx = list(range(len(entries)))
        sec = self.secondary_col
        if sec is not None and sec != column:  # stable sorts: secondary first, then primary
            sec_keys = [key(e, sec) for e in entries]
            order_idx.sort(key=sec_keys.__getitem__, reverse=self.secondary_desc)
        primary = [key(e, column) for e in entries]
        order_idx.sort(key=primary.__getitem__, reverse=order == Qt.SortOrder.DescendingOrder)
        model.reorder(order_idx)
