"""The Safe Mode idle reminder.

Its own QDialog rather than a QMessageBox, for two reasons a message box will
not bend on:

* It carries a draining :class:`~x3.ui.hazard.GuardBar` under the buttons.
  QMessageBox rebuilds its grid in ``showEvent``; a widget added to that layout
  is quietly dropped out of it and left floating over the heading.
* QMessageBox caps its own width at the screen width minus 480, which clipped
  the end off three buttons on a small display.

It also lets the locked state be drawn properly. A greyed button is the whole
point of the pause, and Fusion still paints a disabled *default* button in the
highlight colour, so it read as clickable -- which is the one thing it must not.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .. import hazard, style


class SafeModeReminder(QDialog):
    """Who, for how long, what it costs — and three ways out of it."""

    #: What :meth:`choice` gives back.
    KEEP = "keep"
    DISCARD = "discard"
    STAY = "stay"

    def __init__(
        self,
        *,
        heading: str,
        rows: list[tuple[str, str]],
        consequence: str,
        legend: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Safe Mode idle — {heading}")
        self.setModal(True)
        self._choice: str | None = None

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)
        icon = QLabel()
        px = self.style().standardIcon(
            QStyle.StandardPixmap.SP_MessageBoxWarning
        ).pixmap(40, 40)
        icon.setPixmap(px)
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        top.addWidget(icon)

        body = QVBoxLayout()
        body.setSpacing(8)
        title = QLabel(f"{heading} is still in Safe Mode.")
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        body.addWidget(title)

        # A real grid, not tab stops: "admin@10.0.0.1" and "Safe Mode on for 12
        # minutes" do not line up against a \t on a proportional font.
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(2)
        grid.setContentsMargins(0, 0, 0, 0)
        for r, (key, value) in enumerate(rows):
            k = QLabel(key)
            k.setStyleSheet(f"color: {style.TEXT_DIM};")
            grid.addWidget(k, r, 0, Qt.AlignmentFlag.AlignTop)
            v = QLabel(value)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(v, r, 1)
        grid.setColumnStretch(1, 1)
        body.addLayout(grid)

        warn = QLabel(consequence)
        warn.setWordWrap(True)
        body.addWidget(warn)
        top.addLayout(body, 1)
        outer.addLayout(top)

        self.guard_bar = hazard.GuardBar()
        outer.addWidget(self.guard_bar)

        self._legend = QLabel(legend)
        self._legend.setWordWrap(True)
        self._legend.setStyleSheet(f"color: {style.TEXT_DIM};")
        outer.addWidget(self._legend)

        buttons = QDialogButtonBox()
        self.btn_keep = QPushButton("Leave — keep")
        self.btn_discard = QPushButton("Leave — discard")
        self.btn_stay = QPushButton("Stay in Safe Mode")
        self.btn_keep.setToolTip("Release Safe Mode; everything done here stands")
        self.btn_discard.setToolTip("Release Safe Mode, unrolling everything done here")
        self.btn_stay.setToolTip("Leave Safe Mode armed and ask again later")
        buttons.addButton(self.btn_keep, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.btn_discard, QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton(self.btn_stay, QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_keep.clicked.connect(lambda: self._pick(self.KEEP))
        self.btn_discard.clicked.connect(lambda: self._pick(self.DISCARD))
        self.btn_stay.clicked.connect(lambda: self._pick(self.STAY))
        # Enter lands on the one answer that changes nothing. The other two
        # must not claim default when they take focus, or tabbing onto Leave —
        # discard would arm Return to throw the session's work away.
        self.btn_keep.setAutoDefault(False)
        self.btn_discard.setAutoDefault(False)
        self.btn_stay.setAutoDefault(True)
        self.btn_stay.setDefault(True)
        outer.addWidget(buttons)

        # Explicit, because the theme's own disabled rendering is not reliably
        # obvious -- Fusion keeps painting a disabled default button in the
        # highlight colour, which reads as the button you are meant to press.
        self.setStyleSheet(
            "QPushButton:disabled {"
            f" color: {style.TEXT_DIM}; background: {style.BG_ALT};"
            f" border: 1px solid {style.BORDER}; }}"
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def _pick(self, which: str) -> None:
        self._choice = which
        self.accept()

    def choice(self) -> str | None:
        """KEEP / DISCARD / STAY, or None if it was dismissed unanswered."""
        return self._choice

    def buttons(self) -> list[QWidget]:
        return [self.btn_keep, self.btn_discard, self.btn_stay]

    def set_guard(self, secs_left: float, total: float) -> None:
        """Lock or release the answers, and drain the bar to match."""
        self.guard_bar.set_remaining(secs_left, total)
        unlocked = self.guard_bar.remaining() <= 0
        was_unlocked = self.btn_stay.isEnabled()
        for btn in self.buttons():
            btn.setEnabled(unlocked)
        if unlocked and not was_unlocked:
            # The instant answers become possible, put the keyboard on the one
            # that changes nothing. Qt hands focus to the first enabled button
            # in the row otherwise, which is Leave — keep.
            self.btn_stay.setDefault(True)
            self.btn_stay.setFocus(Qt.FocusReason.OtherFocusReason)
