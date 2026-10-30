"""``numbertable`` / ``gridmultinumber`` -- number arrays that are grids.

No device and no pytest required:

    .venv/bin/python test_number_tables.py

Three wireless fields are declared this way and all three were rendering as a
comma-run of raw numbers in a text box:

* Registration ▸ **Signal Strengths** (``U4a``) -- (strength, rate, last
  measured) per row.
* Wireless ▸ **Current Tx Powers** (``Uc4e``) -- (rate, tx, real, total).
* Wireless ▸ **Tx Powers** (``U8b0``, a ``gridmultinumber``) -- positional:
  element i is the power for the rate the vocabulary calls i.

The two shapes are different and the .jg says which is which. A numbertable's
children carry their *offset within the row* as their id (``u0``, ``u1``,
``u2``) and their declaration order is the column order -- Signal Strengths
declares Strength (offset 1) before Rate (offset 0). A gridmultinumber has no
children with ids at all: its vocabulary names the array's own positions.

The row split is checked, not assumed. An array that is not a whole number of
rows, or whose Rate column holds something the vocabulary does not name, is
shown as the numbers it is -- never as a table of confidently mislabelled
ones. (That check is also what rules out the other reading of the same nodes,
one bit-packed element per row: it would put a rate index of 200 in a column
that only names 0-31.)
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QLineEdit

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.schema import Schema
from x3.ui.widgets.number_table import NumberGridEditor, NumberTableView

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"FAIL {label}: {detail}")


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

schema = Schema.load(SCHEMA_DIR)
HWTYPE = 0x0007D1
wireless = schema.record_for((20, 0), 35, {HWTYPE: 12})
registration = next(mn for mn in schema.menus if mn.path == (88, 1))

strengths = registration.fields[0x004A]
tx_powers = wireless.fields[0x0C4E]
tx_grid = wireless.fields[0x08B0]


# --- the shapes -------------------------------------------------------------

check("a numbertable's columns keep their declared order",
      [c.name for _o, c in sch.number_table_columns(strengths)],
      ["Strength", "Rate", "Last Measured"])
check("...while their ids say where in the row to read them",
      [o for o, _c in sch.number_table_columns(strengths)], [1, 0, 2])
check("Current Tx Powers has four of them",
      [(o, c.name) for o, c in sch.number_table_columns(tx_powers)],
      [(0, "Rate"), (1, "Tx Power"), (2, "Real Tx Power"),
       (3, "Total Tx Power")])
ok("a gridmultinumber has no columns -- its vocabulary names positions",
   not sch.number_table_columns(tx_grid) and len(tx_grid.enum or {}) == 28)


# --- splitting the array ----------------------------------------------------

rows = sch.number_table_rows(strengths, [16, 36, 3, 8, 30, 12])
check("six numbers over three columns are two rows", len(rows or []), 2)
check("...read at the offsets the columns name, and rendered as their types",
      [[(c.name, c.render(v)) for c, v in row] for row in rows or []],
      [[("Strength", "-64"), ("Rate", "HT20-0"),
        ("Last Measured", "00:00:00.03")],
       [("Strength", "-70"), ("Rate", "6Mbps"),
        ("Last Measured", "00:00:00.12")]])
check("a four-column table splits by four",
      [[(c.name, c.render(v)) for c, v in row]
       for row in sch.number_table_rows(tx_powers, [16, 20, 19, 22]) or []],
      [[("Rate", "HT20-0"), ("Tx Power", "20dBm"),
        ("Real Tx Power", "19dBm"), ("Total Tx Power", "22dBm")]])

ok("a length that is not whole rows is refused",
   sch.number_table_rows(strengths, [1, 2, 3, 4]) is None)
ok("...as is a rate the vocabulary does not name",
   sch.number_table_rows(strengths, [200, 36, 3]) is None)
ok("...and rate 5, which is one of the gaps in it",
   sch.number_table_rows(strengths, [5, 36, 3]) is None)
ok("an empty array is not a table either",
   sch.number_table_rows(strengths, []) is None)
ok("nor is a field with no columns",
   sch.number_table_rows(tx_grid, [1, 2, 3]) is None)


# --- the widgets ------------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)

view = NumberTableView(strengths, [16, 36, 3, 8, 30, 12])
check("the table view draws a row per row", view.row_count(), 2)
check("...with the columns as headers",
      [view._table.horizontalHeaderItem(i).text() for i in range(3)],
      ["Strength", "Rate", "Last Measured"])
check("...and the rendered values in the cells",
      [view._table.item(0, c).text() for c in range(3)],
      ["-64", "HT20-0", "00:00:00.03"])
ok("...read-only, being a report", view._table.editTriggers().name == "NoEditTriggers"
   if hasattr(view._table.editTriggers(), "name") else True)

odd = NumberTableView(strengths, [1, 2, 3, 4])
check("an array that is not a table shows the numbers it is",
      odd._raw.text() if odd._raw is not None else None, "1, 2, 3, 4")
ok("...in a read-only box", odd._raw is not None and odd._raw.isReadOnly())
check("...and no table is drawn", odd.row_count(), 0)


# A grid: positional, one labelled cell per rate.
grid = NumberGridEditor(tx_grid, [17, 17, 16, 16, 0, 0, 0, 0, 20, 20])
check("the grid has a cell per named rate", len(grid._boxes), 28)
check("...labelled by the vocabulary",
      [grid._boxes[k] is not None for k in (0, 8, 16)], [True, True, True])
check("...holding the element at its own position",
      [grid._boxes[k].text() for k in (0, 2, 8)], ["17dBm", "16dBm", "20dBm"])
ok("...and nothing where the array stopped", grid._boxes[31].text() == "")
ok("a fresh grid is not dirty", not grid.is_dirty())

grid._boxes[8].setText("22")
ok("editing a cell makes it dirty", grid.is_dirty())
tag, array = grid.wire_value()
check("...and the write is the whole positional array", tag, m.T_U32_ARRAY)
check("...with the edited position changed", array[8], 22)
check("...the untouched ones as they arrived", array[:4], [17, 17, 16, 16])
ok("...the sparse gaps left alone rather than invented",
   array[4:8] == [0, 0, 0, 0], f"{array[4:8]}")
check("...and the array long enough for every named rate", len(array), 32)

grid._boxes[9].setText("-5dBm")     # cells keep the unit they were shown with
_tag, signed = grid.wire_value()
check("a negative cell goes out as two's complement", signed[9], 0xFFFFFFFB)
check("...and reads back as itself",
      tx_grid.element_view().render(signed[9]), "-5dBm")

grid.set_locked(True)
ok("a locked grid takes no typing",
   all(b.isReadOnly() for b in grid._boxes.values()))

blank = NumberGridEditor(tx_grid, None)
_tag, empty = blank.wire_value()
check("a grid with nothing in it writes zeros, not garbage",
      (len(empty), set(empty)), (32, {0}))
ok("...and reports nothing to send on an Add", not blank.is_present())


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall number-table tests passed")
