"""What the app says to a screen reader.

No device and no pytest required:

    .venv/bin/python test_a11y.py

Qt publishes a widget tree to AT-SPI on its own, so most of this app is already
legible to Orca: a QAction names its toolbar button, a QTableView publishes its
cells, a QComboBox its choices. What Qt cannot guess is anything this app knows
and paints:

* **A field's name.** Property forms cannot use ``addRow(str, widget)`` -- the
  buddy path treats a lone ``&`` as a mnemonic and ``Tx&Rx 64`` is a counter's
  name, not a shortcut -- so every field on every form used to be anonymous:
  "edit, 1500", with no way to learn it was the MTU. Rows now name their editor
  *and* pair label to field, because a name alone is not enough on Unix: Qt's
  combo box answers Name with its own current text there and expects the
  platform to take the field name from the label relation instead.
* **A composite editor's parts.** The keyboard lands on the boxes inside, not
  on the row's widget, and those are the things that have to say what they are
  -- including the ones that come and go as a list grows.
* **State drawn as a glyph or a colour.** A tristate flag's tick and cross, an
  optional field's greyed box.

This walks the real accessibility tree -- the same interfaces the AT-SPI bridge
reads -- and asserts nothing focusable is anonymous.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QComboBox, QWidget

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui import a11y
from x3.ui.edit_mode import EditMode, EditSession

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
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())


app = QApplication.instance() or QApplication(sys.argv)

schema = Schema.load("7.21.1-151146055")


# --- reading the tree the way the bridge does -------------------------------

def spoken(widget) -> str:
    """The name an assistive client is handed for this widget.

    Not ``accessibleName()``: several Qt interfaces answer differently (a combo
    box on Unix returns its current text, a button its caption), and what the
    bridge asks for is this.
    """
    iface = QAccessible.queryAccessibleInterface(widget)
    return "" if iface is None else iface.text(QAccessible.Text.Name)


def labelled_by(widget) -> list[str]:
    """The texts of whatever claims to label this widget (AT-SPI LABELLED_BY)."""
    iface = QAccessible.queryAccessibleInterface(widget)
    if iface is None:
        return []
    return [
        other.text(QAccessible.Text.Name)
        for other, rel in iface.relations()
        if rel & QAccessible.RelationFlag.Label
    ]


def anonymous(root) -> list[str]:
    """Every widget the keyboard can reach that would be read out nameless.

    A widget is nameless if it has neither a name of its own nor a label
    pointing at it. Containers that only hold other widgets are exempt: the
    keyboard passes through them to the parts, which is where a name belongs.
    """
    out = []
    for w in root.findChildren(QWidget):
        if w.focusPolicy() == Qt.FocusPolicy.NoFocus or not w.isVisible():
            continue
        if w.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
            continue
        if spoken(w) or labelled_by(w):
            continue
        out.append(f"{type(w).__name__}({w.objectName() or '-'})")
    return out


# --- a property form: every field says which field it is --------------------

PATH = (20, 0)
ETHER = 1


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


def ether_form():
    from x3.ui.widgets.property_window import PropertyWindow

    msg = m.Message()
    msg.add_u32(0xFE0001, 3)                # .id
    msg.add_u32(0x010001, ETHER)            # type — picks the overlay
    msg.add_u32(0x010002, 0x4000A)          # caps
    msg.add_u32(0x010004, 1)                # link
    msg.add_string(0x010006, "ether3")
    msg.add_string(0x010031, "ether3")      # Default Name
    msg.add_u32(0x010064, 1500)             # MTU
    msg.add_raw(0x0003E9, bytes.fromhex("48a98a1122cc"))
    msg.add_u32(0x0003EA, 1)                # ARP = enabled
    # No 0x1003b: ARP Timeout is left unset, so its row is an optional field.
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    row = RowObject(
        path=PATH, raw=msg,
        fields=schema.decode(PATH, msg),
        form_fields=schema.decode_form(PATH, msg),
        values_by_code=values, values_by_code_multi=multi,
    )
    client = StubClient(row)
    win = PropertyWindow(
        "ether3", PATH, row,
        client=client,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(PATH, ETHER),
        row_key="3",
        full_row=True,
    )
    win.resize(700, 620)
    win.show()
    app.processEvents()
    return win


def editor_for(win, field_name):
    for spec, ed in win._rows:
        if spec.get("name") == field_name:
            return ed
    return None


win = ether_form()

check("a text field is read as its label", spoken(editor_for(win, "MTU")), "MTU")
check("...and so is a read-only one",
      spoken(editor_for(win, "Default Name")), "Default Name")

# A dropdown is the case an accessible name cannot fix: Qt hands out the
# current value as the name on Unix, so without the label relation the user
# hears "enabled" and never hears "ARP".
arp = editor_for(win, "ARP")
ok("a dropdown is a dropdown", isinstance(arp, QComboBox))
if sys.platform != "win32":
    # On Windows Qt already names it by its label: the better answer, and
    # the label relation below is what matters on both.
    check("...whose name is still its value", spoken(arp), "enabled")
check("...and which the label speaks for", labelled_by(arp), ["ARP:"])

# The optional field: a box that holds the value and a triangle that decides
# whether there is one. Two tab stops, and the numbering must not swallow them.
timeout = editor_for(win, "ARP Timeout")
parts = a11y.focusables(timeout)
check("an optional field's box carries the field's name",
      spoken(parts[0]), "ARP Timeout")
check("...and its triangle says what it does",
      spoken(parts[1]), "ARP Timeout set or unset")
check("...and says which way it is set now",
      parts[1].accessibleDescription(), "unset — press to set")

check("nothing on the form is anonymous", anonymous(win), [])
win.close()


# --- a list that grows: the parts are counted off ---------------------------

from x3.ui.widgets.group_fold import GroupFold  # noqa: E402
from x3.ui.widgets.scalar_array import ScalarArrayEditor  # noqa: E402

dns = next(mn for mn in schema.menus if mn.path == (51, 1) and mn.kind == "map")
servers = dns.fields[154]
check("the field under test is the array kind", servers.jg_type, "stringarray")

arr = ScalarArrayEditor(servers, ["1.1.1.1", "9.9.9.9"])
a11y.label_editor(arr, "Servers")
arr.show()
app.processEvents()
check("each entry is numbered",
      [spoken(w) for w in a11y.focusables(arr)],
      ["Servers add entry",
       "Servers entry 1 of 2", "Servers remove entry 1 of 2",
       "Servers entry 2 of 2", "Servers remove entry 2 of 2"])

arr._add_row("8.8.8.8")
app.processEvents()
check("...and a new entry renumbers the ones already there",
      [spoken(w) for w in a11y.focusables(arr) if "entry 3" in spoken(w)],
      ["Servers entry 3 of 3", "Servers remove entry 3 of 3"])
arr.close()


# --- a tristate flag: the tick is not the state, the name is ----------------

from x3.ui.widgets.tristate_editor import MultiTristateEditor  # noqa: E402

rules = next(mn for mn in schema.menus if mn.path == (20, 3) and mn.kind == "map")
hotspot = rules.fields[94]
tri = MultiTristateEditor(hotspot, {94: 1, 95: 3})
tri.show()
app.processEvents()
states = [spoken(b) for b in tri._btns.values()]
ok("a required flag says so", any(s.endswith("must be set") for s in states),
   str(states))
ok("...a forbidden one too", any(s.endswith("must be clear") for s in states),
   str(states))
ok("...and one the rule ignores says that instead",
   any(s.endswith("any") for s in states), str(states))
tri.close()


# --- a list: letters, and what is drawn above a row -------------------------

from x3.ui.widgets.list_pane import ListPane, _flag_legend  # noqa: E402

IFACE = schema.record((20, 0))
legend = _flag_legend(IFACE)
check("the flags legend reads the menu's own letters",
      [legend.get(c) for c in "XIDRS"],
      ["disabled", "invalid", "dynamic", "running", "slave"])

COLS = ["", "Name", "Type", "MTU"]
ROWS = [["R", "ether1", "ether", "1500"],
        ["XS", "ether2", "ether", "1500"],
        ["", "bridge1", "bridge", "1500"]]
COMMENTS = ["uplink to ISP", "", ""]

pane = ListPane("Interface", path=(20, 0))
pane._on_loaded((IFACE, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
# The load carries no comments (they come off the wire per object), so the
# banner text is put in directly -- it is the reading that is under test.
pane._model.set_table(COLS, ROWS, comments=COMMENTS, flags_column=0)
pane.resize(700, 300)
pane.show()
app.processEvents()

check("a list table says which list it is", spoken(pane.table), "Interface list")
check("...and the filter box is spoken for by its label",
      labelled_by(pane._filter), ["Find:"])


def cell(row, col, role):
    return pane._model.data(pane._model.index(row, col), role)


ACC_TEXT = Qt.ItemDataRole.AccessibleTextRole
ACC_DESC = Qt.ItemDataRole.AccessibleDescriptionRole

check("a flag letter is read as a word", cell(0, 0, ACC_TEXT), "running")
check("...and a pair of them as two", cell(1, 0, ACC_TEXT), "disabled, slave")
check("...while a row with no flags says nothing", cell(2, 0, ACC_TEXT), None)
check("a value column is left alone", cell(0, 1, ACC_TEXT), None)
check("a comment drawn above the row reaches the row",
      cell(0, 2, ACC_DESC), "comment: uplink to ISP")
check("...and an uncommented row has nothing to add", cell(1, 2, ACC_DESC), None)

# End to end: the same expansion, read back off the table's own accessible
# children -- the interfaces the AT-SPI bridge walks, not the model.
table_iface = QAccessible.queryAccessibleInterface(pane.table)
cells = [table_iface.child(i).text(QAccessible.Text.Name)
         for i in range(table_iface.childCount())]
ok("the bridge is handed the expanded flags", "disabled, slave" in cells,
   str(cells[:12]))
pane.shutdown()
pane.close()


# --- what the app says out loud ---------------------------------------------
#
# A screen reader is told about focus moving and text changing under the caret.
# It is told nothing about a list finishing a re-read, a scan failing or Safe
# Mode arming: sighted users get a status line, a tint, a stripe. Those go out
# as announcements -- and the rule that matters is what is *not* announced,
# because a live list re-reads on a timer.

a11y.forget_said()
pane2 = ListPane("Address", path=(20, 1))
pane2._on_loaded((IFACE, list(COLS), [list(r) for r in ROWS], [None] * 3, None))
check("opening a list says how many rows it has", a11y.said(),
      ["Address: 3 rows"])

a11y.forget_said()
pane2._on_loaded((IFACE, list(COLS), [list(r) for r in ROWS], [None] * 3, None))
check("...and a re-read that changes nothing says nothing", a11y.said(), [])

a11y.forget_said()
pane2._on_loaded((IFACE, list(COLS), [list(ROWS[0])], [None], None))
check("...but a count that moved is worth hearing", a11y.said(),
      ["Address: 1 row"])
pane2.shutdown()
pane2.close()

a11y.forget_said()
line = a11y.SpokenLabel("Scanning…")
line.setText("3 neighbors")
line.setText("3 neighbors")
line.setText("scan failed: no route to host")
check("a status label says what it changed to, once", a11y.said(),
      ["3 neighbors", "scan failed: no route to host"])


class WindowStub:
    """Enough DeviceClient for a MainWindow to build."""

    readonly = True
    schema = None
    session_log = None

    def nav_tree(self):
        return []

    def close(self):
        pass


from x3.ui.session import SessionInfo  # noqa: E402
from x3.ui.widgets.main_window import MainWindow  # noqa: E402

main = MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=WindowStub())
a11y.forget_said()
main.statusBar().showMessage("Safe Mode on — 3 changes held on the router")
check("a status-bar message is read out", a11y.said(),
      ["Safe Mode on — 3 changes held on the router"])
a11y.forget_said()
main.statusBar().clearMessage()
check("...and clearing the bar is silent", a11y.said(), [])
main.close()


# --- the terminal: a widget that paints, and had nothing to read ------------
#
# Note what is *not* called here: textAtOffset and its neighbours segfault when
# called from Python, on stock Qt widgets as much as on ours -- the int-pointer
# out-parameters do not survive the binding. Qt's own default implementations
# of them run in C++ off text() and characterCount(), which is the path the
# AT-SPI bridge takes, so the interface below is what actually gets read.

from x3.ui.widgets.terminal_window import TerminalView  # noqa: E402

term = TerminalView(80, 24)
term.setWindowTitle("Terminal (1) — core-rtr1")
term.resize(700, 400)
term.show()
app.processEvents()
term.feed(b"[admin@core-rtr1] > /interface print\r\n")
term.feed(b"Flags: R - RUNNING\r\n 0 R name=ether1 mtu=1500\r\n")

t_iface = QAccessible.queryAccessibleInterface(term)
check("a terminal says it is one", t_iface.role(), QAccessible.Role.Terminal)
check("...and takes its name from its window", spoken(term),
      "Terminal (1) — core-rtr1")

text = t_iface.textInterface()
ok("...and hands out a text interface", text is not None)
check("the console's output is readable",
      text.text(0, text.characterCount()),
      "[admin@core-rtr1] > /interface print\n"
      "Flags: R - RUNNING\n"
      " 0 R name=ether1 mtu=1500\n")
check("...with the caret where the cursor is",
      text.cursorPosition(), text.characterCount())
check("...and no phantom selection", text.selectionCount(), 0)

# pyte pads its lines and its screen; neither should be read out.
check("no padding is published", term.a11y_lines()[1], "Flags: R - RUNNING")
check("...and no empty tail below the cursor", len(term.a11y_lines()), 4)

# What a reader is told changed. Appending is the whole point; a redraw is not
# an append and must not be reported as one.
check("new output is an insert",
      TerminalView.reader_delta("one\ntwo", "one\ntwo\nthree"),
      ("insert", 7, "\nthree"))
check("...a redrawn prompt is a replacement",
      TerminalView.reader_delta("one\ntwo", "one\nTWO"),
      ("replace", 0, "one\nTWO"))
check("...and nothing changing says nothing",
      TerminalView.reader_delta("one", "one"), None)

term.close()


# --- reaching a window with no mouse ----------------------------------------
#
# An MDI floor is navigated by pointing at it. Qt gives a sub-window no
# keyboard route in or out -- Ctrl+Tab and F6 do nothing to a QMdiArea out of
# the box -- so a window that was not on top could not be reached at all
# without a mouse, and "what is open?" was a question only answerable by
# looking at the screen.

from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QTextEdit  # noqa: E402
from PySide6.QtCore import QEvent  # noqa: E402

main2 = MainWindow(SessionInfo(host="10.0.0.2", user="admin"), client=WindowStub())
main2.resize(900, 600)
main2.show()
app.processEvents()
for name in ("Interface", "Address List", "Terminal (1)"):
    sub = main2.mdi.addSubWindow(QTextEdit())
    sub.setWindowTitle(name)
    sub.show()
app.processEvents()
main2.activateWindow()
app.processEvents()

main2._rebuild_window_list()
listed = [a.text() for a in main2._window_menu.actions()][-3:]
check("every open window is in the menu", listed,
      ["&1 Interface", "&2 Address List", "&3 Terminal (1)"])
check("...with the one in front marked",
      [a.text() for a in main2._window_menu.actions() if a.isChecked()],
      ["&3 Terminal (1)"])

a11y.forget_said()
main2._next_window()
check("cycling moves to the next window",
      main2.mdi.activeSubWindow().windowTitle(), "Interface")
check("...and says which one it landed on", a11y.said(), ["Interface"])
main2._prev_window()
check("...and back the other way",
      main2.mdi.activeSubWindow().windowTitle(), "Terminal (1)")


def press(widget, key, mods):
    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, key, mods))
    app.processEvents()


# The shortcut, not just the slot: Ctrl+Tab has to work from inside whatever
# child widget happens to hold the keyboard.
inner = main2.mdi.activeSubWindow().widget()
inner.setFocus()
app.processEvents()
press(inner, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
check("Ctrl+Tab reaches the next window from inside a child",
      main2.mdi.activeSubWindow().windowTitle(), "Interface")
press(main2.mdi.activeSubWindow().widget(), Qt.Key.Key_F6,
      Qt.KeyboardModifier.NoModifier)
check("...and so does F6", main2.mdi.activeSubWindow().windowTitle(),
      "Address List")

# A console eats plain Tab (completion) and must not eat this one.
term2 = TerminalView(80, 24)
sub = main2.mdi.addSubWindow(term2)
sub.setWindowTitle("Terminal (2)")
sub.show()
term2.setFocus()
app.processEvents()
sent: list[bytes] = []
term2.input_ready.connect(sent.append)
press(term2, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier)
check("a console still gets plain Tab for completion", sent, [b"\t"])
press(term2, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
check("...and does not get Ctrl+Tab", sent, [b"\t"])
ok("...which left the console for another window",
   main2.mdi.activeSubWindow().windowTitle() != "Terminal (2)",
   main2.mdi.activeSubWindow().windowTitle())
main2.close()


# --- state that is only ever drawn ------------------------------------------
#
# A form says a great deal without words: an amber wash for a field you have
# changed, a greyed box for one with no value, a triangle for a group folded
# shut, a coloured bar for a signal. None of it survives being read out, so each
# becomes a word on the widget it belongs to.

# A form with unsaved edits asks before it closes, and nothing may block the
# run. The form builds its own box rather than calling a static, so exec() is
# what has to stand in for the user; this one always discards.
from PySide6.QtWidgets import QMessageBox  # noqa: E402


def _discard(self, *a, **k):
    for btn in self.buttons():
        if "without saving" in btn.text().casefold():
            btn.click()
            return 0
    return 0


QMessageBox.exec = _discard  # type: ignore[method-assign]
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)

win2 = ether_form()
mtu = editor_for(win2, "MTU")
check("an untouched field says nothing extra", mtu.accessibleDescription(), "")
mtu.setValue(9000)
win2._refresh_dirty_highlights()
check("...and an edited one says so", mtu.accessibleDescription(), "edited")
mtu.setValue(1500)
win2._refresh_dirty_highlights()
check("...until it is put back", mtu.accessibleDescription(), "")

timeout2 = editor_for(win2, "ARP Timeout")
box = a11y.focusables(timeout2)[0]
check("a field with no value says that too", box.accessibleDescription(), "unset")
timeout2._flip()
check("...and stops once it has one", box.accessibleDescription(), "")

win2.close()

# A group's triangle: the rows behind a shut one do not exist for the keyboard
# either, so which way it points is worth a word.
fold = GroupFold("Connection Limit", expanded=False)
check("a folded group says it is shut",
      fold._arrow.accessibleDescription(), "collapsed")
check("...and is still named by its own title", spoken(fold._arrow),
      "Connection Limit")
fold.toggle()
check("...and says so when it opens",
      fold._arrow.accessibleDescription(), "expanded")

# A signal bar is a coloured bar and a band name. Both are paint.
from x3.ui.widgets.signal_bar import SignalBar  # noqa: E402

wireless = next(mn for mn in schema.menus
                if mn.path == (120, 5) and mn.kind == "item")
rssi = wireless.fields[0x3EC]
bar = SignalBar(rssi, 25)
check("a bar reads out as its reading", bar.accessibleDescription(),
      bar.text())
ok("...which is a number and a band, not a colour",
   "dBm" in bar.text() and "(" in bar.text(), bar.text())
bar2 = SignalBar(rssi, None)
check("...and says so when there is nothing to report",
      bar2.accessibleDescription(), "no reading")

# The Safe Mode banner paints its own text.
from x3.ui import hazard  # noqa: E402

bar3 = hazard.HazardBar()
bar3.set_text("Safe Mode: 3 changes held on the router")
check("the hazard banner's line is readable", spoken(bar3),
      "Safe Mode: 3 changes held on the router")
a11y.forget_said()
bar3.set_alarm(True)
check("...and the blink that means nobody is watching is said out loud",
      a11y.said(),
      ["Safe Mode unattended — Safe Mode: 3 changes held on the router"])
bar3.set_alarm(False)


# --- a dialog's own hand-built rows -----------------------------------------
#
# QFormLayout.addRow(str, widget) does make the label a buddy, so the dialogs
# that use it were mostly fine -- except where the row hands over a *wrapper*
# holding the real field. Then the relation lands on the wrapper and the field
# inside it is anonymous, which is how the password box ended up as the one
# field in the app that nobody could identify.

from x3.ui.widgets.connect_dialog import ConnectDialog  # noqa: E402

dlg = ConnectDialog()
dlg.resize(900, 600)
dlg.show()
app.processEvents()
check("the password box says what it is", spoken(dlg.password), "Password")
check("...and the plain rows are still spoken for by their labels",
      labelled_by(dlg.host), ["Connect To:"])
check("the address filter is named too", spoken(dlg.cdb_filter),
      "Filter addresses")
check("nothing on the connect dialog is anonymous", anonymous(dlg), [])
dlg.close()


# --- the helpers themselves -------------------------------------------------

check("a label's colon is not spoken", a11y.plain("MAC Address:"), "MAC Address")
check("...nor its padding", a11y.plain("  Tx&Rx 64 : "), "Tx&Rx 64")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all accessibility tests passed")
