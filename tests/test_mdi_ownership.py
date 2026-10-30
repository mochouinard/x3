"""The MDI area has to stay Python's to own, or the process dies on exit.

No device and no pytest required:

    .venv/bin/python test_mdi_ownership.py

``QMdiSubWindow.mdiArea()`` hands the area's ownership from Python to C++ the
moment it is called. An area that Python built and holds by name alone then
belongs to nobody, and the interpreter segfaults collecting it during
finalisation -- "Fatal Python error: Segmentation fault ... Garbage-collecting",
with no Python frame to point at. It cost a morning to find because it lands
*after* the last line of the script: every check in a test file passes, the
summary prints, and the run still exits 139.

Reproducing it needs none of this code:

    area = QMdiArea(); sub = QMdiSubWindow(); area.addSubWindow(sub)
    Shiboken.ownedByPython(area)   # True
    sub.mdiArea()
    Shiboken.ownedByPython(area)   # False  -> segfault at exit

So MdiWindow does not call it: :func:`mdi_area_of` walks the parent chain,
which answers the same question and touches no ownership. What is pinned here
is that invariant -- the area is still Python's after a window has been built,
shown and activated in it -- and that nothing has quietly gone back to calling
``mdiArea()``.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QLabel, QMdiArea
from shiboken6 import Shiboken

from x3.ui import style
from x3.ui.widgets.mdi_window import MdiWindow, mdi_area_of

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


app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "dark", persist=False)

area = QMdiArea()
area.resize(560, 400)
ok("a freshly built area is Python's", Shiboken.ownedByPython(area))

win = MdiWindow()
win.setWindowTitle("Test")
win.setWidget(QLabel("hi"))
area.addSubWindow(win)
area.show()
win.show()
app.processEvents()

# show() alone used to be enough to lose it: showEvent asked for the area.
ok("...and still Python's once a window is shown in it",
   Shiboken.ownedByPython(area))

win.take_focus()
area.setActiveSubWindow(win)
app.processEvents()
ok("...and after activation, which asks again",
   Shiboken.ownedByPython(area))

check("the parent chain finds the same area", mdi_area_of(win) is area, True)
check("a window with no area at all answers None", mdi_area_of(MdiWindow()), None)

# The window still works out what it was asking for. Offscreen, the top-level
# is not "active", so the answer is False -- what matters is that it is the
# answer the two halves of the question add up to, read off the same area.
check("the area agrees which window is active", area.activeSubWindow() is win, True)
check("active means: the area's window, in an active top-level",
      win._is_native_active(),
      area.activeSubWindow() is win and win.window().isActiveWindow())

# Nothing may go back to the binding that started this.
BAD = re.compile(r"(?<!`)\b\w+\.mdiArea\(\)(?!`)")
offenders = []
for path in sorted(Path("x3").rglob("*.py")):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if BAD.search(line):
            offenders.append(f"{path}:{n}")
check("nothing calls mdiArea() any more", offenders, [])

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all MDI ownership tests passed")
