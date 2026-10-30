"""Per-run diagnostic log, written whether or not anyone is watching stderr.

A GUI launched from a desktop file has no terminal, so when it aborts or
freezes there is nothing left to look at afterwards. Everything here exists to
make sure a bad run leaves evidence on disk:

* **faulthandler** -- a C-level traceback on SIGSEGV/SIGABRT/SIGBUS, which is
  what a fatal Qt message (``qFatal``) turns into.
* **excepthooks** -- full tracebacks for the main thread, worker threads, and
  unraisable errors, none of which reach a terminal that isn't there.
* **Qt messages** -- qWarning/qCritical/qFatal, tagged with their category.
* **A freeze watchdog** -- if the event loop stops ticking, dump every thread's
  stack. A hang leaves no trace on its own; this is what turns "it just froze"
  into a stack showing which request it was parked on.
* **SIGUSR1** -- dump all stacks on demand: ``kill -USR1 $(pidof x3)``.

Location, in order of precedence: ``--log PATH``, ``$X3_LOG``, then
``$XDG_STATE_HOME/x3/x3.log`` (``~/.local/state`` by default).
``--log -`` disables the file and leaves stderr alone.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path

#: Rotate at startup once the log passes this, keeping one previous run.
_MAX_BYTES = 2 * 1024 * 1024

#: Kept alive at module scope: faulthandler writes to this fd for the life of
#: the process, so letting it be collected would close the fd underneath it.
_fh = None
_path: Path | None = None
_watchdog_started = False
#: Once stderr is teed into the log, writing to both would duplicate lines.
_teed = False


def _emit(text: str) -> None:
    """Write one line to the log and to stderr, exactly once each."""
    if _fh is not None and not _teed:
        try:
            print(text, file=_fh, flush=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


def default_path() -> Path:
    from x3 import paths

    return paths.state_dir() / "x3.log"


def path() -> Path | None:
    """Where this run is logging, or None if logging to a file is off."""
    return _path


def note(text: str) -> None:
    """Record an application event. Safe to call before or after setup()."""
    _emit(f"[{time.strftime('%H:%M:%S')}] {text}")


def _mark(what: str) -> None:
    if _fh is None:
        return
    print(f"\n===== [{time.strftime('%H:%M:%S')}] {what} =====", file=_fh, flush=True)


def _rotate(p: Path) -> None:
    try:
        if p.exists() and p.stat().st_size > _MAX_BYTES:
            p.replace(p.with_suffix(p.suffix + ".1"))
    except OSError:
        pass


def setup(target: str | os.PathLike | None = None) -> Path | None:
    """Open the log and install the crash hooks. Returns the path, or None.

    Call once, as early in startup as possible -- anything that goes wrong
    before this point can only be reported to a stderr that may not exist.
    """
    global _fh, _path

    if _fh is not None:
        return _path

    raw = str(target) if target is not None else os.environ.get("X3_LOG", "")
    if raw == "-":
        return None
    p = Path(raw).expanduser() if raw else default_path()

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate(p)
        # Append, not truncate: more than one x3 may be running, and a
        # second instance must not wipe the first one's crash.
        _fh = open(p, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"x3: cannot open log {p}: {e}", file=sys.stderr)
        return None

    _path = p
    print(f"\n{'=' * 72}", file=_fh, flush=True)
    note(f"x3 starting — pid {os.getpid()}, python {sys.version.split()[0]}")

    _install_hooks()
    return p


class _Tee:
    """Mirror writes to the log while leaving the original stream working.

    x3ctl prints protocol diagnostics (frame hex dumps on a decode
    failure) to stderr. Launched from a desktop file there is no stderr to
    read, and that dump is exactly what identifies a bad frame -- so keep a
    copy.
    """

    def __init__(self, stream, mirror):
        self._stream = stream
        self._mirror = mirror

    def write(self, data):
        try:
            self._mirror.write(data)
        except Exception:  # noqa: BLE001
            pass
        return self._stream.write(data)

    def flush(self):
        for s in (self._mirror, self._stream):
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_hooks() -> None:
    global _teed

    faulthandler.enable(file=_fh, all_threads=True)
    sys.stderr = _Tee(sys.stderr, _fh)
    _teed = True
    import signal

    if hasattr(signal, "SIGUSR1"):
        # chain=False is load-bearing: chaining falls through to SIGUSR1's
        # default disposition, which kills the process we wanted to inspect.
        faulthandler.register(signal.SIGUSR1, file=_fh, all_threads=True, chain=False)

    def excepthook(exc_type, exc, tb):
        _mark(f"UNHANDLED EXCEPTION in {threading.current_thread().name}")
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

    def threadhook(args):
        name = args.thread.name if args.thread is not None else "?"
        _mark(f"UNHANDLED EXCEPTION in thread {name}")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr
        )

    def unraisablehook(args):
        _mark(f"UNRAISABLE: {args.err_msg or ''} in {args.object!r}")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr
        )

    sys.excepthook = excepthook
    threading.excepthook = threadhook
    sys.unraisablehook = unraisablehook

    _install_qt_handler()


def _install_qt_handler() -> None:
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    levels = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "critical",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def handler(mode, context, message):
        level = levels.get(mode, str(mode))
        where = f" ({context.file}:{context.line})" if context.file else ""
        _emit(f"[qt/{level}]{where} {message}")
        if mode == QtMsgType.QtFatalMsg and _fh is not None:
            # Qt is about to abort(); the Python stacks are the only thing
            # that says which of our threads it was unhappy about.
            _mark("Qt FATAL — Python stacks for every thread follow")
            faulthandler.dump_traceback(file=_fh, all_threads=True)

    qInstallMessageHandler(handler)


def start_watchdog(stall_seconds: float = 5.0) -> None:
    """Dump every thread's stack if the GUI stops responding.

    Needs a QApplication, so call it after one exists. The heartbeat is an
    ordinary timer: if it stops being serviced, the event loop is blocked, and
    whatever is blocking it shows up in the dump.
    """
    global _watchdog_started
    if _fh is None or _watchdog_started:
        return
    _watchdog_started = True

    from PySide6.QtCore import QTimer

    last = [time.monotonic()]
    timer = QTimer()
    timer.timeout.connect(lambda: last.__setitem__(0, time.monotonic()))
    timer.start(500)
    start_watchdog.timer = timer  # keep a reference or it is collected

    def watch():
        stalled = False
        while True:
            time.sleep(1.0)
            age = time.monotonic() - last[0]
            if age > stall_seconds and not stalled:
                stalled = True
                _mark(f"UI FROZEN for {age:.1f}s — all thread stacks follow")
                faulthandler.dump_traceback(file=_fh, all_threads=True)
            elif age <= stall_seconds and stalled:
                stalled = False
                _mark("UI responsive again")

    threading.Thread(target=watch, name="ui-watchdog", daemon=True).start()


def shutdown(rc: int = 0) -> None:
    note(f"x3 exiting — rc={rc}")
