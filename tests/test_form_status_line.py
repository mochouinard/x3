"""The line across the top of a property form: what it says, and whether it is
there at all.

No device and no pytest required:

    .venv/bin/python test_form_status_line.py

The line reported a count of *fields*: "2 field(s) modified (highlighted)". On a
bridge VLAN that is the least useful thing it could say -- pulling two ports out
of Tagged is one field, and adding five to Untagged is one more, so the number
reads 2 either way and says nothing about the ten entries in play. It counted
the same for a whole port list emptied as for one comment retyped.

So a list field says what it gained and lost. The count stays (it is what the
title badge and the close prompt count), and after it the fields are named --
``Tagged −2, Untagged +3`` -- with the entries themselves, before and after, in
the line's tooltip. A list compared as a multiset, so a row that only shuffled
up when its neighbour went counts as neither; same entries in another order is
still an edit and says ``reordered``.

The line itself is optional: View ▸ Form Status Line takes it away, on forms
already open as well as the next one, and the title bar still carries the mode
and the edit count.

Also here because it is the same flow: adding to a list that has every entry in
it already. There was nothing left to offer, so the ▾ handed back a copy of the
last row -- a duplicate the device will not keep, sitting in the form looking
like a real choice. Now the ▾ is greyed and says why.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QToolButton

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.settings import load_form_status_line, save_form_status_line
from x3.ui.widgets.property_window import (
    MultiEnumArrayEditor,
    PropertyWindow,
    _change_summary,
    _joined,
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


schema = Schema.load("7.21.1-151146055")
#: Bridge ▸ VLANs, the form this was reported from: two lists of interfaces
#: (Tagged, Untagged) plus a range list (VLAN IDs) on one small form.
PATH = (16, 13)
PORTS = {i: f"ether{i}" for i in range(1, 9)}
PORTS[20] = "br-lan"


class StubClient:
    """Just enough client for a form: the menu, and an interface table."""

    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, **kw):
        return (schema.record_for(path, type_value) if type_value is not None
                else schema.record(path))

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return dict(PORTS)

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        return [(i, name, "ether") for i, name in sorted(PORTS.items())]


def make_form(tagged=(1, 2, 3, 4, 5), untagged=(6, 7), vlan_ids=(100, 100)):
    msg = m.Message()
    msg.add_u32(0xFE0001, 5)             # .id
    msg.add_u32(0x000002, 20)            # Bridge
    msg.add_u32_array(0x000001, list(vlan_ids))
    msg.add_u32_array(0x000003, list(tagged))
    msg.add_u32_array(0x000004, list(untagged))
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    row = RowObject(
        path=PATH, raw=msg,
        fields=schema.decode(PATH, msg),
        form_fields=schema.decode_form(PATH, msg),
        values_by_code=values, values_by_code_multi=multi,
    )
    client = StubClient(row)
    win = PropertyWindow(
        "VLAN <5>", PATH, row,
        client=client,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(PATH),
        row_key="5",
        full_row=True,
    )
    app.processEvents()
    return win


def field(win, name):
    for spec, editor in win._rows:
        if spec.get("name") == name:
            return editor
    return None


def line(win):
    return win._mode_lab.text()


def shut(win):
    """Close a form with edits on it without the are-you-sure prompt."""
    win._allow_close = True
    win.close()


app = QApplication.instance() or QApplication(sys.argv)

# --- what the line says ----------------------------------------------------

win = make_form()
tagged = field(win, "Tagged")
untagged = field(win, "Untagged")
ok("the two port lists are dropdown arrays",
   isinstance(tagged, MultiEnumArrayEditor)
   and isinstance(untagged, MultiEnumArrayEditor),
   f"{type(tagged).__name__} / {type(untagged).__name__}")
ok("an untouched form claims no edits", "modified" not in line(win),
   line(win))

# Pull two ports out of Tagged: one field, two entries.
tagged._on_remove(tagged._rows[0][2])
tagged._on_remove(tagged._rows[0][2])
app.processEvents()
ok("a two-entry removal says so, and names the list",
   "1 field(s) modified: Tagged −2" in line(win), line(win))

# ...then start adding to Untagged, which is the half-finished edit the count
# alone could not describe.
for _ in range(3):
    untagged._on_add_after(untagged._rows[-1][2])
app.processEvents()
check("Untagged is three entries longer", len(untagged.values()), 5)
ok("both lists are named, each with what it gained and lost",
   "2 field(s) modified: Tagged −2, Untagged +3" in line(win), line(win))
ok("...and Queue mode still says what Escape does",
   "Escape to close" in line(win), line(win))

tip = win._mode_lab.toolTip().split("\n")
check("the tooltip names the entries themselves, one field per line",
      tip[0], "Tagged: ether1, ether2, ether3, ether4, ether5"
              " → ether3, ether4, ether5")
ok("...including the ones about to be added",
   tip[1].startswith("Untagged: ether6, ether7 → ether6, ether7, ether8"),
   tip[1])

# A scalar list (VLAN IDs is one box per range) counts the same way.
win2 = make_form(vlan_ids=(100, 100, 200, 200))
ids = field(win2, "VLAN IDs")
ids._add_row("300", notify=True)
app.processEvents()
ok("a range list counts its boxes too",
   "VLAN IDs +1" in line(win2), line(win2))
shut(win2)

# --- a field that is not a list -------------------------------------------

win3 = make_form()
bridge = field(win3, "Bridge")
bridge.setCurrentIndex(0 if bridge.currentIndex() else 1)
app.processEvents()
ok("a plain field is named, with no count after it",
   line(win3).split("modified: ")[1].startswith("Bridge;"), line(win3))
shut(win3)

# --- the arithmetic, on its own -------------------------------------------

ed = MultiEnumArrayEditor(sorted(PORTS.items()), [1, 2, 3])
check("nothing touched, nothing said", _change_summary(ed), "")
ed._on_remove(ed._rows[1][2])
check("one entry gone", _change_summary(ed), "−1")
ed._on_add_after(ed._rows[-1][2])
check("...and one added back somewhere else", _change_summary(ed), "+1 −1")

ed = MultiEnumArrayEditor(sorted(PORTS.items()), [3, 1, 2])
ed._original = [1, 2, 3]      # as if the device had sent them in order
ok("the same entries in another order is still an edit", ed.is_dirty())
check("...and says which kind", _change_summary(ed), "reordered")

check("a whole form's worth of fields is kept to a line",
      _joined(["a", "b", "c", "d", "e", "f"]),
      "a, b, c, d, … 2 more")

# --- View ▸ Form Status Line ----------------------------------------------

check("the line is on by default", load_form_status_line(), True)
# isHidden, not isVisible: these windows are built but never shown, and every
# child of an unshown window reports itself invisible.
ok("...and it is up on this form", not win._mode_lab.isHidden())
win.set_status_line(False)
app.processEvents()
ok("switching it off takes it off a form already open",
   win._mode_lab.isHidden())
ok("...and the title bar still carries the mode and the count",
   "[queue]" in win.windowTitle() and "2 edited" in win.windowTitle(),
   win.windowTitle())
win.set_status_line(True)
app.processEvents()
ok("...and back on it comes, saying what it said before",
   not win._mode_lab.isHidden()
   and "Tagged −2, Untagged +3" in line(win), line(win))

save_form_status_line(False)
try:
    win4 = make_form()
    ok("a form opened with it off has no line",
       win4._mode_lab.isHidden(), line(win4))
    field(win4, "Tagged")._on_remove(field(win4, "Tagged")._rows[0][2])
    app.processEvents()
    ok("...and an edit does not put it up",
       win4._mode_lab.isHidden(), line(win4))
    ok("...though the form still knows it is dirty", win4._dirty_count == 1)
    shut(win4)
finally:
    save_form_status_line(True)
shut(win)

# --- adding when there is nothing left to add -----------------------------

full = MultiEnumArrayEditor([(1, "ether1"), (2, "ether2")], [1])
add = full._rows[0][1].findChild(QToolButton, "addRow")
ok("with a free entry left, the ▾ offers it", add.isEnabled())
full._on_add_after(full._rows[0][2])
app.processEvents()
check("...and it is the free one, not a copy", full.values(), [1, 2])
add = full._rows[-1][1].findChild(QToolButton, "addRow")
ok("with the vocabulary used up the ▾ is greyed", not add.isEnabled())
check("...and says why", add.toolTip(), "Every entry is already in the list")
full._on_add_after(full._rows[-1][2])
app.processEvents()
check("...and asking anyway adds no duplicate", full.values(), [1, 2])
ok("...so nothing is pending that the device would refuse",
   _change_summary(full) == "+1", _change_summary(full))

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all form status line tests passed")
