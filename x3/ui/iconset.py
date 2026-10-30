"""Original vector icons for x3.

These are *not* the vendor's artwork. Each glyph is a short SVG we drew so the
meaning matches the WinBox /.jg icon index (plus = add, NIC = interfaces, …)
without copying their pixels. Replace any file in ``resources/icons/svg/`` or
drop an override in ``~/.config/x3/icons/<name>.svg``.
"""

from __future__ import annotations

import re
from pathlib import Path

SVG_DIR = Path(__file__).resolve().parent / "resources" / "icons" / "svg"
SVG_MONO_DIR = Path(__file__).resolve().parent / "resources" / "icons" / "svg-mono"

# Fixed palette — readable on classic gray and on the dark skins.
B, R, G, A, S, C, V, K, Y = (
    "#2563eb", "#dc2626", "#16a34a", "#d97706",
    "#475569", "#0891b2", "#7c3aed", "#111827", "#ca8a04",
)
W = "#f8fafc"

_HEAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none">'
)
_TAIL = "</svg>"


def _svg(*parts: str) -> str:
    return _HEAD + "".join(parts) + _TAIL


def _p(d: str, stroke: str, width: float = 1.7, fill: str = "none") -> str:
    return (
        f'<path d="{d}" stroke="{stroke}" stroke-width="{width}" fill="{fill}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )


def _r(x, y, w, h, stroke, fill="none", rx=2.2, width=1.7) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'stroke="{stroke}" fill="{fill}" stroke-width="{width}"/>'
    )


def _c(cx, cy, rad, stroke, fill="none", width=1.7) -> str:
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{rad}" stroke="{stroke}" '
        f'fill="{fill}" stroke-width="{width}"/>'
    )


def _l(x1, y1, x2, y2, stroke, width=1.7) -> str:
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
        f'stroke-width="{width}" stroke-linecap="round"/>'
    )


# ---------------------------------------------------------------------------
# Toolbar / chrome (named; x3 action_icon / icon_for_kind)
# ---------------------------------------------------------------------------

def ico_add() -> str:
    # Bare plus — WinBox 3 style, no plate.
    return _svg(
        _l(12, 5, 12, 19, B, 2.6),
        _l(5, 12, 19, 12, B, 2.6),
    )


def ico_remove() -> str:
    return _svg(_l(5.5, 12, 18.5, 12, R, 2.6))


def ico_enable() -> str:
    return _svg(_p("M5.2 12.2 L10 17.2 L19 6.6", B, 2.5))


def ico_disable() -> str:
    return _svg(
        _l(6.2, 6.2, 17.8, 17.8, R, 2.5),
        _l(17.8, 6.2, 6.2, 17.8, R, 2.5),
    )


def ico_comment() -> str:
    return _svg(
        _p("M4 7.5 h12.5 a2 2 0 0 1 2 2 v5 a2 2 0 0 1-2 2 H10 l-3.5 3 v-3 H6 a2 2 0 0 1-2-2 z",
           A, 1.6, "#fef3c7"),
        _l(7.5, 10.2, 14.5, 10.2, A, 1.3),
        _l(7.5, 13, 12.5, 13, A, 1.3),
    )


def ico_filter() -> str:
    return _svg(_p("M4.5 6.5 h15 l-5.5 6.2 v5.3 l-4-2 v-3.3 z", C, 1.7, "#cffafe"))


def ico_find() -> str:
    # A magnifier: the Find box's toggle, beside the funnel's.
    return _svg(
        _p("M10.5 5 a5.5 5.5 0 1 0 0.01 0 z", C, 1.7, "#cffafe")
        + _p("M14.6 14.6 L19.5 19.5", C, 2.2)
    )


def ico_undo() -> str:
    return _svg(_p("M8 8 H5.5 v2.5 M5.8 10.2 A6.2 6.2 0 1 0 8.2 6.6", B, 1.8))


def ico_redo() -> str:
    return _svg(_p("M16 8 H18.5 v2.5 M18.2 10.2 A6.2 6.2 0 1 1 15.8 6.6", B, 1.8))


def ico_refresh() -> str:
    return _svg(
        _p("M18 12 a6 6 0 1 1-1.8-4.3", B, 1.8),
        _p("M16.2 4.8 v3.2 h3.2", B, 1.8),
    )


def ico_copy() -> str:
    return _svg(
        _r(8, 6, 11, 13, S, "#e2e8f0", rx=1.8),
        _r(5, 3.5, 11, 13, S, W, rx=1.8),
    )


def ico_paste() -> str:
    return _svg(
        _r(6, 5.5, 12, 14.5, S, W, rx=1.8),
        _r(9, 3.5, 6, 4, S, "#e2e8f0", rx=1.2),
        _l(9, 12, 15, 12, S, 1.4),
        _l(9, 15, 13, 15, S, 1.4),
    )


def ico_back() -> str:
    return _svg(_p("M13 6 L7 12 L13 18", B, 2.1), _l(7.5, 12, 18, 12, B, 2.1))


def ico_forward() -> str:
    return _svg(_p("M11 6 L17 12 L11 18", B, 2.1), _l(6, 12, 16.5, 12, B, 2.1))


def ico_move_up() -> str:
    return _svg(_p("M12 6 L6.5 13 h11 z", S, 1.4, S))


def ico_move_down() -> str:
    return _svg(_p("M12 18 L6.5 11 h11 z", S, 1.4, S))


def ico_folder() -> str:
    return _svg(
        _p("M3.5 9.5 h17 v8.2 a1.8 1.8 0 0 1-1.8 1.8 H5.3 a1.8 1.8 0 0 1-1.8-1.8 z",
           A, 1.6, "#fde68a"),
        _p("M3.5 9.5 v-2 a1.5 1.5 0 0 1 1.5-1.5 h4.2 l1.8 2", A, 1.6, "#fbbf24"),
    )


def ico_list() -> str:
    return _svg(
        _r(4, 3.5, 16, 17, B, "#eff6ff", rx=2),
        _l(4, 8, 20, 8, B, 1.3),
        _l(9.5, 8, 9.5, 20.5, B, 1.3),
        _l(6.5, 11.5, 18, 11.5, "#93c5fd", 1.2),
        _l(6.5, 15, 18, 15, "#93c5fd", 1.2),
        _l(6.5, 18.2, 18, 18.2, "#93c5fd", 1.2),
    )


def ico_table() -> str:
    return ico_list()


def ico_terminal() -> str:
    return _svg(
        _r(3, 4.5, 18, 15, K, "#0f172a", rx=2),
        _p("M6.5 9.5 L9.5 12 L6.5 14.5", G, 1.7),
        _l(11, 14.8, 16.5, 14.8, G, 1.6),
    )


def ico_props() -> str:
    return _svg(
        _r(5, 3.5, 13, 17, V, "#f5f3ff", rx=1.8),
        _l(8, 8, 15, 8, V, 1.4),
        _l(8, 12, 15, 12, V, 1.4),
        _l(8, 16, 13, 16, V, 1.4),
    )


def ico_tabs() -> str:
    return _svg(
        _r(6.5, 6.5, 13, 13, C, "#e0f2fe", rx=1.6),
        _r(4, 4, 13, 13, C, "#f0f9ff", rx=1.6),
        _r(4, 4, 5, 3.2, C, "#bae6fd", rx=0.8),
        _r(10, 4, 5, 3.2, C, W, rx=0.8),
    )


def ico_warning() -> str:
    return _svg(
        _p("M12 4.2 L21 19.5 H3 z", A, 1.6, "#fef3c7"),
        _l(12, 9.5, 12, 14.2, A, 1.8),
        _c(12, 16.8, 0.7, A, A, 1),
    )


# ---------------------------------------------------------------------------
# Menu-tree glyphs (.jg ``icon`` / ``groupIcon`` index)
# ---------------------------------------------------------------------------

def ico_bridge() -> str:
    return _svg(
        _r(3.5, 4, 10, 8, B, "#dbeafe", rx=1.6),
        _r(10.5, 11.5, 10, 8, B, "#eff6ff", rx=1.6),
        _l(8.5, 12, 13.5, 11.5, B, 1.4),
    )


def ico_files() -> str:
    return ico_folder()


def ico_graph() -> str:
    return _svg(
        _r(3.5, 4, 17, 16, G, "#f0fdf4", rx=2),
        _p("M6 15.5 L9.5 11.5 L13 13.5 L18 7.5", G, 1.8),
        _c(18, 7.5, 1.1, G, G, 1),
    )


def ico_reply() -> str:
    return _svg(_p("M10 7 L5 12 L10 17", B, 1.8), _l(5.5, 12, 16, 12, B, 1.8),
                _l(16, 12, 16, 8, B, 1.8))


def ico_interfaces() -> str:
    return _svg(
        _r(3.5, 7, 17, 10, C, "#ecfeff", rx=2),
        _l(3.5, 12, 20.5, 12, C, 1.4),
        _c(8, 12, 1.6, C, C, 1),
        _c(16, 12, 1.6, C, C, 1),
        _r(10.2, 9.2, 3.6, 5.6, C, "#a5f3fc", rx=0.8),
    )


def ico_ip() -> str:
    return _svg(
        _r(3.5, 5, 17, 14, A, "#fffbeb", rx=2),
        _p("M7 12 h2.2 M8.1 9 v6 M12.2 9.2 q2.6 0 2.6 3 q0 3-2.6 3 h-1.4 V9.2 z", A, 1.5),
    )


def ico_ipv6() -> str:
    return _svg(
        _r(3.5, 5, 17, 14, V, "#f5f3ff", rx=2),
        _p("M7 10.2 h4 M9 10.2 v5.2 M13.2 15.4 h4.2 M13.2 10.2 h4.2 M13.2 12.8 h3.2", V, 1.5),
    )


def ico_log() -> str:
    return _svg(
        _r(4, 3.5, 16, 17, S, W, rx=2),
        _l(7, 8, 17, 8, S, 1.3),
        _l(7, 12, 17, 12, S, 1.3),
        _l(7, 16, 14, 16, S, 1.3),
        _c(7, 8, 0.7, B, B, 0.8),
        _c(7, 12, 0.7, A, A, 0.8),
        _c(7, 16, 0.7, G, G, 0.8),
    )


def ico_notes() -> str:
    return _svg(
        _r(5, 3.5, 14, 17, S, W, rx=1.8),
        _l(8, 8, 16, 8, S, 1.3),
        _l(8, 12, 16, 12, S, 1.3),
        _l(8, 16, 13, 16, S, 1.3),
    )


def ico_book() -> str:
    return _svg(
        _p("M5 5.5 h6 a2 2 0 0 1 2 2 v11 a3 3 0 0 0-3-3 H5 z", A, 1.5, "#fde68a"),
        _p("M19 5.5 h-6 a2 2 0 0 0-2 2 v11 a3 3 0 0 1 3-3 h5 z", A, 1.5, "#fef3c7"),
    )


def ico_supout() -> str:
    return _svg(
        _p("M7 4.5 h7 l4 4 v11 a1.6 1.6 0 0 1-1.6 1.6 H7 A1.6 1.6 0 0 1 5.4 19.5 V6.1 A1.6 1.6 0 0 1 7 4.5 z",
           S, 1.5, W),
        _p("M14 4.5 v4 h4", S, 1.5),
        _p("M12 11 v6 M9.5 15 L12 17.5 L14.5 15", B, 1.6),
    )


def ico_script() -> str:
    return _svg(
        _p("M7 4.5 h7 l4 4 v11 a1.6 1.6 0 0 1-1.6 1.6 H7 A1.6 1.6 0 0 1 5.4 19.5 V6.1 A1.6 1.6 0 0 1 7 4.5 z",
           B, 1.5, "#eff6ff"),
        _p("M14 4.5 v4 h4", B, 1.5),
        _p("M8.5 13 L10.5 15 L8.5 17", B, 1.5),
        _l(12, 17, 15.5, 17, B, 1.4),
    )


def ico_mesh() -> str:
    return _svg(
        _r(3, 8, 7, 6, C, "#ecfeff", rx=1.4),
        _r(14, 8, 7, 6, C, "#ecfeff", rx=1.4),
        _l(10, 11, 14, 11, C, 1.5),
        _c(7, 9.4, 0.7, C, C, 0.8),
        _c(17, 9.4, 0.7, C, C, 0.8),
    )


def ico_attach() -> str:
    return _svg(_p(
        "M9 11 v5.2 a3 3 0 0 0 6 0 V8.2 a2.4 2.4 0 0 0-4.8 0 v7.4 a1.6 1.6 0 0 0 3.2 0 V11",
        S, 1.7,
    ))


def ico_mpls() -> str:
    return _svg(
        _c(6, 12, 2.2, V, "#ede9fe"),
        _c(18, 7.5, 2.2, V, "#ede9fe"),
        _c(18, 16.5, 2.2, V, "#ede9fe"),
        _l(8, 12, 16, 8.2, V, 1.5),
        _l(8, 12, 16, 15.8, V, 1.5),
    )


def ico_ppp() -> str:
    return _svg(
        _c(12, 12, 7.2, A, "#fffbeb"),
        _p("M12 12 L12 7.5 M12 12 L16.2 14.5", A, 1.7),
        _c(12, 12, 1.1, A, A, 1),
    )


def ico_queues() -> str:
    return _svg(
        _r(4, 15, 3.2, 4, G, "#86efac", rx=0.6),
        _r(9.4, 11, 3.2, 8, G, "#4ade80", rx=0.6),
        _r(14.8, 6.5, 3.2, 12.5, G, "#16a34a", rx=0.6),
    )


def ico_quickset() -> str:
    return _svg(
        _p("M8 19 L10.2 13.2 L6.5 11.2 L17 5 L14.2 15.2 L11.5 12.8 z", A, 1.5, "#fde68a"),
    )


def ico_radius() -> str:
    return _svg(
        _p("M8 11 v7.5 M8 11 a4 3.2 0 0 1 8 0 v1.5 H12", A, 1.7, "#fef3c7"),
        _c(15.5, 16.2, 2.4, A, "#fde68a"),
    )


def ico_users_move() -> str:
    return _svg(
        _c(8, 8, 2.3, B, "#dbeafe"),
        _p("M4.2 16.5 q1.5-4 3.8-4 t3.8 4", B, 1.5),
        _p("M14 10 h6 M17.5 7 v6", G, 1.7),
    )


def ico_tools() -> str:
    return _svg(
        _p("M14.5 5.5 a4 4 0 0 0-5.2 5.1 L5.5 14.4 a2 2 0 0 0 2.8 2.8 l3.8-3.8 A4 4 0 0 0 18.5 9.5 L16 12 z",
           S, 1.6, "#e2e8f0"),
    )


def ico_settings() -> str:
    return _svg(
        _c(12, 12, 3, S, "#e2e8f0"),
        _p(
            "M12 4.2 l1.2 2.1 2.4-.4 1.2 2.1-1.7 1.7.4 2.4-2.1 1.2-1.4-1.9-1.4 1.9-2.1-1.2.4-2.4-1.7-1.7 1.2-2.1 2.4.4 z",
            S, 1.4, "#cbd5e1",
        ),
    )


def ico_ports() -> str:
    return _svg(
        _r(3.5, 6, 17, 12, S, "#f1f5f9", rx=2),
        *[_r(5.5 + i * 3.4, 9.2, 2.4, 5.6, S, "#94a3b8", rx=0.5) for i in range(4)],
    )


def ico_ping() -> str:
    return _svg(
        _c(8, 16, 2, C, "#a5f3fc"),
        _p("M10.2 14.2 A7 7 0 0 1 18.5 8.5 M11.8 15.6 A5 5 0 0 1 17.4 11", C, 1.6),
    )


def ico_globe() -> str:
    return _svg(
        _c(12, 12, 7.2, C, "#ecfeff"),
        _p("M12 4.8 v14.4 M5 12 h14 M7.2 7.5 q4.8 2 9.6 0 M7.2 16.5 q4.8-2 9.6 0", C, 1.4),
    )


def ico_display() -> str:
    return _svg(
        _r(3.5, 5, 17, 11.5, S, "#0f172a", rx=1.8),
        _l(9, 19, 15, 19, S, 1.6),
        _l(12, 16.5, 12, 19, S, 1.6),
    )


def ico_wireless() -> str:
    return _svg(
        _p("M6.2 10.2 a7.2 7.2 0 0 1 11.6 0 M8.4 12.6 a4.4 4.4 0 0 1 7.2 0", C, 1.7),
        _c(12, 16.4, 1.2, C, C, 1),
    )


def ico_lcd() -> str:
    return ico_display()


def ico_edit() -> str:
    return _svg(
        _r(4, 4, 12, 16, S, W, rx=1.6),
        _p("M14 16.5 L20 8.5 L17.5 6.5 L11.5 14.5 v2 z", A, 1.5, "#fde68a"),
    )


def ico_partition() -> str:
    return _svg(
        _c(12, 12, 7.4, B, "#dbeafe"),
        _p("M12 12 L12 4.6 A7.4 7.4 0 0 1 18.8 15.2 z", B, 1.4, B),
    )


def ico_record() -> str:
    return _svg(_c(12, 12, 6.5, R, "#ef4444"))


def ico_home() -> str:
    return _svg(
        _p("M4 12 L12 5 L20 12", A, 1.7),
        _p("M7 11.5 V19 h10 V11.5", A, 1.6, "#fef3c7"),
    )


def ico_shield() -> str:
    return _svg(_p("M12 4 L19 7 v6.2 c0 4.4-3 6.8-7 8.3 c-4-1.5-7-3.9-7-8.3 V7 z", B, 1.6, "#dbeafe"))


def ico_users() -> str:
    return _svg(
        _c(9, 8.5, 2.4, B, "#dbeafe"),
        _p("M4.5 17.5 q1.8-4.4 4.5-4.4 t4.5 4.4", B, 1.5),
        _c(16, 9.2, 2, S, "#e2e8f0"),
        _p("M14.2 17.5 q1-3.2 2.8-3.2 t2.6 3.2", S, 1.4),
    )


def ico_wireguard() -> str:
    return _svg(
        _r(6, 10.5, 12, 9, V, "#ede9fe", rx=2),
        _p("M8.5 10.5 v-2 a3.5 3.5 0 0 1 7 0 v2", V, 1.7),
        _c(12, 15.2, 1.2, V, V, 1),
    )


def ico_crypto() -> str:
    return _svg(
        _c(12, 12, 7, A, "#fffbeb"),
        _p("M12 8.2 v7.6 M9.2 10.5 h5.6 M9.2 13.5 h5.6", A, 1.6),
    )


def ico_radio() -> str:
    return _svg(
        _c(12, 14, 5.2, C, "#ecfeff"),
        _l(12, 8.8, 12, 4.8, C, 1.6),
        _c(12, 14, 1.4, C, C, 1),
    )


def ico_wifi() -> str:
    return ico_wireless()


def ico_dot1x() -> str:
    return _svg(
        _c(12, 12, 7.2, V, "#f5f3ff"),
        _p("M9 12.2 L11 14.4 L15.4 9.4", V, 1.9),
    )


def ico_capsman() -> str:
    return _svg(
        _c(8, 14.5, 3.2, C, "#ecfeff"),
        _c(16, 14.5, 3.2, C, "#ecfeff"),
        _p("M6.5 12.2 A6.5 6.5 0 0 1 17.5 12.2", C, 1.6),
    )


def ico_person() -> str:
    return _svg(
        _c(12, 8, 2.6, S, "#e2e8f0"),
        _p("M6.2 18.5 q1.8-5.2 5.8-5.2 t5.8 5.2", S, 1.6),
    )


def ico_person_off() -> str:
    return _svg(
        _c(12, 8, 2.6, S, "#e2e8f0"),
        _p("M6.2 18.5 q1.8-5.2 5.8-5.2 t5.8 5.2", S, 1.6),
        _l(6, 6, 18, 18, R, 1.8),
    )


def ico_person_add() -> str:
    return _svg(
        _c(9, 8, 2.3, G, "#dcfce7"),
        _p("M4.5 17.5 q1.6-4.4 4.5-4.4 t4.5 4.4", G, 1.5),
        _l(17, 8, 17, 14, G, 1.7),
        _l(14, 11, 20, 11, G, 1.7),
    )


def ico_person_remove() -> str:
    return _svg(
        _c(9, 8, 2.3, R, "#fee2e2"),
        _p("M4.5 17.5 q1.6-4.4 4.5-4.4 t4.5 4.4", R, 1.5),
        _l(14.5, 11, 20, 11, R, 1.7),
    )


def ico_flag() -> str:
    return _svg(_l(7, 4.5, 7, 19.5, S, 1.7), _p("M7 5.2 h10 l-2.2 3.6 L17 12.5 H7 z", R, 1.4, "#fecaca"))


def ico_windows() -> str:
    return _svg(
        _r(4, 4, 7.2, 7.2, B, "#93c5fd", rx=1),
        _r(12.8, 4, 7.2, 7.2, G, "#86efac", rx=1),
        _r(4, 12.8, 7.2, 7.2, A, "#fcd34d", rx=1),
        _r(12.8, 12.8, 7.2, 7.2, C, "#67e8f9", rx=1),
    )


def ico_play() -> str:
    return _svg(_p("M8 6 L18 12 L8 18 z", G, 1.4, "#86efac"))


def ico_group() -> str:
    return ico_users()


def ico_link() -> str:
    return _svg(
        _p("M10 13.5 L8.2 15.3 a2.6 2.6 0 0 1-3.7-3.7 L6.3 9.8", B, 1.7),
        _p("M14 10.5 L15.8 8.7 a2.6 2.6 0 0 1 3.7 3.7 L17.7 14.2", B, 1.7),
        _l(9.5, 14.5, 14.5, 9.5, B, 1.7),
    )


def ico_forbidden() -> str:
    return _svg(_c(12, 12, 7.2, R), _l(7.2, 16.8, 16.8, 7.2, R, 1.8))


def ico_server() -> str:
    return _svg(
        _r(4, 4.5, 16, 5, S, "#e2e8f0", rx=1.4),
        _r(4, 11, 16, 5, S, "#e2e8f0", rx=1.4),
        _c(7.2, 7, 0.7, G, G, 0.8),
        _c(7.2, 13.5, 0.7, G, G, 0.8),
    )


def ico_print() -> str:
    return _svg(
        _r(7, 3.5, 10, 5, S, W, rx=1),
        _r(4.5, 8.5, 15, 8, S, "#cbd5e1", rx=1.6),
        _r(7.5, 13.5, 9, 6.5, S, W, rx=1),
    )


def ico_package() -> str:
    return _svg(
        _p("M4.5 8.5 L12 4.5 L19.5 8.5 L12 12.5 z", A, 1.5, "#fde68a"),
        _p("M4.5 8.5 V16 L12 20 V12.5 z", A, 1.5, "#fbbf24"),
        _p("M19.5 8.5 V16 L12 20 V12.5 z", A, 1.5, "#d97706"),
    )


def ico_certificate() -> str:
    return _svg(
        _r(5, 4, 14, 11, A, "#fffbeb", rx=1.6),
        _c(12, 15.5, 3.2, A, "#fde68a"),
        _p("M10.5 17.5 L9.2 21 L12 19.4 L14.8 21 L13.5 17.5", A, 1.3, "#fbbf24"),
    )


def ico_plus_box() -> str:
    return _svg(
        _r(4, 4, 16, 16, S, "none", rx=2.5),
        _l(12, 8, 12, 16, S, 1.8),
        _l(8, 12, 16, 12, S, 1.8),
    )


def ico_stack_1() -> str:
    return _svg(_r(6, 10, 12, 7, G, "#86efac", rx=1.4))


def ico_stack_2() -> str:
    return _svg(
        _r(6, 13, 12, 5.5, G, "#4ade80", rx=1.2),
        _r(6, 8, 12, 5.5, G, "#86efac", rx=1.2),
    )


def ico_stack_3() -> str:
    return _svg(
        _r(6, 15, 12, 4.5, G, "#16a34a", rx=1),
        _r(6, 10.2, 12, 4.5, G, "#4ade80", rx=1),
        _r(6, 5.5, 12, 4.5, G, "#86efac", rx=1),
    )


def ico_layers() -> str:
    return _svg(
        _p("M4 14 L12 18 L20 14", C, 1.6),
        _p("M4 11 L12 15 L20 11", C, 1.6),
        _p("M4 8 L12 12 L20 8 L12 4 z", C, 1.6, "#a5f3fc"),
    )


def ico_camera() -> str:
    return _svg(
        _r(3.5, 8, 17, 11, S, "#e2e8f0", rx=2),
        _c(12, 13.5, 3.2, S, "#94a3b8"),
        _r(8, 5.5, 4, 2.6, S, "#cbd5e1", rx=0.6),
    )


def ico_ruler() -> str:
    return _svg(
        _p("M5 16.5 L16.5 5 l3 3 L8 19.5 z", S, 1.5, "#e2e8f0"),
        _l(8, 14.5, 9.5, 13, S, 1.2),
        _l(10.5, 12, 12, 10.5, S, 1.2),
    )


def ico_signal() -> str:
    return _svg(
        _p("M7 16 L12 6 L17 16 z", V, 1.5, "#ddd6fe"),
        _p("M10 16 L12 11 L14 16 z", V, 1.3, V),
    )


def ico_alert() -> str:
    return ico_warning()


def ico_award() -> str:
    return _svg(
        _c(12, 10, 5, A, "#fde68a"),
        _p("M9.2 14.5 L8 20 l4-2.2 L16 20 l-1.2-5.5", A, 1.4, "#fbbf24"),
    )


def ico_clock_gear() -> str:
    return _svg(
        _c(12, 12, 7.2, S, "#f1f5f9"),
        _l(12, 12, 12, 8, S, 1.6),
        _l(12, 12, 15.5, 13.8, S, 1.6),
    )


def ico_stop() -> str:
    return _svg(_c(12, 12, 7.2, R, "#fecaca"), _r(9, 9, 6, 6, R, R, rx=1))


def ico_skip() -> str:
    return _svg(_p("M6 7 L13 12 L6 17 z", B, 1.3, B), _l(16.5, 7, 16.5, 17, B, 1.8))


def ico_list_num() -> str:
    return _svg(
        _l(10, 7, 19, 7, S, 1.4),
        _l(10, 12, 19, 12, S, 1.4),
        _l(10, 17, 19, 17, S, 1.4),
        _p("M5 6.2 h2 v4", S, 1.4),
        _p("M5 11.5 h2.4 L5 15.5 h2.4", S, 1.3),
    )


def ico_ok() -> str:
    return _svg(_p("M5 12.5 L9.5 17 L19 7.5", G, 2.2))


def ico_close_sm() -> str:
    return _svg(_l(7, 7, 17, 17, S, 2), _l(17, 7, 7, 17, S, 2))


def ico_blank_page() -> str:
    return _svg(_r(6, 4, 12, 16, S, W, rx=1.6))


def ico_help() -> str:
    return _svg(
        _c(12, 12, 8, B, "#eff6ff"),
        _p("M9.5 9.2 q.6-2 2.5-2 t2.4 1.8 q0 1.6-2.4 2.2 v1.4", B, 1.6),
        _c(12, 16.6, 0.7, B, B, 0.8),
    )


def ico_reset_counters() -> str:
    return _svg(
        _c(12, 12, 7.4, C, "#ecfeff"),
        _p("M8 12 h8 M12 8 v8", C, 1.8),
        _c(12, 12, 2.2, C),
    )


def ico_power() -> str:
    return _svg(_c(12, 13, 6.2, G), _l(12, 5.5, 12, 12, G, 2))


def ico_default() -> str:
    return _svg(_c(12, 12, 6.5, S, "#e2e8f0"))


def ico_system() -> str:
    return ico_settings()


def ico_iface() -> str:
    return ico_interfaces()


def ico_computers() -> str:
    return _svg(
        _r(3, 7.5, 8, 6, C, "#ecfeff", rx=1.3),
        _r(13, 7.5, 8, 6, C, "#ecfeff", rx=1.3),
        _l(7, 13.5, 7, 15.5, C, 1.4),
        _l(17, 13.5, 17, 15.5, C, 1.4),
        _l(5, 15.5, 9, 15.5, C, 1.4),
        _l(15, 15.5, 19, 15.5, C, 1.4),
    )


def ico_remote() -> str:
    return _svg(
        _r(3.5, 6, 11, 9, S, "#0f172a", rx=1.5),
        _p("M16 9 L20.5 12.5 L16 16", B, 1.7),
        _l(14.5, 12.5, 20, 12.5, B, 1.7),
    )


def ico_exchange() -> str:
    return _svg(
        _p("M4 9 H16 L13 6", B, 1.7),
        _p("M20 15 H8 L11 18", C, 1.7),
    )


def ico_umbrella() -> str:
    return _svg(
        _p("M4.5 12 Q12 4.5 19.5 12 H4.5 z", C, 1.6, "#ecfeff"),
        _l(12, 12, 12, 18.5, C, 1.6),
        _p("M12 18.5 q2.4 0 2.4-2", C, 1.5),
    )


def ico_switch() -> str:
    return _svg(
        _r(3.5, 8, 17, 8, S, "#e2e8f0", rx=1.6),
        *[_r(5.4 + i * 3.6, 10.2, 2.4, 3.6, S, "#64748b", rx=0.4) for i in range(4)],
    )


def ico_wifi_off() -> str:
    return _svg(
        _p("M6.2 10.2 a7.2 7.2 0 0 1 11.6 0 M8.4 12.6 a4.4 4.4 0 0 1 7.2 0", C, 1.7),
        _c(12, 16.4, 1.2, C, C, 1),
        _l(6.5, 18, 17.5, 6.5, R, 1.8),
    )


def ico_cloud() -> str:
    return _svg(
        _p("M7.5 16.5 h10 a3.2 3.2 0 0 0 .3-6.4 4.2 4.2 0 0 0-8-1.2 A3.4 3.4 0 0 0 7.5 16.5 z",
           C, 1.6, "#ecfeff"),
    )


def ico_door() -> str:
    return _svg(
        _r(5, 4, 10.5, 16, A, "#fde68a", rx=1.2),
        _c(13.2, 12, 0.7, A, A, 1),
        _p("M16.5 12 H21 L18.2 9.2 M21 12 L18.2 14.8", B, 1.6),
    )


def ico_bars(color: str, fill: str) -> str:
    return _svg(
        _r(13.5, 5.5, 6, 3.2, color, fill, rx=0.6),
        _r(10, 10.4, 9.5, 3.2, color, fill, rx=0.6),
        _r(6.5, 15.3, 13, 3.2, color, fill, rx=0.6),
    )


def ico_bars_ok() -> str:
    return ico_bars(G, "#86efac")


def ico_bars_warn() -> str:
    return ico_bars(Y, "#fde68a")


def ico_bars_err() -> str:
    return ico_bars(R, "#fca5a5")


def ico_bars_off() -> str:
    return ico_bars(S, "#cbd5e1")


def ico_hazard() -> str:
    return _svg(
        _r(5, 5, 14, 14, R, "#fecaca", rx=1.4),
        _p("M7 16.5 L16.5 7", W, 3.2),
        _p("M7 16.5 L16.5 7", R, 1.6),
    )


def ico_prohibition() -> str:
    return _svg(_c(12, 12, 7.2, R, "#fecaca"), _l(7.4, 16.6, 16.6, 7.4, R, 2))


def ico_chevrons() -> str:
    return _svg(
        _p("M10 7 L5 12 L10 17", B, 1.8),
        _p("M14 7 L19 12 L14 17", B, 1.8),
    )


def ico_tee() -> str:
    return _svg(
        _l(12, 4.5, 12, 19.5, S, 2.4),
        _r(6, 10.2, 12, 3.6, Y, "#fde68a", rx=1),
    )


def ico_eye() -> str:
    return _svg(
        _p("M3.5 12 Q12 6 20.5 12 Q12 18 3.5 12 z", S, 1.6, "#e2e8f0"),
        _c(12, 12, 2.2, S, S),
    )


def ico_plug() -> str:
    return _svg(
        _r(4, 13, 8, 5, C, "#bae6fd", rx=1.2),
        _l(7, 13, 7, 9, S, 1.6),
        _l(9, 13, 9, 9, S, 1.6),
        _p("M12 15.5 H18 V12", S, 1.6),
    )


def ico_return_arrow() -> str:
    return _svg(
        _p("M16 7 A6 6 0 1 0 16 17", G, 1.8),
        _p("M16 17 L13 14.2 M16 17 L13 19.2", G, 1.8),
        _r(10.6, 6.2, 3.2, 6.5, S, "#e2e8f0", rx=1),
    )


def ico_flags() -> str:
    return _svg(
        _p("M5 16 L5 8 L9 10 L5 12", R, 1.4, "#fecaca"),
        _p("M10 16 L10 7 L14 9.2 L10 11.4", Y, 1.4, "#fde68a"),
        _p("M15 16 L15 8 L19 10 L15 12", G, 1.4, "#bbf7d0"),
    )


def ico_connector() -> str:
    return _svg(
        _r(5, 5.5, 14, 5, Y, "#fde68a", rx=1),
        _l(7.5, 10.5, 7.5, 13, S, 1.4),
        _l(12, 10.5, 12, 13, S, 1.4),
        _l(16.5, 10.5, 16.5, 13, S, 1.4),
        _r(6.5, 13, 11, 5, S, "#e2e8f0", rx=1),
    )


def ico_info() -> str:
    return _svg(
        _c(12, 12, 7.4, V, "#ede9fe"),
        _l(12, 10.5, 12, 16.2, V, 1.8),
        _c(12, 8.2, 0.7, V, V, 1),
    )


def ico_play_pause() -> str:
    return _svg(
        _p("M4.5 7 L11 12 L4.5 17 z", G, 1.3, "#86efac"),
        _l(14.2, 7, 14.2, 17, S, 1.8),
        _l(17.8, 7, 17.8, 17, S, 1.8),
    )


def ico_sparkle() -> str:
    return _svg(
        _p("M8 6 L10 11 L15 13 L10 15 L8 20 L6 15 L1 13 L6 11 z", V, 1.2, "#ddd6fe"),
        _p("M17 4 L18 7 L21 8 L18 9 L17 12 L16 9 L13 8 L16 7 z", Y, 1.2, "#fde68a"),
    )


def ico_status_on() -> str:
    return _svg(_c(12, 12, 6.5, G, "#4ade80"))


def ico_chain() -> str:
    return _svg(
        _p("M8 10 a3.2 3.2 0 1 1 0 4", C, 1.8),
        _p("M16 10 a3.2 3.2 0 1 0 0 4", B, 1.8),
        _l(9.5, 12, 14.5, 12, C, 1.6),
    )


def ico_passthrough() -> str:
    return _svg(
        _l(4, 12, 20, 12, S, 2.2),
        _p("M8 8 L4 12 L8 16", R, 1.6),
        _p("M16 8 L20 12 L16 16", R, 1.6),
    )


def ico_spotlight() -> str:
    return _svg(
        _r(10, 4.5, 4, 3.2, C, "#bae6fd", rx=0.8),
        _p("M8 8.5 L4.5 18.5 H19.5 L16 8.5 z", V, 1.5, "#ddd6fe"),
    )


def ico_beacon() -> str:
    return _svg(
        _c(12, 6.5, 2, C, "#67e8f9"),
        _l(12, 8.5, 12, 16, S, 1.8),
        _p("M8 19.5 H16 L13.5 16 H10.5 z", Y, 1.4, "#fde68a"),
    )


def ico_aperture() -> str:
    return _svg(
        _c(12, 12, 7.2, S),
        _c(12, 12, 2.4, C, C),
        _p("M12 4.8 L14.2 10.5", S, 1.4),
        _p("M12 19.2 L9.8 13.5", S, 1.4),
    )


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

# slug → builder. Toolbar labels resolve through ALIASES.
GLYPHS: dict[str, callable] = {
    "add": ico_add,
    "remove": ico_remove,
    "enable": ico_enable,
    "disable": ico_disable,
    "comment": ico_comment,
    "filter": ico_filter,
    "find": ico_find,
    "undo": ico_undo,
    "redo": ico_redo,
    "refresh": ico_refresh,
    "copy": ico_copy,
    "paste": ico_paste,
    "back": ico_back,
    "forward": ico_forward,
    "move_up": ico_move_up,
    "move_down": ico_move_down,
    "folder": ico_folder,
    "list": ico_list,
    "table": ico_table,
    "terminal": ico_terminal,
    "props": ico_props,
    "properties": ico_props,
    "tabs": ico_tabs,
    "warning": ico_warning,
    "bridge": ico_bridge,
    "files": ico_files,
    "graph": ico_graph,
    "reply": ico_reply,
    "interfaces": ico_interfaces,
    "ip": ico_ip,
    "ipv6": ico_ipv6,
    "log": ico_log,
    "notes": ico_notes,
    "book": ico_book,
    "supout": ico_supout,
    "script": ico_script,
    "mesh": ico_mesh,
    "attach": ico_attach,
    "mpls": ico_mpls,
    "ppp": ico_ppp,
    "queues": ico_queues,
    "quickset": ico_quickset,
    "radius": ico_radius,
    "users_move": ico_users_move,
    "tools": ico_tools,
    "settings": ico_settings,
    "ports": ico_ports,
    "ping": ico_ping,
    "globe": ico_globe,
    "display": ico_display,
    "wireless": ico_wireless,
    "lcd": ico_lcd,
    "edit": ico_edit,
    "partition": ico_partition,
    "record": ico_record,
    "home": ico_home,
    "shield": ico_shield,
    "users": ico_users,
    "wireguard": ico_wireguard,
    "crypto": ico_crypto,
    "radio": ico_radio,
    "wifi": ico_wifi,
    "dot1x": ico_dot1x,
    "capsman": ico_capsman,
    "person": ico_person,
    "person_off": ico_person_off,
    "person_add": ico_person_add,
    "person_remove": ico_person_remove,
    "flag": ico_flag,
    "windows": ico_windows,
    "play": ico_play,
    "group": ico_group,
    "link": ico_link,
    "forbidden": ico_forbidden,
    "server": ico_server,
    "print": ico_print,
    "package": ico_package,
    "certificate": ico_certificate,
    "plus_box": ico_plus_box,
    "stack_1": ico_stack_1,
    "stack_2": ico_stack_2,
    "stack_3": ico_stack_3,
    "layers": ico_layers,
    "camera": ico_camera,
    "ruler": ico_ruler,
    "signal": ico_signal,
    "alert": ico_alert,
    "award": ico_award,
    "clock_gear": ico_clock_gear,
    "stop": ico_stop,
    "skip": ico_skip,
    "list_num": ico_list_num,
    "ok": ico_ok,
    "close_sm": ico_close_sm,
    "blank_page": ico_blank_page,
    "help": ico_help,
    "reset_counters": ico_reset_counters,
    "power": ico_power,
    "default": ico_default,
    "system": ico_system,
    "iface": ico_iface,
    "computers": ico_computers,
    "remote": ico_remote,
    "exchange": ico_exchange,
    "umbrella": ico_umbrella,
    "switch": ico_switch,
    "wifi_off": ico_wifi_off,
    "cloud": ico_cloud,
    "door": ico_door,
    "bars_ok": ico_bars_ok,
    "bars_warn": ico_bars_warn,
    "bars_err": ico_bars_err,
    "bars_off": ico_bars_off,
    "hazard": ico_hazard,
    "prohibition": ico_prohibition,
    "chevrons": ico_chevrons,
    "tee": ico_tee,
    "eye": ico_eye,
    "plug": ico_plug,
    "return_arrow": ico_return_arrow,
    "flags": ico_flags,
    "connector": ico_connector,
    "info": ico_info,
    "play_pause": ico_play_pause,
    "sparkle": ico_sparkle,
    "status_on": ico_status_on,
    "chain": ico_chain,
    "passthrough": ico_passthrough,
    "spotlight": ico_spotlight,
    "beacon": ico_beacon,
    "aperture": ico_aperture,
}

# Cell index → what the WinBox *sprite* actually depicts (the picture).
# .jg menus that point at a cell are listed separately in JG_USED_BY —
# Ping's groupIcon is 26, and cell 26 is the wrench.
INDEX_TO_NAME: dict[int, str] = {
    1: "bridge",
    2: "files",
    3: "graph",
    4: "reply",
    5: "interfaces",
    6: "ip",
    7: "ipv6",
    8: "log",
    9: "notes",
    10: "door",
    11: "supout",
    12: "script",
    13: "mesh",
    14: "computers",
    15: "attach",
    16: "terminal",
    17: "remote",
    18: "queues",
    19: "quickset",
    20: "radius",
    21: "redo",
    22: "exchange",
    23: "umbrella",
    24: "switch",
    25: "settings",
    26: "tools",
    27: "undo",
    28: "globe",
    29: "wireless",
    30: "lcd",
    31: "edit",
    32: "switch",
    33: "partition",
    34: "record",
    35: "home",
    36: "forward",
    37: "shield",
    38: "users",
    39: "wireguard",
    40: "crypto",
    42: "wifi",
    43: "wifi_off",
    44: "cloud",
    45: "capsman",
    64: "chevrons",
    65: "person_add",
    66: "person",
    67: "person_remove",
    68: "flags",
    69: "windows",
    70: "play",
    71: "group",
    72: "tee",
    73: "group",
    74: "hazard",
    75: "connector",
    76: "print",
    77: "settings",
    78: "package",
    79: "certificate",
    80: "comment",
    81: "status_on",
    82: "eye",
    83: "passthrough",
    84: "info",
    85: "enable",
    86: "disable",
    87: "blank_page",
    88: "undo",
    89: "back",
    90: "play_pause",
    91: "skip",
    92: "edit",
    93: "edit",
    94: "edit",
    95: "plus_box",
    96: "plus_box",
    97: "bars_ok",
    98: "bars_warn",
    99: "bars_err",
    100: "chain",
    101: "camera",
    102: "ruler",
    103: "sparkle",
    104: "alert",
    105: "plug",
    106: "spotlight",
    107: "beacon",
    108: "aperture",
    109: "prohibition",
    110: "skip",
    111: "bars_off",
    112: "ok",
    113: "close_sm",
    114: "blank_page",
    115: "redo",
    116: "wifi",
    117: "return_arrow",
}

# Short "what the pixels show" — used on the preview so we don't pretend
# a hazard stripe is a forbidden sign. Uncertain ones say so.
CELL_LOOKS: dict[int, str] = {
    10: "door with arrow in (login / enter)",
    17: "arrow into a computer (remote session)",
    21: "blue curved arrow right",
    26: "wrench — Tools; .jg Ping reuses this cell",
    32: "three ports on a plate",
    34: "red glossy ball (not sure of function)",
    45: "stylized C (CAPsMAN-like)",
    64: "inward chevrons",
    68: "red / yellow / green pennants",
    72: "T-bar / tee connector",
    74: "red hazard stripe — not the usual forbidden sign",
    75: "toothed connector over a tray",
    82: "eye",
    83: "arrows through a post (passthrough?)",
    89: "purple arrow left",
    97: "green level bars (ok / low) — not a stack",
    98: "yellow level bars (warn / mid)",
    99: "red level bars (err / high)",
    100: "two interlocking rings; .jg Reset Counters",
    105: "plug / cable into a port",
    106: "inverted cone (spotlight / coverage?)",
    107: "stand / beacon (uncertain)",
    108: "split ring / aperture (uncertain)",
    109: "classic prohibition (circle + slash)",
    111: "gray level bars (off / idle)",
    115: "gray clockwise curve — redo/rotate, not undo",
    116: "wifi fan + dot",
    117: "green U-turn around a handle — not wifi",
}

# Which .jg menus point at each cell (from the router plugin set). A cell's
# picture and the menu that uses it are not always the same idea.
JG_USED_BY: dict[int, list[str]] = {
    1: ["Bridge"],
    2: ["Files"],
    5: ["Interfaces"],
    6: ["IP / Addresses"],
    7: ["IPv6 / Addresses"],
    8: ["Log"],
    11: ["Make Supout.rif"],
    13: ["Mesh"],
    15: ["MPLS"],
    16: ["Terminal"],
    17: ["PPP"],
    18: ["Queues"],
    19: ["Quick Set"],
    20: ["RADIUS"],
    22: ["Filters"],
    24: ["Switch", "Settings"],
    25: ["Ports"],
    26: ["Ping"],
    29: ["Wireless"],
    30: ["LCD"],
    33: ["Partition"],
    39: ["WireGuard"],
    42: ["WiFi"],
    43: ["Dot1X"],
    100: ["Reset Counters"],
}

ALIASES: dict[str, str] = {
    "Add": "add",
    "New": "add",
    "Remove": "remove",
    "Delete": "remove",
    "Enable": "enable",
    "Disable": "disable",
    "Comment": "comment",
    "Refresh": "refresh",
    "Filter": "filter",
    "Find": "find",
    "Undo": "undo",
    "Redo": "redo",
    "Copy": "copy",
    "Paste": "paste",
    "Move Up": "move_up",
    "Move Down": "move_down",
    "Back": "back",
    "Forward": "forward",
    "map": "list",
    "dialog": "props",
    "item": "props",
    "query": "refresh",
    "action": "refresh",
    "doit": "enable",
    "multipage": "tabs",
    "page_group": "tabs",
    "forbidden": "prohibition",
}


def resolve_name(name: str) -> str:
    if name in GLYPHS:
        return name
    if name in ALIASES:
        return ALIASES[name]
    slug = name.strip().lower().replace(" ", "_")
    if slug in GLYPHS:
        return slug
    return slug


def svg_for_name(name: str) -> str | None:
    slug = resolve_name(name)
    fn = GLYPHS.get(slug)
    return fn() if fn else None


def svg_for_index(index: int) -> str | None:
    slug = INDEX_TO_NAME.get(index)
    if slug is None:
        return None
    return svg_for_name(slug)


_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


#: The drawable elements of a glyph, in paint order.
_MARK = re.compile(r"<(?:path|rect|circle|ellipse|line|polygon|polyline)\b[^>]*>", re.I)


def _to_symbolic_mark(tag: str, *, covered: bool) -> str:
    """One element of a glyph, re-inked.

    *covered* means something is painted after this one. A dark fill under
    later marks is a plate -- a terminal's screen, an LCD -- and in colour the
    thing on top was a different hue. In one ink, painting it solid swallows
    whatever sits on it (the terminal was a black box with an invisible
    prompt), so a covered plate becomes a wash and its outline carries the
    shape.
    """

    def fill_sub(m: re.Match[str]) -> str:
        color = m.group(1)
        if color.lower() in ("none", "currentcolor"):
            return m.group(0)
        lum = _luminance(color)
        if lum >= 0.72:
            return 'fill="currentColor" fill-opacity="0.14"'
        if lum >= 0.45 or covered:
            return 'fill="currentColor" fill-opacity="0.32"'
        return 'fill="currentColor"'

    tag = re.sub(r'fill="(#[0-9A-Fa-f]{3,8}|none|currentColor)"', fill_sub, tag, flags=re.I)
    return re.sub(r'stroke="#[0-9A-Fa-f]{3,8}"', 'stroke="currentColor"', tag)


def to_symbolic(svg: str) -> str:
    """Rewrite a color SVG to a single-ink *symbolic* glyph.

    Strokes become ``currentColor``. Washes (light fills) become the same
    ink at low opacity so the shape still reads when the UI tints it.
    This is the GNOME/SF-Symbols idea: one silhouette, any theme color.
    """
    # Drop leftover opacity from a previous pass so we can re-run safely.
    svg = re.sub(r'\sfill-opacity="[0-9.]+"', "", svg)

    marks = list(_MARK.finditer(svg))
    last = len(marks) - 1
    out: list[str] = []
    pos = 0
    for i, m in enumerate(marks):
        out.append(svg[pos:m.start()])
        out.append(_to_symbolic_mark(m.group(0), covered=i < last))
        pos = m.end()
    out.append(svg[pos:])
    svg = "".join(out)

    if "currentColor" in svg and "color=" not in svg[:80]:
        svg = svg.replace("<svg ", '<svg color="currentColor" ', 1)
    return svg


def svg_symbolic_for_name(name: str) -> str | None:
    raw = svg_for_name(name)
    return to_symbolic(raw) if raw else None


def write_svg_files(dest: Path | None = None, *, mono: bool = True) -> Path:
    """Materialize every glyph as an .svg (for hand-editing / overrides)."""
    out = dest or SVG_DIR
    out.mkdir(parents=True, exist_ok=True)
    mono_out = SVG_MONO_DIR
    if mono:
        mono_out.mkdir(parents=True, exist_ok=True)
    for slug, fn in GLYPHS.items():
        colored = fn()
        (out / f"{slug}.svg").write_text(colored + "\n", encoding="utf-8")
        if mono:
            (mono_out / f"{slug}.svg").write_text(to_symbolic(colored) + "\n", encoding="utf-8")
    return out
