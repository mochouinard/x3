"""Safe Mode discard follows the router's own count (History rows flagged F).

Undo is router-wide and newest-first, and Safe Mode also holds changes made at
the console or by another login. Counting only our own writes undid the wrong
change (lab CHR 7.23.3: one CRUD add, then one console add; a count of 1 undid
the console add and kept the CRUD one).
"""

from __future__ import annotations

import sys

from x3.proto import message as m
from x3.proto.safemode import CMD_RELEASE, CMD_TAKE, CMD_UNDO, SESSION_ID, SafeModeChannel

FAILS = 0


def check(what, got, want):
    global FAILS
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got!r}" + ("" if ok else f" (want {want!r})"))
    FAILS += not ok


def channel(router_count):
    """router_count: list holding the number of F rows the router reports."""
    sent = []

    def request(path, command, *, from_path=None, extra=None):
        sent.append(command)
        if command == CMD_UNDO:
            router_count[0] -= 1
        r = m.Message()
        if command == CMD_TAKE:
            r.add_u32(SESSION_ID, 7)
        return r

    ch = SafeModeChannel(request, floating=lambda: router_count[0])
    return ch, sent


# One write of ours, one console change we were never told about.
n = [0]
ch, sent = channel(n)
ch.take()
ch.note_change()
n[0] = 2
check("pending counts the console change too", ch.pending, 2)
ch.unroll()
check("both undone", n[0], 0)
check("two undos then release", sent[1:], [CMD_UNDO, CMD_UNDO, CMD_RELEASE])

# A change lands while we unroll: the recount catches it.
n = [0]
ch, sent = channel(n)
ch.take()
n[0] = 1
orig = ch.undo
def undo_and_race():
    orig()
    if sent.count(CMD_UNDO) == 1:
        n[0] += 1
ch.undo = undo_and_race
ch.unroll()
check("recounts after the first pass", (n[0], sent.count(CMD_UNDO)), (0, 2))

# The router can't be read: pending is unknown, and unroll refuses, still held.
from x3.proto.safemode import MAX_UNROLL_STEPS, SafeModeError

ch, sent = channel([0])
ch._floating = lambda: None
ch.take()
ch.note_change(3)
check("unreadable pending is None, not our count", ch.pending, None)
try:
    ch.unroll()
    check("unroll refuses", "returned", "raised")
except SafeModeError:
    check("unroll refuses", "raised", "raised")
check("no undo, no release", sent[1:], [])
check("still held", ch.session_id, 7)

# The recount fails part way: stop, keep Safe Mode held.
n = [2]
ch, sent = channel(n)
ch.take()
reads = [0]
def flaky():
    reads[0] += 1
    if reads[0] > 1:
        raise OSError("link down")
    return n[0]
ch._floating = flaky
try:
    ch.unroll()
    check("mid-way failure raises", "returned", "raised")
except SafeModeError:
    check("mid-way failure raises", "raised", "raised")
check("undid what it read, no release", sent[1:], [CMD_UNDO, CMD_UNDO])
check("still held after failure", ch.session_id, 7)

# The step cap holds inside one pass.
n = [MAX_UNROLL_STEPS + 50]
ch, sent = channel(n)
ch.take()
try:
    ch.unroll()
    check("cap raises", "returned", "raised")
except SafeModeError:
    check("cap raises", "raised", "raised")
check("never more undos than the cap", sent.count(CMD_UNDO), MAX_UNROLL_STEPS)
check("no release at the cap", CMD_RELEASE in sent, False)

# No reader at all (a bare channel): our own count is all there is.
ch, sent = channel([0])
ch._floating = None
ch.take()
ch.note_change(3)
check("readerless pending", ch.pending, 3)
ch.unroll()
check("readerless unroll undoes our count", sent.count(CMD_UNDO), 3)

sys.exit(1 if FAILS else 0)
