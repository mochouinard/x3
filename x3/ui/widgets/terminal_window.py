"""New Terminal MDI child: a real the router console.

Rendered as a cell grid rather than a text document. ``pyte`` keeps the
authoritative cursor position in cell coordinates, so painting a grid puts the
caret in the right place by construction; mapping onto a QTextDocument is lossy
exactly where the router is noisiest -- it emits ``\\x1b[K`` and ``\\x1b[9999B``
(cursor-down-9999) after every prompt, and pads short lines with blanks that a
document does not have.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleTextCursorEvent,
    QAccessibleTextInsertEvent,
    QAccessibleTextInterface,
    QAccessibleTextUpdateEvent,
    QColor,
    QFont,
    QFontMetricsF,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAccessibleWidget,
    QHBoxLayout,
    QLabel,
    QMdiSubWindow,
    QScrollBar,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from .. import settings, style
from ..icons import window_icon

_MISSING_PYTE = (
    "The terminal needs the 'pyte' package for VT102 emulation.\n\n"
    "    pip install pyte\n\n"
    "(it ships in the 'gui' extra: pip install -e '.[gui]')"
)

@dataclass(frozen=True)
class TermScheme:
    """How the console is painted. Purely ours: The router sends the same
    escapes -- ``\\x1b[33m`` syntax colouring, ``\\x1b[0;37;44m`` status bar --
    whatever the client looks like, so a scheme is the default ink and paper,
    the caret, and what each of pyte's colour names maps onto."""

    fg: str
    bg: str
    cursor: str
    ansi: dict


# Winbox v3: black on white, and the classic 16-colour VGA set a GDI program
# draws with. The normal half is dark enough to read on white, and the normal
# half is what the router colours its syntax with.
LIGHT = TermScheme(
    fg="#000000", bg="#ffffff", cursor="#000000",
    ansi={
        "black": "#000000", "red": "#800000", "green": "#008000",
        "brown": "#808000", "yellow": "#808000", "blue": "#000080",
        "magenta": "#800080", "cyan": "#008080", "white": "#c0c0c0",
        "brightblack": "#808080", "brightred": "#ff0000", "brightgreen": "#00a000",
        "brightbrown": "#a0a000", "brightyellow": "#a0a000", "brightblue": "#0000ff",
        "brightmagenta": "#ff00ff", "brightcyan": "#00a0a0", "brightwhite": "#ffffff",
    },
)

# Winbox 4: light on near-black, Tango colours.
DARK = TermScheme(
    fg="#d0d0d0", bg="#101010", cursor="#c8c8c8",
    ansi={
        "black": "#2e3436", "red": "#cc0000", "green": "#4e9a06",
        "brown": "#c4a000", "yellow": "#c4a000", "blue": "#3465a4",
        "magenta": "#75507b", "cyan": "#06989a", "white": "#d3d7cf",
        "brightblack": "#555753", "brightred": "#ef2929", "brightgreen": "#8ae234",
        "brightbrown": "#fce94f", "brightyellow": "#fce94f", "brightblue": "#729fcf",
        "brightmagenta": "#ad7fa8", "brightcyan": "#34e2e2", "brightwhite": "#eeeeec",
    },
)

#: Colour names that vanish on each scheme's own paper when no background is
#: set. Painted in the default ink instead, as a light-background terminal does.
_INVISIBLE = {
    id(LIGHT): {"white", "brightwhite"},
    id(DARK): {"black"},
}


def scheme_for(choice: str) -> TermScheme:
    """``light`` | ``dark`` | ``auto`` -- the last follows the theme's window."""
    if choice == "light":
        return LIGHT
    if choice == "dark":
        return DARK
    return LIGHT if QColor(style.BG_WINDOW).lightnessF() >= 0.5 else DARK


def translate_key(key: int, modifiers, text: str) -> bytes:
    """Qt key event -> the bytes a router console expects.

    Module level and free of widgets so it can be tested without a
    QApplication. ``modifiers`` may be a Qt flag or a plain int -- PySide6
    refuses int() on a KeyboardModifier, so never convert one.
    """
    k = Qt.Key
    mods = getattr(modifiers, "value", modifiers) or 0
    ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier.value)

    simple = {
        int(k.Key_Return): b"\r",
        int(k.Key_Enter): b"\r",
        int(k.Key_Backspace): b"\x7f",
        int(k.Key_Tab): b"\t",
        int(k.Key_Backtab): b"\x1b[Z",
        int(k.Key_Escape): b"\x1b",
        int(k.Key_Up): b"\x1b[A",
        int(k.Key_Down): b"\x1b[B",
        int(k.Key_Right): b"\x1b[C",
        int(k.Key_Left): b"\x1b[D",
        int(k.Key_Home): b"\x1b[H",
        int(k.Key_End): b"\x1b[F",
        int(k.Key_PageUp): b"\x1b[5~",
        int(k.Key_PageDown): b"\x1b[6~",
        int(k.Key_Insert): b"\x1b[2~",
        int(k.Key_Delete): b"\x1b[3~",
        int(k.Key_F1): b"\x1bOP",
        int(k.Key_F2): b"\x1bOQ",
        int(k.Key_F3): b"\x1bOR",
        int(k.Key_F4): b"\x1bOS",
    }
    if key in simple:
        return simple[key]
    # Ctrl+A..Z -> 0x01..0x1a, so Ctrl+C interrupts and Ctrl+D exits.
    if ctrl and int(k.Key_A) <= key <= int(k.Key_Z):
        return bytes([key - int(k.Key_A) + 1])
    if text:
        return text.encode("utf-8")
    return b""


class _TerminalAccessible(QAccessibleWidget, QAccessibleTextInterface):
    """A read-only text interface over a terminal that is entirely painted.

    Qt publishes a widget tree to AT-SPI, and for a widget that draws its own
    cells there is nothing in that tree but an empty box: a screen reader on
    the console heard "terminal" and not one character of what the router said.
    A text interface is what fills it in -- the same one a text editor exposes,
    so an assistive client's ordinary review keys work on the console with no
    special support at either end.

    Read-only on purpose. Selection and the caret are reported, not accepted:
    the caret in a terminal belongs to the device (the router moves it with
    escape sequences, and the console's own line editor owns the line being
    typed), so letting a client move it would be a lie. Text goes in the way it
    goes in for everyone -- by typing.
    """

    def __init__(self, view: "TerminalView") -> None:
        QAccessibleWidget.__init__(self, view, QAccessible.Role.Terminal)

    def _view(self) -> "TerminalView":
        return self.widget()

    def textInterface(self) -> QAccessibleTextInterface:  # noqa: N802
        return self

    # -- naming ---------------------------------------------------------

    def text(self, *args):
        """Both texts Qt asks for, told apart by their arguments.

        ``text(start, end)`` is the text interface asking for a range;
        ``text(QAccessible.Text.X)`` is the widget interface asking for a name
        or a value. One Python method has to answer both.
        """
        if len(args) == 2:
            start, end = args
            body = self._view().a11y_text()
            return body[max(0, start):max(0, end)]
        what = args[0]
        if what == QAccessible.Text.Name:
            window = self._view().window()
            title = window.windowTitle() if window is not None else ""
            return title or "Terminal"
        return QAccessibleWidget.text(self, what)

    # -- the text --------------------------------------------------------

    def characterCount(self) -> int:  # noqa: N802
        return len(self._view().a11y_text())

    def cursorPosition(self) -> int:  # noqa: N802
        return self._view().a11y_caret()

    def characterRect(self, offset: int):  # noqa: N802
        """Where a character is on screen, for magnifiers and braille routing."""
        view = self._view()
        lines = view.a11y_lines()
        row, seen = 0, 0
        for i, text in enumerate(lines):
            if offset <= seen + len(text):
                row, offset = i, offset - seen
                break
            seen += len(text) + 1
            row = i
        w, h = view._cell_w, view._cell_h
        top_left = view.mapToGlobal(QPoint(int(offset * w), int(row * h)))
        return QRect(top_left.x(), top_left.y(), int(w), int(h))

    def offsetAtPoint(self, point) -> int:  # noqa: N802
        view = self._view()
        local = view.mapFromGlobal(point)
        col, row = view._cell_at(local)
        lines = view.a11y_lines()
        if not (0 <= row < len(lines)):
            return -1
        return sum(len(t) + 1 for t in lines[:row]) + min(col, len(lines[row]))

    def attributes(self, offset: int):  # noqa: N802
        """No per-character attributes are published (colour is not meaning).

        The span still has to be the whole text: a client that is told a run is
        one character long will ask again for every character in the screen.
        """
        return "", 0, self.characterCount()

    # -- selection: reported, never set ---------------------------------

    def selectionCount(self) -> int:  # noqa: N802
        return 0

    def selection(self, index: int):
        return 0, 0

    def addSelection(self, start: int, end: int) -> None:  # noqa: N802
        pass

    def removeSelection(self, index: int) -> None:  # noqa: N802
        pass

    def setSelection(self, index: int, start: int, end: int) -> None:  # noqa: N802
        pass

    def setCursorPosition(self, position: int) -> None:  # noqa: N802
        pass

    def scrollToSubstring(self, start: int, end: int) -> None:  # noqa: N802
        pass


#: Interfaces handed to Qt, kept alive here. Qt caches the interface it is
#: given and deletes it when the widget goes; the Python object it wraps has to
#: outlive that, or the cache is left holding a corpse.
_TERMINAL_IFACES: dict[int, _TerminalAccessible] = {}
_FACTORY_INSTALLED = False


def _terminal_a11y_factory(key: str, obj):
    """Qt's hook for "who reads this widget out?", answered for terminals only."""
    if isinstance(obj, TerminalView):
        iface = _TerminalAccessible(obj)
        _TERMINAL_IFACES[id(obj)] = iface
        obj.destroyed.connect(lambda *_a, k=id(obj): _TERMINAL_IFACES.pop(k, None))
        return iface
    return None


def _install_terminal_a11y() -> None:
    """Register the factory once, when the first terminal is built.

    Installed lazily rather than at import: a factory is global to the
    application, and this module is imported by anything that touches a
    session, whether or not a console is ever opened.
    """
    global _FACTORY_INSTALLED
    if not _FACTORY_INSTALLED:
        _FACTORY_INSTALLED = True
        QAccessible.installFactory(_terminal_a11y_factory)


class TerminalView(QWidget):
    """Cell-grid renderer over a pyte screen."""

    input_ready = Signal(bytes)
    size_changed = Signal(int, int)
    #: (lines above the view, lines scrolled back) — drives the scroll bar.
    scroll_changed = Signal(int, int)

    def __init__(self, cols: int = 100, rows: int = 24, parent=None) -> None:
        super().__init__(parent)
        import pyte

        self._cols, self._rows = cols, rows
        # pyte.Screen takes (columns, lines) here but (lines, columns) in
        # resize(). Getting that backwards is the easiest bug to introduce.
        self._screen = pyte.HistoryScreen(cols, rows, history=2000, ratio=0.5)
        self._stream = pyte.ByteStream(self._screen)

        font = QFont("DejaVu Sans Mono")
        if not font.exactMatch():
            font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(8, style.FONT_PT))
        self.setFont(font)
        self._measure()
        self._colours = settings.load_terminal_colours()

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self._blink = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(600)
        self._blink_timer.timeout.connect(self._toggle_blink)
        self._blink_timer.start()
        # Debounce the winch: dragging an MDI corner would emit dozens.
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._emit_size)
        # Mouse selection, as (col, row) cell coordinates on the visible
        # screen. Stream selection, like a terminal: from the anchor cell to
        # the head cell reading left-to-right, top-to-bottom -- not a
        # rectangle.
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_head: tuple[int, int] | None = None
        self._selecting = False
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        # Scrollback: how many lines above the live screen are showing.
        # 0 is the bottom, where a terminal normally sits. The lines
        # themselves are pyte's -- HistoryScreen.index() banks each line as it
        # scrolls off the top -- but paging through them is ours: pyte's
        # prev_page/next_page move half a screen at a time and rewrite the
        # buffer, which a scroll bar and a live feed both fight with.
        self._scroll = 0
        #: The screen as a reader was last told it reads. The diff against it
        #: is what becomes a text-inserted event (see _tell_reader).
        self._said_text = ""
        _install_terminal_a11y()

    def _measure(self) -> None:
        fm = QFontMetricsF(self.font())
        self._cell_w = max(1.0, fm.horizontalAdvance("M"))
        self._cell_h = max(1.0, fm.lineSpacing())
        self._ascent = fm.ascent()

    def _toggle_blink(self) -> None:
        self._blink = not self._blink
        x = int(self._screen.cursor.x * self._cell_w)
        y = int(self._screen.cursor.y * self._cell_h)
        self.update(QRect(x, y, int(self._cell_w) + 2, int(self._cell_h) + 2))

    # --- data in -------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Push device output through the emulator. GUI thread only."""
        before = len(self._screen.history.top)
        self._stream.feed(data)
        if self._scroll > 0:
            # Someone is reading back through the scrollback. Hold the same
            # lines under their eyes as new ones arrive, rather than yanking
            # the view. (Once the history deque is full at its maxlen the
            # oldest lines drop and this can no longer tell how many, so the
            # view drifts by that much -- only ever at the very top of a full
            # 2000-line buffer.)
            grew = len(self._screen.history.top) - before
            if grew > 0:
                self.set_scroll(self._scroll + grew)
        self._emit_scroll()
        self._tell_reader()

    # --- scrollback ----------------------------------------------------

    def max_scroll(self) -> int:
        """How far back there is to go, in lines."""
        return len(self._screen.history.top)

    def scroll_offset(self) -> int:
        return self._scroll

    def set_scroll(self, lines: int) -> None:
        """Show ``lines`` lines above the live screen. 0 is the bottom."""
        want = max(0, min(int(lines), self.max_scroll()))
        if want == self._scroll:
            return
        self._scroll = want
        # The selection is held in document coordinates, so it stays on the
        # text it was made on rather than sliding to whatever scrolled under
        # it.
        self.update()
        self._emit_scroll()
        # Paging the history changes what "the screen" is, and a reader is
        # reading the screen.
        self._tell_reader()

    def scroll_by(self, lines: int) -> None:
        self.set_scroll(self._scroll + lines)

    def scroll_to_bottom(self) -> None:
        self.set_scroll(0)

    def _emit_scroll(self) -> None:
        self.scroll_changed.emit(self.max_scroll(), self._scroll)

    # The scrollback and the live screen are one document, addressed by
    # absolute line number: banked history first, then the screen. The
    # viewport is a window over it, and a selection is a range in it -- which
    # is what lets Select All mean all of it, and lets a selection survive
    # being scrolled.

    def doc_length(self) -> int:
        return len(self._screen.history.top) + self._rows

    def _doc_start(self) -> int:
        """Document line showing at the top of the viewport."""
        ntop = len(self._screen.history.top)
        return ntop - min(self._scroll, ntop)

    def _doc_line(self, index: int):
        ntop = len(self._screen.history.top)
        index = max(0, min(index, self.doc_length() - 1))
        if index < ntop:
            return self._screen.history.top[index]
        return self._screen.buffer[index - ntop]

    def _line_text(self, line) -> str:
        """One line as text, padded to the full width like pyte's display."""
        return "".join(line[x].data or " " for x in range(self._cols))

    def _doc_text(self, index: int) -> str:
        return self._line_text(self._doc_line(index))

    def _visible_lines(self) -> list:
        """The ``rows`` lines under the viewport, scrollback included."""
        start = self._doc_start()
        return [self._doc_line(start + i) for i in range(self._rows)]

    def visible_text(self) -> list[str]:
        return [self._line_text(ln) for ln in self._visible_lines()]

    def document_text(self) -> str:
        """Everything there is: the whole scrollback and the live screen.

        Trailing blanks go per line and blank lines go from both ends -- a
        terminal's screen is padded out to its height, and nobody wants 20
        empty lines pasted after their output.
        """
        lines = [self._doc_text(i).rstrip() for i in range(self.doc_length())]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def screen_text(self) -> str:
        """What is on screen — which is the scrollback when scrolled back."""
        if self._scroll <= 0:
            return "\n".join(self._screen.display)
        return "\n".join(self.visible_text())

    def cursor_pos(self) -> tuple[int, int]:
        return self._screen.cursor.x, self._screen.cursor.y

    # --- what a screen reader is given ---------------------------------
    #
    # This widget paints, so there is nothing for an assistive client to read
    # unless we hand it something: see _TerminalAccessible below. What we hand
    # it is the *visible screen*, not the whole scrollback.
    #
    # The scrollback would be the richer answer and it is the wrong one here.
    # Offsets into it would have to be recomputed against up to 2000 banked
    # lines on every chunk the device sends, and a chunk can arrive many times
    # a second -- so the cost of being readable would land on the feed path,
    # which is the one path in this window that has to stay cheap. The screen
    # is 24 lines: reading it is free, and paging back through the history is
    # already a keystroke away (PgUp), which reports itself as the text having
    # changed. A reader gets the same view of the terminal that a sighted user
    # has, which is the honest answer anyway.

    def a11y_lines(self) -> list[str]:
        """The screen as a reader should hear it: no padding, no empty tail.

        pyte pads every line out to the full width and every screen out to its
        full height. Read literally that is a wall of spaces after each command
        and twenty "blank"s after the prompt.
        """
        lines = [text.rstrip() for text in self.visible_text()]
        last = self._screen.cursor.y if self._scroll <= 0 else 0
        for i in range(len(lines) - 1, -1, -1):
            if lines[i]:
                last = max(last, i)
                break
        return lines[:last + 1]

    def a11y_text(self) -> str:
        return "\n".join(self.a11y_lines())

    def a11y_caret(self) -> int:
        """Where the cursor is as an offset into :meth:`a11y_text`.

        Scrolled back there is no cursor on screen, so the caret sits at the
        start of what is showing -- which is where a reader wants to begin.
        """
        if self._scroll > 0:
            return 0
        lines = self.a11y_lines()
        row = min(self._screen.cursor.y, max(0, len(lines) - 1))
        offset = sum(len(text) + 1 for text in lines[:row])
        return offset + min(self._screen.cursor.x, len(lines[row]) if lines else 0)

    @staticmethod
    def reader_delta(old: str, new: str) -> tuple[str, int, str] | None:
        """How to describe the screen going from ``old`` to ``new``.

        ``("insert", at, text)``, ``("replace", 0, text)``, or None for no
        change. A screen reader on a terminal lives off text-inserted events:
        that is how output gets read out as it arrives, rather than only when
        someone goes looking for it. Ordinary console output only ever appends,
        so the common case is one insert. Anything else -- a redrawn prompt, a
        cleared screen, a page of scrollback -- really is the whole text being
        replaced, and saying so is better than inventing an insert that would
        have the reader read the screen back to front.
        """
        if new == old:
            return None
        if old and new.startswith(old):
            return ("insert", len(old), new[len(old):])
        if not old:
            return ("insert", 0, new)
        return ("replace", 0, new)

    def _tell_reader(self) -> None:
        """Send that description, and say where the cursor ended up."""
        old, new = self._said_text, self.a11y_text()
        self._said_text = new
        if not QAccessible.isActive():
            # Baseline kept current anyway: whatever is on screen when a reader
            # does attach is its starting point, not an insert.
            return
        delta = self.reader_delta(old, new)
        if delta is None:
            return
        kind, at, text = delta
        QAccessible.updateAccessibility(
            QAccessibleTextInsertEvent(self, at, text) if kind == "insert"
            else QAccessibleTextUpdateEvent(self, at, old, text)
        )
        QAccessible.updateAccessibility(
            QAccessibleTextCursorEvent(self, self.a11y_caret())
        )

    # --- painting ------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(
            int(self._cols * self._cell_w) + 2, int(self._rows * self._cell_h) + 2
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        """The smallest grid worth showing, not the current one.

        Without this the layout takes sizeHint as the floor, so the window
        could be grown but never shrunk back -- 100x24 of monospace is a wide
        window to be stuck with."""
        return QSize(int(20 * self._cell_w) + 2, int(4 * self._cell_h) + 2)

    @property
    def scheme(self) -> TermScheme:
        # Resolved per paint, so ``auto`` follows a theme switch with no signal.
        return scheme_for(self._colours)

    def set_colours(self, choice: str) -> None:
        """``auto`` | ``light`` | ``dark``; repaints, does not save."""
        self._colours = choice
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setFont(self.font())
        scheme = self.scheme
        painter.fillRect(self.rect(), QColor(scheme.bg))
        cw, ch = self._cell_w, self._cell_h
        lines = self._visible_lines()
        for y in range(self._rows):
            top = y * ch
            if top > self.height():
                break
            line = lines[y]
            # Group each row into runs of identical styling, so a coloured
            # prompt costs a few drawText calls rather than one per glyph.
            run: list[str] = []
            run_x = 0
            run_style: tuple | None = None
            for x in range(self._cols):
                cell = line[x]
                st = (cell.fg, cell.bg, cell.bold, cell.reverse)
                if st != run_style:
                    self._flush(painter, run, run_x, top, run_style, scheme)
                    run, run_x, run_style = [], x, st
                run.append(cell.data or " ")
            self._flush(painter, run, run_x, top, run_style, scheme)

        # Selection wash, over the text and under the caret.
        span = self._selection_span()
        if span is not None:
            wash = QColor(style.SEL_BG)
            wash.setAlpha(110)
            doc_top = self._doc_start()
            for y in range(self._rows):
                cols = self._row_is_selected(doc_top + y)
                if cols is None:
                    continue
                painter.fillRect(
                    QRect(int(cols[0] * cw), int(y * ch),
                          int((cols[1] - cols[0] + 1) * cw) + 1, int(ch) + 1),
                    wash,
                )

        # The caret. pyte owns the position, so this lands correctly even after
        # the \x1b[9999B that the router emits with every prompt.
        # The caret belongs to the live screen; scrolled back, there is no
        # cursor where you are looking.
        cur = self._screen.cursor
        if self._scroll <= 0 and self.hasFocus() and not cur.hidden and self._blink:
            cx, cy = int(cur.x * cw), int(cur.y * ch)
            painter.fillRect(QRect(cx, cy, max(2, int(cw)), int(ch)), QColor(scheme.cursor))
            try:
                under = lines[cur.y][cur.x].data
            except (IndexError, KeyError):
                under = " "
            if under and under != " ":
                painter.setPen(QColor(scheme.bg))
                painter.drawText(cx, int(cy + self._ascent), under)
        painter.end()

    def _flush(self, painter, run, x, top, st, scheme: TermScheme) -> None:
        if not run or st is None:
            return
        fg, bg, bold, reverse = st
        if bg == "default" and not reverse and fg in _INVISIBLE[id(scheme)]:
            fg = "default"
        fg_col = scheme.ansi.get(str(fg)) or (scheme.fg if fg == "default" else f"#{fg}")
        bg_col = scheme.ansi.get(str(bg)) or (scheme.bg if bg == "default" else f"#{bg}")
        if reverse:
            fg_col, bg_col = bg_col, fg_col
        if bg_col != scheme.bg:
            painter.fillRect(
                QRect(int(x * self._cell_w), int(top),
                      int(len(run) * self._cell_w) + 1, int(self._cell_h) + 1),
                QColor(bg_col),
            )
        text = "".join(run)
        if text.strip():
            font = painter.font()
            if font.bold() != bool(bold):
                font.setBold(bool(bold))
                painter.setFont(font)
            painter.setPen(QColor(fg_col))
            painter.drawText(int(x * self._cell_w), int(top + self._ascent), text)

    # --- selection -----------------------------------------------------

    def _cell_at(self, pos) -> tuple[int, int]:
        """A widget position as (column, document line)."""
        col = int(pos.x() // self._cell_w)
        row = int(pos.y() // self._cell_h)
        col = max(0, min(self._cols - 1, col))
        row = max(0, min(self._rows - 1, row))
        return col, self._doc_start() + row

    def _selection_span(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """(start, end) cells in reading order, or None when nothing is held."""
        if self._sel_anchor is None or self._sel_head is None:
            return None
        a, b = self._sel_anchor, self._sel_head
        if (a[1], a[0]) <= (b[1], b[0]):
            return a, b
        return b, a

    def has_selection(self) -> bool:
        return self._selection_span() is not None

    def clear_selection(self) -> None:
        if self._sel_anchor is None and self._sel_head is None:
            return
        self._sel_anchor = self._sel_head = None
        self.update()

    def select_all(self) -> None:
        """Everything, scrollback included -- not just what is on screen."""
        self._sel_anchor = (0, 0)
        self._sel_head = (self._cols - 1, self.doc_length() - 1)
        self.update()

    def select_screen(self) -> None:
        """Just the lines under the viewport."""
        start = self._doc_start()
        self._sel_anchor = (0, start)
        self._sel_head = (self._cols - 1, start + self._rows - 1)
        self.update()

    def _row_is_selected(self, row: int) -> tuple[int, int] | None:
        """The (first, last) selected column on a document line, inclusive."""
        span = self._selection_span()
        if span is None:
            return None
        (sx, sy), (ex, ey) = span
        if row < sy or row > ey:
            return None
        first = sx if row == sy else 0
        last = ex if row == ey else self._cols - 1
        return (first, last) if first <= last else None

    def selected_text(self) -> str:
        """The held text, trailing blanks trimmed per line like a terminal."""
        span = self._selection_span()
        if span is None:
            return ""
        (_sx, sy), (_ex, ey) = span
        last = self.doc_length() - 1
        out: list[str] = []
        for row in range(max(0, sy), min(ey, last) + 1):
            cols = self._row_is_selected(row)
            if cols is None:
                continue
            out.append(self._doc_text(row)[cols[0]:cols[1] + 1].rstrip())
        return "\n".join(out)

    def copy_selection(self) -> bool:
        return self._to_clipboard(self.selected_text())

    def copy_all(self) -> bool:
        """Copy the whole scrollback without having to select it first."""
        return self._to_clipboard(self.document_text())

    @staticmethod
    def _to_clipboard(text: str) -> bool:
        if not text:
            return False
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        return True

    def clear_screen(self) -> None:
        """Wipe the screen and its scrollback, leaving the session alone.

        Local only -- the device is not told, because this is the client's view
        being cleared, not a command being run.
        """
        self.clear_selection()
        self._screen.history.top.clear()
        self._screen.history.bottom.clear()
        self._scroll = 0
        self._screen.reset()
        self._screen.resize(self._rows, self._cols)   # lines, columns
        self.update()
        self._emit_scroll()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._selecting = True
        self._sel_anchor = self._sel_head = self._cell_at(ev.position())
        self.update()
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if not self._selecting:
            super().mouseMoveEvent(ev)
            return
        head = self._cell_at(ev.position())
        if head != self._sel_head:
            self._sel_head = head
            self.update()
        ev.accept()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(ev)
            return
        self._selecting = False
        # A click with no drag is "put the caret here" in every other widget;
        # here it just means "drop the selection".
        if self._sel_anchor == self._sel_head:
            self._sel_anchor = self._sel_head = None
        self.update()
        ev.accept()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        """Double-click selects the word under the cursor."""
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(ev)
            return
        col, row = self._cell_at(ev.position())
        line = self._doc_text(row)
        if col >= len(line) or not line[col].strip():
            return
        start = end = col
        while start > 0 and line[start - 1].strip():
            start -= 1
        while end + 1 < len(line) and line[end + 1].strip():
            end += 1
        self._sel_anchor, self._sel_head = (start, row), (end, row)
        self.update()
        ev.accept()

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        from PySide6.QtWidgets import QApplication, QMenu

        menu = QMenu(self)
        act_copy = menu.addAction("Copy")
        act_copy.setShortcut(QKeySequence("Ctrl+Shift+C"))
        act_copy.setEnabled(self.has_selection())
        act_copy.triggered.connect(self.copy_selection)
        act_copy_all = menu.addAction("Copy All")
        act_copy_all.setShortcut(QKeySequence("Ctrl+Shift+X"))
        act_copy_all.setToolTip("The whole scrollback, not just what is on screen")
        act_copy_all.setEnabled(bool(self.document_text()))
        act_copy_all.triggered.connect(self.copy_all)
        act_paste = menu.addAction("Paste")
        act_paste.setShortcut(QKeySequence("Ctrl+Shift+V"))
        act_paste.setEnabled(bool(QApplication.clipboard().text()))
        act_paste.triggered.connect(self.paste_clipboard)
        menu.addSeparator()
        act_all = menu.addAction("Select All")
        act_all.setShortcut(QKeySequence("Ctrl+Shift+A"))
        act_all.setToolTip("Everything, scrollback included")
        act_all.triggered.connect(self.select_all)
        act_screen = menu.addAction("Select Screen")
        act_screen.setToolTip("Only the lines currently in view")
        act_screen.triggered.connect(self.select_screen)
        act_none = menu.addAction("Clear Selection")
        act_none.setEnabled(self.has_selection())
        act_none.triggered.connect(self.clear_selection)
        menu.addSeparator()
        act_clear = menu.addAction("Clear Screen")
        act_clear.setShortcut(QKeySequence("Ctrl+Shift+L"))
        act_clear.triggered.connect(self.clear_screen)
        menu.addSeparator()
        colours = menu.addMenu("Colours")
        for choice, label in (("auto", "Follow Theme"),
                              ("light", "Light"),
                              ("dark", "Dark")):
            act = colours.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._colours == choice)
            act.triggered.connect(lambda _=False, c=choice: self._choose_colours(c))
        menu.exec(ev.globalPos())
        ev.accept()

    def _choose_colours(self, choice: str) -> None:
        """Save the choice and repaint every open terminal with it."""
        from PySide6.QtWidgets import QApplication

        settings.save_terminal_colours(choice)
        for w in QApplication.allWidgets():
            if isinstance(w, TerminalView):
                w.set_colours(choice)

    def paste_clipboard(self) -> None:
        """Send the clipboard as typed input, newlines normalised to Enter."""
        from PySide6.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        if not text:
            return
        data = text.replace("\r\n", "\r").replace("\n", "\r")
        self.input_ready.emit(data.encode("utf-8", "replace"))

    # --- input ---------------------------------------------------------

    #: Read by the key-hints panel: F1 here is the console's, not the app's.
    wants_function_keys = True

    def event(self, ev) -> bool:
        # F1-F12 bare are the console's keys -- F1 is the help the router's own
        # banner says to press. Claiming them at ShortcutOverride is what stops
        # View ▸ Keyboard Keys (F1) firing instead; they then arrive at
        # keyPressEvent and go out through translate_key.
        if (
            ev.type() == QEvent.Type.ShortcutOverride
            and int(Qt.Key.Key_F1) <= ev.key() <= int(Qt.Key.Key_F12)
            and not (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                       | Qt.KeyboardModifier.AltModifier))
        ):
            ev.accept()
            return True
        # Tab / Shift+Tab never reach keyPressEvent -- Qt's focus chain takes
        # them first, and Tab completion is the most-used the router CLI key.
        # Ctrl+Tab is not that key: it is how the keyboard leaves this window
        # for the next one, which has to work from inside a console too.
        if (
            ev.type() == QEvent.Type.KeyPress
            and ev.key() in (int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab))
            and not (ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        ):
            self._send(ev)
            return True
        return super().event(ev)

    #: Editing shortcuts, on Ctrl+Shift so the plain Ctrl+letter codes stay
    #: available to the shell -- Ctrl+C is SIGINT, not Copy, in a terminal.
    _EDIT_KEYS = {
        int(Qt.Key.Key_C): "copy",
        int(Qt.Key.Key_V): "paste",
        int(Qt.Key.Key_A): "select_all",
        int(Qt.Key.Key_X): "copy_all",
        int(Qt.Key.Key_L): "clear",
    }

    #: Lines per wheel notch, the usual terminal step.
    _WHEEL_LINES = 3

    def wheelEvent(self, ev) -> None:  # noqa: N802
        steps = ev.angleDelta().y() / 120.0
        if not steps:
            super().wheelEvent(ev)
            return
        self.scroll_by(int(round(steps * self._WHEEL_LINES)))
        ev.accept()

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        # Shift+PageUp / PageDown page the scrollback. Unshifted they are the
        # shell's keys, and the router uses them.
        if ev.modifiers() == Qt.KeyboardModifier.ShiftModifier:
            page = max(1, self._rows - 1)
            if ev.key() == int(Qt.Key.Key_PageUp):
                self.scroll_by(page)
                ev.accept()
                return
            if ev.key() == int(Qt.Key.Key_PageDown):
                self.scroll_by(-page)
                ev.accept()
                return
            if ev.key() == int(Qt.Key.Key_Home):
                self.set_scroll(self.max_scroll())
                ev.accept()
                return
            if ev.key() == int(Qt.Key.Key_End):
                self.scroll_to_bottom()
                ev.accept()
                return
        want = (Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier)
        if ev.modifiers() == want:
            what = self._EDIT_KEYS.get(int(ev.key()))
            if what is not None:
                {"copy": self.copy_selection, "paste": self.paste_clipboard,
                 "select_all": self.select_all, "copy_all": self.copy_all,
                 "clear": self.clear_screen}[what]()
                ev.accept()
                return
        # Typing replaces the selection's meaning: it is not an insertion
        # point, and leaving it highlighted over scrolled-away text is a lie.
        if self.has_selection() and ev.text():
            self.clear_selection()
        # ...and it goes to the prompt, so the prompt is what you should be
        # looking at.
        if self._scroll and ev.text():
            self.scroll_to_bottom()
        self._send(ev)

    def _send(self, ev) -> None:
        data = translate_key(ev.key(), ev.modifiers(), ev.text())
        if data:
            self.input_ready.emit(data)
            ev.accept()

    # --- geometry ------------------------------------------------------

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._resize_timer.start()

    def _emit_size(self) -> None:
        cols = max(20, int(self.width() // self._cell_w))
        rows = max(4, int(self.height() // self._cell_h))
        if (cols, rows) == (self._cols, self._rows):
            return
        self._cols, self._rows = cols, rows
        self._screen.resize(rows, cols)   # lines, columns
        self.set_scroll(self._scroll)     # re-clamp against the new height
        self.size_changed.emit(cols, rows)
        self._emit_scroll()
        self.update()


class TerminalWindow(QMdiSubWindow):
    """MDI child hosting one console."""

    #: Coalesce device output: The router emits many small frames and pyte is pure
    #: Python, so one feed and one repaint per tick is what keeps /export usable.
    _DRAIN_MS = 16
    _DRAIN_MAX = 65536

    def __init__(self, client: Any = None, *, seq: int = 1, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Terminal <{seq}>" if seq > 1 else "Terminal")
        self.setWindowIcon(window_icon("terminal"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.client = client
        self.view: TerminalView | None = None
        self._channel = None
        self._timer: QTimer | None = None
        # The reader thread only appends here; a QTimer on the GUI thread drains
        # it. A deque append is atomic, so no Qt object is touched off-thread.
        self._inbox: deque[bytes] = deque()

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        try:
            import pyte  # noqa: F401
        except ImportError:
            note = QLabel(_MISSING_PYTE)
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            note.setStyleSheet(f"color: {style.TEXT_DIM}; padding: 16px;")
            layout.addWidget(note)
            self.setWidget(container)
            self.resize(style.scale_px(560), style.scale_px(200))
            return

        self.view = TerminalView(100, 24)
        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        body_lay.addWidget(self.view, 1)
        # Scrollback bar. Value counts lines from the top of the history, so
        # the bottom -- where a terminal lives -- is the maximum, and it stays
        # pinned there as output arrives.
        self.scrollbar = QScrollBar(Qt.Orientation.Vertical)
        self.scrollbar.setRange(0, 0)
        self.scrollbar.setSingleStep(1)
        self.scrollbar.valueChanged.connect(self._on_scrollbar)
        body_lay.addWidget(self.scrollbar, 0)
        self.view.scroll_changed.connect(self._on_view_scrolled)
        self._on_view_scrolled(0, 0)   # nothing banked yet: no handle to grab
        layout.addWidget(body, 1)
        # Status line plus a real grab handle. The MDI frame is a 4px band and
        # the terminal fills everything inside it, so the corner is the only
        # place a drag reliably lands on the window rather than on the grid.
        foot = QWidget()
        foot_lay = QHBoxLayout(foot)
        foot_lay.setContentsMargins(0, 0, 0, 0)
        foot_lay.setSpacing(4)
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)}; padding: 1px 4px;"
        )
        foot_lay.addWidget(self._status, 1)
        foot_lay.addWidget(QSizeGrip(self), 0,
                           Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        layout.addWidget(foot, 0)
        self.setWidget(container)
        # Small enough that the window can be dragged down to a corner strip;
        # the grid reflows and the device is told (see _on_resize).
        self.setMinimumSize(style.scale_px(240), style.scale_px(120))
        self.resize(style.scale_px(760), style.scale_px(460))

        self.view.input_ready.connect(self._on_input)
        self.view.size_changed.connect(self._on_resize)

        self._timer = QTimer(self)
        self._timer.setInterval(self._DRAIN_MS)
        self._timer.timeout.connect(self._drain)

        # Escape is a terminal key, so it must not close the window the way it
        # does elsewhere; the frame button and Ctrl+Shift+W do that.
        close = QShortcut(QKeySequence("Ctrl+Shift+W"), self)
        close.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        close.activated.connect(self.close)

        if client is None:
            self._status.setText("not connected")
            return
        self._open()

    # --- channel -------------------------------------------------------

    def _open(self) -> None:
        assert self.view is not None
        try:
            self._channel = self.client.open_terminal(
                cols=self.view._cols,
                rows=self.view._rows,
                on_output=self._inbox.append,   # reader thread: deque only
                on_closed=self._on_channel_closed,
            )
        except Exception as e:  # noqa: BLE001
            self._status.setText(f"terminal failed: {type(e).__name__}: {e}")
            return
        self._status.setText(
            f"session {self._channel.sid}  ·  {self.view._cols}x{self.view._rows}"
        )
        if self._timer is not None:
            self._timer.start()
        self.view.setFocus()

    def _drain(self) -> None:
        if self.view is None:
            return
        chunks: list[bytes] = []
        total = 0
        while self._inbox and total < self._DRAIN_MAX:
            chunk = self._inbox.popleft()
            chunks.append(chunk)
            total += len(chunk)
        if not chunks:
            return
        self.view.feed(b"".join(chunks))
        self.view.update()

    # --- scrollback ----------------------------------------------------

    def _on_view_scrolled(self, total: int, offset: int) -> None:
        """Follow the view: range is the history depth, value is the position."""
        if self.view is None:
            return
        bar = self.scrollbar
        blocked = bar.blockSignals(True)
        try:
            bar.setRange(0, max(0, total))
            bar.setPageStep(max(1, self.view._rows))
            bar.setValue(max(0, total - offset))
        finally:
            bar.blockSignals(blocked)
        # Nothing to scroll is nothing to show a handle for.
        bar.setEnabled(total > 0)

    def _on_scrollbar(self, value: int) -> None:
        if self.view is None:
            return
        self.view.set_scroll(self.scrollbar.maximum() - value)

    def _on_input(self, data: bytes) -> None:
        if self._channel is None:
            return
        try:
            self._channel.write(data)
        except Exception as e:  # noqa: BLE001
            self._status.setText(f"write failed: {type(e).__name__}: {e}")

    def run_line(self, text: str, *, delay_ms: int = 400) -> None:
        """Type one command into this console, once it has a prompt.

        Used by the `termopen` dialog (Tools ▸ Telnet, and the right-click
        Telnet / SSH / MAC Telnet on an ARP or Neighbors row): the connection
        is made from the router's own console, which is the one console this
        app has. The delay is for the prompt -- writing before the router has
        drawn one loses the line.
        """
        line = str(text or "").strip()
        if not line:
            return
        QTimer.singleShot(delay_ms, lambda: self._on_input((line + "\r").encode()))

    def _on_resize(self, cols: int, rows: int) -> None:
        if self._channel is not None:
            try:
                self._channel.resize(cols, rows)
            except Exception:  # noqa: BLE001
                pass
        sid = self._channel.sid if self._channel is not None else "-"
        self._status.setText(f"session {sid}  ·  {cols}x{rows}")

    def _on_channel_closed(self, err: BaseException | None) -> None:
        # Called on the reader thread: touch the deque and nothing else.
        self._inbox.append(
            b"\r\n[disconnected]\r\n" if err is None
            else f"\r\n[reader: {type(err).__name__}: {err}]\r\n".encode()
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._timer is not None:
            self._timer.stop()
        chan, self._channel = self._channel, None
        if chan is not None and self.client is not None:
            try:
                self.client.close_terminal(chan)
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
