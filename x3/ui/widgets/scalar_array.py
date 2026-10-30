"""A growable list editor for scalar arrays (``multinumber`` / ``multistring`` /
``multiipaddr`` / …).

WinBox edits these as one box per entry with a ``+`` to add and a ``×`` to drop.
On the wire they are a typed array keyed by the field id (u32 / u64 / string /
ip6 / raw array). This handles the *scalar* element types; enum arrays keep the
dropdown-per-row MultiEnumArrayEditor and network arrays keep NetworkArrayEditor.
A ``multinumberrange`` is scalar-ish too -- its box holds ``N`` or ``N-M`` and
is worth two elements of the array rather than one.
The value is emitted as an ``(tag, [values])`` pair DeviceClient._encode passes
through untouched.
"""

from __future__ import annotations

import ipaddress
from .. import a11y
from .edit_marks import LineRowMarks, MarkedRows
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from x3.proto import message as m
from x3.proto import schema as sch

#: multi jg_type -> (element kind, wire array tag, placeholder)
_ELEMENT: dict[str, tuple[str, int, str]] = {
    "multinumber": ("number", m.T_U32_ARRAY, "0"),
    # A `multinumberrange` (a bridge VLAN's "VLAN IDs") is one box per *range*:
    # Winbox shows `vlan-ids=100-101,105` as two lines, `100-101` and `105`.
    # Each box is two elements of the wire array -- see sch.range_list_pairs.
    "multinumberrange": ("range", m.T_U32_ARRAY, "N or N-M"),
    "multibignumber": ("number", m.T_U64_ARRAY, "0"),
    "multiipaddr": ("ipaddr", m.T_U32_ARRAY, "0.0.0.0"),
    "multistring": ("string", m.T_STRING_ARRAY, ""),
    "multilinestring": ("string", m.T_STRING_ARRAY, ""),
    "multiip6addr": ("ip6addr", m.T_IP6_ARRAY, "::"),
    "multiraw": ("raw", m.T_RAW_ARRAY, "hex"),
}


def is_scalar_multi(fld) -> bool:
    jg = (getattr(fld, "jg_type", "") or "")
    if jg == "multinumberrange" and getattr(fld, "highid", None) is not None:
        # `id2`: the lows and the highs are two parallel arrays under two codes
        # (Traffic Generator's port lists), not the flat pairs this editor
        # writes. Left to the plain box until it can write both.
        return False
    return jg in _ELEMENT


class ScalarArrayEditor(LineRowMarks, QWidget):
    """A growable list of scalar entries.

    ``LineRowMarks`` is what says *which* entry changed: adding one VLAN to a
    range list used to wash every box in it amber (see widgets/edit_marks.py).
    """

    changed = Signal()

    def __init__(self, fld, value: Any = None, *, readonly: bool = False, parent=None):
        super().__init__(parent)
        self._fld = fld
        self._readonly = readonly
        self._kind, self._tag, self._placeholder = _ELEMENT.get(
            fld.jg_type or "", ("string", m.T_STRING_ARRAY, "")
        )
        self._rows: list[tuple[QWidget, QLineEdit]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        # A host widget rather than a bare layout: the removal marks are an
        # overlay on it, and a layout has nothing to paint on.
        self._rows_host = MarkedRows(lambda: [r for r, _e in self._rows])
        self._rows_box = QVBoxLayout(self._rows_host)
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.setSpacing(2)
        root.addWidget(self._rows_host)

        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        self._add = QToolButton()
        self._add.setText("+")
        self._add.setToolTip("Add an entry")
        self._add.setEnabled(not readonly)
        self._add.clicked.connect(lambda: self._add_row("", notify=True))
        a11y.part(self._add, "add entry")
        add_row.addWidget(self._add)
        add_row.addStretch(1)
        root.addLayout(add_row)

        for text in self._texts_from(value):
            self._add_row(text)
        self._original = self.texts()

    # --- element formatting / parsing ---------------------------------

    def _format(self, elem: Any) -> str:
        try:
            if self._kind == "ipaddr":
                return sch.format_ipv4(int(elem))
            if self._kind == "ip6addr":
                if isinstance(elem, (bytes, bytearray)) and len(elem) >= 16:
                    return str(ipaddress.IPv6Address(bytes(elem[:16])))
                return str(elem)
            if self._kind == "raw":
                return elem.hex() if isinstance(elem, (bytes, bytearray)) else str(elem)
            if self._kind == "number":
                return str(int(elem))
            if self._kind == "range":
                return sch.format_range(elem, elem)
            if isinstance(elem, (bytes, bytearray)):
                return elem.decode("utf-8", errors="replace")
            return str(elem)
        except (TypeError, ValueError):
            return ""

    def _parse(self, text: str) -> Any:
        text = text.strip()
        if self._kind == "ipaddr":
            return sch.parse_ipv4(text)
        if self._kind == "ip6addr":
            return ipaddress.IPv6Address(text).packed
        if self._kind == "raw":
            return bytes.fromhex(text.replace(":", "").replace(" ", ""))
        if self._kind == "number":
            return int(text, 0)
        if self._kind == "range":
            return sch.parse_range(text)     # (low, high), two array elements
        return text

    def _valid(self, text: str) -> bool:
        try:
            self._parse(text)
            return True
        except (TypeError, ValueError, ipaddress.AddressValueError):
            return False

    def _texts_from(self, value: Any) -> list[str]:
        if value is None:
            return []
        if self._kind == "range":
            # The array is a flat run of (low, high) pairs, so one box per pair
            # -- and equal ends are the single number Winbox shows. An array
            # that is not pairs falls through to a box per number rather than
            # inventing ranges out of it.
            pairs = sch.range_list_pairs(value)
            if pairs is not None:
                return [sch.format_range(low, high) for low, high in pairs]
        items = value if isinstance(value, (list, tuple)) else [value]
        out = [self._format(v) for v in items]
        return [t for t in out if t != ""]

    # --- rows ---------------------------------------------------------

    def _add_row(self, text: str = "", *, notify: bool = False) -> None:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        edit = QLineEdit(text)
        edit.setPlaceholderText(self._placeholder)
        edit.setReadOnly(self._readonly)
        edit.textChanged.connect(lambda *_: self.changed.emit())
        box.addWidget(edit, 1)
        drop = QToolButton()
        drop.setText("×")
        drop.setToolTip("Remove this entry")
        drop.setEnabled(not self._readonly)
        drop.clicked.connect(lambda *_, r=row: self._remove_row(r))
        box.addWidget(drop)
        a11y.part(edit, "entry")
        a11y.part(drop, "remove entry")
        self._rows_box.addWidget(row)
        self._rows.append((row, edit))
        # The row count is part of every entry's name ("entry 2 of 3"), so the
        # whole set is renamed whenever the list grows or shrinks.
        a11y.renumber(self)
        if notify:
            edit.setFocus()
            self.changed.emit()

    def _remove_row(self, row: QWidget) -> None:
        self._rows = [(w, e) for w, e in self._rows if w is not row]
        row.setParent(None)
        row.deleteLater()
        a11y.renumber(self)
        self.changed.emit()

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        self._add.setEnabled(not locked)
        for row, edit in self._rows:
            edit.setReadOnly(locked)
            for btn in row.findChildren(QToolButton):
                btn.setEnabled(not locked)

    # --- reading ------------------------------------------------------

    def texts(self) -> list[str]:
        return [e.text().strip() for _w, e in self._rows if e.text().strip()]

    def is_present(self) -> bool:
        return bool(self.texts())

    def is_dirty(self) -> bool:
        return self.texts() != self._original

    def change_items(self) -> tuple[list[str], list[str]]:
        """(what the device sent, what the boxes hold now).

        What a property form's status line counts to say this list is two
        entries shorter than the one that was read, rather than only that
        something in it moved.
        """
        return list(self._original), self.texts()

    def reset_baseline(self) -> None:
        self._original = self.texts()

    def restate(self, raw: Any, rendered: str = "", values=None) -> None:
        """Re-render from a value just read off the device.

        The router rewrites a range list into its own normal form -- a bridge
        VLAN sent ``100-103,105,99`` comes back as ``99-103,105`` -- so after
        Apply the boxes have to show the device's answer, not the text that
        produced it. The device's value is the clean one by definition, hence
        the baseline moves with it.
        """
        for row, _edit in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        for text in self._texts_from(raw):
            self._add_row(text)
        a11y.renumber(self)
        self._original = self.texts()

    def error(self) -> str | None:
        bad = [t for t in self.texts() if not self._valid(t)]
        if not bad:
            return None
        return f"{self._fld.name}: {', '.join(repr(b) for b in bad)} is not valid."

    def label(self) -> str:
        # A range list reads as one value -- `100-101,105` -- the way the list
        # column and the router itself write it.
        sep = "," if self._kind == "range" else ", "
        return sep.join(self.texts()) or "(none)"

    def wire_value(self) -> tuple[int, list]:
        out = []
        for text in self.texts():
            try:
                value = self._parse(text)
            except (TypeError, ValueError, ipaddress.AddressValueError):
                continue  # error() has blocked the save
            if isinstance(value, tuple):
                out += list(value)   # a range: its low and its high
            else:
                out.append(value)
        return (self._tag, out)
