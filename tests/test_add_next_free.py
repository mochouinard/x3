"""A second row opens on a name the first one did not take.

No device and no pytest required:

    .venv/bin/python test_add_next_free.py

Winbox suggests `vlan2` for a second VLAN because `vlan1` is spoken for, and
`_next_default_name` has always done that for a *string* name. 30-odd menus
name their rows with a dropdown instead -- ``nameval:'Interface'`` read off the
live interface table -- and for those the name is the row's identity: one
bridge port per interface, one DHCP client per interface. There, entry one is
the one value a second row cannot be saved with, so a new port on a bridge that
already holds ether1 has to open on ether2.

What is *offered* is untouched, and deliberately: a dropdown's contents are the
.jg's business. Bridge Port ▸ Interface declares exactly one pred --
``{slot:'caps', pred:{bitmap, mask:64}}`` -- and `caps` does not move when an
interface becomes a port. Checked on the wire against the 7.24 lab VM:
enslaving ether6 to bridge1 changes only

    b10029 (`slave`)  absent -> True
    s10020 (master)   absent -> 'br0'
    u10003 (flags)    542017 -> 546121

while u10002 (`caps`) stays 0x42263 either way. And no dynamic source in any
7.21 .jg preds on ``slave`` at all (the whole pred-slot census is wbands, type,
wbands5, wphymodes, caps, hwtype, family, Type, usage, _suppbands, _suppchans,
hasextscan, default, block device, dynamic, master), so no Winbox dropdown
anywhere hides an interface that is already taken. Ours does not either.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QComboBox

from x3.proto import message as m
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema, name_field_code
from x3.ui.edit_mode import EditMode, EditSession, QueuedChange
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.property_window import PropertyWindow

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

schema = Schema.load(SCHEMA_DIR)
PORTS = (16, 2)
IFACES = (20, 0)
ports = schema.record(PORTS)

IFACE, BRIDGE = 0x3, 0x4
IFACE_TYPE, IFACE_CAPS, IFACE_NAME = 0x10001, 0x10002, 0x10006
# Real caps off the wire: an ether has the mask-64 bit, a bridge does not.
ETHER_CAPS, BRIDGE_CAPS, BRIDGE_TYPE = 0x62263, 0x18223, 12


def iface_row(oid, name, typ, caps):
    msg = m.Message()
    msg.add_u32(0xFE0001, oid)
    msg.add_string(IFACE_NAME, name)
    msg.add_u32(IFACE_TYPE, typ)
    msg.add_u32(IFACE_CAPS, caps)
    return RowObject(
        path=IFACES, raw=msg, fields=[], form_fields=[],
        values_by_code={0xFE0001: oid, IFACE_NAME: name, IFACE_TYPE: typ,
                        IFACE_CAPS: caps},
    )


TABLE = [
    iface_row(1, "ether1", 1, ETHER_CAPS),
    iface_row(2, "ether2", 1, ETHER_CAPS),
    iface_row(3, "ether3", 1, ETHER_CAPS),
    iface_row(9, "bridge1", BRIDGE_TYPE, BRIDGE_CAPS),
]


def port_row(oid, iface, bridge=9):
    """A bridge port as the device sends it back."""
    msg = m.Message()
    msg.add_u32(0xFE0001, oid)
    msg.add_u32(IFACE, iface)
    msg.add_u32(BRIDGE, bridge)
    values = {code: value for code, _tag, value in msg}
    return RowObject(
        path=PORTS, raw=msg, fields=schema.decode(PORTS, msg),
        form_fields=schema.decode_form(PORTS, msg), values_by_code=values,
    )


class StubClient:
    """A canned interface table, read through the real dropdown code path."""

    schema = schema
    _enum_from_rows = DeviceClient._enum_from_rows
    enum_source_map = DeviceClient.enum_source_map
    enum_choices = DeviceClient.enum_choices

    def __init__(self, row=None):
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
            rows = TABLE if path == IFACES else []
            self._enum_rows[path] = rows
            self._enum_cache[path] = self._enum_from_rows(path, rows)
        return self._enum_cache[path]

    def interface_choices(self):
        return [(r.values_by_code[0xFE0001], r.values_by_code[IFACE_NAME],
                 "ether") for r in TABLE]

    def alloc_from(self):
        return (0, 7)

    def prefetch_enums(self, paths):
        pass

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row


app = QApplication.instance() or QApplication(sys.argv)

check("the port list names its rows by Interface",
      name_field_code(ports), IFACE)


def add_form(existing, queued=()):
    """What clicking + opens on a list that already holds `existing` ports."""
    session = EditSession(EditMode.QUEUE, client=StubClient())
    for change in queued:
        session.enqueue(change)
    opened = {}

    def capture(path, row, title, row_key, **kw):
        opened["row"], opened["key"] = row, row_key

    pane = ListPane("Ports", path=PORTS, edit_session=session)
    pane._menu = ports
    pane._raw_rows = list(existing)
    pane._on_open_properties = capture
    pane._action_add()
    row = opened["row"]
    win = PropertyWindow(
        "Ports — (new)", PORTS, row,
        client=StubClient(row=row),
        edit_session=session,
        menu=ports,
        row_key=opened["key"],
    )
    app.processEvents()
    win._ensure_all_pages()
    return win


def combo_text(win, label):
    for spec, editor in win._rows:
        if spec.get("name") in (label, f"{label}:"):
            return (editor.currentText() if isinstance(editor, QComboBox)
                    else f"<{type(editor).__name__}>")
    return None


def written(win, code):
    return {e["code"]: e["new"] for e in win.edits()}.get(code)


def combo_items(win, label):
    for spec, editor in win._rows:
        if spec.get("name") in (label, f"{label}:"):
            if isinstance(editor, QComboBox):
                return [editor.itemText(i) for i in range(editor.count())]
    return None


# --- the rule ---------------------------------------------------------------

win = add_form([])
check("an empty list still opens on entry one", combo_text(win, "Interface"),
      "ether1")

win = add_form([port_row(21, 1)])
check("ether1 taken, so the next port opens on ether2",
      combo_text(win, "Interface"), "ether2")
check("...and that is what the ADD writes", written(win, IFACE), 2)
check("...while the list still offers ether1, taken or not",
      combo_items(win, "Interface"), ["ether1", "ether2", "ether3"])

win = add_form([port_row(21, 1), port_row(22, 2)])
check("two taken, so the third", combo_text(win, "Interface"), "ether3")

win = add_form([port_row(21, 1), port_row(22, 2), port_row(23, 3)])
check("every one taken falls back to entry one, not an empty box",
      combo_text(win, "Interface"), "ether1")

# A row the device has never seen: in Queue mode the first port is still in the
# local queue when + is clicked for the second, which is the case that needs
# this most.
pending = QueuedChange(
    path=PORTS, title="Ports — add", row_key="(new)", action="add",
    edits=[{"code": IFACE, "name": "Interface", "new": 1, "label": "ether1"}],
)
win = add_form([], queued=[pending])
check("a queued Add counts as taken too", combo_text(win, "Interface"),
      "ether2")

# The list is nested on Parent, and a port that is a member of a bridge whose
# own row is elsewhere still holds its interface: nothing here depends on which
# bridge took it, because an interface can only be a port of one.
win = add_form([port_row(21, 2, bridge=9)])
check("it is the interface that is taken, not the bridge slot",
      combo_text(win, "Interface"), "ether1")


# --- a string name is still the old rule -----------------------------------
#
# _next_default_name and this one are disjoint by construction: no menu in 7.21
# declares both a `prefix` and an `enm` nameval, so neither ever writes a
# string into a dropdown or an object id into a name box.
both = [
    mn.name for mn in list(schema.menus) + list(schema.overlay_list)
    if (lambda f: f is not None and (f.jg_type or "") == "enm" and f.enum_path)(
        mn.fields.get(name_field_code(mn) or -1))
    and getattr(mn, "cli_prefix", None)
]
check("no menu is claimed by both naming rules", both, [])

print()
if failures:
    print(f"{len(failures)} failure(s):")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("all checks passed")
