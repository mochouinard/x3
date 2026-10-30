"""Right-click a row: the .jg's ``type:contextmenu``.

No device and no pytest required:

    .venv/bin/python test_context_actions.py

Winbox's most-used gesture is a right-click on a list row -- IP ▸ ARP ▸ Tools ▸
Ping, Wireless Registration ▸ Copy to Access List, DHCP Leases ▸ Tools ▸ Ping
-- and none of it is hardcoded anywhere in Winbox. Each entry is declared::

    {name:'Ping',type:'contextmenu',autostart:1,group:'Tools',
     link:[{dst:'Ping To',src:'Address'}],open:'Ping',tab:'Ping'}

which reads: put this row's *Address* in the *Ping To* box of the menu titled
*Ping*, and start it. `{conv:'put',dst:'Type',val:1}` writes a literal instead
of copying a field, which is how one entry becomes Telnet and the next MAC
Telnet against the same target window.

276 of these are declared in 7.21 and every one of them was being dropped on
the floor: the parser had no branch for the node, so the whole gesture was
missing from the UI.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QLineEdit

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.main_window import MainWindow
from x3.ui.widgets.query_window import QueryWindow

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
arp = schema.record(ARP)


# --- the schema side --------------------------------------------------------

names = [a["name"] for a in arp.context_actions]
check("ARP rows carry Winbox's own right-click menu", names,
      ["Make Static", "Ping", "MAC Ping", "Telnet", "MAC Telnet", "Torch"])
ping = next(a for a in arp.context_actions if a["name"] == "Ping")
check("...grouped the way the .jg groups them", ping["group"], "Tools")
check("...naming the menu it opens", (ping["open"], ping["tab"]),
      ("Ping", "Ping"))
ok("...and asking for it to run at once", ping["autostart"])
check("...carrying this row's address into Ping To", ping["link"],
      [{"src": "IP Address", "dst": "Ping To", "val": None, "conv": None}])

mac_telnet = next(a for a in arp.context_actions if a["name"] == "MAC Telnet")
check("a literal link writes a value instead of copying a field",
      [(l["dst"], l["val"], l["conv"]) for l in mac_telnet["link"]
       if l["src"] is None],
      [("Type", 3, "put")])

ok("every plugin's lists get them, not just roteros",
   all(
       any(a["name"] == "Ping" for a in schema.record(p).context_actions)
       for p in ((23, 1), (20, 38))      # DHCP Leases, IPv6 Neighbor List
   ))


# --- what a link resolves to ------------------------------------------------

msg = m.Message()
msg.add_u32(0xFE0001, 1)
msg.add_u32(0x000001, 0xC058A8C0)        # IP Address 192.168.88.192
msg.add_u32(0x000003, 3)                 # Interface (an object id)
row = RowObject(
    path=ARP, raw=msg, fields=schema.decode(ARP, msg),
    form_fields=schema.decode_form(ARP, msg),
    values_by_code={c: v for c, _t, v in msg},
)

# _context_prefill reads nothing off the window, so it answers unbound.
prefill = MainWindow._context_prefill(None, ping, row, arp)
check("the address goes across as the text a box shows",
      prefill.get("Ping To", {}).get("text"), "192.168.88.192")
check("...and as the raw value a dropdown would hold",
      prefill.get("Ping To", {}).get("raw"), 0xC058A8C0)

torch = next(a for a in arp.context_actions if a["name"] == "Torch")
tprefill = MainWindow._context_prefill(None, torch, row, arp)
check("an interface goes across by object id",
      tprefill.get("Interface", {}).get("raw"), 3)
check("...a literal comes across as itself",
      tprefill.get("MAC Protocol", {}).get("raw"), 0)
ok("a link naming a field this row does not have is skipped, not guessed",
   "Nonexistent" not in MainWindow._context_prefill(
       None,
       {"link": [{"src": "Nonexistent", "dst": "Nonexistent",
                  "val": None, "conv": None}]},
       row, arp,
   ))


# --- finding the menu an action names --------------------------------------
# `open:`/`tab:` give a *title*, which is the only handle the .jg offers, so
# the dispatcher has to find it in the nav tree the device's plugins built.


class NavShim:
    """Just the nav tree, which is all _nav_node_for reads off the window."""

    class _Client:
        @staticmethod
        def nav_tree():
            return schema.nav

    client = _Client()


shim = NavShim()
ping_node = MainWindow._nav_node_for(shim, "Ping")
ok("the Ping tool is found by the title the action names", ping_node is not None)
check("...and it is the tool itself, not a folder of the same name",
      (ping_node.kind, ping_node.path is not None), ("query", True))
ok("a tab title deep in a page group is found too",
   MainWindow._nav_node_for(shim, "Access List") is not None)
ok("a title no menu has resolves to nothing, rather than to the wrong menu",
   MainWindow._nav_node_for(shim, "No Such Menu") is None)
ok("...and so does an empty one", MainWindow._nav_node_for(shim, "") is None)


# --- the menu the pane builds ----------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)

fired: list[tuple] = []
pane = ListPane(
    "ARP List", path=ARP,
    on_context_action=lambda act, r, menu: fired.append((act["name"], r, menu)),
)
pane._menu = arp
menu = pane.row_menu_for(row)
ok("a row gets a menu", menu is not None)
top = [a.text() for a in menu.actions() if not a.menu() and a.text()]
groups = {a.text(): a.menu() for a in menu.actions() if a.menu()}
check("groups become submenus", sorted(groups), ["IP", "Tools"])
check("...with the group's entries inside",
      [a.text() for a in groups["Tools"].actions()],
      ["Ping", "MAC Ping", "Telnet", "MAC Telnet", "Torch"])
check("...and Make Static under IP", [a.text() for a in groups["IP"].actions()],
      ["Make Static"])
check("nothing is left loose at the top level", top, [])

groups["Tools"].actions()[0].trigger()
check("triggering an entry hands the action and the row to the window",
      [(name, r is row) for name, r, _m in fired], [("Ping", True)])

ok("a pane with no actions offers no menu",
   ListPane("Empty", path=(1, 1),
            on_context_action=lambda *_a: None).row_menu_for(row) is None)
pane.shutdown()


# --- a toolbar the .jg declares ---------------------------------------------
# Quick Set keeps its verbs on a `type:toolbar`, not on a row's right-click
# menu: Apply Configuration, Port Mapping, and a System group over Reboot /
# Reset Configuration / Password / Check For Updates. Same node type, so the
# only thing telling them apart is where they were declared.

QUICK_SET = (120, 4)
quickset = next(
    mn for mn in schema.menus if mn.path == QUICK_SET and mn.kind == "item"
)
tb = [a for a in quickset.context_actions if a.get("toolbar")]
check("Quick Set's entries are marked as a toolbar's, not a row menu's",
      (len(tb), len(quickset.context_actions) - len(tb)),
      (len(quickset.context_actions), 0))
check("...and an ARP row's are not",
      any(a.get("toolbar") for a in arp.context_actions), False)
check("a bare {type:'contextmenu'} is a separator, not a nameless button",
      [a["name"] for a in tb if not a["name"]], [""])
check("Apply Configuration is a doset, with no menu to open",
      [(a["name"], a["doset"], a["open"]) for a in tb if a.get("doset")],
      [("Apply Configuration", True, None)])
check("...and the rest name what they open",
      sorted((a["name"], a["group"]) for a in tb if a.get("open")),
      [("Check For Updates", "System"), ("Password...", "System"),
       ("Port Mapping", None), ("Reboot", "System"),
       ("Reset Configuration", "System")])

qs_fired: list[str] = []
qpane = ListPane(
    "Home AP", path=QUICK_SET,
    on_context_action=lambda act, r, menu: qs_fired.append(act["name"]),
)
qpane._menu = quickset
qpane._install_jg_toolbar()
buttons = {b.text(): b for b, _a in qpane._jg_buttons}
check("the toolbar's ungrouped entries become buttons",
      sorted(k for k in buttons if k), ["Port Mapping", "System"])
ok("...the group is one button with a menu",
   buttons["System"].menu() is not None)
check("...holding the group's entries",
      [a.text() for a in buttons["System"].menu().actions()],
      ["Check For Updates", "Reboot", "Reset Configuration", "Password..."])
ok("a doset button is not one of them — the form's own Apply does that",
   "Apply Configuration" not in buttons)

buttons["System"].menu().actions()[1].trigger()
check("clicking one hands it to the window", qs_fired, ["Reboot"])
ok("...and it needs no row, having nothing to carry",
   qpane._jg_action_row() is None)

# `on:` gating: Port Mapping is declared `on:'nat'` -- `{on:'NAT',pred:bool 1}`
# -- so it is a button only while this object has NAT switched on. There is no
# row here at all, so it is not.
port_btn = buttons["Port Mapping"]
qpane._update_jg_buttons()
# isHidden(), not isVisible(): the pane itself was never shown, and every
# child of an unshown widget reports invisible.
ok("an on:-gated button is hidden while its condition fails",
   port_btn.isHidden())
ok("...while an ungated one stays", not buttons["System"].isHidden())


class FakeRow:
    """A row with NAT on, which is what `on:'nat'` asks about."""

    values_by_code = {0x9: True}


qpane._raw_rows = [FakeRow()]
qpane._update_jg_buttons()
ok("...and comes back once the object it reads says so",
   not port_btn.isHidden())
qpane.shutdown()
qpane2 = ListPane("ARP List", path=ARP, on_context_action=lambda *_a: None)
qpane2._menu = arp
qpane2._install_jg_toolbar()
check("a menu with no toolbar grows no buttons", qpane2._jg_buttons, [])
qpane2.shutdown()

# Packages declares Check For Updates and Check Installation with
# `group:'System'` too, but outside a toolbar that names the main-menu folder
# they open from. Winbox v3 draws them as two buttons, not a "System ▾".
PACKAGES = (24, 23)
packages = next(mn for mn in schema.menus if mn.path == PACKAGES)
ppane = ListPane("Package List", path=PACKAGES,
                 on_context_action=lambda *_a: None)
ppane._menu = packages
ppane._install_jg_toolbar()
pbuttons = {b.text(): b for b, _a in ppane._jg_buttons}
check("a window-level entry's group is not a dropdown",
      sorted(pbuttons), ["Check For Updates", "Check Installation"])
ok("...each is a plain button",
   all(b.menu() is None for b in pbuttons.values()))
ppane.shutdown()


# --- the same verbs on the form's button column ------------------------------
# Quick Set is two surfaces in this UI (a pane, and the form its row opens)
# where Winbox has one window, so the toolbar's verbs belong on both.

from x3.proto import message as _m
from x3.proto.device import RowObject as _RowObject
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import PropertyWindow


class FormClient:
    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, values=None, **kw):
        return quickset

    def alloc_from(self):
        return (0, 11)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return sorted((fld.enum or {}).items())

    def interface_choices(self):
        return []

    def query_enum(self, spec, params=None, **kw):
        return {}


qmsg = _m.Message()
qmsg.add_u32(0xFE0001, 1)
qmsg.add_bool(0x9, True)              # NAT on, which is what `on:'nat'` reads
qrow = _RowObject(
    path=QUICK_SET, raw=qmsg, fields=schema.decode(QUICK_SET, qmsg),
    form_fields=schema.decode_form(QUICK_SET, qmsg),
    values_by_code={c: v for c, _t, v in qmsg},
)
form_fired: list[str] = []
form = PropertyWindow(
    "Home AP", QUICK_SET, qrow, client=FormClient(qrow),
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=quickset, row_key="qs", full_row=True,
    on_context_action=lambda act, r, mn: form_fired.append(act["name"]),
)
app.processEvents()
labels = [b.text() for b in form._action_btns]
ok("the form's button column grows the toolbar's verbs",
   "Port Mapping" in labels and "System" in labels, f"{labels}")
ok("...after the standard chrome, not instead of it",
   labels.index("OK") < labels.index("Port Mapping"))
system_btn = next(b for b in form._action_btns if b.text() == "System")
ok("...with the group as one button and a menu", system_btn.menu() is not None)
system_btn.menu().actions()[1].trigger()
check("...that hands the action to the window", form_fired, ["Reboot"])
ok("Apply Configuration is still not a button of its own",
   "Apply Configuration" not in labels)
form._allow_close = True
form.close()


# --- the window it opens ----------------------------------------------------

class StubClient:
    """Enough of a client for a query window: a from-path and start_query."""

    def __init__(self):
        self.started: list[dict] = []

    def alloc_from(self):
        return (0, 9)

    def start_query(self, menu, values, from_path=None):
        self.started.append(values)
        return 7

    def poll_query(self, *a, **kw):
        return []

    def enum_map(self, path):
        return {}


ping_menu = schema.command_menu((8, 2), "query") or next(
    mn for mn in schema.menus if mn.kind == "query" and mn.title == "Ping"
)
by_name = {
    (f.name or "").strip(): c for c, f in ping_menu.request_fields.items()
}
client = StubClient()
win = QueryWindow(
    "Ping", ping_menu, client=client,
    prefill=prefill, autostart=True,
)
app.processEvents()
editor = win._editors.get(by_name.get("Ping To"))
ok("the target window has a Ping To box", isinstance(editor, QLineEdit))
check("...opened with the row's address in it", editor.text(),
      "192.168.88.192")
check("autostart:1 runs it without a click", len(client.started), 1)
ok("...and the address is in what was sent",
   any(v == "192.168.88.192" for v in (client.started[0] or {}).values()),
   f"{client.started[0]}")
win.close()

quiet = QueryWindow("Ping", ping_menu, client=StubClient(), prefill=None)
app.processEvents()
check("no autostart means no run", len(quiet.client.started), 0)
quiet.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall context-action tests passed")
