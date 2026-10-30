"""Device-pushed change notifications — §11 SUBSCRIBE / NOTIFY.

No device and no pytest required:

    .venv/bin/python test_subscribe.py

A list was only ever as fresh as the last thing that asked for it, so an edit
made in another Winbox (or by the device itself) left the window showing stale
rows until someone hit Refresh. §11 documents the mechanism: SUBSCRIBE
(0xFE0012) opens a push channel, the device streams NOTIFY (0xFE000B) frames to
the subscriber's own [0,N] endpoint, UNSUBSCRIBE (0xFE0013) tears it down.

The trap §11 calls out: a NOTIFY frame only *routes*. The changed records sit one
level down in the 0xFE0002 array, so reading the top level finds nothing.
"""

import threading
import time

from x3.proto import message as m
from x3.proto.demux import FrameDemux
from x3.proto.device import (
    CMD_SUBSCRIBE,
    CMD_UNSUBSCRIBE,
    DeviceClient,
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


class StubSession:
    """Enough ModernSession for a demux to read from and a client to write to."""

    timeout = 1.0

    def __init__(self):
        self._incoming: list = []
        self._cond = threading.Condition()
        self.sent: list = []
        self._sock = None
        self.closed = False
        self.demux = None

    def _recv_message(self):
        with self._cond:
            while not self._incoming and not self.closed:
                self._cond.wait(2.0)
            if self.closed:
                raise ConnectionError("closed")
            return self._incoming.pop(0)

    def send(self, msg):
        self.sent.append(msg)

    def feed(self, *msgs):
        with self._cond:
            self._incoming.extend(msgs)
            self._cond.notify_all()

    def die(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def notify(to: tuple[int, ...], frm: tuple[int, ...], rows: list):
    """A NOTIFY the way the device sends one: records nested under 0xFE0002."""
    msg = m.Message()
    msg.add_u32_array(m.SYS_TO, list(to))
    msg.add_u32_array(m.SYS_FROM, list(frm))
    msg.set_command(0xFE000B)
    # The record array is a T_MESSAGE_ARRAY, the same shape GETALL uses.
    msg.add_typed(0xFE0002, m.T_MESSAGE_ARRAY, rows)
    return msg


def record(code: int, value: str):
    msg = m.Message()
    msg.add_string(code, value)
    return msg


# --- subscribing sends the documented verb --------------------------------

sess = StubSession()
demux = FrameDemux(sess)
sess.demux = demux
demux.start()

client = DeviceClient.__new__(DeviceClient)   # no socket, no auth
client.session = sess
client.readonly = False
client._lock = threading.RLock()
client._from_seq = 200
client.session_log = None
client.schema = None
client.client_ident = "winbox-3.42"
client._safe_mode = None

try:
    sub, from_path = client.watch((20, 0))
    ok("a subscription is opened", sub is not None)
    check("...on our own endpoint", from_path[0], 0)
    sent = m.Message.from_bytes(sess.sent[-1].to_bytes()) if hasattr(
        sess.sent[-1], "to_bytes") else sess.sent[-1]
    check("SUBSCRIBE is the verb sent", sent.get_u32(m.SYS_COMMAND), CMD_SUBSCRIBE)
    check("...addressed to the handler",
          tuple(sent.find(m.SYS_TO)[1]), (20, 0))
    check("...from the subscription's endpoint",
          tuple(sent.find(m.SYS_FROM)[1]), from_path)
    ok("...with no request id, since nothing replies",
       sent.get_u32(m.SYS_REQUEST_ID) is None)

    # --- a push reaches the subscription ---------------------------------

    sess.feed(notify(from_path, (20, 0), [record(0x0C, "renamed-elsewhere")]))
    pushed = sub.get(timeout=2.0)
    ok("the push is routed to the subscriber", pushed is not None)

    rows = DeviceClient.notify_rows(pushed)
    check("the changed records are one level down", len(rows), 1)
    check("...and carry the new value",
          rows[0].get_string(0x0C), "renamed-elsewhere")

    # Reading the top level finds nothing — the §11 tooling trap.
    ok("the top level does not carry the value",
       pushed.get_string(0x0C) is None)

    # A frame that is not a notify yields no records rather than raising.
    check("a frame with no record array is empty, not an error",
          DeviceClient.notify_rows(m.Message()), [])

    # --- unsubscribing --------------------------------------------------

    client.unwatch((20, 0), sub, from_path=from_path)
    last = sess.sent[-1]
    check("UNSUBSCRIBE is the verb sent", last.get_u32(m.SYS_COMMAND),
          CMD_UNSUBSCRIBE)
    ok("...and the subscription is closed", sub.closed)
finally:
    sess.die()
    demux.stop()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all subscribe tests passed")
