"""Fields the device may hold no value for at all.

No device and no pytest required:

    .venv/bin/python test_optional_field.py

The schema marks these `optional: True`. An unset one has no value at all — IP ▸
Cloud's DDNS Update Interval, an interface's ARP Timeout — so a duration box
reading `00:00:00` claims a setting the device does not have. That is what made
an untouched Cloud form report `DDNS Update Interval: 0 → 00:00:00` as an edit.

`optional` is the only thing marking them: DDNS Enabled and Update Time on the
same menu are ordinary controls and carry no such attribute.

Winbox v3 says "unset" with the box itself rather than with a `+`: the editor is
there but greyed out, with a triangle at its right edge — ▼ to give the field a
value, ▲ to take it away — and clicking the greyed box is the same as ▼.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from x3.proto.schema import Schema
from x3.ui.widgets.property_window import (
    DurationEdit,
    OptionalFieldEditor,
    _optional_is_set,
)

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
cloud = next(x for x in schema.menus if x.path == (123, 1) and x.kind == "item")
interval = cloud.fields[0x7D]   # DDNS Update Interval, minimum 60
relay = cloud.fields[0x92]      # VPN Prefer Relay Code, a string
ddns = cloud.fields[0x64]       # DDNS Enabled — not optional

# --- which fields get a + -------------------------------------------------

check("DDNS Update Interval is optional", interval.optional, True)
check("VPN Prefer Relay Code is optional", relay.optional, True)
check("DDNS Enabled is not", ddns.optional, False)

# --- what counts as "set" -------------------------------------------------

check("absent is unset", _optional_is_set(interval, None), False)
# The device sent 0 for an interval whose own minimum is 60.
check("a zero below the field's minimum is unset",
      _optional_is_set(interval, 0), False)
check("a real interval is set", _optional_is_set(interval, 600), True)
check("the unset sentinel is unset",
      _optional_is_set(interval, 0xFFFFFFFF), False)
check("an empty string is unset", _optional_is_set(relay, ""), False)
check("a string with content is set", _optional_is_set(relay, "eu"), True)

# --- the control ----------------------------------------------------------

def make(fld, present, text=""):
    return OptionalFieldEditor(
        fld, QLineEdit(text), present=present, readonly=False
    )


unset = make(interval, False)
ok("an unset field still shows its editor", unset.inner().isVisibleTo(unset))
ok("...greyed out, so it does not read as a value", unset.inner().isReadOnly())
check("...with the triangle pointing down: press to set",
      unset._toggle.arrowType(), Qt.ArrowType.DownArrow)
check("...and is not an edit", unset.is_dirty(), False)
check("...and reads as unset", unset.label(), "(unset)")

unset._toggle.click()
ok("pressing ▼ wakes the editor up", not unset.inner().isReadOnly())
check("...and the triangle turns around: press to unset",
      unset._toggle.arrowType(), Qt.ArrowType.UpArrow)
ok("...and it counts as an edit", unset.is_dirty())

clicked = make(interval, False)
QTest.mouseClick(clicked.inner(), Qt.MouseButton.LeftButton)
ok("clicking the greyed box sets the field too", clicked.is_present())
ok("...and leaves it ready to type in", not clicked.inner().isReadOnly())

# An inner editor that greys out by being *disabled* (a dropdown, a spin box)
# never sees the click at all -- Qt walks it up to the control instead, which
# is the other half of "clicking the box is the same as pressing the triangle".
combo = QComboBox()
combo.addItem("disabled", 0)
combo.addItem("enabled", 1)
boxed = OptionalFieldEditor(relay, combo, present=False, readonly=False)
boxed.resize(200, 24)
boxed.show()
app.processEvents()
ok("a dropdown greys out by being disabled", not combo.isEnabled())
QTest.mouseClick(boxed, Qt.MouseButton.LeftButton, pos=QPoint(30, 12))
ok("...and clicking it still sets the field", boxed.is_present())

setfield = make(interval, True, "00:10:00")
check("a set field shows its value", setfield.label(), "00:10:00")
check("...untouched, it is not an edit", setfield.is_dirty(), False)
check("...and offers ▲ to take the value away",
      setfield._toggle.arrowType(), Qt.ArrowType.UpArrow)
setfield._toggle.click()
ok("clearing it is an edit", setfield.is_dirty())
check("...and reads as unset", setfield.label(), "(unset)")
ok("...and greys the box back out", setfield.inner().isReadOnly())
setfield.reset_baseline()
check("...until saved", setfield.is_dirty(), False)

# The default is what ▼ switches on: Winbox shows it greyed, so pressing the
# triangle leaves the same number in the box, now editable. Netwatch's Interval
# is one of the few optional fields the .jg gives a `def` (1000, scaled by 100).
netwatch = next(x for x in schema.menus if x.path == (51, 1) and x.kind == "map")
nw_interval = netwatch.fields[0x12]
check("the field under test really has a default", nw_interval.default, 1000)
defaulted = OptionalFieldEditor(
    nw_interval,
    DurationEdit(None, scale=nw_interval.scale),
    present=False, readonly=False,
)
check("an unset field shows the default it would take",
      defaulted.inner().text(), "00:00:10")
check("...without that counting as an edit", defaulted.is_dirty(), False)
defaulted._toggle.click()
check("...and ▼ hands it over unchanged", defaulted.inner().text(), "00:00:10")
ok("...as an edit this time", defaulted.is_dirty())

# A locked form outranks the triangle: neither state may be edited from it.
locked_unset = make(interval, False)
locked_unset.set_locked(True)
check("a locked form disables the triangle", locked_unset._toggle.isEnabled(), False)
QTest.mouseClick(locked_unset.inner(), Qt.MouseButton.LeftButton)
check("...and clicking the box sets nothing", locked_unset.is_present(), False)
locked_unset.set_locked(False)
check("unlocking gives the triangle back", locked_unset._toggle.isEnabled(), True)
ok("...and the box stays greyed while it is unset",
   locked_unset.inner().isReadOnly())

# --- locking reaches inside, like the other composite editors -------------

from x3.ui.widgets.property_window import _set_locked

lockable = make(interval, True, "00:10:00")
_set_locked(lockable, True)
check("locking disables the triangle", lockable._toggle.isEnabled(), False)
check("...and the editor", lockable.inner().isReadOnly(), True)
_set_locked(lockable, False)
check("unlocking restores it", lockable.inner().isReadOnly(), False)
check("...and the triangle", lockable._toggle.isEnabled(), True)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all optional-field tests passed")
