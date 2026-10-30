"""``label``, ``alias``, ``object`` and ``addon`` -- the last of the form chrome.

No device and no pytest required:

    .venv/bin/python test_form_chrome.py

Four small things the parser walked straight past:

* **``{type:'label'}``** (25) is a caption with no value. Quick Set's dual-band
  modes put ``{type:'tuple',c:[{type:'label',name:'2GHz'},
  {type:'label',name:'5GHz'}]}`` over the rows that pair the two radios, and a
  tuple with captions and no ids produced no row at all.

* **``{type:'alias'}``** (26) places the field of that name here *as well*. The
  Mangle action deck declares Passthrough once -- in the pane that owns the
  wire id -- and aliases it into every other action's pane, so it has to be
  exempt from the one-row-per-code rule the rest of the form follows.

* **``{type:'object',on:…}``** (19) is a block belonging to one state of the
  object: Quick Set's CPE declares one for ``cpediscon`` and another for
  ``cpeconn``. The condition is on the block, so it has to reach the fields
  inside it -- including past a ``tab`` inside the block, which is what left 22
  of those 23 rows ungated.

* **``{type:'addon',value:'ipv6'}``** (24) names a package. Two of the four
  values ship a plugin of their own, so their absence is visible in the file
  index the device serves; the other two are unanswerable and stay permissive.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

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
MANGLE = (20, 44)
mangle = next(mn for mn in schema.menus if mn.path == MANGLE)
dual = next(mn for mn in schema.menus
            if mn.title == "Home AP Dual" and mn.kind == "item")
cpe = next(mn for mn in schema.menus
           if mn.title == "CPE" and mn.kind == "item")

PASSTHROUGH = 0x3F1


# --- labels -----------------------------------------------------------------

labels = [i for i in dual.form_layout if i.get("kind") == "label"]
names = [i["name"] for i in labels]
ok("a tuple of captions becomes a caption row", "2GHz 5GHz" in names,
   f"{names}")
ok("...alongside the sections an `inline` tab heads",
   {"Wireless", "Internet", "System"} <= set(names), f"{names}")
ok("a caption carries no wire code",
   all(i.get("code") is None for i in labels))
check("...and Quick Set is one page, as Winbox draws it",
      dual.tab_names(), ["General"])


# --- aliases ----------------------------------------------------------------

aliases = [i for i in mangle.form_layout if i.get("kind") == "alias"]
ok("Mangle aliases Passthrough into every action's pane", len(aliases) >= 4,
   f"{len(aliases)}")
check("...each gated by the pane it sits in",
      len({a.get("on") for a in aliases}), len(aliases))
ok("...all of them naming the same field",
   {a["name"] for a in aliases} == {"Passthrough"})
declared = [
    i for i in mangle.form_layout
    if i.get("kind") == "field" and i.get("code") == PASSTHROUGH
]
ok("...and the field itself is still declared once, where it owns the id",
   len(declared) == 1, f"{len(declared)}")


# --- object blocks ----------------------------------------------------------

on_counts: dict = {}
for item in cpe.form_layout:
    on_counts[item.get("on")] = on_counts.get(item.get("on"), 0) + 1
ok("a CPE's disconnected block gates its rows",
   on_counts.get("cpediscon", 0) > 10, f"{on_counts.get('cpediscon')}")
ok("...and its connected block gates its own",
   on_counts.get("cpeconn", 0) > 5, f"{on_counts.get('cpeconn')}")
ok("both conditions are real ones the menu declares",
   {"cpediscon", "cpeconn"} <= set(cpe.conditions))
ok("a field with an `on:` of its own keeps it",
   on_counts.get("pppoe", 0) > 0)
check("the two blocks are mutually exclusive, so one form shows one of them",
      [sch.condition_is_true(cpe, name, {}) for name in
       ("cpediscon", "cpeconn")].count(True) <= 1, True)


# --- addon packages ---------------------------------------------------------

PLUGINS = {"roteros.jg", "wave2.jg", "dhcp.jg", "secure.jg"}
check("a package with no plugin on the device fails its predicate",
      sch.menu_pred_holds({"type": "addon", "value": "ipv6"},
                          board="ap", plugins=PLUGINS), False)
check("...and one that is there holds",
      sch.menu_pred_holds({"type": "addon", "value": "dhcp"},
                          board="ap", plugins=PLUGINS), True)
check("a package that ships no .jg at all stays permissive",
      [sch.menu_pred_holds({"type": "addon", "value": v},
                           board="ap", plugins=PLUGINS)
       for v in ("storage", "oflow")], [True, True])
check("...as does anything, when the plugin set is unknown",
      sch.menu_pred_holds({"type": "addon", "value": "ipv6"},
                          board="ap", plugins=None), True)


# --- on the form ------------------------------------------------------------

class StubClient:
    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, values=None, **kw):
        return mangle

    def alloc_from(self):
        return (0, 13)

    def get(self, path):
        return self._row

    def getobj(self, path, obj_id, **kw):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return sorted((fld.enum or {}).items())

    def interface_choices(self):
        return []

    def query_enum(self, spec, params=None, **kw):
        return {}


app = QApplication.instance() or QApplication(sys.argv)

ACTION = next(
    c for c, f in mangle.fields.items()
    if f.name == "Action" and f.enum
)
msg = m.Message()
msg.add_u32(0xFE0001, 1)
msg.add_u32(ACTION, 31)              # mark-connection: an aliased pane
msg.add_bool(PASSTHROUGH, True)
row = RowObject(
    path=MANGLE, raw=msg, fields=schema.decode(MANGLE, msg),
    form_fields=schema.decode_form(MANGLE, msg),
    values_by_code={c: v for c, _t, v in msg},
)
win = PropertyWindow(
    "Mangle rule", MANGLE, row, client=StubClient(row),
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=mangle, row_key="1", full_row=True,
)
app.processEvents()

for i in range(win._tabs.count() if win._tabs else 0):
    if win._tabs.tabText(i) == "Action":
        win._tabs.setCurrentIndex(i)
        app.processEvents()

passthrough_rows = [
    spec for spec, _e in win._rows if spec.get("name") == "Passthrough"
]
ok("the form builds a Passthrough row per action pane",
   len(passthrough_rows) > 1, f"{len(passthrough_rows)}")
ok("...each with its own pane condition",
   len({s.get("cond_on") for s in passthrough_rows}) == len(passthrough_rows),
   f"{[s.get('cond_on') for s in passthrough_rows]}")
ok("...and all reading the same wire code",
   {s.get("code") for s in passthrough_rows} == {PASSTHROUGH})
win._allow_close = True
win.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall form-chrome tests passed")
