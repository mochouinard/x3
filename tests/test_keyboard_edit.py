"""Interface ▸ ether1 ▸ Enter, and a change made without touching the mouse.

No device and no pytest required:

    .venv/bin/python test_keyboard_edit.py

`test_keyboard_nav.py` covers getting *to* a row (arrows survive a poll, Enter
opens it). This is the other half: once the form is open, every step of the
change is a key.

* **The form opens ready to type.** Qt hands a fresh tab widget's keyboard to
  the tab strip, which is the one part of a property form nobody opened it to
  change; and the scroll area each page sits in was a tab stop of its own, so
  Tab out of the last field paused on an empty scroller. The form lands on the
  first editable row instead, and the chain from there is fields, then buttons.
* **The tabs are keys.** Ctrl+PageDown/PageUp walks them (Ctrl+Tab is not ours:
  QMdiArea cycles windows with it) and takes the keyboard to the next tab's
  first field -- building that page on the way, since tabs are built on demand.
* **Enter is OK, Ctrl+Enter is Apply.** These forms are ``QMdiSubWindow``s, not
  dialogs, so ``setDefault(True)`` on the OK button buys nothing at all: Enter
  did nothing whatsoever until the window handled it itself.
* **An unset optional field can be set.** Winbox greys the box and puts a ▼ at
  its edge; ours *disables* the box when it is a combo or a spin box, so there
  was nothing on the row for the keyboard to land on and no way to switch the
  field on. The triangle is a tab stop now, Space flips it, Ctrl+Space in the
  box takes the value away again, and typing in a greyed box wakes it.
* **A closed group fold can be opened**, or the rows inside it exist for the
  mouse only.
* **The list's verbs have keys** -- Ins/Del/Ctrl+E/Ctrl+Shift+E/Ctrl+M/Alt+↑↓ --
  scoped to the table, so Delete still deletes a character in the filter box.
  Down in the filter box hands the keyboard to the list, on a row.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMainWindow,
    QMdiArea,
    QMessageBox,
    QTabBar,
    QToolButton,
)

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui import style
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.group_fold import GroupFold
from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.property_window import (
    OptionalFieldEditor,
    PropertyWindow,
    plain_label,
)
from x3.ui.models.table_model import TREE_COLLAPSED, TREE_EXPANDED

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


# Every dialog in the write path answers itself; a real one never returns
# without a human, and this test has none.
shown: list[tuple[str, str]] = []
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[1])))
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warn", a[1])))
QMessageBox.question = staticmethod(
    lambda *a, **k: (shown.append(("ask", a[1])), QMessageBox.StandardButton.Yes)[1]
)

app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "dark", persist=False)

schema = Schema.load("7.21.1-151146055")
PATH = (20, 0)
ETHERNET = 1


class StubClient:
    """Just enough client for a form: menus, and dropdowns that answer."""

    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, **kw):
        return (schema.record_for(path, type_value) if type_value is not None
                else schema.record(path))

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {1: "main"}

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        return [(i, f"ether{i}", "ether") for i in range(1, 6)]


# The MDI area a real form lives in: focus set on a subwindow the area does not
# consider active does not stick, so take_focus needs one to be honest.
host = QMainWindow()
mdi = QMdiArea()
host.setCentralWidget(mdi)
host.show()


def ether1_row() -> RowObject:
    msg = m.Message()
    msg.add_u32(0xFE0001, 3)                # .id
    msg.add_u32(0x010001, ETHERNET)         # type — picks the overlay
    msg.add_u32(0x010002, 0x4000A)          # caps
    msg.add_u32(0x010004, 1)                # link
    msg.add_string(0x010006, "ether1")
    msg.add_string(0x010031, "ether1")      # Default Name
    msg.add_u32(0x010064, 1500)             # MTU
    msg.add_u32(0x010069, 1500)             # Actual MTU
    msg.add_u32(0x010065, 1514)             # L2 MTU
    msg.add_raw(0x0003E9, bytes.fromhex("48a98a1122cc"))
    msg.add_u32(0x0003EA, 1)                # ARP = enabled
    # No 0x1003b: ARP Timeout is the unset optional field under test.
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    return RowObject(
        path=PATH, raw=msg,
        fields=schema.decode(PATH, msg),
        form_fields=schema.decode_form(PATH, msg),
        values_by_code=values, values_by_code_multi=multi,
    )


def make_form(session: EditSession | None = None):
    row = ether1_row()
    client = StubClient(row)
    win = PropertyWindow(
        "Interface — ether1", PATH, row,
        client=client,
        edit_session=session or EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(PATH, ETHERNET),
        row_key="3",
        full_row=True,
    )
    mdi.addSubWindow(win)
    win.show()
    win.take_focus()
    app.processEvents()
    return win


def row_of(win, widget):
    """Which form row a widget belongs to -- '(button)' for the stack."""
    for spec, editor in win._rows:
        if editor is widget or editor.isAncestorOf(widget):
            return spec.get("name")
    for btn in win._action_btns:
        if btn is widget:
            # plain_label: the form in front marks its buttons' Alt letters
            # ("&OK"), and this is about which button it is.
            return f"(button {plain_label(btn.text())})"
    return f"({type(widget).__name__})"


def find_row(win, label):
    for spec, editor in win._rows:
        if spec.get("name") == label:
            return spec, editor
    return None, None


# --- 1. the form opens on the first thing you would change ----------------

win = make_form()
check("the keyboard lands on the first editable field, not the tab strip",
      row_of(win, app.focusWidget()), "Name")
ok("...which is a box you can type in",
   isinstance(app.focusWidget(), QLineEdit))

chain = []
for _ in range(14):
    fw = app.focusWidget()
    chain.append((type(fw).__name__, row_of(win, fw)))
    QTest.keyClick(fw, Qt.Key.Key_Tab)
    app.processEvents()
ok("no empty stop between the fields and the buttons",
   not any(kind == "QScrollArea" for kind, _ in chain), f"chain: {chain}")
ok("the tab strip is not in the walk either",
   not any(kind == "QTabBar" for kind, _ in chain), f"chain: {chain}")
names = [name for _kind, name in chain]
check("the walk is the form's own order",
      names[:4], ["Name", "Default Name", "Type", "MTU"])
ok("...and reaches the button stack after the last row",
   "(button OK)" in names, f"chain: {names}")
# The optional field's triangle sits with its box, not off at the end.
check("an optional field's ▼ follows the box it belongs to",
      names[names.index("ARP Timeout"):names.index("ARP Timeout") + 2],
      ["ARP Timeout", "ARP Timeout"])
check("...and the second stop on that row is the triangle itself",
      [kind for kind, name in chain if name == "ARP Timeout"][1], "QToolButton")

# --- 2. Ctrl+PageDown / Ctrl+PageUp walk the tabs -------------------------

tabs = win._tabs
check("the form has tabs to walk", tabs.count() > 1, True)
_, name_ed = find_row(win, "Name")
name_ed.setFocus()
app.processEvents()
QTest.keyClick(name_ed, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
check("Ctrl+PageDown moves to the next tab", tabs.currentIndex(), 1)
check("...and the keyboard goes with it",
      win._current_tab_name(), "Ethernet")
ok("...onto a field on that tab",
   row_of(win, app.focusWidget()) not in (None, "Name"),
   f"focus is {row_of(win, app.focusWidget())!r}")
ok("...which is one this tab lets you change",
   not next(spec for spec, ed in win._rows
            if ed is app.focusWidget() or ed.isAncestorOf(app.focusWidget())
            ).get("locked"))
QTest.keyClick(app.focusWidget(), Qt.Key.Key_PageUp,
               Qt.KeyboardModifier.ControlModifier)
app.processEvents()
check("Ctrl+PageUp comes back", tabs.currentIndex(), 0)

# Walking the strip itself is browsing, so the keyboard stays on the strip.
tabs.tabBar().setFocus()
app.processEvents()
QTest.keyClick(tabs.tabBar(), Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
check("from the strip, Ctrl+PageDown still moves", tabs.currentIndex(), 1)
ok("...and leaves the keyboard on the strip, ready for the next one",
   isinstance(app.focusWidget(), QTabBar))
win.close()
app.processEvents()

# --- 3. Enter is OK, Ctrl+Enter is Apply ---------------------------------

session = EditSession(EditMode.QUEUE, client=None)
win = make_form(session)
_, mtu = find_row(win, "MTU")
mtu.setFocus()
mtu.setValue(1400)
app.processEvents()
QTest.keyClick(mtu, Qt.Key.Key_Return)
app.processEvents()
check("Enter queues the edit", len(session.queue), 1)
check("...the one that was typed", session.queue[0].values, {"MTU": 1400})
ok("...and closes the form, the way OK does", not win.isVisible())

session = EditSession(EditMode.QUEUE, client=None)
win = make_form(session)
_, mtu = find_row(win, "MTU")
mtu.setFocus()
mtu.setValue(9000)
app.processEvents()
QTest.keyClick(mtu, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
check("Ctrl+Enter queues it too", len(session.queue), 1)
ok("...and stays open, the way Apply does", win.isVisible())
ok("...with nothing left to save", not win.is_dirty())
win._allow_close = True
win.close()
app.processEvents()

# A focused button answers Enter for itself.
session = EditSession(EditMode.QUEUE, client=None)
win = make_form(session)
cancel = next(b for b in win._action_btns
              if plain_label(b.text()) == "Cancel")
cancel.setFocus()
app.processEvents()
QTest.keyClick(cancel, Qt.Key.Key_Return)
app.processEvents()
ok("Enter on a focused button presses that button", not win.isVisible())
check("...so Cancel queued nothing", len(session.queue), 0)

# --- 4. an unset optional field, set from the keyboard -------------------

win = make_form()
spec, arp = find_row(win, "ARP Timeout")
ok("ARP Timeout is the optional row", isinstance(arp, OptionalFieldEditor))
ok("...and it arrives unset", not arp.is_present())
triangle = arp.findChild(QToolButton)
ok("the triangle is a tab stop now",
   bool(triangle.focusPolicy() & Qt.FocusPolicy.TabFocus))
triangle.setFocus()
app.processEvents()
QTest.keyClick(triangle, Qt.Key.Key_Space)
app.processEvents()
ok("Space on the triangle gives the field a value", arp.is_present())
ok("...and hands the keyboard to the box that just woke up",
   app.focusWidget() is arp.inner(),
   f"focus is {type(app.focusWidget()).__name__}")
QTest.keyClick(arp.inner(), Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
ok("Ctrl+Space in the box takes the value away again", not arp.is_present())
QTest.keyClick(arp.inner(), Qt.Key.Key_Tab)
app.processEvents()
ok("...and Tab out of an unset box is still Tab", not arp.is_present())
QTest.keyClick(arp.inner(), Qt.Key.Key_4)
app.processEvents()
ok("typing in the greyed box sets the field", arp.is_present())
ok("...and the keystroke lands in it rather than being eaten",
   "4" in arp.inner().text(), f"box reads {arp.inner().text()!r}")
win._allow_close = True
win.close()
app.processEvents()

# --- 5. a closed fold is not a wall ------------------------------------

fold = GroupFold("Connection Limit", expanded=False, flag=True)
fold.show()
app.processEvents()
arrow = fold.findChild(QToolButton)
ok("a fold's triangle is a tab stop",
   bool(arrow.focusPolicy() & Qt.FocusPolicy.TabFocus))
moves: list[bool] = []
fold.folded.connect(moves.append)
arrow.setFocus()
QTest.keyClick(arrow, Qt.Key.Key_Space)
app.processEvents()
ok("Space opens the fold", fold.expanded)
QTest.keyClick(arrow, Qt.Key.Key_Left)
app.processEvents()
ok("Left closes it, the way a tree does", not fold.expanded)
QTest.keyClick(arrow, Qt.Key.Key_Right)
app.processEvents()
ok("Right opens it again", fold.expanded)
QTest.keyClick(arrow, Qt.Key.Key_Return)
app.processEvents()
ok("Enter flips it", not fold.expanded)
check("...and every flip told the form to move its rows", moves,
      [True, False, True, False])
# A key that would change nothing is left alone, so it stays the ordinary walk.
ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
app.sendEvent(fold, ev)
ok("Left on an already-closed fold is not swallowed", not ev.isAccepted())
fold.close()

# --- 6. the list: verbs on keys, scoped to the table -------------------

NAME, TX, OID = 0x10001, 0x100D4, 0xFE0001


class StubMenu:
    autorefresh_ms = 1000
    refresh_filter = 0x4000A
    is_collection = True
    has_age_field = False
    # What the Add path reads off a menu to build the blank row it opens.
    nameval = "Name"
    cli_prefix = "iface"
    fields: dict = {}
    form_order: tuple = ()


def list_row(oid: int, name: str) -> RowObject:
    msg = m.Message()
    msg.add_u32(OID, oid)
    obj = RowObject(path=PATH, raw=msg)
    obj.values_by_code[OID] = oid
    for code, title, value in ((NAME, "Name", name), (TX, "Tx", "0 bps")):
        obj.fields.append((code, title, value, True))
        obj.values_by_code[code] = value
    return obj


session = EditSession(EditMode.QUEUE, client=None)
opened: list = []
pane = ListPane(
    "Interface", path=PATH, type_value=None, readonly=False,
    edit_session=session,
    on_open_properties=lambda path, raw, title, key, **kw: opened.append(title),
)
NAMES = ["ether1", "ether2", "ether3"]
pane._on_loaded((
    StubMenu(), ["", "Name", "Tx"],
    [["R", n, "0 bps"] for n in NAMES],
    [list_row(i + 1, n) for i, n in enumerate(NAMES)],
    None,
))
pane.show()
app.processEvents()

acts = {a.text(): a for a in pane._write_actions}
check("Remove answers to Delete",
      acts["Remove"].shortcut().toString(), "Del")
check("Add answers to Insert", acts["Add"].shortcut().toString(), "Ins")
check("Enable and Disable are a pair",
      (acts["Enable"].shortcut().toString(), acts["Disable"].shortcut().toString()),
      ("Ctrl+E", "Ctrl+Shift+E"))
ok("...and Disable is not Ctrl+D, which disconnects the session",
   "Ctrl+D" not in [a.shortcut().toString() for a in pane._write_actions])
ok("the key is in the tooltip, where anyone reaching for the mouse can see it",
   "[Del]" in acts["Remove"].toolTip(), acts["Remove"].toolTip())
pane.set_readonly(True)
ok("...and it survives a mode switch rewriting the tooltips",
   "[Del]" in acts["Remove"].toolTip(), acts["Remove"].toolTip())
pane.set_readonly(False)
ok("Refresh is F5 on the pane, so it works from the filter box too",
   pane._refresh_act in pane.actions()
   and pane._refresh_act.shortcut().toString() == "F5")
ok("Move Up/Down are hidden on an unordered list, and so are their keys",
   not pane._move_up_act.isVisible() and not pane._move_up_act.isEnabled())

# The filter box: typing narrows, Down walks in.
pane._filter.setFocus()
app.processEvents()
pane._filter.setText("ether")
app.processEvents()
QTest.keyClick(pane._filter, Qt.Key.Key_Down)
app.processEvents()
ok("Down in the filter box hands the keyboard to the list",
   pane.table.hasFocus())
check("...on a row, not beside one", pane.table.currentIndex().row(), 0)
pane._filter.setFocus()
app.processEvents()
QTest.keyClick(pane._filter, Qt.Key.Key_Escape)
app.processEvents()
check("Escape empties the filter", pane._filter.text(), "")
ok("...and keeps the keyboard in the box", pane._filter.hasFocus())
ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
app.sendEvent(pane._filter, ev)
ok("...but an empty box lets Escape through to the window, which closes on it",
   not ev.isAccepted())

# Delete on the list removes a row; Delete in the filter box is still a Delete.
pane.table.setFocus()
pane.table.selectRow(1)
pane.table.setCurrentIndex(pane._model.index(1, 1))
app.processEvents()
pane._filter.setFocus()
app.processEvents()
QTest.keyClick(pane._filter, Qt.Key.Key_Delete)
app.processEvents()
check("Delete in the filter box removes nothing", len(session.queue), 0)
pane.table.setFocus()
app.processEvents()
QTest.keyClick(pane.table, Qt.Key.Key_Delete)
app.processEvents()
check("Delete on the list queues the removal", len(session.queue), 1)
check("...of the row the keyboard was on", session.queue[0].values["_label"],
      "ether2")
QTest.keyClick(pane.table, Qt.Key.Key_Insert)
app.processEvents()
check("Insert opens a form for a new row", len(opened), 1)

# An interface tree folds with the arrows.
folded: list[int] = []
pane.table.branch_toggled.connect(folded.append)
pane._model.set_tree_meta({0: (0, TREE_COLLAPSED), 1: (1, 0)}, 1)
pane.table.setCurrentIndex(pane._model.index(0, 1))
app.processEvents()
QTest.keyClick(pane.table, Qt.Key.Key_Right)
app.processEvents()
check("Right opens a collapsed parent", folded, [0])
pane._model.set_tree_meta({0: (0, TREE_EXPANDED), 1: (1, 0)}, 1)
QTest.keyClick(pane.table, Qt.Key.Key_Left)
app.processEvents()
check("Left closes an open one", folded, [0, 0])
pane.table.setCurrentIndex(pane._model.index(1, 1))
app.processEvents()
QTest.keyClick(pane.table, Qt.Key.Key_Left)
app.processEvents()
check("a row that does not fold keeps the arrows for the columns",
      folded, [0, 0])
pane.close()

# --- 7. the same tab keys on a list page group ---------------------------

# The keys live on the shared MDI base (register_tab_strip installs them), so
# Interfaces ▸ Ethernet/VLAN walks with the same two as a property form's tabs.
from x3.ui.widgets.multi_page_window import MultiPageListWindow

pages = [{"title": n, "path": PATH, "type_value": i}
         for i, n in enumerate(("Interface", "Ethernet", "VLAN"))]
group = MultiPageListWindow("Interfaces", pages)
mdi.addSubWindow(group)
group.show()
# The standalone pane above was a top-level window of its own and took the
# application's keyboard with it; a widget-scoped key needs it back in here.
host.activateWindow()
app.processEvents()
group.take_focus()
app.processEvents()
check("a page group opens on its first page", group.tabs.currentIndex(), 0)
# Whatever holds the keyboard right now: the key is the window's, and it must
# reach it from anywhere inside.
QTest.keyClick(app.focusWidget() or group, Qt.Key.Key_PageDown,
               Qt.KeyboardModifier.ControlModifier)
app.processEvents()
check("Ctrl+PageDown walks a page group too", group.tabs.currentIndex(), 1)
fw = app.focusWidget()
ok("...and the keyboard lands in the page it opened",
   group.active_pane().isAncestorOf(fw), f"focus={type(fw).__name__}")
ok("...on a control that can use it, not the container holding it",
   bool(fw.focusPolicy() & Qt.FocusPolicy.TabFocus), f"focus={type(fw).__name__}")
QTest.keyClick(app.focusWidget() or group, Qt.Key.Key_PageUp,
               Qt.KeyboardModifier.ControlModifier)
app.processEvents()
check("Ctrl+PageUp comes back", group.tabs.currentIndex(), 0)
group.close()
app.processEvents()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all keyboard-edit tests passed")
