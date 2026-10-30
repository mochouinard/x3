"""Status marks in a picker: is the thing you are choosing up, and what did
someone write on it.

A dynamic dropdown offers the rows of another table, and Winbox shows you
nothing but their names -- you pick `ether5` and find out afterwards that the
cable is out, or that a colleague left "do not touch, WAN handoff" on it. The
read that fills the box already carries both facts (see
``DeviceClient.enum_marks``), so this is a display change, not a protocol one.

**The label is not where the mark goes.** A dropdown's item text is load
bearing in three places: for an `s`-prefix or string enum the label *is* the
value written to the device (``device.enum_choices``), the form selects the
current entry by matching ``label == rendered``, and ``_restate_combo``
re-selects by ``findText`` after an Apply. Decorating the text would put a
green dot in a firewall chain name and break re-selection everywhere else. So
the state rides on the item's *icon* -- which Qt draws in the popup and in the
closed box, both for free -- and the comment on a role this module's delegate
paints. ``currentText()`` stays the bare name.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from .. import style
from ..settings import load_dropdown_marks

#: What the entry's own row says about itself: 'up', 'down', 'disabled', ''.
MARK_STATE_ROLE = Qt.ItemDataRole.UserRole + 11
#: The comment on that row, '' when it has none.
MARK_COMMENT_ROLE = Qt.ItemDataRole.UserRole + 12

#: Dot box in px at 100% zoom. Deliberately under the 16px Winbox sprite cell:
#: an icon taller than the text grows every row of the popup, and these sit in
#: a 15px row beside an 11px font.
_DOT_BOX = 9
#: Widest a comment may make the popup, in px at 100% zoom. Past this it is
#: elided -- a dropdown that grows to fit someone's paragraph is worse than one
#: that says the first few words of it. The tooltip still has all of it.
_COMMENT_MAX = 220
#: Space between the name and its comment.
_COMMENT_GAP = 10

_icon_cache: dict[tuple, QIcon] = {}


def reset_mark_cache() -> None:
    """Drop the cached dots. Call after a theme or zoom change: the colours
    and the box size are both baked into the pixmap."""
    _icon_cache.clear()


def marks_enabled() -> bool:
    """View ▸ Dropdown Status Marks. Read per dropdown built, not cached here:
    :func:`x3.ui.settings.load_settings` already caches on the file's stat,
    so this is a stat call and not a parse."""
    return load_dropdown_marks()


def marks_for(client: Any, path: Any) -> dict[int, Any]:
    """``{object id: EnumMark}`` for a source table, or ``{}``.

    Asked for by name rather than by type: every UI test writes its own client
    double, and a feature that made twenty of them fail to construct would be
    paid for in test churn rather than in anything the user sees.
    """
    if client is None or not path or not marks_enabled():
        return {}
    hook = getattr(client, "enum_marks", None)
    if hook is None:
        return {}
    try:
        return hook(tuple(path)) or {}
    except Exception:  # noqa: BLE001 - a mark we cannot read is not a crash
        return {}


def mark_icon(state: str) -> QIcon:
    """The dot for one state, or a null icon when there is nothing to say.

    Filled green for a running entry, a hollow red ring for one that is up in
    the config and down on the wire, and a flat grey disc for one switched
    off -- three shapes, so the difference survives a monochrome screen or a
    reader who does not separate red from green.
    """
    if state not in ("up", "down", "disabled"):
        return QIcon()
    box = max(6, style.scale_px(_DOT_BOX))
    colour = {
        "up": style.MARK_UP,
        "down": style.INFO_RED,
        "disabled": style.TEXT_DIM,
    }[state]
    key = (state, box, colour)
    hit = _icon_cache.get(key)
    if hit is not None:
        return hit
    # Painted at the screen's device pixel ratio: a 9px disc rasterised at 1x
    # and scaled up by a HiDPI compositor is a blurred smudge next to text that
    # was rendered at 2x (see the dev display's dpr of 2).
    app = QApplication.instance()
    dpr = float(app.devicePixelRatio()) if app is not None else 1.0
    pm = QPixmap(int(box * dpr), int(box * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Logical coordinates: the pixmap carries the ratio, so the painter works
    # in the same px the rest of the row is laid out in. One px of inset all
    # round keeps the stroke inside the box instead of half off its edge.
    body = QRect(1, 1, box - 2, box - 2)
    col = QColor(colour)
    if state == "down":
        painter.setPen(QPen(col, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
    else:
        painter.setPen(QPen(col.darker(125), 1))
        painter.setBrush(col)
    painter.drawEllipse(body)
    painter.end()
    icon = QIcon(pm)
    # The same dot in every mode. Qt washes an icon out on a disabled widget,
    # which is right for a control and wrong for this: a locked form is where
    # you are *reading* the state, and a greyed-out dot is a greyed-out fact.
    # It is the flags column's argument -- Winbox greys a read-only row's
    # values and still prints its flags in full ink.
    for mode in (QIcon.Mode.Disabled, QIcon.Mode.Selected, QIcon.Mode.Active):
        icon.addPixmap(pm, mode)
    _icon_cache[key] = icon
    return icon


class MarkDelegate(QStyledItemDelegate):
    """Default item painting, plus the row's comment in dim text after it.

    A second column would be the obvious way to draw this and the wrong one: a
    QComboBox popup is a one-column list view, and giving it a model with two
    would mean replacing the model every dropdown in the app builds. Painting
    after the base delegate leaves the icon, the selection and the elision
    exactly as Qt drew them.
    """

    def paint(self, painter, option, index) -> None:  # noqa: N802
        comment = index.data(MARK_COMMENT_ROLE) or ""
        state = index.data(MARK_STATE_ROLE) or ""
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        if state == "disabled":
            # The same greying the list gives a disabled row. Only the name:
            # the comment is already dim, and dimming it twice loses it.
            dim = QColor(style.TEXT_DIM)
            for group in (
                opt.palette.ColorGroup.Active,
                opt.palette.ColorGroup.Inactive,
                opt.palette.ColorGroup.Normal,
            ):
                opt.palette.setColor(group, opt.palette.ColorRole.Text, dim)
        super().paint(painter, opt, index)
        if not comment:
            return
        rect = self._comment_rect(opt, index)
        if rect is None or rect.width() <= 0:
            return
        painter.save()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        ink = QColor(
            opt.palette.color(opt.palette.ColorRole.HighlightedText)
            if selected else QColor(style.TEXT_DIM)
        )
        if selected:
            # TEXT_DIM on the highlight bar is unreadable on half the themes,
            # so a highlighted row's comment takes the selection's own ink and
            # is set apart by a touch of transparency instead.
            ink.setAlphaF(0.85)
        painter.setPen(QPen(ink))
        text = painter.fontMetrics().elidedText(
            comment, Qt.TextElideMode.ElideRight, rect.width()
        )
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802
        size = super().sizeHint(option, index)
        comment = index.data(MARK_COMMENT_ROLE) or ""
        if comment:
            width = option.fontMetrics.horizontalAdvance(comment)
            cap = max(40, style.scale_px(_COMMENT_MAX))
            size.setWidth(
                size.width() + style.scale_px(_COMMENT_GAP) + min(width, cap)
            )
        return size

    def _comment_rect(self, opt, index):
        """Where the comment goes: after the name, to the end of the row."""
        widget = getattr(opt, "widget", None)
        qstyle = widget.style() if widget is not None else QApplication.style()
        text_rect = qstyle.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget
        )
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        used = opt.fontMetrics.horizontalAdvance(name)
        left = text_rect.left() + used + style.scale_px(_COMMENT_GAP)
        if left >= text_rect.right():
            return None
        rect = text_rect.adjusted(0, 0, 0, 0)
        rect.setLeft(left)
        return rect


def decorate_items(widget, marks: dict, *, wire_of=None) -> None:
    """Hang the marks on an already-filled combo or list, by wire value.

    Takes either -- a QComboBox addresses its items by index and a QListWidget
    by object, and both pickers offer the same interfaces (the dropdown and
    the Add Several dialog behind it), so they had better agree about which
    ones are up.

    ``wire_of`` reads the wire value out of item *i*; the default is where
    each of the two keeps it. An item whose value has no mark is *cleared*
    rather than left alone: this runs again on a refresh, and a dot nobody
    cleared is a dot about the previous read.
    """
    if widget is None:
        return
    combo = hasattr(widget, "setItemIcon")
    if combo:
        getter = wire_of or (lambda i: widget.itemData(i))
    else:
        getter = wire_of or (
            lambda i: widget.item(i).data(Qt.ItemDataRole.UserRole)
        )
    for i in range(widget.count()):
        mark = marks.get(getter(i)) if marks else None
        state = getattr(mark, "state", "") if mark is not None else ""
        comment = getattr(mark, "comment", "") if mark is not None else ""
        icon = mark_icon(state)
        if combo:
            widget.setItemIcon(i, icon)
            widget.setItemData(i, state, MARK_STATE_ROLE)
            widget.setItemData(i, comment, MARK_COMMENT_ROLE)
            if comment:
                widget.setItemData(i, comment, Qt.ItemDataRole.ToolTipRole)
            continue
        item = widget.item(i)
        if item is None:
            continue
        item.setIcon(icon)
        item.setData(MARK_STATE_ROLE, state)
        item.setData(MARK_COMMENT_ROLE, comment)
        if comment:
            item.setData(Qt.ItemDataRole.ToolTipRole, comment)


def install_delegate(widget) -> None:
    """Put the comment painter on a combo's popup (or on a list widget)."""
    view = widget.view() if isinstance(widget, QComboBox) else widget
    if view is None or isinstance(view.itemDelegate(), MarkDelegate):
        return
    delegate = MarkDelegate(view)
    view.setItemDelegate(delegate)
    # Kept alive by the view's parentage, but held here too: a QComboBox
    # replaces its view on some style changes and the delegate would go with it.
    widget.setProperty("_markDelegate", delegate)


def apply_marks(widget, marks: dict, *, wire_of=None) -> bool:
    """Decorate a picker and size its icons. False when there is nothing to
    show, so a caller can leave the widget exactly as it was."""
    if not marks:
        return False
    box = max(6, style.scale_px(_DOT_BOX))
    setter = getattr(widget, "setIconSize", None)
    if setter is not None:
        setter(QSize(box, box))
    decorate_items(widget, marks, wire_of=wire_of)
    install_delegate(widget)
    return True


class MarkedCombo(QComboBox):
    """A dropdown that re-reads its marks as it opens.

    A name going stale is invisible; a status dot going stale is a lie -- it
    says "up" about a link that dropped while the form sat open. The read is
    the one moment it is worth spending, and ``refresh_enum_rows`` bounds it
    twice: it skips a table read seconds ago, and it waits two seconds rather
    than the client's full timeout, because this is the UI thread.

    Only the *marks* are re-applied, never the item list. Repopulating a box
    the user is about to click could move the selection under them, and an
    interface that appeared since the form opened is the next form's business.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mark_client = None
        self._mark_path: tuple[int, ...] | None = None
        self._mark_provider = None

    def set_mark_source(self, client, path, *, provider=None) -> None:
        """Remember where to re-read from, and how to key the answer.

        ``provider`` returns the marks in the key space this box's item data
        uses -- an id for most enums, but the name itself for a string one,
        whose label *is* the wire value. Without it the refresh would look up
        b'ether1' in a map keyed by 2 and quietly clear every dot.

        Decorating is the caller's business: it happens once the box is
        filled, and only then.
        """
        self._mark_client = client
        self._mark_path = tuple(path) if path else None
        self._mark_provider = provider

    def refresh_marks(self) -> None:
        client, path = self._mark_client, self._mark_path
        if client is None or not path or not marks_enabled():
            return
        hook = getattr(client, "refresh_enum_rows", None)
        try:
            if hook is not None and not hook(path):
                return      # nothing newer than what is already on the items
            provider = self._mark_provider
            marks = provider() if provider is not None else marks_for(client, path)
            apply_marks(self, marks)
        except Exception:  # noqa: BLE001 - a stale dot beats a broken popup
            pass

    def showPopup(self) -> None:  # noqa: N802
        self.refresh_marks()
        super().showPopup()
