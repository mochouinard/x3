"""``termopen``, ``hyperlink`` and ``type:filter`` -- the last three.

No device and no pytest required:

    .venv/bin/python test_links_and_filters.py

* **``termopen``** is Tools ▸ Telnet: a dialog with a Type (telnet / ssh / mac
  telnet), an address or a MAC, a port and a user -- and *no handler path*,
  because Winbox makes the connection from the PC itself. It was missing from
  the nav entirely, which also left the right-click Telnet / MAC Telnet entries
  on ARP, Neighbors and Registration pointing at nothing: they name their
  target by title (``open:'Telnet'``). The dialog is now built from the
  schema's own fields, and OK opens the session from the router's console --
  the one console this app has, and the only route to a host only the router
  can reach. The dialog says so.

* **``hyperlink``** (2) is a value that names an object of *another* menu: a
  route's Immediate Gateway is a row of (44,16), its Session a row of (44,33).
  The id is that object's, so the menu it lives in is what can name it -- and
  is worth being able to open.

* **``type:filter``** (24 lists) is the dropdown above a list: Firewall filters
  by Chain, with ``static`` and ``dynamic`` alongside the chains themselves;
  Address Lists by List; Routes by Routing Table. ``filteron`` names the field
  to match and ``conds`` are the entries that are conditions rather than
  values.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.main_window import _termopen_command

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


# --- termopen ---------------------------------------------------------------

def nav_leaves(nodes, kind):
    out = []
    for n in nodes:
        if n.kind == kind:
            out.append(n.title)
        out.extend(nav_leaves(n.children or [], kind))
    return out


check("the Telnet dialog is in the menu tree, where the .jg groups it",
      nav_leaves(schema.nav, "termopen"), ["Telnet"])
telnet = schema.dialogs.get("Telnet")
ok("...with its own field namespace, and no handler path", telnet is not None)
check("...built from the schema rather than by hand",
      sorted({(f.name or "") for f in telnet.fields.values()}),
      ["Address", "MAC Address", "Port", "Src. Address", "Type", "User", "VRF"])
kind_field = next(f for f in telnet.fields.values() if f.name == "Type")
check("...with the Type vocabulary the .jg declares",
      [kind_field.enum[k] for k in sorted(kind_field.enum) if kind_field.enum[k]],
      ["Telnet", "SSH", "MAC Telnet"])

check("Telnet becomes the router's own telnet command",
      _termopen_command({"Type": 1, "Address": "192.168.88.1"}),
      "/system telnet 192.168.88.1")
check("...with a port only when it is not the default",
      [_termopen_command({"Type": 1, "Address": "10.0.0.1", "Port": p})
       for p in ("23", "2323")],
      ["/system telnet 10.0.0.1", "/system telnet 10.0.0.1 port=2323"])
check("SSH is its own command, and takes a user",
      _termopen_command({"Type": 2, "Address": "10.0.0.1", "User": "admin"}),
      "/system ssh 10.0.0.1 user=admin")
check("MAC Telnet goes by MAC address",
      _termopen_command({"Type": 3, "MAC Address": "48:A9:8A:11:22:CC"}),
      "/tool mac-telnet 48:A9:8A:11:22:CC")
check("nothing to connect to is no command at all",
      [_termopen_command({"Type": 1}), _termopen_command({"Type": 3})],
      ["", ""])

# The right-click entries that name it now resolve.
arp = schema.record((20, 8))
entry = next(a for a in arp.context_actions if a["name"] == "MAC Telnet")
check("an ARP row's MAC Telnet entry names this dialog", entry["open"],
      "Telnet")
check("...and carries the row's MAC into it, with the Type as a literal",
      [(l["dst"], l.get("src"), l.get("val")) for l in entry["link"]],
      [("Type", None, 3), ("MAC Address", "MAC Address", None)])


# --- hyperlink --------------------------------------------------------------

routes = next(
    mn for mn in schema.overlay_list
    if any(f.link_path for f in mn.fields.values())
)
links = [
    (f.name, f.link_path, f.enum_path)
    for f in routes.fields.values() if f.link_path
]
check("a route's hyperlinks know the menu they point at", sorted(links),
      [("Immediate Gateway", (44, 16), (44, 16)),
       ("Session", (44, 33), (44, 33))])
gateway = next(f for f in routes.fields.values() if f.name == "Immediate Gateway")
check("...and name their value out of it, like a dynamic enum",
      gateway.render(7, lookup={7: "ether5"}), "ether5")
ok("...while remaining an ordinary row of the form",
   any(i.get("code") == gateway.code for i in routes.form_layout))


# --- list filters -----------------------------------------------------------

FILTER_RULES = (20, 3)
rules = schema.record(FILTER_RULES)
check("a firewall list declares one filter, on Chain",
      [(f["filteron"], f["path"], [c["name"] for c in f["conds"]])
       for f in rules.list_filters],
      [("Chain", (20, 2), ["static", "dynamic"])])
addr = schema.record((20, 35))
check("an address list filters on List, with no conditions",
      [(f["filteron"], f["path"], f["conds"]) for f in addr.list_filters],
      [("List", (20, 34), [])])
check("23 lists in all declare one",
      sum(1 for mn in list(schema.menus) + list(schema.overlay_list)
          if mn.list_filters), 23)

CHAIN = sch.field_code_by_name(rules, "Chain")
ok("the field a firewall list filters on is one of its own", CHAIN is not None)


app = QApplication.instance() or QApplication(sys.argv)


class Session:
    """An edit session that only has to answer for its client."""

    readonly = True

    class client:
        @staticmethod
        def enum_map(path):
            return {1: "input", 2: "forward", 3: "output"}

    def __init__(self):
        pass


def rule_row(chain: str, dynamic: bool = False):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    msg.add_string(CHAIN, chain)
    if dynamic:
        msg.add_bool(sch.FLAG_DYNAMIC_CODE, True)
    return RowObject(
        path=FILTER_RULES, raw=msg, fields=schema.decode(FILTER_RULES, msg),
        form_fields=[], values_by_code={c: v for c, _t, v in msg},
    )


pane = ListPane("Filter Rules", path=FILTER_RULES, edit_session=Session())
pane._menu = rules
pane._install_jg_filters()
check("the pane grows one dropdown for it", len(pane._jg_filters), 1)
combo, spec = pane._jg_filters[0]
labels = [combo.itemText(i) for i in range(combo.count())]
check("...offering everything, then the conditions, then the chains",
      labels, ["all chains", "static", "dynamic", "forward", "input", "output"])
check("...and its default keeps every row", combo.currentData(), None)

rows = [rule_row("input"), rule_row("forward"), rule_row("input", dynamic=True)]
pane._all_rows = [["input"], ["forward"], ["input"]]
pane._all_comments = ["", "", ""]
pane._all_info = [(), (), ()]
pane._raw_rows = rows
pane._columns = ["Chain"]

check("with nothing chosen, every row stays",
      len(pane._view_rows()[0]), 3)
combo.setCurrentIndex(labels.index("input"))
check("choosing a chain keeps only that chain's rows",
      [pane._all_rows[i][0] for i in pane._view_rows()[2]], ["input", "input"])
combo.setCurrentIndex(labels.index("dynamic"))
check("choosing a condition filters by it instead",
      len(pane._view_rows()[2]), 1)
check("...and it is the dynamic row", pane._view_rows()[2], [2])
combo.setCurrentIndex(labels.index("static"))
check("the other condition is its complement",
      pane._view_rows()[2], [0, 1])
combo.setCurrentIndex(0)
check("back to all, every row again", len(pane._view_rows()[0]), 3)
pane.shutdown()

plain = ListPane("Interfaces", path=(20, 0), edit_session=Session())
plain._menu = schema.record((20, 0))
plain._install_jg_filters()
check("a list with no filter declared grows none", plain._jg_filters, [])
plain.shutdown()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall link/filter tests passed")
