"""The .jg's value machinery: filtered enums, slot-sourced enums, bitmask sets,
shifted numbers, radio buttons and signal bars.

No device and no pytest required:

    .venv/bin/python test_jg_values.py

Everything here was in the schema and ignored by the UI, and each one showed
the user something that was not merely plain but *wrong*:

* **``{type:'slot'}`` predicates** (626 of them) had no branch in the
  evaluator, so they answered True. They are how the .jg asks about a
  *different* field than the one being decided -- an enum entry against the
  radio's band bits, an MCS index against ``hwtype``.

* **``enumfilter``** (37) returned no map at all, so wireless Band and Channel
  Width -- the two most-used settings on a wireless interface -- had no
  dropdown whatsoever. With the filters read, the vocabulary still renders
  every value the device may send, while the dropdown offers only the bands
  this radio has.

* **``slotenum``** (Tx/Rx Chains) has no vocabulary in the schema by design:
  the card reports its chains in two parallel arrays on the object itself.

* **A ``set`` is a bitmask**, member at enum key k = bit 1<<k. Rendering it as
  a scalar enum named the wrong member: PPP Authentication 0x1 read back as
  the *second* entry in the map.

* **``offset``** means the wire number is not the value. LTE RSSI arrives as
  25 and means -70 dBm.

* **``radio:1``** and **``numbar``** are how Winbox draws two of these: radio
  buttons for a small enum, a coloured bar for a signal reading.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.schema import Schema
from x3.ui.widgets.property_window import _share_set_pitch
from x3.ui.widgets.radio_group import RadioGroupEditor
from x3.ui.widgets.tristate_editor import SetEditor
from x3.ui.widgets.signal_bar import SignalBar

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
IFACE = (20, 0)
HWTYPE = 0x0007D1
WPHYMODES = 0x000906
BAND = 0x00081A
CHANNEL_WIDTH = 0x00081B
TX_CHAINS = 0x0008D4
TX_CHAIN_NAMES = 0x00090B
TX_CHAIN_VALUES = 0x00090A

wireless = schema.record_for(IFACE, 35, {HWTYPE: 12})
band = wireless.fields[BAND]
width = wireless.fields[CHANNEL_WIDTH]
chains = wireless.fields[TX_CHAINS]

# The radio's wphymodes bits, straight out of the Band filters themselves:
# 0x2 = 5GHz A, 0x8 = 5GHz N, 0x20 = 5GHz AC,
# 0x20000 = 2GHz B, 0x40000 = 2GHz G, 0x100000 = 2GHz N.
PHY_2G = 0x20000 | 0x40000 | 0x100000
PHY_5G = 0x2 | 0x8 | 0x20


# --- enum vocabularies ------------------------------------------------------

check("an enumfilter still yields the whole vocabulary", len(band.enum or {}), 12)
check("...with one filter per entry", len(band.enum_filters or ()), 12)


def bands(phy):
    ids = sch.enum_allowed_ids(band, wireless, {WPHYMODES: phy})
    return sorted(band.enum[i] for i in ids)


check("a 2.4GHz radio is offered 2.4GHz bands only", bands(PHY_2G),
      ["2GHz-B", "2GHz-B/G", "2GHz-B/G/N", "2GHz-G/N", "2GHz-only-G",
       "2GHz-only-N"])
check("...and a 5GHz radio the 5GHz ones", bands(PHY_5G),
      ["5GHz-A", "5GHz-A/N", "5GHz-A/N/AC", "5GHz-N/AC", "5GHz-only-AC",
       "5GHz-only-N"])
check("a dual-band card is offered both", len(bands(PHY_2G | PHY_5G)), 12)
ok("the filters really are slot predicates",
   all(
       p is None or p.get("type") == "slot"
       for _fid, p in (band.enum_filters or ())
   ))
ok("a field with no enumfilter is not narrowed",
   sch.enum_allowed_ids(wireless.fields[0x0007D2], wireless, {}) is None)
check("Channel Width narrows too, on the same slot",
      len(sch.enum_allowed_ids(width, wireless, {WPHYMODES: PHY_2G}) or ()) <
      len(width.enum or {}), True)

# A slot predicate on its own: what the filters are made of.
slot_pred = {"type": "slot", "slot": "wphymodes",
             "pred": {"type": "bitmap", "mask": 0x20000, "value": 0x20000}}
check("a slot pred reads the field it names, not the one being decided",
      [sch._eval_pred(slot_pred, None, wireless, {WPHYMODES: phy})
       for phy in (PHY_2G, PHY_5G)], [True, False])

# slotenum: the card's own chain list.
check("a slotenum's entries come off the object",
      sch.enum_from_slots(chains, wireless,
                          {TX_CHAIN_NAMES: ["chain0", "chain1"],
                           TX_CHAIN_VALUES: [0, 1]}),
      {0: "chain0", 1: "chain1"})
check("...four chains give four entries",
      len(sch.enum_from_slots(
          chains, wireless,
          {TX_CHAIN_NAMES: ["c0", "c1", "c2", "c3"],
           TX_CHAIN_VALUES: [0, 1, 2, 3]}) or {}), 4)
ok("...and a card that reported nothing gets no invented entries",
   sch.enum_from_slots(chains, wireless, {}) is None)


# --- rendering --------------------------------------------------------------

msg = m.Message()
msg.add_u32(0x010001, 35)
msg.add_u32(HWTYPE, 12)
msg.add_u32(TX_CHAINS, 3)
msg.add_string_array(TX_CHAIN_NAMES, ["chain0", "chain1"])
msg.add_u32_array(TX_CHAIN_VALUES, [0, 1])
named = {name: value for _c, name, value, _ok in schema.decode(IFACE, msg)}
check("a set renders as its members, from the row's own labels",
      named.get("Tx Chains"), "chain0,chain1")

auth = next(
    f for menu in schema.menus for f in menu.fields.values()
    if f.jg_type == "set" and (f.enum or {}) and f.name == "Authentication"
    and len(f.enum) == 4
)
lowest = min(auth.enum)
check("bit 1<<k names member k, not enum[value]",
      auth.render(1 << lowest), auth.enum[lowest])
check("...and several bits list several members",
      auth.render((1 << lowest) | (1 << (lowest + 1))),
      f"{auth.enum[lowest]},{auth.enum[lowest + 1]}")
check("an empty set is empty, not 0", auth.render(0), "")

rssi = next(
    f for menu in schema.menus for f in menu.fields.values()
    if f.jg_type == "numbar" and f.name == "RSSI"
)
check("offset:95 means 25 on the wire is -70 dBm", rssi.render(25), "-70dBm")
check("...and 40 is the top of the bar", rssi.render(40), "-55dBm")
check("the unset sentinel reads as nothing at all",
      rssi.render(0x80000000), "")
sinr = next(
    f for menu in schema.menus for f in menu.fields.values()
    if f.jg_type == "numbar" and f.name == "SINR"
)
check("a numbar with no offset is its own value", sinr.render(14), "14dB")


# --- the widgets ------------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)

rate = next(
    f for menu in schema.overlay_list for f in menu.fields.values()
    if f.radio and f.name == "Rate"
)
group = RadioGroupEditor([(0, "default"), (1, "configured")], 1)
check("a radio group reports the wire value of what is checked",
      group.editor_value(), 1)
group.set_value(0)
check("...and follows set_value", group.editor_value(), 0)
check("...and knows it changed", group.is_dirty(), True)
group.set_locked(True)
ok("a locked group takes no clicks",
   not any(b.isEnabled() for _w, b in group._buttons))
ok("Rate is one of the fields the .jg marks radio", rate.radio)
check("...and declares no compact, so the form gives it a section title",
      (rate.compact, RadioGroupEditor([(0, "a")], 0, title="Rate").title),
      (False, "Rate"))

# `columns:` on a set -- how many checkboxes Winbox puts on a row, and one
# pitch for the rows it draws as a single grid. The rate sets of a CAPs Rate
# want ~73px per column and the MCS sets ~38px, and Winbox draws them as two
# grids (each `group` is its own container), so they must not be levelled
# onto one pitch the way a Data Rates tab's four rate rows are.
caps_rate = schema.record((88, 164))
sets = {f.name: f for f in caps_rate.fields.values() if f.jg_type == "set"}
check("a set carries its .jg column count",
      {n: f.set_columns for n, f in sorted(sets.items())},
      {"Basic Rates": 6, "HT Basic MCS": 6, "HT Supported MCS": 6,
       "Supported Rates": 6})
grids = {n: SetEditor(f, {}) for n, f in sets.items()}
check("a twelve-member set wraps into two rows of six",
      grids["Basic Rates"].layout().rowCount(), 2)
check("...and a twenty-four member one into four",
      grids["HT Basic MCS"].layout().rowCount(), 4)
_share_set_pitch(list(grids.values()))
pitch = {n: g.layout().columnMinimumWidth(0) for n, g in grids.items()}
check("rate grids share a pitch with each other",
      pitch["Basic Rates"], pitch["Supported Rates"])
check("...MCS grids with each other",
      pitch["HT Basic MCS"], pitch["HT Supported MCS"])
ok("...and the two kinds are left alone", pitch["Basic Rates"] > pitch["HT Basic MCS"],
   "Winbox grids each container separately; 1Mbps is a wider column than 0")

empty = RadioGroupEditor([(0, ""), (1, ""), (2, "802.11"), (3, "nstreme")], 2)
check("blank map entries are not drawn as buttons", len(empty._buttons), 2)
check("...and the value still selects the right one", empty.editor_value(), 2)

bar = SignalBar(rssi, 25)
check("a signal bar shows the shifted value and its band",
      bar.text(), "-70dBm (good)")
bar.set_reading(5)
check("...a weak reading is 'poor'", bar.text(), "-90dBm (poor)")
bar.set_reading(40)
check("...a strong one is 'excellent'", bar.text(), "-55dBm (excellent)")
bar.set_reading(0x80000000)
check("...and no reading at all is a dash", bar.text(), "—")


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall .jg value tests passed")
