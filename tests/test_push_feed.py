"""The device-driven 1 Hz feed: SUBSCRIBE with an interval, and patched rows.

No device and no pytest required:

    .venv/bin/python test_push_feed.py

Winbox does not poll a live list. It subscribes once with the menu's record
mask and ``0xFF0012 = 1000``, and the device pushes those records every second
-- REQ#11 in captured-winbox-session.log. This pins our side of that:

* the SUBSCRIBE carries the flag, the mask and the interval, in Winbox's order,
  and the mask is the menu's *bare* refreshfilter (0x4000A), not the wider one
  a full read sends;
* a push of records we already hold is patched into them -- one repaint, no
  round trip, no model reset -- and a push that names a row we do not have, or
  carries no records at all, falls back to re-reading;
* a push *defers* the next poll rather than cancelling it, so a device that
  answers SUBSCRIBE with one NOTIFY and then never pushes again -- which is
  what a 10-port router on 7.21 does on the Interface handler, measured on the wire --
  costs one interval of stale counters and not four seconds.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import (
    STD_FILTER,
    SUB_INTERVAL_MS,
    SUB_PUSH_DATA,
    DeviceClient,
    RowObject,
)
from x3.ui import settings, style
from x3.ui.widgets.list_pane import (
    PUSH_GRACE,
    PUSH_REREAD_EVERY,
    ListPane,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if not cond:
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "dark", persist=False)

NAME, TX, OID = 0x10001, 0x100D4, 0xFE0001


# --- the subscribe frame -------------------------------------------------

class RecordingSession:
    """Captures the one-way frames a subscribe sends."""

    def __init__(self):
        self.sent: list[m.Message] = []
        self.demux = self

    # demux
    def subscribe(self, from_path, **kw):
        return object()

    def unsubscribe(self, sub):
        pass

    def send(self, msg):
        self.sent.append(msg)


client = DeviceClient.__new__(DeviceClient)
client.session = RecordingSession()
client._from_seq = 0
client.schema = None


def _send_oneway(path, cmd, *, from_path=None, extra=None):
    msg = m.Message()
    msg.add_u32(m.SYS_COMMAND, cmd)
    if extra is not None:
        extra(msg)
    client.session.sent.append(msg)


client._send_oneway = _send_oneway
client.alloc_from = lambda: (0, 5)

client.watch((20, 0), mask=0x4000A, interval_ms=1000)
frame = client.session.sent[-1]
codes = [code for code, _tag, _val in frame]
check("the subscribe carries flag, mask and interval",
      [c for c in codes if c in (SUB_PUSH_DATA, STD_FILTER, SUB_INTERVAL_MS)],
      [SUB_PUSH_DATA, STD_FILTER, SUB_INTERVAL_MS])
check("...the interval Winbox sends", frame.get_u32(SUB_INTERVAL_MS), 1000)
check("...and the menu's bare refreshfilter", frame.get_u32(STD_FILTER), 0x4000A)

client.session.sent.clear()
client.watch((20, 13), mask=5, interval_ms=None)
frame = client.session.sent[-1]
ok("no interval asked for, none sent", frame.get_u32(SUB_INTERVAL_MS) is None)
ok("...and no push flag either", frame.find(SUB_PUSH_DATA) is None)


# --- merging a partial record -------------------------------------------

def row(oid: int, cells: dict[int, str]) -> RowObject:
    msg = m.Message()
    msg.add_u32(OID, oid)
    obj = RowObject(path=(20, 0), raw=msg)
    obj.values_by_code[OID] = oid
    for code, value in cells.items():
        obj.fields.append((code, {NAME: "Name", TX: "Tx"}[code], value, True))
        obj.values_by_code[code] = value
    return obj


held = row(1, {NAME: "ether1", TX: "5.8 Mbps"})
held.merge(row(1, {TX: "6.1 Mbps"}))
check("a partial push updates the field it carries", held.get_rendered(TX), "6.1 Mbps")
check("...and leaves the ones it does not", held.get_rendered(NAME), "ether1")
ok("the whole message it was built from is kept", held.raw is not None)


# --- the pane's push path ------------------------------------------------

class StubClient:
    """A client whose pushes are handed over already decoded."""

    def __init__(self, objs):
        self.objs = objs
        self.rendered = 0

    def notify_objects(self, path, msg):
        return list(msg) if isinstance(msg, list) else []

    def list_column_defs(self, menu):
        return ["Name", "Tx"]

    def render_row(self, menu, obj, *, cols=None, index=None, titles=None):
        self.rendered += 1
        return ["R", obj.get_rendered(NAME), obj.get_rendered(TX)]


class StubMenu:
    """Just the schema bits the pane reads off a menu."""

    autorefresh_ms = 1000
    refresh_filter = 0x4000A
    is_collection = True
    has_age_field = False


MENU = StubMenu()


def stub_session(client):
    """A real EditSession, read-only, pointed at the stub client."""
    from x3.ui.edit_mode import EditMode, EditSession

    return EditSession(EditMode.READONLY, client=client)


COLS = ["", "Name", "Tx"]
ROWS = [["R", "ether1", "5.8 Mbps"], ["R", "ether2", "0 bps"]]
RAW = [row(1, {NAME: "ether1", TX: "5.8 Mbps"}), row(2, {NAME: "ether2", TX: "0 bps"})]

stub = StubClient(RAW)
pane = ListPane(
    "Interface", path=(20, 0), type_value=None,
    edit_session=stub_session(stub),
    live_fetcher=lambda: (MENU, COLS, ROWS, RAW, None),
)
pane._on_loaded((MENU, COLS, [list(r) for r in ROWS], RAW, None))

before_reset = pane._model.rowCount()
merged = pane._apply_push([row(1, {TX: "9.9 Mbps"})])
ok("a push of a row we hold is patched, not re-read", merged)
check("the cell moved", pane._model.row_values(0)[2], "9.9 Mbps")
check("the row it did not name is untouched", pane._model.row_values(1)[2], "0 bps")
check("no rows were added or dropped", pane._model.rowCount(), before_reset)
check("the pane's own copy moved too", pane._all_rows[0][2], "9.9 Mbps")

ok("a push naming a row we have never seen is not patched",
   not pane._apply_push([row(99, {TX: "1 bps"})]))
ok("a push with no records at all is not patched", not pane._apply_push([]))

# What the pane would subscribe with, straight off the menu.
check("the pane subscribes with the menu's mask and rate",
      pane._watch_kwargs(), {"mask": 0x4000A, "interval_ms": 1000})
settings.save_list_refresh_secs(0.0)
check("...and asks for no interval when the poll is switched off",
      pane._watch_kwargs()["interval_ms"], None)
settings.save_list_refresh_secs(None)

# The feed defers the poll while it runs, and only while it runs.
#
# Standing the poll down on the first push is what cost the Interface window
# four seconds of still counters: the device answered SUBSCRIBE with a single
# NOTIFY 30ms later and pushed nothing after it, so the pane sat on a stopped
# poll until a three-interval watchdog fired and a one-interval poll followed.
# A deferred poll is never worse than one interval late, whatever the device
# does with the interval it was given.
pane._push_interval_ms = 1000
pane.set_auto_refresh(1.0)
ok("the poll is running before any push", pane._poll_timer.isActive())
check("...on the cadence it was given", pane._poll_timer.interval(), 1000)
pane._note_push()
ok("a push does not stop the poll", pane._poll_timer.isActive())
check("...it pushes it back by the grace factor",
      pane._poll_timer.interval(), int(1000 * PUSH_GRACE))
ok("...so a feed that dies is one interval stale, not four seconds",
   pane._poll_timer.interval() <= 2000,
   f"{pane._poll_timer.interval()}ms")
pane._poll_tick()
check("a poll that has had to fire takes the cadence back",
      pane._poll_timer.interval(), 1000)

# Whatever the feed does, a pane with polling switched off stays switched off.
pane.set_auto_refresh(0.0)
pane._note_push()
ok("a push does not start polling a list that asked for none",
   not pane._poll_timer.isActive())

check("a merged run still re-reads eventually", PUSH_REREAD_EVERY, 20)
pane.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all push-feed tests passed")
