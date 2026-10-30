"""A dropdown says which of its entries is up, and what someone wrote on it.

No device and no pytest required:

    .venv/bin/python test_status_marks.py

Winbox shows bare names in an interface picker: you choose ether5 and find out
afterwards that the cable is out, or that a colleague left "WAN handoff, do not
touch" on it. Both facts are already in the read that fills the box -- a
maskless GETALL on the interface table carries `running` (0x01000e), the
standard Disabled bit and the comment (0xfe0009) beside the name, confirmed on
a 7.23.3 virtual router by commenting ether6 and disabling vlan33 on the console and
watching them arrive in the same rows the dropdown is built from.

So this is a display change. What it must not become is a *value* change, and
that is most of what this file checks:

**The item text stays the bare name.** A dropdown's label is load bearing in
three separate places -- for an `s`-prefix or string enum the label is what
goes on the wire, the form selects the current entry by matching the label
against the rendered value, and a re-read after Apply re-selects with
``findText``. The marks therefore ride on the item's icon and on two roles of
our own, and ``currentText()`` is exactly what it was before.

**A string enum is keyed by name, not by id.** Those fields store the name, so
marks keyed by object id would match nothing and quietly clear every dot.

**Off means off.** The whole thing is a deliberate divergence from Winbox v3,
so View ▸ Dropdown Status Marks turns it off, and off leaves the plain list.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QStyleOptionViewItem

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, EnumMark, RowObject
from x3.proto.schema import Schema
from x3.ui import settings as ui_settings
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets import status_marks as sm
from x3.ui.widgets.property_window import (
    MultiEnumArrayEditor,
    PropertyWindow,
    _editor_value,
    _restate_combo,
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


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

app = QApplication.instance() or QApplication(sys.argv)
schema = Schema.load(SCHEMA_DIR)

IFACE = tuple(sch.IFACE_ENUM_PATH)
BRIDGE_VLAN = (16, 13)

#: The interface table behind every picker in this file. ether3 is up with a
#: comment on it, ether4 is configured but down, bridge1 is switched off.
PORTS = {1: "ether1", 2: "ether2", 3: "ether3", 4: "ether4", 6: "bridge1"}
MARKS = {
    1: EnumMark(flags="R", running=True),
    2: EnumMark(flags="R", running=True),
    3: EnumMark(flags="R", running=True, comment="WAN handoff"),
    4: EnumMark(flags="", running=False),
    6: EnumMark(flags="X", running=False, disabled=True, comment="spare"),
}


# --- what a mark says --------------------------------------------------------

check("a running row is up", EnumMark(running=True).state, "up")
check("one that is not is down", EnumMark(running=False).state, "down")
check("disabled outranks down -- it is one fact, not two",
      EnumMark(running=False, disabled=True).state, "disabled")
check("a table with no running flag has no state to draw",
      EnumMark(running=None).state, "")
ok("...and nothing to draw at all", not EnumMark(running=None))
ok("a comment alone is worth drawing", bool(EnumMark(comment="hi")))


# --- the client reads them out of the rows it already has --------------------

def iface_row(oid, name, *, running=True, disabled=False, comment=""):
    """One interface as the device sends it: name, running flag, comment."""
    msg = m.Message()
    msg.add_u32(0xFE0001, oid)
    msg.add_string(0x010006, name)
    msg.add_bool(0x01000E, running)          # `running`, the R in the flags
    msg.add_bool(0xFE000A, disabled)         # the standard Disabled bit
    msg.add_string(0xFE0009, comment)        # STD_DESCR
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    return RowObject(
        path=IFACE, raw=msg, fields=schema.decode(IFACE, msg),
        values_by_code=values, values_by_code_multi=multi,
    )


client = DeviceClient("192.0.2.1", "u", "p")
client.schema = schema
client._enum_rows[IFACE] = [
    iface_row(1, "ether1"),
    iface_row(3, "ether3", comment="WAN handoff"),
    iface_row(4, "ether4", running=False),
    iface_row(6, "bridge1", running=False, disabled=True, comment="spare"),
]
marks = client.enum_marks(IFACE)
check("the rows the dropdown was built from are the marks", len(marks), 4)
check("a live port is up", marks[1].state, "up")
check("a port with no carrier is down", marks[4].state, "down")
check("a switched-off one says so", marks[6].state, "disabled")
check("...and keeps its comment", marks[6].comment, "spare")
check("the comment comes through", marks[3].comment, "WAN handoff")
check("so does the flags column's own string", marks[3].flags, "R")
check("and a disabled row's", marks[6].flags, "X")
ok("asking twice does not rebuild them",
   client.enum_marks(IFACE) is client.enum_marks(IFACE))

# A write to the source table drops the rows, and the marks go with them.
client._forget_enums(IFACE)
ok("a write to the table invalidates the marks",
   IFACE not in client._enum_marks_cache)

# A source table with no `running` flag must not claim a state.
addr = (14, 2)
if schema.record(addr) is not None:
    amsg = m.Message()
    amsg.add_u32(0xFE0001, 1)
    amsg.add_string(0xFE0009, "management")
    avals = {c: v for c, _t, v in amsg}
    client._enum_rows[addr] = [RowObject(
        path=addr, raw=amsg, fields=[], values_by_code=avals,
        values_by_code_multi={c: [(t, v)] for c, t, v in amsg},
    )]
    amark = client.enum_marks(addr)[1]
    check("an address is neither up nor down", amark.state, "")
    check("...but it still carries its comment", amark.comment, "management")


# --- a form's dropdowns ------------------------------------------------------

class StubClient:
    """Just enough client for a form: menus, an interface table, its marks."""

    schema = schema

    def __init__(self):
        self._row = None
        self.refreshed = 0

    def menu_for(self, path, type_value=None, **kw):
        return schema.record(path)

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path, **kw):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return dict(PORTS) if tuple(path) == IFACE else {}

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        return [(k, v, "ether") for k, v in sorted(PORTS.items())]

    def enum_marks(self, path):
        return dict(MARKS) if tuple(path) == IFACE else {}

    def refresh_enum_rows(self, path, **kw):
        self.refreshed += 1
        return True


def make_row(path, fields: dict, *, resolver=None):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    for code, value in fields.items():
        if isinstance(value, list):
            msg.add_u32_array(code, value)
        else:
            msg.add_u32(code, value)
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    return RowObject(
        path=path, raw=msg,
        fields=schema.decode(path, msg, resolver=resolver),
        form_fields=schema.decode_form(path, msg, resolver=resolver),
        values_by_code=values, values_by_code_multi=multi,
    )


def make_form(path, fields: dict):
    stub = StubClient()
    resolver = lambda p: dict(PORTS) if tuple(p) == IFACE else {}
    row = make_row(path, fields, resolver=resolver)
    stub._row = row
    win = PropertyWindow(
        "row", path, row,
        client=stub,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=stub.menu_for(path),
        row_key="1",
        full_row=True,
    )
    win._ensure_all_pages()
    app.processEvents()
    return win


def find(win, label):
    for spec, ed in win._rows:
        if spec.get("name") == label:
            return ed
    return None


win = make_form(BRIDGE_VLAN, {0x02: 6, 0x01: [100, 100], 0x03: [3, 4]})
bridge = find(win, "Bridge")
ok("the Bridge picker is a dropdown", isinstance(bridge, QComboBox),
   f"got {type(bridge).__name__}")


def texts(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def state_of(combo, name):
    for i in range(combo.count()):
        if combo.itemText(i) == name:
            return combo.itemData(i, sm.MARK_STATE_ROLE)
    return "<no such entry>"


def comment_of(combo, name):
    for i in range(combo.count()):
        if combo.itemText(i) == name:
            return combo.itemData(i, sm.MARK_COMMENT_ROLE)
    return "<no such entry>"


def icon_of(combo, name):
    for i in range(combo.count()):
        if combo.itemText(i) == name:
            return combo.itemIcon(i)
    return None


check("every entry is still its bare name", texts(bridge),
      ["ether1", "ether2", "ether3", "ether4", "bridge1"])
check("a live port is marked up", state_of(bridge, "ether1"), "up")
check("one with no carrier, down", state_of(bridge, "ether4"), "down")
check("a switched-off one, disabled", state_of(bridge, "bridge1"), "disabled")
check("the comment rides beside the name", comment_of(bridge, "ether3"),
      "WAN handoff")
check("...and an entry with none says nothing", comment_of(bridge, "ether1"), "")
ok("a marked entry has a dot", not icon_of(bridge, "ether1").isNull())
ok("...and one whose state we do not know has none",
   icon_of(bridge, "ether2") is not None)
check("the comment is also the entry's tooltip",
      bridge.itemData(2, Qt.ItemDataRole.ToolTipRole), "WAN handoff")

# The three ways the label is load bearing, all still holding.
check("the value is the wire id, not the text", _editor_value(bridge), 6)
check("the closed box still reads as the bare name", bridge.currentText(),
      "bridge1")
ok("a re-read after Apply still finds the entry by its text",
   _restate_combo(bridge, None, "ether3") and bridge.currentText() == "ether3")
bridge.setCurrentIndex(4)


# --- an array of interfaces --------------------------------------------------

tagged = find(win, "Tagged")
ok("Tagged is a dropdown array", isinstance(tagged, MultiEnumArrayEditor),
   f"got {type(tagged).__name__}")
row0 = tagged._rows[0][0]
check("a row's dropdown keeps bare names too",
      [row0.itemText(i) for i in range(row0.count())][:2], ["ether1", "ether2"])
check("and carries the comments", comment_of(row0, "ether3"), "WAN handoff")
# Row 1 holds ether4, and a row's own value is always in its box even when no
# other row is offered it (that is the exclusivity rule, not a missing mark).
check("...and the state of a port with no carrier",
      state_of(tagged._rows[1][0], "ether4"), "down")

# A row's box is thrown away and rebuilt whenever a sibling frees a value.
tagged._refresh_all_combos()
app.processEvents()
check("a refilled row is marked again",
      state_of(tagged._rows[1][0], "ether4"), "down")
check("...comments included", comment_of(tagged._rows[0][0], "ether3"),
      "WAN handoff")
check("...and its value is untouched", tagged.values(), [3, 4])


# --- picking several at once -------------------------------------------------

import x3.ui.widgets.property_window as pw

seen = {}
real_dialog = pw.MultiPickDialog


class Picker:
    def __init__(self, choices, *, title="", marks=None, parent=None):
        seen["marks"] = marks

    def exec(self):
        return 0

    def picked(self):
        return []


pw.MultiPickDialog = Picker
try:
    tagged.open_multi_pick()
finally:
    pw.MultiPickDialog = real_dialog
check("Add Several is offered the same marks", (seen.get("marks") or {}).get(4),
      MARKS[4])


# --- a string enum is keyed by the name it sends -----------------------------

name_fld = sch.Field(
    code=0x99, prefix="s", name="Chain", jg_type="string", enum_path=IFACE,
)
keyed = win._field_marks(name_fld)
check("a string enum's marks are keyed by name", sorted(keyed)[:2],
      ["bridge1", "ether1"])
check("...and say the same thing", keyed["ether4"].state, "down")
id_fld = sch.Field(
    code=0x98, prefix="u", name="Interface", jg_type="enm", enum_path=IFACE,
)
check("an id enum's are keyed by id", sorted(win._field_marks(id_fld)),
      [1, 2, 3, 4, 6])


# --- opening the box re-reads the state --------------------------------------

before = win.client.refreshed
bridge.showPopup()
bridge.hidePopup()
app.processEvents()
ok("opening a dropdown asks the device for the current state",
   win.client.refreshed > before,
   f"refreshed {win.client.refreshed}, was {before}")
check("...and the entries are still just names", texts(bridge),
      ["ether1", "ether2", "ether3", "ether4", "bridge1"])

# A client that has never heard of the refresh (an older one, a test double)
# must not take the popup down with it.
bridge.set_mark_source(object(), IFACE, provider=lambda: MARKS)
bridge.refresh_marks()
ok("a client with no refresh hook is not an error", True)


# --- the comment is painted, not appended ------------------------------------

delegate = sm.MarkDelegate()
view = bridge.view()
plain = view.model().index(0, 0)      # ether1, no comment
noted = view.model().index(2, 0)      # ether3, "WAN handoff"
opt = QStyleOptionViewItem()
opt.initFrom(view)
wide = delegate.sizeHint(opt, noted).width()
narrow = delegate.sizeHint(opt, plain).width()
ok("a commented entry asks for more room", wide > narrow,
   f"{wide} vs {narrow}")
capped = sm.MarkDelegate().sizeHint(opt, noted).width()
ok("...but not unboundedly: the cap is honoured",
   capped - narrow <= sm.style.scale_px(sm._COMMENT_MAX)
   + sm.style.scale_px(sm._COMMENT_GAP),
   f"{capped - narrow}px of comment")


# --- off is off --------------------------------------------------------------

ui_settings.save_dropdown_marks(False)
try:
    off = make_form(BRIDGE_VLAN, {0x02: 6, 0x01: [100, 100], 0x03: [3, 4]})
    plain_combo = find(off, "Bridge")
    check("with the setting off the names are unchanged", texts(plain_combo),
          ["ether1", "ether2", "ether3", "ether4", "bridge1"])
    ok("...and there are no dots",
       all(plain_combo.itemIcon(i).isNull()
           for i in range(plain_combo.count())))
    ok("...and no comments", not any(
        plain_combo.itemData(i, sm.MARK_COMMENT_ROLE)
        for i in range(plain_combo.count())))
    off._allow_close = True
    off.close()
finally:
    ui_settings.save_dropdown_marks(True)

win._allow_close = True
win.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all dropdown status-mark tests passed")
