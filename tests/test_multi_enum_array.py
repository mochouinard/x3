"""The array-of-dropdowns editor — what it offers, and what it says changed.

No device and no pytest required:

    .venv/bin/python test_multi_enum_array.py

Winbox edits a multi-valued enum (Ethernet ▸ Advertise, a bridge VLAN's Tagged
ports, Switch ▸ VLAN's Ports) as one dropdown per entry with ▲ to drop a row
and ▼ to insert one. Two things about that editor are checked here.

**A dropdown offers what is still free.** An interface already tagged is not
offered a second time -- listing it again invites a duplicate the device will
not keep. The row's own value is always in its own box, including the case that
matters most: a value the vocabulary does not contain (an id the enum source
filtered out, or a row from a device newer than the schema). Dropping it would
silently select a *different* interface and write that on Apply.

**A new row is a guess the vocabulary makes.** Two vocabularies come through
this editor and they order differently. A rate ladder (Ethernet ▸ Advertise)
has arithmetic in its labels, so a row inserted between 10M and 1G is offered
100M. An interface table has none, and its wire keys are row ids rather than
positions -- on the lab VM ether1 is id 2 and vlan33 is id 3,
so ordering by the key put *vlan33* under a Tagged row holding ether1. Names
are ordered by the box: the next free entry below this row's, wrapping to the
top, which is also where Winbox's own "a dropdown is never empty" lands.

**Ordering is on the right-click, per field.** The device returns these in the
order the entries were added, which on a twelve-port bridge is no order to read
down. Right-click offers *Sort By Name* -- by what the row says, whatever the
vocabulary is, with the key the list columns use -- and *Device Order* back
again. Only the field you asked about moves. A row can also be moved by hand:
drag it by the grip on its left, or Alt+↑ / Alt+↓ with its box focused. Sorting an untouched list re-bases
its before-image so it does not read as edited; sorting one that *has* been
edited must not, or the pending change would be swallowed.

**Dirtiness is per row.** The form's plain highlight washes an editor whole,
which on a twelve-port bridge says only "something in here moved". Pulling one
port out is a one-row change and reads as one: the rows that really differ are
marked, and a removal -- which leaves no row behind to mark -- leaves a mark in
the gap it left, between the two rows that closed over it. The edit script is a
SequenceMatcher over the two lists of wire values, so the rows that merely
shuffled up are not marked at all.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from x3.ui import style
from x3.ui.widgets.property_window import (
    MultiEnumArrayEditor,
    ReadOnlyValueList,
    _RowGrip,
    _apply_dirty_style,
    _owns_baseline,
)

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

#: An interface table, the way a bridge VLAN's Tagged resolves it.
PORTS = [(1, "ether1"), (2, "ether2"), (3, "ether3"), (4, "ether4"),
         (5, "ether5"), (6, "bridge1")]


def editor(values, choices=PORTS):
    ed = MultiEnumArrayEditor(choices, list(values))
    app.processEvents()
    return ed


def items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def marks(ed):
    """(rows marked, gap -> what was removed there) as the form would paint."""
    _apply_dirty_style(ed, ed.is_dirty())
    return ed._edit_marks()


# --- what the dropdowns offer ------------------------------------------------

ed = editor([1, 3])
check("a row offers itself and whatever no other row holds",
      items(ed._rows[0][0]), ["ether1", "ether2", "ether4", "ether5", "bridge1"])
check("...and the other row the same, minus this one's",
      items(ed._rows[1][0]), ["ether2", "ether3", "ether4", "ether5", "bridge1"])
check("every row still reports its own value", ed.values(), [1, 3])
check("the array reads as words, not as wire keys", ed.label(), "ether1, ether3")

combo = ed._rows[0][0]
combo.setCurrentIndex(items(combo).index("ether2"))
app.processEvents()
check("changing one row frees what it held and takes what it took",
      items(ed._rows[1][0]),
      ["ether1", "ether3", "ether4", "ether5", "bridge1"])
check("the sibling itself did not move", ed.values(), [2, 3])

# A value the vocabulary does not name: an id the enum filter dropped, or a row
# from a device newer than the .jg cache.
ed = editor([1, 99])
check("an unknown value keeps its row rather than selecting another",
      ed.values(), [1, 99])
check("...and is shown as plainly as it can be",
      ed._rows[1][0].currentText(), "99")
ok("...with nothing marked as edited: nothing was", not ed.is_dirty())

ed = editor([])
check("an empty array has no rows", ed.values(), [])
ok("...and offers the whole vocabulary once a row is added",
   bool(ed._choices))


# --- what a new row is given -------------------------------------------------

#: The lab VM's interface table as it reports it (the lab VM). The keys are row ids, not positions: ether1 is 2, vlan33 3.
LAB_IFACES = [(2, "ether1"), (14, "ether2"), (15, "ether3"), (16, "ether4"),
       (17, "ether5"), (18, "ether6"), (13, "bridge1"), (1, "lo"),
       (6, "vlan1"), (9, "vlan2"), (10, "vlan3"), (3, "vlan33")]

#: Ethernet ▸ Advertise, the other kind of vocabulary.
RATES = [(1, "10M baseT half"), (2, "10M baseT full"), (3, "100M baseT half"),
         (4, "100M baseT full"), (5, "1G baseT half"), (6, "1G baseT full")]


def texts(ed):
    return [c.currentText() for c, _b, _r in ed._rows]


def added(values, at=-1, choices=LAB_IFACES):
    """What the rows read after ▾ under row ``at`` (or ▾ on an empty list)."""
    ed = editor(values, choices)
    if ed._rows:
        ed._on_add_after(ed._rows[at][2])
    else:
        ed._insert_row(0, highlight=True)
    app.processEvents()
    return texts(ed)


check("a second interface row is the next interface, not the next id",
      added([2]), ["ether1", "ether2"])
check("...and the one after that the one after that",
      added([2, 14]), ["ether1", "ether2", "ether3"])
check("the first row of an empty list is the first entry in the box",
      added([]), ["ether1"])
check("a row inserted above one takes the entry below its own",
      added([2, 16], at=0), ["ether1", "ether2", "ether4"])
check("under the last entry in the box it wraps to the first free one",
      added([3]), ["vlan33", "ether1"])
check("...and under a value the vocabulary does not name, the same",
      added([99]), ["99", "ether1"])
check("a rate ladder still lands between its neighbours",
      added([2, 6], at=0, choices=RATES),
      ["10M baseT full", "100M baseT full", "1G baseT full"])
check("...and an empty one on a common rate rather than the slowest",
      added([], choices=RATES), ["1G baseT full"])


# --- right-click: order this list ---------------------------------------------

MIXED = [(1, "ether10"), (2, "ether2"), (3, "bridge1")]


ed = editor([1, 2, 3], MIXED)
check("the rows start in the order the device sent them",
      texts(ed), ["ether10", "ether2", "bridge1"])
ed.sort_by_name()
app.processEvents()
check("sorted, they read the way the list columns sort -- ether2 before ether10",
      texts(ed), ["bridge1", "ether2", "ether10"])
ok("...and sorting to read is not an edit", not ed.is_dirty())
check("...because the before-image moved with the rows", ed._original, [3, 2, 1])
ed.restore_reported_order()
app.processEvents()
check("and back to the device's order on ask",
      texts(ed), ["ether10", "ether2", "bridge1"])
ok("...still not an edit", not ed.is_dirty())

# Sorting must not swallow a change that has not been saved yet.
ed = editor([1, 2, 3], MIXED)
ed._on_remove(ed._rows[0][2])                      # drop ether10 -- a real edit
app.processEvents()
before = ed.is_dirty()
ed.sort_by_name()
app.processEvents()
ok("sorting an edited list keeps the edit", before and ed.is_dirty(),
   f"dirty was {before}, now {ed.is_dirty()}")
check("...and the edit is still what it was", sorted(ed.values()), [2, 3])
check("...and still marked, where the sorted view puts it",
      ed._edit_marks()[1], {2: "ether10"})

ed = editor([1], MIXED)
ok("one row has nothing to sort", len(ed._rows) == 1)

ro = ReadOnlyValueList([1, 2, 3], "ether10, ether2, bridge1", None)
check("the reported twin starts as reported",
      ro.toPlainText().split("\n"), ["ether10", "ether2", "bridge1"])
ro.set_items(sorted(ro._items, key=lambda t: t))
check("...and sorts on its own ask, alone",
      ro.toPlainText().split("\n"), ["bridge1", "ether10", "ether2"])
ro.set_items(ro._as_reported)
check("...and goes back", ro.toPlainText().split("\n"),
      ["ether10", "ether2", "bridge1"])


# --- moving the rows by hand -------------------------------------------------

def drag(ed, index, dy):
    """Press the grip of one row, move by dy, release -- a real drag."""
    row = ed._rows[index][2]
    grip = row.findChild(_RowGrip)
    start = grip.mapToGlobal(QPoint(4, grip.height() // 2))
    for kind, at in (
        (QEvent.Type.MouseButtonPress, start),
        (QEvent.Type.MouseMove, start + QPoint(0, dy // 2)),
        (QEvent.Type.MouseMove, start + QPoint(0, dy)),
        (QEvent.Type.MouseButtonRelease, start + QPoint(0, dy)),
    ):
        local = grip.mapFromGlobal(at)
        app.sendEvent(grip, QMouseEvent(
            kind, QPointF(local), QPointF(at), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        ))
        app.processEvents()


ed = editor([1, 2, 3, 4])
ok("every row has a grip to hold it",
   all(r.findChild(_RowGrip) is not None for _c, _b, r in ed._rows))
drag(ed, 1, ed._ROW_H * 3)
check("dragged down, the row lands where it was dropped",
      texts(ed), ["ether1", "ether3", "ether4", "ether2"])
ok("...and moving a row nobody had edited is not an edit", not ed.is_dirty())

ed = editor([1, 2, 3])
drag(ed, 0, 1)
check("a drag that ends where it began moves nothing",
      (texts(ed), ed.is_dirty()), (["ether1", "ether2", "ether3"], False))

ed = editor([1, 2, 3])
combo = ed._rows[0][0]
combo.setFocus()
app.sendEvent(combo, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down,
                               Qt.KeyboardModifier.AltModifier))
app.processEvents()
check("Alt+Down moves the focused row -- the keyboard's grip",
      texts(ed), ["ether2", "ether1", "ether3"])
app.sendEvent(combo, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                               Qt.KeyboardModifier.AltModifier))
app.processEvents()
check("...and Alt+Up back again", texts(ed), ["ether1", "ether2", "ether3"])
before = texts(ed)
app.sendEvent(combo, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                               Qt.KeyboardModifier.AltModifier))
app.processEvents()
check("...and the top row has nowhere further up to go", texts(ed), before)

ed = editor([1, 2, 3])
ed._on_remove(ed._rows[2][2])                      # a real edit
app.processEvents()
drag(ed, 0, ed._ROW_H * 2)
ok("moving a row in an edited list keeps the edit",
   ed.is_dirty() and sorted(ed.values()) == [1, 2],
   f"dirty={ed.is_dirty()} values={ed.values()}")

ed = editor([1, 2])
ed.set_locked(True)
ok("a locked list offers no grip to drag",
   not any(r.findChild(_RowGrip).isVisible() for _c, _b, r in ed._rows))


# --- what it says changed ----------------------------------------------------

ok("the editor keeps its own before-image", _owns_baseline(editor([1])))

ed = editor([1, 3, 4, 5])
ok("nothing is marked before anything is touched", not ed.is_dirty())
check("...and no rows, no gaps", marks(ed), (set(), {}))

ed = editor([1, 3, 4, 5])
ed._on_remove(ed._rows[1][2])                      # pull ether3 out
app.processEvents()
check("a removal is one gap, not four changed rows",
      marks(ed), (set(), {1: "ether3"}))
check("...and the value is what is left", ed.values(), [1, 4, 5])
check("...said in words too", ed._rows_host.toolTip(), "Removed: ether3")

ed = editor([1, 3, 4, 5])
ed._on_remove(ed._rows[3][2])                      # the last row
app.processEvents()
check("a removal off the end marks the gap past the last row",
      marks(ed), (set(), {3: "ether5"}))

ed = editor([1, 3, 4, 5])
combo = ed._rows[2][0]
combo.setCurrentIndex(items(combo).index("ether2"))
app.processEvents()
check("a changed row is that row, and no gap",
      marks(ed), ({2}, {}))

ed = editor([1, 3])
ed._on_add_after(ed._rows[1][2])
app.processEvents()
changed, gaps = marks(ed)
check("an added row is marked", sorted(changed), [2])
check("...and adding is not removing", gaps, {})

ed = editor([1, 3, 4])
ed._on_remove(ed._rows[0][2])
ed._on_add_after(ed._rows[-1][2])
app.processEvents()
changed, gaps = marks(ed)
ok("a removal and an addition are both said, separately",
   gaps == {0: "ether1"} and changed == {2}, f"{changed} {gaps}")

ed = editor([1, 3])
while ed._rows:
    ed._on_remove(ed._rows[0][2])
app.processEvents()
changed, gaps = marks(ed)
check("emptying the list is one gap holding everything that was there",
      gaps, {0: "ether1, ether3"})
ok("...and the row that offers to add one carries the mark",
   bool(ed._empty_host.property("dirtyField")))

ed = editor([1, 3])
ed._on_remove(ed._rows[0][2])
app.processEvents()
ok("it is dirty until told otherwise", ed.is_dirty())
ed.reset_baseline()
ok("...and the new baseline clears the marks",
   not ed.is_dirty() and ed._edit_marks() == (set(), {}))
check("...and the mark is off the screen", ed._rows_host.marks._gaps, {})

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all good")
