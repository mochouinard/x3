"""Editor for a ``multi`` list of networks — IP ▸ Services "Available From".

Winbox shows one text box per entry with a ``+`` to add another, and nothing
but the ``+`` while the list is empty. An empty list means "no restriction", so
a single blank box would misrepresent it.

On the wire the field is an array of nested messages, one per entry, each an
(address, mask) pair keyed by the schema's nested field and its ``maskid``::

    0x06 (T_MESSAGE_ARRAY) = [ {1: 192.168.0.0, 2: 255.255.0.0}, … ]

so :meth:`NetworkArrayEditor.wire_value` returns an explicit ``(tag, value)``
pair, which DeviceClient._encode passes through untouched.

This field decides who can reach a service, so an entry that will not parse
blocks the save rather than being dropped: silently sending a shorter list
than the one on screen could open a service to everyone.
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


def parse_cidr(text: str) -> tuple[int, int] | None:
    """``192.168.0.0/16`` → (address, mask) in the device's byte order.

    A bare address is a single host. Returns None if it will not parse, which
    the caller must treat as "do not save", never as "leave it out".
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None
    if net.version != 4:
        return None
    return sch.parse_ipv4(str(net.network_address)), sch.parse_ipv4(str(net.netmask))


def format_cidr(addr: Any, mask: Any) -> str:
    """The inverse, for loading the editor from the wire."""
    try:
        text = sch.format_ipv4(int(addr))
    except (TypeError, ValueError):
        return ""
    bits = sch.mask_prefix_len(mask)
    return text if bits is None or bits >= 32 else f"{text}/{bits}"


class NetworkArrayEditor(LineRowMarks, QWidget):
    """A growable list of network entries.

    ``LineRowMarks`` is what says *which* entry changed rather than washing the
    whole list (see widgets/edit_marks.py).
    """

    changed = Signal()

    def __init__(self, fld, value: Any = None, *, readonly: bool = False, parent=None):
        super().__init__(parent)
        self._fld = fld
        self._readonly = readonly
        self._rows: list[tuple[QWidget, QLineEdit]] = []

        # The IPv4 entry inside the multi, and the code its mask rides on.
        self._addr_code, self._mask_code = self._entry_codes(fld)

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
        self._add.setToolTip("Add an address or network")
        self._add.clicked.connect(lambda: self._add_row("", notify=True))
        a11y.part(self._add, "add entry")
        self._add.setEnabled(not readonly)
        add_row.addWidget(self._add)
        add_row.addStretch(1)
        root.addLayout(add_row)

        for text in self._texts_from(value):
            self._add_row(text)
        self._original = self.texts()

    @staticmethod
    def _entry_codes(fld) -> tuple[int | None, int | None]:
        for code, nested in (getattr(fld, "nested", None) or {}).items():
            if (nested.jg_type or "") in ("network", "ipaddr"):
                return code, nested.maskid
        return None, None

    def _texts_from(self, value: Any) -> list[str]:
        out: list[str] = []
        for entry in value or []:
            if not isinstance(entry, m.Message):
                continue
            by_code = {c: v for c, _t, v in entry}
            addr = by_code.get(self._addr_code)
            if addr is None:
                continue
            text = format_cidr(addr, by_code.get(self._mask_code))
            if text:
                out.append(text)
        return out

    def set_locked(self, locked: bool) -> None:
        """Lock or unlock in place, for a mode change on an open form.

        The rows' widgets are disabled individually at construction, and
        enabling this container does not undo that — so without this, switching
        from Read-only to Live left the editor stuck until the window was
        closed and reopened.
        """
        self._readonly = bool(locked)
        self._add.setEnabled(not locked)
        for row, edit in self._rows:
            edit.setReadOnly(locked)
            for btn in row.findChildren(QToolButton):
                btn.setEnabled(not locked)

    # --- rows ---------------------------------------------------------

    def _add_row(self, text: str = "", *, notify: bool = False) -> None:
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        edit = QLineEdit(text)
        edit.setPlaceholderText("0.0.0.0/0")
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

    # --- reading ------------------------------------------------------

    def texts(self) -> list[str]:
        return [e.text().strip() for _w, e in self._rows if e.text().strip()]

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
        """Re-render from a value just read off the device (see Apply)."""
        for row, _edit in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        for text in self._texts_from(raw):
            self._add_row(text)
        a11y.renumber(self)
        self._original = self.texts()

    def error(self) -> str | None:
        bad = [t for t in self.texts() if parse_cidr(t) is None]
        if not bad:
            return None
        return (
            f"{self._fld.name}: {', '.join(repr(b) for b in bad)} "
            "is not an address or network."
        )

    def label(self) -> str:
        return ", ".join(self.texts()) or "(any)"

    def wire_value(self) -> tuple[int, list]:
        """``(tag, [Message, …])`` for DeviceClient._encode to pass through."""
        entries = []
        for text in self.texts():
            pair = parse_cidr(text)
            if pair is None:
                continue  # error() has already blocked the save
            addr, mask = pair
            entry = m.Message()
            entry.add_u32(self._addr_code, addr)
            if self._mask_code is not None:
                entry.add_u32(self._mask_code, mask)
            entries.append(entry)
        return (m.T_MESSAGE_ARRAY, entries)
