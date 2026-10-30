"""``inv``, ``optval``, ``private``, ``inline``, ``nonaddable``, ``fulltitle``.

No device and no pytest required:

    .venv/bin/python test_list_flags.py

A second sweep of the .jg's attribute keys against the source turned up these,
and three of them were showing the user something untrue:

* **``inv``** (29 fields) means the UI shows the *opposite* of the wire. A
  wireless interface's "Multicast Buffering" is the negation of the bit the
  device keeps, and a bridge host's flag letter appears when its bit is clear.
  Read straight, every one of them was backwards -- and a checkbox read
  backwards is also a checkbox *written* backwards.

* **``optval``** (12) is the value a field uses to say "not set": a hotspot
  profile's Shared Users of 0 is unlimited, not zero users. Shown as 0 it read
  as a real setting, and clearing the field left it out of the message instead
  of sending the 0 that means unset.

* **``private``** (35) is a secret the device never sends back (Quick Set's WiFi
  Password). Whatever arrives is not the password, so showing it invites
  sending a stand-in back as if it were one.

* **``inline``** (120 tabs) is not a tab: it heads a *section* of the page it is
  on. All 120 are Quick Set modes, which Winbox draws as one long form with
  named rules -- where this showed up to eighteen tabs.

* **``nonaddable``** (61 maps) / **``nonmovable``** (4): the lists that are not
  the user's to build or to order. ``removable`` (42) is recorded and
  deliberately not acted on -- 380 maps that plainly do allow Remove declare
  nothing, so the flag cannot be the gate for it, and guessing would take an
  action away.

* **``fulltitle``** (24) is the window's title where it differs from the menu's:
  the tree says "Static", the window says "DNS Static".
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.schema import Schema
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.property_window import OptionalFieldEditor, _optional_is_set

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
wireless = schema.record_for((20, 0), 35, {0x0007D1: 12})


# --- inv --------------------------------------------------------------------

buffering = next(
    f for f in wireless.fields.values() if f.name == "Multicast Buffering"
)
ok("the .jg marks Multicast Buffering as inverted", buffering.invert)
check("a set bit reads as off", buffering.render(True), "false")
check("...and a clear bit as on", buffering.render(False), "true")
check("writing 'on' sends the bit clear",
      sch.encode_form_value(buffering, "true"), (m.T_BOOL_FALSE, False))
check("...and 'off' sends it set",
      sch.encode_form_value(buffering, "false"), (m.T_BOOL_TRUE, True))
plain = next(f for f in wireless.fields.values() if f.name == "Hide SSID")
ok("an ordinary bool is untouched", not plain.invert)
check("...and reads as it is", [plain.render(True), plain.render(False)],
      ["true", "false"])

# The flags column: a letter shown when the bit is *clear*.
conns = next(
    mn for mn in schema.menus
    if any(f.invert and f.jg_type == "flag" and f.hint
           for f in mn.fields.values())
)
flag = next(
    f for f in conns.fields.values()
    if f.invert and f.jg_type == "flag" and f.hint
)
check("an inverted flag letter shows when its bit is clear",
      sch.format_flag_letters(conns, {flag.code: False}), flag.hint)
check("...and not when it is set",
      sch.format_flag_letters(conns, {flag.code: True}), "")
check("...and not at all when the row does not carry it",
      sch.format_flag_letters(conns, {}), "")


# --- optval -----------------------------------------------------------------

shared = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.name == "Shared Users" and f.unset_value is not None
)
check("Shared Users says 'unset' with a 0", shared.unset_value, 0)
check("...so a 0 shows as nothing at all", shared.render(0), "")
check("...while a real count shows", shared.render(3), "3")
ok("...and 0 reads as not set", not _optional_is_set(shared, 0))
ok("...while 3 reads as set", _optional_is_set(shared, 3))
lifetime = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.name == "RA Lifetime" and f.unset_value is not None
)
check("an RA Lifetime of 0 is none, not zero seconds",
      lifetime.render(0), "")


# --- private ----------------------------------------------------------------

password = next(
    f for mn in schema.menus for f in mn.fields.values() if f.private
)
check("the .jg marks Quick Set's WiFi Password private", password.name,
      "WiFi Password")
check("...so whatever arrives is not shown", password.render(b"place-holder"),
      "")
ok("...and it is still a secret", sch.is_secret(password))


# --- inline tabs ------------------------------------------------------------

for title in ("Home AP", "LTE AP", "CPE"):
    mode = next(mn for mn in schema.menus
                if mn.title == title and mn.kind == "item")
    check(f"Quick Set {title} is one page", mode.tab_names(), ["General"])
    sections = [i["name"] for i in mode.form_layout if i.get("kind") == "label"]
    ok(f"...with {title}'s sections as rules in it", len(sections) >= 3,
       f"{sections}")

ordinary = schema.record((20, 3))
ok("a menu with real tabs still has them", len(ordinary.tab_names()) > 1,
   f"{ordinary.tab_names()}")


# --- list capability flags --------------------------------------------------

check("an interface list is not one you add to",
      schema.record((20, 0)).addable, False)
check("...while a firewall list is", schema.record((20, 3)).addable, True)
check("the four nonmovable lists are known",
      sorted(mn.title for mn in schema.menus if not mn.movable),
      ["BGP VPLS", "Identities", "LDP Neighbor", "Tunnel"])
ok("`removable` is recorded, not acted on",
   any(mn.removable_declared for mn in schema.menus))
ok("...and some declare it while still being addable, which is why it"
   " cannot be the gate for Remove",
   sum(1 for mn in schema.menus if mn.removable_declared and mn.addable) > 0)
ok("...while most lists that plainly allow Remove declare nothing",
   sum(1 for mn in schema.menus
       if mn.kind == "map" and not mn.removable_declared) > 300)

check("a window title differs from its tree label where the .jg says so",
      sorted(
          {(mn.title, mn.full_title) for mn in schema.menus
           if mn.full_title and mn.title in ("Static", "Cache")}
      ),
      [("Cache", "DNS Cache"), ("Cache", "Web Proxy Cache"),
       ("Static", "DNS Static")])
check("...on 24 menus in all",
      sum(1 for mn in schema.menus if mn.full_title), 24)


# --- the widgets ------------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)


class Session:
    readonly = False
    client = None


pane = ListPane("Interface", path=(20, 0), edit_session=Session())
add = next(a for a in pane._write_actions if a.text() == "Add")
ok("a pane starts with Add available", add.isEnabled())
pane._on_loaded((schema.record((20, 0)), ["Name"], [["ether1"]], [None], None))
app.processEvents()
ok("...and loses it on a list the .jg calls nonaddable", not add.isEnabled())
ok("...with the reason on the button", "add rows" in add.toolTip().lower(),
   add.toolTip())
pane.shutdown()

pane2 = ListPane("Filter Rules", path=(20, 3), edit_session=Session())
add2 = next(a for a in pane2._write_actions if a.text() == "Add")
pane2._on_loaded((schema.record((20, 3)), ["Chain"], [["input"]], [None], None))
app.processEvents()
ok("a list you can add to keeps its Add", add2.isEnabled())
pane2.shutdown()


# --- numflag: a number whose value is the letter ----------------------------
# Route declares its A/C/S/d letters as {type:'numflag',id:'u22',c:{4:[...,'A']}}
# on All Routes, and the IPv4 Routes overlay the list is drawn with inherits
# them. Values read off a 7.23.3 virtual router, beside Winbox v3 showing the same rows.

routes = schema.record_for((44, 21), 2)
DYNAMIC = 0xFE0007
check("a DHCP default route reads as Winbox shows it",
      sch.format_flag_letters(routes, {DYNAMIC: True, 0x22: 4, 0x112: 10}), "DAd")
check("...a connected one", sch.format_flag_letters(
      routes, {DYNAMIC: True, 0x22: 4, 0x112: 2}), "DAC")
check("...and a static one, which the overlay alone left blank",
      sch.format_flag_letters(routes, {0x22: 4, 0x112: 3}), "AS")
check("a value the numflag does not name adds nothing",
      sch.format_flag_letters(routes, {0x22: 2, 0x112: 11}), "")

band = sch.Menu(path=(0,), name="t", title="t", kind="map", numflags=[
    {"code": 9, "letters": {0: "D", 1: "H", 4: "S"}, "band": True}])
check("with band:1 the keys are bits, not values",
      sch.format_flag_letters(band, {9: 0b10011}), "DHS")

# --- an address joined to its mask, as Winbox lists it ---------------------

from x3.proto.device import DeviceClient, RowObject  # noqa: E402

addr_menu = next(x for x in schema.menus if x.path == (20, 1) and x.kind == "map")
col = next(c for c in sch.list_columns(addr_menu) if c.name == "Address")
wire = m.Message().add_u32(0x1, 0x0F02000A).add_u32(0x3, 0x00FFFFFF)
row = RowObject(path=(20, 1), raw=wire, fields=schema.decode((20, 1), wire),
                form_fields=schema.decode_form((20, 1), wire),
                values_by_code={0x1: 0x0F02000A, 0x3: 0x00FFFFFF})
check("...and the form opens on the same text, prefix and all",
      next(v for _t, c, _n, v, _ok in row.form_fields if c == 0x1), "10.0.2.15/24")
client = DeviceClient("192.0.2.1", "admin", "", schema_dir=SCHEMA_DIR)
check("IP ▸ Addresses shows the prefix with the address",
      client._column_text(col, row, addr_menu, {}), "10.0.2.15/24")


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall list-flag tests passed")
