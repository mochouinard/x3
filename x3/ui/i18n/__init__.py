"""Languages: an exact-match table from the English text to another language.

One JSON file per language in this folder, ``<code>.json``::

    {"name": "Français", "colon": " :", "strings": {"Addresses": "Adresses", ...}}

``colon`` (optional, ``":"`` by default) is how the language ends a label
the table holds without its colon: French puts a space before it.

:func:`tr` looks a string up exactly as it is written -- case, punctuation and
the ``&`` of a menu mnemonic included -- and returns it unchanged when the
table has no entry, so anything not yet translated simply stays English.
Adding a language is adding a file: ``tools/i18n_catalog.py --new de`` writes
one holding every known English string, to be filled in.

The same table covers both kinds of text on screen:

* x3's own -- every dialog, button, menu and message. :func:`install` wraps
  the Qt calls that put text on a widget (``QLabel.setText``, ``QAction``,
  ``setWindowTitle``, ``addTab`` and so on) so x3's code is not touched at all.
* the router's -- menu entries, window titles, tabs, columns and field labels
  come from its ``.jg`` files in English, and go through the same calls, or
  through :func:`tr` directly where they are not widgets (the menu tree, a
  table's column headers).

What is never translated: anything the router stores or is sent. Values in
editable fields, drop-down choices and table cells are not routed through
here, so a translated screen never writes another language to a router.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGLISH = "en"

_table: dict[str, str] = {}
#: Every key the language file lists, translated or left empty on purpose.
_known: frozenset[str] = frozenset()
_colon = ":"
_active = ENGLISH
_installed = False


def available() -> dict[str, str]:
    """Language code -> its own name, for every table shipped (English first)."""
    out = {ENGLISH: "English"}
    for f in sorted(HERE.glob("*.json")):
        if f.stem == "catalog":
            continue
        try:
            out[f.stem] = str(json.loads(f.read_text(encoding="utf-8")).get("name") or f.stem)
        except (OSError, ValueError):
            continue
    return out


def system_languages() -> list[str]:
    """The desktop's languages, most preferred first, as language codes.

    The environment first, the way gettext reads it: ``LANGUAGE`` is a list
    (``fr_CA:fr:en``), then whichever of ``LC_ALL``, ``LC_MESSAGES`` and
    ``LANG`` is set. Then the display languages Qt has from the system, which
    is where Windows and macOS keep theirs: a program started from their menus
    has no ``LANG`` at all.
    """
    tags = [t for t in os.environ.get("LANGUAGE", "").split(":") if t]
    tags += [next((v for v in map(os.environ.get, ("LC_ALL", "LC_MESSAGES", "LANG")) if v), "")]
    try:
        from PySide6.QtCore import QLocale

        tags += QLocale.system().uiLanguages()
    except Exception:  # noqa: BLE001
        pass
    out: list[str] = []
    for tag in tags:
        code = re.split(r"[-_.@]", tag)[0].lower()
        if code and code not in ("c", "posix") and code not in out:
            out.append(code)
    return out


def system_language() -> str:
    """What "System default" means: the desktop's first language x3 has a
    table for, or English."""
    langs = available()
    return next((c for c in system_languages() if c in langs), ENGLISH)


def resolve(choice: str | None) -> str:
    """``auto`` (or nothing) follows the system; an unknown code is English."""
    if choice and choice != "auto":
        return choice if choice in available() else ENGLISH
    return system_language()


def choices() -> list[tuple[str, str]]:
    """A language picker's entries, ``(label, choice)``: "System default"
    naming the language it comes to, then each language under its own name.
    The labels are :class:`verbatim`, so "English" never becomes "Anglais"."""
    langs = available()
    auto = verbatim(f"{tr('System default')} ({langs[system_language()]})")
    return [(auto, "auto")] + [(verbatim(name), code) for code, name in langs.items()]


#: The language the user picked during this run (View ▸ Language, the
#: connection page), once they have. It outranks ``--lang`` from the next
#: connection page on; app.py applies it there.
picked: str | None = None


def load(code: str) -> None:
    """Make ``code`` the active language. English empties the table."""
    global _table, _active, _colon, _known
    _active = code if code in available() else ENGLISH
    _table, _colon, _known = {}, ":", frozenset()
    if _active == ENGLISH:
        return
    data = json.loads((HERE / f"{_active}.json").read_text(encoding="utf-8"))
    _colon = str(data.get("colon") or ":")
    strings = data.get("strings") or {}
    _table = {k: v for k, v in strings.items() if isinstance(v, str) and v}
    _known = frozenset(strings)


def active() -> str:
    return _active


class verbatim(str):  # noqa: N801 -- reads as a marker, not a type
    """A string :func:`tr` hands back untouched: a language's own name in
    View ▸ Language, say, which must not become "Anglais"."""


def tr(text):
    """``text`` in the active language when the table holds it exactly; else
    ``text`` itself. Anything that is not a string passes through."""
    if not _table or not isinstance(text, str) or not text or isinstance(text, verbatim):
        return text
    hit = _lookup(text)
    if hit is not None:
        return hit
    return _caption(text)


def trf(template: str, /, **values) -> verbatim:
    """``template`` in the active language, then filled in: a message with a
    count or a path in it ("{n} neighbor(s)"), which the exact-match table
    could never hold filled. The result is :class:`verbatim`, so the widget it
    is set on does not look it up again."""
    return verbatim(tr(template).format(**values))


def _lookup(text: str) -> str | None:
    hit = _table.get(text)
    if hit is not None:
        return hit
    # A mnemonic the table was written without: "&Theme" finds "Theme", and
    # the translation gets its first letter marked instead.
    if "&" in text:
        bare = text.replace("&&", "\0").replace("&", "").replace("\0", "&")
        hit = _table.get(bare)
        if hit is not None:
            hit = hit.replace("&", "&&")
            i = next((i for i, ch in enumerate(hit) if ch.isalnum()), None)
            return hit if i is None else hit[:i] + "&" + hit[i:]
    # A trailing ellipsis or colon on a label the table holds bare. A label
    # kept in English on purpose ("MTU") still takes the language's colon, so
    # a form's labels all end the same way.
    for tail in ("…", "...", ":"):
        if text.endswith(tail) and len(text) > len(tail):
            bare = text[: -len(tail)]
            hit = _table.get(bare)
            if hit is None and tail == ":" and bare in _known and _colon != ":":
                hit = bare
            if hit is not None:
                return hit + (_colon if tail == ":" else tail)
    return None


def _caption(text: str) -> str:
    """A label with something after it: ``Interface <ether1>``, ``Changed
    elsewhere — rt1``, ``Address List [read-only]``, ``Pending changes (3)``.
    Each side is looked up on its own; what sits in ``<...>`` is a name from
    the router and is kept."""
    for sep, close in ((" — ", ""), (" [", "]"), (" <", ">"), (" · ", ""), (" (", ")")):
        head, found, rest = text.partition(sep)
        if not (found and head and rest) or (close and not rest.endswith(close)):
            continue
        if sep == " <":
            tail = rest
        elif close:
            tail = tr(rest[: -len(close)]) + close
        else:
            tail = tr(rest)
        return tr(head) + found + tail
    return text


# --- Qt -------------------------------------------------------------------

#: One lowercase word -- ``on``, ``auto``, ``ether1``: what a router stores,
#: not a label. A widget showing one shows it as the router has it; x3's own
#: code may still ask for ``tr("on")`` where it means the word.
_VALUE = re.compile(r"^[a-z0-9_\-.]+$")


def _shown(text: str) -> str:
    return text if _VALUE.match(text) else tr(text)


def _wrap(cls, name, positions, *, static=False):
    """Translate the string arguments at ``positions`` of ``cls.name``."""
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_x3_tr", False):
        return

    def wrapper(*args, **kwargs):
        offset = 0 if static else 1
        args = list(args)
        for p in positions:
            i = p + offset
            if i < len(args) and isinstance(args[i], str):
                args[i] = _shown(args[i])
        return orig(*args, **kwargs)

    wrapper._x3_tr = True
    setattr(cls, name, staticmethod(wrapper) if static else wrapper)


def install() -> None:
    """Wrap the Qt calls x3 puts text through. Once, and only for a language
    other than English: an English session runs Qt exactly as it ships."""
    global _installed
    if _installed or _active == ENGLISH:
        return
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QAbstractButton, QCheckBox, QDialogButtonBox, QFileDialog, QFormLayout,
        QGroupBox, QLabel, QLineEdit, QMenu, QMenuBar, QMessageBox, QPushButton,
        QRadioButton, QTabBar, QTabWidget, QToolButton, QWidget,
    )

    for cls in (QLabel, QPushButton, QCheckBox, QRadioButton, QGroupBox):
        _wrap(cls, "__init__", (0,))
    _wrap(QAction, "__init__", (0, 1))         # (text, parent) or (icon, text, parent)
    _wrap(QMenu, "__init__", (0,))
    _wrap(QLabel, "setText", (0,))
    _wrap(QAbstractButton, "setText", (0,))
    _wrap(QToolButton, "setText", (0,))
    _wrap(QGroupBox, "setTitle", (0,))
    _wrap(QWidget, "setWindowTitle", (0,))
    _wrap(QWidget, "setToolTip", (0,))
    _wrap(QWidget, "setStatusTip", (0,))
    _wrap(QWidget, "setWhatsThis", (0,))
    _wrap(QLineEdit, "setPlaceholderText", (0,))
    for name in ("setText", "setToolTip", "setStatusTip", "setIconText"):
        _wrap(QAction, name, (0,))
    _wrap(QMenu, "setTitle", (0,))
    _wrap(QMenu, "addMenu", (0, 1))
    _wrap(QMenu, "addAction", (0, 1))
    _wrap(QMenu, "addSection", (0, 1))
    _wrap(QMenuBar, "addMenu", (0, 1))
    _wrap(QTabWidget, "addTab", (1, 2))
    _wrap(QTabWidget, "insertTab", (2, 3))
    _wrap(QTabWidget, "setTabText", (1,))
    _wrap(QTabWidget, "setTabToolTip", (1,))
    _wrap(QTabBar, "addTab", (0, 1))
    _wrap(QTabBar, "insertTab", (1, 2))
    _wrap(QTabBar, "setTabText", (1,))
    _wrap(QTabBar, "setTabToolTip", (1,))
    _wrap(QFormLayout, "addRow", (0,))
    _wrap(QDialogButtonBox, "addButton", (0,))
    _wrap(QMessageBox, "setText", (0,))
    _wrap(QMessageBox, "setInformativeText", (0,))
    _wrap(QMessageBox, "addButton", (0,))
    for name in ("information", "warning", "critical", "question"):
        _wrap(QMessageBox, name, (1, 2), static=True)
    _wrap(QMessageBox, "about", (1, 2), static=True)
    _wrap(QFileDialog, "getOpenFileName", (1, 3), static=True)
    _wrap(QFileDialog, "getSaveFileName", (1, 3), static=True)
    _wrap(QFileDialog, "getExistingDirectory", (1,), static=True)
    _installed = True
