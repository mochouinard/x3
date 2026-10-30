"""Hold Ctrl: the panel that says which keys work here, and Alt's underlines.

No device and no pytest required:

    .venv/bin/python test_key_hints.py

Two affordances, one point: the keys added in `test_keyboard_edit.py` are no use
to anyone who cannot find out they exist.

* **Hold Ctrl for half a second** and a panel floats over the window in front
  listing its keys; let go and it is gone. Every ``Ctrl+<key>`` chord has to
  pass through without a flash, or the peek becomes a flicker on every copy and
  paste -- so the other key cancels the timer on its way past.
* **F1 pins the same panel** open, and Esc then closes the *panel*, not the
  window under it.
* **The panel is read off the window.** Verbs with keys are ``QAction``s, so
  what it prints is the shortcut each one actually carries: greyed out where the
  action is (Remove with nothing selected), and absent where the action is
  hidden (Move Up on a list that is not ordered). Qt's own MDI system menu
  (Ctrl+F4) is not this window's verb and stays out.
* **Alt underlines the button letters** -- ``&OK`` and friends -- but only on
  the form in front. Two open forms would both claim Alt+O, which Qt calls an
  ambiguous shortcut and answers by moving the focus instead of pressing
  anything, so the letters are put on and taken off as a form comes forward.
* **Nothing about a peek may touch the keyboard**: the panel takes no focus and
  no mouse events.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QMainWindow,
    QMdiArea,
    QMessageBox,
)

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui import style
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.key_hints import HOLD_MS, KeyHints, hint_sections
from x3.ui.widgets.list_window import ListWindow
from x3.ui.widgets.property_window import (
    PropertyWindow,
    _MENUBAR_MNEMONICS,
    plain_label,
)

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


QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)

app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "dark", persist=False)

schema = Schema.load("7.21.1-151146055")
PATH = (20, 0)
ETHERNET = 1
NAME, TX, OID = 0x10001, 0x100D4, 0xFE0001

#: Long enough that the hold timer has certainly fired.
HELD = HOLD_MS + 250


class StubClient:
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


class StubMenu:
    autorefresh_ms = 1000
    refresh_filter = 0x4000A
    is_collection = True
    has_age_field = False
    nameval = "Name"
    cli_prefix = "iface"
    fields: dict = {}
    form_order: tuple = ()


def ether1_row() -> RowObject:
    msg = m.Message()
    msg.add_u32(0xFE0001, 3)
    msg.add_u32(0x010001, ETHERNET)
    msg.add_u32(0x010002, 0x4000A)
    msg.add_u32(0x010004, 1)
    msg.add_string(0x010006, "ether1")
    msg.add_string(0x010031, "ether1")
    msg.add_u32(0x010064, 1500)
    msg.add_raw(0x0003E9, bytes.fromhex("48a98a1122cc"))
    msg.add_u32(0x0003EA, 1)
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


def list_row(oid: int, name: str) -> RowObject:
    msg = m.Message()
    msg.add_u32(OID, oid)
    obj = RowObject(path=PATH, raw=msg)
    obj.values_by_code[OID] = oid
    for code, title, value in ((NAME, "Name", name), (TX, "Tx", "0 bps")):
        obj.fields.append((code, title, value, True))
        obj.values_by_code[code] = value
    return obj


host = QMainWindow()
mdi = QMdiArea()
host.setCentralWidget(mdi)
host.show()
host.activateWindow()
hints = KeyHints(host, mdi)
app.processEvents()


def rows_of(sections, title):
    for section in sections:
        if section.title == title:
            return section.keys
    return []


def flat(sections):
    return [(k.keys, k.what, k.enabled) for s in sections for k in s.keys]


# --- 1. a list window: what the panel says about it -----------------------

session = EditSession(EditMode.QUEUE, client=None)
lw = ListWindow("Interface", path=PATH, readonly=False, edit_session=session)
mdi.addSubWindow(lw)
lw.show()
mdi.setActiveSubWindow(lw)
lw.take_focus()
lw.pane._on_loaded((
    StubMenu(), ["", "Name", "Tx"],
    [["R", n, "0 bps"] for n in ("ether1", "ether2")],
    [list_row(1, "ether1"), list_row(2, "ether2")],
    None,
))
app.processEvents()

heading, sections = hint_sections(app.focusWidget(), host)
check("the panel is headed by the window it is about", heading, "Interface")
ok("...and it opens on the find box, so it says what that does",
   any("find" in s.title for s in sections), f"{[s.title for s in sections]}")
verbs = {k.keys: k for k in rows_of(sections, "its verbs")}
check("the row verbs are read off the toolbar's own actions",
      sorted(verbs), sorted(["Ins", "Del", "Ctrl+E", "Ctrl+Shift+E", "Ctrl+M", "F5",
                             "Ctrl+F"]))
check("...with the label the button carries", verbs["Del"].what, "Remove")
ok("Move Up is left out of an unordered list, not shown as unavailable",
   not any("Alt+Up" in k for k in verbs), f"{sorted(verbs)}")
ok("Qt's own MDI system menu stays out of it",
   not any("F4" in k for k in verbs), f"{sorted(verbs)}")
ok("a verb with nothing selected reads as unavailable",
   not verbs["Del"].enabled and verbs["Ins"].enabled)

lw.pane.focus_rows()
app.processEvents()
_, sections = hint_sections(app.focusWidget(), host)
verbs = {k.keys: k for k in rows_of(sections, "its verbs")}
ok("...and reads as available once the keyboard is on a row",
   verbs["Del"].enabled, "Remove still dim with a row selected")
ok("arriving on the list selects a row, which is what makes them available",
   bool(lw.pane.table.selectionModel().hasSelection()))
ok("the list's own navigation is spelled out too",
   ("Enter", "open the row's properties", True) in flat(sections))

# --- 2. holding Ctrl, and every chord that must not flash it --------------

table = lw.pane.table
table.setFocus()
app.processEvents()
QTest.keyPress(table, Qt.Key.Key_Control)
ok("nothing appears the instant Ctrl goes down", not hints.is_visible())
QTest.qWait(HELD)
ok("...and the panel is up once it has been held", hints.is_visible())
ok("...without taking the keyboard off the list", table.hasFocus(),
   f"focus={type(app.focusWidget()).__name__}")
ok("...or the mouse", hints._panel.testAttribute(
    Qt.WidgetAttribute.WA_TransparentForMouseEvents))
QTest.keyRelease(table, Qt.Key.Key_Control)
app.processEvents()
ok("letting go puts it away", not hints.is_visible())

QTest.keyPress(table, Qt.Key.Key_Control)
QTest.qWait(60)
QTest.keyClick(table, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
QTest.qWait(HELD)
ok("a Ctrl+key chord cancels the peek instead of flashing it",
   not hints.is_visible())
QTest.keyRelease(table, Qt.Key.Key_Control)
app.processEvents()

# --- 3. F1 pins it, and Esc then belongs to the panel --------------------

QTest.keyClick(table, Qt.Key.Key_F1)
app.processEvents()
ok("F1 pins the panel open", hints.is_visible() and hints.is_pinned())
QTest.keyClick(table, Qt.Key.Key_Escape)
app.processEvents()
ok("Esc closes the panel", not hints.is_visible())
ok("...and leaves the window it was over open", lw.isVisible())
QTest.keyClick(table, Qt.Key.Key_Escape)
app.processEvents()
ok("...so the next Esc is the window's own again", not lw.isVisible())

# --- 4. the setting is about it appearing unasked ------------------------

lw2 = ListWindow("Interface", path=PATH, readonly=False, edit_session=session)
mdi.addSubWindow(lw2)
lw2.show()
mdi.setActiveSubWindow(lw2)
lw2.take_focus()
app.processEvents()
hints.set_enabled(False)
QTest.keyPress(lw2.pane._filter, Qt.Key.Key_Control)
QTest.qWait(HELD)
ok("with the hold turned off, Ctrl shows nothing", not hints.is_visible())
QTest.keyRelease(lw2.pane._filter, Qt.Key.Key_Control)
QTest.keyClick(lw2.pane._filter, Qt.Key.Key_F1)
app.processEvents()
ok("...but F1 still answers: the setting hides the peek, not the help",
   hints.is_visible())
hints.hide_panel()
hints.set_enabled(True)

# A peek in one session must not light up another router's window.
other = QMainWindow()
other_box = QLineEdit(other)
other.show()
app.processEvents()
ok("an event from another window is not ours to answer",
   not hints._owns(other_box))
other.close()
lw2.close()
# Closing a top-level window leaves the application with no active one, and a
# window-scoped key (a button's Alt letter) only answers in the active window.
host.activateWindow()
app.processEvents()

# --- 5. a property form: its keys, and Alt on its buttons ---------------

row = ether1_row()
client = StubClient(row)
form = PropertyWindow(
    "Interface — ether1", PATH, row, client=client,
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=client.menu_for(PATH, ETHERNET), row_key="3", full_row=True,
)
mdi.addSubWindow(form)
form.show()
mdi.setActiveSubWindow(form)
form.take_focus()
app.processEvents()

_, sections = hint_sections(app.focusWidget(), host)
form_keys = {k.keys: k.what for k in rows_of(sections, "this form")}
for keys in ("Tab / Shift+Tab", "Enter", "Ctrl+Enter", "Ctrl+Space",
             "Ctrl+PgDn / Ctrl+PgUp"):
    ok(f"the form panel names {keys}", keys in form_keys, f"{sorted(form_keys)}")
letters = {k.keys: k.what for k in rows_of(sections, "its buttons")}
check("every button's Alt letter is listed", letters.get("Alt+O"), "OK")
check("...including the ones the schema declares",
      letters.get("Alt+B"), "Blink")
ok("...each letter only once",
   len(letters) == len(rows_of(sections, "its buttons")),
   f"{[k.keys for k in rows_of(sections, 'its buttons')]}")
taken = {k.keys.split("+")[-1].lower() for k in rows_of(sections, "its buttons")}
ok("...and never one the menu bar already answers for",
   not (taken & set(_MENUBAR_MNEMONICS)), f"{sorted(taken)}")

marks = [b.text() for b in form._action_btns]
ok("the form in front wears its underlines", any("&" in t for t in marks), marks)
check("Alt+O is OK's letter",
      form._mnemonic_texts.get("OK"), "&OK")
check("...and the label still reads as itself",
      plain_label(form._mnemonic_texts["OK"]), "OK")
ok("the Enable button's other spelling has its own letter reserved",
   plain_label(form._mnemonic_texts.get("Enable", "")) == "Enable"
   and form._mnemonic_texts["Enable"] != "Enable",
   f"{form._mnemonic_texts.get('Enable')!r}")

# Alt+A presses Apply. animateClick() holds the button down for ~100 ms and
# emits clicked on the way back up, so the press has to be waited out.
applied = []
form.queued.connect(lambda change: applied.append(change))
spec, mtu = next((s, e) for s, e in form._rows if s.get("name") == "MTU")
mtu.setFocus()
mtu.setValue(1400)
app.processEvents()
QTest.keyClick(mtu, Qt.Key.Key_A, Qt.KeyboardModifier.AltModifier)
QTest.qWait(250)
check("Alt+A presses Apply", len(applied), 1)
ok("...and the form is still open, the way Apply leaves it", form.isVisible())

# A second form must not make Alt+O ambiguous: only the one in front is marked.
form2 = PropertyWindow(
    "Interface — ether2", PATH, ether1_row(), client=client,
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=client.menu_for(PATH, ETHERNET), row_key="4", full_row=True,
)
mdi.addSubWindow(form2)
form2.show()
mdi.setActiveSubWindow(form2)
app.processEvents()
ok("the form that just came forward wears the letters",
   any("&" in b.text() for b in form2._action_btns))
ok("...and the one behind it takes them off",
   not any("&" in b.text() for b in form._action_btns),
   f"{[b.text() for b in form._action_btns]}")

form2._allow_close = True
form2.close()
form._allow_close = True
form.close()
app.processEvents()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all key-hint tests passed")
