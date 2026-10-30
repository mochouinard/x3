"""Automatic config snapshots: one export, a quiet interval after the last change.

Not a schedule. A session that changes nothing exports nothing, ever -- leaving
a window open on a router for two days must not fill a directory with 192
identical files. The timer is a **debounce armed by change**:

    change ─→ (re)start a 15 min countdown ─→ fires once ─→ disarmed
                     ↑                                          │
                     └────────── next change re-arms ───────────┘

So an edit in the first minute of a two-day session produces exactly one file,
fifteen minutes after that edit. A burst of twenty edits over an hour produces
one file, fifteen minutes after the twentieth.

Two things arm it:

* **our own writes** -- every successful :meth:`EditSession.apply`, which is the
  one choke point every local change goes through (set, add, remove, move,
  enable/disable, comment, doit, settings write).
* **someone else's writes** -- a list whose configuration content moved without
  us touching it. :mod:`x3.ui.change_watch` is what separates that from the
  counter feed arriving on the same subscription.

The export itself is :meth:`DeviceClient.export_config` -- a console
``/export terse`` read straight into memory. Nothing is written to the router's
flash, which is the whole point: this runs unattended, on every session, across
a fleet that includes boxes whose flash has already had a long life.

Identical captures are not stored twice. The body digest is stable across
sessions (measured: two runs three minutes apart on the same device produce the
same sha256), so a change that was made and then reverted correctly stores
nothing and says so.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from . import applog, settings

#: Default quiet interval before a snapshot is taken.
DEFAULT_IDLE_SECS = 15 * 60

#: Filename-safe stand-in for anything the device calls itself that a
#: filesystem would object to.
_SAFE = "-"


def _slug(text: str, *, limit: int = 40) -> str:
    keep = [c if (c.isalnum() or c in "._-") else _SAFE for c in (text or "")]
    out = "".join(keep).strip(_SAFE) or "device"
    return out[:limit]


def export_root() -> Path:
    return settings.data_dir() / "config-exports"


def device_dir(session: Any) -> Path:
    """One directory per device, named so a human can find it.

    Identity *and* host: identity alone collides across a fleet where three
    boxes are still called the vendor, and host alone is meaningless once DHCP
    moves it.
    """
    identity = _slug(getattr(session, "identity", "") or "device")
    host = _slug(getattr(session, "host", "") or "")
    name = f"{identity}_{host}" if host else identity
    return export_root() / name


@dataclass
class ChangeNote:
    """One thing that armed the timer."""

    #: "local" (we wrote it) or "remote" (it changed under us).
    source: str
    #: Where -- a menu title, or the window that made the write.
    where: str
    detail: str
    when: float = field(default_factory=time.time)

    def clock(self) -> str:
        return datetime.fromtimestamp(self.when).strftime("%H:%M:%S")

    def line(self) -> str:
        return f"{self.clock()}  {self.where} — {self.detail}"


@dataclass
class ExportRecord:
    """What one stored snapshot turned out to be."""

    path: Path | None
    digest: str
    reason: str
    changes: int
    #: True when the config matched the last stored capture, so nothing new
    #: was written.
    unchanged: bool = False
    disturbed: bool = False


class _ExportWorker(QThread):
    """Run one export off the UI thread.

    A 27 KB export took about two seconds on the 10-port router: not long, but long
    enough that doing it on the UI thread would freeze the window on a timer
    the user never pressed.
    """

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class AutoExport(QObject):
    """The debounce, the store, and the record of who changed what.

    Owned by :class:`MainWindow`; one per session, so two windows on two
    routers keep separate countdowns and separate directories.
    """

    #: Anything the chrome needs to redraw: armed/disarmed, a new remote note,
    #: a capture landing.
    state_changed = Signal()
    #: A snapshot was dealt with -- stored, or found identical to the last one.
    exported = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        client: Any,
        session: Any,
        idle_secs: int = DEFAULT_IDLE_SECS,
        enabled: bool = True,
        show_sensitive: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.session = session
        self.idle_secs = int(idle_secs)
        self.enabled = bool(enabled)
        #: Whether the captures include secrets. See
        #: :func:`settings.load_auto_export_sensitive` for why this is off
        #: unless somebody asked for it.
        self.show_sensitive = bool(show_sensitive)
        #: Changes seen since the last capture, oldest first.
        self.pending: list[ChangeNote] = []
        #: Remote changes not yet acknowledged by the user. Kept apart from
        #: `pending`: the timer forgets a change once it has exported it, the
        #: warning stays up until somebody looks at it.
        self.remote_notes: list[ChangeNote] = []
        self.last_record: ExportRecord | None = None
        self.last_error: str = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._worker: _ExportWorker | None = None
        self._deadline: float | None = None
        #: Wall-clock of the most recent local write, per menu path. A device
        #: notifies *every* subscriber of a change including the one that made
        #: it, so our own edit comes back as a push a moment later; without
        #: this the session would report itself as "someone else".
        self._own_writes: dict[tuple, float] = {}
        #: How long a push is still attributable to our own write.
        self.self_write_grace = 8.0

    # -- state ----------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._timer.isActive()

    @property
    def busy(self) -> bool:
        return self._worker is not None

    def seconds_left(self) -> int:
        if self._deadline is None or not self.armed:
            return 0
        return max(0, int(round(self._deadline - time.monotonic())))

    def status_line(self) -> str:
        """One line for a tooltip: what this is waiting for, or what it did."""
        if not self.enabled:
            return "Automatic config snapshots are off."
        if self.busy:
            return "Taking a config snapshot now…"
        if self.armed:
            left = self.seconds_left()
            mins, secs = divmod(left, 60)
            when = f"{mins} min {secs:02d} s" if mins else f"{secs} s"
            n = len(self.pending)
            return (
                f"{n} change{'s' if n != 1 else ''} recorded — snapshot in {when} "
                "of quiet, and the countdown restarts on the next change."
            )
        if self.last_error:
            return f"Last snapshot failed: {self.last_error}"
        rec = self.last_record
        if rec is None:
            return "No change yet this session, so no snapshot has been needed."
        if rec.unchanged:
            return "Last snapshot: config identical to the previous one, nothing stored."
        return f"Last snapshot: {rec.path.name if rec.path else '?'}"

    # -- arming ---------------------------------------------------------

    def note_local(self, where: str, detail: str, path: tuple | None = None) -> None:
        """Record a write this session made."""
        if path is not None:
            self._own_writes[tuple(path)] = time.monotonic()
        self._add(ChangeNote("local", where or "session", detail))

    def note_remote(self, where: str, detail: str) -> None:
        """Record a change that arrived from outside this session."""
        note = ChangeNote("remote", where or "device", detail)
        self.remote_notes.append(note)
        applog.note(f"remote change: {note.where} — {note.detail}")
        self._add(note)

    def owns_recent_write(self, path: tuple | None) -> bool:
        """Whether a push on ``path`` is the echo of our own recent write."""
        if path is None:
            return False
        stamp = self._own_writes.get(tuple(path))
        if stamp is None:
            return False
        return (time.monotonic() - stamp) <= self.self_write_grace

    def _add(self, note: ChangeNote) -> None:
        self.pending.append(note)
        if self.enabled and self.idle_secs > 0:
            self._deadline = time.monotonic() + self.idle_secs
            self._timer.start(self.idle_secs * 1000)
        self.state_changed.emit()

    def clear_remote(self) -> None:
        """Acknowledge the remote-change warning. Does not touch the timer."""
        if not self.remote_notes:
            return
        self.remote_notes.clear()
        self.state_changed.emit()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled:
            self._timer.stop()
            self._deadline = None
        elif self.pending:
            # Switched on with changes already recorded: they are still owed a
            # snapshot, so start the countdown rather than waiting for another
            # edit that may never come.
            self._deadline = time.monotonic() + self.idle_secs
            self._timer.start(self.idle_secs * 1000)
        self.state_changed.emit()

    def set_idle_secs(self, secs: int) -> None:
        self.idle_secs = max(1, int(secs))
        if self.armed:
            self._deadline = time.monotonic() + self.idle_secs
            self._timer.start(self.idle_secs * 1000)
        self.state_changed.emit()

    # -- firing ---------------------------------------------------------

    def _fire(self) -> None:
        self._deadline = None
        if not self.enabled or self.client is None:
            return
        if self.busy:
            # A capture is already running; let it finish and re-arm behind it
            # rather than opening a second console.
            self._deadline = time.monotonic() + 30
            self._timer.start(30_000)
            return
        reason = "idle"
        worker = _ExportWorker(lambda: self._capture(reason), self)
        self._worker = worker
        worker.finished_ok.connect(self._on_done)
        worker.finished_err.connect(self._on_fail)
        worker.finished.connect(self._clear_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.state_changed.emit()

    def _clear_worker(self) -> None:
        self._worker = None
        self.state_changed.emit()

    def _capture(self, reason: str) -> ExportRecord:
        """Blocking: read the export and store it. Runs on a worker thread."""
        result = self.client.export_config(show_sensitive=self.show_sensitive)
        return store(
            result,
            session=self.session,
            reason=reason,
            changes=list(self.pending),
        )

    def _on_done(self, record: object) -> None:
        if not isinstance(record, ExportRecord):  # pragma: no cover - defensive
            return
        self.last_record = record
        self.last_error = ""
        self.pending.clear()
        if record.unchanged:
            applog.note(
                f"config snapshot: unchanged (sha256:{record.digest[:12]}), "
                "nothing stored"
            )
        else:
            applog.note(f"config snapshot stored: {record.path}")
        self.exported.emit(record)
        self.state_changed.emit()

    def _on_fail(self, msg: str) -> None:
        self.last_error = msg
        applog.note(f"config snapshot failed: {msg}")
        # The changes stay pending: the next change re-arms and tries again,
        # and a failed capture must not silently drop what it was for.
        self.failed.emit(msg)
        self.state_changed.emit()

    def flush_now(self, reason: str = "disconnect") -> ExportRecord | None:
        """Take the outstanding snapshot synchronously, if one is owed.

        For the shutdown path, where there is no event loop left to wait on a
        worker. Returns None when nothing was owed or it could not be taken --
        a disconnect must never be what fails.
        """
        self._timer.stop()
        self._deadline = None
        if not self.enabled or self.client is None or not self.pending:
            return None
        try:
            record = self._capture(reason)
        except Exception as e:  # noqa: BLE001
            applog.note(f"final config snapshot failed: {type(e).__name__}: {e}")
            return None
        self.last_record = record
        self.pending.clear()
        applog.note(
            f"final config snapshot: {'unchanged' if record.unchanged else record.path}"
        )
        return record


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


def _last_digest(index: Path) -> str:
    """Digest of the most recent stored capture, or "" if there is none."""
    try:
        with index.open("r", encoding="utf-8") as fh:
            last = ""
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return ""
        return str(json.loads(last).get("digest") or "")
    except (OSError, ValueError):
        return ""


def store(
    result: Any,
    *,
    session: Any,
    reason: str,
    changes: list[ChangeNote] | None = None,
    root: Path | None = None,
) -> ExportRecord:
    """Write one capture into the per-device directory.

    Skipped when the body matches the previous capture: the artifact is
    byte-stable for an unchanged config, so storing it again would only add
    noise to a directory somebody may one day have to read through in a hurry.
    Either way the attempt is appended to ``index.jsonl`` -- knowing a snapshot
    was taken and found identical is itself worth keeping.
    """
    directory = (root or device_dir(session))
    directory.mkdir(parents=True, exist_ok=True)
    # Exports carry every configured value on the router. Not world-readable.
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    index = directory / "index.jsonl"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = result.digest
    unchanged = digest == _last_digest(index)
    notes = list(changes or [])

    path: Path | None = None
    if not unchanged:
        # The digest is in the name, not just the index. Two captures can land
        # in the same second -- Take Snapshot Now twice, or a manual one and the
        # disconnect flush -- and a second-resolution stamp alone would have the
        # later one silently overwrite the earlier. Identical bodies are skipped
        # above, so two files in one second always differ here.
        path = (
            directory
            / f"{stamp}-{_slug(reason, limit=16)}-{digest[:8]}.rsc"
        )
        path.write_text(result.text + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    entry = {
        "ts": stamp,
        "reason": reason,
        "digest": digest,
        "file": path.name if path else None,
        "unchanged": unchanged,
        "bytes": len(result.text),
        "config_lines": len(result.body.splitlines()),
        # Which form produced it: a file captured without secrets cannot
        # restore a device, and six months later nothing else says so.
        "command": getattr(result, "command", ""),
        "changes": [
            {"source": n.source, "where": n.where, "detail": n.detail}
            for n in notes
        ],
        "host": getattr(session, "host", ""),
        "identity": getattr(session, "identity", ""),
        "ros_version": getattr(session, "ros_version", ""),
        "board": getattr(session, "board", ""),
        "disturbed": bool(getattr(result, "disturbed", False)),
        "spliced_logs": list(getattr(result, "spliced_logs", ()) or ()),
    }
    try:
        with index.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        os.chmod(index, 0o600)
    except OSError as e:
        applog.note(f"could not append export index: {e}")

    return ExportRecord(
        path=path,
        digest=digest,
        reason=reason,
        changes=len(notes),
        unchanged=unchanged,
        disturbed=bool(getattr(result, "disturbed", False)),
    )
