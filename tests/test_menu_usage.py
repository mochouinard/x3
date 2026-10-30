"""Offscreen tests for the "most used" group: policy, and what counts as a use.

No device and no pytest required:

    .venv/bin/python test_menu_usage.py

Two things are pinned here. The ranking policy (manual pins first, then the
entries opened at least DEFAULT_MIN_USES times, DEFAULT_TOP_K slots, minus the
"never show at top" list), and the rule that decides when an open is counted at
all: the tally lands only after the window has stayed open for
``USAGE_DWELL_MS``. A menu row is one click away from its neighbour, so a
window opened by mistake and shut again must not climb the list.

Runs against a throwaway XDG_CONFIG_HOME so it never touches real settings.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QElapsedTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from x3.ui import menu_usage
from x3.ui.session import SessionInfo

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


# --- ranking policy -------------------------------------------------------

CANDIDATES = ["a", "b", "c", "d", "e"]
for _ in range(5):
    menu_usage.record_open("a")
for _ in range(3):
    menu_usage.record_open("b")
menu_usage.record_open("c")          # one open only

check("one open is not enough to reach the top",
      menu_usage.most_used(CANDIDATES), ["a", "b"])
menu_usage.record_open("c")
check("two are", menu_usage.most_used(CANDIDATES), ["a", "b", "c"])

for _ in range(9):
    menu_usage.record_open("d")
check("and the group holds DEFAULT_TOP_K, most used first",
      menu_usage.most_used(CANDIDATES), ["d", "a", "b"])

menu_usage.set_excluded("d", True)
check("'never show at top' drops it however used it is",
      menu_usage.most_used(CANDIDATES), ["a", "b", "c"])
menu_usage.set_excluded("d", False)

menu_usage.set_pinned("e", True)     # never opened at all
check("a manual pin comes first and ignores the threshold",
      menu_usage.most_used(CANDIDATES), ["e", "d", "a"])

menu_usage.remove_from_top("e")
check("the X unpins a manual pin", menu_usage.is_pinned("e"), False)
menu_usage.remove_from_top("d")
check("...and resets the count of an auto one", menu_usage.usage_counts().get("d"), None)
check("...which drops it out of the group",
      menu_usage.most_used(CANDIDATES), ["a", "b", "c"])
check("only the top group changed; nothing was excluded",
      menu_usage.excluded_ids(), set())

check("auto off leaves only the manual pins",
      menu_usage.most_used(CANDIDATES, auto=False), [])

print()

# --- what counts as an open ----------------------------------------------


class StubClient:
    """Enough DeviceClient for MainWindow to build against."""

    readonly = True
    schema = None
    session_log = None

    def nav_tree(self):
        return []

    def safe_mode_enter(self):
        return None

    def safe_mode_leave(self, *, keep_changes=True):
        return None


app = QApplication.instance() or QApplication(sys.argv)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.No
)

from x3.ui.widgets import main_window as mw

mw.USAGE_DWELL_MS = 40  # the real 5s, only faster
win = mw.MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=StubClient())


def pump(ms: int) -> None:
    """Let queued timers fire without blocking the event loop."""
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        app.processEvents()


def counted(open_id: str) -> int:
    return int(menu_usage.usage_counts().get(open_id, 0))


# open_menu arms the counter for the kinds that open a window of their own,
# and disarms it again on the way out.
armed: list[object] = []
win._dispatch_menu = lambda payload: armed.append(win._pending_open_id)

win.open_menu({"kind": "map", "id": "p:20,1:Address", "path": (20, 1)})
check("a map open is countable", armed[-1], "p:20,1:Address")
win.open_menu({"kind": "page_group", "id": "group:DHCP Server", "pages": []})
check("so is a tab group", armed[-1], "group:DHCP Server")
win.open_menu({"kind": "terminal", "id": "term"})
check("a terminal is not", armed[-1], None)
check("and the slot is clear once the call is done", win._pending_open_id, None)

del win._dispatch_menu

# A window that sticks around is a use.
win._pending_open_id = "group:Sticks"
kept = QWidget()
kept.show()
win._register("kept", kept)
win._pending_open_id = None
check("nothing is counted at click time", counted("group:Sticks"), 0)
pump(120)
check("...only once the window has been open a while", counted("group:Sticks"), 1)

# A window shut again straight away is a mis-click.
win._pending_open_id = "group:Misclick"
oops = QWidget()
oops.show()
win._register("oops", oops)
win._pending_open_id = None
oops.close()
pump(120)
check("a window closed before the dwell never counts", counted("group:Misclick"), 0)

# Windows opened by anything but the nav (the session log, a property window)
# leave the tally alone.
other = QWidget()
other.show()
win._register("session-log", other)
pump(120)
check("a window opened outside the nav counts nothing",
      sorted(menu_usage.usage_counts()), ["a", "b", "c", "group:Sticks"])

kept.close()
other.close()
win.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all menu-usage tests passed")
