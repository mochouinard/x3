"""Per-tab toolbars: every tab of a window gets its own buttons.

No device and no pytest required:

    .venv/bin/python test_tab_toolbars.py

Winbox v3 does not give a window one toolbar -- it gives every tab its own.
Interfaces ▸ Interface has Detect Internet, Ethernet has Power Cycle, Bonding
has Monitor Slaves, LTE has Modem and Antenna Scan, and none of them shows up
on any other tab. The whole association is in the `.jg`, in two shapes, and
both are pinned here:

* order. A folder's `item` forms and its `doit`/`action`/`query` commands
  belong to the map they are declared *after*; the ones declared before the
  first map belong to the window itself. The window is the folder's *first*
  child, so when that child is an `item` the window is that form and its
  siblings are the buttons on it -- System ▸ Resources is the Resources form
  with Hardware / CPU / IRQ beside it, IP ▸ DNS the Settings form with Static /
  Cache / Adlist / Forwarders. Nothing in the nodes flags which child is the
  window; the order is the flag.
* a `type:contextmenu` with no `link:`. It carries nothing from a row, which is
  what makes it a window-level button rather than a right-click entry: Interface
  List ▸ Lists, VXLAN ▸ VTEP and FDB, MACsec ▸ Profiles.

`hide:1` siblings are deliberately *not* toolbar buttons in their own right --
each is named by the contextmenu of the tab that owns it -- which is why VTEP
sits on VXLAN and not on the tab it happens to follow.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QToolButton

from x3.proto import message as m
from x3.proto.device import RowObject
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

from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.list_window import ListWindow
from x3.ui.widgets.main_window import MainWindow
from x3.ui.widgets.menu_tree import _payload_from_nav
from x3.ui.widgets.multi_page_window import MultiPageListWindow

schema = Schema.load(SCHEMA_DIR)
IFACE = (20, 0)


def folder(title: str):
    stack = list(schema.nav)
    while stack:
        node = stack.pop(0)
        if node.title == title and node.kind == "folder":
            return node
        stack.extend(node.children or [])
    raise AssertionError(f"no nav folder titled {title!r}")


def pages(title: str) -> list[dict]:
    return _payload_from_nav(folder(title))["page_group"]["pages"]


def acts(page: dict) -> list[str]:
    return [a["title"] for a in page.get("actions") or []]


def page_of(group: list[dict], title: str) -> dict:
    return next(p for p in group if p["title"] == title)


# --- the schema says which tab owns which button ----------------------------

iface = pages("Interfaces")

check("the tab a form is declared under is the tab that offers it",
      acts(page_of(iface, "Interface")), ["Detect Internet"])
check("a command belongs to its tab too", acts(page_of(iface, "Ethernet")),
      ["Power Cycle"])
check("...as does a query", acts(page_of(iface, "Bonding")),
      ["Monitor Slaves"])
check("a tab with several keeps them in .jg order",
      acts(page_of(iface, "LTE")),
      ["LTE Cell Monitor", "LTE Scanner", "Modem", "Antenna Scan"])
ok("and no other tab shows them", all(
    not acts(page_of(iface, t))
    for t in ("Interface List", "VLAN", "VXLAN", "VRRP", "MACsec")
))
ok("a hide:1 sibling is not a button of the tab it follows",
   "Detect Internet State" not in acts(page_of(iface, "Interface"))
   and "VTEP" not in acts(page_of(iface, "VXLAN")))

# What the button opens is the menu itself, so one dispatch serves all kinds.
check("a button carries the menu it opens",
      [(a["kind"], tuple(a["path"]))
       for a in page_of(iface, "LTE")["actions"]],
      [("query", (20, 61)), ("query", (20, 61)),
       ("item", (20, 107)), ("action", (20, 106))])

# Declared before the first tab: the window's own, and the window is the form
# they were declared on -- not a button repeated over every tab.
dns = pages("DNS")
check("a folder's first child is the window it opens",
      (dns[0]["kind"], dns[0]["title"]), ("item", "Settings"))
check("...and carries what was declared with it", acts(dns[0]), ["Settings"])
ok("no sibling repeats it", all(not acts(p) for p in dns[1:]))

res = pages("Resources")
check("System ▸ Resources opens the Resources form, not the Hardware list",
      [(p["kind"], p["title"]) for p in res],
      [("item", "Resources"), ("map", "Hardware"), ("map", "CPU"),
       ("map", "IRQ"), ("map", "RPS")])


# What the form window makes of that group: the siblings become its buttons,
# in .jg order, and the leading item is not one of them (it is the window).
check("the form's buttons are its siblings",
      [b["title"] for b in MainWindow._form_group_buttons(res)],
      ["Hardware", "CPU", "IRQ", "RPS"])
check("...each carrying the menu it opens",
      [(b["kind"], tuple(b["path"]))
       for b in MainWindow._form_group_buttons(res)],
      [("map", (6, 2)), ("map", (24, 2)), ("map", (20, 57)), ("map", (20, 58))])
# RouterBOARD is the same shape with a command in the middle of it, and with
# `hide:1` siblings (CPU Frequency, Boot Devices) that are not buttons at all --
# the form reaches those through a dropdown of its own.
check("a command declared among them is a button too, and only once",
      [b["title"] for b in MainWindow._form_group_buttons(pages("RouterBOARD"))],
      ["Settings", "PoE Settings", "USB", "Mode Button", "Reset Button",
       "USB Power Reset"])

# The window's title: the .jg names the folder and the form apart ("DNS" +
# "Settings"), and v3 shows them joined -- but never doubled.
check("a generic form title is qualified by its folder",
      MainWindow._form_group_title("DNS", "Settings"), "DNS Settings")
check("...and one that already says it is left alone",
      [MainWindow._form_group_title(g, t) for g, t in
       (("Resources", "Resources"), ("Web Proxy", "Web Proxy Settings"))],
      ["Resources", "Web Proxy Settings"])

# --- the window puts them where the schema says ----------------------------

def fetcher(path, type_value=None, family=None, menu_name=None):
    def fetch():
        return (None, [], [], [], None)
    return fetch


def buttons(pane) -> list[str]:
    return [b.text() for b in pane._bar.findChildren(QToolButton) if b.text()]


opened: list[dict] = []
win = MultiPageListWindow(
    "Interfaces", iface, make_fetcher=fetcher,
    on_open_action=opened.append,
)
by_title = {win.tabs.tabText(i): i for i in range(win.tabs.count())}
ok("an 'item' form is not a tab", "Detect Internet" not in by_title)

for tab, want in (("Interface", "Detect Internet"),
                  ("Ethernet", "Power Cycle"),
                  ("LTE", "Modem")):
    win.tabs.setCurrentIndex(by_title[tab])
    pane = win.active_pane()
    ok(f"{tab} has its own button", want in buttons(pane))
    ok(f"...and not another tab's",
       not [b for b in ("Detect Internet", "Power Cycle", "Modem")
            if b != want and b in buttons(pane)])

win.tabs.setCurrentIndex(by_title["LTE"])
lte = buttons(win.active_pane())
check("a tab's buttons read in the order the schema declares them",
      [b for b in lte if b in ("LTE Cell Monitor", "LTE Scanner", "Modem",
                               "Antenna Scan")],
      ["LTE Cell Monitor", "LTE Scanner", "Modem", "Antenna Scan"])

win.tabs.setCurrentIndex(by_title["Ethernet"])
pane = win.active_pane()
next(b for b in pane._bar.findChildren(QToolButton)
     if b.text() == "Power Cycle").click()
check("pressing a button dispatches the menu behind it",
      [(o["kind"], tuple(o["path"]), o["title"]) for o in opened],
      [("doit", (20, 0), "Power Cycle")])
win.close()

# A tab opened in a window of its own keeps the buttons it had.
lone = ListWindow(
    "Ethernet", path=IFACE, type_value=1,
    on_open_action=opened.append,
    actions=page_of(iface, "Ethernet")["actions"],
)
ok("a tab in its own window keeps its buttons",
   "Power Cycle" in buttons(lone.pane))
lone.close()


# One openable child is the same rule with nothing to choose between: the
# folder opens that child, so what it declared alongside is on that window.


def only_child(title: str) -> tuple:
    kids = [
        c for c in _payload_from_nav(folder(title))["children"]
        if c.get("path") is not None and not c.get("hidden")
    ]
    assert len(kids) == 1, f"{title}: {len(kids)} openable children"
    return kids[0]["title"], [a["title"] for a in kids[0]["actions"]]

check("a folder's one child is handed the folder's own buttons",
      only_child("Email"), ("Email Settings", ["Send Email"]))
check("...and a command among several is still just the command",
      only_child("License"), ("License", ["Renew License"]))
check("a button that would reopen the window is not one",
      only_child("Note"), ("System Note", []))


# --- which window the group opens -------------------------------------------
# The decision is _open_page_group's, and it is made on the first page's kind
# alone. Only that decision is exercised here: a real MainWindow needs a
# session and a device (see test_menu_usage), and the three branches are
# distinguishable without one.


class Router:
    """Just enough MainWindow to watch _open_page_group choose a window."""

    _open_page_group = MainWindow._open_page_group
    _is_quickset = MainWindow._is_quickset
    _page_first = staticmethod(MainWindow._page_first)
    _form_group_title = staticmethod(MainWindow._form_group_title)
    _form_group_buttons = staticmethod(MainWindow._form_group_buttons)

    client = object()            # connected: the branches all need a device

    def __init__(self):
        self.chose: list[tuple] = []
        self._open: dict = {}       # the window registry _focus_existing reads

    def _open_quickset(self, payload):
        self.chose.append(("quickset", payload.get("title")))

    def _open_item_dialog(self, path, title, buttons=None, *, name=None):
        self.chose.append(("form", title, [b["title"] for b in buttons or []]))

    def _focus_existing(self, key):     # the tabbed branch gets this far
        self.chose.append(("tabs", key))
        return True


def chosen(title: str, group_pages: list[dict]) -> tuple:
    router = Router()
    router._open_page_group({"kind": "page_group", "title": title,
                             "id": f"group:{title}", "pages": group_pages})
    return router.chose[-1]

check("an item first opens that form, with its siblings on it",
      chosen("Resources", res),
      ("form", "Resources", ["Hardware", "CPU", "IRQ", "RPS"]))
check("...and the folder's name qualifies a generic one",
      chosen("DNS", dns), ("form", "DNS Settings",
                           ["Static", "Cache", "Adlist", "Forwarders"]))
check("a map first still opens the tabbed list window",
      chosen("Interfaces", iface), ("tabs", "group:Interfaces"))
check("and Quick Set is still its own window",
      chosen("Quick Set", pages("Quick Set"))[0], "quickset")


# --- the form window shows the buttons the group gives it -------------------
# Winbox v3's System ▸ Resources: the readings on the left, Hardware / CPU / IRQ
# down the right. Each is a menu, so pressing one dispatches it like a nav click.

from x3.proto.device import RowObject as _RowObject
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import PropertyWindow

RESOURCES = (24, 2)
res_menu = schema.record(RESOURCES)


class FormClient:
    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, values=None, **kw):
        return res_menu

    def alloc_from(self):
        return (0, 14)

    def get(self, path, **kw):
        return self._row

    def getobj(self, path, obj_id, **kw):
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


res_row = _RowObject(path=RESOURCES, raw=m.Message(), fields=[], form_fields=[],
                     values_by_code={})
dispatched: list[tuple] = []
form = PropertyWindow(
    "Resources", RESOURCES, res_row, client=FormClient(res_row),
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=res_menu, full_row=True,
    menu_buttons=MainWindow._form_group_buttons(res),
    on_open_menu=lambda pl: dispatched.append((pl["kind"], tuple(pl["path"]))),
)
app.processEvents()
labels = [b.text() for b in form._action_btns]
check("the form column carries one button per sibling menu",
      [b for b in labels if b in ("Hardware", "CPU", "IRQ", "RPS")],
      ["Hardware", "CPU", "IRQ", "RPS"])
ok("...after the standard chrome, not instead of it",
   labels.index("OK") < labels.index("Hardware"))
next(b for b in form._action_btns if b.text() == "IRQ").click()
check("pressing one opens the menu behind it", dispatched, [("map", (20, 57))])
form.close()


# --- a menu's own related-list button (contextmenu with no link) ------------

MEMBER = (20, 91)
member = schema.record(MEMBER)
ok("Interface List declares its Lists button", any(
    a.get("name") == "Lists" and not a.get("link")
    for a in member.context_actions
))

row = RowObject(path=MEMBER, raw=m.Message(), fields=[], form_fields=[],
                values_by_code={})
fired: list[tuple] = []
pane = ListPane("Interface List", path=MEMBER,
                on_context_action=lambda a, r, mn: fired.append((a["name"], r)))
pane._on_loaded((member, ["List", "Interface"], [["l1", "ether1"]], [row], None))
ok("...and the pane makes it a button", "Lists" in buttons(pane))
rows_menu = pane.row_menu_for(row)
ok("...which is not also a right-click entry on a row",
   rows_menu is None
   or "Lists" not in [a.text() for a in rows_menu.actions()])
next(b for b in pane._bar.findChildren(QToolButton)
     if b.text() == "Lists").click()
check("pressing it runs the action with no row to carry", fired,
      [("Lists", row)])
pane.shutdown()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print("  - " + f)
    raise SystemExit(1)
print("all per-tab toolbar tests passed")
