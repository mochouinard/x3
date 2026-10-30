"""``ro:1`` on the menu node -- the form of readings, and its one button.

No device and no pytest required:

    .venv/bin/python test_menu_readonly.py

``ro`` was read on a *field* and on a container that wraps fields, but not on
the menu node itself, where 104 of the 9 plugin files' item/map nodes declare
it: System ▸ Resources, About, License, Log, Neighbors, Active Users, every
Connections list. So System ▸ Resources came back with eighteen writable
fields and the full OK / Cancel / Apply stack -- a form offering to write Free
Memory to the router.

Three things follow from the flag, and this file pins all of them:

* **Fields.** The menu's ``ro`` seeds ``inherited_ro``, the same path a ``ro``
  container already took, so every field under it is read-only and every
  editor on the form opens locked.

* **Form buttons.** Cancel and Apply are the two halves of an edit -- revert
  it, send it -- and a form with nothing to edit has neither. OK is left, and
  it is not a write button: it closes the window, so it stays live even in a
  read-only session.

* **List toolbar.** Add goes with it. That in turn is what finally reads
  ``removable``, which looked meaningless on its own -- 380 lists that plainly
  allow Remove declare nothing, so it cannot mean "Remove is allowed". It is
  load-bearing on exactly the lists you *cannot* add to, where it separates
  Active Users and Connections (drop a session, kill a connection: ``-`` but
  no ``+``) from Neighbors, Log and Packages (neither). Add and Remove alike
  are also off on a ``nonaddable`` list that declares no ``removable`` -- IP ▸
  Services, whose eight rows you configure but never create or destroy.

Enable/Disable and Comment come from the same place they always did,
``type:enable`` and ``type:comment``; they were simply never read on the list
toolbar, so Active Users offered to disable a login session.

Two deliberate exemptions:

* The `request` half of a query menu. Those are the parameters you fill in --
  QoS Monitor is `ro` because its *results* are readings, and locking its
  request would leave a query you cannot run.
* A ``generic`` base. Interface and Files are ``nonaddable`` because a row is
  created from a type overlay (Add ▸ VLAN) or by upload, not because the
  device owns it, and Winbox removes a VLAN from the Interface list.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import jg
from x3.proto import message as m
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.list_pane import ListPane
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

RESOURCES = (24, 2)
ADDRESS = (20, 1)        # IP ▸ Addresses -- an ordinary writable list

resources = next(mn for mn in schema.menus
                 if mn.path == RESOURCES and mn.title == "Resources")
address = next(mn for mn in schema.menus
               if mn.path == ADDRESS and mn.group == "IP")


# --- the flag is in the file ------------------------------------------------

def jg_ro_nodes():
    """Every item/map node in the 9 plugin files that declares ``ro:1``."""
    found = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") in ("item", "map") and node.get("ro"):
            found.append(node.get("title") or node.get("name"))
        for key in ("c", "request"):
            if key in node:
                walk(node[key])

    for name in sorted(os.listdir(SCHEMA_DIR)):
        if name.endswith(".jg"):
            walk(jg.load(os.path.join(SCHEMA_DIR, name)))
    return found


declared = jg_ro_nodes()
ok("the .jg marks whole menus read-only, not only fields",
   len(declared) > 90, f"{len(declared)}")
ok("...System ▸ Resources among them", "Resources" in declared)
ok("...and the other windows that are all readings",
   {"About", "License", "Log", "Neighbors", "Active Users"} <= set(declared),
   f"{sorted(set(declared))[:12]}")

check("the flag reaches the menu", resources.read_only, True)
check("...and a menu that does not declare it stays writable",
      address.read_only, False)

parsed_ro = {mn.title for mn in schema.menus if mn.read_only}
ok("every declaring menu carries it", len(parsed_ro) > 50, f"{len(parsed_ro)}")
ok("...and no writable list picked it up",
   not {"Address", "Routes", "Filter Rules"} & parsed_ro,
   f"{sorted(parsed_ro & {'Address', 'Routes', 'Filter Rules'})}")


# --- and reaches every field under it ---------------------------------------

ok("Resources has fields at all", len(resources.fields) > 10,
   f"{len(resources.fields)}")
writable = [f.name for f in resources.fields.values() if not f.ro]
check("...and not one of them is a setting", writable, [])
ok("...including the ones the .jg leaves unmarked (Free Memory, Uptime)",
   all(f.ro for f in resources.fields.values()
       if f.name in ("Free Memory", "Uptime", "CPU Load")))
ok("a writable menu is untouched by this",
   any(not f.ro for f in address.fields.values()))

qos = next((mn for mn in schema.menus
            if mn.title == "QoS Monitor" and mn.read_only), None)
if qos is not None and qos.request_fields:
    ok("a read-only query still lets you fill in its parameters",
       any(not f.ro for f in qos.request_fields.values()),
       f"{[f.name for f in qos.request_fields.values() if not f.ro]}")


# --- on the form ------------------------------------------------------------

class StubClient:
    schema = schema

    def __init__(self, row, menu):
        self._row = row
        self._menu = menu

    def menu_for(self, path, type_value=None, values=None, **kw):
        return self._menu

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


def open_form(menu, mode=EditMode.QUEUE):
    msg = m.Message()
    msg.add_u32(0xFE0001, 1)
    row = RowObject(
        path=menu.path, raw=msg, fields=schema.decode(menu.path, msg),
        form_fields=schema.decode_form(menu.path, msg),
        values_by_code={c: v for c, _t, v in msg},
    )
    win = PropertyWindow(
        menu.title, menu.path, row, client=StubClient(row, menu),
        edit_session=EditSession(mode, client=None),
        menu=menu, row_key="1", full_row=True,
    )
    app.processEvents()
    return win


def button_labels(win):
    return [b.text() for b in win._action_btns]


res_win = open_form(resources)
labels = button_labels(res_win)
ok("the Resources form keeps OK", "OK" in labels, f"{labels}")
ok("...and offers no Apply", "Apply" not in labels, f"{labels}")
ok("...and no Cancel", "Cancel" not in labels, f"{labels}")
check("...OK first, as it is on every other form", labels[0], "OK")
ok("every editor on it opens locked",
   all(spec.get("locked") for spec, _e in res_win._rows),
   f"{[s.get('name') for s in (sp for sp, _e in res_win._rows) if not s.get('locked')]}")
check("...and the form is locked", res_win._ro, True)
ok("...with a title that does not offer a mode to switch to",
   res_win.windowTitle().endswith("[readings · read-only]"),
   f"{res_win.windowTitle()!r}")
ok("OK is not a write button, so a read-only session cannot grey it out",
   not any(b.text() == "OK" for b in res_win._write_action_btns),
   f"{[b.text() for b in res_win._write_action_btns]}")

addr_win = open_form(address)
labels = button_labels(addr_win)
ok("an ordinary form is unchanged: OK / Cancel / Apply",
   labels[:3] == ["OK", "Cancel", "Apply"], f"{labels[:3]}")
ok("...and it does have editable rows",
   any(not spec.get("locked") for spec, _e in addr_win._rows))

# A read-only *session* is the other way a form locks. The one button a ro:1
# menu has left must survive it -- pressing OK is how you close the window.
res_ro = open_form(resources, EditMode.READONLY)
okbtn = next(b for b in res_ro._action_btns if b.text() == "OK")
ok("OK stays live in a read-only session too", okbtn.isEnabled())

for win in (res_win, addr_win, res_ro):
    win._allow_close = True
    win.close()


# --- the five standard verbs, per menu ---------------------------------------

# Each of these is a window in Winbox 3, and the verbs are what its toolbar
# actually shows. `+` Add, `-` Remove, `✓`/`✗` Enable and Disable, `C` Comment.
VERBS = {
    #  group / title              add    remove enable comment
    ("System", "Active Users"): (False, True,  False, False),
    ("IP", "Neighbors"):        (False, False, False, False),
    (None, "Log"):              (False, False, False, False),
    ("IP", "Services"):         (False, False, True,  False),
    ("IP", "Connections"):      (False, True,  False, False),
    ("System", "Packages"):     (False, False, True,  False),
    ("IP", "Address"):          (True,  True,  True,  True),
    ("IP", "Filter Rules"):     (True,  True,  True,  True),
    # `nonaddable` but a `generic` base: rows come from the type overlays
    # (Add ▸ VLAN), and Winbox removes a VLAN from this window.
    (None, "Interface"):        (False, True,  True,  True),
    # `nonaddable` and says `removable` outright.
    ("System", "Ports"):        (False, True,  False, False),
}

by_title: dict = {}
for mn in schema.menus:
    if mn.kind == "map" and not mn.nonpublic:
        by_title.setdefault((mn.group, mn.title), mn)

for key, want in VERBS.items():
    mn = by_title.get(key)
    if mn is None:
        failures.append(f"no menu {key}")
        print(f"FAIL no menu {key}")
        continue
    got = (mn.can_add, mn.can_remove, mn.has_enable, mn.has_comment)
    name = f"{key[0] or '—'} ▸ {key[1]}"
    check(f"{name}: add/remove/enable/comment", got, want)

# The rule may only ever take a verb away from a list you cannot add to: the
# 267 ordinary ones are what `removable` was never about.
ordinary = [mn for mn in schema.menus
            if mn.kind == "map" and not mn.nonpublic and mn.can_add]
ok("every list you can add to can also be removed from",
   all(mn.can_remove for mn in ordinary),
   f"{[mn.title for mn in ordinary if not mn.can_remove][:5]}")
ok("...and that is most of them", len(ordinary) > 250, f"{len(ordinary)}")

fixed = [mn for mn in schema.menus
         if mn.kind == "map" and not mn.nonpublic and not mn.can_add]
ok("a ro list is never one you can add to",
   all(not mn.can_add for mn in schema.menus if mn.read_only and mn.is_collection))
ok("...and some of the fixed ones still have Remove",
   0 < len([mn for mn in fixed if mn.can_remove]) < len(fixed),
   f"{len([mn for mn in fixed if mn.can_remove])}/{len(fixed)}")


# --- and on the toolbar ------------------------------------------------------

def toolbar_state(menu, *, type_value=None):
    """{verb: enabled} for the five standard buttons, after the first load.

    With a row selected and the session unlocked, i.e. the state in which every
    verb the menu *does* have is live -- so whatever is still greyed is greyed
    because the .jg said so, not because nothing is picked.
    """
    pane = ListPane(menu.title, path=menu.path, readonly=False,
                    type_value=type_value)
    pane._on_loaded((menu, ["", "Name"], [["", "row1"]], [None], None))
    pane.table.selectRow(0)
    pane._update_selection_actions()
    app.processEvents()
    state = {}
    for act in pane._write_actions_all:
        state[act.text()] = act.isEnabled()
    return pane, state


# _write_actions loses the verbs a menu does not have, so the test needs the
# original five to ask about. They are the first five the bar was given.
ListPane._write_actions_all = property(
    lambda self: [a for a in self.findChildren(type(self._refresh_act))
                  if a.text() in ("Add", "Remove", "Enable", "Disable", "Comment")]
)

for key in (("System", "Active Users"), ("IP", "Services"), ("IP", "Neighbors"),
            ("IP", "Address")):
    mn = by_title[key]
    pane, state = toolbar_state(mn)
    want_add, want_rm, want_en, want_cm = VERBS[key]
    name = f"{key[0] or '—'} ▸ {key[1]} toolbar"
    check(f"{name}: Add", state.get("Add"), want_add)
    check(f"{name}: Remove", state.get("Remove"), want_rm)
    check(f"{name}: Enable/Disable",
          (state.get("Enable"), state.get("Disable")), (want_en, want_en))
    check(f"{name}: Comment", state.get("Comment"), want_cm)
    pane.shutdown()

# A verb the .jg took away must not come back when a row is selected, nor when
# the session leaves read-only -- those are the two paths that re-enable in
# bulk.
mn = by_title[("System", "Active Users")]
pane, _ = toolbar_state(mn)
pane.table.selectRow(0)
pane._update_selection_actions()
pane.set_readonly(False)
app.processEvents()
state = {a.text(): a.isEnabled() for a in pane._write_actions_all}
ok("a selected row does not resurrect Enable on Active Users",
   not state.get("Enable") and not state.get("Disable"), f"{state}")
ok("...nor does leaving read-only resurrect Add", not state.get("Add"), f"{state}")
check("...while Remove, which it does have, is live", state.get("Remove"), True)
pane.shutdown()


# `nonaddable` on a type overlay, which is where the .jg puts it -- and the
# proof that the base's flag is not inherited is that the overlays bother to
# declare it at all. Ethernet, LTE, W60G, PWR, Loopback and VRF each say it for
# themselves: there is no + on a window listing ports that exist in hardware.
# Bridge, VLAN, Bonding, the tunnels and 30 others say nothing, and Winbox
# draws the + on every one of them -- adding is the entire point of those
# windows. Merging the two with AND left a dead + on all of them.

iface = by_title[(None, "Interface")]
tvs = sorted(schema.family_type_values(iface.generic))
by_type = {schema.type_title_for(iface, tv): tv for tv in tvs}
merged = {t: schema.merge_type_overlay(iface, tv) for t, tv in by_type.items()}

check("the bare Interface list adds nothing: it lists every kind at once",
      iface.can_add, False)
check("a Bridge is a row you add", merged["Bridge"].can_add, True)
check("...an Ethernet port is not", merged["Ethernet"].can_add, False)
addable = {t for t, mn in merged.items() if mn.can_add}
ok("...and it is the .jg's own split, not a list of names here",
   {"VLAN", "Bonding", "VXLAN", "VETH", "WireGuard", "GRE Tunnel"} <= addable
   and not ({"LTE", "W60G", "Loopback", "VRF"} & addable),
   f"{sorted(addable)[:8]}")
ok("...most types are addable, a handful are hardware",
   len(addable) > 30 and len(merged) - len(addable) >= 6,
   f"{len(addable)}/{len(merged)}")

# And on the toolbar, which is what the complaint was: the Bridge window's +.
pane, state = toolbar_state(merged["Bridge"], type_value=by_type["Bridge"])
check("Bridge window: Add is live", state.get("Add"), True)
check("...and Remove with it", state.get("Remove"), True)
pane.shutdown()

pane, state = toolbar_state(merged["Ethernet"], type_value=by_type["Ethernet"])
check("Ethernet window: no Add", state.get("Add"), False)
check("...but Remove, which the generic base allows", state.get("Remove"), True)
pane.shutdown()

pane, state = toolbar_state(iface)
check("the bare Interface window: no Add either", state.get("Add"), False)
pane.shutdown()


# --- the property form's own Copy / Remove -----------------------------------

active = by_title[("System", "Active Users")]
services = by_title[("IP", "Services")]

act_win = open_form(active)
labels = button_labels(act_win)
ok("an Active Users row keeps Remove", "Remove" in labels, f"{labels}")
ok("...but not Copy, which is an Add with the fields filled in",
   "Copy" not in labels, f"{labels}")
ok("...and no Enable, which its rows do not have",
   not {"Enable", "Disable"} & set(labels), f"{labels}")

svc_win = open_form(services)
labels = button_labels(svc_win)
ok("an IP Service is editable, so it keeps Apply",
   {"OK", "Cancel", "Apply"} <= set(labels), f"{labels}")
ok("...and Enable, which is all the .jg gives it besides",
   "Disable" in labels or "Enable" in labels, f"{labels}")
ok("...with neither Copy nor Remove",
   not {"Copy", "Remove"} & set(labels), f"{labels}")

for win in (act_win, svc_win):
    win._allow_close = True
    win.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall good")
