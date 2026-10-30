"""Keyboard on a live list: Enter opens a row, and a poll does not move you.

No device and no pytest required:

    .venv/bin/python test_keyboard_nav.py

Two things a Winbox list does with the keyboard, both of which a list being fed
1 Hz counters used to break:

* **Enter opens the highlighted row**, the same as double-clicking it. Qt does
  not do this for us -- ``activated`` is a mouse story on most styles -- so the
  table says it itself (``CommentTableView.row_activated``).
* **Up/Down carry on from where you were** after a re-read. A poll rebuilds the
  model wholesale, and a model reset clears the view's *current* index, which is
  a separate thing from the selection. A table with no current index answers the
  next Down with its first row -- so every re-read (the Interface feed re-reads
  every PUSH_REREAD_EVERY pushes) threw the user back to the top of the list,
  and Enter had nothing to open.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.ui import style
from x3.ui.widgets.list_pane import ListPane

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if not cond:
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "dark", persist=False)

NAME, TX, OID = 0x10001, 0x100D4, 0xFE0001


class StubMenu:
    autorefresh_ms = 1000
    refresh_filter = 0x4000A
    is_collection = True
    has_age_field = False


def row(oid: int, name: str, tx: str) -> RowObject:
    msg = m.Message()
    msg.add_u32(OID, oid)
    obj = RowObject(path=(20, 0), raw=msg)
    obj.values_by_code[OID] = oid
    for code, title, value in ((NAME, "Name", name), (TX, "Tx", tx)):
        obj.fields.append((code, title, value, True))
        obj.values_by_code[code] = value
    return obj


MENU = StubMenu()
COLS = ["", "Name", "Tx"]
NAMES = ["ether1", "ether2", "ether3", "ether4"]
opened: list = []

pane = ListPane(
    "Interface", path=(20, 0), type_value=None,
    on_open_properties=lambda path, raw, title, key, **kw: opened.append(title),
)


def feed(rates):
    """A read of the list, with whatever the counters say this second."""
    raw = [row(i + 1, n, rates[i]) for i, n in enumerate(NAMES)]
    rows = [["R", n, rates[i]] for i, n in enumerate(NAMES)]
    pane._on_loaded((MENU, COLS, rows, raw, None))


feed(["5.8 Mbps", "0 bps", "1.2 Mbps", "44 kbps"])
check("the list is on screen", pane._model.rowCount(), 4)

# The user clicks ether3 and walks up one, the way anyone would.
pane.table.selectRow(2)
pane.table.setCurrentIndex(pane._model.index(2, 1))
QTest.keyClick(pane.table, Qt.Key.Key_Up)
check("Up moves to the row above", pane.table.currentIndex().row(), 1)
check("...and takes the selection with it", pane._selected_view_rows(), [1])

# One second later the device pushes new counters, and the pane re-reads.
feed(["6.1 Mbps", "0 bps", "980 kbps", "44 kbps"])
check("the counters moved", pane._model.row_values(0)[2], "6.1 Mbps")
check("the re-read leaves the cursor on the same row",
      pane.table.currentIndex().row(), 1)
check("...on the same object", pane._model.row_values(1)[1], "ether2")
check("...and the selection with it", pane._selected_view_rows(), [1])
check("...on a column the user can see",
      pane.table.isColumnHidden(pane.table.currentIndex().column()), False)

QTest.keyClick(pane.table, Qt.Key.Key_Down)
check("Down after a re-read carries on down the list, not back to the top",
      pane.table.currentIndex().row(), 2)
check("...which is the row after the one we were on",
      pane._model.row_values(pane.table.currentIndex().row())[1], "ether3")

# Enter opens the row the arrows left us on.
QTest.keyClick(pane.table, Qt.Key.Key_Return)
check("Enter opens the current row", opened, ["Interface — ether3"])
QTest.keyClick(pane.table, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
check("...and so does the keypad's Enter",
      opened, ["Interface — ether3", "Interface — ether3"])

# A row sorted somewhere else is still the row the cursor is on.
opened.clear()
pane._sort_col, pane._sort_desc = "Name", True
pane._rebuild_view()
check("sorting keeps the cursor on the object, not the row number",
      pane._model.row_values(pane.table.currentIndex().row())[1], "ether3")
feed(["1 bps", "2 bps", "3 bps", "4 bps"])
QTest.keyClick(pane.table, Qt.Key.Key_Return)
check("...and Enter still opens it after another re-read",
      opened, ["Interface — ether3"])

# Nothing to keep is not a crash: a fresh list has no cursor to put back.
pane2 = ListPane("Interface", path=(20, 0), type_value=None)
pane2._on_loaded((MENU, COLS, [["R", "ether1", "0 bps"]],
                  [row(1, "ether1", "0 bps")], None))
ok("a list nobody has touched has no current row",
   not pane2.table.currentIndex().isValid())
pane2.close()
pane.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all keyboard-nav tests passed")
