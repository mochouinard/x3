"""``type:'numbar'`` -- a signal reading as a coloured bar.

The LTE and wireless status tabs report RSSI / RSRP / RSRQ / SINR / RSCP this
way, and the .jg carries everything the drawing needs::

    {name:'RSSI',type:'numbar',id:'u3ec',max:40,offset:95,postfix:'dBm',
     colors:[{level:10,color:'#ff0000',label:'poor'},
             {level:20,color:'#ffb20d',label:'fair'},
             {level:30,color:'#ffff48',label:'good'},
             {level:2147483647,color:'#42d342',label:'excellent'}]}

``offset`` is not decoration: the wire value 25 *is* -70 dBm, so a bar-less
text row was showing the wrong number, not merely the right number plainly.
The colour bands are read against the raw value (0..``max``) -- that is the
scale their levels are on -- while the text shows the shifted value with its
unit, and the band's own word for it ("good").

Read-only by construction: a numbar is a reading, and Winbox never lets one be
typed into.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from .. import a11y, style


class _Bar(QWidget):
    """The bar itself: a track, a filled part, and nothing else."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frac = 0.0
        self._color = QColor("#888888")
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(max(10, style.ROW_HEIGHT - 8))

    def set_reading(self, frac: float, color: str) -> None:
        self._frac = max(0.0, min(1.0, frac))
        if color:
            self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        rect = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(QColor(style.BORDER))
        p.setBrush(QColor(style.BG))
        p.drawRect(rect)
        if self._frac > 0:
            inner = QRectF(rect).adjusted(1, 1, 0, 0)
            inner.setWidth(max(1.0, (inner.width()) * self._frac))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._color)
            p.drawRect(inner)
        p.end()


class SignalBar(QWidget):
    """A ``numbar`` field: coloured bar plus "-70dBm (good)"."""

    def __init__(self, fld, value: Any = None, *, parent=None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._max = int(getattr(fld, "maximum", 0) or 0)
        self._bands = tuple(getattr(fld, "bar_colors", None) or ())
        self._value: Any = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._bar = _Bar()
        self._label = QLabel("")
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(self._bar, 1)
        row.addWidget(self._label, 0)
        self.set_reading(value)

    # --- value ---------------------------------------------------------

    def _band(self, raw: int) -> tuple[str, str]:
        """(colour, word) for a raw reading -- the first band it fits in."""
        for level, color, label in self._bands:
            if raw <= level:
                return color, label
        if self._bands:
            return self._bands[-1][1], self._bands[-1][2]
        return "", ""

    def set_reading(self, value: Any) -> None:
        self._value = value
        raw: int | None
        try:
            raw = int(value) if value is not None else None
        except (TypeError, ValueError):
            raw = None
        # The same "nothing to report" sentinel Field.render blanks.
        if raw is None or raw >= 0x80000000:
            self._bar.set_reading(0.0, "")
            self._label.setText("—")
            a11y.describe(self, "no reading")
            return
        color, word = self._band(raw)
        frac = (raw / self._max) if self._max else 0.0
        self._bar.set_reading(frac, color)
        text = self._fld.render(raw)
        self._label.setText(f"{text} ({word})" if word else text)
        # The bar is the reading; the band is a colour. Neither is speakable,
        # and the row's own name is all a reader would otherwise get, so the
        # whole reading goes on the field itself. Not announced: these tabs
        # poll at 1 Hz.
        a11y.describe(self, self._label.text())

    def editor_value(self) -> Any:
        return self._value

    def text(self) -> str:
        return self._label.text()

    def set_locked(self, locked: bool) -> None:
        return  # a reading is never editable
