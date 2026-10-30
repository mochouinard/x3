"""Which columns a list opens with, and how wide.

No device and no pytest required:

    .venv/bin/python test_default_columns.py

Winbox's default column set was being guessed. ``preferred_columns`` sorted
the menu's fields by a hand-written list of good names ("name", "address",
"interface", …) and cut it off at eight, so Interface opened on Name, Default
Name, Actual MTU, L2 MTU, VRF and three link-timestamps -- against Winbox v3's
Name, Type, Actual MTU, L2 MTU and the eight traffic counters.

The .jg had the answer all along: a field node that carries a ``width`` *is* a
default column, at that pixel width, in declaration order. What is pinned here
is that rule and the three shapes that do not fit a plain field -- a
``columnalias``, a non-separate ``tuple``, and ``type:objtype`` -- plus
``ordered:1``, the .jg's own word on which lists show a rule number.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import (
    Schema,
    default_column_names,
    default_column_widths,
    default_columns,
    is_ordered,
    is_ordered_path,
    list_columns,
    preferred_columns,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

schema = Schema.load(SCHEMA_DIR)
IFACE = (20, 0)


def menu_at(path, kind="map"):
    return next(x for x in schema.menus if x.path == path and x.kind == kind)


# --- the width rule ---------------------------------------------------------

iface = menu_at(IFACE)
check("Interface opens on Winbox's own columns",
      default_column_names(iface),
      ["Name", "Type", "Actual MTU", "L2 MTU", "Tx", "Rx",
       "Tx Packet", "Rx Packet", "FP Tx", "FP Rx",
       "FP Tx Packet", "FP Rx Packet"])
check("...at the widths it declares",
      [default_column_widths(iface)[n] for n in ("Name", "Type", "Actual MTU")],
      [100, 120, 70])

every = [c.name for c in list_columns(iface)]
ok("a widthless field is still offered, just not shown",
   all(n in every for n in ("VRF", "Link Downs", "Tx Bytes", "Rx Drops")))
ok("...and it is behind the default set, not in it",
   every.index("VRF") > every.index("FP Rx Packet"))
ok("the traffic counters come from the Traffic tab, not just General",
   "Tx" in default_column_names(iface))

# A nonpublic field is not drawn at all, width or no width: the generic
# interface declares MTU with one, and only a type overlay shows it.
ok("nonpublic stays out", "MTU" not in default_column_names(iface))

files = menu_at((72, 5))
check("Files lists what Winbox lists", default_column_names(files),
      ["File Name", "Type", "Size", "Last Modified", "File Share URL"])
ok("width:0xffffffff means never a column",
   not {"Contents", "Directory"} & set(default_column_names(files)))

log = menu_at((3, 4))
check("Log keeps its four", default_column_names(log),
      ["Time", "Buffer", "Topics", "Message"])
check("...and the message column is the wide one",
      default_column_widths(log)["Message"], 350)


# --- columnalias: a column the form does not draw ---------------------------

rules = menu_at((20, 3))
names = default_column_names(rules)
check("the firewall's Action column leads, aliased at 50px",
      (names[0], default_column_widths(rules)["Action"]), ("Action", 50))
check("...and it appears exactly once", names.count("Action"), 1)
ok("the counters are default columns too",
   {"Bytes", "Packets"} <= set(names))
ok("an opt-wrapped value carries the wrapper's width",
   default_column_widths(rules)["Src. Address"] == 70)


# --- a non-separate tuple is one column -------------------------------------

ports = menu_at((16, 2))
bpdu = next(c for c in default_columns(ports) if c.name == "Tx/Rx BPDU's")
check("a tuple with no `separate` is a single joined column",
      (bpdu.kind, len(bpdu.codes), bpdu.sep, bpdu.width),
      ("tuple", 2, "/", 100))
tx = next(c for c in default_columns(iface) if c.name == "Tx")
check("a `separate` tuple is one column per member",
      (tx.kind, tx.width), ("field", 120))


# --- type overlays own the final width --------------------------------------

check("IP ▸ Routes takes its columns from the IPv4 overlay",
      default_column_names(schema.record_for((44, 21), 2)),
      ["Dst. Address", "Gateway", "Distance", "Routing Table", "Pref. Source"])
ok("...and the nonpublic base on its own declares none",
   not default_columns(menu_at((44, 21))))

eth = default_column_names(schema.record_for(IFACE, 1))
ok("an Ethernet list adds the overlay's own columns",
   {"PoE Out", "PoE Voltage"} <= set(eth))
check("...and MTU, now public, lands where the base declared it",
      eth[:5], ["Name", "Type", "MTU", "Actual MTU", "L2 MTU"])


# --- ordered:1 --------------------------------------------------------------

ok("the .jg says which lists are ordered", menu_at((20, 3)).ordered)
ok("...including the 57 that were never hardcoded",
   all(menu_at(p).ordered for p in ((20, 9), (14, 3), (16, 2), (85, 1))))
ok("simple queues are ordered even though the path list never said so",
   is_ordered(menu_at((20, 9))) and not is_ordered_path((20, 9)))
ok("addresses are not", not is_ordered(menu_at((20, 1))))
ok("the hardcoded paths still answer without a menu", is_ordered_path((20, 3)))


# --- no widths at all: the old heuristic still answers ----------------------

bare = sch.Menu(path=(99, 99), name="Bare", title="Bare", kind="map", fields={
    0x01: sch.Field(0x01, "s", "Comment", "string"),
    0x02: sch.Field(0x02, "s", "Name", "string"),
})
check("a menu that declares no column falls back to the name heuristic",
      [f.name for f in preferred_columns(bare)], ["Name", "Comment"])
ok("...and default_columns admits it has nothing", not default_columns(bare))


# --- the rows that come out of it -------------------------------------------

TYPE_CODE = sch.type_field_code(schema.record(IFACE))
NAME_CODE = next(c for c, f in schema.record(IFACE).fields.items()
                 if f.name == "Name")
HWTYPE = next(c for c, f in schema.overlay_by_generic["wlan"].fields.items()
              if f.name == "hwtype")


def iface_row(name: str, type_value: int, extra: dict | None = None) -> RowObject:
    return RowObject(
        path=IFACE, raw=m.Message(), fields=[(NAME_CODE, "Name", name, True)],
        form_fields=[], values_by_code={TYPE_CODE: type_value, **(extra or {})},
    )


class StubClient(DeviceClient):
    """A client that answers GETALL from a fixture and nothing else."""

    def __init__(self, rows, path):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.schema = schema
        self.session = None
        self._rows = rows
        self._path = path

    def getall(self, path, **kw):
        return list(self._rows)

    def uptime(self):
        return 0.0

    def enum_map(self, path, **kw):
        return {}


client = StubClient([iface_row("ether1", 1), iface_row("bridge1", 12),
                     iface_row("wlan1", 35, {HWTYPE: 12})], IFACE)
_menu, cols, rows, _raw, _up = client.table_rows(IFACE)
check("the flags column still leads", cols[0], "")
check("the header is the .jg's set", cols[1:13],
      ["Name", "Type", "Actual MTU", "L2 MTU", "Tx", "Rx", "Tx Packet",
       "Rx Packet", "FP Tx", "FP Rx", "FP Tx Packet", "FP Rx Packet"])
# Winbox names the deepest overlay that applies, which for a radio is the
# chip -- the same string the property form's Type row shows.
check("a row's type is spelled out per row, not per list",
      [r[2] for r in rows], ["Ethernet", "Bridge", "Wireless (IPQ4019)"])


BPDU_TX, BPDU_RX = 0xE0, 0xE1
port = RowObject(path=(16, 2), raw=m.Message(), fields=[], form_fields=[],
                 values_by_code={BPDU_TX: 12, BPDU_RX: 9})
_menu, cols, rows, _raw, _up = StubClient([port], (16, 2)).table_rows((16, 2))
i = cols.index("Tx/Rx BPDU's")
check("a joined tuple renders both members", rows[0][i], "12/9")
check("...and the list carries its rule number", cols[1], sch.INDEX_LABEL)


# --- and what the pane makes of them ----------------------------------------

from PySide6.QtWidgets import QApplication

from x3.ui.widgets.list_pane import ListPane

app = QApplication.instance() or QApplication(sys.argv)


def pane_for(path, rows):
    payload = StubClient(rows, path).table_rows(path)
    pane = ListPane("test", path=path)
    pane._on_loaded(payload)
    return pane


pane = pane_for(IFACE, [iface_row("ether1", 1)])
shown = [
    c for i, c in enumerate(pane._columns)
    if c and c != "Comment" and not pane.table.isColumnHidden(i)
]
check("the pane opens on the declared set and hides the rest", shown,
      ["Name", "Type", "Actual MTU", "L2 MTU", "Tx", "Rx", "Tx Packet",
       "Rx Packet", "FP Tx", "FP Rx", "FP Tx Packet", "FP Rx Packet"])
ok("...with the rest one click away in the chooser",
   "VRF" in pane._base_columns and "VRF" in pane._hidden_cols)

hdr = pane.table.horizontalHeader()
ok("a column opens at the width the .jg asks for",
   hdr.sectionSize(pane._columns.index("Name")) >= 100)

log_row = RowObject(path=(3, 4), raw=m.Message(), fields=[], form_fields=[],
                    values_by_code={})
log_pane = pane_for((3, 4), [log_row])
hdr = log_pane.table.horizontalHeader()
ok("...even past the 200px cap that measuring imposes",
   hdr.sectionSize(log_pane._columns.index("Message")) >= 350)

# A declared width is where a column starts, not a ceiling: DHCP Client's
# IP Address is 80px, and a routable address with its prefix clipped there.
long_name = "sfp-sfpplus1-uplink-to-core"
wide_pane = pane_for(IFACE, [iface_row(long_name, 1)])
hdr = wide_pane.table.horizontalHeader()
fm = wide_pane.table.fontMetrics()
ok("a column grows past its declared width to fit what is in it",
   hdr.sectionSize(wide_pane._columns.index("Name"))
   >= fm.horizontalAdvance(long_name))
ok("...while one whose text fits keeps the declared width",
   pane.table.horizontalHeader().sectionSize(pane._columns.index("Name")) == 100)

for p in (pane, log_pane, wide_pane):
    p.shutdown()


print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all default-column tests passed")
