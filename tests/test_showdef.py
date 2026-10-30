"""``showdef`` -- the inherited value a field shows while it is unset.

No device and no pytest required:

    .venv/bin/python test_showdef.py

A WiFi interface sets almost nothing itself: SSID, Country, Chains, channel
and security all come from its configuration profile, and Winbox shows each of
them greyed with the *inherited* value in the box, so you can see what the
interface will actually do before you override anything. 217 fields are
declared that way::

    {name:'SSID',type:'opt',showdef:1,c:[{type:'string',id:'s1004',max:32}]}

Only 5 of the 217 carry a `def` of their own, so the value is not in the
schema -- and the menu says where it is instead::

    {name:'Interface',title:'WiFi',type:'map',def:'mfe0024',…}

``def:'mfe0024'`` is a nested *message* (prefix m, code 0xFE0024) the device
sends alongside the object, holding what each field would be if left unset.
Three menus declare one: WiFi and WiFi Configuration (0xFE0024) and a CAP
interface (0x7008).

With that read, the machinery already in the form does the rest: an unset
optional field greys its box and shows its default, and ▼ hands it to you
still holding that value -- which is exactly Winbox's behaviour.

Confirmed on a live 7.21 wireless router: every ``/interface`` row of type 68 carries
0xFE0024 as a nested message, and inside it the same codes the showdef fields
use -- 0x1004 SSID, 0x1001 Country, 0x6002 Band, 0x6003 Channel Width, 0x2004
Passphrase. Two of that box's four WiFi interfaces inherit one profile's SSID
and two another's, which is the whole point of showing it.

That last code matters: the defaults message carries a *secret*, so the greyed
box has to mask it exactly like a configured one. Pinned below.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QLineEdit

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import OptionalFieldEditor, PropertyWindow

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
IFACE = (20, 0)
WIFI_TYPE = 68
DEFAULTS = 0xFE0024
SSID = 0x1004
PASSPHRASE = 0x2004
MAX_TX_POWER = 0x1006
TYPE_CODE = 0x010001

wifi = schema.record_for(IFACE, WIFI_TYPE)


# --- the schema side --------------------------------------------------------

check("the WiFi menu names the message its defaults arrive in",
      wifi.defaults_code, DEFAULTS)
ok("...and its fields are marked as showing them",
   wifi.fields[SSID].show_default and wifi.fields[MAX_TX_POWER].show_default)
ok("...while an ordinary field is not",
   not wifi.fields[0x0003E9].show_default)      # MAC Address
check("every menu that declares a defaults message",
      sorted(
          (mn.title, hex(mn.defaults_code))
          for mn in list(schema.menus) + list(schema.overlay_list)
          if mn.defaults_code
      ),
      [("CAP Interface", "0x7008"), ("Configuration", "0xfe0024"),
       ("WiFi", "0xfe0024")])
check("all 217 showdef fields survive parsing",
      sum(1 for mn in list(schema.menus) + list(schema.overlay_list)
          for f in mn.fields.values() if f.show_default), 217)
ok("...and every named one is an optional field, which is what greys it",
   all(f.optional for f in wifi.fields.values() if f.show_default and f.name))
check("...the unnamed ones being members of a wrapped pair (a time range)",
      sorted({f.jg_type for f in wifi.fields.values()
              if f.show_default and not f.name}),
      ["clocktime", "interval", "number"])


# --- the form ---------------------------------------------------------------

class StubClient:
    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, values=None, **kw):
        if type_value is None:
            return schema.record(path)
        return schema.record_for(path, type_value, values)

    def alloc_from(self):
        return (0, 9)

    def getobj(self, path, obj_id, **kw):
        return self._row

    def get(self, path):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return sorted((fld.enum or {}).items())

    def interface_choices(self):
        return []


def make_form(*, ssid: str | None, inherited: dict | None,
              show_passwords: bool = False):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    msg.add_u32(TYPE_CODE, WIFI_TYPE)
    msg.add_string(0x010006, "wifi1")
    if ssid is not None:
        msg.add_string(SSID, ssid)
    if inherited is not None:
        # The device's own defaults, in the nested message the menu names.
        sub = m.Message()
        for code, value in inherited.items():
            if isinstance(value, str):
                sub.add_string(code, value)
            else:
                sub.add_u32(code, value)
        msg.add_message(DEFAULTS, sub)
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    row = RowObject(
        path=IFACE, raw=msg, fields=schema.decode(IFACE, msg),
        form_fields=schema.decode_form(IFACE, msg),
        values_by_code=values, values_by_code_multi=multi,
    )
    client = StubClient(row)
    win = PropertyWindow(
        "wifi1", IFACE, row, client=client,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=client.menu_for(IFACE, WIFI_TYPE, values=values),
        row_key="1", full_row=True, show_passwords=show_passwords,
    )
    app.processEvents()
    return win


def open_tab(win, name):
    if win._tabs is None:
        return False
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


app = QApplication.instance() or QApplication(sys.argv)

win = make_form(ssid=None, inherited={SSID: "office-main", MAX_TX_POWER: 17})
open_tab(win, "Configuration")
spec, editor = find(win, "SSID")
ok("the SSID row is there", spec is not None)
ok("...as an optional field", isinstance(editor, OptionalFieldEditor),
   f"{type(editor).__name__}")
if isinstance(editor, OptionalFieldEditor):
    ok("...unset, since the interface sets no SSID of its own",
       not editor.is_present())
    inner = editor._inner
    check("...showing the value it inherits instead of an empty box",
          inner.text() if isinstance(inner, QLineEdit) else None,
          "office-main")
    # The default is the wire value, so it arrives as the bytes the device
    # sent; it is the field that knows how to read it.
    check("...and the device's default reached the field",
          spec["field"].render(spec["field"].default), "office-main")
    # ▼ hands the field over still holding the inherited value: Winbox's
    # "override this" gesture starts from what the interface is doing now.
    editor._flip()
    app.processEvents()
    ok("▼ takes the value over", editor.is_present())
    check("...without changing it",
          editor._inner.text() if isinstance(editor._inner, QLineEdit) else None,
          "office-main")

pspec, peditor = find(win, "Max Tx Power")
ok("a numeric inherited value comes across too", pspec is not None)
check("...as the number the device reports", pspec["field"].default, 17)
win._allow_close = True
win.close()

# A value of its own wins over the inherited one.
win = make_form(ssid="override", inherited={SSID: "office-main"})
open_tab(win, "Configuration")
spec, editor = find(win, "SSID")
if isinstance(editor, OptionalFieldEditor):
    ok("a field the interface does set reads as set", editor.is_present())
    check("...showing its own value, not the inherited one",
          editor._inner.text() if isinstance(editor._inner, QLineEdit) else None,
          "override")
win._allow_close = True
win.close()

# No defaults message at all (an older firmware, or a menu without one) must
# behave exactly as before: an empty greyed box.
win = make_form(ssid=None, inherited=None)
open_tab(win, "Configuration")
spec, editor = find(win, "SSID")
if isinstance(editor, OptionalFieldEditor):
    ok("with nothing to inherit the field is still unset",
       not editor.is_present())
    check("...and the box is simply empty",
          editor._inner.text() if isinstance(editor._inner, QLineEdit) else None,
          "")
    check("...with no default invented for it", spec["field"].default, None)
win._allow_close = True
win.close()


# An inherited *secret*. The defaults message really carries one -- a live
# 7.21 wireless router sends the profile's Passphrase in it alongside the SSID -- so
# the greyed box must mask it exactly like a configured one.
win = make_form(ssid=None, inherited={PASSPHRASE: "inherited-secret"})
open_tab(win, "Security")
spec, editor = find(win, "Passphrase")
ok("the inherited passphrase reaches the form", spec is not None)
if isinstance(editor, OptionalFieldEditor):
    inner = editor._inner
    ok("...in a text box", isinstance(inner, QLineEdit), f"{type(inner).__name__}")
    check("...masked, the way a configured secret is",
          inner.echoMode(), QLineEdit.EchoMode.Password)
    check("...while still being the value ▼ would hand over",
          inner.text(), "inherited-secret")
win._allow_close = True
win.close()

win = make_form(ssid=None, inherited={PASSPHRASE: "inherited-secret"},
                show_passwords=True)
open_tab(win, "Security")
_spec, editor = find(win, "Passphrase")
if isinstance(editor, OptionalFieldEditor):
    check("Settings ▸ Show Passwords reveals it, and nothing else does",
          editor._inner.echoMode(), QLineEdit.EchoMode.Normal)
win._allow_close = True
win.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall showdef tests passed")
