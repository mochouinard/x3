"""Offline tests for MAC-Winbox inbound reassembly.

No device and no pytest required:

    .venv/bin/python test_macwinbox.py

MAC-Winbox is an ARQ protocol over UDP: the device resends a DATA packet until
an ACK covers it, and the 4-byte ``counter`` is the byte offset that says where
each packet belongs in the stream.

The receiver used to ignore ``counter`` entirely and append every DATA payload
in arrival order, ACKing whatever had just turned up. A retransmission -- the
normal consequence of one dropped ACK -- was therefore appended a second time,
splicing duplicate bytes into the Winbox stream. The framing layer then ran
past the end of the current frame and met the next frame's first-segment
marker where a 0xff continuation belonged:

    ModernProtocolError: bad continuation marker: 0x6      (0x06 = _MARKER_FIRST)

which killed the reader thread and left every later request timing out.

It only showed up on big replies (a full interface or address list). Those take
many datagrams, and only then is a lost ACK likely; a small GET fits in one
packet and always looked fine.
"""

from x3.proto.macwinbox import (
    PTYPE_DATA,
    MacWinboxTransport,
    parse_header,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def make_transport():
    t = MacWinboxTransport("aa:bb:cc:dd:ee:ff")
    t.src_mac = b"\x11" * 6
    t.acked: list[int] = []
    # No socket in an offline test; record what would have been ACKed.
    t._broadcast = lambda pkt: t.acked.append(parse_header(pkt)["counter"])
    return t


def data(counter: int, payload: bytes) -> dict:
    return {"ptype": PTYPE_DATA, "counter": counter, "payload": payload}


t = make_transport()

# --- the ordinary case is unchanged ---------------------------------------

t._handle_incoming(data(0, b"AAAA"))
t._handle_incoming(data(4, b"BBBB"))
check("in-order payloads are kept in order", bytes(t._recv_buf), b"AAAABBBB")
check("the ack is cumulative", t.acked[-1], 8)

# --- a retransmission must not be appended twice --------------------------

t._handle_incoming(data(4, b"BBBB"))
check("a resent packet is not appended again", bytes(t._recv_buf), b"AAAABBBB")
check("...but is acked, so the device stops resending", t.acked[-1], 8)
check("...and is counted", t.duplicates, 1)

# --- a gap must not be spliced over ---------------------------------------

t._handle_incoming(data(12, b"DDDD"))
check("a packet past a gap is not spliced in", bytes(t._recv_buf), b"AAAABBBB")
check("...the ack holds at the last contiguous byte", t.acked[-1], 8)
check("...and the gap is counted", t.gaps, 1)

t._handle_incoming(data(8, b"CCCC"))  # the missing packet, resent
check("the resend fills the hole, held packet and all",
      bytes(t._recv_buf), b"AAAABBBBCCCCDDDD")
# The held DDDD is now contiguous again, so it is delivered with it rather
# than being dropped -- dropping it stalled the stream until the device gave
# up and ended the session.
check("...and the held packet follows it", bytes(t._recv_buf)[-4:], b"DDDD")
check("...with the ack past both", t.acked[-1], 16)
check("nothing is left held", len(t._held), 0)

# --- pure reordering is not a loss at all --------------------------------

# The first datagram seen defines the origin — it cannot itself be "early",
# there is nothing to be early relative to. Reordering after that is handled.
t3 = make_transport()
t3._handle_incoming(data(0, b"AAAA"))   # establishes the origin
t3._handle_incoming(data(8, b"CCCC"))   # arrives before BBBB
check("an early datagram is held, not appended",
      bytes(t3._recv_buf), b"AAAA")
t3._handle_incoming(data(4, b"BBBB"))
check("out-of-order datagrams are reassembled, not lost",
      bytes(t3._recv_buf), b"AAAABBBBCCCC")
check("...and acked as one contiguous run", t3.acked[-1], 12)

# --- the device need not start counting at zero ---------------------------

t2 = make_transport()
t2._handle_incoming(data(5000, b"XY"))
check("the first packet sets the origin", bytes(t2._recv_buf), b"XY")
check("...and the ack follows from it", t2.acked[-1], 5002)
check("...with nothing treated as a gap", t2.gaps, 0)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all mac-winbox tests passed")
