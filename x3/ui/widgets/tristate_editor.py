"""Editor for a ``multitristate`` field — a set of flags each of which can be
must-be-set, must-be-clear, or ignored.

Firewall TCP Flags is the canonical one: ``tcp-flags=syn,!ack`` means SYN must
be set and ACK must be clear, the other six ignored. On the wire it is two u32
bitfields keyed by the field id and its ``maskid``::

    value (0x56) = OR(1<<i for flags that must be SET)
    mask  (0x57) = OR(1<<i for flags that are EXAMINED at all)

so a flag is *set* when its mask AND value bits are on, *clear* when mask is on
and value is off, and *ignored* when mask is off. The flag at enum index ``i``
occupies bit ``i`` (confirmed against the router: ``tcp-flags=syn,!ack`` round
trips through this mapping). Values are emitted as ``(tag, value)`` pairs the
write path passes through untouched.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QToolButton,
    QWidget,
)

from x3.proto import message as m

#: The three states a flag cycles through on click.
_IGNORE, _SET, _CLEAR = 0, 1, 2

#: How each state reads out loud. "any" is the ignore state: the rule does not
#: care about this flag either way.
_SPOKEN = {_IGNORE: "any", _SET: "must be set", _CLEAR: "must be clear"}


def _as_int(value: Any) -> int:
    try:
        if isinstance(value, (bytes, bytearray)):
            return int.from_bytes(value, "little")
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, list) and value:
            return _as_int(value[0])
    except (TypeError, ValueError):
        return 0
    return 0


class MultiTristateEditor(QWidget):
    """A row of flag buttons; each cycles ignore → set → clear."""

    changed = Signal()

    def __init__(
        self,
        fld,
        values_by_code: dict[int, Any] | None = None,
        *,
        readonly: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._fld = fld
        self._value_code = fld.code
        self._mask_code = fld.maskid
        self._readonly = readonly
        # enum is {bit_index: name}; sort by index so bit i == flag i.
        self._flags: list[tuple[int, str]] = sorted(
            (int(i), str(n)) for i, n in (fld.enum or {}).items()
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self._btns: dict[int, QToolButton] = {}
        for idx, name in self._flags:
            b = QToolButton()
            b.setText(name)
            b.setToolTip(f"{name}: click to require set / clear / ignore")
            b.setEnabled(not readonly)
            b.clicked.connect(lambda _=False, i=idx: self._cycle(i))
            self._btns[idx] = b
            row.addWidget(b)
        row.addStretch(1)

        values = values_by_code or {}
        self._state: dict[int, int] = {}
        self._load(_as_int(values.get(self._value_code)),
                   _as_int(values.get(self._mask_code)) if self._mask_code else 0)
        self._original = dict(self._state)

    # --- state ---------------------------------------------------------

    def _load(self, value: int, mask: int) -> None:
        for idx, _name in self._flags:
            bit = 1 << idx
            if not (mask & bit):
                st = _IGNORE
            else:
                st = _SET if (value & bit) else _CLEAR
            self._state[idx] = st
            self._restyle(idx)

    def _cycle(self, idx: int) -> None:
        self._state[idx] = (self._state.get(idx, _IGNORE) + 1) % 3
        self._restyle(idx)
        self.changed.emit()

    def _restyle(self, idx: int) -> None:
        b = self._btns.get(idx)
        if b is None:
            return
        st = self._state.get(idx, _IGNORE)
        name = dict(self._flags).get(idx, "")
        # The tick and the cross *are* the state, and a screen reader would
        # read them as punctuation or skip them, so the state is spelled out in
        # the button's name -- which is what an assistive client reads, and
        # what it re-reads when the name changes under the keyboard.
        b.setAccessibleName(f"{name} {_SPOKEN[st]}")
        if st == _SET:
            b.setText(f"✓{name}")
            b.setStyleSheet(
                "QToolButton{color:#0a7d24;font-weight:bold;"
                "border:1px solid #0a7d24;border-radius:3px;padding:1px 5px;}"
            )
        elif st == _CLEAR:
            b.setText(f"✕{name}")
            b.setStyleSheet(
                "QToolButton{color:#c02626;font-weight:bold;"
                "border:1px solid #c02626;border-radius:3px;padding:1px 5px;}"
            )
        else:
            b.setText(name)
            b.setStyleSheet(
                "QToolButton{color:palette(text);"
                "border:1px solid palette(mid);border-radius:3px;padding:1px 5px;}"
            )

    def _bits(self) -> tuple[int, int]:
        """(value, mask) for the current selection."""
        value = mask = 0
        for idx, st in self._state.items():
            if st == _IGNORE:
                continue
            mask |= 1 << idx
            if st == _SET:
                value |= 1 << idx
        return value, mask

    # --- API used by the property form (mirrors OptMatchEditor) --------

    def is_present(self) -> bool:
        return any(st != _IGNORE for st in self._state.values())

    def is_dirty(self) -> bool:
        return self._state != self._original

    def reset_baseline(self) -> None:
        self._original = dict(self._state)

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-render from the value/mask pair just read off the device."""
        vals = values or {}
        self._load(_as_int(vals.get(self._value_code)),
                   _as_int(vals.get(self._mask_code)) if self._mask_code else 0)
        self._original = dict(self._state)

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        for b in self._btns.values():
            b.setEnabled(not locked)

    def error(self) -> str | None:
        return None

    def label(self) -> str:
        names = dict(self._flags)
        parts = []
        for idx, st in sorted(self._state.items()):
            if st == _SET:
                parts.append(names[idx])
            elif st == _CLEAR:
                parts.append("!" + names[idx])
        return ",".join(parts) if parts else "(any)"

    def value(self) -> Any:
        return self._bits()[0]

    def wire_values(self) -> dict[int, Any]:
        value, mask = self._bits()
        out: dict[int, Any] = {self._value_code: (m.T_U32, value)}
        if self._mask_code is not None:
            out[self._mask_code] = (m.T_U32, mask)
        return out


class SetEditor(QWidget):
    """A multi-select bitmask (`type:'set'`) as a row of checkboxes.

    Firewall Connection State / NAT State, PPP Authentication (pap/chap/…),
    cipher lists: each named member is one bit, and the field's value is the OR
    of the checked members (member at enum key ``k`` is bit ``1<<k`` -- confirmed
    against the router: connection-state=established,new reads back as 0x0a). The
    value is emitted as a ``(tag, value)`` pair the write path passes through.
    Optional / negatable set matches (Connection State) are hosted inside
    OptMatchEditor, which adds the "+" and "!"; a standalone set (Authentication)
    uses this directly.

    A set declaring ``columns:`` is a grid rather than a row: Winbox wraps the
    wireless rates four to a line, and lines every rate row of the tab up on
    one pitch (see :meth:`set_column_pitch`).
    """

    changed = Signal()

    def __init__(
        self,
        fld,
        values_by_code: dict[int, Any] | None = None,
        *,
        readonly: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._fld = fld
        self._code = fld.code
        self._readonly = readonly
        self._items: list[tuple[int, str]] = sorted(
            (int(k), str(n)) for k, n in (fld.enum or {}).items()
        )

        # `columns:` in the .jg is how many members Winbox puts on one row
        # before wrapping: Supported Rates A/G is eight members over two rows
        # of four, and the second row's boxes line up under the first's. A set
        # that declares it gets a grid even when it fits on one row -- Winbox
        # runs the whole Data Rates tab on one four-column pitch, so the
        # four-member B row lines up with the eight-member A/G row above it.
        self.columns = getattr(fld, "set_columns", None) or 0
        grid = bool(self.columns)
        row: Any = QGridLayout(self) if grid else QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        if grid:
            row.setVerticalSpacing(2)
        self._boxes: dict[int, QCheckBox] = {}
        value = _as_int((values_by_code or {}).get(self._code))
        for i, (key, name) in enumerate(self._items):
            cb = QCheckBox(name)
            cb.setChecked(bool(value & (1 << key)))
            cb.setEnabled(not readonly)
            cb.toggled.connect(lambda *_: self.changed.emit())
            self._boxes[key] = cb
            if grid:
                row.addWidget(cb, i // self.columns, i % self.columns)
            else:
                row.addWidget(cb)
        if grid:
            # The spacer column past the last one keeps the grid left instead
            # of spreading it across the form.
            row.setColumnStretch(self.columns, 1)
            self.set_column_pitch(self.natural_pitch())
        else:
            row.addStretch(1)
        self._original = self._value()

    def _value(self) -> int:
        v = 0
        for key, cb in self._boxes.items():
            if cb.isChecked():
                v |= 1 << key
        return v

    # --- column grid ---------------------------------------------------

    def natural_pitch(self) -> int:
        """Column width this set needs on its own: its widest member."""
        if not self._boxes:
            return 0
        return max(cb.sizeHint().width() for cb in self._boxes.values())

    def set_column_pitch(self, pitch: int) -> None:
        """Widen every column to ``pitch``, so sibling sets share a grid.

        Winbox draws the four rate rows of a Data Rates tab on one pitch, not
        on each row's own longest label -- the property form hands every set on
        a page the widest pitch any of them asked for (see _form_page).
        """
        lay = self.layout()
        if not isinstance(lay, QGridLayout) or not self.columns:
            return
        for column in range(self.columns):
            lay.setColumnMinimumWidth(column, max(0, int(pitch)))

    # --- API used by the property form (mirrors OptMatchEditor) --------

    def is_present(self) -> bool:
        return self._value() != 0

    def is_dirty(self) -> bool:
        return self._value() != self._original

    def reset_baseline(self) -> None:
        self._original = self._value()

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-tick the members from the bitmask just read off the device."""
        value = _as_int((values or {}).get(self._code))
        for key, cb in self._boxes.items():
            cb.setChecked(bool(value & (1 << key)))
        self._original = self._value()

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        for cb in self._boxes.values():
            cb.setEnabled(not locked)

    def error(self) -> str | None:
        return None

    def label(self) -> str:
        names = dict(self._items)
        on = [names[k] for k, cb in self._boxes.items() if cb.isChecked()]
        return ",".join(on) if on else "(none)"

    def value(self) -> Any:
        return self._value()

    def wire_values(self) -> dict[int, Any]:
        return {self._code: (m.T_U32, self._value())}
