"""Winbox v3 "Inline Comment: off" rendering, plus device status lines.

With inline comments off, Winbox shows a commented item as a ``;;; comment``
line above the item's own line. This draws that as one taller row rather than
inserting a second row, so the model stays 1:1 with the device objects -- every
selection, delete-by-index and raw-row lookup in the list pane keeps working,
and sorting can never separate a comment from the item it belongs to.

The same strip carries the device's own status lines (STD_INFO 0xfe001c), which
Winbox draws as ``-- text`` in red: a CAPsMAN-managed wlan says "managed by
CAPsMAN" and its channel there. Those are not comments -- they are read-only,
there is no column to move them into, and the user cannot edit them -- so they
show whether inline comments are on or off, below the comment line when an
object has both. Every line in the strip is ruled off in the table's own grid
colour, the last one included, so a row carrying three lines reads as three
ruled lines instead of one tall cell.

The work is split in two because a banner is a row-level artifact spanning all
columns, while a delegate only ever sees one cell:

* ``CommentBannerDelegate`` reserves the height and keeps the item's own text
  below the strip.
* ``CommentTableView`` draws the comment text itself, after ``QTableView`` has
  drawn its grid -- otherwise the vertical grid lines chop the sentence into
  per-column fragments.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
)

from .. import style
from ..models.table_model import (
    COMMENT_ROLE,
    INFO_ROLE,
    TREE_COLLAPSED,
    TREE_EXPANDED,
    TREE_ROLE,
)
from .column_rule import ColumnRuleDelegate

__all__ = ["COMMENT_ROLE", "INFO_ROLE", "CommentBannerDelegate", "CommentTableView"]

#: Interface-tree geometry: pixels of indent per depth level, and the width of
#: the arrow slot that sits before the type icon at each level.
TREE_STEP = 14
TREE_ARROW_W = 14
TREE_ARROW_SIZE = 9


def _line_height(fm) -> int:
    # One px more than the text needs: the bottom row of every line belongs to
    # the rule that separates it from the next one (or from the item's own
    # line), so a descender never lands on it.
    return fm.height() + 3


def _banner_height(fm, lines: int = 1) -> int:
    return _line_height(fm) * max(0, lines)


def _tree_gutter(depth: int) -> int:
    """Left inset reserved for the branch arrow and indent at ``depth``."""
    return max(0, depth) * TREE_STEP + TREE_ARROW_W


def _arrow_slot(cell_rect: QRect, depth: int) -> QRect:
    """The clickable / paintable arrow rect for a row at ``depth``.

    One source of truth so the delegate paints the triangle exactly where the
    view hit-tests the click."""
    x = cell_rect.left() + max(0, depth) * TREE_STEP
    return QRect(x, cell_rect.top(), TREE_ARROW_W, cell_rect.height())


class CommentBannerDelegate(ColumnRuleDelegate):
    """Reserves a strip at the top of a row for its comment and status lines.

    Inherits the column separator, so installing this on a table does not
    quietly drop the rule the rest of the app's tables draw.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._enabled = False
        # Per-row copy of the option the view handed us for column 0, so the
        # view can refill the banner strip with the exact same background
        # (alternating stripe / selection highlight) it just painted.
        self._row_options: dict[int, QStyleOptionViewItem] = {}

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def take_row_options(self) -> dict[int, QStyleOptionViewItem]:
        opts, self._row_options = self._row_options, {}
        return opts

    @property
    def enabled(self) -> bool:
        return self._enabled

    def comment_for(self, index: QModelIndex) -> str:
        if not self._enabled:
            return ""
        text = index.data(COMMENT_ROLE)
        return str(text).strip() if text else ""

    @staticmethod
    def info_for(index: QModelIndex) -> tuple[str, ...]:
        """The row's device status lines. Not gated on the inline-comment
        setting: there is no column these could move into."""
        data = index.data(INFO_ROLE)
        if not data:
            return ()
        if isinstance(data, str):
            data = [data]
        return tuple(str(t).strip() for t in data if str(t).strip())

    def banner_lines(self, index: QModelIndex) -> tuple[tuple[str, bool], ...]:
        """The strip's lines top to bottom, as ``(text, is_status)`` pairs.

        The comment comes first so an object carrying both reads the way Winbox
        stacks them: what the operator wrote, then what the device says.
        """
        lines: list[tuple[str, bool]] = []
        comment = self.comment_for(index)
        if comment:
            lines.append((f";;; {comment}", False))
        lines.extend((f"-- {text}", True) for text in self.info_for(index))
        return tuple(lines)

    def banner_height(self, fm, lines: int = 1) -> int:
        return _banner_height(fm, lines)

    @staticmethod
    def _tree_of(index: QModelIndex) -> tuple[int, int | None]:
        """(depth, state) for a tree row, or (0, None) for a plain cell."""
        data = index.data(TREE_ROLE)
        if isinstance(data, tuple) and len(data) == 2:
            return int(data[0]), int(data[1])
        return 0, None

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        # Pin the base height to the theme row height rather than the style's
        # own hint: the vertical header runs in ResizeToContents while banners
        # are on, so anything taller here would inflate every uncommented row
        # and lose Winbox's density.
        size = super().sizeHint(option, index)
        extra = _banner_height(option.fontMetrics, len(self.banner_lines(index)))
        depth, state = self._tree_of(index)
        # The gutter is drawn width the delegate does not otherwise report, so
        # add it here or auto-sizing the Name column clips the indented text.
        extra_w = _tree_gutter(depth) if state is not None else 0
        return QSize(size.width() + extra_w, style.ROW_HEIGHT + extra)

    def paint(self, painter, option, index) -> None:  # noqa: N802
        lines = len(self.banner_lines(index))
        depth, state = self._tree_of(index)
        gutter = _tree_gutter(depth) if state is not None else 0

        if not lines and gutter == 0:
            # No rule drawn here or below: CommentTableView paints the column
            # separators for the whole viewport in one pass.
            self.paint_cell(painter, option, index)
            return

        banner = _banner_height(option.fontMetrics, lines)
        view = option.widget
        qstyle = view.style() if view is not None else QApplication.style()

        # Paint the item background (alternating stripe, selection highlight)
        # across the *whole* cell first -- the shrunk paint below only covers the
        # content band, which would otherwise leave the banner strip and the
        # tree gutter as unpainted notches. Drop the icon from this pass so it is
        # not drawn twice (once here, once shifted below).
        bg = QStyleOptionViewItem(option)
        self.initStyleOption(bg, index)
        bg.text = ""
        bg.icon = QIcon()
        qstyle.drawControl(QStyle.ControlElement.CE_ItemViewItem, bg, painter, view)
        if lines and index.column() == 0:
            self._row_options[index.row()] = QStyleOptionViewItem(bg)

        # ...then the item itself in what is left below the banner and right of
        # the tree gutter.
        rest = QStyleOptionViewItem(option)
        rest.rect = QRect(
            option.rect.x() + gutter,
            option.rect.y() + banner,
            option.rect.width() - gutter,
            option.rect.height() - banner,
        )
        self.paint_cell(painter, rest, index)

        if state in (TREE_EXPANDED, TREE_COLLAPSED):
            self._paint_arrow(painter, option, index, depth, state == TREE_EXPANDED)

    def _paint_arrow(self, painter, option, index, depth, expanded) -> None:
        slot = QRectF(_arrow_slot(option.rect, depth))
        cx = slot.left() + TREE_ARROW_W / 2.0
        cy = slot.center().y()
        s = TREE_ARROW_SIZE
        if expanded:  # pointing down
            tri = QPolygonF(
                [
                    QPointF(cx - s / 2.0, cy - s / 4.0),
                    QPointF(cx + s / 2.0, cy - s / 4.0),
                    QPointF(cx, cy + s / 2.0),
                ]
            )
        else:  # pointing right
            tri = QPolygonF(
                [
                    QPointF(cx - s / 4.0, cy - s / 2.0),
                    QPointF(cx - s / 4.0, cy + s / 2.0),
                    QPointF(cx + s / 2.0, cy),
                ]
            )
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        role = (
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        colour = option.palette.color(role)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPolygon(tri)
        painter.restore()


class CommentTableView(QTableView):
    """QTableView that writes a row's ``;;; comment`` and ``-- status`` lines
    across the top of the row.

    Drawn in paintEvent after super(), because QTableView paints its grid after
    the items: a delegate-drawn banner gets sliced by every vertical grid line.
    """

    #: Emitted when a row is dragged onto another: (from_row, to_row), both in
    #: view coordinates. The pane decides whether the move is meaningful.
    rows_reordered = Signal(int, int)

    #: Emitted when the branch arrow of an interface-tree parent is clicked, with
    #: the view row. The pane flips that parent's collapsed state.
    branch_toggled = Signal(int)

    #: Emitted when a row is opened -- double-clicked, or Return/Enter pressed
    #: on it -- with that row's index. Winbox opens the highlighted row's
    #: properties on Enter and Qt leaves the key to the application; the
    #: double-click is ours too, because Qt's own drops it when the model resets
    #: mid-click (see mouseDoubleClickEvent). Either way the pane hears about
    #: opening a row on this one signal.
    row_activated = Signal(QModelIndex)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._banner: CommentBannerDelegate | None = None
        self._drag_row: int | None = None
        #: Row the drag is currently over, for the insertion marker.
        self._drop_row: int | None = None

    def enable_row_drag(self, on: bool) -> None:
        """Allow dragging rows to reorder them (ordered tables only)."""
        self.setDragEnabled(on)
        self.setAcceptDrops(on)
        self.setDropIndicatorShown(on)
        self.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
            if on
            else QAbstractItemView.DragDropMode.NoDragDrop
        )
        self.setDragDropOverwriteMode(False)

    def _branch_arrow_row(self, pos) -> int | None:
        """The view row whose branch arrow is at ``pos``, or None.

        One hit test for the press that toggles a subtree and the double-click
        that must not be read as "open this row".
        """
        index = self.indexAt(pos)
        model = self.model()
        if not index.isValid() or model is None:
            return None
        tcol = getattr(model, "tree_column", lambda: None)()
        meta = getattr(model, "tree_meta_for", lambda _r: None)(index.row())
        if (
            tcol is None
            or index.column() != tcol
            or not isinstance(meta, tuple)
            or len(meta) != 2
            or meta[1] not in (TREE_EXPANDED, TREE_COLLAPSED)
        ):
            return None
        if not _arrow_slot(self.visualRect(index), int(meta[0])).contains(pos):
            return None
        return index.row()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # A click on a parent's branch arrow toggles its subtree instead of
        # selecting the row, so expanding a tree never disturbs the selection.
        if event.button() == Qt.MouseButton.LeftButton:
            row = self._branch_arrow_row(event.position().toPoint())
            if row is not None:
                self.branch_toggled.emit(row)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Open the row the double-click started on, even if the list re-read
        between the two clicks.

        Qt holds the pressed row in a *persistent* index and drops the
        double-click when it no longer equals the row under the cursor. A model
        reset invalidates every persistent index, so a list on a 1s poll --
        Interface, and every other menu with an `autorefresh` -- swallowed the
        double-click whenever a re-read landed in the gap between the clicks.
        Nothing visible happened: `_restore_selection` puts the selection and
        the scroll position back, so the row stayed highlighted and no window
        opened, every few tries.

        The row to act on is the *current* one rather than whatever is under the
        cursor now, because the pane puts the current index back on the same
        object after a rebuild (see ListPane._current_row_ref). That is also the
        right answer when the rebuild reordered the rows underneath: sorting by
        a live Tx/Rx column does exactly that, and the user pressed on ether1,
        not on row 3.
        """
        pos = event.position().toPoint()
        if (
            event.button() == Qt.MouseButton.LeftButton
            # Not the empty space below the last row, where a double-click
            # means nothing, and not an arrow -- that press was a fold, so the
            # current row is some other row entirely.
            and self.indexAt(pos).isValid()
            and self._branch_arrow_row(pos) is None
        ):
            index = self.currentIndex()
            if index.isValid():
                self.row_activated.emit(index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Enter opens the row the keyboard is on, the way double-click does.
        # Taken from the current index and not from the selection: they are the
        # same row in normal use, and where they are not it is the current one
        # the arrow keys have been walking.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self.currentIndex()
            if index.isValid():
                self.row_activated.emit(index)
                event.accept()
                return
        # Right opens an interface tree's parent, Left closes it -- the keys a
        # tree has everywhere else. Only on a row where that means something:
        # anywhere else the arrows stay the walk across the columns.
        if (
            event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Left)
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and self._branch_key_row(event.key()) is not None
        ):
            self.branch_toggled.emit(self._branch_key_row(event.key()))
            event.accept()
            return
        super().keyPressEvent(event)

    def _branch_key_row(self, key) -> int | None:
        """The row Right/Left would fold, or None if this row does not fold."""
        index = self.currentIndex()
        model = self.model()
        if not index.isValid() or model is None:
            return None
        meta = getattr(model, "tree_meta_for", lambda _r: None)(index.row())
        if not (isinstance(meta, tuple) and len(meta) == 2):
            return None
        want = TREE_COLLAPSED if key == Qt.Key.Key_Right else TREE_EXPANDED
        return index.row() if meta[1] == want else None

    def startDrag(self, actions) -> None:  # noqa: N802
        index = self.currentIndex()
        self._drag_row = index.row() if index.isValid() else None
        super().startDrag(actions)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._drag_row is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        # Accept everywhere: every row is a legitimate target. Qt's own drop
        # indicator never appears while we handle the drag ourselves, so track
        # the landing row and draw the line in paintEvent.
        if self._drag_row is not None:
            event.acceptProposedAction()
            row = self._row_under(event.position().toPoint())
            if row != self._drop_row:
                self._drop_row = row
                self.viewport().update()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._clear_drop_marker()
        super().dragLeaveEvent(event)

    def _row_under(self, pos) -> int | None:
        index = self.indexAt(pos)
        if index.isValid():
            return index.row()
        model = self.model()
        return (model.rowCount() - 1) if model is not None else None

    def _clear_drop_marker(self) -> None:
        if self._drop_row is not None:
            self._drop_row = None
            self.viewport().update()

    def dropEvent(self, event) -> None:  # noqa: N802
        # Take the drop ourselves and tell Qt to do nothing: the model holds a
        # device-ordered list that only the router can reorder, so letting the
        # view move rows would show an order the device does not have.
        target = self.indexAt(event.position().toPoint())
        src = self._drag_row
        self._drag_row = None
        self._clear_drop_marker()
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if src is None:
            return
        # Past the last row means "move to the end".
        row = target.row() if target.isValid() else self.model().rowCount() - 1
        if row != src:
            self.rows_reordered.emit(src, row)

    def set_banner_delegate(self, delegate: CommentBannerDelegate) -> None:
        self._banner = delegate
        self.setItemDelegate(delegate)

    def _strip_brush(self, row: int, option, selected: bool):
        palette = option.palette if option is not None else self.palette()
        if selected:
            return palette.brush(QPalette.ColorRole.Highlight)
        alternate = bool(row % 2) and self.alternatingRowColors()
        if option is not None:
            try:
                alternate = bool(
                    option.features & QStyleOptionViewItem.ViewItemFeature.Alternate
                )
            except (AttributeError, TypeError):
                pass
        role = (
            QPalette.ColorRole.AlternateBase if alternate else QPalette.ColorRole.Base
        )
        return palette.brush(role)

    def _paint_drop_marker(self) -> None:
        """A line where the dragged row will land, and a tint on the target.

        Winbox shows you where a rule is going before you let go; without it a
        drag is a guess. Qt's own indicator is not drawn while we handle the
        drag ourselves, so this is it.
        """
        if self._drop_row is None or self._drag_row is None:
            return
        rect = self.visualRect(self.model().index(self._drop_row, 0))
        if not rect.isValid():
            return
        # Above the target when moving up, below it when moving down — which
        # is where the row actually ends up.
        y = rect.top() if self._drop_row < self._drag_row else rect.bottom()
        painter = QPainter(self.viewport())
        try:
            colour = self.palette().color(QPalette.ColorRole.Highlight)
            band = QColor(colour)
            band.setAlpha(48)
            painter.fillRect(
                0, rect.top(), self.viewport().width(), rect.height(), band
            )
            pen = painter.pen()
            pen.setColor(colour)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(0, y, self.viewport().width(), y)
        finally:
            painter.end()

    def paint_column_rules(self) -> None:
        """Winbox's vertical separators, drawn once for the whole viewport.

        A delegate can only see one cell, so drawing the rules there is a
        Python call per cell per frame -- the single most expensive thing in a
        repaint of a wide list, and it buys nothing that thirteen full-height
        lines do not. Rules stop at the last row, the way they do in Winbox:
        the empty space under a short list is not ruled. A theme that asks for
        no rules at all (``column_rules=False``) gets none: this is where the
        visible ones come from, so it is where the switch has to be read.
        """
        if not style.COLUMN_RULES:
            return
        model = self.model()
        if model is None or not model.rowCount() or not model.columnCount():
            return
        last = model.rowCount() - 1
        bottom = min(
            self.viewport().height(),
            self.rowViewportPosition(last) + self.rowHeight(last),
        )
        if bottom <= 0:
            return
        header = self.horizontalHeader()
        painter = QPainter(self.viewport())
        try:
            painter.setPen(QPen(QColor(style.GRIDLINE), 1))
            width = self.viewport().width()
            for visual in range(header.count()):
                logical = header.logicalIndex(visual)
                if header.isSectionHidden(logical):
                    continue
                x = (
                    self.columnViewportPosition(logical)
                    + self.columnWidth(logical)
                    - 1
                )
                if 0 <= x < width:
                    painter.drawLine(x, 0, x, bottom)
        finally:
            painter.end()

    def paintEvent(self, event) -> None:  # noqa: N802
        banner = self._banner
        if banner is not None:
            banner.take_row_options()  # start clean; super() refills it
        super().paintEvent(event)
        self.paint_column_rules()
        self._paint_drop_marker()
        model = self.model()
        if banner is None or model is None:
            return
        # `banner.enabled` alone is not the gate: it only means "comments get a
        # strip", and a row can still carry status lines with inline comments
        # on. Bail on the tables that have neither rather than build a painter.
        if not banner.enabled and not getattr(model, "has_info", bool)():
            return
        rows = model.rowCount()
        if not rows:
            return
        row_options = banner.take_row_options()

        painter = QPainter(self.viewport())
        fm = self.fontMetrics()
        height = _line_height(fm)
        dim = QColor(style.TEXT_DIM)
        red = QColor(style.INFO_RED)
        grid = QColor(style.GRIDLINE)
        hilite = self.palette().color(QPalette.ColorRole.HighlightedText)
        sm = self.selectionModel()
        viewport_h = self.viewport().height()
        # Start at the first row on screen rather than row 0: a long list would
        # otherwise walk thousands of off-screen rows on every repaint.
        first = max(0, self.rowAt(0))
        x0 = self.columnViewportPosition(0)
        width = max(0, self.viewport().width() - x0)
        for row in range(first, rows):
            top = self.rowViewportPosition(row)
            if top > viewport_h:
                break
            index = model.index(row, 0)
            lines = banner.banner_lines(index)
            if not lines:
                continue
            selected = sm is not None and sm.isRowSelected(row, QModelIndex())
            brush = self._strip_brush(row, row_options.get(row), selected)
            for n, (text, status) in enumerate(lines):
                strip = QRect(x0, top + n * height, width, height)
                # Repaint the strip over the grid: QTableView draws its grid
                # after the items, so the vertical rules would otherwise show
                # through the spaces between words and read as a row of separate
                # cells. The item style paints no opaque background under our
                # stylesheets, so fill from the palette -- Base/AlternateBase/
                # Highlight match what the view actually rendered in every theme.
                painter.fillRect(strip, brush)
                if selected:
                    # On a selected row the highlight decides the contrast, so
                    # red would be the less readable colour, not the louder one.
                    painter.setPen(hilite)
                else:
                    painter.setPen(red if status else dim)
                painter.drawText(
                    strip.adjusted(4, 0, -4, -1),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    fm.elidedText(text, Qt.TextElideMode.ElideRight, width - 8),
                )
                # Rule off every line, the last one included: that one divides
                # the strip from the item's own line. In the table's own grid
                # colour, so a three-line row reads as three ruled lines rather
                # than one tall cell -- the fill above wiped the grid here.
                painter.setPen(grid)
                painter.drawLine(
                    strip.left(), strip.bottom(), strip.right(), strip.bottom()
                )
        painter.end()
