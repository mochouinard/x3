"""A ``type:group`` fold -- Winbox's collapsible group of form rows.

A named ``group`` in the .jg is not a label over its fields, it is a fold. Where
it is declared Winbox draws a rule with a triangle and the group's name, and the
group's rows are hidden until the triangle is clicked::

                 Nth: [            ] ▼
    ─ ▲ Connection Limit ───────────────
               Limit: [!] [100] / [32]
    ─ ▼ Limit ─────────────────────────
    ─ ▼ Dst. Limit ────────────────────

Read off Winbox 3.43 on 7.21.1: a firewall rule's Extra tab (above) and a CAPs
Rate, whose six rate grids are six folds. ▼ is closed, ▲ is open, and the rows
that follow a fold in declaration order but belong to no group -- Src. Address
Type and friends above -- are unaffected by it.

Most groups also carry an id, and it is a *bool*: ``{name:'Limit', type:'group',
id:'b1a0', …}`` is the firewall's "this match is in use" flag. That flag is the
fold's state, which is why opening a fold is an edit and not just a view change:
the group's rows mean nothing to the router until the flag is set. A group with
no id (a CAPs Rate's Basic Rates) folds locally and writes nothing.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

from .. import a11y, style
from ..i18n import tr, trf
from .section import section_header

#: Air above the rule, so a fold reads as a break between groups of rows.
_TOP_PAD = 5


class GroupFold(QWidget):
    """The header row of a group: a rule, a triangle, and the group's name."""

    #: The fold opened or closed. The form listens to move its member rows.
    folded = Signal(bool)
    #: Same signal the other editors use, so dirty tracking needs no case.
    changed = Signal()

    def __init__(
        self,
        title: str,
        *,
        expanded: bool = False,
        flag: bool = False,
        readonly: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.title = str(title or "")
        #: Whether the group owns a wire flag (its `id`) that the fold writes.
        self.flag = bool(flag)
        self._expanded = bool(expanded)
        self._readonly = bool(readonly)

        self._arrow = QToolButton()
        self._arrow.setAutoRaise(True)
        self._arrow.setCursor(Qt.CursorShape.ArrowCursor)
        # A fold is a tab stop, because its rows do not exist for the keyboard
        # until it is open: a closed fold nothing can focus walls them off
        # completely. Space (the button's own key) and Enter flip it; Right
        # opens and Left closes, the way a tree does -- see keyPressEvent.
        self._arrow.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._arrow.setAccessibleName(self.title)
        self._arrow.setStyleSheet(
            f"QToolButton:focus {{ border: 1px solid {style.SEL_BG}; }}"
        )
        self._arrow.setFixedSize(14, 14)
        self._arrow.clicked.connect(self.toggle)
        self._arrow.installEventFilter(self)
        # Focus given to the fold lands on its triangle; a tab chain names it
        # through focusProxy() rather than reaching into the fold.
        self.setFocusProxy(self._arrow)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, _TOP_PAD, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(section_header(self.title, self._arrow))
        self._paint_arrow()
        self._original = self.editor_value()

    # --- folding -------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802
        """The triangle's keys, taken before the button sees them.

        A QToolButton leaves Left/Right *accepted* while doing nothing with
        them -- they walk the group only on an auto-exclusive button -- so a
        fold waiting for the key to propagate up to it waited forever. Space is
        left to the button: it clicks, and a click already toggles.
        """
        if (
            obj is self._arrow
            and event.type() == QEvent.Type.KeyPress
            and self._fold_key(event)
        ):
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if not self._fold_key(e):
            super().keyPressEvent(e)

    def _fold_key(self, e) -> bool:
        """Right opens the fold, Left closes it, Enter flips it -- a tree's keys.

        True when the key was used. Only a key that would change something is
        taken: Left on an already-closed fold stays the ordinary walk to the
        previous field rather than looking like a dead key.
        """
        if e.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        key = e.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Plus) and not self._expanded:
            self.set_expanded(True)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Minus) and self._expanded:
            self.set_expanded(False)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle()
        else:
            return False
        e.accept()
        return True

    @property
    def expanded(self) -> bool:
        return self._expanded

    def _paint_arrow(self) -> None:
        self._arrow.setArrowType(
            Qt.ArrowType.UpArrow if self._expanded else Qt.ArrowType.DownArrow
        )
        # Which way the triangle points is the whole state of the group, and
        # the rows behind a shut one do not exist for the keyboard either.
        a11y.state(self._arrow, "expanded", self._expanded)
        a11y.state(self._arrow, "collapsed", not self._expanded)
        self._arrow.setToolTip(trf(
            "Hide {title} (Space)" if self._expanded else "Show {title} (Space)",
            title=tr(self.title),
        ))

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        # A locked form still folds: hiding rows is not an edit unless the
        # group carries a flag, and a read-only form has nothing to write.
        if self._readonly and self.flag:
            return
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._paint_arrow()
        self.folded.emit(expanded)
        if self.flag:
            self.changed.emit()

    # --- the flag, for the write path ----------------------------------

    def editor_value(self) -> bool | None:
        """The group's flag: open means the match is in use."""
        return self._expanded if self.flag else None

    def is_dirty(self) -> bool:
        return self.flag and self.editor_value() != self._original

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)

    def label(self) -> str:
        return "yes" if self._expanded else "no"
