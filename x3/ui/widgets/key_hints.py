"""Hold Ctrl: the keys that work *here*.

Winbox has nothing like this, and neither does Qt. A shortcut nobody can see is
a shortcut nobody uses, so the keys say so themselves: hold Ctrl for about half
a second and a panel fades up over the window in front listing exactly the keys
that window answers to. Let go and it is gone. F1 pins it open (Esc closes it).

Three decisions worth knowing about.

**Ctrl, held, with nothing else pressed.** There is no platform standard for
"long-press a modifier to see the keys" -- what *is* standard is Alt underlining
a button's letter, which the property form now also does. So this had to pick
something, and the modifier every chord starts with is the one a hand already
rests on. Every ``Ctrl+<key>`` chord cancels the timer on its way through, so a
panel never flashes at somebody who was reaching for Ctrl+E.

**The content is read off the window, not written down twice.** Every verb with
a key is a ``QAction`` -- the list toolbar's Add/Remove/Enable/Disable/Comment,
the terminal's copy and paste -- so the panel walks the focused window's actions
and prints the shortcut each one actually carries, dimmed when the action is
disabled right now. A key that changes in the code changes here; a verb that is
greyed out because nothing is selected reads as greyed out. Only the keys no
action can express (the arrows, Tab, Enter, Space) are spelled out by hand.

**It never takes the keyboard.** The panel is mouse-transparent, has no focus
policy, and is a plain child of the MDI viewport -- so nothing about showing it
disturbs what you were typing into, which is the whole point of a peek.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QLabel,
    QMdiSubWindow,
    QWidget,
)

from .. import style

#: Gap between the panel and the corner of the MDI area it sits in.
_MARGIN = 8

#: How long Ctrl has to be held down before the panel appears. Long enough that
#: every Ctrl+<key> chord passes through without a flash, short enough that
#: "hold it and see" reads as an answer rather than a delay.
HOLD_MS = 500


@dataclass(frozen=True)
class Key:
    """One row: what to press, what it does, and whether it does it now."""

    keys: str
    what: str
    enabled: bool = True


@dataclass
class Section:
    """A titled group of rows. An empty one is dropped before painting.

    ``compact`` runs the rows together into one wrapped line instead of a row
    each -- a property form's button stack is eleven letters long, and eleven
    rows of it turned the panel into a column taller than the form.
    """

    title: str
    keys: list[Key] = field(default_factory=list)
    compact: bool = False


def _plain(text: str) -> str:
    """A label without its mnemonic marker ("Co&mment" -> "Comment")."""
    from .property_window import plain_label

    return plain_label(text)


def _shortcut_text(seq: QKeySequence) -> str:
    return seq.toString(QKeySequence.SequenceFormat.NativeText)


def _action_keys(root: QWidget | None, *, seen: set[str]) -> list[Key]:
    """Every keyed ``QAction`` under `root`, in the order they were built.

    This is where the panel gets its honesty: the list's verbs and the
    terminal's are read off the actions that carry the keys, so nothing here can
    claim a key the app does not answer, and one that is greyed out right now
    (Remove with nothing selected) reads as greyed out.

    Hidden actions are left out rather than dimmed -- Move Up/Down on a list
    that is not ordered is not a key that is unavailable, it is a key that does
    not exist here. ``seen`` holds the key strings already printed, so the same
    key is never listed twice by two sections.
    """
    if root is None:
        return []
    # Qt hangs a system menu off every MDI subwindow (Ctrl+F4 to close, and
    # friends). Those are the window manager's, not this window's verbs. Held in
    # a list and compared with `is`: id() is no good here, because a PySide
    # wrapper for a QAction that nothing keeps alive is collected and the next
    # call hands out a new one with a different id.
    system = getattr(root, "systemMenu", None)
    menu = system() if callable(system) else None
    chrome = list(menu.actions()) if menu is not None else []
    out: list[Key] = []
    for act in root.findChildren(QAction):
        seq = act.shortcut()
        if seq.isEmpty() or not act.isVisible():
            continue
        if any(act is other for other in chrome):
            continue
        keys = _shortcut_text(seq)
        if keys in seen:
            continue
        seen.add(keys)
        out.append(Key(keys, _plain(act.text()) or "action", act.isEnabled()))
    return out


def _seed(seen: set[str], keys: list[Key]) -> list[Key]:
    """Remember hand-written rows' keys, so no action prints them again."""
    for key in keys:
        seen.add(key.keys)
    return keys


def _mnemonic_keys(form) -> list[Key]:
    """``Alt+O -> OK`` for each button of a form's stack that has a letter."""
    out: list[Key] = []
    texts = getattr(form, "_mnemonic_texts", None) or {}
    for btn in getattr(form, "_action_btns", None) or ():
        try:
            plain = _plain(btn.text())
            marked = texts.get(plain, "")
            enabled = btn.isEnabled()
        except RuntimeError:
            continue
        letter = _mnemonic_letter(marked)
        if letter:
            out.append(Key(f"Alt+{letter.upper()}", plain, enabled))
    return out


def _mnemonic_letter(marked: str) -> str:
    """The letter behind a single ``&`` in a label, or "" when there is none."""
    i = 0
    while i < len(marked):
        if marked[i] == "&":
            if i + 1 < len(marked) and marked[i + 1] == "&":
                i += 2
                continue
            return marked[i + 1] if i + 1 < len(marked) else ""
        i += 1
    return ""


def _ancestor(widget, kind):
    while widget is not None:
        if isinstance(widget, kind):
            return widget
        widget = widget.parentWidget()
    return None


def _subwindow(widget) -> QMdiSubWindow | None:
    return _ancestor(widget, QMdiSubWindow)


def hint_sections(focus, main_window=None) -> tuple[str, list[Section]]:
    """(heading, sections) for whatever has the keyboard right now."""
    from .list_pane import ListPane, _FilterEdit
    from .property_window import PropertyWindow

    win = _subwindow(focus)
    seen: set[str] = set()
    sections: list[Section] = []
    heading = _plain(win.windowTitle()) if win is not None else "x3"

    pane = _ancestor(focus, ListPane)
    if pane is None and win is not None:
        active = getattr(win, "active_pane", None)
        pane = active() if callable(active) else None

    if isinstance(win, PropertyWindow):
        sections.append(Section("this form", _seed(seen, _form_keys(win))))
        sections.append(Section("its buttons", _mnemonic_keys(win), compact=True))
    elif pane is not None:
        if isinstance(focus, _FilterEdit):
            sections.append(Section("the find box", _seed(seen, [
                Key("(type)", "narrow the list as you type"),
                Key("↓ / Enter", "hand the keyboard to the rows"),
                Key("Esc", "empty the find box"),
            ])))
        sections.append(Section("this list", _seed(seen, _list_keys(pane, win))))
        sections.append(Section("its verbs", _action_keys(win or pane, seen=seen)))
    elif win is not None:
        sections.append(Section("this window", _seed(seen, [
            *_window_tab_keys(win),
            Key("Esc", "close the window"),
        ])))
        sections.append(Section("its verbs", _action_keys(win, seen=seen)))

    everywhere = _seed(seen, [
        Key("Ctrl+Tab / Ctrl+Shift+Tab", "next / previous window"),
        # Written by hand rather than read off the action, because the action
        # carries three sequences for the one chord (Ctrl+= / Ctrl++ /
        # Ctrl+Shift+=) and printing all three says less than printing none.
        Key("Ctrl+= / Ctrl+-", "the whole UI a size larger / smaller"),
        Key("Ctrl+0", "back to normal size"),
        Key("F1", "pin this panel open (Esc closes it)"),
    ])
    bar = main_window.menuBar() if main_window is not None else None
    everywhere += _action_keys(bar, seen=seen)
    sections.append(Section("anywhere", everywhere))
    return heading, [s for s in sections if s.keys]


def _window_tab_keys(win) -> list[Key]:
    """The tab keys, but only for a window that has tabs to walk."""
    tabs = getattr(win, "_tab_host", None)
    if tabs is None or tabs.count() < 2:
        return []
    return [Key("Ctrl+PgDn / Ctrl+PgUp", "next / previous tab")]


def _form_keys(form) -> list[Key]:
    from .group_fold import GroupFold

    locked = bool(getattr(form, "_ro", False))
    keys = [
        Key("Tab / Shift+Tab", "next / previous field"),
        *_window_tab_keys(form),
        Key("Space", "toggle a box · flip an optional field ▼"),
        Key("Ctrl+Space", "set / unset the field you are in"),
        Key("Ctrl+wheel", "change the dropdown under the pointer", not locked),
        Key("Alt (hold)", "underline the buttons' letters"),
    ]
    if form.findChildren(GroupFold):
        keys.append(Key("→ / ←", "open / close a group fold"))
    keys += [
        Key("Enter", "OK — commit and close", not locked),
        Key("Ctrl+Enter", "Apply — commit, stay open", not locked),
        Key("Esc", "close (asks first if there are edits)"),
    ]
    return keys


def _list_keys(pane, win) -> list[Key]:
    keys = [
        Key("↑ / ↓", "walk the rows"),
        Key("Enter", "open the row's properties"),
    ]
    model = getattr(pane, "_model", None)
    tree_col = getattr(model, "tree_column", lambda: None)()
    if tree_col is not None:
        keys.append(Key("→ / ←", "open / close a tree parent"))
    keys += _window_tab_keys(win) if win is not None else []
    keys.append(Key("Esc", "close the window"))
    return keys


def _rgba(color: str, alpha: int) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


class KeyHintPanel(QFrame):
    """The panel: a heading, then key/meaning rows in titled sections.

    Painted from the theme so it belongs to whatever skin is on, and nearly
    opaque rather than fully -- enough to read as an overlay on the window it is
    about, not enough to make the text fight the form underneath it.
    """

    #: Alpha of the panel's own background, 0-255.
    _ALPHA = 244
    #: Width a compact section wraps at, which is what sets the panel's width.
    _COMPACT_W = 380

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("keyhints")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # A peek must not be in the way of the thing it is about.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(
            f"QFrame#keyhints {{ background: {_rgba(style.BG_PANEL, self._ALPHA)};"
            f" border: 1px solid {style.BORDER}; }}"
            f"QLabel {{ color: {style.TEXT}; }}"
            f'QLabel[part="keys"] {{ font-weight: 600; }}'
            f'QLabel[part="quiet"] {{ color: {style.TEXT_DIM}; '
            f"font-size: {style.small_css(9)}; }}"
        )
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(10, 7, 12, 7)
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(2)
        self._grid.setColumnStretch(1, 1)

    def set_content(self, heading: str, sections: list[Section],
                    footer: str) -> None:
        """Rebuild the panel. Cheap: a couple of dozen labels, on a keypress."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        row = 0
        title = QLabel(heading)
        title.setProperty("part", "keys")
        self._grid.addWidget(title, row, 0, 1, 2)
        row += 1
        for section in sections:
            head = QLabel(f"— {section.title} —")
            head.setProperty("part", "quiet")
            self._grid.addWidget(head, row, 0, 1, 2)
            row += 1
            if section.compact:
                self._grid.addWidget(self._compact_label(section), row, 0, 1, 2)
                row += 1
                continue
            for key in section.keys:
                left = QLabel(key.keys)
                left.setProperty("part", "keys")
                left.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                right = QLabel(key.what)
                if not key.enabled:
                    # Dim rather than dropped: "Remove is a key, and it is not
                    # available right now" is the useful answer.
                    for lab in (left, right):
                        lab.setStyleSheet(f"color: {style.TEXT_DIM};")
                self._grid.addWidget(left, row, 0)
                self._grid.addWidget(right, row, 1)
                row += 1
        note = QLabel(footer)
        note.setProperty("part", "quiet")
        self._grid.addWidget(note, row, 0, 1, 2)
        self.adjustSize()

    def _compact_label(self, section: Section) -> QLabel:
        """One wrapped line: ``Alt+O OK · Alt+C Cancel · (Alt+R Remove)``.

        A key in brackets is one that is there but not available right now --
        the same thing the dimming says in a row-per-key section, in the space a
        compact one has.
        """
        parts = [
            f"{k.keys} {k.what}" if k.enabled else f"({k.keys} {k.what})"
            for k in section.keys
        ]
        lab = QLabel(" · ".join(parts))
        lab.setWordWrap(True)
        lab.setMaximumWidth(self._COMPACT_W)
        return lab


class KeyHints(QObject):
    """Watches for Ctrl held down, and floats :class:`KeyHintPanel` over the MDI.

    One per session window. The filter is on the application (a modifier held
    on its own is nobody's shortcut, so there is nothing else to hang it on) and
    scoped to this window's own tree, or a peek in one session would light up
    every other open router.
    """

    def __init__(self, window, area, *, enabled: bool = True) -> None:
        super().__init__(window)
        self._window = window
        self._area = area
        self._panel = KeyHintPanel(area.viewport())
        self._panel.hide()
        self._pinned = False
        self._enabled = bool(enabled)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(HOLD_MS)
        self._timer.timeout.connect(lambda: self.show_panel(pinned=False))
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            # A pinned panel is about whatever has the keyboard now, not about
            # the window it was opened over.
            app.focusChanged.connect(self._on_focus_changed)

    # --- the switch -------------------------------------------------------

    def set_enabled(self, on: bool) -> None:
        """Turn the hold-Ctrl peek off. F1 still opens the panel: the setting is
        about something appearing unasked, not about hiding the help."""
        self._enabled = bool(on)
        if not self._enabled and not self._pinned:
            self.hide_panel()

    def is_enabled(self) -> bool:
        return self._enabled

    def is_visible(self) -> bool:
        return self._panel.isVisible()

    def is_pinned(self) -> bool:
        return self._pinned

    # --- showing it -------------------------------------------------------

    def show_panel(self, *, pinned: bool) -> None:
        focus = QApplication.focusWidget()
        heading, sections = hint_sections(focus, self._window)
        self._panel.set_content(
            heading, sections,
            "pinned — Esc closes it" if pinned else "holding Ctrl — F1 pins it",
        )
        self._pinned = bool(pinned)
        self._place()
        self._panel.show()
        self._panel.raise_()

    def hide_panel(self) -> None:
        self._pinned = False
        self._timer.stop()
        self._panel.hide()

    def toggle_pinned(self) -> None:
        """What F1 (and View ▸ Keyboard Keys) does."""
        if self._pinned:
            self.hide_panel()
        else:
            self.show_panel(pinned=True)

    def _on_focus_changed(self, _old, now) -> None:
        if self._pinned and now is not None and self._owns(now):
            self.show_panel(pinned=True)

    def _place(self) -> None:
        """Bottom-right of the area, out of the way of what it is about.

        Centred over the window in front reads better in a mockup and worse in
        use: it sits on the very rows and fields the keys are for. One fixed
        corner is also easier to ignore once you know what it says.
        """
        viewport = self._area.viewport()
        self._panel.adjustSize()
        w, h = self._panel.width(), self._panel.height()
        rect = viewport.rect()
        x = max(rect.left() + _MARGIN, rect.right() - w - _MARGIN)
        y = max(rect.top() + _MARGIN, rect.bottom() - h - _MARGIN)
        self._panel.move(x, y)

    # --- watching the keyboard --------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        et = event.type()
        if et == QEvent.Type.KeyPress:
            if self._on_key(obj, event):
                return True
        elif et == QEvent.Type.KeyRelease:
            if (
                event.key() == Qt.Key.Key_Control
                and not event.isAutoRepeat()
                and not self._pinned
            ):
                self.hide_panel()
        elif et == QEvent.Type.Resize and obj is self._area.viewport():
            # A pinned panel outlives the window being dragged about, and its
            # corner moves with the area.
            if self._panel.isVisible():
                self._place()
        elif et in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.WindowDeactivate,
            QEvent.Type.ApplicationDeactivate,
        ):
            # A peek that outlives the hand holding the key is a stuck overlay.
            if not self._pinned:
                self.hide_panel()
        return super().eventFilter(obj, event)

    def _on_key(self, obj, event) -> bool:
        """True to swallow the key. Only F1, ``?`` and a pinned Esc are ours."""
        if not self._owns(obj):
            return False
        key = event.key()
        if event.isAutoRepeat():
            return False        # a held Ctrl repeats; that is not a new press
        if key == Qt.Key.Key_F1:
            # A console's F1 is the router's help, which its own prompt tells
            # you to press; the panel is one View ▸ Keyboard Keys away.
            if getattr(QApplication.focusWidget(), "wants_function_keys", False):
                return False
            self.toggle_pinned()
            return True
        if key == Qt.Key.Key_Question and self._question_is_free():
            self.toggle_pinned()
            return True
        if key == Qt.Key.Key_Escape and self._pinned:
            self.hide_panel()
            return True
        if key == Qt.Key.Key_Control:
            if self._enabled and not self._pinned:
                self._timer.start()
            return False
        # Any other key is the chord Ctrl was pressed for; the panel gets out of
        # the way and does not come back until Ctrl is pressed again.
        self._timer.stop()
        if not self._pinned:
            self.hide_panel()
        return False

    @staticmethod
    def _question_is_free() -> bool:
        """Is ``?`` a spare key right now, or a character somebody is typing?

        Only on a table or a tree, where there is nothing to type into. Text
        boxes -- and the terminal, which is one big text box -- keep it.
        """
        return isinstance(QApplication.focusWidget(), QAbstractItemView)

    def _owns(self, obj) -> bool:
        """Is this event on its way into this session's window?"""
        widget = obj if isinstance(obj, QWidget) else None
        while widget is not None:
            if widget is self._window:
                return True
            parent = widget.parentWidget()
            widget = parent if parent is not None else None
        return False
