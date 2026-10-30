"""Stop dropdowns and spin boxes eating a form's scroll.

Qt gives ``QComboBox`` and ``QAbstractSpinBox`` a focus policy of
``WheelFocus``: rolling the wheel over one focuses it *and* changes its
value. On a long property form -- a wireless interface has upwards of fifty
rows -- scrolling past means silently editing whatever the pointer happened
to cross, and the form does not scroll while it happens. The user finds out
when they hit Apply.

So **a plain wheel roll never changes a value**: the event goes on to the
scroll area the user was actually aiming at. Focus does not buy the wheel back
either. It used to -- the rule was "unfocused scrolls, focused adjusts" -- but
a field stays focused for as long as it was last clicked in, which on a form
still being read is most of them, and the box you just typed a VLAN into is
exactly the one a stray roll must not rewrite.

**Ctrl+wheel is the deliberate one** and passes straight through to the widget,
focused or not. Nothing else in the app claims it (zoom is Ctrl+= / Ctrl+-),
and a modifier is not something the hand does by accident while reading.

Sliders and spin boxes are guarded the same way -- one rule for everything
whose value a wheel can move. Scroll bars deliberately are not: scrolling is
what they are for.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollBar,
    QSlider,
    QWidget,
)

#: Widget types that change value on a wheel roll.
WHEELY = (QComboBox, QAbstractSpinBox, QSlider)


def _scrolling_ancestor(widget: QWidget) -> QAbstractScrollArea | None:
    """The scroll area the wheel was meant for, if there is one."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


class _WheelGuard(QObject):
    """Event filter: a plain wheel roll over a value editor scrolls instead."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.Wheel:
            return False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False              # deliberate: Ctrl+wheel still adjusts
        try:
            area = _scrolling_ancestor(obj)
        except RuntimeError:          # the widget went away under the pointer
            return True
        if area is not None:
            # Hand it on rather than dropping it, so the form still moves.
            QApplication.sendEvent(area.viewport(), event)
        return True


#: One filter for the whole app. Event filters are stateless here, and a
#: per-widget QObject would be thousands of objects for nothing.
_GUARD = _WheelGuard()


def guard_wheel(root: QWidget) -> int:
    """Guard ``root`` and everything under it. Returns how many were guarded.

    Safe to call more than once on the same widget: Qt ignores a duplicate
    ``installEventFilter`` for the same filter object.
    """
    if root is None:
        return 0
    targets = [root] if isinstance(root, WHEELY) else []
    targets += [
        w for w in root.findChildren(QWidget)
        if isinstance(w, WHEELY) and not isinstance(w, QScrollBar)
    ]
    for w in targets:
        if w.focusPolicy() == Qt.FocusPolicy.WheelFocus:
            # Otherwise the wheel gives it focus first and then the guard,
            # seeing a focused widget, waves the change through.
            w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.installEventFilter(_GUARD)
    return len(targets)
