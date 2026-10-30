"""Interface ▸ ether3 ▸ General, laid out the way Winbox v3 lays it out.

No device and no pytest required:

    .venv/bin/python test_iface_general.py

Four things Winbox does on this tab that the form used to get wrong:

* **Type.** The .jg says ``{"name":"Type","type":"objtype"}`` — a row with no
  wire id at all, whose value is the name of the type overlay the object's
  ``type`` slot selects (1 → Ethernet, 12 → Bridge). Having no id, it fell
  through every branch of the parser and the row simply never appeared.
* **Numbers are boxes.** Winbox has no steppers: MTU is typed into a box as wide
  as the row above it, not into a 140-pixel spin box with arrows.
* **Optional fields are greyed, not hidden.** ARP Timeout is ``opt:1``: unset,
  Winbox greys the box and puts a ▼ at its right edge rather than replacing the
  whole row with a ``+``.
* **The label column is inset**, so a form reads as a form and not as a list.

And two on the Loop Protect tab: every value control spans the value column
(a dropdown does not stretch unless it is told to), and a `type:separator` is a
one-pixel rule rather than a sunken two-pixel groove.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
)

from x3.proto import message as m
from x3.proto.device import RowObject, list_read_mask
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import (
    _FORM_LEFT_INSET,
    OptionalFieldEditor,
    PropertyWindow,
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


schema = Schema.load("7.21.1-151146055")
PATH = (20, 0)
ETHERNET = 1
BRIDGE = 12


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


def make_form(type_value: int, name: str = "ether3", extra: dict | None = None):
    msg = m.Message()
    msg.add_u32(0xFE0001, 3)                # .id
    msg.add_u32(0x010001, type_value)       # type — picks the overlay
    msg.add_u32(0x010002, 0x4000A)          # caps
    msg.add_u32(0x010004, 1)                # link
    msg.add_string(0x010006, name)
    msg.add_string(0x010031, name)          # Default Name
    msg.add_u32(0x010064, 1500)             # MTU
    msg.add_u32(0x010069, 1500)             # Actual MTU
    msg.add_u32(0x010065, 1514)             # L2 MTU
    msg.add_raw(0x0003E9, bytes.fromhex("48a98a1122cc"))
    msg.add_u32(0x0003EA, 1)                # ARP = enabled
    msg.add_u32(0x00044C, 1)                # tray status enum → "link ok"
    msg.add_bool(0x01000E, True)            # running
    msg.add_bool(0x010029, True)            # slave
    # No 0x1003b: ARP Timeout is the unset optional field under test.
    for code, value in (extra or {}).items():   # fields only this subtype has
        if isinstance(value, bool):
            msg.add_bool(code, value)
        elif isinstance(value, str):
            msg.add_string(code, value)
        else:
            msg.add_u32(code, value)
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
        name, PATH, row,
        client=client,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(PATH, type_value),
        row_key="3",
        full_row=True,
    )
    app.processEvents()
    return win


def general_rows(win):
    return [(spec, ed) for spec, ed in win._rows if spec.get("tab") == "General"]


def find(win, label):
    for spec, ed in win._rows:
        if spec.get("name") == label:
            return spec, ed
    return None, None


app = QApplication.instance() or QApplication(sys.argv)

# --- the schema side: a row with no wire id -------------------------------

base = schema.record(PATH)
ether = schema.merge_type_overlay(base, ETHERNET)
objtype_rows = [i for i in ether.form_layout if i.get("kind") == "objtype"]
check("the template's Type row survives parsing", len(objtype_rows), 1)
check("...on the General tab", objtype_rows[0].get("tab"), "General")
check("...and the merged menu knows which type it is", ether.type_title,
      "Ethernet")
check("a bridge is a Bridge",
      schema.merge_type_overlay(base, BRIDGE).type_title, "Bridge")
# Bridge re-declares Type with override:1 — one row, not two.
check("an overlay's own Type row does not double it",
      len([i for i in schema.merge_type_overlay(base, BRIDGE).form_layout
           if i.get("kind") == "objtype"]), 1)
check("a Type row carries no wire code",
      [k for k in objtype_rows[0] if k == "code"], [])

# --- the form ------------------------------------------------------------

win = make_form(ETHERNET)
labels = [spec["name"] for spec, _ed in general_rows(win)]
ok("General shows Type", "Type" in labels, f"rows: {labels}")
# Winbox order: Name, Default Name, Type, MTU, …
if "Type" in labels:
    check("...after Default Name and before MTU",
          (labels.index("Default Name") < labels.index("Type")
           < labels.index("MTU")), True)
spec, editor = find(win, "Type")
check("Type reads the interface family", spec.get("rendered"), "Ethernet")
check("...and shows it", editor.text(), "Ethernet")
ok("...as device status, never editable", spec.get("always_locked"))
ok("...so it is not something to queue",
   not any(e.get("name") == "Type" for e in win.edits(changed_only=False)),
   "Type turned up in the write list")

bridge = make_form(BRIDGE, "bridge1")
b_spec, _b_ed = find(bridge, "Type")
check("a bridge's form says Bridge", b_spec.get("rendered"), "Bridge")
check("...once", len([s for s, _e in general_rows(bridge)
                      if s["name"] == "Type"]), 1)
bridge.close()

# --- numbers: a plain box that grows with the window ----------------------

_spec, mtu = find(win, "MTU")
ok("MTU is a number editor", isinstance(mtu, QSpinBox), f"{type(mtu).__name__}")
check("...with no stepper arrows", mtu.buttonSymbols(),
      QAbstractSpinBox.ButtonSymbols.NoButtons)
check("...taking the width the row offers it",
      mtu.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
check("...and no width cap of its own", mtu.maximumWidth(), 16777215)
check("...still typed as a number", mtu.value(), 1500)

# --- the optional field ---------------------------------------------------

_spec, arp = find(win, "ARP Timeout")
ok("ARP Timeout is the optional-field control",
   isinstance(arp, OptionalFieldEditor), f"{type(arp).__name__}")
ok("...unset, since the device sent no value", not arp.is_present())
ok("...but its box is on screen, greyed", arp.inner().isVisibleTo(arp))
check("...with the triangle offering to set it",
      arp._toggle.arrowType(), Qt.ArrowType.DownArrow)
check("...and the duration box spans the row like the others",
      arp.inner().maximumWidth(), 16777215)
QTest.mouseClick(arp.inner(), Qt.MouseButton.LeftButton)
ok("clicking the greyed box sets the field", arp.is_present())
check("...and turns the triangle around", arp._toggle.arrowType(),
      Qt.ArrowType.UpArrow)

# --- the label column is inset -------------------------------------------

page = win._tabs.widget(0)
form = next((c for c in page.findChildren(QFormLayout)), None)
ok("the General page is a form", isinstance(form, QFormLayout),
   f"{type(form).__name__}")
if isinstance(form, QFormLayout):
    check("...held clear of the window edge",
          form.contentsMargins().left(), _FORM_LEFT_INSET)
    check("...with the labels against the fields",
          form.labelAlignment() & Qt.AlignmentFlag.AlignRight,
          Qt.AlignmentFlag.AlignRight)
    # Winbox punctuates every labelled row -- `Name:`, `MTU:`, `MAC Address:`.
    check("...and a colon after each one",
          [form.labelForField(find(win, n)[1]).text()
           for n in ("Name", "MTU", "MAC Address")],
          ["Name:", "MTU:", "MAC Address:"])

# --- Loop Protect: full-width controls, and a hairline separator ----------

loop = next(i for i in range(win._tabs.count())
            if win._tabs.tabText(i) == "Loop Protect")
win._tabs.setCurrentIndex(loop)
app.processEvents()

_spec, dropdown = find(win, "Loop Protect")
ok("the Loop Protect enum is a dropdown", isinstance(dropdown, QComboBox),
   f"{type(dropdown).__name__}")
check("...spanning the value column like the boxes below it",
      dropdown.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
for row in ("Send Interval", "Disable Time", "Status"):
    _s, ed = find(win, row)
    check(f"...as does {row}", ed.sizePolicy().horizontalPolicy(),
          QSizePolicy.Policy.Expanding)

# A check box is the exception: Winbox keeps it at its natural size.
eth = next(i for i in range(win._tabs.count())
           if win._tabs.tabText(i) == "Ethernet")
win._tabs.setCurrentIndex(eth)
app.processEvents()
_spec, autoneg = find(win, "Auto Negotiation")
ok("Auto Negotiation is a check box", isinstance(autoneg, QCheckBox),
   f"{type(autoneg).__name__}")
ok("...and is not stretched across the row",
   autoneg.sizePolicy().horizontalPolicy() is not QSizePolicy.Policy.Expanding,
   "the check box grew with the window")
# ...and the one row that takes no colon: Winbox writes a bool's name beside
# the box, so the name is a caption and not a label to punctuate.
check("a bool's name is left unpunctuated",
      next(c for c in win._tabs.widget(eth).findChildren(QFormLayout))
      .labelForField(autoneg).text(), "Auto Negotiation")

page = win._tabs.widget(loop)
rules = [f for f in page.findChildren(QFrame) if f.frameShape() == QFrame.Shape.HLine]
check("the tab's separator is drawn once", len(rules), 1)
if rules:
    rule = rules[0]
    check("...one pixel high", rule.height(), 1)
    check("...with no bevel to it", rule.frameShadow(), QFrame.Shadow.Plain)
    # It is wrapped, so the rule gets air above and below rather than sitting
    # as close to the next field as two fields sit to each other.
    holder = rule.parentWidget()
    margins = holder.layout().contentsMargins()
    ok("...and air above and below it",
       margins.top() > 0 and margins.bottom() > 0,
       f"holder margins: {margins.top()}/{margins.bottom()}")
    check("...which is what makes its row taller than the line",
          holder.height() > rule.height(), True)

form = next(c for c in page.findChildren(QFormLayout))
ok("form rows are given a little air between them",
   form.verticalSpacing() >= 4, f"vertical spacing is {form.verticalSpacing()}")

# --- the window tray -----------------------------------------------------
#
# Winbox reads "enabled | running | slave | passthrough | inactive | link ok"
# along the bottom of an ether form. All six cells come out of the .jg, but
# from three kinds of node: `type:enable` (the first), the base map's four
# `type:flag` nodes, and the Ethernet overlay's `type:statusbar` (the last).
# A flag whose bit is clear is greyed rather than dropped, so the tray is
# markup; compare the flattened text.
tray = QTextDocument()
tray.setHtml(win._form_footer.text())
check("the tray reads the way Winbox's does",
      [c.strip() for c in tray.toPlainText().split("·")],
      ["Enabled", "running", "slave", "passthrough", "inactive", "link ok"])
ok("...and is shown at all", win._form_footer.isVisible()
   or not win.isVisible())
dim_flags = [
    fld.name for fld in win._footer_flag_fields()
    if not win._flag_is_on(fld, win._row_values())
]
check("the flags the row does not carry are the greyed ones", dim_flags,
      ["passthrough", "inactive"])
ok("...and only they are wrapped in the dim span",
   ">passthrough</span>" in win._form_footer.text()
   and "running</span>" not in win._form_footer.text(),
   win._form_footer.text())
# `type:enable` already spells 0xFE000A out as Enabled/Disabled; the injected
# Disabled flag must not appear a second time as its own chip.
ok("the Disabled flag is not said twice",
   0xFE000A not in {f.code for f in win._footer_flag_fields()},
   f"{[hex(f.code) for f in win._footer_flag_fields()]}")

# The tray's last cell only arrives when the read carries the menu's mask:
# (20,0) keeps its link status behind `refreshfilter`, so a GETOBJ sent with
# no 0xFE000C at all came back without 0x44C and the cell read "—" until the
# Traffic tab's own masked poll replaced the row.
check("a form's own read carries the mask the menu declares",
      win._open_read_mask(), 0x4000F)
check("...which is the mask Winbox opens an interface with",
      list_read_mask(schema.merge_type_overlay(base, ETHERNET)), 0x4000F)
ok("...and stays None for a menu that declares no filter",
   list_read_mask(schema.record((13, 1))) is None,
   f"{list_read_mask(schema.record((13, 1)))}")

# --- a flag is chrome, not a form row -------------------------------------
#
# The template's four `type:'flag'` nodes -- running / slave / passthrough /
# inactive -- are declared at its very end, after the last tab has been
# opened, so parsing them as ordinary bools gave the Traffic tab four
# checkboxes under the graphs: rows Winbox does not draw, offering to write
# state the router owns. A flag belongs to the list's letter column and the
# window tray.
win._ensure_all_pages()
app.processEvents()
FLAG_NAMES = {"running", "slave", "passthrough", "inactive"}
ok("a flag is chrome, not a form row",
   not [spec.get("name") for spec, _e in win._rows
        if str(spec.get("name") or "").lower() in FLAG_NAMES],
   f"{[(spec.get('tab'), spec.get('name')) for spec, _e in win._rows if str(spec.get('name') or '').lower() in FLAG_NAMES]}")
FLAG_CODES = {0x01000E, 0x010029, 0x01003D, 0xFE0008}
ok("...which is decided in the layout, for every menu at once",
   not [i for i in ether.form_layout if i.get("code") in FLAG_CODES],
   f"{[(i.get('tab'), hex(i.get('code'))) for i in ether.form_layout if i.get('code') in FLAG_CODES]}")
check("...and the fields are still there for the letters and the tray",
      [f.hint for f in ether.flag_fields()], ["X", "R", "S", "P", "I"])
# The graph box is what the Traffic tab now ends with.
traffic = [i for i in ether.form_layout if i.get("tab") == "Traffic"]
check("the traffic graphs are the last thing on the tab",
      traffic[-1].get("kind"), "graphbox")

win._allow_close = True
win.close()

# --- an IP Tunnel's IPsec Secret is masked like any other password --------
# The .jg puts the secrecy on the leaf and the leaf inside the opt:
# {name:'IPsec Secret',type:'opt',id:'b7da',c:[{type:'secret',id:'s7db'}]}.
# The row it is labelled on is an ordinary optional match, so with Show
# Passwords off the secret was still on screen in plain text.

IP_TUNNEL = 21
IPSEC, IPSEC_VALUE = 0x7DA, 0x7DB

tun = make_form(IP_TUNNEL, "gre-tunnel1",
                extra={IPSEC: True, IPSEC_VALUE: "hunter2"})
_spec, ipsec = find(tun, "IPsec Secret")
ok("the tunnel form has an IPsec Secret row", ipsec is not None)
check("its box is masked with Show Passwords off", ipsec._value.echoMode(),
      QLineEdit.EchoMode.Password)
tun.set_show_passwords(True)
check("...and the setting reaches inside the opt", ipsec._value.echoMode(),
      QLineEdit.EchoMode.Normal)
tun.set_show_passwords(False)
check("...and back again", ipsec._value.echoMode(),
      QLineEdit.EchoMode.Password)
check("masking never touches what would be written",
      ipsec.wire_values()[IPSEC_VALUE], "hunter2")
tun._allow_close = True
tun.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all interface-General tests passed")
