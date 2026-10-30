"""Side-by-side view of the fonts each Winbox generation draws in.

Winbox v3 is Tahoma, Winbox 4 is Segoe UI, and older the router dialogs are
Microsoft Sans Serif -- but on a stock Linux box all three names substitute to
the same family, so switching skins changed nothing about the type. This
window shows what is really being drawn: one column per family with the same
sample at the same size, the metrics underneath, and an overlay strip that
puts the samples on a shared baseline so the width drift is visible rather
than a matter of memory.

Each column can be pinned to the whole UI (``Use in the UI``); the pin lives
in settings and survives a theme change. See :mod:`x3.ui.fonts` for where
the font files come from.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import fonts as fonts_mod
from .. import style
from ..fonts import solid_ink
from .wheel_guard import guard_wheel

#: Strings chosen for what actually differs between these faces: Tahoma's
#: narrow digits and tight punctuation show up in the address line, Segoe UI's
#: rounder lowercase in the menu line, and the confusables line is where a
#: substituted font stops being harmless.
SAMPLES: tuple[str, ...] = (
    "Interfaces  Bridge  IP  Routing  System  Tools",
    "192.168.88.1/24  48:A9:8A:0F:1C:22  1000Mbps  -73dBm",
    "Il1 O0 rn m  gjpqy  {}[]()<>  0123456789",
)


#: Overlay ink. The first family borrows the theme's text colour; the other
#: two are fixed hues picked to stay legible on both a light Win32 gray and a
#: dark slate, because a theme's own accents are not guaranteed to differ from
#: each other (classic uses one navy for both selection and title bar).
_OVERLAY_HUES = ("#d64545", "#2f81f7")


class _Overlay(QWidget):
    """The same string in every family: one baseline, one left edge.

    Two readings in one strip. The overlaid text shows the drawing -- where
    the glyphs are wider, rounder, taller. The bars underneath show the cost
    of that: how far a column, a label or a menu shifts when the family
    changes, which is the part that breaks a dense layout.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._families: list[str] = []
        self._text = SAMPLES[0]
        self._px = 11
        self._hint = None
        self.setMinimumHeight(96)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Fixed
        )

    def set_state(self, families: list[str], text: str, px: int, hint=None) -> None:
        self._families = list(families)
        self._text = text
        self._px = px
        self._hint = hint
        self.setMinimumHeight(self.sizeHint().height())
        self.updateGeometry()
        self.update()

    def _face(self, family: str, px: int) -> QFont:
        font = QFont(family)
        font.setPixelSize(max(7, px))
        if self._hint is not None:
            font.setHintingPreference(self._hint)
        return font

    def _colors(self) -> list[QColor]:
        out = [QColor(style.TEXT)]
        out += [QColor(h) for h in _OVERLAY_HUES]
        return out

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt name
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(style.BG_WINDOW))
        if not self._families:
            return
        colors = self._colors()
        left = 8
        small = self._face(self.font().family(), self._px - 2)
        legend_h = QFontMetricsF(small).height()

        # Legend: swatch + family name, so the colours mean something.
        p.setFont(small)
        x = float(left)
        for i, fam in enumerate(self._families):
            c = colors[i % len(colors)]
            p.fillRect(int(x), int(legend_h * 0.35), 8, 8, c)
            p.setPen(QPen(c))
            p.drawText(int(x) + 12, int(legend_h), fam)
            x += 12 + QFontMetricsF(small).horizontalAdvance(fam) + 18

        # The overlay itself: every family from the same origin.
        baseline = legend_h + 6 + self._px * 1.6
        widths: list[float] = []
        for i, fam in enumerate(self._families):
            font = self._face(fam, self._px)
            fm = QFontMetricsF(font)
            widths.append(fm.horizontalAdvance(self._text))
            c = QColor(colors[i % len(colors)])
            c.setAlpha(150)
            p.setFont(font)
            p.setPen(QPen(c))
            p.drawText(left, int(baseline), self._text)

        # Width bars: the same measurement, unstacked and labelled. One row
        # per family, a row tall enough for its own label -- stacking them
        # tighter than the small font is exactly how the numbers collide.
        p.setFont(small)
        step = int(QFontMetricsF(small).height()) + 2
        bar_y = int(baseline) + 8
        for i, w in enumerate(widths):
            c = colors[i % len(colors)]
            y = bar_y + i * step
            p.fillRect(left, y, int(w), 3, c)
            p.setPen(QPen(c))
            p.drawText(left + int(w) + 6, y + 5, f"{w:.0f}px")
        p.end()

    def sizeHint(self):  # noqa: N802 - Qt name
        from PySide6.QtCore import QSize

        width = 320
        for fam in self._families:
            font = self._face(fam, self._px)
            width = max(
                width, int(QFontMetricsF(font).horizontalAdvance(self._text)) + 70
            )
        small = self._face(self.font().family(), self._px - 2)
        step = int(QFontMetricsF(small).height()) + 2
        height = int(QFontMetricsF(small).height()) + 14 + int(self._px * 1.6)
        height += step * max(1, len(self._families)) + 6
        return QSize(width, height)


class _Column(QFrame):
    """One family: status, samples, metrics, and a button to adopt it."""

    chosen = Signal(str)

    def __init__(self, family: str, parent=None) -> None:
        super().__init__(parent)
        self.family = family
        self.setFrameShape(QFrame.Shape.StyledPanel)
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(4)

        self._title = QLabel(family)
        f = QFont(self._title.font())
        f.setBold(True)
        self._title.setFont(f)
        box.addWidget(self._title)

        self._status = QLabel()
        self._status.setWordWrap(True)
        box.addWidget(self._status)

        self._samples = [QLabel(s) for s in SAMPLES]
        for lab in self._samples:
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(lab)

        self._custom = QLabel()
        box.addWidget(self._custom)

        self._metrics = QLabel()
        self._metrics.setStyleSheet(f"color: {style.TEXT_DIM};")
        box.addWidget(self._metrics)

        box.addStretch(1)
        self._use = QPushButton("Use in the UI")
        self._use.clicked.connect(lambda: self.chosen.emit(self.family))
        box.addWidget(self._use)

    def refresh(self, text: str, px: int, *, pinned: str, hint=None) -> None:
        font = QFont(self.family)
        font.setPixelSize(px)
        if hint is not None:
            font.setHintingPreference(hint)
        for lab in self._samples:
            lab.setFont(font)
        self._custom.setFont(font)
        self._custom.setText(text)

        got = fonts_mod.resolve(self.family, pixel_size=px)
        sub = got.casefold() != self.family.casefold()
        stand = fonts_mod.standin_for(self.family)
        if not sub:
            self._status.setText("installed — this is the real face")
            self._status.setStyleSheet(f"color: {style.TITLE_ACTIVE};")
        elif stand:
            kind = (
                "same metrics"
                if stand in fonts_mod.METRIC_CLONES
                else "closest on this box"
            )
            self._status.setText(f"not installed — standing in: {stand} ({kind})")
            self._status.setStyleSheet(f"color: {style.TEXT_DIM};")
        else:
            self._status.setText(f"not installed — {got}, an unrelated face")
            self._status.setStyleSheet(f"color: {style.TEXT_DIM};")

        fm = QFontMetricsF(font)
        self._metrics.setText(
            f"{fm.horizontalAdvance(SAMPLES[1]):.0f}px wide · "
            f"{fm.height():.0f}px line · asc {fm.ascent():.0f} desc {fm.descent():.0f} · "
            f"{100 * solid_ink(font, SAMPLES[1]):.0f}% solid ink"
        )
        on = pinned.casefold() == self.family.casefold()
        self._use.setText("Pinned" if on else "Use in the UI")
        self._use.setEnabled(not on)


class FontCompareDialog(QDialog):
    """View → Font → Compare…: what these three families actually look like."""

    fontChosen = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Font comparison")
        theme = style.active_theme()
        self._px_default = style.ui_pixel_size()

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        head = QLabel(
            "The classic skins draw in Tahoma, the slate ones in Segoe UI, and "
            "older dialogs in Microsoft Sans Serif. A family that is not installed "
            "is silently substituted — the column says which. Sizes are in "
            "pixels because that is how Windows specifies them: v3's list font "
            "is an 11px Tahoma, and points would put it at the mercy of this "
            "screen's DPI."
        )
        head.setWordWrap(True)
        root.addWidget(head)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Size:"))
        self._px = QSpinBox()
        self._px.setRange(7, 28)
        self._px.setValue(self._px_default)
        self._px.setSuffix(" px")
        self._px.setToolTip("Em box in pixels. The classic skin's lists are 11px.")
        guard_wheel(self._px)
        self._px.valueChanged.connect(self._refresh)
        controls.addWidget(self._px)
        controls.addWidget(QLabel("Sample:"))
        self._text = QLineEdit(SAMPLES[1])
        self._text.textChanged.connect(self._refresh)
        controls.addWidget(self._text, 1)
        self._gdi = QCheckBox("GDI hinting")
        self._gdi.setChecked(theme.font_hinting == "full")
        self._gdi.setToolTip(
            "Snap stems onto whole pixels the way Windows' GDI does, instead of "
            "fontconfig's vertical-only hinting. Same size, more solid black "
            "pixels — this is the weight difference, not a heavier face."
        )
        self._gdi.toggled.connect(self._refresh)
        controls.addWidget(self._gdi)
        self._overlay_on = QCheckBox("Overlay")
        self._overlay_on.setChecked(True)
        self._overlay_on.setToolTip(
            "Draw every family's sample on one baseline from one left edge"
        )
        self._overlay_on.toggled.connect(self._refresh)
        controls.addWidget(self._overlay_on)
        root.addLayout(controls)

        cols = QHBoxLayout()
        cols.setSpacing(6)
        self.columns = [_Column(fam, self) for fam in fonts_mod.COMPARE_FAMILIES]
        for col in self.columns:
            col.chosen.connect(self._choose)
            cols.addWidget(col, 1)
        root.addLayout(cols, 1)

        self.overlay = _Overlay(self)
        root.addWidget(self.overlay)

        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet(f"color: {style.TEXT_DIM};")
        root.addWidget(self._note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self._theme_btn = buttons.addButton(
            "Back to theme default", QDialogButtonBox.ButtonRole.ResetRole
        )
        self._theme_btn.setToolTip(
            f"Let the theme choose again ({theme.font_family or 'system UI'})"
        )
        self._theme_btn.clicked.connect(lambda: self._choose(""))
        root.addWidget(buttons)

        fonts_mod.sideload()
        self._refresh()

    def _refresh(self) -> None:
        px = self._px.value()
        text = self._text.text()
        pinned = fonts_mod.override()
        # Always a concrete preference, never "unset": QFont resolves anything
        # it was not told from the application font, so an unticked box would
        # have inherited the classic skin's full hinting and shown no
        # difference at all. Unticked means the desktop's own setting.
        hint = (
            QFont.HintingPreference.PreferFullHinting
            if self._gdi.isChecked()
            else QFont.HintingPreference.PreferDefaultHinting
        )
        for col in self.columns:
            col.refresh(text, px, pinned=pinned, hint=hint)
        show_overlay = self._overlay_on.isChecked()
        self.overlay.setVisible(show_overlay)
        if show_overlay:
            self.overlay.set_state(list(fonts_mod.COMPARE_FAMILIES), text, px, hint)

        missing = [
            c.family
            for c in self.columns
            if fonts_mod.is_substituted(c.family, pixel_size=px)
        ]
        if missing:
            self._note.setText(
                f"{', '.join(missing)} not installed — those columns are "
                f"stand-ins, not the same drawing. For the real thing, copy "
                f"segoeui.ttf / micross.ttf / tahoma.ttf into "
                f"{fonts_mod.drop_dir()} and reopen: they are loaded straight "
                f"from there, no system install needed. "
                f"Pinned: {pinned or 'theme default'}"
            )
        else:
            self._note.setText(
                f"All three are the real faces. Pinned: {pinned or 'theme default'}"
            )

    def _choose(self, family: str) -> None:
        fonts_mod.apply(family)
        self.fontChosen.emit(family)
        self._refresh()
