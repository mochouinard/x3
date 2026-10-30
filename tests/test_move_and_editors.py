"""Reordering a rule, and number fields the device sends as "unset".

No device and no pytest required:

    .venv/bin/python test_move_and_editors.py

Two faults found driving the firewall UI:

* **Dragging a rule did nothing.** Qt checks the *model's* item flags before it
  will start a drag or accept a drop, so a view configured for InternalMove
  against a model that advertises neither is silently inert — no indicator, no
  drop event.
* **IP ▸ Services would not open at all.** Its rows carry ``Parent =
  0xFFFFFFFF``, the router's "unset" sentinel, which does not fit a QSpinBox's
  signed 32-bit range. setValue() raised OverflowError while building the form,
  so double-clicking a service did nothing whatsoever.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox

from x3.ui.models.table_model import SimpleTableModel
from x3.ui.widgets.list_pane import move_anchor

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


app = QApplication.instance() or QApplication(sys.argv)

# --- where a MOVE anchors -------------------------------------------------
#
# MOVE says "put this one ahead of that one", so the anchor is whatever ends
# up at the target once the moved row is taken out.

IDS = ["A", "B", "C", "D"]

check("moving the last rule to the top anchors on the old first",
      move_anchor(IDS, 3, 0), "A")
check("moving the first rule down anchors on what follows it",
      move_anchor(IDS, 0, 2), "D")
check("moving one place down", move_anchor(IDS, 0, 1), "C")
check("moving to the end has no anchor at all", move_anchor(IDS, 0, 3), None)
check("...as does a target past the end", move_anchor(IDS, 1, 99), None)

# --- the router keeps its own rules at the top ---------------------------
#
# The router refuses to insert ahead of its own rules — "cannot move builtin" —
# and nothing on the wire marks them beyond the dynamic flag. So the highest
# position a rule can take is the first row that is not dynamic, and a move
# aimed above that lands there instead of being sent and rejected.

from x3.proto.schema import FLAG_DYNAMIC_CODE
from x3.ui.widgets.list_pane import ListPane


class Row:
    def __init__(self, oid, dynamic=False):
        self.values_by_code = {FLAG_DYNAMIC_CODE: dynamic}
        self._oid = oid

    def object_id(self):
        return self._oid


class FakePane:
    """Just the two methods under test, over a fixed row list."""

    _is_dynamic_row = ListPane._is_dynamic_row
    _first_movable_index = ListPane._first_movable_index

    def __init__(self, rows):
        self._raw_rows = rows


two_dynamic = FakePane([Row(1, True), Row(2, True), Row(3), Row(4)])
check("the boundary sits below the dynamic rules",
      two_dynamic._first_movable_index(), 2)
check("a dynamic row is recognised", two_dynamic._is_dynamic_row(0), True)
check("...and a static one is not", two_dynamic._is_dynamic_row(2), False)

check("no dynamic rules means the top is free",
      FakePane([Row(1), Row(2)])._first_movable_index(), 0)
check("all dynamic leaves nowhere to move to",
      FakePane([Row(1, True)])._first_movable_index(), 1)
check("an empty list has no boundary", FakePane([])._first_movable_index(), 0)

# Dragging the last rule to the very top, with two dynamic rules above,
# lands at position 2 — so it is inserted ahead of whatever sits there once
# the moved row is taken out, which is id 3.
ids = [1, 2, 3, 4]
check("a move clamped to the boundary anchors on the row it lands above",
      move_anchor(ids, 3, max(0, 2)), 3)

# --- the model has to advertise dragging ---------------------------------

model = SimpleTableModel(["#", "Chain"], [["0", "input"], ["1", "forward"]])
idx = model.index(0, 0)

flags = model.flags(idx)
ok("a plain list is not draggable",
   not (flags & Qt.ItemFlag.ItemIsDragEnabled))

model.set_row_drag_enabled(True)
flags = model.flags(idx)
ok("an ordered list advertises drag", bool(flags & Qt.ItemFlag.ItemIsDragEnabled))
ok("...and drop", bool(flags & Qt.ItemFlag.ItemIsDropEnabled))
ok("...and is still selectable", bool(flags & Qt.ItemFlag.ItemIsSelectable))
ok("empty space below the rows accepts a drop, so a rule can go last",
   bool(model.flags(QModelIndex()) & Qt.ItemFlag.ItemIsDropEnabled))
ok("moves are a supported action",
   bool(model.supportedDropActions() & Qt.DropAction.MoveAction))

model.set_row_drag_enabled(False)
ok("turning it off puts the flags back",
   not (model.flags(idx) & Qt.ItemFlag.ItemIsDragEnabled))

# --- a number the device marks "unset" must not break the form -----------

from x3.proto.schema import Schema
from x3.ui.widgets.property_window import PropertyWindow

schema = Schema.load("7.21.1-151146055")
services = next(m for m in schema.menus if m.path == (68, 1) and m.kind == "map")
port_fld = next(f for f in services.fields.values() if f.name == "Port")

make_editor = PropertyWindow._make_editor


class FakeForm:
    """The bits of PropertyWindow that _make_editor reaches for."""

    _show_pw = False
    client = None
    menu = None
    # Enum narrowing (a field's `enumfilter`) is part of building any editor
    # with choices; borrow the real one rather than stubbing it out.
    _filter_enum_choices = PropertyWindow._filter_enum_choices


def editor_for(fld, raw, rendered=""):
    return make_editor(
        FakeForm(), {"field": fld, "raw": raw, "rendered": rendered}, False
    )

ok("an ordinary number is a spin box",
   isinstance(editor_for(port_fld, 8291), QSpinBox))
check("...holding its value", editor_for(port_fld, 8291).value(), 8291)

# 0xFFFFFFFF — exactly what IP ▸ Services sends for its Parent field.
w = editor_for(port_fld, 4294967295, rendered="")
ok("the unset sentinel does not blow up the form", w is not None)
ok("...and is not shown as a number", not isinstance(w, QSpinBox))
check("...it reads as blank", w.text() if isinstance(w, QLineEdit) else None, "")

ok("a value just inside the range still spins",
   isinstance(editor_for(port_fld, 2147483647), QSpinBox))
ok("one just outside does not",
   not isinstance(editor_for(port_fld, 2147483648), QSpinBox))

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all move/editor tests passed")
