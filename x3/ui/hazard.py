"""Hazard chrome: the yellow-and-black a session wears while Safe Mode is on.

Safe Mode is a work zone. Everything changed under it is provisional, and a
session that drops takes the lot with it -- so the one thing the window must
not do while it is armed is look ordinary. A checked toolbar button was exactly
that: one pressed button in a row of buttons, invisible at a glance and easy to
leave armed for an hour without noticing.

So the session puts on construction tape. Three pieces, all driven from
``MainWindow._refresh_safe_mode_chrome``:

* :func:`stripe_pixmap` -- a seamlessly tileable 45-degree stripe tile.
* :func:`mdi_brush` -- that tile muted towards the theme's desktop colour, so
  the MDI floor reads as a work zone without swallowing the windows on it.
* :class:`HazardBar` -- the banner under the toolbar. It says why the window
  looks like this, and it blinks (:meth:`HazardBar.set_alarm`) once the idle
  watchdog decides the session has been armed and then forgotten.
* :class:`GuardBar` -- the draining bar in the reminder dialog, counting out
  the seconds during which that dialog will not accept an answer.

Tiles are cached per (period, colours): the MDI floor repaints on every resize
and on every window drag, and rebuilding a pixmap per paint is pure waste.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QLineF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import a11y

#: Barricade tape, near enough: a warm saturated yellow, and a black that is
#: not quite black -- flat #000 reads as a hole punched in a dark theme.
HAZARD_YELLOW = "#f2c40f"
HAZARD_BLACK = "#17171a"

#: The banner's other face while the idle alarm blinks.
ALARM_RED = "#c62828"

#: Tile edge in px. What you see is the stripe pitch, and 28 is wide enough to
#: read as tape rather than as a hatch fill.
PERIOD = 28

#: Fraction of the stripe pitch the black band covers. Real tape is even.
DUTY = 0.5

_TILES: dict[tuple, QPixmap] = {}


def _mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    """``a`` moved ``t`` of the way towards ``b`` (0 = a, 1 = b)."""
    ca, cb = QColor(a), QColor(b)
    t = min(1.0, max(0.0, float(t)))
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    )


def stripe_pixmap(
    period: int = PERIOD,
    *,
    yellow: str = HAZARD_YELLOW,
    black: str = HAZARD_BLACK,
    duty: float = DUTY,
) -> QPixmap:
    """A square of 45-degree hazard stripes that tiles without a seam.

    Seamless by construction: the stripes run at exactly 45 degrees and sit one
    tile-width apart along x, so sliding the pattern down by ``period`` slides
    it across by ``period`` too -- which is the tile. Each line is drawn well
    past both edges (a wide flat-capped pen ends square *across* the line, so a
    line stopping at the border would leave a triangular notch), and the
    neighbouring tiles' lines are drawn as well so nothing is clipped away.
    """
    p = max(4, int(period))
    key = (p, str(yellow), str(black), round(float(duty), 3))
    cached = _TILES.get(key)
    if cached is not None:
        return cached

    pix = QPixmap(p, p)
    pix.fill(QColor(yellow))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(black))
    # Pen width is measured across the stripe. Lines spaced `p` apart along x
    # are only p/sqrt(2) apart perpendicular, so that is the pitch to divide.
    pen.setWidthF(max(1.0, float(duty) * p / math.sqrt(2)))
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    for i in range(-2, 3):
        x = i * p
        painter.drawLine(QLineF(x - p, -p, x + 2 * p, 2 * p))
    painter.end()

    _TILES[key] = pix
    return pix


def stripe_brush(**kw) -> QBrush:
    """Full-strength tape, for the banner."""
    return QBrush(stripe_pixmap(**kw))


#: The floor's stripe pitch. Wider than the banner's on purpose: at the
#: banner's 28px a wall-sized area reads as woven fabric, not as tape.
FLOOR_PERIOD = 52


def mdi_brush(
    desktop: str, *, period: int = FLOOR_PERIOD, strength: float = 0.36
) -> QBrush:
    """The tape muted towards ``desktop``, for the MDI floor.

    Full-strength tape across the whole floor fights every window standing on
    it. At about a third strength the diagonal still says "work zone" the
    moment the window comes into view, and the windows keep the foreground.
    """
    return QBrush(
        stripe_pixmap(
            period,
            yellow=_mix(desktop, HAZARD_YELLOW, strength).name(),
            black=_mix(desktop, HAZARD_BLACK, strength).name(),
        )
    )


def button_qss() -> str:
    """Stylesheet for the toolbar toggle while Safe Mode is armed."""
    return (
        "QToolButton {"
        f" background: {HAZARD_YELLOW}; color: {HAZARD_BLACK};"
        f" border: 1px solid {HAZARD_BLACK}; border-radius: 2px;"
        " font-weight: bold; padding: 1px 6px; }"
        f"QToolButton:hover {{ background: {_mix(HAZARD_YELLOW, '#ffffff', 0.25).name()}; }}"
    )


class GuardBar(QWidget):
    """A draining bar showing how long the controls stay locked.

    Buttons that are merely greyed out read as a dialog that has hung, and a
    number ticking down in a paragraph of body text is not where anyone is
    looking. This gives the wait a shape: a caption, and tape draining out of a
    track beside it.

    The caption sits *next to* the bar rather than on it. Written across the
    tape it needed a plate behind it to stay legible, and that plate covered
    most of the bar -- leaving stripes at the two ends and nothing that read as
    draining at all.

    It never changes size and never leaves the layout: at zero it goes quiet and
    says so. A widget that vanished, or a caption that moved the bar as its
    digits changed, would shift the dialog at the exact moment the buttons
    became clickable -- precisely when nothing should move under the pointer.
    """

    #: Reserved caption width is measured off this, not off the live text, so
    #: the bar's left edge does not step sideways once a second.
    _WIDEST = "Controls locked — 88s"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("guardBar")
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._fraction = 0.0
        self._secs = 0

    def remaining(self) -> int:
        return self._secs

    def set_remaining(self, secs_left: float, total: float) -> None:
        """How much of the wait is left, as seconds and as a 0..1 fraction."""
        total = max(1e-6, float(total))
        fraction = min(1.0, max(0.0, float(secs_left) / total))
        secs = max(0, math.ceil(float(secs_left) - 1e-9))
        # Repaint only on a visible change: a timer drives this many times a
        # second, and most ticks move the tape by less than a pixel.
        if abs(fraction - self._fraction) < 0.004 and secs == self._secs:
            return
        self._fraction, self._secs = fraction, secs
        self.update()

    def _caption(self) -> str:
        return (
            f"Controls locked — {self._secs}s"
            if self._secs > 0
            else "Controls unlocked"
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        locked = self._secs > 0

        font = self.font()
        font.setBold(True)
        p.setFont(font)
        fm = p.fontMetrics()
        text_w = fm.horizontalAdvance(self._WIDEST)
        full = QRectF(self.rect())

        p.setPen(
            self.palette().color(
                QPalette.ColorGroup.Active if locked else QPalette.ColorGroup.Disabled,
                QPalette.ColorRole.WindowText,
            )
        )
        p.drawText(
            QRectF(full.left(), full.top(), text_w, full.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._caption(),
        )

        bar = QRectF(full.left() + text_w + 10, 0, 0, 11)
        bar.setRight(full.right() - 1)
        bar.moveTop(full.top() + (full.height() - bar.height()) / 2.0)
        if bar.width() < 12:
            p.end()
            return
        radius = bar.height() / 2.0

        track = QPainterPath()
        track.addRoundedRect(bar, radius, radius)
        p.fillPath(track, QColor(_mix(HAZARD_BLACK, "#ffffff", 0.74)))

        if self._fraction > 0:
            # Clipped to the rounded track, so the draining edge keeps the
            # bar's shape instead of squaring it off.
            p.save()
            p.setClipPath(track)
            p.fillRect(
                QRectF(bar.left(), bar.top(),
                       bar.width() * self._fraction, bar.height()),
                stripe_brush(period=14),
            )
            p.restore()

        p.setPen(QPen(QColor(_mix(HAZARD_BLACK, "#ffffff", 0.45)), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(track)
        p.end()


class HazardBar(QWidget):
    """The banner under the toolbar: striped tape carrying one line of text.

    Painted rather than styled. A QSS background-image would tile from the
    widget's own origin and stay put, which is all we need -- but the alarm
    swaps the whole fill for solid red on a blink, and swapping a stylesheet
    twice a second re-polishes the widget tree each time.
    """

    #: Half-period of the alarm blink. Slow enough not to strobe, fast enough
    #: to catch the eye from across a desk.
    BLINK_MS = 450

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("hazardBar")
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._text = str(text)
        self._alarm = False
        self._blink_on = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.BLINK_MS)
        self._timer.timeout.connect(self._toggle_blink)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        text = str(text)
        if text != self._text:
            self._text = text
            # Painted text is invisible to a screen reader, and this line is
            # the one that says why the window is wearing tape.
            self.setAccessibleName(text)
            self.update()

    def alarming(self) -> bool:
        return self._alarm

    def set_alarm(self, on: bool) -> None:
        """Start or stop the blink. Idempotent, so callers need not track it."""
        on = bool(on)
        if on == self._alarm:
            return
        self._alarm = on
        self._blink_on = on
        if on:
            # A blink is the one piece of this chrome with nothing else behind
            # it: the banner has words, the tape has a colour, and the alarm is
            # motion. Said out loud instead. (The reminder dialog that follows
            # is modal and speaks for itself.)
            a11y.announce(self, f"Safe Mode unattended — {self._text}")
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        rect = self.rect()
        flashing = self._alarm and self._blink_on
        if flashing:
            p.fillRect(rect, QColor(ALARM_RED))
        else:
            p.fillRect(rect, stripe_brush())
        if self._text:
            font = self.font()
            font.setBold(True)
            p.setFont(font)
            fm = p.fontMetrics()
            w = min(rect.width() - 8, fm.horizontalAdvance(self._text) + 20)
            h = min(rect.height() - 6, fm.height() + 6)
            plate = QRectF(
                (rect.width() - w) / 2.0, (rect.height() - h) / 2.0, w, h
            )
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(Qt.PenStyle.NoPen)
            # The plate stays black through the blink, so the text never
            # vanishes into the red half of the cycle.
            p.setBrush(QColor(HAZARD_BLACK))
            p.drawRoundedRect(plate, 3, 3)
            p.setPen(QColor(HAZARD_YELLOW))
            p.drawText(plate, Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()
