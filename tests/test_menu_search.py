"""Find any menu entry by name: the toolbar box, Ctrl+K, or typing in the nav.

No device and no pytest required:

    .venv/bin/python test_menu_search.py

The nav is a flyout menu, and the tree's own type-ahead only reaches its top
level: "serv" found nothing, because IP ▸ Services is one flyout down.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


class Nav:
    """Stand-in for x3.proto.schema.NavNode."""

    def __init__(self, title, kind="folder", path=None, children=(), hidden=False):
        self.title = title
        self.kind = kind
        self.path = path
        self.children = list(children)
        self.node_id = title
        self.icon = None
        self.type_value = None
        self.generic = None
        self.hidden = hidden


app = QApplication.instance() or QApplication(sys.argv)

from x3.ui.widgets.menu_search import MenuSearch, menu_entries, rank  # noqa: E402
from x3.ui.widgets.menu_tree import MenuTree, _payload_from_nav  # noqa: E402

roots = [_payload_from_nav(n) for n in (
    Nav("IP", children=[
        Nav("Addresses", children=[Nav("Address", kind="map", path=(20, 1))]),
        Nav("Services", children=[Nav("Services", kind="map", path=(19, 1))]),
        Nav("DHCP Client", children=[
            Nav("DHCP Client", kind="map", path=(43, 1)),
            Nav("DHCP Client Options", kind="map", path=(43, 5)),
        ]),
    ]),
    Nav("Dot1X", children=[
        Nav("Client", kind="map", path=(90, 1)),
        Nav("Server", kind="map", path=(90, 2)),
    ]),
    Nav("Log", kind="map", path=(3, 4)),
)]

entries = menu_entries(roots)
names = [" ▸ ".join(p) for p, _t in entries]
check("a nested entry is found, not only the top level",
      "IP ▸ Services" in names, True)
check("a one-list folder is the list it opens, listed once",
      names.count("IP ▸ Addresses"), 1)
check("a tab of a group is an entry of its own",
      "IP ▸ DHCP Client ▸ DHCP Client Options" in names, True)

hits = [" ▸ ".join(p) for p, _t in rank(entries, "dhcp opt")]
check("every word typed has to match, anywhere in the path",
      hits, ["IP ▸ DHCP Client ▸ DHCP Client Options"])
check("a name starting with the query comes first",
      [" ▸ ".join(p) for p, _t in rank(entries, "serv")][:2],
      ["Dot1X ▸ Server", "IP ▸ Services"])
services_id = next(t.get("id") for p, t in entries if p == ["IP", "Services"])
check("...unless the other is the one used most",
      [" ▸ ".join(p) for p, _t in rank(entries, "serv", {services_id: 5})][0],
      "IP ▸ Services")
check("nothing typed, nothing listed", rank(entries, "  "), [])

# --- the box -----------------------------------------------------------------

host = QWidget()
host.resize(800, 500)
box = MenuSearch(lambda: roots, host=host, parent=host)
host.show()
opened: list = []
box.chosen.connect(opened.append)
box.start("s")
box.start("e")
box.start("r")
check("keys handed over one by one build the word", box.text(), "ser")
check("...and list what it matches", box._list.isVisible(), True)
QTest.keyClick(box, Qt.Key.Key_Down)
QTest.keyClick(box, Qt.Key.Key_Return)
check("Enter opens the highlighted entry",
      [o.get("path") for o in opened], [(19, 1)])
check("...and the box is ready for the next search",
      (box.text(), box._list.isVisible()), ("", False))
box.start("log")
QTest.keyClick(box, Qt.Key.Key_Escape)
check("Esc gives up without opening anything", (len(opened), box.text()), (1, ""))

# --- typing in the nav -------------------------------------------------------

tree = MenuTree()
handed: list[str] = []
tree.type_ahead.connect(handed.append)
tree.search_takes_typing = True
QTest.keyClicks(tree, "se")
check("a letter typed in the nav goes to the search", handed, ["s", "e"])
QTest.keyClick(tree, Qt.Key.Key_Down)
check("...while the arrows still move in the tree", handed, ["s", "e"])
plain = MenuTree()
plain_handed: list[str] = []
plain.type_ahead.connect(plain_handed.append)
QTest.keyClicks(plain, "s")
check("a tree nobody searches for keeps its own type-ahead", plain_handed, [])

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all menu-search tests passed")
