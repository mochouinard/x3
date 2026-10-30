"""Pick several entries at once for an array-of-dropdowns field.

A bridge VLAN's Tagged list is one dropdown per port, which is how Winbox
edits it, and how :class:`~.property_window.MultiEnumArrayEditor` follows it:
to tag eight interfaces you press ``▾`` eight times and pick eight times. This
is the shortcut for that -- one list of everything the field has *not* got yet,
Ctrl-click or Shift-click as many as you want, Add.

The dialog knows nothing about the field it serves. It is handed ``(wire,
label)`` pairs and hands back the wire keys that were picked, in the order it
showed them, so the caller can insert a row per key and nothing else.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .. import a11y, style
from ..sortkey import sort_key
from .status_marks import apply_marks


class MultiPickDialog(QDialog):
    """A multi-select list of what an array field could still hold.

    Sorted the way the rows and the list columns sort -- ether2 before
    ether10 -- because a picker is read down, not searched. Above about a
    handful of choices it also grows a filter box: an interface list on a real
    device is twenty-odd entries once VLANs and bridges are in it.
    """

    #: Below this many choices a filter box is chrome for its own sake.
    _FILTER_MIN = 8

    def __init__(
        self,
        choices: list[tuple[Any, str]],
        *,
        title: str = "Add Entries",
        marks: dict | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._choices = sorted(choices, key=lambda c: sort_key(c[1]))

        root = QVBoxLayout(self)
        pad = style.MARGIN + 4
        root.setContentsMargins(pad, pad, pad, pad)
        root.setSpacing(style.SPACING + 2)

        self._filter: QLineEdit | None = None
        if len(self._choices) >= self._FILTER_MIN:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(style.SPACING + 2)
            row.addWidget(QLabel("Filter:"))
            self._filter = QLineEdit()
            self._filter.setPlaceholderText("part of a name")
            self._filter.setClearButtonEnabled(True)
            self._filter.textChanged.connect(self._apply_filter)
            a11y.name(self._filter, "Filter")
            row.addWidget(self._filter, 1)
            root.addLayout(row)

        self._list = QListWidget()
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._list.setUniformItemSizes(True)
        for wire, label in self._choices:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, wire)
            self._list.addItem(item)
        # The same dots and comments the dropdown behind this dialog shows:
        # picking six ports at once is exactly when knowing which are down
        # saves a second trip.
        apply_marks(self._list, marks or {})
        self._list.itemSelectionChanged.connect(self._sync_button)
        # A double-click means "that one, now" -- the common single add.
        self._list.itemDoubleClicked.connect(lambda *_: self.accept())
        a11y.name(self._list, "Entries to add")
        root.addWidget(self._list, 1)

        hint = QLabel("Ctrl-click or Shift-click to pick several.")
        hint.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(8)};"
        )
        root.addWidget(hint)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setDefault(True)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

        self._sync_button()
        rows = min(max(len(self._choices), 4), 14)
        self.resize(
            style.scale_px(240),
            style.scale_px(96) + rows * max(15, style.ROW_HEIGHT + 3),
        )
        if self._filter is not None:
            self._filter.setFocus()
        else:
            self._list.setFocus()

    # --- filtering -----------------------------------------------------

    def _apply_filter(self, text: str) -> None:
        """Hide what does not match, and drop it from the selection.

        A hidden row that stays selected is added by a button that says
        nothing about it: the count on Add has to be the count you can see.
        The one exception is the point of typing at all -- when exactly one
        row is left it is selected for you, so a name and Enter is an add.
        """
        needle = text.strip().casefold()
        visible: list[QListWidgetItem] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            hide = bool(needle) and needle not in item.text().casefold()
            item.setHidden(hide)
            if hide:
                item.setSelected(False)
            else:
                visible.append(item)
        if needle and len(visible) == 1:
            visible[0].setSelected(True)
        self._sync_button()

    def _sync_button(self) -> None:
        picked = self.picked()
        self._ok.setEnabled(bool(picked))
        self._ok.setText(f"Add {len(picked)}" if picked else "Add")

    # --- result --------------------------------------------------------

    def picked(self) -> list[Any]:
        """The wire keys selected, in the order the list showed them."""
        out: list[Any] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.isSelected() and not item.isHidden():
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out
