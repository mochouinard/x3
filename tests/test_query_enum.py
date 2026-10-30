"""``type:'queryenum'`` -- a dropdown the router computes rather than keeps.

No device and no pytest required:

    .venv/bin/python test_query_enum.py

Wireless Frequency has no table behind it. What frequencies a radio may use
depends on its band, channel width and country, so the .jg says *ask*::

    {type:'queryenum',path:[88,3],cmd:6,valuesid:'U4',bvaluesid:'U5',
     params:[{name:'wlan',id:'u1'},{name:'Band',id:'u81a'},
             {name:'Channel Width',id:'u81b'},{name:'Country',id:'u8ad'},
             {name:'Frequency Mode',id:'u8ac'}]}

Seven fields declare one -- Frequency on the wireless interface and in six
Quick Set modes -- and none of them had any dropdown at all.

The parameters are the trap: each is a (name, code) pair where the *name*
finds the field on the form being filled in and the *code* is what the target
handler calls it. Quick Set keeps Band at 0x259 and the query wants it as
0x81a, so copying code-for-code asks the router with nothing at all.

``bvaluesid`` is a second number array whose meaning is not settled; it is used
as labels only if it ever arrives as strings, and the values label themselves
otherwise -- which is what Winbox shows.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QComboBox

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import ALT_CODE_ROLE, PropertyWindow

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
HOME_AP = (120, 4)
home = next(x for x in schema.menus if x.path == HOME_AP and x.kind == "item")
freq = home.fields[0x25D]

# Home AP's own codes for the query's parameters.
QS_WLAN, QS_BAND, QS_WIDTH = 0x260, 0x259, 0x25E
QS_COUNTRY, QS_FREQ_MODE = 0x25F, 0x25B
# What handler (88,3) calls them.
W_WLAN, W_BAND, W_WIDTH = 0x1, 0x81A, 0x81B
W_COUNTRY, W_FREQ_MODE = 0x8AD, 0x8AC


# --- the schema side --------------------------------------------------------

check("the query behind Frequency is parsed",
      (freq.enum_query or {}).get("path"), (88, 3))
check("...with the command to send", (freq.enum_query or {}).get("cmd"), 6)
check("...the array to read the answer from",
      (freq.enum_query or {}).get("values_id"), 4)
check("...and its parameters, named and coded",
      [(n, hex(c)) for n, c in (freq.enum_query or {}).get("params") or []],
      [("wlan", "0x1"), ("Band", "0x81a"), ("Channel Width", "0x81b"),
       ("Country", "0x8ad"), ("Frequency Mode", "0x8ac")])
ok("no static vocabulary comes with it", not freq.enum and not freq.enum_path)
check("every Frequency in the schema has one",
      sum(1 for mn in list(schema.menus) + list(schema.overlay_list)
          for f in mn.fields.values() if f.enum_query), 7)

# The name is what finds the value on the form being filled in.
row_values = {QS_BAND: 7, QS_COUNTRY: 3, QS_WLAN: 2, QS_FREQ_MODE: 1}
check("a parameter is looked up by name, not by code",
      sch.field_value_by_name(home, "Band", row_values), 7)
ok("...and a code-for-code read finds nothing",
   row_values.get(W_BAND) is None)


# --- the client -------------------------------------------------------------

class StubClient(DeviceClient):
    """A client whose only device is a recorded request/reply pair."""

    def __init__(self, reply: m.Message | None = None, fail: bool = False):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.schema = schema
        self.session = None
        self.sent: list[tuple[tuple[int, ...], int, dict]] = []
        self._reply = reply
        self._fail = fail

    def _request(self, path, command, *, from_path=None, extra=None, quiet=False):
        msg = m.Message()
        if extra is not None:
            extra(msg)
        self.sent.append((tuple(path), command, {c: v for c, _t, v in msg}))
        if self._fail:
            raise __import__("x3.proto.device", fromlist=["DeviceError"]).DeviceError(
                "no such handler"
            )
        return self._reply if self._reply is not None else m.Message()

    def menu_for(self, path, type_value=None, values=None, **kw):
        if type_value is None:
            return schema.record(path)
        return schema.record_for(path, type_value, values)

    def alloc_from(self):
        return (0, 9)

    def getobj(self, path, obj_id, **kw):
        raise AssertionError("not used")

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def interface_choices(self):
        return []


answer = m.Message()
answer.add_u32_array(4, [2412, 2417, 2422])
answer.add_u32_array(5, [0, 0, 1])          # the second array, meaning unsettled
client = StubClient(answer)
entries = client.query_enum(freq.enum_query, {W_BAND: 7, W_COUNTRY: 3})
check("the query's answer becomes the dropdown's entries", entries,
      {2412: "2412", 2417: "2417", 2422: "2422"})
check("...asked of the handler the .jg names",
      [(p, c) for p, c, _f in client.sent], [((88, 3), 6)])
check("...with the parameters under the codes that handler uses",
      client.sent[0][2], {W_BAND: 7, W_COUNTRY: 3})
check("a number array is not mistaken for labels",
      sorted(entries.values()), ["2412", "2417", "2422"])

client.query_enum(freq.enum_query, {W_BAND: 7, W_COUNTRY: 3})
check("the same question is not asked twice", len(client.sent), 1)
client.query_enum(freq.enum_query, {W_BAND: 8, W_COUNTRY: 3})
check("...but a different band is a different question", len(client.sent), 2)

labelled = m.Message()
labelled.add_u32_array(4, [2412, 2417])
labelled.add_string_array(5, ["2412 (ch 1)", "2417 (ch 2)"])
check("string labels, if they ever arrive, are used",
      StubClient(labelled).query_enum(freq.enum_query, {}),
      {2412: "2412 (ch 1)", 2417: "2417 (ch 2)"})

single = m.Message()
single.add_u32(4, 2412)          # The router collapses a one-element array
check("a one-frequency answer is still a list",
      StubClient(single).query_enum(freq.enum_query, {}), {2412: "2412"})

err = m.Message()
err.add_u32(0xFF0008, 6)
check("an error answers with nothing to offer",
      StubClient(err).query_enum(freq.enum_query, {}), {})
check("...and so does a handler that is not there",
      StubClient(fail=True).query_enum(freq.enum_query, {}), {})


# --- the form ---------------------------------------------------------------

app = QApplication.instance() or QApplication(sys.argv)


def make_form(reply, band=7):
    msg = m.Message()
    msg.add_u32(QS_WLAN, 2)
    msg.add_u32(QS_BAND, band)
    msg.add_u32(QS_WIDTH, 0)
    msg.add_u32(QS_COUNTRY, 3)
    msg.add_u32(QS_FREQ_MODE, 1)
    msg.add_u32(0x25D, 2417)
    values = {c: v for c, _t, v in msg}
    row = RowObject(
        path=HOME_AP, raw=msg, fields=schema.decode(HOME_AP, msg),
        form_fields=schema.decode_form(HOME_AP, msg),
        values_by_code=values,
    )
    cl = StubClient(reply)
    win = PropertyWindow(
        "Home AP", HOME_AP, row, client=cl,
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=home, row_key="qs", full_row=True,
    )
    app.processEvents()
    return cl, win


def find(win, label, tab=None):
    # Quick Set is one page: its `tab inline:1` sections are rules inside the
    # form, not tab pages, so a row's tab is General whatever section it is in.
    for spec, editor in win._rows:
        if spec.get("name") == label:
            return spec, editor
    return None, None


def open_tab(win, name):
    if win._tabs is None:
        return True          # one page: everything is already built

    for i in range(win._tabs.count()):
        if win._tabs.tabText(i) == name:
            win._tabs.setCurrentIndex(i)
            app.processEvents()
            return True
    return False


cl, win = make_form(answer)
open_tab(win, "Wireless")
_spec, editor = find(win, "Frequency", "Wireless")
ok("Frequency is a dropdown now", isinstance(editor, QComboBox),
   f"{type(editor).__name__}")
if isinstance(editor, QComboBox):
    check("...holding 'auto' and what the router answered, in one box",
          [editor.itemText(i) for i in range(editor.count())],
          ["auto", "2412MHz", "2417MHz", "2422MHz"])
    check("...with the row's own frequency selected", editor.currentData(), 2417)
    check("...and that entry writes to the union member that owns it",
          editor.itemData(editor.currentIndex(), ALT_CODE_ROLE), 0x25D)
    check("...while 'auto' stays with the primary field",
          editor.itemData(0, ALT_CODE_ROLE), None)
# `cl.sent` also holds the one session-handler read the form makes for the
# device's capability bits, so pick out the frequency query itself.
freq_queries = [f for p, _c, f in cl.sent if p == (88, 3)]
check("...and the query carried the form's band",
      [f.get(W_BAND) for f in freq_queries], [7])

# Changing a parameter re-asks. Channel Width is one of the five, and unlike
# Band it is not narrowed to a single entry by this fixture's radio.
_wspec, width_editor = find(win, "Channel Width", "Wireless")
ok("Channel Width is on the form to change", isinstance(width_editor, QComboBox))
before = len([1 for p, _c, _f in cl.sent if p == (88, 3)])
other = next(
    i for i in range(width_editor.count())
    if width_editor.itemData(i) != width_editor.currentData()
)
width_editor.setCurrentIndex(other)
app.processEvents()
ok("a new channel width re-asks the router",
   len([1 for p, _c, _f in cl.sent if p == (88, 3)]) > before,
   f"{cl.sent}")
last_query = [f for p, _c, f in cl.sent if p == (88, 3)][-1]
check("...with the width that is now on the form",
      last_query.get(W_WIDTH), width_editor.currentData())
check("...and every other parameter still along with it",
      sorted(last_query), sorted([W_WLAN, W_BAND, W_WIDTH, W_COUNTRY,
                                  W_FREQ_MODE]))
before = len([1 for p, _c, _f in cl.sent if p == (88, 3)])
_nspec, name_editor = find(win, "Network Name", "Wireless")
if name_editor is not None and hasattr(name_editor, "setText"):
    name_editor.setText("office-guest")
    app.processEvents()
check("...while an edit that changes no parameter asks nothing",
      len([1 for p, _c, _f in cl.sent if p == (88, 3)]), before)
win._allow_close = True
win.close()

# A device that cannot answer must not leave an empty dropdown behind.
cl2, win2 = make_form(m.Message())
open_tab(win2, "Wireless")
_s2, e2 = find(win2, "Frequency", "Wireless")
ok("an unanswered query falls back to a plain box, not an empty list",
   not isinstance(e2, QComboBox) or e2.count() > 0,
   f"{type(e2).__name__} with {getattr(e2, 'count', lambda: '-')()} items")
win2._allow_close = True
win2.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall queryenum tests passed")
