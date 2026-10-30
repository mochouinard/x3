"""Offline tests for audit mode: the .wba container, the suppression rules and
the hooks that feed it.

No device and no pytest required:

    .venv/bin/python test_audit.py

What matters here is that the two promises in x3/proto/audit.py hold. A reader
must survive a record type it has never seen (or the format cannot grow), and a
frame is only ever dropped when its content is provably already in the file --
so every suppression case below is paired with a check that a *changed* reply
gets through.
"""

import os
import struct
import tempfile

from x3.proto import audit
from x3.proto import message as m
from x3.proto.client_ident import CLIENT_IDENT_FIELD
from x3.proto.device import (
    CMD_GETALL,
    CMD_GETOBJ,
    CMD_SETOBJ,
    DeviceClient,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


TMP = tempfile.mkdtemp(prefix="wbaudit-")
_seq = [0]


def recorder(level="config", **kw):
    _seq[0] += 1
    path = os.path.join(TMP, f"t{_seq[0]}.wba")
    return audit.AuditRecorder(
        "10.0.0.1", "admin", port=8291, path=path,
        policy=audit.AuditPolicy(level=level, **kw),
    )


def req(path, command, rid, *, fields=()):
    msg = (
        m.Message()
        .add_u32_array(m.SYS_TO, list(path))
        .add_u32_array(m.SYS_FROM, [0, 12])
        .set_command(command)
        .set_request_id(rid)
    )
    for code, value in fields:
        msg.add_u32(code, value)
    return msg


def rep(path, rid, *, fields=(), error=None):
    msg = (
        m.Message()
        .add_u32_array(m.SYS_TO, [0, 12])
        .add_u32_array(m.SYS_FROM, list(path))
        .set_request_id(rid)
    )
    for code, value in fields:
        msg.add_u32(code, value)
    if error is not None:
        msg.add_string(m.SYS_ERROR_STRING, error)
    return msg


def exchange(rec, request, reply):
    rec.sent(request.to_bytes(), request,
             poll=bool(getattr(request, "audit_poll", False)))
    if reply is not None:
        rec.received(reply.to_bytes(), reply)


def read(rec):
    rec.close(reason="test")
    return audit.AuditFile(rec.path)


def kinds(af):
    return [r.name for r in af]


# --- the container -------------------------------------------------------

r = recorder()
r.note("hello")
af = read(r)
check("magic identifies the container", af.header["container"], "wbaudit")
check("version is in the header and the magic", af.version, audit.VERSION)
check("the device is named in the header", af.header["host"], "10.0.0.1")
check("so is the level", af.header["level"], "config")
check("a note round-trips", [x.text() for x in af if x.type == audit.REC_NOTE],
      ["hello"])
check("and the file is closed off", kinds(af)[-1], "END")

# Nothing recorded means no file at all, rather than a header nobody can use.
r = recorder()
r.close()
ok("a session that said nothing leaves no file", not os.path.exists(r.path))

# --- forward compatibility: the whole point of the length field ----------

r = recorder()
r.note("before")
r._write(0x5E, 0, b"a record type from the future")   # noqa: SLF001
r.note("after")
af = read(r)
names = kinds(af)
check("an unknown record type is skipped, not fatal",
      [n for n in names if n == "NOTE"], ["NOTE", "NOTE"])
ok("and it is still handed to the reader",
   any(x.type == 0x5E for x in af))
check("records after it survive",
      [x.text() for x in af if x.type == audit.REC_NOTE], ["before", "after"])

# A file cut off mid-record must say so rather than yield half a frame.
r = recorder()
exchange(r, req((14, 1), CMD_GETALL, 1), rep((14, 1), 1, fields=[(0x10, 1)]))
r.close()
whole = open(r.path, "rb").read()
cut = os.path.join(TMP, "cut.wba")
open(cut, "wb").write(whole[:-4])
try:
    list(audit.AuditFile(cut))
    failures.append("a truncated file should raise")
except ValueError as e:
    ok("a truncated record is reported", "truncated" in str(e))

# --- pairing: a request is held until its reply decides its fate ---------

r = recorder()
exchange(r, req((14, 1), CMD_GETALL, 1), rep((14, 1), 1, fields=[(0x10, 1)]))
af = read(r)
check("a request and its reply are both kept",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])
check("in request-then-reply order", kinds(af)[0], "SENT")

r = recorder()
r.sent(*(lambda q: (q.to_bytes(), q))(req((14, 1), CMD_GETALL, 7)))
af = read(r)
check("a request whose reply never came is still written",
      [x.name for x in af if x.is_wire], ["SENT"])

# A frame with no request id can never be paired, so it goes out immediately.
r = recorder()
oneway = (
    m.Message()
    .add_u32_array(m.SYS_TO, [14, 1])
    .add_u32_array(m.SYS_FROM, [0, 12])
    .set_command(0xFE0012)
)
r.sent(oneway.to_bytes(), oneway)
af = read(r)
check("a one-way send is written straight through",
      [x.name for x in af if x.is_wire], ["SENT"])

# --- suppression rule 1: polling the caller marked as polling ------------

r = recorder()
q = req((14, 1), CMD_GETOBJ, 1)
q.audit_poll = True
exchange(r, q, rep((14, 1), 1, fields=[(0x10, 5)]))
af = read(r)
check("a polling exchange is dropped", [x.name for x in af if x.is_wire], [])
check("and counted instead", af.records()[-1].json()["suppressed_polls"], 1)

r = recorder(level="all")
q = req((14, 1), CMD_GETOBJ, 1)
q.audit_poll = True
exchange(r, q, rep((14, 1), 1, fields=[(0x10, 5)]))
af = read(r)
check("level=all keeps polling", [x.name for x in af if x.is_wire],
      ["SENT", "RECV"])
ok("flagged as polling rather than dropped",
   all(x.flags & audit.F_POLL for x in af if x.is_wire))

# level=stats is how a user turns statistics capture on: polling is kept, but
# the content-based collapse still applies, so nothing is recorded twice.
r = recorder(level="stats")
q = req((14, 1), CMD_GETOBJ, 1)
q.audit_poll = True
exchange(r, q, rep((14, 1), 1, fields=[(0x10, 5)]))
af = read(r)
check("level=stats keeps a polling exchange",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])
ok("still flagged as polling, so a reader can filter it out",
   all(x.flags & audit.F_POLL for x in af if x.is_wire))
check("and nothing is counted as suppressed",
      af.records()[-1].json()["suppressed_polls"], 0)

r = recorder(level="stats")
for rid, value in ((1, 5), (2, 6), (3, 7)):
    q = req((14, 1), CMD_GETOBJ, rid)
    q.audit_poll = True
    exchange(r, q, rep((14, 1), rid, fields=[(0x10, value)]))
af = read(r)
check("a counter series is kept sample by sample",
      len([x for x in af if x.name == "RECV"]), 3)

r = recorder(level="stats")
for rid in (1, 2, 3, 4):
    q = req((14, 1), CMD_GETOBJ, rid)
    q.audit_poll = True
    exchange(r, q, rep((14, 1), rid, fields=[(0x10, 5)]))
af = read(r)
check("but polling something static still collapses",
      len([x for x in af if x.name == "RECV"]), 1)
check("counted as unchanged, not as polling",
      (af.records()[-1].json()["suppressed_unchanged"],
       af.records()[-1].json()["suppressed_polls"]), (3, 0))

# The ladder: each level keeps everything the one below it keeps.
check("levels are ordered least to most",
      audit.LEVELS, ("changes", "config", "stats", "all"))
check("the default is config", audit.DEFAULT_LEVEL, "config")
drops = {lv: (audit.AuditPolicy(level=lv).drop_polls,
              audit.AuditPolicy(level=lv).drop_unchanged,
              audit.AuditPolicy(level=lv).writes_only)
         for lv in audit.LEVELS}
check("changes drops polls and reads", drops["changes"], (True, True, True))
check("config drops polls, keeps reads", drops["config"], (True, True, False))
check("stats keeps polls", drops["stats"], (False, True, False))
check("all keeps everything", drops["all"], (False, False, False))
check("every level is described for the UI and the CLI",
      sorted(audit.LEVEL_HELP), sorted(audit.LEVELS))

# --- suppression rule 2: a reply that repeats the last one ---------------

r = recorder()
for rid in (1, 2, 3):
    exchange(r, req((14, 1), CMD_GETALL, rid), rep((14, 1), rid, fields=[(0x10, 5)]))
af = read(r)
check("three identical reads collapse to one exchange",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])
check("the repeats are counted",
      af.records()[-1].json()["suppressed_unchanged"], 2)

r = recorder()
for rid, value in ((1, 5), (2, 5), (3, 9)):
    exchange(r, req((14, 1), CMD_GETALL, rid),
             rep((14, 1), rid, fields=[(0x10, value)]))
af = read(r)
check("a reply that actually changed is never dropped",
      [x.name for x in af if x.is_wire], ["SENT", "RECV", "SENT", "RECV"])

# The rule keys on the request shape, so two different reads never mask
# each other however similar their replies look.
r = recorder()
exchange(r, req((14, 1), CMD_GETALL, 1), rep((14, 1), 1, fields=[(0x10, 5)]))
exchange(r, req((20, 1), CMD_GETALL, 2), rep((20, 1), 2, fields=[(0x10, 5)]))
af = read(r)
check("identical replies from different handlers both survive",
      len([x for x in af if x.name == "RECV"]), 2)

# Request ids differ every time; they must not defeat the digest.
r = recorder()
exchange(r, req((14, 1), CMD_GETALL, 100), rep((14, 1), 100, fields=[(0x10, 5)]))
exchange(r, req((14, 1), CMD_GETALL, 999), rep((14, 1), 999, fields=[(0x10, 5)]))
af = read(r)
check("the request id is excluded from the digest",
      len([x for x in af if x.name == "RECV"]), 1)

# --- what suppression must never touch ----------------------------------

r = recorder()
w = req((20, 1), CMD_SETOBJ, 1, fields=[(0x10, 1)])
w.audit_poll = True        # even mislabelled, a write is kept
exchange(r, w, rep((20, 1), 1))
exchange(r, w, rep((20, 1), 1))
af = read(r)
check("a write is recorded however it was flagged",
      len([x for x in af if x.name == "SENT"]), 2)

r = recorder()
q = req((14, 1), CMD_GETALL, 1)
q.audit_poll = True
exchange(r, q, rep((14, 1), 1, error="no such item (2)"))
af = read(r)
check("an error reply beats every suppression rule",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])

# A dropped frame must not seed the "unchanged" baseline: otherwise a pane
# whose first fetch happens to be a quiet auto-refresh loses its config to the
# audit entirely, which is the one thing suppression may never do.
r = recorder()
q = req((14, 1), CMD_GETALL, 1)
q.audit_poll = True
exchange(r, q, rep((14, 1), 1, fields=[(0x10, 5)]))       # dropped as polling
exchange(r, req((14, 1), CMD_GETALL, 2),
         rep((14, 1), 2, fields=[(0x10, 5)]))             # same content, kept
af = read(r)
check("a real read after a dropped poll of the same thing is still recorded",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])
check("and only then does a repeat of it collapse",
      af.records()[-1].json()["suppressed_unchanged"], 0)

r = recorder()
q = req((14, 1), CMD_GETALL, 1)
q.audit_poll = True
exchange(r, q, rep((14, 1), 1, fields=[(0x10, 5)]))
exchange(r, req((14, 1), CMD_GETALL, 2), rep((14, 1), 2, fields=[(0x10, 5)]))
exchange(r, req((14, 1), CMD_GETALL, 3), rep((14, 1), 3, fields=[(0x10, 5)]))
af = read(r)
check("a repeat of content already in the file does collapse",
      af.records()[-1].json()["suppressed_unchanged"], 1)

# Neither correlation map may grow without bound on a long-lived session.
r = recorder()
for rid in range(1, audit._MAX_TRACKED + 60):          # noqa: SLF001
    q = req((14, 1), CMD_GETALL, rid)
    r.sent(q.to_bytes(), q)                            # no reply, ever
ok("requests awaiting a reply are capped",
   len(r._pending) <= audit._MAX_TRACKED)              # noqa: SLF001
af = read(r)
check("and the evicted ones were written, not discarded",
      len([x for x in af if x.name == "SENT"]), audit._MAX_TRACKED + 59)  # noqa: SLF001

r = recorder()
for i in range(audit._MAX_TRACKED + 60):               # noqa: SLF001
    exchange(r, req((14, 1), CMD_GETALL, i, fields=[(0x20, i)]),
             rep((14, 1), i, fields=[(0x10, i)]))
ok("so is the last-reply map",
   len(r._last_reply) <= audit._MAX_TRACKED)           # noqa: SLF001
r.close()

# --- level=changes -------------------------------------------------------

r = recorder(level="changes")
exchange(r, req((14, 1), CMD_GETALL, 1), rep((14, 1), 1, fields=[(0x10, 5)]))
exchange(r, req((20, 1), CMD_SETOBJ, 2, fields=[(0x10, 1)]), rep((20, 1), 2))
af = read(r)
check("level=changes keeps only the write and its reply",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])
got = [x.message().get_u32(m.SYS_COMMAND) for x in af if x.name == "SENT"]
check("and it is the write", got, [CMD_SETOBJ])
check("the reads are counted", af.records()[-1].json()["suppressed_reads"], 2)

# --- redaction -----------------------------------------------------------

r = recorder(secrets=(b"sup3rsecret",))
q = req((2, 2), 0xFE000D, 1)
q.add_string(0x000001, "sup3rsecret")
q.add_string(0x000002, "ether1")
r.sent(q.to_bytes(), q)
af = read(r)
sent = [x for x in af if x.name == "SENT"][0]
body = sent.message()
check("the secret is gone", body.get_string(0x000001), "***********")
check("replaced in place, so the frame still decodes",
      body.get_string(0x000002), "ether1")
ok("and the record says it was redacted", sent.flags & audit.F_REDACTED)

r = recorder(secrets=(b"sup3rsecret",))
q = req((14, 1), CMD_GETALL, 1)
r.sent(q.to_bytes(), q)
af = read(r)
ok("a frame with no secret is not marked redacted",
   not any(x.flags & audit.F_REDACTED for x in af if x.is_wire))

# A password too short to be distinguishable from ordinary binary content is
# left alone: substituting it would corrupt unrelated bytes in every frame.
r = recorder(secrets=(b"ab",))
q = req((14, 1), CMD_GETALL, 1)
q.add_string(0x000002, "ether1")
r.sent(q.to_bytes(), q)
af = read(r)
check("a 2-byte secret is not substituted into the frame",
      [x for x in af if x.name == "SENT"][0].message().get_string(0x000002),
      "ether1")

# --- compression ---------------------------------------------------------

big = req((14, 1), CMD_GETALL, 1)
big.add_string(0x000009, "x" * 4000)
r = recorder()
r.sent(big.to_bytes(), big)
af = read(r)
rec_big = [x for x in af if x.name == "SENT"][0]
ok("a large frame is stored compressed", rec_big.flags & audit.F_ZLIB)
check("and comes back byte-identical", rec_big.payload, big.to_bytes())
ok("the file is smaller than the frame it holds",
   os.path.getsize(r.path) < len(big.to_bytes()))

r = recorder(zlib_min=0)
r.sent(big.to_bytes(), big)
af = read(r)
ok("zlib_min=0 turns compression off",
   not any(x.flags & audit.F_ZLIB for x in af))

small = req((14, 1), CMD_GETALL, 1)
r = recorder()
r.sent(small.to_bytes(), small)
af = read(r)
ok("a small frame is left alone",
   not any(x.flags & audit.F_ZLIB for x in af if x.is_wire))

# --- the digest ----------------------------------------------------------

a = req((14, 1), CMD_GETALL, 1)
b = req((14, 1), CMD_GETALL, 2)
check("two requests differing only in id digest alike",
      audit.content_digest(a), audit.content_digest(b))
c = req((14, 1), CMD_GETALL, 1, fields=[(0x10, 1)])
ok("a request with an extra field does not",
   audit.content_digest(a) != audit.content_digest(c))
nested = m.Message().add_message(0xFE0002, m.Message().add_u32(0x10, 1))
nested2 = m.Message().add_message(0xFE0002, m.Message().add_u32(0x10, 2))
ok("the digest reaches into nested messages",
   audit.content_digest(nested) != audit.content_digest(nested2))

# --- close is idempotent, and survives a double owner -------------------

r = recorder()
exchange(r, req((14, 1), CMD_GETALL, 1), rep((14, 1), 1, fields=[(0x10, 1)]))
r.close(reason="first")
r.close(reason="second")
af = audit.AuditFile(r.path)
check("close twice writes one END record",
      len([x for x in af if x.name == "END"]), 1)
check("and the first reason is the one kept",
      [x.json()["reason"] for x in af if x.name == "END"], ["first"])
r.note("after close")
r.sent(b"M2", None)
af = audit.AuditFile(r.path)
check("nothing is appended after close", kinds(af)[-1], "END")

# --- the ModernSession hook ---------------------------------------------


class FakeWire:
    """A ModernSession with the socket replaced, to exercise the audit hook
    where it actually lives -- send() and _recv_message()."""

    def __init__(self, rec):
        from x3.proto.modern import ModernSession

        self.sess = ModernSession("10.0.0.1")
        self.sess._authenticated = True   # noqa: SLF001
        self.sess.audit = rec
        self.wire: list[bytes] = []
        self.inbox: list[bytes] = []
        self.sess._send_encrypted = self.wire.append      # noqa: SLF001
        self.sess._recv_encrypted = lambda: self.inbox.pop(0)   # noqa: SLF001


r = recorder()
fw = FakeWire(r)
q = req((14, 1), CMD_GETALL, 1)
fw.sess.send(q)
fw.inbox.append(rep((14, 1), 1, fields=[(0x10, 3)]).to_bytes())
fw.sess._recv_message()   # noqa: SLF001
af = read(r)
check("send() and _recv_message() feed the recorder",
      [x.name for x in af if x.is_wire], ["SENT", "RECV"])
check("the recorded bytes are the bytes that went out",
      [x for x in af if x.name == "SENT"][0].payload, q.to_bytes())

r = recorder()
fw = FakeWire(r)
q = req((14, 1), CMD_GETOBJ, 1)
q.audit_poll = True
fw.sess.send(q)
fw.inbox.append(rep((14, 1), 1, fields=[(0x10, 3)]).to_bytes())
fw.sess._recv_message()   # noqa: SLF001
af = read(r)
check("audit_poll on the message reaches the recorder",
      [x.name for x in af if x.is_wire], [])

# An undecodable frame is exactly the one worth keeping.
r = recorder()
fw = FakeWire(r)
fw.inbox.append(b"NOT-M2-AT-ALL")
try:
    fw.sess._recv_message()   # noqa: SLF001
except Exception:
    pass
af = read(r)
ok("an undecodable frame is recorded before the reader dies",
   any(x.name == "RECV" and x.payload == b"NOT-M2-AT-ALL" for x in af))
ok("with a note saying why",
   any(x.name == "NOTE" and "undecodable" in x.text() for x in af))

# closing the session rounds the file off, so no caller has to remember
r = recorder()
fw = FakeWire(r)
q = req((14, 1), CMD_GETALL, 1)
fw.sess.send(q)
fw.sess.close()
af = audit.AuditFile(r.path)
check("ModernSession.close() closes the audit file", kinds(af)[-1], "END")

# --- DeviceClient: off by default, and the flags mean what they say ------

os.environ.pop("X3_AUDIT", None)
os.environ.pop("X3_AUDIT_DIR", None)
c = DeviceClient("10.0.0.1", "u", "p")
ok("audit is off unless asked for", not c.audit_enabled)
check("and defaults to the config level", c.audit_level, "config")
ok("with nothing recording yet", c.audit is None)

c = DeviceClient("10.0.0.1", "u", "p", audit=True, audit_level="all")
ok("audit=True turns it on", c.audit_enabled)
check("audit_level is honoured", c.audit_level, "all")

os.environ["X3_AUDIT"] = "1"
ok("$X3_AUDIT turns it on", DeviceClient("10.0.0.1", "u", "p").audit_enabled)
ok("and an explicit audit=False still wins",
   not DeviceClient("10.0.0.1", "u", "p", audit=False).audit_enabled)
os.environ["X3_AUDIT"] = "off"
ok("'off' means off", not DeviceClient("10.0.0.1", "u", "p").audit_enabled)
os.environ.pop("X3_AUDIT")

os.environ["X3_AUDIT_LEVEL"] = "changes"
check("$X3_AUDIT_LEVEL is read",
      DeviceClient("10.0.0.1", "u", "p").audit_level, "changes")
os.environ["X3_AUDIT_LEVEL"] = "nonsense"
check("an unknown level falls back to config",
      DeviceClient("10.0.0.1", "u", "p").audit_level, "config")
os.environ.pop("X3_AUDIT_LEVEL")


# DeviceClient must stamp audit_poll from the same `quiet` it already uses for
# the transcript, or nothing downstream can tell a poll from a config read.
class FakeSession:
    def __init__(self):
        self.sent: list[m.Message] = []
        self._rid = 0

    def next_request_id(self) -> int:
        self._rid += 1
        return self._rid

    def request(self, msg: m.Message) -> m.Message:
        self.sent.append(msg)
        return m.Message()

    def send(self, msg: m.Message) -> None:
        self.sent.append(msg)


c = DeviceClient("10.0.0.1", "u", "p")
c.session = FakeSession()
c._request((14, 1), CMD_GETALL)                 # noqa: SLF001
c._request((14, 1), CMD_GETOBJ, quiet=True)     # noqa: SLF001
check("quiet=False leaves the frame unmarked",
      getattr(c.session.sent[0], "audit_poll", None), False)
check("quiet=True marks it as polling",
      getattr(c.session.sent[1], "audit_poll", None), True)

# --- level validation ----------------------------------------------------

check("a bogus level is coerced, not raised",
      audit.AuditPolicy(level="wat").level, "config")
check("command names are resolved", audit.command_name(CMD_SETOBJ), "SETOBJ")
check("and an unknown one still prints", audit.command_name(0x1234), "0x1234")

# --- header structure is exactly as documented --------------------------

r = recorder()
r.note("x")
r.close()
raw = open(r.path, "rb").read()
check("the magic is 8 bytes", raw[:8], audit.MAGIC)
hdr_len = struct.unpack("<I", raw[8:12])[0]
ok("the header length points at the first record",
   raw[12 + hdr_len] == audit.REC_NOTE)
rtype, flags, ts_us, n = struct.unpack("<BBQI", raw[12 + hdr_len:12 + hdr_len + 14])
check("record header is type, flags, u64 µs, u32 len", (rtype, flags, n),
      (audit.REC_NOTE, 0, 1))
ok("the timestamp is absolute epoch microseconds", ts_us > 1_600_000_000_000_000)

# --- the transcript is the other file this process writes ----------------
# device._log_fields feeds the per-session transcript, which masks secrets by
# code. An interface declares its secrets on the *subtype* -- a tunnel's IPsec
# Secret, a WireGuard private key -- and the base record at (20, 0) has a field
# for neither, so both were written to disk in the clear. Resolving the row's
# own type is what makes them findable, and it has to be the row's: 0x3e9 is
# the private key on a WireGuard interface and the MAC address on an Ethernet.

if os.path.isdir("7.21.1-151146055"):
    from x3.proto import schema as sch

    IFACE = (20, 0)
    WIREGUARD, IP_TUNNEL, ETHERNET = 70, 21, 1

    c = DeviceClient("10.0.0.1", "u", "p")
    c.schema = sch.Schema.load("7.21.1-151146055")

    def logged(type_value, fields):
        row = m.Message()
        row.add_u32(0x010001, type_value)        # type — picks the overlay
        row.add_string(0x010006, "iface1")
        for code, value in fields:
            if isinstance(value, bool):
                row.add_bool(code, value)
            elif isinstance(value, str):
                row.add_string(code, value)
            else:
                row.add_raw(code, value)
        return dict(c._log_fields(row, IFACE))  # noqa: SLF001

    tunnel = logged(IP_TUNNEL, [(0x7DA, True), (0x7DB, "hunter2")])
    check("a tunnel's IPsec Secret is not written to the transcript",
          tunnel.get("0x7db"), "<hidden>")
    check("...and the rest of the row still reads", tunnel.get("Name"),
          b"iface1")

    wg = logged(WIREGUARD, [(0x3E9, "cGVla2Fib28=")])
    check("nor is a WireGuard private key",
          wg.get("Private Key"), "<hidden>")

    mac = bytes.fromhex("48a98a1122cc")
    eth = logged(ETHERNET, [(0x3E9, mac)])
    check("...while the same code on an Ethernet is its MAC, and readable",
          eth.get("MAC Address"), mac)

    # A write is where a *new* password appears, and a SETOBJ carries only the
    # fields being written -- no type slot, so the subtype cannot be resolved
    # at all. The family's secret codes are the fallback there.
    write = m.Message()
    write.add_u32(0xFE0001, 3)
    write.add_bool(0x7DA, True)
    write.add_string(0x7DB, "hunter2")
    write.add_u32(0x010064, 1400)
    sent = dict(c._log_fields(write, IFACE))  # noqa: SLF001
    check("a write with no type in it still masks the secret",
          sent.get("0x7db"), "<hidden>")
    check("...and leaves the fields that are not secrets anywhere alone",
          sent.get("MTU"), 1400)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all audit tests passed")
