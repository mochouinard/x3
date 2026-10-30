"""``changerate`` columns, and the ``bitrate`` that was never wired.

No device and no pytest required:

    .venv/bin/python test_rate_columns.py

Two gaps from the same corner of the .jg, found by re-tallying every node type
against what the code actually reads.

**``type:bitrate``** (19 uses). ``bigbitrate`` -- the u64 an interface's Rx
Rate arrives in -- has read as "1.50 Mbps" forever. Its u32 sibling was never
added to the same branch, so Tools ▸ Ping Speed and a Simple Queue's Avg. Rate
showed the raw bits per second.

**The change-rate family** (28 uses, three spellings). These declare no ``id``
at all: ``{name:'Packet Rate',type:'bigchangerate',postfix:'p/s',value:'Packets'}``
names *another* field, and the column is how fast that one is moving. Nothing
downstream ever saw a field, so Firewall ▸ Filter/NAT/Mangle's **Rate** and
**Packet Rate** columns and Hotspot ▸ Active's **Rx/Tx Rate** did not exist --
`default_columns` said as much in its own docstring, and left them out on the
grounds that an empty column is worse than one the header menu never lists.

What a rate needs that a reply does not carry is the *previous* reply, so the
split is: the schema declares the column and which counter it watches, and the
list pane keeps one sample per row and fills the cell. Which is also why the
first sighting of a row is blank -- there is no interval yet.

``width`` decides visibility here as it does everywhere else: Hotspot ▸ Active
declares 50 for its rates and Winbox shows them; a firewall rule's Packet Rate
declares none and waits in the header menu.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto.schema import Schema, list_columns, default_column_names
from x3.ui.widgets.list_pane import ListPane, _rate_text

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


def menu(group, title, kind="map"):
    return next(m for m in schema.menus
                if m.group == group and m.title == title and m.kind == kind)


# --- type:bitrate ------------------------------------------------------------

rate_fields = [(m, f) for m in schema.menus for f in m.fields.values()
               if f.jg_type == "bitrate"]
ok("the .jg has bitrate fields", len(rate_fields) > 10, f"{len(rate_fields)}")
_m, fld = rate_fields[0]
check("a u32 bitrate reads like the u64 one", fld.render(1_500_000), "1.50 Mbps")
check("...and stays in bps below a kilobit", fld.render(999), "999 bps")
big = next(f for m in schema.menus for f in m.fields.values()
           if f.jg_type == "bigbitrate")
check("...identically to bigbitrate, which is the point",
      fld.render(1_500_000), big.render(1_500_000))


# --- the columns the change-rate family declares -----------------------------

active = menu("IP", "Active")            # Hotspot ▸ Active
filt = menu("IP", "Filter Rules")

cols = {c.name: c for c in list_columns(active) if c.kind == "rate"}
check("Hotspot ▸ Active declares the four Winbox shows",
      sorted(cols), ["Rx Packets", "Rx Rate", "Tx Packets", "Tx Rate"])
ok("...each watching a counter that exists",
   all(c.rate_code in active.fields for c in cols.values()),
   f"{[(n, c.rate_code) for n, c in cols.items()]}")
check("...a byte counter shown as a bit rate", cols["Rx Rate"].rate_bits, True)
check("...a packet counter keeping the .jg's own unit",
      (cols["Rx Packets"].rate_bits, cols["Rx Packets"].postfix), (False, "p/s"))

ok("...and `width` puts the rates on screen from the start",
   {"Rx Rate", "Tx Rate"} <= set(default_column_names(active)),
   f"{default_column_names(active)}")
ok("...while the packet ones wait in the header menu",
   not {"Rx Packets", "Tx Packets"} & set(default_column_names(active)))

fcols = {c.name: c for c in list_columns(filt) if c.kind == "rate"}
check("a firewall rule declares Rate and Packet Rate",
      sorted(fcols), ["Packet Rate", "Rate"])
ok("...neither of them shown until asked for, as neither declares a width",
   not {"Rate", "Packet Rate"} & set(default_column_names(filt)))
check("...Rate off the byte counter", fcols["Rate"].rate_bits, True)
check("...Packet Rate off the packet one",
      fcols["Packet Rate"].rate_code,
      next(c for c, f in filt.fields.items() if f.name == "Packets"))

# A menu with no such node grows no such column.
ok("a list that declares none has none",
   not [c for c in list_columns(menu("IP", "Address")) if c.kind == "rate"])


# --- the arithmetic ----------------------------------------------------------

NOW = 1000.0
rx, pk = cols["Rx Rate"], cols["Rx Packets"]
check("a byte counter moving 1.25 MB in a second is 10 Mbps",
      _rate_text(rx, (0, NOW - 1), 1_250_000, NOW), "10.00 Mbps")
check("60 packets over two seconds is 30 p/s",
      _rate_text(pk, (100, NOW - 2), 160, NOW), "30p/s")
check("a counter that went backwards reports nothing, not a negative rate",
      _rate_text(pk, (500, NOW - 1), 100, NOW), "")
check("...nor does a zero interval divide by it",
      _rate_text(pk, (100, NOW), 200, NOW), "")
check("a counter standing still is 0, which is a reading",
      _rate_text(pk, (100, NOW - 1), 100, NOW), "0p/s")


# --- and on the pane ---------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)


class FakeRow:
    """Just enough RowObject for the rate path: an id and raw counters."""

    def __init__(self, oid, values):
        self._oid = oid
        self._values = values

    def object_id(self):
        return self._oid

    def raw_value(self, code, **kw):
        return self._values.get(code)


class Clock:
    """time, with monotonic pinned. A rate is a division by an interval, so a
    test of one has to own the interval -- otherwise the answer moves with how
    long the run took."""

    now = 0.0

    def __getattr__(self, name):
        return getattr(real_time, name)

    def monotonic(self):
        return self.now


import time as real_time  # noqa: E402
from x3.ui.widgets import list_pane as lp  # noqa: E402

clock = Clock()
lp.time = clock

RX = cols["Rx Rate"].rate_code
PKT = cols["Rx Packets"].rate_code
# The row shape table_rows produces: [flags] + one cell per column of
# list_columns(), Active being unordered. Built from the schema rather than
# written out, because the cell a rate lands in is exactly what the pane has
# to get right.
ALL = list_columns(active)
NAMES = [""] + [c.name for c in ALL]
BLANK = [""] * len(NAMES)
RX_CELL = 1 + [c.name for c in ALL].index("Rx Rate")
PKT_CELL = 1 + [c.name for c in ALL].index("Rx Packets")

pane = ListPane("Active", path=active.path, readonly=False)


def read(rx_bytes, packets, at):
    clock.now = at
    row = FakeRow(7, {RX: rx_bytes, PKT: packets})
    pane._on_loaded((active, NAMES, [list(BLANK)], [row], None))
    return pane._all_rows[0]


cells = read(0, 0, 1000.0)
check("the first sighting of a row shows no rate", cells[RX_CELL], "")
ok("...and the counters are remembered for the next read", pane._rate_seen)

cells = read(1_250_000, 60, 1001.0)
check("a second read one second later is a rate", cells[RX_CELL], "10.00 Mbps")
check("...in its own cell, and the packet rate in its own",
      cells[PKT_CELL], "60p/s")

# Rates are what the 1 Hz push feed is for, so the patched path computes one
# too rather than blanking the cell it cannot re-derive.
cells = read(2_500_000, 120, 1002.0)
check("and again on the read after that", cells[RX_CELL], "10.00 Mbps")

# A row that leaves the list takes its sample with it.
clock.now = 1003.0
pane._on_loaded((active, NAMES, [], [], None))
check("a row that is gone leaves no sample behind", len(pane._rate_seen), 0)
pane.shutdown()
lp.time = real_time


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall good")
