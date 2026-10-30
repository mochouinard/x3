"""Wireless ▸ Wireless: the family tab, and a wlan interface's own form.

No device and no pytest required:

    .venv/bin/python test_wireless_tabs.py

Two things went wrong on a 10-port router running the legacy wireless package, and
both come from the same corner of the .jg. ``WiFi Interfaces`` is declared
``typevalue:0xffffffff`` -- not a type, the *base* of the 'wlan' family -- and
dropping it left the window opening on W60G, a tab that only exists on a 60GHz
board and is empty everywhere else. And a wireless interface is type 35 for
every Atheros card, with ``hwtype`` picking the concrete one a level further
down, so resolving a single overlay level gave wlan1 the bare Interface form
with no Wireless tab at all.

What is pinned here is the whole path the UI takes: nav → tab strip → the
per-tab fetch, and the per-row form the list opens.
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

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}")


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

app = QApplication.instance() or QApplication(sys.argv)

from x3.ui.widgets.menu_tree import _payload_from_nav
from x3.ui.widgets.multi_page_window import MultiPageListWindow

schema = Schema.load(SCHEMA_DIR)
IFACE = (20, 0)
BOARD = "EDGE10-LAB"
HWTYPE = next(c for c, f in schema.overlay_by_generic["wlan"].fields.items()
              if f.name == "hwtype")
TYPE_CODE = sch.type_field_code(schema.record(IFACE))


# --- the tab strip ----------------------------------------------------------

def wireless_payload(board=BOARD):
    nav = sch.filter_nav_by_pred(schema.nav, board=board, arch="arm")
    folder = next(n for n in nav if n.title == "Wireless")
    # Wireless ▸ Wireless (the sibling of CAPsMAN), which is the window.
    return _payload_from_nav(next(k for k in folder.children
                                  if k.title == "Wireless"))


payload = wireless_payload()
tabs = [p["title"] for p in payload["page_group"]["pages"]]
check("the window opens on the family tab, not on W60G", tabs[0],
      "WiFi Interfaces")
ok("...and W60G is not there at all on a board without a 60GHz radio",
   "W60G" not in tabs)
ok("the rest of Winbox's tab strip is intact", all(
    t in tabs for t in ("Nstreme Dual", "Access List", "Registration",
                        "Connect List", "Security Profiles", "Channels")))
check("the family tab carries the family instead of a type",
      (payload["page_group"]["pages"][0]["family"],
       payload["page_group"]["pages"][0]["type_value"],
       tuple(payload["page_group"]["pages"][0]["path"])),
      ("wlan", None, IFACE))
ok("a 60GHz board does get the W60G tab",
   "W60G" in [p["title"] for p in wireless_payload("wAP 60adG")
              ["page_group"]["pages"]])


# --- the fetch behind each tab ----------------------------------------------

def row(type_value: int, extra: dict | None = None) -> RowObject:
    values = {TYPE_CODE: type_value, **(extra or {})}
    return RowObject(path=IFACE, raw=m.Message(), fields=[], form_fields=[],
                     values_by_code=values)


ROWS = [
    row(1),                       # ether1
    row(35, {HWTYPE: 12}),        # wlan1, an IPQ4019 radio
    row(35, {HWTYPE: 12}),        # wlan2
    row(36),                      # wds1
    row(38),                      # nstreme1
    row(12),                      # bridge1
]


class StubClient(DeviceClient):
    """A client that answers GETALL from the fixture above and nothing else."""

    def __init__(self):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.schema = schema
        self.session = None
        self.asked: list[tuple[int, ...]] = []

    def getall(self, path, **kw):
        self.asked.append(tuple(path))
        return list(ROWS)

    def uptime(self):
        return 0.0

    def enum_map(self, path, **kw):
        return {}


client = StubClient()

menu, _cols, _str_rows, raw, _up = client.table_rows(IFACE, family="wlan")
check("the family tab lists every wireless type and nothing else",
      sorted(r.values_by_code[TYPE_CODE] for r in raw), [35, 35, 36, 38])
ok("...with the family's own columns", "Wireless" in menu.tab_names())
check("a single-type tab still filters to one type",
      [r.values_by_code[TYPE_CODE]
       for r in client.table_rows(IFACE, type_value=1)[3]], [1])
check("no type and no family still lists everything",
      len(client.table_rows(IFACE)[3]), len(ROWS))


# --- the panes the window builds --------------------------------------------

built: list[tuple] = []


def make_fetcher(path, type_value=None, family=None, menu_name=None):
    built.append((tuple(path), type_value, family))

    def fetch():
        return client.table_rows(tuple(path), type_value=type_value,
                                 family=family)
    return fetch


win = MultiPageListWindow(
    "Wireless", payload["page_group"]["pages"], make_fetcher=make_fetcher,
)
check("only the tab on screen has a pane", len(win.panes()), 1)
check("...and it is the family one",
      (win.panes()[0].path, win.panes()[0].type_value, win.panes()[0].family),
      (IFACE, None, "wlan"))
ok("its fetcher was built for the family too", (IFACE, None, "wlan") in built)

# Visiting every tab builds the rest, once each.
for i in range(win.tabs.count()):
    win.tabs.setCurrentIndex(i)
panes = win.panes()
check("every tab builds its pane when selected", len(panes), win.tabs.count())
for i in range(win.tabs.count()):
    win.tabs.setCurrentIndex(i)
check("...and revisiting builds nothing again", len(win.panes()), len(panes))
ok("panes on one path stay distinct in the saved layout",
   len({p._pref_key() for p in panes}) == len(panes))
check("each pane fetches what its tab is for",
      built,
      # 'item' pages are single-object actions (CAP), which Winbox puts on the
      # toolbar rather than in the tab strip -- they get no pane.
      [(tuple(p["path"]), p.get("type_value"), p.get("family"))
       for p in payload["page_group"]["pages"] if p.get("kind") != "item"])
win.close()


def opens_on(type_value=None, family=None):
    """Which tab a nav click lands on. Ten tabs share (20,0) here."""
    w = MultiPageListWindow(
        "Wireless", payload["page_group"]["pages"], make_fetcher=make_fetcher,
        active_path=IFACE, active_type_value=type_value, active_family=family,
    )
    title = w.tabs.tabText(w.tabs.currentIndex())
    w.close()
    return title


check("clicking the family node opens its tab", opens_on(family="wlan"),
      "WiFi Interfaces")
check("clicking a type node opens that type's tab", opens_on(type_value=64),
      "W60G Station")
check("a path with no type still opens something on that path",
      opens_on(), "WiFi Interfaces")


# --- the form the list opens ------------------------------------------------

wlan1 = ROWS[1]
base = client.menu_for(IFACE)
type_value = sch.wire_type_value(wlan1, base)
check("the row's type is read through the field the schema names",
      type_value, 35)
form = client.menu_for(IFACE, type_value, values=wlan1.values_by_code)
check("a wireless interface gets its wireless tabs", form.tab_names(),
      ["General", "Wireless", "Data Rates", "Advanced", "HT", "HT MCS",
       "WDS", "Nstreme", "NV2", "Tx Power", "Current Tx Power",
       "Advanced Status", "Compression Status", "Status", "Traffic"])
ok("...and the fields behind them",
   {"SSID", "Band", "Radio Name", "Country"}
   <= {f.name for f in form.fields.values()})
ok("a chip-specific overlay is reached too",
   len(form.fields) > len(client.menu_for(IFACE, 35).fields))
check("an ethernet port is unaffected",
      client.menu_for(IFACE, 1, values=ROWS[0].values_by_code).tab_names(),
      ["General", "PoE", "SFP", "Ethernet", "Loop Protect", "Overall Stats",
       "Rx Stats", "Tx Stats", "Status", "Traffic"])


# --- decoding a row ---------------------------------------------------------
# The form's labels come from decode_form(), which resolves the overlay itself
# off the message. Doing that without the row stopped the chain one level in:
# the Wireless tab appeared, but everything past SSID -- the whole hardware
# overlay -- decoded as <0x…> because those codes belonged to no known field.

RADIO_NAME, COUNTRY, MAX_STA = 0x89A, 0x8AD, 0x7F9
wire = m.Message()
wire.add_u32(TYPE_CODE, 35)
wire.add_u32(HWTYPE, 12)
wire.add_string(0x7D3, "office")     # SSID, from the family overlay
wire.add_string(RADIO_NAME, "r1")    # the three below are hardware-overlay
wire.add_u32(COUNTRY, 3)
wire.add_u32(MAX_STA, 2007)

ok("the premise: the hardware overlay is out of reach without the row",
   RADIO_NAME not in schema.record_for(IFACE, 35).fields)
named = {code: name for _tab, code, name, _v, _ok in schema.decode_form(IFACE, wire)}
check("every wireless field decodes to its name, not a hex code",
      [named.get(c) for c in (0x7D3, RADIO_NAME, COUNTRY, MAX_STA)],
      ["SSID", "Radio Name", "Country", "Max Station Count"])
# Max Station Count is not a Wireless-tab field: the .jg hangs it off the
# named `Advanced` deck, which is Winbox v3's Advanced tab.
check("...and they land on the tab Winbox puts them on",
      {n: tab for tab, code, n, _v, _ok in schema.decode_form(IFACE, wire)
       if code in (0x7D3, RADIO_NAME, COUNTRY, MAX_STA)},
      {"SSID": "Wireless", "Radio Name": "Wireless", "Country": "Wireless",
       "Max Station Count": "Advanced"})
ok("the flat decode names them too", not any(
    n.startswith("<0x") for c, n, _v, _ok in schema.decode(IFACE, wire)
    if c in (0x7D3, RADIO_NAME, COUNTRY, MAX_STA)))


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall wireless-tab tests passed")
