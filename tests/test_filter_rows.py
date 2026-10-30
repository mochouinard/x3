"""The funnel: field / operator / value rows narrowing any list.

No device and no pytest required:

    .venv/bin/python test_filter_rows.py

Winbox v3 puts it on every list. A Firewall rule has seventy-odd columns, so
the field box narrows as it is typed into: "dst" leaves the Dst. ones.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton

from x3.ui.widgets.filter_rows import row_matches
from x3.ui.widgets.list_pane import ListPane

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)

row = ["", "drop", "input", "10.0.0.0/8"]
check("is: the whole cell, case-blind", row_matches(row, [(1, "is", "DROP")]), True)
check("is not", row_matches(row, [(1, "is not", "drop")]), False)
check("contains: part of it", row_matches(row, [(3, "contains", "10.0")]), True)
check("contains not", row_matches(row, [(3, "contains not", "10.0")]), False)
check("every condition must hold",
      row_matches(row, [(1, "is", "drop"), (2, "is", "forward")]), False)

COLS = ["", "Action", "Chain", "Dst. Address", "Dst. Port", "Src. Address", "Comment"]
ROWS = [
    ["", "accept", "input", "", "22", "", "ssh"],
    ["", "drop", "input", "10.0.0.0/8", "", "", ""],
    ["X", "drop", "forward", "", "", "192.168.88.0/24", "old"],
]
pane = ListPane("Filter Rules", path=(21, 1))
pane._on_loaded((None, COLS, [list(r) for r in ROWS], [None] * 3, None))
m = pane._model


def actions():
    return [m.row_values(i)[1] for i in range(m.rowCount())]


check("the funnel starts closed and filters nothing", actions(), ["accept", "drop", "drop"])
check("every column is a field, hidden or shown",
      pane._filter_fields(), COLS[1:])
pane._filter_act.trigger()
panel = pane._filter_panel
check("opening it offers a first row to fill", len(panel.rows), 1)
r = panel.rows[0]
le = r.picker.lineEdit()
le.selectAll()
QTest.keyClicks(le, "dst")
comp = r.picker.completer()
check("typing in the field box narrows it",
      [comp.completionModel().index(i, 0).data() for i in range(comp.completionCount())],
      ["Dst. Address", "Dst. Port"])
r.picker.setCurrentText("Action")
le.editingFinished.emit()
check("the value box offers what the column holds",
      [r.value.itemText(i) for i in range(r.value.count())], ["accept", "drop"])
r.value.setCurrentText("drop")
check("Action is drop", actions(), ["drop", "drop"])
check("each row carries its own − and +, side by side",
      [b.text() for b in r.findChildren(QToolButton)],
      ["−", "+"])
second = panel.add_row()
second.picker.setCurrentText("Chain")
second.picker.lineEdit().editingFinished.emit()
second.value.setCurrentText("input")
check("a second row narrows further", actions(), ["drop"])
second.removed.emit(second)
check("its − takes it away again", actions(), ["drop", "drop"])
r.add_below.emit(r)
check("a row's + adds the next one right under it", len(panel.rows), 2)
panel.rows[1].removed.emit(panel.rows[1])
r.removed.emit(r)
check("taking the last one away leaves an empty row to start from",
      (len(panel.rows), panel.rules()), (1, []))
r = panel.rows[0]
le = r.picker.lineEdit()
r.picker.setCurrentText("Action")
le.editingFinished.emit()
r.value.setCurrentText("drop")
r.picker.setCurrentText("Nonsense")
le.editingFinished.emit()
check("a name that is no column snaps back", r.picker.currentText(), "Action")
pane._filter_act.trigger()
check("closing the funnel shows every row again", actions(), ["accept", "drop", "drop"])
pane._filter_act.trigger()
check("...and opening it brings the conditions back", actions(), ["drop", "drop"])

# --- Find: the free-text box, on its own toggle --------------------------------

from x3.ui.settings import save_find_open  # noqa: E402

fp = ListPane("Filter Rules", path=(21, 1))
fp._on_loaded((None, COLS, [list(r) for r in ROWS], [None] * 3, None))
check("Find starts open by default",
      (fp._find_act.isChecked(), fp._find_row.isHidden()), (True, False))
fp._filter.setText("ssh")
check("...and narrows the list", fp._model.rowCount(), 1)
fp._find_act.trigger()
check("closing Find hides its whole row", fp._find_row.isHidden(), True)
check("...and stops it narrowing the list out of sight",
      (fp._filter.text(), fp._model.rowCount()), ("", 3))
save_find_open(False)
closed = ListPane("Filter Rules", path=(21, 1))
check("with View ▸ Find Box Open off, a new list starts without it",
      (closed._find_act.isChecked(), closed._find_row.isHidden()), (False, True))
save_find_open(True)

# --- Find skips what keeps counting -----------------------------------------
# On a home router, "38" in a Firewall list's Find made rules appear and vanish
# each second: their Packets and Bytes counters passed through numbers holding
# 38 and out again. Find now leaves the router's counters and rates alone.

from x3.proto.schema import Schema, is_ordered, list_columns  # noqa: E402

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "7.21.1-151146055")
if os.path.isdir(SCHEMA_DIR):
    fw = next(x for x in Schema.load(SCHEMA_DIR).menus
              if x.path == (20, 3) and x.kind == "map")
    names = [c.name for c in list_columns(fw)]
    head = ["", "#"] if is_ordered(fw, fw.path) else [""]

    def rule(i, **cells):
        return head[:1] + ([str(i)] if len(head) == 2 else []) + [
            cells.get(n, "") for n in names]

    rows = [rule(0, Action="accept", Packets="1383", Bytes="91 KiB"),
            rule(1, Action="drop", **{"Dst. Port": "38"})]
    fwp = ListPane("Filter Rules", path=fw.path)
    fwp._on_loaded((fw, head + names, rows, [None, None], None))
    fwp._filter.setText("38")
    check("Find does not match a rule on its packet counter",
          [fwp._model.row_values(r)[2] for r in range(fwp._model.rowCount())],
          ["drop"])

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all filter-row tests passed")
