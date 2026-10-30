"""Replacing the text in a self-refreshing view without losing the reader.

The Log window and the "changed elsewhere" window both show a transcript that
grows while it is open, and both used to redraw it with a plain
``setPlainText``. That is a wholesale replacement, and Qt treats it as one: the
caret goes back to the start of the document, the selection is gone, and the
scrollbar drops to the top. So scrolling up in the Log to read the request that
went wrong lasted until the next entry arrived -- 1.5s -- and then threw you
back to line 0. Dragging over a few lines to copy them lost the selection the
same way.

Both windows guarded the tail case only ("if we were at the bottom, go back to
the bottom"), which is the one case where being moved is what the reader wants.
This is the rest of it.
"""

from __future__ import annotations

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

#: How near the bottom still counts as parked at the tail. A scrollbar lands a
#: pixel or two off the maximum often enough that an exact test reads as "the
#: user has scrolled up" when they have not.
TAIL_SLACK = 4


def at_tail(view: QPlainTextEdit) -> bool:
    """Is the view showing the end of its text?"""
    bar = view.verticalScrollBar()
    return bar.value() >= bar.maximum() - TAIL_SLACK


def set_text_keeping_place(
    view: QPlainTextEdit, text: str, *, follow: bool = False
) -> bool:
    """Put ``text`` in ``view``, leaving the reader where they were.

    Returns True when the text was actually replaced -- an unchanged transcript
    is not redrawn at all, which is what keeps an idle window perfectly still.

    Three cases, in the order they are decided:

    * **The reader has selected something.** That is the strongest statement
      anyone makes in a read-only view -- they are copying it -- so the
      selection and the scroll position are both kept, tail or no tail, and the
      new lines pile up below unseen. ``follow`` does not override this; a
      window that yanks the text out from under a drag is unusable for the one
      thing it is for.
    * **The reader is at the tail** (or has asked to be pinned there with
      Follow). Then being moved *is* the point: the caret goes to the end and
      the view goes with it.
    * **Otherwise** they have scrolled up to read something. Caret and
      scrollbar both go back exactly where they were.
    """
    if text == view.toPlainText():
        return False

    bar = view.verticalScrollBar()
    tail = follow or at_tail(view)
    where = bar.value()
    cursor = view.textCursor()
    anchor, position = cursor.anchor(), cursor.position()
    selected = cursor.hasSelection()

    view.setPlainText(text)

    if selected or not tail:
        limit = len(text)
        restored = view.textCursor()
        restored.setPosition(min(anchor, limit))
        restored.setPosition(
            min(position, limit), QTextCursor.MoveMode.KeepAnchor
        )
        # setTextCursor scrolls the caret into view, so the scrollbar is set
        # after it and not before -- where the reader was is the authority,
        # not where the caret happens to be.
        view.setTextCursor(restored)
        bar.setValue(min(where, bar.maximum()))
    else:
        view.moveCursor(QTextCursor.MoveOperation.End)
        bar.setValue(bar.maximum())
    return True
