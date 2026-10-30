"""The rule Winbox draws around a group of fields, and its title.

Winbox has one piece of chrome for "these rows belong together": a hairline
across the form with the group's name sitting in it::

    ─ Rate ──────────────────────────────────
    ─ ▼ Connection Limit ────────────────────

The name may be preceded by a fold triangle (``type:group``) or stand alone
(a non-``compact`` radio group). Both are the same rule, so both are built
here -- see :mod:`x3.ui.widgets.group_fold` and
:mod:`x3.ui.widgets.radio_group`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from .. import style

#: The stub of rule Winbox draws to the left of a section's title.
_STUB = 8


def rule() -> QFrame:
    """One pixel of hairline, no bevel -- the same recipe as a separator row."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {style.BORDER}; border: none;")
    return line


def section_header(title: str, lead: QWidget | None = None) -> QWidget:
    """``─ Title ──────────``, with ``lead`` (a fold triangle) before the name."""
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    stub = rule()
    stub.setFixedWidth(_STUB)
    lay.addWidget(stub)
    if lead is not None:
        lay.addWidget(lead)
    label = QLabel(title or "")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    lay.addWidget(label)
    lay.addWidget(rule(), 1)
    return holder
