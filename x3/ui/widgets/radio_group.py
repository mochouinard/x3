"""A small enum as radio buttons -- the .jg's ``radio:1``.

Winbox draws a two-to-five member enum as radio buttons, not a dropdown: PPP's
``Use IPv6  ( ) no  ( ) yes  ( ) required  ( ) default``, the wireless Data
Rates ``Rate ( ) default ( ) configured``, a bridge's Protocol Mode. Every one
of the 67 fields declaring it in 7.21 has five members or fewer, so there is no
size at which this has to fall back to a combo.

``compact:1`` is an ordinary labelled row -- name in the label column, buttons
beside it. **Without** it the group is a section, which is a different shape
altogether: the field name sits in a rule spanning the form, and the buttons
go on one row under it, indented::

    ─ Rate ─────────────────────────────────
      (•) default   ( ) configured

Verified against Winbox 3.43 on 7.21.1 (a wlan's Data Rates tab, and a PPP
profile's Protocols tab, where all four groups are drawn that way). An earlier
reading of `compact` had it choosing row-vs-stack, which is what put ``Rate``
in the label column with its two buttons stacked underneath.

The editor reports the *wire* value of the checked member through
``editor_value()``, the same thing a combo reports through currentData(), so
the dirty-tracking and write paths need no special case.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .section import section_header

#: Air above a section rule, so it reads as a break rather than a squashed row.
_SECTION_TOP_PAD = 6
#: How far Winbox insets the buttons from the rule above them.
_SECTION_INDENT = 14


class RadioGroupEditor(QWidget):
    """Radio buttons over ``(wire value, label)`` choices."""

    changed = Signal()

    #: Beyond this many members a compact row wraps into two columns rather
    #: than pushing the form wider than the window.
    _WRAP_AT = 5

    def __init__(
        self,
        choices: list[tuple[Any, str]],
        value: Any = None,
        *,
        compact: bool = True,
        readonly: bool = False,
        title: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # A blank label is a placeholder in the .jg's map (Wireless Protocol
        # declares two of them), never a button Winbox draws.
        self._choices = [(w, str(lab)) for w, lab in choices if str(lab).strip()]
        self._readonly = readonly
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[tuple[Any, QRadioButton]] = []
        #: Set when this group is a section of its own rather than a labelled
        #: row: the form adds it spanning both columns and draws no label.
        self.title = str(title) if title else None

        # A section owns its title, so the buttons live one level down.
        host: QWidget = self
        if self.title:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, _SECTION_TOP_PAD, 0, 0)
            outer.setSpacing(3)
            outer.addWidget(section_header(self.title))
            host = QWidget()
            outer.addWidget(host)

        if compact and len(self._choices) <= self._WRAP_AT:
            lay: Any = QHBoxLayout(host)
            lay.setSpacing(10)
        elif compact:
            lay = QGridLayout(host)
            lay.setHorizontalSpacing(10)
            lay.setVerticalSpacing(2)
        else:
            lay = QVBoxLayout(host)
            lay.setSpacing(2)
        lay.setContentsMargins(_SECTION_INDENT if self.title else 0, 0, 0, 0)

        for i, (wire, label) in enumerate(self._choices):
            btn = QRadioButton(label)
            btn.setEnabled(not readonly)
            btn.toggled.connect(self._on_toggled)
            self._group.addButton(btn, i)
            self._buttons.append((wire, btn))
            if isinstance(lay, QGridLayout):
                lay.addWidget(btn, i // 2, i % 2)
            else:
                lay.addWidget(btn)
        if isinstance(lay, QHBoxLayout):
            lay.addStretch(1)

        self.set_value(value)
        self._original = self.editor_value()

    def _on_toggled(self, checked: bool) -> None:
        # One click flips two buttons; only the one being checked is news.
        if checked:
            self.changed.emit()

    # --- value ---------------------------------------------------------

    def set_value(self, value: Any) -> None:
        for wire, btn in self._buttons:
            if wire == value:
                btn.setChecked(True)
                return
        # Nothing matched: leave every button clear rather than picking the
        # first, which would show a value the device never sent.
        prev = self._group.exclusive()
        self._group.setExclusive(False)
        for _wire, btn in self._buttons:
            btn.setChecked(False)
        self._group.setExclusive(prev)

    def editor_value(self) -> Any:
        for wire, btn in self._buttons:
            if btn.isChecked():
                return wire
        return None

    def is_dirty(self) -> bool:
        return self.editor_value() != self._original

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-select from the value just read off the device (see Apply)."""
        self.set_value(raw)
        self._original = self.editor_value()

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        for _wire, btn in self._buttons:
            btn.setEnabled(not locked)
