"""Scrolling a form must scroll it, not edit whatever the pointer crossed.

No device and no pytest required:

    .venv/bin/python test_wheel_guard.py

Qt gives QComboBox and QAbstractSpinBox a focus policy of ``WheelFocus``: a
wheel roll over one focuses it *and* changes its value, and the scroll area
never sees the event. A wireless interface's form is sixty dropdowns over
several screens, so scrolling down it silently rewrote every setting the
pointer passed — Mode, Band, Channel Width, Country — and the form did not
move while it happened. You find out at Apply.

Guarded, a plain wheel roll never changes a value — the scroll area gets it
instead. Focus does not buy the wheel back: a field stays focused for as long
as it was last clicked in, which on a form still being read is most of them,
and the box you just typed a VLAN into is exactly the one a stray roll must
not rewrite. **Ctrl+wheel** is the deliberate one and still adjusts, focused
or not; nothing else in the app claims that chord.

Rows that appear after the form was built are the gap this used to leave: an
array field (a bridge VLAN's Tagged) makes a new dropdown every time ▾ is
pressed, and the form's one-time guard never saw it.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QScrollArea,
    QScrollBar,
    QSpinBox,
    QVBoxLayout,
    QWidget,
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
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())


app = QApplication.instance() or QApplication(sys.argv)

from x3.ui.widgets.wheel_guard import guard_wheel


def wheel(notches: int = -1, mods=Qt.KeyboardModifier.NoModifier) -> QWheelEvent:
    return QWheelEvent(
        QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton, mods,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


CTRL = Qt.KeyboardModifier.ControlModifier


def build_form(rows: int = 40):
    """A tall form inside a scroll area, the shape of a property window."""
    page = QWidget()
    form = QFormLayout(page)
    combos, spins = [], []
    for i in range(rows):
        combo = QComboBox()
        combo.addItems([f"opt {n}" for n in range(5)])
        combo.setCurrentIndex(2)
        form.addRow(f"Enum {i}", combo)
        combos.append(combo)
        spin = QSpinBox()
        spin.setRange(0, 1000)
        spin.setValue(50)
        form.addRow(f"Number {i}", spin)
        spins.append(spin)
    area = QScrollArea()
    area.setWidget(page)
    area.setWidgetResizable(True)
    host = QWidget()
    QVBoxLayout(host).addWidget(area)
    host.resize(320, 200)
    host.show()
    app.processEvents()
    return host, area, page, combos, spins


# --- unguarded: the bug ------------------------------------------------------

host, area, page, combos, spins = build_form()
ok("the form really does scroll (precondition)",
   area.verticalScrollBar().maximum() > 0,
   f"range {area.verticalScrollBar().maximum()}")
before = combos[0].currentIndex()
combos[0].clearFocus()
QApplication.sendEvent(combos[0], wheel(-1))
app.processEvents()
ok("unguarded, a wheel roll over a dropdown changes it (the bug)",
   combos[0].currentIndex() != before,
   f"{before} -> {combos[0].currentIndex()}")
host.close()


# --- guarded -----------------------------------------------------------------

host, area, page, combos, spins = build_form()
n = guard_wheel(page)
check("every value editor on the page is guarded", n, len(combos) + len(spins))
ok("...and none is left on WheelFocus",
   not [w for w in combos + spins
        if w.focusPolicy() == Qt.FocusPolicy.WheelFocus])

bar = area.verticalScrollBar()
bar.setValue(0)
combo = combos[0]
combo.clearFocus()
idx = combo.currentIndex()
QApplication.sendEvent(combo, wheel(-1))
app.processEvents()
check("an unfocused dropdown does not change", combo.currentIndex(), idx)
ok("...and the form scrolls instead", bar.value() > 0, f"value {bar.value()}")

bar.setValue(0)
spin = spins[0]
spin.clearFocus()
val = spin.value()
QApplication.sendEvent(spin, wheel(-1))
app.processEvents()
check("an unfocused spin box does not change", spin.value(), val)
ok("...and the form scrolls instead", bar.value() > 0, f"value {bar.value()}")

# Focus does not buy the wheel back: the field you last clicked in is the one
# a stray roll would do the most damage to.
bar.setValue(0)
combo.setFocus()
app.processEvents()
idx = combo.currentIndex()
QApplication.sendEvent(combo, wheel(-1))
app.processEvents()
check("a focused dropdown does not answer the wheel either",
      combo.currentIndex(), idx)
ok("...it scrolls, like every other roll", bar.value() > 0, f"value {bar.value()}")

spin.setFocus()
app.processEvents()
val = spin.value()
QApplication.sendEvent(spin, wheel(1))
app.processEvents()
check("nor does a focused spin box", spin.value(), val)

# Deliberate use is the modifier, and it needs no click first.
combo.clearFocus()
app.processEvents()
bar.setValue(0)
idx = combo.currentIndex()
QApplication.sendEvent(combo, wheel(-1, CTRL))
app.processEvents()
ok("Ctrl+wheel changes an unfocused dropdown -- the deliberate one",
   combo.currentIndex() != idx, f"{idx} -> {combo.currentIndex()}")
check("...and does not scroll the form as well", bar.value(), 0)

spin.clearFocus()
app.processEvents()
val = spin.value()
QApplication.sendEvent(spin, wheel(1, CTRL))
app.processEvents()
ok("...and a spin box the same", spin.value() != val,
   f"{val} -> {spin.value()}")

# Guarding twice must not double-handle anything.
guard_wheel(page)
bar.setValue(0)
combo.clearFocus()
idx = combo.currentIndex()
QApplication.sendEvent(combo, wheel(-1))
app.processEvents()
check("guarding twice leaves the value alone", combo.currentIndex(), idx)
ok("...and still scrolls once", bar.value() > 0, f"value {bar.value()}")
host.close()


# --- scroll bars are not value editors --------------------------------------

host, area, page, combos, spins = build_form()
guard_wheel(host)
ok("a scroll bar is never guarded",
   not [w for w in host.findChildren(QScrollBar)
        if w.focusPolicy() == Qt.FocusPolicy.StrongFocus
        and w.focusPolicy() != QScrollBar().focusPolicy()])
bar = area.verticalScrollBar()
bar.setValue(0)
QApplication.sendEvent(bar, wheel(-1))
app.processEvents()
ok("...so rolling over it still scrolls", bar.value() > 0, f"value {bar.value()}")
host.close()


# --- a form with nowhere to scroll ------------------------------------------

loose = QWidget()
lay = QFormLayout(loose)
free = QComboBox()
free.addItems(["a", "b", "c"])
free.setCurrentIndex(1)
lay.addRow("Enum", free)
spin = QDoubleSpinBox()
spin.setValue(1.5)
lay.addRow("Number", spin)
loose.show()
app.processEvents()
check("a form outside a scroll area is guarded too", guard_wheel(loose), 2)
free.clearFocus()
idx = free.currentIndex()
QApplication.sendEvent(free, wheel(-1))
app.processEvents()
check("...and an unfocused dropdown still does not change",
      free.currentIndex(), idx)
loose.close()


# --- rows that appear after the form was built ------------------------------

from x3.ui import style as _style  # noqa: E402
_style.apply_theme(app, "light", persist=False)
from x3.ui.widgets.property_window import MultiEnumArrayEditor  # noqa: E402

PORTS = [(2, "ether1"), (14, "ether2"), (15, "ether3"), (16, "ether4")]

page = QWidget()
lay = QVBoxLayout(page)
ed = MultiEnumArrayEditor(PORTS, [2])
lay.addWidget(ed)
area = QScrollArea()
area.setWidget(page)
area.setWidgetResizable(True)
area.resize(200, 40)
area.show()
app.processEvents()
guard_wheel(page)                      # what a property form does, once

ed._on_add_after(ed._rows[0][2])       # ▾ -- a dropdown the guard never saw
app.processEvents()
late = ed._rows[1][0]
late.setFocus()
app.processEvents()
idx = late.currentIndex()
QApplication.sendEvent(late, wheel(-1))
app.processEvents()
check("a row added after the form was guarded is guarded too",
      late.currentIndex(), idx)
QApplication.sendEvent(late, wheel(-1, CTRL))
app.processEvents()
ok("...and still answers Ctrl+wheel", late.currentIndex() != idx,
   f"{idx} -> {late.currentIndex()}")
area.close()


# --- the real property form --------------------------------------------------

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if os.path.isdir(SCHEMA_DIR):
    from x3.proto import message as m
    from x3.proto.device import RowObject
    from x3.proto.schema import Schema, type_field_code
    from x3.ui import style
    from x3.ui.edit_mode import EditMode, EditSession
    from x3.ui.widgets.property_window import PropertyWindow

    style.apply_theme(app, "light", persist=False)
    schema = Schema.load(SCHEMA_DIR)
    PATH = (20, 0)
    TC = type_field_code(schema.record(PATH))
    MENU = schema.record_for(PATH, 35, {0x7D1: 12})   # a wireless interface

    wire = m.Message()
    wire.add_u32(TC, 35)
    wire.add_u32(0x7D1, 12)
    wire.add_u32(0xFE0001, 3)
    for code, fld in MENU.fields.items():
        if code in (TC, 0x7D1, 0xFE0001):
            continue
        try:
            if fld.prefix == "u":
                wire.add_u32(code, 1)
            elif fld.prefix == "b":
                wire.add_bool(code, True)
            elif fld.prefix == "s":
                wire.add_string(code, b"value")
            elif fld.prefix == "r":
                wire.add_raw(code, b"\x00\x11\x22\x33\x44\x55")
            elif fld.prefix in ("U", "M", "A"):
                wire.add_u32_array(code, [1])
            elif fld.prefix == "S":
                wire.add_string_array(code, [b"a"])
        except Exception:  # noqa: BLE001
            pass
    ROW = RowObject(path=PATH, raw=wire, fields=schema.decode(PATH, wire),
                    form_fields=schema.decode_form(PATH, wire),
                    values_by_code={c: v for c, _t, v in wire})

    class Stub:
        def menu_for(self, path, tv=None, *, values=None, family=None):
            return MENU

        def alloc_from(self):
            return (0, 7)

        def getobj(self, p, o, **k):
            return ROW

        def get(self, p, **k):
            return ROW

        def prefetch_enums(self, paths):
            pass

        def enum_map(self, p, **k):
            return {}

        def enum_choices(self, fld):
            return [(1, "one"), (2, "two"), (3, "three")]

        def interface_choices(self):
            return []

    win = PropertyWindow(
        "wlan1", PATH, ROW, client=Stub(),
        edit_session=EditSession(EditMode.LIVE, client=None),
        menu=MENU, row_key="3", full_row=True,
    )
    win.show()
    app.processEvents()
    for i in range(win._tabs.count()):     # every tab, built on demand
        win._tabs.setCurrentIndex(i)
        app.processEvents()

    form_combos = win.findChildren(QComboBox)
    ok("the wireless form really is full of dropdowns", len(form_combos) > 20,
       f"{len(form_combos)} combos")
    unguarded = [c for c in form_combos
                 if c.focusPolicy() == Qt.FocusPolicy.WheelFocus]
    check("none of them is left able to steal the wheel", unguarded, [])

    picked = next((c for c in form_combos if c.count() > 1 and c.isEnabled()), None)
    ok("there is a live dropdown to test", picked is not None)
    if picked is not None:
        picked.clearFocus()
        idx = picked.currentIndex()
        QApplication.sendEvent(picked, wheel(-1))
        app.processEvents()
        check("scrolling past it leaves the setting alone",
              picked.currentIndex(), idx)
    win._allow_close = True
    win.close()
else:
    print(f"skipped the real form: no plugin cache at {SCHEMA_DIR}")


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall wheel-guard tests passed")
