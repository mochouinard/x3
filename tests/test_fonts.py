"""UI font choice: side-loading, honest substitution reporting, live pinning.

No device and no pytest required:

    .venv/bin/python test_fonts.py

Why any of this exists: the classic skin asks for Tahoma and the flat skins
for Segoe UI, but on a stock Linux box fontconfig answers Tahoma, Segoe UI and
Microsoft Sans Serif all with the same substitute — so the skins were one
typeface at three sizes and "switching fonts" changed nothing on screen.

Four things are pinned.

*Side-loading* registers font files that exist on disk but outside
fontconfig's path (Wine's ``tahoma.ttf``, anything dropped in the user's own
font directory), so asking for Tahoma is not a wish.

*Substitution is reported, never hidden*: ``resolve`` returns the family Qt
really draws, because ``QFontInfo.exactMatch()`` stays False even for a face
we just loaded ourselves and would have called every font missing.

*The pin outranks the theme* and survives a theme change — that is the whole
point of comparing: hold the font still, change the skin, or the reverse.

*Empty string means "follow the theme"* and clears the key rather than
storing "", so an old settings file and a reset end up in the same state.
"""

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="x3-fonts-test-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")

from dataclasses import replace

from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF
from PySide6.QtWidgets import QApplication, QLabel

from x3.ui import fonts, style
from x3.ui.settings import load_ui_font, save_ui_font

#: Wine ships a metric-compatible Tahoma. It is the one real member of the
#: comparison set most Linux boxes have, so the side-load test uses it when
#: present and says so when it is not.
WINE_TAHOMA = "/usr/share/wine/fonts/tahoma.ttf"

_FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        _FAILS.append(label)


def test_settings_roundtrip() -> None:
    print("\n[settings] the pin is stored, and \"\" clears it")
    check(load_ui_font() == "", "no pin by default")
    save_ui_font("Tahoma")
    check(load_ui_font() == "Tahoma", "a pinned family reads back")
    save_ui_font("")
    check(load_ui_font() == "", "empty string clears the pin")
    save_ui_font("  ")
    check(load_ui_font() == "", "blank input is not a family")


def test_sideload() -> None:
    print("\n[sideload] font files outside fontconfig's path get registered")
    drop = fonts.drop_dir()
    drop.mkdir(parents=True, exist_ok=True)
    check(drop in fonts.search_dirs(), "the user drop directory is searched")

    if not os.path.exists(WINE_TAHOMA):
        print(f"  skip   no {WINE_TAHOMA} on this box; side-load unverified")
        return
    shutil.copy(WINE_TAHOMA, drop / "tahoma.ttf")
    loaded = fonts.sideload(force=True)
    families = {f for fams in loaded.values() for f in fams}
    check("Tahoma" in families, f"Tahoma loaded from the drop dir ({len(loaded)} file(s))")
    check(QFontDatabase.hasFamily("Tahoma"), "Qt now has a real Tahoma family")
    check(not fonts.is_substituted("Tahoma"), "Tahoma stops being a substitution")

    # The point of loading it: the type really is different now. Width alone
    # is a bad witness -- an address line happens to come out within half a
    # pixel of Noto Sans -- so the line box is measured too, which is what a
    # dense table actually feels.
    def metrics(fam: str) -> tuple[float, float]:
        f = QFont(fam)
        f.setPointSize(10)
        fm = QFontMetricsF(f)
        return fm.height(), fm.horizontalAdvance(
            "Interfaces  Bridge  IP  Routing  System  Tools"
        )

    tah_h, tah_w = metrics("Tahoma")
    sub_h, sub_w = metrics("Noto Sans")
    check(
        abs(tah_h - sub_h) > 1.0 or abs(tah_w - sub_w) > 1.0,
        f"Tahoma measures differently than the substitute "
        f"(line {tah_h:.1f} vs {sub_h:.1f}px, menu row {tah_w:.0f} vs {sub_w:.0f}px)",
    )


def test_resolution_is_honest() -> None:
    print("\n[resolve] a missing family is reported, not silently swapped")
    missing = "No Such Family 12345"
    check(fonts.resolve(missing) != missing, "a bogus family resolves elsewhere")
    check(fonts.is_substituted(missing), "and is flagged as substituted")
    check(not fonts.is_substituted(""), "the theme default is never 'substituted'")

    theme = style.get_theme("classic")
    entries = fonts.choices(theme.font_family, point_size=theme.font_pt)
    check(entries[0].family == "", "first entry follows the theme")
    check(
        theme.font_family in entries[0].label,
        f"and names it: {entries[0].label!r}",
    )
    fams = [e.family for e in entries[1:]]
    check(fams == list(fonts.COMPARE_FAMILIES), f"then the comparison set: {fams}")
    for e in entries[1:]:
        if e.substituted:
            check(
                e.resolved.lower() in e.label.lower(),
                f"{e.family}: the label says what is really drawn ({e.resolved})",
            )


def test_default_is_tahoma(app: QApplication) -> None:
    print("\n[default] a theme with no font of its own draws in Tahoma")
    save_ui_font("")
    fonts.sideload()
    check(fonts.UI_STACK[0] == "Tahoma", "Tahoma leads the fallback stack")
    style.apply_theme(app, "light", persist=False)
    check(
        style.active_theme().font_family == "",
        "the default theme names no family of its own",
    )
    # Wine's tahoma.ttf is side-loaded, so this is the real face, not a guess.
    check(
        app.font().family() == "Tahoma",
        f"so the app font is Tahoma (got {app.font().family()!r})",
    )


def test_pin_beats_theme(app: QApplication) -> None:
    print("\n[apply] the pin outranks the theme and survives a theme change")
    save_ui_font("")
    style.apply_theme(app, "classic", persist=False)
    theme_pick = app.font().family()
    check(theme_pick == "Tahoma", f"winbox3 asks for Tahoma (got {theme_pick!r})")

    fonts.apply("Microsoft Sans Serif", app)
    check(
        app.font().family() == "Microsoft Sans Serif",
        "a pinned family is what the app font asks for",
    )
    style.apply_theme(app, "light", persist=False)
    check(
        app.font().family() == "Microsoft Sans Serif",
        "and it is still asked for after switching to a theme with its own font",
    )
    check(load_ui_font() == "Microsoft Sans Serif", "the pin was persisted")

    fonts.apply("", app)
    style.apply_theme(app, "classic", persist=False)
    check(app.font().family() == "Tahoma", "clearing the pin gives the theme back")


def test_compare_dialog(app: QApplication) -> None:
    print("\n[compare] the dialog shows one column per family, and can pin one")
    from x3.ui.widgets.font_compare import FontCompareDialog

    save_ui_font("")
    style.apply_theme(app, "classic", persist=False)
    dlg = FontCompareDialog()
    check(
        [c.family for c in dlg.columns] == list(fonts.COMPARE_FAMILIES),
        "a column per comparison family",
    )
    dlg._px.setValue(12)
    widths = []
    for col in dlg.columns:
        f = col._samples[0].font()
        widths.append((col.family, f.family(), f.pixelSize()))
    check(
        all(w[1] == w[0] and w[2] == 12 for w in widths),
        f"every column draws its own family at the chosen size: {widths}",
    )
    # The size is quoted in pixels because that is the unit Windows specifies
    # a UI font in; a pt spinbox could not name v3's 11px Tahoma exactly.
    check(dlg._px.suffix().strip() == "px", "the size is expressed in pixels")
    dlg._gdi.setChecked(False)
    plain = dlg.columns[1]._samples[0].font().hintingPreference()
    dlg._gdi.setChecked(True)
    gdi = dlg.columns[1]._samples[0].font().hintingPreference()
    check(
        gdi == QFont.HintingPreference.PreferFullHinting and gdi != plain,
        f"the GDI hinting box reaches the samples ({plain.name} -> {gdi.name})",
    )

    seen: list[str] = []
    dlg.fontChosen.connect(seen.append)
    dlg.columns[1]._use.click()  # Tahoma
    check(load_ui_font() == "Tahoma", "the column's button pins that family")
    check(seen == ["Tahoma"], "and tells the window, so its menu can re-tick")
    check(not dlg.columns[1]._use.isEnabled(), "the pinned column says so")

    dlg._theme_btn.click()
    check(load_ui_font() == "", "'Back to theme default' clears the pin")
    dlg.deleteLater()


def test_classic_is_pixel_exact(app: QApplication) -> None:
    print("\n[classic] the v3 skin is sized and hinted the way GDI sizes and hints")
    save_ui_font("")
    style.apply_theme(app, "classic", persist=False)
    t = style.active_theme()
    check(t.font_px == 11, f"winbox3 pins 11px, v3's own lfHeight (got {t.font_px})")
    check(style.size_css(t) == "11px", f"and the QSS says so: {style.size_css(t)!r}")
    check(f"font-size: {style.size_css(t)};" in style.stylesheet_for(t),
          "the universal rule carries it -- that is what sizes a list")

    f = app.font()
    check(f.pixelSize() == 11, f"the app font is 11px (got {f.pixelSize()})")
    check(
        f.hintingPreference() == QFont.HintingPreference.PreferFullHinting,
        f"and fully hinted (got {f.hintingPreference().name})",
    )
    # A real widget, polished through the app stylesheet: neither the size nor
    # the hinting is allowed to be lost on the way to a cell.
    lab = QLabel("ether1")
    app.setStyleSheet(style.stylesheet_for(t))
    lab.ensurePolished()
    wf = lab.font()
    check(wf.pixelSize() == 11, f"a polished widget still measures 11px ({wf.pixelSize()})")
    check(
        wf.hintingPreference() == QFont.HintingPreference.PreferFullHinting,
        "and the QSS font-size rule does not drop the hinting preference",
    )
    lab.deleteLater()

    if not QFontDatabase.hasFamily("Tahoma"):
        print("  skip   no real Tahoma on this box; ink unmeasured")
        return
    if sys.platform == "win32":
        # The offscreen platform does no hinting on Windows: the ink is the
        # same either way, and there is nothing here to measure.
        print("  skip   no hinting on offscreen Windows; ink unmeasured")
        return
    # What the hinting is worth, and where. On a 1x display it roughly doubles
    # how much of the coverage is solid black, so 11px hinted is no lighter than
    # the 13px it replaced. On a 2x display an 11px request is a 22-device-pixel
    # rendering with resolution to spare, and the knob does almost nothing --
    # asserted here so nobody (me included) re-derives the 1x number and calls
    # it the fix for a HiDPI screen. The size change stands on its own; this one
    # is display-dependent, which is the honest claim.
    row = "ether1 bridge Interfaces 1500 192.168.88.1"

    def ink(px: int, hinting: str, dpr: float) -> float:
        g = QFont("Tahoma")
        g.setPixelSize(px)
        g.setHintingPreference(style.hinting_preference(replace(t, font_hinting=hinting)))
        return fonts.solid_ink(g, row, dpr=dpr)

    slight_11, gdi_11, old_13 = ink(11, "", 1.0), ink(11, "full", 1.0), ink(13, "", 1.0)
    check(
        gdi_11 > slight_11 * 1.5,
        f"at 1x, GDI hinting darkens 11px Tahoma: {100*slight_11:.0f}% -> "
        f"{100*gdi_11:.0f}% solid ink",
    )
    check(
        gdi_11 >= old_13,
        f"so at 1x, 11px is no lighter than the 13px it replaces "
        f"({100*gdi_11:.0f}% vs {100*old_13:.0f}%)",
    )
    hi_slight, hi_gdi = ink(11, "", 2.0), ink(11, "full", 2.0)
    check(
        abs(hi_gdi - hi_slight) < 0.05,
        f"at 2x it is close to a no-op ({100*hi_slight:.0f}% -> {100*hi_gdi:.0f}%) "
        f"— on a HiDPI screen the colour and the size are the levers, not this",
    )
    check(
        fonts.solid_ink(QFont(app.font()), row) == fonts.solid_ink(
            QFont(app.font()), row, dpr=fonts.screen_dpr()
        ),
        f"and solid_ink defaults to this screen's ratio ({fonts.screen_dpr():g}x), "
        f"not to 1x",
    )


def test_theme_font_traits(app: QApplication) -> None:
    print("\n[traits] every font trait a theme sets lands, and none of them stick")
    save_ui_font("")
    base = replace(style.get_theme("light"), id="_traits")
    style.THEMES["_traits"] = base

    loud = replace(
        base,
        id="_loud",
        font_weight=700,
        font_letter_spacing=1.5,
        font_antialias="nosubpixel",
    )
    style.THEMES["_loud"] = loud
    f = style.apply_font_traits(QFont(), loud)
    check(f.weight() == QFont.Weight(700), f"weight reaches the font ({f.weight()})")
    check(abs(f.letterSpacing() - 1.5) < 0.01, f"tracking too ({f.letterSpacing()})")
    check(
        f.styleStrategy() == QFont.StyleStrategy.NoSubpixelAntialias,
        f"and the antialiasing mode ({f.styleStrategy().name})",
    )

    # The bug worth a test: a default-constructed QFont starts from the
    # *application* font, so a trait a theme leaves alone is not Qt's default —
    # it is whatever the last theme put there. Guarding on truthiness meant one
    # bold skin made every later skin bold.
    style.apply_theme(app, "_loud", persist=False)
    style.apply_theme(app, "_traits", persist=False)
    plain = style.apply_font_traits(QFont(), base)
    check(
        plain.weight() == QFont.Weight.Normal,
        f"a theme with no weight goes back to normal, not the last one "
        f"({plain.weight()})",
    )
    check(
        abs(plain.letterSpacing()) < 0.01,
        f"same for tracking ({plain.letterSpacing()})",
    )

    # ...and the trait Qt refuses to carry on the application font, which is
    # why the data tables set their own.
    style.apply_theme(app, "_loud", persist=False)
    check(
        app.font().styleStrategy() != QFont.StyleStrategy.NoSubpixelAntialias,
        "QApplication.setFont does not carry styleStrategy (it is not in the "
        "resolve mask) — so this cannot ride the app font",
    )
    from PySide6.QtWidgets import QTableWidget

    table = QTableWidget(1, 1)
    style.style_data_table(table)
    check(
        table.font().styleStrategy() == QFont.StyleStrategy.NoSubpixelAntialias,
        f"style_data_table puts it on the table instead "
        f"({table.font().styleStrategy().name})",
    )
    table.deleteLater()
    for tid in ("_traits", "_loud"):
        style.THEMES.pop(tid, None)
    style.apply_theme(app, "light", persist=False)


def test_secondary_labels_never_outgrow_the_body(app: QApplication) -> None:
    print("\n[small] a 9pt status label is clamped under an 11px body")
    style.apply_theme(app, "classic", persist=False)
    check(style.small_css(9) == "11px", f"clamped to the body: {style.small_css(9)!r}")
    check(style.small_css(7) == "9px", f"a genuinely smaller size survives: {style.small_css(7)!r}")
    style.apply_theme(app, "roomy", persist=False)
    check(style.small_css(9) == "9pt", f"a point-sized theme is untouched: {style.small_css(9)!r}")


def test_derived_fonts_keep_their_size(app: QApplication) -> None:
    print("\n[derived] a hand-built font shrinks by a step, not to the floor")
    style.apply_theme(app, "classic", persist=False)
    # QFont.pointSize() answers -1 on a pixel-sized font, so "one step smaller
    # than what I inherited" used to collapse to the 6pt floor.
    base = QFont(app.font())
    check(base.pointSize() == -1, "the inherited font has no point size to read")
    small = style.size_font(QFont(base), delta_px=-2)
    check(small.pixelSize() == 9, f"two pixels off 11 is 9 (got {small.pixelSize()})")
    style.apply_theme(app, "roomy", persist=False)
    small = style.size_font(QFont(app.font()), delta_px=-2)
    check(
        abs(small.pointSizeF() - 8.5) < 0.01,
        f"and a point-sized theme steps in points (got {small.pointSizeF()})",
    )


class _StubClient:
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


def test_view_menu(app: QApplication) -> None:
    print("\n[menu] View ▸ Font offers the families and follows the theme")
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)

    from x3.ui.session import SessionInfo
    from x3.ui.widgets import main_window as mw

    save_ui_font("")
    style.apply_theme(app, "classic", persist=False)
    win = mw.MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=_StubClient())
    labels = [a.text() for a in win._font_actions]
    check(
        labels[0].startswith("Theme default (Tahoma)"),
        f"the theme's own font leads the menu: {labels[0]!r}",
    )
    check(
        [a.data() for a in win._font_actions[1:]] == list(fonts.COMPARE_FAMILIES),
        "then one entry per comparison family",
    )
    check(win._font_actions[0].isChecked(), "nothing pinned -> the theme entry is ticked")

    win._set_ui_font("Tahoma")
    ticked = [a.data() for a in win._font_actions if a.isChecked()]
    check(ticked == ["Tahoma"], f"picking one re-ticks the menu: {ticked}")
    check(app.font().family() == "Tahoma", "and the app font follows immediately")

    win._set_theme("light")
    labels = [a.text() for a in win._font_actions]
    check(
        labels[0].startswith("Theme default (system UI)"),
        f"a theme change rewrites the default's label: {labels[0]!r}",
    )
    check(
        [a.data() for a in win._font_actions if a.isChecked()] == ["Tahoma"],
        "and leaves the pin alone",
    )

    win._set_ui_font("")
    win.close()
    win.deleteLater()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    test_settings_roundtrip()
    test_sideload()
    test_resolution_is_honest()
    test_default_is_tahoma(app)
    test_pin_beats_theme(app)
    test_classic_is_pixel_exact(app)
    test_theme_font_traits(app)
    test_secondary_labels_never_outgrow_the_body(app)
    test_derived_fonts_keep_their_size(app)
    test_compare_dialog(app)
    test_view_menu(app)
    save_ui_font("")
    print()
    if _FAILS:
        print(f"{len(_FAILS)} check(s) failed:")
        for f in _FAILS:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
