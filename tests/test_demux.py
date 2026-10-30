"""Offline tests for the frame demultiplexer.

No device and no pytest required:

    .venv/bin/python test_demux.py

The session is stubbed: frames come from a scripted list instead of a socket,
so routing can be driven deterministically.
"""

import threading
import time

from x3.proto import message as m
from x3.proto.demux import DemuxClosed, FrameDemux

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    if not cond:
        failures.append(label)


def frame(to=None, frm=None, rid=None, cmd=0xFE0004, data=None):
    msg = m.Message()
    if to is not None:
        msg.add_u32_array(m.SYS_TO, list(to))
    if frm is not None:
        msg.add_u32_array(m.SYS_FROM, list(frm))
    msg.set_command(cmd)
    if rid is not None:
        msg.set_request_id(rid)
    if data is not None:
        msg.add_raw(0x000002, data)
    return msg


class StubSession:
    """Minimal ModernSession stand-in: a queue in, a list of sent frames out."""

    timeout = 1.0

    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self._cond = threading.Condition()
        self.sent = []
        self._sock = None
        self.closed = False

    # -- what the demux reader calls
    def _recv_message(self):
        with self._cond:
            while not self._incoming and not self.closed:
                self._cond.wait(2.0)
            if self.closed:
                raise ConnectionError("peer closed during read")
            return self._incoming.pop(0)

    def send(self, msg):
        self.sent.append(msg)

    # -- test helpers
    def feed(self, *msgs):
        with self._cond:
            self._incoming.extend(msgs)
            self._cond.notify_all()

    def die(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def wait_for(pred, secs=2.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# --- replies route on (SYS_TO, request id) ----------------------------------

sess = StubSession()
d = FrameDemux(sess)
d.start()
try:
    reply = []
    t = threading.Thread(
        target=lambda: reply.append(d.request(frame(to=(76,), frm=(0, 11), rid=7))),
        daemon=True,
    )
    t.start()
    ok("request sent the frame", wait_for(lambda: len(sess.sent) == 1))
    # The device echoes our SYS_FROM back as SYS_TO.
    sess.feed(frame(to=(0, 11), frm=(76,), rid=7))
    t.join(timeout=2.0)
    check("reply reaches the waiter", len(reply), 1)
    check("nothing went unrouted", d.unrouted, 0)

    # rid 0 is legal and must not read as "no rid".
    reply0 = []
    t = threading.Thread(
        target=lambda: reply0.append(d.request(frame(to=(24, 2), frm=(0, 12), rid=0))),
        daemon=True,
    )
    t.start()
    ok("rid-0 request sent", wait_for(lambda: len(sess.sent) == 2))
    sess.feed(frame(to=(0, 12), rid=0))
    t.join(timeout=2.0)
    check("rid 0 routes like any other id", len(reply0), 1)
    check("rid 0 did not go unrouted", d.unrouted, 0)
finally:
    d.stop()

# --- two callers sharing a request id on different paths --------------------

sess = StubSession()
d = FrameDemux(sess)
d.start()
try:
    got = {}

    def ask(path, tag):
        got[tag] = d.request(frame(to=(24, 2), frm=path, rid=14))

    a = threading.Thread(target=ask, args=((0, 151), "a"), daemon=True)
    b = threading.Thread(target=ask, args=((0, 153), "b"), daemon=True)
    a.start()
    b.start()
    ok("both requests sent", wait_for(lambda: len(sess.sent) == 2))
    # Same rid, different paths -- each must reach its own caller.
    sess.feed(frame(to=(0, 153), rid=14, data=b"B"))
    sess.feed(frame(to=(0, 151), rid=14, data=b"A"))
    a.join(timeout=2.0)
    b.join(timeout=2.0)
    check("a got its own reply", got["a"].get_raw(0x000002), b"A")
    check("b got its own reply", got["b"].get_raw(0x000002), b"B")
    check("no cross-talk went unrouted", d.unrouted, 0)
finally:
    d.stop()

# --- unsolicited frames route to a per-path subscription --------------------

sess = StubSession()
d = FrameDemux(sess)
d.start()
try:
    sub = d.subscribe((0, 201))
    # A terminal data frame: no request id at all.
    sess.feed(frame(to=(0, 201), frm=(76,), cmd=0xA0067, data=b"hello"))
    msg = sub.get(timeout=2.0)
    check("terminal output reaches its subscription", msg.get_raw(0x000002), b"hello")
    check("subscribed frames are not unrouted", d.unrouted, 0)

    # A second terminal on its own path must not see the first one's output.
    other = d.subscribe((0, 202))
    sess.feed(frame(to=(0, 201), frm=(76,), cmd=0xA0067, data=b"first"))
    check("the other terminal stays empty", other.get(timeout=0.3), None)
    check("output went to the right terminal", sub.get(timeout=1.0).get_raw(0x000002), b"first")

    d.unsubscribe(other)
    ok("unsubscribe closes the queue", other.closed)
finally:
    d.stop()

# --- a frame nobody claimed is counted, not dropped ------------------------

sess = StubSession()
seen = []
d = FrameDemux(sess, on_unrouted=seen.append)
d.start()
try:
    sess.feed(frame(to=(9, 9), frm=(1067,), cmd=0xFE0014))
    ok("unrouted frame is counted", wait_for(lambda: d.unrouted == 1))
    check("on_unrouted was called", len(seen), 1)
    # ...and it is still retrievable, so the legacy recv() shim sees it.
    check("it lands in the sink", d.recv_any(timeout=1.0).get_u32(m.SYS_COMMAND), 0xFE0014)
finally:
    d.stop()

# --- a late reply is classified as late, not unrouted ---------------------

sess = StubSession()
d = FrameDemux(sess)
d.start()
try:
    try:
        d.request(frame(to=(24, 2), frm=(0, 30), rid=5), timeout=0.2)
        failures.append("a request with no reply should time out")
    except TimeoutError:
        pass
    sess.feed(frame(to=(0, 30), rid=5))
    ok("the late reply is recorded as late", wait_for(lambda: d.late_replies == 1))
    check("a late reply is not counted as unrouted", d.unrouted, 0)
finally:
    d.stop()

# --- a dead connection wakes every waiter --------------------------------

sess = StubSession()
d = FrameDemux(sess)
d.start()
try:
    sub = d.subscribe((0, 55))
    errors = []

    def blocked():
        try:
            d.request(frame(to=(24, 2), frm=(0, 56), rid=9), timeout=5.0)
        except BaseException as e:  # noqa: BLE001
            errors.append(type(e).__name__)

    t = threading.Thread(target=blocked, daemon=True)
    t.start()
    ok("request is in flight", wait_for(lambda: len(sess.sent) == 1))
    sess.die()
    t.join(timeout=3.0)
    ok("the blocked request was woken, not left hanging", errors == ["ConnectionError"])
    try:
        sub.get(timeout=1.0)
        failures.append("a closed subscription should raise")
    except (DemuxClosed, ConnectionError):
        pass
finally:
    d.stop()

# --- request() refuses to route what it cannot ---------------------------

sess = StubSession()
d = FrameDemux(sess)
try:
    try:
        d.request(frame(to=(76,), frm=(0, 11)))  # no request id
        failures.append("request() without a rid should be rejected")
    except ValueError:
        pass
finally:
    d.stop()

# --- ModernSession dispatch: the shims, the lifecycle, the write lock -------
# The tests above drive FrameDemux directly; this one goes through the real
# recv()/request()/send()/start_demux() wiring.

from x3.proto.modern import ModernSession  # noqa: E402


class FakeSession(ModernSession):
    """A ModernSession with the socket replaced by a scripted frame queue."""

    def __init__(self):
        super().__init__("192.0.2.1")
        self._authenticated = True
        self.timeout = 1.0
        self._q = []
        self._cond = threading.Condition()
        self.wire = []
        self.dead = False
        self._concurrent_writes = 0
        self.max_concurrent_writes = 0

    def _recv_message(self):
        with self._cond:
            while not self._q and not self.dead:
                self._cond.wait(2.0)
            if self.dead:
                raise ConnectionError("peer closed during read")
            return self._q.pop(0)

    def _send_encrypted(self, payload):
        # Detect any overlap: two threads inside a write at once would
        # interleave bytes on the wire.
        self._concurrent_writes += 1
        self.max_concurrent_writes = max(
            self.max_concurrent_writes, self._concurrent_writes
        )
        time.sleep(0.002)
        self.wire.append(payload)
        self._concurrent_writes -= 1

    def feed(self, *msgs):
        with self._cond:
            self._q.extend(msgs)
            self._cond.notify_all()

    def die(self):
        with self._cond:
            self.dead = True
            self._cond.notify_all()


s = FakeSession()
check("no demux by default", s.demux, None)
d = s.start_demux()
ok("start_demux returns the demux", d is not None and s.demux is d)
check("start_demux is idempotent", s.start_demux() is d, True)
try:
    # request() dispatches through the demux and routes by (from, rid).
    out = []
    rid = s.next_request_id()
    t = threading.Thread(
        target=lambda: out.append(s.request(frame(to=(24, 2), frm=(0, 11), rid=rid))),
        daemon=True,
    )
    t.start()
    ok("request reached the wire", wait_for(lambda: len(s.wire) == 1))
    s.feed(frame(to=(0, 11), rid=rid, data=b"ok"))
    t.join(timeout=2.0)
    check("session.request routes via the demux", len(out), 1)

    # A terminal subscription and menu reads interleave on one connection --
    # this is the case that made a shared session impossible before.
    term = d.subscribe((0, 201))
    chunks = []
    stop_reader = threading.Event()

    def term_reader():
        while not stop_reader.is_set():
            try:
                msg = term.get(timeout=0.1)
            except Exception:  # noqa: BLE001
                return
            if msg is not None:
                chunks.append(msg.get_raw(0x000002))

    tr = threading.Thread(target=term_reader, daemon=True)
    tr.start()

    polls = []

    def poller(n):
        r = s.next_request_id()
        polls.append(s.request(frame(to=(24, 2), frm=(0, 20 + n), rid=r), timeout=2.0))

    workers = [threading.Thread(target=poller, args=(i,), daemon=True) for i in range(4)]
    for w in workers:
        w.start()
    ok("four polls in flight", wait_for(lambda: len(s.wire) >= 5))
    # Terminal output arrives in the middle of the outstanding polls.
    for i in range(6):
        s.feed(frame(to=(0, 201), frm=(76,), cmd=0xA0067, data=f"out{i}".encode()))
    for i in range(4):
        s.feed(frame(to=(0, 20 + i), rid=i + 2))
    for w in workers:
        w.join(timeout=3.0)
    # Let the terminal reader drain before stopping it. Joining on a timeout
    # while frames are still queued made this flake under load: the check
    # below counted whatever had arrived, not what was sent.
    wait_for(lambda: len(chunks) >= 6, 3.0)
    stop_reader.set()
    tr.join(timeout=2.0)
    check("every poll got its reply", len(polls), 4)
    check("terminal output was not eaten by the polls", len(chunks), 6)
    check("interleaving lost nothing", d.unrouted, 0)
    check("writes never overlapped", s.max_concurrent_writes, 1)
finally:
    s.stop_demux()
check("stop_demux clears it", s.demux, None)

# With no demux, recv() and request() keep the original send-then-recv path.
s2 = FakeSession()
s2.feed(frame(to=(0, 11), rid=1, data=b"direct"))
check("recv() without a demux reads the socket",
      s2.recv().get_raw(0x000002), b"direct")


# --- Safe Mode requests route like any other request -------------------------
# The old implementation typed /safe-mode/* into a terminal channel and drained
# the socket itself, which ate frames belonging to other callers. Native Safe
# Mode is handler [17] over plain request/reply, so it needs nothing special
# from the demux -- this pins that it stays an ordinary caller.

from x3.proto.safemode import (  # noqa: E402
    CMD_RELEASE,
    CMD_TAKE,
    SAFE_MODE_PATH,
    SESSION_ID,
    SafeModeChannel,
)

SAFE_FROM = (0, 487)


class TimeoutSpy:
    """Socket stand-in that records every settimeout()."""

    def __init__(self):
        self.timeouts = []

    def gettimeout(self):
        return 10.0

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        pass

    def close(self):
        pass


s3 = FakeSession()
spy = TimeoutSpy()
s3._sock = spy
# The replying thread has to notice the write before the waiter gives up, and
# the default 1s loses that race on a loaded machine.
s3.timeout = 5.0
noted = []
d3 = s3.start_demux(on_unrouted=noted.append)


def request(path, command, *, from_path=None, extra=None, quiet=False):
    """Stand-in for DeviceClient._request, routed through the real demux."""
    msg = (
        m.Message()
        .add_u32_array(m.SYS_TO, list(path))
        .add_u32_array(m.SYS_FROM, list(from_path or (0, 1)))
        .set_command(command)
        .set_request_id(s3.next_request_id())
        .set_reply_expected(True)
    )
    if extra is not None:
        extra(msg)
    return s3.request(msg)


chan = SafeModeChannel(request, from_path=SAFE_FROM)
try:
    wire_before = len(s3.wire)

    def answer(fields=None):
        for _ in range(300):
            if len(s3.wire) > wire_before:
                break
            time.sleep(0.01)
        # FakeSession records raw payloads, not Messages.
        rid = m.Message.from_bytes(s3.wire[-1]).get_u32(m.SYS_REQUEST_ID)
        # A frame addressed to nobody, of the kind the old drain swallowed.
        s3.feed(frame(to=(0, 999), frm=(1067,), cmd=0xFE0014))
        reply = frame(to=SAFE_FROM, frm=(17,), rid=rid)
        for code, value in (fields or {}).items():
            reply.add_u32(code, value)
        s3.feed(reply)

    threading.Thread(target=lambda: answer({SESSION_ID: 7}), daemon=True).start()
    st = chan.take()
    check("take reports the device's session id", st.session_id, 7)
    ok("and that this session holds Safe Mode", st.active_here)

    sent = m.Message.from_bytes(s3.wire[-1])
    check("take addresses handler 17", list(sent.find(m.SYS_TO)[1]), [17])
    check("take is verb 3 with the handler-command bit",
          sent.get_u32(m.SYS_COMMAND), CMD_TAKE)
    check("take sends no session id of its own", sent.get_u32(SESSION_ID), None)
    check("it goes out on our own from-path",
          tuple(sent.find(m.SYS_FROM)[1]), SAFE_FROM)

    wire_before = len(s3.wire)
    threading.Thread(target=lambda: answer(), daemon=True).start()
    st = chan.release()
    sent = m.Message.from_bytes(s3.wire[-1])
    check("release is verb 5", sent.get_u32(m.SYS_COMMAND), CMD_RELEASE)
    check("release echoes the id the device assigned",
          sent.get_u32(SESSION_ID), 7)
    ok("and the session no longer holds Safe Mode", not st.active_here)

    check("no 0.2s timeout left on the shared socket",
          [t for t in spy.timeouts if t == 0.2], [])
    check("only start_demux touched the socket timeout", spy.timeouts, [None])
    check("foreign frames are reported, not eaten", d3.unrouted, 2)
    check("on_unrouted fired for them", len(noted), 2)
finally:
    s3.stop_demux()

# --- the reader dying with nobody waiting -----------------------------------
#
# _fail_all only reaches waiters that happen to be parked at that instant, so
# a reader that died between requests used to leave no trace at all: the next
# request was sent into a socket nobody was reading and came back 12s later as
# "no reply", which reads as a slow device rather than a dead session.

s4 = StubSession()
reader_errors = []
d4 = FrameDemux(s4, on_reader_error=reader_errors.append)
d4.start()
ok("reader is up", wait_for(lambda: d4.alive))

s4.die()  # raises inside the reader while no request is outstanding
ok("reader stops", wait_for(lambda: not d4.alive))
check("a reader that dies unwatched still reports it", len(reader_errors), 1)
ok("...and remembers why", isinstance(d4.reader_error, ConnectionError))

t0 = time.monotonic()
try:
    d4.request(frame(to=(0, 1), frm=(0, 11), rid=99), timeout=5.0)
    after = None
except Exception as e:  # noqa: BLE001
    after = e
ok("a request after that is refused", isinstance(after, DemuxClosed))
# The point is that it does not sit out the 5s timeout; the bound is loose so
# a loaded machine cannot make this flaky.
ok("...at once, not after the timeout", time.monotonic() - t0 < 2.5)
ok("...naming what killed the reader", "ConnectionError" in str(after))
check("...and nothing was sent into the dead socket", len(s4.sent), 0)

if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all demux tests passed")
