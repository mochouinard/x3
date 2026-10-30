"""Cancelling a connect that is still in flight must not abort the process.

No device and no pytest required:

    .venv/bin/python test_connect_cancel.py

The bug this pins down: clicking Connect and then Cancel while the router had
not answered yet killed the app with

    QThread: Destroyed while thread '' is still running

Two things were wrong. Cancel/Esc reach QDialog.reject(), which goes through
done() and never emits a close event -- so the worker teardown that lived in
closeEvent did not run at all. And that teardown only waited 200ms for a
worker blocked on a 12s request, so even the window-close path returned with
the thread still running. Either way Qt destroyed a live QThread and aborted.

The fix is to cancel rather than wait it out: closing the client stops the
demux reader, which fails the pending waiter immediately, so the worker
returns and the join is instant. These tests drive both dismissal paths with a
client that blocks exactly like a silent router.
"""

import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

import x3.proto.device
from PySide6.QtWidgets import QApplication
from x3.ui.qthreads import _ORPHANS
from x3.ui.widgets.connect_dialog import ConnectDialog, _ConnectWorker

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


app = QApplication.instance() or QApplication(sys.argv)


class BlockingClient:
    """Stands in for a DeviceClient whose router never answers.

    connect() parks the way demux.Pending.result() does, and close() releases
    it with an exception -- which is what demux._fail_all does to every
    pending waiter when the reader stops.
    """

    last = None
    created = threading.Event()

    def __init__(self, *a, **k):
        self.closed = False
        self.entered = threading.Event()
        self._released = threading.Event()
        BlockingClient.last = self
        BlockingClient.created.set()

    def connect(self, progress=None):
        if progress is not None:
            progress("Authenticating…")
        self.entered.set()
        if not self._released.wait(20):  # pragma: no cover - hang guard
            raise AssertionError("connect() was never cancelled")
        raise OSError("demux stopped")

    def close(self):
        self.closed = True
        self._released.set()


x3.proto.device.DeviceClient = BlockingClient


def make_worker(dialog):
    BlockingClient.last = None
    BlockingClient.created.clear()
    worker = _ConnectWorker(
        host="192.0.2.1", user="admin", password="pw", port=8291,
        transport="tcp", schema_dir=None, parent=dialog,
    )
    # The dialog wires all three before start(); the teardown drops them
    # again, so leaving them unconnected here would only test a state the
    # app never reaches.
    worker.finished_ok.connect(lambda _c: None)
    worker.finished_err.connect(lambda _e: None)
    worker.status.connect(lambda _s: None)
    return worker


def start_worker(dialog):
    """Start a worker and return once it is genuinely parked in connect()."""
    worker = make_worker(dialog)
    dialog._connect_worker = worker
    worker.start()
    ok("worker built its client", BlockingClient.created.wait(5))
    ok("worker reached connect()", BlockingClient.last.entered.wait(5))
    return worker


# --- Cancel / Esc ---------------------------------------------------------

d = ConnectDialog()
w = start_worker(d)
ok("worker is running before Cancel", w.isRunning())

d.reject()

check("Cancel joins the worker", w.isRunning(), False)
ok("Cancel closed the in-flight client", BlockingClient.last.closed)
check("no worker is left on the dialog", d._connect_worker, None)
check("nothing had to be orphaned", len(_ORPHANS), 0)
d.close()

# --- window close ---------------------------------------------------------

d = ConnectDialog()
w = start_worker(d)
d.close()

check("closing the window joins it too", w.isRunning(), False)
ok("...and closed the client", BlockingClient.last.closed)

# --- cancel racing the client's construction ------------------------------

d = ConnectDialog()
worker = make_worker(d)
worker.cancel()          # cancel lands before run() publishes the client
worker.start()
check("a worker cancelled before it starts still exits", worker.wait(5000), True)
ok("...without ever entering connect()",
   BlockingClient.last is None or not BlockingClient.last.entered.is_set())
d.close()

# --- the phase labels the dialog shows ------------------------------------

d = ConnectDialog()
seen: list[str] = []
w = make_worker(d)
d._connect_worker = w
w.status.connect(seen.append)          # connected before start, or we miss them
w.start()
BlockingClient.created.wait(5)
BlockingClient.last.entered.wait(5)
BlockingClient.last.close()            # let run() finish
w.wait(5000)
app.processEvents()
ok("the dialog is told which phase connect() is in", "Authenticating…" in seen)
ok("...and which host it is dialling",
   any(x.startswith("Connecting to 192.0.2.1") for x in seen))
d.close()


# --- cancelling before there is a session at all --------------------------
#
# The first fix only reached a client that already had a session to close. But
# DeviceClient.connect() blocks *inside* the transport: the MAC one walks every
# local interface waiting `timeout` on each, and self.session is not set until
# authentication succeeds. So a cancel had nothing to close, the worker ran on
# for up to timeout x interfaces, and Qt destroyed it -- aborting the process
# with the worker still parked in macwinbox._try_iface.

class ConnectingClient:
    """Blocks in connect() the way a transport does, with nothing to close yet."""

    last = None
    created = threading.Event()

    def __init__(self, *a, **k):
        self.entered = threading.Event()
        self._released = threading.Event()
        self.aborted = False
        self.closed = False
        ConnectingClient.last = self
        ConnectingClient.created.set()

    def connect(self, progress=None):
        self.entered.set()
        if not self._released.wait(20):  # pragma: no cover - hang guard
            raise AssertionError("connect() was never aborted")
        raise OSError("connect cancelled")

    def abort(self):
        # What DeviceClient.abort does: reach the session being opened.
        self.aborted = True
        self._released.set()

    def close(self):
        self.closed = True
        self._released.set()


x3.proto.device.DeviceClient = ConnectingClient

d = ConnectDialog()
w = make_worker(d)
d._connect_worker = w
w.start()
ok("the worker built its client", ConnectingClient.created.wait(5))
ok("the worker is blocked inside connect()",
   ConnectingClient.last.entered.wait(5))
d.reject()
check("Cancel joins it", w.isRunning(), False)
ok("...by aborting the connect, not just closing a session",
   ConnectingClient.last.aborted)
check("nothing had to be orphaned", len(_ORPHANS), 0)
d.close()


# --- Cancel during an attempt returns to the form, it does not quit -------
#
# app.py has nothing to show but this dialog, so dismissing it ends the
# application. "Cancel" while a connect is in flight has to mean "stop trying
# to reach this router", not "quit" -- otherwise a mistyped address costs you
# the whole session.

x3.proto.device.DeviceClient = BlockingClient

d = ConnectDialog()
closed: list[int] = []
d.finished.connect(closed.append)
w = start_worker(d)

d.reject()
check("the attempt is cancelled", w.isRunning(), False)
check("...and the client closed", BlockingClient.last.closed, True)
check("but the dialog stays open", closed, [])
check("...and says what happened", d.status.text(), "Connect cancelled.")
ok("...with the form usable again", d._btn_connect.isEnabled())
ok("...and the host field editable", d.host.isEnabled())

# A second Cancel, with nothing in flight, does dismiss it.
d.reject()
check("cancelling an idle dialog dismisses it", len(closed), 1)
d.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all connect-cancel tests passed")
