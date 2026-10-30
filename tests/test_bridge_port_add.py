"""Bridge ▸ Ports ▸ + , filled in the way Winbox v3 fills it in.

No device and no pytest required:

    .venv/bin/python test_bridge_port_add.py

Four things v3 does on an Add form that ours did not:

* **The tree link is not a row.** ``treeon:'Parent'`` names the field holding
  each row's parent, and every menu that declares one declares that field
  ``nonpublic`` -- a bridge port's Parent, a file's container, a package's
  bundle. Winbox nests the list on it and never draws it; ours opened every new
  port with "Parent: 0".

* **A dropdown's source table is narrowed by its ``pred``.** Bridge reads
  ``{type:'dynamic',path:[20,0],pred:{slot:'type',pred:{number,[12]}}}`` --
  every interface, filtered to the bridges. We read the path and dropped the
  pred, so the Bridge box offered ether1. 217 dynamic sources in 7.21 carry
  one -- 140 fields once the overlays are merged -- and every one of them was
  reading the whole table.

* **A dropdown is never empty.** A new object has no value for anything the
  .jg gave no ``def``, and Winbox's combo box cannot show nothing: it opens on
  its first entry, which is also what the ADD writes. That is where v3's
  Learn: auto, Frame Types: admit all and MVRP normal / normal participant
  come from -- not from a ``def``, since not one of them declares one in any
  of the 208 builds cached locally (6.30 → 7.21.1). The firewall-rule ADD in
  a session capture (frame #546) is the proof on
  the wire: Winbox sends every field, and the two enums with no ``def``
  (Action, Mode) go out as 0 -- entry one of each.

* **A number starts at its floor.** 90 of those 208 builds declare ``def:1``
  on PVID; the 7.x ones dropped the def and kept ``min:1``, and v3 opens on
  PVID 1 either way, because 0 is not a value the box can hold. Ours wrote 0,
  which the router reads as "no VLAN".

The one v3 default not derivable here is **Multicast Router: temporary
query**. The .jg gives `def:1` to the *bridge*'s Multicast Router (`u71`) and
nothing at all to the *port*'s (`u1e`) -- in all 208 builds -- so the
first-entry rule lands on Disabled. And the .jg is all v3 has to go on: in the
capture above, opening an Add form sends no request of its own (frames #526 →
#546 are GETALLs of the list). Whatever shows temporary query there is not in
the schema; see the note at the end of this file.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QComboBox, QSpinBox

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.property_window import PropertyWindow

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

schema = Schema.load(SCHEMA_DIR)
PORTS = (16, 2)
IFACES = (20, 0)
ports = schema.record(PORTS)
ifaces = schema.record(IFACES)

# bridge port fields
PARENT, IFACE, BRIDGE = 0x5, 0x3, 0x4
LEARN, EDGE, P2P, MCAST = 0xC, 0xA, 0xB, 0x1E
PRIORITY, PVID, FRAME_TYPES = 0x2, 0x13, 0x14
MVRP_REG, MVRP_APP = 0x22, 0x23
# interface fields the preds read
IFACE_TYPE, IFACE_CAPS, IFACE_NAME = 0x10001, 0x10002, 0x10006
BRIDGE_TYPE = 12


# --- what the .jg says ------------------------------------------------------

check("the port list is a tree on Parent", ports.tree_on, "Parent")
ok("...and Parent is nonpublic", ports.fields[PARENT].nonpublic)

check("Bridge reads the interface table", ports.fields[BRIDGE].enum_path,
      IFACES)
check("...narrowed to type 12", ports.fields[BRIDGE].enum_pred,
      {"type": "slot", "slot": "type",
       "pred": {"type": "number", "value": [BRIDGE_TYPE]}})
check("Interface reads the same table", ports.fields[IFACE].enum_path, IFACES)
check("...narrowed to what can be a port", ports.fields[IFACE].enum_pred,
      {"type": "slot", "slot": "caps",
       "pred": {"type": "bitmap", "mask": 64, "value": 64}})
check("140 fields are narrowed this way, not one",
      sum(1 for mn in list(schema.menus) + list(schema.overlay_list)
          for f in mn.fields.values() if f.enum_pred), 140)

for code, name in ((LEARN, "Learn"), (EDGE, "Edge"), (P2P, "Point To Point"),
                   (MCAST, "Multicast Router"), (FRAME_TYPES, "Frame Types"),
                   (MVRP_REG, "MVRP Registrar State"),
                   (MVRP_APP, "MVRP Applicant State")):
    fld = ports.fields[code]
    ok(f"{name} declares no def", fld.default is None, f"def={fld.default!r}")
check("PVID declares none either", ports.fields[PVID].default, None)
check("...but it does declare a floor", ports.fields[PVID].minimum, 1)
check("Priority is the one with a def", ports.fields[PRIORITY].default, 128)


# --- the source table, narrowed --------------------------------------------

def iface_row(oid, name, typ, caps=None):
    msg = m.Message()
    msg.add_u32(0xFE0001, oid)
    msg.add_string(IFACE_NAME, name)
    msg.add_u32(IFACE_TYPE, typ)
    values = {0xFE0001: oid, IFACE_NAME: name, IFACE_TYPE: typ}
    if caps is not None:
        msg.add_u32(IFACE_CAPS, caps)
        values[IFACE_CAPS] = caps
    return RowObject(path=IFACES, raw=msg, fields=[], form_fields=[],
                     values_by_code=values)


# Real `caps` bitmaps, off the wire: an ether reads 0x62263 and a bridge
# 0x18223 (session captures), which
# is where the mask-64 pred gets its answer -- an ether has the bit, a bridge
# does not, so v3's Interface box does not offer to make a bridge a port.
ETHER_CAPS, BRIDGE_CAPS = 0x62263, 0x18223

TABLE = [
    iface_row(1, "ether1", 1, ETHER_CAPS),
    iface_row(2, "ether2", 1, ETHER_CAPS),
    iface_row(3, "bridge-lan", BRIDGE_TYPE, BRIDGE_CAPS),
    iface_row(4, "vlan10", 20, ETHER_CAPS),
]


class StubClient:
    """A canned interface table, read through the real dropdown code path."""

    schema = schema
    # The methods under test, not stand-ins for them.
    _enum_from_rows = DeviceClient._enum_from_rows
    enum_source_map = DeviceClient.enum_source_map
    enum_choices = DeviceClient.enum_choices

    def __init__(self, table=None, row=None):
        self._table = list(TABLE if table is None else table)
        self._row = row
        self._enum_cache: dict = {}
        self._enum_rows: dict = {}
        self._enum_pred_cache: dict = {}

    def menu_for(self, path, type_value=None, **kw):
        path = tuple(path)
        return (schema.record_for(path, type_value) if type_value is not None
                else schema.record(path))

    def enum_map(self, path):
        path = tuple(path)
        if path not in self._enum_cache:
            rows = self._table if path == IFACES else []
            self._enum_rows[path] = rows
            self._enum_cache[path] = self._enum_from_rows(path, rows)
        return self._enum_cache[path]

    def interface_choices(self):
        return [(r.values_by_code[0xFE0001], r.values_by_code[IFACE_NAME],
                 "ether") for r in self._table]

    def alloc_from(self):
        return (0, 7)

    def prefetch_enums(self, paths):
        pass

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row


client = StubClient()
check("the Bridge box offers the bridges", client.enum_choices(ports.fields[BRIDGE]),
      [(3, "bridge-lan")])
check("...and the Interface box what can be a port",
      client.enum_choices(ports.fields[IFACE]),
      [(1, "ether1"), (2, "ether2"), (4, "vlan10")])
check("rendering still knows the whole table",
      sorted(client.enum_map(IFACES).items()),
      [(1, "ether1"), (2, "ether2"), (3, "bridge-lan"), (4, "vlan10")])

# A handler that does not volunteer the slot must not empty the box: offering
# too much beats offering nothing.
blind = StubClient([iface_row(1, "ether1", 1), iface_row(3, "br0", BRIDGE_TYPE)])
check("a reply with no caps field offers every row",
      blind.enum_choices(ports.fields[IFACE]),
      [(1, "ether1"), (3, "br0")])
check("...while a slot it did send still filters",
      blind.enum_choices(ports.fields[BRIDGE]), [(3, "br0")])


# --- the Add form -----------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)


class Session:
    readonly = False
    live = False
    client = None


def add_form():
    """What clicking + on the port list opens, built the way the pane does."""
    opened = {}

    def capture(path, row, title, row_key, **kw):
        opened["path"], opened["row"], opened["key"] = path, row, row_key

    pane = ListPane("Ports", path=PORTS, edit_session=Session())
    pane._menu = ports
    pane._on_open_properties = capture
    pane._action_add()
    row = opened["row"]
    win = PropertyWindow(
        "Ports — (new)", PORTS, row,
        client=StubClient(row=row),
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=ports,
        row_key=opened["key"],
    )
    app.processEvents()
    # The STP and VLAN tabs are built on demand; a save touches them all.
    win._ensure_all_pages()
    return win


win = add_form()
check("it really is an Add form", win._write_action(win._object_id()), "add")


def row_named(label):
    for spec, editor in win._rows:
        if spec.get("name") in (label, f"{label}:"):
            return spec, editor
    return None, None


def combo_text(label):
    _spec, editor = row_named(label)
    if not isinstance(editor, QComboBox):
        return f"<{type(editor).__name__}>"
    return editor.currentText()


ok("the tree link gets no row", row_named("Parent") == (None, None),
   "a Parent row was built")
ok("...though the field is still there to nest the list on",
   PARENT in ports.fields)

check("Bridge offers the bridges, not the interfaces",
      [combo_text("Bridge")], ["bridge-lan"])
check("Interface opens on the first one it may use", combo_text("Interface"),
      "ether1")
check("Learn opens on auto", combo_text("Learn"), "auto")
check("Edge opens on auto", combo_text("Edge"), "auto")
check("Point To Point opens on auto", combo_text("Point To Point"), "auto")
check("Frame Types opens on admit all", combo_text("Frame Types"),
      "admit all")
check("MVRP Registrar State opens on normal",
      combo_text("MVRP Registrar State"), "normal")
check("MVRP Applicant State opens on normal participant",
      combo_text("MVRP Applicant State"), "normal participant")

_spec, pvid = row_named("PVID")
ok("PVID is a box", isinstance(pvid, QSpinBox), f"{type(pvid).__name__}")
check("...opening on its floor, not on 0", pvid.value(), 1)

check("a def in the .jg is shown as the field renders it",
      row_named("Priority")[1].text(), "0x80")

# What the form shows is what the ADD writes: an Add sends every field, so the
# first entry of each box is the value the new object gets.
written = {e["code"]: e["new"] for e in win.edits()}
check("the ADD carries PVID 1", written.get(PVID), 1)
check("...Learn auto", written.get(LEARN), 0)
check("...Frame Types admit all", written.get(FRAME_TYPES), 0)
check("...and the bridge that was offered", written.get(BRIDGE), 3)
ok("...and no Parent at all", PARENT not in written,
   f"Parent was written as {written.get(PARENT)!r}")

# Multicast Router: the first-entry rule, with nothing in the .jg to say
# otherwise. Recorded rather than asserted-away, so a build (or a capture)
# that does carry a def shows up here as a failure to look at.
check("Multicast Router opens on the first entry the .jg gives it",
      combo_text("Multicast Router"), "Disabled")


# --- an existing object still says nothing where it said nothing -----------
#
# The first-entry rule is the Add form's alone. On a real object a field the
# reply left out has to keep reading blank: filling it in would offer to write
# a value the router never sent, and a SET only sends what looks changed.

msg = m.Message()
msg.add_u32(0xFE0001, 21)
msg.add_u32(IFACE, 1)
msg.add_u32(BRIDGE, 3)
values = {code: value for code, _tag, value in msg}
existing = RowObject(
    path=PORTS, raw=msg, fields=schema.decode(PORTS, msg),
    form_fields=schema.decode_form(PORTS, msg), values_by_code=values,
)
live = PropertyWindow(
    "bridge-lan", PORTS, existing,
    client=StubClient(row=existing),
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=ports, row_key="21", full_row=True,
)
app.processEvents()
live._ensure_all_pages()
win = live
check("an existing row is a set", win._write_action(win._object_id()), "set")
check("...and a field it did not send reads blank", combo_text("Learn"), "")
check("...its PVID box too", row_named("PVID")[1].value(), 0)
check("...so nothing is queued from opening it", win.edits(changed_only=True),
      [])
ok("...and its Parent is still not a row", row_named("Parent") == (None, None))


# --- the open question: Multicast Router -----------------------------------
#
# v3 is reported to open a new bridge port on temporary query, and nothing in
# the schema says so (see the header). Two ways to settle it, both offline of
# this test:
#
#   * Capture the reference client adding a port: the ADD frame's `0x1e` is
#     what it actually writes. 0 = it shows Disabled like we do; 1 = it knows
#     something the .jg does not.
#   * Read the object back. `/interface bridge port add` from the console, then
#     GETALL (16,2): that gives the router's own default for a port it made
#     itself, which is what an ADD that leaves 0x1e out would produce.
#
# Until then this file records what the .jg supports, and the check above will
# fail loudly if a build ever does declare the def.

print()
if failures:
    print(f"{len(failures)} failure(s):")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("all checks passed")
