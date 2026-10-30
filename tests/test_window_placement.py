"""Where a new child window opens: the middle of the main window.

No device and no pytest required:

    .venv/bin/python test_window_placement.py

Qt's MDI placer fills its area from the top-left corner -- the first window
lands in the corner, and each later one steps towards the bottom edge, away
from where the user is looking. Winbox opens a window where the eye already
is, so every window this app opens goes through
``MainWindow._show_child`` and comes up centred.

Three things are pinned:

  * a window opens in the middle of the area, not in its corner;
  * a second window the same size as the first steps down-right instead of
    hiding it completely -- two windows centred on the same pixel means the
    older one is simply gone;
  * a caller that placed its window first keeps that position. ``move()``
    before the add is the "I know where this goes" signal, and it has to be
    replayed after ``addSubWindow`` because the placer overrides a position
    set before the window has an area.

And that nothing opens a window around the back: the only ``addSubWindow``
call in the app is the one inside the helper.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from x3.ui.session import SessionInfo

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if not cond:
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())
    else:
        print(f"ok  {label}")


class StubClient:
    """Enough DeviceClient for MainWindow to build against."""

    readonly = True
    schema = None
    session_log = None

    def nav_tree(self):
        return []


app = QApplication.instance() or QApplication(sys.argv)
# Nothing modal may block the run.
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)

from x3.ui.widgets.main_window import _STACK_STEP, MainWindow
from x3.ui.widgets.mdi_window import MdiWindow

main = MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=StubClient())
main.resize(1000, 700)
main.show()
app.processEvents()


def child(w, h, at=None):
    win = MdiWindow()
    win.setWidget(QLabel("body"))
    win.resize(w, h)
    if at is not None:
        win.move(at)
    main._show_child(win)
    app.processEvents()
    return win


def centre_of(size):
    area = main.mdi.viewport().rect()
    return QPoint(
        max(0, (area.width() - size.width()) // 2),
        max(0, (area.height() - size.height()) // 2),
    )


# --- the middle, not the corner -----------------------------------------

first = child(400, 300)
check("the first window opens centred", first.pos(), centre_of(first.size()))
ok("...which is not the corner Qt would have used", first.pos() != QPoint(0, 0))

# --- a second window of the same size must not hide the first ------------

second = child(400, 300)
check("a window landing on an occupied spot steps down-right",
      second.pos(), first.pos() + QPoint(_STACK_STEP, _STACK_STEP))

third = child(400, 300)
check("...and the next one steps again",
      third.pos(), first.pos() + QPoint(2 * _STACK_STEP, 2 * _STACK_STEP))

# A different size centres somewhere else, so it collides with nothing and
# takes the middle even with three windows already open.
other = child(600, 420)
check("a differently sized window still opens centred",
      other.pos(), centre_of(other.size()))

# --- a caller that knows where its window goes keeps it ------------------

placed = child(300, 200, at=QPoint(37, 41))
check("a window moved before the add keeps its place", placed.pos(), QPoint(37, 41))

# --- a window too big for the area starts at the corner, not off it ------

huge = child(4000, 3000)
check("an oversized window is not pushed off the top-left", huge.pos(), QPoint(0, 0))

# --- nothing opens a window around the back ------------------------------

# Every `addSubWindow` in the app must be the one inside the helper: a window
# added anywhere else is a window that opens in the corner again.
adds = []
for path in sorted(Path("x3").rglob("*.py")):
    enclosing = "<module>"
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("def "):
            enclosing = line.split("def ", 1)[1].split("(", 1)[0]
        if ".addSubWindow(" in line:
            adds.append(f"{path}:{n} in {enclosing}()")

check("one place in the app adds a window to the area", len(adds), 1)
ok("...and it is the centring helper", bool(adds) and "_show_child()" in adds[0], adds)

for w in list(main.mdi.subWindowList()):
    w.close()
main.close()
app.processEvents()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all window placement tests passed")
