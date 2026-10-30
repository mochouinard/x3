"""Audit mode: an exact, replayable record of one connection's wire traffic.

The session transcript in :mod:`x3.proto.session_log` is for a human reading
back what changed. This is the other half: every M2 frame we sent and every one
we received, verbatim, in a binary container that a tool -- a diff, a replay
app, a decoder -- can read years from now.

Two rules shaped the format.

*Nothing decodes a record it does not understand.* Every record carries its own
type, flags and length, so a reader written today skips a record type added
tomorrow instead of losing the rest of the file. That is why record types are
grouped with gaps (wire, session, visual, ...) rather than numbered densely:
recording UI events for a replay app means adding a type, not a version bump.

*Dropping a frame must never drop information.* Audit mode exists to answer
"what did we actually do to this device", so the only frames it leaves out are
ones whose content it can prove are already in the file: periodic polling the
caller marked as such, and re-reads whose reply is byte-identical to the last
one. Both are counted, and the counts are written into the file.

How much of that applies is one ordered knob, :data:`LEVELS` -- ``changes``,
``config`` (default), ``stats``, ``all`` -- each level a superset of the one
before. Keeping the statistics polling is therefore a step up the ladder, not a
second switch crossed with the first; there is no combination to get wrong.


File layout
-----------

::

    magic      8       b"WBAUDIT\\x01"   (last byte = container version)
    hdr_len    u32 LE
    hdr        hdr_len bytes of UTF-8 JSON -- session metadata

    records    repeated until EOF:
        type   u8      record type (see below)
        flags  u8      bit flags (see below)
        ts_us  u64 LE   epoch microseconds
        len    u32 LE   stored payload length
        payload  len bytes

Absolute epoch microseconds, not a delta: files stay mergeable and sortable,
and a truncated write cannot skew every timestamp after it.

Record types
------------

============  ====  ==========================================================
``SENT``      0x01  raw M2 frame written to the device
``RECV``      0x02  raw M2 frame read from the device
``NOTE``      0x10  UTF-8 text -- connect, edit mode, safe mode, disconnect
``SUPPRESSED`` 0x11 JSON roll-up of frames deliberately not recorded
``UI``        0x20  reserved: visual/interaction events for a replay app
``END``       0x7F  JSON footer -- totals, close reason
============  ====  ==========================================================

Flags
-----

============  ====  ==========================================================
``ZLIB``      0x01  payload is zlib-compressed (large frames only)
``REDACTED``  0x02  a secret was blanked inside the payload
``POLL``      0x04  recorder classified this as periodic polling
``DUP``       0x08  reply identical to the previous one for this request shape
============  ====  ==========================================================

Ordering
--------

Records appear in *flush* order, which is not always chronological: a request
is held until its reply arrives, because deciding whether a poll is worth
keeping needs both halves. ``ts_us`` is always the true time, so a reader that
cares sorts by it.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import message as m

MAGIC = b"WBAUDIT\x01"
VERSION = 1

# --- record types (grouped, with gaps, so new kinds never renumber) ---
REC_SENT = 0x01
REC_RECV = 0x02
REC_NOTE = 0x10
REC_SUPPRESSED = 0x11
REC_UI = 0x20
REC_END = 0x7F

REC_NAME = {
    REC_SENT: "SENT",
    REC_RECV: "RECV",
    REC_NOTE: "NOTE",
    REC_SUPPRESSED: "SUPPRESSED",
    REC_UI: "UI",
    REC_END: "END",
}

# --- flags ---
F_ZLIB = 0x01
F_REDACTED = 0x02
F_POLL = 0x04
F_DUP = 0x08

_REC_HDR = struct.Struct("<BBQI")

#: Compress a payload only when it is big enough to pay for the header. Below
#: this, zlib usually grows a frame that is mostly 4-byte markers.
ZLIB_MIN = 512

#: Cap on the two correlation maps -- requests awaiting a reply, and the last
#: reply seen per request shape. A session left open for days must not grow a
#: map per request that timed out.
_MAX_TRACKED = 4096

#: The only fields that vary between two otherwise identical calls, so the only
#: ones the content digest ignores. SYS_TO and SYS_FROM stay in: the handler
#: path is part of what a request *is*, and leaving it out let a GETALL on
#: [14,1] and one on [20,1] hash alike whenever their payloads matched.
_PER_CALL = frozenset((m.SYS_REQUEST_ID, m.SYS_REPLY_EXPECTED))

#: Commands that change the device. Their frames are never suppressed, whatever
#: the level -- an audit that drops a SET is worthless.
WRITE_COMMANDS = frozenset((
    0xFE0003,  # SETOBJ
    0xFE0005,  # ADDOBJ
    0xFE0006,  # REMOVEOBJ
    0xFE0007,  # MOVEOBJ
    0xFE000E,  # SET
))

#: How much to keep, least to most. Each level is a superset of the one before
#: it, so there is exactly one knob and no combination can contradict itself --
#: which is why statistics polling is a step on this ladder rather than a second
#: switch crossed with it.
LEVELS = ("changes", "config", "stats", "all")

#: What a picker shows for each level. Each one names everything it keeps, so
#: the list reads as the ladder it is -- "config" and "stats" read as two
#: separate things to choose between when they were shown by their keys.
LEVEL_LABEL = {
    "changes": "Changes only",
    "config": "Changes + configuration",
    "stats": "Changes + configuration + statistics",
    "all": "Everything",
}

#: One-line description per level, so the CLI help, the GUI picker and the docs
#: cannot drift apart. Phrased as what each step *adds* to the one before.
LEVEL_HELP = {
    "changes": "writes and the errors they caused",
    "config": "also every configuration read",
    "stats": "also the 1 Hz traffic and counter polling",
    "all": "every frame, nothing collapsed",
}

DEFAULT_LEVEL = "config"

#: Handler command codes, for readable output. Same values as the CMD_*
#: constants in device.py, kept here so the offline reader needs nothing else.
COMMAND_NAMES = {
    0xFE0001: "GETPARAM",
    0xFE0002: "GETOBJ",
    0xFE0003: "SETOBJ",
    0xFE0004: "GETALL",
    0xFE0005: "ADDOBJ",
    0xFE0006: "REMOVEOBJ",
    0xFE0007: "MOVEOBJ",
    0xFE000B: "NOTIFY",
    0xFE000D: "GET",
    0xFE000E: "SET",
    0xFE0012: "SUBSCRIBE",
    0xFE0013: "UNSUBSCRIBE",
}


def command_name(cmd: int | None) -> str:
    if cmd is None:
        return "-"
    return COMMAND_NAMES.get(cmd, f"{cmd:#x}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def audit_dir() -> Path:
    """Where audit files land. Shares the transcript directory by default, so
    a connection's ``.log`` and ``.wba`` sit side by side under one basename."""
    env = os.environ.get("X3_AUDIT_DIR")
    if env:
        return Path(env).expanduser()
    from .session_log import log_dir

    return log_dir()


def env_level(default: str = DEFAULT_LEVEL) -> str:
    raw = (os.environ.get("X3_AUDIT_LEVEL") or "").strip().lower()
    return raw if raw in LEVELS else default


def env_enabled() -> bool:
    """``$X3_AUDIT`` as a boolean. Absent or 0/no/off/false = off."""
    raw = os.environ.get("X3_AUDIT")
    if raw is None:
        return bool(os.environ.get("X3_AUDIT_DIR"))
    return raw.strip().lower() not in ("", "0", "no", "off", "false")


@dataclass
class AuditPolicy:
    """What to keep. ``level`` is the knob a user actually turns.

    ``changes``  writes, errors and notes only -- the smallest useful file.
    ``config``   default. Drops polling and unchanged re-reads.
    ``stats``    keeps the 1 Hz statistics polling too, so a traffic graph or a
                 counter series is in the file. Identical consecutive replies
                 still collapse, which costs nothing: a live counter changes
                 every second, and a poll of something static says nothing new.
    ``all``      every frame, nothing collapsed. Protocol work.
    """

    level: str = DEFAULT_LEVEL
    #: Compress payloads at or above this size. 0 disables compression.
    zlib_min: int = ZLIB_MIN
    #: Byte strings replaced with ``*`` of equal length before a frame is
    #: written. Length-preserving, so the record still decodes as valid M2.
    secrets: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            self.level = DEFAULT_LEVEL

    @property
    def drop_polls(self) -> bool:
        """The 1 Hz refreshes the caller marked as polling."""
        return self.level in ("changes", "config")

    @property
    def drop_unchanged(self) -> bool:
        """Replies byte-identical to the last one for the same request."""
        return self.level != "all"

    @property
    def writes_only(self) -> bool:
        return self.level == "changes"


# ---------------------------------------------------------------------------
# Frame inspection helpers
# ---------------------------------------------------------------------------


def _path(msg: Any, code: int) -> tuple[int, ...]:
    try:
        hit = msg.find(code)
    except Exception:  # noqa: BLE001
        return ()
    if hit is None:
        return ()
    v = hit[1]
    if isinstance(v, (list, tuple)):
        return tuple(int(x) for x in v)
    if isinstance(v, int):
        return (int(v),)
    return ()


def _rid(msg: Any) -> int | None:
    try:
        hit = msg.find(m.SYS_REQUEST_ID)
    except Exception:  # noqa: BLE001
        return None
    if hit is None:
        return None
    return int(hit[1]) if isinstance(hit[1], int) else None


def content_digest(msg: Any) -> bytes:
    """Hash a frame, ignoring the fields that differ on every call.

    Used two ways: as the identity of a request *shape* (so repeated polls of
    the same object collapse onto one key) and as the identity of a reply's
    *content* (so a reply that says exactly what the last one said can be
    dropped without losing anything). A request id must not enter it for either
    use, or nothing would ever match; everything else must, or unrelated frames
    would. See :data:`_PER_CALL`.
    """
    h = hashlib.blake2b(digest_size=16)
    try:
        _feed(h, msg)
    except Exception:  # noqa: BLE001
        # An undigestible frame gets a unique digest rather than a shared one,
        # so it can never collide into being treated as a duplicate.
        return os.urandom(16)
    return h.digest()


def _feed(h: Any, msg: Any) -> None:
    for code, tag, value in msg:
        if code in _PER_CALL:
            continue
        h.update(struct.pack("<IB", code & 0xFFFFFFFF, tag & 0xFF))
        _feed_value(h, value)


def _feed_value(h: Any, value: Any) -> None:
    if isinstance(value, bool):
        h.update(b"\x01" if value else b"\x00")
    elif isinstance(value, int):
        h.update(struct.pack("<q", value if -(2**63) <= value < 2**63 else 0))
    elif isinstance(value, (bytes, bytearray)):
        h.update(struct.pack("<I", len(value)))
        h.update(bytes(value))
    elif isinstance(value, (list, tuple)):
        h.update(struct.pack("<I", len(value)))
        for item in value:
            _feed_value(h, item)
    elif hasattr(value, "__iter__"):  # nested Message
        h.update(b"{")
        _feed(h, value)
        h.update(b"}")
    else:
        h.update(repr(value).encode("utf-8", "replace"))


def redact(raw: bytes, secrets: tuple[bytes, ...]) -> tuple[bytes, bool]:
    """Blank each secret with ``*`` of the same length.

    Length-preserving on purpose: every length prefix in the frame stays
    correct, so the record is still a decodable M2 message. Real Winbox sends
    the account password in the terminal-open frame even on an authenticated
    session, so this is not a hypothetical.

    Secrets under three bytes are left alone. A one- or two-byte sequence turns
    up all over ordinary binary content -- object ids, counters, field markers
    -- and substituting it would corrupt frames wholesale to hide a password
    that short. Note the limit rather than pretend it does not exist.
    """
    if not secrets:
        return raw, False
    out = raw
    hit = False
    for s in secrets:
        if len(s) < 3 or s not in out:
            continue
        out = out.replace(s, b"*" * len(s))
        hit = True
    return out, hit


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class AuditRecorder:
    """Append-only audit file for one connection.

    Thread-safe: ``sent`` is called from whichever thread issued a request and
    ``received`` from the frame reader, so both take the same lock.
    """

    def __init__(
        self,
        host: str,
        user: str,
        *,
        port: int = 8291,
        policy: AuditPolicy | None = None,
        path: str | os.PathLike | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.policy = policy or AuditPolicy()
        self.started = time.time()
        self.path: Path | None = None
        self._fh = None
        self._closed = False
        self._failed = False
        self._lock = threading.Lock()

        # Counters, mirrored into the END footer.
        self.sent_frames = 0
        self.recv_frames = 0
        self.bytes_written = 0
        self.suppressed_polls = 0
        self.suppressed_unchanged = 0
        self.suppressed_reads = 0

        #: (from_path, rid) -> held SENT record awaiting its reply. Deciding
        #: whether a poll is worth keeping needs the reply, so the request
        #: waits here rather than going straight to disk.
        self._pending: dict[tuple, tuple] = {}
        #: request shape digest -> digest of the last reply seen for it.
        self._last_reply: dict[bytes, bytes] = {}
        #: (from_path, rid) -> request shape digest, for the reply side.
        self._shape: dict[tuple, bytes] = {}
        self._extra = dict(extra or {})

        self.path = self._name(path)

    # --- lifecycle ----------------------------------------------------

    def _name(self, path: Any) -> Path | None:
        if path is not None:
            return Path(path).expanduser()
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in self.host)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
        try:
            return audit_dir() / f"{stamp}-{safe}.wba"
        except Exception:  # noqa: BLE001
            return None

    def _open(self) -> bool:
        """Create the file and write the container header, once.

        Deferred to the first record on purpose: a connection that fails to
        authenticate, or one that never says anything, should not leave a
        header-only file behind for someone to puzzle over later.
        """
        if self._fh is not None:
            return True
        # Deliberately not gated on _closed: close() itself may need to create
        # the file, to report frames it suppressed. Post-close writes are
        # already refused by the `active` check every entry point makes.
        if self._failed or self.path is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "wb")
            header = {
                "container": "wbaudit",
                "version": VERSION,
                "host": self.host,
                "port": self.port,
                "user": self.user,
                "started": self.started,
                "started_local": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.started)
                ),
                "level": self.policy.level,
                "pid": os.getpid(),
            }
            header.update(self._extra)
            blob = json.dumps(header, default=str).encode("utf-8")
            self._fh.write(MAGIC + struct.pack("<I", len(blob)) + blob)
            self._fh.flush()
            return True
        except OSError:
            self._failed = True
            self._fh = None
            return False

    @property
    def active(self) -> bool:
        """Whether records are still being accepted: false once the file could
        not be created, and once close() has run."""
        return not (self._failed or self._closed)

    def close(self, reason: str = "") -> None:
        with self._lock:
            if self._closed:
                return
            # Held requests first, and before the closed flag goes up: a
            # request whose reply never arrived is the only trace of it, and
            # it may be the only record in the file.
            for key in list(self._pending):
                self._flush_pending(key)
            self._closed = True
            if self._fh is None:
                if not (self.sent_frames or self.recv_frames):
                    # The session never said anything. No file, rather than a
                    # header-only one for someone to puzzle over later.
                    return
                # It did talk, and every frame was suppressed. The file still
                # has to exist: "audit ran and dropped 1843 polls" and "audit
                # never ran" are different answers, and only the file tells
                # them apart.
                if not self._open():
                    return
            self._write(
                REC_END,
                0,
                json.dumps({
                    "ended": time.time(),
                    "reason": reason,
                    "sent_frames": self.sent_frames,
                    "recv_frames": self.recv_frames,
                    "suppressed_polls": self.suppressed_polls,
                    "suppressed_unchanged": self.suppressed_unchanged,
                    "suppressed_reads": self.suppressed_reads,
                }).encode("utf-8"),
            )
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def __enter__(self) -> "AuditRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(reason=f"{exc_type.__name__}: {exc}" if exc_type else "")

    # --- raw record write ---------------------------------------------

    def _write(self, rtype: int, flags: int, payload: bytes, when: float | None = None) -> None:
        """Caller holds the lock."""
        if self._fh is None and not self._open():
            return
        if self.policy.zlib_min and len(payload) >= self.policy.zlib_min:
            packed = zlib.compress(payload, 6)
            if len(packed) < len(payload):
                payload = packed
                flags |= F_ZLIB
        ts = int((time.time() if when is None else when) * 1_000_000)
        try:
            self._fh.write(_REC_HDR.pack(rtype, flags, ts, len(payload)) + payload)
            self._fh.flush()
            self.bytes_written += _REC_HDR.size + len(payload)
        except OSError:
            self._fh = None

    # --- wire recording -----------------------------------------------

    def sent(
        self,
        raw: bytes,
        msg: Any = None,
        *,
        poll: bool = False,
        when: float | None = None,
    ) -> None:
        """Record an outgoing frame.

        ``poll`` is the caller saying "this is the 1 Hz refresh, not a config
        read". It is an explicit signal rather than a guess because the caller
        is the only one that knows; see ``DeviceClient._request(quiet=...)``.

        ``when`` lets the caller stamp the frame at the moment it went on the
        wire rather than when this call runs. Several threads share one socket,
        so a sender descheduled between its write and this call would otherwise
        record a timestamp behind a frame that left after it.
        """
        if not self.active:
            return
        when = time.time() if when is None else when
        flags = F_POLL if poll else 0
        with self._lock:
            self.sent_frames += 1
            if msg is None:
                payload, hit = self._prep(raw)
                self._write(
                    REC_SENT, flags | (F_REDACTED if hit else 0), payload, when
                )
                return
            cmd = _cmd(msg)
            rid = _rid(msg)
            if self.policy.writes_only and cmd not in WRITE_COMMANDS:
                self.suppressed_reads += 1
                # Still track the shape so the matching reply is dropped too.
                if rid is not None:
                    self._shape[(_path(msg, m.SYS_FROM), rid)] = b""
                return
            if rid is None:
                # Nothing will ever pair with it (subscribe, terminal data):
                # write it now.
                payload, hit = self._prep(raw)
                self._write(REC_SENT, flags | (F_REDACTED if hit else 0), payload, when)
                return
            key = (_path(msg, m.SYS_FROM), rid)
            shape = content_digest(msg)
            self._shape[key] = shape
            self._pending[key] = (when, flags, raw, shape, cmd)
            if len(self._pending) > _MAX_TRACKED:
                # Requests whose reply never came -- a timeout, or firmware
                # that answers on a path we cannot match. Flush the oldest
                # rather than hold them forever: an unanswered request is
                # itself worth recording.
                for old_key in list(self._pending)[: len(self._pending) // 2]:
                    self._flush_pending(old_key)

    def received(self, raw: bytes, msg: Any = None) -> None:
        """Record an incoming frame, and settle the request it answers."""
        if not self.active:
            return
        when = time.time()
        with self._lock:
            self.recv_frames += 1
            if msg is None:
                payload, hit = self._prep(raw)
                self._write(
                    REC_RECV, F_REDACTED if hit else 0, payload, when
                )
                return
            rid = _rid(msg)
            key = (_path(msg, m.SYS_TO), rid) if rid is not None else None
            held = self._pending.pop(key, None) if key is not None else None
            shape = self._shape.pop(key, None) if key is not None else None

            if self.policy.writes_only and shape == b"":
                # Its request was dropped as a read; drop the reply with it.
                self.suppressed_reads += 1
                return

            errored = _has_error(msg)
            is_write = held is not None and held[4] in WRITE_COMMANDS
            poll = bool(held is not None and held[1] & F_POLL)
            digest = content_digest(msg)
            comparable = shape is not None and not errored and not is_write
            dup = comparable and self._last_reply.get(shape) == digest

            if not errored and not is_write:
                if poll and self.policy.drop_polls:
                    self.suppressed_polls += 1
                    return
                if dup and self.policy.drop_unchanged:
                    self.suppressed_unchanged += 1
                    return

            if comparable:
                # Recorded only now that the reply is definitely being written.
                # Seeding this from a dropped frame would let a later read be
                # dismissed as a repeat of something the file never contained,
                # which is precisely the information loss the rule forbids: a
                # pane whose first fetch is a quiet auto-refresh would lose its
                # config to the audit entirely.
                self._last_reply[shape] = digest
                if len(self._last_reply) > _MAX_TRACKED:
                    # An unbounded map on a session that runs for days. Evicting
                    # only ever costs a missed collapse, never a wrong drop.
                    for old_key in list(self._last_reply)[: len(self._last_reply) // 2]:
                        del self._last_reply[old_key]

            if held is not None:
                h_when, h_flags, h_raw, _shape, _cmd = held
                payload, hit = self._prep(h_raw)
                self._write(
                    REC_SENT, h_flags | (F_REDACTED if hit else 0), payload, h_when
                )
            flags = (F_POLL if poll else 0) | (F_DUP if dup else 0)
            payload, hit = self._prep(raw)
            self._write(REC_RECV, flags | (F_REDACTED if hit else 0), payload, when)

    def _flush_pending(self, key: tuple) -> None:
        """Write out a request whose reply never came. Caller holds the lock."""
        held = self._pending.pop(key, None)
        self._shape.pop(key, None)
        if held is None:
            return
        h_when, h_flags, h_raw, _s, _c = held
        if h_flags & F_POLL and self.policy.drop_polls:
            self.suppressed_polls += 1
            return
        payload, hit = self._prep(h_raw)
        self._write(REC_SENT, h_flags | (F_REDACTED if hit else 0), payload, h_when)

    def _prep(self, raw: bytes) -> tuple[bytes, bool]:
        return redact(raw, self.policy.secrets)

    # --- session-level records ----------------------------------------

    def note(self, text: str) -> None:
        if not self.active:
            return
        when = time.time()
        with self._lock:
            self._write(REC_NOTE, 0, text.encode("utf-8", "replace"), when)

    def ui(self, event: dict[str, Any]) -> None:
        """Record a UI/interaction event.

        Reserved for a replay app: the container was built so this could be
        added without disturbing anything that reads the wire records.
        """
        if not self.active:
            return
        when = time.time()
        with self._lock:
            self._write(
                REC_UI, 0, json.dumps(event, default=str).encode("utf-8"), when
            )

    def checkpoint(self) -> None:
        """Write the running suppression counts into the file.

        Without this a reader cannot tell a quiet session from a heavily
        filtered one; ``close()`` writes the same numbers, but a session that
        dies with the process would take them with it.
        """
        if not self.active:
            return
        when = time.time()
        with self._lock:
            self._write(
                REC_SUPPRESSED,
                0,
                json.dumps({
                    "polls": self.suppressed_polls,
                    "unchanged": self.suppressed_unchanged,
                    "reads": self.suppressed_reads,
                }).encode("utf-8"),
                when,
            )

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "level": self.policy.level,
            "sent_frames": self.sent_frames,
            "recv_frames": self.recv_frames,
            "bytes_written": self.bytes_written,
            "suppressed_polls": self.suppressed_polls,
            "suppressed_unchanged": self.suppressed_unchanged,
            "suppressed_reads": self.suppressed_reads,
        }


def _cmd(msg: Any) -> int | None:
    try:
        return msg.get_u32(m.SYS_COMMAND)
    except Exception:  # noqa: BLE001
        return None


def _has_error(msg: Any) -> bool:
    try:
        return bool(msg.has_error())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


@dataclass
class AuditRecord:
    type: int
    flags: int
    ts: float          # epoch seconds
    payload: bytes     # already decompressed

    @property
    def name(self) -> str:
        return REC_NAME.get(self.type, f"type-{self.type:#02x}")

    @property
    def is_wire(self) -> bool:
        return self.type in (REC_SENT, REC_RECV)

    def message(self) -> Any:
        """Decode the payload as M2, or None if it is not a wire record."""
        if not self.is_wire:
            return None
        return m.Message.from_bytes(self.payload)

    def text(self) -> str:
        return self.payload.decode("utf-8", "replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except ValueError:
            return None


class AuditFile:
    """Reader for a ``.wba`` audit file.

    Unknown record types come back as :class:`AuditRecord` with their raw
    payload rather than raising -- that is the whole point of the length field.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        with open(self.path, "rb") as fh:
            magic = fh.read(len(MAGIC))
            if magic[:7] != MAGIC[:7]:
                raise ValueError(f"{self.path}: not a wbaudit file")
            self.version = magic[7]
            (hdr_len,) = struct.unpack("<I", fh.read(4))
            self.header = json.loads(fh.read(hdr_len).decode("utf-8"))
            self._body_at = len(MAGIC) + 4 + hdr_len

    def __iter__(self) -> Iterator[AuditRecord]:
        with open(self.path, "rb") as fh:
            fh.seek(self._body_at)
            while True:
                head = fh.read(_REC_HDR.size)
                if not head:
                    return
                if len(head) < _REC_HDR.size:
                    raise ValueError(
                        f"{self.path}: truncated record header ({len(head)} bytes)"
                    )
                rtype, flags, ts, n = _REC_HDR.unpack(head)
                body = fh.read(n)
                if len(body) < n:
                    raise ValueError(
                        f"{self.path}: truncated record body "
                        f"(want {n}, got {len(body)})"
                    )
                if flags & F_ZLIB:
                    body = zlib.decompress(body)
                yield AuditRecord(rtype, flags, ts / 1_000_000, body)

    def records(self) -> list[AuditRecord]:
        return list(self)
