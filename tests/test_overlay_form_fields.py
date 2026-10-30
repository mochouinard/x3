"""Opening an interface reads it against its type overlay, not the bare base.

No device and no pytest required:

    .venv/bin/python test_overlay_form_fields.py

A bridge's ``MAC Address``, ``ARP`` and ``DHCP Snooping`` -- and every
interface's ``MAC Address`` -- are fields of the *type overlay* (Bridge,
Ethernet, ...), not of the generic Interface base. ``open_property_window``
re-reads the object with a GETOBJ before showing it, and the menu it hands the
decoder decides how the reply reads. Reading a bridge against the bare base
left the whole Bridge tab as ``<0x000xxx>`` rows with raw values -- a MAC as
``525400123456`` -- because the base has no field of that id to name or type.

This pins the fix: the object is read and decoded against the base *merged with
its wire type's overlay*, so the overlay's fields come back named, and a
``macaddr`` field comes back as ``52:54:00:12:34:56`` off its declared type.
The bug was invisible to the other form tests because they decode their fixture
with :meth:`Schema.decode_form` and *no* menu, which resolves the overlay from
the wire on its own -- the exact step a menu passed to GETOBJ suppresses.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.main_window import MainWindow

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
BRIDGE = 12
MAC = bytes.fromhex("525400123456")

app = QApplication.instance() or QApplication(sys.argv)


def _row(msg: m.Message, menu) -> RowObject:
    """A RowObject decoded against ``menu`` -- exactly what GETOBJ builds."""
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    return RowObject(
        path=PATH, raw=msg,
        fields=schema.decode(PATH, msg, menu=menu),
        form_fields=schema.decode_form(PATH, msg, menu=menu),
        values_by_code=values, values_by_code_multi=multi,
    )


def _bridge_msg() -> m.Message:
    msg = m.Message()
    msg.add_u32(0xFE0001, 7)          # .id
    msg.add_u32(0x010001, BRIDGE)     # type -> picks the Bridge overlay
    msg.add_string(0x010006, "bridge1")   # Name (base field)
    msg.add_u32(0x010064, 1500)       # MTU (base field)
    msg.add_raw(0x0003E9, MAC)        # MAC Address -- an overlay macaddr field
    msg.add_bool(0x00006F, False)     # DHCP Snooping -- an overlay field
    return msg


class StubClient:
    """A client whose GETOBJ decodes against the menu it is handed, so that
    which menu ``open_property_window`` chooses is observable in the result."""

    schema = schema

    def __init__(self, full: m.Message):
        self._full = full

    def menu_for(self, path, type_value=None, *, values=None, name=None, **kw):
        if type_value is not None:
            return schema.record_for(tuple(path), type_value, values, name=name)
        return schema.record(tuple(path), name=name)

    def getobj(self, path, obj_id, *, filter_mask=None, menu=None, **kw):
        return _row(self._full, menu)

    # Chrome the form touches but this test does not exercise.
    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return list(sorted((fld.enum or {}).items()))

    def interface_choices(self):
        return []

    def uptime(self):
        return 0.0


class Router:
    """Just enough MainWindow to open one property form for real."""

    open_property_window = MainWindow.open_property_window

    def __init__(self, client):
        self.client = client
        self.edit = EditSession(EditMode.QUEUE, client=None)
        self._show_passwords = False
        self.opened = None

    def _focus_existing(self, key):
        return False

    def run_context_action(self, *a, **k):
        pass

    def open_menu(self, *a, **k):
        pass

    def _on_property_change(self, *a, **k):
        pass

    def _show_child(self, win):
        self.opened = win

    def _register(self, key, win):
        pass


def specs_by_code(win):
    return {spec.get("code"): spec for spec, _ed in win._rows}


# The list row is sparse and (like the list pane) carries the type but not the
# overlay's own fields -- MAC Address is not on it yet.
list_row = _row(_bridge_msg(), schema.record(PATH, name="Interface"))
ok("the list row alone leaves the MAC unnamed",
   any(n.startswith("<0x0003e9") for _t, _c, n, _v, _o in list_row.form_fields),
   "the base decode should not know 0x3e9 -- that is the whole problem")

router = Router(StubClient(_bridge_msg()))
router.open_property_window(PATH, list_row, "bridge1", menu_name="Interface")
app.processEvents()
win = router.opened
ok("a form was opened", win is not None)

by_code = specs_by_code(win)

mac = by_code.get(0x0003E9)
ok("the MAC row is on the form", mac is not None)
check("...named from the .jg, not a hex id", mac and mac.get("name"),
      "MAC Address")
check("...and formatted off its macaddr type, colons and all",
      mac and mac.get("rendered"), "52:54:00:12:34:56")

snoop = by_code.get(0x00006F)
ok("the DHCP Snooping row is on the form", snoop is not None)
check("...named", snoop and snoop.get("name"), "DHCP Snooping")

ok("no field the merged menu declares is left as a <0x…> label",
   not any(str(spec.get("name", "")).startswith("<0x")
           and spec.get("code") in schema.record_for(PATH, BRIDGE).fields
           for spec, _ed in win._rows),
   "an overlay field showing <0x…> means the object was read against the base")

win.close()

if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall overlay-form-field tests passed")
