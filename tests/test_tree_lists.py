"""``treeon`` -- the lists the .jg says are trees.

No device and no pytest required:

    .venv/bin/python test_tree_lists.py

Twenty menus declare ``treeon``, naming the field that holds each row's parent:
a VLAN's `Interface`, a bridge port's `Parent`, a package's `bundle`, a file's
`container`, a queue's `Parent`. Only interfaces were nested, by a hardcoded
field code and a `generic == 'iface'` gate.

Two things had to be read rather than assumed:

* **What a row is called.** The parent link points at the other row's *own*
  name, which is ``nameval``'s field -- Interface for a bridge port, List for
  an address list, Name for a queue -- not at a column called "Name".

* **How the link is written.** A VLAN's `Interface` is the parent's *name*; a
  bridge port's `Parent` is the parent row's object *id*, since it is a plain
  `number`. The .jg does not say which, so each row is joined by id first and
  by name second, and whichever hits is the parent.

The interface family list keeps working throughout: the base Interface map
declares no `treeon` (every sub-interface *type* declares one), so a list of
mixed types falls back to the parent field by code.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import schema as sch
from x3.proto.schema import Schema
from x3.ui.models.table_model import (
    TREE_COLLAPSED,
    TREE_EXPANDED,
    TREE_LEAF,
)
from x3.ui.widgets.list_pane import ListPane

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
PORTS = (16, 2)
ports = schema.record(PORTS)
PORT_IFACE, PORT_PARENT = 0x3, 0x5


# --- what the schema says ---------------------------------------------------

check("a bridge port list is a tree on Parent", ports.tree_on, "Parent")
check("...and a row is called by its Interface", ports.nameval, "Interface")
check("a VLAN nests on its Interface",
      schema.record_for((20, 0), 30).tree_on, "Interface")
ok("...while the base Interface map declares no tree of its own",
   schema.record((20, 0)).tree_on is None)
check("a package nests on its bundle", schema.record((24, 23)).tree_on,
      "bundle")
check("twenty menus declare one",
      sum(1 for mn in list(schema.menus) + list(schema.overlay_list)
          if mn.tree_on), 20)


# --- the pane ---------------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)


class Row:
    """Just what _apply_tree_order reads off a row."""

    def __init__(self, oid, values, rendered=None):
        self._oid = oid
        self.values_by_code = dict(values)
        self._rendered = dict(rendered or {})

    def object_id(self):
        return self._oid

    def get_rendered(self, code, prefer_name=None):
        if code in self._rendered:
            return self._rendered[code]
        value = self.values_by_code.get(code)
        return "" if value is None else str(value)


class Session:
    readonly = True
    client = None


def tree_of(pane, cols, rows, raws):
    pane._all_rows = [list(r) for r in rows]
    pane._all_comments = [""] * len(rows)
    pane._all_info = [()] * len(rows)
    pane._raw_rows = list(raws)
    pane._columns = list(cols)
    out_rows, out_comments, out_sources = pane._apply_tree_order(
        list(cols), [list(r) for r in rows], [""] * len(rows),
        list(range(len(rows))),
    )
    return out_sources, dict(pane._pending_tree_meta)


# A bridge port list: the child's Parent is the parent row's object id.
pane = ListPane("Ports", path=PORTS, edit_session=Session())
pane._menu = ports
cols = ["", "Interface", "Bridge"]
raws = [
    Row(11, {PORT_IFACE: 1, PORT_PARENT: 0}, {PORT_IFACE: "ether1"}),
    Row(12, {PORT_IFACE: 2, PORT_PARENT: 11}, {PORT_IFACE: "ether2"}),
    Row(13, {PORT_IFACE: 3, PORT_PARENT: 0}, {PORT_IFACE: "ether3"}),
]
rows = [["", "ether1", "bridge1"], ["", "ether2", "bridge1"],
        ["", "ether3", "bridge1"]]
sources, meta = tree_of(pane, cols, rows, raws)
check("a child follows its parent", sources, [0, 1, 2])
check("...indented under it", [meta[i][0] for i in range(3)], [0, 1, 0])
check("...with the parent drawn as expandable",
      [meta[i][1] for i in range(3)],
      [TREE_EXPANDED, TREE_LEAF, TREE_LEAF])
check("the indent goes on the column the row is named by",
      pane._tree_label_column(cols, ports), 1)

# Collapsing the parent hides the subtree, keeping the parent's own row.
pane._collapsed = {"ether1"}
sources, meta = tree_of(pane, cols, rows, raws)
check("a collapsed parent keeps its row", len(sources), 2)
check("...and hides its child", sources, [0, 2])
check("...and says it is collapsed", meta[0][1], TREE_COLLAPSED)
pane._collapsed = set()

# A parent id nothing answers for leaves the row at the top level.
orphan = [
    Row(11, {PORT_IFACE: 1, PORT_PARENT: 0}, {PORT_IFACE: "ether1"}),
    Row(12, {PORT_IFACE: 2, PORT_PARENT: 999}, {PORT_IFACE: "ether2"}),
]
sources, meta = tree_of(pane, cols, rows[:2], orphan)
check("an unmatched parent leaves the list flat", meta, {})
check("...with every row still there", sources, [0, 1])
pane.shutdown()

# An interface family list: the link is the parent's *name*, and the menu it
# is shown through declares no treeon.
iface = schema.record((20, 0))
IFACE_PARENT = 0x7D2
pane2 = ListPane("Interface", path=(20, 0), edit_session=Session())
pane2._menu = iface
cols2 = ["", "Name", "Type"]
raws2 = [
    Row(1, {}, {IFACE_PARENT: ""}),
    Row(2, {}, {IFACE_PARENT: "ether1"}),
]
rows2 = [["", "ether1", "Ethernet"], ["", "vlan10", "VLAN"]]
sources2, meta2 = tree_of(pane2, cols2, rows2, raws2)
check("a VLAN nests under the interface it names",
      [meta2[i][0] for i in range(2)], [0, 1])
pane2.shutdown()

# A list that is neither a tree nor an interface list is left alone.
rules = schema.record((20, 3))
pane3 = ListPane("Filter Rules", path=(20, 3), edit_session=Session())
pane3._menu = rules
sources3, meta3 = tree_of(
    pane3, ["", "Chain"], [["", "input"], ["", "forward"]],
    [Row(1, {}), Row(2, {})],
)
check("a plain list is not turned into a tree", meta3, {})
check("...and its rows are untouched", sources3, [0, 1])
pane3.shutdown()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall tree-list tests passed")
