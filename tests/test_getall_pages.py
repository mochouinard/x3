"""GETALL follows the device's pages.

RouterOS answers a long list a page at a time: ~20-30 objects, the total in
0xfe0019, and in 0xfe0003 the id the next page starts at. Echoing that id on
another GETALL gets the next page; the last page has no 0xfe0003. Reading only
the first reply left a 72-rule NAT list at 22 rows (seen live on 7.19.4:
22 + 21 + 29).
"""

from __future__ import annotations

import sys

from x3.proto import message as m
from x3.proto.device import CMD_GETALL, QUERY_CURSOR, STD_OBJ_COUNT, STD_OBJS, DeviceClient

FAILS = 0


def check(what, got, want):
    global FAILS
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {what}: {got!r}" + ("" if ok else f" (want {want!r})"))
    FAILS += not ok


def obj(i: int) -> m.Message:
    return m.Message().add_u32(0xFE0001, i)


def page_reply(ids: list[int]) -> m.Message:
    r = m.Message()
    r._entries.append((STD_OBJS, m.T_MESSAGE | 0x80, [obj(i) for i in ids]))  # message array
    return r


def device(pages: list[tuple[list[int], int | None]], total: int):
    """A client whose list answers ``pages``: (ids, next cursor) keyed by the cursor sent."""
    c = DeviceClient("192.0.2.1", "admin", "")
    sent: list[int | None] = []
    by_cursor = {}
    prev = None
    for ids, nxt in pages:
        by_cursor[prev] = (ids, nxt)
        prev = nxt

    def fake_request(path, command, *, from_path=None, extra=None, quiet=False):
        assert command == CMD_GETALL
        req = m.Message()
        if extra:
            extra(req)
        cur = req.get_u32(QUERY_CURSOR)
        sent.append(cur)
        ids, nxt = by_cursor[cur]
        r = page_reply(ids).add_u32(STD_OBJ_COUNT, total)
        if nxt is not None:
            r.add_u32(QUERY_CURSOR, nxt)
        return r

    c._request = fake_request
    c._row_from_msg = lambda path, it: it.get_u32(0xFE0001)
    return c, sent


pages = [(list(range(0, 22)), 68), (list(range(68, 89)), 14), (list(range(14, 43)), None)]
c, sent = device(pages, 72)
rows = c.getall((20, 5))
check("all 72 rows", len(rows), 72)
check("in the device's order", rows[:2] + rows[22:24] + rows[-1:], [0, 1, 68, 69, 42])
check("cursors echoed", sent, [None, 68, 14])
check("not partial", c.last_getall_partial, False)

c, sent = device([(list(range(5)), None)], 5)
check("one-page list: one request", (len(c.getall((20, 1))), sent), (5, [None]))

c, sent = device(pages, 72)
rows = c.getall((20, 5), limit=30)
check("limit stops paging", (len(rows), sent), (43, [None, 68]))
check("limit marks partial", c.last_getall_partial, True)

# A device that repeats a cursor must not loop forever.
c, sent = device([(list(range(3)), 7), (list(range(3, 6)), 7)], 99)
c2_pages = {None: ([0, 1, 2], 7), 7: ([3, 4, 5], 7)}
def looping(path, command, *, from_path=None, extra=None, quiet=False):
    req = m.Message()
    if extra:
        extra(req)
    ids, nxt = c2_pages[req.get_u32(QUERY_CURSOR)]
    return page_reply(ids).add_u32(QUERY_CURSOR, nxt)
c._request = looping
rows = c.getall((20, 5))
check("repeated cursor stops", (len(rows), c.last_getall_partial), (6, True))

sys.exit(1 if FAILS else 0)
