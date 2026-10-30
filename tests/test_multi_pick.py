"""Filling an array field a dozen entries at a time.

No device and no pytest required:

    .venv/bin/python test_multi_pick.py

A bridge VLAN's Tagged list is one dropdown per port -- that is how Winbox
edits it and how MultiEnumArrayEditor follows it. Tagging eight interfaces is
then eight presses of ``▾`` and eight trips through a dropdown that gets one
entry shorter each time, which is the wrong amount of work for "these eight
ports are tagged".

So the ``▾`` has a second meaning and the right-click has a second item: a
picker holding everything the field has *not* got yet, Ctrl-click or
Shift-click as many as you want, Add. What is checked here:

**The picker offers only what is free.** An interface already tagged is not in
it -- the same rule the row dropdowns follow, for the same reason: a duplicate
the device will not keep. The order is the order the rows sort in, ether2
before ether10, because a picker is read down rather than searched.

**Typing narrows it, and narrowing unpicks.** A hidden row that stayed selected
would be added by a button whose count never mentioned it. Down to one match,
that one is picked for you -- a name and Enter is the quick single add.

**Adding several is one edit.** One ``changed`` for the batch, not one per row:
the form recomputes its status line and its pending-write queue on every one,
and eight new ports are one thing to read. The rows land in the order they were
picked, at the ``▾`` that was pressed, and each is marked as the new row it is.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QDialog

from x3.ui import style
from x3.ui.widgets import property_window as pw
from x3.ui.widgets.multi_pick import MultiPickDialog
from x3.ui.widgets.property_window import MultiEnumArrayEditor, _apply_dirty_style

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


app = QApplication.instance() or QApplication([])
style.apply_theme(app, "light", persist=False)

#: An interface table the way a bridge VLAN's Tagged resolves it -- wire keys
#: are row ids, so they say nothing about the order the names read in.
PORTS = [(2, "ether1"), (14, "ether2"), (15, "ether3"), (16, "ether4"),
         (17, "ether5"), (18, "ether10"), (13, "bridge1"), (3, "vlan33")]


def editor(values, choices=PORTS, **kw):
    ed = MultiEnumArrayEditor(choices, list(values), **kw)
    app.processEvents()
    return ed


def texts(ed):
    return [c.currentText() for c, _b, _r in ed._rows]


def labels(dlg):
    return [dlg._list.item(i).text() for i in range(dlg._list.count())
            if not dlg._list.item(i).isHidden()]


def pick(dlg, *names):
    for i in range(dlg._list.count()):
        item = dlg._list.item(i)
        if item.text() in names:
            item.setSelected(True)
    app.processEvents()


# --- the picker itself -------------------------------------------------------

dlg = MultiPickDialog(PORTS, title="Add to Tagged")
check("the picker reads down the way the rows sort",
      labels(dlg),
      ["bridge1", "ether1", "ether2", "ether3", "ether4", "ether5",
       "ether10", "vlan33"])
check("nothing is picked to start with", dlg.picked(), [])
check("...so Add has no count on it and nothing to do",
      (dlg._ok.text(), dlg._ok.isEnabled()), ("Add", False))

pick(dlg, "ether2", "ether4", "bridge1")
check("what was picked comes back as wire keys, in the shown order",
      dlg.picked(), [13, 14, 16])
check("...and Add says how many", dlg._ok.text(), "Add 3")

dlg._filter.setText("ether")
app.processEvents()
check("typing narrows the list", labels(dlg),
      ["ether1", "ether2", "ether3", "ether4", "ether5", "ether10"])
check("...and drops what it hid from the picking", dlg.picked(), [14, 16])
check("...with the count following", dlg._ok.text(), "Add 2")
dlg._filter.setText("")
app.processEvents()
check("clearing the filter shows everything again, still picked as it was",
      (len(labels(dlg)), dlg.picked()), (8, [14, 16]))

dlg = MultiPickDialog(PORTS)
dlg._filter.setText("vlan")
app.processEvents()
check("one match is picked for you -- a name and Enter is one add",
      dlg.picked(), [3])
dlg._filter.setText("zzz")
app.processEvents()
check("no match picks nothing", (labels(dlg), dlg.picked()), ([], []))
ok("...and Add cannot be pressed", not dlg._ok.isEnabled())

small = MultiPickDialog(PORTS[:3])
ok("a handful of choices gets no filter box", small._filter is None)
check("...and still offers all of them", len(labels(small)), 3)


# --- what the editor offers it ----------------------------------------------

ed = editor([14, 3])                                  # ether2, vlan33
check("the picker is offered what no row holds",
      [lab for _w, lab in ed._free_choices()],
      ["ether1", "ether3", "ether4", "ether5", "ether10", "bridge1"])
ok("...and there is something to add", ed._can_add())

ed = editor([w for w, _lab in PORTS])
check("a list holding everything offers nothing", ed._free_choices(), [])
ok("...so the ▾ and the menu item are both dead", not ed._can_add())

ed = editor([2], max_rows=1)
ok("a list already at its maximum has no room either", not ed._can_add())

ed = editor([2], readonly=True)
ok("a read-only list adds nothing", not ed._can_add())


# --- adding several ----------------------------------------------------------

def counted(ed):
    """A box that counts what the form would see -- one edit, or many."""
    box = {"n": 0}
    ed.changed.connect(lambda: box.__setitem__("n", box["n"] + 1))
    return box


ed = editor([2])
seen = counted(ed)
n = ed.add_values([14, 15, 13])
app.processEvents()
check("every picked entry lands, in the order it was picked",
      texts(ed), ["ether1", "ether2", "ether3", "bridge1"])
check("...and the caller is told how many", n, 3)
check("...as one edit, not one per row", seen["n"], 1)
_apply_dirty_style(ed, ed.is_dirty())
changed, gaps = ed._edit_marks()
ok("...with the new rows marked as the new rows they are",
   changed == {1, 2, 3} and gaps == {}, f"{changed} {gaps}")

ed = editor([2, 3])
ed.add_values([14, 15], at=1)
app.processEvents()
check("added under a row, they go under that row",
      texts(ed), ["ether1", "ether2", "ether3", "vlan33"])

ed = editor([2, 14])
seen = counted(ed)
check("an entry the list already holds is not added twice",
      ed.add_values([14, 15]), 1)
check("...only the one that was free", texts(ed),
      ["ether1", "ether2", "ether3"])
check("nothing picked is nothing changed", ed.add_values([]), 0)
check("...and no edit to report", seen["n"], 1)

ed = editor([2], max_rows=3)
check("the maximum still holds when a dozen are picked at once",
      ed.add_values([14, 15, 16, 17]), 2)
check("...up to the maximum and no further", len(ed._rows), 3)

ed = editor([2], readonly=True)
check("a read-only list adds nothing at all", ed.add_values([14]), 0)

ed = editor([])
ed.add_values([14, 15])
app.processEvents()
check("filling an empty list works from nothing", texts(ed),
      ["ether2", "ether3"])
ok("...and the Add… row it showed is gone", not ed._empty_host.isVisible())


# --- the two ways in ---------------------------------------------------------

class Picker:
    """Stand-in for the modal picker: says what it was offered, replies flat."""

    last: tuple[list, str] | None = None
    marks: dict | None = None
    reply: list = []
    accept = True

    def __init__(self, choices, *, title="", marks=None, parent=None):
        Picker.last = ([lab for _w, lab in choices], title)
        Picker.marks = marks

    def exec(self):
        return (QDialog.DialogCode.Accepted if Picker.accept
                else QDialog.DialogCode.Rejected)

    def picked(self):
        return list(Picker.reply)


real = pw.MultiPickDialog
pw.MultiPickDialog = Picker
try:
    ed = editor([14], name="Tagged")
    Picker.reply = [2, 13]
    check("the ▾ with Ctrl held opens the picker instead of adding one row",
          ed._on_add_after(ed._rows[0][2], multi=True), None)
    app.processEvents()
    check("...and what it returns lands under that row",
          texts(ed), ["ether2", "ether1", "bridge1"])
    check("...having been offered only what the list had not got",
          Picker.last[0],
          ["ether1", "ether3", "ether4", "ether5", "ether10", "bridge1",
           "vlan33"])
    check("...under the field's own name", Picker.last[1], "Add to Tagged")

    ed = editor([])
    Picker.reply = [15]
    ed._on_add_first(multi=True)
    app.processEvents()
    check("the ▾ of an empty list opens it too", texts(ed), ["ether3"])

    ed = editor([2])
    Picker.accept = False
    seen = counted(ed)
    check("a cancelled picker adds nothing", ed.open_multi_pick(), 0)
    check("...and is not an edit", seen["n"], 0)
    Picker.accept = True

    ed = editor([2], readonly=True)
    check("a read-only list does not even open it", ed.open_multi_pick(), 0)

    ed = editor([w for w, _lab in PORTS])
    check("nor does a full one", ed.open_multi_pick(), 0)

    ed = editor([2], max_rows=1)
    check("nor one already at its maximum, with ports still free",
          ed.open_multi_pick(), 0)
finally:
    pw.MultiPickDialog = real

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all good")
