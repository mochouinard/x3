"""``type:group`` -- the folds Winbox draws around a group of form rows.

No device and no pytest required:

    .venv/bin/python test_group_folds.py

A named ``group`` in the .jg is not a caption over its fields, it is a fold.
Winbox 3.43 on 7.21.1 draws a rule with a triangle where the group is declared
and keeps the group's rows hidden until it is clicked -- a firewall rule's Extra
tab is five of them, a CAPs Rate is six::

                 Nth: [            ] ▼
    ─ ▲ Connection Limit ───────────────
               Limit: [!] [100] / [32]
    ─ ▼ Limit ─────────────────────────

Three things follow, and this pins all three:

* **Position.** The header sits where the group is declared, and only the
  group's own children fold with it: on the Extra tab, Src. Address Type and
  the three rows after it follow the Connection Limit fold in declaration order
  but belong to no group, so the fold does not touch them.
* **The flag.** Most groups carry an id, and it is a bool -- ``{name:'Limit',
  type:'group', id:'b1a0'}`` is the firewall's "this match is in use" flag. It
  decides whether the fold starts open, and opening the fold sets it: the rows
  inside mean nothing to the router until it is set, which is why the fold is
  an editor and not just a view. A group with no id (a CAPs Rate's rate grids)
  folds locally and writes nothing.
* **The rows it owns.** Flattened, a group's fields were indistinguishable from
  the rows around them; now each row remembers its fold, and a row in a closed
  fold stays hidden however its ``on:`` condition evaluates.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QFormLayout

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.group_fold import GroupFold
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
RATES = (88, 164)               # Wireless ▸ CAPsMAN ▸ Rates: six flagless folds
FILTER = (20, 3)                # IP ▸ Firewall ▸ Filter Rules: flagged folds

LIMIT = 0x0001A0                # `{name:'Limit', type:'group', id:'b1a0'}`
DST_LIMIT = 0x0001A4
TIME = 0x0001A5
PSD = 0x0001A7
CONN_LIMIT = 0x00019F


# --- the schema side --------------------------------------------------------

rates = schema.record(RATES)
folds = [it for it in rates.form_layout if it.get("kind") == "group"]
check("a named group is a row of the layout, not a silent container",
      [it["title"] for it in folds],
      ["Basic Rates", "Supported Rates", "HT Basic MCS", "HT Supported MCS",
       "VHT Basic MCS", "VHT Supported MCS"])
check("...and each of its fields remembers the fold it is in",
      [it.get("group") == folds[0]["group"] for it in rates.form_layout
       if it.get("kind") == "field" and it.get("code") == 0xA000],
      [True])
ok("a group with no id has no flag to write",
   all("code" not in it for it in folds))

filt = schema.record(FILTER)
extra = [it for it in filt.form_layout if it.get("tab") == "Extra"]


def shape(item):
    code = item.get("code")
    name = item.get("title") or (
        filt.fields[code].name if code in filt.fields else "?")
    return (item.get("kind"), name, bool(item.get("group")))


check("the Extra tab is laid out the way Winbox lays it out",
      [shape(it) for it in extra[:8]],
      [("field", "Nth", False),
       ("group", "Connection Limit", True),
       # These four follow the fold in declaration order but are not in it.
       ("field", "Src. Address Type", False),
       ("field", "Dst. Address Type", False),
       ("field", "Hotspot", False),
       ("field", "IP Fragment", False),
       ("group", "Limit", True),
       ("field", "Burst", True)])
check("a group's id becomes the bool the fold writes",
      [(filt.fields[c].name, filt.fields[c].jg_type)
       for c in (LIMIT, DST_LIMIT, TIME, PSD)],
      [("Limit", "bool"), ("Dst. Limit", "bool"), ("Time", "bool"),
       ("PSD", "bool")])
check("...and the header carries it",
      next(it["code"] for it in extra if it.get("title") == "Limit"), LIMIT)


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


def make_form(path, *, bools=None, numbers=None, title="row"):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    for code, value in (numbers or {}).items():
        msg.add_u32(code, value)
    for code, value in (bools or {}).items():
        msg.add_bool(code, value)
    values, multi = {}, {}
    for code, tag, value in msg:
        values[code] = value
        multi.setdefault(code, []).append((tag, value))
    row = RowObject(
        path=path, raw=msg,
        fields=schema.decode(path, msg),
        form_fields=schema.decode_form(path, msg),
        values_by_code=values, values_by_code_multi=multi,
    )
    win = PropertyWindow(
        title, path, row,
        client=StubClient(row),
        edit_session=EditSession(EditMode.QUEUE, client=None),
        menu=schema.record(path),
        row_key="1",
        full_row=True,
    )
    app.processEvents()
    return win


def tab_index(win, name):
    for i in range(win._tabs.count()):
        if win._tabs.tabText(i) == name:
            return i
    return -1


def open_tab(win, name):
    i = tab_index(win, name)
    if i < 0:
        return False
    win._tabs.setCurrentIndex(i)
    app.processEvents()
    return True


def fold_of(win, name):
    for spec, editor in win._rows:
        if isinstance(editor, GroupFold) and spec.get("name") == name:
            return editor
    return None


def row_of(win, label):
    """(QFormLayout, row index) of the editor whose row reads ``label``."""
    for spec, editor in win._rows:
        if spec.get("name") != label or isinstance(editor, GroupFold):
            continue
        parent = editor.parentWidget()
        while parent is not None:
            layout = parent.layout()
            if isinstance(layout, QFormLayout):
                return layout, layout.getWidgetPosition(editor)[0]
            parent = parent.parentWidget()
    return None, -1


app = QApplication.instance() or QApplication(sys.argv)

# A CAPs Rate: six folds, no flags, all closed.
win = make_form(RATES, title="rate1")
names = [spec["name"] for spec, ed in win._rows if isinstance(ed, GroupFold)]
check("every group on the form is a fold", names,
      ["Basic Rates", "Supported Rates", "HT Basic MCS", "HT Supported MCS",
       "VHT Basic MCS", "VHT Supported MCS"])
basic = fold_of(win, "Basic Rates")
ok("...closed, the way Winbox opens the dialog", not basic.expanded)
ok("...with nothing to write", not basic.flag)
form, index = row_of(win, "Basic Rates")
ok("the rows it owns are hidden with it", not form.isRowVisible(index))
ok("...and a row outside every fold is not", form.isRowVisible(
    row_of(win, "Name")[1]))

basic.toggle()
app.processEvents()
ok("clicking the triangle opens the fold", basic.expanded)
ok("...and its rows come with it", form.isRowVisible(index))
check("...but a flagless fold queues nothing", win.edits(changed_only=True), [])
basic.toggle()
app.processEvents()
ok("clicking again closes it", not basic.expanded and not form.isRowVisible(index))
win._allow_close = True
win.close()

# A firewall rule: the folds carry flags, and the flag is the fold's state.
win = make_form(FILTER, bools={LIMIT: False, TIME: False}, title="rule")
ok("the Extra tab opens", open_tab(win, "Extra"))
limit = fold_of(win, "Limit")
ok("a flagged fold knows it has one to write", limit.flag)
ok("...and starts closed while the match is unused", not limit.expanded)
burst, burst_row = row_of(win, "Burst")
ok("its rows start hidden too", not burst.isRowVisible(burst_row))
ok("Connection Limit is not drawn at all",
   fold_of(win, "Connection Limit") is None,
   "its only child is a `not` wrapping a tuple, which the parser drops -- an "
   "empty fold is worse than no fold")

limit.toggle()
app.processEvents()
check("opening the fold sets the match's flag",
      [(e["code"], e["old"], e["new"]) for e in win.edits(changed_only=True)],
      [(LIMIT, False, True)])
ok("...and shows the rows it gates", burst.isRowVisible(burst_row))
limit.toggle()
app.processEvents()
check("...closing it again puts the flag back",
      win.edits(changed_only=True), [])
win._allow_close = True
win.close()

# A rule that *does* use the match opens with that fold already open.
win = make_form(FILTER, bools={LIMIT: True}, title="limited")
ok("the Extra tab opens", open_tab(win, "Extra"))
limit = fold_of(win, "Limit")
ok("a match in use opens its fold", limit.expanded)
burst, burst_row = row_of(win, "Burst")
ok("...with its rows on screen", burst.isRowVisible(burst_row))
ok("...and the other folds still closed", not fold_of(win, "Time").expanded)
check("nothing is queued by just opening the form",
      win.edits(changed_only=True), [])
win._allow_close = True
win.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall group-fold tests passed")
