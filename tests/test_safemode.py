"""Offline tests for native Safe Mode — handler [17].

No device and no pytest required:

    .venv/bin/python test_safemode.py

The verbs and their wire shapes were captured on the wire (firmware 7.23.1,
decrypted on the wire) and were re-confirmed live on a 7.23.3 wireless router:
take → ADD → leave(discard) removed the row, take → ADD → leave(keep) did not.

The interesting behaviour is not the frames but the bookkeeping. The handler
never reports how deep its undo buffer is and never says it has hit the bottom,
so "leave without keeping" can only wind back the writes it was told about —
and every path out has to drop the session id, or the next take is a silent
no-op against an id the device has already retired.
"""

from x3.proto import message as m
from x3.proto.safemode import (
    CMD_REDO,
    CMD_RELEASE,
    CMD_TAKE,
    CMD_UNDO,
    SAFE_MODE_PATH,
    SESSION_ID,
    SafeModeChannel,
    SafeModeError,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


class Recorder:
    """Stands in for DeviceClient._request."""

    def __init__(self, session_id=7):
        self.sent: list[tuple] = []
        self.session_id = session_id
        self.fail_release = False

    def __call__(self, path, command, *, from_path=None, extra=None, quiet=False):
        msg = m.Message()
        if extra is not None:
            extra(msg)
        self.sent.append((tuple(path), command, msg.get_u32(SESSION_ID)))
        if command == CMD_RELEASE and self.fail_release:
            raise RuntimeError("device did not answer the release")
        reply = m.Message()
        if command == CMD_TAKE and self.session_id is not None:
            reply.add_u32(SESSION_ID, self.session_id)
        return reply

    @property
    def commands(self):
        return [cmd for _p, cmd, _sid in self.sent]


def chan(**kw):
    r = Recorder(**kw)
    return SafeModeChannel(r, from_path=(0, 487)), r


# --- take -----------------------------------------------------------------

c, r = chan()
st = c.take()
check("take is verb 3 on handler 17", r.sent, [(SAFE_MODE_PATH, CMD_TAKE, None)])
check("the device's session id is kept", c.session_id, 7)
ok("and this session now holds Safe Mode", st.active_here)
check("status reports the id", c.status().session_id, 7)

# Taking twice must not open a second session the device would not know about.
c.take()
check("a second take is a no-op while held", len(r.sent), 1)

# A take whose reply carries no id leaves nothing to release with, so it must
# not report a Safe Mode this session cannot actually exit.
c2, _r2 = chan(session_id=None)
try:
    c2.take()
    failures.append("take without a session id was accepted")
except SafeModeError as e:
    check("take without a session id is refused", "no session id" in str(e), True)
ok("and nothing is left holding", c2.session_id is None)

# --- release --------------------------------------------------------------

c, r = chan()
c.take()
st = c.release()
check("release is verb 5", r.commands[-1], CMD_RELEASE)
check("and echoes the id the device assigned", r.sent[-1][2], 7)
ok("the session no longer holds Safe Mode", not st.active_here)
check("the id is cleared", c.session_id, None)

c, r = chan()
check("releasing when not held sends nothing", (c.release().active_here, r.sent),
      (False, []))

# A release that never gets an answer still has to clear local state: keeping a
# stale id would make the next take() return early and silently do nothing.
c, r = chan()
c.take()
r.fail_release = True
try:
    c.release()
except RuntimeError:
    pass
check("a failed release still drops the id", c.session_id, None)
r.fail_release = False
c.take()
ok("so a later take really re-enters", c.session_id == 7 and len(r.sent) == 3)

# --- undo / redo ----------------------------------------------------------

c, r = chan()
c.take()
c.undo()
c.redo()
check("undo is verb 1, redo is verb 2",
      r.commands[-2:], [CMD_UNDO, CMD_REDO])
check("neither carries a session id", [r.sent[-2][2], r.sent[-1][2]], [None, None])

c, _r = chan()
for verb, call in (("undo", lambda: c.undo()), ("redo", lambda: c.redo())):
    try:
        call()
        failures.append(f"{verb} was allowed outside Safe Mode")
    except SafeModeError:
        print(f"ok  {verb} outside Safe Mode is refused")

# --- unroll: wind back what we were told about, then leave ---------------

c, r = chan()
c.take()
c.note_change()
c.note_change()
c.note_change()
c.unroll()
check("unroll undoes once per recorded change, then releases",
      r.commands, [CMD_TAKE, CMD_UNDO, CMD_UNDO, CMD_UNDO, CMD_RELEASE])
check("and leaves nothing held", c.session_id, None)

c, r = chan()
c.take()
c.unroll()
check("with no changes recorded, unroll is just a release",
      r.commands, [CMD_TAKE, CMD_RELEASE])

# Redo puts a change back, so it counts again on the way out.
c, r = chan()
c.take()
c.note_change()
c.note_change()
c.undo()
c.unroll()
check("undo already taken is not repeated by unroll",
      r.commands, [CMD_TAKE, CMD_UNDO, CMD_UNDO, CMD_RELEASE])

c, r = chan()
c.take()
c.note_change()
c.undo()
c.redo()
c.unroll()
check("a redone change is undone again on the way out",
      r.commands, [CMD_TAKE, CMD_UNDO, CMD_REDO, CMD_UNDO, CMD_RELEASE])

# Writes made before Safe Mode was taken are not in its buffer.
c, r = chan()
c.note_change()
c.take()
c.unroll()
check("changes from before the take are not unrolled",
      r.commands, [CMD_TAKE, CMD_RELEASE])

# --- toggle / close -------------------------------------------------------

c, r = chan()
c.toggle()
check("toggle enters when idle", r.commands, [CMD_TAKE])
c.toggle()
check("and leaves when held (keeping changes)",
      r.commands, [CMD_TAKE, CMD_RELEASE])

c, r = chan()
c.take()
c.note_change()
c.close()
check("close releases rather than unrolling — it keeps what was written",
      r.commands, [CMD_TAKE, CMD_RELEASE])
check("and clears the id", c.session_id, None)

c, r = chan()
c.close()
check("closing an unheld channel sends nothing", r.sent, [])

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all safe-mode tests passed")
