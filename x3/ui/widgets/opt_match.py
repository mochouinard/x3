"""Winbox's optional match: a ``+`` until set, then ``[!] value [×]``.

Firewall matches are optional, and an unset one means "match anything" — which
is why Winbox shows a bare ``+`` rather than an empty text box. Once set it
shows the value with a ``!`` to negate the match and an ``×`` to drop it again.

Those three states are three sibling wire codes, which the schema nests as
``opt -> not -> value`` and the device flattens onto one row::

    opt   0x1a2  'Src. Address'   True     <- the match exists
     └─ not   200                 False    <- the ! toggle
         └─ network6  50          127.0.0.1, paired with 51 as a from/to range

:meth:`OptMatchEditor.wire_values` returns exactly that mapping, so one editor
writes all three codes.
"""

from __future__ import annotations

import ipaddress
from .. import a11y
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QToolButton,
    QWidget,
)

from x3.proto import message as m
from x3.proto import schema as sch

from .status_marks import MarkedCombo, apply_marks

#: Inner types edited as an address (or an address range).
_ADDRESS_TYPES = ("ipaddr", "network", "network6", "addr")


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, (bytes, bytearray)) and len(value) <= 8:
            return int.from_bytes(value, "little")
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
    except (TypeError, ValueError):
        return None
    return None


class OptMatchEditor(QWidget):
    """One optional match, with its presence, negation and value."""

    changed = Signal()

    def __init__(
        self,
        fld,
        values_by_code: dict[int, Any] | None = None,
        *,
        choices: list[tuple[Any, str]] | None = None,
        groups: list[tuple[int, str, str]] | None = None,
        marks: dict | None = None,
        mark_source: tuple | None = None,
        readonly: bool = False,
        show_passwords: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._fld = fld
        self._readonly = readonly
        #: Status dots and comments for the picker's entries, and where to
        #: re-read them (client, path, provider) as the box opens.
        self._marks = dict(marks or {})
        self._mark_source = mark_source
        # Settings > Show Passwords, read before the value editor is built:
        # _make_value_editor picks the echo mode from it.
        self._show_pw = bool(show_passwords)
        values = values_by_code or {}

        self._neg_fld, self._inner_fld = sch.opt_chain(fld)
        self._opt_code = fld.code
        self._not_code = self._neg_fld.code if self._neg_fld is not None else None
        self._inner_code = self._inner_fld.code if self._inner_fld is not None else None
        self._pair_code = (
            self._inner_fld.maskid if self._inner_fld is not None else None
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        # "+" — the whole control when the match is not set.
        self._add = QToolButton()
        self._add.setText("+")
        self._add.setToolTip("Match on this field")
        self._add.clicked.connect(lambda: self._set_present(True))
        a11y.part(self._add, "match on")
        row.addWidget(self._add)

        # "!" — negate the match. A bare checkable QToolButton shows almost no
        # pressed state in Fusion, so the state is spelled out instead of
        # relying on the frame: the label itself changes.
        self._neg = QToolButton()
        self._neg.setCheckable(True)
        self._neg.setStyleSheet(
            "QToolButton { padding: 1px 6px; border: 1px solid palette(mid);"
            " border-radius: 3px; }"
            "QToolButton:checked { background: palette(highlight);"
            " color: palette(highlighted-text); font-weight: bold; }"
        )
        self._neg.toggled.connect(self._sync_negate)
        self._neg.toggled.connect(lambda *_: self.changed.emit())
        a11y.part(self._neg, "negate")
        row.addWidget(self._neg)

        inner_jg = (self._inner_fld.jg_type or "") if self._inner_fld else ""
        if inner_jg.startswith("multitristate"):
            # A flag-set match (firewall TCP Flags): the value is its own
            # tristate editor, writing its own value + mask codes.
            from .tristate_editor import MultiTristateEditor

            self._value = MultiTristateEditor(
                self._inner_fld, values, readonly=readonly
            )
            self._value.changed.connect(lambda *_: self.changed.emit())
        elif inner_jg == "set":
            # A bitmask match (Connection State / NAT State): checkboxes for the
            # members, with this editor's "+" / "!" around them.
            from .tristate_editor import SetEditor

            self._value = SetEditor(self._inner_fld, values, readonly=readonly)
            self._value.changed.connect(lambda *_: self.changed.emit())
        else:
            self._value = self._make_value_editor(choices, groups)
        a11y.part(self._value)
        row.addWidget(self._value, 1)

        # "×" — unset, back to matching anything.
        self._clear = QToolButton()
        self._clear.setText("×")
        self._clear.setToolTip("Do not match on this field")
        self._clear.clicked.connect(lambda: self._set_present(False))
        a11y.part(self._clear, "stop matching")
        row.addWidget(self._clear)

        present = values.get(self._opt_code) in (True, 1)
        if self._not_code is not None:
            self._neg.setChecked(values.get(self._not_code) in (True, 1))
        self._sync_negate()
        self._load_value(values)
        # Presence first: state() reads it, so snapshotting before this would
        # record "unset" and make every already-set match look edited.
        self._set_present(present, notify=False)
        self._original = self.state()

        if readonly:
            for w in (self._add, self._neg, self._clear, self._value):
                w.setEnabled(False)

    def _sync_negate(self, on: bool | None = None) -> None:
        """Say in words which way the toggle is set."""
        on = self._neg.isChecked() if on is None else on
        self._neg.setText("≠" if on else "=")
        self._neg.setToolTip(
            "Negated: matches everything EXCEPT this value. Click to match it."
            if on
            else "Matches this value. Click to negate (match everything else)."
        )

    # --- value editor -------------------------------------------------

    def _is_address(self) -> bool:
        return (
            self._inner_fld is not None
            and (self._inner_fld.jg_type or "") in _ADDRESS_TYPES
        )

    def _is_range_list(self) -> bool:
        return (
            self._inner_fld is not None
            and self._inner_fld.jg_type in sch.RANGE_LIST_TYPES
            and self._inner_fld.highid is None
        )

    def _range_list(self) -> list[int] | None:
        """The typed ``13239`` / ``400-500,8080`` as the wire's flat pairs,
        or None when it does not read as one or leaves the field's bounds."""
        try:
            flat = sch.parse_range_list(self._text())
        except ValueError:
            return None
        top = self._inner_fld.maximum
        low = self._inner_fld.minimum or 0
        if not flat or any(v < low or (top is not None and v > top) for v in flat):
            return None
        return flat

    def _is_secret(self) -> bool:
        """Does the value this match holds want masking?

        The .jg hangs the secrecy on the leaf, not on the opt above it: a
        tunnel's IPsec Secret is ``opt -> secret``, so the row reads as an
        ordinary optional match and only the inner field says otherwise.
        """
        return sch.is_secret(self._inner_fld)

    def _echo_mode(self) -> QLineEdit.EchoMode:
        return (
            QLineEdit.EchoMode.Normal
            if self._show_pw
            else QLineEdit.EchoMode.Password
        )

    def set_show_passwords(self, show: bool) -> None:
        """Reveal or mask the value, for a toggle on an already-open form.

        Echo mode does not touch ``QLineEdit.text()``, so dirty tracking and
        :meth:`wire_values` are unaffected.
        """
        self._show_pw = bool(show)
        if self._is_secret() and isinstance(self._value, QLineEdit):
            self._value.setEchoMode(self._echo_mode())

    def _make_value_editor(self, choices, groups) -> QWidget:
        if groups is not None:
            combo = self._new_picker()
            # Winbox groups the interface picker by kind. The headings are
            # labels, not values -- selecting one would write a wire id that
            # does not exist.
            by_type: dict[str, list[tuple[int, str]]] = {}
            for oid, name, kind in groups:
                by_type.setdefault(kind or "other", []).append((oid, name))
            for kind in sorted(by_type):
                combo.addItem(f"— all {kind} —", None)
                idx = combo.count() - 1
                combo.model().item(idx).setEnabled(False)
                for oid, name in sorted(by_type[kind], key=lambda p: p[1]):
                    combo.addItem(name, oid)
            combo.currentIndexChanged.connect(lambda *_: self.changed.emit())
            apply_marks(combo, self._marks)
            return combo
        if choices:
            combo = self._new_picker()
            for wire, label in choices:
                combo.addItem(str(label), wire)
            combo.currentIndexChanged.connect(lambda *_: self.changed.emit())
            apply_marks(combo, self._marks)
            return combo
        line = QLineEdit()
        if self._is_address():
            line.setPlaceholderText("0.0.0.0 or 0.0.0.0-0.0.0.0")
        if self._is_secret():
            line.setEchoMode(self._echo_mode())
        line.textChanged.connect(lambda *_: self.changed.emit())
        return line

    def _new_picker(self) -> QComboBox:
        """A dropdown that re-reads its status dots as it opens.

        Plain QComboBox when nothing told this editor where its entries come
        from -- a `pair` of static words is not a table and has nothing to
        re-read.
        """
        if self._mark_source is None:
            return QComboBox()
        combo = MarkedCombo()
        client, path, provider = self._mark_source
        combo.set_mark_source(client, path, provider=provider)
        return combo

    def _load_value(self, values: dict[int, Any]) -> None:
        # The flag / set editors loaded themselves from values in their
        # constructor.
        from .tristate_editor import MultiTristateEditor, SetEditor

        if isinstance(self._value, (MultiTristateEditor, SetEditor)):
            return
        if self._inner_code is None:
            return
        raw = values.get(self._inner_code)
        if isinstance(self._value, QComboBox):
            iv = _as_int(raw)
            idx = self._value.findData(iv)
            if idx >= 0:
                self._value.setCurrentIndex(idx)
            return
        if raw is None:
            return
        if self._is_address():
            low = _as_int(raw)
            high = _as_int(values.get(self._pair_code)) if self._pair_code else None
            text = sch.format_ipv4(low) if low is not None else ""
            if high is not None and high != low:
                # 192.168.88.0/24, not its two ends: see format_ipv4_range.
                text = sch.format_ipv4_range(text, sch.format_ipv4(high))
            self._value.setText(text)
            return
        if self._is_range_list() and isinstance(raw, (list, tuple)):
            # Firewall Dst. Port 13239 arrives as [13239, 13239]: the list's
            # (low, high) pairs, which Winbox shows as 13239 -- and
            # 400-500,8080 as typed.
            self._value.setText(self._inner_fld.render(list(raw)))
            return
        self._value.setText(
            raw.decode("utf-8", errors="replace")
            if isinstance(raw, (bytes, bytearray))
            else str(raw)
        )

    # --- presence -----------------------------------------------------

    def _set_present(self, present: bool, *, notify: bool = True) -> None:
        self._present = present
        self._add.setVisible(not present)
        for w in (self._neg, self._value, self._clear):
            w.setVisible(present)
        if notify:
            self.changed.emit()

    def is_present(self) -> bool:
        return self._present

    def set_locked(self, locked: bool) -> None:
        """Lock or unlock in place, for a mode change on an open form.

        These widgets are disabled individually at construction, and enabling
        the container does not undo that — so switching from Read-only to Live
        left the control dead until the window was reopened.
        """
        self._readonly = bool(locked)
        for w in (self._add, self._neg, self._clear, self._value):
            w.setEnabled(not locked)

    # --- reading ------------------------------------------------------

    def _text(self) -> str:
        from .tristate_editor import MultiTristateEditor, SetEditor

        if isinstance(self._value, (MultiTristateEditor, SetEditor)):
            return self._value.label()
        if isinstance(self._value, QComboBox):
            return self._value.currentText()
        return self._value.text().strip()

    def _address_pair(self) -> tuple[int, int] | None:
        """(low, high) for the typed address or range, or None if unparseable."""
        text = self._text()
        if not text:
            return None
        try:
            # An address, a range, or a subnet: 192.168.88.0/24 is the range
            # 192.168.88.0-192.168.88.255 on the wire.
            low_s, high_s = sch.parse_ipv4_range(text)
            low = sch.parse_ipv4(low_s)
            high = sch.parse_ipv4(high_s)
        except Exception:  # noqa: BLE001
            return None
        return low, high

    def error(self) -> str | None:
        """Why this match cannot be sent, or None."""
        if not self._present:
            return None
        if self._is_address():
            if not self._text():
                return f"{self._fld.name}: no address entered."
            if self._address_pair() is None:
                return (
                    f"{self._fld.name}: {self._text()!r} is not an address "
                    "or address range."
                )
            return None
        if self._is_range_list() and self._range_list() is None:
            top = self._inner_fld.maximum
            return (
                f"{self._fld.name}: {self._text()!r} is not a number list -- "
                "e.g. 80, 400-500 or 80,443,8000-8100"
                + (f", each at most {top}." if top is not None else ".")
            )
        if isinstance(self._value, QComboBox) and self._value.currentData() is None:
            return f"{self._fld.name}: nothing selected."
        return None

    def state(self) -> tuple:
        """Comparable snapshot, for telling an edit from an untouched field."""
        return (
            getattr(self, "_present", False),
            self._neg.isChecked(),
            self._text(),
        )

    def is_dirty(self) -> bool:
        return self.state() != self._original

    def reset_baseline(self) -> None:
        """Treat what is on screen as what the device now holds."""
        self._original = self.state()

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-render from the three codes just read off the device (see Apply).

        Presence before the snapshot, as at construction: ``state()`` reads it,
        and taking the before-image first records "unset" and leaves an
        already-set match reading as edited.
        """
        from .tristate_editor import MultiTristateEditor, SetEditor

        vals = values or {}
        if isinstance(self._value, (MultiTristateEditor, SetEditor)):
            self._value.restate(values=vals)
        if self._not_code is not None:
            self._neg.setChecked(vals.get(self._not_code) in (True, 1))
        self._sync_negate()
        self._load_value(vals)
        self._set_present(vals.get(self._opt_code) in (True, 1), notify=False)
        self._original = self.state()

    def label(self) -> str:
        if not self._present:
            return "(not matched)"
        return ("!" if self._neg.isChecked() else "") + (self._text() or "(unset)")

    def wire_values(self) -> dict[int, Any]:
        """The three sibling codes this match writes."""
        if not self._present:
            # Unset the match; the value fields go with it.
            return {self._opt_code: False}
        out: dict[int, Any] = {self._opt_code: True}
        if self._not_code is not None:
            out[self._not_code] = self._neg.isChecked()
        from .tristate_editor import MultiTristateEditor, SetEditor

        if isinstance(self._value, (MultiTristateEditor, SetEditor)):
            # The flag / set editor writes its own code(s).
            out.update(self._value.wire_values())
            return out
        if self._inner_code is None:
            return out
        if self._is_address():
            pair = self._address_pair()
            if pair is not None:
                low, high = pair
                out[self._inner_code] = low
                if self._pair_code is not None:
                    out[self._pair_code] = high
            return out
        if isinstance(self._value, QComboBox):
            data = self._value.currentData()
            if data is not None:
                out[self._inner_code] = data
            return out
        if self._is_range_list():
            # Sent as the flat (low, high) array, typed: the code is nested
            # under the opt, where the client's field lookup does not reach,
            # and a string under a `U` id is a write the device discards.
            flat = self._range_list()
            if flat is not None:
                out[self._inner_code] = (m.T_U32_ARRAY, flat)
            return out
        out[self._inner_code] = self._text()
        return out


def ipv4_is_valid(text: str) -> bool:
    """Shared by the tests; mirrors what the editor accepts for one address."""
    try:
        ipaddress.IPv4Address(text.strip())
        return True
    except ValueError:
        return False
