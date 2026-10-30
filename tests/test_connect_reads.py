"""What a connect costs in round trips.

No device and no pytest required:

    .venv/bin/python test_connect_reads.py

On a LAN an extra round trip is a millisecond and nobody notices. Over a WAN
link it is the whole latency again, and connecting used to pay it eight times
over: the plugin index, then resources, then identity, then the same two again
from the fingerprint read, then the board serial, the licence, the
interface list and the address list -- each one sent only after the previous
answer came back.

What is pinned here is that the independent ones now go out together, and that
a burst still degrades correctly: a device that refuses one of them (a virtual router has
no /system/routerboard) must still yield everything else.
"""

import sys

from x3.proto import message as m
from x3.proto.device import (
    CMD_GET,
    CMD_GETALL,
    DeviceClient,
    DeviceError,
    STD_OBJS,
)

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
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())


RESOURCES = (24, 2)
IDENTITY = (24, 1)
ROUTERBOARD = (24, 17)
LICENCE = (65,)
IFACES = (20, 0)
ADDRS = (20, 1)


def row(**fields) -> m.Message:
    msg = m.Message()
    for code, value in fields.items():
        code = int(code[1:], 16)
        if isinstance(value, bytes):
            msg.add_raw(code, value)
        elif isinstance(value, str):
            msg.add_string(code, value)
        else:
            msg.add_u32(code, value)
    return msg


def listing(*rows: m.Message) -> m.Message:
    out = m.Message()
    out.add_typed(STD_OBJS, m.T_MESSAGE_ARRAY, list(rows))
    return out


class CountingClient(DeviceClient):
    """Records how the reads were issued: one burst, or one at a time."""

    #: path -> reply, or an exception to answer with.
    ANSWERS = {
        RESOURCES: row(x16="7.21.1", x2C="EDGE10-LAB", x1A="arm"),
        IDENTITY: row(x01="rb-lab"),
        ROUTERBOARD: row(xD="HGF88TQ2X4Z", xA="EDGE10-LAB"),
        LICENCE: row(xD="ABCD-1234"),
        IFACES: listing(row(x404=b"\xe4\x8d\x8c\x11\x22\x33"),
                        row(x404=b"\xe4\x8d\x8c\x11\x22\x34")),
        ADDRS: listing(row(x1=0x0157A8C0)),   # 192.168.87.1, little-endian
    }

    def __init__(self, refuse=()):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.session = object()      # only truthiness is checked
        self.bursts: list[list[tuple]] = []
        self.serial: list[tuple] = []
        self._refuse = set(refuse)

    def _answer(self, path):
        if path in self._refuse:
            return DeviceError(f"{path} refused")
        return self.ANSWERS.get(path, m.Message())

    def pipeline(self, items, *, timeout=None, quiet=False):
        if self.session is None:      # what the real one does
            raise DeviceError("not connected")
        self.bursts.append([(tuple(it[0]), it[1]) for it in items])
        return [self._answer(tuple(it[0])) for it in items]

    def _request(self, path, command, **kw):
        self.serial.append((tuple(path), command))
        answer = self._answer(tuple(path))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def round_trips(self) -> int:
        """Latencies paid: one per burst, one per lone request."""
        return len(self.bursts) + len(self.serial)


# --- device info ------------------------------------------------------------

c = CountingClient()
info = c.refresh_info()
check("resources and identity go out together, both as GET",
      c.bursts, [[(RESOURCES, CMD_GET), (IDENTITY, CMD_GET)]])
check("...and nothing is sent on its own", c.serial, [])
check("...for one round trip, not two", c.round_trips(), 1)
check("the answers are still decoded", (info.board, info.version, info.identity),
      ("EDGE10-LAB", "7.21.1", "rb-lab"))

# One refused read must not take the other down with it.
c = CountingClient(refuse={IDENTITY})
info = c.refresh_info()
check("a refused identity still leaves the resources", info.board,
      "EDGE10-LAB")
check("...and reports no identity", info.identity, "")
c = CountingClient(refuse={RESOURCES, IDENTITY})
info = c.refresh_info()
check("both refused is an empty answer, not a raise", (info.board, info.identity),
      ("", ""))


# --- fingerprint ------------------------------------------------------------

c = CountingClient()
c.refresh_info()
c.bursts.clear()
c.serial.clear()
fp = c.read_fingerprint()
check("the fingerprint's four reads are one burst, with the right commands",
      c.bursts, [[(ROUTERBOARD, CMD_GET), (LICENCE, CMD_GET),
                  (IFACES, CMD_GETALL), (ADDRS, CMD_GETALL)]])
check("...with nothing sent on its own", c.serial, [])
check("...for one round trip, not four", c.round_trips(), 1)
check("the serial is read", fp.serial, "HGF88TQ2X4Z")
check("the model is read", fp.model, "EDGE10-LAB")
check("every interface MAC is read", fp.macs,
      ["e4:8d:8c:11:22:33", "e4:8d:8c:11:22:34"])
check("the addresses are read", fp.ips, ["192.168.87.1"])
ok("a device with a serial needs no licence id", not fp.system_id)

# A virtual router has no /system/routerboard: the licence id stands in, and it was in the
# same burst rather than waiting to find out whether it was needed.
c = CountingClient(refuse={ROUTERBOARD})
c.refresh_info()
c.bursts.clear()
fp = c.read_fingerprint()
check("a refused routerboard read costs no extra round trip", len(c.bursts), 1)
check("...and the licence id stands in for the serial", fp.system_id, "ABCD-1234")
check("...with the serial left empty", fp.serial, "")

# connect() already read the device info; the fingerprint must not read it again.
c = CountingClient()
c.refresh_info()
before = c.round_trips()
c.read_fingerprint()
check("the fingerprint reuses the info connect already read",
      c.round_trips() - before, 1)

# With nothing to talk to, both stay best-effort rather than raising.
c = CountingClient()
c.session = None
try:
    check("info with no session is empty, not an error",
          c.refresh_info().board, "")
except Exception as e:  # noqa: BLE001
    failures.append(f"refresh_info raised with no session: {e!r}")
    print(f"FAIL refresh_info raised with no session: {e!r}")
try:
    fp = c.read_fingerprint()
    check("...and so is the fingerprint", (fp.serial, fp.macs), ("", []))
except Exception as e:  # noqa: BLE001
    failures.append(f"read_fingerprint raised with no session: {e!r}")
    print(f"FAIL read_fingerprint raised with no session: {e!r}")


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall connect-read tests passed")
