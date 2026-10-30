"""Auto-detecting editor for a ``single`` union field.

A ``single`` union is one logical value the device stores under whichever of
several wire codes matches its type -- an address list entry is an IPv4
network/range **or** a DNS name; a graphing rule's Allow Address is an IPv4
network **or** an IPv6 one. WinBox shows one text box with no type picker: you
type ``192.168.1.0/24``, ``10.0.0.1-10.0.0.5`` or ``example.com`` and it works
out which variant you meant from the text.

This mirrors that. On save :meth:`UnionEditor.wire_values` returns the codes for
exactly one variant, already encoded as ``(tag, value)`` pairs that
``DeviceClient._encode`` passes through untouched -- so the address/mask byte
order matches what NetworkArrayEditor and OptMatchEditor already send for the
same flat network fields.
"""

from __future__ import annotations

import ipaddress
from .. import a11y
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from x3.proto import message as m
from x3.proto import schema as sch

#: jg types edited as an IPv4 address / network / range.
_V4_TYPES = ("network", "ipaddr", "addr")
#: jg types edited as an IPv6 address / network.
_V6_TYPES = ("network6", "ip6addr")


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, (bytes, bytearray)) and len(value) <= 8:
            return int.from_bytes(value, "little")
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            return int(value)
    except (TypeError, ValueError):
        return None
    return None


def _has_value(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, (bytes, bytearray)):
        return any(raw)
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, int):
        return True
    return bool(raw)


class UnionEditor(QWidget):
    """One text box that reads and writes any one variant of a single union."""

    changed = Signal()

    def __init__(
        self,
        primary,
        values_by_code: dict[int, Any] | None = None,
        *,
        readonly: bool = False,
        is_add: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._readonly = readonly
        self._variants = [primary, *getattr(primary, "union_alts", [])]
        self._primary = primary
        values = values_by_code or {}

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._edit = QLineEdit()
        self._edit.setReadOnly(readonly)
        self._edit.setPlaceholderText(self._placeholder())
        a11y.part(self._edit)
        row.addWidget(self._edit)

        # Codes that already carried a value: only these get cleared when the
        # user switches the entry to a different variant, so an Add (nothing
        # present) never sends a mistyped zero for a variant it isn't using.
        self._orig_present = {
            var.code for var in self._variants if _has_value(values.get(var.code))
        }

        text = self._load_text(values)
        if not text and is_add and self._default_text():
            text = self._default_text()
        self._edit.setText(text)
        self._original = text
        self._edit.textChanged.connect(lambda *_: self.changed.emit())

    # --- variant lookup ------------------------------------------------

    def _v4(self):
        return next((v for v in self._variants if (v.jg_type or "") in _V4_TYPES), None)

    def _v6(self):
        return next((v for v in self._variants if (v.jg_type or "") in _V6_TYPES), None)

    def _string(self):
        return next(
            (v for v in self._variants if (v.jg_type or "") == "string"), None
        )

    def _placeholder(self) -> str:
        bits = []
        if self._v4() is not None:
            bits.append("0.0.0.0/0" + ("-…" if self._v4().is_range else ""))
        if self._v6() is not None:
            bits.append("::/0")
        if self._string() is not None:
            bits.append("name")
        return "  or  ".join(bits)

    def _default_text(self) -> str:
        # WinBox pre-fills a plain (non-range) IPv4 network with 0.0.0.0/0 --
        # its rendering of an all-zero address. A range field (address list)
        # and a string are left blank, which is what WinBox shows there.
        v4 = self._v4()
        if v4 is not None and not v4.is_range and (v4.jg_type or "") == "network":
            return "0.0.0.0/0"
        return ""

    # --- loading -------------------------------------------------------

    def _load_text(self, values: dict[int, Any]) -> str:
        for var in self._variants:
            raw = values.get(var.code)
            if not _has_value(raw):
                continue
            kind = var.jg_type or ""
            if kind in _V4_TYPES:
                return self._format_v4(var, values)
            if kind in _V6_TYPES:
                return self._format_v6(var, values)
            if kind == "string":
                if isinstance(raw, (bytes, bytearray)):
                    return raw.decode("utf-8", errors="replace")
                return str(raw)
        return ""

    def _format_v4(self, var, values: dict[int, Any]) -> str:
        low = _as_int(values.get(var.code))
        if low is None:
            return ""
        text = sch.format_ipv4(low)
        pair = _as_int(values.get(var.maskid)) if var.maskid is not None else None
        if pair is None:
            return text
        if var.is_range:
            high = pair
            if high and high != low:
                return f"{text}-{sch.format_ipv4(high)}"
            return text
        bits = sch.mask_prefix_len(pair)
        if bits is None or bits >= 32:
            return text
        return f"{text}/{bits}"

    def _format_v6(self, var, values: dict[int, Any]) -> str:
        raw = values.get(var.code)
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < 16:
            return "" if raw is None else str(raw)
        try:
            addr = ipaddress.IPv6Address(bytes(raw[:16]))
        except ValueError:
            return ""
        prefix = _as_int(values.get(var.maskid)) if var.maskid is not None else None
        if prefix is None or prefix >= 128 or prefix <= 0:
            return str(addr)
        return f"{addr}/{prefix}"

    # --- detection -----------------------------------------------------

    def _text(self) -> str:
        return self._edit.text().strip()

    @staticmethod
    def _looks_ipv4(text: str) -> bool:
        head = text.split("-", 1)[0].split("/", 1)[0].strip()
        try:
            ipaddress.IPv4Address(head)
            return True
        except ValueError:
            return False

    def _detect(self):
        """The variant field the current text should be written as, or None."""
        text = self._text()
        if not text:
            return None
        if ":" in text and self._v6() is not None:
            return self._v6()
        if self._v4() is not None and self._looks_ipv4(text):
            return self._v4()
        if self._string() is not None:
            return self._string()
        return self._v4() or self._v6() or self._primary

    # --- reading / state ----------------------------------------------

    def is_present(self) -> bool:
        return bool(self._text())

    def is_dirty(self) -> bool:
        return self._text() != (self._original or "").strip()

    def reset_baseline(self) -> None:
        self._original = self._text()

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-render from the variant codes just read off the device.

        Which codes carry a value is part of what a write changes, so the set
        of "originally present" ones is re-taken too: otherwise switching the
        entry to another variant afterwards would clear a code the device has
        since filled, or leave one it has since emptied.
        """
        vals = values or {}
        self._orig_present = {
            var.code for var in self._variants if _has_value(vals.get(var.code))
        }
        self._edit.setText(self._load_text(vals))
        self._original = self._text()

    def label(self) -> str:
        return self._text() or "(unset)"

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        self._edit.setReadOnly(locked)

    def error(self) -> str | None:
        text = self._text()
        if not text:
            return None
        var = self._detect()
        if var is None:
            return None
        kind = var.jg_type or ""
        name = self._primary.name or "value"
        if kind in _V4_TYPES:
            if self._parse_v4(text) is None:
                return f"{name}: {text!r} is not an IPv4 address, network or range."
        elif kind in _V6_TYPES:
            if self._parse_v6(text) is None:
                return f"{name}: {text!r} is not an IPv6 address or network."
        return None

    # --- encoding ------------------------------------------------------

    def _parse_v4(self, text: str) -> tuple[int, int] | None:
        """(low, mask-or-high) for a v4 variant, or None if unparseable.

        Range fields carry the range's HIGH address in the mask code; plain
        network fields carry a netmask. A CIDR on a range field becomes its
        network..broadcast span, which matches the same addresses.
        """
        var = self._v4()
        is_range = bool(var and var.is_range)
        try:
            if "-" in text:
                low_s, _, high_s = text.partition("-")
                low = sch.parse_ipv4(low_s.strip())
                high = sch.parse_ipv4(high_s.strip())
                return low, high
            net = ipaddress.ip_network(text, strict=False)
            if net.version != 4:
                return None
            low = sch.parse_ipv4(str(net.network_address))
            if is_range:
                high = sch.parse_ipv4(str(net.broadcast_address))
                return low, high
            return low, sch.parse_ipv4(str(net.netmask))
        except (ValueError, TypeError):
            return None

    def _parse_v6(self, text: str) -> tuple[bytes, int] | None:
        try:
            net = ipaddress.ip_network(text, strict=False)
            if net.version != 6:
                return None
            return net.network_address.packed, net.prefixlen
        except (ValueError, TypeError):
            return None

    def wire_values(self) -> dict[int, Any]:
        """The codes for the one variant the text names, as ``{code: (tag, val)}``.

        Non-selected variants that held a value are cleared, so switching an
        existing entry from an address to a name does not leave the old code
        set alongside the new one.
        """
        out: dict[int, Any] = {}
        text = self._text()
        chosen = self._detect() if text else None

        if chosen is not None:
            kind = chosen.jg_type or ""
            if kind in _V4_TYPES:
                pair = self._parse_v4(text)
                if pair is not None:
                    low, other = pair
                    out[chosen.code] = (m.T_U32, low)
                    if chosen.maskid is not None:
                        out[chosen.maskid] = (m.T_U32, other)
            elif kind in _V6_TYPES:
                pair = self._parse_v6(text)
                if pair is not None:
                    raw, prefix = pair
                    out[chosen.code] = (m.T_IP6, raw.ljust(16, b"\x00")[:16])
                    if chosen.maskid is not None:
                        out[chosen.maskid] = (m.T_U32, prefix)
            else:  # string / DNS name
                out[chosen.code] = (m.T_STRING, text)

        # Clear the other variants' codes so a type switch actually takes, but
        # only the ones that held a value -- with the right wire type, never a
        # u32 zero into an IPv6 field.
        for var in self._variants:
            if chosen is not None and var.code == chosen.code:
                continue
            if var.code not in self._orig_present:
                continue
            kind = var.jg_type or ""
            if kind == "string":
                out.setdefault(var.code, (m.T_STRING, ""))
            elif kind in _V6_TYPES:
                out.setdefault(var.code, (m.T_IP6, b"\x00" * 16))
                if var.maskid is not None:
                    out.setdefault(var.maskid, (m.T_U32, 0))
            else:
                out.setdefault(var.code, (m.T_U32, 0))
                if var.maskid is not None:
                    out.setdefault(var.maskid, (m.T_U32, 0))
        return out

    def value(self) -> Any:
        """The primary code's value, for the edits() summary."""
        return self.wire_values().get(self._primary.code)
