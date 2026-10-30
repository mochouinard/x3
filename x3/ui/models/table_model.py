"""Dense table model for live list windows."""

from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from ..i18n import tr
from ..sortkey import is_quantity

#: Role a view uses to fetch a row's comment (Winbox v3 "Inline Comment: off").
COMMENT_ROLE = int(Qt.ItemDataRole.UserRole) + 77

#: Role a view uses to fetch a row's device status lines (STD_INFO) as a tuple
#: of strings. Read-only device text, drawn in red above the row like a comment.
INFO_ROLE = int(Qt.ItemDataRole.UserRole) + 79

#: Role a view uses to fetch a row's interface-tree state, as ``(depth, state)``
#: where state is 0 leaf / 1 expanded parent / 2 collapsed parent. Only the
#: tree column answers it; every other cell returns None.
TREE_ROLE = int(Qt.ItemDataRole.UserRole) + 78

#: ``state`` values carried by :data:`TREE_ROLE`.
TREE_LEAF = 0
TREE_EXPANDED = 1
TREE_COLLAPSED = 2

#: Fixed-width stand-in for a hidden secret. Deliberately not length preserving
#: -- matching the real length would leak how long the password is.
SECRET_MASK = "•" * 8

#: How long a freshly-arrived row stays tinted, in seconds.
FRESH_SECONDS = 60.0

#: Qt's roles as plain ints. ``data()`` is asked nine times per cell per
#: repaint, and reaching through ``Qt.ItemDataRole.X`` for each comparison put
#: 130k enum member lookups in a single repaint of a 40-row list.
_R_DISPLAY = int(Qt.ItemDataRole.DisplayRole)
_R_EDIT = int(Qt.ItemDataRole.EditRole)
_R_ALIGN = int(Qt.ItemDataRole.TextAlignmentRole)
_R_BACKGROUND = int(Qt.ItemDataRole.BackgroundRole)
_R_FOREGROUND = int(Qt.ItemDataRole.ForegroundRole)
_R_FONT = int(Qt.ItemDataRole.FontRole)
_R_DECORATION = int(Qt.ItemDataRole.DecorationRole)
_R_TOOLTIP = int(Qt.ItemDataRole.ToolTipRole)
#: What a cell is *called* and what else is worth knowing about it, for a
#: screen reader. Qt's table cell interface reads exactly these two: the first
#: replaces the cell's spoken name, the second is read after it. Nothing but an
#: assistive client ever asks for them, so answering them costs a repaint
#: nothing.
_R_ACC_TEXT = int(Qt.ItemDataRole.AccessibleTextRole)
_R_ACC_DESC = int(Qt.ItemDataRole.AccessibleDescriptionRole)

#: Everything data() can answer. Anything else -- CheckState, SizeHint,
#: StatusTip -- is turned away on the first line rather than falling down the
#: whole chain.
_ANSWERED_ROLES = frozenset((
    _R_DISPLAY, _R_EDIT, _R_ALIGN, _R_BACKGROUND, _R_FOREGROUND, _R_FONT,
    _R_DECORATION, _R_TOOLTIP, COMMENT_ROLE, INFO_ROLE, TREE_ROLE,
    _R_ACC_TEXT, _R_ACC_DESC,
))

#: Non-empty cells read before a column's alignment is called. A column of
#: five thousand numbers is a column of numbers; reading a few hundred says so
#: just as well, and keeps a poll of a long list from re-measuring every cell.
ALIGN_SAMPLE = 512

#: Grey for a disabled row, and the bold monospace of the flag column. Both
#: were built fresh on every cell that asked for them; neither ever varies.
_DISABLED_FG = None
_FLAGS_FONT = None


def _disabled_fg():
    global _DISABLED_FG
    if _DISABLED_FG is None:
        _DISABLED_FG = QColor("#808080")
    return _DISABLED_FG


def _flags_font():
    global _FLAGS_FONT
    if _FLAGS_FONT is None:
        font = QFont()
        font.setBold(True)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("DejaVu Sans Mono")
        _FLAGS_FONT = font
    return _FLAGS_FONT


#: Tint for a row that just appeared. Blended toward the row's own background as
#: it ages, so the fade works on light and dark skins alike.
FRESH_COLOR = QColor("#22c55e")
#: Queued-change tints: a new object, an edit, a removal -- held, not sent.
PENDING_COLORS = {"add": "#8fd694", "edit": "#ffd27a", "remove": "#f4a3a3"}


class SimpleTableModel(QAbstractTableModel):
    def __init__(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        parent=None,
        *,
        flags_column: int | None = None,
        comments: Sequence[str] | None = None,
        sources: Sequence[int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._columns = list(columns)
        self._rows = [list(r) for r in rows]
        # Index of the Winbox-style flags letter column (usually 0), or None.
        self.flags_column = flags_column
        # Letter -> meaning for that column, for the benefit of anything that
        # has to say it out loud. Set by the pane once the menu is known; the
        # column reads as bare letters until then.
        self._flag_legend: dict[str, str] = {}
        # Per-row disabled state for grey text (parallel to _rows).
        self._row_disabled: list[bool] = [False] * len(self._rows)
        #: row -> (kind, tooltip) for rows a queued change will touch.
        self._pending: dict[int, tuple[str, str]] = {}
        #: view row -> hover text; set by the pane (its row summary).
        self.row_tooltip = None
        # Per-row comment, and per-row index back into the pane's unfiltered
        # rows. The latter is what keeps "which object is this?" correct while a
        # filter is active, without comparing rendered strings.
        self._comments: list[str] = self._pad(comments, len(self._rows), "")
        self._sources: list[int] = self._pad(
            sources, len(self._rows), -1, default=list(range(len(self._rows)))
        )
        # Per-row device status lines (STD_INFO). Pushed separately from the
        # rows because they are read-only device text with no column: the pane
        # maps them through `sources` after every rebuild.
        self._info: list[tuple[str, ...]] = [()] * len(self._rows)
        self._secret_cols: set[int] = set()
        self._show_secrets = False
        # view row -> local monotonic time the row was first seen. Anchored on
        # the local clock, not the device's, so the fade stays smooth between
        # polls instead of stepping once per refresh.
        self._fresh_at: dict[int, float] = {}
        # Optional per-row icon shown in one column (interface type icons).
        self._row_icons: list = []
        self._icon_column: int | None = None
        # Optional per-row interface-tree state (depth + expand/collapse arrow),
        # keyed by view row, answered on the tree column via TREE_ROLE.
        self._tree_meta: dict[int, tuple[int, int]] = {}
        self._tree_column: int | None = None
        # Per-column text alignment, worked out from the column's own contents
        # the first time a cell asks. Cleared whenever the rows change.
        self._alignments: dict[int, int] = {}

    def set_tree_meta(self, meta: dict[int, tuple[int, int]], column: int) -> None:
        """Show branch arrows / indent in ``column`` for the given rows."""
        self._tree_meta = dict(meta)
        self._tree_column = column if column is not None and column >= 0 else None
        if self._rows and self._tree_column is not None:
            tl = self.index(0, self._tree_column)
            br = self.index(len(self._rows) - 1, self._tree_column)
            self.dataChanged.emit(tl, br, [TREE_ROLE])

    def tree_meta_for(self, row: int) -> tuple[int, int] | None:
        return self._tree_meta.get(row)

    def tree_column(self) -> int | None:
        return self._tree_column

    def set_row_icons(self, icons: Sequence[Any], column: int) -> None:
        """Show ``icons[row]`` (a QIcon or None) as the decoration in ``column``."""
        self._row_icons = list(icons)
        self._icon_column = column if column >= 0 else None
        if self._rows and self._icon_column is not None and self._icon_column < len(self._columns):
            tl = self.index(0, self._icon_column)
            br = self.index(len(self._rows) - 1, self._icon_column)
            self.dataChanged.emit(tl, br, [Qt.ItemDataRole.DecorationRole])

    @staticmethod
    def _pad(values, count: int, filler, *, default=None) -> list:
        if values is None:
            return list(default) if default is not None else [filler] * count
        out = list(values)
        while len(out) < count:
            out.append(filler)
        return out[:count]

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802
        if parent is not None and parent.isValid():
            return 0
        return len(self._columns)

    def column_alignment(self, col: int) -> int:
        """Alignment for a whole column: right for quantities, left for text.

        Per column and not per cell. Deciding cell by cell would ladder a
        column that holds a mix -- a blank Tx next to ``5.8 Mbps`` -- and even
        a column of pure numbers would jump about while a poll refills it. The
        column is right-aligned only when every value it holds is a quantity
        (up to :data:`ALIGN_SAMPLE` of them), so digits line up by magnitude
        the way Winbox shows them.
        """
        cached = self._alignments.get(col)
        if cached is not None:
            return cached
        left = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        align = left
        if col != self.flags_column and col not in self._secret_cols:
            seen = 0
            for row in self._rows:
                value = row[col] if col < len(row) else None
                if value is None or value == "":
                    continue  # a blank says nothing about the column
                if not is_quantity(value):
                    seen = 0
                    break
                seen += 1
                if seen >= ALIGN_SAMPLE:
                    break
            align = right if seen else left
        self._alignments[col] = align
        return align

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        # Ordered by how often a repaint asks, and nothing is looked up before
        # the role that needs it: this runs once per role per cell per frame,
        # and a resize is frames all the way down.
        role = int(role)
        if role not in _ANSWERED_ROLES:
            return None
        row, col = index.row(), index.column()
        if row < 0 or row >= len(self._rows) or col < 0 or col >= len(self._columns):
            return None

        if role == _R_DISPLAY or role == _R_EDIT:
            cells = self._rows[row]
            value = cells[col] if col < len(cells) else ""
            if (
                role == _R_DISPLAY
                and not self._show_secrets
                and col in self._secret_cols
                and value not in (None, "")
            ):
                return SECRET_MASK
            if value is None:
                return ""
            if value.__class__ is str:
                return value
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        if role == _R_ALIGN:
            return self._alignments.get(col) or self.column_alignment(col)

        if role == _R_BACKGROUND:
            pending = self._pending.get(row)
            if pending is not None:
                # Queued, not sent: tinted by what will happen to it, so a held
                # change never reads as the router's state.
                tint = QColor(PENDING_COLORS.get(pending[0], PENDING_COLORS["edit"]))
                tint.setAlphaF(0.45)
                return tint
            if not self._fresh_at:
                return None
            strength = self.freshness(row)
            if strength <= 0.0:
                return None
            tint = QColor(FRESH_COLOR)
            # Alpha rather than a blended solid: the view keeps its own
            # alternating-row striping and selection colour underneath.
            tint.setAlphaF(0.55 * strength)
            return tint

        if role == _R_FOREGROUND:
            if row < len(self._row_disabled) and self._row_disabled[row]:
                return _disabled_fg()
            return None

        value = self._rows[row][col] if col < len(self._rows[row]) else ""
        disabled = row < len(self._row_disabled) and self._row_disabled[row]

        if role == COMMENT_ROLE:
            # Answered for every column: a row's height is the max over its
            # cells, so every cell has to know the banner is there. Only one of
            # them draws the text (see CommentBannerDelegate).
            return self._comments[row] if row < len(self._comments) else ""

        if role == INFO_ROLE:
            # Answered for every column, for the same reason as COMMENT_ROLE:
            # the row's height is the max over its cells.
            return self._info[row] if row < len(self._info) else ()

        if role == TREE_ROLE:
            # Only the tree column carries the arrow/indent; other cells stay
            # flush so the gutter is drawn once, not per column.
            if col == self._tree_column:
                return self._tree_meta.get(row)
            return None

        if (
            role == _R_DECORATION
            and col == self._icon_column
            and row < len(self._row_icons)
        ):
            return self._row_icons[row]

        if role == _R_FONT and row in self._pending:
            font = QFont(_flags_font()) if (
                self.flags_column is not None and col == self.flags_column
            ) else QFont()
            font.setItalic(True)
            font.setStrikeOut(self._pending[row][0] == "remove")
            return font

        if role == _R_TOOLTIP and row in self._pending:
            return self._pending[row][1]

        if role == _R_FONT and self.flags_column is not None and col == self.flags_column:
            return _flags_font()

        if (
            role == _R_TOOLTIP and self.row_tooltip is not None
            and not (self.flags_column is not None and col == self.flags_column)
        ):
            try:
                return self.row_tooltip(row)
            except Exception:  # noqa: BLE001 - a hover must never break a paint
                return None

        if role == _R_TOOLTIP and self.flags_column is not None and col == self.flags_column:
            # Expand letters for hover help when we only have the string
            text = str(value or "")
            if not text:
                return None
            spelled = self.spoken_flags(row)
            return f"Flags: {text} — {spelled}" if spelled else f"Flags: {text}"

        if role == _R_ACC_TEXT:
            # "R" is a letter, not a word. Nothing else needs replacing: a
            # cell's spoken name falls back to what it displays.
            if self.flags_column is not None and col == self.flags_column:
                return self.spoken_flags(row) or None
            return None

        if role == _R_ACC_DESC:
            # Answered for every column, and on purpose. A comment and the
            # device's own status lines belong to the *row* -- they are drawn
            # above it, spanning the whole width -- and a table cell is the
            # only thing Qt lets a model describe. A reader moving down the
            # list hears them whichever column it is in.
            return self.row_context(row) or None

        return None

    # -- what a reader is told that a sighted user can see ----------------

    def set_flag_legend(self, legend: dict[str, str] | None) -> None:
        """Teach the model what the letters in the flags column mean."""
        self._flag_legend = dict(legend or {})

    def spoken_flags(self, row: int) -> str:
        """The flags cell as words: ``"XR"`` -> ``"disabled, running"``.

        An unknown letter is kept as itself rather than dropped -- a legend
        that has fallen behind the schema should read oddly, not lie.
        """
        col = self.flags_column
        if col is None or not (0 <= row < len(self._rows)):
            return ""
        cells = self._rows[row]
        text = str((cells[col] if col < len(cells) else "") or "")
        if not text:
            return ""
        legend = self._flag_legend
        return ", ".join(legend.get(ch, ch) for ch in text)

    def row_context(self, row: int) -> str:
        """A row's comment and device status lines, for a reader.

        Both are painted into a banner above the row (see CommentBannerDelegate)
        and so belong to no cell of their own. Disabled is only spelled out for
        a list with no flags column, where nothing else says it: elsewhere the
        X in the flags is already read out.
        """
        if not (0 <= row < len(self._rows)):
            return ""
        parts: list[str] = []
        comment = self._comments[row] if row < len(self._comments) else ""
        if comment:
            parts.append(f"comment: {comment}")
        for line in (self._info[row] if row < len(self._info) else ()) or ():
            if line:
                parts.append(str(line))
        if (
            self.flags_column is None
            and row < len(self._row_disabled)
            and self._row_disabled[row]
        ):
            parts.append("disabled")
        return "; ".join(parts)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            role == Qt.ItemDataRole.TextAlignmentRole
            and orientation == Qt.Orientation.Horizontal
        ):
            # A label sitting left of a right-aligned column names it from a
            # distance; both edges have to move together.
            if 0 <= section < len(self._columns):
                return self.column_alignment(section)
            return None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._columns):
                # Winbox uses an unnamed column for flag letters
                name = self._columns[section]
                return "" if name in ("", " ", "Flags") else tr(name)
        return str(section + 1)

    def set_row_drag_enabled(self, on: bool) -> None:
        """Allow rows to be dragged, for tables whose order is meaningful.

        Qt refuses a drag outright unless the *model* says the items support
        it, however the view is configured — so without this the drop event
        never fires and dragging looks like it simply does nothing.
        """
        self._row_drag = bool(on)

    def supportedDropActions(self):  # noqa: N802
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def flags(self, index: QModelIndex):  # noqa: N802
        if not index.isValid():
            # Empty space below the last row: a valid drop target, so a rule
            # can be dragged to the end of the list.
            return (
                Qt.ItemFlag.ItemIsDropEnabled
                if getattr(self, "_row_drag", False)
                else Qt.ItemFlag.NoItemFlags
            )
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if getattr(self, "_row_drag", False):
            base |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
        return base

    # -- mutation ---------------------------------------------------------
    # Everything below is the public replacement for reaching into _rows /
    # _columns from the outside; the three parallel lists have to move together.

    def set_table(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        comments: Sequence[str] | None = None,
        sources: Sequence[int] | None = None,
        flags_column: int | None = None,
    ) -> None:
        self.beginResetModel()
        self._columns = list(columns)
        self._rows = [list(r) for r in rows]
        # View rows are renumbered by a rebuild, so the row-keyed fade state no
        # longer refers to the same objects. The pane re-applies it from stable
        # object ids right after this returns.
        self._fresh_at.clear()
        # Tree state is row-keyed too; the pane re-applies it after this returns
        # (see ListPane._apply_tree_meta), so drop the stale mapping now.
        self._tree_meta = {}
        self._alignments.clear()
        if flags_column is not None:
            self.flags_column = flags_column
        self._comments = self._pad(comments, len(self._rows), "")
        self._sources = self._pad(
            sources, len(self._rows), -1, default=list(range(len(self._rows)))
        )
        # Row-keyed like the fade and the tree state; the pane re-applies it
        # from `sources` right after this returns (ListPane._apply_info).
        self._info = [()] * len(self._rows)
        # Row-keyed, re-applied by the pane after a rebuild (set_pending).
        self._pending = {}
        self.reset_disabled_from_flags()
        self.endResetModel()

    def set_pending(self, pending: dict[int, tuple[str, str]]) -> None:
        """Mark rows a queued change will touch: ``{row: (kind, tooltip)}``.

        ``kind`` is ``add``, ``edit`` or ``remove``. Drawn tinted and in
        italics -- struck through for a removal -- until the queue is sent or
        dropped, whichever the router's own rows say afterwards.
        """
        self._pending = dict(pending)
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, max(0, len(self._columns) - 1)),
                [Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.FontRole,
                 Qt.ItemDataRole.ToolTipRole],
            )

    def pending_kind(self, row: int) -> str | None:
        hit = self._pending.get(row)
        return hit[0] if hit else None

    def set_rows(
        self,
        rows: Sequence[Sequence[Any]],
        *,
        comments: Sequence[str] | None = None,
        sources: Sequence[int] | None = None,
    ) -> None:
        self.set_table(self._columns, rows, comments=comments, sources=sources)

    def replace_row(
        self, view_row: int, values: Sequence[Any], *, comment: str | None = None
    ) -> None:
        if view_row < 0 or view_row >= len(self._rows):
            return
        old = self._rows[view_row]
        self._rows[view_row] = list(values)
        # A pushed counter replaces "5.8 Mbps" with "6.1 Mbps" once a second.
        # Re-deciding every column for that would re-read the whole table each
        # time, so only a cell that changed *kind* drops its column's answer.
        for col in list(self._alignments):
            before = old[col] if col < len(old) else None
            after = values[col] if col < len(values) else None
            if before != after and is_quantity(before) != is_quantity(after):
                del self._alignments[col]
        if comment is not None and view_row < len(self._comments):
            self._comments[view_row] = comment
        left = self.index(view_row, 0)
        right = self.index(view_row, max(0, self.columnCount() - 1))
        self.dataChanged.emit(left, right)

    def remove_row(self, view_row: int) -> None:
        if view_row < 0 or view_row >= len(self._rows):
            return
        self.beginRemoveRows(QModelIndex(), view_row, view_row)
        del self._rows[view_row]
        self._alignments.clear()
        for parallel in (self._comments, self._info, self._sources,
                         self._row_disabled):
            if view_row < len(parallel):
                del parallel[view_row]
        self.endRemoveRows()

    def source_row(self, view_row: int) -> int | None:
        """Index into the pane's unfiltered rows, or None when unresolvable."""
        if view_row < 0 or view_row >= len(self._sources):
            return None
        src = self._sources[view_row]
        return None if src < 0 else src

    def set_info(self, values: Sequence[Sequence[str]]) -> None:
        """Device status lines per view row (STD_INFO), parallel to the rows."""
        self._info = self._pad(
            [tuple(v) for v in values], len(self._rows), ()
        )
        if self._rows:
            tl = self.index(0, 0)
            br = self.index(len(self._rows) - 1, max(0, self.columnCount() - 1))
            self.dataChanged.emit(tl, br, [INFO_ROLE])

    def has_comments(self) -> bool:
        """True when any row carries a comment. Lets a view skip the whole
        banner machinery on the lists -- most of them -- that have none."""
        return any(self._comments)

    def has_info(self) -> bool:
        """True when any row carries status lines. Lets a view skip the whole
        banner pass on the tables -- most of them -- that have none."""
        return any(self._info)

    def info(self, view_row: int) -> tuple[str, ...]:
        if view_row < 0 or view_row >= len(self._info):
            return ()
        return self._info[view_row]

    def comment(self, view_row: int) -> str:
        if view_row < 0 or view_row >= len(self._comments):
            return ""
        return self._comments[view_row]

    def set_comment(self, view_row: int, text: str) -> None:
        if view_row < 0 or view_row >= len(self._comments):
            return
        self._comments[view_row] = text
        left = self.index(view_row, 0)
        right = self.index(view_row, max(0, self.columnCount() - 1))
        self.dataChanged.emit(left, right, [COMMENT_ROLE])

    # -- arrival highlight -------------------------------------------------

    def mark_fresh(self, view_row: int, *, age: float = 0.0) -> None:
        """Tint ``view_row`` as newly arrived, ``age`` seconds into its fade.

        ``age`` lets a row that the device says is already 20s old start the
        fade 20s in, instead of restarting it at full strength.
        """
        if view_row < 0 or view_row >= len(self._rows):
            return
        if age >= FRESH_SECONDS:
            return
        self._fresh_at[view_row] = time.monotonic() - max(0.0, age)

    def freshness(self, view_row: int) -> float:
        """1.0 for a row that just arrived, easing to 0.0 over FRESH_SECONDS."""
        started = self._fresh_at.get(view_row)
        if started is None:
            return 0.0
        elapsed = time.monotonic() - started
        if elapsed >= FRESH_SECONDS or elapsed < 0:
            self._fresh_at.pop(view_row, None)
            return 0.0
        return 1.0 - (elapsed / FRESH_SECONDS)

    def has_fresh_rows(self) -> bool:
        """True while any row is still fading — lets the view idle its timer."""
        return bool(self._fresh_at)

    def clear_fresh(self) -> None:
        self._fresh_at.clear()

    def repaint_fresh(self) -> None:
        """Re-emit dataChanged for fading rows only."""
        if not self._fresh_at:
            return
        last_col = max(0, self.columnCount() - 1)
        for row in sorted(self._fresh_at):
            if row >= len(self._rows):
                continue
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, last_col),
                [Qt.ItemDataRole.BackgroundRole],
            )

    def set_secret_columns(self, cols: Iterable[int]) -> None:
        self._secret_cols = {int(c) for c in cols}
        # A masked column is a row of bullets, never a quantity.
        self._alignments.clear()

    def set_show_secrets(self, show: bool) -> None:
        show = bool(show)
        if show == self._show_secrets:
            return
        self._show_secrets = show
        if not self._secret_cols or not self._rows:
            return
        lo, hi = min(self._secret_cols), max(self._secret_cols)
        self.dataChanged.emit(
            self.index(0, lo),
            self.index(len(self._rows) - 1, hi),
            [Qt.ItemDataRole.DisplayRole],
        )

    def row_dict(self, row: int) -> dict[str, Any]:
        if row < 0 or row >= len(self._rows):
            return {}
        return {self._columns[i]: self._rows[row][i] for i in range(len(self._columns))}

    def row_values(self, row: int) -> list[Any]:
        if row < 0 or row >= len(self._rows):
            return []
        return list(self._rows[row])

    def set_row_disabled(self, row: int, disabled: bool) -> None:
        if row < 0 or row >= len(self._rows):
            return
        while len(self._row_disabled) < len(self._rows):
            self._row_disabled.append(False)
        self._row_disabled[row] = disabled
        left = self.index(row, 0)
        right = self.index(row, max(0, self.columnCount() - 1))
        self.dataChanged.emit(left, right, [
            Qt.ItemDataRole.ForegroundRole,
            Qt.ItemDataRole.DisplayRole,
        ])

    def reset_disabled_from_flags(self) -> None:
        """Mark rows grey when the flags column contains 'X' (disabled)."""
        self._row_disabled = []
        fc = self.flags_column
        for row in self._rows:
            flags = ""
            if fc is not None and fc < len(row):
                flags = str(row[fc] or "")
            self._row_disabled.append("X" in flags)
