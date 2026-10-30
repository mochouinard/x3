"""High-level device client: auth + schema + reads + the write path.

Reads are GET / GETALL / GETOBJ / SUBSCRIBE; writes are ADD / SET / REMOVE /
MOVE, and Safe Mode wraps them so a session that drops takes its changes with
it. Every write refuses to run while ``readonly`` is set, which is how the UI
keeps Queue and Read-only modes off the wire.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import configexport as cexp
from . import message as m
from . import schema as sch
from .client_ident import (
    CLIENT_IDENT_DEFAULT,
    CLIENT_IDENT_FIELD,
    resolve_client_ident,
)
from .modern import (
    DEFAULT_FROM,
    ModernAuthError,
    ModernProtocolError,
    ModernSession,
)
from .session import WinboxError

#: Announces each candidate path in a command_line_for console line.
_CLI_MARK = "==WBCLI"

# GETALL reply: array of nested messages
STD_OBJS = 0xFE0002
STD_OBJ_COUNT = 0xFE0019
QUERY_CURSOR = 0xFE0003  # rows already delivered: a query reply carries it, the next poll echoes it
STD_FILTER = 0xFE000C  # field/query mask on GETALL / GETOBJ / SUBSCRIBE
STD_OBJ_ID = 0xFE0001  # object .id
STD_DESCR = 0xFE0009  # object comment / description (Talos STD_DESCR)
#: Read-only status lines the device attaches to an object, as a string
#: array. Not in any .jg -- Winbox hard-codes it, and draws each line in
#: red above the row where a comment would go. A CAPsMAN-managed wireless
#: interface on (20,0) carries two: "managed by CAPsMAN" and
#: "channel: 5180/20-Ceee/ac(27dBm), SSID: ..., local forwarding".
STD_INFO = 0xFE001C
#: "This run has finished" on a progress poll, as a bool. Shares its number
#: with CMD_NOTIFY -- position is what tells them apart, as with ADD/SETOBJ.
#: Seen going True with Created=100 on the last Make Supout.rif poll.
QUERY_DONE = 0xFE000B

# Command codes (Cisco-Talos dissector)
CMD_GETALL = 0xFE0004
CMD_GETOBJ = 0xFE0002
CMD_GET = 0xFE000D
CMD_SUBSCRIBE = 0xFE0012
CMD_UNSUBSCRIBE = 0xFE0013
CMD_NOTIFY = 0xFE000B
#: Field write. Captured from real Winbox changing the update Channel: sent
#: fire-and-forget (no request id, no reply_expected) carrying only the field
#: that changed, which is what the schema's ``type:autoset`` wrapper means.
CMD_SET = 0xFE000E
# Error: handler does not implement this command
ERR_NOTIMP = 0xFE0003

# The mutation verbs, captured 2026-08-06 against firmware 7.21.1 on [20,1] IP
# Address, [20,3] Firewall Rule and [72] Files.
CMD_SETOBJ = 0xFE0003     # edit an existing object, keyed on its .id
CMD_ADDOBJ = 0xFE0005     # create; the reply carries the new .id in 0xFE0001
CMD_REMOVEOBJ = 0xFE0006  # delete by .id
CMD_MOVEOBJ = 0xFE0007    # reorder within an ordered table
#: Inside a MOVE this is the anchor ("insert before this .id"), not a command.
#: The number is shared with CMD_ADDOBJ; position is what tells them apart.
MOVE_ANCHOR = 0xFE0005
#: 0xFE000A, which every captured ADD and SET carries as False. The schema
#: knows this code as the standard "disabled" flag, so it is only ever sent
#: when asked for: injecting False into a SET would quietly re-enable a
#: disabled object that the edit never mentioned.
STD_DISABLED = 0xFE000A

# Common name field codes used by list objects (nameval / sfe0010 / s1)
_NAME_CODES = (0xFE0010, 0x01, 0x02, 0x03)

# Field masks observed from real Winbox on Interface (20,0)
#: /system package update. Its state field 0x5 is 0 = current/idle,
#: 1 = a newer version is on offer, 2 = downloading or installing.
PKG_UPDATE_PATH = (24, 24)
PKG_UPDATE_STATE = 0x5

#: The two fields Winbox adds to a list SUBSCRIBE to make it a data feed. Every
#: list subscription in the 2026-08-05 capture carries both: 0xFF0017 as a bare
#: True, and 0xFF0012 as the milliseconds the device should leave between
#: pushes. The flag's own name is a guess -- what is not a guess is that the
#: interval only appears next to it (REQ#11, #14, #16, #21 ...).
SUB_PUSH_DATA = 0xFF0017
SUB_INTERVAL_MS = 0xFF0012

FILTER_IFACE_STATIC = 0x20000000   # list identity columns
FILTER_IFACE_TRAFFIC = 0x4000A     # rates + counters (Traffic tab)
FILTER_IFACE_TRAFFIC_FULL = 0x4000F


def list_read_mask(menu: "sch.Menu | None") -> int | None:
    """The 0xFE000C a full list read sends, or None to send none at all.

    A handler does not volunteer its counters. Winbox opening the Interface
    list sends GETALL with 0x4000F -- ``STD_MASK_BASE | refreshfilter``, the
    menu's own two numbers -- and only then does the reply carry Tx, Rx and
    the packet rates; the same mask is 0x7 on every firewall chain, which is
    what fills Bytes and Packets. Without it those columns come back empty,
    which is exactly what they looked like here (capture REQ#12, 2026-08-05).

    Only for the 41 menus that declare a ``refreshfilter``. The rest would
    send a bare 5, which asks for nothing extra -- and a mask on a read that
    works today is a way to lose fields, not gain them.
    """
    if menu is None or not getattr(menu, "refresh_filter", None):
        return None
    return menu.write_mask


class DeviceError(Exception):
    pass


class QueryPoll(list):
    """The rows one poll returned, plus what the reply said about the run.

    A list so callers that only want rows keep working.
    """

    cursor: int | None = None   # 0xfe0003: echo it on the next poll
    incremental: bool = False   # rows are new-since-last-poll, not a snapshot
    done: bool = False          # 0xfe000b on the frame: the run is over


@dataclass
class RowObject:
    """One GETALL/GET object, decoded against the schema when available."""

    path: tuple[int, ...]
    raw: m.Message
    # Decoded rows: may include multiple entries for the same code (u12 vs U12)
    fields: list[tuple[int, str, str, bool]] = field(default_factory=list)
    form_fields: list[tuple[str, int, str, str, bool]] = field(default_factory=list)
    values_by_code: dict[int, Any] = field(default_factory=dict)
    # code → [(tag, value), ...] preserving multi-declaration replies
    values_by_code_multi: dict[int, list[tuple[int, Any]]] = field(default_factory=dict)

    def get_rendered(self, code: int, *, prefer_name: str | None = None) -> str:
        """Rendered string for a field code.

        When the same code is both an enum and a multi (DHCP Client 0x12),
        ``prefer_name`` selects the right declaration (e.g. 'Add Default Route').
        """
        if prefer_name:
            for c, n, v, _ok in self.fields:
                if c == code and n == prefer_name:
                    return v
        for c, _n, v, _ok in self.fields:
            if c == code:
                return v
        val = self.values_by_code.get(code)
        return "" if val is None else str(val)

    def merge(self, other: "RowObject") -> None:
        """Fold a partial update of this object into it, in place.

        A pushed record carries only the fields its subscription mask selects
        -- an Interface feed is rates and counters, with no name and no type --
        so this updates what the push knows and leaves the rest standing.
        ``raw`` is deliberately not replaced: it is the last *whole* message,
        and identity lookups still read the fields a partial push omits.
        """
        seen = {(code, name): i for i, (code, name, _v, _ok) in enumerate(self.fields)}
        for entry in other.fields:
            key = (entry[0], entry[1])
            index = seen.get(key)
            if index is None:
                seen[key] = len(self.fields)
                self.fields.append(entry)
            else:
                self.fields[index] = entry
        self.values_by_code.update(other.values_by_code)
        self.values_by_code_multi.update(other.values_by_code_multi)

    def retime_ages(self, menu: "sch.Menu", uptime: float) -> bool:
        """Re-render this row's ``type:'age'`` cells against a newer clock.

        An age field is a *stamp* on the device's uptime clock, so the number
        on the wire never moves -- what moves is the clock it is measured
        against. Winbox never re-reads a lease to make Expires After count
        down; it runs its copy of the uptime forward locally and redraws the
        column. This is that redraw: same object, one second later.

        Returns True when any cell's text actually changed, so a caller can
        skip the repaint on the rows where it did not.
        """
        if menu is None or uptime is None:
            return False
        codes = menu.age_codes()
        if not codes:
            return False
        changed = False
        for code in codes:
            base = menu.fields.get(code)
            if base is None:
                continue
            for tag, value in self.values_by_code_multi.get(code) or []:
                fld = base.for_tag(tag)
                # One id can be two fields (DHCP Lease 0x7b is Client ID as a
                # string and Age as a u32); only the age one is a stamp.
                if fld.jg_type != "age":
                    continue
                text = fld.render(value, uptime=uptime)
                name = fld.name or f"<{code:#08x}>"
                for i, (c, n, old, ok) in enumerate(self.fields):
                    if c == code and n == name and old != text:
                        self.fields[i] = (c, n, text, ok)
                        changed = True
        return changed

    def raw_value(self, code: int, *, prefer_scalar: bool = True) -> Any:
        entries = self.values_by_code_multi.get(code) or []
        if not entries:
            return self.values_by_code.get(code)
        if prefer_scalar:
            for tag, val in entries:
                if tag not in (
                    m.T_U32_ARRAY, m.T_U64_ARRAY, m.T_STRING_ARRAY,
                    m.T_MESSAGE_ARRAY, m.T_RAW_ARRAY, m.T_BOOL_ARRAY,
                ):
                    return val
        return entries[0][1]

    def comment(self) -> str:
        """Object comment (STD_DESCR 0xfe0009).

        The schema treats ``type:comment`` as chrome, so a comment never reaches
        ``fields`` -- only the two menus that spell out ``id:'sfe0009'`` do. For
        every other object it is in ``values_by_code`` and nowhere else.
        """
        for _c, n, v, _ok in self.fields:
            if (n or "").lower() in ("comment", "description"):
                return v or ""
        val = self.raw_value(STD_DESCR)
        if isinstance(val, (bytes, bytearray)):
            return bytes(val).decode("utf-8", "replace")
        return "" if val is None else str(val)

    def info_lines(self) -> tuple[str, ...]:
        """Device-supplied status lines (STD_INFO 0xfe001c), in wire order.

        A read-only string array -- no menu declares it, so it never reaches
        ``fields``. Winbox draws these where a comment goes but in red, which is
        how a CAPsMAN-managed wlan says so in the interface list.
        """
        val = self.values_by_code.get(STD_INFO)
        if val is None:
            return ()
        items = val if isinstance(val, (list, tuple)) else [val]
        out: list[str] = []
        for item in items:
            if isinstance(item, (bytes, bytearray)):
                item = bytes(item).decode("utf-8", "replace")
            text = str(item).strip()
            if text:
                out.append(text)
        return tuple(out)

    def set_comment(self, text: str) -> None:
        """Record a locally edited comment so the list can redraw it."""
        self.values_by_code[STD_DESCR] = text
        self.values_by_code_multi[STD_DESCR] = [(m.T_STRING, text)]

    def property_tuples(self) -> tuple[tuple[str, str, str], ...]:
        if self.form_fields:
            return tuple((tab, name, val) for tab, _c, name, val, _ok in self.form_fields)
        return tuple(("General", name, val) for _c, name, val, _ok in self.fields)

    def property_specs(self) -> list[dict[str, Any]]:
        """Rich field specs for editable property forms."""
        out: list[dict[str, Any]] = []
        if self.form_fields:
            for tab, code, name, rendered, _ok in self.form_fields:
                out.append({
                    "tab": tab,
                    "code": code,
                    "name": name,
                    "rendered": rendered,
                    "raw": self.raw_value(code),
                })
        else:
            for code, name, rendered, _ok in self.fields:
                out.append({
                    "tab": "General",
                    "code": code,
                    "name": name,
                    "rendered": rendered,
                    "raw": self.raw_value(code),
                })
        return out

    def object_id(self) -> int | None:
        """Router object `.id` (0xfe0001), used by GETOBJ / live traffic."""
        if self.raw is not None:
            oid = self.raw.get_u32(STD_OBJ_ID)
            if oid is not None:
                return oid
        val = self.values_by_code.get(STD_OBJ_ID)
        return int(val) if isinstance(val, int) else None


@dataclass(frozen=True)
class EnumMark:
    """How one dropdown entry is doing, beside the name it shows.

    A dynamic enum offers rows of another table, and the row carries more than
    its name: whether it is running, whether it is switched off, and whatever
    comment its owner left on it. Winbox shows none of that in a dropdown --
    you pick `ether5` and find out afterwards that the cable is out -- but the
    read that fills the box already carries all of it (see
    :meth:`DeviceClient.enum_marks`), so showing it costs nothing on the wire.

    ``flags`` is the same letter string the list's flags column shows ('RS',
    'X'). ``running`` is None on a table with no such flag: a bridge or an
    address list is neither up nor down, and a picker of those must not draw
    a state it does not have.
    """

    flags: str = ""
    running: bool | None = None
    disabled: bool = False
    comment: str = ""

    @property
    def state(self) -> str:
        """``'up'`` / ``'down'`` / ``'disabled'`` / ``''`` -- what to draw.

        Disabled outranks not-running: an interface that is off is not
        reported as running either, and two marks for one fact reads as two
        problems.
        """
        if self.disabled:
            return "disabled"
        if self.running is None:
            return ""
        return "up" if self.running else "down"

    def __bool__(self) -> bool:
        """False when there is nothing to draw -- no state, no comment."""
        return bool(self.state or self.comment)


@dataclass
class DeviceInfo:
    identity: str = ""
    version: str = ""
    board: str = ""
    architecture: str = ""
    platform: str = ""
    cpu_load: str = ""
    free_memory: str = ""
    total_memory: str = ""
    uptime: str = ""


@dataclass
class DeviceFingerprint:
    """A device's identity, read after connect for TOFU verification and
    Neighbors matching. ``serial`` is the board's hardware serial (empty on
    x86 VMs, where ``system_id`` — the /system/license software id — stands in).
    ``macs`` is the join key against MNDP neighbours."""

    serial: str = ""
    system_id: str = ""
    model: str = ""
    identity: str = ""
    macs: list[str] = field(default_factory=list)
    ips: list[str] = field(default_factory=list)
    salt: str = ""  # stage-2 SRP salt (hex): a per-install fingerprint

    @property
    def key(self) -> str:
        """Stable device key: hardware serial if present, else the software-id."""
        return self.serial or self.system_id


#: Winbox's first exchange: the session handler, which answers with the
#: device's capability bits among other session facts. See system_caps.
SESSION_PATH = (13, 4)
SESSION_COMMAND = 7
SESSION_INFO_FIELD = 0x28
SYSCAP_FIELD = 0x6D

#: "system_caps has not been read yet", which is not the same as "the device
#: did not tell us" (None).
_UNREAD = object()

#: How long a lookup column's second read is reused. ARP and DHCP Leases
#: auto-refresh every few seconds, and re-reading the bridge-port table on
#: every one of those polls would double the traffic of an idle window for a
#: column that changes when a host moves ports. An explicit Refresh clears it
#: outright (see forget_lookups).
LOOKUP_TTL = 30.0


def _field_code_by_name(menu: "sch.Menu | None", name: str) -> int | None:
    """The wire code of a menu's field of this name, or None."""
    if menu is None or not name:
        return None
    for code, fld in (menu.fields or {}).items():
        if (fld.name or "") == name:
            return code
    return None


def _as_list(hit: tuple[int, Any] | None) -> list[Any]:
    """A reply field as a list, whether it arrived as an array or a scalar.

    The router collapses a single-element array to a bare value (see ACCEPTED_TAGS in
    schema.py), so a one-frequency answer arrives as one number.
    """
    if hit is None:
        return []
    value = hit[1]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class DeviceClient:
    """Authenticated modern Winbox session with schema-backed read ops."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        password_bytes: bytes | None = None,
        port: int = 8291,
        timeout: float = 10.0,
        transport: str = "auto",
        schema_dir: str | None = None,
        readonly: bool = True,
        client_ident: str | None = None,
        audit: bool | None = None,
        audit_level: str | None = None,
        audit_path: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        #: The password as the source stored it, when that source can give it
        #: byte-for-byte (the address book does; a typed str does not). The
        #: login hashes this in preference to ``password`` so a secret saved
        #: in a single-byte codepage is not mangled by a UTF-8 round trip --
        #: see ModernSession.authenticate and cdb.CdbEntry.password_bytes.
        self.password_bytes = password_bytes
        self.port = port
        self.timeout = timeout
        self.transport = transport
        self.schema_dir_hint = schema_dir
        self.readonly = readonly
        # Default winbox-3.42 (reliable); override with winbox-4.0 when needed.
        self.client_ident = resolve_client_ident(client_ident)
        self.session: ModernSession | None = None
        self.schema: sch.Schema | None = None
        self.info = DeviceInfo()
        self._plugin_version = ""
        self._plugin_build = 0
        #: The plugin-cache directory :attr:`schema` was parsed from, and the
        #: build id it holds when that is not the build the device advertised.
        #: A mismatch means the menus on screen are another firmware's -- how
        #: Routing ▸ GMP went missing on a 7.21.1 box whose version prefix
        #: matched a stub cache -- so the UI has to be able to say so.
        self.schema_dir: str | None = None
        self.schema_build_mismatch: tuple[int | None, int] | None = None
        #: ``*.jg`` plugin names the device advertises in its file index (e.g.
        #: {"wave2.jg", "roteros.jg", …}); used to hide nav menus for plugins
        #: this device lacks. Empty = unknown, so no filtering is applied.
        self.device_plugins: set[str] = set()
        #: Coarse hardware capabilities derived from the interface list, cached
        #: on first probe (see ``quickset_capabilities``). None = not yet probed.
        self._caps: set[str] | None = None
        self._lock = threading.RLock()
        # enum_path → {object_id: display_name}
        self._enum_cache: dict[tuple[int, ...], dict[int, str]] = {}
        #: enum_path → the source rows the map above was built from. Kept
        #: because a dropdown with a ``pred`` (Bridge Port ▸ Bridge: the
        #: interfaces whose `type` is 12) has to read each row's own fields to
        #: decide it, which the {id: name} map has thrown away.
        self._enum_rows: dict[tuple[int, ...], list[RowObject]] = {}
        #: enum_path → the marks derived from those rows (see enum_marks),
        #: kept because every dropdown on a form asks the same question of the
        #: same table and the answer only changes when the rows do.
        self._enum_marks_cache: dict[tuple[int, ...], dict[int, "EnumMark"]] = {}
        #: enum_path → when its rows were last read, on the monotonic clock.
        #: A dropdown re-reads its source when it opens, and this is what keeps
        #: opening the same box twice in a row from costing two reads.
        self._enum_read_at: dict[tuple[int, ...], float] = {}
        #: (enum_path, pred) → the narrowed {id: name}. One dropdown asks the
        #: same question of every open form, and the answer only changes when
        #: the source table does (see _forget_enums).
        self._enum_pred_cache: dict[tuple, dict[int, str]] = {}
        #: `queryenum` results, keyed by (path, cmd, the parameters sent). The
        #: answer depends on the parameters -- a frequency list is per band and
        #: per country -- so the cache cannot be keyed on the handler alone.
        self._query_enum_cache: dict[tuple, dict[int, str]] = {}
        #: `type:lookup` columns: (path, dst name, val name) -> {dst: val}, as
        #: rendered text on both sides, with the time it was read.
        self._lookup_cache: dict[tuple, dict[str, str]] = {}
        self._lookup_read_at: dict[tuple, float] = {}
        #: Set by table_rows when a `maxobjs` cap dropped rows, for the pane's
        #: status line. None when the last read was complete.
        self._last_truncated: str | None = None
        #: Device capability bits, read once from the session handler.
        self._syscaps: frozenset[int] | None = _UNREAD
        #: Interface picker rows, cached like the enum tables above. Every
        #: optional-match editor asks for these, so a firewall form used to
        #: re-read the interface list once per field.
        self._iface_choices: list[tuple[int, str, str]] | None = None
        # Unique SYS_FROM second component for property windows / subscriptions
        self._from_seq = 200
        # Lazy Safe Mode terminal channel (same TCP session)
        self._safe_mode = None
        self.safe_mode_active: bool = False
        #: The session while it is still being opened. See abort().
        self._connecting = None
        # Open consoles (one per Terminal window), each on its own SYS_FROM
        self._terminals: list = []
        # Per-connection transcript (see x3.proto.session_log)
        self.session_log = None
        # Audit mode: the raw frame record (see x3.proto.audit). Off unless
        # asked for, either here or via $X3_AUDIT -- it is the user's
        # call, not a default, because it writes every config value to disk.
        from . import audit as _audit

        self.audit_enabled = _audit.env_enabled() if audit is None else bool(audit)
        self.audit_level = audit_level or _audit.env_level()
        self.audit_path = audit_path
        self.audit = None
        #: Per-phase connect timings in ms, filled by connect().
        self.connect_ms: dict[str, float] = {}
        # Device uptime in seconds, refreshed by uptime(). type:'age' fields are
        # stamps on this clock, so decoding a row without it leaves them as bare
        # integers -- see schema.format_age_stamp.
        self._uptime: float | None = None
        # Monotonic stamp for the reading above, so it can be run forward
        # locally instead of re-read on every poll. See :meth:`uptime`.
        self._uptime_at: float | None = None

    # --- lifecycle ----------------------------------------------------

    def connect(self, progress=None, *, need_schema: bool = True) -> None:
        """Open the session, load schema, read device info.

        ``progress`` is an optional callable taking one string; it is invoked
        as each phase starts. Schema load and refresh_info can each take
        seconds, so a caller showing a status line wants to name the phase it
        is actually in rather than leave "Authenticating…" up for all of it.

        ``need_schema=False`` skips the ``.jg`` load entirely. The schema is
        what turns wire codes into menus and field names, so anything browsing
        the device needs it -- but a console export needs no menus at all, and
        :meth:`read_fingerprint` addresses its fields by code. Insisting on it
        there would fail a backup on exactly the firmware whose plugin set was
        never cached, which is the one nobody has looked at in a year.
        """

        import time as _time

        # Phase durations, so a caller can say where a slow connect went
        # rather than leaving the user guessing (the UI logs them).
        self.connect_ms = {}
        marks = [("start", _time.perf_counter())]

        def _mark(name: str) -> None:
            now = _time.perf_counter()
            self.connect_ms[name] = 1000.0 * (now - marks[-1][1])
            marks.append((name, now))

        def _say(text: str) -> None:
            if progress is None:
                return
            try:
                progress(text)
            except Exception:  # noqa: BLE001
                pass

        sess = ModernSession(
            self.host,
            self.port,
            self.timeout,
            transport=self.transport,
            client_ident=self.client_ident,
        )
        # Published before the blocking connect, so abort() can reach it. The
        # MAC transport walks every local interface waiting `timeout` on each,
        # and self.session is not set until authentication succeeds -- so a
        # cancel had nothing to close and the worker ran on for a minute.
        self._connecting = sess
        try:
            _say(f"Connecting to {self.host}:{self.port}…")
            sess.connect()
            _say("Authenticating…")
            sess.authenticate(
                self.user,
                self.password_bytes if self.password_bytes is not None
                else self.password,
            )
        except (ModernAuthError, ModernProtocolError, OSError, WinboxError, TimeoutError) as e:
            try:
                sess.close()
            except Exception:
                pass
            raise DeviceError(str(e)) from e
        self.session = sess
        _mark("auth")
        # Start the transcript before schema/info so those show up in it too.
        if self.session_log is None:
            from .session_log import SessionLog

            self.session_log = SessionLog(self.host, self.user)
            self.session_log.note(
                f"connected to {self.host}:{self.port} as {self.user} "
                f"({'read-only' if self.readonly else 'writable'})"
            )
        # Audit before the demux starts, so the schema fetch and the identity
        # reads that follow are in the file too -- they are the baseline every
        # later change is read against.
        self._start_audit()
        # Route frames by (SYS_TO, request id) from here on, so a terminal or a
        # Safe Mode drain can share this connection with menu reads instead of
        # stealing each other's replies -- which is what real Winbox does.
        sess.start_demux(
            on_unrouted=self._on_unrouted, on_reader_error=self._on_reader_error
        )
        # Schema + identity are best-effort: a slow/hanging file fetch or GET
        # must not kill an otherwise good session (and freeze the GUI).
        if need_schema:
            _say("Loading schema…")
            try:
                self._load_schema()
            except Exception as e:
                # Keep session; UI can still open with whatever schema we have.
                if self.schema is None:
                    try:
                        self._load_schema_local_only()
                    except Exception:
                        pass
                if self.schema is None:
                    raise DeviceError(
                        f"connected but schema load failed: {e}. "
                        "Run `x3ctl mirror --host …` or pass schema_dir=."
                    ) from e
        elif self.session_log is not None and self.schema is None:
            self.session_log.note("schema load skipped (not needed by caller)")
        _mark("schema")
        if self.audit is not None and self.schema is not None:
            # Which .jg set names the fields in this file. Without it, reading
            # an audit back months later means guessing at --schema-dir.
            self.audit.note(f"schema {self.schema.version}")
        _say("Reading device info…")
        try:
            self.refresh_info()
        except Exception:
            pass
        _mark("info")
        self._connecting = None

    def _start_audit(self) -> None:
        """Attach an audit recorder to the live session, if audit is on.

        The recorder hangs off ``ModernSession`` rather than off this client so
        it also sees terminal channels and file transfers, which build their own
        frames and never pass through ``_request``.
        """
        if self.audit is not None and self.session is not None:
            # A reconnect: the same recording carries on over the new session.
            self.session.audit = self.audit
            return
        if not self.audit_enabled or self.session is None:
            return
        from . import audit as _audit

        secrets: list[bytes] = []
        if self.password:
            # Real Winbox puts the account password in the terminal-open frame
            # even on an authenticated session, so it does reach the wire.
            secrets.append(self.password.encode("utf-8"))
        try:
            rec = _audit.AuditRecorder(
                self.host,
                self.user,
                port=self.port,
                path=self.audit_path,
                policy=_audit.AuditPolicy(
                    level=self.audit_level, secrets=tuple(secrets)
                ),
                extra={
                    "client_ident": self.client_ident,
                    "transport": "mac" if self.session.via_mac else "tcp",
                    "readonly": self.readonly,
                },
            )
        except Exception as e:  # noqa: BLE001
            # Auditing is an extra, never a reason a connection fails to open.
            if self.session_log is not None:
                self.session_log.note(f"audit could not start: {e}")
            return
        if not rec.active:
            if self.session_log is not None:
                self.session_log.note("audit could not start: log file not writable")
            return
        self.audit = rec
        self.session.audit = rec
        rec.note(
            f"connected to {self.host}:{self.port} as {self.user} "
            f"({'read-only' if self.readonly else 'writable'}), "
            f"audit level {self.audit_level}"
        )
        if self.session_log is not None:
            self.session_log.note(f"audit recording to {rec.path}")

    def _on_reader_error(self, exc: BaseException) -> None:
        """The frame reader died, so this session is over.

        Worth a full traceback: it is almost always a decode failure on a
        frame we mis-parsed, and the stack says where. Without this the thread
        just disappeared and every later request timed out for no stated
        reason.
        """
        import traceback

        log = self.session_log
        if log is None:
            return
        detail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).strip()
        log.note(f"frame reader stopped: {type(exc).__name__}: {exc}\n{detail}")

    def _on_unrouted(self, msg) -> None:
        """Note a frame nothing was waiting for.

        These used to be swallowed silently by whichever blocking read happened
        to pick them up (Safe Mode's drain ate device notifications this way),
        so record them rather than lose them.
        """
        log = self.session_log
        if log is None:
            return
        try:
            to = msg.find(m.SYS_TO)
            frm = msg.find(m.SYS_FROM)
            cmd = msg.get_u32(m.SYS_COMMAND)
            log.note(
                f"unrouted frame to={to[1] if to else None} "
                f"from={frm[1] if frm else None} "
                f"cmd={cmd:#x}" if cmd is not None else "unrouted frame"
            )
        except Exception:  # noqa: BLE001
            pass

    # --- query tools (Ping, Traceroute, Torch) --------------------------
    # Wire shape captured from real Winbox; see the query-tools section of
    # README. Two models: start+poll (Ping) and subscribe+notify (Torch).

    def start_query(
        self,
        menu: sch.Menu,
        values: dict[int, Any] | None = None,
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> int | None:
        """Run a query's startcmd. Returns the per-run session id.

        The session id is what keeps concurrent runs apart -- two Ping windows
        get 1 and 2 and each polls with its own.
        """
        if menu.start_command is None:
            raise DeviceError(f"{menu.title} declares no startcmd")

        def add_params(msg: m.Message) -> None:
            for code, raw in (values or {}).items():
                fld = menu.request_fields.get(code)
                if fld is None:
                    continue
                try:
                    tag, value = sch.encode_form_value(fld, raw)
                    if fld.jg_type == "addr" and not global_start:
                        value = m.Message().add_u32(
                            sch.ADDR_INNER_V4, sch.parse_ipv4(raw)
                        )
                except (ValueError, TypeError) as e:
                    raise DeviceError(f"{fld.name}: {e}") from e
                msg.add_typed(code, tag, value)

        # An addr's byte order follows the verb family. The global start
        # (0xFE000F: Ping, Traceroute) takes it in network order -- a ping to
        # The router's own 192.168.219.1 answers with TTL 255 that way. A tool
        # started with its handler's own verb (Bandwidth Test and Speed Test,
        # cmd 0x2 on [29]) takes it low byte first, like a record menu: Winbox
        # sends 127.0.0.1 as 16777343. Sent the other way round, a bandwidth
        # test to 127.0.0.1 went to 1.0.0.127 and sat at "connecting...".
        global_start = menu.start_command == 0xFE000F

        reply = self._request(
            menu.path, menu.start_command, from_path=from_path, extra=add_params
        )
        if reply.has_error():
            raise DeviceError(reply.error_string() or "query start failed")
        return reply.get_u32(STD_OBJ_ID)

    def poll_query(
        self,
        menu: sch.Menu,
        session_id: int,
        *,
        from_path: tuple[int, ...] | None = None,
        filter_mask: int | None = 5,
        cursor: int | None = None,
    ) -> "QueryPoll":
        """One poll for a running query.

        The device answers a GETALL poll with only the rows *new since the
        last one*, and says how far it has got in 0xfe0003 (``.cursor`` on
        the result). The next poll must echo it back. Captured from real
        Winbox: it sends 0xfe0003=N on poll N+1. Leave it out and the device
        ends the run after the first reply, so the poll after that is
        UNKNOWNID (0xfe0004) -- what Ping showed. The reply to the poll that
        finishes the run (packet count reached) also carries 0xfe000b
        (``.done``); the session is gone after it, so it must be the last.

        *Which* command that is comes from the menu. Ping, Traceroute, Torch
        and IP Scan declare no pollcmd and are read with GETALL -- but a third
        of the .jg's command menus name their own (Make Supout.rif, Cable Test,
        the backup transfers, Bandwidth Test), and their handler answers GETALL
        with NOTIMP. Verified on a 7.24 virtual router: `(24,25)` GETALL -> 0xfe0003, while
        its declared pollcmd 0xfe0010 returns the progress percentage.
        """
        command = menu.poll_command or CMD_GETALL

        def add_session(msg: m.Message) -> None:
            msg.add_u32(STD_OBJ_ID, session_id & 0xFFFFFFFF)
            # STD_FILTER is a GETALL argument. A handler's own poll command
            # takes the session id and nothing else.
            if filter_mask is not None and command == CMD_GETALL:
                msg.add_u32(STD_FILTER, filter_mask & 0xFFFFFFFF)
            if cursor is not None and command == CMD_GETALL:
                msg.add_u32(QUERY_CURSOR, cursor & 0xFFFFFFFF)

        reply = self._request(
            menu.path, command, from_path=from_path,
            extra=add_session, quiet=True,
        )
        if reply.has_error():
            raise DeviceError(reply.error_string() or "query poll failed")
        out = QueryPoll(self._rows_from_reply(reply, menu))
        out.cursor = reply.get_u32(QUERY_CURSOR)
        out.incremental = out.cursor is not None
        out.done = bool(reply.get_bool(QUERY_DONE))
        return out

    def cancel_query(
        self,
        menu: sch.Menu,
        session_id: int | None = None,
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        # Tools ▸ Renew License declares `cancelcmd:0xffffffff`, which is the
        # .jg's way of saying there is nothing to cancel -- not a command to
        # send. Sending it puts a bogus verb on the wire for a run that cannot
        # be stopped anyway.
        if menu.cancel_command is None or menu.cancel_command == 0xFFFFFFFF:
            return

        def add_session(msg: m.Message) -> None:
            if session_id is not None:
                msg.add_u32(STD_OBJ_ID, session_id & 0xFFFFFFFF)

        self._request(
            menu.path, menu.cancel_command, from_path=from_path, extra=add_session
        )

    def _rows_from_reply(self, reply: m.Message, menu: sch.Menu) -> list[RowObject]:
        """Decode the nested-message array a query reply carries.

        Unlike a normal GETALL -- one object per reply frame -- a query answers
        with every current row nested under 0xfe0002.

        A progress-style run (Make Supout.rif, an upgrade, a file transfer) has
        no rows to nest: its poll reply carries the declared result fields on
        the frame itself, plus 0xfe000b once it has finished. That reads as one
        row, so the caller needs no second shape.
        """
        hit = reply.find(0xFE0002)
        if hit is None:
            codes = {code for code, _tag, _value in reply}
            # A declared field, or -- for a run polled with its own command,
            # where a bare 0xfe000b is the whole answer -- the end marker. A
            # GETALL-polled tool is left alone: an empty Ping poll is empty,
            # not a row of nothing.
            if codes & set(menu.fields) or (
                menu.poll_command is not None and QUERY_DONE in codes
            ):
                # Read against the command menu: these handlers have no record
                # menu of their own, so the default lookup finds no field names
                # and "43%" comes out as a bare 43.
                return [self._row_from_msg(menu.path, reply, menu=menu)]
            return []
        payload = hit[1]
        if not isinstance(payload, (list, tuple)):
            payload = [payload]
        out: list[RowObject] = []
        for nested in payload:
            if isinstance(nested, m.Message):
                out.append(self._row_from_msg(menu.path, nested, menu=menu))
        return out

    # --- package update / firmware upgrade (24,24) ----------------------
    # /system package update -- Winbox's "Check For Updates". The .jg declares
    # the whole thing: the four buttons carry their own command numbers and an
    # on: condition keyed on field 0x5 (state), so read them from the schema
    # rather than hardcoding. Surveyed across all 222 cached builds from
    # 6.0rc14 to 7.23.2: the menu is present in every one and the commands are
    # identical (1 download, 2 cancel, 3 install, 4 check). What does move is
    # the Channel field id -- 0xa normally but 0xb on the 6.48.6-6.49.19 long
    # term branch, absent before 6.31 -- which is why callers must walk
    # menu.fields instead of naming codes. See the README section.

    def package_update_menu(self) -> sch.Menu | None:
        """The (24,24) menu, or None on firmware/schema that lacks it."""
        return self.menu_for(PKG_UPDATE_PATH)

    def package_update_status(
        self,
        *,
        from_path: tuple[int, ...] | None = None,
        quiet: bool = True,
    ) -> RowObject:
        """One GET of the update state. ``quiet`` keeps 1 Hz polling out of
        the transcript, as the Traffic tab does."""
        menu = self.package_update_menu()
        command = menu.read_command if menu is not None else CMD_GET
        reply = self._request(
            PKG_UPDATE_PATH, command, from_path=from_path, quiet=quiet
        )
        return self._row_from_msg(PKG_UPDATE_PATH, reply)

    def package_update_run(
        self,
        command: int,
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        """Fire one of the menu's declared doit commands.

        The install command is answered immediately and the device reboots a
        moment later on its own, so the caller should expect the next poll to
        fail with a dropped session rather than treat that as an error.

        Real Winbox tags every one of these with object id 0, so we do too.
        """
        def add_obj(msg: m.Message) -> None:
            msg.add_u32(STD_OBJ_ID, 0)

        self._request(
            PKG_UPDATE_PATH, command, from_path=from_path, extra=add_obj
        )

    def package_update_set(
        self,
        code: int,
        value: Any,
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        """Write one field of /system package update -- e.g. the Channel.

        Captured from Winbox: command 0xFE000E carrying only the field that
        changed, with no request id and no reply_expected. The device answers
        nothing directly; it re-runs its version check and reports the new
        value on the next GET or pushed NOTIFY, so callers should read the
        result back rather than assume success.
        """
        menu = self.package_update_menu()
        fld = (menu.fields or {}).get(code) if menu is not None else None
        if fld is None:
            raise DeviceError(
                f"field {code:#x} is not declared on {PKG_UPDATE_PATH}"
            )
        try:
            tag, encoded = sch.encode_form_value(fld, value)
        except (ValueError, TypeError) as e:
            raise DeviceError(f"{fld.name or hex(code)}: {e}") from e

        def add_field(msg: m.Message) -> None:
            msg.add_typed(code, tag, encoded)

        self._send_oneway(
            PKG_UPDATE_PATH, CMD_SET, from_path=from_path, extra=add_field
        )

    def package_update_subscribe(
        self, *, from_path: tuple[int, ...]
    ) -> Any:
        """Have the device push download progress instead of polling for it.

        Real Winbox stops GET-polling the moment a download starts and
        subscribes instead; the router then pushes a NOTIFY roughly five times
        a second whose status field reads ``Downloaded 89% (22.5MiB)``. A GET
        never reports that -- Winbox issues none at all between subscribing and
        the reboot -- so progress is only available this way.

        Both SUBSCRIBE and UNSUBSCRIBE go out with no request id and no
        reply_expected, which is what the capture shows Winbox doing. The
        frames come back addressed to ``from_path``, so the demux routes them
        to the returned subscription.
        """
        if self.session is None:
            raise DeviceError("not connected")
        demux = self.session.demux
        if demux is None:
            raise DeviceError("progress subscription needs the frame demux")
        sub = demux.subscribe(tuple(from_path))
        try:
            self._send_oneway(PKG_UPDATE_PATH, CMD_SUBSCRIBE, from_path=from_path)
        except Exception:
            demux.unsubscribe(sub)
            raise
        return sub

    def package_update_unsubscribe(
        self, sub: Any, *, from_path: tuple[int, ...]
    ) -> None:
        """Drop the progress subscription. Safe to call on a dead session."""
        try:
            self._send_oneway(
                PKG_UPDATE_PATH, CMD_UNSUBSCRIBE, from_path=from_path
            )
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass
        demux = self.session.demux if self.session is not None else None
        if demux is not None and sub is not None:
            try:
                demux.unsubscribe(sub)
            except Exception:  # noqa: BLE001
                pass

    def package_update_notify_row(self, msg: m.Message) -> RowObject | None:
        """Decode one pushed NOTIFY frame into a row.

        The record arrives nested under 0xfe0002 rather than as top-level
        fields, exactly like a query poll reply.
        """
        menu = self.package_update_menu()
        if menu is None:
            return None
        rows = self._rows_from_reply(msg, menu)
        return rows[0] if rows else None

    def _send_oneway(
        self,
        path: tuple[int, ...],
        command: int,
        *,
        from_path: tuple[int, ...] | None = None,
        extra: Callable[[m.Message], None] | None = None,
    ) -> None:
        """Send a request that expects no reply -- no request id, no ack.

        Subscribe/unsubscribe are shaped this way on the wire; giving them a
        request id would leave a waiter in the demux that nothing ever answers.
        """
        if self.session is None:
            raise DeviceError("not connected")
        msg = (
            m.Message()
            .add_u32_array(m.SYS_TO, list(path))
            .add_u32_array(m.SYS_FROM, list(from_path or DEFAULT_FROM))
            .set_command(command)
        )
        msg.add_string_array(
            CLIENT_IDENT_FIELD, [self.client_ident.encode("utf-8")]
        )
        if extra is not None:
            extra(msg)
        with self._lock:
            if self.session_log is not None:
                self.session_log.request(path, command, self._log_fields(msg, path))
            self.session.send(msg)

    def open_terminal(
        self,
        *,
        cols: int = 80,
        rows: int = 24,
        on_output=None,
        on_closed=None,
    ):
        """Open a router console on this session.

        Each console gets its own SYS_FROM, so several terminals -- and Safe
        Mode -- coexist on one connection the way they do in real Winbox.

        Note the open request carries the account password in field 0x000001
        (real Winbox does the same even though the session is authenticated).
        It bypasses ``_request``, so it never reaches the transcript's field
        logger: terminal traffic must stay out of the log.
        """
        if self.session is None:
            raise DeviceError("not connected")
        from .terminal import TerminalChannel

        chan = TerminalChannel(
            self.session,
            password=self.password,
            from_path=self.alloc_from(),
            cols=cols,
            rows=rows,
            client_ident=self.client_ident,
            on_output=on_output,
            on_closed=on_closed,
        )
        chan.open()
        chan.start_reader()
        self._terminals.append(chan)
        if self.session_log is not None:
            self.session_log.note(
                f"terminal opened sid={chan.sid} from={chan.from_path} "
                f"{cols}x{rows}"
            )
        return chan

    def close_terminal(self, chan) -> None:
        """Close one console and record what went through it."""
        try:
            chan.close()
        finally:
            if chan in self._terminals:
                self._terminals.remove(chan)
            if self.session_log is not None:
                self.session_log.note(
                    f"terminal closed — {chan.bytes_in} B in / "
                    f"{chan.bytes_out} B out"
                )

    def export_config(
        self,
        *,
        command: str | None = None,
        show_sensitive: bool = True,
        timeout: float = 90.0,
        settle: float = 1.2,
        stall: float = 20.0,
    ) -> cexp.ConfigExport:
        """Capture a text ``/export`` through a console, entirely in memory.

        No file is written on the device: the config is read off the terminal
        channel, not out of the filespace. That matters on a box with tired
        flash, and it means this is a pure read -- no ``_guard_write``, nothing
        for Safe Mode to unroll, nothing to clean up afterwards.

        The console is the only way to ask for an export at all: the ``.jg``
        schema has no export handler (only PPP's ``Export .ovpn`` at [27,53]
        and the licence-key ones), so there is no CRUD path to use instead.

        Two details are load-bearing, both measured -- see
        :mod:`x3.proto.configexport`. The command is ``/export terse``,
        because the compact form's layout follows the terminal width and so
        scrapes differently at two window sizes. And the end of output is found
        with a ``:put`` sentinel, because the console's own prompt is neither
        stable nor escape-free.

        ``show_sensitive`` asks for the secrets too, which is what makes the
        result restorable rather than merely readable -- see
        :mod:`x3.proto.configexport`. The word only exists from firmware 6.43,
        so if the console refuses it this drops back to the plain form once and
        records which one produced the text in ``result.command``. An explicit
        ``command`` is taken as given and never retried.

        ``settle`` is the quiet period that ends the opening banner, where
        nothing more is coming. ``stall`` is the much longer one that ends a
        capture whose sentinel never arrives -- long because a pause in the
        middle of an export is a busy router far more often than a dead one,
        and cutting the read short there truncates the config. ``timeout``
        caps the whole read either way.
        """
        if self.session is None:
            raise DeviceError("not connected")
        chosen = command or cexp.export_command(
            self.info.version, show_sensitive=show_sensitive
        )
        needle = cexp.SENTINEL.encode()

        buf = bytearray()
        lock = threading.Lock()

        def on_output(data: bytes) -> None:
            with lock:
                buf.extend(data)

        def snapshot() -> tuple[int, bytes]:
            with lock:
                return len(buf), bytes(buf)

        def drain(*, want_sentinel: bool, quiet: float) -> bytes:
            """Read until the sentinel has been seen twice, or output stops for
            ``quiet`` seconds.

            Twice, because the first occurrence is the console echoing the
            ``:put`` back at us -- stopping there would cut the capture off
            before the output it is meant to terminate.

            ``quiet`` means two different things to the two phases, which is
            why the caller sets it. Draining the banner, silence is the only
            terminator there is and a short window is right. Draining the
            export, silence proves nothing: the ``:put`` has already been
            typed, so the sentinel *is* coming unless the session is broken,
            and a router that pauses mid-export -- a slow WAN hop, a big
            wireless menu -- would otherwise have its config truncated at
            whatever byte it had reached. Hence a window long enough to be
            evidence of a dead session rather than of a busy one.
            """
            start = time.monotonic()
            seen, last_change = -1, start
            while time.monotonic() - start < timeout:
                time.sleep(0.15)
                now = time.monotonic()
                size, data = snapshot()
                if size != seen:
                    seen, last_change = size, now
                    if want_sentinel and data.count(needle) >= 2:
                        # Let any straggling frame land before we stop.
                        time.sleep(0.4)
                        break
                    continue
                if now - last_change >= quiet:
                    break
            with lock:
                out = bytes(buf)
                buf.clear()
            return out

        chan = self.open_terminal(cols=cexp.EXPORT_COLS, on_output=on_output)
        try:
            self._reach_shell(chan, lambda: drain(want_sentinel=False, quiet=settle))

            def run(cmd: str) -> cexp.ConfigExport:
                chan.write(cmd.encode() + b"\r")
                # The two writes must not be coalesced into one line: `:put`
                # has to run as its own command, after the export has finished
                # printing.
                time.sleep(0.2)
                chan.write(f':put "{cexp.SENTINEL}"'.encode() + b"\r")
                return cexp.parse(
                    drain(want_sentinel=True, quiet=stall),
                    command=cmd,
                    sentinel=cexp.SENTINEL,
                )

            result = run(chosen)
            # Pre-6.43 firmware has no `show-sensitive` and says so. It also
            # needs none -- that release is what started hiding secrets -- so
            # the plain form is the complete export there. Same console: the
            # fallback costs a command, not a reconnect.
            if result.refused and command is None and chosen != cexp.EXPORT_COMMAND:
                if self.session_log is not None:
                    self.session_log.note(
                        f"console refused {chosen!r} ({result.console_error}); "
                        f"retrying as {cexp.EXPORT_COMMAND!r}"
                    )
                result = run(cexp.EXPORT_COMMAND)
        finally:
            self.close_terminal(chan)
        if self.session_log is not None:
            note = (
                f"config export ({result.command}) — {result.raw_len} B read, "
                f"{len(result.body.splitlines())} config lines, "
                f"sha256:{result.digest[:12]}"
            )
            if not result.complete:
                note += " (INCOMPLETE: no sentinel)"
            if result.refused:
                note += f" (REFUSED: {result.console_error})"
            if result.spliced_logs:
                note += f" ({len(result.spliced_logs)} log line(s) spliced in)"
            self.session_log.note(note)
        if result.refused:
            raise DeviceError(
                f"the console refused {result.command!r}: {result.console_error}"
            )
        if not result.complete:
            raise DeviceError(
                "export did not finish: the end-of-output marker never arrived "
                f"({result.raw_len} B read)"
            )
        return result

    def _reach_shell(self, chan, drain_banner) -> None:
        """Get a fresh console to its shell prompt, or refuse to type at all.

        ``drain_banner`` reads until the console goes quiet and returns what
        it said. Nothing is typed until we know what is listening: a console
        holding a first-login question answers it with whatever arrives next,
        so sending a command blind either wastes the capture or -- at a
        change-your-password prompt -- sets the account password to the
        command. See configexport.console_state.
        """
        state = cexp.console_state(drain_banner())
        if state == "password":
            raise DeviceError(
                "the console is asking for a new password, not running "
                "commands. Anything sent now would become this account's "
                "password. Log in to this device by hand and set one."
            )
        if state == "license":
            # Declining is a display choice, not a configuration change.
            if self.session_log is not None:
                self.session_log.note(
                    "console was at the licence prompt; declined it to "
                    "reach a shell"
                )
            chan.write(b"n\r")
            if cexp.console_state(drain_banner()) == "password":
                raise DeviceError(
                    "the console asked for a new password after the licence "
                    "prompt; set one by hand first"
                )

    def command_line_for(
        self,
        candidates: list[str],
        obj_id: int,
        *,
        menu_path: Any = None,
        timeout: float = 20.0,
    ) -> str | None:
        """The router's own CLI line for one object, or None.

        Not built from the form: the .jg names fields for display ("Dst.
        Address"), and a guessed ``dst-address`` would be a guess. Asked of the
        router instead -- ``<path> export terse where .id=*N`` prints exactly
        that object's line. The .jg does not say a menu's CLI path either, so
        the caller offers candidates and every one goes in a single console
        line, each wrapped so a wrong path fails quietly (``:parse`` defers the
        parse, or one bad path would sink the whole line). The line that comes
        back carries its own path, which is remembered per menu, so the next
        object needs one try. Read-only: export changes nothing.
        """
        cache = self.__dict__.setdefault("_cli_paths", {})
        known = cache.get(menu_path) if menu_path is not None else None
        tries = [known] if known else []
        tries += [p for p in candidates if p not in tries]
        safe = [p for p in tries if re.fullmatch(r"/[a-z0-9][a-z0-9 -]*", p or "")]
        if not safe:
            return None
        oid = f"*{int(obj_id):X}"
        # Each try is announced, so its lines are read apart: the generic
        # /interface export ignores `where` for its sub-sections and prints
        # every interface, and a second try that also succeeds would double up.
        safe = safe[:16]
        command = "; ".join(
            f':put "{_CLI_MARK}{i}"; '
            f':do {{[:parse "{p} export terse where .id={oid}"]}} on-error={{}}'
            for i, p in enumerate(safe)
        )
        result = self.run_console(command, timeout=timeout)
        found: dict[int, list[str]] = {}
        current = None
        for ln in (result.text or "").splitlines():
            ln = ln.strip()
            mark = re.fullmatch(_CLI_MARK + r"(\d+)", ln)
            if mark:
                current = int(mark.group(1))
                continue
            if current is not None and re.match(
                re.escape(safe[current]) + r" (add|set|remove)\b", ln
            ):
                # Only lines of the path tried: a sub-section's are not this
                # object's.
                found.setdefault(current, []).append(ln)
        for i in range(len(safe)):
            if found.get(i):
                if menu_path is not None:
                    cache[menu_path] = safe[i]
                return "\n".join(found[i])
        return None

    def export_lines(self, cli_path: str, *, timeout: float = 60.0) -> dict[int, str]:
        """Every object's own export line under ``cli_path``, keyed by .id.

        One console line for the whole list: a :foreach over ``find`` that
        announces each id and exports just that object, so the lines come back
        already matched to their rows. Only what differs from the default is
        in an export, which is what a "what was set here" summary wants and
        what the .jg cannot say for most fields. Read-only.
        """
        if not re.fullmatch(r"/[a-z0-9][a-z0-9 -]*", cli_path or ""):
            return {}
        command = (
            f':foreach i in=[{cli_path} find] do={{'
            f':put "{_CLI_MARK}ID$i"; '
            f':do {{[:parse "{cli_path} export terse where .id=$i"]}} on-error={{}}}}'
        )
        result = self.run_console(command, timeout=timeout)
        out: dict[int, str] = {}
        current = None
        verb = re.compile(re.escape(cli_path) + r" (add|set|remove)\b")
        for ln in (result.text or "").splitlines():
            ln = ln.strip()
            mark = re.fullmatch(_CLI_MARK + r"ID\*([0-9A-Fa-f]+)", ln)
            if mark:
                current = int(mark.group(1), 16)
                continue
            if current is not None and verb.match(ln):
                out[current] = (out[current] + "\n" + ln) if current in out else ln
        return out

    def cli_path_known(self, key: Any) -> str | None:
        """The CLI path command_line_for found for ``key``, if it has."""
        return self.__dict__.get("_cli_paths", {}).get(key)

    def run_console(
        self,
        command: str,
        *,
        timeout: float = 60.0,
        settle: float = 1.2,
        rows: int = 500,
    ) -> cexp.ConfigExport:
        """Type one command at a fresh console and return what it printed.

        Done when the prompt comes back, not at a ``:put`` sentinel the way
        :meth:`export_config` ends: a sentinel is typed while the command is
        still running, and the console hands those keystrokes to whatever
        holds it. The prompt is the console's own word that the command is
        over, and it costs nothing to wait for.

        A command still running at ``timeout`` gets a Ctrl-C, and the result
        comes back with ``complete`` False. ``rows`` is the console height a
        pager would stop at, so long output is not cut at a ``-- more`` line.

        No guard here on what the command does: the console runs anything,
        whatever ``readonly`` says. Callers decide what may be typed.
        """
        if self.session is None:
            raise DeviceError("not connected")
        buf = bytearray()
        lock = threading.Lock()

        def on_output(data: bytes) -> None:
            with lock:
                buf.extend(data)

        def take() -> bytes:
            with lock:
                return bytes(buf)

        def drain_banner() -> bytes:
            seen, last = -1, time.monotonic()
            while time.monotonic() - last < settle:
                time.sleep(0.15)
                size = len(take())
                if size != seen:
                    seen, last = size, time.monotonic()
            with lock:
                out = bytes(buf)
                buf.clear()
            return out

        def at_prompt(data: bytes) -> bool:
            # The prompt alone on the last line. The echo of the command also
            # starts with the prompt, so a line with anything after it is not
            # the console coming back.
            last = cexp.strip_ansi(data).rstrip("\n").rsplit("\n", 1)[-1]
            return bool(cexp.PROMPT_RE.match(last)) and not cexp.PROMPT_RE.sub(
                "", last, count=1
            ).strip()

        chan = self.open_terminal(cols=cexp.EXPORT_COLS, rows=rows, on_output=on_output)
        done = False
        try:
            self._reach_shell(chan, drain_banner)
            chan.write(command.encode() + b"\r")
            start = time.monotonic()
            seen, quiet_since = -1, start
            while time.monotonic() - start < timeout:
                time.sleep(0.2)
                data = take()
                if len(data) != seen:
                    seen, quiet_since = len(data), time.monotonic()
                    continue
                if time.monotonic() - quiet_since >= 0.4 and at_prompt(data):
                    done = True
                    break
            if not done:
                chan.write(b"\x03")
                time.sleep(0.5)
            # Taken before the close: closing types a /quit, and its echo is
            # not this command's output.
            raw = take()
        finally:
            self.close_terminal(chan)
        # parse() looks for a sentinel to call the capture complete; there is
        # none here, so completeness is the prompt's, set below.
        result = cexp.parse(raw, command=command, sentinel="\x00", split_logs=False)
        result.complete = done
        if self.session_log is not None:
            self.session_log.note(
                f"console command {command!r} — {result.raw_len} B"
                + ("" if done else " (TIMED OUT)")
            )
        return result

    def connection_lost(self) -> BaseException | None:
        """Why this session's connection is gone, or None while it is up.

        The frame reader is what notices: a reset, an EOF, a frame it cannot
        read. Requests after that fail at once (DemuxClosed), so nothing
        hangs -- but nothing says the session is over either, unless asked.
        """
        sess = self.session
        demux = getattr(sess, "demux", None) if sess is not None else None
        if demux is None or demux.alive:
            return None
        from .demux import DemuxClosed

        return demux.reader_error or DemuxClosed("connection reader stopped")

    def reconnect(self, progress=None) -> None:
        """A fresh session in place of a dead one, on this same client.

        Every open window holds this object, so replacing the session under it
        is what lets them carry on: the schema, the caches, the session log
        and the audit file all stay. What belonged to the old connection goes
        with it -- its consoles, and its Safe Mode, which the router unrolled
        the moment the link dropped.
        """
        old = self.session
        self.session = None
        self._terminals.clear()
        self._safe_mode = None
        self.safe_mode_active = False
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001 - it is already dead
                pass
        if self.session_log is not None:
            self.session_log.note(f"reconnecting to {self.host}:{self.port}")
        self.connect(progress, need_schema=self.schema is None)
        if self.session_log is not None:
            self.session_log.note("reconnected")

    def abort(self) -> None:
        """Abandon a connect in progress. Safe from another thread.

        connect() blocks for as long as the transport takes to give up, and
        until it returns there is no self.session to close -- so this is the
        only way to stop a cancelled connect promptly.
        """
        sess = self._connecting
        if sess is None:
            return
        cancel = getattr(sess, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self.abort()
        # Consoles first: /quit needs a live socket.
        for chan in list(self._terminals):
            try:
                self.close_terminal(chan)
            except Exception:
                pass
        if self._safe_mode is not None:
            try:
                self._safe_mode.close()
            except Exception:
                pass
            self._safe_mode = None
        self.safe_mode_active = False
        if self.session is not None:
            try:
                self.session.close()
            finally:
                self.session = None
        if self.session_log is not None:
            self.session_log.note("disconnected")
            self.session_log.close()
        if self.audit is not None:
            self.audit.close(reason="disconnected")
            self.audit = None

    def __enter__(self) -> "DeviceClient":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # --- schema -------------------------------------------------------

    def _schema_search_paths(self) -> list[str]:
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return [repo, os.path.join(repo, "mirror"), os.getcwd()]

    def _load_schema_local_only(
        self,
        *,
        version: str | None = None,
        build_id: int | None = None,
    ) -> None:
        """Load schema from disk without talking to the device."""
        directory = sch.resolve_schema_dir(
            explicit=self.schema_dir_hint,
            version=version or self._plugin_version or None,
            build_id=build_id if build_id is not None else (self._plugin_build or None),
            search_paths=self._schema_search_paths(),
        )
        if not directory:
            raise DeviceError(
                "no local .jg schema cache found. Run "
                "`x3ctl mirror --host …` once, or pass schema_dir=."
            )
        self.schema = sch.Schema.load(directory)
        self.schema_dir = directory
        want = build_id if build_id is not None else (self._plugin_build or None)
        got = sch.build_id_of_dir(directory)
        self.schema_build_mismatch = (
            (got, int(want)) if want and got != int(want) else None
        )
        if self.session_log is not None:
            note = f"schema from {directory}"
            if self.schema_build_mismatch is not None:
                note += (
                    f" -- build {got} is not this device's {want}; "
                    "menus may be another firmware's"
                )
            self.session_log.note(note)

    def _load_schema(self) -> None:
        assert self.session is not None
        version = build_id = None
        # Why the index matters beyond the plugin list: without its build id the
        # schema is chosen by version prefix, and a version can name more than
        # one cached set. Keep the failure so the fallback is not silent.
        index_err: Exception | None = None
        # Plugin index can hang with some client-idents on some firmwares
        # (seen: winbox-4.0 + firmware 7.23.1 list open timed out). Cap the wait
        # and fall back to the best local cache.
        # With the demux running the deadline belongs on the request, not the
        # socket: a socket timeout that fires part way through a frame leaves
        # the stream desynchronised for good.
        cap = min(float(self.timeout or 10.0), 5.0)
        demuxed = getattr(self.session, "demux", None) is not None
        old_timeout = None
        sock = None if demuxed else getattr(self.session, "_sock", None)
        try:
            if sock is not None and hasattr(sock, "gettimeout"):
                old_timeout = sock.gettimeout()
                sock.settimeout(cap)
            index = self.session.fetch_file("list", handler=(2, 2), timeout=cap)
            version, build_id = _parse_list_meta(index)
            self._plugin_version = version or ""
            self._plugin_build = build_id or 0
            self.device_plugins = _parse_plugin_names(index)
        except Exception as first_err:
            index_err = first_err
            # Retry list once with the alternate classic identity if we are
            # on the Winbox-4 default — some firmware builds accept 4.0 for menus
            # but still require 3.42 for mproxy file fetch.
            try:
                from .client_ident import CLIENT_IDENT_WINBOX3, is_winbox4_ident

                if is_winbox4_ident(self.client_ident) and self.session is not None:
                    if sock is not None and hasattr(sock, "settimeout"):
                        sock.settimeout(cap)
                    index = self.session.fetch_file(
                        "list", handler=(2, 2),
                        client_ident=CLIENT_IDENT_WINBOX3, timeout=cap,
                    )
                    version, build_id = _parse_list_meta(index)
                    self._plugin_version = version or ""
                    self._plugin_build = build_id or 0
                    self.device_plugins = _parse_plugin_names(index)
                    index_err = None
            except Exception as retry_err:
                index_err = retry_err
        finally:
            if sock is not None and old_timeout is not None and hasattr(sock, "settimeout"):
                try:
                    sock.settimeout(old_timeout)
                except Exception:
                    pass

        if index_err is not None and self.session_log is not None:
            self.session_log.note(
                f"plugin index unavailable ({type(index_err).__name__}: {index_err}); "
                "choosing a schema cache without the device's build id"
            )
        if index_err is None:
            self._fetch_plugin_set(index, version, build_id)
        self._load_schema_local_only(version=version, build_id=build_id)

    def _fetch_plugin_set(
        self, index: bytes, version: str | None, build_id: int | None
    ) -> None:
        """Download this firmware's plugins when no cache holds its build.

        What Winbox itself does on connect. Without it a firmware nobody had
        cached yet got another release's menus -- and a menu that release does
        not draw, or draws differently, is simply missing from the tree.
        """
        if self.schema_dir_hint or not version or build_id is None:
            return
        if sch.find_exact_dir(version, build_id):
            return
        dest = os.path.join(sch.user_cache_dir(), f"{version}-{build_id}")
        log = self.session_log.note if self.session_log is not None else None
        if log:
            log(f"no plugin cache for {version}-{build_id}; downloading to {dest}")
        per_file = float(self.timeout or 10.0)
        try:
            got, cached, failed = sch.download_plugin_set(
                sch.plugin_index_entries(index), build_id, dest,
                lambda name: self.session.fetch_file(
                    name, handler=(2, 2), timeout=per_file),
            )
            with open(os.path.join(dest, "list"), "wb") as fh:
                fh.write(index)
        except Exception as e:  # noqa: BLE001 - a local cache is still a fallback
            if log:
                log(f"plugin download failed ({type(e).__name__}: {e})")
            return
        if log:
            log(f"plugins: {got} downloaded, {cached} cached, {failed} failed")

    # --- enum resolution ----------------------------------------------

    def enum_map(self, path: tuple[int, ...]) -> dict[int, str]:
        """Map object ids → names for a dynamic enum source menu."""
        if path in self._enum_cache:
            return self._enum_cache[path]
        self._enum_cache[path] = {}      # placeholder: breaks reference cycles
        if self.menu_for(path) is None:
            # No schema record for this menu, so there is nothing to name its
            # rows with -- and the firmware probably has no such handler either.
            # Firmware 7.21 dropped OpenFlow, and a WiFi interface still declares
            # 'OpenFlow Switch' with its source at (125,1): the device answers
            # that request with silence, so asking cost a full client timeout
            # (12s in the UI) on every wifi form opened.
            return self._enum_cache[path]
        mapping: dict[int, str] = {}
        try:
            # Prefer list; fall back to single-object GET for settings menus
            rows = self.getall(path)
        except DeviceError:
            rows = []
        else:
            self._enum_rows[path] = rows
            self._enum_read_at[path] = time.monotonic()
            mapping = self._enum_from_rows(path, rows)
        self._enum_cache[path] = mapping
        return mapping

    def enum_source_map(
        self, path: tuple[int, ...], pred: Any = None
    ) -> dict[int, str]:
        """``{object id: name}`` for a dropdown, narrowed by its ``.jg`` pred.

        The path names a whole table and the pred says which of its rows this
        field may be set to -- see :func:`schema.enum_source_allows`. 217
        dynamic sources in 7.21 carry one, and reading the path without the
        pred is the wrong list: Bridge Port ▸ Bridge offered every interface
        on the box.

        Narrowing is for *offering* a value. Rendering one that is already
        stored still goes through :meth:`enum_map`, whose whole-table map is
        what names a row the pred would no longer offer.
        """
        path = tuple(path)
        if not pred:
            return self.enum_map(path)
        key = (path, json.dumps(pred, sort_keys=True, default=str))
        hit = self._enum_pred_cache.get(key)
        if hit is not None:
            return hit
        whole = self.enum_map(path)      # also fills _enum_rows
        source = self._enum_rows.get(path)
        if not source:
            # Nothing to judge: either the table really is empty, or whatever
            # filled the map did not keep the rows. Offer the map as it stands
            # rather than an empty box -- the same call the caller would have
            # made without a pred.
            return whole
        menu = self.menu_for(path)
        rows = [
            r for r in source
            if sch.enum_source_allows(
                menu, pred, getattr(r, "values_by_code", None)
            )
        ]
        out = self._enum_from_rows(path, rows)
        self._enum_pred_cache[key] = out
        return out

    def query_enum(
        self,
        spec: dict[str, Any],
        params: dict[int, Any] | None = None,
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> dict[int, str]:
        """Run a ``queryenum`` and return ``{wire value: label}``.

        Some dropdowns are not a table the device keeps but an answer it
        computes. Wireless Frequency is the case in hand::

            {type:'queryenum',path:[88,3],cmd:6,valuesid:'U4',bvaluesid:'U5',
             params:[{name:'Band',id:'u81a'},{name:'Channel Width',id:'u81b'},
                     {name:'Country',id:'u8ad'},…]}

        -- "ask handler (88,3) which frequencies this band, width and country
        allow". The parameters are the form's own current values, which is why
        they come in from the caller rather than being read here, and why the
        answer is cached against them.

        ``bvaluesid`` is a second array whose meaning is not settled (it is a
        number array, not labels). If it ever arrives as strings it is used as
        the labels; otherwise the values label themselves, as Winbox shows
        them. An unanswerable query returns ``{}`` -- the caller falls back to
        a plain box rather than an empty dropdown.
        """
        path = tuple(spec.get("path") or ())
        cmd = spec.get("cmd")
        if not path or not isinstance(cmd, int) or not cmd:
            return {}
        sent = {
            code: value for code, value in (params or {}).items()
            if isinstance(code, int) and value is not None
        }
        key = (path, cmd, tuple(sorted(sent.items(), key=lambda kv: kv[0])))
        hit = self._query_enum_cache.get(key)
        if hit is not None:
            return hit
        self._query_enum_cache[key] = {}      # placeholder: no re-entry storms

        def add_params(msg: m.Message) -> None:
            for code, value in sent.items():
                if isinstance(value, bool):
                    msg.add_bool(code, value)
                elif isinstance(value, int):
                    msg.add_u32(code, value & 0xFFFFFFFF)
                elif isinstance(value, (bytes, bytearray)):
                    msg.add_raw(code, bytes(value))
                else:
                    msg.add_string(code, str(value))

        try:
            reply = self._request(path, cmd, from_path=from_path, extra=add_params)
        except DeviceError:
            return {}
        if reply.has_error():
            return {}
        values = _as_list(reply.find(spec.get("values_id")))
        labels = _as_list(reply.find(spec.get("labels_id")))
        if len(labels) != len(values) or not all(
            isinstance(x, (str, bytes, bytearray)) for x in labels
        ):
            labels = []
        out: dict[int, str] = {}
        for i, value in enumerate(values):
            try:
                wire = int(value)
            except (TypeError, ValueError):
                continue
            if labels:
                label = labels[i]
                text = (
                    label.decode("utf-8", "replace")
                    if isinstance(label, (bytes, bytearray))
                    else str(label)
                )
            else:
                text = str(wire)
            out[wire] = text
        self._query_enum_cache[key] = out
        return out

    def system_caps(self) -> frozenset[int] | None:
        """The numbered capabilities this build and board report, or None.

        The .jg gates menus and fields on them -- ``{type:'syscap',value:53}``
        -- and the conds that read them are named after switch-chip families
        (``prestera``, ``aldrin2``, ``switch_mv88e6xxx``, ``qstats``), so they
        say what the hardware can do rather than what the software has.

        They arrive unasked in the session handler's own reply: (13,4) command
        7 answers with a nested message at 0x28 whose 0x6D is a u32 array --
        128 bits, one per capability, LSB of the first word being capability 0.
        Confirmed against a 7.23 AP, whose set included the mode button and
        RPS and excluded BTRFS, VETH, SwOS, PTP and every switch-chip family.

        ``None`` means the device did not tell us, and every syscap predicate
        then answers True: showing a menu Winbox would hide is recoverable,
        hiding one it shows is not.
        """
        if self._syscaps is not _UNREAD:
            return self._syscaps
        self._syscaps = None
        try:
            reply = self._request(SESSION_PATH, SESSION_COMMAND, quiet=True)
        except DeviceError:
            return None
        info = reply.find(SESSION_INFO_FIELD)
        nested = info[1] if info and isinstance(info[1], m.Message) else None
        if nested is None:
            return None
        words = _as_list(nested.find(SYSCAP_FIELD))
        if not words:
            return None
        caps: set[int] = set()
        for index, word in enumerate(words):
            try:
                value = int(word)
            except (TypeError, ValueError):
                continue
            for bit in range(32):
                if value & (1 << bit):
                    caps.add(index * 32 + bit)
        self._syscaps = frozenset(caps)
        return self._syscaps

    def lookup_map(
        self, path: tuple[int, ...], dst: str, val: str
    ) -> dict[str, str]:
        """``{dst value: val value}`` from another handler, for a lookup column.

        ARP ▸ Bridge Port is "the bridge port whose MAC Address is this row's":
        one read of (16,3), keyed by the field the .jg names. Matching is on the
        *rendered* text -- both ends are the same field type, so both render the
        same way, and a MAC is not an object id that could be compared as a
        number.
        """
        key = (tuple(path), dst, val)
        hit = self._lookup_cache.get(key)
        if hit is not None and (
            time.monotonic() - self._lookup_read_at.get(key, 0.0) < LOOKUP_TTL
        ):
            return hit
        self._lookup_cache[key] = {}      # placeholder: no re-entry storms
        self._lookup_read_at[key] = time.monotonic()
        if self.menu_for(tuple(path)) is None:
            return self._lookup_cache[key]
        try:
            rows = self.getall(tuple(path))
        except DeviceError:
            return self._lookup_cache[key]
        out: dict[str, str] = {}
        for row in rows:
            by_name = {
                str(name): (value or "")
                for _c, name, value, _ok in getattr(row, "fields", None) or []
            }
            k = str(by_name.get(dst, "")).strip()
            v = str(by_name.get(val, "")).strip()
            if k and v:
                out.setdefault(k.lower(), v)
        self._lookup_cache[key] = out
        return out

    def last_truncated(self) -> str | None:
        """What to say about the last list read, if a `maxobjs` cap bit."""
        return self._last_truncated

    def forget_lookups(self) -> None:
        """Drop the lookup reads, so the next list load takes fresh ones."""
        self._lookup_cache.clear()
        self._lookup_read_at.clear()

    def _getall_rows(
        self, path: tuple[int, ...], reply: m.Message
    ) -> list[RowObject]:
        """The objects a GETALL reply carries, as rows.

        Not ``_rows_from_reply``: that name is taken by the query-poll reader
        a few hundred lines up, which takes a menu rather than a path and
        reads a different reply shape.
        """
        objs = reply.find(STD_OBJS)
        items = objs[1] if objs and isinstance(objs[1], list) else []
        return [
            self._row_from_msg(path, it)
            for it in items
            if isinstance(it, m.Message)
        ]

    def _enum_from_rows(
        self, path: tuple[int, ...], rows: list[RowObject]
    ) -> dict[int, str]:
        """``{object id: name}`` for the rows of an enum source menu."""
        msgs = [r.raw for r in rows if isinstance(getattr(r, "raw", None), m.Message)]
        if hasattr(sch, "enum_map_from_objects"):
            menu = self.menu_for(path)
            mapping = sch.enum_map_from_objects(menu, msgs)
            # A table with no name of any kind is named by its first address,
            # as the decoded row shows it. A route's next hops are only that:
            # Immediate Gateway read "#539501504" where the hop is 10.0.2.2.
            addr_codes = [
                c for c, f in (menu.fields.items() if menu else ())
                if f.jg_type in ("addr", "ipaddr", "network", "ip6addr", "network6")
            ]
            for row in rows:
                oid = row.values_by_code.get(0xFE0001)
                if not isinstance(oid, int) or mapping.get(oid) != f"#{oid}":
                    continue
                shown = next((row.get_rendered(c) for c in addr_codes
                              if c in row.values_by_code and row.get_rendered(c)), "")
                if shown:
                    mapping[oid] = shown
            return mapping
        mapping = {}
        for row in rows:
            oid = None
            for code in (0xFE0001, 0xFE000A):
                if code in row.values_by_code and isinstance(row.values_by_code[code], int):
                    oid = row.values_by_code[code]
                    break
            name = None
            for _c, n, v, _ok in row.fields:
                if n.lower() in ("name", "default name", "interface"):
                    name = v
                    break
            if name is None and row.fields:
                name = row.fields[0][2]
            if oid is not None and name:
                mapping[oid] = str(name)
        return mapping

    def prefetch_enums(self, paths) -> None:
        """Fill the enum cache for several source menus in one pipelined burst.

        A property form's dropdowns come from other menus -- a wifi interface
        references eleven of them. Read one at a time that is eleven round trips
        the user waits through before the window appears; sent as one burst it
        is about one. Winbox does exactly this (see :meth:`pipeline`).

        Paths already cached are skipped, and a path the device refuses is
        cached empty so the form does not ask again field by field.
        """
        want: list[tuple[int, ...]] = []
        for path in paths:
            p = tuple(path)
            if not p or p in self._enum_cache or p in want:
                continue
            if self.menu_for(p) is None:
                # Nothing to name the rows with, and no handler to ask (see
                # enum_map). Cache the emptiness so no editor asks either.
                self._enum_cache[p] = {}
                continue
            want.append(p)
        if not want:
            return
        # Bounded: this is an optimisation, and one handler answering with
        # silence must not hold the window that is waiting for the burst.
        replies = self.pipeline(
            [(p, CMD_GETALL) for p in want],
            timeout=min(float(self.timeout or 10.0), 3.0),
        )
        for path, reply in zip(want, replies):
            if isinstance(reply, DeviceError):
                # The device refused it: GETALL is NOTIMP on settings-style
                # menus, which need the single-object GET that enum_map falls
                # back to. Leave it uncached so enum_map can try that.
                continue
            if isinstance(reply, BaseException):
                # No reply at all -- an unimplemented handler. Cache it empty:
                # asking again only spends another timeout.
                self._enum_cache[path] = {}
                continue
            rows = self._getall_rows(path, reply)
            self._enum_rows[path] = rows
            self._enum_cache[path] = self._enum_from_rows(path, rows)
            self._enum_read_at[path] = time.monotonic()

    #: Interface table fields: name, fallback name, and the type label the
    #: device sends as a string ('ether', 'bridge', 'wifi', 'loopback').
    _IFACE_NAME = 0x010006
    _IFACE_DEFAULT_NAME = 0x010031
    _IFACE_TYPE = 0x01001E

    def interface_choices(self) -> list[tuple[int, str, str]]:
        """``(id, name, type)`` for every interface, for a picker.

        Winbox groups its interface dropdown by kind — all ethernet, all vlan,
        all wireless — so the type label comes back alongside the name rather
        than being inferred from it.
        """
        if self._iface_choices is not None:
            return self._iface_choices
        out: list[tuple[int, str, str]] = []
        try:
            rows = self.getall(sch.IFACE_ENUM_PATH)
        except Exception:  # noqa: BLE001
            return out      # not cached: a failed probe must not become the answer

        def text(v: Any) -> str:
            if isinstance(v, (bytes, bytearray)):
                return v.decode("utf-8", errors="replace")
            return v if isinstance(v, str) else ""

        for row in rows:
            vals = getattr(row, "values_by_code", {}) or {}
            try:
                oid = row.object_id()
            except Exception:  # noqa: BLE001
                oid = None
            if oid is None:
                continue
            name = text(vals.get(self._IFACE_NAME)) or text(
                vals.get(self._IFACE_DEFAULT_NAME)
            )
            if not name:
                continue
            out.append((oid, name, text(vals.get(self._IFACE_TYPE)) or "other"))
        self._iface_choices = out
        return out

    #: Flag fields whose letter means "this row is up". Three menus in 7.21.1
    #: declare one -- Interface (20,0) and Port (20,55) at 0x1000e, Partition
    #: (92,1) at 0x3 -- so it is looked up by *name* and not by a fixed code.
    #: Every other source table has none, which is what stops a bridge or an
    #: address-list picker drawing a state it has no business having.
    _RUNNING_FLAG_NAMES = ("running",)

    def _running_flag_code(self, menu: "sch.Menu | None") -> int | None:
        """Code of this menu's `running` flag, or None if it has none."""
        if menu is None:
            return None
        try:
            flags = menu.flag_fields()
        except Exception:  # noqa: BLE001
            return None
        for fld in flags:
            if (fld.name or "").strip().lower() in self._RUNNING_FLAG_NAMES:
                return fld.code
        return None

    def enum_marks(self, path: tuple[int, ...]) -> dict[int, "EnumMark"]:
        """``{object id: EnumMark}`` for the rows behind a dynamic enum.

        Free on the wire: these are the very rows :meth:`enum_map` read to name
        the entries, kept in ``_enum_rows`` for the pred filter. A maskless
        GETALL on the interface table carries `running`, the standard Disabled
        bit and the comment alongside the name -- confirmed against 7.23.3,
        where commenting ether6 and disabling vlan33 on the console showed up
        in the same read that fills every interface dropdown.

        Cached with those rows and dropped with them, so a write that changes
        the table (see :meth:`_forget_enums`) invalidates the marks too.
        """
        p = tuple(path)
        if not p:
            return {}
        hit = self._enum_marks_cache.get(p)
        if hit is not None:
            return hit
        rows = self._enum_rows.get(p)
        if rows is None:
            self.enum_map(p)        # fills _enum_rows on the way past
            rows = self._enum_rows.get(p)
        if not rows:
            return {}
        menu = self.menu_for(p)
        run_code = self._running_flag_code(menu)
        out: dict[int, EnumMark] = {}
        for row in rows:
            try:
                oid = row.object_id()
            except Exception:  # noqa: BLE001
                oid = None
            if oid is None:
                continue
            vals = getattr(row, "values_by_code", None) or {}
            # Present-and-false, not absent: a reply that leaves a flag out has
            # not said the link is down, and drawing "down" off a missing field
            # would put a red mark on every row of a table that omits them.
            running = bool(vals.get(run_code)) if run_code in vals else None
            try:
                comment = row.comment()
            except Exception:  # noqa: BLE001
                comment = ""
            out[oid] = EnumMark(
                flags=sch.format_flag_letters(menu, vals),
                running=running,
                disabled=bool(vals.get(sch.FLAG_DISABLED_CODE)),
                comment=comment or "",
            )
        self._enum_marks_cache[p] = out
        return out

    def refresh_enum_rows(
        self, path: tuple[int, ...], *, min_age: float = 2.0, timeout: float = 2.0
    ) -> bool:
        """Re-read a dynamic enum's source table so its marks are current.

        A stale *name* is invisible; a stale *mark* is a lie -- it says "up"
        about a link that dropped while the form sat open. A dropdown asks for
        this as it opens, which is the moment before anyone reads the answer.

        Bounded twice over. ``min_age`` skips the read when the rows are newer
        than that, so opening the same box twice does not cost two reads;
        ``timeout`` is short because this runs on the UI thread and a device
        that has gone quiet must not hold the popup for the client's full wait.
        Returns True when fresh rows landed -- False leaves everything as it
        was, which is the right answer for a box that can still show the rows
        it has.
        """
        p = tuple(path)
        if not p or self.session is None:
            return False
        last = self._enum_read_at.get(p)
        if last is not None and (time.monotonic() - last) < min_age:
            return False
        if self.menu_for(p) is None:
            return False        # nothing to name the rows with -- see enum_map
        try:
            replies = self.pipeline(
                [(p, CMD_GETALL)],
                timeout=max(0.5, float(timeout)),
                quiet=True,
            )
        except Exception:  # noqa: BLE001
            return False
        reply = replies[0] if replies else None
        if not isinstance(reply, m.Message):
            # A DeviceError or a timeout in the slot. Stamp it anyway: a
            # handler that will not answer must not be asked again on every
            # click of the same dropdown.
            self._enum_read_at[p] = time.monotonic()
            return False
        rows = self._getall_rows(p, reply)
        self._enum_rows[p] = rows
        self._enum_cache[p] = self._enum_from_rows(p, rows)
        self._enum_marks_cache.pop(p, None)
        for key in [k for k in self._enum_pred_cache if k[0] == p]:
            self._enum_pred_cache.pop(key, None)
        self._enum_read_at[p] = time.monotonic()
        return True

    def quickset_capabilities(self) -> set[str]:
        """Coarse device capabilities for filtering QuickSet modes, cached.

        Winbox only offers the QuickSet modes a box can actually run — a virtual router
        with no radios shows *Ethernet* alone; a wireless router shows the wireless
        modes too. We derive the same from the interface list: the returned set
        holds any of ``{"ether", "wireless", "lte", "w60g"}`` that apply.
        ``ether`` is always included so the Ethernet mode is never hidden.
        Empty only when the probe itself failed (caller then hides nothing).
        """
        if self._caps is not None:
            return self._caps
        caps = {"ether"}
        try:
            for _oid, _name, typ in self.interface_choices():
                t = (typ or "").lower()
                if "lte" in t:
                    caps.add("lte")
                if "w60" in t or "60g" in t:
                    caps.add("w60g")
                elif any(k in t for k in ("wlan", "wifi", "wireless")):
                    caps.add("wireless")
        except Exception:  # noqa: BLE001
            return set()  # unknown -> caller shows every mode
        self._caps = caps
        return caps

    def _resolver(self, fld: sch.Field, value: Any) -> str | None:
        if not fld.enum_path:
            return None
        try:
            if isinstance(value, (bytes, bytearray)) and len(value) <= 8:
                iv = int.from_bytes(value, "little")
            else:
                iv = int(value)
        except (TypeError, ValueError):
            return None
        names = self.enum_map(fld.enum_path)
        if iv in names:
            return names[iv]
        return f"#{iv}"

    def enum_choices(self, fld: sch.Field) -> list[tuple[Any, str]]:
        """Options for a combo box: (wire_value, label).

        Order is the .jg's business. ``sorted:1`` asks for alphabetical (54
        string fields do), ``sortbyvalue:1`` for wire order (68 enums), and
        anything else keeps the order the values arrived in -- which for a live
        list is the device's own, and for a static map is how it was declared.
        """
        def live(mapping: dict) -> list[tuple[Any, str]]:
            items = list(mapping.items())
            if fld.sort_alpha:
                items.sort(key=lambda kv: str(kv[1]).lower())
            elif fld.sort_by_value:
                items.sort(key=lambda kv: kv[0])
            return items

        if fld.enum and fld.enum_path:
            # A `pair` enum: fixed entries (none / self) plus a live list
            # (interfaces). Show the static ones first, then the dynamic.
            out: list[tuple[Any, str]] = list(fld.enum.items())
            seen = {k for k, _ in out}
            try:
                dyn = (
                    self.enum_source_map(fld.enum_path, fld.enum_pred)
                    if fld.enum_pred else self.enum_map(fld.enum_path)
                )
            except Exception:  # noqa: BLE001
                dyn = {}
            for k, v in live(dyn):
                if k not in seen:
                    out.append((k, v))
            return out
        if fld.enum:
            return list(fld.enum.items())
        if fld.enum_path:
            mp = (
                self.enum_source_map(fld.enum_path, fld.enum_pred)
                if fld.enum_pred else self.enum_map(fld.enum_path)
            )
            pairs = live(mp)
            # A string field names its target rather than referencing its id.
            # Firewall Chain is `s`-prefixed with a source table of chains: the
            # device wants b'input', and sending the row id put a rule in a
            # chain literally called "1".
            if fld.prefix == "s" or fld.jg_type == "string":
                return [(label, label) for _oid, label in pairs]
            return [(k, v) for k, v in pairs]
        if fld.jg_type in ("bool", "flag"):
            return [(False, "false"), (True, "true")]
        return []

    # --- reads --------------------------------------------------------

    def alloc_from(self) -> tuple[int, int]:
        """Allocate a unique SYS_FROM path for a UI window / subscription."""
        with self._lock:
            n = self._from_seq
            self._from_seq = n + 1 if n < 0xFF00 else 200
            return (0, n)

    def _build_request(
        self,
        path: tuple[int, ...],
        command: int,
        *,
        from_path: tuple[int, ...] | None = None,
        extra: Callable[[m.Message], None] | None = None,
    ) -> m.Message:
        """One request frame, ready to send.

        Callers hold ``_lock``: the request id this consumes has to be unique
        across every thread sharing the socket, since that id is what routes
        the reply back to the caller that asked.
        """
        msg = (
            m.Message()
            .add_u32_array(m.SYS_TO, list(path))
            .add_u32_array(m.SYS_FROM, list(from_path or DEFAULT_FROM))
            .set_command(command)
            .set_request_id(self.session.next_request_id())
            .set_reply_expected(True)
        )
        # Firmware 7 expects a client identity on authenticated requests
        # (Winbox 4 omits it; we always send one — see client_ident.py).
        msg.add_string_array(
            CLIENT_IDENT_FIELD, [self.client_ident.encode("utf-8")]
        )
        if extra is not None:
            extra(msg)
        return msg

    def pipeline(
        self,
        items: list[tuple],
        *,
        timeout: float | None = None,
        quiet: bool = False,
    ) -> list[Any]:
        """Send several requests as one burst and collect the replies in order.

        Each item is ``(path, command)`` or ``(path, command, extra)``. Replies
        come back positionally; one the device refused (or that timed out)
        yields its :class:`DeviceError` in that slot rather than sinking the
        whole batch.

        This is how Winbox reads a form: a session capture has Winbox
        3.43 opening a wifi interface with 21 frames sent in 6.5 ms and every
        reply in by 15.9 ms. One at a time -- send, wait, send, wait -- the same
        replies sum to about 100 ms of device time, before any of our own
        overhead, which is why a form used to take about a second to appear.

        Needs the frame demux to route a burst's replies. Without one there is
        nothing to match reply to request, so it falls back to sending them in
        turn: same answers, same order, just no better than before.
        """
        if self.session is None:
            raise DeviceError("not connected")
        if not items:
            return []
        demux = getattr(self.session, "demux", None)
        if demux is None:
            out: list[Any] = []
            for it in items:
                extra = it[2] if len(it) > 2 else None
                try:
                    out.append(
                        self._request(it[0], it[1], extra=extra, quiet=quiet)
                    )
                except DeviceError as e:
                    out.append(e)
            return out

        built: list[tuple[tuple[int, ...], int, m.Message]] = []
        with self._lock:
            for it in items:
                path = tuple(it[0])
                command = it[1]
                extra = it[2] if len(it) > 2 else None
                msg = self._build_request(path, command, extra=extra)
                msg.audit_poll = quiet
                if not quiet and self.session_log is not None:
                    self.session_log.request(
                        path, command, self._log_fields(msg, path)
                    )
                built.append((path, command, msg))
            replies = demux.pipeline(
                [msg for _p, _c, msg in built], timeout=timeout
            )

        out = []
        for (path, command, _msg), reply in zip(built, replies):
            where = ",".join(str(x) for x in path)
            log = None if quiet else self.session_log
            if isinstance(reply, BaseException):
                if log is not None:
                    log.error(path, command, str(reply))
                # Handed back as-is: a caller has to be able to tell "the device
                # said no" (DeviceError, below) from "nothing came back at all",
                # which is what a handler the firmware does not implement does
                # to a request -- it drops it in silence.
                out.append(reply)
                continue
            if reply.has_error():
                detail = reply.error_string() or "request failed"
                if log is not None:
                    log.error(path, command, detail)
                out.append(
                    DeviceError(f"{detail} [to=({where}) cmd={command:#x}]")
                )
                continue
            if log is not None:
                log.reply(path, command, self._log_fields(reply, path))
            out.append(reply)
        return out

    def _request(
        self,
        path: tuple[int, ...],
        command: int,
        *,
        from_path: tuple[int, ...] | None = None,
        extra: Callable[[m.Message], None] | None = None,
        quiet: bool = False,
    ) -> m.Message:
        """Send one request. ``quiet`` keeps 1 Hz polling out of the log."""
        if self.session is None:
            raise DeviceError("not connected")
        if not path:
            raise DeviceError("empty handler path")
        with self._lock:
            msg = self._build_request(
                path, command, from_path=from_path, extra=extra
            )
            # Same signal the transcript uses, carried down to the audit
            # recorder: `quiet` marks the 1 Hz refreshes, and only the caller
            # knows which reads those are.
            msg.audit_poll = quiet
            log = None if quiet else self.session_log
            if log is not None:
                log.request(path, command, self._log_fields(msg, path))
            elif self.session_log is not None:
                self.session_log.skip()
            try:
                reply = self.session.request(msg)
            except (TimeoutError, OSError) as e:
                where = ",".join(str(x) for x in path)
                if log is not None:
                    log.error(path, command, f"timeout: {e}")
                raise DeviceError(
                    f"timeout talking to device [to=({where}) cmd={command:#x}]: {e}"
                ) from e
            if reply.has_error():
                where = ",".join(str(x) for x in path)
                cmd = f"{command:#x}"
                detail = reply.error_string() or "request failed"
                if log is not None:
                    log.error(path, command, detail)
                raise DeviceError(f"{detail} [to=({where}) cmd={cmd}]")
            if log is not None:
                log.reply(path, command, self._log_fields(reply, path))
            return reply

    @staticmethod
    def _looks_secret(name: str | None) -> bool:
        """Last-resort mask by name, for fields the schema does not cover."""
        n = (name or "").lower()
        return "password" in n or "passphrase" in n or n.endswith("secret")

    def _log_fields(
        self, msg: m.Message, path: tuple[int, ...] | None = None
    ) -> list[tuple[str, Any]]:
        """Name/value pairs for the transcript, using schema names when known.

        Secrets are masked. The transcript is a plain file kept per session, and
        a password change would otherwise write both the old and the new
        password into it in cleartext -- a durable copy of the very thing the
        form masks on screen.
        """
        names: dict[int, str] = {}
        secret: set[int] = set()

        def mark(code: int, field) -> None:
            # An opt-wrapped secret (a tunnel's IPsec Secret) sends its value
            # under the leaf's own code, as a sibling of the opt's, and that
            # code has no name of its own for the fallback below to catch.
            leaf = sch.secret_leaf(field)
            if leaf is not None:
                secret.update((code, leaf.code))

        base = rec = None
        if self.schema is not None and path:
            try:
                base = self.schema.record(tuple(path))
                # An interface declares its secrets on the *subtype*: a
                # tunnel's IPsec Secret, a PPPoE client's Password, an LTE PIN.
                # The base record at (20, 0) has no field for any of them, so
                # without the overlay their codes are nameless here and the
                # name fallback below cannot see them either. The row says
                # which type it is; a message that carries no type keeps the
                # base, and its subtype fields stay unnamed as before.
                rec = base
                if base is not None and base.generic:
                    row = {c: v for c, _tag, v in msg}
                    rec = self.schema.record_for(
                        tuple(path), sch.wire_type_value(msg, base), row
                    ) or base
                if rec is not None:
                    for c, f in (rec.fields or {}).items():
                        if f.name:
                            names[c] = f.name
                        mark(c, f)
            except Exception:
                names = {}
                secret = set()
            # The record covers the map at this path; a command menu on the
            # same path (System ▸ Password is a doit on (13,1)) has its own
            # fields, and those are the ones carrying passwords.
            try:
                for menu in self.schema.by_path.get(tuple(path)) or ():
                    for c, f in (menu.fields or {}).items():
                        names.setdefault(c, f.name or "")
                        mark(c, f)
            except Exception:
                pass
            # A write carries only the fields being written, so it has no type
            # slot for the overlay above to resolve -- and a write is exactly
            # when a new password is in the message. Where a code is nameless
            # at this path and holds a secret in *some* subtype of the family,
            # take it as one: over-masking a sibling subtype's field in a
            # transcript is far cheaper than writing a password to disk.
            if rec is not None and rec is base and base.generic:
                try:
                    for c in self.schema.family_secret_codes(base.generic):
                        if not names.get(c):
                            secret.add(c)
                except Exception:
                    pass
            names = {c: n for c, n in names.items() if n}
        out: list[tuple[str, Any]] = []
        try:
            for code, _tag, value in msg:
                if code in secret or self._looks_secret(names.get(code)):
                    value = "<hidden>"
                elif isinstance(value, (bytes, bytearray)) and len(value) > 64:
                    value = f"<{len(value)} bytes>"
                out.append((names.get(code) or f"{code:#x}", value))
        except Exception:
            pass
        return out

    def getall(
        self,
        path: tuple[int, ...],
        *,
        filter_mask: int | None = None,
    ) -> list[RowObject]:
        """GETALL (0xFE0004). Falls back to GET when the handler is not a list.

        ``filter_mask`` is the Winbox field mask (0xfe000c). For Interface
        traffic columns use ``FILTER_IFACE_TRAFFIC`` / ``FILTER_IFACE_TRAFFIC_FULL``.
        """
        def add_filter(msg: m.Message) -> None:
            if filter_mask is not None:
                msg.add_u32(STD_FILTER, filter_mask & 0xFFFFFFFF)
                # Winbox list GETALL also sends fe0018=0 on the static mask path
                if filter_mask == FILTER_IFACE_STATIC:
                    msg.add_u32(0xFE0018, 0)

        try:
            reply = self._request(path, CMD_GETALL, extra=add_filter)
        except DeviceError as e:
            # Single-object / settings menus often reject GETALL with NOTIMP.
            if "NOTIMP" in str(e) or hex(ERR_NOTIMP) in str(e).lower() or "0xfe0003" in str(e).lower():
                try:
                    one = self.get(path)
                    return [one]
                except DeviceError:
                    raise e from None
            raise
        objs = reply.find(STD_OBJS)
        items = objs[1] if objs and isinstance(objs[1], list) else []
        rows: list[RowObject] = []
        for item in items:
            if not isinstance(item, m.Message):
                continue
            rows.append(self._row_from_msg(path, item))
        return rows

    def get(
        self,
        path: tuple[int, ...],
        *,
        quiet: bool = False,
        menu: "sch.Menu | None" = None,
    ) -> RowObject:
        """GET (0xFE000D) -- the one object a settings handler holds.

        ``menu`` is which of the menus at this path the caller is showing, and
        it decides how the reply is read back. (20,17) is a single object that
        six unrelated dialogs each show a disjoint slice of, so a reply decoded
        without it lands under whichever menu the path heuristic picks -- L3 Hw
        Settings, whose 48 codes include none of Bridge Settings' nine. The
        form then draws `<0x00003d>` where the .jg says "Use IP Firewall".
        """
        reply = self._request(path, CMD_GET, quiet=quiet)
        return self._row_from_msg(path, reply, menu=menu)

    #: System Resources handler and the field holding uptime in whole seconds.
    #: Not 0x0a (Total HDD Size) and not 0x2e (jiffies, uptime x100).
    RESOURCE_PATH = (24, 2)
    UPTIME_CODE = 0x0001

    #: How long a reading of the device clock may be carried on the local one
    #: before it is read again.
    UPTIME_TTL = 30.0

    def uptime(self, *, refresh: bool = True) -> float | None:
        """Device uptime in seconds — the clock ``type:'age'`` stamps count on.

        Cached so row decoding can reference it without a round trip per row;
        pass ``refresh=False`` to read the cache only.

        A list that reads its age columns once a second would otherwise put a
        GET on (24,2) between every read, to re-learn a clock that advances at
        one second per second. Inside :data:`UPTIME_TTL` the cached reading is
        carried forward on the local monotonic clock instead, and the device is
        asked again once it has had time to drift.
        """
        if not refresh:
            return self._uptime
        now = time.monotonic()
        if (
            self._uptime is not None
            and self._uptime_at is not None
            and 0 <= now - self._uptime_at < self.UPTIME_TTL
        ):
            self._uptime += now - self._uptime_at
            self._uptime_at = now
            return self._uptime
        try:
            reply = self._request(self.RESOURCE_PATH, CMD_GET, quiet=True)
        except DeviceError:
            return self._uptime
        for code, _tag, val in reply:
            if code == self.UPTIME_CODE and isinstance(val, int):
                self._uptime = float(val)
                self._uptime_at = time.monotonic()
                return self._uptime
        return self._uptime

    def getobj(
        self,
        path: tuple[int, ...],
        obj_id: int,
        *,
        filter_mask: int | None = FILTER_IFACE_TRAFFIC,
        from_path: tuple[int, ...] | None = None,
        quiet: bool = False,
        menu: "sch.Menu | None" = None,
    ) -> RowObject:
        """GETOBJ (0xFE0002) — one list object by `.id`.

        Used for property forms and 1 Hz Traffic-tab polling. On Interface
        (20,0), plain GET is NOTIMP; GETOBJ with a traffic filter mask is what
        Winbox uses for live Rx/Tx rates.
        """
        def add_fields(msg: m.Message) -> None:
            msg.add_u32(STD_OBJ_ID, obj_id & 0xFFFFFFFF)
            if filter_mask is not None:
                msg.add_u32(STD_FILTER, filter_mask & 0xFFFFFFFF)

        reply = self._request(
            path, CMD_GETOBJ, from_path=from_path, extra=add_fields, quiet=quiet
        )
        # ``menu``: same reason as :meth:`get` -- which menu at this path the
        # caller is showing decides how the reply reads.
        row = self._row_from_msg(path, reply, menu=menu)
        # The device does not echo the object id in a GETOBJ reply — it
        # answers the question, not which row it was about. Callers replace
        # the row they had with this one, so without putting the id back the
        # object silently loses its identity: a property form opened on an
        # existing rule then had no .id, and saving it ADDed a duplicate
        # instead of editing.
        try:
            if row.values_by_code.get(STD_OBJ_ID) is None:
                row.values_by_code[STD_OBJ_ID] = int(obj_id)
        except Exception:  # noqa: BLE001
            pass
        return row

    # --- the write path ------------------------------------------------
    #
    # ADD / REMOVE / MOVE / SET, captured 2026-08-06 against firmware 7.21.1 and
    # captured on the wire. All four select the record type with
    # 0xFE000C -- the same mask a full GETALL sends, which the schema derives
    # (Menu.write_mask) rather than this layer guessing at it.
    #
    # Every one of these refuses to run while `self.readonly` is set. That flag
    # is the last line before the wire: the UI has its own mode switch and its
    # own Safe Mode guard, but a client constructed read-only must not write no
    # matter what calls it.

    def _write_mask(
        self, path: tuple[int, ...], override: int | None = None
    ) -> int:
        if override is not None:
            return override & 0xFFFFFFFF
        menu = self.menu_for(path)
        return (menu.write_mask if menu is not None else sch.STD_MASK_BASE)

    def _guard_write(self, verb: str, path: tuple[int, ...]) -> None:
        if self.readonly:
            where = ",".join(str(x) for x in path)
            raise DeviceError(
                f"refusing {verb} on ({where}): this client is read-only"
            )

    def _forget_enums(self, path: tuple[int, ...]) -> None:
        """Drop the cached options for a menu this session just wrote to.

        Enum source tables are cached for the life of the session (they back
        every dropdown in every form), so adding a VLAN would otherwise leave it
        missing from the interface pickers until reconnect.
        """
        p = tuple(path)
        self._enum_cache.pop(p, None)
        self._enum_rows.pop(p, None)
        self._enum_marks_cache.pop(p, None)
        self._enum_read_at.pop(p, None)
        for key in [k for k in self._enum_pred_cache if k[0] == p]:
            self._enum_pred_cache.pop(key, None)
        if p == tuple(sch.IFACE_ENUM_PATH):
            self._iface_choices = None
            self._caps = None

    def _note_safe_mode_change(self) -> None:
        """Tell the Safe Mode buffer a reversible change just landed.

        Handler [17] never reports how deep its buffer is, and undo does not
        say when it has hit the bottom, so the count has to be kept here or
        `leave(keep_changes=False)` would not know how far to wind back.
        """
        if self._safe_mode is not None and self._safe_mode.session_id is not None:
            self._safe_mode.note_change()

    def _encode(
        self, path: tuple[int, ...], code: int, value: Any
    ) -> tuple[int, Any]:
        """A field value as ``(tag, value)``, typed by the schema.

        A caller that already knows the wire tag passes ``(tag, value)`` through
        untouched -- that is how a re-sent raw value keeps its exact encoding
        instead of being round-tripped through display text.
        """
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], int):
            return value
        fld = self.field_for(path, code)
        if fld is None:
            # No schema for this code: fall back to the shape of the value.
            if isinstance(value, bool):
                return (m.T_BOOL_TRUE if value else m.T_BOOL_FALSE), value
            if isinstance(value, int):
                return (m.T_U64 if value > 0xFFFFFFFF else m.T_U32), value
            if isinstance(value, (bytes, bytearray)):
                return m.T_RAW, bytes(value)
            return m.T_STRING, "" if value is None else str(value)
        if fld.jg_type == "addr":
            # A record's addr is {kind, prefix, interface, address} with the
            # IPv4 low byte first; the encoder writes Ping To's bare,
            # network-order shape. Sent to a Route that would aim the gateway
            # at 1.113.0.203 instead of 203.0.113.1. Refused until a real
            # Winbox write of one has been captured.
            raise DeviceError(
                f"{fld.name}: editing this address field is not supported yet"
            )
        return sch.encode_form_value(fld, value)

    def _write_fields(
        self, msg: m.Message, path: tuple[int, ...], values: dict[int, Any]
    ) -> None:
        for code, value in values.items():
            tag, val = self._encode(path, int(code), value)
            msg.add_typed(int(code), tag, val)

    def set_obj(
        self,
        path: tuple[int, ...],
        obj_id: int,
        values: dict[int, Any],
        *,
        mask: int | None = None,
        from_path: tuple[int, ...] | None = None,
    ) -> RowObject | None:
        """SETOBJ (0xFE0003) — change fields of one existing object.

        Only the fields in ``values`` are sent; Winbox re-sends a few unchanged
        ones alongside, which the device tolerates either way. Returns the
        object read back with GETOBJ, or None when the handler will not serve
        one (settings-style menus have no per-object read).
        """
        self._guard_write("SETOBJ", path)
        if not values:
            raise DeviceError("SETOBJ with no fields to set")

        def add_fields(msg: m.Message) -> None:
            msg.add_u32(STD_FILTER, self._write_mask(path, mask))
            msg.add_u32(STD_OBJ_ID, int(obj_id) & 0xFFFFFFFF)
            self._write_fields(msg, path, values)

        self._request(path, CMD_SETOBJ, from_path=from_path, extra=add_fields)
        self._note_safe_mode_change()
        self._forget_enums(path)
        return self._read_back(path, int(obj_id), from_path=from_path)

    #: The Users collection. System ▸ Password is a command on it.
    USERS_PATH = (13, 1)

    def current_user_id(self) -> int | None:
        """Object id of the account this session authenticated as."""
        try:
            rows = self.getall(self.USERS_PATH)
        except Exception:  # noqa: BLE001
            return None
        for row in rows:
            name = row.values_by_code.get(0x000001)
            if isinstance(name, (bytes, bytearray)):
                name = name.decode("utf-8", errors="replace")
            if name == self.user:
                try:
                    return row.object_id()
                except Exception:  # noqa: BLE001
                    return None
        return None

    def default_command_target(self, path: tuple[int, ...]) -> int | None:
        """Which object a doit acts on when the user picked no row.

        System ▸ Password is a command on the Users collection opened from the
        menu tree, so there is no selected row -- but the object it means is
        the account this session logged in as. Nothing else has a sensible
        default; sending 0 is what produced UNKNOWNID.
        """
        if tuple(path) == self.USERS_PATH:
            return self.current_user_id()
        # A singleton menu's one object is id 0. Captured from Winbox running
        # IP ▸ Cloud's Force Update: [123,1] cmd=0x1 with 0xFE0001 = 0.
        menu = None
        if self.schema is not None:
            try:
                menu = self.menu_for(tuple(path))
            except Exception:  # noqa: BLE001
                menu = None
        if menu is not None and not getattr(menu, "is_collection", True):
            return 0
        return None

    def get_file(self, name: str, *, timeout: float | None = None) -> bytes:
        """Read a file out of the device's filespace via mproxy (2,2).

        The chunked open-and-read Winbox uses on the Files list. The one-shot
        fetch is the plugin path, and refused every file a user had put there.
        """
        if self.session is None:
            raise DeviceError("not connected")
        try:
            return self.session.download_file(name, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"download {name!r} failed: {e}") from e

    def put_file(
        self, name: str, data: bytes, *, timeout: float | None = None
    ) -> None:
        """Write a file into the device's filespace via mproxy (2,2).

        A write, so it is refused while this client is read-only — the same
        guard every other mutation goes through.
        """
        self._guard_write("UPLOAD", (2, 2))
        if self.session is None:
            raise DeviceError("not connected")
        try:
            self.session.upload_file(name, data, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise DeviceError(f"upload {name!r} failed: {e}") from e

    def watch(
        self,
        path: tuple[int, ...],
        *,
        from_path: tuple[int, ...] | None = None,
        mask: int | None = None,
        interval_ms: int | None = None,
    ) -> tuple[Any, tuple[int, ...]]:
        """SUBSCRIBE (0xFE0012) — have the device push this handler's changes.

        Returns ``(subscription, from_path)``. The device streams unsolicited
        NOTIFY (0xFE000B) frames addressed to ``from_path`` until
        :meth:`unwatch`, and the demux routes them to the subscription. This is
        how a view stays current without polling, including when the change came
        from another session entirely.

        The changed records ride in the *nested* 0xFE0002 array, not at the top
        level of the push.

        ``interval_ms`` turns the subscription into the 1 Hz feed Winbox runs a
        list on: the device then pushes the records the ``mask`` selects every
        so many milliseconds, whether or not anything changed, which is where
        the Interface list's live Tx/Rx comes from. The mask that goes with it
        is the menu's bare ``refreshfilter`` (0x4000A on Interface, 5 on a menu
        that declares none) -- not the wider one a full GETALL sends.
        """
        if self.session is None:
            raise DeviceError("not connected")
        demux = self.session.demux
        if demux is None:
            raise DeviceError("a subscription needs the frame demux")
        if from_path is None:
            from_path = self.alloc_from()
        sub = demux.subscribe(tuple(from_path))

        def add_fields(msg: m.Message) -> None:
            # Winbox's own order: the flag, the record mask, then the period.
            if interval_ms is not None:
                msg.add_bool(SUB_PUSH_DATA, True)
            if mask is not None:
                msg.add_u32(STD_FILTER, mask & 0xFFFFFFFF)
            if interval_ms is not None:
                msg.add_u32(SUB_INTERVAL_MS, max(0, int(interval_ms)))

        try:
            self._send_oneway(
                path, CMD_SUBSCRIBE, from_path=from_path,
                extra=(
                    add_fields
                    if (mask is not None or interval_ms is not None)
                    else None
                ),
            )
        except Exception:
            demux.unsubscribe(sub)
            raise
        return sub, tuple(from_path)

    def unwatch(
        self, path: tuple[int, ...], sub: Any, *, from_path: tuple[int, ...]
    ) -> None:
        """UNSUBSCRIBE (0xFE0013). Safe to call on a session that has died."""
        try:
            self._send_oneway(path, CMD_UNSUBSCRIBE, from_path=from_path)
        except Exception:  # noqa: BLE001
            pass
        try:
            demux = self.session.demux if self.session is not None else None
            if demux is not None:
                demux.unsubscribe(sub)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def notify_rows(msg: Any) -> list[Any]:
        """The records carried by a NOTIFY push.

        A 0xFE000B frame only routes; the changed records are one level down in
        the 0xFE0002 message array. Reading the top level finds nothing, which
        is the same trap the .mitm.log tooling note in §11 warns about.
        """
        try:
            hit = msg.find(STD_OBJS)
        except Exception:  # noqa: BLE001
            return []
        if not hit or not isinstance(hit[1], list):
            return []
        return list(hit[1])

    def notify_objects(self, path: tuple[int, ...], msg: Any) -> list[RowObject]:
        """A push decoded against the schema, the way GETALL rows are.

        Empty for a push that carries no records -- some are pure "look
        again" -- and callers should read the list rather than assume nothing
        changed.
        """
        out: list[RowObject] = []
        for item in self.notify_rows(msg):
            if isinstance(item, m.Message):
                try:
                    out.append(self._row_from_msg(path, item))
                except Exception:  # noqa: BLE001
                    continue
        return out

    def list_column_defs(self, menu: "sch.Menu") -> list["sch.Column"]:
        """The column set :meth:`table_rows` renders, for a caller that wants
        to re-render one row later without asking for the whole table."""
        return sch.list_columns(menu)

    def render_row(
        self,
        menu: "sch.Menu",
        obj: RowObject,
        *,
        cols: "list[sch.Column] | None" = None,
        index: int | None = None,
        titles: dict | None = None,
    ) -> list[str]:
        """One row's cells, exactly as :meth:`table_rows` would build them.

        ``index`` is the device-order row number an ordered table shows in its
        second column; leave it None for a table that has no such column.
        """
        if cols is None:
            cols = self.list_column_defs(menu)
        if titles is None:
            titles = {}
        flags = sch.format_flag_letters(menu, obj.values_by_code)
        cells = [self._column_text(col, obj, menu, titles) for col in cols]
        return [flags] + ([str(index)] if index is not None else []) + cells

    def set_settings(
        self,
        path: tuple[int, ...],
        values: dict[int, Any],
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        """Write fields of a single-object menu — System ▸ Identity, IP ▸ Cloud.

        Command 0xFE000E carrying just the changed fields: no object id, no
        request id, no reply expected. That is the shape captured from Winbox
        writing /system package update's Channel, which is a settings menu of
        the same kind (see package_update_set).

        These menus hold one object that has no ``.id`` — it is absent from the
        GET reply too — so SETOBJ has nothing to key on and the device answers
        UNKNOWNID, which is exactly what it did.

        The device acknowledges nothing here: it applies the change and reports
        the new value on the next GET. Callers must read it back rather than
        assume success.
        """
        self._guard_write("SET", path)
        if not values:
            raise DeviceError("SET with no fields to set")

        def add_fields(msg: m.Message) -> None:
            self._write_fields(msg, path, values)

        self._send_oneway(path, CMD_SET, from_path=from_path, extra=add_fields)
        self._note_safe_mode_change()
        self._forget_enums(path)

    def run_command(
        self,
        path: tuple[int, ...],
        command: int,
        values: dict[int, Any] | None = None,
        *,
        obj_id: int | None = None,
        from_path: tuple[int, ...] | None = None,
    ) -> m.Message:
        """Run a schema ``kind:'doit'`` command, e.g. System ▸ Password.

        These are one-shot actions rather than edits to a row, and they carry
        their arguments as ordinary fields.

        ``obj_id`` is the object the command acts on, and is omitted when None.
        Do not pass 0 as a stand-in for "no object": on a collection handler
        the device looks the id up and answers UNKNOWNID, which is what a
        password change did when it sent 0 against the Users map.

        The reply is returned as-is: a doit answers with an error string on
        refusal (``Bad old password!``), which _request already raises, and
        otherwise has nothing interesting to say.
        """
        self._guard_write("COMMAND", path)

        def add_fields(msg: m.Message) -> None:
            if obj_id is not None:
                msg.add_u32(STD_OBJ_ID, int(obj_id) & 0xFFFFFFFF)
            if values:
                self._write_fields(msg, path, values)

        reply = self._request(
            path, int(command), from_path=from_path, extra=add_fields
        )
        self._note_safe_mode_change()
        self._forget_enums(path)
        return reply

    def add_obj(
        self,
        path: tuple[int, ...],
        values: dict[int, Any],
        *,
        mask: int | None = None,
        from_path: tuple[int, ...] | None = None,
    ) -> tuple[int | None, RowObject | None]:
        """ADDOBJ (0xFE0005) — create an object; returns ``(new_id, row)``.

        The device allocates the id and hands it back in 0xFE0001. Winbox then
        immediately GETOBJs it to render the row, because the reply itself
        carries nothing else -- computed fields like an address's broadcast
        only exist after the read-back.
        """
        self._guard_write("ADDOBJ", path)

        def add_fields(msg: m.Message) -> None:
            # Winbox sends this on every ADD; it means "create enabled".
            if STD_DISABLED not in values:
                msg.add_bool(STD_DISABLED, False)
            msg.add_u32(STD_FILTER, self._write_mask(path, mask))
            self._write_fields(msg, path, values)

        reply = self._request(path, CMD_ADDOBJ, from_path=from_path, extra=add_fields)
        self._note_safe_mode_change()
        self._forget_enums(path)
        new_id = reply.get_u32(STD_OBJ_ID)
        row = (
            self._read_back(path, int(new_id), from_path=from_path)
            if new_id is not None else None
        )
        return (int(new_id) if new_id is not None else None), row

    def remove_obj(
        self,
        path: tuple[int, ...],
        obj_id: int,
        *,
        mask: int | None = None,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        """REMOVEOBJ (0xFE0006) — delete by `.id`. Smallest frame in the protocol."""
        self._guard_write("REMOVEOBJ", path)

        def add_fields(msg: m.Message) -> None:
            msg.add_u32(STD_FILTER, self._write_mask(path, mask))
            msg.add_u32(STD_OBJ_ID, int(obj_id) & 0xFFFFFFFF)

        self._request(path, CMD_REMOVEOBJ, from_path=from_path, extra=add_fields)
        self._note_safe_mode_change()
        self._forget_enums(path)

    def move_obj(
        self,
        path: tuple[int, ...],
        obj_id: int,
        *,
        before: int | None = None,
        mask: int | None = None,
        from_path: tuple[int, ...] | None = None,
    ) -> None:
        """MOVEOBJ (0xFE0007) — reorder within an ordered table.

        ``before`` is the anchor to insert ahead of; omit it to move to the end.
        Only ordered menus implement this — firewall rules are order-sensitive,
        IP addresses are not — and the device rejects a move of a builtin row
        with ``cannot move builtin``.
        """
        self._guard_write("MOVEOBJ", path)

        def add_fields(msg: m.Message) -> None:
            msg.add_u32(STD_FILTER, self._write_mask(path, mask))
            msg.add_u32(STD_OBJ_ID, int(obj_id) & 0xFFFFFFFF)
            if before is not None:
                msg.add_u32(MOVE_ANCHOR, int(before) & 0xFFFFFFFF)

        self._request(path, CMD_MOVEOBJ, from_path=from_path, extra=add_fields)
        self._note_safe_mode_change()
        self._forget_enums(path)

    def _read_back(
        self,
        path: tuple[int, ...],
        obj_id: int,
        *,
        from_path: tuple[int, ...] | None = None,
    ) -> RowObject | None:
        """GETOBJ after a write, the way Winbox does. None if unsupported.

        A failed read-back does not undo the write, so it must not be reported
        as a failed write: the caller gets None and the row refreshes on the
        next list poll.
        """
        try:
            return self.getobj(
                path, obj_id, filter_mask=self._write_mask(path), from_path=from_path
            )
        except DeviceError:
            return None

    def rerender(self, row: RowObject) -> RowObject:
        """The same object, decoded again.

        For when a display preference changed how a value reads but not what
        the device holds -- byte units, say. Everything on screen was rendered
        when the object arrived, and re-reading the device to restate it would
        be a request that answers nothing new.
        """
        return self._row_from_msg(row.path, row.raw)

    def _row_from_msg(
        self,
        path: tuple[int, ...],
        msg: m.Message,
        *,
        menu: "sch.Menu | None" = None,
    ) -> RowObject:
        values: dict[int, Any] = {}
        multi: dict[int, list[tuple[int, Any]]] = {}
        for code, tag, value in msg:
            # Keep object .id and other mid-range system fields; still skip
            # pure framing (SYS_TO/FROM/CMD/…).
            if code in (m.SYS_TO, m.SYS_FROM, m.SYS_COMMAND, m.SYS_REQUEST_ID,
                        m.SYS_REPLY_EXPECTED, CLIENT_IDENT_FIELD, 0xFF0003):
                continue
            if code >= 0xFF0000 and code not in (STD_OBJ_ID,):
                continue
            values[code] = value
            multi.setdefault(code, []).append((tag, value))
        fields: list[tuple[int, str, str, bool]] = []
        form_fields: list[tuple[str, int, str, str, bool]] = []
        if self.schema is not None:
            fields = self.schema.decode(
                path, msg, resolver=self.enum_map, uptime=self._uptime,
                menu=menu,
            )
            form_fields = self.schema.decode_form(
                path, msg, resolver=self.enum_map, uptime=self._uptime,
                menu=menu,
            )
        else:
            for code, value in values.items():
                fields.append((code, f"<{code:#08x}>", repr(value), True))
                form_fields.append(("General", code, f"<{code:#08x}>", repr(value), True))
        return RowObject(
            path=path,
            raw=msg,
            fields=fields,
            form_fields=form_fields,
            values_by_code=values,
            values_by_code_multi=multi,
        )

    def menu_for(
        self,
        path: tuple[int, ...],
        type_value: int | None = None,
        *,
        values: dict[int, Any] | None = None,
        family: str | None = None,
        name: str | None = None,
    ) -> sch.Menu | None:
        """Schema record for a menu, specialised as far as we can tell.

        ``family`` names an overlay namespace instead of one type -- Wireless ▸
        WiFi Interfaces lists every wireless type at once, so its columns come
        from the family's own overlay. ``values`` is the object's wire row,
        which lets nested overlays resolve past the first level (an interface's
        type says 'Atheros'; its ``hwtype`` says which one).

        ``name`` is the `.jg` ``name`` of the menu wanted, for the paths where
        several live (IP ▸ Settings and IPv6 ▸ Settings are both (20,17)); see
        :meth:`Schema.record`.
        """
        if self.schema is None:
            return None
        if family:
            return self.schema.record_for_family(path, family, name=name)
        if type_value is None:
            return self.schema.record(path, name=name)
        return self.schema.record_for(path, type_value, values, name=name)

    def field_for(self, path: tuple[int, ...], code: int, name: str | None = None) -> sch.Field | None:
        menu = self.menu_for(path)
        if menu is None:
            return None
        base = menu.fields.get(code)
        if base is None:
            return None
        if name and base.name != name and name in {base.name, *(v.name for v in base.variants.values())}:
            if base.name == name:
                return base
            for v in base.variants.values():
                if v.name == name:
                    return v
        return base

    def nav_tree(self) -> list[sch.NavNode]:
        if self.schema is None:
            return []
        return self.schema.nav

    # --- Safe Mode (firmware 7.18+ via terminal /safe-mode/*) ---------------

    def _safe_channel(self):
        if self.session is None:
            raise DeviceError("not connected")
        if self._safe_mode is None:
            from .safemode import SafeModeChannel

            # Handler [17] speaks plain M2, so it goes through _request like
            # every other menu: same transcript, same error envelope. (_lock is
            # an RLock, so the callers below may hold it across this.)
            self._safe_mode = SafeModeChannel(
                self._request, from_path=self.alloc_from()
            )
        return self._safe_mode

    def safe_mode_status(self):
        """Query Safe Mode state (enabled/current/owner)."""
        from .safemode import SafeModeError

        with self._lock:
            try:
                st = self._safe_channel().status()
            except SafeModeError as e:
                raise DeviceError(str(e)) from e
            self.safe_mode_active = st.active_here
            return st

    def safe_mode_enter(self):
        """Take Safe Mode for this session (changes unrolled on disconnect)."""
        from .safemode import SafeModeError

        with self._lock:
            try:
                st = self._safe_channel().take()
            except SafeModeError as e:
                raise DeviceError(str(e)) from e
            self.safe_mode_active = st.active_here
            return st

    # --- undo / redo: The router's own history (System > History) ---------

    def history_next(self) -> tuple[str | None, str | None]:
        """What Undo and Redo would act on now, as "action (by, time)".

        The history handler is Safe Mode's, [17], and its GETALL is the
        System > History list, newest first: each row flagged U (undoable) or
        R (redoable). The first U is the next undo, and the last R -- the most
        recently undone, sitting just above it -- the next redo. The router
        undo is router-wide -- it takes back the latest undoable change
        whoever made it -- which is exactly why a button should say what it
        is about to touch.
        """
        from .safemode import SAFE_MODE_PATH

        undo = redo = None
        try:
            rows = self.getall(SAFE_MODE_PATH)
        except DeviceError:
            return None, None
        for row in rows:
            state = row.values_by_code.get(0x4)
            text = row.get_rendered(0x3) or "change"
            by, when = row.get_rendered(0x6), row.get_rendered(0x7)
            label = f"{text} ({', '.join(p for p in (by, when) if p)})"
            if state == 0 and undo is None:
                undo = label
            elif state == 1:
                redo = label
        return undo, redo

    def history_undo(self) -> None:
        """Take back the router's last undoable change (Winbox's Undo)."""
        self._history_step(undo=True)

    def history_redo(self) -> None:
        """Reapply the last undone change (Winbox's Redo)."""
        self._history_step(undo=False)

    def _history_step(self, *, undo: bool) -> None:
        from .safemode import CMD_REDO, CMD_UNDO, SAFE_MODE_PATH, SafeModeError

        self._guard_write("UNDO" if undo else "REDO", SAFE_MODE_PATH)
        with self._lock:
            ch = self._safe_mode
            if ch is not None and ch.session_id is not None:
                # Under Safe Mode the same verbs move its buffer, and its count
                # of pending changes has to move with them.
                try:
                    ch.undo() if undo else ch.redo()
                except SafeModeError as e:
                    raise DeviceError(str(e)) from e
                return
            reply = self._request(SAFE_MODE_PATH, CMD_UNDO if undo else CMD_REDO)
        if reply.has_error():
            raise DeviceError(reply.error_string() or ("undo" if undo else "redo") + " failed")

    def safe_mode_pending(self) -> int:
        """Changes written under the Safe Mode this session holds, if any."""
        ch = self._safe_mode
        return ch.pending if ch is not None and ch.session_id is not None else 0

    def safe_mode_leave(self, *, keep_changes: bool = True):
        """Leave Safe Mode: release (commit) or unroll (rollback)."""
        from .safemode import SafeModeError

        with self._lock:
            ch = self._safe_channel()
            try:
                st = ch.release() if keep_changes else ch.unroll()
            except SafeModeError as e:
                raise DeviceError(str(e)) from e
            self.safe_mode_active = st.active_here
            return st

    def safe_mode_toggle(self):
        """Toggle Safe Mode ownership for this session."""
        from .safemode import SafeModeError

        with self._lock:
            try:
                st = self._safe_channel().toggle()
            except SafeModeError as e:
                raise DeviceError(str(e)) from e
            self.safe_mode_active = st.active_here
            return st

    def read_fingerprint(self) -> DeviceFingerprint:
        """Read the device identity for TOFU / Neighbors: board serial +
        model, else /system/license system-id; plus interface MACs and IPv4s.
        Every read is best-effort — a missing handler just leaves its field empty
        (a virtual router has no /system/routerboard, so it yields a system-id instead)."""
        import struct as _struct

        fp = DeviceFingerprint()

        def _s(row: RowObject, *codes: int) -> str:
            for c in codes:
                v = row.values_by_code.get(c)
                if isinstance(v, (bytes, bytearray)) and v:
                    return v.decode("utf-8", "replace")
                if isinstance(v, str) and v:
                    return v
            return ""

        try:
            # connect() has normally just read this; asking again is two more
            # round trips for an answer we already have.
            info = self.info
            if not (info.board or info.identity):
                info = self.refresh_info()
            fp.model, fp.identity = info.board, info.identity
        except Exception:
            pass

        # The four reads below are independent of each other, so they go out
        # as one burst. Serially they are four round trips, which is nothing
        # on a LAN and most of a second on a WAN link. The licence read is
        # unconditional here where it used to be a fallback -- in a burst an
        # extra request costs a frame, not a round trip.
        try:
            rb_msg, lic_msg, ifaces_msg, addrs_msg = self.pipeline([
                ((24, 17), CMD_GET),
                ((65,), CMD_GET),
                ((20, 0), CMD_GETALL),
                ((20, 1), CMD_GETALL),
            ], quiet=True)
        except DeviceError:
            rb_msg = lic_msg = ifaces_msg = addrs_msg = None

        def _rows(path, reply):
            if isinstance(reply, BaseException) or reply is None:
                return []
            objs = reply.find(STD_OBJS)
            items = objs[1] if objs and isinstance(objs[1], list) else []
            return [self._row_from_msg(path, it) for it in items
                    if isinstance(it, m.Message)]

        if not isinstance(rb_msg, BaseException) and rb_msg is not None:
            try:  # board serial + model (absent on x86 VMs)
                rb = self._row_from_msg((24, 17), rb_msg)
                fp.serial = _s(rb, 0xD)
                fp.model = fp.model or _s(rb, 0xA)
            except Exception:
                pass
        if not fp.serial and not isinstance(lic_msg, BaseException) and lic_msg is not None:
            try:  # /system/license system-id when no hardware serial
                fp.system_id = _s(self._row_from_msg((65,), lic_msg), 0xD, 0x1, 0xA)
            except Exception:
                pass
        try:  # interface hardware MACs — field 0x404 on [20,0]
            for r in _rows((20, 0), ifaces_msg):
                v = r.values_by_code.get(0x404)
                mac = ""
                if isinstance(v, (bytes, bytearray)) and len(v) >= 6:
                    mac = ":".join(f"{b:02x}" for b in v[:6])
                elif isinstance(v, str) and len(v) == 12:
                    mac = ":".join(v[i:i + 2] for i in range(0, 12, 2))
                elif isinstance(v, int) and v:
                    mac = ":".join(f"{b:02x}" for b in v.to_bytes(6, "big"))
                if mac and mac != "00:00:00:00:00:00" and mac not in fp.macs:
                    fp.macs.append(mac)
        except Exception:
            pass
        try:  # interface IPv4s — field 0x1 on [20,1], u32 little-endian
            for r in _rows((20, 1), addrs_msg):
                v = r.values_by_code.get(0x1)
                if isinstance(v, int) and v:
                    ip = ".".join(str(x) for x in _struct.pack("<I", v & 0xFFFFFFFF))
                    if ip not in fp.ips:
                        fp.ips.append(ip)
        except Exception:
            pass
        fp.salt = getattr(self.session, "salt", "") or ""
        return fp

    def refresh_info(self) -> DeviceInfo:
        info = DeviceInfo()
        # Prefer resources (24,2) first — it is the reliable GET on modern the router.
        # Identity (24,1) can hang or return sparse data on some builds.
        def _str_field(row: RowObject, *codes: int) -> str:
            for code in codes:
                v = row.values_by_code.get(code)
                if isinstance(v, (bytes, bytearray)) and v:
                    return v.decode("utf-8", errors="replace")
                if isinstance(v, str) and v:
                    return v
            return ""

        # Resources and identity are independent reads, so they go out
        # together -- two round trips on a WAN link is two too many for a
        # window title.
        try:
            res_msg, ident_msg = self.pipeline(
                [(self.RESOURCE_PATH, CMD_GET), ((24, 1), CMD_GET)], quiet=True
            )
        except DeviceError:
            res_msg = ident_msg = None
        try:
            if isinstance(res_msg, BaseException) or res_msg is None:
                raise DeviceError("no resources reply")
            res = self._row_from_msg(self.RESOURCE_PATH, res_msg)
            by_name = {n.lower(): v for _c, n, v, _ok in res.fields}
            info.version = (
                by_name.get("version", "")
                or by_name.get("current firmware", "")
                or _str_field(res, 0x16)
            )
            info.board = (
                by_name.get("board name", "")
                or by_name.get("board-name", "")
                or _str_field(res, 0x2C, 0x19)
            )
            info.architecture = (
                by_name.get("architecture name", "")
                or by_name.get("architecture", "")
                or _str_field(res, 0x1A)
            )
            info.platform = by_name.get("platform", "") or info.architecture
            self._apply_resource_row(res, info)
            if not info.version and self._plugin_version:
                info.version = self._plugin_version
        except Exception:
            if self._plugin_version:
                info.version = self._plugin_version
        try:
            if isinstance(ident_msg, BaseException) or ident_msg is None:
                raise DeviceError("no identity reply")
            ident = self._row_from_msg((24, 1), ident_msg)
            for _c, name, val, _ok in ident.fields:
                if name.lower() == "name" and val:
                    info.identity = val
            if not info.identity:
                info.identity = _str_field(ident, 0x01, 0x0C, 0x0D)
        except Exception:
            pass
        self.info = info
        return info

    def _apply_resource_row(self, res: RowObject, info: DeviceInfo) -> None:
        """Copy the moving readings of /system/resource onto ``info``.

        These four are the only fields of a resource reply that change while a
        session is open, so they are the whole of what a repeated read is for.
        The uptime cache is refreshed from the same reply: ``type:'age'`` fields
        are stamps on that clock (see :meth:`uptime`), and this read already has
        it in hand.
        """
        by_name = {n.lower(): v for _c, n, v, _ok in res.fields}

        def named(*names: str) -> str:
            for n in names:
                val = by_name.get(n)
                if val:
                    return val
            return ""

        info.cpu_load = named("cpu load", "cpu-load")
        info.free_memory = named("free memory", "free-memory")
        info.total_memory = named("total memory", "total-memory")
        info.uptime = named("uptime")
        secs = res.raw_value(self.UPTIME_CODE)
        if isinstance(secs, int):
            self._uptime = float(secs)
            self._uptime_at = time.monotonic()

    def refresh_resources(self) -> DeviceInfo:
        """Re-read CPU load, memory and uptime, updating :attr:`info` in place.

        This is what a live readout polls: one quiet GET on (24,2), where
        :meth:`refresh_info` is two round trips and re-reads a version, board
        and identity that cannot change under it. Quiet keeps a recurring poll
        out of the session transcript, the same way :meth:`uptime` does.
        """
        res = self.get(self.RESOURCE_PATH, quiet=True)
        self._apply_resource_row(res, self.info)
        return self.info

    def table_rows(
        self,
        path: tuple[int, ...],
        *,
        type_value: int | None = None,
        family: str | None = None,
        name: str | None = None,
    ) -> tuple[sch.Menu | None, list[str], list[list[str]], list[RowObject], float | None]:
        """Return (menu, column_names, string_rows, raw_rows, uptime) for a list.

        ``type_value`` restricts the rows to one Winbox interface type (Ethernet,
        VLAN, ...) and merges that type's overlay into the menu, so the columns
        match the type. ``family`` does the same for a whole overlay namespace:
        Wireless ▸ WiFi Interfaces is 'wlan', which is every wireless type at
        once (ath, WDS, Nstreme Dual). The filtering is client side by
        necessity: STD_FILTER is a field mask, not a row predicate -- there is
        no "only type N" query.

        ``name`` picks the menu on a path that hosts more than one -- Web Proxy
        ▸ Connections and the Web Proxy Settings form are both (96,1), and the
        columns come from whichever of them the caller meant.
        """
        menu = self.menu_for(path, type_value, family=family, name=name)
        keep: set[int] | None = None
        if family and self.schema is not None:
            keep = self.schema.family_type_values(family)
        elif type_value is not None:
            keep = {int(type_value)}
        # Refresh the uptime clock before decoding: type:'age' columns are
        # stamps against it, and a stale reading skews every duration.
        if menu is not None and menu.has_age_field:
            self.uptime()
        # Prefetch dynamic enums used by preferred columns
        if menu is not None:
            for fld in sch.preferred_columns(menu):
                if fld.enum_path:
                    try:
                        self.enum_map(fld.enum_path)
                    except DeviceError:
                        pass
        objs = self.getall(path, filter_mask=list_read_mask(menu))
        if keep is not None:
            objs = [o for o in objs if sch.wire_type_value(o, menu) in keep]
        # `maxobjs`: All Routes, Connections and the proxy cache stop at 10000
        # rows rather than trying to draw a router's whole table.
        cap = getattr(menu, "max_objects", None) if menu is not None else None
        self._last_truncated = None
        if isinstance(cap, int) and cap > 0 and len(objs) > cap:
            self._last_truncated = (
                getattr(menu, "max_objects_message", None)
                or f"Showing the first {cap} of {len(objs)} rows."
            )
            objs = objs[:cap]
        if menu is None:
            cols: list[str] = []
            if objs:
                cols = [n for _c, n, _v, _ok in objs[0].fields][:8]
            str_rows = []
            for obj in objs:
                by = {n: v for _c, n, v, _ok in obj.fields}
                str_rows.append([by.get(c, "") for c in cols])
            return None, cols, str_rows, objs, self._uptime

        # Every column the menu can show, the .jg's default-visible ones
        # first. The list pane hides the tail until the header menu asks.
        cols = sch.list_columns(menu)
        # Winbox left flag column (unnamed): X=disabled, R=running, S=slave, …
        # On an ordered table it is followed by the rule number, because
        # position decides which rule wins and the list is meaningless without
        # it. The number is the device's own order, not the current sort.
        ordered = sch.is_ordered(menu, path)
        col_names = [""] + ([sch.INDEX_LABEL] if ordered else []) + [
            c.name for c in cols
        ]
        str_rows = []
        titles: dict[int | None, str] = {}
        for i, obj in enumerate(objs):
            flags = sch.format_flag_letters(menu, obj.values_by_code)
            cells = [self._column_text(col, obj, menu, titles) for col in cols]
            str_rows.append([flags] + ([str(i)] if ordered else []) + cells)
        return menu, col_names, str_rows, objs, self._uptime

    def _column_text(
        self,
        col: "sch.Column",
        obj: RowObject,
        menu: sch.Menu,
        titles: dict[int | None, str],
    ) -> str:
        """One list cell.

        Most columns are one field, but a non-separate tuple holds both of its
        members joined by the .jg's own separator (a bridge port's "Tx/Rx
        BPDU's" reads "12/9"), and a ``type:objtype`` column has no wire field
        at all -- it names the type overlay the row belongs to.
        """
        if col.kind == "objtype":
            return self._row_type_title(obj, menu, titles)
        if col.kind == "tuple":
            parts = [obj.get_rendered(code) for code in col.codes]
            return col.sep.join(parts) if any(parts) else ""
        if col.kind == "lookup":
            return self._lookup_text(col, obj, menu)
        if col.kind == "rate":
            # A rate is a difference between two replies, and this is one
            # reply. The list pane holds the previous sample and fills the
            # cell (ListPane._fill_rates); an invented number here would be
            # wrong on every row.
            return ""
        fld = col.field
        if fld is None:
            return ""
        # Match by field *name* so dual-id codes (Add Default Route vs
        # DHCP Options both 0x12) pick the right rendered value.
        # An address with its own mask field (IP ▸ Addresses) arrives joined
        # already, "10.0.2.15/24": see schema._render_top.
        return obj.get_rendered(fld.code, prefer_name=fld.name)

    def _lookup_text(
        self, col: "sch.Column", obj: RowObject, menu: sch.Menu
    ) -> str:
        """A lookup column's cell: this row's ``src``, found in another table."""
        if not col.lookup_path:
            return ""
        code = _field_code_by_name(menu, col.lookup_src)
        if code is None:
            return ""
        want = str(obj.get_rendered(code, prefer_name=col.lookup_src) or "").strip()
        if not want:
            return ""
        try:
            table = self.lookup_map(col.lookup_path, col.lookup_dst, col.lookup_val)
        except Exception:  # noqa: BLE001
            return ""
        return table.get(want.lower(), "")

    def _row_type_title(
        self, obj: RowObject, menu: sch.Menu, titles: dict[int | None, str]
    ) -> str:
        """'Ethernet', 'Bridge', 'VLAN' — the row's type, spelled out.

        Resolved per row rather than from the merged menu: a family list
        (Wireless ▸ WiFi Interfaces) is one menu covering three wire types, and
        its own title would call every row 'WiFi Interfaces'. Memoised per
        type value, since a chain lookup per row on a 48-port switch is waste.
        """
        # The decoded row first: the type field is nonpublic, so it is in
        # values_by_code and not in the rendered fields, and a row built
        # without a raw message (a test fixture, a queued add) still has it.
        value = sch.wire_type_value(obj, menu)
        if value is None:
            value = sch.wire_type_value(obj.raw, menu)
        hit = titles.get(value)
        if hit is not None:
            return hit
        text = ""
        if self.schema is not None:
            base = self.schema.record(menu.path) or menu
            text = self.schema.type_title_for(base, value, obj.values_by_code)
        if not text:
            text = str(getattr(menu, "type_title", "") or "")
        if not text and value is not None:
            text = f"type {value}"
        titles[value] = text
        return text


def _parse_plugin_names(index_bytes: bytes) -> set[str]:
    """The set of ``*.jg`` plugin names the device advertises in its file index
    (``name: "wave2.jg"`` entries). A device without a plugin omits it, which is
    how we know e.g. legacy ``wlan6.jg`` (Wireless) is absent."""
    import re

    return {
        mo.decode("ascii", "replace")
        for mo in re.findall(rb'name:\s*"([^"]+\.jg)"', index_bytes)
    }


def _parse_list_meta(index_bytes: bytes) -> tuple[str | None, int | None]:
    text = index_bytes.decode("utf-8", errors="replace")
    version = None
    build_id = None
    m = re.search(r'version\s*:\s*"([^"]+)"', text)
    if m:
        version = m.group(1)
    for blk in re.finditer(r"\{([^}]+)\}", text):
        body = blk.group(1)
        if "roteros" not in body:
            continue
        cm = re.search(r"crc\s*:\s*(\d+)", body)
        if cm:
            build_id = int(cm.group(1))
        break
    if build_id is None:
        cm = re.search(r"crc\s*:\s*(\d+)", text)
        if cm:
            build_id = int(cm.group(1))
    return version, build_id
