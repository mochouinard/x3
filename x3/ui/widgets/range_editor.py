"""Editor for a ``numberrange`` / ``integerrange`` — a low–high pair.

Firewall To Ports, AP Signal Strength Range: two u32 codes (the field id is the
low end, its ``highid`` the high end). WinBox lets you type ``N`` for a single
value or ``N-M`` for a range; we do the same and write both codes as
``(tag, value)`` pairs the write path passes through untouched.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget
from .. import a11y

from x3.proto import message as m

#: The router marks an unset high end with a near-max sentinel.
_SENTINEL = 0xFFFFFF00


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, (bytes, bytearray)):
            return int.from_bytes(value, "little")
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            return int(value, 0)
    except (TypeError, ValueError):
        return None
    return None


class RangeEditor(QWidget):
    """One box holding ``N`` or ``N-M``, writing the low + high codes."""

    changed = Signal()

    def __init__(self, fld, values_by_code: dict[int, Any] | None = None, *,
                 readonly: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._low_code = fld.code
        self._high_code = fld.highid
        self._readonly = readonly
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self._edit = QLineEdit()
        self._edit.setReadOnly(readonly)
        self._edit.setPlaceholderText("N or N-M")
        a11y.part(self._edit)
        row.addWidget(self._edit)
        self._edit.setText(self._load(values_by_code or {}))
        self._original = self._text()
        self._edit.textChanged.connect(lambda *_: self.changed.emit())

    def _load(self, values: dict[int, Any]) -> str:
        low = _as_int(values.get(self._low_code))
        if low is None:
            return ""
        high = _as_int(values.get(self._high_code)) if self._high_code else None
        if high is None or high == low or high >= _SENTINEL:
            return str(low)
        return f"{low}-{high}"

    def _text(self) -> str:
        return self._edit.text().strip()

    def _pair(self) -> tuple[int, int] | None:
        text = self._text()
        if not text:
            return None
        try:
            if "-" in text:
                lo_s, _, hi_s = text.partition("-")
                lo = int(lo_s.strip(), 0)
                hi = int(hi_s.strip(), 0)
            else:
                lo = hi = int(text, 0)
        except (TypeError, ValueError):
            return None
        return lo, hi

    # --- API used by the property form --------------------------------

    def is_present(self) -> bool:
        return bool(self._text())

    def is_dirty(self) -> bool:
        return self._text() != (self._original or "")

    def reset_baseline(self) -> None:
        self._original = self._text()

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-render from the codes just read off the device (see Apply)."""
        self._edit.setText(self._load(values or {}))
        self._original = self._text()

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        self._edit.setReadOnly(locked)

    def error(self) -> str | None:
        if self._text() and self._pair() is None:
            return f"{self._fld.name}: {self._text()!r} is not a number or N-M range."
        return None

    def label(self) -> str:
        return self._text() or "(unset)"

    def value(self) -> Any:
        pair = self._pair()
        return pair[0] if pair else None

    def wire_values(self) -> dict[int, Any]:
        pair = self._pair()
        if pair is None:
            return {}
        lo, hi = pair
        out: dict[int, Any] = {self._low_code: (m.T_U32, lo)}
        if self._high_code is not None:
            out[self._high_code] = (m.T_U32, hi)
        return out
