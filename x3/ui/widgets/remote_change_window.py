"""What somebody else changed on this router, while you had it open.

Deliberately a plain window and nothing more. The moment this has something to
say is the moment the session may already be half-way through fixing whatever
caused it -- somebody has just changed a firewall rule, or dropped an address,
and the person at this keyboard needs the UI *working*, not a modal in front of
it wanting to be dismissed first. So:

* it opens only when the toolbar button is clicked, never on its own;
* it is an ordinary MDI subwindow, so every other window stays usable behind it;
* nothing here blocks, beeps, steals focus, or waits for an answer.

The list refreshes itself while it is open, because the changes it is reporting
may still be arriving.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMdiSubWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import style
from ..icons import window_icon
from . import live_text


class RemoteChangeWindow(QMdiSubWindow):
    """The running list of changes that did not come from this session."""

    #: The user has read them and wants the toolbar warning to go away.
    dismissed = Signal()

    def __init__(self, notes, device_label: str, parent=None) -> None:
        super().__init__(parent)
        self._notes = list(notes)
        self._source = notes  # live list, so late arrivals show up
        self.setWindowTitle(f"Changed elsewhere — {device_label}")
        self.setWindowIcon(window_icon("log"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(
            style.MARGIN, style.MARGIN, style.MARGIN, style.MARGIN
        )
        root.setSpacing(style.SPACING)

        blurb = QLabel(
            "These changes arrived from another management session, the console, or "
            "the device itself — this session did not make them. Counter and "
            "status updates are not listed; only configuration."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};"
        )
        root.addWidget(blurb)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("DejaVu Sans Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(8, style.FONT_PT - 1))
        self._view.setFont(font)
        root.addWidget(self._view, stretch=1)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};"
        )
        bar.addWidget(self._status, stretch=1)
        btn_copy = QPushButton("Copy")
        btn_copy.clicked.connect(self._copy_all)
        bar.addWidget(btn_copy)
        # "Dismiss" clears the toolbar warning and closes. It does not clear
        # the snapshot the changes armed -- acknowledging that somebody edited
        # The router is not the same as deciding not to record it.
        btn_dismiss = QPushButton("Dismiss")
        btn_dismiss.setToolTip(
            "Clear the toolbar warning. The pending config snapshot is "
            "unaffected."
        )
        btn_dismiss.clicked.connect(self._dismiss)
        bar.addWidget(btn_dismiss)
        root.addLayout(bar)

        self.setWidget(container)
        self.resize(style.scale_px(760), style.scale_px(320))
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _refresh(self) -> None:
        try:
            notes = list(self._source)
        except (TypeError, RuntimeError):
            notes = self._notes
        text = "\n".join(n.line() for n in notes)
        # Somebody reading this list is reading it *now*, while the changes are
        # still arriving -- so a 2s redraw keeps their selection and their place
        # in it (x3.ui.widgets.live_text).
        live_text.set_text_keeping_place(self._view, text)
        n = len(notes)
        self._status.setText(
            f"{n} change{'s' if n != 1 else ''} from outside this session"
            if n else "nothing outstanding"
        )

    def _copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication

        clip = QApplication.clipboard()
        if clip is not None:
            clip.setText(self._view.toPlainText())

    def _dismiss(self) -> None:
        self._timer.stop()
        self.dismissed.emit()
        self.close()
