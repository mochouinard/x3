"""Themes + Qt stylesheets: five skins over the same windows and forms.

Select a theme with :func:`apply_theme` (font + stylesheet; choice is saved).
Module-level palette constants (``BG``, ``TEXT``, …) always track the active
theme so ad-hoc widget stylesheets stay in sync.

Looks are **skins**: same windows/forms, different chrome.

- **classic** and **high-contrast** (``modern=False``): Win32 bevels.
- **light**, **dark** and **roomy** (``modern=True``): flat Fusion chrome;
  metrics control density.

Chrome is per theme, set in the skin's own ``Theme(...)`` block alongside
the palette — copy a nearby theme and change what you want:

- ``mdi_frame``: 1px border around child windows (False = frameless).
- ``mdi_round``: rounded top corners on child windows. True is Fusion's
  native stair; False squares the title strip corner to corner.
- ``radius``: corner radius of the widgets themselves (menus, inputs,
  tables); unrelated to the two above.

Sizes are separable from the skin. :func:`apply_theme` takes a ``scale``, and
every pixel a theme declares -- type, rows, icons, pads, the nav strip -- is
multiplied by it before anything is published, so the same skin can be drawn
larger or smaller without a second ``Theme`` block. The stylesheet gets there
by a different route: it is built from the *unscaled* theme and then rewritten
length by length, which is the only way its own hard-coded numbers (a
scrollbar's width, a button's minimum) come along. See :func:`scale_theme`,
:func:`scale_css` and :data:`SCALE_STEPS`; :func:`base_theme` is the skin as
written, :func:`active_theme` the one on screen.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

# --- structural defaults (overridden by active theme via _publish) ---
TREE_WIDTH = 152
ROW_HEIGHT = 17
CELL_PAD = 2
ICON_SIZE = 16
TOOLBAR_ICON = 16
MARGIN = 2
SPACING = 2


@dataclass(frozen=True)
class Theme:
    """One visual preset (palette + chrome metrics)."""

    id: str
    name: str
    description: str
    font_family: str  # empty = system UI stack
    font_pt: int
    bg: str
    bg_panel: str
    bg_window: str
    bg_alt: str
    bg_header: str
    border: str
    border_light: str
    border_dark: str
    text: str
    text_dim: str
    sel_bg: str
    sel_fg: str
    toolbar_bg: str
    title_active: str
    mdi_bg: str
    gridline: str
    scrollbar: str
    button_hover: str
    # modern=True → flat Fusion chrome; False → classic Win32 bevels
    modern: bool = False
    radius: int = 0
    row_height: int = 17
    # Density knobs used by modern QSS
    pad_y: int = 1
    pad_x: int = 4
    # Dead space each side of a table cell's text, and of the header label
    # above it, so the two line up. Qt's own item painting hard-codes 3px
    # (PM_FocusFrameHMargin + 1); ColumnRuleDelegate reclaims the difference.
    cell_pad: int = 2
    toolbar_pad: int = 1
    menu_item_pad: int = 2
    tree_width: int = 152
    control_min_h: int = 16
    # False → MDI children get no frame at all: no 1px border, no rounded
    # outer corner, just the flat title strip meeting the content.
    mdi_frame: bool = True
    # Colour of a 1px outline drawn around a child window, "" = none. The
    # frameless skins paint their resize band in the backdrop colour so it
    # disappears (see the QMdiSubWindow rule), which also leaves the window with
    # no visible edge at all: a pale panel on a slightly-less-pale backdrop.
    # This puts the edge back without touching the band.
    #
    # Not a QSS border on purpose. QStyleSheetStyle derives
    # PM_MdiSubWindowFrameWidth from that rule's box, so declaring one here
    # would cut the resize grab band from 4px to 1px and make edges almost
    # impossible to grab. MdiWindow.paintEvent draws it instead.
    mdi_border: str = ""
    # False → square window corners. The rounding is not painted: Fusion
    # answers SH_WindowFrame_Mask with a stair cut out of each top corner and
    # QMdiSubWindow applies it as a widget mask, so the title strip shows the
    # MDI backdrop through it. Off, the strip runs corner to corner.
    mdi_round: bool = True
    # Toolbar / tree / title-bar icon edge in px. 16 is the Winbox sprite cell;
    # dense skins look better a couple of px smaller.
    icon_px: int = 16
    # Winbox v3 is a GDI program: its font is a LOGFONT with lfHeight=-11, i.e.
    # eleven *pixels*, not eight points. Points go through the screen's logical
    # DPI, so asking for 10pt drew 13px here -- a fifth taller than the real
    # thing in the same 17px row. Non-zero pins the em box in pixels and
    # outranks font_pt everywhere the size is set; 0 keeps points.
    font_px: int = 0
    # How hard to snap glyphs onto the pixel grid. GDI applies a font's full
    # TrueType hinting, which lands Tahoma's stems on whole pixels and prints
    # them solid black; fontconfig's usual "slight" only hints vertically and
    # leaves every vertical stem split across two grey pixels, which is the
    # thinner, washed-out look. "" = whatever the desktop says, else "full",
    # "vertical" or "none".
    #
    # Worth much less than it sounds on a HiDPI screen: a logical 11px font at
    # devicePixelRatio 2 is a 22-device-pixel rendering with resolution to
    # spare, and full hinting moves the solid-ink share by about a point there
    # against roughly double at 1x. Set it because it is what GDI does, not as
    # a way to make type heavier -- for that, see the text colour.
    font_hinting: str = ""
    # Stroke weight, 0 = leave it to the face. A number between the weights a
    # family actually ships lands back on the nearest one it has: Tahoma is
    # Regular and Bold only, so 300-500 all draw as 400 and 600 jumps straight
    # to the Bold face. Nothing synthesises in between, so this is a two-state
    # knob for most UI faces -- check it moved before believing it.
    font_weight: int = 0
    # Extra space between glyphs, in px, applied as absolute tracking. Widens a
    # line without changing its height, which is occasionally what separates a
    # dense list from an unreadable one. Negative tightens.
    font_letter_spacing: float = 0.0
    # How glyph coverage is turned into pixels. "" follows the desktop, which on
    # a screen configured for subpixel output (`Xft.rgba: rgb`) means an LCD
    # filter and colour fringes; "nosubpixel" asks for plain grayscale, which is
    # what GDI does without ClearType and therefore what Winbox v3 under Wine
    # most likely draws. "prefer" forces antialiasing on, "none" off entirely.
    font_antialias: str = ""
    # Height of the header strip in px, 0 = whatever the row height implies.
    header_height: int = 0
    # Dead space each side of a header label, -1 = follow cell_pad so the label
    # lines up with the column beneath it. Set it only to break that alignment
    # on purpose.
    header_pad: int = -1
    # Winbox rules its columns and leaves its rows alone. False drops the
    # vertical hairline too, for a list with no rules at all.
    column_rules: bool = True


# ---------------------------------------------------------------------------
# Built-in themes
# ---------------------------------------------------------------------------

# The default: dense and flat, black type, and a hairline edge on every child
# window. Its type and row metrics were measured side by side against the
# classic client, not guessed -- treat font_px, text and row_height as
# load-bearing.
LIGHT = Theme(
    id="light",
    name="Light",
    description="Dense flat light skin: black type, hairline window edges.",
    font_family="",
    font_pt=9,  # unused: font_px wins. The fallback if the pixel pin is cleared.
    # Measured on a 2x display rather than guessed. 9pt landed on 12px here
    # and read a pixel too big; hinting was left alone on purpose -- at
    # devicePixelRatio 2 an 11px request is a 22-device-pixel rendering and
    # full hinting moves nothing there.
    font_px=11,
    bg="#e9ecef",
    bg_panel="#f8f9fa",
    bg_window="#ffffff",
    bg_alt="#f1f3f5",
    bg_header="#e9ecef",
    border="#ced4da",
    border_light="#f1f3f5",
    # The title bar is light with dark text rather than a blue accent; its
    # active/inactive shades are title_active/bg_header below.
    border_dark="#868e96",
    # Pure black, not a near-black. On a HiDPI screen the hinting knobs are
    # inert and contrast is what is left: #212529 costs ~13% of black's ink
    # against white, which reads as washed-out type.
    text="#000000",
    text_dim="#6c757d",
    sel_bg="#228be6",
    sel_fg="#ffffff",
    toolbar_bg="#f8f9fa",
    title_active="#dbe6f5",
    mdi_bg="#dee2e6",
    gridline="#e9ecef",
    scrollbar="#adb5bd",
    button_hover="#f1f3f5",
    modern=True,
    mdi_frame=False,
    # A child window on this backdrop had no readable edge: its resize band is
    # painted in the backdrop colour by design, so panel met backdrop with
    # nothing between them. One dark hairline, hugging the content.
    mdi_border="#000000",
    mdi_round=False,
    icon_px=14,
    radius=2,
    # 15, and it now means 15: QHeaderView clamps every section to a
    # font-derived minimum (16px at this size), so shorter rows were
    # silently ignored until list_pane stopped letting it -- see
    # _apply_row_heights.
    row_height=15,
    # The header as tall as a row: its old two spare pixels sat under the
    # label and read as padding.
    header_height=15,
    pad_y=0,
    pad_x=3,
    toolbar_pad=0,
    menu_item_pad=1,
    tree_width=148,
    control_min_h=15,
)

# Light's layout in dark slate, so switching between the two moves nothing.
# The active title bar is a muted blue that takes white ink, the dark
# counterpart of Light's pale one; a bright accent there drew a pastel strip
# with dark text that shouted over the window under it.
DARK = replace(
    LIGHT,
    id="dark",
    name="Dark",
    description="The Light skin's layout in dark slate.",
    bg="#1b1e24",
    bg_panel="#23272f",
    bg_window="#14161b",
    bg_alt="#1a1d24",
    bg_header="#2a2f38",
    border="#3a404c",
    border_light="#4a5160",
    border_dark="#0f1115",
    text="#eceff4",
    text_dim="#9aa3b2",
    sel_bg="#2f6fed",
    sel_fg="#ffffff",
    toolbar_bg="#23272f",
    title_active="#2d4a77",
    mdi_bg="#101218",
    gridline="#2a2f38",
    scrollbar="#4a5160",
    button_hover="#2e343f",
    # The hairline that gives a child window its edge, lighter than the
    # backdrop instead of darker.
    mdi_border="#4a5160",
)

# Comfort over density: the same flat light chrome with room around things.
ROOMY = Theme(
    id="roomy",
    name="Roomy",
    description="Flat light skin with more padding: comfort over density.",
    font_family="",
    font_pt=10,
    bg="#f0f2f5",
    bg_panel="#ffffff",
    bg_window="#ffffff",
    bg_alt="#f7f8fa",
    bg_header="#f3f4f6",
    border="#d0d5dd",
    border_light="#eef0f3",
    border_dark="#98a2b3",
    text="#101828",
    text_dim="#667085",
    sel_bg="#2563eb",
    sel_fg="#ffffff",
    toolbar_bg="#ffffff",
    title_active="#1d4ed8",
    mdi_bg="#e4e7ec",
    gridline="#eaecf0",
    scrollbar="#c5cad3",
    button_hover="#f9fafb",
    modern=True,
    mdi_frame=False,
    radius=5,
    row_height=20,
    pad_y=2,
    pad_x=6,
    toolbar_pad=2,
    menu_item_pad=3,
    tree_width=160,
    control_min_h=18,
)

CLASSIC = Theme(
    id="classic",
    name="Classic",
    description="Win32 gray with bevels, Tahoma and dense tables.",
    font_family="Tahoma",
    font_pt=10,  # unused: font_px wins. Kept as the fallback if the pin clears.
    font_px=11,
    font_hinting="full",
    bg="#d4d0c8",
    bg_panel="#ece9d8",
    bg_window="#ffffff",
    bg_alt="#f5f5f0",
    bg_header="#ece9d8",
    border="#808080",
    border_light="#ffffff",
    border_dark="#404040",
    text="#000000",
    text_dim="#404040",
    sel_bg="#0a246a",
    sel_fg="#ffffff",
    toolbar_bg="#d4d0c8",
    title_active="#0a246a",
    mdi_bg="#808080",
    gridline="#c0c0c0",
    scrollbar="#c0c0c0",
    button_hover="#f0ede0",
    modern=False,
    radius=0,
    row_height=18,
    tree_width=168,
)

HIGH_CONTRAST = Theme(
    id="high-contrast",
    name="High contrast",
    description="Strong borders and larger type for readability.",
    font_family="",
    font_pt=11,
    bg="#c0c0c0",
    bg_panel="#e0e0e0",
    bg_window="#ffffff",
    bg_alt="#f0f0f0",
    bg_header="#d0d0d0",
    border="#000000",
    border_light="#ffffff",
    border_dark="#000000",
    text="#000000",
    text_dim="#202020",
    sel_bg="#000080",
    sel_fg="#ffffff",
    toolbar_bg="#c0c0c0",
    title_active="#000080",
    mdi_bg="#606060",
    gridline="#808080",
    scrollbar="#a0a0a0",
    button_hover="#d8d8d8",
    modern=False,
    radius=0,
    row_height=20,
    tree_width=168,
)

THEMES: dict[str, Theme] = {
    t.id: t for t in (LIGHT, DARK, ROOMY, CLASSIC, HIGH_CONTRAST)
}

DEFAULT_THEME_ID = LIGHT.id

# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------
#: Where Ctrl+= / Ctrl+- stop. Every size in a theme is multiplied by the
#: active stop and rounded in its own unit, so the stops have to be far enough
#: apart that a 9pt face *and* a 2px pad both move -- steps of roughly 15% do
#: that. 1.0 is one of them, which is what makes "reset" an ordinary stop
#: rather than a special case.
SCALE_STEPS: tuple[float, ...] = (
    0.65, 0.75, 0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0
)

_active: Theme = LIGHT
#: The active theme as written in this module, before the zoom was applied.
#: Two things read it rather than ``_active``. The stylesheet, because it is
#: the *finished sheet* that gets scaled (see :func:`scale_css`) and scaling
#: already-scaled numbers would square the factor. And every "is this a dense
#: skin" decision -- SPACING, the MDI title height, the font's style hint --
#: because at scale 2 every skin has tall rows, and reading the scaled numbers
#: would tip a compact skin into the roomy branch halfway up the zoom range.
_base: Theme = LIGHT
_scale: float = 1.0
FONT_PT = _active.font_pt
#: Em box of the UI font in pixels when the active theme pins one, else 0
#: (the theme is sized in points and the screen's DPI decides the pixels).
FONT_PX = _active.font_px
#: False → data tables draw no column separator at all (see ColumnRuleDelegate).
COLUMN_RULES = _active.column_rules
#: Colour of the 1px outline MdiWindow paints around itself, "" = none.
MDI_BORDER = _active.mdi_border
BG = _active.bg
BG_PANEL = _active.bg_panel
BG_WINDOW = _active.bg_window
BG_ALT = _active.bg_alt
BG_HEADER = _active.bg_header
BORDER = _active.border
BORDER_LIGHT = _active.border_light
BORDER_DARK = _active.border_dark
TEXT = _active.text
TEXT_DIM = _active.text_dim
#: Colour of a device status line (STD_INFO), which Winbox draws in red.
#: Derived, not per theme: every skin wants the same warning, only lightened
#: enough to stay legible on a dark ground.
INFO_RED = "#c00000"
#: The "up" half of a dropdown status mark (see widgets/status_marks.py).
#: Derived the same way and for the same reason -- a green that reads as green
#: on a pale ground disappears into a dark one. "Down" borrows INFO_RED and
#: "disabled" borrows TEXT_DIM, so this is the only new ink a mark needs.
MARK_UP = "#2e7d32"
#: Ink for a cell that acts when clicked (the connect page's Address and MAC
#: columns). Derived like the two above: Winbox's navy vanishes on a dark row.
LINK = "#0000aa"
SEL_BG = _active.sel_bg
SEL_FG = _active.sel_fg
TOOLBAR_BG = _active.toolbar_bg
TITLE_ACTIVE = _active.title_active
GRIDLINE = _active.gridline


def active_theme() -> Theme:
    return _active


def base_theme() -> Theme:
    """The active theme before the zoom factor was applied to it."""
    return _base


def ui_scale() -> float:
    """The active zoom factor. 1.0 is the theme at the size it was written."""
    return _scale


def clamp_scale(value: float) -> float:
    """``value`` held inside the zoom range (not snapped onto a stop)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(max(v, SCALE_STEPS[0]), SCALE_STEPS[-1])


def step_scale(delta: int, current: float | None = None) -> float:
    """The stop ``delta`` places along from ``current`` (default: the active zoom).

    ``delta`` is +1 for one step in, -1 for one step out; the ends of the ladder
    hold rather than wrap. A factor that is not itself a stop -- restored from a
    settings file written by another build, or hand-edited -- snaps to the
    nearest one, and the step still moves: one step in from 1.05 is the stop
    *above* 1.05, not the 1.0 it snapped to.
    """
    cur = clamp_scale(_scale if current is None else current)
    i = min(range(len(SCALE_STEPS)), key=lambda n: abs(SCALE_STEPS[n] - cur))
    if delta > 0 and SCALE_STEPS[i] > cur:
        delta -= 1
    elif delta < 0 and SCALE_STEPS[i] < cur:
        delta += 1
    return SCALE_STEPS[min(max(i + delta, 0), len(SCALE_STEPS) - 1)]


def _round(value: float) -> int:
    """Round half away from zero.

    Not :func:`round`, which rounds a half to the *even* number: that makes 7.5
    and 8.5 both come out 8, so a 10pt skin drew the same 8pt face at two
    neighbouring zoom stops and one of them did nothing to the type.
    """
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def _px(value: float, scale: float) -> int:
    """Scale one pixel length. A hairline never rounds away to nothing."""
    if not value:
        return 0
    out = _round(value * scale)
    if value > 0:
        return max(1, out)
    return min(-1, out)


def scale_px(value: float) -> int:
    """A pixel number this module did not publish, at the active zoom.

    For the literals widgets keep for themselves -- the nav strip's maximum
    width, a splitter handle. Anything read off :data:`ROW_HEIGHT`,
    :data:`ICON_SIZE`, :data:`TREE_WIDTH`, :data:`CELL_PAD`, :data:`MARGIN` or
    :data:`SPACING` is already scaled and must not go through here twice.
    """
    return _px(value, _scale)


def scale_window(widget, ratio: float) -> None:
    """Grow or shrink a top-level window by ``ratio`` around its top-left.

    A zoom that leaves the window the same size only changes how much of it the
    chrome fills -- a list that showed thirty rows now shows fifteen. When the
    window is the user's to size (not maximized, not full screen), scaling it
    with the zoom keeps the content-to-window ratio it had, so zooming in makes
    the window bigger rather than the view smaller.

    Clamped to the screen's available area, and nudged back on if a corner ran
    off it, so zooming in never leaves the title bar out of reach. A maximized
    or full-screen window is left exactly alone -- its size is not the user's to
    give, and the zoom just re-fills it.

    ``ratio`` is new-scale / old-scale, not the absolute factor: stepping 1.15
    -> 1.3 grows the window by 1.3/1.15, so the walk up the ladder compounds
    the same way the sizes do.
    """
    if widget is None or ratio == 1.0:
        return
    if widget.isMaximized() or widget.isFullScreen():
        return
    geo = widget.geometry()
    w = max(1, _round(geo.width() * ratio))
    h = max(1, _round(geo.height() * ratio))
    screen = widget.screen()
    if screen is not None:
        avail = screen.availableGeometry()
        # Never ask for more than the screen holds; the frame is a few px more
        # than the client area, so leave a little room for it rather than
        # exactly filling the width and letting the WM shave the difference.
        w = min(w, avail.width())
        h = min(h, avail.height())
    widget.resize(w, h)
    if screen is not None:
        avail = screen.availableGeometry()
        fg = widget.frameGeometry()
        x, y = widget.x(), widget.y()
        if fg.right() > avail.right():
            x -= fg.right() - avail.right()
        if fg.bottom() > avail.bottom():
            y -= fg.bottom() - avail.bottom()
        x = max(avail.left(), x)
        y = max(avail.top(), y)
        if x != widget.x() or y != widget.y():
            widget.move(x, y)


def scale_theme(theme: Theme, scale: float) -> Theme:
    """``theme`` with every size multiplied by ``scale``.

    Sizes only: no colour moves and no flag flips, because a zoom is the same
    skin seen from closer up. Each number is rounded in the unit its theme
    keeps it in -- a 9pt face becomes 10pt, an 11px face becomes 13px -- rather
    than being converted to pixels on the way, so the size the QSS prints, the
    size ``QFont`` is given and the size View ▸ Font quotes are one number.
    """
    if scale == 1.0:
        return theme
    return replace(
        theme,
        # Points stay points. The alternative -- pt -> px so the zoom has one
        # unit to work in -- pins the theme to a 96 DPI reference screen the
        # moment somebody zooms, which is a second, invisible change riding
        # along with the first.
        font_pt=max(6, _round(theme.font_pt * scale)),
        font_px=_px(theme.font_px, scale),
        radius=_px(theme.radius, scale),
        row_height=_px(theme.row_height, scale),
        pad_y=_px(theme.pad_y, scale),
        pad_x=_px(theme.pad_x, scale),
        cell_pad=_px(theme.cell_pad, scale),
        toolbar_pad=_px(theme.toolbar_pad, scale),
        menu_item_pad=_px(theme.menu_item_pad, scale),
        tree_width=_px(theme.tree_width, scale),
        control_min_h=_px(theme.control_min_h, scale),
        icon_px=_px(theme.icon_px, scale),
        # 0 means "whatever the row height implies" and -1 means "line up with
        # the cells"; both are sentinels, not lengths.
        header_height=_px(theme.header_height, scale),
        header_pad=(
            theme.header_pad if theme.header_pad < 0
            else _px(theme.header_pad, scale)
        ),
        font_letter_spacing=theme.font_letter_spacing * scale,
    )


#: Every length in a stylesheet: ``12px``, ``-1px``, ``9pt``, ``10.5pt``.
_CSS_LEN_RE = re.compile(r"(-?\d+(?:\.\d+)?)(px|pt)\b")


def scale_css(css: str, scale: float | None = None) -> str:
    """Multiply every length in ``css`` by the zoom factor.

    Most of the numbers in these stylesheets are not theme metrics: a
    scrollbar's 8px width, a combo arrow's 16px box, a button's 64px minimum,
    the 1px rules between them. A zoom that left those behind would grow the
    type and the rows and nothing else -- a 3px scrollbar beside a 34px row.
    Rewriting the finished sheet reaches all of them without threading a factor
    through six hundred lines of f-string.

    Which is why the sheet is built from :func:`base_theme` and not from the
    scaled one: its own metrics come through here too, and land on the same
    numbers :func:`scale_theme` hands the widgets. Scaling ``rh`` and scaling
    ``rh + 2`` differ only by the scaled 2.
    """
    s = _scale if scale is None else scale
    if s == 1.0:
        return css

    def one(m: "re.Match[str]") -> str:
        value, unit = float(m.group(1)), m.group(2)
        if unit == "px":
            return f"{_px(value, s)}px"
        # Points are rounded whole: Qt's QSS parser is not documented to take a
        # fractional font-size, and a rule it cannot parse is a rule it drops
        # -- which for the `*` font-size is the whole point of the exercise.
        return f"{max(6, _round(value * s))}pt"

    return _CSS_LEN_RE.sub(one, css)


def list_themes() -> list[Theme]:
    """Themes for the View menu (aliases last so primary names show first)."""
    primary = [
        "pro", "pro-dark", "slate", "slate-light",
        "classic", "classic-dense", "modern", "modern-dark", "high-contrast",
    ]
    out: list[Theme] = []
    seen: set[str] = set()
    for tid in primary:
        if tid in THEMES and tid not in seen:
            out.append(THEMES[tid])
            seen.add(tid)
    for t in THEMES.values():
        if t.id not in seen:
            out.append(t)
            seen.add(t.id)
    return out


def style_data_table(table, *, delegate: bool = True) -> None:
    """Give a table Winbox's list chrome.

    Three things separate a Winbox list from Qt's default, and all are set on
    the widget rather than in the stylesheet:

    * **Column rules, no row rules.** Qt's ``showGrid`` draws both axes; Winbox
      rules the columns and leaves the rows alone. Ruling every row as well is
      most of why a dense Qt table reads as busier than the same data in
      Winbox. The grid comes off and :class:`ColumnRuleDelegate` paints the
      vertical hairline instead — see that module for why this cannot be a
      ``QTableView::item`` stylesheet rule.
    * **No alternating stripe.** With column rules doing the work of guiding
      the eye across a row, the stripe is redundant contrast. Winbox has none.
    * **Left-aligned headers.** ``QHeaderView`` centres by default, which
      leaves each label floating away from the column it names while the data
      underneath is left-aligned. This is only the fallback: a model that
      answers ``TextAlignmentRole`` for a section wins, which is how
      :class:`~x3.ui.models.table_model.SimpleTableModel` swings the
      heading of a numeric column over to the right with its values.

    Safe to call before the table has any columns. Pass ``delegate=False`` for
    a table that installs its own (it should subclass ColumnRuleDelegate).
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont

    from .widgets.column_rule import ColumnRuleDelegate

    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    # styleStrategy does not survive QApplication.setFont (see
    # apply_font_traits), so the table is given the theme's font itself. This is
    # the widget the skin is actually judged on, and the only one where
    # grayscale-vs-subpixel is worth a round trip.
    table.setFont(apply_font_traits(QFont(table.font())))
    if delegate:
        table.setItemDelegate(ColumnRuleDelegate(table))

    hdr = table.horizontalHeader()
    hdr.setDefaultAlignment(
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    # A theme that names a header height means it. The sheet's `height` is
    # only a floor: Qt sizes a header from its label plus its margins, which at
    # 1.3x came to 22px over 20px rows -- the space under the label that was
    # asked away. Pinned, the label is centred in the height the theme gives.
    if _active.header_height:
        hdr.setFixedHeight(int(_active.header_height))
    else:
        hdr.setMinimumHeight(0)
        hdr.setMaximumHeight(16777215)
    # setDefaultAlignment only reaches headers a *model* supplies. QTableWidget
    # keeps real items for them, and those carry their own centred alignment.
    if hasattr(table, "horizontalHeaderItem"):
        for col in range(table.columnCount()):
            item = table.horizontalHeaderItem(col)
            if item is not None:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )


def get_theme(theme_id: str | None) -> Theme:
    if theme_id and theme_id in THEMES:
        return THEMES[theme_id]
    return THEMES[DEFAULT_THEME_ID]


def _info_red(theme: Theme) -> str:
    """Red for device status lines, legible on this theme's row background."""
    from PySide6.QtGui import QColor

    return "#ff6b6b" if QColor(theme.bg).lightnessF() < 0.5 else "#c00000"


def _mark_up(theme: Theme) -> str:
    """Green for a running dropdown entry, legible on this theme's ground."""
    from PySide6.QtGui import QColor

    return "#5cc85c" if QColor(theme.bg).lightnessF() < 0.5 else "#2e7d32"


def _link(theme: Theme) -> str:
    """Blue for a clickable cell, legible on this theme's ground."""
    from PySide6.QtGui import QColor

    return "#8ab4ff" if QColor(theme.bg).lightnessF() < 0.5 else "#0000aa"


def _publish(theme: Theme, base: Theme | None = None, scale: float = 1.0) -> None:
    """Mirror palette + density onto module globals for ad-hoc stylesheets.

    ``theme`` is already scaled; ``base`` is the same theme unscaled (see
    :data:`_base` for who needs which).
    """
    global _active, _base, _scale
    global FONT_PT, BG, BG_PANEL, BG_WINDOW, BG_ALT, BG_HEADER
    global BORDER, BORDER_LIGHT, BORDER_DARK, TEXT, TEXT_DIM, INFO_RED
    global MARK_UP, LINK
    global SEL_BG, SEL_FG, TOOLBAR_BG, TITLE_ACTIVE, ROW_HEIGHT, TREE_WIDTH
    global MARGIN, SPACING, MDI_BG, ICON_SIZE, TOOLBAR_ICON, GRIDLINE
    global CELL_PAD, FONT_PX, COLUMN_RULES, MDI_BORDER
    _active = theme
    _base = theme if base is None else base
    _scale = scale
    FONT_PT = theme.font_pt
    FONT_PX = theme.font_px
    BG = theme.bg
    BG_PANEL = theme.bg_panel
    BG_WINDOW = theme.bg_window
    BG_ALT = theme.bg_alt
    BG_HEADER = theme.bg_header
    BORDER = theme.border
    BORDER_LIGHT = theme.border_light
    BORDER_DARK = theme.border_dark
    TEXT = theme.text
    TEXT_DIM = theme.text_dim
    INFO_RED = _info_red(theme)
    MARK_UP = _mark_up(theme)
    LINK = _link(theme)
    SEL_BG = theme.sel_bg
    SEL_FG = theme.sel_fg
    TOOLBAR_BG = theme.toolbar_bg
    TITLE_ACTIVE = theme.title_active
    GRIDLINE = theme.gridline
    ROW_HEIGHT = theme.row_height
    CELL_PAD = theme.cell_pad
    COLUMN_RULES = theme.column_rules
    MDI_BORDER = theme.mdi_border
    TREE_WIDTH = theme.tree_width
    MDI_BG = theme.mdi_bg
    ICON_SIZE = theme.icon_px
    TOOLBAR_ICON = theme.icon_px
    # Density from the unscaled skin, then scaled -- not read off the scaled
    # numbers, which would put a zoomed compact skin in the roomy branch.
    MARGIN = _px(2 if theme.modern else 4, scale)
    SPACING = _px(1 if theme.modern and _base.row_height <= 17 else 3, scale)


#: Points-to-pixels at the 96 DPI the Windows look was drawn for. A theme that
#: pins pixels is pinned to that reference screen by definition, so secondary
#: sizes quoted in points convert against it rather than the live DPI.
_PT_TO_PX = 96.0 / 72.0


def size_css(theme: Theme | None = None) -> str:
    """The UI text size as a CSS length: ``"11px"`` or ``"10pt"``.

    Both stylesheets set this on ``*``, which is what actually sizes the type
    in a list -- the application font only reaches widgets no rule touches.
    """
    t = theme or _active
    return f"{t.font_px}px" if t.font_px else f"{t.font_pt}pt"


def small_css(pt: float = 9.0) -> str:
    """A secondary label's size: ``pt`` points, but never above the body text.

    Status lines and hints around the app quote a fixed 9pt, which read as
    "a bit smaller" back when every theme was 9pt or more. Under an 11px body
    a 9pt label is a pixel *taller* than the text it annotates, so the quoted
    size is treated as a ceiling and clamped. Themes at 9pt and up are
    unaffected -- they already were the smaller of the two.

    The quoted point size is scaled by the zoom, because these are set inline
    on individual widgets and so never pass through :func:`scale_css` the way
    the app stylesheet does. Without it a status line stayed ~12px while the
    body around it grew to 22, which reads as a different, tiny typeface rather
    than "a bit smaller". The clamp is against the *scaled* body (``_active``
    is the zoomed theme), so the "never above the body" promise still holds.
    """
    t = _active
    pt = pt * _scale
    if t.font_px:
        return f"{min(t.font_px, _round(pt * _PT_TO_PX))}px"
    return f"{min(float(t.font_pt), pt):g}pt"


def size_font(font, *, delta_px: int = 0):
    """Size ``font`` like the UI text, ``delta_px`` pixels off. Returns it.

    For the fonts built by hand rather than inherited -- the terminal's and the
    log's monospace, a flyout's small close button. Reading the base size off
    the font itself is the trap: ``QFont.pointSize()`` answers **-1** on a
    pixel-sized font, so "one step smaller than what I have" silently became
    "as small as allowed" the moment a theme pinned pixels. The theme is asked
    instead, in whichever unit it keeps.
    """
    t = _active
    if t.font_px:
        font.setPixelSize(max(7, t.font_px + delta_px))
    else:
        font.setPointSizeF(max(6.0, t.font_pt + delta_px * 0.75))
    return font


def header_pad(theme: Theme | None = None) -> int:
    """Header label inset in px; -1 means line up with the cells below."""
    t = theme or _active
    return int(t.cell_pad if t.header_pad < 0 else t.header_pad)


def style_strategy(theme: Theme | None = None):
    """``theme.font_antialias`` as a :class:`QFont.StyleStrategy`."""
    from PySide6.QtGui import QFont

    t = theme or _active
    return {
        "nosubpixel": QFont.StyleStrategy.NoSubpixelAntialias,
        "prefer": QFont.StyleStrategy.PreferAntialias,
        "none": QFont.StyleStrategy.NoAntialias,
    }.get(t.font_antialias, QFont.StyleStrategy.PreferDefault)


def apply_font_traits(font, theme: Theme | None = None):
    """Put a theme's size, hinting, weight, tracking and AA onto ``font``.

    One place, because these travel together: a widget that builds its own font
    and sets only the size gets the skin's type at the wrong weight, and the
    mismatch is the kind nobody spots until it is side by side with the rest.

    **Every trait is set unconditionally, including the ones a theme leaves at
    its default.** A default-constructed ``QFont`` starts from the *application*
    font, so skipping a trait does not mean "Qt's default" — it means "whatever
    the previously applied theme left there". Guarding these on truthiness made
    weight and tracking stick across a theme switch: once one skin asked for
    700, every skin after it drew bold.

    One trait cannot be delivered this way at all. ``QApplication.setFont``
    does not carry ``styleStrategy`` — it is absent from QFont's resolve mask,
    so an app font asking for grayscale antialiasing reads back as
    ``PreferDefault``. Widgets that care set it themselves; for the data tables
    that is :func:`style_data_table`.
    """
    from PySide6.QtGui import QFont

    t = theme or _active
    if t.font_px:
        font.setPixelSize(t.font_px)
    else:
        font.setPointSize(t.font_pt)
    font.setHintingPreference(hinting_preference(t))
    font.setStyleStrategy(style_strategy(t))
    font.setWeight(QFont.Weight(t.font_weight or QFont.Weight.Normal.value))
    font.setLetterSpacing(
        QFont.SpacingType.AbsoluteSpacing, t.font_letter_spacing
    )
    return font


def ui_pixel_size() -> int:
    """The UI font's em box in pixels, whichever unit the theme keeps it in.

    A point-sized theme only becomes pixels once a screen's DPI is known, so
    the live application font is measured rather than guessed. For anything
    comparing itself against Winbox's pixel-specified type, this is the number
    to compare against.
    """
    if _active.font_px:
        return _active.font_px
    from PySide6.QtGui import QFontInfo
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return int(round(_active.font_pt * _PT_TO_PX))
    return QFontInfo(app.font()).pixelSize()


def hinting_preference(theme: Theme | None = None):
    """``theme.font_hinting`` as a :class:`QFont.HintingPreference`."""
    from PySide6.QtGui import QFont

    t = theme or _active
    return {
        "full": QFont.HintingPreference.PreferFullHinting,
        "vertical": QFont.HintingPreference.PreferVerticalHinting,
        "none": QFont.HintingPreference.PreferNoHinting,
    }.get(t.font_hinting, QFont.HintingPreference.PreferDefaultHinting)


def stylesheet_for(theme: Theme) -> str:
    """Build the full app QSS for ``theme``."""
    if theme.modern:
        return _modern_stylesheet(theme)
    return _classic_stylesheet(theme)


def _classic_stylesheet(t: Theme) -> str:
    """Winbox-v3-ish: dense, bevelled, rectangular."""
    rh = t.row_height
    cpad = int(t.cell_pad)
    hpad = header_pad(t)
    hh = t.header_height or (rh + 2)
    return f"""
    * {{
        font-size: {size_css(t)};
    }}
    QMainWindow, QDialog, QWidget {{
        background-color: {t.bg};
        color: {t.text};
    }}
    QMenuBar {{
        background-color: {t.bg};
        border-bottom: 1px solid {t.border};
        padding: 0px;
        spacing: 0px;
    }}
    QMenuBar::item {{
        padding: 2px 6px;
        background: transparent;
    }}
    QMenuBar::item:selected {{
        background: {t.sel_bg};
        color: {t.sel_fg};
    }}
    QMenu {{
        background-color: {t.bg_panel};
        border: 1px solid {t.border};
        padding: 1px;
    }}
    QMenu::item {{
        padding: 2px 18px 2px 20px;
    }}
    QMenu::item:selected {{
        background: {t.sel_bg};
        color: {t.sel_fg};
    }}
    QToolBar {{
        background: {t.toolbar_bg};
        border: none;
        border-bottom: 1px solid {t.border};
        spacing: 1px;
        padding: 1px 2px;
    }}
    QToolBar::separator {{
        background: {t.border};
        width: 1px;
        margin: 2px 3px;
    }}
    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        padding: 1px 3px;
        margin: 0px;
    }}
    QToolButton:hover {{
        background: {t.bg_panel};
        border: 1px solid {t.border};
    }}
    QToolButton:pressed {{
        background: {t.bg_header};
        border: 1px solid {t.border_dark};
    }}
    QStatusBar {{
        background: {t.bg};
        border-top: 1px solid {t.border_light};
        color: {t.text_dim};
    }}
    QStatusBar::item {{
        border: none;
    }}
    QSplitter::handle {{
        background: {t.bg};
        width: 3px;
    }}
    QTreeWidget, QTreeView, QListWidget {{
        background: {t.bg_window};
        border: 1px solid {t.border};
        outline: none;
        show-decoration-selected: 1;
        alternate-background-color: {t.bg_alt};
        color: {t.text};
    }}
    QTreeWidget::item, QTreeView::item, QListWidget::item {{
        height: {rh}px;
        padding: 0px 2px;
    }}
    QTreeWidget::item:selected, QTreeView::item:selected, QListWidget::item:selected {{
        background: {t.sel_bg};
        color: {t.sel_fg};
    }}
    QTableView {{
        background: {t.bg_window};
        alternate-background-color: {t.bg_alt};
        border: 1px solid {t.border};
        gridline-color: {t.gridline};
        selection-background-color: {t.sel_bg};
        selection-color: {t.sel_fg};
        outline: none;
        color: {t.text};
    }}
    QHeaderView::section {{
        background: {t.bg_header};
        color: {t.text};
        border: 1px solid {t.border};
        border-left: none;
        border-top: none;
        /* Same inset the cells below use, so a label sits over its column. */
        padding: 1px {hpad}px;
        font-weight: normal;
        height: {hh}px;
    }}
    QHeaderView::section:first {{
        border-left: 1px solid {t.border};
    }}
    QHeaderView::section:last, QHeaderView::section:only-one {{
        border-right: none;
    }}
    QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit {{
        background: {t.bg_window};
        color: {t.text};
        border: 1px solid {t.border};
        padding: 1px 3px;
        selection-background-color: {t.sel_bg};
        selection-color: {t.sel_fg};
    }}
    QComboBox {{
        padding-right: 18px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 16px;
        border: none;
        border-left: 1px solid {t.border};
        background: {t.bg_panel};
    }}
    QComboBox::drop-down:hover {{
        background: {t.button_hover};
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0;
        height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {t.text};
        margin-right: 4px;
    }}
    QPushButton {{
        background: {t.bg_panel};
        color: {t.text};
        border: 1px solid {t.border_dark};
        border-top-color: {t.border_light};
        border-left-color: {t.border_light};
        padding: 3px 12px;
        min-height: 20px;
        min-width: 64px;
    }}
    QPushButton:hover {{
        background: {t.button_hover};
    }}
    QPushButton:pressed {{
        border: 1px solid {t.border_dark};
        background: {t.bg_header};
    }}
    QPushButton:default {{
        border: 2px solid {t.sel_bg};
        padding: 2px 11px;
    }}
    QCheckBox {{
        spacing: 4px;
        color: {t.text};
    }}
    /* Greyed out has to look greyed out. Every rule above names a colour, and
       a stylesheet colour beats the palette's disabled role -- so a disabled
       Start or Stop was drawn exactly like an enabled one, in every window. */
    QPushButton:disabled, QPushButton:default:disabled {{
        color: {t.text_dim};
        background: {t.bg};
        border: 1px solid {t.border};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled,
    QTextEdit:disabled, QPlainTextEdit:disabled {{
        color: {t.text_dim};
        background: {t.bg};
    }}
    QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled {{
        color: {t.text_dim};
    }}
    QTabWidget::pane {{
        border: 1px solid {t.border};
        background: {t.bg_panel};
        top: -1px;
    }}
    QTabBar::tab {{
        background: {t.bg};
        color: {t.text};
        border: 1px solid {t.border};
        border-bottom: none;
        padding: 2px 8px;
        margin-right: 1px;
        min-width: 40px;
    }}
    QTabBar::tab:selected {{
        background: {t.bg_panel};
        margin-bottom: -1px;
        padding-bottom: 3px;
    }}
    QTabBar::tab:!selected {{
        margin-top: 2px;
    }}
    QGroupBox {{
        border: 1px solid {t.border};
        margin-top: 8px;
        padding-top: 6px;
        font-weight: normal;
        color: {t.text};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 6px;
        padding: 0 3px;
    }}
    QMdiArea {{
        background: {t.mdi_bg};
    }}
    QScrollBar:vertical {{
        background: {t.bg_panel};
        width: 12px;
        margin: 0px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.scrollbar};
        min-height: 16px;
        border: 1px solid {t.border};
    }}
    QScrollBar:horizontal {{
        background: {t.bg_panel};
        height: 12px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.scrollbar};
        min-width: 16px;
        border: 1px solid {t.border};
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0px;
        height: 0px;
    }}
    QLabel {{
        color: {t.text};
    }}
    QLabel#toolbarStat {{
        color: {t.text_dim};
        padding: 0 6px;
    }}
    QFrame#connectFrame {{
        background: {t.bg_panel};
        border: 1px solid {t.border};
    }}
    QFrame#navFlyout {{
        background: {t.bg_panel};
        border: 1px solid {t.border_dark};
    }}
    /* Left nav: chrome, not a data well. A white field inside a 1px border
       read as a sunken hole next to the toolbar; the frame grey with no
       border sits it on top, flush with the splitter and the status bar. */
    QTreeWidget#navTree {{
        background: {t.bg};
        border: none;
    }}
    """



def _darken(hex_color: str, factor: float = 0.85) -> str:
    """Scale a #rrggbb colour toward black (factor<1) for hover shades."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        rr, gg, bb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    rr = max(0, min(255, int(rr * factor)))
    gg = max(0, min(255, int(gg * factor)))
    bb = max(0, min(255, int(bb * factor)))
    return f"#{rr:02x}{gg:02x}{bb:02x}"


def _modern_stylesheet(t: Theme) -> str:
    """Flat Fusion chrome; density driven by theme pad/row metrics."""
    r = max(0, int(t.radius))
    sel_hover = _darken(t.sel_bg, 0.85)
    rh = int(t.row_height)
    py = int(t.pad_y)
    px = int(t.pad_x)
    cpad = int(t.cell_pad)
    hpad = header_pad(t)
    hh = t.header_height or (rh + 2)
    htop = 1 if hh <= rh else 0
    tpad = int(t.toolbar_pad)
    mpad = int(t.menu_item_pad)
    ch = int(t.control_min_h)
    return f"""
    * {{
        font-size: {size_css(t)};
    }}
    QMainWindow, QDialog, QWidget {{
        background-color: {t.bg};
        color: {t.text};
    }}
    QMenuBar {{
        background-color: {t.bg_panel};
        border-bottom: 1px solid {t.border};
        padding: 0px {tpad}px;
        spacing: 0px;
    }}
    QMenuBar::item {{
        padding: {mpad}px {px + 2}px;
        border-radius: {r}px;
        background: transparent;
    }}
    QMenuBar::item:selected {{
        background: {t.sel_bg};
        color: {t.sel_fg};
    }}
    QMenu {{
        background-color: {t.bg_panel};
        border: 1px solid {t.border};
        border-radius: {r}px;
        padding: 2px;
    }}
    QMenu::item {{
        padding: {mpad}px {px + 10}px {mpad}px {px + 6}px;
        border-radius: {max(0, r - 1)}px;
    }}
    QMenu::item:selected {{
        background: {t.sel_bg};
        color: {t.sel_fg};
    }}
    QToolBar {{
        background: {t.toolbar_bg};
        border: none;
        border-bottom: 1px solid {t.border};
        spacing: 1px;
        padding: {tpad}px {px}px;
    }}
    QToolBar::separator {{
        background: {t.border};
        width: 1px;
        margin: 2px 3px;
    }}
    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {r}px;
        padding: {py}px {px}px;
        margin: 0px;
    }}
    QToolButton:hover {{
        background: {t.button_hover};
        border: 1px solid {t.border};
    }}
    QToolButton:pressed {{
        background: {t.bg_header};
        border: 1px solid {t.border};
    }}
    QStatusBar {{
        background: {t.bg_panel};
        border-top: 1px solid {t.border};
        color: {t.text_dim};
        padding: 0px;
        min-height: {rh + 2}px;
    }}
    QStatusBar::item {{
        border: none;
    }}
    QSplitter::handle {{
        background: {t.bg};
        width: 3px;
    }}
    QTreeWidget, QTreeView, QListWidget {{
        background: {t.bg_window};
        border: 1px solid {t.border};
        border-radius: {r}px;
        outline: none;
        show-decoration-selected: 1;
        alternate-background-color: {t.bg_alt};
        color: {t.text};
        padding: 0px;
    }}
    QTreeWidget::item, QTreeView::item, QListWidget::item {{
        height: {rh}px;
        padding: 0px {px}px;
        border-radius: 0px;
    }}
    QTreeWidget::item:selected, QTreeView::item:selected, QListWidget::item:selected {{
        background: {t.sel_bg};
        color: {t.sel_fg};
    }}
    QTreeWidget::item:hover:!selected, QTreeView::item:hover:!selected,
    QListWidget::item:hover:!selected {{
        background: {t.bg_alt};
    }}
    QTableView {{
        background: {t.bg_window};
        alternate-background-color: {t.bg_alt};
        border: 1px solid {t.border};
        border-radius: {r}px;
        gridline-color: {t.gridline};
        selection-background-color: {t.sel_bg};
        selection-color: {t.sel_fg};
        outline: none;
        color: {t.text};
    }}
    QHeaderView::section {{
        background: {t.bg_header};
        color: {t.text_dim};
        border: none;
        /* Hairline column rule, solid rule under the whole header. Boxing each
           cell on every side is what made these read as buttons. */
        border-right: 1px solid {t.gridline};
        border-bottom: 1px solid {t.border};
        /* Same inset the cells below use, so a label sits over its column.
           A header no taller than a row takes its one spare pixel on top:
           the label's descent already leaves room underneath. */
        padding: {htop}px {hpad}px 0px {hpad}px;
        font-weight: 600;
        height: {hh}px;
    }}
    QHeaderView::section:last, QHeaderView::section:only-one {{
        border-right: none;
    }}
    /* Column rules continue down through the data (Winbox draws these); the
       row rules stay off, which is what showGrid=False in style_data_table
       buys us. */
    QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit {{
        background: {t.bg_window};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {r}px;
        padding: {py}px {px}px;
        selection-background-color: {t.sel_bg};
        selection-color: {t.sel_fg};
        min-height: {ch}px;
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
        border: 1px solid {t.sel_bg};
    }}
    QComboBox {{
        padding-right: 18px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 16px;
        border: none;
        border-left: 1px solid {t.border};
        border-top-right-radius: {r}px;
        border-bottom-right-radius: {r}px;
        background: {t.bg_header};
    }}
    QComboBox::drop-down:hover {{
        background: {t.button_hover};
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0;
        height: 0;
        border-left: 3px solid transparent;
        border-right: 3px solid transparent;
        border-top: 4px solid {t.text_dim};
        margin-right: 4px;
    }}
    QComboBox QAbstractItemView {{
        background: {t.bg_panel};
        border: 1px solid {t.border};
        selection-background-color: {t.sel_bg};
        selection-color: {t.sel_fg};
        outline: none;
        padding: 0px;
    }}
    QPushButton {{
        background: {t.bg_panel};
        color: {t.text};
        border: 1px solid {t.border};
        border-radius: {r}px;
        padding: {py + 1}px {px + 6}px;
        min-height: {ch}px;
        min-width: 56px;
    }}
    QPushButton:hover {{
        background: {t.button_hover};
        border-color: {t.border_dark};
    }}
    QPushButton:pressed {{
        background: {t.bg_header};
    }}
    QPushButton:default {{
        background: {t.sel_bg};
        color: {t.sel_fg};
        border: 1px solid {t.sel_bg};
    }}
    QPushButton:default:hover {{
        background: {sel_hover};
        color: {t.sel_fg};
        border-color: {sel_hover};
    }}
    QCheckBox {{
        spacing: 4px;
        color: {t.text};
    }}
    /* Greyed out has to look greyed out. Every rule above names a colour, and
       a stylesheet colour beats the palette's disabled role -- so a disabled
       Start or Stop was drawn exactly like an enabled one, in every window. */
    QPushButton:disabled, QPushButton:default:disabled {{
        color: {t.text_dim};
        background: {t.bg};
        border: 1px solid {t.border};
    }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled,
    QTextEdit:disabled, QPlainTextEdit:disabled {{
        color: {t.text_dim};
        background: {t.bg};
    }}
    QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled {{
        color: {t.text_dim};
    }}
    QTabWidget::pane {{
        border: 1px solid {t.border};
        border-radius: {r}px;
        background: {t.bg_panel};
        top: -1px;
        padding: 2px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {t.text_dim};
        border: none;
        border-bottom: 2px solid transparent;
        padding: {py + 1}px {px + 4}px;
        margin-right: 1px;
        min-width: 36px;
    }}
    QTabBar::tab:selected {{
        color: {t.sel_bg};
        border-bottom: 2px solid {t.sel_bg};
        background: transparent;
        margin-bottom: 0px;
    }}
    QTabBar::tab:hover:!selected {{
        color: {t.text};
        background: {t.bg_alt};
    }}
    QGroupBox {{
        border: 1px solid {t.border};
        border-radius: {r}px;
        margin-top: 8px;
        padding-top: 6px;
        font-weight: 600;
        color: {t.text};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 8px;
        padding: 0 3px;
        color: {t.text_dim};
    }}
    QMdiArea {{
        background: {t.mdi_bg};
    }}
    /* Active title uses palette Highlight (set in apply_theme) — keep blue, not orange. */
    /* Resizing is hit-tested against PM_MdiSubWindowFrameWidth, and
       QStyleSheetStyle derives that from this rule's box — "border: none" made
       it 0 and silently killed edge/corner resize. So the frameless skins
       declare no border and let the proxy style supply a 4px grab band.

       QMdiSubWindow always insets its child by the frame width, so that band
       has to be painted something; there is no way to turn it inward and have
       the child cover it. In the window's own colour it shows up as a pale
       outline against the darker MDI backdrop — the very border we are trying
       to lose. Painted in the backdrop colour it disappears, which is what we
       want; it is the only place this colour shows, since the child fills the
       rest. Trade-off: overlapping windows carry a 4px backdrop-coloured edge. */
    QMdiSubWindow {{
        background: {t.mdi_bg if not t.mdi_frame else t.bg_panel};
        color: {t.text};
        {f"border: 1px solid {t.border};" if t.mdi_frame else ""}
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 8px;
        margin: 1px;
    }}
    QScrollBar::handle:vertical {{
        background: {t.scrollbar};
        min-height: 20px;
        border-radius: 4px;
        border: none;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t.border_dark};
    }}
    QScrollBar:horizontal {{
        background: transparent;
        height: 8px;
        margin: 1px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t.scrollbar};
        min-width: 20px;
        border-radius: 4px;
        border: none;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0px;
        height: 0px;
    }}
    QLabel {{
        color: {t.text};
    }}
    QLabel#toolbarStat {{
        color: {t.text_dim};
        padding: 0 {px}px;
    }}
    QFrame#connectFrame {{
        background: {t.bg_panel};
        border: 1px solid {t.border};
        border-radius: {r}px;
    }}
    QFrame#navFlyout {{
        background: {t.bg_panel};
        border: 1px solid {t.border};
        border-radius: {r}px;
    }}
    /* Left nav: chrome, not a data well -- see the classic sheet. Hover moves
       to bg_panel because bg_alt is a shade of the *window* white and all but
       vanishes against the frame grey. */
    QTreeWidget#navTree {{
        background: {t.bg};
        border: none;
        border-radius: 0px;
    }}
    QTreeWidget#navTree::item:hover:!selected {{
        background: {t.bg_panel};
    }}
    """


def app_stylesheet() -> str:
    """The whole app's QSS: the active theme, at the active zoom.

    Built from the unscaled theme and scaled afterwards, which is the only way
    the sheet's own hard-coded lengths move with the rest -- see
    :func:`scale_css`.
    """
    return scale_css(stylesheet_for(_base), _scale)


def dense_stylesheet() -> str:
    """Stylesheet for the currently active theme (backward-compatible name)."""
    return app_stylesheet()


class _DenseMdiStyle:
    """Proxy style for modern skins: thin, flat MDI subwindow title bars.

    Fusion paints MDI titles as a rounded, bevelled gradient — a distinctly
    mid-2000s look. For the modern themes we repaint the whole title bar as a
    flat strip with monochrome glyph buttons instead.
    """

    def __init__(self, base_style, title_h: int = 18, theme: "Theme | None" = None) -> None:
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QColor, QPalette, QPen
        from PySide6.QtWidgets import QProxyStyle, QStyle

        t = theme or _active

        # Every override here is on a hot path: this is the *application*
        # style, so each one is a C++ -> Python -> C++ round trip on every
        # query Qt makes while painting anything. Painting a 40-row list asks
        # for PM_FocusFrameHMargin 1200 times -- twice per cell, from Qt's own
        # item painting -- so the metrics are answered from ints and a small
        # cache rather than by walking enums down to the base style.
        PM_TITLE_H = int(QStyle.PixelMetric.PM_TitleBarHeight)
        PM_MDI_FRAME = int(QStyle.PixelMetric.PM_MdiSubWindowFrameWidth)
        PM_MDI_MIN_W = int(QStyle.PixelMetric.PM_MdiSubWindowMinimizedWidth)
        # The cell inset, answered here rather than worked around per cell.
        # Qt's item painting insets text by PM_FocusFrameHMargin + 1 and there
        # is no way to style it; answering the metric ourselves is what makes
        # a cell as tight as its header, and it costs nothing at paint time --
        # ColumnRuleDelegate then finds nothing left to reclaim and stands
        # aside. One less than CELL_PAD, because Qt adds that 1 back.
        PM_FOCUS_H = int(QStyle.PixelMetric.PM_FocusFrameHMargin)
        CELL_INSET = max(0, int(t.cell_pad) - 1)
        # Answered from ints for the same reason, so the zoom has to be baked
        # in here rather than read per query: a resize band that stayed 4px
        # while the window it belongs to doubled is a band you cannot grab.
        MDI_FRAME_W = scale_px(1 if t.mdi_frame else 4)
        MDI_MIN_W = scale_px(120)
        # Checkbox / radio indicators: a style constant (~13px in Fusion) that
        # the sheet never overrides, so it is the one thing that does not grow
        # with the zoom. scale_px reads the active factor (set by _publish
        # before this proxy is built), so at 100% these stay Fusion's default.
        _ind = QStyle.PixelMetric
        INDICATORS = {
            int(m): scale_px(base_style.pixelMetric(m))
            for m in (_ind.PM_IndicatorWidth, _ind.PM_IndicatorHeight,
                      _ind.PM_ExclusiveIndicatorWidth,
                      _ind.PM_ExclusiveIndicatorHeight)
        }
        # Fusion answers this one with a constant -- it looks at neither the
        # option nor the widget -- and an item view asks for it twice a cell.
        PM_CACHEABLE = frozenset((
            int(QStyle.PixelMetric.PM_FocusFrameVMargin),
        ))
        # Fusion outlines a checkbox or radio button in the window colour
        # darkened by 40%: a hairline on a light skin, and nothing at all on a
        # dark one, where the unchecked boxes on a form simply vanished. Those
        # two primitives get a mid-grey "window" to darken instead. Item views
        # draw their check boxes through PE_IndicatorCheckBox too.
        INDICATORS_PE = (
            QStyle.PrimitiveElement.PE_IndicatorCheckBox,
            QStyle.PrimitiveElement.PE_IndicatorRadioButton,
        )
        INDICATOR_WINDOW = (
            QColor(t.text_dim) if QColor(t.bg).lightnessF() < 0.5 else None
        )
        SH_FRAME_MASK = int(QStyle.StyleHint.SH_WindowFrame_Mask)
        SH_FILL_MAXIMIZE = int(QStyle.StyleHint.SH_Workspace_FillSpaceOnMaximize)
        PE_FRAME_WINDOW = int(QStyle.PrimitiveElement.PE_FrameWindow)

        # Build a real QProxyStyle subclass instance dynamically so we keep
        # style.py importable without Qt until apply_theme runs.
        class _Impl(QProxyStyle):
            def __init__(self_inner, base, height: int) -> None:
                super().__init__(base)
                self_inner._title_h = height
                self_inner._pm_cache: dict[int, int] = {}

            def pixelMetric(self_inner, metric, option=None, widget=None):  # noqa: N802
                # `key` for the comparisons, `metric` itself for anything
                # handed back to Qt: the enum is what the base style's
                # signature wants.
                key = int(metric)
                if key == PM_FOCUS_H:
                    return CELL_INSET
                if key in PM_CACHEABLE:
                    hit = self_inner._pm_cache.get(key)
                    if hit is None:
                        hit = super().pixelMetric(metric, option, widget)
                        self_inner._pm_cache[key] = hit
                    return hit
                if key == PM_TITLE_H:
                    return self_inner._title_h
                if key == PM_MDI_FRAME:
                    # This is the grab band QMdiSubWindow hit-tests for resizing,
                    # so it must stay non-zero even when we draw no frame —
                    # returning 0 silently killed edge and corner resizing.
                    return MDI_FRAME_W
                if key == PM_MDI_MIN_W:
                    return MDI_MIN_W
                ind = INDICATORS.get(key)
                if ind is not None:
                    return ind
                return super().pixelMetric(metric, option, widget)

            def styleHint(self_inner, hint, option=None, widget=None, returnData=None):  # noqa: N802
                # Fusion answers this with a rounded region and QMdiSubWindow
                # masks itself with it, which is what nicks the title bar's top
                # corners — our own painting is a plain fillRect. Answering 0
                # leaves the window unmasked, i.e. square.
                if hint == SH_FRAME_MASK and not t.mdi_round:
                    return 0
                # Keep a maximized subwindow's own title bar. Answering 0 here
                # (Fusion's answer) makes QMdiSubWindow drop the bar entirely on
                # maximize and re-home the min/restore/close buttons into the
                # main window's menu bar -- Windows-3.1 MDI. That leaves nothing
                # to double-click to come back, strands any control floated into
                # the bar (a tab strip, QuickSet's mode combo) on top of the
                # window's content, and puts window buttons in the menu bar.
                if hint == SH_FILL_MAXIMIZE:
                    return 1
                return super().styleHint(hint, option, widget, returnData)

            if INDICATOR_WINDOW is not None:
                # Only built for a dark skin, so a light one pays nothing for
                # it on the paint path.
                def drawPrimitive(  # noqa: N802
                    self_inner, element, option, painter, widget=None
                ):
                    if element in INDICATORS_PE and option is not None:
                        opt = type(option)(option)
                        opt.palette.setColor(QPalette.ColorRole.Window, INDICATOR_WINDOW)
                        super().drawPrimitive(element, opt, painter, widget)
                        return
                    super().drawPrimitive(element, option, painter, widget)

            def drawComplexControl(  # noqa: N802
                self_inner, control, option, painter, widget=None
            ):
                if control != QStyle.ComplexControl.CC_TitleBar:
                    super().drawComplexControl(control, option, painter, widget)
                    return
                self_inner._draw_flat_title(option, painter, widget)

            # --- flat title bar ------------------------------------------

            def _draw_flat_title(self_inner, opt, p, widget) -> None:
                active = bool(opt.titleBarState & QStyle.StateFlag.State_Active.value)
                bg = QColor(t.title_active if active else t.bg_header)
                if active:
                    # Some themes use a light accent; pick the readable ink.
                    fg = QColor(t.sel_fg) if bg.lightnessF() < 0.6 else QColor("#0b1220")
                else:
                    fg = QColor(t.text_dim)

                p.save()
                p.setPen(Qt.PenStyle.NoPen)
                p.fillRect(opt.rect, bg)
                p.setPen(QPen(QColor(t.border), 1))
                p.drawLine(
                    opt.rect.left(), opt.rect.bottom(),
                    opt.rect.right(), opt.rect.bottom(),
                )

                sc = QStyle.SubControl
                icon_r = self_inner.subControlRect(
                    QStyle.ComplexControl.CC_TitleBar, opt, sc.SC_TitleBarSysMenu, widget
                )
                if not opt.icon.isNull() and icon_r.isValid() and icon_r.width() > 2:
                    size = min(icon_r.width(), icon_r.height(), 16)
                    target = QRect(0, 0, size, size)
                    target.moveCenter(icon_r.center())
                    opt.icon.paint(p, target)

                label_r = self_inner.subControlRect(
                    QStyle.ComplexControl.CC_TitleBar, opt, sc.SC_TitleBarLabel, widget
                )
                if label_r.isValid() and opt.text:
                    label_r = label_r.adjusted(5, 0, -4, 0)
                    p.setPen(fg)
                    fm = p.fontMetrics()
                    text = fm.elidedText(
                        opt.text, Qt.TextElideMode.ElideRight, label_r.width()
                    )
                    p.drawText(
                        label_r,
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                        text,
                    )

                buttons = (
                    (sc.SC_TitleBarMinButton, "min"),
                    (sc.SC_TitleBarNormalButton, "restore"),
                    (sc.SC_TitleBarMaxButton, "max"),
                    (sc.SC_TitleBarCloseButton, "close"),
                )
                hovered = opt.activeSubControls
                sunken = bool(opt.state & QStyle.StateFlag.State_Sunken)
                for subcontrol, glyph in buttons:
                    r = self_inner.subControlRect(
                        QStyle.ComplexControl.CC_TitleBar, opt, subcontrol, widget
                    )
                    if not r.isValid() or r.width() < 3 or r.height() < 3:
                        continue
                    is_hot = bool(hovered & subcontrol)
                    if is_hot:
                        if glyph == "close":
                            hot = QColor("#e81123")
                        else:
                            hot = QColor(255, 255, 255, 46) if active else QColor(t.button_hover)
                        if sunken:
                            hot = hot.darker(115)
                        p.fillRect(r, hot)
                    gp = QColor("#ffffff") if (is_hot and glyph == "close") else fg
                    p.setPen(QPen(gp, 1))
                    self_inner._draw_glyph(p, r, glyph)
                p.restore()

            @staticmethod
            def _draw_glyph(p, r, glyph: str) -> None:
                # 8x8 glyph box, centred: flat monochrome marks, no bevels.
                box = QRect(0, 0, 8, 8)
                box.moveCenter(r.center())
                if glyph == "min":
                    y = box.bottom() - 1
                    p.drawLine(box.left(), y, box.right(), y)
                elif glyph == "max":
                    p.drawRect(box.adjusted(0, 0, -1, -1))
                elif glyph == "restore":
                    back = box.adjusted(2, 0, 0, -2)
                    front = box.adjusted(0, 2, -2, 0)
                    p.drawRect(back)
                    p.drawRect(front)
                elif glyph == "close":
                    p.drawLine(box.topLeft(), box.bottomRight())
                    p.drawLine(box.topRight(), box.bottomLeft())

        if not t.mdi_frame:
            # Only a frameless theme has anything to intercept here, and the
            # override is not free to have: defined, it puts a Python call in
            # front of every primitive Qt draws -- 700 of them per repaint of
            # a list, none of which is a window frame. So it exists only on
            # the skins that suppress the frame.
            class _ImplFrameless(_Impl):
                def drawPrimitive(  # noqa: N802
                    self_inner, element, option, painter, widget=None
                ):
                    # Fusion's window frame is what rounds the outer corner.
                    if element == PE_FRAME_WINDOW:
                        return
                    super().drawPrimitive(element, option, painter, widget)

            self._style = _ImplFrameless(base_style, title_h)
        else:
            self._style = _Impl(base_style, title_h)

    def qstyle(self):
        return self._style


class _ZoomedMetricsStyle:
    """Proxy style for the classic skins at a zoom: title bars that grow too.

    Fusion answers ``PM_TitleBarHeight`` with a constant -- it looks at neither
    the option nor the font -- so a classic child window wore the same 28px
    title strip above 18px rows and above 36px ones. The modern skins get their
    strip from :class:`_DenseMdiStyle`, which already takes the zoom; this is
    the same correction for the skins that keep Fusion's own painting, and it
    touches nothing else.

    Only installed when there *is* a zoom. Every metric query Qt makes while
    painting anything comes through a proxy's ``pixelMetric`` -- an item view
    asks for the focus-frame margins twice a cell -- so an unzoomed classic
    skin keeps plain Fusion and pays nothing, which is what it did before this
    existed. The three answers are precomputed for the same reason.
    """

    def __init__(self, base_style, scale: float) -> None:
        from PySide6.QtWidgets import QProxyStyle, QStyle

        PM_TITLE_H = int(QStyle.PixelMetric.PM_TitleBarHeight)
        PM_MDI_FRAME = int(QStyle.PixelMetric.PM_MdiSubWindowFrameWidth)
        PM_MDI_MIN_W = int(QStyle.PixelMetric.PM_MdiSubWindowMinimizedWidth)
        # A checkbox / radio indicator is a style constant (~13px in Fusion), so
        # a sheet that sets no explicit size leaves it the one thing that does
        # not grow with the zoom -- a 13px box beside doubled text and rows.
        # Scaled, Fusion still draws the tick, just bigger.
        PM_IND_W = int(QStyle.PixelMetric.PM_IndicatorWidth)
        PM_IND_H = int(QStyle.PixelMetric.PM_IndicatorHeight)
        PM_EX_IND_W = int(QStyle.PixelMetric.PM_ExclusiveIndicatorWidth)
        PM_EX_IND_H = int(QStyle.PixelMetric.PM_ExclusiveIndicatorHeight)
        # Asked of the base style once, with no option: the only metric here
        # that varies by option is the title height, and it varies for tool
        # windows -- which an MDI area has none of.
        scaled = {
            key: _px(base_style.pixelMetric(QStyle.PixelMetric(key)), scale)
            for key in (PM_TITLE_H, PM_MDI_FRAME, PM_MDI_MIN_W,
                        PM_IND_W, PM_IND_H, PM_EX_IND_W, PM_EX_IND_H)
        }

        class _Impl(QProxyStyle):
            def pixelMetric(self_inner, metric, option=None, widget=None):  # noqa: N802
                hit = scaled.get(int(metric))
                if hit is not None:
                    return hit
                return super().pixelMetric(metric, option, widget)

        self._style = _Impl(base_style)

    def qstyle(self):
        return self._style


def apply_theme(
    app: QApplication | None = None,
    theme_id: str | None = None,
    *,
    persist: bool = True,
    scale: float | None = None,
) -> Theme:
    """Apply a theme to the QApplication (font + stylesheet).

    If ``theme_id`` is None, loads the saved preference (or default). Same for
    ``scale``: None means "whatever zoom is saved", so re-applying a theme for
    any other reason -- a font pin, View ▸ Re-apply -- keeps the zoom it had.

    Returns the *scaled* theme, which is what everything on screen is sized
    from. :func:`base_theme` is the one the user picked by name.
    """
    from PySide6.QtGui import QColor, QFont, QPalette
    from PySide6.QtWidgets import QApplication as QApp
    from PySide6.QtWidgets import QStyleFactory

    if theme_id is None:
        try:
            from .settings import load_theme_id

            theme_id = load_theme_id()
        except Exception:
            theme_id = DEFAULT_THEME_ID

    if scale is None:
        try:
            from .settings import load_ui_scale

            scale = load_ui_scale()
        except Exception:  # noqa: BLE001
            scale = 1.0
    scale = clamp_scale(scale)

    unscaled = get_theme(theme_id)
    theme = scale_theme(unscaled, scale)
    _publish(theme, base=unscaled, scale=scale)

    if app is None:
        app = QApp.instance()
    if app is not None:
        # Fusion + dense title bars for modern skins; classic keeps platform feel.
        if theme.modern:
            base = QStyleFactory.create("Fusion")
            title_h = scale_px(22 if unscaled.row_height <= 17 else 26)
            proxy = _DenseMdiStyle(base, title_h=title_h, theme=theme)
            app.setStyle(proxy.qstyle())
        else:
            # Set even when there is nothing to correct, because a proxy is
            # global: without this, switching from a modern skin left that
            # skin's flat title bars painting under the classic one, and
            # switching zoom left the previous factor's metrics behind.
            fusion = QStyleFactory.create("Fusion")
            app.setStyle(
                fusion if scale == 1.0
                else _ZoomedMetricsStyle(fusion, scale).qstyle()
            )
        # Register any Tahoma / Segoe UI / MS Sans Serif file we can find
        # before asking for one, then let a pinned family (View -> Font) win
        # over the theme's own. Both are in x3.ui.fonts.
        family = theme.font_family
        stack = ["Noto Sans", "sans-serif"]
        try:
            from . import fonts as fonts_mod

            fonts_mod.sideload()
            family = fonts_mod.override() or family
            stack = list(fonts_mod.UI_STACK)
        except Exception:  # noqa: BLE001 - never let font plumbing block a repaint
            pass
        font = QFont()
        if family:
            font.setFamily(family)
        else:
            # Prefer contemporary system UI fonts when available.
            font.setFamilies(stack)
        apply_font_traits(font, theme)
        # Slightly tighter metrics for dense skins
        if unscaled.row_height <= 17:
            font.setStyleHint(QFont.StyleHint.SansSerif)
        app.setFont(font)

        # Active MDI title / highlight: theme blue — never system orange.
        pal = app.palette()
        sel = QColor(theme.sel_bg)
        sel_fg = QColor(theme.sel_fg)
        title = QColor(theme.title_active)
        for group in (
            QPalette.ColorGroup.Active,
            QPalette.ColorGroup.Inactive,
            QPalette.ColorGroup.Disabled,
        ):
            pal.setColor(group, QPalette.ColorRole.Highlight, sel)
            pal.setColor(group, QPalette.ColorRole.HighlightedText, sel_fg)
            pal.setColor(group, QPalette.ColorRole.Link, title)
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Window, QColor(theme.bg))
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Base, QColor(theme.bg_window))
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Button, QColor(theme.bg_panel))
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Text, QColor(theme.text))
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.WindowText, QColor(theme.text))
        pal.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.ButtonText, QColor(theme.text))
        app.setPalette(pal)

        app.setStyleSheet(app_stylesheet())

        # A subwindow only re-masks itself on resize, so open children would
        # keep the previous skin's corners until dragged. Clear the mask and
        # hand each one a same-size resize so it asks the new style again.
        from PySide6.QtGui import QResizeEvent
        from PySide6.QtWidgets import QMdiSubWindow

        # Walked from the top-level windows, not app.allWidgets(): that wraps
        # every widget in the process, including ones Qt is halfway through
        # destroying, and wrapping one of those crashed a theme change made
        # while a dialog was closing.
        for top in app.topLevelWidgets():
            for w in top.findChildren(QMdiSubWindow):
                w.clearMask()
                app.sendEvent(w, QResizeEvent(w.size(), w.size()))

    if persist:
        try:
            from .settings import save_theme_id, save_ui_scale

            save_theme_id(theme.id)
            save_ui_scale(scale)
        except Exception:
            pass
    return theme
