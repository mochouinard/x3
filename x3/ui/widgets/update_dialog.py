"""Finding and installing a newer x3: the background check, its notice, and
the dialog that installs it.

:data:`notifier` is the one place a found update is announced; the connection
page and every session window show a button while it holds one. The work
itself is in :mod:`x3.ui.updates`; this module only drives it off the GUI
thread and asks before anything is installed.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .. import __version__, applog, settings, style, updates
from ..icons import app_icon


class _Notifier(QObject):
    """Holds the newest release found, and says so when one turns up."""

    available = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.release: updates.Release | None = None

    def offer(self, release: updates.Release) -> None:
        self.release = release
        self.available.emit(release)


notifier = _Notifier()


class CheckWorker(QThread):
    """Reads the release feed. ``finished_ok`` carries the Release."""

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(updates.fetch_latest())
        except updates.UpdateError as e:
            self.finished_err.emit(str(e))


class DownloadWorker(QThread):
    """Downloads and verifies this install's file. ``finished_ok`` carries its path."""

    finished_ok = Signal(object)
    finished_err = Signal(str)
    progress = Signal(int, int)

    def __init__(self, release: updates.Release, kind: str, parent=None) -> None:
        super().__init__(parent)
        self._release = release
        self._kind = kind

    def run(self) -> None:  # noqa: N802
        try:
            path = updates.fetch_verified(
                self._release, self._kind, updates.download_folder(),
                progress=lambda done, total: self.progress.emit(done, total),
            )
            self.finished_ok.emit(path)
        except updates.UpdateError as e:
            self.finished_err.emit(str(e))


def check_in_background(parent, *, manual: bool = False, on_result=None) -> CheckWorker:
    """Start a feed read. A newer, not-skipped release goes to :data:`notifier`
    (a manual check offers even a skipped one). ``on_result(release_or_None,
    error_or_None)`` is called either way."""
    worker = CheckWorker(parent)

    def ok(release: updates.Release) -> None:
        settings.save_update_state(last_check=time.time())
        _last, skipped = settings.load_update_state()
        newer = updates.is_newer(release.version)
        if newer and (manual or release.version != skipped):
            notifier.offer(release)
        if on_result is not None:
            on_result(release if newer else None, None)

    def err(message: str) -> None:
        if on_result is not None:
            on_result(None, message)

    worker.finished_ok.connect(ok)
    worker.finished_err.connect(err)
    worker.start()
    return worker


def maybe_check_at_start(parent) -> CheckWorker | None:
    """The daily automatic check, if the user turned it on."""
    if not settings.load_update_check():
        return None
    last, _skipped = settings.load_update_state()
    if not updates.due(last):
        return None
    return check_in_background(parent)


class DailyCheck(QObject):
    """Checks at start and then every hour whether a day has passed.

    x3 is often left open on a router for days, so a check only at start would
    go stale. Each tick is cheap -- it reads the last-check time and returns --
    and the check itself still happens at most once a day, and only while the
    user has it on.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: CheckWorker | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(3600 * 1000)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        self.tick()

    def tick(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._worker = maybe_check_at_start(self)


class UpdateDialog(QDialog):
    """What the new version is, and the one action this install can take."""

    def __init__(self, release: updates.Release, parent=None) -> None:
        super().__init__(parent)
        self.release = release
        self.kind = updates.install_kind()
        self._worker: DownloadWorker | None = None
        self.setWindowTitle("Update x3")
        self.resize(style.scale_px(560), style.scale_px(440))

        root = QVBoxLayout(self)
        head = QHBoxLayout()
        mark = QLabel()
        px = style.scale_px(48)
        mark.setPixmap(app_icon().pixmap(px, px))
        head.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        title = QLabel(
            f"<span style='font-size:large; font-weight:600'>x3 {release.version} "
            f"is available</span><br>You have {__version__}."
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        head.addWidget(title, 1)
        root.addLayout(head)

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "_No release notes._")
        root.addWidget(notes, 1)

        self.how = QLabel()
        self.how.setWordWrap(True)
        self.how.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.how)
        self.bar = QProgressBar()
        self.bar.hide()
        root.addWidget(self.bar)

        buttons = QHBoxLayout()
        self.skip_button = QPushButton("&Skip This Version")
        self.page_button = QPushButton("Release &Page")
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.page_button)
        buttons.addStretch(1)
        self.later_button = QPushButton("&Later")
        self.action_button = QPushButton()
        self.action_button.setDefault(True)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.action_button)
        root.addLayout(buttons)

        self.skip_button.clicked.connect(self._skip)
        self.page_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(release.page))
        )
        self.later_button.clicked.connect(self.reject)
        if self.kind == "pip":
            self.how.setText(
                "This copy of x3 was installed with pip, so it cannot replace "
                f"itself. To update, run:\n\n    {updates.upgrade_command()}"
            )
            self.action_button.setText("&Copy Command")
            self.action_button.clicked.connect(self._copy_command)
        else:
            what = ("the installer runs and x3 starts again" if self.kind == "windows"
                    else "the AppImage is replaced and x3 starts again")
            self.how.setText(
                f"x3 downloads the new version, checks it against the release's "
                f"published SHA-256, then closes: {what}. Open sessions are "
                "disconnected, and changes still in the queue are not sent."
            )
            self.action_button.setText("&Install and Restart")
            self.action_button.clicked.connect(self._install)

    def _skip(self) -> None:
        settings.save_update_state(skipped=self.release.version)
        self.reject()

    def _copy_command(self) -> None:
        QGuiApplication.clipboard().setText(updates.upgrade_command())
        self.action_button.setText("Copied")

    def _install(self) -> None:
        for b in (self.action_button, self.skip_button, self.later_button):
            b.setEnabled(False)
        self.bar.show()
        self.bar.setRange(0, 0)
        self._worker = DownloadWorker(self.release, self.kind, self)
        self._worker.progress.connect(self._progress)
        self._worker.finished_ok.connect(self._apply)
        self._worker.finished_err.connect(self._failed)
        self._worker.start()

    def _progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)

    def _failed(self, message: str) -> None:
        self.bar.hide()
        self.how.setText(f"The update was not installed: {message}")
        for b in (self.action_button, self.skip_button, self.later_button):
            b.setEnabled(True)

    def _apply(self, path) -> None:
        try:
            if self.kind == "windows":
                updates.apply_windows(path)
            else:
                updates.apply_appimage(path)
        except OSError as e:
            self._failed(str(e))
            return
        # The new copy is starting; this one gets out of its way at once. The
        # dialog said open sessions close and queued changes are not sent, so
        # no window gets to ask about them now (a prompt here would leave both
        # copies running). Settings are saved as they change; only the log
        # needs flushing.
        applog.shutdown(0)
        os._exit(0)
