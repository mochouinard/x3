"""Which family the interface is actually drawn in.

Winbox v3 is a Tahoma program, Winbox 4 a Segoe UI one, and the router's own
older dialogs predate both with Microsoft Sans Serif. The classic skin asks
for Tahoma accordingly -- but on Linux none of those fonts exist, fontconfig
answers all three names with the same substitute, and the skins turn out to be
one typeface at different sizes. Two halves fix that:

*Side-loading*: font files already on disk but outside fontconfig's path are
handed to Qt directly. Wine ships a metric-compatible ``tahoma.ttf`` under
``/usr/share/wine/fonts``, and anything dropped into
``~/.local/share/x3/fonts`` (``segoeui.ttf`` / ``micross.ttf`` copied
off a Windows box) is picked up the same way. Nothing is downloaded and
nothing is shipped -- these are files the user already has.

*Stand-ins*: Segoe UI and Microsoft Sans Serif ship with Windows and cannot be
downloaded, so a missing one is pointed at the closest installed face
(:data:`STANDINS`) rather than at whatever fontconfig would have guessed --
three columns of three typefaces instead of one repeated.

*Honesty*: :func:`resolve` reports the family Qt really picked, so the Font
menu says "Segoe UI - via Lato (closest installed)" rather than offering a
choice that changes nothing on screen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .settings import data_dir, load_ui_font, save_ui_font

#: The families worth comparing: what each generation of Winbox draws in.
COMPARE_FAMILIES: tuple[str, ...] = ("Segoe UI", "Tahoma", "Microsoft Sans Serif")

#: Used when nothing is pinned and the theme has no family of its own. Tahoma
#: leads it because that is what Winbox v3 draws in, and Wine's metric-
#: compatible ``tahoma.ttf`` is side-loaded above -- so on a box that has it,
#: the default face is the one the forms were laid out for. Everything after it
#: is the fallback for a box that has no Tahoma at all.
UI_STACK: tuple[str, ...] = (
    "Tahoma",
    "Segoe UI",
    "Inter",
    "Ubuntu",
    "Cantarell",
    "Noto Sans",
    "Helvetica Neue",
    "sans-serif",
)

#: Files we recognise inside a shared font directory. A whole Wine font dir is
#: mostly .fon bitmaps and Courier variants that Qt should not be offered, so
#: only the faces belonging to a comparison family are registered. The user's
#: own drop directory is loaded wholesale -- putting a file there is the ask.
_KNOWN_FILES = frozenset(
    {
        "tahoma.ttf",
        "tahomabd.ttf",
        "segoeui.ttf",
        "segoeuib.ttf",
        "segoeuii.ttf",
        "segoeuiz.ttf",
        "segoeuil.ttf",
        "seguisb.ttf",
        "micross.ttf",
        # Microsoft's own open, metric-compatible stand-in for Segoe UI.
        "selawk.ttf",
        "selawkb.ttf",
        "selawkl.ttf",
    }
)

_FONT_SUFFIXES = frozenset({".ttf", ".ttc", ".otf"})

#: When the real face cannot be had -- Segoe UI and Microsoft Sans Serif ship
#: with Windows and are not redistributable -- the best installed alternative
#: is registered as its substitute, so the three columns are three typefaces
#: instead of the same fallback three times. Order is best-first.
STANDINS: dict[str, tuple[str, ...]] = {
    "Segoe UI": ("Selawik", "Lato", "Carlito"),
    "Microsoft Sans Serif": ("Liberation Sans", "Arimo", "Arial"),
    "Tahoma": ("Wine Tahoma", "DejaVu Sans"),
}

#: The stand-ins that carry the original's metrics, so a form laid out for the
#: real font still lines up: Selawik is Microsoft's own open Segoe UI clone,
#: and Arial's metrics are what Microsoft Sans Serif was drawn against. The
#: rest are only the nearest face on the box -- said plainly, not as metrics.
METRIC_CLONES = frozenset({"Selawik", "Liberation Sans", "Arimo", "Arial", "Wine Tahoma"})


def drop_dir() -> Path:
    """Where a user parks font files for us: ``<data>/x3/ui/fonts``."""
    return data_dir() / "fonts"


def search_dirs() -> list[Path]:
    """Directories scanned for side-loadable faces, most specific first."""
    out = [drop_dir()]
    env = os.environ.get("X3_FONT_DIR")
    if env:
        out.extend(Path(p) for p in env.split(os.pathsep) if p)
    prefix = os.environ.get("WINEPREFIX")
    if prefix:
        out.append(Path(prefix) / "drive_c" / "windows" / "Fonts")
    out.append(Path.home() / ".wine" / "drive_c" / "windows" / "Fonts")
    out.append(Path("/usr/share/wine/fonts"))
    return out


#: sideload() is cheap only the first time; keyed on the directory list so a
#: test (or a user) moving XDG_DATA_HOME still gets a fresh scan.
_LOADED: tuple[tuple[str, ...], dict[str, list[str]]] | None = None

#: {family: stand-in} chosen by install_standins() during the last sideload.
_STANDINS: dict[str, str] = {}


def sideload(*, force: bool = False) -> dict[str, list[str]]:
    """Register findable font files with Qt. Returns ``{path: [families]}``.

    Safe to call repeatedly and safe to call without a QApplication -- Qt just
    reports a failure for each file, and the next call retries.
    """
    global _LOADED
    dirs = search_dirs()
    key = tuple(str(d) for d in dirs)
    if not force and _LOADED is not None and _LOADED[0] == key:
        return dict(_LOADED[1])

    from PySide6.QtGui import QFontDatabase

    found: dict[str, list[str]] = {}
    own = drop_dir()
    for d in dirs:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        wholesale = d == own
        for path in entries:
            if path.suffix.lower() not in _FONT_SUFFIXES:
                continue
            if not wholesale and path.name.lower() not in _KNOWN_FILES:
                continue
            fid = QFontDatabase.addApplicationFont(str(path))
            if fid < 0:
                continue
            fams = list(QFontDatabase.applicationFontFamilies(fid))
            if fams:
                found[str(path)] = fams
    _LOADED = (key, found)
    global _STANDINS
    _STANDINS = install_standins()
    return dict(found)


def install_standins() -> dict[str, str]:
    """Point each missing family at its closest installed clone.

    Returns ``{family: standin}`` for the ones that got one. Qt applies these
    at resolution time, so :func:`resolve` keeps reporting what is really
    drawn and the menu can say "via Selawik" rather than pretending.
    """
    from PySide6.QtGui import QFont, QFontDatabase

    out: dict[str, str] = {}
    for family, candidates in STANDINS.items():
        if QFontDatabase.hasFamily(family):
            continue
        for cand in candidates:
            if QFontDatabase.hasFamily(cand):
                QFont.insertSubstitution(family, cand)
                out[family] = cand
                break
    return out


def standin_for(family: str) -> str:
    """The stand-in in use for ``family``, or "" (installed, or none found)."""
    return _standins().get(family, "")


def _standins() -> dict[str, str]:
    if _LOADED is None:
        sideload()
    return dict(_STANDINS)


def sized(family: str, *, point_size: int = 10, pixel_size: int = 0):
    """A QFont for ``family`` at the given size, pixels winning over points.

    Winbox's own type is specified in pixels (see ``Theme.font_px``), so every
    size that travels through this module can be either unit; ``pixel_size``
    non-zero means the caller has a pixel-exact size to hit.
    """
    from PySide6.QtGui import QFont

    font = QFont()
    if family:
        font.setFamily(family)
    else:
        font.setFamilies(list(UI_STACK))
    if pixel_size:
        font.setPixelSize(pixel_size)
    else:
        font.setPointSize(point_size)
    return font


def resolve(family: str, *, point_size: int = 10, pixel_size: int = 0) -> str:
    """The family Qt would really use when asked for ``family``.

    ``QFontInfo.exactMatch()`` stays False even for a side-loaded face, so the
    name is what gets compared.
    """
    from PySide6.QtGui import QFontInfo

    return QFontInfo(sized(family, point_size=point_size, pixel_size=pixel_size)).family()


def is_substituted(family: str, *, point_size: int = 10, pixel_size: int = 0) -> bool:
    """True when asking for ``family`` quietly gets something else."""
    if not family:
        return False
    got = resolve(family, point_size=point_size, pixel_size=pixel_size)
    return got.casefold() != family.casefold()


#: Grey-level thresholds for :func:`solid_ink`, as byte translation tables so
#: the counting happens in C rather than per pixel in Python. "Lit" is any
#: pixel the rasteriser touched at all; "solid" is one it filled essentially
#: to the pen colour.
_LIT = bytes(1 if g < 250 else 0 for g in range(256))
_SOLID = bytes(1 if g < 26 else 0 for g in range(256))


def screen_dpr() -> float:
    """The primary screen's device-pixel ratio, or 1.0 with no screen."""
    from PySide6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen() if QGuiApplication.instance() else None
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


def solid_ink(font, text: str, *, dpr: float | None = None) -> float:
    """Share of a rendering's lit pixels that come out fully black, 0.0-1.0.

    The measurable half of "v3 looks darker". Two rasterisations of the same
    face at the same size light up a similar number of pixels; what differs is
    how much of that coverage is solid rather than grey. A vertical stem landing
    between two pixel columns prints as two half-grey columns; snapped onto the
    grid it prints as one black column -- same ink, half the pixels, twice the
    contrast, and the text reads heavier.

    **The device-pixel ratio dominates this number**, so it is rasterised at
    the screen's own ratio unless one is passed. A logical 11px font on a
    2x display is a 22-device-pixel rendering, which has resolution to spare:
    hinting barely moves this figure there, while on a 1x display it can double
    it. Measuring at 1x and reasoning about a 2x screen gets the answer
    backwards -- pass ``dpr`` explicitly only to ask about a specific display.

    Grayscale antialiasing, because that is all a QImage can do. A screen set to
    subpixel (``Xft.rgba: rgb``) lays the same glyph down differently again, so
    treat this as a comparison between fonts, not as a prediction of a pixel.

    Needs a QGuiApplication, because it really rasterises the glyphs.
    """
    from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter

    if not text:
        return 0.0
    ratio = screen_dpr() if dpr is None else dpr
    fm = QFontMetricsF(font)
    w = int(fm.horizontalAdvance(text)) + 4
    h = int(fm.height()) + 4
    dw, dh = max(1, int(w * ratio)), max(1, int(h * ratio))
    img = QImage(dw, dh, QImage.Format.Format_Grayscale8)
    img.setDevicePixelRatio(ratio)
    img.fill(255)
    p = QPainter(img)
    p.setFont(font)
    p.setPen(QColor("black"))
    p.drawText(2, int(fm.ascent()) + 2, text)
    p.end()

    bpl = img.bytesPerLine()
    buf = bytes(img.constBits())
    data = b"".join(buf[y * bpl : y * bpl + dw] for y in range(dh))
    lit = data.translate(_LIT).count(b"\x01")
    if not lit:
        return 0.0
    return data.translate(_SOLID).count(b"\x01") / lit


@dataclass(frozen=True)
class FontChoice:
    """One entry in the Font menu."""

    family: str  # "" = follow the theme
    label: str  # menu text, substitution noted
    resolved: str  # what Qt actually draws
    substituted: bool
    standin: str = ""  # a deliberate metric clone, not fontconfig's guess


def choices(
    theme_family: str = "", *, point_size: int = 10, pixel_size: int = 0
) -> list[FontChoice]:
    """The Font menu: the theme's own pick, then the comparison families."""
    sideload()
    size = {"point_size": point_size, "pixel_size": pixel_size}
    theme_name = theme_family or "system UI"
    out = [
        FontChoice(
            family="",
            label=f"Theme default ({theme_name})",
            resolved=resolve(theme_family, **size),
            substituted=is_substituted(theme_family, **size),
        )
    ]
    for fam in COMPARE_FAMILIES:
        sub = is_substituted(fam, **size)
        got = resolve(fam, **size)
        stand = standin_for(fam)
        if not sub:
            label = fam
        elif stand and stand in METRIC_CLONES:
            label = f"{fam} — via {stand} (metric-compatible)"
        elif stand:
            label = f"{fam} — via {stand} (closest installed)"
        else:
            label = f"{fam} — not installed, drawn as {got}"
        out.append(
            FontChoice(
                family=fam,
                label=label,
                resolved=got,
                substituted=sub,
                standin=stand,
            )
        )
    return out


def override() -> str:
    """The pinned UI family, or "" to follow the theme."""
    try:
        return load_ui_font()
    except Exception:  # noqa: BLE001 - a bad settings file must not stop startup
        return ""


def apply(family: str, app=None) -> str:
    """Pin ``family`` (or "" for the theme's own) and restyle everything now.

    Returns the family Qt ended up drawing, which is not always the one asked
    for -- see :func:`resolve`.
    """
    from . import style as style_mod

    save_ui_font(family)
    theme = style_mod.apply_theme(app, style_mod.active_theme().id, persist=False)
    return resolve(
        family or theme.font_family,
        point_size=theme.font_pt,
        pixel_size=theme.font_px,
    )
