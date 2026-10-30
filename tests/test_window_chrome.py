"""Window chrome: Esc closes, tabs move between the body and the title bar.

No device and no pytest required:

    .venv/bin/python test_window_chrome.py

Three things are pinned.

*Esc* used to be a ``QShortcut`` with ``WidgetWithChildrenShortcut``, which
only fires when the focus widget is the window or inside it. A window opened
from the nav tree does not necessarily take the keyboard with it, so Esc went
to the tree and the window stayed put — reliably for a single list window,
not for a page group. It is a ``keyPressEvent`` now, which reaches the window
by ordinary event propagation from whatever child had focus.

*Tabs in the title bar* was applied once, at construction, so the setting
appeared not to work on anything already on screen — and property forms never
had it at all. It is a live toggle on the shared base now, which means the
strip has to carry the window title too: a form's "· 2 edited" badge is
written with setWindowTitle, and the native title text is off while the strip
owns the bar.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMainWindow,
    QMdiArea,
    QSplitter,
    QTreeWidget,
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

from x3.ui.widgets.list_window import ListWindow
from x3.ui.widgets.mdi_window import MdiWindow
from x3.ui.widgets.multi_page_window import MultiPageListWindow

PAGES = [
    {"title": n, "path": (20, 0), "type_value": i}
    for i, n in enumerate(("Interface", "Ethernet", "VLAN"))
]


def area() -> tuple[QMainWindow, QMdiArea, QTreeWidget]:
    """A nav tree beside an MDI area, laid out the way MainWindow does it.

    The tree matters: it is what holds the keyboard when a window is opened
    from it, and a stand-in in its own top-level window would not -- focus
    cannot leave the active window, so the test would be measuring Qt rather
    than us.
    """
    main = QMainWindow()
    split = QSplitter()
    tree = QTreeWidget()
    mdi = QMdiArea()
    split.addWidget(tree)
    split.addWidget(mdi)
    main.setCentralWidget(split)
    main.resize(900, 600)
    main.show()
    app.processEvents()
    main.activateWindow()
    app.processEvents()
    return main, mdi, tree


def send_escape(target) -> None:
    for typ in (QKeyEvent.Type.KeyPress, QKeyEvent.Type.KeyRelease):
        QApplication.sendEvent(
            target,
            QKeyEvent(typ, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
        )
    app.processEvents()


def gone(w) -> bool:
    return not shiboken6.isValid(w) or not w.isVisible()


# --- Esc closes, whichever child had the key -------------------------------

def esc_trial(label, make, pick):
    main, mdi, _tree = area()
    w = make()
    mdi.addSubWindow(w)
    w.show()
    app.processEvents()
    target = pick(w)
    ok(f"{label}: the key target exists", target is not None)
    if target is None:
        main.close()
        return
    send_escape(target)
    ok(f"{label}: Esc closed the window", gone(w))
    main.close()


esc_trial("single list", lambda: ListWindow("Address", path=(20, 1)),
          lambda w: w.pane.table)
esc_trial("single list, filter box", lambda: ListWindow("Address", path=(20, 1)),
          lambda w: w.pane._filter)
esc_trial("page group", lambda: MultiPageListWindow("Interfaces", PAGES),
          lambda w: w.active_pane().table)
esc_trial("page group, tab bar", lambda: MultiPageListWindow("Interfaces", PAGES),
          lambda w: w.tabs.tabBar())
esc_trial("the window itself", lambda: MultiPageListWindow("Interfaces", PAGES),
          lambda w: w)

# A modifier is somebody else's shortcut, not ours.
main, mdi, _tree = area()
w = MultiPageListWindow("Interfaces", PAGES)
mdi.addSubWindow(w)
w.show()
app.processEvents()
for typ in (QKeyEvent.Type.KeyPress, QKeyEvent.Type.KeyRelease):
    QApplication.sendEvent(w, QKeyEvent(typ, Qt.Key.Key_Escape,
                                        Qt.KeyboardModifier.ShiftModifier))
app.processEvents()
ok("Shift+Esc is not a close", not gone(w))
main.close()


# --- opening a window hands it the keyboard --------------------------------

def focus_trial(label, make, wants):
    """Open a window with the keyboard elsewhere, then claim it."""
    main, mdi, tree = area()
    tree.setFocus()
    app.processEvents()
    ok(f"{label}: the nav tree starts with the keyboard",
       QApplication.focusWidget() is tree,
       f"focus={type(QApplication.focusWidget()).__name__}")
    w = make()
    mdi.addSubWindow(w)
    w.show()
    app.processEvents()
    w.take_focus()
    app.processEvents()
    ok(f"{label}: take_focus activates the subwindow", mdi.activeSubWindow() is w)
    fw = QApplication.focusWidget()
    inside = fw is not None and (fw is w or w.isAncestorOf(fw))
    ok(f"{label}: ...and the keyboard is inside it", inside,
       f"focus={type(fw).__name__ if fw else None}")
    # By class, not by name: the filter box has grown keys of its own
    # (_FilterEdit -- Down walks into the list, Esc empties it), and what
    # matters here is still that the keyboard landed in a box that takes text.
    ok(f"{label}: ...on something that can use it",
       inside and isinstance(fw, wants),
       f"focus={type(fw).__name__ if fw else None}, want a {wants.__name__}")
    # Which is the whole point: Esc now reaches the window it was aimed at.
    if inside:
        send_escape(fw)
        ok(f"{label}: ...so Esc closes it", gone(w))
    main.close()


# Winbox puts you in the filter box: you can type to narrow a list straight
# away. A bare container or the first toolbar button would take the key and
# do nothing with it.
focus_trial("single list", lambda: ListWindow("Address", path=(20, 1)), QLineEdit)
focus_trial("page group", lambda: MultiPageListWindow("Interfaces", PAGES),
            QLineEdit)


# --- tabs move between body and title bar, live ----------------------------

main, mdi, _tree = area()
w = MultiPageListWindow("Interfaces", PAGES)
mdi.addSubWindow(w)
w.show()
app.processEvents()

ok("a window starts with its tabs in the body", not w.titlebar_tabs_enabled())
ok("...and its tab bar visible", w.tabs.tabBar().isVisible())

w.set_titlebar_tabs(True)
app.processEvents()
ok("the setting moves them into the title bar", w.titlebar_tabs_enabled())
ok("...and hides the body tab bar", not w.tabs.tabBar().isVisible())
check("the strip lists the same tabs",
      [b.text() for b in w._title_tabs._tab_btns],
      [w.tabs.tabText(i) for i in range(w.tabs.count())])

# The strip replaces the native title text, so the title has to reach it.
w.setWindowTitle("Interfaces · 2 edited")
check("the strip carries the window title",
      w._title_tabs._name.text(), "Interfaces · 2 edited")
check("...and the native title text is left off", w.windowTitle(), "")

# Selecting through the strip drives the real tab widget, and back.
w._title_tabs.tabSelected.emit(2)
app.processEvents()
check("picking a tab on the strip selects it", w.tabs.currentIndex(), 2)
w.tabs.setCurrentIndex(1)
app.processEvents()
check("...and selecting it elsewhere updates the strip", w._title_tabs._active, 1)

w.set_titlebar_tabs(False)
app.processEvents()
ok("turning it off puts them back", not w.titlebar_tabs_enabled())
ok("...and shows the body tab bar again", w.tabs.tabBar().isVisible())
check("...and gives the title back to the window",
      w.windowTitle(), "Interfaces · 2 edited")

w.set_titlebar_tabs(False)
ok("turning it off twice is a no-op", not w.titlebar_tabs_enabled())
w.set_titlebar_tabs(True)
w.set_titlebar_tabs(True)
ok("...and so is turning it on twice", w.titlebar_tabs_enabled())
main.close()


# --- a window with nothing to move -----------------------------------------

main, mdi, _tree = area()
one = ListWindow("Address", path=(20, 1))
mdi.addSubWindow(one)
one.show()
app.processEvents()
one.set_titlebar_tabs(True)
ok("a single-pane window has no strip to install", not one.titlebar_tabs_enabled())
check("...and keeps its title", one.windowTitle(), "Address")
main.close()

# --- the window's own edge ------------------------------------------------
#
# A frameless skin paints its resize band in the backdrop colour so the band
# disappears, which also left the window with no visible edge: a pale panel
# meeting a slightly-less-pale backdrop. `mdi_border` paints one back.
#
# It is painted rather than declared as a QSS border because QStyleSheetStyle
# derives PM_MdiSubWindowFrameWidth from that rule's box -- a 1px border would
# cut the resize grab band from 4px to 1px. Both halves are checked: the line
# is on the pixels, and the band is untouched.
from dataclasses import replace as _replace

from x3.ui import style

from PySide6.QtGui import QColor as _QColor
from PySide6.QtGui import QImage as _QImage
from PySide6.QtWidgets import QLabel as _QLabel
from PySide6.QtWidgets import QMdiArea as _QMdiArea
from PySide6.QtWidgets import QStyle as _QStyle


def _edge_map(theme):
    style.THEMES[theme.id] = theme
    style.apply_theme(app, theme.id, persist=False)
    host = QMainWindow()
    area = _QMdiArea()
    host.setCentralWidget(area)
    host.resize(400, 300)
    host.show()
    sub = MdiWindow()
    sub.setWidget(_QLabel("content"))
    sub.setWindowTitle("Interface")
    area.addSubWindow(sub)
    sub.setGeometry(40, 40, 240, 150)
    sub.show()
    app.processEvents()
    band = sub.style().pixelMetric(
        _QStyle.PixelMetric.PM_MdiSubWindowFrameWidth, None, sub
    )
    img = _QImage(sub.size(), _QImage.Format.Format_RGB32)
    img.fill(_QColor("magenta"))
    sub.render(img)
    want = _QColor(theme.mdi_border).name() if theme.mdi_border else ""
    w, h = sub.width(), sub.height()
    top = sub.contentsMargins().top()

    def col(x):
        return [y for y in range(h) if want and img.pixelColor(x, y).name() == want]

    def row(y):
        return [x for x in range(w) if want and img.pixelColor(x, y).name() == want]

    out = {
        "band": band,
        "top_margin": top,
        "left": col(band - 1),
        "right": col(w - band),
        "bottom": row(h - band),
        "outer_top_row": row(0),
        "beside_title": row(max(0, top // 2)),
        "size": (w, h),
    }
    host.close()
    style.THEMES.pop(theme.id, None)
    return out


_base = _replace(style.LIGHT, id="_edge_on", mdi_border="#000000")
on = _edge_map(_base)
off = _edge_map(_replace(_base, id="_edge_off", mdi_border=""))

# Three sides only, and starting below the title band: the title strip is
# already a solid block against the backdrop, so ruling it too boxes the bar in.
ok("the left edge runs from the first content row to the bottom",
   on["left"] and on["left"][0] == on["top_margin"]
   and on["left"][-1] == on["size"][1] - on["band"],
   f"left rows {on['left'][:1]}..{on['left'][-1:]} top_margin={on['top_margin']}")
ok("so does the right edge", on["right"] == on["left"], "left and right disagree")
ok("the bottom edge spans between them",
   on["bottom"] and on["bottom"][0] == on["band"] - 1
   and on["bottom"][-1] == on["size"][0] - on["band"],
   f"bottom cols {on['bottom'][:1]}..{on['bottom'][-1:]}")
ok("there is no line across the top", not on["outer_top_row"], f"{on['outer_top_row']}")
ok("and none beside the title bar", not on["beside_title"], f"{on['beside_title']}")

ok("a theme with no mdi_border draws none",
   not (off["left"] or off["right"] or off["bottom"]))

# A hairline means one *device* pixel, and a pen width is in logical units --
# QPen(colour, 1) is two device pixels on a 2x display, which reads as a rule
# rather than a hairline. Rendering into a 2x-ratio image reproduces a HiDPI
# screen without needing QT_SCALE_FACTOR at process start, so the cosmetic pen
# is checked here rather than taken on trust.
def _edge_thickness(ratio):
    style.THEMES[_base.id] = _base
    style.apply_theme(app, _base.id, persist=False)
    host = QMainWindow()
    area = _QMdiArea()
    host.setCentralWidget(area)
    host.resize(400, 300)
    host.show()
    sub = MdiWindow()
    sub.setWidget(_QLabel("content"))
    area.addSubWindow(sub)
    sub.setGeometry(40, 40, 240, 150)
    sub.show()
    app.processEvents()
    img = _QImage(int(240 * ratio), int(150 * ratio), _QImage.Format.Format_RGB32)
    img.setDevicePixelRatio(ratio)
    img.fill(_QColor("magenta"))
    sub.render(img)
    black = _QColor(_base.mdi_border).name()
    y = int(img.height() * 0.7)
    run = [x for x in range(int(20 * ratio)) if img.pixelColor(x, y).name() == black]
    host.close()
    style.THEMES.pop(_base.id, None)
    return len(run)

check("the edge is one device pixel at 1x", _edge_thickness(1.0), 1)
check("...and still one device pixel at 2x", _edge_thickness(2.0), 1)
check("the resize grab band is not shrunk by the line", on["band"], off["band"])
ok("the band is still grabbable at all", on["band"] >= 4, f"band={on['band']}px")
style.apply_theme(app, "light", persist=False)

# The base class is what every window inherits this from.
ok("the shared base owns the behaviour",
   issubclass(ListWindow, MdiWindow) and issubclass(MultiPageListWindow, MdiWindow))

if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall window-chrome tests passed")
