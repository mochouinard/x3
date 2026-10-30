"""Offline tests for tab-strip framing when a long tab group overflows.

No device and no pytest required:

    .venv/bin/python test_tab_framing.py

Interfaces has 13 tabs on a 7.21 device, so the strip always overflows. Picking
one in the middle (VRRP) used to park it against the strip's *right* edge, which
scrolled every following tab out of view -- the strip looked reshuffled. Both
strips now frame the selection the same way: the tab before it leads, then the
selection, then whatever follows. These tests pin that framing for the title-bar
strip and for the regular QTabWidget strip, plus the fact that selecting a tab
never moves the tabs themselves (only the scroll offset).
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QAbstractButton, QApplication, QMdiArea

from x3.ui.widgets import multi_page_window as mpw
from x3.ui.widgets.multi_page_window import MultiPageListWindow
from x3.ui.widgets.title_tab_bar import TitleTabBar

NAMES = [
    "Interface", "Interface List", "Ethernet", "EoIP Tunnel", "IP Tunnel",
    "GRE Tunnel", "VLAN", "VRRP", "Bonding", "LTE",
]

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def check_true(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(f"{label}: {detail}")


# -- title-bar strip --------------------------------------------------------


def strip_visible(strip: TitleTabBar) -> list[str]:
    """Tab names fully inside the strip's viewport, in order."""
    out = []
    for b in strip._tab_btns:
        g = b.geometry()
        if g.left() - strip._offset >= 0 and g.right() - strip._offset <= strip._clip.width():
            out.append(b.text())
    return out


def test_title_strip(app):
    strip = TitleTabBar("Interfaces", NAMES)
    strip.resize(420, 26)
    strip.show()
    app.processEvents()
    check_true(
        "title strip overflows (precondition)",
        strip._max_offset > 0,
        f"max_offset {strip._max_offset}",
    )

    # Tabs must not move when the selection does: only the offset changes.
    strip.set_active(0)
    app.processEvents()
    lefts = [b.geometry().left() for b in strip._tab_btns]
    strip.set_active(5)
    app.processEvents()
    check("selecting a tab does not move the tabs", [b.geometry().left() for b in strip._tab_btns], lefts)

    # Mid-strip: the previous tab leads, the selection follows, and so do
    # tabs after it (the old behaviour scrolled those off).
    strip.set_active(4)
    app.processEvents()
    vis = strip_visible(strip)
    check("mid-strip: previous tab leads", vis[0], NAMES[3])
    check("mid-strip: selection is second", vis[1], NAMES[4])
    check_true("mid-strip: successors still shown", len(vis) > 2, f"visible {vis}")

    # First tab: nothing before it, so the strip sits at its start.
    strip.set_active(0)
    app.processEvents()
    check("first tab: strip at offset 0", strip._offset, 0)
    check("first tab: leads the strip", strip_visible(strip)[0], NAMES[0])

    # Near the end the ideal framing is unreachable; clamp to the strip's end
    # rather than overscrolling, and keep the selection visible.
    strip.set_active(len(NAMES) - 1)
    app.processEvents()
    vis = strip_visible(strip)
    check("last tab: clamped to the end", strip._offset, strip._max_offset)
    check("last tab: selection visible", vis[-1], NAMES[-1])

    # Curating the pinned subset from the ▾ menu rebuilds the row. The fresh
    # buttons are still unpositioned at that point (the layout places them on
    # the next pass), so the framing has to come from their pinned widths.
    strip.set_active(7)
    app.processEvents()
    strip._toggle_pin(1, False)
    strip._toggle_pin(2, False)
    app.processEvents()
    vis = strip_visible(strip)
    check_true("rebuild: selection still visible", NAMES[7] in vis, f"visible {vis}")
    check_true(
        "rebuild: previous tab leads (or the strip is at its end)",
        vis[0] == NAMES[6] or strip._offset == strip._max_offset,
        f"visible {vis}, offset {strip._offset}/{strip._max_offset}",
    )


# -- regular QTabWidget strip ------------------------------------------------


def bar_visible(bar) -> list[str]:
    return [
        bar.tabText(i)
        for i in range(bar.count())
        if bar.tabRect(i).left() >= 0 and bar.tabRect(i).right() <= bar.width()
    ]


def make_window(app, active: int):
    pages = [
        {"title": n, "path": (20, 0), "type_value": i} for i, n in enumerate(NAMES)
    ]
    win = MultiPageListWindow("Interfaces", pages, active_path=(20, 0))
    win.tabs.setCurrentIndex(active)
    area = QMdiArea()
    area.resize(560, 400)
    area.addSubWindow(win)
    win.resize(430, 300)
    area.show()
    win.show()
    app.processEvents()
    app.processEvents()  # the framing pass is deferred to the event loop
    return area, win


def test_regular_strip(app):
    saved = mpw.load_titlebar_tabs
    mpw.load_titlebar_tabs = lambda: False  # this test is about the in-body strip
    try:
        area, win = make_window(app, 7)
        bar = win.tabs.tabBar()
        rb = bar.findChild(QAbstractButton, "ScrollRightButton")
        check_true(
            "regular strip overflows (precondition)",
            rb is not None and rb.isVisible(),  # Qt only shows these on overflow
            f"bar width {bar.width()}, scroll button {rb}",
        )
        vis = bar_visible(bar)
        check_true(
            "opened on a late tab: selection visible with successors",
            NAMES[7] in vis and vis.index(NAMES[7]) < len(vis) - 1,
            f"visible {vis}",
        )

        win.tabs.setCurrentIndex(4)
        app.processEvents()
        vis = bar_visible(bar)
        check("mid-strip: previous tab leads", vis[0], NAMES[3])
        check("mid-strip: selection is second", vis[1], NAMES[4])
        check_true("mid-strip: successors still shown", len(vis) > 2, f"visible {vis}")

        win.tabs.setCurrentIndex(0)
        app.processEvents()
        check("first tab: leads the strip", bar_visible(bar)[0], NAMES[0])

        win.close()
        area.close()
    finally:
        mpw.load_titlebar_tabs = saved


def main() -> int:
    app = QApplication.instance() or QApplication([])
    test_title_strip(app)
    test_regular_strip(app)
    if failures:
        print("\nFAILURES")
        for f in failures:
            print(" -", f)
        return 1
    print("\nall tab-framing tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
