"""Queue mode you can see: the queue drawn over every list, and reviewable.

No device and no pytest required:

    .venv/bin/python test_queue_view.py

Driven on a virtual router, Queue mode held changes nobody could see: a queued Add never
appeared in its list, a queued edit looked exactly like the router's state
until a poll wiped it out, and a queued Remove deleted the row outright. The
review was a message box whose only choices were the whole queue.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from x3.ui.edit_mode import EditMode, EditSession, QueuedChange
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.pending_window import PendingChangesDialog, change_lines

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)

PATH = (20, 1)
COLS = ["", "Address", "Interface", "Comment"]


class Raw:
    def __init__(self, oid):
        self._oid = oid

    def object_id(self):
        return self._oid


session = EditSession(EditMode.QUEUE)
pane = ListPane("Address", path=PATH, readonly=False, edit_session=session)
rows = [["", "10.0.0.1/24", "ether1", ""], ["", "10.0.0.2/24", "ether2", "lab"]]
pane._on_loaded((None, COLS, rows, [Raw(1), Raw(2)], None))
session.listeners.append(pane._rebuild_view)
m = pane._model


def table():
    return [(m.row_values(i)[:4], m.pending_kind(i)) for i in range(m.rowCount())]


session.enqueue(QueuedChange(
    path=PATH, title="Address — (new)", row_key="(new)", action="add",
    edits=[{"code": 1, "name": "Address", "new": "10.9.0.1/24", "label": "10.9.0.1/24"},
           {"code": 4, "name": "Interface", "old": 2, "new": 5, "label": "bridge"}],
    origin="form-a",
))
session.enqueue(QueuedChange(
    path=PATH, title="Address — comment 10.0.0.1/24", row_key="a", obj_id=1,
    action="comment", values={"Comment": "uplink"},
    edits=[{"code": 0xFE0009, "name": "Comment", "old": "", "new": "uplink", "label": "uplink"}],
))
session.enqueue(QueuedChange(path=PATH, title="Address — disable 10.0.0.1/24",
                             row_key="a", obj_id=1, action="disable"))
session.enqueue(QueuedChange(path=PATH, title="Address — remove 10.0.0.2/24",
                             row_key="b", obj_id=2, action="remove"))
check("the list draws the queue over the router's rows", table(), [
    (["X", "10.0.0.1/24", "ether1", "uplink"], "edit"),
    (["", "10.0.0.2/24", "ether2", "lab"], "remove"),
    (["+", "10.9.0.1/24", "bridge", ""], "add"),
])
check("...and says so on hover",
      m.data(m.index(0, 1), Qt.ItemDataRole.ToolTipRole).splitlines()[0],
      "Queued, not sent yet:")
check("...in italics, struck through for a removal",
      (m.data(m.index(0, 1), Qt.ItemDataRole.FontRole).italic(),
       m.data(m.index(1, 1), Qt.ItemDataRole.FontRole).strikeOut()),
      (True, True))

pane._on_loaded((None, COLS, [list(r) for r in rows], [Raw(1), Raw(2)], None))
check("a fresh read from the router does not wipe it out",
      [k for _r, k in table()], ["edit", "remove", "add"])

# --- the review window -------------------------------------------------------

check("an Add lists its values, not a prefilled id as 'old'",
      change_lines(session.queue[0]), ["Address: 10.9.0.1/24", "Interface: bridge"])
dlg = PendingChangesDialog(session)
labels = [dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())]
check("each change is its own entry, named by its object", labels,
      ["1. Add new object", "2. Comment 10.0.0.1/24",
       "3. Disable 10.0.0.1/24", "4. Remove 10.0.0.2/24"])
dlg.tree.topLevelItem(3).setSelected(True)
dlg._remove_selected()
check("Remove takes out only the selected change",
      [c.action for c in session.queue], ["add", "comment", "disable"])
check("...and the list stops drawing it at once",
      [k for _r, k in table()], ["edit", None, "add"])
dlg.close()

session.clear()
check("an emptied queue leaves the router's rows as they are", table(), [
    (["", "10.0.0.1/24", "ether1", ""], None),
    (["", "10.0.0.2/24", "ether2", "lab"], None),
])

# --- a removal is kept on screen until it is sent ------------------------------

pane.table.selectRow(1)
pane._action_remove(pane._selected_items())
check("Remove in Queue mode keeps the row, struck through",
      [k for _r, k in table()], [None, "remove"])

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all queue-view tests passed")
