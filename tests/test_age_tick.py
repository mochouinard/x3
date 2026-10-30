"""``type:'age'`` columns tick off the local clock, the way Winbox's do.

No device and no pytest required:

    .venv/bin/python test_age_tick.py

DHCP Server ▸ Leases shows **Expires After** counting down once a second in
Winbox -- and dhcp.jg declares no ``autorefresh`` on that list at all, so the
countdown cannot be coming from a re-read. It isn't: an age field is a *stamp*
on the device's uptime clock, the stamp never moves, and what moves is the
clock it is measured against. Winbox keeps its own copy of that clock running
and redraws the column.

Ours redrew only when the list was polled, so Expires After stepped five
seconds at a time and stood still whenever Live was off. This is the local
tick: same object, later clock, nothing on the wire.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema, list_columns

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
leases = next(mn for mn in schema.menus
              if mn.group == "IP" and mn.title == "Leases" and mn.kind == "map")

EXPIRES, LAST_SEEN, AGE = 0x67, 0x77, 0x7B
ADDRESS = next(c for c, f in leases.fields.items() if f.name == "Address")

# The list Winbox never polls: no autorefresh anywhere on it.
ok("Leases asks for no autorefresh, yet its column counts down",
   not getattr(leases, "autorefresh_ms", None),
   f"{getattr(leases, 'autorefresh_ms', None)}")

UPTIME = 16_681_094.0


def lease_row(expires_in=600, age=3600, addr=0x0158A8C0):
    """One decoded lease, as table_rows would hand it over."""
    stamps = {
        EXPIRES: int(UPTIME + expires_in),
        LAST_SEEN: int(UPTIME - 30),
        AGE: int(UPTIME - age),
    }
    multi = {code: [(m.T_U32, val)] for code, val in stamps.items()}
    multi[ADDRESS] = [(m.T_U32, addr)]
    fields = []
    for code, val in [*stamps.items(), (ADDRESS, addr)]:
        fld = leases.fields[code].for_tag(m.T_U32)
        fields.append((code, fld.name, fld.render(val, uptime=UPTIME), True))
    return RowObject(
        path=leases.path, raw=None, fields=fields,
        values_by_code={c: v[0][1] for c, v in multi.items()},
        values_by_code_multi=multi,
    )


def cell(obj, name):
    return next(v for _c, n, v, _ok in obj.fields if n == name)


# --- the row re-renders itself against a later clock -------------------------

row = lease_row(expires_in=600)
check("as read, Expires After is the gap to the clock",
      cell(row, "Expires After"), "00:10:00")
check("...and Age the gap the other way", cell(row, "Age"), "01:00:00")

changed = row.retime_ages(leases, UPTIME + 1)
ok("one second on, the row says its text moved", changed)
check("...Expires After counted down", cell(row, "Expires After"), "00:09:59")
check("...Age counted up", cell(row, "Age"), "01:00:01")
check("...and Last Seen, which is the same type, with it",
      cell(row, "Last Seen"), "00:00:31")
check("a field that is not a stamp is left alone",
      cell(row, "Address"), "192.168.88.1")

ok("the same clock twice is not a change, so nothing repaints",
   not row.retime_ages(leases, UPTIME + 1))

# The sentinels still win over the clock: retiming must go through render(),
# not do the subtraction itself.
sent = lease_row()
sent.values_by_code_multi[LAST_SEEN] = [(m.T_U32, 0xFFFFFFFF)]
sent.retime_ages(leases, UPTIME + 5)
check("'never' survives a tick", cell(sent, "Last Seen"), "never")

boot = lease_row()
boot.values_by_code_multi[AGE] = [(m.T_U32, 4217407898)]
boot.retime_ages(leases, UPTIME + 5)
check("a previous-boot stamp stays blank rather than ticking",
      cell(boot, "Age"), "")

# 0x7b is declared twice -- s7b Client ID, u7b Age -- and only the u32 variant
# is a stamp. A string under the same id must not be run through the clock.
cid = lease_row()
cid.values_by_code_multi[AGE] = [(m.T_STRING, "01:aa:bb")]
cid.fields = [(AGE, "Active Client ID", "01:aa:bb", True)]
ok("a string sharing the age id is not a stamp",
   not cid.retime_ages(leases, UPTIME + 5))

# A menu with no age field has nothing to tick.
addrs = next(mn for mn in schema.menus
             if mn.group == "IP" and mn.title == "Address" and mn.kind == "map")
ok("a list with no age column never retimes",
   not lease_row().retime_ages(addrs, UPTIME + 5))


# --- and on the pane, without asking the device anything --------------------

app = QApplication.instance() or QApplication(sys.argv)


class Clock:
    """time, with monotonic pinned: a countdown is a division of the clock, so
    a test of one has to own the clock."""

    now = 0.0

    def __getattr__(self, name):
        return getattr(real_time, name)

    def monotonic(self):
        return self.now


import time as real_time  # noqa: E402

from x3.ui.widgets import list_pane as lp  # noqa: E402
from x3.ui.widgets.list_pane import ListPane  # noqa: E402

clock = Clock()
lp.time = clock


class Session:
    """An edit session that is only ever asked for its client."""

    readonly = True
    live = False

    def __init__(self, client):
        self.client = client


# A real client, never connected: render_row is pure schema work, and the one
# lookup column on Leases (Bridge Port) answers "" when there is no session.
client = DeviceClient("192.0.2.1", "x", "", readonly=True)
client.schema = schema

COLS = list_columns(leases)
NAMES = [""] + [c.name for c in COLS]
EXPIRES_CELL = 1 + [c.name for c in COLS].index("Expires After")

pane = ListPane("Leases", path=leases.path, readonly=True,
                edit_session=Session(client))

clock.now = 1000.0
row = lease_row(expires_in=600)
rendered = client.render_row(leases, row, cols=COLS)
pane._on_loaded((leases, NAMES, [list(rendered)], [row], UPTIME))
check("the pane shows what the read said",
      pane._all_rows[0][EXPIRES_CELL], "00:10:00")
ok("...and started a timer to keep it moving",
   pane._age_timer is not None and pane._age_timer.isActive())
check("...at one second, which is what a second is",
      pane._age_timer.interval(), 1000)

clock.now = 1003.0
pane._tick_ages()
check("three seconds of local clock, three seconds off the countdown",
      pane._all_rows[0][EXPIRES_CELL], "00:09:57")
check("...and the view row moved with it",
      pane._model.data(pane._model.index(0, EXPIRES_CELL)), "00:09:57")

# Nothing was read to do that.
ok("the device was never contacted", client.session is None)

# A list with no rows stops the timer rather than ticking an empty table.
pane._on_loaded((leases, NAMES, [], [], UPTIME))
pane._tick_ages()
ok("an empty list stops ticking",
   pane._age_timer is None or not pane._age_timer.isActive())

pane.shutdown()

# A list that carries no age field never starts one.
plain = ListPane("Address", path=addrs.path, readonly=True,
                 edit_session=Session(client))
pcols = list_columns(addrs)
plain._on_loaded((addrs, [""] + [c.name for c in pcols], [], [], UPTIME))
ok("a list with no age column starts no timer", plain._age_timer is None)
plain.shutdown()

lp.time = real_time


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall good")
