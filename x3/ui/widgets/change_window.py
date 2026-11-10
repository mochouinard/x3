"""The x3 change window: one proposed change, a person, and what happened.

Opened by :mod:`x3.mcp.approve` when an assistant proposes a change
through the MCP server. It is the only place a proposal can turn into a
write: the commands are shown exactly as they would be typed, the checklist
in words, and nothing is sent until Approve. Then the same window follows
the run (:class:`x3.mcp.change.ChangeRun`) and keeps a Revert button
live until the change is kept.

The job file is the window's voice to the assistant: every step is written
there, and a heartbeat with it, so the assistant can tell a change that is
running from a window that was closed or a process that died.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...mcp import change as C
from .. import qthreads
from ..i18n import tr, trf, verbatim

#: The window opens this large when the screen has the room: the proposal
#: and, beside it, the steps. It is never resized from here afterwards; a
#: window that changes size under the pointer is one that gets misclicked.
OPEN_SIZE = (1320, 660)

#: The window answers nothing for this long after it appears: not Approve,
#: not Reject, not Esc, not its close button. It can come up over whatever
#: the person was doing, and a click or a key already on its way must not
#: decide anything.
ARM_AFTER_MS = 4000

_STATE_TEXT = {
    C.KEPT: "Kept: every check passed",
    C.REVERTED: "Reverted: the router is as it was",
    C.NOT_APPLIED: "Nothing was changed",
    C.UNCERTAIN: "Not confirmed: look at the router",
    C.REJECTED: "Rejected",
}
_STATE_COLOUR = {
    C.KEPT: "#1b7f3b", C.REVERTED: "#a35a00", C.NOT_APPLIED: "#555555",
    C.UNCERTAIN: "#b00020", C.REJECTED: "#555555",
}


class _RunWorker(QThread):
    """The run, off the UI thread: it blocks on the router for minutes."""

    progress = Signal(object)
    finished_ok = Signal(object)

    def __init__(self, run: C.ChangeRun, parent=None) -> None:
        super().__init__(parent)
        self._run = run

    def run(self) -> None:  # noqa: N802
        try:
            result = self._run.run()
        except Exception as e:  # noqa: BLE001 -- a crash here must still end the job
            result = {"status": C.UNCERTAIN, "why": f"x3 failed while running the change: {e!r}",
                      "baseline": [], "checks": [], "applied": 0}
        self.finished_ok.emit(result)


class ChangeWindow(QDialog):
    """Approve or reject one job, then show it running.

    ``make_run(job, emit, cancel)`` builds the :class:`ChangeRun` once the
    person approves: the window knows nothing about address books or
    sessions, only about the job and what the run reports.
    """

    def __init__(
        self,
        job: dict[str, Any],
        *,
        make_run: Callable[[dict[str, Any], Callable[[dict[str, Any]], None], threading.Event], C.ChangeRun],
        first_time: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.job = job
        self._make_run = make_run
        self._first_time = first_time
        self._worker: _RunWorker | None = None
        self._cancel = threading.Event()
        self._close_when_done = False
        self._deadline_at: float | None = None
        self._armed = False
        self._arm_at = time.monotonic() + ARM_AFTER_MS / 1000
        self.setWindowTitle(trf("x3: change proposed for {device}", device=job["device"]))
        self.setMinimumWidth(720)

        # What is proposed on the left, for as long as the window is open.
        # What happens to it on the right, once approved: the steps get the
        # full height of the window there, where under the proposal they had
        # a few lines.
        proposal = QWidget()
        left = QVBoxLayout(proposal)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        left.addWidget(self._heading())
        left.addWidget(self._described(), 1)
        left.addWidget(_rule())
        self._decide = self._decision_page()
        left.addWidget(self._decide)
        self._watch = self._progress_page()
        self._split = QSplitter(Qt.Orientation.Horizontal)
        self._split.setChildrenCollapsible(False)
        self._split.addWidget(proposal)
        self._split.addWidget(self._watch)
        # Extra width goes to the steps first: they are what runs long.
        self._split.setStretchFactor(0, 2)
        self._split.setStretchFactor(1, 3)
        root = QVBoxLayout(self)
        root.addWidget(self._split)
        screen = self.screen().availableGeometry() if self.screen() is not None else None
        width, height = OPEN_SIZE
        if screen is not None and screen.width() > 0:
            width, height = min(width, screen.width() - 60), min(height, screen.height() - 80)
        self.resize(width, height)
        self._split.setSizes([width * 9 // 20, width * 11 // 20])
        # Keys typed at the window before anyone has looked at it land in a
        # box that cannot be typed in.
        self.commands.setFocus()

        self._beat = QTimer(self)
        self._beat.setInterval(4000)
        self._beat.timeout.connect(self._heartbeat)
        self._beat.start()
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._countdown)
        self._lock = QTimer(self)
        self._lock.setInterval(max(20, min(250, ARM_AFTER_MS // 2)))
        self._lock.timeout.connect(self._arm)
        self._lock.start()
        self._arm()
        self._heartbeat()

    # --- what is proposed ----------------------------------------------------

    def _heading(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel(trf(
            "<b>Change proposed for {device}</b> &nbsp; {login}@{host}",
            device=_esc(self.job["device"]), login=_esc(self.job["login"]),
            host=_esc(self.job["host"])))
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setWordWrap(True)
        lay.addWidget(title)
        whose = QLabel("Proposed by an AI assistant through x3. Nothing is "
                       "sent to the router unless you approve.")
        whose.setWordWrap(True)
        lay.addWidget(whose)
        if self._first_time:
            new = QLabel("No change has been approved for this router before.")
            new.setStyleSheet("color: #b00020; font-weight: bold;")
            new.setWordWrap(True)
            new.setAccessibleName("First proposal for this router")
            lay.addWidget(new)
        return box

    def _described(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        # The assistant's words and the commands are shown as they came:
        # never looked up in a language table.
        summary = QLabel(verbatim(self.job["summary"]) if self.job.get("summary")
                         else "(no summary)")
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(QLabel("<b>What for</b>"))
        lay.addWidget(summary)
        lay.addSpacing(4)

        lay.addWidget(QLabel("<b>Commands</b>, run at the console exactly as shown:"))
        self.commands = QPlainTextEdit()
        self.commands.setReadOnly(True)
        self.commands.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.commands.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.commands.setPlainText("\n".join(
            f"{i}. {c}" for i, c in enumerate(self.job["commands"], 1)))
        self.commands.setAccessibleName("Commands")
        self.commands.setMinimumHeight(96)
        lay.addWidget(self.commands, 1)
        lay.addSpacing(4)

        lay.addWidget(QLabel("<b>Checklist</b>, run after the change. All must pass, "
                             "or it is reverted:"))
        checks = QLabel(verbatim("\n".join(f"  •  {C.check_label(c)}" for c in self.job["checks"])))
        checks.setWordWrap(True)
        checks.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(checks)

        how = QLabel(trf(
            "How it runs: the configuration is saved, Safe Mode is taken, and a timer "
            "is set on the router itself that reverts everything after {seconds}s. "
            "Then the commands, then the checklist. The change is kept only if every "
            "check passes; otherwise it is reverted and the configuration compared "
            "with the saved one.", seconds=C.deadline_s(self.job)))
        how.setWordWrap(True)
        lay.addSpacing(4)
        lay.addWidget(how)
        return box

    # --- deciding ---------------------------------------------------------------

    def _decision_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Note to the assistant:"))
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional. Sent back with your decision.")
        self.note.setAccessibleName("Note to the assistant")
        row.addWidget(self.note, 1)
        lay.addLayout(row)
        self.remember = QCheckBox("Do not flag this router as new again")
        self.remember.setVisible(self._first_time)
        lay.addWidget(self.remember)

        self.lock_note = QLabel("")
        self.lock_note.setAccessibleName("Buttons locked")
        lay.addWidget(self.lock_note)
        buttons = QHBoxLayout()
        self.never_btn = QPushButton("Never for this router")
        self.never_btn.setToolTip("Reject this, and refuse every later proposal for "
                                  "this router without showing it.")
        self.reject_btn = QPushButton("Reject")
        self.approve_btn = QPushButton("Approve and run")
        # Enter presses nothing: no button is the default, so deciding
        # either way takes a click, or a key aimed at one button.
        for b in (self.never_btn, self.reject_btn, self.approve_btn):
            b.setAutoDefault(False)
            b.setDefault(False)
            b.setEnabled(False)
        self.never_btn.clicked.connect(self._never)
        self.reject_btn.clicked.connect(lambda: self._reject("rejected"))
        self.approve_btn.clicked.connect(self._approve)
        buttons.addWidget(self.never_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.reject_btn)
        buttons.addWidget(self.approve_btn)
        lay.addLayout(buttons)
        return page

    def _arm(self) -> None:
        """Count the lock down, and lift it when its time is up."""
        left = self._arm_at - time.monotonic()
        if left > 0:
            self.lock_note.setText(trf("Read it first. The buttons unlock in {seconds} s.",
                                       seconds=int(left) + 1))
            return
        self._lock.stop()
        self._armed = True
        self.lock_note.setText("")
        if self.job["status"] == C.WAITING:
            for b in (self.never_btn, self.reject_btn, self.approve_btn):
                b.setEnabled(True)

    def _never(self) -> None:
        C.set_policy(self.job["host"], self.job["login"], C.NEVER)
        self._reject("rejected, and this router is closed to proposals")

    def _reject(self, how: str) -> None:
        if self.job["status"] != C.WAITING:
            return
        self.job["status"] = C.REJECTED
        self.job["note"] = self.note.text().strip()
        self.job["result"] = {"status": C.REJECTED, "why": f"The user {how}."}
        self._save()
        self.accept()

    def _approve(self) -> None:
        if self.job["status"] != C.WAITING:
            return
        if self._first_time and self.remember.isChecked():
            C.set_policy(self.job["host"], self.job["login"], C.ALWAYS)
        self.job["status"] = C.RUNNING
        self.job["note"] = self.note.text().strip()
        self._save()
        self._decide.hide()
        self.state.setText("Running")
        self.revert_btn.setEnabled(True)
        run = self._make_run(self.job, self._from_run, self._cancel)
        self._worker = _RunWorker(run, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.start()

    # --- running ------------------------------------------------------------------

    def _progress_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(10, 0, 0, 0)
        self.state = QLabel("Waiting for your decision")
        self.state.setStyleSheet("font-weight: bold;")
        self.state.setAccessibleName("State of the change")
        lay.addWidget(self.state)
        self.deadline = QLabel("")
        lay.addWidget(self.deadline)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setAccessibleName("Steps")
        self.log.setMinimumWidth(360)
        # Not one of the calls the language tables wrap, so looked up here.
        self.log.setPlaceholderText(tr("The steps appear here once you approve."))
        lay.addWidget(self.log, 1)
        buttons = QHBoxLayout()
        self.revert_btn = QPushButton("Revert now")
        self.revert_btn.setToolTip("Stop and undo the change. Works until it is kept.")
        self.revert_btn.setEnabled(False)
        self.revert_btn.setAutoDefault(False)
        self.revert_btn.clicked.connect(self._revert_now)
        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.setAutoDefault(False)
        self.close_btn.clicked.connect(self.accept)
        buttons.addWidget(self.revert_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.close_btn)
        lay.addLayout(buttons)
        return page

    def _from_run(self, event: dict[str, Any]) -> None:
        """Called on the worker thread: hand the event to the UI thread."""
        worker = self._worker
        if worker is not None:
            worker.progress.emit(event)

    def _on_progress(self, event: dict[str, Any]) -> None:
        self.job["events"].append(event)
        self._save()
        stamp = time.strftime("%H:%M:%S", time.localtime(event.get("t", time.time())))
        self.log.appendPlainText(f"{stamp}  {event.get('text', '')}")
        step = event.get("step")
        if step == "timer" and event.get("deadline_s"):
            self._deadline_at = time.monotonic() + float(event["deadline_s"])
            self._tick.start()
            self._countdown()
        if step == "keep":
            # Kept is kept: there is nothing left for Revert to undo.
            self.revert_btn.setEnabled(False)
        if step == "revert":
            self.state.setText("Reverting")
            self.revert_btn.setEnabled(False)

    def _countdown(self) -> None:
        if self._deadline_at is None:
            return
        left = int(self._deadline_at - time.monotonic())
        if left > 0:
            self.deadline.setText(trf(
                "If this is not finished, the router reverts it by itself in {time}.",
                time=f"{left // 60}:{left % 60:02d}"))
        else:
            self.deadline.setText("The router's own revert timer has passed.")

    def _revert_now(self) -> None:
        self._cancel.set()
        self.revert_btn.setEnabled(False)
        self.state.setText("Reverting (asked by you)")

    def _on_done(self, result: dict[str, Any]) -> None:
        status = result.get("status", C.UNCERTAIN)
        self.job["status"] = status
        self.job["result"] = result
        self._save()
        self._tick.stop()
        self.deadline.setText("")
        if status == C.KEPT and result.get("warnings"):
            self.state.setText("Kept, with warnings: read them below")
        else:
            self.state.setText(_STATE_TEXT.get(status, status))
        self.state.setStyleSheet(f"font-weight: bold; color: {_STATE_COLOUR.get(status, '#000')};")
        if result.get("why"):
            self.log.appendPlainText(f"          {result['why']}")
        for line in result.get("diff") or []:
            self.log.appendPlainText(f"          {line}")
        self.revert_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self._beat.stop()
        if self._close_when_done:
            self.accept()

    # --- the job file ----------------------------------------------------------------

    def _save(self) -> None:
        self.job["beat"] = time.time()
        try:
            C.save(self.job)
        except OSError:
            pass  # the run matters more than its record; the next save may land

    def _heartbeat(self) -> None:
        if self.job["status"] not in C.FINAL:
            self._save()

    # --- closing -------------------------------------------------------------------------

    def reject(self) -> None:
        """Esc, or the window's close button."""
        status = self.job["status"]
        if status == C.WAITING:
            if self._armed:        # an Esc in the first seconds was not meant for this
                self._reject("closed the window without deciding")
            return
        if status == C.RUNNING:
            # Closing mid-change means: take it back. The window stays until
            # the router has been seen to be as it was.
            self._close_when_done = True
            self._revert_now()
            self.state.setText("Reverting before closing")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.job["status"] == C.RUNNING:
            event.ignore()
            self.reject()
            return
        if self.job["status"] == C.WAITING:
            if not self._armed:
                event.ignore()
                return
            self._reject("closed the window without deciding")
        qthreads.retire(self._worker)
        super().closeEvent(event)


def _rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _esc(text: object) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
