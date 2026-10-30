"""Icons: original SVGs, in colour or as one tintable ink.

Two sets, chosen in View → Icon Set:

``builtin``
    Our colour vector set (`iconset.py` / ``resources/icons/svg``). Default.
``mono``
    The same glyphs as one ink (``resources/icons/svg-mono``), tinted to the
    active theme's text colour so a dark skin gets light glyphs instead of the
    black ``currentColor`` falls back to.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import iconset, settings, style

_RES_DIR = Path(__file__).resolve().parent / "resources"
_SVG_DIR = _RES_DIR / "icons" / "svg"
_MONO_DIR = _RES_DIR / "icons" / "svg-mono"
_APPICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

_cache: dict[tuple, QIcon] = {}
_app_icon: QIcon | None = None

def reset_icon_cache() -> None:
    """Drop cached pixmaps (call after changing icon source)."""
    global _app_icon
    _cache.clear()
    _app_icon = None


def _use_mono() -> bool:
    return settings.load_icon_source() == "mono"


#: Glyphs for actions that take something away from a running router. The
#: colour set already draws these red; in the symbolic set they keep a red ink
#: (View > Icon Set > Red for Remove and Disable) so the one button that
#: deletes does not wear the same silhouette colour as the one that adds.
DANGER_SLUGS = frozenset({"remove", "disable"})


def _mono_ink(slug: str = "") -> str:
    """The ink a symbolic glyph is drawn in.

    The theme's text colour, so the set follows a skin change: on the dark
    skins a black silhouette would be a hole in the toolbar. The theme's
    warning red for :data:`DANGER_SLUGS`, unless that is switched off.
    """
    if slug in DANGER_SLUGS and settings.load_danger_ink():
        return style.INFO_RED
    return style.TEXT


def _tinted(svg: str, ink: str) -> str:
    """Pin ``currentColor`` on the root so Qt resolves the whole glyph to *ink*.

    The shipped symbolic files carry ``color="currentColor"``, which is
    circular on its own -- Qt renders it black. Naming a real colour there is
    what makes the set tintable.
    """
    out = svg.replace('color="currentColor"', f'color="{ink}"', 1)
    if out == svg and "<svg " in svg:
        out = svg.replace("<svg ", f'<svg color="{ink}" ', 1)
    return out


def _mono_svg_text(slug: str) -> str | None:
    """Symbolic source for *slug*: the user's override, the shipped symbolic
    file, or the drawn original run through the converter.

    An override is only converted when it is not symbolic already -- rerunning
    :func:`iconset.to_symbolic` on one would flatten its washes.
    """
    over = _override_svg(slug)
    if over is not None:
        try:
            raw = over.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        if raw:
            return raw if "currentColor" in raw else iconset.to_symbolic(raw)
    shipped = _MONO_DIR / f"{slug}.svg"
    if shipped.is_file():
        try:
            return shipped.read_text(encoding="utf-8")
        except OSError:
            pass
    return iconset.svg_symbolic_for_name(slug)


def _icon_from_mono_name(slug: str, size: int) -> QIcon | None:
    ink = _mono_ink(slug)
    key = ("mono", slug, size, ink)
    if key in _cache:
        return _cache[key]
    raw = _mono_svg_text(slug)
    if not raw:
        return None
    icon = _rasterize_svg(_tinted(raw, ink), size)
    if icon is not None:
        _cache[key] = icon
    return icon


def _override_svg(slug: str) -> Path | None:
    p = settings.config_dir() / "icons" / f"{slug}.svg"
    return p if p.is_file() else None


def _shipped_svg(slug: str) -> Path | None:
    p = _SVG_DIR / f"{slug}.svg"
    return p if p.is_file() else None


def _rasterize_svg(svg: str | Path, size: int) -> QIcon | None:
    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    if isinstance(svg, Path):
        renderer = QSvgRenderer(str(svg))
    else:
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return None
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return QIcon(pm)


def _icon_from_svg_name(name: str, size: int) -> QIcon | None:
    slug = iconset.resolve_name(name)
    if _use_mono():
        icon = _icon_from_mono_name(slug, size)
        if icon is not None:
            return icon
    key = ("svg", slug, size)
    if key in _cache:
        return _cache[key]
    path = _override_svg(slug) or _shipped_svg(slug)
    icon = None
    if path is not None:
        icon = _rasterize_svg(path, size)
    if icon is None:
        raw = iconset.svg_for_name(slug)
        if raw:
            icon = _rasterize_svg(raw, size)
    if icon is not None:
        _cache[key] = icon
    return icon


def app_icon() -> QIcon:
    """The x3 application icon: resource files when shipped, drawn otherwise."""
    global _app_icon
    if _app_icon is not None:
        return _app_icon
    icon = QIcon()
    for px in _APPICON_SIZES:
        f = _RES_DIR / f"appicon-{px}.png"
        if f.is_file():
            icon.addFile(str(f), QSize(px, px))
    if icon.isNull():
        svg = _RES_DIR / "appicon.svg"
        if svg.is_file():
            icon = QIcon(str(svg))
    if icon.isNull():
        icon = _drawn_icon("app", 32)
    _app_icon = icon
    return icon


def _drawn_icon(kind: str, size: int | None = None) -> QIcon:
    """Last-resort painter fallback if SVG rasterization is unavailable."""
    size = style.ICON_SIZE if size is None else size
    # The generic marker is the icon every unmapped nav kind gets, so it is not
    # rare enough to leave in fixed colours: in the symbolic set it takes the
    # same ink as everything else, or a monochrome toolbar has one blue-gray
    # blob in it.
    ink = _mono_ink() if (kind != "app" and _use_mono()) else ""
    key = ("drawn", kind, size, ink)
    if key in _cache:
        return _cache[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if kind == "app":
        k = size / 64.0

        def pt(x: float, y: float) -> QPointF:
            return QPointF(x * k, y * k)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#60a5fa"))
        p.drawPolygon([pt(4, 14), pt(14, 4), pt(60, 4), pt(50, 14)])
        p.setBrush(QColor("#1d4ed8"))
        p.drawPolygon([pt(50, 14), pt(60, 4), pt(60, 50), pt(50, 60)])
        p.setBrush(QColor("#2563eb"))
        p.drawRect(QRectF(4 * k, 14 * k, 46 * k, 46 * k))
        pen = QPen(QColor("#ffffff"), max(1.5, 9 * k))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(pt(13, 30), pt(32, 49))
        p.drawLine(pt(32, 30), pt(13, 49))
        if size >= 24:
            pen.setWidthF(max(1.0, 4.6 * k))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            three = QPainterPath(pt(37.5, 21))
            three.lineTo(pt(43, 21))
            three.quadTo(pt(47, 21), pt(47, 24.4))
            three.quadTo(pt(47, 27.5), pt(43.4, 27.8))
            three.quadTo(pt(47, 28.1), pt(47, 31.2))
            three.quadTo(pt(47, 34.6), pt(43, 34.6))
            three.lineTo(pt(37.5, 34.6))
            p.drawPath(three)
    else:
        wash = QColor(ink or "#94a3b8")
        if ink:
            wash.setAlphaF(0.14)
        p.setBrush(wash)
        p.setPen(QColor(ink or "#475569"))
        p.drawEllipse(2, 2, size - 4, size - 4)
    p.end()
    icon = QIcon(pm)
    _cache[key] = icon
    return icon


def icon_from_index(index: int | None, size: int | None = None) -> QIcon:
    """Icon for a .jg ``icon`` / ``groupIcon`` cell index."""
    size = style.ICON_SIZE if size is None else size
    if index is None:
        return icon_for_kind("default", size=size)
    slug = iconset.INDEX_TO_NAME.get(index)
    if slug:
        icon = _icon_from_svg_name(slug, size)
        if icon is not None:
            return icon
    return _drawn_icon("default", size)


def icon_for_kind(kind: str, index: int | None = None, size: int | None = None) -> QIcon:
    size = style.ICON_SIZE if size is None else size
    if index is not None:
        return icon_from_index(index, size)
    if kind == "app":
        return app_icon()
    icon = _icon_from_svg_name(kind, size)
    if icon is not None:
        return icon
    return _drawn_icon(iconset.resolve_name(kind), size)


def window_icon(
    kind: str = "list",
    *,
    sprite_index: int | None = None,
    size: int | None = None,
) -> QIcon:
    """Icon for an MDI subwindow title bar / task switcher."""
    if sprite_index is not None:
        return icon_from_index(sprite_index, size)
    return icon_for_kind(kind, size=size)


def action_icon(name: str, size: int | None = None) -> QIcon:
    """Toolbar action icon by label (Add, Remove, …)."""
    return icon_for_kind(name, size=style.ICON_SIZE if size is None else size)


def icon_size() -> QSize:
    return QSize(style.ICON_SIZE, style.ICON_SIZE)


#: Interface type -> .jg icon cell. Only a few types have a picture of their
#: own; everything else falls back to the generic Interface cell (64). Cells
#: confirmed against the 7.21.1 plugin set.
_IFACE_TYPE_CELL = {
    "ethernet": 83,
    "pwr": 83,
    "bridge": 73,
    "bridge port": 73,
    "bridge ports": 73,
    "wifi": 116,
    "cap": 116,
    "cap interface": 116,
    "l2tp ethernet": 82,
}


def iface_type_icon(type_name: str, size: int | None = None) -> "QIcon":
    """Icon for an interface row, keyed on its Type: Ethernet=83,
    Bridge=73, WiFi/CAP=116, L2TP-Ethernet=82, everything else the generic
    Interface cell (64)."""
    cell = _IFACE_TYPE_CELL.get((type_name or "").strip().lower(), 64)
    return icon_from_index(cell, size)


#: Interface wire type_value -> .jg icon cell. Same picture set as above, but
#: keyed on the unambiguous wire type instead of the rendered Type string.
_IFACE_TYPEVALUE_CELL = {
    1: 83,    # Ethernet
    63: 83,   # PWR
    12: 73,   # Bridge
    68: 116,  # WiFi
    61: 116,  # CAP
    73: 82,   # L2TP Ethernet
}


def iface_type_icon_for_value(type_value: "int | None", size: int | None = None) -> "QIcon":
    """Interface icon keyed on the wire type_value (see :data:`_IFACE_TYPEVALUE_CELL`);
    unknown/None falls back to the generic Interface cell (64)."""
    cell = _IFACE_TYPEVALUE_CELL.get(type_value, 64)
    return icon_from_index(cell, size)
