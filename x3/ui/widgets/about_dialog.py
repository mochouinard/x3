"""Help ▸ About: what x3 is, its licence, and the licences of what it runs on.

The License tab shows the repository's ``LICENSE`` file itself
(:func:`license_text`), so there is one copy of the terms; :data:`DERIVED`
names the code that carries another project's licence, and
``tests/test_about.py`` keeps the dependency list honest. The third-party table lists
what x3 imports at run time. Nothing from those projects is copied into this
repository: each is installed by pip from its own distribution, under its own
licence, and the LGPL components (Qt, PySide6, shiboken6, pyte) stay separate,
replaceable libraries.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
)

from .. import __version__, style
from ..icons import app_icon

COPYRIGHT = "Copyright (c) 2026 Marc O. Chouinard"

#: How the licence is named in one line, wherever it is named.
LICENSE_SUMMARY = (
    "PolyForm Strict License 1.0.0: free for personal and noncommercial use; "
    "changes only as contributions to x3"
)


def license_text() -> str:
    """The ``LICENSE`` file: beside the package in a source checkout, or among
    the installed distribution's licence files."""
    from pathlib import Path

    here = Path(__file__).resolve().parents[3] / "LICENSE"
    if here.is_file():
        return here.read_text(encoding="utf-8")
    try:
        from importlib.metadata import distribution

        dist = distribution("x3")
        for name in ("licenses/LICENSE", "LICENSE"):
            text = dist.read_text(name)
            if text:
                return text
    except Exception:  # noqa: BLE001 -- a missing file must not break About
        pass
    return f"{COPYRIGHT}\n\n{LICENSE_SUMMARY}.\n\nThe LICENSE file was not found."


DISCLAIMER = (
    "x3 is an independent research project and a homage to a classic client: "
    "it is not a product, and not meant to compete with any vendor's own "
    "tools. MikroTik, RouterOS and Winbox are names used by Mikrotīkls SIA for "
    "its company, products and management protocol, and may be its "
    "trademarks; x3 uses \"Winbox\" only to name the protocol it speaks, and "
    "is not affiliated with, endorsed or sponsored by Mikrotīkls SIA. Other "
    "product names belong to their respective owners."
)


@dataclass(frozen=True)
class Component:
    name: str
    licence: str
    url: str
    role: str


#: What x3 runs on, in the order a reader cares about. Kept in step with
#: ``pyproject.toml`` and ``THIRD-PARTY-NOTICES.md``.
THIRD_PARTY: tuple[Component, ...] = (
    Component("Qt 6", "LGPL-3.0-only", "https://www.qt.io/",
              "The GUI toolkit, through Qt for Python."),
    Component("PySide6 (Qt for Python)", "LGPL-3.0-only", "https://pyside.org",
              "Python bindings for Qt."),
    Component("shiboken6", "LGPL-3.0-only", "https://pyside.org",
              "The binding generator runtime PySide6 needs."),
    Component("pyte", "LGPL-3.0", "https://github.com/selectel/pyte",
              "VT100 emulation for the terminal window."),
    Component("wcwidth", "MIT", "https://github.com/jquast/wcwidth",
              "Character widths, used by pyte."),
    Component("PyCryptodome", "BSD-2-Clause and public domain",
              "https://www.pycryptodome.org",
              "AES and SHA primitives for the encrypted session."),
    Component("python-ecdsa", "MIT", "https://github.com/tlsfuzzer/python-ecdsa",
              "Elliptic-curve arithmetic for EC-SRP5 login."),
    Component("six", "MIT", "https://github.com/benjaminp/six",
              "Python 2/3 shim, used by python-ecdsa."),
    Component("psutil", "BSD-3-Clause", "https://github.com/giampaolo/psutil",
              "Network interface addresses on Windows and macOS."),
)

#: Code in this repository that derives from another project's source, and
#: so carries that project's licence alongside x3's own.
DERIVED: tuple[Component, ...] = (
    Component("Margin Research EC-SRP reference client", "Apache-2.0",
              "https://github.com/MarginResearch/mikrotik_authentication",
              "Curve maths and session-key derivation in x3/proto/crypto.py."),
)


def third_party_html() -> str:
    rows = "".join(
        f"<tr><td><b>{escape(c.name)}</b><br>{escape(c.role)}</td>"
        f"<td>{escape(c.licence)}</td>"
        f'<td><a href="{escape(c.url)}">{escape(c.url)}</a></td></tr>'
        for c in THIRD_PARTY
    )
    return (
        "<p>x3 is installed alongside these projects and uses them unmodified, "
        "each under its own licence. Qt, PySide6, shiboken6 and pyte are "
        "LGPL-3.0 libraries: they are loaded at run time from their own "
        "packages and can be replaced with any compatible build.</p>"
        '<table cellspacing="0" cellpadding="4" width="100%">'
        "<tr><th align=left>Component</th><th align=left>Licence</th>"
        "<th align=left>Source</th></tr>"
        f"{rows}</table>"
        "<h4>Derived code</h4>"
        "<p>Parts of x3 derive from another project's source and stay under "
        "its licence; the full text is in LICENSES/Apache-2.0.txt.</p>"
        '<table cellspacing="0" cellpadding="4" width="100%">'
        + "".join(
            f"<tr><td><b>{escape(c.name)}</b><br>{escape(c.role)}</td>"
            f"<td>{escape(c.licence)}</td>"
            f'<td><a href="{escape(c.url)}">{escape(c.url)}</a></td></tr>'
            for c in DERIVED
        )
        + "</table>"
        "<p>The icons and the application mark are original artwork made for "
        "x3 and are covered by the x3 license.</p>"
    )


class AboutDialog(QDialog):
    """About / Licence / Third-party, with the live session's schema if any."""

    def __init__(self, parent=None, *, session_lines: list[str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About x3")
        self.setModal(True)
        self.resize(style.scale_px(560), style.scale_px(420))

        root = QVBoxLayout(self)

        head = QHBoxLayout()
        mark = QLabel()
        px = style.scale_px(64)
        mark.setPixmap(app_icon().pixmap(px, px))
        mark.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(mark)
        title = QLabel(
            f"<h2 style='margin:0'>x3</h2>"
            f"<p style='margin:2px 0'>Version {escape(__version__)}</p>"
            "<p style='margin:2px 0'>A desktop client and toolkit for routers "
            "managed over the Winbox protocol.</p>"
            f"<p style='margin:2px 0'>{escape(COPYRIGHT)}.<br>"
            f"{escape(LICENSE_SUMMARY)}.</p>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setWordWrap(True)
        head.addWidget(title, 1)
        root.addLayout(head)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        about = QTextBrowser()
        about.setOpenExternalLinks(True)
        session = ""
        if session_lines:
            session = (
                "<h4>This session</h4><p>"
                + "<br>".join(escape(line) for line in session_lines)
                + "</p>"
            )
        about.setHtml(
            "<p>x3 reads each router's own schema files, so its menus, lists "
            "and forms come from the device rather than from hand-written "
            "screens. It opens read-only; changes are queued for review, or "
            "sent live once you switch the session to Live.</p>"
            f"<p>{escape(DISCLAIMER)}</p>"
            "<p>This software is provided as is, without warranty of any kind. "
            "Take a backup before you change a router with it.</p>"
            f"{session}"
        )
        tabs.addTab(about, "About")

        licence = QPlainTextEdit(license_text())
        licence.setReadOnly(True)
        licence.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        tabs.addTab(licence, "License")

        third = QTextBrowser()
        third.setOpenExternalLinks(True)
        third.setHtml(third_party_html())
        tabs.addTab(third, "Third-party")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def tab_titles(self) -> list[str]:
        tabs = self.findChild(QTabWidget)
        return [tabs.tabText(i) for i in range(tabs.count())] if tabs else []
