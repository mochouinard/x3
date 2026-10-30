"""The vertical column separator Winbox draws between table cells.

Why a delegate and not one line of QSS: ``QTableView::item { border-right: … }``
draws exactly this, but styling ``::item`` hands cell painting to
QStyleSheetStyle, which then ignores the model's ``BackgroundRole``. That
silently kills the arrival tint on new rows — the cells keep their borders and
quietly lose their colour. Painting the rule after the default delegate has run
leaves every background, the alternating stripe and the selection intact.

The same delegate owns the cell's horizontal inset, for the same reason: the
padding is not stylable either. Qt's item painting hard-codes it as
``PM_FocusFrameHMargin + 1`` — 3px a side under Fusion — which is airier than
Winbox and does not match the padding the header QSS uses above it.
:data:`x3.ui.style.CELL_PAD` is the number both now honour, in both
directions: a theme asking for more than Qt's own inset gets a roomier cell,
not a silently dropped number.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from .. import style


#: Last (style, pads) the inset was worked out for, and the answer. Asking the
#: style is a stylesheet rule lookup — cheap once, but this is on the path of
#: every cell of every repaint. Both parts of the key change only when a theme
#: is applied, and the style is held by the app for as long as it is in use, so
#: a stale entry cannot outlive its style.
_inset_cache: tuple | None = None


def reclaimed_padding(option) -> int:
    """Px of Qt's built-in cell padding to move by, per side.

    Positive tightens the cell; negative pads it out. Not clamped at zero, so a
    theme asking for a roomier cell than the style offers is expressed rather
    than dropped.

    Usually zero, and that is not a bug. ``PM_FocusFrameHMargin`` is answered by
    QStyleSheetStyle, which derives it from the padding our own sheet sets — so
    the metric already tracks ``CELL_PAD`` and there is nothing left to correct.
    The correction matters only where the two can diverge. Measured end to end,
    a cell's text inset moves 1px for 1px with ``cell_pad`` in both directions,
    which is the behaviour that counts.

    Symmetric on purpose: per-side insets would need a delegate on every cell,
    and most lists deliberately run on Qt's own for speed (see
    ``ListPane._choose_item_delegate``), so a left/right split would have
    silently worked on comment and tree lists only.
    """
    global _inset_cache
    widget = getattr(option, "widget", None)
    qstyle = widget.style() if widget is not None else QApplication.style()
    pad = int(style.CELL_PAD)
    key = (qstyle, pad)
    if _inset_cache is not None and _inset_cache[0] == key:
        return _inset_cache[1]
    builtin = (
        qstyle.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, option, widget) + 1
    )
    shift = builtin - pad
    _inset_cache = (key, shift)
    return shift


class ColumnRuleDelegate(QStyledItemDelegate):
    """Default cell painting, re-inset, plus a hairline down the right edge."""

    def paint(self, painter, option, index) -> None:  # noqa: N802
        self.paint_cell(painter, option, index)
        self.paint_column_rule(painter, option)

    def sizeHint(self, option, index):  # noqa: N802
        # Auto-sizing a column asks the delegate how wide the cell wants to be,
        # so the inset comes off here too — otherwise every column goes on
        # reserving room it no longer draws in, or (padding out) too little.
        size = super().sizeHint(option, index)
        total = 2 * reclaimed_padding(option)
        if total and size.width() > total:
            size.setWidth(size.width() - total)
        return size

    def paint_cell(self, painter, option, index) -> None:
        """Qt's own cell painting, inset by CELL_PAD instead of its own 3px.

        Qt measures the text from the rect it is handed, so the way to tighten
        the inset is to hand it a rect *wider* than the cell by exactly what we
        want back, and clip to the real one. Everything the base delegate draws
        — background, selection, icon, elided text — then lands where it would
        have, minus the slack.
        """
        shift = reclaimed_padding(option)
        if not shift:
            super().paint(painter, option, index)
            return
        wide = QStyleOptionViewItem(option)
        wide.rect = option.rect.adjusted(-shift, 0, shift, 0)
        painter.save()
        painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
        super().paint(painter, wide, index)
        painter.restore()

    @staticmethod
    def paint_column_rule(painter, option) -> None:
        """Draw the separator for one cell. Re-read per paint so a theme
        switch takes effect without rebuilding every view."""
        if not style.COLUMN_RULES:
            return
        painter.save()
        painter.setPen(QPen(QColor(style.GRIDLINE), 1))
        x = option.rect.right()
        painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
        painter.restore()
