"""A handler path is not always one menu, and the .jg name is what picks.

No device and no pytest required:

    .venv/bin/python test_shared_paths.py

Handler ``(20, 17)`` is a single settings object that six unrelated dialogs
each show a slice of: IP Settings, IPv6 Settings, Bridge Settings, Connection
Tracking, Discovery Settings and L3 Hw Settings. Their field sets are
*disjoint*, so resolving the path by "largest field set wins" does not pick a
slightly wrong form -- it picks a different dialog outright, and every one of
those six menus opened L3 Hw Settings' three tabs of switch-offload knobs.

Winbox tells them apart the only way the .jg allows: by which menu you clicked.
The nav node carries that menu's ``name`` -- unique within a path where the
titles are not, since both Bridge's dialog and IP's are *titled* "Settings" --
and it has to survive into the payload, the window and back down to
``Schema.record``. Pinned here at both ends: the schema honours a name, and the
nav payload still carries one for it to honour.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from x3.proto.schema import Schema
from x3.ui.widgets.main_window import MainWindow
from x3.ui.widgets.menu_tree import (
    _click_target,
    _lone_child,
    _payload_from_nav,
)

SCHEMA_DIR = "7.21.1-151146055"
SETTINGS = (20, 17)

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


schema = Schema.load(SCHEMA_DIR)


# --- the premise: one path, six disjoint dialogs ---------------------------

sharing = [m for m in schema.by_path[SETTINGS] if m.kind in ("map", "item")]
ok(
    "six menus share (20,17)",
    len(sharing) == 6,
    f"{[m.name for m in sharing]}",
)
seen: dict[int, str] = {}
overlap = {
    f.code for m in sharing for f in m
    if seen.setdefault(f.code, m.name) != m.name
}
ok(
    "...and their fields do not overlap, so a wrong pick shows a wrong dialog",
    not overlap,
    f"shared codes {sorted(hex(c) for c in overlap)}",
)
names = [m.name for m in sharing]
ok(
    "...and the name is unique among them, which is what the fix leans on",
    len(set(names)) == len(names),
    f"{names}",
)
ok(
    "...where the title is not even the menu's own name",
    {m.name: m.title for m in sharing if m.title != m.name}
    == {"Bridge Settings": "Settings", "Connection Tracking": "Tracking"},
    "so matching on the tree label would not do",
)


# --- Schema.record honours the name ----------------------------------------

for name, field_name in [
    ("IP Settings", "IP Forward"),
    ("IPv6 Settings", "Disable IPv6"),
    ("Bridge Settings", "Use IP Firewall"),
    ("Connection Tracking", "Loose TCP Tracking"),
    ("Discovery Settings", "Discover Interval"),
    ("L3 Hw Settings", "Autorestart"),
]:
    menu = schema.record(SETTINGS, name=name)
    check(f"{name} resolves to itself", menu.name, name)
    ok(
        f"...and has {field_name!r}",
        any(f.name == field_name for f in menu),
        f"fields are {[f.name for f in menu][:4]}",
    )

check(
    "IP Settings is not L3 Hw Settings' three tabs",
    schema.record(SETTINGS, name="IP Settings").tab_names(),
    ["General"],
)

# A caller that never went through the nav keeps the old heuristic rather than
# getting nothing: an enum source or a write mask is looked up by path alone.
check(
    "no name still resolves",
    schema.record(SETTINGS).name,
    "L3 Hw Settings",
)
check(
    "a name nothing answers to falls back too",
    schema.record(SETTINGS, name="Nonexistent Settings").name,
    "L3 Hw Settings",
)
check(
    "a name is not needed where a path has one menu",
    schema.record((20, 1), name="whatever").name,
    schema.record((20, 1)).name,
)


# --- the nav carries the name down to the window ----------------------------

def find(nodes, path, name):
    for n in nodes:
        if n.path == path and n.name == name:
            return n
        hit = find(list(n.children) + list(n.actions), path, name)
        if hit is not None:
            return hit
    return None


ip_settings = find(schema.nav, SETTINGS, "IP Settings")
ok("IP ▸ Settings is in the nav", ip_settings is not None)
if ip_settings is not None:
    check("...and the payload carries its name",
          _payload_from_nav(ip_settings).get("name"), "IP Settings")

# The folder is what gets clicked; its lone child is what opens (menu_tree's
# _lone_child), and the name has to survive that hop too.
folder = next(
    (f for n in schema.nav if n.title == "IP"
     for f in n.children if f.title == "Settings"),
    None,
)
ok("IP has a Settings folder", folder is not None)
if folder is not None:
    folder_payload = _payload_from_nav(folder)
    kids = folder_payload.get("children") or []
    check("...whose one child is named IP Settings",
          [c.get("name") for c in kids], ["IP Settings"])
    # The route a click actually takes: the folder holds one menu, so the tree
    # opens that menu rather than a one-row flyout (menu_tree._handle).
    lone = _lone_child(folder_payload)
    ok("...and clicking the folder opens that child", lone is not None)
    if lone is not None:
        check("...carrying the name with it",
              (_click_target(lone) or {}).get("name"), "IP Settings")

# Every other path where distinct menus collide: the name has to reach each of
# them, or one of the pair keeps opening the other.
collisions = 0
for path, menus in sorted(schema.by_path.items()):
    recs = [m for m in menus if m.kind in ("map", "item")]
    if len(recs) < 2 or len({m.title for m in recs}) < 2:
        continue
    collisions += 1
    for rec in recs:
        got = schema.record(path, name=rec.name)
        ok(f"{path} {rec.name!r} resolves to itself", got is rec,
           f"got {got.name!r}")
ok("there are more of these than just (20,17)", collisions > 1, f"{collisions}")


# --- and the window asks the schema with it --------------------------------
# Three hops, each of which dropped the name before this: the nav payload, the
# item dialog, and the property window's own menu lookup. A stub for each end
# so a regression shows up here rather than as a wrong form on a router.


class Router:
    """Just enough MainWindow to watch a name travel from nav to schema."""

    _dispatch_menu = MainWindow._dispatch_menu
    _open_item_dialog = MainWindow._open_item_dialog

    def __init__(self):
        self.asked: list[str | None] = []
        self.menu_name: str | None = "<not called>"
        #: Which menu the GET reply was decoded against, by .jg name.
        self.decoded_as: str | None = "<not called>"
        outer = self

        class Client:
            def get(self, path, *, quiet=False, menu=None):
                outer.decoded_as = getattr(menu, "name", None)
                return None

            def menu_for(self, path, type_value=None, **kw):
                outer.asked.append(kw.get("name"))
                return schema.record(tuple(path), name=kw.get("name"))

        self.client = Client()

    def _focus_existing(self, key):
        return False

    def open_property_window(self, path, row, title, row_key="", *,
                             menu_buttons=None, menu_name=None):
        self.menu_name = menu_name
        # What the real one does with it, minus building the widget.
        self.menu = self.client.menu_for(tuple(path), name=menu_name)


def opened(payload: dict) -> Router:
    router = Router()
    router._dispatch_menu(payload)
    return router


ip_payload = _payload_from_nav(ip_settings) if ip_settings is not None else {}
router = opened(ip_payload)
check("the item dialog is told which menu", router.menu_name, "IP Settings")
check("...and the window resolves that one", router.menu.name, "IP Settings")
ok("...and asked the schema by name every time",
   router.asked and all(a == "IP Settings" for a in router.asked),
   f"{router.asked}")
ok("...so the form has IP Forward on it",
   any(f.name == "IP Forward" for f in router.menu),
   f"{[f.name for f in router.menu][:4]}")
check("...and the reply is read back as that menu, not the path's default",
      router.decoded_as, "IP Settings")

bridge = find(schema.nav, SETTINGS, "Bridge Settings")
ok("Bridge ▸ Settings is in the nav", bridge is not None)
if bridge is not None:
    router = opened(_payload_from_nav(bridge))
    check("Bridge ▸ Settings opens the bridge dialog",
          router.menu.name, "Bridge Settings")
    check("...and its reply is decoded as the bridge dialog",
          router.decoded_as, "Bridge Settings")


# --- what the device sends back, read as the dialog that asked --------------
# The handler answers a bare GET with the whole object -- 134 fields on a live
# 7.24 virtual router, every dialog's slice at once. Decoding that against the wrong menu
# was the whole of the complaint: nine labels came out as `<0x00003d>`, the
# separators the .jg declares went missing with them, and a `bigbytes` counter
# rendered as `98765432 (0x5e30a78)` rather than 94.2 MiB. The other 120 codes
# are not wire leftovers either -- they are another window's fields.

from x3.proto import message as m  # noqa: E402

bridge_menu = schema.record(SETTINGS, name="Bridge Settings")
sibling_codes = set()
for mn in schema.by_path.get(SETTINGS, []):
    if mn.name != "Bridge Settings":
        sibling_codes |= set(mn.fields)
sibling_codes -= set(bridge_menu.fields)
UNDECLARED = (0x1A, 0x29, 0x2E, 0x2F, 0x34)   # in the real reply, in no menu

reply = m.Message()
for code in sorted(sibling_codes) + list(UNDECLARED):
    reply.add_bool(code, False)
for code in sorted(bridge_menu.fields):
    reply.add_u32(code, 4096) if code >= 0x42 else reply.add_bool(code, True)

form = schema.decode_form(SETTINGS, reply, menu=bridge_menu)
names = [name for _tab, _code, name, _val, _ok in form]
ok("every field the .jg gives the dialog is named",
   [n for n in names if not n.startswith("<0x")] == [
       "Use IP Firewall", "Use IP Firewall For VLAN",
       "Use IP Firewall For PPPoE", "Allow Fast Path",
       "Bridge Fast Path Active", "Bridge Fast Path Packets",
       "Bridge Fast Path Bytes", "Bridge Fast Forward Packets",
       "Bridge Fast Forward Bytes"], f"{names}")
ok("...the other five dialogs' 120 fields are not on it",
   not (sibling_codes & {code for _t, code, _n, _v, _o in form}),
   f"{sorted(sibling_codes & {c for _t, c, _n, _v, _o in form})[:6]}")
ok("...while a code no menu here declares is still shown",
   {c for _t, c, _n, _v, _o in form if c in UNDECLARED} == set(UNDECLARED),
   f"{[c for _t, c, _n, _v, _o in form if c in UNDECLARED]}")
check("a bigbytes counter reads as bytes, not as decimal-and-hex",
      dict((n, v) for _t, _c, n, v, _o in form)["Bridge Fast Path Bytes"],
      "4096 B")
ok("...and the .jg's two separators are on the layout",
   sum(1 for e in (bridge_menu.form_layout or [])
       if e.get("kind") == "separator") == 2,
   f"{bridge_menu.form_layout}")

tracking = find(schema.nav, SETTINGS, "Connection Tracking")
ok("Connection Tracking is in the nav", tracking is not None)
if tracking is not None:
    check("...and opens its own form, not IP's or L3 Hw's",
          opened(_payload_from_nav(tracking)).menu.name, "Connection Tracking")


def main() -> int:
    if failures:
        print("\nFAILURES")
        for f in failures:
            print(" -", f)
        return 1
    print("\nall shared-path tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
