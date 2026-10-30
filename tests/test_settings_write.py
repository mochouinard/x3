"""Writing a settings menu — one object, no `.id`.

No device and no pytest required:

    .venv/bin/python test_settings_write.py

System ▸ Identity and IP ▸ Cloud are single-object menus: `kind: item`,
`is_collection: False`, and no 0xFE0001 anywhere — not even in the GET reply.
The property form decided its verb from "do we have an object id?", so these
fell through to `add`:

    NOTIMP (0xfe0003 / 16646147)    [to=(24,1) cmd=0xfe0005]   ADDOBJ

and a plain SETOBJ has no id to key on either:

    UNKNOWNID (0xfe0004 / 16646148) [to=(24,1) cmd=0xfe0003]   SETOBJ

What works on a menu of this shape is 0xFE000E — fields only, no id, no
request id, no reply — which is what Winbox was captured sending to change
/system package update's Channel, itself a non-collection `item`.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession, QueuedChange
from x3.ui.widgets.property_window import PropertyWindow

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


schema = Schema.load("7.21.1-151146055")
identity = next(m for m in schema.menus if m.path == (24, 1) and m.kind == "item")
rules = next(m for m in schema.menus if m.path == (20, 3) and m.kind == "map")

check("Identity is a single object", identity.is_collection, False)
check("Filter Rules is a collection", rules.is_collection, True)

# --- which verb the form picks --------------------------------------------

pick = PropertyWindow._write_action


class Form:
    def __init__(self, menu):
        self.menu = menu


check("a settings menu with no id gets its own verb, not an ADD",
      pick(Form(identity), None), "set_settings")
check("a collection with no id is a New form, so ADD",
      pick(Form(rules), None), "add")
check("an existing row is always a SET", pick(Form(rules), 4), "set")
check("...settings menus too", pick(Form(identity), 4), "set")

# --- and what reaches the device -----------------------------------------


class StubClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def set_settings(self, path, values, **kw):
        self.calls.append(("set_settings", path, values))

    def set_obj(self, path, obj_id, values, **kw):
        self.calls.append(("set_obj", path, obj_id, values))

    def add_obj(self, path, values, **kw):
        self.calls.append(("add_obj", path, values))
        return 7, None


IDENT = 0x0C

client = StubClient()
session = EditSession(EditMode.LIVE, client=client)

session.apply(QueuedChange(
    path=(24, 1), title="Identity", row_key="", obj_id=None,
    edits=[{"code": IDENT, "name": "Identity", "new": "gateway"}],
    action="set_settings",
))
check("a settings write goes out without an id",
      client.calls[-1], ("set_settings", (24, 1), {IDENT: "gateway"}))

session.apply(QueuedChange(
    path=(20, 3), title="Filter Rules", row_key="4", obj_id=4,
    edits=[{"code": 0x27, "name": "Chain", "new": "input"}],
    action="set",
))
check("a row write still carries its id",
      client.calls[-1], ("set_obj", (20, 3), 4, {0x27: "input"}))

check("nothing was added", [c[0] for c in client.calls].count("add_obj"), 0)


# --- a two-option enum can be a bool on the wire --------------------------
#
# IP ▸ Cloud's "DDNS Enabled" is `b`-prefixed with values {0: auto, 1: yes}.
# Encoded as a u32 the device accepted the frame and silently ignored it — the
# read-back still said auto. "Back To Home VPN" on the same menu is
# `u`-prefixed and really is a number, so the id letter is the only thing that
# tells the two apart.

from x3.proto import message as m
from x3.proto.schema import encode_form_value

cloud = next(x for x in schema.menus if x.path == (123, 1) and x.kind == "item")
ddns = cloud.fields[0x64]
bth = cloud.fields[0x7E]

check("DDNS Enabled is a bool-prefixed enum", (ddns.jg_type, ddns.prefix), ("enm", "b"))
check("...so 'yes' goes out as a bool", encode_form_value(ddns, 1), (m.T_BOOL_TRUE, True))
check("...and 'auto' as the false bool", encode_form_value(ddns, 0), (m.T_BOOL_FALSE, False))

check("Back To Home VPN is a number-prefixed enum", bth.prefix, "u")
check("...so it still goes out as a u32", encode_form_value(bth, 2), (m.T_U32, 2))

check("Identity is a plain string",
      encode_form_value(identity.fields[0x0C], "gateway"), (m.T_STRING, "gateway"))

# --- a settings form sends its whole form ---------------------------------
#
# 0xFE000E means "apply this form", not "change this field", so a partial send
# risks the device defaulting whatever was left out. Captured from Winbox on
# [123,1] IP ▸ Cloud: every field goes on Apply. A row SET is the opposite —
# it is keyed on an id and carries only what changed.

verb = PropertyWindow._write_action

check("a settings menu is not an Add", verb(Form(identity), None), "set_settings")
check("...but sends the whole form, like an Add does",
      verb(Form(identity), None) in ("add", "set_settings"), True)
check("a New form on a collection is an Add", verb(Form(rules), None), "add")
check("an existing row is a keyed SET, so only its changes go",
      verb(Form(rules), 4) in ("add", "set_settings"), False)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all settings-write tests passed")
