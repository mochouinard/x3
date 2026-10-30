"""Double-clicking a row on a list that re-reads itself underneath.

No device and no pytest required:

    .venv/bin/python test_double_click.py

Qt holds the pressed row in a *persistent* model index and drops the
double-click when it no longer equals the row under the cursor. A model reset
invalidates every persistent index, and a live list resets its model on every
re-read (ListPane._rebuild_view -> TableModel.set_table), so on Interface --
1000ms of `autorefresh` -- a re-read landing between the two clicks swallowed
the double-click. It looked like nothing at all happened, because
_restore_selection puts the selection and the scroll position back: the row
stayed highlighted and no property window opened, every few tries.

So the table works out an opened row itself
(``CommentTableView.mouseDoubleClickEvent``) instead of trusting Qt's
``doubleClicked``, and it acts on the *current* row -- the one the pane put back
on the same object -- which is also the right row when the rebuild moved it.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.ui import style
from x3.ui.models.table_model import TREE_EXPANDED
from x3.ui.widgets.comment_delegate import _arrow_slot
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
pane.resize(600, 300)
pane.show()


def feed(rates):
    """A read of the list, with whatever the counters say this second."""
    raw = [row(i + 1, n, rates[i]) for i, n in enumerate(NAMES)]
    rows = [["R", n, rates[i]] for i, n in enumerate(NAMES)]
    pane._on_loaded((MENU, COLS, rows, raw, None))


def name_at(view_row: int) -> str:
    return pane._model.row_values(view_row)[1]


def centre_of(view_row: int) -> QPoint:
    return pane.table.visualRect(pane._model.index(view_row, 1)).center()


def double_click(pos: QPoint, *, between=None) -> None:
    """Press, release, then the second click -- the event order Qt delivers.

    ``between`` runs where a poll's re-read lands: after the first click has
    been taken and before the double-click event arrives.
    """
    view = pane.table.viewport()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    if between is not None:
        between()
    QTest.mouseDClick(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


feed(["5.8 Mbps", "0 bps", "1.2 Mbps", "44 kbps"])
check("the list is on screen", pane._model.rowCount(), 4)
check("row 0 is the first interface", name_at(0), "ether1")

# The plain case first: no poll in the way.
double_click(centre_of(0))
check("a double-click opens the row", opened, ["Interface — ether1"])

# The bug. One second later the device's counters move and the pane re-reads,
# and the re-read lands between the two clicks.
opened.clear()
double_click(
    centre_of(0),
    between=lambda: feed(["6.1 Mbps", "0 bps", "980 kbps", "44 kbps"]),
)
check("a re-read between the clicks does not eat the double-click",
      opened, ["Interface — ether1"])
check("the counters did move underneath", pane._model.row_values(0)[2],
      "6.1 Mbps")

# A rebuild can move the row as well as refill it -- which is what sorting by a
# live Tx/Rx column does every second. The object the user pressed is the one to
# open, not whatever row number the cursor now sits on.
opened.clear()
pane._sort_col, pane._sort_desc = "Name", False
pane._rebuild_view()
check("sorted by name, ether2 is the second row", name_at(1), "ether2")


def flip_the_order():
    pane._sort_desc = True
    pane._rebuild_view()


double_click(centre_of(1), between=flip_the_order)
check("the rows really did move", name_at(1), "ether3")
check("...and the double-click still opens the row it started on",
      opened, ["Interface — ether2"])

# A double-click that means nothing still means nothing.
opened.clear()
pane._sort_col = None
pane._rebuild_view()
last = pane.table.visualRect(pane._model.index(3, 1))
double_click(QPoint(last.center().x(), last.bottom() + 40))
check("a double-click below the last row opens nothing", opened, [])

# A tree parent's branch arrow is a fold, not a row to open: the first click
# toggled the subtree without touching the current row, so the second must not
# open whatever that current row happens to be.
opened.clear()
pane.table.setCurrentIndex(pane._model.index(2, 1))
pane._model.set_tree_meta({0: (0, TREE_EXPANDED)}, 1)
arrow = _arrow_slot(pane.table.visualRect(pane._model.index(0, 1)), 0)
double_click(arrow.center())
check("a double-click on a branch arrow opens nothing", opened, [])

pane.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all double-click tests passed")
