"""An ethernet port's stats tabs, against what Winbox 3 shows for the same port.

No device and no pytest required:

    .venv/bin/python test_stats_tabs.py

A 10-port router's ether3, Overall Stats. Winbox 3 draws seven rows -- Tx/Rx Bytes and six
Tx&Rx size buckets -- and ours drew eight, five of them dashes, none of them
the same eight. Three separate things were behind that, and this pins all of
them:

* An ethernet declares one wire id twice. ``u469`` is "PoE Out Status" on the
  PoE tab and ``Q469`` is "Tx Bytes" on Overall Stats; ``s45c`` is the SFP
  vendor serial and ``Q45c`` is "Rx Bytes". Keeping one form row per *code*
  gave the first declaration the row and the tab, so 19 of Overall Stats' 27
  rows never existed and their values landed on the SFP tab -- which is hidden
  on a copper port, so they vanished.
* ``multituple`` (the Tx/Rx pairs) is laid out like ``tuple``: two boxes on one
  row. It was falling through to one row per member.
* ``hideonempty`` is what keeps a stats tab down to the counters the chip
  actually keeps, instead of a column of dashes.

Plus two presentation details from the same screenshot: a counter declared
``{type:'bigbytes'}`` per element reads "2404.8 MiB", and "Tx&Rx 64" keeps its
ampersand instead of being eaten as a Qt mnemonic.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtGui import QAccessible, QKeySequence
from PySide6.QtWidgets import QApplication, QLabel

from x3.proto import message as m
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema

failures: list[str] = []


def painted(label: QLabel) -> str:
    """A label's text as it reaches the screen, ampersands resolved.

    Qt strips a mnemonic marker for the accessible name with the same rule it
    paints by, so this is the rendered string without rendering it.
    """
    return QAccessible.queryAccessibleInterface(label).text(QAccessible.Text.Name)


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
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

schema = Schema.load(SCHEMA_DIR)
IFACE = (20, 0)
ETHER = 1
BOARD = "EDGE10-LAB"
menu = schema.record_for(IFACE, ETHER)


# --- the tab strip: a row per declaration, not per wire code ----------------

def rows_on(tab: str) -> list[str]:
    """Row titles the form would lay out on one tab, in template order."""
    out: list[str] = []
    for item in menu.form_layout:
        if item.get("tab") != tab:
            continue
        if item.get("kind") == "tuple":
            prefixes = list(item.get("prefixes") or [])
            codes = list(item.get("codes") or [])
            prefixes += [None] * (len(codes) - len(prefixes))
            out.append(item.get("title") or " / ".join(
                (menu.field_for(c, p).name or "?") for c, p in zip(codes, prefixes)
            ))
        elif item.get("kind") == "field":
            fld = menu.field_for(item["code"], item.get("prefix"))
            out.append(fld.name if fld is not None else "?")
    return out


check("Overall Stats has every counter the template declares", rows_on("Overall Stats"), [
    "Tx/Rx Packets", "Tx/Rx Bytes", "Tx/Rx 64", "Tx/Rx 65-127", "Tx/Rx 128-255",
    "Tx/Rx 256-511", "Tx/Rx 512-1023", "Tx/Rx 1024-1518", "Tx/Rx 1519-max",
    "Tx/Rx 1024-max", "Tx&Rx 64", "Tx&Rx 65-127", "Tx&Rx 128-255",
    "Tx&Rx 256-511", "Tx&Rx 512-1023", "Tx&Rx 1024-1518", "Tx&Rx 1519-max",
    "Tx&Rx 1024-max",
])
check("...and Rx Stats too", rows_on("Rx Stats"), [
    "Rx Unicast", "Rx Broadcast", "Rx Pause", "Rx Multicast", "Rx FCS Error",
    "Rx Align Error", "Rx Too Short", "Rx Too Long", "Rx Fragment",
    "Rx Overflow", "Rx Control", "Rx Unknown Op", "Rx Length Error",
    "Rx Code Error", "Rx Carrier Error", "Rx Jabber", "Rx Drop",
    "Rx Error Events",
])
ok("...and the ids they share with SFP/PoE keep those rows too",
   {"PoE Out Status", "PoE Out Current"} & set(rows_on("PoE"))
   and "Vendor Serial" in rows_on("SFP"),
   f"PoE={rows_on('PoE')}")
ok("Rx Broadcast survives sharing 0x44c with the window tray",
   "Rx Broadcast" in rows_on("Rx Stats")
   and 0x44C in menu.statusbar_codes())
check("a shared id resolves to the field the row names",
      [menu.field_for(0x469, "u").name, menu.field_for(0x469, "Q").name],
      ["PoE Out Status", "Tx Bytes"])


# --- decoding: the tag says which of the two fields arrived -----------------

TX_BYTES, RX_BYTES = 0x469, 0x45C
COUNTERS = {                      # what the 10-port router's ether3 answers with
    TX_BYTES: 2521615565,         # Winbox: 2404.8 MiB
    RX_BYTES: 601882624,          # Winbox: 574.0 MiB
    0x471: 8459480,               # Tx&Rx 64
    0x472: 352148,                # Tx&Rx 65-127
    0x473: 1433009,               # Tx&Rx 128-255
    0x474: 6555477,               # Tx&Rx 256-511
    0x475: 38,                    # Tx&Rx 512-1023
    0x476: 132,                   # Tx&Rx 1024-1518
}

wire = m.Message()
wire.add_u32(0xFE0001, 3)
wire.add_u32(0x10001, ETHER)
wire.add_string(0x10006, "ether3")
for code, value in COUNTERS.items():
    wire.add_typed(code, m.T_U64_ARRAY, [value])

decoded = schema.decode_form(IFACE, wire)
by_name = {name: (tab, rendered) for tab, _c, name, rendered, _ok in decoded}
check("a u64 array at 0x469 is Tx Bytes, on Overall Stats",
      by_name.get("Tx Bytes"), ("Overall Stats", "2404.8 MiB"))
check("...and its Rx half reads the same way",
      by_name.get("Rx Bytes"), ("Overall Stats", "574.0 MiB"))
ok("nothing leaked onto the tabs those ids belong to elsewhere",
   not [t for t, _c, _n, _r, _o in decoded if t in ("SFP", "PoE")],
   f"{[(t, n) for t, _c, n, _r, _o in decoded if t in ('SFP', 'PoE')]}")
check("a plain counter is still a plain number",
      by_name.get("Tx&Rx 64"), ("Overall Stats", "8459480"))

# The same id as a scalar is the PoE field, and reads as its enum.
poe = m.Message()
poe.add_u32(0x10001, ETHER)
poe.add_u32(TX_BYTES, 3)
check("the same id as a u32 is PoE Out Status, on the PoE tab",
      [(t, n, r) for t, c, n, r, _o in schema.decode_form(IFACE, poe)
       if c == TX_BYTES],
      [("PoE", "PoE Out Status", "powered on")])


# --- how a byte counter reads -----------------------------------------------
# Winbox changes unit only when the number would need a fifth digit: the same
# port reads "2404.8 MiB" for 2.3 GiB of tx and "27.7 GiB" once it is past
# 28355.8 MiB. Both are off one router, in Winbox 3 and 4 alike.

from x3.proto.schema import format_bytes

check("a counter reads the way Winbox prints it",
      [format_bytes(n) for n in (0, 9999, 601882624, 2521615565, 29733585715)],
      ["0 B", "9999 B", "574.0 MiB", "2404.8 MiB", "27.7 GiB"])
ok("...and 2.3 GiB stays in MiB rather than promoting at every 1024",
   format_bytes(2521615565).endswith("MiB"))

# Winbox's rounding is the default, not the only reading: a counter is also
# something people round the ordinary way, or paste into a calculator.
from x3.proto.schema import BYTE_UNIT_MODES, byte_unit_mode, set_byte_unit_mode

check("the reading is a choice", list(BYTE_UNIT_MODES),
      ["compact", "binary", "exact"])
set_byte_unit_mode("binary")
check("binary units round at every 1024", format_bytes(2521615565), "2.3 GiB")
set_byte_unit_mode("exact")
check("exact leaves the counter alone", format_bytes(2521615565),
      "2521615565 B")
check("a value that is not a mode falls back to Winbox's",
      set_byte_unit_mode("nonsense"), "compact")
check("...and that is what the rest of this file runs under",
      (byte_unit_mode(), format_bytes(2521615565)), ("compact", "2404.8 MiB"))


# --- the form Winbox draws --------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)

from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import (
    PropertyWindow,
    _row_label,
    _tuple_part_labels,
)


class StubInfo:
    board = BOARD


# A real client, with no socket: `rerender` is pure decode, so the window test
# below exercises the shipped method rather than a stand-in for it.
DECODER = DeviceClient("127.0.0.1", "u", "p", readonly=True)
DECODER.schema = schema
DECODER.session = None


class StubClient:
    """Answers only what opening a form asks for; never talks to a device."""

    info = StubInfo()

    def rerender(self, row):
        return DECODER.rerender(row)

    def menu_for(self, path, type_value=None, values=None):
        if type_value is None:
            return schema.record(path)
        return schema.record_for(path, type_value, values)

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return ROW

    def get(self, path):
        return ROW

    def prefetch_enums(self, paths):
        return None

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return list(sorted((fld.enum or {}).items()))

    def interface_choices(self):
        return []


# The read that opens a form (unfiltered GETOBJ) does not carry the counters:
# The router sends those on the refresh the menu asks for by its own
# `refreshfilter` (0x4000A). So the form opens with the stats tabs present and
# empty, and the poll is what fills them -- which is why "is this counter
# empty?" cannot be answered once at build time.
OPEN_WIRE = m.Message()
OPEN_WIRE.add_u32(0xFE0001, 3)
OPEN_WIRE.add_u32(0x10001, ETHER)
OPEN_WIRE.add_string(0x10006, "ether3")
OPEN_ROW = RowObject(
    path=IFACE, raw=OPEN_WIRE,
    fields=schema.decode(IFACE, OPEN_WIRE),
    form_fields=schema.decode_form(IFACE, OPEN_WIRE),
    values_by_code={0xFE0001: 3, 0x10001: ETHER, 0x10006: b"ether3"},
)
ROW = RowObject(
    path=IFACE,
    raw=wire,
    fields=schema.decode(IFACE, wire),
    form_fields=decoded,
    values_by_code={0xFE0001: 3, 0x10001: ETHER, 0x10006: b"ether3",
                    **{c: [v] for c, v in COUNTERS.items()}},
)


def shown(win, tab: str) -> list[tuple[str, str]]:
    """The rows a tab is actually showing, and what is in their boxes."""
    out = []
    for spec, editor in win._rows:
        if spec.get("tab") != tab or editor.isHidden():
            continue
        if spec.get("kind") == "tuple":
            text = " / ".join(lab.text() for lab in _tuple_part_labels(editor))
        else:
            text = editor.text() if hasattr(editor, "text") else ""
        out.append((spec["name"], text))
    return out


win = PropertyWindow(
    "Interface — ether3", IFACE, OPEN_ROW,
    client=StubClient(),
    edit_session=EditSession(EditMode.LIVE, client=None),
    menu=menu,
    row_key="3",
    full_row=True,
)
win._ensure_all_pages()
app.processEvents()

tabs = win._tabs
titles = [tabs.tabText(i) for i in range(tabs.count())]
ok("the stats tabs are there", {"Overall Stats", "Rx Stats", "Tx Stats"} <= set(titles),
   f"{titles}")
ok("...and the tabs this port has no hardware for are not",
   not ({"PoE", "SFP"} & set(titles)), f"{titles}")
check("before the first refresh the counters are declared but not shown",
      (len([s for s, _e in win._rows if s.get("tab") == "Overall Stats"]),
       shown(win, "Overall Stats")),
      (18, []))
ok("...and the tab is on the strip all the same",
   "Overall Stats" in titles, f"{titles}")

# The refresh lands: the port's own counters, and nothing for the ones its
# chip does not keep.
win._on_traffic_row(ROW)
app.processEvents()

check("the refresh shows exactly what Winbox 3 shows for this port, in order",
      shown(win, "Overall Stats"),
      [("Tx/Rx Bytes", "2404.8 MiB / 574.0 MiB"),
       ("Tx&Rx 64", "8459480"),
       ("Tx&Rx 65-127", "352148"),
       ("Tx&Rx 128-255", "1433009"),
       ("Tx&Rx 256-511", "6555477"),
       ("Tx&Rx 512-1023", "38"),
       ("Tx&Rx 1024-1518", "132")])
check("...and Rx Stats stays empty, without a column of dashes",
      shown(win, "Rx Stats"), [])

index = titles.index("Overall Stats")
tabs.setCurrentIndex(index)
app.processEvents()
ok("a stats tab keeps the 1 Hz refresh running, like Traffic",
   win._traffic_tab_active())

# Changing how bytes read restates what is already on screen: the object is
# in hand, so nothing is re-read from the device to say the same number
# differently.
set_byte_unit_mode("binary")
win.rerender_values()
app.processEvents()
check("switching units restates the open form", shown(win, "Overall Stats")[0],
      ("Tx/Rx Bytes", "2.3 GiB / 574.0 MiB"))
set_byte_unit_mode("compact")
win.rerender_values()
check("...and back", shown(win, "Overall Stats")[0],
      ("Tx/Rx Bytes", "2404.8 MiB / 574.0 MiB"))

# (Winbox punctuates a row label; the ampersand is what is under test here.)
#
# The label is a field's buddy now -- that relation is the only thing that
# names a dropdown for a screen reader on Unix (see x3.ui.a11y.pair) -- and
# a buddy's lone ampersand *would* be a mnemonic, so pairing doubles it. What
# must not change is what reaches the screen and the keyboard: still one
# ampersand painted, still no Alt+R claimed anywhere.
labels = [w for w in win.findChildren(QLabel) if painted(w).endswith("&Rx 64:")]
check("a label keeps its ampersand", [painted(w) for w in labels],
      ["Tx&Rx 64:"])
ok("...and claims no mnemonic for it",
   labels and QKeySequence.mnemonic(labels[0].text()).isEmpty())
ok("...while still naming the field it labels",
   labels and labels[0].buddy() is not None)
check("...and a label with no buddy is not doubled at all",
      _row_label("Tx&Rx 64").text(), "Tx&Rx 64:")

win.close()


# --- the 1 Hz poll paints into the row it answered for ----------------------
# A PoE-capable port has both declarations of 0x469 on screen: "PoE Out
# Status" on the PoE tab and "Tx Bytes" on Overall Stats. A poll that carries
# only the counter must not repaint the enum's box with a byte count.

CAPS, POE_CAP = 0x10002, 0x80000
poe_wire = m.Message()
poe_wire.add_u32(0xFE0001, 4)
poe_wire.add_u32(0x10001, ETHER)
poe_wire.add_string(0x10006, "ether1")
poe_wire.add_u32(CAPS, POE_CAP)
poe_wire.add_u32(TX_BYTES, 3)                       # u469: PoE Out Status
poe_wire.add_typed(TX_BYTES, m.T_U64_ARRAY, [4096])  # Q469: Tx Bytes

POE_ROW = RowObject(
    path=IFACE, raw=poe_wire,
    fields=schema.decode(IFACE, poe_wire),
    form_fields=schema.decode_form(IFACE, poe_wire),
    values_by_code={0xFE0001: 4, 0x10001: ETHER, CAPS: POE_CAP,
                    0x10006: b"ether1", TX_BYTES: 3},
)
poe_win = PropertyWindow(
    "Interface — ether1", IFACE, POE_ROW,
    client=StubClient(),
    edit_session=EditSession(EditMode.LIVE, client=None),
    menu=menu, row_key="4", full_row=True,
)
poe_win._ensure_all_pages()
app.processEvents()

both = {
    (spec["tab"], spec["name"]): spec.get("rendered")
    for spec, _e in poe_win._rows if TX_BYTES in
    ([spec.get("code")] + [p.get("code") for p in spec.get("parts") or []])
}
check("one id, two rows, each on its own tab with its own value", both,
      {("PoE", "PoE Out Status"): "powered on",
       ("Overall Stats", "Tx/Rx Bytes"): "4096 B / —"})

# The poll answers with the counter only.
tick = m.Message()
tick.add_u32(0xFE0001, 4)
tick.add_u32(0x10001, ETHER)
tick.add_typed(TX_BYTES, m.T_U64_ARRAY, [8192])
poe_win._on_traffic_row(RowObject(
    path=IFACE, raw=tick,
    fields=schema.decode(IFACE, tick),
    form_fields=schema.decode_form(IFACE, tick),
    values_by_code={0xFE0001: 4, 0x10001: ETHER},
))
app.processEvents()
check("the poll repaints the counter and leaves the enum alone",
      {name: lab.text() for name, lab in poe_win._live_labels[TX_BYTES]},
      {"Tx Bytes": "8192 B", "PoE Out Status": "powered on"})
poe_win.close()


# --- and an SFP port does not read its link length as a counter -------------
# 0x473 is `u473` "Cable Assembly Link Length" on SFP and `Q473` "Tx&Rx
# 128-255" on Overall Stats. With only the SFP value on the wire, the counter
# row has nothing -- so it is not a row.

SFP_FLAG, CABLE_LEN = 0x400, 0x473
sfp_wire = m.Message()
sfp_wire.add_u32(0xFE0001, 5)
sfp_wire.add_u32(0x10001, ETHER)
sfp_wire.add_string(0x10006, "sfp-sfpplus1")
sfp_wire.add_bool(SFP_FLAG, True)
sfp_wire.add_u32(CABLE_LEN, 3)
sfp_win = PropertyWindow(
    "Interface — sfp-sfpplus1", IFACE,
    RowObject(path=IFACE, raw=sfp_wire,
              fields=schema.decode(IFACE, sfp_wire),
              form_fields=schema.decode_form(IFACE, sfp_wire),
              values_by_code={0xFE0001: 5, 0x10001: ETHER, SFP_FLAG: True,
                              0x10006: b"sfp-sfpplus1", CABLE_LEN: 3}),
    client=StubClient(),
    edit_session=EditSession(EditMode.LIVE, client=None),
    menu=menu, row_key="5", full_row=True,
)
sfp_win._ensure_all_pages()
app.processEvents()
check("the SFP value stays on the SFP tab",
      [r for r in shown(sfp_win, "SFP") if r[0].startswith("Cable")],
      [("Cable Assembly Link Length", "0.3m")])
ok("...and is not also read as the counter that shares its id",
   not [r for r in shown(sfp_win, "Overall Stats") if "128-255" in r[0]],
   f"{shown(sfp_win, 'Overall Stats')}")
sfp_win.close()


# --- a refresh must not eat what the user is copying ------------------------
# The counters tick once a second. Selecting one to copy it used to lose the
# selection on the next tick, which made copying a counter impossible.

from x3.ui.widgets.property_window import ReadOnlyValueEdit, _set_live_text

box = ReadOnlyValueEdit("27.7 GiB")
box.setCursorPosition(3)
_set_live_text(box, "27.8 GiB")
check("a refresh repaints the value", box.text(), "27.8 GiB")
check("...without scrolling the box back to the end", box.cursorPosition(), 3)
# A double-click selects the whole value, and what belongs in the clipboard is
# the current number -- so it keeps updating, still selected.
box.selectAll()
_set_live_text(box, "27.9 GiB")
check("a wholly selected value stays selected, and stays current",
      (box.text(), box.selectedText()), ("27.9 GiB", "27.9 GiB"))
ok("...so there is nothing stale to warn about", not box.is_held)

# Dragging across part of it is the case where repainting would lie: the range
# would land on digits nobody picked. So that one is held, and says so.
box.setSelection(0, 4)
_set_live_text(box, "28.0 GiB")
check("a part-selected value is left alone until the selection goes",
      (box.text(), box.selectedText()), ("27.9 GiB", "27.9"))
ok("...and says so, rather than going quietly stale", box.is_held)
check("...carrying what the device now reports", box.toolTip(),
      "Held while selected — the device now reports 28.0 GiB")
_set_live_text(box, "28.1 GiB")
check("...kept current while the hold lasts", box.toolTip(),
      "Held while selected — the device now reports 28.1 GiB")
box.deselect()
_set_live_text(box, "28.1 GiB")
check("...and catches up after that", box.text(), "28.1 GiB")
ok("...dropping the held mark with it", not box.is_held and not box.toolTip())

# A value that has not moved is never repainted, so a selection on a static
# counter is never disturbed and never marked held.
still = ReadOnlyValueEdit("8459480")
still.setSelection(0, 3)
_set_live_text(still, "8459480")
ok("a value that has not moved is not touched at all",
   not still.is_held and still.selectedText() == "845")


# --- the preference itself --------------------------------------------------

from x3.ui.settings import apply_byte_units, load_byte_units, save_byte_units

check("the default reading is Winbox's", load_byte_units(), "compact")
save_byte_units("exact")
check("...and the choice is remembered", load_byte_units(), "exact")
check("...and applying it reaches the renderer", apply_byte_units(), "exact")
check("...which is what every value then goes through",
      format_bytes(2521615565), "2521615565 B")
save_byte_units("nonsense")
check("a settings file with junk in it still renders",
      (load_byte_units(), apply_byte_units()), ("compact", "compact"))


# --- the Traffic tab cannot refresh faster than the device recomputes -------
# The header used to offer 4 / s and 2 / s. The router answers every one of those
# polls, but with the same numbers: on a virtual router at 200 ms all 40 replies in 8 s
# carried the previous second's rates *and* the previous second's byte and
# packet counters, so a faster poll cannot even be turned into a rate of its
# own. So 1 / s is the floor, and a settings file left holding 250 -- ours
# would be, from when the header offered it -- opens at 1 / s.

from x3.ui.settings import load_traffic_poll_ms, save_traffic_poll_ms
from x3.ui.widgets.property_window import TrafficGraphBox

ok("nothing faster than the device's own tick is offered",
   min(ms for ms, _text in TrafficGraphBox.RATES) == 1000,
   f"{TrafficGraphBox.RATES}")
save_traffic_poll_ms(250)
check("a rate saved before the floor existed comes back as 1 / s",
      load_traffic_poll_ms(), 1000)

box = TrafficGraphBox([], rate_ms=250)
check("...and a header handed one selects 1 / s rather than the first entry",
      box._rate_combo.currentText(), "1 / s")
box.deleteLater()

save_traffic_poll_ms(5000)
box = TrafficGraphBox([], rate_ms=load_traffic_poll_ms())
check("a rate the list still has is honoured", box._rate_combo.currentText(),
      "1 / 5s")
box.deleteLater()


# --- .jg autorefresh: an item form re-reads itself --------------------------
# The rate is on the menu, not on the tab: System ▸ Resources declares
# `autorefresh:1000`, and Winbox v3 counts its uptime and CPU load while the
# window sits there. A list pane honoured that already; the form is what this
# pins -- a timer at the menu's own rate, one GET a tick on a worker thread,
# and the reply painted into the same labels the traffic poll writes to.

RESOURCES = (24, 2)
res_menu = schema.record(RESOURCES)
check("the menu carries the rate, and it is a form not a list",
      (res_menu.kind, res_menu.autorefresh_ms, res_menu.is_collection),
      ("item", 1000, False))


class PollClient(StubClient):
    """A menu of readings, one row, and a count of the polls for it."""

    def __init__(self, menu, row):
        self.menu = menu
        self.row = row
        self.gets = 0
        self.fail = False

    def menu_for(self, path, type_value=None, values=None):
        return self.menu

    def get(self, path, **kw):
        self.gets += 1
        if self.fail:
            raise RuntimeError("no reply")
        return self.row


def form(menu, path, row, client) -> PropertyWindow:
    win = PropertyWindow(
        str(menu.title), path, row, client=client,
        edit_session=EditSession(EditMode.LIVE, client=None),
        menu=menu, full_row=True,
    )
    win.show()                      # an unshown form does not poll
    app.processEvents()
    return win


def tick(win) -> None:
    """One poll, waited out -- the reply arrives on the event loop."""
    win._menu_refresh_tick()
    worker = win._refresh_worker
    assert worker is not None, "the tick started no poll"
    worker.wait(2000)
    app.processEvents()


def row_for(path, values: dict) -> RowObject:
    msg = m.Message()
    for code, value in values.items():
        msg.add_u32(code, value)
    return RowObject(
        path=path, raw=msg, fields=schema.decode(path, msg),
        form_fields=schema.decode_form(path, msg),
        values_by_code=dict(values),
    )


res_client = PollClient(res_menu, row_for(RESOURCES, {0x9: 42}))
res_win = form(res_menu, RESOURCES, row_for(RESOURCES, {0x9: 5}), res_client)

ok("the form starts a timer of its own", res_win._refresh_timer is not None)
check("...at the rate the menu asked for",
      res_win._refresh_timer.interval(), 1000)
tick(res_win)
check("a tick reads the menu once", res_client.gets, 1)
tick(res_win)
check("...and again on the next one", res_client.gets, 2)

# A poll that fails stops rather than repeating the same failure every second.
res_client.fail = True
tick(res_win)
ok("a failed re-read stops the timer", not res_win._refresh_timer.isActive())
ok("...and says why", "no reply" in res_win._live_status.text())
res_win.close()

# What a tick repaints, and what it must not. NTP Client is an item form of
# settings *and* readings at 1 Hz: the readings move on a poll, and an edited
# setting beside them does not -- only the registered read-only values are
# painted (_paint_live_values), so a refresh can never overwrite what is being
# typed into one of the editable forms that declare a rate.
NTP = (47, 1)
ntp_menu = schema.record(NTP)
STRATUM = 0x65
ntp_client = PollClient(ntp_menu, row_for(NTP, {STRATUM: 30}))
ntp_win = form(ntp_menu, NTP, row_for(NTP, {STRATUM: 1}), ntp_client)


def reading(win, name: str):
    return next(e for s, e in win._rows if s.get("name") == name)


check("the form opens on the reading it was handed",
      reading(ntp_win, "Synced Stratum").text(), "1")
enabled = reading(ntp_win, "Enabled")
enabled.setChecked(True)                    # an edit the poll must not undo
tick(ntp_win)
check("a tick repaints the reading",
      reading(ntp_win, "Synced Stratum").text(), "30")
ok("...and leaves an edited setting where the user put it", enabled.isChecked())

# A reading someone is dragging across is held (see _set_live_text above), and
# the form says so rather than letting a frozen number look like a dead device.
reading(ntp_win, "Synced Stratum").setSelection(0, 1)   # part of "30"
ntp_client.row = row_for(NTP, {STRATUM: 90})
tick(ntp_win)
check("a held reading is left alone until the selection goes",
      reading(ntp_win, "Synced Stratum").text(), "30")
check("...and is reported, rather than going quietly stale",
      ntp_win._live_status.text(), "1 value(s) held while selected")
ntp_win._allow_close = True      # the Enabled edit above is still pending
ntp_win.close()

# A row's form is not polled here: the object is one row of a list, and the
# Traffic tab's poll already reads it under the mask that tab needs.
ok("a list row's form starts no menu refresh", win._refresh_timer is None)


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall stats-tab tests passed")
