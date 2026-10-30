"""A dropped connection: said out loud, reconnected, and the windows carry on.

No device and no pytest required:

    .venv/bin/python test_reconnect.py

A 7.25 router stopped answering mid-session (15:26), reset the connection two
minutes later, and the app never noticed: DHCP Leases popped "timeout talking
to device", a background poll knocked on the dead socket for six hours, and
nothing said the session was over. Now the window watches the connection,
shows "Disconnected — reconnecting…", puts a new session under the same
client (DeviceClient.reconnect, checked live on a 7.23.3 virtual router), and has the
open lists read again. Safe Mode does not survive a drop -- the router unrolls
it -- so the window stops claiming it is on, and says what happened.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-reconnect-")

from PySide6.QtWidgets import QApplication, QMessageBox

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
warned: list[str] = []
QMessageBox.warning = staticmethod(lambda _p, title, text, *a, **k: warned.append(title))

from x3.ui.session import SessionInfo  # noqa: E402
from x3.ui.widgets import main_window as mw  # noqa: E402
from x3.ui.widgets.list_pane import ListPane  # noqa: E402


class _State:
    def __init__(self, active):
        self.active_here = active


class DroppingClient:
    """A client whose link can be cut, and whose reconnect fails ``refuse`` times."""

    readonly = True
    schema = None
    session_log = None

    def __init__(self):
        self.lost: BaseException | None = None
        self.refuse = 0
        self.reconnects = 0

    def nav_tree(self):
        return []

    def close(self):
        pass

    def safe_mode_enter(self):
        return _State(True)

    def connection_lost(self):
        return self.lost

    def reconnect(self, progress=None):
        self.reconnects += 1
        if self.refuse > 0:
            self.refuse -= 1
            raise OSError("No route to host")
        self.lost = None


def settle(ms: int = 100):
    """Let the reconnect worker run and its signals land."""
    import time

    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    w = win._reconnect_worker
    if w is not None:
        w.wait(2000)
    app.processEvents()
    app.processEvents()


client = DroppingClient()
win = mw.MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=client)
win.show()

check("an up connection shows nothing", win._link_label.isVisible(), False)
win._check_link()
check("...and starts no reconnect", client.reconnects, 0)

reloads: list[str] = []
pane = ListPane("Leases", path=(23, 1))
pane.resume_after_reconnect = lambda: reloads.append("Leases")
win.mdi.addSubWindow(pane).show()

# Safe Mode is armed when the link drops.
win._safe_act.trigger()
w = win._safe_worker
if w is not None:
    w.wait(2000)
app.processEvents()
check("Safe Mode armed before the drop", win._safe_mode_on, True)

client.lost = ConnectionResetError(104, "Connection reset by peer")
client.refuse = 1
win._check_link()
check("a dropped connection says so in the window",
      (win._link_label.isVisible(), win._link_label.text()),
      (True, "Disconnected — reconnecting…"))
check("...and why, on hover", "Connection reset by peer" in win._link_label.toolTip(), True)
check("Safe Mode is not claimed once the router has unrolled it", win._safe_mode_on, False)

check("...with a reconnect on its way", win._reconnect_timer.isActive(), True)


def try_now():
    """Fire the pending attempt now rather than wait the backoff out."""
    win._reconnect_timer.stop()
    win._try_reconnect()
    settle()


try_now()
check("a refused reconnect is tried again later",
      (client.reconnects, win._link_label.text().startswith("Disconnected — retrying in")),
      (1, True))
check("...not straight away", win._reconnect_timer.isActive(), True)
try_now()
check("the next try gets through", client.reconnects, 2)
check("...the notice goes", win._link_label.isVisible(), False)
check("...the open lists are read again", reloads, ["Leases"])
check("...and the Safe Mode loss is told, once", warned, ["Safe Mode"])

win._check_link()
settle()
check("a healthy link after that starts nothing more", client.reconnects, 2)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all reconnect tests passed")
