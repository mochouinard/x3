"""Offscreen tests for the Icon Set choice (colour / monochrome):

    .venv/bin/python test_icon_set.py

The symbolic set has been on disk in ``resources/icons/svg-mono`` since it was
generated, and nothing could ask for it: ``icons.py`` hard-coded the colour
directory. This pins the two-way choice and, more importantly, the thing that makes a symbolic set
worth having.

A symbolic glyph is written in ``currentColor``, and the shipped files declare
``color="currentColor"`` on the root — which is circular, so Qt paints them
black. Black silhouettes are holes in a dark skin's toolbar. The set is only
usable if something names a real ink, so:

  * the ink is the active theme's text colour, not a constant;
  * changing the theme re-inks every glyph (and the cache is keyed on the ink,
    so switching back does not re-rasterize);
  * a user's own override joins the symbolic set instead of staying colour,
    but one that is already symbolic is left alone rather than run through the
    converter a second time (that flattens its washes).
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="x3-iconset-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from x3.ui import icons, iconset, settings, style  # noqa: E402

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


def ink_of(icon, size=24):
    """The colour the glyph is actually painted in.

    The most opaque, most saturated pixel: an antialiased stroke edge is a
    blend with the transparent ground, so sampling a fixed coordinate reads a
    washed-out version of the ink.
    """
    img = icon.pixmap(size, size).toImage()
    best, best_key = None, None
    for y in range(img.height()):
        for x in range(img.width()):
            c = QColor(img.pixelColor(x, y))
            if c.alpha() < 250:
                continue
            key = (c.alpha(), -c.lightnessF() if c.lightnessF() < 0.5 else c.lightnessF())
            if best_key is None or c.alpha() > best_key[0]:
                best, best_key = c, key
    return best


def all_inks(icon, size=24):
    img = icon.pixmap(size, size).toImage()
    out = set()
    for y in range(img.height()):
        for x in range(img.width()):
            c = QColor(img.pixelColor(x, y))
            if c.alpha() >= 250:
                out.add(c.name())
    return out


def one_ink(icon, ink, size=24):
    """True when every opaque pixel is the same ink.

    Not string equality on the colour: a glyph with a wash under a stroke
    (the terminal's screen) blends ink over ink, which rounds a channel or
    two. Same ink, one rounding step -- not a second colour.
    """
    want = QColor(ink)
    for name in all_inks(icon, size):
        c = QColor(name)
        if max(abs(c.red() - want.red()),
               abs(c.green() - want.green()),
               abs(c.blue() - want.blue())) > 8:
            return False
    return True


# --- the setting itself ---------------------------------------------------

check("the default is the colour set", settings.load_icon_source(), "builtin")

settings.save_icon_source("mono")
check("monochrome round-trips", settings.load_icon_source(), "mono")

settings.save_icon_source("MONO")
check("and is not case-sensitive on the way in", settings.load_icon_source(), "mono")

settings.save_icon_source("purple-spaghetti")
check("a value we do not know falls back to colour",
      settings.load_icon_source(), "builtin")

check("every choice has a label for the menu",
      [v for v, _ in settings.ICON_SOURCE_CHOICES], ["builtin", "mono"])
check("and the label drops its mnemonic for the status bar",
      settings.icon_source_label("mono"), "Monochrome")


# --- what gets drawn ------------------------------------------------------

light = style.apply_theme(app, "light", persist=False)
settings.save_icon_source("builtin")
icons.reset_icon_cache()
colour_add = icons.icon_for_kind("add", size=24)
check("the colour set draws Add in its own blue, not the theme's",
      ink_of(colour_add).name(), "#2563eb")

settings.save_icon_source("mono")
icons.reset_icon_cache()
mono_add = icons.icon_for_kind("add", size=24)
check("the symbolic set draws it in the theme's text colour",
      ink_of(mono_add).name(), light.text.lower())
ok("and in nothing else — one ink is the whole point",
   one_ink(mono_add, light.text),
   f"got {sorted(all_inks(mono_add))}")

dark = style.apply_theme(app, "dark", persist=False)
dark_add = icons.icon_for_kind("add", size=24)
check("a dark skin re-inks it rather than leaving a black hole",
      ink_of(dark_add).name(), dark.text.lower())
ok("the two skins' glyphs are genuinely different pixmaps",
   ink_of(dark_add).name() != ink_of(mono_add).name())

before = len(icons._cache)
icons.icon_for_kind("add", size=24)
check("asking again is free", len(icons._cache), before)
style.apply_theme(app, "light", persist=False)
back = icons.icon_for_kind("add", size=24)
check("and switching back reuses what the first theme rasterized",
      (len(icons._cache), ink_of(back).name()), (before, light.text.lower()))

# Danger ink: Remove and Disable keep a red in the symbolic set, so the
# button that deletes does not look like the one that adds.
check("red for Remove and Disable is on unless switched off",
      settings.load_danger_ink(), True)
for label in ("Remove", "Disable", "Delete"):
    ico = icons.action_icon(label, size=24)
    ok(f"mono {label} is drawn in the theme's warning red, and only that",
       one_ink(ico, style.INFO_RED), f"got {sorted(all_inks(ico))}")
ok("Add stays in the text ink beside it",
   one_ink(icons.action_icon("Add", size=24), style.TEXT))
style.apply_theme(app, "dark", persist=False)
ok("a dark skin gets its own lightened red, not the pale-ground one",
   one_ink(icons.action_icon("Remove", size=24), style.INFO_RED)
   and style.INFO_RED.lower() != "#c00000",
   f"INFO_RED={style.INFO_RED}")
style.apply_theme(app, "light", persist=False)
settings.save_danger_ink(False)
icons.reset_icon_cache()
ok("switched off, Remove is one more silhouette in the text ink",
   one_ink(icons.action_icon("Remove", size=24), style.TEXT))
settings.save_danger_ink(True)
icons.reset_icon_cache()

# Cells: a .jg icon index goes through the same set as a named slug.
settings.save_icon_source("builtin")
icons.reset_icon_cache()
cell_colour = all_inks(icons.icon_from_index(16, 24))   # 16 = terminal
settings.save_icon_source("mono")
icons.reset_icon_cache()
cell_icon = icons.icon_from_index(16, 24)
cell_mono = all_inks(cell_icon)
ok("a .jg cell index obeys the choice too",
   one_ink(cell_icon, style.TEXT) and cell_colour != cell_mono,
   f"colour={sorted(cell_colour)} mono={sorted(cell_mono)}")

# The app icon is the product's own mark, not a UI glyph: it stays itself.
ok("the application icon is not tinted with the rest",
   not icons.app_icon().isNull()
   and all_inks(icons.app_icon(), 32) != {style.TEXT.lower()})


# A plate -- a terminal's screen, an LCD -- was drawn in colour as near-black
# with the content in another hue on top. Inked solid, one colour swallows the
# other and the icon is a black box: cell 16 came out 54% solid ink. A covered
# fill has to be a wash, and only a covered one (a lone dark shape, like the
# record dot, is meant to be solid).
plate = iconset.to_symbolic(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
    '<rect x="3" y="4" width="18" height="16" fill="#0f172a" stroke="#111827"/>'
    '<line x1="6" y1="12" x2="18" y2="12" stroke="#16a34a"/>'
    "</svg>"
)
ok("a plate with something drawn on it becomes a wash",
   'fill="currentColor" fill-opacity="0.32"' in plate, plate)
lone = iconset.to_symbolic(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
    '<circle cx="12" cy="12" r="6" fill="#ef4444" stroke="#dc2626"/>'
    "</svg>"
)
ok("a lone dark shape stays solid", 'fill="currentColor"/' in lone
   or ('fill="currentColor"' in lone and "fill-opacity" not in lone), lone)

shipped_terminal = (
    icons._MONO_DIR / "terminal.svg").read_text(encoding="utf-8")
ok("and the shipped symbolic set was regenerated with that rule",
   "fill-opacity" in shipped_terminal)


# --- user overrides -------------------------------------------------------

over_dir = settings.config_dir() / "icons"
over_dir.mkdir(parents=True, exist_ok=True)
(over_dir / "add.svg").write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
    '<line x1="4" y1="12" x2="20" y2="12" stroke="#ff00ff" stroke-width="4"/>'
    "</svg>\n",
    encoding="utf-8",
)
icons.reset_icon_cache()
over_mono = icons.icon_for_kind("add", size=24)
ok("a colour override is converted, not shown in its own colour",
   one_ink(over_mono, style.TEXT),
   f"got {sorted(all_inks(over_mono))}")

settings.save_icon_source("builtin")
icons.reset_icon_cache()
check("while the colour set still shows the override as drawn",
      ink_of(icons.icon_for_kind("add", size=24)).name(), "#ff00ff")

# An override that is already symbolic must not be re-converted: to_symbolic
# strips fill-opacity before it runs, so a second pass eats the wash that
# distinguishes a filled shape from an outlined one.
washed = iconset.to_symbolic(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
    '<rect x="4" y="4" width="16" height="16" fill="#f1f5f9" stroke="#111827"/>'
    "</svg>"
)
(over_dir / "add.svg").write_text(washed + "\n", encoding="utf-8")
settings.save_icon_source("mono")
icons.reset_icon_cache()
ok("a symbolic override keeps its wash",
   'fill-opacity="0.14"' in (icons._mono_svg_text("add") or ""))
ok("and is inked like the rest of the set",
   f'color="{style.TEXT}"' in icons._tinted(icons._mono_svg_text("add"), style.TEXT))

(over_dir / "add.svg").unlink()
icons.reset_icon_cache()

# Nothing on disk at all: the drawn originals still answer symbolically.
raw = iconset.svg_symbolic_for_name("terminal")
ok("the in-code originals can supply the symbolic set on their own",
   raw is not None and "currentColor" in raw)

# The generic marker is what every nav kind we have no glyph for gets — common
# enough that leaving it in fixed blue-gray would put one colour blob in an
# otherwise monochrome toolbar.
ok("the painter fallback is inked too",
   ink_of(icons._drawn_icon("no-such-kind", 24)).name() == style.TEXT.lower(),
   f"got {ink_of(icons._drawn_icon('no-such-kind', 24)).name()}")
settings.save_icon_source("builtin")
icons.reset_icon_cache()
ok("and goes back to its own colours with the colour set",
   ink_of(icons._drawn_icon("no-such-kind", 24)).name() != style.TEXT.lower())
settings.save_icon_source("mono")
icons.reset_icon_cache()


# --- the menu -------------------------------------------------------------

from x3.ui.session import SessionInfo  # noqa: E402
from x3.ui.widgets.main_window import MainWindow  # noqa: E402


class StubClient:
    readonly = True
    schema = None
    session_log = None

    def nav_tree(self):
        return []

    def close(self):
        pass


settings.save_icon_source("builtin")
win = MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=StubClient())

acts = {a.data(): a for a in win._icon_source_actions}
check("View ▸ Icon Set offers both",
      [a.data() for a in win._icon_source_actions], ["builtin", "mono"])


def entries(menu_title):
    act = next(a for a in win.menuBar().actions() if a.text() == menu_title)
    return [
        "---" if a.isSeparator() else a.text()
        for a in act.menu().actions()
    ]


# Which menu it hangs off is not cosmetic: View is what the app looks like and
# Settings is what it does, and an icon set inked from the theme's text colour
# is the same decision as the theme. Byte Units (how a counter reads) and Tabs
# In Title Bar (where a window keeps its tabs) moved with it. Show Passwords
# and Inline Comment did not -- they stay under Settings.
view, sett = entries("&View"), entries("&Settings")
# Zoom joined the cluster: skin, typeface, size, glyphs, then how a counter
# reads. Its title carries the live factor ("&Zoom (100%)") because that is the
# only place the percentage is shown, so the comparison is on the stem.
stems = [v.split(" (")[0] for v in view]
check("Icon Set sits with Theme, Font and Zoom",
      stems[:5], ["&Theme", "&Font", "&Zoom", "&Icon Set", "&Byte Units"])
ok("and the window-chrome toggle is there too", "&Tabs In Title Bar" in view)
ok("Settings keeps none of them",
   not ({"&Icon Set", "&Byte Units", "&Tabs In Title Bar"} & set(sett)), f"got {sett}")
ok("but keeps Show Passwords and Inline Comment",
   {"Show &Passwords", "&Inline Comment"} <= set(sett), f"got {sett}")
ok("and neither menu ends up with a stray divider",
   "---" not in (view[:1] + view[-1:] + sett[:1] + sett[-1:])
   and not any(a == b == "---" for a, b in zip(view, view[1:]))
   and not any(a == b == "---" for a, b in zip(sett, sett[1:])),
   f"view={view} settings={sett}")
ok("they are checkable, so the set in use is visible",
   all(a.isCheckable() for a in win._icon_source_actions))
check("colour starts checked", [d for d, a in acts.items() if a.isChecked()], ["builtin"])

acts["mono"].trigger()
check("picking monochrome saves it", settings.load_icon_source(), "mono")
check("and moves the checkmark, exclusively",
      [d for d, a in acts.items() if a.isChecked()], ["mono"])

# Clicking a checkable action toggles it before the handler runs; clicking the
# one already checked must not turn the whole thing off.
acts["mono"].trigger()
check("clicking the current set again is a no-op, not an unset",
      (settings.load_icon_source(),
       [d for d, a in acts.items() if a.isChecked()]),
      ("mono", ["mono"]))

danger = win._danger_ink_act
ok("the red toggle is live under monochrome", danger.isEnabled())
danger.setChecked(False)
check("unticking it saves it", settings.load_danger_ink(), False)
ok("and the cache was dropped, so Remove redraws in the text ink",
   one_ink(icons.action_icon("Remove", size=24), style.TEXT))
danger.setChecked(True)
ok("ticking it back makes Remove red again",
   one_ink(icons.action_icon("Remove", size=24), style.INFO_RED))

acts["builtin"].trigger()
check("and back", settings.load_icon_source(), "builtin")
check("the checkmark follows", [d for d, a in acts.items() if a.isChecked()], ["builtin"])
ok("the red toggle greys out: the colour set is red already",
   not danger.isEnabled())

# The set has to change on the windows already open, or the setting reads as
# one that did not work.
tree_icon_colour = all_inks(win.tree.topLevelItem(0).icon(0), 16) if (
    win.tree.topLevelItemCount()) else None
acts["mono"].trigger()
if tree_icon_colour is not None:
    tree_icon_mono = all_inks(win.tree.topLevelItem(0).icon(0), 16)
    ok("the open nav tree is re-iconed in place",
       tree_icon_mono != tree_icon_colour,
       f"{sorted(tree_icon_colour)} -> {sorted(tree_icon_mono)}")
else:
    print("ok  (no nav rows in the stub tree to re-icon)")

win.close()

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all icon-set checks passed")
