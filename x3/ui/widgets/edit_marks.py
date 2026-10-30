"""Saying *which* entries of an array changed, rather than washing the lot.

A scalar field has one value, so one amber wash over its box says everything
there is to say. An array does not: a bridge VLAN's Tagged ports is twelve
rows, and washing all twelve because one was pulled out says only "something
in here moved" -- which on a twelve-port bridge is no help at all.

So an array marks the rows that really differ, and a removal -- which leaves no
row behind to mark -- leaves a mark in the gap it left, between the two rows
that closed over it. :func:`edit_marks` is the shortest edit script over the
before and after lists, so rows that merely shuffled up to close a gap are not
marked at all.

Shared because three editors are arrays with rows: the array of dropdowns
(Tagged, Ethernet ▸ Advertise), the growable list of boxes (a bridge VLAN's
VLAN IDs, DNS servers) and the network array (IP ▸ Services' Available From).
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from .. import a11y, style

#: The amber a modified editor wears, at full strength -- one visual language
#: for "this is not what the device sent", and skin-proof.
COLOUR = (245, 158, 11)


def apply_dirty_style(widget: QWidget, dirty: bool) -> None:
    """The amber wash itself, on one widget.

    Kept here rather than in the property form because the row editors reach
    for it too: an array marks its own rows, and each mark is this same wash on
    one box instead of on the whole list.
    """
    prev = bool(widget.property("dirtyField"))
    if not dirty and not prev:
        # Nothing applied, nothing to clear. Worth the early exit: every row of
        # a freshly opened form lands here, and unpolish/polish is not free.
        return
    widget.setProperty("dirtyField", dirty)
    # The wash is the only thing that says this field has been touched, and a
    # queue-mode form can hold a dozen of them before anything is sent.
    a11y.state(widget, "edited", dirty)
    if dirty:
        # Soft amber wash + accent border — readable on light and dark skins.
        widget.setStyleSheet(
            f"border: 1px solid {style.SEL_BG};"
            f"border-radius: 3px;"
            f"background-color: rgba({COLOUR[0]}, {COLOUR[1]}, {COLOUR[2]}, 0.20);"
        )
    elif prev:
        # Only clear styles we applied for dirty highlighting.
        widget.setStyleSheet("")
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def edit_marks(
    old: list[Any], new: list[Any]
) -> tuple[set[int], dict[int, list[Any]]]:
    """Which entries of ``new`` differ, and where entries of ``old`` went.

    Returns ``(indices into new, {index into new: what was removed there})``.
    A shortest edit script, so pulling ether3 out of the middle marks the seam
    it left instead of every row that shuffled up to close it.
    """
    if old == new:
        return set(), {}
    changed: set[int] = set()
    gone: dict[int, list[Any]] = {}
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        if j2 > j1:
            changed.update(range(j1, j2))     # added here, or replaced
        elif i2 > i1:
            gone.setdefault(j1, []).extend(old[i1:i2])
    return changed, gone


class RemovalMarks(QWidget):
    """The thin line that says a row used to be here.

    Drawn on a transparent overlay rather than in the host's own paintEvent:
    children paint after their parent, and the mark has to be visible across a
    1px layout gap between two rows.
    """

    #: How thick the mark is, and how far its end ticks reach down the rows.
    _THICK = 2
    _TICK = 3

    def __init__(self, rows_of: Callable[[], list[QWidget]], parent=None) -> None:
        super().__init__(parent)
        self._rows_of = rows_of          # () -> list[QWidget], top to bottom
        self._gaps: dict[int, str] = {}
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_gaps(self, gaps: dict[int, str]) -> None:
        if gaps == self._gaps:
            return
        self._gaps = dict(gaps)
        self.setVisible(bool(gaps))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._gaps:
            return
        rows = self._rows_of()
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(*COLOUR))
        width = self.width()
        for index in sorted(self._gaps):
            if index < len(rows):
                y = rows[index].geometry().top()
            elif rows:
                y = rows[-1].geometry().bottom() + 1
            else:
                y = 0
            top = max(0, y - self._THICK // 2)
            painter.drawRect(0, top, width, self._THICK)
            # Ticks at both ends, so the mark reads as a seam rather than as a
            # border belonging to either neighbour.
            for x in (0, width - self._THICK):
                painter.drawRect(x, top - self._TICK, self._THICK,
                                 self._TICK * 2 + self._THICK)
        painter.end()


class MarkedRows(QWidget):
    """The rows host, with :class:`RemovalMarks` kept over it."""

    def __init__(self, rows_of: Callable[[], list[QWidget]], parent=None) -> None:
        super().__init__(parent)
        self.marks = RemovalMarks(rows_of, self)
        self.marks.setVisible(False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.marks.setGeometry(self.rect())
        self.marks.raise_()


class LineRowMarks:
    """Per-row edit marks for a growable column of one-line boxes.

    Mixed into the two editors that are exactly that -- a range list and a
    network array. It expects the host to keep

    * ``_rows``       -- ``(row widget, box)`` pairs, top to bottom
    * ``_original``   -- the texts the device sent, as ``texts()`` reports them
    * ``_rows_host``  -- a :class:`MarkedRows` the rows are laid out in
    * ``_add``        -- the ``+`` button

    An empty box is not an entry: ``texts()`` leaves it out, so a row's index
    and its entry's index are not the same number once someone has pressed
    ``+``. The marks are computed over the entries and then mapped back onto
    the rows that carry them -- without that step, adding a blank row shifted
    every mark below it onto the wrong box.
    """

    def _entry_rows(self) -> tuple[list[str], list[int]]:
        """(the entries, the row index each one lives in)."""
        texts: list[str] = []
        rows: list[int] = []
        for index, (_row, box) in enumerate(self._rows):
            text = box.text().strip()
            if text:
                texts.append(text)
                rows.append(index)
        return texts, rows

    def _edit_marks(self) -> tuple[set[int], dict[int, str]]:
        """Rows that differ from what the device sent, and the removal gaps.

        Both keyed by *row* index, ready to paint. A gap past the last entry is
        keyed one past the last row, which :class:`RemovalMarks` draws under it.
        """
        new, row_of = self._entry_rows()
        changed, gone = edit_marks(list(self._original), new)
        at_row = lambda j: row_of[j] if j < len(row_of) else len(self._rows)
        return (
            {at_row(j) for j in changed},
            {at_row(j): ", ".join(str(t) for t in texts)
             for j, texts in gone.items()},
        )

    def show_dirty(self, dirty: bool) -> None:
        """Mark the rows that differ, not the whole list.

        Called by the property form's ``_apply_dirty_style``, which paints
        every other editor as a whole. Adding one VLAN to a bridge VLAN's list
        washed every range in it and said nothing about which one was new.
        """
        changed, gaps = self._edit_marks() if dirty else (set(), {})
        for index, (_row, box) in enumerate(self._rows):
            apply_dirty_style(box, index in changed)
        self._rows_host.marks.set_gaps(gaps)
        if gaps:
            # The mark is a line on a screen; say it in words too, since a
            # removed row leaves nothing for a screen reader to land on.
            removed = ", ".join(gaps[at] for at in sorted(gaps))
            self._rows_host.setToolTip(f"Removed: {removed}")
            a11y.describe(self, f"{removed} removed")
        else:
            self._rows_host.setToolTip("")
            a11y.describe(self, "")
        # An emptied list has no row left to carry the mark, and a zero-height
        # host has nowhere to paint it: the + button is what is left to wash.
        apply_dirty_style(self._add, bool(gaps) and not self._rows)
