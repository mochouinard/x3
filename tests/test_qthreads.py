"""Closing a window while a worker is mid-request must not abort the process.

No device and no pytest required:

    .venv/bin/python test_qthreads.py

Same failure as test_connect_cancel, reached from the other direction: close
the main window while a list pane is still loading and Qt destroys the running
_FetchWorker --

    QThread: Destroyed while thread '' is still running

Nothing joined those workers. The pane parented one to itself (list_pane.py)
and never waited for it, and MainWindow.closeEvent closed the client but went
straight on to teardown. Order is the whole fix: closing the client fails
every pending demux waiter, so the blocked threads unwind at once and the join
is instant -- but the join has to actually happen.

Also covers the log file, which is what makes any of this visible after a GUI
dies with no terminal attached.
"""

import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_tmp = tempfile.mkdtemp(prefix="x3-test-")
os.environ["XDG_CONFIG_HOME"] = _tmp
os.environ["XDG_STATE_HOME"] = _tmp

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QWidget

from x3.ui import applog, qthreads

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


class Blocked(QThread):
    """A worker parked on a reply, the way _FetchWorker sits in demux."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entered = threading.Event()
        self.released = threading.Event()

    def run(self):
        self.entered.set()
        self.released.wait(20)


def blocked_under(parent):
    w = Blocked(parent)
    w.start()
    ok("worker is blocked", w.entered.wait(5))
    return w


# --- a worker that can be unblocked (client.close equivalent) --------------

host = QWidget()
w = blocked_under(host)
joined = qthreads.retire(w, wait_ms=4000, cancel=w.released.set)
ok("retire() joins a worker its cancel can release", joined)
check("...and nothing is orphaned", len(qthreads._ORPHANS), 0)

# --- a worker that will not let go -----------------------------------------

w2 = blocked_under(host)
joined = qthreads.retire(w2, wait_ms=100)
check("retire() reports a worker it could not join", joined, False)
ok("...and keeps it alive rather than destroying it", w2 in qthreads._ORPHANS)
check("...detached from its parent", w2.parent(), None)
w2.released.set()
w2.wait(5000)
app.processEvents()
ok("...and drops it once it finishes", w2 not in qthreads._ORPHANS)

# --- the sweep that MainWindow.closeEvent and app.py run ------------------

host2 = QWidget()
kids = [blocked_under(host2) for _ in range(3)]
nested = blocked_under(QWidget(host2))          # deeper in the widget tree
for k in kids + [nested]:
    k.released.set()                            # what client.close() amounts to
stubborn = qthreads.retire_all(host2, wait_ms=4000)
check("retire_all() sweeps the whole widget tree", stubborn, 0)
ok("...leaving nothing running", not any(k.isRunning() for k in kids + [nested]))

# --- the log file ---------------------------------------------------------

p = applog.setup()
ok("a log file is created without being asked", p is not None and p.exists())
check("...under XDG_STATE_HOME", str(p).startswith(_tmp), True)
applog.note("hello from the test")
ok("...and application events land in it", "hello from the test" in p.read_text())
ok("...with the pid, so two instances can be told apart",
   str(os.getpid()) in p.read_text())

check("setup() is idempotent", applog.setup(), p)
check("default_path() follows XDG_STATE_HOME",
      applog.default_path(), p)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all qthreads/applog tests passed")
