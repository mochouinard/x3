"""``concat`` names and ``type:lookup`` columns.

No device and no pytest required:

    .venv/bin/python test_lookup_columns.py

Two ways the .jg reaches outside one field, both of them ignored until now.

**What an object is called.** ``nameval`` says which field names an object, and
55 menus build a compound name out of a ``concat`` tree instead of a single
field: a DHCP lease is ``Address,Active Address``, a walled-garden entry
``Dst. Host/Path``, a firewall rule
``Src. Address:Src. Port->Dst. Address:Dst. Port``. Only the plain-string form
was read, so every one of those 55 fell back to guessing -- a hardcoded
preference list of column names, which is not what the schema says.

**A column from another table.** ``{name:'Bridge Port',type:'lookup',
path:[16,3],src:'MAC Address',dst:'MAC Address',val:'Interface',width:80}``
reads: find the row of handler (16,3) whose MAC Address is this row's MAC
Address, and show its Interface. Six columns across ARP, DHCP Leases and the
IPv6 Neighbor list are declared this way, and they were dropped on the floor
because they are not fields of the menu at all.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema
from x3.ui.widgets.list_pane import ListPane

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
ARP = (20, 8)
LEASES = (23, 1)
BRIDGE_PORT = (16, 3)
arp = schema.record(ARP)
leases = schema.record(LEASES)
rules = schema.record((20, 42))

ARP_IP, ARP_IFACE, ARP_MAC = 0x1, 0x3, 0x7


# --- names ------------------------------------------------------------------

check("a lease is named by two fields, joined",
      sch.nameval_field_names(leases), ["Address", "Active Address"])
check("...a firewall rule by four, in two pairs",
      sch.nameval_field_names(rules),
      ["Src. Address", "Src. Port", "Dst. Address", "Dst. Port"])
check("...and an ARP entry by one", sch.nameval_field_names(arp),
      ["IP Address"])

check("the label joins them with the .jg's own separators",
      sch.object_label(rules, {
          "Src. Address": "10.0.0.1", "Src. Port": "22",
          "Dst. Address": "8.8.8.8", "Dst. Port": "53"}),
      "10.0.0.1:22->8.8.8.8:53")
check("...dropping a part that is empty, and its separator with it",
      sch.object_label(rules, {"Dst. Address": "8.8.8.8"}), "8.8.8.8")
check("...and not repeating a value that appears twice",
      sch.object_label(leases, {"Address": "192.168.88.10",
                                "Active Address": "192.168.88.10"}),
      "192.168.88.10")
check("a lease that is not active is named by its address alone",
      sch.object_label(leases, {"Address": "192.168.88.10"}),
      "192.168.88.10")
check("an object with nothing to name it gets no name, not punctuation",
      sch.object_label(leases, {}), "")


# --- lookup columns ---------------------------------------------------------

def lookups(menu):
    return [
        (c.name, c.lookup_path, c.lookup_src, c.lookup_dst, c.lookup_val, c.width)
        for c in sch.default_columns(menu) if c.kind == "lookup"
    ]


check("ARP declares two columns that live in other tables", lookups(arp),
      [("Bridge Port", BRIDGE_PORT, "MAC Address", "MAC Address",
        "Interface", 80),
       ("Host Name", LEASES, "MAC Address", "Active MAC Address",
        "Active Host Name", 120)])
check("...and a lease one", lookups(leases),
      [("Bridge Port", BRIDGE_PORT, "Active MAC Address", "MAC Address",
        "Interface", 80)])
ok("a lookup is not a field of the menu it is shown on",
   not any((f.name or "") == "Bridge Port" for f in arp.fields.values()))
ok("...and it is offered as a default column, being declared with a width",
   "Bridge Port" in [c.name for c in sch.default_columns(arp)])


MAC = bytes.fromhex("48a98a1122cc")
OTHER_MAC = bytes.fromhex("48a98a3344ee")


def arp_row(mac: bytes, ip: int):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    msg.add_u32(ARP_IP, ip)
    msg.add_u32(ARP_IFACE, 2)
    # ARP's MAC Address is `{type:'opt',id:'b7',c:[{type:'macaddr',id:'r2'}]}`:
    # the flag at 0x7 says it is set, the value itself lives at 0x2.
    msg.add_raw(0x2, mac)
    msg.add_bool(ARP_MAC, True)
    return RowObject(
        path=ARP, raw=msg, fields=schema.decode(ARP, msg),
        form_fields=schema.decode_form(ARP, msg),
        values_by_code={c: v for c, _t, v in msg},
    )


#: (16,3) is the bridge host table: MAC Address at 0x2, and Interface at 0x1 as
#: an enum whose names come from (20,0) -- so a lookup's value is only readable
#: with the interface list resolved, which is what the real client does.
PORT_IFACE, PORT_MAC = 0x1, 0x2
IFACES = {5: "ether5", 7: "ether7"}


def port_row(mac: bytes, iface_id: int):
    msg = m.Message()
    msg.add_u32(0xFE0001, 90 + iface_id)
    msg.add_u32(PORT_IFACE, iface_id)
    msg.add_raw(PORT_MAC, mac)
    return RowObject(
        path=BRIDGE_PORT, raw=msg,
        fields=schema.decode(BRIDGE_PORT, msg, resolver=lambda _p: IFACES),
        form_fields=[],
        values_by_code={c: v for c, _t, v in msg},
    )


class StubClient(DeviceClient):
    """Answers GETALL from a fixture and counts the reads."""

    def __init__(self, tables):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.schema = schema
        self.session = None
        self._tables = tables
        self.reads: list[tuple[int, ...]] = []

    def getall(self, path, **kw):
        self.reads.append(tuple(path))
        return list(self._tables.get(tuple(path), []))

    def uptime(self):
        return 0.0

    def enum_map(self, path, **kw):
        return {2: "bridge1"}


ports = schema.record(BRIDGE_PORT)
check("the lookup's own fields are where the .jg says",
      [(ports.fields[PORT_MAC].name, ports.fields[PORT_MAC].jg_type),
       (ports.fields[PORT_IFACE].name, ports.fields[PORT_IFACE].jg_type)],
      [("MAC Address", "macaddr"), ("Interface", "enm")])

client = StubClient({
    ARP: [arp_row(MAC, 0x0158A8C0)],
    BRIDGE_PORT: [port_row(MAC, 5), port_row(OTHER_MAC, 7)],
})
table = client.lookup_map(BRIDGE_PORT, "MAC Address", "Interface")
check("the second read is keyed by the field the .jg names",
      table.get("48:a9:8a:11:22:cc"), "ether5")
check("...and holds every row of it", len(table), 2)
client.lookup_map(BRIDGE_PORT, "MAC Address", "Interface")
check("...read once, not once per row", client.reads.count(BRIDGE_PORT), 1)
client.forget_lookups()
client.lookup_map(BRIDGE_PORT, "MAC Address", "Interface")
check("...and again after a Refresh drops it",
      client.reads.count(BRIDGE_PORT), 2)

menu, col_names, rows, raw_rows, _up = client.table_rows(ARP)
ok("Bridge Port is a column of the ARP list", "Bridge Port" in col_names,
   f"{col_names}")
cell = rows[0][col_names.index("Bridge Port")]
check("...filled from the other table", cell, "ether5")
hostname = rows[0][col_names.index("Host Name")]
check("a lookup with no table behind it is blank, not an error", hostname, "")

# A MAC that is in no bridge port leaves the cell empty rather than guessing.
client2 = StubClient({
    ARP: [arp_row(OTHER_MAC, 0x0258A8C0)],
    BRIDGE_PORT: [port_row(MAC, 5)],
})
_m2, names2, rows2, _r2, _u2 = client2.table_rows(ARP)
check("an unmatched row gets an empty cell",
      rows2[0][names2.index("Bridge Port")], "")


# --- what the lookup needed on the way in -----------------------------------
# ARP's MAC Address is `{type:'opt',id:'b7',c:[{type:'macaddr',id:'r2'}]}` --
# optional but not negatable. Every opt was being read as the firewall shape
# (opt -> not -> value), which left the shorter one with no value field at all,
# so 267 of the 629 optional fields in 7.21 rendered as the bare word "set".
# The lookup could not have found its source value without this.
arp_mac = arp.fields[0x7]
neg, inner = sch.opt_chain(arp_mac)
check("a plain optional has a value and no negate",
      (neg, inner.code if inner else None, inner.jg_type if inner else None),
      (None, 0x2, "macaddr"))
fw_src = next(
    f for f in rules.fields.values()
    if f.jg_type == "opt" and f.name == "Src. Address"
)
fneg, finner = sch.opt_chain(fw_src)
ok("a firewall match still has both", fneg is not None and finner is not None,
   f"{fneg} {finner}")
check("...with the negate in the middle, as declared",
      fneg.jg_type if fneg else None, "not")
check("an ARP row now says which MAC, not just that it has one",
      {n: v for _c, n, v, _ok in arp_row(MAC, 0x0158A8C0).fields}
      .get("MAC Address"),
      "48:A9:8A:11:22:CC")


# --- the label the UI shows -------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)

pane = ListPane("Leases", path=LEASES)
pane._menu = leases
pane._columns = ["", "Address", "Active Address", "MAC Address"]


class LeaseRow:
    fields = [
        (0x1, "Address", "192.168.88.50", True),
        (0x2, "Active Address", "192.168.88.50", True),
        (0x3, "MAC Address", "48:A9:8A:11:22:CC", True),
    ]
    values_by_code: dict = {}


check("a lease row is labelled the way the .jg names it",
      pane._row_label(["", "192.168.88.50", "192.168.88.50",
                       "48:A9:8A:11:22:CC"], LeaseRow()),
      "192.168.88.50")
pane._menu = None
check("...and with no menu the old guessing still answers",
      pane._row_label(["", "192.168.88.50", "", ""]), "192.168.88.50")
pane.shutdown()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall lookup/label tests passed")
