"""Command Line…: The router line for one row, asked of the router itself.

Right-click a row, Command Line…, and the router's own ``export terse`` line
for that object comes up, ready to copy:

    /ip firewall filter add action=accept chain=input comment="allow ping" ...

Exact because the router writes it (see DeviceClient.command_line_for). The
one thing this side supplies is where to look: the .jg names no CLI path, so
candidates are made from the menu's place in the nav -- IP ▸ Firewall ▸ Filter
Rules gives /ip firewall filter -- and the router says which one exists.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import style

#: How many paths one lookup tries at most.
MAX_CANDIDATES = 16


def _segment_forms(title: str) -> list[str]:
    """CLI spellings a nav title may stand for, likeliest first.

    "Filter Rules" -> filter, filter-rule, filter-rules; "Addresses" ->
    address, addresses; "DHCP Server" -> dhcp-server.
    """
    base = re.sub(r"[^a-z0-9 -]", "", title.lower().replace(".", "")).strip()
    base = re.sub(r"\s+", " ", base)
    if not base:
        return []
    forms: list[str] = []
    for suffix in (" rules", " rule"):
        if base.endswith(suffix):
            forms.append(base[: -len(suffix)])
    if base.endswith("sses") or base.endswith("xes"):
        forms.append(base[:-2])
    elif base.endswith("s") and not base.endswith("ss"):
        forms.append(base[:-1])
    forms.append(base)
    out: list[str] = []
    for f in forms:
        f = f.replace(" ", "-")
        if f and f not in out:
            out.append(f)
    return out


def cli_candidates(crumbs: list[list[str]]) -> list[str]:
    """Candidate CLI paths for a list, from its nav breadcrumbs.

    Each breadcrumb is the titles from the top of the nav down to the list,
    e.g. ["IP", "Firewall", "Filter Rules"]. A title repeated by its own child
    (IP ▸ Services ▸ Services, Interfaces ▸ Interface) counts once.
    """
    out: list[str] = []
    for crumb in crumbs:
        parts: list[str] = []
        for title in crumb:
            # A folder and the list inside it often share a word, singular or
            # plural: Interfaces ▸ Interface is /interface, not twice over.
            first = (_segment_forms(title) or [""])[0]
            if not parts or first != (_segment_forms(parts[-1]) or [""])[0]:
                parts.append(title)
        forms = [_segment_forms(t) or [""] for t in parts]
        for combo in itertools.product(*forms):
            path = "/" + " ".join(c for c in combo if c)
            if path != "/" and path not in out:
                out.append(path)
            if len(out) >= MAX_CANDIDATES:
                return out
    return out


def line_settings(line: str) -> list[tuple[str, str]]:
    """``key=value`` pairs of an export line, in its own order.

    "/ip firewall filter add action=accept chain=input comment="allow ping""
    gives action/accept, chain/input, comment/allow ping. A ``set [ find
    default-name=ether1 ]`` selector is which object, not a setting.
    """
    import shlex

    out: list[tuple[str, str]] = []
    for one in str(line or "").splitlines():
        body = re.sub(r"^/[a-z0-9 -]+? (add|set|remove)\b\s*", "", one.strip())
        body = re.sub(r"^\[[^\]]*\]\s*", "", body)
        try:
            tokens = shlex.split(body)
        except ValueError:
            tokens = body.split()
        for tok in tokens:
            key, sep, value = tok.partition("=")
            if sep and key:
                out.append((key, value))
    return out


class _LookupWorker(QThread):
    done = Signal(object, str)   # (line or None, error)

    def __init__(self, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: N802
        try:
            self.done.emit(self._fn(), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, f"{type(e).__name__}: {e}")


class CommandLineDialog(QDialog):
    """Shows the line once the router has given it; Copy puts it on the clipboard."""

    def __init__(self, title: str, lookup: Callable[[], str | None], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Command Line — {title}")
        self.resize(style.scale_px(640), style.scale_px(160))
        lay = QVBoxLayout(self)
        self._note = QLabel("Asking the router…")
        lay.addWidget(self._note)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self._copy = QPushButton("Copy")
        self._copy.setEnabled(False)
        self._copy.clicked.connect(self.copy)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(self._copy)
        row.addWidget(close)
        lay.addLayout(row)
        self._worker = _LookupWorker(lookup, self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, line: object, error: str) -> None:
        if error:
            self._note.setText(f"The router could not be asked: {error}")
            return
        if not line:
            self._note.setText(
                "The router printed no line for this object -- its CLI path could "
                "not be worked out from the menu, or it exports nothing (a "
                "default or dynamic entry)."
            )
            return
        self._note.setText("From the router's own export (terse):")
        self.text.setPlainText(str(line))
        self._copy.setEnabled(True)
        self._copy.setFocus()

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())
        self._note.setText("Copied to the clipboard.")

    def closeEvent(self, event) -> None:  # noqa: N802
        # A console lookup in flight is left to finish rather than torn down.
        if self._worker.isRunning():
            self._worker.done.disconnect(self._on_done)
            self._worker.setParent(None)
            self._worker.finished.connect(self._worker.deleteLater)
        super().closeEvent(event)
