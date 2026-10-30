"""Offline tests for the left nav: folders that wrap a single menu.

No device and no pytest required:

    .venv/bin/python test_menu_tree.py

The `.jg` nav describes IP → Addresses as a *folder* holding one map (Address,
[20,1]) — same for ARP, Routes, System → License and 15 others on a 7.21
device. Those used to open a flyout with a single row in it, which reads as a
dead end. These tests pin the collapse: one click on the folder emits the leaf,
the folder is drawn without the `›` caret, and folders that hold real siblings
(or a tab group) still open their flyout.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from x3.ui.widgets.menu_tree import (
    _ROLE_CARET,
    MenuTree,
    _click_target,
    _lone_child,
    _opens_submenu,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


class Nav:
    """Stand-in for x3.proto.schema.NavNode."""

    def __init__(self, title, kind="folder", path=None, children=(), *,
                 node_id=None, icon=None, type_value=None, generic=None,
                 hidden=False):
        self.title = title
        self.kind = kind
        self.path = path
        self.children = list(children)
        self.node_id = node_id or title
        self.icon = icon
        self.type_value = type_value
        self.generic = generic
        self.hidden = hidden


app = QApplication.instance() or QApplication(sys.argv)

from x3.ui.widgets.menu_tree import _payload_from_nav

# IP → Addresses → Address, exactly as a 7.21 device describes it.
addresses = _payload_from_nav(
    Nav("Addresses", children=[Nav("Address", kind="map", path=(20, 1))])
)
# IP → DHCP Client → {DHCP Client, DHCP Client Options}: two maps, so Winbox
# opens them as tabs of one window.
dhcp = _payload_from_nav(
    Nav("DHCP Client", children=[
        Nav("DHCP Client", kind="map", path=(43, 1)),
        Nav("DHCP Client Options", kind="map", path=(43, 5)),
    ])
)
# A folder of folders — the top-level IP entry itself.
ip = _payload_from_nav(Nav("IP", children=[
    Nav("Addresses", children=[Nav("Address", kind="map", path=(20, 1))]),
    Nav("Firewall", children=[Nav("Filter Rules", kind="map", path=(20, 3))]),
]))
# A folder whose one visible map has a hidden sibling. Winbox collapses this
# and reaches the hidden entry from the page that owns it -- IP → Cloud is the
# case that proved it, with Back To Home Users/Files in the Cloud page's
# Configuration panel rather than the tree. So it collapses to the visible map.
with_hidden = _payload_from_nav(Nav("Bridge", children=[
    Nav("Bridge", kind="map", path=(11, 0)),
    Nav("Bridge Port", kind="map", path=(11, 1), hidden=True),
]))

# --- which folders collapse ---------------------------------------------

check("a folder wrapping one map resolves to that map",
      (_lone_child(addresses) or {}).get("path"), (20, 1))
check("a folder that is a tab group does not collapse",
      _lone_child(dhcp), None)
check("a folder of folders does not collapse", _lone_child(ip), None)
check("a leaf is not a collapsible folder",
      _lone_child({"kind": "map", "path": (20, 1)}), None)
check("a hidden sibling does not hold the folder open",
      (_lone_child(with_hidden) or {}).get("path"), (11, 0))

# --- what a click emits --------------------------------------------------

check("the collapsed folder opens the leaf itself",
      _click_target(_lone_child(addresses)),
      addresses["children"][0])
check("a tab-group member opens the group",
      (_click_target(dhcp["children"][0]) or {}).get("kind"), "page_group")
check("the group opens on the page that was clicked",
      (_click_target(dhcp["children"][1]) or {}).get("active_path"), (43, 5))
check("a folder is not itself clickable",
      _click_target({"kind": "folder", "children": []}), None)

# --- the tree emits it in one click --------------------------------------

tree = MenuTree()
tree.load_nav([
    Nav("Addresses", children=[Nav("Address", kind="map", path=(20, 1))]),
    Nav("IP", children=[
        Nav("Addresses", children=[Nav("Address", kind="map", path=(20, 1))]),
        Nav("Firewall", children=[Nav("Filter Rules", kind="map", path=(20, 3))]),
    ]),
])

emitted: list[object] = []
tree.menu_activated.connect(emitted.append)

lone_item = tree.topLevelItem(0)
folder_item = tree.topLevelItem(1)

check("a collapsed folder loses the caret", lone_item.text(0), "Addresses")
check("...and its row is not flagged for one",
      bool(lone_item.data(0, _ROLE_CARET)), False)
# The caret is painted right-aligned by _CaretDelegate, so the label itself
# stays clean and only the flag says whether an arrow is drawn.
check("a real folder keeps its plain label", folder_item.text(0), "IP")
check("...and is flagged for the caret",
      bool(folder_item.data(0, _ROLE_CARET)), True)

tree._handle(lone_item)
check("clicking it emits the leaf, not a flyout", len(emitted), 1)
check("and the leaf is the map behind it",
      (emitted[0] or {}).get("path") if emitted else None, (20, 1))

emitted.clear()
tree._handle(folder_item)
check("clicking a real folder emits nothing (it opens the flyout)",
      len(emitted), 0)

# --- and the flyout does too ---------------------------------------------

chosen: list[object] = []
flyout = tree._flyout
assert flyout is not None, "clicking a folder should have built the flyout"
flyout.item_chosen.connect(chosen.append)

first = flyout._list.item(0)
check("the flyout row for a collapsed folder loses the caret too",
      first.text(), "Addresses")
check("...and carries no caret flag",
      bool(first.data(_ROLE_CARET)), False)
flyout._on_click(first)
check("choosing it emits the leaf", len(chosen), 1)
check("and the leaf is the map behind it",
      (chosen[0] or {}).get("path") if chosen else None, (20, 1))

tree.close()

print()

# --- a folder whose only visible child is a page opens that page -----------
#
# IP → Cloud wraps one visible item plus two `hide:1` siblings (Back To Home
# Users / Files). Winbox reaches those from the Cloud page's own Configuration
# panel, never from the tree — so counting them made Cloud look like a branch
# and clicking it opened a flyout instead of the page.

from x3.ui.widgets.menu_tree import _click_target, _lone_child, visible_children

cloud = {
    "kind": "folder", "title": "Cloud", "path": None, "page_group": None,
    "children": [
        {"kind": "item", "title": "Cloud", "path": (123, 1), "hidden": False},
        {"kind": "map", "title": "Back To Home Users", "path": (156, 1), "hidden": True},
        {"kind": "map", "title": "Back To Home Files", "path": (156, 2), "hidden": True},
    ],
}

check("hidden children are not navigation",
      [c["title"] for c in visible_children(cloud)], ["Cloud"])
lone = _lone_child(cloud)
check("...so the folder stands for its one visible page",
      (lone or {}).get("path"), (123, 1))
check("...and clicking it opens that page",
      (_click_target(lone) or {}).get("kind"), "item")

# A folder with two visible children is still a real branch.
two = {
    "kind": "folder", "title": "Firewall", "path": None, "page_group": None,
    "children": [
        {"kind": "map", "title": "Filter Rules", "path": (20, 3), "hidden": False},
        {"kind": "map", "title": "NAT", "path": (20, 5), "hidden": False},
    ],
}
check("a folder with real siblings is left alone", _lone_child(two), None)

# --- who gets an arrow ----------------------------------------------------
#
# Winbox v3 marks only the rows that open a submenu. A folder that collapses to
# one menu, or one whose maps open as tabs of a single window, opens a window.

check("a real branch opens a submenu", _opens_submenu(ip), True)
check("a collapsed folder does not", _opens_submenu(addresses), False)
check("a tab-group folder does not", _opens_submenu(dhcp), False)
check("a leaf does not", _opens_submenu(addresses["children"][0]), False)
check("a childless folder does not",
      _opens_submenu({"kind": "folder", "children": []}), False)

# All children hidden: keep the folder rather than make it a dead end.
allhidden = {
    "kind": "folder", "title": "Odd", "path": None, "page_group": None,
    "children": [{"kind": "map", "title": "X", "path": (1, 1), "hidden": True}],
}
check("a folder of only hidden children does not collapse",
      _lone_child(allhidden), None)

# --- the nav strip is chrome, not a sunken data well ----------------------
#
# The nav used to inherit the generic tree QSS: a white field inside a 1px
# border, which read as a hole cut into the window. It carries an object name
# now so both stylesheets can paint it in the frame grey with no border.

from PySide6.QtWidgets import QFrame  # noqa: E402

from x3.ui import style  # noqa: E402

chrome = MenuTree()
check("the nav is addressable by name", chrome.objectName(), "navTree")
check("...and draws no frame of its own",
      chrome.frameShape(), QFrame.Shape.NoFrame)
chrome.close()

for theme in style.list_themes():
    qss = style.stylesheet_for(theme)
    rule = qss.split("QTreeWidget#navTree {", 1)[-1].split("}", 1)[0]
    ok = f"background: {theme.bg};" in rule and "border: none;" in rule
    check(f"{theme.id}: nav painted in the frame grey, unbordered", ok, True)

check("Tools ▸ Telnet is something a click opens",
      (_click_target({"kind": "termopen", "title": "Telnet"}) or {}).get("kind"),
      "termopen")

# --- the flyout answers the keyboard ----------------------------------------
# Typing in a submenu moves its current row; Enter used to do nothing, because
# only a mouse click opened anything.

from PySide6.QtTest import QTest  # noqa: E402

from x3.ui.widgets.menu_tree import NavFlyout  # noqa: E402

fly = NavFlyout()
chosen: list = []
fly.item_chosen.connect(chosen.append)
fly.show_items("IP", ip["children"], fly.pos())
app.processEvents()
QTest.keyClicks(fly._list, "Add")
check("typing picks the row it spells", fly._list.currentItem().text(), "Addresses")
QTest.keyClick(fly._list, Qt.Key.Key_Return)
app.processEvents()
check("Enter opens it", [c.get("path") for c in chosen], [(20, 1)])
fly.hide()

print()

if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all menu-tree tests passed")
