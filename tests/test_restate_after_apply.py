"""Apply shows what the device answered, not what was typed.

No device and no pytest required:

    .venv/bin/python test_restate_after_apply.py

The router rewrites what it is sent into its own normal form. A bridge VLAN
holding ``100-103,105`` handed a new entry ``99`` does not come back as three
ranges: it comes back as ``99-103,105``, because 99 abuts 100 and the device
merges them. Winbox v3 restates the field from that reply -- the two boxes on
screen become ``99-103`` and ``105`` the moment Apply lands. Ours kept showing
the three boxes that had been typed until the window was closed and reopened,
so the form and the list it was opened from disagreed about the same object.

Apply already re-read the object (it had to, to stop the fields reading as
modified); what it did not do was put the answer back on screen. It does now,
field by field, into the widgets already there -- rebuilding the form would
throw away the tab, the scroll position and the keyboard.

Three things this has to get right:

* **The restate comes before the baselines are re-taken.** Reversed, every
  field the device normalised would read as edited the instant it was shown.
* **An editor that cannot take a value back is left alone**, not blanked: a
  value we cannot render is not the same as no value.
* **An inverted flag (`inv:1`) stays inverted.** The box shows the opposite of
  the bit, and a plain QCheckBox has no field to ask on the way back in, so the
  form records it on the widget.

And what the edit reads as *before* it is sent: adding one VLAN to the list
marked every range in it, because the whole editor got the one amber wash a
scalar field gets. An array marks the row that changed -- the array of
dropdowns already did, and the growable list of boxes does it the same way now,
through the same shared edit script (widgets/edit_marks.py). A removal leaves
no row behind to mark, so it leaves a mark in the gap it left instead.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QWidget,
)

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import (
    PropertyWindow,
    _apply_dirty_style,
    _restate_editor,
)
from x3.ui.widgets.scalar_array import ScalarArrayEditor

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


app = QApplication.instance() or QApplication(sys.argv)

schema = Schema.load("7.21.1-151146055")
PATH = (16, 13)          # Bridge ▸ VLANs
VLAN_IDS = 0x1           # U1: the flat (low, high) pairs


def vlan_row(pairs: list[int]) -> RowObject:
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)                  # .id
    msg.add_u32_array(VLAN_IDS, list(pairs))
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    return RowObject(
        path=PATH, raw=msg,
        fields=schema.decode(PATH, msg),
        form_fields=schema.decode_form(PATH, msg),
        values_by_code=values, values_by_code_multi=multi,
    )


class StubClient:
    """A device that merges abutting VLAN ranges, the way the router does."""

    schema = schema

    def __init__(self, row):
        self._row = row
        self.writes: list[tuple] = []

    # --- what a form asks for -------------------------------------------
    def menu_for(self, path, type_value=None, **kw):
        return schema.record(path)

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        return [(i, f"ether{i}", "ether") for i in range(1, 4)]

    # --- the write ------------------------------------------------------
    def set_obj(self, path, obj_id, values, **kw):
        self.writes.append((path, obj_id, values))
        self._row = vlan_row(self._merge(values[VLAN_IDS][1]))
        return None

    @staticmethod
    def _merge(flat: list[int]) -> list[int]:
        """(low, high) pairs, sorted and merged where they touch or overlap."""
        pairs = sorted(
            (flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)
        )
        out: list[tuple[int, int]] = []
        for low, high in pairs:
            if out and low <= out[-1][1] + 1:
                out[-1] = (out[-1][0], max(out[-1][1], high))
            else:
                out.append((low, high))
        return [n for pair in out for n in pair]


def form(pairs: list[int]):
    row = vlan_row(pairs)
    client = StubClient(row)
    win = PropertyWindow(
        "brtest", PATH, row,
        client=client,
        edit_session=EditSession(EditMode.LIVE, client=client),
        menu=client.menu_for(PATH),
        row_key="1",
        full_row=True,
    )
    app.processEvents()
    return win, client


def field(win, needle):
    for spec, editor in win._rows:
        if needle in str(spec.get("name") or ""):
            return spec, editor
    return None, None


# --- the device merges, and the form says so ---------------------------------

win, client = form([100, 103, 105, 105])
spec, ids = field(win, "VLAN ID")
ok("VLAN IDs is the growable range editor", isinstance(ids, ScalarArrayEditor),
   f"got {type(ids).__name__}")
check("it opens on the device's ranges, not its elements",
      ids.texts(), ["100-103", "105"])

ids._add_row("99", notify=True)
app.processEvents()
check("a new entry goes in on its own line", ids.texts(),
      ["100-103", "105", "99"])
ok("...and reads as edited until it is sent", ids.is_dirty())

win._action_apply()
app.processEvents()

check("the write carries the pairs, low then high",
      client.writes[0][2][VLAN_IDS][1], [100, 103, 105, 105, 99, 99])
check("99 abutting 100 comes back as one range",
      client._row.values_by_code[VLAN_IDS], [99, 103, 105, 105])
check("and that is what the boxes show -- what Winbox v3 shows",
      ids.texts(), ["99-103", "105"])
ok("nothing reads as edited: the device and the form agree",
   not ids.is_dirty() and win._dirty_count == 0,
   f"editor dirty={ids.is_dirty()} form={win._dirty_count}")
check("the spec carries the new value too, for the rows that read it",
      spec.get("raw"), [99, 103, 105, 105])

# The restate has to precede the re-taken baseline; reversed, every field the
# device normalised would come back reading as edited.
win2, client2 = form([10, 20])
_spec2, ids2 = field(win2, "VLAN ID")
ids2._add_row("21", notify=True)
app.processEvents()
win2._action_apply()
app.processEvents()
check("a second range that touches the first is merged too",
      ids2.texts(), ["10-21"])
check("...and the title says nothing is pending",
      win2.windowTitle(), "brtest [live]")

# A write the device refuses nothing of, but that changes nothing either: the
# form must not invent an edit out of its own re-read.
win3, client3 = form([100, 100])
_spec3, ids3 = field(win3, "VLAN ID")
ids3._add_row("200", notify=True)
app.processEvents()
win3._action_apply()
app.processEvents()
check("two ranges that do not touch stay two", ids3.texts(), ["100", "200"])
check("...counted as no pending edits", win3._dirty_count, 0)

# --- which row reads as edited, before Apply --------------------------------

def row_marks(editor):
    """(the boxes wearing the wash, the marks the editor asked for)."""
    _apply_dirty_style(editor, editor.is_dirty())
    app.processEvents()
    return (
        [bool(box.property("dirtyField")) for _row, box in editor._rows],
        editor._edit_marks(),
    )


win4, _client4 = form([100, 103, 105, 105])
_spec4, ids4 = field(win4, "VLAN ID")
check("an untouched list marks nothing", row_marks(ids4),
      ([False, False], (set(), {})))

ids4._add_row("99", notify=True)
app.processEvents()
check("adding an entry marks that entry and no other",
      row_marks(ids4), ([False, False, True], ({2}, {})))
ok("...and does not wash the list it was added to",
   not ids4.property("dirtyField") and ids4.styleSheet() == "",
   f"container wears {ids4.styleSheet()!r}")

win5, _client5 = form([100, 103, 105, 105, 200, 200])
_spec5, ids5 = field(win5, "VLAN ID")
ids5._rows[1][1].setText("106")
app.processEvents()
check("retyping the middle range marks the middle box",
      row_marks(ids5), ([False, True, False], ({1}, {})))

win6, _client6 = form([100, 103, 105, 105, 200, 200])
_spec6, ids6 = field(win6, "VLAN ID")
ids6._remove_row(ids6._rows[1][0])
app.processEvents()
marked, (changed, gaps) = row_marks(ids6)
check("a removal marks no row -- there is none left to mark", marked,
      [False, False])
check("...it marks the seam it left, between the rows that closed over it",
      (changed, gaps), (set(), {1: "105"}))
check("...and says so in words, for a reader that cannot see the line",
      ids6._rows_host.toolTip(), "Removed: 105")

win7, _client7 = form([100, 100])
_spec7, ids7 = field(win7, "VLAN ID")
ids7._remove_row(ids7._rows[0][0])
app.processEvents()
_marked, (_changed, gaps7) = row_marks(ids7)
check("emptying the list still leaves the mark somewhere", gaps7, {0: "100"})
ok("...on the + button, the only thing left to carry it",
   bool(ids7._add.property("dirtyField")))

# An empty box is not an entry, so a row's index and its entry's index part
# company the moment + is pressed. The marks have to land on the right box.
win8, _client8 = form([100, 100, 200, 200])
_spec8, ids8 = field(win8, "VLAN ID")
ids8._add_row("", notify=True)          # + pressed, nothing typed yet
app.processEvents()
check("a blank row is no edit at all", row_marks(ids8),
      ([False, False, False], (set(), {})))
ids8._rows[0][1].setText("101")
app.processEvents()
check("...and the first box's edit is still marked on the first box",
      row_marks(ids8), ([True, False, False], ({0}, {})))

# Apply clears the marks with everything else: the rows are rebuilt on what
# the device answered, so there is nothing left that differs from it.
win9, _client9 = form([100, 103])
_spec9, ids9 = field(win9, "VLAN ID")
ids9._add_row("104", notify=True)
app.processEvents()
check("before Apply the new range is marked", row_marks(ids9)[0],
      [False, True])
win9._action_apply()
app.processEvents()
check("the device merges it", ids9.texts(), ["100-104"])
check("...and nothing is marked any more", row_marks(ids9),
      ([False], (set(), {})))


# --- the plain-Qt ladder ----------------------------------------------------

box = QLineEdit("typed")
ok("a line edit takes the rendered text",
   _restate_editor(box, b"ignored", "ether5", {}) and box.text() == "ether5",
   f"got {box.text()!r}")
_restate_editor(box, None, "—", {})
check("an em dash is the absence of a value, not a value", box.text(), "")

combo = QComboBox()
combo.addItem("ether1", 1)
combo.addItem("ether2", 2)
ok("a dropdown re-selects by wire data",
   _restate_editor(combo, 2, "ether2", {}) and combo.currentData() == 2,
   f"got {combo.currentData()!r}")
ok("...and by rendered text when the data does not match",
   _restate_editor(combo, None, "ether1", {}) and combo.currentData() == 1,
   f"got {combo.currentData()!r}")
ok("a value it cannot place leaves the selection alone",
   not _restate_editor(combo, 99, "ether9", {}) and combo.currentData() == 1,
   f"got {combo.currentData()!r}")

typed = QComboBox()
typed.setEditable(True)
typed.addItem("known", 1)
ok("an editable dropdown takes a name the device made up",
   _restate_editor(typed, None, "new-list", {})
   and typed.currentText() == "new-list",
   f"got {typed.currentText()!r}")

cb = QCheckBox()
cb.setChecked(True)
ok("a checkbox follows the bit", _restate_editor(cb, 0, "false", {})
   and not cb.isChecked())
inv = QCheckBox()
inv.setProperty("invertedFlag", True)     # `inv:1`, as the form records it
ok("an inverted flag shows the opposite of the bit, on the way back in too",
   _restate_editor(inv, 0, "false", {}) and inv.isChecked(),
   "a restate flipped Auto Negotiation off")

# An editor this does not know keeps what it holds: blanking a field because
# we cannot re-render it would lose the value the device is holding.
mystery = QWidget()
ok("an editor with no way back is left alone",
   not _restate_editor(mystery, 5, "five", {}))

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
