"""Winbox's list filter: rows of field / operator / value, under the funnel.

Every list has one, as in Winbox v3: Firewall ▸ "Action is drop", Addresses ▸
"Interface is bridge", DHCP Leases ▸ "Host Name contains phone". Rows add up
(all must hold), and the list narrows as they are typed.

The field box is the one a long list needs help with: a Firewall rule has
seventy-odd columns. It is a box you type into, and what you type narrows the
choices -- "dst" leaves Dst. Address, Dst. Port, Dst. Address List... -- the
same as clicking its arrow and scrolling.
"""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QHBoxLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import style

#: The operators, as Winbox words them.
OPERATORS = ("is", "is not", "contains", "contains not")


def row_matches(cells: list, rules: Iterable[tuple[int, str, str]]) -> bool:
    """Whether a row's displayed cells satisfy every ``(column, op, value)``.

    On what the list shows, case-blind: "drop" is the Action cell's text, and
    a filter is a way of looking, not a query to the router.
    """
    for col, op, value in rules:
        cell = str(cells[col] if 0 <= col < len(cells) else "").strip().lower()
        want = value.strip().lower()
        if op == "is":
            ok = cell == want
        elif op == "is not":
            ok = cell != want
        elif op == "contains":
            ok = want in cell
        elif op == "contains not":
            ok = want not in cell
        else:
            ok = True
        if not ok:
            return False
    return True


class FieldPicker(QComboBox):
    """A column chooser you can type into: the typing narrows the list."""

    def __init__(self, names: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems(names)
        self._names = list(names)
        completer = QCompleter(QStringListModel(names, self), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Anywhere in the name, not just its start: "addr" finds Src. Address.
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setMaxVisibleItems(16)
        self.setCompleter(completer)
        self.setMinimumWidth(style.scale_px(150))
        self.lineEdit().setPlaceholderText("field…")
        self.lineEdit().editingFinished.connect(self._settle)
        self._last = self.currentText()

    def field(self) -> str:
        text = self.currentText().strip()
        return text if text in self._names else ""

    def _settle(self) -> None:
        """A name that is not a column snaps back to the last one that was."""
        text = self.currentText().strip()
        match = next((n for n in self._names if n.lower() == text.lower()), None)
        if match is None:
            self.setCurrentText(self._last)
            return
        if match != text:
            self.setCurrentText(match)
        self._last = match


class FilterRow(QWidget):
    """One condition. ``changed`` on any edit; ``removed`` from its "−",
    ``add_below`` from its "+"."""

    changed = Signal()
    removed = Signal(object)
    add_below = Signal(object)

    def __init__(
        self,
        names: list[str],
        values_for: Callable[[str], list[str]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._values_for = values_for
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.picker = FieldPicker(names)
        self.op = QComboBox()
        self.op.addItems(OPERATORS)
        self.value = QComboBox()
        # A dropdown of what the column holds -- Action offers accept, drop,
        # jump -- that also takes anything typed, an address or part of one.
        self.value.setEditable(True)
        self.value.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.value.setMinimumWidth(style.scale_px(160))
        self.value.lineEdit().setPlaceholderText("value…")
        drop = QToolButton()
        drop.setText("−")
        drop.setToolTip("Remove this condition")
        drop.clicked.connect(lambda: self.removed.emit(self))
        # On the row, beside its "−", as Winbox has it: a "+" on a line of its
        # own under the rows read as part of no condition.
        more = QToolButton()
        more.setText("+")
        more.setToolTip("Add a condition below this one (every one must hold)")
        more.clicked.connect(lambda: self.add_below.emit(self))
        lay.addWidget(self.picker, 2)
        lay.addWidget(self.op)
        lay.addWidget(self.value, 3)
        lay.addWidget(drop)
        lay.addWidget(more)
        self.picker.currentTextChanged.connect(self._field_changed)
        self.op.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.value.currentTextChanged.connect(lambda _t: self.changed.emit())
        self._field_changed(self.picker.currentText())

    def _field_changed(self, _text: str) -> None:
        name = self.picker.field()
        typed = self.value.currentText()
        self.value.blockSignals(True)
        self.value.clear()
        if name:
            self.value.addItems(self._values_for(name))
        self.value.setCurrentText(typed)
        self.value.blockSignals(False)
        self.changed.emit()

    def rule(self) -> tuple[str, str, str] | None:
        """``(field, op, value)``, or None while it is not a whole condition."""
        name = self.picker.field()
        value = self.value.currentText()
        if not name or not value.strip():
            return None
        return name, self.op.currentText(), value


class FilterPanel(QWidget):
    """The rows, and the "+" that adds one. ``changed`` when any rule moves."""

    changed = Signal()

    def __init__(
        self,
        names: Callable[[], list[str]],
        values_for: Callable[[str], list[str]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._names = names
        self._values_for = values_for
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(2, 1, 2, 1)
        self._lay.setSpacing(2)
        self.rows: list[FilterRow] = []

    def add_row(self, *, focus: bool = False, after: FilterRow | None = None) -> FilterRow:
        row = FilterRow(self._names(), self._values_for)
        row.changed.connect(self.changed)
        row.removed.connect(self._remove_row)
        row.add_below.connect(lambda r: self.add_row(focus=True, after=r))
        at = self.rows.index(after) + 1 if after in self.rows else len(self.rows)
        self._lay.insertWidget(at, row)
        self.rows.insert(at, row)
        if focus:
            row.picker.setFocus()
            row.picker.lineEdit().selectAll()
        self.changed.emit()
        return row

    def _remove_row(self, row: FilterRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        if not self.rows:
            # The "+" lives on the rows, so the panel keeps one to start from;
            # the funnel is what closes it.
            self.add_row(focus=True)
            return
        self.changed.emit()

    def rules(self) -> list[tuple[str, str, str]]:
        """The whole conditions, while the panel is shown; none while hidden."""
        if self.isHidden():
            return []
        return [r for r in (row.rule() for row in self.rows) if r is not None]
