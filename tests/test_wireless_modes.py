"""Wireless ▸ wlan1: Simple/Advanced Mode, and rows that grey out.

No device and no pytest required:

    .venv/bin/python test_wireless_modes.py

Three pieces of the .jg the form used to ignore, all of them visible on a
wireless interface:

* **The mode button.** `{name:'umode',type:'toggle',id:1001,modes:['Simple
  Mode','Advanced Mode']}` is not a field -- 1001 is a dialog-local widget id,
  same as the `buttonsep` above it. What it is, is the slot every ``on:'adv'``
  condition reads. With nothing answering for ``umode`` the whole advanced half
  of the dialog evaluated False and was unreachable.

* **Named decks are tabs.** A `type:deck` with a name is a Winbox v3 tab
  (Data Rates, Advanced, HT MCS, Tx Power, Current Tx Power); an anonymous one
  is an in-place field swap (firewall Action, DNS Type). The named ones were
  being flattened into whatever tab they followed, which is how Max Station
  Count ended up on Wireless instead of Advanced.

* **A third kind of on:.** A ``cond`` carrying neither ``hide`` nor
  ``hidedynamicly`` does not decide whether a row exists -- it decides whether
  the row is *live*. Winbox shows Supported Rates with the router's defaults in
  them and greys them until Rate says `configured`; ``oron`` splits the two
  jobs across two conditions, so Supported Rates B is on a 2.4GHz-capable
  radio only, and greyed there. Dropping those rows was wrong twice over: they
  vanished instead of greying, and picking `configured` never brought them
  back.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
)

from x3.ui.widgets.radio_group import RadioGroupEditor

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import PropertyWindow

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
PATH = (20, 0)
WIRELESS = 35

TYPE = 0x010001
HWTYPE = 0x0007D1
MODE = 0x0007D2
SSID = 0x0007D3
RATE = 0x000838
RATES_AG = 0x0007DA
RADIO_NAME = 0x00089A
WPROTOCOL = 0x0008E2
WCAPS = 0x0008FD
WPHYMODES = 0x000906

# The radio's own capability bits, which is what decides whether a rate row
# exists at all: 0x20000 = the B band, 0x40000 = A/G, 0x100008 = N.
PHY_ALL = 0x20000 | 0x40000 | 0x100008

wireless = schema.record_for(PATH, WIRELESS, {HWTYPE: 12})


# --- the schema side --------------------------------------------------------

check("the mode button survives parsing", wireless.mode_toggle,
      {"name": "umode", "modes": ["Simple Mode", "Advanced Mode"]})
ok("every named deck opens a tab of its own", all(
    t in wireless.tab_names()
    for t in ("Data Rates", "Advanced", "HT MCS", "Tx Power",
              "Current Tx Power")))
check("...and the mode button is what gates them",
      sch.tab_condition(wireless, "Data Rates"), "__deck:umode:1")
check("Max Station Count belongs to the Advanced deck, not to Wireless",
      next(i.get("tab") for i in wireless.form_layout
           if i.get("code") == 0x0007F9), "Advanced")

check("umode answers the adv conditions",
      [sch.condition_is_true(wireless, "adv", {}, ui_slots={"umode": u})
       for u in (0, 1)], [False, True])
ok("...and nothing answers for it without the button",
   not sch.condition_is_true(wireless, "adv", {}))

ok("a flagless cond does not decide a row exists",
   not sch.condition_hides(wireless, "rate"))
check("...it decides whether the row is live",
      sch.condition_enable_on(wireless, "rate"), "rate")
ok("a hide: cond still decides the row exists",
   sch.condition_hides(wireless, "bmode"))
check("...and its oron: names what greys it",
      sch.condition_enable_on(wireless, "bmode"), "rate")
check("a hidedynamicly cond greys nothing",
      sch.condition_enable_on(wireless, "adv"), None)


# --- the form ---------------------------------------------------------------

class StubClient:
    """Just enough client for a form: menus, and dropdowns that answer."""

    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, values=None, **kw):
        if type_value is None:
            return schema.record(path)
        return schema.record_for(path, type_value, values)

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return list(sorted((fld.enum or {}).items()))

    def interface_choices(self):
        return []


def make_form(extra: dict | None = None):
    numbers = {
        0xFE0001: 1,             # .id
        TYPE: WIRELESS,
        HWTYPE: 12,             # an IPQ4019 radio
        WPHYMODES: PHY_ALL,
        WCAPS: 0,
        MODE: 1,                # ap bridge
        WPROTOCOL: 2,           # 802.11 -- so NV2 is not a tab
        RATE: 0,                # Rate: default
        **(extra or {}),
    }
    msg = m.Message()
    for code, value in numbers.items():
        msg.add_u32(code, value)
    msg.add_string(SSID, "office")
    msg.add_string(RADIO_NAME, "r1")
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
        "wlan1", PATH, row,
        client=client,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(PATH, WIRELESS, values=values),
        row_key="1",
        full_row=True,
    )
    app.processEvents()
    return win


def tab_names(win):
    return [win._tabs.tabText(i) for i in range(win._tabs.count())]


def open_tab(win, name):
    for i in range(win._tabs.count()):
        if win._tabs.tabText(i) == name:
            win._tabs.setCurrentIndex(i)
            app.processEvents()
            return True
    return False


def find(win, label):
    for spec, editor in win._rows:
        if spec.get("name") == label:
            return spec, editor
    return None, None


def row_visible(win, label):
    """Whether the form row carrying ``label`` is on screen."""
    for spec, editor in win._rows:
        if spec.get("name") != label:
            continue
        for form, index, _on in win._cond_rows:
            if form.itemAt(index, form.ItemRole.FieldRole) is None:
                continue
            if form.itemAt(index, form.ItemRole.FieldRole).widget() is editor:
                return form.isRowVisible(index)
        return True     # not a conditional row: always there
    return None


app = QApplication.instance() or QApplication(sys.argv)

win = make_form()

# The button, and what a click does.
ok("the form grows a mode button", win._mode_btn is not None)
check("...labelled with the mode it switches to", win._mode_btn.text(),
      "Advanced Mode")
ok("Simple Mode has no Data Rates tab", "Data Rates" not in tab_names(win))
ok("...but the form built it anyway",
   "Data Rates" in [n for n, _h, _l in win._all_tabs],
   "a tab a click can open cannot be dropped at build time")

open_tab(win, "Wireless")
check("an adv row is built in Simple Mode, and hidden",
      row_visible(win, "Radio Name"), False)

win._mode_btn.click()
app.processEvents()
check("the button now offers the way back", win._mode_btn.text(),
      "Simple Mode")
check("...the adv row is on screen", row_visible(win, "Radio Name"), True)
ok("...and Advanced Mode opens the tabs it owns", all(
    t in tab_names(win)
    for t in ("Data Rates", "Advanced", "Tx Power", "Current Tx Power")))
check("Data Rates lands in the .jg's position, not at the end",
      tab_names(win)[:4], ["General", "Wireless", "Data Rates", "Advanced"])

# The rates themselves: shown with the device's defaults, greyed until the
# Rate radio says `configured`.
ok("the Data Rates tab opens", open_tab(win, "Data Rates"))
spec, editor = find(win, "Supported Rates A/G")
ok("a rate row exists with Rate on `default`", spec is not None,
   "Winbox greys these rows, it does not drop them")
check("...and it is greyed", bool(spec and spec.get("locked")), True)

rate_spec, rate_editor = find(win, "Rate")
# `radio:1` in the .jg -- Winbox draws Rate as two radio buttons, not a
# dropdown, and the schema says so.
ok("the Rate radio really is radio buttons",
   isinstance(rate_editor, RadioGroupEditor))
rate_editor.set_value(1)                     # configured
app.processEvents()
check("picking `configured` hands the rates back", spec.get("locked"), False)
rate_editor.set_value(0)                     # default
app.processEvents()
check("...and going back greys them again", spec.get("locked"), True)


# --- the shape of the Data Rates tab ---------------------------------------
# Read off Winbox 3.43 against this same 7.21.1 wlan (and confirmed on a PPP
# profile's Protocols tab, where all four groups are drawn the same way):
#
#     ─ Rate ──────────────────────────────────────────
#       (•) default   ( ) configured
#        Supported Rates B:  [x]1Mbps [x]2Mbps [x]5.5Mbps [x]11Mbps
#      Supported Rates A/G:  [x]6Mbps  [x]9Mbps [x]12Mbps [x]18Mbps
#                            [x]24Mbps [x]36Mbps [x]48Mbps [x]54Mbps
#
# Two things carry that: a radio group with no `compact:1` is a *section*
# rather than a labelled row, and a `set` wraps at its `columns:`.

def form_of(widget):
    """The QFormLayout the row carrying ``widget`` was added to."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent.layout(), QFormLayout):
            return parent.layout()
        parent = parent.parentWidget()
    return None


def cell(editor, label):
    """(row, column) of the checkbox reading ``label`` in a set's grid."""
    lay = editor.layout()
    for box in editor._boxes.values():
        if box.text() == label:
            return lay.getItemPosition(lay.indexOf(box))[:2]
    return None


check("Rate has no compact:1, so its name goes in a rule of its own",
      rate_editor.title, "Rate")
buttons = [b for _w, b in rate_editor._buttons]
ok("...its buttons share one row", (
    buttons[0].parentWidget() is buttons[1].parentWidget()
    and isinstance(buttons[0].parentWidget().layout(), QHBoxLayout)
), "Winbox draws default / configured side by side, not stacked")
check("...and the section spans the form, leaving no label behind",
      form_of(rate_editor).getWidgetPosition(rate_editor)[1],
      QFormLayout.ItemRole.SpanningRole)

ag = find(win, "Supported Rates A/G")[1]
b_set = find(win, "Supported Rates B")[1]
check("the .jg's columns:4 reaches the editor", (ag.columns, b_set.columns),
      (4, 4))
ok("an eight-member set is a grid", isinstance(ag.layout(), QGridLayout))
check("...that wraps after the fourth member",
      (cell(ag, "18Mbps"), cell(ag, "24Mbps"), cell(ag, "54Mbps")),
      ((0, 3), (1, 0), (1, 3)))
check("a set that fits on one row is still on the same four columns",
      cell(b_set, "11Mbps"), (0, 3))
check("...on one pitch, so every rate row lines up",
      ag.layout().columnMinimumWidth(0),
      b_set.layout().columnMinimumWidth(0))
ok("...and the pitch is the widest label on the page, not per row",
   ag.layout().columnMinimumWidth(0)
   >= max(ag.natural_pitch(), b_set.natural_pitch()))

# `compact:1` is the other shape: an ordinary row, name in the label column.
ok("the Advanced tab opens", open_tab(win, "Advanced"))
_pspec, preamble = find(win, "Preamble Mode")
ok("Preamble Mode is radio buttons too",
   isinstance(preamble, RadioGroupEditor))
check("...but compact:1 keeps it in the label column", preamble.title, None)
check("...as a normal two-column row",
      form_of(preamble).getWidgetPosition(preamble)[1],
      QFormLayout.ItemRole.FieldRole)

# A tab whose own condition is dynamic comes and goes the same way.
ok("NV2 is not a tab on an 802.11 interface", "NV2" not in tab_names(win))
proto_spec, proto_editor = find(win, "Wireless Protocol")
ok("Wireless Protocol is editable", proto_editor is not None)
proto_editor.setCurrentIndex(
    [proto_editor.itemText(i) for i in range(proto_editor.count())].index("nv2")
)
app.processEvents()
ok("...and picking nv2 opens the NV2 tab", "NV2" in tab_names(win))

win._allow_close = True      # the edits above are what makes it dirty
win.close()

# The mode is remembered, the way Winbox remembers it.
win = make_form({RATE: 1})
ok("the next form opens in the mode the last one was left in", win._advanced)
ok("...so Data Rates is there from the start", open_tab(win, "Data Rates"))
spec, _ed = find(win, "Supported Rates A/G")
ok("the rate row is built", spec is not None)
check("...and Rate `configured` opens it right away", spec.get("locked"), False)
# Read-only mode must not hand a greyed row back, and must not release one.
win.set_readonly(True)
check("read-only locks it again", spec.get("locked"), True)
win._allow_close = True
win.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall wireless-mode tests passed")
