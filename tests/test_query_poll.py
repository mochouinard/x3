"""Polling a run: the command the .jg names, not always GETALL.

No device and no pytest required:

    .venv/bin/python test_query_poll.py

Winbox's run-and-watch dialogs come in two shapes. Ping, Traceroute, Torch and
IP Scan declare a startcmd and no pollcmd, and are read with GETALL::

    {name:'Ping',title:'Ping',type:'query',path:[22],startcmd:16646159,
     cancelcmd:16646161,…}

The other 31 name their own poll command, and their handler does not implement
GETALL at all::

    {name:'Make Supout.rif',c:[{title:'Make it!',type:'action',path:[24,25],
     startcmd:16646159,pollcmd:16646160,cancelcmd:16646161,
     request:[{name:'File Name',type:'string',id:'s1',def:'supout.rif'}],
     c:[{name:'Created',type:'number',id:'u1',postfix:'%'}]}]}

Polling those with GETALL is what made *Make Supout.rif* read as unsupported:
verified on a 7.24 virtual router, GETALL on (24,25) answers NOTIMP (0xfe0003) while the
declared 0xfe0010 returns the percentage and flips 0xfe000b at 100.

The second half of the shape: a progress run has no rows to nest. Its reply
carries the result fields on the frame itself, where the 0xfe0002 array would
have been.
"""

import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import (
    CMD_GETALL,
    QUERY_DONE,
    STD_FILTER,
    STD_OBJ_ID,
    DeviceClient,
)
from x3.proto.schema import Schema
from x3.ui.widgets.query_window import QueryWindow

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
SUPOUT = (24, 25)
supout = schema.command_menu(SUPOUT, "action")
ping = schema.command_menu((22,), "query")


# --- the schema side --------------------------------------------------------

ok("Make Supout.rif is reachable as a command menu", supout is not None)
check("...under the title the .jg gives its button", supout.title, "Make it!")
check("...naming its own poll command", supout.poll_command, 0xFE0010)
ok("...separately from the one that starts it",
   supout.start_command == 0xFE000F and supout.poll_command != CMD_GETALL)
ok("Ping names none, so GETALL is right for it", ping.poll_command is None)


# --- what goes on the wire --------------------------------------------------

class StubSession:
    """Enough ModernSession to build a request against and answer it."""

    timeout = 1.0

    def __init__(self, reply):
        self.reply = reply
        self.sent: list = []
        self._rid = 0

    def next_request_id(self):
        self._rid += 1
        return self._rid

    def request(self, msg):
        self.sent.append(msg)
        return self.reply


def client_for(reply):
    c = DeviceClient.__new__(DeviceClient)     # no socket, no auth
    c.session = StubSession(reply)
    c.schema = schema
    c.session_log = None
    c.client_ident = "winbox-3.42"
    c.readonly = False
    c._lock = threading.RLock()
    c._uptime = None
    c._enum_cache = {}
    return c


def progress(percent: int | None, *, done: bool | None = None) -> m.Message:
    """A poll reply shaped the way (24,25) answers: fields, no row array."""
    msg = m.Message()
    msg.add_u32_array(m.SYS_TO, [0, 12])
    msg.add_u32_array(m.SYS_FROM, list(SUPOUT))
    if done is not None:
        msg.add_bool(QUERY_DONE, done)
    if percent is not None:
        msg.add_u32(0x01, percent)
    return msg


c = client_for(progress(43, done=False))
rows = c.poll_query(supout, 1)
sent = c.session.sent[-1]
check("the poll uses the command the .jg names",
      sent.get_u32(m.SYS_COMMAND), 0xFE0010)
check("...addressed to the handler", tuple(sent.find(m.SYS_TO)[1]), SUPOUT)
check("...carrying the run's session id", sent.get_u32(STD_OBJ_ID), 1)
ok("...and no GETALL field mask, which this command has no use for",
   sent.get_u32(STD_FILTER) is None)

c = client_for(progress(43, done=False))
c.poll_query(ping, 1)
sent = c.session.sent[-1]
check("a menu with no pollcmd still polls with GETALL",
      sent.get_u32(m.SYS_COMMAND), CMD_GETALL)
check("...with the field mask GETALL takes", sent.get_u32(STD_FILTER), 5)


# --- a progress reply reads as one row --------------------------------------

check("a reply with no row array is still one row", len(rows), 1)
check("...carrying the field the .jg declares",
      rows[0].values_by_code.get(0x01), 43)
check("...named and rendered off the command menu, which is the only "
      "declaration this path has",
      rows[0].get_rendered(0x01, prefer_name="Created"), "43%")
check("...and the finished flag as sent", rows[0].values_by_code.get(QUERY_DONE),
      False)

c = client_for(progress(100, done=True))
rows = c.poll_query(supout, 1)
check("the last poll says the run is over",
      rows[0].values_by_code.get(QUERY_DONE), True)

c = client_for(progress(None))
check("a reply with neither rows nor fields yields none",
      c.poll_query(supout, 1), [])

c = client_for(progress(None, done=True))
check("...but a bare end marker is still the last row",
      len(c.poll_query(supout, 1)), 1)
c = client_for(progress(None, done=True))
ok("...only for a run polled with its own command, so an empty Ping poll "
   "stays empty", c.poll_query(ping, 1) == [])


# --- the .jg's "nothing to cancel" sentinel ---------------------------------

renew = next(mn for mn in schema.menus
             if mn.title == "Renew License" and mn.poll_command)
check("Renew License declares no real cancel command",
      renew.cancel_command, 0xFFFFFFFF)
c = client_for(progress(None))
c.cancel_query(renew, 1)
check("...so nothing is sent when the window stops it", c.session.sent, [])


# --- the window stops when the device says it is done -----------------------

app = QApplication.instance() or QApplication(sys.argv)


class StubClient:
    """A client whose polls hand back a scripted progress sequence."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.polls = 0
        self.cancelled: list = []
        self._decoder = client_for(m.Message())

    def alloc_from(self):
        return (0, 12)

    def start_query(self, menu, values, from_path=None):
        return 3

    def poll_query(self, menu, sid, from_path=None):
        self.polls += 1
        reply = self.replies[min(self.polls, len(self.replies)) - 1]
        return self._decoder._rows_from_reply(reply, menu)

    def cancel_query(self, menu, sid, from_path=None):
        self.cancelled.append(sid)

    def enum_map(self, path):
        return {}


def pump(win) -> None:
    """Let a queued start() run, then the poll thread, then its signal."""
    app.processEvents()
    worker = win._worker
    if worker is not None:
        worker.wait(2000)
    app.processEvents()


client = StubClient([progress(43, done=False)])
win = QueryWindow("Make Supout.rif", supout, client=client, autostart=True)
pump(win)
check("a run in progress keeps the window polling", client.polls, 1)
ok("...with Stop offered while it runs", win._stop_btn.isEnabled())
check("...and the percentage on screen, in the unit the .jg gives it",
      win._model.data(win._model.index(0, 0)), "43%")

client.replies.append(progress(100, done=True))
win._tick()
pump(win)
check("the finished flag stops the polling", win._timer.isActive(), False)
check("...says so rather than counting rows", win._status.text(), "done")
ok("...and offers Start again", win._start_btn.isEnabled()
   and not win._stop_btn.isEnabled())
check("...without cancelling a run the device already finished",
      client.cancelled, [])
win.close()
app.processEvents()
check("closing a finished window cancels nothing either", client.cancelled, [])

# --- Ping: the cursor the device hands back goes back on the next poll -------
# Live on firmware 7.23.3: a poll without it got one reply, then the session
# was gone and the next poll answered UNKNOWNID (0xfe0004). Real Winbox sends
# 0xfe0003=N on poll N+1 (a session capture).

from x3.proto.device import QUERY_CURSOR


def ping_reply(cursor, *, done=False, rows=1) -> m.Message:
    msg = m.Message()
    msg.add_u32_array(m.SYS_TO, [0, 200])
    msg.add_u32_array(m.SYS_FROM, [22])
    nested = []
    for _ in range(rows):
        row = m.Message()
        row.add_u32(0x0F, 0)
        nested.append(row)
    msg._entries.append((0xFE0002, m.T_MESSAGE_ARRAY, nested))
    if cursor is not None:
        msg.add_u32(QUERY_CURSOR, cursor)
    if done:
        msg.add_bool(QUERY_DONE, True)
    return msg


c = client_for(ping_reply(1))
first = c.poll_query(ping, 7)
ok("the first poll sends no cursor",
   c.session.sent[-1].get_u32(QUERY_CURSOR) is None)
check("...and reads the cursor the device answers with", first.cursor, 1)
ok("...marking the rows as new-since-last-poll", first.incremental)
c.poll_query(ping, 7, cursor=first.cursor)
check("the next poll echoes it", c.session.sent[-1].get_u32(QUERY_CURSOR), 1)
c = client_for(progress(43, done=False))
c.poll_query(supout, 1, cursor=4)
ok("a run polled with its own command never carries one",
   c.session.sent[-1].get_u32(QUERY_CURSOR) is None)
last = client_for(ping_reply(None, done=True)).poll_query(ping, 7, cursor=3)
ok("the poll that ends a run says so on the frame", last.done)
ok("...and an ordinary poll does not",
   not client_for(ping_reply(2)).poll_query(ping, 7).done)


# --- the window: tabs, Enter, accumulating rows, no polling past the end -----

class PingClient(StubClient):
    """Replays Ping's wire behaviour: new rows only, cursor out, done last."""

    def __init__(self):
        super().__init__([])
        self.cursors: list = []
        self.started: list = []

    def start_query(self, menu, values, from_path=None):
        self.started.append(values)
        return 3

    def poll_query(self, menu, sid, from_path=None, cursor=None):
        self.cursors.append(cursor)
        n = (cursor or 0) + 1
        if sid is None or n > 3:
            raise AssertionError("polled past the end of the run")
        out = QueryPoll(self._decoder._rows_from_reply(ping_reply(n), menu))
        out.cursor, out.incremental, out.done = (None if n == 3 else n), True, n == 3
        return out


from x3.proto.device import QueryPoll
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QTabWidget

pc = PingClient()
win = QueryWindow("Ping", ping, client=pc)
tabs = win.findChild(QTabWidget)
ok("Ping's request form has tabs", tabs is not None)
check("...General then Advanced, as the .jg declares them",
      [tabs.tabText(i) for i in range(tabs.count())], ["General", "Advanced"])
ok("...with Ping To on General and TTL on Advanced",
   tabs.widget(0).isAncestorOf(win._editors[22])
   and tabs.widget(1).isAncestorOf(win._editors[5]))
win._editors[22].setText("10.0.2.2")
QTest.keyClick(win._editors[22], Qt.Key_Return)
pump(win)
check("Enter in Ping To starts the run", len(pc.started), 1)
check("...with the address typed", pc.started[0].get(22), "10.0.2.2")
for _ in range(2):
    win._tick()
    pump(win)
check("each poll echoes the cursor of the one before", pc.cursors, [None, 1, 2])
check("rows accumulate instead of replacing one another",
      win._model.rowCount(), 3)
check("the poll carrying the done flag ends the run",
      win._timer.isActive(), False)
ok("...and Start is offered again", win._start_btn.isEnabled())
win._tick()
check("...and nothing polls the finished session", pc.cursors, [None, 1, 2])
win.close()

# Traceroute declares Count / Max Hops defaults of 0xffffffff: a QSpinBox is a
# C int, and constructing one from that raised OverflowError -- the window
# never opened.
trace = schema.command_menu((26,), "query", "Traceroute")
if trace is not None:
    win = QueryWindow("Traceroute", trace, client=StubClient([]))
    ok("Traceroute opens despite 32-bit field limits", win is not None)
    ok("...with its fields built", len(win._editors) >= 8)
    win.close()

# --- Ping's table is Winbox's: its columns, its units, its host ------------
# The .jg's width-bearing columns are Seq #, Host, Time, Reply Size, TTL,
# Status. Taking every named field showed Min/Avg instead (the status bar's)
# and no Host (a tuple). Time is an enm around {fixedpoint, scale:1000}: a
# 12.044ms reply rendered "12044ms".

win = QueryWindow("Ping", ping, client=StubClient([]))
heads = [win._model.headerData(i, Qt.Orientation.Horizontal)
         for i in range(win._model.columnCount())]
check("Ping shows Winbox's columns, the unit in the header",
      heads, ["Seq #", "Host", "Time (ms)", "Reply Size", "TTL", "Status"])
reply = m.Message()
reply.add_u32(0x0F, 3)
reply.add_u32(0x01, 0x08080404)      # network order, as Ping answers: 8.8.4.4
reply.add_u32(0x07, 12044)
reply.add_u32(0x03, 56)
reply.add_u32(0x05, 117)
row = win.client._decoder._row_from_msg(ping.path, reply, menu=ping) \
    if hasattr(win.client, "_decoder") else None
if row is not None:
    check("...and a reply reads as Winbox reads it",
          [win._cell(row, c) for c in win._columns],
          ["3", "8.8.4.4", "12.044", "56", "117", ""])
win.close()

# --- Start and Stop: one at a time, and visibly so ---------------------------
# The window always swapped them, but every button rule in the sheet names a
# colour, which beats the palette's disabled role: a greyed-out Stop looked
# exactly like a live one, in every window of the app.

from PySide6.QtWidgets import QApplication as _QApp, QPushButton  # noqa: E402
from x3.ui import style as _style  # noqa: E402

_style.apply_theme(_QApp.instance(), "light", scale=1.0, persist=False)
pc2 = PingClient()
win = QueryWindow("Ping", ping, client=pc2)
check("before a run: Start on, Stop off",
      (win._start_btn.isEnabled(), win._stop_btn.isEnabled()), (True, False))
win._editors[22].setText("10.0.2.2")
win.start()
check("while it runs: Start off, Stop on",
      (win._start_btn.isEnabled(), win._stop_btn.isEnabled()), (False, True))
win.stop()
check("stopped: back again",
      (win._start_btn.isEnabled(), win._stop_btn.isEnabled()), (True, False))
win.close()


def darkest(btn: QPushButton) -> int:
    btn.resize(90, 26)
    img = btn.grab().toImage()
    return min(img.pixelColor(x, y).lightness()
               for x in range(10, 80) for y in range(5, 21))


live, dead = QPushButton("Stop"), QPushButton("Stop")
dead.setEnabled(False)
ok("a disabled button is drawn dimmer than a live one",
   darkest(dead) > darkest(live) + 40, f"{darkest(live)} vs {darkest(dead)}")

# --- New Window: on the measuring tools, nowhere else -------------------------

from x3.proto.schema import offers_new_window  # noqa: E402

opened: list = []
win = QueryWindow("Ping", ping, client=StubClient([]), on_new_window=lambda: opened.append(1))
ok("Ping offers New Window", win._new_btn is not None)
win._new_btn.click()
check("...which asks for another window of the same tool", opened, [1])
win.close()
by_title = {x.title: x for x in schema.menus if x.kind in ("query", "action", "doit")}
for title in ("Traceroute", "Torch", "Bandwidth Test"):
    if title in by_title:
        ok(f"{title} offers it too", offers_new_window(by_title[title]))
for title in ("Format Drive", "Download Backup", "Sign"):
    if title in by_title:
        ok(f"{title} does not", not offers_new_window(by_title[title]))

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all query-poll tests passed")
