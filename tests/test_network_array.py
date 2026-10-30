"""The "Available From" array editor: one box per network, + to add.

No device and no pytest required:

    .venv/bin/python test_network_array.py

IP ▸ Services restricts a service to a list of networks. On the wire that is an
array of nested messages, each an (address, mask) pair; in Winbox it is one
text box per entry with a + to add another, and nothing but the + while empty —
an empty list means "no restriction", so a lone blank box would misstate it.

This field decides who can reach a service, so an entry that will not parse
blocks the save. Dropping it silently would send a shorter list than the one on
screen, which for this field means opening a service up rather than locking it
down.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.schema import Schema
from x3.ui.widgets.network_array import (
    NetworkArrayEditor,
    format_cidr,
    parse_cidr,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


app = QApplication.instance() or QApplication(sys.argv)

schema = Schema.load("7.21.1-151146055")
services = next(x for x in schema.menus if x.path == (68, 1) and x.kind == "map")
avail = services.fields[0x6]


def entry(addr: int, mask: int):
    msg = m.Message()
    msg.add_u32(1, addr)
    msg.add_u32(2, mask)
    return msg


WIRE = [entry(0x0000A8C0, 65535), entry(0x000A0A0A, 16777215)]

# --- parsing round-trips --------------------------------------------------

check("a network parses to (address, mask)",
      parse_cidr("192.168.0.0/16"), (0x0000A8C0, 65535))
check("a bare address is a single host",
      parse_cidr("192.168.0.1"), (0x0100A8C0, 0xFFFFFFFF))
check("...and formats back bare", format_cidr(0x0100A8C0, 0xFFFFFFFF), "192.168.0.1")
check("a network formats back as CIDR", format_cidr(0x0000A8C0, 65535), "192.168.0.0/16")
check("nonsense does not parse", parse_cidr("not-an-ip"), None)
check("an empty entry does not parse", parse_cidr("  "), None)
check("a host bit set is normalised, not rejected",
      parse_cidr("192.168.5.7/16"), (0x0000A8C0, 65535))

# --- the editor -----------------------------------------------------------

ed = NetworkArrayEditor(avail, WIRE)
check("it loads one box per entry", ed.texts(), ["192.168.0.0/16", "10.10.10.0/24"])
check("untouched is not an edit", ed.is_dirty(), False)
check("nothing is wrong with it", ed.error(), None)

ed._add_row("172.16.0.0/12")
ok("adding a row marks it edited", ed.is_dirty())
check("...and it is included", ed.texts()[-1], "172.16.0.0/12")

tag, entries = ed.wire_value()
check("it writes a message array", tag, m.T_MESSAGE_ARRAY)
check("...with one message per entry", len(entries), 3)
first = {c: v for c, _t, v in entries[0]}
check("...carrying the address", first[1], 0x0000A8C0)
check("...and its mask", first[2], 65535)

# --- a bad entry must block the save, not vanish -------------------------

ed._add_row("192.168.1.0/xx")
ok("a malformed entry is reported", ed.error() is not None)
ok("...naming the field", "Available From" in (ed.error() or ""))

# --- removing, and the empty case ----------------------------------------

empty = NetworkArrayEditor(avail, [])
check("an empty list starts with no boxes at all", empty.texts(), [])
check("...and nothing to complain about", empty.error(), None)
tag, entries = empty.wire_value()
check("...writing an empty array, which means no restriction", entries, [])

one = NetworkArrayEditor(avail, WIRE)
one._remove_row(one._rows[0][0])
check("removing a row drops it", one.texts(), ["10.10.10.0/24"])
ok("...and counts as an edit", one.is_dirty())
one.reset_baseline()
check("...until it is saved", one.is_dirty(), False)

# --- a mode change has to reach inside the editor -------------------------
#
# The rows' widgets are disabled individually, so enabling the container does
# not undo that. Switching Read-only → Live left the editor dead until the
# window was closed and reopened.

from x3.ui.widgets.property_window import _set_locked

locked = NetworkArrayEditor(avail, WIRE, readonly=True)
check("it starts locked", locked._rows[0][1].isReadOnly(), True)
check("...including the + button", locked._add.isEnabled(), False)

_set_locked(locked, False)
check("going Live unlocks the boxes", locked._rows[0][1].isReadOnly(), False)
check("...and the + button", locked._add.isEnabled(), True)
check("...and the row's remove button",
      locked._rows[0][0].findChildren(type(locked._add))[0].isEnabled(), True)

_set_locked(locked, True)
check("going back to read-only re-locks", locked._rows[0][1].isReadOnly(), True)

# A row added while unlocked must not come back locked.
_set_locked(locked, False)
locked._add_row("172.16.0.0/12")
check("a row added after unlocking is editable",
      locked._rows[-1][1].isReadOnly(), False)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all network-array tests passed")
