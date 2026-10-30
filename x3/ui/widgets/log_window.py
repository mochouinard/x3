"""Session transcript viewer: what we sent, what came back, what we changed."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMdiSubWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import style
from ..icons import window_icon
from . import live_text

_KINDS = ("request", "reply", "change", "note", "error")


class SessionLogWindow(QMdiSubWindow):
    """Live view of the connection's transcript, with a filter."""

    def __init__(self, session_log, parent=None) -> None:
        super().__init__(parent)
        self._log = session_log
        self.setWindowTitle("Session Log")
        self.setWindowIcon(window_icon("list"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(style.MARGIN, style.MARGIN, style.MARGIN, style.MARGIN)
        root.setSpacing(style.SPACING)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("filter (field name, value, path…)")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._refresh)
        bar.addWidget(self._filter, stretch=1)
        self._changes_only = QCheckBox("Changes only")
        self._changes_only.setToolTip("Show only configuration changes")
        self._changes_only.toggled.connect(self._refresh)
        bar.addWidget(self._changes_only)
        self._follow = QCheckBox("Follow")
        self._follow.setChecked(True)
        bar.addWidget(self._follow)
        btn_copy = QPushButton("Copy all")
        btn_copy.clicked.connect(self._copy_all)
        bar.addWidget(btn_copy)
        root.addLayout(bar)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("DejaVu Sans Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(8, style.FONT_PT - 1))
        self._view.setFont(font)
        root.addWidget(self._view, stretch=1)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self.setWidget(container)
        self.resize(style.scale_px(820), style.scale_px(460))
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self.close)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _entries(self):
        if self._log is None:
            return []
        needle = self._filter.text().strip().lower()
        out = []
        for e in list(self._log.entries):
            if self._changes_only.isChecked() and e.kind != "change":
                continue
            if needle and needle not in e.as_text().lower():
                continue
            out.append(e)
        return out

    def _refresh(self) -> None:
        entries = self._entries()
        text = "\n".join(e.as_text() for e in entries)
        # A 1.5s redraw must not take the reader's place away: scrolled up to
        # read an exchange, or dragging over lines to copy them, is exactly
        # when this window is being used for something.
        live_text.set_text_keeping_place(
            self._view, text, follow=self._follow.isChecked()
        )
        where = getattr(self._log, "path", None)
        skipped = getattr(self._log, "suppressed", 0)
        bits = [f"{len(entries)} shown"]
        if skipped:
            bits.append(f"{skipped} polling exchanges not logged")
        if where:
            bits.append(str(where))
        self._status.setText("  ·  ".join(bits))

    def _copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._view.toPlainText())
