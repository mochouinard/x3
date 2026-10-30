"""Tagged and Untagged offer the same ports, and never the same port twice.

No device and no pytest required:

    .venv/bin/python test_exclusive_arrays.py

A bridge VLAN's **Tagged** and **Untagged** are two lists over one interface
table, and the router will not have a port in both of them: adding one answers

    failure: interface cannot be in tagged and untagged at the same time

on ``add`` and on ``set`` alike (verified on a 7.24 virtual router). Egress cannot be both
tagged and untagged for one VLAN. Across *rows* it is ordinary -- a port tagged
on one VLAN and untagged on another is a trunk with a native VLAN -- so the
rule is per object, never per port.

Nothing in the ``.jg`` declares it, so the schema declares it for us
(``EXCLUSIVE_ARRAY_NAMES`` / ``exclusive_array_peers``) and the form links the
two editors it finds. What that buys, and what it must not break:

**Neither list offers what the other holds.** The same rule each list already
applies to its own rows, read across the pair -- in the row dropdowns, in what
a new ``▾`` row is given, and in the *Add Several* picker. An offer the router
will refuse is worse than no offer.

**Freeing a port next door frees it here.** The lists are linked live, not
sampled at build: dropping ether3 from Untagged makes it a candidate for
Tagged immediately, and taking it in Tagged withdraws it from Untagged.

**A peer's edit is not this list's edit.** Refreshing what a dropdown offers
must not mark the other field as changed, or editing Untagged would queue a
write to Tagged that nobody asked for.

**A pair the device reports anyway is kept.** The router will not produce one,
but a form must never silently rewrite what it was handed: both rows keep the
port, and it is simply not offered anywhere else.

**Only that pair.** Current Tagged and Current Untagged are readings, not
settings -- and they legitimately overlap the settings while a change is
pending -- so they restrict nothing. A lone list (Ethernet ▸ Advertise) is
unaffected.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import (
    MultiEnumArrayEditor,
    PropertyWindow,
    ReadOnlyValueList,
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


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

app = QApplication.instance() or QApplication(sys.argv)
schema = Schema.load(SCHEMA_DIR)

BRIDGE_VLAN = (16, 13)
IFACE = (20, 0)
ETHERNET = (13, 1)

#: The interface table a bridge VLAN's ports are named from.
PORTS = {1: "ether1", 2: "ether2", 3: "ether3", 4: "ether4", 6: "bridge1"}

TAGGED, UNTAGGED = 0x03, 0x04


class StubClient:
    """Just enough client for a form: menus, and an interface table."""

    schema = schema

    def __init__(self, row=None):
        self._row = row

    def menu_for(self, path, type_value=None, **kw):
        return schema.record(path)

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path, **kw):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return dict(PORTS) if tuple(path) == IFACE else {}

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        return [(k, v, "ether") for k, v in sorted(PORTS.items())]


def make_row(path, fields: dict, *, resolver=None):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    for code, value in fields.items():
        if isinstance(value, list):
            msg.add_u32_array(code, value)
        else:
            msg.add_u32(code, value)
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    return RowObject(
        path=path, raw=msg,
        fields=schema.decode(path, msg, resolver=resolver),
        form_fields=schema.decode_form(path, msg, resolver=resolver),
        values_by_code=values, values_by_code_multi=multi,
    )


def make_form(path, fields: dict):
    client = StubClient()
    resolver = lambda p: dict(PORTS) if tuple(p) == IFACE else {}
    row = make_row(path, fields, resolver=resolver)
    client._row = row
    win = PropertyWindow(
        "row", path, row,
        client=client,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(path),
        row_key="1",
        full_row=True,
    )
    win._ensure_all_pages()
    app.processEvents()
    return win


def find(win, label):
    for spec, ed in win._rows:
        if spec.get("name") == label:
            return ed
    return None


def offers(editor, row=0):
    """What one row's dropdown lists, as names."""
    combo = editor._rows[row][0]
    return [combo.itemText(i) for i in range(combo.count())]


def free(editor):
    return [lab for _w, lab in editor._free_choices()]


# --- the schema is what says which fields these are --------------------------

menu = schema.record(BRIDGE_VLAN)
peers = sch.exclusive_array_peers(menu, menu.fields[TAGGED])
check("the schema pairs Tagged with Untagged", [f.name for f in peers],
      ["Untagged"])
check("...and the other way round",
      [f.name for f in sch.exclusive_array_peers(menu, menu.fields[UNTAGGED])],
      ["Tagged"])
check("a reading is not a setting: Current Tagged pairs with nothing",
      sch.exclusive_array_peers(menu, menu.fields[0x05]), [])


# --- what the two lists offer ------------------------------------------------

win = make_form(BRIDGE_VLAN, {
    0x02: 6,                    # Bridge
    0x01: [100, 100],           # VLAN IDs
    TAGGED: [1, 2],             # ether1, ether2
    UNTAGGED: [3],              # ether3
})
tagged, untagged = find(win, "Tagged"), find(win, "Untagged")
ok("both lists are dropdown arrays",
   isinstance(tagged, MultiEnumArrayEditor)
   and isinstance(untagged, MultiEnumArrayEditor),
   f"{type(tagged).__name__} / {type(untagged).__name__}")
ok("...and the form linked them", untagged in tagged._peers
   and tagged in untagged._peers, f"{tagged._peers} {untagged._peers}")

check("Tagged's row keeps its own port and offers what neither list holds",
      offers(tagged, 0), ["ether1", "ether4", "bridge1"])
check("...and its second row the same, minus the first's",
      offers(tagged, 1), ["ether2", "ether4", "bridge1"])
check("Untagged offers the same free ports, not the tagged ones",
      offers(untagged, 0), ["ether3", "ether4", "bridge1"])
check("the picker offers exactly what is free of both", free(tagged),
      ["ether4", "bridge1"])
check("...and offers it to either list", free(untagged),
      ["ether4", "bridge1"])


# --- freeing a port next door frees it here ---------------------------------

untagged._on_remove(untagged._rows[0][2])       # drop ether3 from Untagged
app.processEvents()
check("a port dropped next door is a candidate here at once",
      free(tagged), ["ether3", "ether4", "bridge1"])
check("...and appears in the row dropdowns too",
      offers(tagged, 0), ["ether1", "ether3", "ether4", "bridge1"])
ok("...without marking this list as edited", not tagged.is_dirty())
check("...and the list that was edited still is",
      (untagged.is_dirty(), untagged.values()), (True, []))

tagged.add_values([3])                           # take ether3 as tagged
app.processEvents()
check("taking it here withdraws it from there", free(untagged),
      ["ether4", "bridge1"])
check("...and the row that took it holds it", tagged.values(), [1, 2, 3])


# --- a new row never lands on a port the other list holds -------------------

win = make_form(BRIDGE_VLAN, {
    0x02: 6, 0x01: [100, 100], TAGGED: [1], UNTAGGED: [2, 3],
})
tagged, untagged = find(win, "Tagged"), find(win, "Untagged")
tagged._on_add_after(tagged._rows[0][2])         # ▾
app.processEvents()
check("▾ gives the next port free of both lists",
      [c.currentText() for c, _b, _r in tagged._rows], ["ether1", "ether4"])

check("a batch add refuses a port the other list holds",
      tagged.add_values([2, 6]), 1)
check("...and takes only the free one", tagged.values(), [1, 4, 6])
ok("...leaving nothing to add", not tagged._can_add())
btn = tagged._rows[0][1].findChild(type(tagged._add_first), "addRow")
check("...and the ▾ says which lists to look in",
      btn.toolTip(), "Every entry is already in this list or Untagged")


# --- a pair the device reported is kept -------------------------------------

win = make_form(BRIDGE_VLAN, {
    0x02: 6, 0x01: [100, 100], TAGGED: [1, 2], UNTAGGED: [2],
})
tagged, untagged = find(win, "Tagged"), find(win, "Untagged")
check("a port the device put in both keeps its row here", tagged.values(),
      [1, 2])
check("...and there", untagged.values(), [2])
ok("...with neither list reading as edited",
   not tagged.is_dirty() and not untagged.is_dirty())
check("...and it is offered nowhere else", free(tagged),
      ["ether3", "ether4", "bridge1"])


# --- the readings restrict nothing ------------------------------------------

win = make_form(BRIDGE_VLAN, {
    0x02: 6, 0x01: [100, 100], TAGGED: [1], UNTAGGED: [2],
    0x05: [1, 4], 0x06: [2, 6],          # Current Tagged / Current Untagged
})
tagged = find(win, "Tagged")
ok("Current Tagged is a reading", isinstance(find(win, "Current Tagged"),
                                             ReadOnlyValueList))
check("...so what it holds is still on offer", free(tagged),
      ["ether3", "ether4", "bridge1"])
check("...and only the editable peer restricts anything",
      [p._name for p in tagged._peers], ["Untagged"])


# --- a lone list is untouched ----------------------------------------------

lone = MultiEnumArrayEditor(sorted(PORTS.items()), [1])
check("a list with no peer offers its whole vocabulary", free(lone),
      ["ether2", "ether3", "ether4", "bridge1"])
ok("...and has no peers to ask", lone._peers == [])

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all good")
