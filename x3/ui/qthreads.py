"""Joining Qt worker threads before Qt gets a chance to destroy them.

Every worker in this app blocks on a device request, so at shutdown one is
usually parked in :mod:`x3.proto.demux` waiting for a reply that is no longer
coming. Destroying a QThread that is still running is a *fatal* abort --

    QThread: Destroyed while thread '' is still running

-- which is how the app died when Cancel was clicked during a connect, and
again when the main window was closed while a list pane was still loading.

Closing the DeviceClient is what makes joining cheap: it stops the demux
reader, which fails every pending waiter at once, so the blocked threads
unwind in milliseconds. The shutdown order is therefore always *close the
client, then retire the threads* -- doing it the other way round means waiting
out the full per-request timeout instead.
"""

from __future__ import annotations

import warnings

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

#: Workers that outlived their parent widget. Holding a reference is what
#: keeps Qt from destroying a thread that is still running; entries drop out
#: again when the thread finishes.
_ORPHANS: set = set()

#: Signals a worker may expose. They are dropped before the join so a late
#: result cannot land in a widget that is being torn down.
_WORKER_SIGNALS = ("finished_ok", "finished_err", "status", "progress", "finished")


def retire(worker, *, wait_ms: int = 4000, cancel=None) -> bool:
    """Detach *worker*'s signals and join it. True if it really finished.

    ``cancel`` is an optional callable that unblocks the worker (typically
    closing the client it is waiting on). A worker that still will not join is
    re-parented and kept alive here rather than destroyed -- leaking a thread
    is recoverable, aborting the process is not.
    """
    if worker is None:
        return True
    try:
        if not worker.isRunning():
            return True
    except RuntimeError:  # already deleted by Qt
        return True

    # Disconnecting a signal nobody connected is fine here, but PySide warns
    # about it rather than raising, which would litter the log on every close.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for name in _WORKER_SIGNALS:
            sig = getattr(worker, name, None)
            if sig is None:
                continue
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass  # nothing was connected

    if cancel is not None:
        try:
            cancel()
        except Exception:  # noqa: BLE001
            pass

    if worker.wait(wait_ms):
        return True

    try:
        worker.setParent(None)
    except (RuntimeError, TypeError):
        pass
    _ORPHANS.add(worker)
    try:
        worker.finished.connect(lambda w=worker: _ORPHANS.discard(w))
    except (RuntimeError, TypeError):
        pass
    return False


def retire_all(root=None, *, wait_ms: int = 4000) -> int:
    """Retire every running QThread under *root*, or under every window.

    Returns how many refused to join, which is also how many were orphaned.
    """
    roots = []
    if root is not None:
        roots.append(root)
    else:
        app = QApplication.instance()
        if app is not None:
            roots.extend(app.topLevelWidgets())

    seen: set[int] = set()
    stubborn = 0
    for r in roots:
        try:
            children = r.findChildren(QThread)
        except RuntimeError:  # widget already gone
            continue
        for t in children:
            if id(t) in seen:
                continue
            seen.add(id(t))
            if not retire(t, wait_ms=wait_ms):
                stubborn += 1
    return stubborn


def still_running() -> int:
    """How many retired workers are still blocked -- a connect, say, inside a
    TCP SYN to a host that never answers, which no client close can reach."""
    alive = 0
    for t in list(_ORPHANS):
        try:
            alive += bool(t.isRunning())
        except RuntimeError:  # already deleted by Qt
            pass
    return alive
