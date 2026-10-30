"""A reported array is a box with one entry per line, not a comma run.

No device and no pytest required:

    .venv/bin/python test_reported_lists.py

Winbox v3 shows a read-only list field -- Bridge ▸ VLANs' **Current Tagged**
and **Current Untagged**, Bridge ▸ Ports' **Declared** / **Registered VLAN
IDs**, Ethernet's Advertising -- as a non-editable multi-line box holding one
entry per line. Ours squeezed everything but the enum-vocabulary ones into a
single-line label, so a twelve-port bridge's Current Tagged read as a comma run
running off the right of the form.

Three things decide the row, and each is checked here.

* **Declared an array, and carrying one.** Twenty-odd switch-port counters
  (`Tx&Rx 64`, `Drop Byte`) are declared ``multibignumber`` and carry a single
  number, which Winbox shows in a plain box -- and which the live poll
  refreshes through the label path. Going by the declaration alone turned every
  one of them into a one-line list box that then never updated.
* **Each line is rendered as an element**, not carved out of the row's text.
  That is what tells `100-101` (a range list is flat (low, high) pairs) from
  `100, 101`, and it needs the field's own vocabulary: an interface id is
  resolved against the interface table, the same one the editable twin's
  dropdowns are filled from.
* **It follows the device.** Current Tagged is what the bridge *did* with the
  ports Tagged asked for, so it moves on Apply and on every refresh. An order
  the reader chose (right-click ▸ Sort By Name) survives the new reading; the
  device's own does not.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import (
    MultiEnumArrayEditor,
    PropertyWindow,
    ReadOnlyValueEdit,
    ReadOnlyValueList,
    _is_reported_list,
    _set_locked,
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
BRIDGE_PORT = (16, 2)
IFACE = (20, 0)

#: The interface table a bridge VLAN's ports are named from.
PORTS = {1: "ether1", 2: "ether2", 3: "ether3", 4: "ether4", 6: "bridge1"}


class StubClient:
    """Just enough client for a form: menus, and an interface table."""

    schema = schema

    def __init__(self, row=None):
        self._row = row
        self.enum_reads: list[tuple[int, ...]] = []

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
        self.enum_reads.append(tuple(path))
        return dict(PORTS) if tuple(path) == IFACE else {}

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        return [(k, v, "ether") for k, v in sorted(PORTS.items())]


def make_row(path, fields: dict, *, resolver=None):
    """A RowObject the way the device hands one over, arrays and all."""
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)                          # .id
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


def make_form(path, fields: dict, *, resolver=None):
    client = StubClient()
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
    win._ensure_all_pages()      # Declared VLAN IDs lives on Status
    app.processEvents()
    return win, client


def find(win, label):
    for spec, ed in win._rows:
        if spec.get("name") == label:
            return spec, ed
    return None, None


def lines(editor):
    return editor.toPlainText().split("\n") if editor.toPlainText() else []


# --- Bridge ▸ VLANs: Current Tagged / Current Untagged -----------------------

win, client = make_form(BRIDGE_VLAN, {
    0x02: 6,                        # Bridge
    0x01: [100, 101],               # VLAN IDs
    0x03: [1, 2],                   # Tagged
    0x04: [3],                      # Untagged
    0x05: [1, 2, 6],                # Current Tagged
    0x06: [3, 4],                   # Current Untagged
}, resolver=lambda p: dict(PORTS) if tuple(p) == IFACE else {})

spec, current = find(win, "Current Tagged")
ok("Current Tagged is a box, not a label",
   isinstance(current, ReadOnlyValueList), f"{type(current).__name__}")
check("...with one port per line", lines(current), ["ether1", "ether2", "bridge1"])
ok("...that cannot be typed into", current.isReadOnly())
ok("...and the form leaves it that way", spec.get("locked") is True)
_set_locked(current, False)
ok("...even when the toolbar unlocks the form", current.isReadOnly())

spec, untagged = find(win, "Current Untagged")
ok("Current Untagged is the same kind of row",
   isinstance(untagged, ReadOnlyValueList), f"{type(untagged).__name__}")
check("...with its own ports", lines(untagged), ["ether3", "ether4"])

_spec, tagged = find(win, "Tagged")
ok("...while Tagged, which is written, keeps its dropdowns",
   isinstance(tagged, MultiEnumArrayEditor), f"{type(tagged).__name__}")


# --- the names come from the interface table, not from the row's text --------
# A row decoded without a resolver renders the ids as numbers. The box asks the
# same table the dropdowns are filled from, so it still reads as ports.

win2, client2 = make_form(BRIDGE_VLAN, {
    0x02: 6, 0x03: [1, 2], 0x05: [1, 2, 6],
})
spec2, current2 = find(win2, "Current Tagged")
check("the row itself rendered the ids as ids", spec2.get("rendered"), "1, 2, 6")
check("...and the box names them anyway",
      lines(current2), ["ether1", "ether2", "bridge1"])
ok("...by reading the interface table", IFACE in client2.enum_reads,
   f"read {client2.enum_reads}")

# An id the table does not name keeps its own place rather than borrowing a
# neighbour's name.
win3, _c = make_form(BRIDGE_VLAN, {0x02: 6, 0x05: [1, 99]})
_s, current3 = find(win3, "Current Tagged")
check("an id the table cannot name is still its own line, as plainly as it can be",
      lines(current3), ["ether1", "99"])


# --- Bridge ▸ Ports: Declared / Registered VLAN IDs --------------------------
# A range list is a flat run of (low, high) pairs, so a line is a *range*.

win4, _c = make_form(BRIDGE_PORT, {
    0xDE: [100, 101, 105, 105],       # Declared VLAN IDs
    0xDF: [1, 1, 200, 210],           # Registered VLAN IDs
})
_s, declared = find(win4, "Declared VLAN IDs")
ok("Declared VLAN IDs is a box too",
   isinstance(declared, ReadOnlyValueList), f"{type(declared).__name__}")
check("...one line per range, not per number", lines(declared), ["100-101", "105"])
_s, registered = find(win4, "Registered VLAN IDs")
check("...and a single VLAN is one number, not a range of itself",
      lines(registered), ["1", "200-210"])


# --- a declared array carrying one value is not a list -----------------------

port = schema.record(BRIDGE_PORT)
declared_fld = next(f for c, f in port.fields.items() if c == 0xDE)
ok("an array field with an array in it is a list",
   _is_reported_list(declared_fld, [100, 101]))
ok("...and the same field with a single number is not",
   not _is_reported_list(declared_fld, 100))
ok("...nor is one with nothing in it",
   not _is_reported_list(declared_fld, None))
iface = schema.merge_type_overlay(schema.record(IFACE), 1)
counter = iface.fields[0x471]                    # Tx&Rx 64 -- multibignumber
check("the switch-port counter this protects is declared an array",
      (counter.name, counter.jg_type, counter.ro),
      ("Tx&Rx 64", "multibignumber", True))
ok("...and carrying its single number it stays a plain box",
   not _is_reported_list(counter, 8459480))


# --- it follows the device ---------------------------------------------------

box = ReadOnlyValueList([1, 2], "ether1, ether2",
                        schema.record(BRIDGE_VLAN).fields[0x05],
                        lookup=dict(PORTS))
check("the box starts on what the device sent", lines(box), ["ether1", "ether2"])
box.restate([1, 2, 6], "ether1, ether2, bridge1")
check("...and a refresh restates it", lines(box), ["ether1", "ether2", "bridge1"])

box._sorted = True                                # right-click ▸ Sort By Name
box.set_items(sorted(box.values(), key=lambda t: t))
check("sorted, it reads alphabetically",
      lines(box), ["bridge1", "ether1", "ether2"])
box.restate([2, 6], "ether2, bridge1")
check("...and the reader's order survives the next reading",
      lines(box), ["bridge1", "ether2"])
check("...while Device Order still knows what the device said",
      box._as_reported, ["ether2", "bridge1"])

grown = ReadOnlyValueList([1], "ether1",
                          schema.record(BRIDGE_VLAN).fields[0x05],
                          lookup=dict(PORTS))
one_line = grown.height()
grown.restate([1, 2, 3, 4], "ether1, ether2, ether3, ether4")
ok("a box that grew is taller than a box that did not",
   grown.height() > one_line, f"{one_line} -> {grown.height()}")

empty = ReadOnlyValueList(None, "—", schema.record(BRIDGE_VLAN).fields[0x05])
check("nothing reported is no lines, not a dash", lines(empty), [])

# The rendered text is the fallback when there is no vocabulary in hand -- a
# box built without a lookup must not print bare object ids.
no_lookup = ReadOnlyValueList([1, 2, 6], "ether1, ether2, bridge1",
                              schema.record(BRIDGE_VLAN).fields[0x05])
check("with no table to read, the row's own text is split instead",
      lines(no_lookup), ["ether1", "ether2", "bridge1"])

ok("a plain status value is still a plain box",
   isinstance(ReadOnlyValueEdit("1500"), ReadOnlyValueEdit))

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all reported-list tests passed")
