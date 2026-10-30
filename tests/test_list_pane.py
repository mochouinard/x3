"""Offline tests for list-pane column power: sort, chooser, saved widths.

No device and no pytest required:

    .venv/bin/python test_list_pane.py

The invariant that matters most here is the view→object mapping. Sorting only
permutes the view, so model.source_row(view_row) must still name the object the
row came from — get that wrong and double-clicking a sorted row opens the wrong
property window, or Remove queues a delete against the wrong object.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from x3.ui import settings, style
from x3.ui.models.table_model import SECRET_MASK
from x3.ui.sortkey import sort_key
from x3.ui.widgets.list_pane import FLAGS_LABEL, ListPane

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    if not cond:
        failures.append(label)
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
app.setStyle("Fusion")
style.apply_theme(app, "light", persist=False)

PATH = (20, 0)
COLS = ["", "Name", "Type", "MTU", "Rx"]
#                flags  name        type       mtu     rx
ROWS = [
    ["R",  "ether10",     "ether",  "1500",  "9 KB"],
    ["R",  "ether2",      "ether",  "1500",  "1.5 GB"],
    ["X",  "bridge1",     "bridge", "1500",  "512 KB"],
    ["R",  "ether1",      "ether",  "9000",  "883.4 MB"],
]
NAME_COL = 1


def make(*, path=PATH, type_value=None, cols=None, rows=None, menu=None) -> ListPane:
    pane = ListPane("Interface", path=path, type_value=type_value)
    pane._on_loaded((menu, list(cols or COLS), [list(r) for r in (rows or ROWS)],
                     [None] * len(rows or ROWS), None))
    return pane


def names(pane) -> list[str]:
    return [
        pane._model.row_values(r)[NAME_COL] for r in range(pane._model.rowCount())
    ]


def sources(pane) -> list[int]:
    return [pane._model.source_row(r) for r in range(pane._model.rowCount())]


# --- unsorted = device order --------------------------------------------

p = make()
check("device order out of the box", names(p),
      ["ether10", "ether2", "bridge1", "ether1"])
check("sources are identity while unsorted", sources(p), [0, 1, 2, 3])

# --- click to sort --------------------------------------------------------

p._on_header_clicked(NAME_COL)
check("first click sorts ascending, naturally (ether2 < ether10)",
      names(p), ["bridge1", "ether1", "ether2", "ether10"])
check("sources follow the permutation", sources(p), [2, 3, 1, 0])
ok("sort indicator points at the sorted column",
   p.table.horizontalHeader().sortIndicatorSection() == NAME_COL)

p._on_header_clicked(NAME_COL)
check("second click sorts descending",
      names(p), ["ether10", "ether2", "ether1", "bridge1"])
check("sources still follow", sources(p), [0, 1, 3, 2])

p._on_header_clicked(NAME_COL)
check("third click returns to device order",
      names(p), ["ether10", "ether2", "bridge1", "ether1"])
check("sort cleared", p._sort_col, None)
check("indicator cleared too",
      p.table.horizontalHeader().sortIndicatorSection(), -1)

# --- the mapping is what everything else depends on ----------------------

p._on_header_clicked(NAME_COL)
for view_row in range(p._model.rowCount()):
    src = p._model.source_row(view_row)
    shown = p._model.row_values(view_row)[NAME_COL]
    backing = p._all_rows[src][NAME_COL]
    if shown != backing:
        failures.append(f"row {view_row}: shows {shown!r} but maps to {backing!r}")
ok("every sorted view row maps back to the object it displays", True)

# --- type-aware ordering on the other columns ----------------------------

p._sort_col, p._sort_desc = "Rx", False
p._rebuild_view()
check("byte sizes sort by magnitude, not text",
      [r[4] for r in (p._model.row_values(i) for i in range(4))],
      ["9 KB", "512 KB", "883.4 MB", "1.5 GB"])

# --- sort survives a poll ------------------------------------------------

p._sort_col, p._sort_desc = "Name", False
p._rebuild_view()
p._on_loaded((None, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
check("a refresh keeps the sort applied",
      names(p), ["bridge1", "ether1", "ether2", "ether10"])

# --- sort composes with the filter ---------------------------------------

p._filter.setText("ether")
check("filter narrows and the sort still holds",
      names(p), ["ether1", "ether2", "ether10"])
check("sources point past the filtered-out row", sources(p), [3, 1, 0])
p._filter.setText("")

# --- a masked secret column must not be sortable -------------------------

sp = make(cols=["", "Name", "Password"],
          rows=[["", "a", "zzz"], ["", "b", "aaa"], ["", "c", "mmm"]])
ok("the Password column was detected as secret", sp._col_secret[2])
sp._sort_col, sp._sort_desc = "Password", False
sp._rebuild_view()
check("sorting a masked column does not reorder (no oracle for the secret)",
      [sp._model.row_values(i)[1] for i in range(3)], ["a", "b", "c"])
sp.set_show_passwords(True)
sp._rebuild_view()
check("once revealed, the same column sorts normally",
      [sp._model.row_values(i)[1] for i in range(3)], ["b", "c", "a"])

# --- ...including one whose secret is nested in an opt --------------------
# The name hint above is only a fallback: where the schema knows the column it
# is the authority, and Self Encryption Password on a disk is declared
# opt -> secret. Asked at the row, that reads as an ordinary optional field, so
# the schema's "no" overrode the hint and the passphrase went on screen.

from x3.proto.schema import Schema

disk = Schema.load("7.21.1-151146055").record((79, 1))
enc = make(path=(79, 1), menu=disk,
           cols=["", "Name", "Self Encryption Password"],
           rows=[["", "disk1", "hunter2"]])
ok("a nested secret column is detected as one", enc._col_secret[2])
check("...so the cell reads as a mask, not as the passphrase",
      enc._masked(enc._all_rows[0])[2], SECRET_MASK)

# --- column chooser -------------------------------------------------------

p = make()
check("flags column is offered under a real name",
      p._column_labels(), [FLAGS_LABEL, "Name", "Type", "MTU", "Rx"])
p._set_column_shown("MTU", False)
ok("hidden column is hidden in the view", p.table.isColumnHidden(3))
ok("its neighbours are not", not p.table.isColumnHidden(2))
check("hiding does not change the model", p._model.columnCount(), 5)
p._set_column_shown("MTU", True)
ok("and it comes back", not p.table.isColumnHidden(3))

for label in p._column_labels():
    p._set_column_shown(label, False)
check("hiding every column is refused — one always survives",
      len(p._hidden_cols), len(p._columns) - 1)
p._show_all_columns()
check("Show All restores them", p._hidden_cols, set())

# --- widths ---------------------------------------------------------------

hdr = p.table.horizontalHeader()
hdr.resizeSection(NAME_COL, 173)          # stands in for a user drag
check("a user drag is recorded", p._prefs["widths"]["Name"], 173)
p._on_loaded((None, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
check("a refresh no longer re-measures over the dragged width",
      hdr.sectionSize(NAME_COL), 173)

before = dict(p._prefs["widths"])
p._rebuild_view()
check("an internal rebuild does not record its own sizing as user intent",
      p._prefs["widths"], before)

# --- persistence ----------------------------------------------------------

p._hidden_cols = {"MTU"}
p._sort_col, p._sort_desc = "Name", True
p._save_prefs()

saved = settings.load_column_prefs(str(PATH))
check("hidden columns persisted", saved.get("hidden"), ["MTU"])
check("sort persisted", saved.get("sort"), ["Name", "desc"])
check("width persisted", saved.get("widths", {}).get("Name"), 173)

p2 = make()
check("a new pane restores the hidden set", p2._hidden_cols, {"MTU"})
check("...the sort", (p2._sort_col, p2._sort_desc), ("Name", True))
ok("...and applies them", p2.table.isColumnHidden(3))
check("restored sort is actually in effect",
      names(p2), ["ether10", "ether2", "ether1", "bridge1"])
check("...and the width", p2.table.horizontalHeader().sectionSize(NAME_COL), 173)

# A width is stored at 1x and drawn at the zoom: dragged to fit an address at
# 1.0, it still fits that address at 1.3.
style.apply_theme(app, "light", scale=1.3, persist=False)
p3 = make()
check("...grown by the zoom it is drawn at",
      p3.table.horizontalHeader().sectionSize(NAME_COL), style.scale_px(173))
p3.table.horizontalHeader().resizeSection(NAME_COL, 260)
check("a drag at 1.3 is stored at 1x", p3._prefs["widths"]["Name"], 200)
p3.shutdown()
style.apply_theme(app, "light", scale=1.0, persist=False)

# The stretched last column is sized by the window, not by anyone's choice.
last = p2._stretched_section()
p2._prefs["widths"].pop(p2._column_labels()[last], None)
p2.resize(p2.width() + 300, p2.height())
p2.table.horizontalHeader().resizeSection(last, 900)
ok("the stretched last column's size is never recorded",
   p2._column_labels()[last] not in p2._prefs["widths"])

p2._reset_columns()
check("Reset clears the saved layout", settings.load_column_prefs(str(PATH)), {})
check("...and the sort with it", p2._sort_col, None)

# --- tabs sharing one path keep separate layouts -------------------------

a = make(type_value=None)
b = make(type_value=7)
ok("type_value separates the pref keys", a._pref_key() != b._pref_key())
check("the overlay is in the key", b._pref_key(), f"{PATH}#7")
b._hidden_cols = {"Type"}
b._save_prefs()
check("saving one tab leaves the other alone",
      settings.load_column_prefs(a._pref_key()).get("hidden"), None)
check("and the tab that saved keeps its own",
      settings.load_column_prefs(b._pref_key()).get("hidden"), ["Type"])

# --- sort_key spot checks -------------------------------------------------

check("ether10 sorts after ether2",
      sorted(["ether10", "ether2"], key=sort_key), ["ether2", "ether10"])
check("addresses sort numerically",
      sorted(["10.0.0.10", "10.0.0.9"], key=sort_key), ["10.0.0.9", "10.0.0.10"])
check("durations sort as time",
      sorted(["3d 04:15:00", "00:59:59"], key=sort_key), ["00:59:59", "3d 04:15:00"])
check("a mixed column still has a total order",
      sorted(["10.0.0.1", "", "dhcp", "5"], key=sort_key),
      ["", "5", "10.0.0.1", "dhcp"])

# --- Ctrl+M starts from the comment that is there ----------------------------
# The form's Comment button prefilled; the list's opened an empty box, so a
# typo meant retyping the whole comment.

from PySide6.QtWidgets import QInputDialog  # noqa: E402


class Commented:
    def __init__(self, text):
        self._text = text

    def comment(self):
        return self._text


asked: list = []
_orig_get_text = QInputDialog.getText
QInputDialog.getText = staticmethod(
    lambda *a, **k: (asked.append(k.get("text", "")), ("", False))[1]
)
try:
    cp = make()
    cp._action_comment([(0, [], Commented("uplink to ISP"))])
    cp._action_comment([(0, [], Commented("lab")), (1, [], Commented("lab"))])
    cp._action_comment([(0, [], Commented("a")), (1, [], Commented("b"))])
finally:
    QInputDialog.getText = _orig_get_text
check("one commented row opens with its comment", asked[0], "uplink to ISP")
check("...several rows that agree open with theirs", asked[1], "lab")
check("...and rows that differ open empty, copying neither", asked[2], "")

# --- a row's height belongs to its row, not to the slot it sorts into --------
# With comments as banners, a commented row is taller. Qt keeps a size it was
# given on the *position*, so after a sort an uncommented 10.3.3.2 landing where
# a "defconf" row had been inherited its banner height: a blank band twice the
# row's size, seen on a home router's IP > Addresses sorted by Address.


class Noted:
    def __init__(self, oid, text):
        self._oid, self._text = oid, text

    def object_id(self):
        return self._oid

    def comment(self):
        return self._text

    def info_lines(self):
        return ()


bp = ListPane("Address", path=(20, 1), inline_comments=False)
bp.resize(600, 300)
bp.show()
data = [("192.168.88.1/24", "defconf"), ("10.3.3.2/24", ""), ("172.16.99.1/24", "lab")]
bp._on_loaded((None, ["", "Address"], [["", a] for a, _c in data],
               [Noted(i, c) for i, (_a, c) in enumerate(data)], None))
app.processEvents()
bp._on_header_clicked(1)
app.processEvents()
heights = {bp._model.row_values(r)[1]: bp.table.rowHeight(r)
           for r in range(bp._model.rowCount())}
check("after a sort, an uncommented row is back at the theme's height",
      heights["10.3.3.2/24"], style.ROW_HEIGHT)
ok("...while a commented one keeps room for its banner",
   heights["192.168.88.1/24"] > style.ROW_HEIGHT)

print()

if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all list-pane tests passed")
