"""Maximizing a child window keeps its own title bar -- and gives it back.

No device and no pytest required:

    .venv/bin/python test_maximize.py

Double-clicking an MDI child's title bar maximized it and then wrecked the
chrome: Qt's default answer to ``SH_Workspace_FillSpaceOnMaximize`` is 0, which
makes ``QMdiSubWindow`` drop its title bar entirely on maximize and re-home the
min/restore/close buttons into the main window's *menu bar* -- Windows-3.1 MDI.
So the title vanished, window buttons appeared among File/Edit/View, anything
floated into the bar (an inline tab strip, QuickSet's mode combo) was left
sitting on top of the window's content, and there was no title bar left to
double-click to come back.

The proxy style answers 1, so the bar stays and the native double-click toggles
both ways. Two gestures Qt does not provide are added on top: dragging a
maximized window pops it back to its old size under the cursor (Qt slides the
full-size window around instead), and the inline tab strip -- which covers the
bar it is drawn in -- answers a double-click itself.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMdiArea,
    QSplitter,
    QStyle,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())


app = QApplication.instance() or QApplication(sys.argv)

from x3.ui import style
from x3.ui.widgets.mdi_window import MdiWindow
from x3.ui.widgets.multi_page_window import MultiPageListWindow

# The proxy style is what carries the fix, and it only goes on for the modern
# skins -- so the theme has to be applied, not defaulted.
style.apply_theme(app, "roomy", persist=False)

PAGES = [
    {"title": n, "path": (20, 0), "type_value": i}
    for i, n in enumerate(("Interface", "Interface List", "Ethernet"))
]


def area():
    """A nav tree beside an MDI area *under a menu bar*, as MainWindow has it.

    The menu bar is the whole point: without one Qt keeps the maximized
    child's title bar anyway (there is nowhere else to put the buttons), so a
    stand-in without it would pass no matter what the style answers.
    """
    main = QMainWindow()
    main.menuBar().addMenu("&File")
    split = QSplitter()
    split.addWidget(QTreeWidget())
    mdi = QMdiArea()
    split.addWidget(mdi)
    main.setCentralWidget(split)
    main.resize(900, 600)
    main.show()
    app.processEvents()
    main.activateWindow()
    app.processEvents()
    return main, mdi


def plain_window(title="Interfaces"):
    w = MdiWindow()
    body = QWidget()
    QVBoxLayout(body).addWidget(QLabel("body"))
    w.setWidget(body)
    w.setWindowTitle(title)
    return w


def mouse(target, typ, local, buttons=Qt.MouseButton.LeftButton):
    app.sendEvent(
        target,
        QMouseEvent(
            typ,
            QPointF(local),
            QPointF(target.mapToGlobal(local)),
            Qt.MouseButton.LeftButton,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        ),
    )


def double_click(target, local):
    """A real double-click, press included.

    QMdiSubWindow only acts on a double-click while its current operation is
    Move, and that is set by the press -- a bare DblClick event does nothing,
    which would make this test pass for the wrong reason.
    """
    none = Qt.MouseButton.NoButton
    mouse(target, QEvent.Type.MouseButtonPress, local)
    mouse(target, QEvent.Type.MouseButtonRelease, local, none)
    mouse(target, QEvent.Type.MouseButtonPress, local)
    mouse(target, QEvent.Type.MouseButtonDblClick, local)
    mouse(target, QEvent.Type.MouseButtonRelease, local, none)
    app.processEvents()


def corner_buttons(main):
    return main.menuBar().cornerWidget(Qt.Corner.TopRightCorner)


# --- the style keeps the bar ------------------------------------------------

ok(
    "the style asks for a title bar on a maximized subwindow",
    bool(app.style().styleHint(QStyle.StyleHint.SH_Workspace_FillSpaceOnMaximize)),
)

main, mdi = area()
w = plain_window()
mdi.addSubWindow(w)
w.resize(640, 320)
w.move(30, 20)
w.show()
app.processEvents()

band = w.title_band_height()
ok("a normal window has a title band", band > 0)
normal = w.geometry()

double_click(w, QPoint(60, band // 2))
ok("double-clicking the title bar maximizes", w.isMaximized())
check("...and the band survives it", w.title_band_height(), band)
check("...so no window buttons move into the menu bar", corner_buttons(main), None)
check("...and the window keeps its title", w.windowTitle(), "Interfaces")

double_click(w, QPoint(60, band // 2))
ok("double-clicking it again restores", not w.isMaximized())
check("...to exactly the geometry it had", w.geometry(), normal)

# --- dragging a maximized window brings it back -----------------------------

w.showMaximized()
app.processEvents()
grab = QPoint(w.width() // 2, band // 2)
grabbed_at = w.mapToGlobal(grab)
mouse(w, QEvent.Type.MouseButtonPress, grab)
mouse(w, QEvent.Type.MouseMove, w.mapFromGlobal(grabbed_at + QPoint(3, 2)))
app.processEvents()
ok("a nudge inside the slop is not a drag", w.isMaximized())

moved = grabbed_at + QPoint(40, 60)
mouse(w, QEvent.Type.MouseMove, w.mapFromGlobal(moved))
app.processEvents()
ok("dragging it off the top restores it", not w.isMaximized())
check("...at its old size", w.size(), normal.size())
under = w.mapFromGlobal(moved)
check("...with the grabbed point still under the cursor (x)",
      round(under.x() / max(1, w.width()), 1), 0.5)
ok("...and the cursor still on the title bar", 0 <= under.y() < band,
   f"y={under.y()} band={band}")

before = w.pos()
further = moved + QPoint(25, 15)
mouse(w, QEvent.Type.MouseMove, w.mapFromGlobal(further))
app.processEvents()
check("...and the same drag keeps moving it", w.pos() - before, QPoint(25, 15))
mouse(w, QEvent.Type.MouseButtonRelease, w.mapFromGlobal(further),
      Qt.MouseButton.NoButton)
ok("releasing leaves it restored", not w.isMaximized())

# A press on the buttons is not a drag handle: min/restore/close still work.
w.showMaximized()
app.processEvents()
ok("the button strip is not a drag zone",
   not w._in_title_drag_zone(QPoint(w.width() - 12, band // 2)))
ok("...and the body is not either", not w._in_title_drag_zone(QPoint(60, band + 20)))
main.close()

# --- the inline tab strip covers the bar, so it answers for it ---------------

main, mdi = area()
pages = MultiPageListWindow("Interfaces", PAGES)
mdi.addSubWindow(pages)
pages.show()
app.processEvents()
pages.set_titlebar_tabs(True)
app.processEvents()
ok("the strip is in the bar", pages.titlebar_tabs_enabled())
strip = pages._title_tabs
band = pages.title_band_height()
normal = pages.geometry()

double_click(strip._name, QPoint(4, band // 2))
ok("double-clicking the strip's name maximizes", pages.isMaximized())
double_click(strip._name, QPoint(4, band // 2))
ok("...and again restores", not pages.isMaximized())
check("...to the geometry it had", pages.geometry(), normal)

pages.showMaximized()
app.processEvents()
start = strip._name.mapToGlobal(QPoint(4, band // 2))
mouse(strip._name, QEvent.Type.MouseButtonPress, QPoint(4, band // 2))
target = start + QPoint(30, 50)
mouse(strip._name, QEvent.Type.MouseMove, strip._name.mapFromGlobal(target))
app.processEvents()
ok("dragging the strip off the top restores the window", not pages.isMaximized())
check("...at its old size", pages.size(), normal.size())
before = pages.pos()
mouse(strip._name, QEvent.Type.MouseMove,
      strip._name.mapFromGlobal(target + QPoint(20, 10)))
app.processEvents()
check("...and the drag carries on from there", pages.pos() - before, QPoint(20, 10))
mouse(strip._name, QEvent.Type.MouseButtonRelease,
      strip._name.mapFromGlobal(target + QPoint(20, 10)), Qt.MouseButton.NoButton)
app.processEvents()
main.close()

# --- a style that does hide the bar must not strand the strip ---------------

main, mdi = area()
pages = MultiPageListWindow("Interfaces", PAGES)
mdi.addSubWindow(pages)
pages.show()
app.processEvents()
pages.set_titlebar_tabs(True)
app.processEvents()
strip = pages._title_tabs
ok("the strip is visible in the bar", strip.isVisible())
# Stand in for a style that drops the bar on maximize (the classic skin keeps
# the platform style, which does): maximized, and no band left to sit in.
pages.showMaximized()
pages.setContentsMargins(0, 0, 0, 0)
pages._reposition_title_widget()
check("a maximized window with no margins has no band", pages.title_band_height(), 0)
ok("with no band to sit in, the strip is parked, not drawn over the body",
   not strip.isVisible())
pages.setContentsMargins(4, 26, 4, 4)
pages._reposition_title_widget()
ok("...and comes back when the bar does", strip.isVisible())
main.close()

if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall maximize tests passed")
