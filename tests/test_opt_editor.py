"""Editing an optional firewall match: the +, the !, and the picker.

No device and no pytest required:

    .venv/bin/python test_opt_editor.py

An optional match has three states, and Winbox shows all three in one control:
a bare ``+`` while unset (an unset match means "match anything", so an empty
text box would be a lie), then ``[!] value [×]`` once set. The property form
rendered these as ordinary always-on fields, so there was no way to unset a
match, negate one, or pick an interface from a list.

Underneath, one editor owns three sibling wire codes -- opt / not / value --
plus the paired end of an address range. wire_values() is where that mapping
lives, and QueuedChange.wire_values carries the extra codes through.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from x3.proto import schema as sch
from x3.proto.schema import Schema
from x3.ui.edit_mode import QueuedChange
from x3.ui.widgets.opt_match import OptMatchEditor

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

schema = Schema.load("7.21.1-151146055")
rules = next(m for m in schema.menus if m.path == (20, 3) and m.kind == "map")

SRC, IN_IF = 0x1A2, 0x190
LOOPBACK = 16777343  # 127.0.0.1

ROW = {SRC: True, 0xC8: False, 0x32: LOOPBACK, 0x33: LOOPBACK,
       IN_IF: True, 0x19: False, 0x01: 1}

INTERFACES = [(1, "lo0", "loopback"), (2, "ether1", "ether"),
              (3, "ether2", "ether"), (9, "wifi1", "wifi")]


# --- an address match -----------------------------------------------------

src = OptMatchEditor(rules.fields[SRC], ROW)
ok("a set match shows its value, not a checkbox",
   isinstance(src._value, QLineEdit))
check("...loaded from the sibling code", src._value.text(), "127.0.0.1")
# isVisibleTo, not isVisible: nothing is shown in an offscreen test, so
# isVisible() would be False for every widget and prove nothing.
ok("...with the + hidden and the × offered",
   not src._add.isVisibleTo(src) and src._clear.isVisibleTo(src))
check("an untouched match is not an edit", src.is_dirty(), False)

# Negate it.
src._neg.setChecked(True)
ok("toggling ! marks it edited", src.is_dirty())
check("...and says so", src.label(), "!127.0.0.1")
check("...writing the negate code", src.wire_values()[0xC8], True)

# Unset it entirely — this is the × / "match anything" case.
src._neg.setChecked(False)
src._set_present(False)
check("unsetting writes the opt flag off", src.wire_values(), {SRC: False})
check("...and reads as not matched", src.label(), "(not matched)")

# --- addresses and ranges -------------------------------------------------

src = OptMatchEditor(rules.fields[SRC], ROW)
src._value.setText("192.168.88.0")
wire = src.wire_values()
check("a single address fills both ends of the pair",
      (wire[0x32], wire[0x33]), (wire[0x32], wire[0x32]))

src._value.setText("10.0.0.1-10.0.0.9")
wire = src.wire_values()
ok("a range writes two different ends", wire[0x32] != wire[0x33])

src._value.setText("not-an-ip")
ok("a bad address is caught before sending", src.error() is not None)
src._value.setText("")
ok("...as is an empty one on a set match", src.error() is not None)
src._value.setText("192.168.88.1")
check("...and a good one passes", src.error(), None)

# --- the interface picker -------------------------------------------------

inif = OptMatchEditor(rules.fields[IN_IF], ROW, groups=INTERFACES)
ok("an interface match is a dropdown, not a text box",
   isinstance(inif._value, QComboBox))
check("...showing the current interface", inif._value.currentText(), "lo0")

labels = [inif._value.itemText(i) for i in range(inif._value.count())]
ok("...grouped by kind, the way Winbox does",
   "— all ether —" in labels and "— all wifi —" in labels)
ok("...listing every interface", {"lo0", "ether1", "ether2", "wifi1"} <= set(labels))

headings = [i for i in range(inif._value.count())
            if inif._value.itemData(i) is None]
ok("the group headings carry no wire value", headings)
ok("...and cannot be picked",
   all(not inif._value.model().item(i).isEnabled() for i in headings))

inif._value.setCurrentIndex(labels.index("ether1"))
wire = inif.wire_values()
check("picking one writes the interface id", wire[0x01], 2)
check("...along with the match flags", (wire[IN_IF], wire[0x19]), (True, False))

# --- an unset match starts as a bare + ------------------------------------

blank = OptMatchEditor(rules.fields[SRC], {})
ok("an unset match offers only +", blank._add.isVisibleTo(blank))
ok("...hiding the value and the ×",
   not blank._value.isVisibleTo(blank) and not blank._neg.isVisibleTo(blank))
check("...and reports nothing to send", blank.error(), None)
check("...and is not an edit until touched", blank.is_dirty(), False)
blank._set_present(True)
ok("pressing + marks it edited", blank.is_dirty())

# --- the extra codes reach the wire --------------------------------------

change = QueuedChange(
    path=(20, 3), title="Filter Rules", row_key="4", obj_id=4,
    edits=[{"code": SRC, "name": "Src. Address", "new": True,
            "extra": {0xC8: True, 0x32: LOOPBACK, 0x33: LOOPBACK}}],
)
check("all three codes are sent, not just the opt flag",
      change.wire_values(),
      {SRC: True, 0xC8: True, 0x32: LOOPBACK, 0x33: LOOPBACK})

# --- an opt-wrapped secret is still a secret ------------------------------
# The .jg hangs secrecy on the leaf, and on a tunnel the leaf is *inside* the
# opt:
#
#     {name:'IPsec Secret',type:'opt',id:'b7da',c:[{type:'secret',id:'s7db'}]}
#
# Asked at the row it is labelled on, that is an ordinary optional match, so the
# value went to screen in the clear with Show Passwords off.

IP_TUNNEL = 21                      # wire interface type, titled 'IP Tunnel'
IPSEC, IPSEC_VALUE = 0x7DA, 0x7DB

ipsec = schema.overlays["iface"][IP_TUNNEL].fields[IPSEC]
check("the row itself is only an opt", ipsec.jg_type, "opt")
check("...and the secret is its leaf", sch.secret_leaf(ipsec).code, IPSEC_VALUE)
ok("...so the field counts as a secret after all", sch.is_secret(ipsec))

TUNNEL = {IPSEC: True, IPSEC_VALUE: "hunter2"}

masked = OptMatchEditor(ipsec, TUNNEL)
check("the value box starts masked", masked._value.echoMode(),
      QLineEdit.EchoMode.Password)
check("...while still holding the secret for the wire",
      masked.wire_values()[IPSEC_VALUE], "hunter2")
masked.set_show_passwords(True)
check("Show Passwords reveals it", masked._value.echoMode(),
      QLineEdit.EchoMode.Normal)
masked.set_show_passwords(False)
check("...and clearing it masks again", masked._value.echoMode(),
      QLineEdit.EchoMode.Password)

shown = OptMatchEditor(ipsec, TUNNEL, show_passwords=True)
check("a form built with the setting on starts revealed",
      shown._value.echoMode(), QLineEdit.EchoMode.Normal)

plain = OptMatchEditor(rules.fields[SRC], ROW)
check("an ordinary match is not masked", plain._value.echoMode(),
      QLineEdit.EchoMode.Normal)


# --- a mode change has to reach inside this one too -----------------------

from x3.ui.widgets.property_window import _set_locked

ro = OptMatchEditor(rules.fields[SRC], ROW, readonly=True)
check("it starts locked", ro._value.isEnabled(), False)
_set_locked(ro, False)
check("going Live unlocks the value", ro._value.isEnabled(), True)
check("...and the negate toggle", ro._neg.isEnabled(), True)
check("...and the remove button", ro._clear.isEnabled(), True)
_set_locked(ro, True)
check("going back to read-only re-locks", ro._value.isEnabled(), False)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all opt-editor tests passed")
