"""Offline tests for the mutation verbs: ADD / REMOVE / MOVE / SETOBJ.

No device and no pytest required:

    .venv/bin/python test_write_path.py

Every frame here is checked against the real Winbox frames captured on
2026-08-06 against firmware 7.21.1 and captured on the wire — the
IP → Address add (frame #661), the remove (#…), the firewall-rule moves and
the comment SETOBJ (#605). A fake session records what would go on the wire,
so nothing is sent.

Also pins the two guards that keep a write from happening by accident: a
read-only client refuses every verb, and the 0xFE000C mask is derived from the
schema (5 | refreshfilter) rather than guessed.
"""

import os
import sys

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.client_ident import CLIENT_IDENT_FIELD
from x3.proto.device import (
    CMD_ADDOBJ,
    CMD_MOVEOBJ,
    CMD_REMOVEOBJ,
    CMD_SETOBJ,
    MOVE_ANCHOR,
    STD_DISABLED,
    STD_FILTER,
    STD_OBJ_ID,
    DeviceClient,
    DeviceError,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


class FakeSession:
    """Records requests and answers with a canned reply."""

    def __init__(self, reply=None):
        self.sent: list[m.Message] = []
        self._reply = reply
        self._rid = 0

    def next_request_id(self) -> int:
        self._rid += 1
        return self._rid

    def request(self, msg: m.Message) -> m.Message:
        self.sent.append(msg)
        return self._reply() if callable(self._reply) else (self._reply or m.Message())


def client(reply=None, *, readonly=False, schema_dir="7.21.1-151146055"):
    c = DeviceClient("127.0.0.1", "u", "p", readonly=readonly)
    c.session = FakeSession(reply)
    if os.path.isdir(schema_dir):
        c.schema = sch.Schema.load(schema_dir)
    return c


def fields(msg: m.Message) -> dict[int, object]:
    """Payload fields of a request, minus the framing."""
    skip = {m.SYS_TO, m.SYS_FROM, m.SYS_COMMAND, m.SYS_REQUEST_ID,
            m.SYS_REPLY_EXPECTED, CLIENT_IDENT_FIELD}
    return {code: value for code, _tag, value in msg if code not in skip}


def tags(msg: m.Message) -> dict[int, int]:
    return {code: tag for code, tag, _v in msg}


HAVE_SCHEMA = os.path.isdir("7.21.1-151146055")
if not HAVE_SCHEMA:
    print("skipped: no 7.21.1-151146055 plugin cache")
    raise SystemExit(0)

ADDRESS = (20, 1)
FIREWALL = (20, 3)

# --- the mask is derived, not guessed ------------------------------------
# Confirmed against every mutation frame in the capture: 5 on [20,1] and [72],
# 7 on [20,3] and [20,5], 0x4000F on Interface.
_s = sch.Schema.load("7.21.1-151146055")
check("an unadorned menu masks with the base alone",
      _s.record(ADDRESS).write_mask, 5)
check("Firewall Rule ORs in its refreshfilter of 2",
      _s.record(FIREWALL).write_mask, 7)
check("Interface ORs in 0x4000A", _s.record((20, 0)).write_mask, 0x4000F)

# --- REMOVE: the smallest frame in the protocol --------------------------

c = client()
c.remove_obj(ADDRESS, 5)
sent = c.session.sent[0]
check("REMOVE uses 0xFE0006", sent.get_u32(m.SYS_COMMAND), CMD_REMOVEOBJ)
check("REMOVE addresses the handler", list(sent.find(m.SYS_TO)[1]), [20, 1])
check("REMOVE carries just the mask and the id",
      fields(sent), {STD_FILTER: 5, STD_OBJ_ID: 5})

# --- MOVE: 0xFE0005 is an anchor here, not the ADD command ---------------

c = client()
c.move_obj(FIREWALL, 32, before=11)
sent = c.session.sent[0]
check("MOVE uses 0xFE0007", sent.get_u32(m.SYS_COMMAND), CMD_MOVEOBJ)
check("MOVE names the row and the row to precede",
      fields(sent), {STD_FILTER: 7, STD_OBJ_ID: 32, MOVE_ANCHOR: 11})

c = client()
c.move_obj(FIREWALL, 32)
check("MOVE to the end simply omits the anchor",
      fields(c.session.sent[0]), {STD_FILTER: 7, STD_OBJ_ID: 32})

# --- ADD: the device allocates the id, then Winbox reads the row back ----

replies = iter([
    m.Message().add_u32(STD_OBJ_ID, 25),          # the ADD ack
    m.Message().add_u32(STD_OBJ_ID, 25).add_u32(0x04, 0xFFEAA8C0),  # GETOBJ
])
c = client(lambda: next(replies))
new_id, row = c.add_obj(ADDRESS, {
    0x0A: 1,                 # interface, by .id
    0x01: "192.168.234.1",   # address
    0x02: "0.0.0.0",         # network (0 = derive)
    # 0x03 is the netmask, which this menu's .jg never declares -- Winbox sends
    # it anyway. An undeclared code has no type to encode from, so the caller
    # supplies the wire tag rather than letting the fallback guess it is text.
    0x03: (m.T_U32, sch.parse_ipv4("255.255.255.0")),
    0xFE0009: "",            # comment
})
add = c.session.sent[0]
check("ADD uses 0xFE0005", add.get_u32(m.SYS_COMMAND), CMD_ADDOBJ)
check("ADD creates the object enabled", fields(add).get(STD_DISABLED), False)
check("ADD carries the mask", fields(add).get(STD_FILTER), 5)
# The router stores ipaddr as a little-endian u32, so 192.168.234.1 is 32155840
# and 255.255.255.0 is 16777215 -- the exact numbers from capture frame #661.
check("ipaddr goes out as a little-endian u32",
      fields(add).get(0x01), 32155840)
check("the netmask encodes the same way", fields(add).get(0x03), 16777215)
check("ipaddr is tagged u32, not string", tags(add).get(0x01), m.T_U32)
check("the comment is a string", fields(add).get(0xFE0009), b"")
check("the new id comes back from the device", new_id, 25)
ok("ADD reads the object back", row is not None)
check("the read-back is a GETOBJ of the new id",
      c.session.sent[1].get_u32(STD_OBJ_ID), 25)
ok("the read-back materialises computed fields (broadcast)",
   row is not None and 0x04 in row.values_by_code)

# --- SETOBJ: only the fields that changed --------------------------------

replies = iter([m.Message(), m.Message().add_u32(STD_OBJ_ID, 9)])
c = client(lambda: next(replies))
c.set_obj(ADDRESS, 9, {0xFE0009: "New Comment"})
sent = c.session.sent[0]
check("SET uses 0xFE0003", sent.get_u32(m.SYS_COMMAND), CMD_SETOBJ)
check("SET keys on the object id", fields(sent).get(STD_OBJ_ID), 9)
check("SET sends only what changed",
      fields(sent), {STD_FILTER: 5, STD_OBJ_ID: 9, 0xFE0009: b"New Comment"})
# 0xFE000A is the standard disabled flag. Winbox happens to re-send it as
# False, but injecting it into every SET would silently re-enable a disabled
# object that the edit never mentioned.
ok("SET does not smuggle in the disabled flag", STD_DISABLED not in fields(sent))

c = client(lambda: m.Message())
try:
    c.set_obj(ADDRESS, 9, {})
    failures.append("an empty SET should not reach the wire")
except DeviceError:
    print("ok  an empty SET is refused before it is sent")

# --- a caller can pin the exact wire tag ---------------------------------

c = client(lambda: m.Message())
c.set_obj(ADDRESS, 9, {0x01: (m.T_U32, 0x0158A8C0)})
check("a (tag, value) pair passes through untouched",
      fields(c.session.sent[0]).get(0x01), 0x0158A8C0)

# --- read-only clients refuse every verb ---------------------------------

ro = client(readonly=True)
for label, call in (
    ("SETOBJ", lambda: ro.set_obj(ADDRESS, 1, {0xFE0009: "x"})),
    ("ADDOBJ", lambda: ro.add_obj(ADDRESS, {0x01: "10.0.0.1"})),
    ("REMOVEOBJ", lambda: ro.remove_obj(ADDRESS, 1)),
    ("MOVEOBJ", lambda: ro.move_obj(FIREWALL, 1)),
):
    try:
        call()
        failures.append(f"a read-only client sent {label}")
    except DeviceError as e:
        check(f"a read-only client refuses {label}", "read-only" in str(e), True)
check("and nothing reached the wire", len(ro.session.sent), 0)

# --- a failed read-back is not a failed write ----------------------------

def _boom():
    if len(c2.session.sent) > 1:
        raise OSError("handler will not GETOBJ")
    return m.Message()


c2 = client(_boom)
row = c2.set_obj(ADDRESS, 9, {0xFE0009: "x"})
check("the write stands even when the read-back fails", row, None)
check("and the write itself was still sent", len(c2.session.sent), 2)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all write-path tests passed")
