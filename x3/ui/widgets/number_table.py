"""``numbertable`` and ``gridmultinumber`` -- a number array that is a table.

Wireless reports several things as one flat number array that is really a grid:

* ``{name:'Signal Strengths',type:'numbertable',id:'U4a',c:[
     {name:'Strength',type:'fixedpoint',id:'u1',offset:100},
     {name:'Rate',type:'enm',id:'u0',values:{…32 rates…}},
     {name:'Last Measured',type:'interval',id:'u2'}]}``

  Each row is as many consecutive elements as there are columns, at the
  offsets the column ids name -- so the array ``[16,36,3, 8,30,12]`` is two
  rows: HT20-0 at -64 dBm measured 0.03 s ago, and 6Mbps at -70 dBm. Rendered
  as one field it was a meaningless comma-run of six numbers.

* ``{name:'Tx Powers',type:'gridmultinumber',id:'U8b0',maxsize:32,
     postfix:'dBm',values:{…rate names…},c:[{type:'integer'}]}``

  Here the array is *positional*: element i is the value for the rate the
  values map calls i. Winbox draws one labelled cell per rate, which is the
  only way to edit a 28-entry table without counting commas.

The decode is checked rather than assumed (see schema.number_table_rows): if
the array is not a whole number of rows, or a column with a vocabulary holds
something outside it, the raw numbers are shown instead of a confidently
mislabelled table.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from x3.proto import message as m
from x3.proto import schema as sch

from .. import style
from .. import a11y

#: Rows shown before the table scrolls rather than growing the form.
_MAX_VISIBLE_ROWS = 6
#: Labelled cells per line in a grid.
_GRID_COLS = 4


class NumberTableView(QWidget):
    """A ``numbertable`` field, drawn as the table it is. Read-only."""

    def __init__(self, fld, value: Any = None, *, parent=None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._value = value
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._table: QTableWidget | None = None
        self._raw: QLineEdit | None = None
        rows = sch.number_table_rows(fld, value)
        if rows is None:
            # Not the shape we know: say what the device sent, plainly.
            self._raw = QLineEdit(_raw_text(value))
            self._raw.setReadOnly(True)
            a11y.part(self._raw)
            root.addWidget(self._raw)
            return
        cols = sch.number_table_columns(fld)
        table = QTableWidget(len(rows), len(cols))
        table.setHorizontalHeaderLabels(
            [col.name or f"<{code:#x}>" for code, col in cols]
        )
        style.style_data_table(table)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        table.verticalHeader().setDefaultSectionSize(style.ROW_HEIGHT)
        for r, row in enumerate(rows):
            for c, (col, cell) in enumerate(row):
                item = QTableWidgetItem(col.render(cell))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(r, c, item)
        shown = min(len(rows), _MAX_VISIBLE_ROWS)
        table.setFixedHeight(
            style.ROW_HEIGHT * (shown + 1) + 6
        )
        self._table = table
        root.addWidget(table)

    def row_count(self) -> int:
        return self._table.rowCount() if self._table is not None else 0

    def editor_value(self) -> Any:
        return self._value

    def set_locked(self, locked: bool) -> None:
        return  # a reported table is never editable

    def text(self) -> str:
        """What a caller repainting live values would compare against."""
        return _raw_text(self._value)


class NumberGridEditor(QWidget):
    """A ``gridmultinumber``: one labelled cell per named position."""

    changed = Signal()

    def __init__(self, fld, value: Any = None, *, readonly: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._readonly = readonly
        # Untouched positions are written back exactly as they arrived: the
        # vocabulary is sparse (rates 4-7 do not exist) and the array is
        # positional, so the gaps are not ours to invent.
        self._original_array = [
            _as_int(v) or 0
            for v in (value if isinstance(value, (list, tuple)) else [])
        ]
        self._boxes: dict[int, QLineEdit] = {}
        # A cell is one *element* of the array. The field's own vocabulary
        # names the array's positions, so rendering a cell through it turned a
        # Tx Power of 17 dBm into the rate called 17 -- see Field.element_view.
        self._cell = fld.element_view()

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        keys = sorted(fld.enum or {})
        for i, key in enumerate(keys):
            r, c = divmod(i, _GRID_COLS)
            label = QLabel(str((fld.enum or {}).get(key, key)))
            box = QLineEdit()
            box.setReadOnly(readonly)
            box.setMaximumWidth(60)
            if key < len(self._original_array):
                box.setText(self._cell.render(self._original_array[key]))
            box.textChanged.connect(lambda *_: self.changed.emit())
            self._boxes[key] = box
            grid.addWidget(label, r, c * 2)
            grid.addWidget(box, r, c * 2 + 1)
        self._original = self.texts()

    # --- the array this grid stands for --------------------------------

    def _array(self) -> list[int]:
        out = list(self._original_array)
        if self._boxes:
            need = max(self._boxes) + 1
            out += [0] * max(0, need - len(out))
        postfix = (self._fld.postfix or "").strip()
        for key, box in self._boxes.items():
            text = box.text().strip()
            if postfix and text.endswith(postfix):
                text = text[: -len(postfix)].strip()
            try:
                out[key] = int(text, 0) if text else 0
            except ValueError:
                continue        # left as it arrived rather than zeroed
        return out

    def texts(self) -> list[str]:
        return [b.text().strip() for b in self._boxes.values() if b.text().strip()]

    def editor_value(self) -> Any:
        return self._array()

    def wire_value(self) -> tuple[int, list]:
        # Two's complement on the way out: these cells are signed (a Tx Power
        # may be -5 dBm) and the array goes on the wire as u32s.
        return (m.T_U32_ARRAY, [v & 0xFFFFFFFF for v in self._array()])

    def label(self) -> str:
        postfix = (self._fld.postfix or "").strip()
        parts = [
            f"{(self._fld.enum or {}).get(k, k)}={b.text().strip()}"
            for k, b in self._boxes.items() if b.text().strip()
        ]
        return ("  ".join(parts) + (f" {postfix}" if postfix and parts else ""))

    def is_dirty(self) -> bool:
        return self.texts() != self._original

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-fill the cells from the array just read off the device.

        The untouched-positions image is retaken with it: those are written
        back verbatim, so keeping the pre-write copy would send the device's
        own gaps back one write out of date.
        """
        self._original_array = [
            _as_int(v) or 0
            for v in (raw if isinstance(raw, (list, tuple)) else [])
        ]
        for key, box in self._boxes.items():
            box.setText(
                self._cell.render(self._original_array[key])
                if key < len(self._original_array) else ""
            )
        self._original = self.texts()

    def is_present(self) -> bool:
        return bool(self.texts())

    def set_locked(self, locked: bool) -> None:
        self._readonly = bool(locked)
        for box in self._boxes.values():
            box.setReadOnly(locked)


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            return int(value, 0)
    except (TypeError, ValueError):
        return None
    return None


def _raw_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)
