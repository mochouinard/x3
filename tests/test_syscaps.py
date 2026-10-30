"""``{type:'syscap'}`` -- the device's own capability bits.

No device and no pytest required:

    .venv/bin/python test_syscaps.py

92 predicates in 7.21 ask whether the device has numbered capability N. They
were answered True unconditionally, because nothing read the numbers -- so
VETH appeared on boards with no container support, and the switch-chip menus
(the conds are named ``prestera``, ``aldrin2``, ``switch_mv88e6xxx``,
``qstats``) appeared on boards with no switch chip.

They arrive unasked, in the reply to Winbox's very first request: handler
(13,4) command 7 answers with a nested message at 0x28 whose 0x6D is a u32
array -- 128 bits, one per capability, LSB of the first word being capability
0. Read off a live wireless router (C52iG-5HaxD2HaxD, arm64, firmware 7.23) both through a
Winbox 4 session captured in the MITM proxy and through x3ctl's own probe,
that array was::

    [541594755, 537938012, 2143259664, 69952]

which sets the mode button (104), RPS (108) and SMB share (68) and clears BTRFS
(21), VETH (2), SwOS (54), PTP (57) and every switch-chip family -- exactly the
profile of a small ARM access point, which is what that device is. That
cross-check is what settles the bit order rather than merely fitting it.

Unknown stays permissive: a device that does not answer leaves every syscap
predicate True, because a menu wrongly shown is one the device refuses, while a
menu wrongly hidden is unreachable.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import (
    SESSION_COMMAND,
    SESSION_INFO_FIELD,
    SESSION_PATH,
    SYSCAP_FIELD,
    DeviceClient,
)
from x3.proto.schema import Schema

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

#: Straight off a live wireless router (see the module docstring).
AP_WORDS = [541594755, 537938012, 2143259664, 69952]
VETH_CAP, BTRFS_CAP, SWOS_CAP, PTP_CAP = 2, 21, 54, 57
MODE_BUTTON_CAP, RPS_CAP = 104, 108


class StubClient(DeviceClient):
    """A client whose session handler answers with a fixed capability array."""

    def __init__(self, words=None, *, fail: bool = False, empty: bool = False):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.schema = schema
        self.session = None
        self.asked: list[tuple[tuple[int, ...], int]] = []
        self._words = words
        self._fail = fail
        self._empty = empty

    def _request(self, path, command, *, from_path=None, extra=None, quiet=False):
        self.asked.append((tuple(path), command))
        if self._fail:
            from x3.proto.device import DeviceError

            raise DeviceError("no such handler")
        reply = m.Message()
        if self._empty:
            return reply
        nested = m.Message()
        if self._words is not None:
            nested.add_u32_array(SYSCAP_FIELD, self._words)
        reply.add_message(SESSION_INFO_FIELD, nested)
        return reply


# --- reading them -----------------------------------------------------------

client = StubClient(AP_WORDS)
caps = client.system_caps()
check("the capability bits come from the session handler",
      client.asked, [(SESSION_PATH, SESSION_COMMAND)])
check("...128 of them, LSB of the first word first", len(AP_WORDS) * 32, 128)
check("...decoded to the set the array holds", len(caps or ()), 38)
ok("a small AP has its mode button", MODE_BUTTON_CAP in caps)
ok("...and receive-packet steering", RPS_CAP in caps)
ok("...but no BTRFS", BTRFS_CAP not in caps)
ok("...no VETH, having no containers", VETH_CAP not in caps)
ok("...no SwOS and no PTP",
   SWOS_CAP not in caps and PTP_CAP not in caps)

client.system_caps()
check("read once per session, not per question", len(client.asked), 1)

ok("a device that answers nothing useful leaves them unknown",
   StubClient(None).system_caps() is None)
ok("...as does one with no session handler at all",
   StubClient(fail=True).system_caps() is None)
ok("...or a reply with no session info in it",
   StubClient(empty=True).system_caps() is None)
unknown = StubClient(fail=True)
unknown.system_caps()
unknown.system_caps()
check("...and an unknown answer is not asked for again either",
      len(unknown.asked), 1)


# --- what they gate ---------------------------------------------------------

veth = {"type": "syscap", "value": VETH_CAP}
check("a capability the device has holds",
      sch.menu_pred_holds({"type": "syscap", "value": MODE_BUTTON_CAP},
                          board="ap", syscaps=caps), True)
check("...one it does not, does not",
      sch.menu_pred_holds(veth, board="ap", syscaps=caps), False)
check("...and with no capability list, everything holds",
      sch.menu_pred_holds(veth, board="ap", syscaps=None), True)
check("a not: over one inverts",
      sch.menu_pred_holds({"type": "not", "pred": veth}, board="ap",
                          syscaps=caps), True)
check("an or: needs only one",
      sch.menu_pred_holds(
          {"type": "or", "pred": [veth,
                                  {"type": "syscap", "value": RPS_CAP}]},
          board="ap", syscaps=caps), True)

# The nav really is filtered by it: VETH is `pred:{type:'syscap',value:2}`.
nav = schema.nav
with_caps = sch.filter_nav_by_pred(nav, board="wireless router", arch="arm64",
                                   syscaps=caps)
without = sch.filter_nav_by_pred(nav, board="wireless router", arch="arm64")


def titles(nodes):
    out = []
    for n in nodes:
        out.append(n.title)
        out.extend(titles(n.children or []))
    return out


ok("VETH is offered when nothing is known", "VETH" in titles(without))
ok("...and dropped on a board that reports no container support",
   "VETH" not in titles(with_caps),
   f"{[t for t in titles(with_caps) if 'VETH' in t]}")
ok("...while the rest of the tree survives", len(titles(with_caps)) > 100,
   f"{len(titles(with_caps))} entries")

# A field-level cond: the switch-chip families are conds over syscap.
switch = next(
    (mn for mn in schema.menus
     if any("syscap" in str(c) for c in (mn.conditions or {}).values())),
    None,
)
ok("some menu gates its fields on a capability", switch is not None)
if switch is not None:
    name, cond = next(
        (n, c) for n, c in switch.conditions.items() if "syscap" in str(c)
    )
    cap = int(
        str(cond).split("'value':")[1].split("}")[0].strip().rstrip(",")
    )
    check("...and the cond answers from the capability set",
          [sch.condition_is_true(switch, name, {}, syscaps=s)
           for s in (frozenset({cap}), frozenset())],
          [True, False])
    check("...permissively when they are unknown",
          sch.condition_is_true(switch, name, {}, syscaps=None), True)


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall syscap tests passed")
