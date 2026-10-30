"""Ctrl+= / Ctrl+- : the whole window at another size.

    .venv/bin/python test_ui_scale.py

A theme is a set of colours *and* a set of sizes -- 11px type in an 18px row
with a 16px icon and a 168px nav strip. Zoom multiplies the second set by one
factor and leaves the first alone, which is why it is not a theme and not a
font: it is this skin, seen from closer up.

Five things are pinned here, and each of them was a way for a zoom to look
half-done.

*Everything moves together.* Type, row height, icon edge, pads, the nav strip.
A zoom that grew the type and left the rows behind would just clip.

*Including the stylesheet's own numbers.* Most of the lengths in those six
hundred lines are not theme metrics -- a scrollbar's 8px width, a combo arrow's
16px box, a button's 64px minimum. The sheet is built from the unscaled theme
and scaled afterwards, so those come along; see ``style.scale_css``.

*And the MDI title strip.* Fusion answers ``PM_TitleBarHeight`` with a
constant, so a child window kept a 28px strip above 36px rows until the classic
path got a proxy of its own.

*Every stop actually moves.* The rounding is half-away-from-zero rather than
Python's round-half-to-even, which made 7.5pt and 8.5pt both 8 and cost a stop.

*Nothing but sizes moves.* No colour, no flag: the skin is the skin.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="x3-scale-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")

import re  # noqa: E402
from dataclasses import fields  # noqa: E402

from PySide6.QtGui import QFontInfo, QKeySequence  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMainWindow,
    QMdiArea,
    QStyle,
    QWidget,
)

from x3.ui import style  # noqa: E402
from x3.ui.settings import load_settings, load_ui_scale, save_ui_scale  # noqa: E402

_FAILS: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    print(("ok  " if cond else "FAIL ") + label + (f"  {detail}" if not cond and detail else ""))
    if not cond:
        _FAILS.append(f"{label} {detail}".strip())


def eq(label: str, got, want) -> None:
    ok(label, got == want, f"got {got!r}, want {want!r}")


# --- the ladder -----------------------------------------------------------


def test_ladder() -> None:
    print("\n[ladder] the stops, and how a step walks them")
    steps = style.SCALE_STEPS
    ok("1.0 is a stop, so 'normal size' is not a special case", 1.0 in steps)
    ok("the ladder only climbs", list(steps) == sorted(steps))
    eq("out from 1.0", style.step_scale(-1, 1.0), 0.85)
    eq("in from 1.0", style.step_scale(1, 1.0), 1.15)
    eq("the top holds rather than wraps", style.step_scale(1, steps[-1]), steps[-1])
    eq("and so does the bottom", style.step_scale(-1, steps[0]), steps[0])
    # A factor between two stops -- an older build's settings file, or a hand
    # edit. It snaps to the ladder, and the step still has to move.
    eq("in from 1.05 is the stop above it", style.step_scale(1, 1.05), 1.15)
    eq("out from 1.05 is the stop below it", style.step_scale(-1, 1.05), 1.0)
    eq("out of range clamps in", style.clamp_scale(99), steps[-1])
    eq("and so does under it", style.clamp_scale(0.01), steps[0])
    eq("junk is no zoom at all", style.clamp_scale("wide"), 1.0)


def test_every_stop_moves() -> None:
    print("\n[stops] no stop is a no-op, for any skin")
    for theme in style.list_themes():
        seen_font: list[int] = []
        seen_row: list[int] = []
        for s in style.SCALE_STEPS:
            t = style.scale_theme(theme, s)
            seen_font.append(t.font_px or t.font_pt)
            seen_row.append(t.row_height)
        stuck = [
            f"font {a}" for a, b in zip(seen_font, seen_font[1:]) if a == b
        ] + [
            f"row {a}" for a, b in zip(seen_row, seen_row[1:]) if a == b
        ]
        ok(f"{theme.id}: every stop changes the type and the rows",
           not stuck, f"stuck at {stuck}")


def test_sizes_only() -> None:
    print("\n[scale_theme] sizes move, nothing else does")
    base = style.get_theme("classic")
    big = style.scale_theme(base, 2.0)
    same = [
        f.name for f in fields(base)
        if isinstance(getattr(base, f.name), (str, bool))
        and getattr(base, f.name) != getattr(big, f.name)
    ]
    ok("no colour, no family, no flag", not same, f"changed: {same}")
    eq("the id is the same theme", big.id, base.id)
    eq("11px type doubles", big.font_px, 22)
    eq("an 18px row doubles", big.row_height, 36)
    eq("the 16px icon edge doubles", big.icon_px, 32)
    eq("the nav strip doubles", big.tree_width, base.tree_width * 2)
    eq("scale 1.0 hands back the very same object", style.scale_theme(base, 1.0), base)

    # -1 means "line up with the cells" and 0 means "whatever the rows imply".
    # Multiply either and the sentinel becomes a length.
    eq("header_pad's -1 sentinel survives", big.header_pad, base.header_pad)
    eq("header_height's 0 sentinel survives", big.header_height, 0)
    # A 1px hairline zoomed *out* must still be a hairline.
    eq("a 1px pad never rounds away", style.scale_theme(base, 0.65).cell_pad >= 1, True)


def test_css_lengths() -> None:
    print("\n[scale_css] the sheet's own hard-coded lengths come too")
    eq("no zoom, no rewrite", style.scale_css("width: 8px;", 1.0), "width: 8px;")
    eq("px doubles", style.scale_css("width: 8px;", 2.0), "width: 16px;")
    eq("a multi-value shorthand does too",
       style.scale_css("padding: 2px 18px 2px 20px;", 2.0),
       "padding: 4px 36px 4px 40px;")
    eq("a negative offset keeps its sign",
       style.scale_css("top: -1px;", 2.0), "top: -2px;")
    eq("a hairline stays a hairline when zooming out",
       style.scale_css("border: 1px solid #000;", 0.65),
       "border: 1px solid #000;")
    eq("zero stays zero", style.scale_css("margin: 0px;", 2.0), "margin: 0px;")
    eq("points scale, and stay whole so Qt can parse them",
       style.scale_css("font-size: 9pt;", 1.5), "font-size: 14pt;")
    ok("a unitless number is not a length",
       style.scale_css("font-weight: 600;", 2.0) == "font-weight: 600;")
    eq("and a hex colour is not a length either",
       style.scale_css("color: #123456;", 2.0), "color: #123456;")


# --- applied to a live application ---------------------------------------


def test_applied(app) -> None:
    print("\n[apply] the app font, the constants and the sheet agree")
    for tid in ("classic", "light"):
        for s in (0.75, 1.0, 1.5, 2.0):
            t = style.apply_theme(app, tid, scale=s, persist=False)
            css = app.styleSheet()
            eq(f"{tid}@{s}: the sheet's body size is the theme's",
               re.search(r"font-size: (\S+);", css).group(1),
               style.size_css(t))
            eq(f"{tid}@{s}: ROW_HEIGHT is published scaled",
               style.ROW_HEIGHT, t.row_height)
            eq(f"{tid}@{s}: ICON_SIZE is published scaled",
               style.ICON_SIZE, t.icon_px)
            eq(f"{tid}@{s}: ui_scale reports what was asked",
               style.ui_scale(), s)
            eq(f"{tid}@{s}: base_theme is the skin, not the zoom",
               style.base_theme(), style.get_theme(tid))
            if t.font_px:
                eq(f"{tid}@{s}: the application font is the pinned em box",
                   QFontInfo(app.font()).pixelSize(), t.font_px)

    # small_css is set inline on individual widgets -- a status line, a form's
    # mode label -- and so never rides the app sheet through scale_css. It has
    # to scale itself, or a hint stays tiny beside a doubled body.
    for tid in ("classic", "light"):
        style.apply_theme(app, tid, scale=1.0, persist=False)
        small_1 = style.small_css(9)
        style.apply_theme(app, tid, scale=2.0, persist=False)
        small_2, body_2 = style.small_css(9), style.size_css()
        ok(f"{tid}: an inline secondary label grows with the zoom",
           small_2 != small_1, f"{small_1} -> {small_2}")
        # The whole point of small_css: never taller than the body it annotates.
        n = lambda s: int(s.rstrip("ptx"))  # noqa: E731  ("11px"/"9pt" -> int)
        ok(f"{tid}: ...and still is not taller than the body",
           n(small_2) <= n(body_2), f"small {small_2} vs body {body_2}")

    # A number the stylesheet hard-codes rather than reading off the theme.
    style.apply_theme(app, "light", scale=1.0, persist=False)
    at_1 = int(re.search(r"QScrollBar:vertical \{[^}]*width: (\d+)px",
                         app.styleSheet()).group(1))
    style.apply_theme(app, "light", scale=2.0, persist=False)
    at_2 = int(re.search(r"QScrollBar:vertical \{[^}]*width: (\d+)px",
                         app.styleSheet()).group(1))
    eq("a scrollbar the sheet sizes itself doubles too", at_2, at_1 * 2)

    # A checkbox / radio indicator is a style pixel-metric, not a QSS length,
    # so the sheet never touches it -- it stayed 13px next to doubled text and
    # rows until the proxy styles scaled the metric. Both classic (winbox3) and
    # modern (pro) skins, and the radio's exclusive pair too.
    from PySide6.QtWidgets import QCheckBox, QRadioButton
    for tid in ("classic", "light"):
        style.apply_theme(app, tid, scale=1.0, persist=False)
        cb1, rb1 = QCheckBox(), QRadioButton()
        chk_1 = cb1.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, None, cb1)
        rad_1 = rb1.style().pixelMetric(
            QStyle.PixelMetric.PM_ExclusiveIndicatorWidth, None, rb1)
        style.apply_theme(app, tid, scale=2.0, persist=False)
        cb2, rb2 = QCheckBox(), QRadioButton()
        chk_2 = cb2.style().pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, None, cb2)
        rad_2 = rb2.style().pixelMetric(
            QStyle.PixelMetric.PM_ExclusiveIndicatorWidth, None, rb2)
        ok(f"{tid}: the checkbox indicator grows with the zoom",
           chk_2 >= chk_1 * 2 - 1, f"{chk_1} -> {chk_2}")
        ok(f"{tid}: ...and so does the radio's",
           rad_2 >= rad_1 * 2 - 1, f"{rad_1} -> {rad_2}")


def test_title_strip(app) -> None:
    print("\n[chrome] an MDI child's title strip grows with its rows")
    for tid in ("light", "classic", "high-contrast"):
        heights = {}
        for s in (1.0, 2.0):
            style.apply_theme(app, tid, scale=s, persist=False)
            win = QMainWindow()
            mdi = QMdiArea()
            win.setCentralWidget(mdi)
            child = QWidget()
            child.setWindowTitle("Interface List")
            sub = mdi.addSubWindow(child)
            win.resize(900, 700)
            win.show()
            app.processEvents()
            heights[s] = sub.height() - child.height()
            win.close()
            win.deleteLater()
            app.processEvents()
        # Not exactly 2x: the strip is a title plus a frame, and each rounds.
        grew = heights[2.0] >= heights[1.0] * 1.8
        ok(f"{tid}: {heights[1.0]}px -> {heights[2.0]}px", grew, f"{heights}")


def test_open_list_re_measures(app) -> None:
    print("\n[list] a pane already on screen re-reads the sizes")
    from x3.ui.widgets.list_pane import ListPane

    style.apply_theme(app, "classic", scale=1.0, persist=False)
    pane = ListPane("Interface", path=(0x8f, 0))
    vh = pane.table.verticalHeader()
    at_1 = (vh.defaultSectionSize(), pane._bar.iconSize().width())
    eq("built at the theme's row height", at_1[0], style.ROW_HEIGHT)

    # The zoom lands while the pane is open: the stylesheet reaches its type on
    # its own, but the row height and the toolbar's icon edge were set on the
    # widgets and would otherwise hold the size the pane was built at.
    style.apply_theme(app, "classic", scale=2.0, persist=False)
    stale = (vh.defaultSectionSize(), pane._bar.iconSize().width())
    eq("...and the sheet alone does not move it", stale, at_1)
    pane.refresh_metrics()
    eq("refresh_metrics re-reads the row height",
       vh.defaultSectionSize(), style.ROW_HEIGHT)
    eq("...and the toolbar's icon edge",
       pane._bar.iconSize().width(), style.ICON_SIZE)
    ok("both actually grew", vh.defaultSectionSize() > at_1[0]
       and pane._bar.iconSize().width() > at_1[1])
    pane.shutdown()


def test_window_grows(app) -> None:
    print("\n[window] a floating window grows with the zoom; a maximized one does not")
    from PySide6.QtWidgets import QWidget

    style.apply_theme(app, "light", scale=1.0, persist=False)

    # scale_window is ratio-only and compounds, the way the ladder does.
    w = QWidget()
    w.resize(300, 200)
    w.show()
    app.processEvents()
    style.scale_window(w, 1.15)
    app.processEvents()
    eq("a step out from 300x200", (w.width(), w.height()), (345, 230))
    style.scale_window(w, 1.3 / 1.15)
    app.processEvents()
    eq("the next step compounds on that", (w.width(), w.height()), (390, 260))
    style.scale_window(w, 1.0)
    app.processEvents()
    eq("ratio 1.0 is a no-op", (w.width(), w.height()), (390, 260))
    # Never wider or taller than the screen holds.
    avail = w.screen().availableGeometry()
    style.scale_window(w, 100.0)
    app.processEvents()
    ok("clamped to the screen", w.width() <= avail.width() and w.height() <= avail.height(),
       f"{w.width()}x{w.height()} vs {avail.width()}x{avail.height()}")
    w.close()

    # A maximized window is not the user's to resize -- the zoom re-fills it.
    m = QWidget()
    m.resize(300, 200)
    m.showMaximized()
    app.processEvents()
    before = (m.width(), m.height())
    style.scale_window(m, 2.0)
    app.processEvents()
    eq("a maximized window is left exactly alone", (m.width(), m.height()), before)
    m.close()


def test_persistence(app) -> None:
    print("\n[settings] the zoom is remembered, and 1.0 is remembered as nothing")
    save_ui_scale(1.0)
    ok("no zoom, no key", "ui_scale" not in load_settings())
    style.apply_theme(app, "light", scale=1.3, persist=True)
    eq("a zoom is saved", load_ui_scale(), 1.3)
    # This is the path a font pin and View -> Re-apply take: no scale named.
    style.apply_theme(app, "light", persist=True)
    eq("re-applying the theme for another reason keeps the zoom",
       style.ui_scale(), 1.3)
    style.apply_theme(app, "classic", persist=True)
    eq("and so does changing the skin", style.ui_scale(), 1.3)
    style.apply_theme(app, "light", scale=1.0, persist=True)
    ok("back to normal size clears the key", "ui_scale" not in load_settings())
    save_ui_scale("nonsense")
    eq("an unreadable saved value reads as no zoom", load_ui_scale(), 1.0)


# --- the menu and the keys -----------------------------------------------


def test_view_menu(app) -> None:
    print("\n[View -> Zoom] the entries, their keys, and what they do")
    from x3.ui.session import SessionInfo
    from x3.ui.widgets.main_window import MainWindow

    class StubClient:
        readonly = True
        schema = None
        session_log = None

        def nav_tree(self):
            return []

        def close(self):
            pass

    style.apply_theme(app, "light", scale=1.0, persist=True)
    win = MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=StubClient())

    keys = lambda act: [k.toString() for k in act.shortcuts()]  # noqa: E731
    ok("Ctrl+= zooms in (the key people actually press)",
       "Ctrl+=" in keys(win._zoom_in_act), f"got {keys(win._zoom_in_act)}")
    ok("...and so does Ctrl++, for the shifted key and the keypad",
       "Ctrl++" in keys(win._zoom_in_act), f"got {keys(win._zoom_in_act)}")
    ok("Ctrl+- zooms out",
       "Ctrl+-" in keys(win._zoom_out_act), f"got {keys(win._zoom_out_act)}")
    eq("Ctrl+0 is normal size",
       win._zoom_reset_act.shortcut(), QKeySequence("Ctrl+0"))
    ok("the keys answer from anywhere in the window, not just the menu bar",
       all(a in win.actions() for a in
           (win._zoom_in_act, win._zoom_out_act, win._zoom_reset_act)))

    eq("the submenu wears the factor", win._zoom_menu.title(), "&Zoom (100%)")
    ok("nothing to reset at 100%", not win._zoom_reset_act.isEnabled())

    win.resize(700, 500)
    app.processEvents()
    row_at_1, size_at_1 = style.ROW_HEIGHT, (win.width(), win.height())
    win._zoom_in_act.trigger()
    app.processEvents()
    eq("one step in", style.ui_scale(), 1.15)
    eq("the title follows", win._zoom_menu.title(), "&Zoom (115%)")
    ok("the rows grew", style.ROW_HEIGHT > row_at_1,
       f"{row_at_1} -> {style.ROW_HEIGHT}")
    ok("and the window grew with them",
       (win.width(), win.height()) != size_at_1
       and win.width() >= size_at_1[0] and win.height() >= size_at_1[1],
       f"{size_at_1} -> {(win.width(), win.height())}")
    ok("and reset is live now", win._zoom_reset_act.isEnabled())

    win._zoom_out_act.trigger()
    # A press inside the settle window after a re-skin moves the target and
    # lands once the presses stop -- three quick Ctrl+= are one re-skin, not
    # three queued behind each other.
    eq("a press right after a zoom waits for the settle", style.ui_scale(), 1.15)
    import time as _time
    deadline = _time.monotonic() + 2
    while win._zoom_timer.isActive() and _time.monotonic() < deadline:
        app.processEvents()
        _time.sleep(0.02)
    eq("one step back out", style.ui_scale(), 1.0)
    eq("...to the rows it started with", style.ROW_HEIGHT, row_at_1)

    # The ends of the ladder read as spent rather than quietly doing nothing --
    # which also takes the shortcut out, so the chord is honest too.
    win._set_ui_scale(style.SCALE_STEPS[-1])
    ok("at the top, Zoom In is greyed out", not win._zoom_in_act.isEnabled())
    ok("but Zoom Out is not", win._zoom_out_act.isEnabled())
    win._set_ui_scale(style.SCALE_STEPS[0])
    ok("at the bottom, Zoom Out is greyed out", not win._zoom_out_act.isEnabled())
    ok("but Zoom In is not", win._zoom_in_act.isEnabled())

    win._zoom_reset_act.trigger()
    eq("Normal Size goes straight back to 1.0", style.ui_scale(), 1.0)

    # The nav strip is set on the widget, not in the sheet, so it is one of the
    # things _refresh_open_metrics has to re-read on an open window.
    narrow = win.tree.minimumWidth()
    win._set_ui_scale(2.0)
    ok("the open nav strip is re-measured in place",
       win.tree.minimumWidth() > narrow,
       f"{narrow} -> {win.tree.minimumWidth()}")
    ok("...and its icons with it", win.tree.iconSize().width() == style.ICON_SIZE,
       f"{win.tree.iconSize().width()} vs {style.ICON_SIZE}")
    from PySide6.QtWidgets import QToolBar
    tb = win.findChild(QToolBar, "mainToolbar")
    eq("the session toolbar's icon edge", tb.iconSize().width(), style.ICON_SIZE)

    # A zoom the user never asked to keep would be a surprise on next launch.
    eq("the zoom is persisted from the menu too", load_ui_scale(), 2.0)

    win._zoom_reset_act.trigger()
    win.close()
    win.deleteLater()


def test_connect_dialog(app) -> None:
    print("\n[connect] the first screen zooms too, before any MainWindow exists")
    from PySide6.QtGui import QShortcut

    from x3.ui import qthreads
    from x3.ui.widgets.connect_dialog import ConnectDialog

    style.apply_theme(app, "classic", scale=1.0, persist=True)
    d = ConnectDialog()
    chords = {s.key().toString() for s in d.findChildren(QShortcut)}
    for want in ("Ctrl+=", "Ctrl++", "Ctrl+Shift+=", "Ctrl+-", "Ctrl+0"):
        ok(f"the dialog carries {want}", want in chords, f"got {sorted(chords)}")

    d.resize(500, 400)
    app.processEvents()
    at_1, dsize_1 = style.ROW_HEIGHT, (d.width(), d.height())
    row_1 = d.addresses.verticalHeader().defaultSectionSize()
    d._zoom_step(1)
    app.processEvents()
    eq("Ctrl+= steps it in", style.ui_scale(), 1.15)
    ok("...and the rows grow with no window to re-measure",
       style.ROW_HEIGHT > at_1, f"{at_1} -> {style.ROW_HEIGHT}")
    # The bug this guards: a table's row height is set on the widget, not by the
    # sheet, so a live zoom used to leave it stale -- text ran together going up,
    # gaps opened coming back down. _refresh_metrics re-applies ROW_HEIGHT.
    eq("...and the table row height follows the zoom live",
       d.addresses.verticalHeader().defaultSectionSize(), style.ROW_HEIGHT)
    ok("...and the dialog grows too", (d.width(), d.height()) != dsize_1
       and d.width() >= dsize_1[0], f"{dsize_1} -> {(d.width(), d.height())}")
    d._zoom_reset()
    app.processEvents()
    eq("Ctrl+0 goes back", style.ui_scale(), 1.0)
    eq("...to the rows it started with", style.ROW_HEIGHT, at_1)
    eq("...and the table row height comes back down with it (no huge gaps)",
       d.addresses.verticalHeader().defaultSectionSize(), row_1)
    ok("...and the dialog shrinks back", (d.width(), d.height()) == dsize_1,
       f"{(d.width(), d.height())} vs {dsize_1}")
    # A zoom set here carries into the session window, which reads the saved
    # factor at startup -- so the two screens agree.
    d._zoom_step(1)
    eq("the connection page persists its zoom", load_ui_scale(), 1.15)

    d.reject()
    qthreads.retire_all()

    # Built under an active zoom (the startup path: theme applied before the
    # dialog exists), the window opens proportionally larger and its rows are
    # already scaled -- not the 100% size with big content crammed in.
    style.apply_theme(app, "classic", scale=1.0, persist=False)
    small = ConnectDialog()
    base_w, base_row = small.width(), small.addresses.verticalHeader().defaultSectionSize()
    small.reject()
    qthreads.retire_all()
    style.apply_theme(app, "classic", scale=2.0, persist=False)
    big = ConnectDialog()
    ok("a dialog built at a saved zoom opens larger",
       big.width() >= base_w * 2 - 2, f"{base_w} -> {big.width()}")
    ok("...with its rows already scaled",
       big.addresses.verticalHeader().defaultSectionSize() >= base_row * 2 - 2,
       f"{base_row} -> {big.addresses.verticalHeader().defaultSectionSize()}")
    big.reject()
    qthreads.retire_all()
    style.apply_theme(app, "light", scale=1.0, persist=True)


def test_child_windows_open_scaled(app) -> None:
    print("\n[child] a window opened while zoomed in opens at the zoomed size")
    import os

    from x3.ui.widgets.list_window import ListWindow

    # The complaint this guards: zoom in, double-click an interface, and the
    # form opened at its 100% size with 2x-tall rows crammed into it. A window
    # already on screen is re-measured in place (_refresh_open_metrics); a
    # window's own opening size is left to the next time it opens -- which is
    # this constructor, and it has to read the zoom.
    def sizes(build):
        out = {}
        for scale in (1.0, 2.0):
            style.apply_theme(app, "classic", scale=scale, persist=False)
            win = build()
            app.processEvents()
            out[scale] = (win.width(), win.height(),
                          win.minimumWidth(), win.minimumHeight())
            win._allow_close = True
            win.close()
            win.deleteLater()
            app.processEvents()
        return out

    lists = sizes(lambda: ListWindow("Interface", path=(0x8f, 0)))
    ok("a list window doubles", lists[2.0][0] >= lists[1.0][0] * 2 - 2
       and lists[2.0][1] >= lists[1.0][1] * 2 - 2, f"{lists}")

    # The reported case: Interface -> double-click a row -> the ether3 form.
    # It needs a decoded object, so it needs the plugin cache.
    schema_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "7.21.1-151146055")
    if not os.path.isdir(schema_dir):
        print(f"skipped (form): no plugin cache at {schema_dir}")
        return

    from x3.proto import message as m
    from x3.proto.device import RowObject
    from x3.proto.schema import Schema
    from x3.ui.edit_mode import EditMode, EditSession
    from x3.ui.widgets.property_window import PropertyWindow

    loaded = Schema.load("7.21.1-151146055")
    path = (20, 0)

    class StubClient:
        # A class body does not close over the function's locals, so the
        # schema comes in under another name.
        schema = loaded

        def __init__(self, row):
            self._row = row

        def menu_for(self, p, type_value=None, **kw):
            return (loaded.record_for(p, type_value) if type_value is not None
                    else loaded.record(p))

        def alloc_from(self):
            return (0, 7)

        def get(self, p):
            return self._row

        def getobj(self, p, obj_id, **kw):
            return self._row

        def prefetch_enums(self, paths):
            pass

        def enum_map(self, p):
            return {}

        def enum_choices(self, fld):
            return sorted((fld.enum or {}).items())

        def interface_choices(self):
            return []

        def query_enum(self, spec, params=None, **kw):
            return {}

    def form():
        msg = m.Message()
        msg.add_u32(0xFE0001, 3)
        msg.add_u32(0x010001, 1)          # type -> Ethernet
        msg.add_string(0x010006, "ether3")
        msg.add_u32(0x010064, 1500)
        row = RowObject(
            path=path, raw=msg, fields=loaded.decode(path, msg),
            form_fields=loaded.decode_form(path, msg),
            values_by_code={c: v for c, _t, v in msg},
        )
        return PropertyWindow(
            "ether3", path, row, client=StubClient(row),
            edit_session=EditSession(EditMode.QUEUE, client=None),
            menu=loaded.record_for(path, 1), row_key="3", full_row=True,
        )

    forms = sizes(form)
    # Within a few pixels: Windows rounds its metrics differently, and came
    # out 3 px short of double (1381 for 1384).
    ok("the interface form doubles", forms[2.0][0] >= forms[1.0][0] * 2 - 4
       and forms[2.0][1] >= forms[1.0][1] * 2 - 4, f"{forms}")
    ok("...and so does the floor it can be dragged to",
       forms[2.0][2] >= forms[1.0][2] * 2 - 2
       and forms[2.0][3] >= forms[1.0][3] * 2 - 2, f"{forms}")
    style.apply_theme(app, "light", scale=1.0, persist=True)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    test_ladder()
    test_every_stop_moves()
    test_sizes_only()
    test_css_lengths()
    test_applied(app)
    test_title_strip(app)
    test_open_list_re_measures(app)
    test_window_grows(app)
    test_persistence(app)
    test_view_menu(app)
    test_connect_dialog(app)
    test_child_windows_open_scaled(app)
    save_ui_scale(1.0)
    print()
    if _FAILS:
        print(f"{len(_FAILS)} check(s) failed:")
        for f in _FAILS:
            print(f"  - {f}")
        return 1
    print("all UI-scale checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
