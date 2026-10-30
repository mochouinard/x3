"""Range lists -- ``multinumberrange`` / ``numberrangelist``.

No device and no pytest required:

    .venv/bin/python test_range_lists.py

A range list is **one flat number array holding two elements per range**, low
then high, and a single value is a range whose ends are equal. Captured off
firmware 7.23.3 (lab VM), Bridge ▸ VLANs `[16,13]` field ``U1``:

    vlan-ids=100        ->  [100, 100]
    vlan-ids=10-20      ->  [10, 20]
    vlan-ids=5,7-9,300  ->  [5, 5, 7, 9, 300, 300]

Rendering that array element by element is what put ``100, 100`` in the VLAN
IDs column where Winbox v3 -- and the router's own ``print terse`` -- say ``100``.

Two shapes share the encoding and the .jg says which editor each gets:

* ``multinumberrange`` (VLAN IDs) is the growable one -- Winbox shows
  ``vlan-ids=100-101,105`` as two boxes, ``100-101`` and ``105``.
* ``numberrangelist`` (QoS PCP/DSCP, PCI address windows) is one box holding
  the whole list.

A ``multinumberrange`` that declares ``id2`` is a **third** shape: two parallel
arrays, one element per range in each, and pairing one of them up on its own
would invent ranges. Also captured off 7.23.3 -- a Traffic Generator packet
template with ``udp-dst-port=80-90,443,1000``::

    Ud6 (id)  = [80, 443, 1000]      the lows
    Ue1 (id2) = [90, 443, 1000]      the highs

The write side is checked too: the box's text has to go out as the pairs, not
as the string it was typed as -- a `U` field id holding a string is a write the
device discards.
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
from x3.ui.widgets.property_window import _is_multi_enum
from x3.ui.widgets.scalar_array import ScalarArrayEditor, is_scalar_multi

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

app = QApplication.instance() or QApplication([])
schema = Schema.load(SCHEMA_DIR)
bridge_vlan = next(mn for mn in schema.menus if mn.path == (16, 13))
vlan_ids = bridge_vlan.fields[0x01]        # U1  multinumberrange
tagged = bridge_vlan.fields[0x03]          # U3  multinumber of enm
bridge = bridge_vlan.fields[0x02]          # u2  scalar enm


# --- the shape ---------------------------------------------------------------

check("VLAN IDs is the growable range list",
      (vlan_ids.name, vlan_ids.jg_type, vlan_ids.element_type, vlan_ids.prefix),
      ("VLAN IDs", "multinumberrange", "numberrange", "U"))
check("a flat array is read as inclusive (low, high) pairs",
      sch.range_list_pairs([5, 5, 7, 9, 300, 300]),
      [(5, 5), (7, 9), (300, 300)])
ok("an array that is not pairs is not one",
   sch.range_list_pairs([5, 5, 7]) is None)
ok("...nor is one holding something that is not a number",
   sch.range_list_pairs([5, "ether1"]) is None)


# --- rendering ---------------------------------------------------------------

check("a single VLAN is one number, not its two ends",
      vlan_ids.render([100, 100]), "100")
check("a real range keeps both", vlan_ids.render([10, 20]), "10-20")
check("...and a list of them reads as the router writes it",
      vlan_ids.render([5, 5, 7, 9, 300, 300]), "5,7-9,300")
check("two ranges", vlan_ids.render([100, 101, 105, 105]), "100-101,105")
check("an unset field is blank", vlan_ids.render([]), "")
check("an array that is not pairs shows the numbers it is",
      vlan_ids.render([5, 5, 7]), "5, 5, 7")
pcp = next(f for f in schema.record((20, 116)).fields.values()
           if f.jg_type == "numberrangelist")
check("a single-box range list renders the same way",
      pcp.render([0, 3, 5, 5]), "0-3,5")
memory = next(f for f in schema.record((6, 2)).fields.values()
              if f.name == "Memory")
check("...and a radix:16 one is read in hex (PCI address windows)",
      memory.render([0xFE9C0000, 0xFE9DFFFF]), "0xfe9c0000-0xfe9dffff")


# --- the other shape: `id2`, two parallel arrays -----------------------------

dst_port = schema.record((119, 9)).fields[0x00D6]      # Ud6, id2 = Ue1
check("a range list may carry its highs in a companion array",
      (dst_port.name, dst_port.jg_type, dst_port.highid),
      ("Dst. Port", "multinumberrange", 0x00E1))
check("the two are read side by side",
      sch.format_split_range_list([80, 443, 1000], [90, 443, 1000]),
      "80-90,443,1000")
ok("...and not at all when they do not line up",
   sch.format_split_range_list([80, 443], [90]) is None)
check("a row renders it from both codes",
      sch._render(dst_port, [80, 443, 1000],
                 others={0x00D6: [80, 443, 1000], 0x00E1: [90, 443, 1000]}),
      "80-90,443,1000")
check("the array on its own is the lows, never paired up",
      dst_port.render([80, 443, 1000, 2000]), "80, 443, 1000, 2000")
ok("...and it keeps the plain box, which is the only one that can write both",
   not is_scalar_multi(dst_port))


# --- parsing back ------------------------------------------------------------

check("a bare number is a range with equal ends", sch.parse_range("100"),
      (100, 100))
check("N-M is the pair", sch.parse_range(" 10 - 20 "), (10, 20))
check("a list of both flattens", sch.parse_range_list("5,7-9,300"),
      [5, 5, 7, 9, 300, 300])
check("blank writes nothing", sch.parse_range_list("  "), [])
for bad in ("9-2", "ether1", "10-"):
    try:
        sch.parse_range(bad)
        ok(f"{bad!r} is refused", False, "parsed without complaint")
    except ValueError:
        print(f"ok  {bad!r} is refused")


# --- the write path ----------------------------------------------------------

check("the box's text goes out as the pairs, under the array tag",
      sch.encode_form_value(vlan_ids, "200-210,400"),
      (m.T_U32_ARRAY, [200, 210, 400, 400]))
check("...and an array field handed its list encodes element by element",
      sch.encode_form_value(tagged, [1, 2]), (m.T_U32_ARRAY, [1, 2]))
address_list = next(
    f for f in schema.record((23, 0)).fields.values()
    if f.name == "Address List"
)
check("a multistring's list goes out as a string array",
      sch.encode_form_value(address_list, ["lan", "guests"]),
      (m.T_STRING_ARRAY, ["lan", "guests"]))


# --- the editor --------------------------------------------------------------

ok("VLAN IDs is edited as an array", is_scalar_multi(vlan_ids))
ok("...and not as a dropdown -- it has no vocabulary",
   not _is_multi_enum(vlan_ids))

ed = ScalarArrayEditor(vlan_ids, [100, 101, 105, 105])
check("one box per range, the way Winbox lists them",
      ed.texts(), ["100-101", "105"])
check("...and back to the wire unchanged",
      ed.wire_value(), (m.T_U32_ARRAY, [100, 101, 105, 105]))
check("the summary reads as one value", ed.label(), "100-101,105")
ed._rows[0][1].setText("200-210")
ed._add_row("7")
check("an edited box and an added one are still pairs", ed.wire_value(),
      (m.T_U32_ARRAY, [200, 210, 105, 105, 7, 7]))
ed._add_row("9-2")
ok("a backwards range blocks the save", ed.error() is not None, "no error")


# --- the neighbouring fields -------------------------------------------------
# Tagged / Untagged / MVRP Forbidden are multis whose vocabulary comes from
# another menu (the interface table). Requiring an inline vocabulary gave them
# a single-select dropdown -- one interface, where Winbox lists several.

ok("a multi with a vocabulary from another menu is a multi-enum",
   _is_multi_enum(tagged), f"enum={tagged.enum} enum_path={tagged.enum_path}")
ok("...while a scalar enum on the same menu stays scalar",
   not _is_multi_enum(bridge), f"Bridge jg={bridge.jg_type}")

# --- inside an optional match ------------------------------------------------
# Firewall Dst. Port is opt -> not -> numberrangelist (U63). Its form box showed
# the raw "[13239, 13239]", and what was typed went out as a string -- a write
# Firmware 7.23.3 takes without error and without effect. As the typed array,
# the same virtual router exports dst-port=13239-13240,400.

from PySide6.QtWidgets import QLineEdit  # noqa: E402

from x3.ui.widgets.opt_match import OptMatchEditor  # noqa: E402

rules = next(mn for mn in schema.menus if mn.path == (20, 3) and mn.kind == "map")
DST_PORT = next(c for c, f in rules.fields.items() if f.name == "Dst. Port")
inner = sch.opt_chain(rules.fields[DST_PORT])[1]
port = OptMatchEditor(rules.fields[DST_PORT],
                      {DST_PORT: True, inner.code: [13239, 13239]})
box = port.findChildren(QLineEdit)[0]
check("a matched port reads as Winbox has it", box.text(), "13239")
check("...and is not an edit until it is changed", port.is_dirty(), False)
box.setText("13239-13240, 400")
check("a typed list goes out as the (low, high) array",
      port.wire_values().get(inner.code), (m.T_U32_ARRAY, [13239, 13240, 400, 400]))
check("...and is fine", port.error(), None)
box.setText("80,70000")
ok("a port past 65535 is refused", port.error() is not None, port.error())
box.setText("http")
ok("...and so is a word", port.error() is not None and inner.code not in port.wire_values(),
   str(port.wire_values()))

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all good")
