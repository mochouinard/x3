"""What this app says to a screen reader.

Qt hands out most of an accessible UI for free: a ``QAction`` names its toolbar
button, a ``QTableView`` publishes its cells as a table with column headers, a
``QComboBox`` publishes its choices. Everything here is for the places where
that free ride stops -- and in a Winbox clone there are four kinds of them.

**Fields with no name.** ``QFormLayout.addRow(str, widget)`` makes the label a
*buddy* of the field, and a field with no accessible name of its own answers
with its buddy's text. Property forms cannot use that overload: a counter
called ``Tx&Rx 64`` is a name, not a mnemonic, and the buddy path paints the
ampersand as an underline (see ``property_window._row_label``). So the label
goes in as a widget, the buddy link is never made, and every field on every
form is anonymous -- a screen reader reads "edit, 1500" with no way to learn
that it is the MTU. :func:`label_editor` puts the name back, and does it for
whatever inside a composite editor actually takes the keyboard.

**Meaning carried by paint.** A flags column that reads ``RX``, a comment drawn
into the row above the item, a signal strength drawn as a coloured bar. The
text exists in the model; it just never reaches a cell's accessible name.
:func:`describe` is how a widget carries it.

**State carried by paint.** An amber wash for a field you have edited, a greyed
box for one with no value, a triangle for a group folded shut, a white pill for
the tab you are on. Each is a word, and a widget can be in several at once, so
:func:`state` accumulates them rather than overwriting.

**Things that only arrive.** A list finishing a re-read, a commit failing, Safe
Mode arming. Sighted users get a status line, a tint, a stripe. There is no
focus change to hang those on, so nothing is spoken. :func:`announce` says them
out loud (Qt 6.8+ ``QAccessibleAnnouncementEvent``), and :class:`SpokenLabel` is
for a status line that many places write to. The judgement in using either is
what to leave out: nothing that repeats on a timer belongs in speech.

One kind of gap is not here. A widget that paints its own content rather than
its own chrome -- the terminal -- needs a whole interface of its own, not a
name; that lives beside the widget it belongs to (see
``terminal_window._TerminalAccessible``).

Nothing here costs anything when no assistive tech is listening:
``QAccessible.updateAccessibility`` returns immediately while the framework is
inactive, and the setters are plain property writes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent
from PySide6.QtWidgets import QAbstractButton, QGroupBox, QLabel, QWidget


def active() -> bool:
    """Is anything actually listening? (Orca, Accerciser, a test harness.)"""
    return QAccessible.isActive()


def plain(text: str | None) -> str:
    """A label's text as it should be *spoken*.

    Form labels end in a colon and section captions can be padded out with
    spaces; neither belongs in speech. Ampersands stay literal -- see the
    module docstring for why this app never lets Qt treat them as mnemonics.
    """
    out = (text or "").strip()
    while out.endswith(":"):
        out = out[:-1].rstrip()
    return " ".join(out.split())


def _intrinsic_name(widget: QWidget) -> str:
    """The name Qt would answer with on its own, if any.

    A check box beside its caption, a group box with a title and a label all
    name themselves from their text. Those must not be overwritten with the
    row's name: "Disabled" on the box is better than "ARP" around it.
    """
    if isinstance(widget, QAbstractButton):
        return widget.text()
    if isinstance(widget, QGroupBox):
        return widget.title()
    if isinstance(widget, QLabel):
        return widget.text()
    return ""


def name(widget: QWidget | None, text: str, *, force: bool = False) -> None:
    """Name a widget, unless it already has a name worth keeping."""
    if widget is None or not text:
        return
    if not force and (widget.accessibleName() or _intrinsic_name(widget)):
        return
    widget.setAccessibleName(plain(text))


def describe(widget: QWidget | None, text: str, *, force: bool = True) -> None:
    """Give a widget the prose a sighted user gets from its paint.

    Description is the second thing a screen reader reads, after the name, so
    this is where an expanded flags column, a comment banner or a bar's
    rendered reading goes. A tool tip already lands here (Qt uses it as the
    description when there is none), which is why ``force`` defaults on: what
    we compute is better than the hover text.
    """
    if widget is None:
        return
    if not force and widget.accessibleDescription():
        return
    widget.setAccessibleDescription(" ".join((text or "").split()))


#: Qt property naming what a part of a composite editor *is* -- the ``!``
#: button of a negatable field, the triangle that sets an optional one. The row
#: supplies the subject ("ARP Timeout"), the part supplies the rest.
PART = "a11yPart"


def part(widget: QWidget | None, suffix: str = "") -> None:
    """Say which part of its row a sub-widget is.

    An empty suffix marks the part that *is* the row's value, and takes the
    row's name unchanged -- a screen reader should read the box of an optional
    field as "ARP Timeout", not "ARP Timeout 1 of 2".
    """
    if widget is not None:
        widget.setProperty(PART, suffix or "")


def pair(label: QWidget | None, editor: QWidget | None) -> None:
    """Make a form label the editor's buddy, for the AT-SPI label relation.

    An accessible name is not enough on Unix. Qt's combo box answers Name with
    its own current text there and expects the platform to take the field name
    from the label *relation* instead, so a named-but-buddyless dropdown reads
    as "enabled, combo box" -- the value twice and the setting never.
    ``QLabel.setBuddy`` is what Qt exports as ``LABELLED_BY``.

    The ampersand is why this app could not simply use ``addRow(str, widget)``
    and get the buddy for free: a lone ``&`` in a buddy's text is a mnemonic,
    so the counter ``Tx&Rx 64`` would paint with an underline under the R
    and quietly claim Alt+R. Doubling it first keeps the paint exact
    -- a label renders ``&&`` as one literal ampersand -- and leaves no
    mnemonic for the buddy to claim.
    """
    if not isinstance(label, QLabel) or editor is None:
        return
    text = label.text()
    if "&" in text:
        label.setText(text.replace("&&", "&").replace("&", "&&"))
    label.setBuddy(editor)


#: Qt property holding the state words currently on a widget, joined by "|".
#: Kept beside the description rather than parsed back out of it, so a word can
#: be dropped again without guessing which comma belonged to it.
STATES = "a11yStates"


def state(widget: QWidget | None, word: str, on: bool = True) -> None:
    """Add or drop one word from what a widget says about itself.

    States are the things a form draws instead of writing: an amber wash for a
    field you have edited, a greyed box for one that is unset, an arrow for a
    group that is folded shut. Each is a word, and a widget can be in more than
    one at once -- an edited field can also be the unset one -- so they
    accumulate into the description rather than overwriting it.
    """
    if widget is None or not word:
        return
    words = [w for w in str(widget.property(STATES) or "").split("|") if w]
    if on and word not in words:
        words.append(word)
    elif not on and word in words:
        words.remove(word)
    else:
        return
    widget.setProperty(STATES, "|".join(words))
    describe(widget, ", ".join(words))


def focusables(widget: QWidget, *, limit: int = 24) -> list[QWidget]:
    """The descendants of a composite editor that can take the keyboard.

    An editor built out of parts (a range's two spin boxes, an address list's
    line edits, a negatable field's box and its ``!`` button) is what the form
    row points at, but the keyboard lands on the parts. Those are the widgets
    that need the row's name.
    """
    out: list[QWidget] = []
    stack: list[QWidget] = [widget]
    while stack and len(out) < limit:
        w = stack.pop(0)
        for child in w.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
            if child.focusPolicy() != Qt.FocusPolicy.NoFocus:
                out.append(child)
            stack.append(child)
    return out


def label_editor(editor: QWidget | None, label: str, *, hint: str = "") -> None:
    """Attach a form row's label to the editor the row hands over.

    The editor itself gets the name whether or not it takes focus -- a screen
    reader walking the form by object still has to be able to say what the row
    is -- and then :func:`renumber` spreads it over the parts inside.
    """
    if editor is None:
        return
    text = plain(label)
    if not text:
        return
    name(editor, text)
    if hint:
        describe(editor, hint, force=False)
    renumber(editor)


def renumber(editor: QWidget | None) -> None:
    """Spread an editor's own name over the parts that take the keyboard.

    A part that said what it is (:func:`part`) gets "<row> <part>"; parts
    sharing a role are counted off, so an address list reads "Networks entry 2
    of 3" rather than three identical boxes. A part that names itself (a
    captioned check box) is left alone, and anything still anonymous is
    numbered.

    Editors that grow and shrink -- the ``+``/``×`` lists -- call this again
    after every add and remove, which is why the row's name is read back off
    the editor rather than passed in: by then only the widget knows it.
    """
    if editor is None:
        return
    text = plain(editor.accessibleName())
    if not text:
        return
    roles: list[tuple[QWidget, str]] = []
    anonymous: list[QWidget] = []
    for w in focusables(editor):
        role = w.property(PART)
        if role is not None:
            roles.append((w, str(role)))
        elif not (w.accessibleName() or _intrinsic_name(w)):
            anonymous.append(w)
    seen: dict[str, int] = {}
    for _w, role in roles:
        seen[role] = seen.get(role, 0) + 1
    used: dict[str, int] = {}
    for w, role in roles:
        total = seen[role]
        used[role] = n = used.get(role, 0) + 1
        stem = f"{text} {role}".strip()
        name(w, stem if total == 1 else f"{stem} {n} of {total}", force=True)
    if len(anonymous) == 1:
        name(anonymous[0], text, force=True)
        return
    total = len(anonymous)
    for i, w in enumerate(anonymous, start=1):
        name(w, f"{text} {i} of {total}", force=True)


class SpokenLabel(QLabel):
    """A status label that says what it changed to.

    A status line is the app talking. A sighted user notices the words change
    out of the corner of their eye; a screen reader user is told nothing, because
    a label changing its text is not an event anything listens for. This makes
    it one.

    Only for labels that change when something *happens* -- a scan finishing, a
    connection reaching a stage, a save landing. A label a timer writes to is a
    different thing entirely, and putting one of those in here would leave the
    window talking over itself forever.
    """

    def setText(self, text: str) -> None:  # noqa: N802
        changed = text != self.text()
        super().setText(text)
        if changed:
            announce(self, text)


#: The last few things the app tried to say, oldest first. Qt offers no way to
#: read an announcement back once it is dispatched -- and dispatches nothing at
#: all while no assistive client is listening -- so this is the only record of
#: what the app said out loud. Tests read it; so can anyone wondering why a
#: window was chattering.
_said: list[str] = []

#: How much of that to keep. Long enough to see a burst in order.
SAID_LIMIT = 32


def said() -> list[str]:
    """What the app has tried to say, oldest first."""
    return list(_said)


def forget_said() -> None:
    _said.clear()


def announce(widget: QWidget | None, message: str) -> None:
    """Say something that no focus change would have said.

    Politely: the default priority queues behind whatever the user is reading,
    which is what we want for "43 rows" arriving while they are still hearing
    the row they were on. Nothing that repeats on a timer belongs here -- a
    1 Hz counter would talk over everything, forever.
    """
    if widget is None or not message:
        return
    text = " ".join(message.split())
    _said.append(text)
    del _said[:-SAID_LIMIT]
    if not QAccessible.isActive():
        # Nothing is listening. Recorded above anyway: what the app decided to
        # say is worth testing whether or not a bridge is there to say it.
        return
    QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(widget, text))
