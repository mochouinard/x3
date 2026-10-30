"""Offscreen tests for the toolbar's live CPU / memory / uptime. No device:

    .venv/bin/python test_toolbar_stats.py

The three readings used to be rendered once, at build time, from the connect
reply -- so a session opened yesterday still showed yesterday's load and an
uptime that never moved. They are polled now, and this pins the poll:

  * a reply updates the session and redraws all three labels,
  * one read is out at a time, so a device slower than the tick cannot make
    the window accumulate worker threads,
  * repeated failure gives up instead of knocking on a router that is gone,
  * Settings -> Live Updates turns the poll off and on again.
"""

import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from x3.proto.device import DeviceInfo
from x3.ui.session import SessionInfo

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


class StubClient:
    """Enough DeviceClient for MainWindow, plus a resource read it controls."""

    readonly = True
    schema = None
    session_log = None

    def __init__(self):
        self.info = DeviceInfo(
            cpu_load="3%", free_memory="512 MB", total_memory="1024 MB",
            uptime="1d00:00:01",
        )
        self.reads = 0
        self.fail = False
        #: Set to block inside the read, so a tick lands while one is out.
        self.hold = None
        #: Raised once the worker thread is actually inside the read.
        self.entered = threading.Event()

    def nav_tree(self):
        return []

    def refresh_resources(self):
        self.reads += 1
        self.entered.set()
        if self.hold is not None:
            self.hold.wait(5)
        if self.fail:
            raise RuntimeError("not connected")
        return self.info


app = QApplication.instance() or QApplication(sys.argv)
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.No
)

from x3.ui.widgets.main_window import MainWindow

client = StubClient()
session = SessionInfo(host="10.0.0.1", user="admin")
win = MainWindow(session, client=client)


def labels():
    return {k: lab.text() for k, lab in win._stat_labels.items()}


# --- the labels start from the session, as before ------------------------

check("CPU starts from the connect reading", labels()["cpu"], "CPU: ?")
check("so does uptime", labels()["uptime"], "Uptime: ?")
check("and the poll is armed", win._stats_timer is not None, True)

# --- a reply moves all three --------------------------------------------

win._on_stats_read(client.info)
check("CPU follows the reply", labels()["cpu"], "CPU: 3%")
check("memory renders free / used / total",
      labels()["memory"], "Memory: 512 MB / 512 MB / 1024 MB")
check("uptime follows too", labels()["uptime"], "Uptime: 1d00:00:01")
check("and the session carries the new values", session.cpu_load, "3%")

# A second reply redraws rather than sticking at the first.
client.info.cpu_load = "17%"
client.info.uptime = "1d00:00:04"
win._on_stats_read(client.info)
check("the next reply redraws", labels()["cpu"], "CPU: 17%")
check("...including uptime", labels()["uptime"], "Uptime: 1d00:00:04")

# An empty field is a reply that did not carry the reading, not a new value:
# blanking the label would flicker the toolbar on a sparse build.
client.info.cpu_load = ""
win._on_stats_read(client.info)
check("a missing reading keeps the last one", labels()["cpu"], "CPU: 17%")

# --- only one read is ever out ------------------------------------------

client.hold = threading.Event()
win._poll_stats()
check("a tick starts a read", client.entered.wait(5) and client.reads, 1)
win._poll_stats()
win._poll_stats()
check("further ticks are dropped while it is out", client.reads, 1)
check("one worker, not three", win._stats_worker is not None, True)
client.hold.set()
win._stats_worker.wait(2000)
client.hold = None
app.processEvents()
check("and the slot frees when it returns", win._stats_worker, None)

# --- a minimised window costs nothing -----------------------------------

before = client.reads
win.setWindowState(win.windowState() | win.windowState().WindowMinimized)
win._poll_stats()
check("a minimised window does not poll", client.reads, before)
win.setWindowState(win.windowState() & ~win.windowState().WindowMinimized)

# --- failure gives up rather than hammering -----------------------------

win._stats_errors = 0
win._on_stats_error("boom")
check("one failure keeps the poll", win._stats_timer is not None, True)
win._on_stats_error("boom")
win._on_stats_error("boom")
check("three in a row stop it", win._stats_timer, None)
check("and the last good reading stays on screen", labels()["cpu"], "CPU: 17%")

# --- Settings -> Live Updates governs it --------------------------------

win._on_live_updates_toggled(True)
check("re-enabling live updates re-arms the poll", win._stats_timer is not None, True)
check("...with the error count cleared", win._stats_errors, 0)
win._on_live_updates_toggled(False)
check("turning live updates off stops it", win._stats_timer, None)
win._on_live_updates_toggled(True)

win.close()
check("closing stops the poll", win._stats_timer, None)

# --- the client side: one quiet GET, four fields, uptime cache -----------

from x3.proto.device import DeviceClient, RowObject


class FakeClient(DeviceClient):
    """A client whose only device is the resource row below."""

    def __init__(self):
        super().__init__("10.0.0.1", "admin", "")
        self.asked: list[tuple] = []

    def get(self, path, *, quiet=False):
        self.asked.append((tuple(path), quiet))
        return RowObject(
            path=tuple(path),
            raw=None,
            fields=[
                (0x16, "Version", "7.20.2", True),
                (0x02, "CPU Load", "9%", True),
                (0x04, "Free Memory", "700 MB", True),
                (0x05, "Total Memory", "1024 MB", True),
                (0x01, "Uptime", "2d03:00:00", True),
            ],
            values_by_code={0x01: 183600},
        )


fake = FakeClient()
info = fake.refresh_resources()
check("it reads /system/resource, quietly",
      fake.asked, [(DeviceClient.RESOURCE_PATH, True)])
check("CPU load lands on info", info.cpu_load, "9%")
check("free memory too", info.free_memory, "700 MB")
check("and total", info.total_memory, "1024 MB")
check("and uptime", info.uptime, "2d03:00:00")
check("it updates the client's own info", fake.info is info, True)
# type:'age' columns are stamps on this clock, and the reply already carried
# it -- so the poll keeps it fresh for free.
check("the uptime clock is refreshed from the same reply",
      fake.uptime(refresh=False), 183600.0)
check("...and the version it also saw is left alone", info.version, "")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all toolbar stats tests passed")
