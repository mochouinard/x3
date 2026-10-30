"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path

from .proto import audit
from .proto import configexport as cexp
from .proto import message as m
from .proto import schema as sch
from .proto.client_ident import (
    CLIENT_IDENT_DEFAULT,
    CLIENT_IDENT_WINBOX3,
    CLIENT_IDENT_WINBOX4,
    resolve_client_ident,
)
from .proto.modern import DEFAULT_FROM, ModernAuthError, ModernProtocolError, ModernSession
from .proto.cdb import CLIENT_DIRS
from .proto.session import WinboxError, WinboxSession


def _utf8_output() -> None:
    """Write UTF-8 to a pipe or a file, whatever the platform's code page.

    Help text and router names are not all ASCII. Redirected on Windows,
    Python writes the ANSI code page and stops at the first character it lacks:
    ``x3ctl export --help > f`` died on the "→" in the help. A pipe should carry
    UTF-8 anyway, and an MCP client reads nothing else.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = argparse.ArgumentParser(
        prog="x3ctl",
        description="Manage routers over the Winbox protocol (TCP 8291).",
    )
    from . import __version__

    parser.add_argument("--version", action="version", version=f"x3ctl {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    _add_login_cmd(sub)
    _add_try_creds_cmd(sub)
    _add_version_cmd(sub)
    _add_inspect_cmd(sub)
    _add_getall_cmd(sub)
    _add_schema_cmd(sub)
    _add_probe_cmd(sub)
    _add_subscribe_cmd(sub)
    _add_download_cmd(sub)
    _add_fetch_plugins_cmd(sub)
    _add_mirror_cmd(sub)
    _add_terminal_cmd(sub)
    _add_export_cmd(sub)
    _add_backup_cmd(sub)
    _add_neighbors_cmd(sub)
    _add_audit_cmd(sub)
    _add_mcp_cmd(sub)

    args = parser.parse_args(argv)
    try:
        try:
            if getattr(args, "note", None) is not None or "note" in vars(args):
                _resolve_note(args)
            return args.func(args)
        finally:
            _report_audit(args)
    except (WinboxError, ModernAuthError, ModernProtocolError) as e:
        print(f"x3ctl: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"x3ctl: network error: {e}", file=sys.stderr)
        return 3


# ---------------------------------------------------------------------------
# Common argument scaffolding
# ---------------------------------------------------------------------------


def _add_common_conn_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--host",
        default=None,
        help="Device IP / hostname, or MAC address (aa:bb:cc:dd:ee:ff) for "
             "MAC-Winbox. Optional when --note is given.",
    )
    p.add_argument(
        "--note",
        default=None,
        help="Resolve host/port/user/password from the address book (Addresses.cdb) "
             "(Addresses.cdb) by matching this NOTE field. --host/--user/"
             "--password still override anything the entry supplies.",
    )
    p.add_argument(
        "--note-index",
        type=int,
        default=None,
        metavar="N",
        help="When several address-book entries share the same --note (e.g. "
             "different paths to one device), pick entry number N (0-based, "
             "in address-book order). Without it, an ambiguous note is listed "
             "and refused.",
    )
    p.add_argument("--port", type=int, default=8291)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument(
        "--transport",
        choices=["auto", "tcp", "mac"],
        default="auto",
        help="Connection path: tcp (8291), mac (UDP 20561 MAC-Winbox), or auto "
             "(MAC-shaped --host → mac, else tcp). Default: auto.",
    )


def _add_auth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--protocol",
        choices=["modern", "legacy", "legacy-unauth"],
        default="modern",
        help=(
            "Wire-protocol variant. 'modern' = firmware 6.43+ (SRP+AES, default). "
            "'legacy' = pre-6.43 plain-text with MD5 login. 'legacy-unauth' = "
            "the pre-6.42-patch unauthenticated mproxy(2,2) path (lab/legacy gear only)."
        ),
    )
    p.add_argument(
        "--user",
        help="Username. Required for modern and legacy protocols. "
             "Reads $X3_USER if unset.",
    )
    p.add_argument(
        "--password",
        help="Password. Reads $X3_PASSWORD or prompts if unset.",
    )
    p.add_argument(
        "--client-ident",
        default=None,
        metavar="IDENT",
        help=(
            "Client identity string for field 0xFF001C (v7 firmware gate). "
            f"Default {CLIENT_IDENT_DEFAULT!r}. "
            f"Aliases: 3 / winbox-3.42 → {CLIENT_IDENT_WINBOX3!r}; "
            f"4 / winbox-4.0 → {CLIENT_IDENT_WINBOX4!r}. "
            "Also $X3_CLIENT_IDENT. Use winbox-4.0 when firmware "
            "rejects the 3.x identity (test file fetch first)."
        ),
    )


def _add_audit_args(p: argparse.ArgumentParser) -> None:
    """Audit mode. Opt-in: it writes every config value that crosses the wire
    to a file, which is the point, and also why it is never on by default."""
    p.add_argument(
        "--audit",
        action="store_true",
        help="Record every M2 frame sent and received to an audit file "
             "(see `x3ctl audit`). Also $X3_AUDIT=1.",
    )
    p.add_argument(
        "--audit-level",
        choices=list(audit.LEVELS),
        default=None,
        metavar="LEVEL",
        help="How much to keep, least to most; each level keeps everything "
             "the one before it does — "
             + "; ".join(
                 f"'{name}' = {why}"
                 + (" (default)" if name == audit.DEFAULT_LEVEL else "")
                 for name, why in audit.LEVEL_HELP.items()
             )
             + ". Also $X3_AUDIT_LEVEL.",
    )
    p.add_argument(
        "--audit-file",
        default=None,
        metavar="PATH",
        help="Write the audit there instead of a timestamped file under "
             "$X3_AUDIT_DIR (default: the session-log directory).",
    )


def _audit_wanted(args) -> bool:
    return bool(getattr(args, "audit", False)) or audit.env_enabled()


def _start_audit(args, sess: ModernSession) -> None:
    """Attach a recorder to a session, if the user asked for one.

    The session owns it from here: ``ModernSession.close()`` rounds the file
    off, and ``_report_audit`` prints the tally once the command returns.
    """
    if not _audit_wanted(args):
        return
    pw = getattr(args, "_resolved_password", None)
    rec = audit.AuditRecorder(
        args.host,
        getattr(args, "user", None) or os.environ.get("X3_USER") or "",
        port=args.port,
        path=getattr(args, "audit_file", None),
        policy=audit.AuditPolicy(
            level=getattr(args, "audit_level", None) or audit.env_level(),
            # Real Winbox sends the account password in the terminal-open
            # frame even on an authenticated session, so it does reach a frame.
            secrets=((pw if isinstance(pw, bytes) else pw.encode("utf-8")),) if pw else (),
        ),
        extra={
            "client_ident": _session_client_ident(args),
            "command": getattr(args, "cmd", ""),
        },
    )
    sess.audit = rec
    args._audit_recorder = rec
    print(f"audit: recording to {rec.path}", file=sys.stderr)


def _report_audit(args) -> None:
    """Close the recorder and say what it kept. Safe to call twice."""
    rec = getattr(args, "_audit_recorder", None)
    if rec is None:
        return
    rec.close(reason="command finished")
    if not rec.sent_frames and not rec.recv_frames:
        print("audit: no frames recorded", file=sys.stderr)
        return
    print(
        f"audit: {rec.sent_frames} sent, {rec.recv_frames} received, "
        f"{rec.suppressed_polls} polls / {rec.suppressed_unchanged} unchanged "
        f"re-reads dropped -> {rec.path}",
        file=sys.stderr,
    )


def _resolve_creds(args) -> tuple[str | None, str | None]:
    user = args.user or os.environ.get("X3_USER")
    if args.protocol == "legacy-unauth":
        return None, None
    if user is None:
        raise WinboxError("missing --user (or $X3_USER) for authenticated protocol")
    pw = args.password or os.environ.get("X3_PASSWORD")
    if pw is None:
        pw = getpass.getpass(f"password for {user}@{args.host}: ")
    # Kept for the audit recorder, which blanks it out of any frame that
    # carries it (a terminal open does, even post-auth).
    args._resolved_password = pw
    return user, pw


def _split_host_port(host: str) -> tuple[str, int | None]:
    """Split an address-book host string into (host, port).

    Entries are stored as ``host:port`` (e.g. ``10.2.0.11:41002`` or
    ``d4f10d72f071.sn.example.net:41002``). A bare host, a MAC address, or an
    IPv6 literal without a bracketed port comes back with ``port=None``.
    """
    if _MAC_RE.match(host):
        return host, None
    # Bracketed IPv6, optionally with :port
    if host.startswith("["):
        inside, _, tail = host[1:].partition("]")
        if tail.startswith(":") and tail[1:].isdigit():
            return inside, int(tail[1:])
        return inside, None
    # Bare IPv6 (2+ colons) has no port suffix we can safely split.
    if host.count(":") >= 2:
        return host, None
    h, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        return h, int(tail)
    return host, None


def _resolve_note(args) -> None:
    """Fill host/port/user/password from an address-book entry named by --note.

    Command-line values win: anything the user passed explicitly is left alone,
    and the entry only supplies the gaps. Called once from ``main`` before the
    subcommand runs, so every device subcommand gains --note for free.
    """
    note = getattr(args, "note", None)
    if note is None:
        if getattr(args, "host", None) is None and "host" in vars(args):
            raise WinboxError("missing --host (or use --note NOTE)")
        return

    path = _find_cdb(getattr(args, "cdb", None))
    from .proto.cdb import CdbFile

    matches = [e for e in CdbFile.load(path).entries if (e.note or "") == note]
    if not matches:
        notes = sorted({e.note for e in CdbFile.load(path).entries if e.note})
        raise WinboxError(
            f"no address-book entry with note {note!r} in "
            f"{os.path.basename(path)}. Known notes: " + ", ".join(notes)
        )

    idx = getattr(args, "note_index", None)
    if len(matches) > 1 and idx is None:
        lines = [
            f"note {note!r} matches {len(matches)} entries in "
            f"{os.path.basename(path)} -- pick one with --note-index N:"
        ]
        for i, e in enumerate(matches):
            lines.append(f"  [{i}] {e.host or '?'}  (login={e.login or '?'})")
        raise WinboxError("\n".join(lines))
    if idx is None:
        idx = 0
    if not (0 <= idx < len(matches)):
        raise WinboxError(
            f"--note-index {idx} out of range: note {note!r} has "
            f"{len(matches)} entr{'y' if len(matches) == 1 else 'ies'} (0.."
            f"{len(matches) - 1})"
        )
    entry = matches[idx]

    host, port = _split_host_port(entry.host or "")
    if not host:
        raise WinboxError(f"address-book entry for note {note!r} has no host")
    if getattr(args, "host", None) is None:
        args.host = host
        # Only adopt the entry's port when the user left --port at its default.
        if port is not None and getattr(args, "port", None) == 8291:
            args.port = port
    if getattr(args, "user", None) is None and entry.login:
        args.user = entry.login
    if getattr(args, "password", None) is None and entry.password_bytes is not None:
        # Prefer the raw stored bytes: a secret saved in a single-byte codepage
        # is lossy through UTF-8 (see CdbEntry.password_bytes). authenticate()
        # accepts bytes, so _resolve_creds hands these straight through.
        args.password = entry.password_bytes
    print(
        f"note {note!r} -> {args.host}:{getattr(args, 'port', 8291)} "
        f"(login={getattr(args, 'user', None) or '?'})",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _add_login_cmd(sub) -> None:
    p = sub.add_parser(
        "login",
        help="Authenticate against the device and report success. No file IO.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.set_defaults(func=_cmd_login)


def _cmd_login(args) -> int:
    user, password = _resolve_creds(args)
    if args.protocol == "modern":
        with _modern_session(args) as sess:
            z = sess.authenticate(user, password)
            via = "mac-winbox" if sess.via_mac else f"tcp/{args.port}"
            print(
                f"OK modern ({via}, client-ident={sess.client_ident}): "
                f"shared secret z = {z.hex()}"
            )
        return 0
    if args.protocol == "legacy":
        with WinboxSession(args.host, args.port, args.timeout) as sess:
            sid = sess.login(user, password)
            print(f"OK legacy: session_id = {sid}")
        return 0
    raise WinboxError(f"login does not support protocol {args.protocol!r}")


# ---------------------------------------------------------------------------
# try-creds: recover access to your own device by testing stored credentials
# ---------------------------------------------------------------------------

# Common places an Addresses.cdb turns up on this workstation. --cdb overrides.
_CDB_SEARCH = (
    "Addresses.cdb",
    *(f"~/.wine/drive_c/users/*/AppData/Roaming/{d}/Addresses.cdb" for d in CLIENT_DIRS),
    *(f"~/.local/share/{d}/Addresses.cdb" for d in CLIENT_DIRS),
    *(os.path.join(os.environ["APPDATA"], d, "Addresses.cdb")
      for d in CLIENT_DIRS if os.environ.get("APPDATA")),
)


def _find_cdb(explicit: str | None) -> str:
    """Locate an Addresses.cdb, preferring an explicit --cdb path."""
    if explicit:
        if not os.path.exists(explicit):
            raise WinboxError(f"--cdb file not found: {explicit}")
        return explicit
    import glob

    for pat in _CDB_SEARCH:
        for hit in glob.glob(os.path.expanduser(pat)):
            if os.path.isfile(hit):
                return hit
    raise WinboxError(
        "could not locate Addresses.cdb automatically; pass --cdb PATH"
    )


_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", re.I)


def _norm_host(h: str) -> str:
    """Normalize an address-book host / --host for comparison.

    Lower-cases, and strips a trailing ``:port`` from IP/hostname entries while
    leaving MAC addresses (which also contain colons) intact.
    """
    h = (h or "").strip().lower()
    if _MAC_RE.match(h):
        return h
    return h.rsplit(":", 1)[0] if ":" in h else h


def _uniq(seq):
    """Order-preserving de-duplication."""
    seen = set()
    out = []
    for x in seq:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _add_try_creds_cmd(sub) -> None:
    p = sub.add_parser(
        "try-creds",
        help="Locked out of your own device? Try every credential in your "
             "Addresses.cdb against it and report which password works.",
        description=(
            "Reads your saved address book (Addresses.cdb) and tries its stored "
            "credentials against one device over the modern (firmware 6.43+) handshake, "
            "reporting which password authenticates. Intended for recovering "
            "access to your OWN devices when you have reused a password across "
            "the fleet and lost track of which one a box carries.\n\n"
            "The password never crosses the wire (EC-SRP5), but the USERNAME does "
            "in cleartext, every failed attempt is logged on the device, and a "
            "hardened box may auto-blacklist your source IP after repeated Winbox "
            "failures -- hence the pacing (--delay) and the consecutive-error "
            "circuit breaker."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_conn_args(p)
    p.add_argument(
        "--cdb",
        metavar="PATH",
        help="Path to the Addresses.cdb to draw candidates from. "
             "Auto-detected if omitted.",
    )
    p.add_argument(
        "--user",
        help="Fix the username and sweep every distinct saved PASSWORD against it "
             "(the reused-password case). Reads $X3_USER if unset. Without it, "
             "the stored (login, password) pairs are tried as-is.",
    )
    p.add_argument(
        "--cross",
        action="store_true",
        help="Try the full cross-product of every distinct saved login x password "
             "(most exhaustive, and noisiest). Ignored if --user is given.",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Keep going after the first hit and report every credential that works "
             "(default: stop at the first success).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Spray a --host that is NOT in your address book. Refused by default: "
             "the whole point of the tool is testing YOUR devices, so an unknown "
             "host is treated as 'a router I do not control' until you say otherwise.",
    )
    p.add_argument(
        "--expect-salt",
        metavar="HEX",
        help="Abort before sending any credential unless the device's SRP salt "
             "(a per-install fingerprint, read at handshake stage 2) equals this. "
             "Pin it from a healthy login to guarantee you are talking to the box "
             "you think you are, not an impostor at the same address.",
    )
    p.add_argument(
        "--no-fingerprint",
        action="store_true",
        help="Skip the stage-2 salt fingerprint pre-flight (it is printed by "
             "default; --expect-salt implies it regardless).",
    )
    p.add_argument(
        "--include-empty",
        action="store_true",
        help="Also try entries whose stored password is blank (skipped by default).",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="Pause between attempts to stay under any Winbox-failure blacklist "
             "rule on the device. Default 0.5; use 0 on a trusted LAN.",
    )
    p.add_argument(
        "--max-conn-errors",
        type=int,
        default=3,
        metavar="N",
        help="Stop after this many consecutive connection failures -- either the "
             "device is unreachable or it just blacklisted this IP. Default 3.",
    )
    p.add_argument(
        "--client-ident",
        default=None,
        metavar="IDENT",
        help="Client identity for field 0xFF001C (see 'login --help'). "
             "Try winbox-4.0 if the firmware rejects the default.",
    )
    p.set_defaults(func=_cmd_try_creds)


def _cmd_try_creds(args) -> int:
    from .proto import cdb as cdbmod

    path = _find_cdb(args.cdb)
    book = cdbmod.CdbFile.load(path)

    # Guard 1: refuse to fan credentials at a host you have no saved relationship
    # with. "A router I do not control" == a host that is not in my address book.
    target = _norm_host(args.host)
    book_hosts = {_norm_host(e.host) for e in book if e.host}
    if target not in book_hosts and not args.force:
        raise WinboxError(
            f"{args.host} is not in {os.path.basename(path)} -- refusing to try "
            "your saved passwords against a host you may not control. If you do "
            "own this device, re-run with --force."
        )

    # (login, password) as stored, keeping only entries that carry a password.
    stored = []
    pw_origin: dict[str, str] = {}
    for e in book:
        pw = e.password
        if pw is None:
            continue
        if pw == "" and not args.include_empty:
            continue
        login = (e.login or "").strip()
        stored.append((login, pw))
        if pw not in pw_origin:
            where = e.host or "?"
            note = e.note
            pw_origin[pw] = f"{where} ({note})" if note else where

    passwords = _uniq([pw for (_l, pw) in stored])
    logins = _uniq([l for (l, _pw) in stored if l])

    user = args.user or os.environ.get("X3_USER")
    if user:
        candidates = [(user, pw) for pw in passwords]
        mode = f"username {user!r} x {len(passwords)} distinct passwords"
    elif args.cross:
        candidates = [(u, pw) for u in logins for pw in passwords]
        mode = f"{len(logins)} logins x {len(passwords)} passwords (cross-product)"
    else:
        candidates = _uniq([(l, pw) for (l, pw) in stored if l])
        mode = f"{len(candidates)} stored (login, password) pairs"

    candidates = _uniq(candidates)
    if not candidates:
        raise WinboxError(
            f"no candidate credentials in {path} "
            "(is the book empty, or all passwords blank? see --include-empty)"
        )

    transport = _session_transport(args)
    client_ident = _session_client_ident(args)
    print(
        f"try-creds: {os.path.basename(path)} -> {args.host}:{args.port}  "
        f"[{mode}]"
    )

    # Guard 2: fingerprint the device via the stage-2 SRP salt BEFORE committing
    # any password material. Stage 1 discloses only the username (unavoidable in
    # SRP); stage 3 -- the only password-derived bytes -- is never sent here.
    if args.expect_salt or not args.no_fingerprint:
        probe_user = candidates[0][0]
        probe = ModernSession(
            args.host, args.port, args.timeout,
            transport=transport, client_ident=client_ident,
        )
        salt = None
        try:
            probe.connect()
            salt = probe.peek_salt(probe_user)
        except OSError as e:
            raise WinboxError(f"cannot reach {args.host}:{args.port}: {e}")
        except (ModernAuthError, ModernProtocolError) as e:
            # Device answered but wouldn't complete a stage-2 probe for this
            # username. Only fatal if we were asked to verify the fingerprint.
            if args.expect_salt:
                raise WinboxError(
                    f"could not read device fingerprint to match --expect-salt "
                    f"({e}); aborting before any password is tried."
                )
            print(f"  device fingerprint: unavailable ({e})")
        finally:
            probe.close()
        if salt is not None:
            print(f"  device fingerprint (SRP salt, user {probe_user!r}): {salt}")
            if args.expect_salt and salt.lower() != args.expect_salt.strip().lower():
                raise WinboxError(
                    "fingerprint mismatch: this device's salt is "
                    f"{salt}, not the expected {args.expect_salt.strip().lower()}. "
                    "Aborting before any password is tried -- you may be pointed "
                    "at a different (or impostor) device. Only the username was "
                    "disclosed."
                )

    successes: list[tuple[str, str]] = []
    consecutive_conn_errors = 0
    n = len(candidates)
    for i, (cuser, cpw) in enumerate(candidates, 1):
        label = f"[{i}/{n}] login={cuser!r}"
        sess = ModernSession(
            args.host,
            args.port,
            args.timeout,
            transport=transport,
            client_ident=client_ident,
        )
        try:
            try:
                sess.connect()
            except OSError as e:
                consecutive_conn_errors += 1
                print(f"{label}  unreachable ({e})")
                if consecutive_conn_errors >= args.max_conn_errors:
                    print(
                        f"x3ctl: {consecutive_conn_errors} connection failures "
                        f"in a row -- {args.host}:{args.port} is unreachable or has "
                        "blacklisted this IP; stopping.",
                        file=sys.stderr,
                    )
                    break
                time.sleep(args.delay)
                continue
            consecutive_conn_errors = 0
            try:
                sess.authenticate(cuser, cpw)
            except ModernAuthError:
                print(f"{label}  no")
            except ModernProtocolError as e:
                # Some devices drop the socket on
                # a bad login instead of sending the router's stage-4 error string; the
                # pre-flight already proved the device reachable, so a mid-
                # handshake drop here is a rejection, not a fatal fault.
                print(f"{label}  no (handshake dropped: {e})")
            else:
                origin = pw_origin.get(cpw, "?")
                print(f"{label}  YES  <== works (this password is saved for {origin})")
                successes.append((cuser, cpw))
        finally:
            sess.close()

        if successes and not args.all:
            break
        time.sleep(args.delay)

    print()
    if not successes:
        print("No stored credential authenticated against "
              f"{args.host}:{args.port}.")
        return 1
    print(f"{len(successes)} working credential(s):")
    for cuser, cpw in successes:
        print(f"  {cuser} : {cpw}")
    return 0


def _add_version_cmd(sub) -> None:
    p = sub.add_parser(
        "version",
        help="Query the device version handler ((24,2) cmd=0xFE000D). "
             "Useful as a smoke test of the encrypted transport.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.set_defaults(func=_cmd_version)


def _cmd_version(args) -> int:
    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("version probe is implemented only for the modern protocol")
    with _modern_session(args) as sess:
        sess.authenticate(user, password)
        req = (
            m.Message()
            .set_to(24, 2)
            .add_u32_array(m.SYS_FROM, list(DEFAULT_FROM))
            .set_command(0xFE000D)
            .set_request_id(sess.next_request_id())
            .set_reply_expected(True)
        )
        sess.stamp_client_ident(req)
        reply = sess.request(req)
        if reply.has_error():
            print(f"server error: {reply.error_string()}", file=sys.stderr)
            return 2
        print("version reply fields:")
        for code, t, v in reply:
            print(f"  field {code:#08x} tag {t:#04x} = {_pretty(v)}")
    return 0


def _add_schema_cmd(sub) -> None:
    p = sub.add_parser(
        "schema",
        help="Browse the router's menu/field schema from a local .jg plugin "
             "cache. Offline -- no device needed.",
        description=(
            "Read the .jg plugin files the router serves (as cached by 'x3ctl mirror' "
            "downloads) and report the handler paths, menus and wire field ids "
            "they define. This is the map of the router's management surface."
        ),
    )
    p.add_argument("--dir", help="Plugin cache directory holding .jg files.")
    p.add_argument("--version", help="Resolve '<version>-*' from the local plugin cache, e.g. 7.23.1.")
    p.add_argument("--list", action="store_true", help="List every record (map/item) menu.")
    p.add_argument("--find", help="List menus whose name/title/CLI prefix matches this text.")
    p.add_argument("--show", help="Show all fields of a menu, by path ('43,1') or by name.")
    p.add_argument("--all-kinds", action="store_true",
                   help="With --list, include doit/action/query dialogs too.")
    p.set_defaults(func=_cmd_schema)


def _resolve_schema_dir(args) -> str:
    if args.dir:
        return args.dir
    base = sch.default_cache_dir()
    if not base:
        raise WinboxError("no .jg plugin cache found; pass --dir")
    if args.version:
        hit = sch.find_version_dir(args.version, base)
        if not hit:
            raise WinboxError(f"no '{args.version}-*' directory under {base}")
        return hit
    import glob as _glob

    dirs = [d for d in _glob.glob(os.path.join(base, "*")) if os.path.isdir(d)
            and _glob.glob(os.path.join(d, "*.jg"))]
    if not dirs:
        raise WinboxError(f"no plugin cache with .jg files under {base}")
    newest = max(dirs, key=os.path.getmtime)
    print(f"# using newest cache: {os.path.basename(newest)}", file=sys.stderr)
    return newest


def _cmd_schema(args) -> int:
    schema = sch.Schema.load(_resolve_schema_dir(args))

    if args.show:
        target = args.show
        menus = []
        if all(c.isdigit() or c in ", " for c in target):
            path = tuple(int(x) for x in target.split(",") if x.strip())
            menus = schema.by_path.get(path, [])
        if not menus:
            menus = [x for x in schema.find(target) if x.fields]
        if not menus:
            raise WinboxError(f"no menu matching {target!r}")
        for menu in menus:
            if not menu.fields:
                continue
            cli = f"  /{menu.cli_prefix}" if menu.cli_prefix else ""
            print(f"\n{menu.label()}{cli}   [{menu.source}]")
            print(f"  read command: {menu.read_command:#x}"
                  + (f"   action cmd: {menu.command}" if menu.command else ""))
            print(f"  {'field':>9}  {'wire':4} {'type':16} {'name'}")
            for fld in sorted(menu.fields.values(), key=lambda f: f.code):
                extra = []
                if fld.ro:
                    extra.append("ro")
                if fld.optional:
                    extra.append("opt")
                if fld.enum:
                    vals = ",".join(list(fld.enum.values())[:6])
                    extra.append(f"enum[{vals}]")
                if fld.enum_path:
                    extra.append("enum<-" + ",".join(str(x) for x in fld.enum_path))
                if fld.scale:
                    extra.append(f"scale={fld.scale}{fld.postfix or ''}")
                if fld.nested:
                    extra.append(f"{len(fld.nested)} nested")
                tail = ("  " + " ".join(extra)) if extra else ""
                print(f"  {fld.code:#9x}  {fld.prefix}{fld.code:<3x} {fld.jg_type:16} "
                      f"{fld.name or '-'}{tail}")
        return 0

    if args.find:
        hits = schema.find(args.find)
        print(f"# {len(hits)} menu(s) matching {args.find!r} in {schema.version}")
        for menu in sorted(hits, key=lambda x: (x.path, x.name)):
            cli = f" /{menu.cli_prefix}" if menu.cli_prefix else ""
            print(f"  {','.join(str(x) for x in menu.path):<12} {menu.kind:7} "
                  f"{len(menu.fields):3}f  {menu.name}{cli}")
        return 0

    menus = schema.menus if args.all_kinds else schema.records()
    if args.list:
        print(f"# {schema.version}: {len(menus)} menus, {len(schema.by_path)} handler paths, "
              f"{schema.field_count} fields")
        for menu in sorted(menus, key=lambda x: (x.group or "", x.path)):
            cli = f" /{menu.cli_prefix}" if menu.cli_prefix else ""
            print(f"  {','.join(str(x) for x in menu.path):<12} {menu.kind:7} "
                  f"{len(menu.fields):3}f  {(menu.group or '-'):14} {menu.name}{cli}")
        return 0

    groups = {}
    for menu in menus:
        groups[menu.group or "-"] = groups.get(menu.group or "-", 0) + 1
    print(f"schema {schema.version}")
    print(f"  handler paths : {len(schema.by_path)}")
    print(f"  menus         : {len(schema.menus)} ({len(schema.records())} record menus)")
    print(f"  fields        : {schema.field_count}")
    shared = sum(1 for v in schema.by_path.values()
                 if sum(1 for x in v if x.kind in ('map', 'item')) > 1)
    print(f"  paths hosting >1 record menu (ambiguous): {shared}")
    print("\n  menus by UI group:")
    for g, n in sorted(groups.items(), key=lambda kv: -kv[1]):
        print(f"    {g:18} {n}")
    print("\nUse --list, --find TEXT, or --show PATH|NAME.")
    return 0


def _add_probe_cmd(sub) -> None:
    p = sub.add_parser(
        "probe",
        help="Send an arbitrary handler/command request and dump the reply. "
             "Use this to map v7 handlers we haven't catalogued yet.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument(
        "--to",
        required=True,
        help="Comma-separated handler path, e.g. '24,2' or '2,2'.",
    )
    p.add_argument(
        "--command",
        required=True,
        help="Command code (decimal or 0x-prefixed hex).",
    )
    p.add_argument(
        "--from",
        dest="from_path",
        help="Optional sys_from handler path, e.g. '0,11'.",
    )
    p.add_argument(
        "--str",
        action="append",
        default=[],
        help="String field, format CODE=VALUE (CODE is decimal or 0x-prefixed hex). "
             "Repeatable.",
    )
    p.add_argument(
        "--u32",
        action="append",
        default=[],
        help="u32 field, format CODE=VALUE. Repeatable.",
    )
    p.add_argument(
        "--raw",
        action="append",
        default=[],
        help="Raw-bytes field (tag 0x30/0x31), format CODE=VALUE. Repeatable.",
    )
    p.add_argument(
        "--str-array",
        action="append",
        default=[],
        help="String-array field (tag 0xa0). Format CODE=VALUE1,VALUE2,... "
             "(commas separate elements). Repeatable.",
    )
    p.set_defaults(func=_cmd_probe)


def _cmd_probe(args) -> int:
    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("probe is implemented only for the modern protocol")

    to_path = [int(x, 0) for x in args.to.split(",") if x.strip()]
    cmd = int(args.command, 0)

    with _modern_session(args) as sess:
        sess.authenticate(user, password)
        if args.from_path:
            from_p = [int(x, 0) for x in args.from_path.split(",") if x.strip()]
        else:
            from_p = list(DEFAULT_FROM)
        msg = (
            m.Message()
            .add_u32_array(m.SYS_TO, to_path)
            .add_u32_array(m.SYS_FROM, from_p)
            .set_command(cmd)
            .set_request_id(sess.next_request_id())
            .set_reply_expected(True)
        )
        for spec in args.str:
            code, _, value = spec.partition("=")
            msg.add_string(int(code, 0), value)
        for spec in args.u32:
            code, _, value = spec.partition("=")
            msg.add_u32(int(code, 0), int(value, 0))
        for spec in args.raw:
            code, _, value = spec.partition("=")
            msg.add_raw(int(code, 0), value.encode("utf-8"))
        for spec in args.str_array:
            code, _, value = spec.partition("=")
            elements = [v.encode("utf-8") for v in value.split(",")]
            msg._entries.append((int(code, 0), m.T_STRING_ARRAY, elements))

        sess.stamp_client_ident(msg)

        reply = sess.request(msg)
        print(f"reply has_error={reply.has_error()}")
        for code, t, v in reply:
            print(f"  field {code:#08x} tag {t:#04x} = {_pretty(v)}")
    return 0


def _pretty(value):
    if isinstance(value, (bytes, bytearray)):
        if value and all(32 <= b < 127 or b in (9, 10, 13) for b in value):
            return value.decode("ascii", errors="replace")
        return value.hex()
    return value


# ---------------------------------------------------------------------------
# Plugin index / schema helpers
# ---------------------------------------------------------------------------


def _parse_plugin_index(index_bytes: bytes) -> tuple[list[dict], str, int]:
    """Parse the `list` plugin index into (entries, version, build_id).

    Winbox names its cache directory "<version>-<build_id>", where build_id is
    the CRC of roteros.jg (the root plugin) as given in the index.
    """
    text = index_bytes.decode("utf-8", errors="replace")
    entries: list[dict] = []
    for blk in re.finditer(r"\{([^}]+)\}", text):
        entry: dict = {}
        for kv in re.finditer(r"(\w+):\s*(\"([^\"]*)\"|(\d+))", blk.group(1)):
            entry[kv.group(1)] = kv.group(3) if kv.group(3) is not None else int(kv.group(4))
        if "name" in entry:
            entries.append(entry)
    if not entries:
        raise WinboxError("could not parse any entries from `list`")

    version = next(
        (e["version"] for e in entries if isinstance(e.get("version"), str)),
        "unknown",
    )
    build_id = next(
        (e["crc"] for e in entries if e.get("name") == "roteros.jg" and "crc" in e),
        None,
    )
    if build_id is None:
        build_id = next((e["crc"] for e in entries if isinstance(e.get("crc"), int)), 0)

    for warning in _index_deviations(text, entries):
        print(f"# warning: plugin index {warning}", file=sys.stderr)

    return entries, version, build_id


def _index_deviations(text: str, entries: list[dict]) -> list[str]:
    """Ways this index differs from what a real device emits.

    The parser above is deliberately loose so it keeps working across firmware
    revisions, but that tolerance cuts both ways: it happily accepted a
    hand-written index that no Winbox would touch, and the resulting bug -- a
    client that read the index, said nothing, and never asked for the plugin --
    took a long time to find. Warning is the compromise. We still parse it.

    A real firmware 7.21.1 emits, one entry per line:

        { crc: 151146055, size: 109651, name: "roteros.jg", unique: "roteros-7bf05b783f0b.jg", version: "7.21.1" },
    """
    out: list[str] = []
    if ";" in text:
        out.append("separates fields with ';'; a real device uses ','")
    if "\r\n" in text:
        out.append("uses CRLF line endings; a real device uses LF")
    if re.search(r"\w+:\S", text):
        out.append("has no space after a colon; a real device writes 'crc: 1'")
    if not re.search(r"\},?\s*$", text.strip()):
        out.append("does not end with a closing brace")
    for e in entries:
        name = e.get("name", "?")
        if str(name).endswith(".jg") and "unique" not in e:
            # Not needed to fetch anything -- the wire name is name + ".gz" --
            # but a real device always emits it, so its absence says the index
            # was not written by one.
            out.append(f"entry {name!r} has no 'unique'; a real device emits it")
        for key in ("crc", "size"):
            if key not in e:
                out.append(f"entry {name!r} has no {key!r}")
    return out


def _add_schema_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--decode",
        action="store_true",
        help="Name and format fields using the .jg schema instead of printing "
             "raw ids. Resolves the schema from --schema-dir, or from the "
             "device's own plugin index plus the local plugin cache.",
    )
    p.add_argument(
        "--schema-dir",
        help="Directory of .jg files (a '<version>-<build_id>' plugin cache). "
             "Defaults to auto-detection against the connected device.",
    )
    p.add_argument(
        "--no-schema-fetch",
        action="store_true",
        help="Do not download the device's plugin files when no local cache "
             "matches its firmware build; fail instead.",
    )


def _resolve_schema(args, sess=None) -> sch.Schema:
    """Load the schema for --decode, preferring an exact device-version match.

    Falls back to downloading the device's own plugin files, so pointing
    x3ctl at a firmware build nobody has cached yet just works.
    """
    if getattr(args, "schema_dir", None):
        return sch.Schema.load(args.schema_dir)
    if sess is None:
        raise WinboxError("--decode needs --schema-dir when not connected to a device")

    entries, version, build_id = _parse_plugin_index(sess.fetch_file("list", handler=(2, 2)))

    exact = sch.find_exact_dir(version, build_id)
    if exact:
        return sch.Schema.load(exact)

    if getattr(args, "no_schema_fetch", False):
        near = sch.find_version_dir(version)
        if near:
            print(f"# schema: no cache for {version}-{build_id}; using "
                  f"{os.path.basename(near)}", file=sys.stderr)
            return sch.Schema.load(near)
        raise WinboxError(
            f"no local schema for {version}-{build_id} and --no-schema-fetch is set. "
            f"Run 'x3ctl mirror --host {args.host}' to download it."
        )

    dest = os.path.join(sch.user_cache_dir(), f"{version}-{build_id}")
    print(f"# schema: {version}-{build_id} not cached; downloading plugin files "
          f"to {dest}", file=sys.stderr)
    fetch = lambda name: sess.fetch_file(name, handler=(2, 2))
    got, cached, failed = sch.download_plugin_set(
        entries, build_id, dest, fetch, log=lambda s: print(s, file=sys.stderr))
    if failed:
        print(f"# schema: {failed} file(s) failed to download", file=sys.stderr)
    try:
        return sch.Schema.load(dest)
    except FileNotFoundError as e:
        raise WinboxError(f"downloaded no usable .jg files for {version}-{build_id}: {e}")


class _EnumResolver:
    """Names dynamic-enum ids by GETALL-ing the handler the schema points at.

    `.jg` stores those values as another object's id, so 'Interface 8' only
    becomes 'bridge' by asking (20,0). Results are cached per run; a placeholder
    is stored before the lookup so a reference cycle between two menus cannot
    recurse.
    """

    def __init__(self, sess, schema: sch.Schema) -> None:
        self._sess = sess
        self._schema = schema
        self._cache: dict[tuple[int, ...], dict[int, str]] = {}

    def __call__(self, path: tuple[int, ...]) -> dict[int, str]:
        if path in self._cache:
            return self._cache[path]
        self._cache[path] = {}
        try:
            req = (
                m.Message()
                .add_u32_array(m.SYS_TO, list(path))
                .add_u32_array(m.SYS_FROM, list(DEFAULT_FROM))
                .set_command(0xFE0004)
                .set_request_id(self._sess.next_request_id())
                .set_reply_expected(True)
            )
            self._sess.stamp_client_ident(req)
            reply = self._sess.request(req)
            if reply.has_error():
                return self._cache[path]
            hit = reply.find(0xFE0002)
            items = hit[1] if hit and isinstance(hit[1], list) else []
            self._cache[path] = sch.enum_map_from_objects(
                self._schema.record(path),
                [x for x in items if isinstance(x, m.Message)],
            )
        except (WinboxError, ModernProtocolError, OSError):
            pass
        return self._cache[path]


def _print_decoded(schema: sch.Schema, path: tuple[int, ...], msg, indent: str = "  ",
                   resolver=None) -> None:
    menu = schema.record(path)
    if menu is None:
        print(f"{indent}# no schema menu for path {path}; showing raw ids", file=sys.stderr)
    else:
        others = [x for x in schema.by_path.get(path, []) if x.kind in ("map", "item") and x is not menu]
        note = f"  (+{len(others)} other record menus share this path)" if others else ""
        print(f"{indent}# {menu.label()}{note}")
    for code, name, value, type_ok in schema.decode(path, msg, resolver=resolver):
        flag = "" if type_ok else "  <- wire type differs from schema"
        print(f"{indent}{name:<28} {value}{flag}")


def _add_inspect_cmd(sub) -> None:
    p = sub.add_parser(
        "inspect",
        help="Send GET (cmd 0xFE000D) to a handler and pretty-print every "
             "returned field. Works on record-style handlers like (24,2).",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument("--to", required=True, help="Handler path 'X,Y'.")
    _add_schema_args(p)
    p.set_defaults(func=_cmd_inspect)


def _cmd_inspect(args) -> int:
    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("inspect is implemented only for the modern protocol")
    to_path = [int(x, 0) for x in args.to.split(",") if x.strip()]
    with _modern_session(args) as sess:
        sess.authenticate(user, password)
        schema = _resolve_schema(args, sess) if args.decode else None
        msg = (
            m.Message()
            .add_u32_array(m.SYS_TO, to_path)
            .add_u32_array(m.SYS_FROM, list(DEFAULT_FROM))
            .set_command(0xFE000D)
            .set_request_id(sess.next_request_id())
            .set_reply_expected(True)
        )
        sess.stamp_client_ident(msg)

        reply = sess.request(msg)
        if reply.has_error():
            print(f"server error: {reply.error_string()}", file=sys.stderr)
            return 2
        if schema is not None:
            _print_decoded(schema, tuple(to_path), reply,
                           resolver=_EnumResolver(sess, schema))
        else:
            for code, t, v in reply:
                print(f"  {code:#08x} tag {t:#04x} = {_pretty(v)}")
    return 0


def _add_getall_cmd(sub) -> None:
    p = sub.add_parser(
        "getall",
        help="Send GETALL (cmd 0xFE0004) to a menu handler and dump all "
             "returned objects. Works on collection-style handlers.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument("--to", required=True, help="Handler path 'X,Y'.")
    _add_schema_args(p)
    p.set_defaults(func=_cmd_getall)


def _cmd_getall(args) -> int:
    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("getall is implemented only for the modern protocol")
    to_path = [int(x, 0) for x in args.to.split(",") if x.strip()]
    with _modern_session(args) as sess:
        sess.authenticate(user, password)
        schema = _resolve_schema(args, sess) if args.decode else None
        msg = (
            m.Message()
            .add_u32_array(m.SYS_TO, to_path)
            .add_u32_array(m.SYS_FROM, list(DEFAULT_FROM))
            .set_command(0xFE0004)
            .set_request_id(sess.next_request_id())
            .set_reply_expected(True)
        )
        sess.stamp_client_ident(msg)

        reply = sess.request(msg)
        if reply.has_error():
            print(f"server error: {reply.error_string()}", file=sys.stderr)
            return 2
        objs = reply.find(0xFE0002)
        count_field = reply.find(0xFE0019)
        count = count_field[1] if count_field else 0
        items = objs[1] if objs and isinstance(objs[1], list) else []
        enum_resolver = _EnumResolver(sess, schema) if schema is not None else None
        print(f"# {len(items)} object(s) (server count={count})")
        for i, item in enumerate(items):
            print(f"--- object {i} ---")
            if not hasattr(item, "__iter__"):
                continue
            if schema is not None:
                _print_decoded(schema, tuple(to_path), item, resolver=enum_resolver)
            else:
                for code, t, v in item:
                    print(f"  {code:#08x} tag {t:#04x} = {_pretty(v)}")
    return 0


def _add_subscribe_cmd(sub) -> None:
    p = sub.add_parser(
        "subscribe",
        help="Open a streaming subscription on (2,2) cmd 7 against a menu name "
             "(e.g. 'file', 'user', 'list') and dump every reply until quiet.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument(
        "--name",
        required=True,
        help="Menu name to subscribe to. 'file' should enumerate files.",
    )
    p.add_argument(
        "--max-events",
        type=int,
        default=200,
        help="Stop after this many replies (default 200).",
    )
    p.add_argument(
        "--timeout-secs",
        type=float,
        default=2.0,
        help="Quiet-period after which we stop reading.",
    )
    p.set_defaults(func=_cmd_subscribe)


def _cmd_subscribe(args) -> int:
    import socket as _socket

    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("subscribe is implemented only for the modern protocol")

    with _modern_session(args) as sess:
        sess.authenticate(user, password)
        req = (
            m.Message()
            .set_to(2, 2)
            .add_u32_array(m.SYS_FROM, list(DEFAULT_FROM))
            .set_command(7)
            .set_request_id(sess.next_request_id())
            .set_reply_expected(True)
            .add_string(0x000001, args.name)
        )
        sess.stamp_client_ident(req)
        sess.send(req)

        sess._sock.settimeout(args.timeout_secs)  # tighten while we drain
        seen = 0
        while seen < args.max_events:
            try:
                reply = sess.recv()
            except _socket.timeout:
                break
            except Exception as e:
                print(f"recv stopped: {type(e).__name__}: {e}")
                break
            seen += 1
            print(f"--- event {seen} ---")
            for code, t, v in reply:
                print(f"  field {code:#08x} tag {t:#04x} = {_pretty(v)}")
        print(f"# {seen} replies read")
    return 0


def _add_download_cmd(sub) -> None:
    p = sub.add_parser(
        "download",
        help="Fetch a file from the device.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument("--file", required=True, help="Remote file path")
    p.add_argument(
        "--output",
        type=Path,
        help="Local path to save to (defaults to basename of remote file).",
    )
    p.add_argument(
        "--handler",
        default="2,2",
        help="Comma-separated handler path for file IO. Default '2,2', the "
             "mproxy file handler files are saved through; '72,1' answers "
             "NOTALLOWED on 7.23.",
    )
    p.set_defaults(func=_cmd_download)


def _cmd_download(args) -> int:
    out = args.output or Path(os.path.basename(args.file.rstrip("/")) or "download.bin")
    user, password = _resolve_creds(args)
    handler = tuple(int(x, 0) for x in args.handler.split(","))
    if len(handler) != 2:
        raise WinboxError(f"--handler must be exactly two values, got {args.handler!r}")
    data = _fetch_file(args, user, password, args.file, handler=handler)
    out.write_bytes(data)
    print(f"wrote {len(data)} bytes to {out}")
    return 0


def _add_fetch_plugins_cmd(sub) -> None:
    p = sub.add_parser(
        "fetch-plugins",
        help="Bulk-download a list of files into a local directory.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument(
        "--names",
        required=True,
        help="Comma-separated list of remote file paths to fetch.",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="Local directory to write files into (created if missing).",
    )
    p.set_defaults(func=_cmd_fetch_plugins)


def _cmd_fetch_plugins(args) -> int:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    user, password = _resolve_creds(args)

    with _open_session(args, user, password) as sess:
        for remote in names:
            local = args.out_dir / os.path.basename(remote.rstrip("/"))
            try:
                data = sess.fetch_file(remote)
            except (WinboxError, ModernProtocolError) as e:
                print(f"  ! {remote}: {e}", file=sys.stderr)
                continue
            local.write_bytes(data)
            print(f"  + {remote} -> {local} ({len(data)} bytes)")
    return 0


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


class _SessionCM:
    """Tiny context manager that holds an authenticated session of either kind."""

    def __init__(self, session, host) -> None:
        self._session = session
        self._host = host

    def __enter__(self):
        return self._session

    def __exit__(self, exc_type, exc, tb):
        self._session.close()


def _session_transport(args) -> str:
    return getattr(args, "transport", "auto") or "auto"


def _session_client_ident(args) -> str:
    return resolve_client_ident(getattr(args, "client_ident", None))


def _modern_session(args) -> ModernSession:
    sess = ModernSession(
        args.host,
        args.port,
        args.timeout,
        transport=_session_transport(args),
        client_ident=_session_client_ident(args),
    )
    _start_audit(args, sess)
    return sess


def _open_session(args, user: str | None, password: str | None) -> _SessionCM:
    if args.protocol == "modern":
        sess = _modern_session(args)
        sess.connect()
        try:
            sess.authenticate(user, password)
        except Exception:
            sess.close()
            raise
        return _SessionCM(sess, args.host)

    sess = WinboxSession(args.host, args.port, args.timeout)
    sess.connect()
    try:
        if args.protocol == "legacy":
            sess.login(user, password)
        # legacy-unauth: skip login entirely
    except Exception:
        sess.close()
        raise
    return _SessionCM(sess, args.host)


def _add_mirror_cmd(sub) -> None:
    p = sub.add_parser(
        "mirror",
        help="Fetch the router's plugin index and mirror referenced files into "
             "a local cache directory named '<version>-<build_id>'. Skips files "
             "already present with matching size and CRC.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument(
        "--out-root",
        type=Path,
        default=Path("."),
        help="Parent directory under which the version-build cache dir is "
             "created (default: current directory).",
    )
    p.set_defaults(func=_cmd_mirror)


def _cmd_mirror(args) -> int:
    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("mirror is only implemented for the modern protocol")

    with _modern_session(args) as sess:
        sess.authenticate(user, password)

        index_bytes = sess.fetch_file("list", handler=(2, 2))
        entries, version, build_id = _parse_plugin_index(index_bytes)

        cache_dir = args.out_root / f"{version}-{build_id}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        print(f"# cache dir: {cache_dir}")
        (cache_dir / "list").write_bytes(index_bytes)

        downloaded, cached, failed = sch.download_plugin_set(
            entries, build_id, str(cache_dir),
            lambda name: sess.fetch_file(name, handler=(2, 2)),
            log=print,
        )
        print(f"# done: {downloaded} downloaded, {cached} cached, {failed} failed")
    return 0 if failed == 0 else 1


def _add_terminal_cmd(sub) -> None:
    p = sub.add_parser(
        "terminal",
        help="Interactive router console over the Winbox protocol. Cols/rows "
             "default to your local terminal size and are forwarded on "
             "SIGWINCH so /export wraps correctly when you resize.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument("--cols", type=int, default=None,
                   help="Initial terminal columns (default: detected).")
    p.add_argument("--rows", type=int, default=None,
                   help="Initial terminal rows (default: detected).")
    p.set_defaults(func=_cmd_terminal)


def _cmd_terminal(args) -> int:
    from .proto.terminal import run_terminal

    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("terminal is only implemented for the modern protocol")

    cols, rows = args.cols, args.rows
    if cols is None or rows is None:
        try:
            sz = os.get_terminal_size()
            if cols is None: cols = sz.columns
            if rows is None: rows = sz.lines
        except OSError:
            if cols is None: cols = 80
            if rows is None: rows = 24

    return run_terminal(
        host=args.host,
        port=args.port,
        user=user,
        password=password,
        cols=cols,
        rows=rows,
        client_ident=_session_client_ident(args),
    )


def _fetch_file(
    args,
    user: str | None,
    password: str | None,
    remote: str,
    handler: tuple[int, int] | None = None,
) -> bytes:
    with _open_session(args, user, password) as sess:
        if isinstance(sess, ModernSession):
            # The chunked open-and-read: the one-shot fetch serves the plugin
            # set and answers a user's file with "cannot open source file".
            return sess.download_file(remote, handler=handler or (2, 2))
        return sess.fetch_file(remote)


def _add_export_cmd(sub) -> None:
    p = sub.add_parser(
        "export",
        help="Capture a text /export from one device and print or save it. "
             "Read-only: the config comes off the console in memory, so "
             "nothing is written to the device's flash.",
    )
    _add_common_conn_args(p)
    _add_auth_args(p)
    _add_audit_args(p)
    p.add_argument(
        "--output", "-o", metavar="PATH",
        help="Write the export here instead of stdout.",
    )
    p.add_argument(
        "--no-show-sensitive", action="store_true",
        help="Ask the device to hide secrets. The result reads fine and "
             "cannot be restored from -- PSKs and PPP secrets come out blank. "
             "Default is to include them.",
    )
    p.add_argument(
        "--redact", action="store_true",
        help="Mask secret values locally after capture. Unlike "
             "--no-show-sensitive this still needs the device to send them, "
             "so it is for sharing a config, not for storing one.",
    )
    p.add_argument(
        "--command", metavar="CMD",
        help="Console command to run instead of the measured default "
             f"({cexp.EXPORT_COMMAND_SENSITIVE!r}). Taken as given: no "
             "fallback is attempted if the console refuses it.",
    )
    p.add_argument(
        "--stable", action="store_true",
        help="Drop the capture time from the header. Two exports of an "
             "untouched router then compare equal, which is what you want if "
             "the file is going into version control or a diff.",
    )
    p.add_argument(
        "--export-timeout", type=float, default=90.0, metavar="SECS",
        help="Cap on the console read itself (default: 90). Separate from "
             "--timeout, which bounds the connection.",
    )
    p.set_defaults(func=_cmd_export)


def _cmd_export(args) -> int:
    from .proto.device import DeviceClient, DeviceError

    user, password = _resolve_creds(args)
    if args.protocol != "modern":
        raise WinboxError("export is only implemented for the modern protocol")

    client = DeviceClient(
        args.host, user, password,
        port=args.port,
        timeout=args.timeout,
        transport=args.transport,
        readonly=True,
        client_ident=_session_client_ident(args),
        audit=getattr(args, "audit", None),
        audit_level=getattr(args, "audit_level", None),
        audit_path=getattr(args, "audit_file", None),
    )
    try:
        # An export needs no menu map, so do not make one a precondition.
        client.connect(need_schema=False)
        result = client.export_config(
            command=args.command,
            show_sensitive=not args.no_show_sensitive,
            timeout=args.export_timeout,
        )
    except DeviceError as e:
        raise WinboxError(str(e)) from e
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    text = result.stable_text if args.stable else result.text
    if args.redact:
        text = cexp.redact(text)
    if not text.endswith("\n"):
        text += "\n"

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0600: unless it was redacted, this file holds the device's secrets.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(
            f"{path}: {len(result.body.splitlines())} config lines "
            f"({result.command}), sha256:{result.digest[:12]}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)

    if result.spliced_logs:
        print(
            f"x3ctl: note — the device logged {len(result.spliced_logs)} "
            "line(s) into the console during the capture; they were removed "
            "from the config but the read was disturbed",
            file=sys.stderr,
        )
    return 0


def _default_cdb_path() -> str | None:
    """Where the address book (Addresses.cdb) usually is on this machine."""
    env = os.environ.get("X3_CDB")
    if env:
        return env
    try:
        return _find_cdb(None)
    except WinboxError:
        return None


def _add_backup_cmd(sub) -> None:
    from .proto.fleet import DEFAULT_SKIP_GROUPS

    found = _default_cdb_path()
    p = sub.add_parser(
        "backup",
        help="Back up every router in an address book (Addresses.cdb) into a "
             "git-tracked archive. Read-only; one commit per run.",
        description=(
            "Reads Addresses.cdb, captures a /export terse plus an identity "
            "record from each router over the Winbox protocol, and writes them "
            "into a git repository -- one file per router, one commit per run. "
            "Nothing is written to any device."
        ),
    )
    p.add_argument(
        "--cdb", default=found, required=found is None, metavar="PATH",
        help="Address book (Addresses.cdb) to read the fleet from."
             + (f" Default: {found}" if found else " No default found here."),
    )
    p.add_argument(
        "--archive", "-a", required=True, metavar="DIR",
        help="Archive directory. Created (mode 0700) and git-initialised if "
             "it does not exist.",
    )
    p.add_argument(
        "--match", default="", metavar="REGEX",
        help="Only back up routers whose address-book note matches this.",
    )
    p.add_argument(
        "--skip-group", action="append", default=None, metavar="NAME",
        help="Address-book group to leave out. Repeatable. Default: "
             + ", ".join(DEFAULT_SKIP_GROUPS),
    )
    p.add_argument(
        "--allow-mac", action="store_true",
        help="Also try entries whose host is a MAC address. Only works from "
             "the router's own LAN segment; skipped by default so a remote "
             "run does not stall on them.",
    )
    p.add_argument(
        "--concurrency", "-j", type=int, default=4, metavar="N",
        help="Routers to work on at once (default: 4).",
    )
    p.add_argument(
        "--retries", type=int, default=2, metavar="N",
        help="Connection attempts per router (default: 2).",
    )
    p.add_argument("--timeout", type=float, default=10.0, metavar="SECS",
                   help="Connection timeout per attempt (default: 10).")
    p.add_argument("--export-timeout", type=float, default=90.0, metavar="SECS",
                   help="Cap on each console read (default: 90).")
    p.add_argument(
        "--no-show-sensitive", action="store_true",
        help="Hide secrets in the exports. The archive becomes a reference "
             "rather than something you can restore from.",
    )
    p.add_argument("--no-git", action="store_true",
                   help="Write the files without version control.")
    p.add_argument("--no-commit", action="store_true",
                   help="Update the files but leave the changes uncommitted.")
    p.add_argument(
        "--list", action="store_true",
        help="Print the routers that would be backed up, and exit. Connects "
             "to nothing.",
    )
    p.add_argument("--json", action="store_true",
                   help="Print the run summary as JSON on stdout.")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Only report changes, failures and the summary.")
    p.set_defaults(func=_cmd_backup)


def _cmd_backup(args) -> int:
    from .proto import fleet

    skip_groups = (
        args.skip_group if args.skip_group is not None else list(fleet.DEFAULT_SKIP_GROUPS)
    )

    if args.list:
        targets, skipped = fleet.load_targets(
            args.cdb, skip_groups=skip_groups, match=args.match,
            allow_mac=args.allow_mac,
        )
        for target in targets:
            print(f"{target.label}\t{target.host}:{target.port}\t{target.login}")
        print(
            f"\n{len(targets)} router(s) to back up, {len(skipped)} skipped.",
            file=sys.stderr,
        )
        return 0

    def on_event(kind: str, result) -> None:
        if kind == "skipped":
            if not args.quiet:
                print(f"  skip  {result.target.label}: {result.reason}",
                      file=sys.stderr)
            return
        if result.status == "failed":
            print(f"  FAIL  {result.target.label}: {result.reason}",
                  file=sys.stderr)
        elif result.changed:
            what = []
            if result.config_changed:
                what.append("config")
            if result.info_changed:
                what.append("info")
            print(
                f"  CHANGED {result.target.label} "
                f"({'+'.join(what)}, {result.config_lines} lines, "
                f"{result.version})",
                file=sys.stderr,
            )
        elif not args.quiet:
            print(
                f"  ok    {result.target.label} "
                f"({result.config_lines} lines, {result.version})",
                file=sys.stderr,
            )

    summary = fleet.run_backup(
        args.cdb,
        args.archive,
        skip_groups=skip_groups,
        match=args.match,
        allow_mac=args.allow_mac,
        concurrency=args.concurrency,
        show_sensitive=not args.no_show_sensitive,
        timeout=args.export_timeout,
        connect_timeout=args.timeout,
        retries=args.retries,
        use_git=not args.no_git,
        commit=not args.no_commit,
        on_event=on_event,
    )

    if args.json:
        print(json.dumps(_summary_json(summary), indent=2))
    else:
        _print_backup_summary(summary)
    return summary.exit_code


def _summary_json(summary) -> dict:
    return {
        "archive": str(summary.archive_root),
        "elapsed_seconds": round(summary.elapsed, 1),
        "reached": len(summary.succeeded),
        "changed": len(summary.changed),
        "failed": len(summary.failed),
        "skipped": len(summary.skipped),
        "vcs_error": summary.vcs_error,
        "commit": (
            {
                "committed": summary.commit.committed,
                "sha": summary.commit.sha,
                "files_changed": summary.commit.files_changed,
                "error": summary.commit.error,
            }
            if summary.commit is not None
            else None
        ),
        "routers": [
            {
                "name": r.target.label,
                "host": f"{r.target.host}:{r.target.port}",
                "status": r.status,
                "reason": r.reason,
                "config_changed": r.config_changed,
                "info_changed": r.info_changed,
                "firmware": r.version,
                "model": r.model,
                "serial": r.serial,
                "config_lines": r.config_lines,
                "seconds": round(r.elapsed, 1),
            }
            for r in summary.results
        ],
    }


def _print_backup_summary(summary) -> None:
    print(f"\narchive: {summary.archive_root}", file=sys.stderr)
    print(
        f"{len(summary.succeeded)} reached, {len(summary.changed)} changed, "
        f"{len(summary.failed)} failed, {len(summary.skipped)} skipped "
        f"in {summary.elapsed:.0f}s",
        file=sys.stderr,
    )
    if summary.vcs_error:
        print(f"version control: {summary.vcs_error}", file=sys.stderr)
    if summary.commit is not None:
        if summary.commit.error:
            print(f"commit failed: {summary.commit.error}", file=sys.stderr)
        elif summary.commit.committed:
            print(
                f"committed {summary.commit.sha} "
                f"({summary.commit.files_changed} file(s))",
                file=sys.stderr,
            )
        else:
            print("nothing changed; no commit made", file=sys.stderr)
    if summary.disturbed:
        print(
            f"{len(summary.disturbed)} capture(s) had log lines spliced in; "
            "those exports were cleaned but read while the device was talking",
            file=sys.stderr,
        )
    for result in summary.failed:
        print(f"failed: {result.target.label}: {result.reason}", file=sys.stderr)


def _add_neighbors_cmd(sub) -> None:
    from .proto import mndp

    p = sub.add_parser(
        "neighbors",
        help="Discover routers on the LAN via MNDP broadcast "
             "(UDP 5678). No credentials or --host required.",
    )
    mndp.add_arguments(p)


# ---------------------------------------------------------------------------
# audit -- read back a recorded connection
# ---------------------------------------------------------------------------


def _add_mcp_cmd(sub) -> None:
    p = sub.add_parser(
        "mcp",
        help="Serve the saved address book and its routers to an AI assistant "
             "over MCP (stdio). Read-only: it cannot change a router.",
    )
    p.add_argument("--cdb", help="Addresses.cdb to read (default: the one x3 finds).")
    p.set_defaults(func=_cmd_mcp)


def _cmd_mcp(args) -> int:
    from .mcp.__main__ import main as mcp_main

    return mcp_main(["--cdb", args.cdb] if args.cdb else [], prog="x3ctl mcp")


def _add_audit_cmd(sub) -> None:
    p = sub.add_parser(
        "audit",
        help="Read an audit file written by --audit: summary by default, "
             "or a chronological frame-by-frame listing.",
    )
    p.add_argument("path", help="Path to a .wba audit file.")
    p.add_argument(
        "--list", action="store_true",
        help="One line per record, in time order.",
    )
    p.add_argument(
        "--decode", action="store_true",
        help="Expand each frame's fields. Names them from the schema when "
             "--schema-dir is given, otherwise prints raw field codes.",
    )
    p.add_argument(
        "--schema-dir",
        help="Directory of .jg files ('<version>-<build_id>') used to name "
             "fields under --decode.",
    )
    p.add_argument(
        "--path-filter", metavar="X,Y",
        help="Only records whose handler path is this, e.g. '14,1'.",
    )
    p.add_argument(
        "--writes", action="store_true",
        help="Only the frames that changed the device, and their replies.",
    )
    p.add_argument(
        "--no-polls", action="store_true",
        help="Hide the 1 Hz statistics polling. Only files recorded at level "
             "'stats' or 'all' contain any; they stay flagged, so reading is "
             "where you choose whether to see them.",
    )
    p.add_argument(
        "--only-polls", action="store_true",
        help="Show only the statistics polling — a counter series on its own.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit one JSON object per record on stdout, for other tools.",
    )
    p.set_defaults(func=_cmd_audit)


def _audit_where(rec, msg) -> tuple[int, ...]:
    """The handler a frame concerns.

    Which field holds it depends on the direction: a request addresses the
    handler in SYS_TO, and the reply comes back *from* it with our own path in
    SYS_TO. Reading SYS_TO either way files every reply under the client's
    from-path, which is not a handler at all.
    """
    if msg is None:
        return ()
    order = (
        (m.SYS_FROM, m.SYS_TO)
        if rec.type == audit.REC_RECV
        else (m.SYS_TO, m.SYS_FROM)
    )
    for code in order:
        hit = msg.find(code)
        if hit and isinstance(hit[1], (list, tuple)) and len(hit[1]) > 1:
            return tuple(int(x) for x in hit[1])
    return ()


def _cmd_audit(args) -> int:
    # This is the one command built to be piped into head / grep / jq, so a
    # closed pipe is the caller being done, not an error to report.
    try:
        return _audit_read(args)
    except BrokenPipeError:
        return 0
    finally:
        # The last flush has to happen here rather than be left to interpreter
        # shutdown. Piped stdout is block-buffered, so the write that finds the
        # pipe closed is often that final flush -- and by then there is no frame
        # left to catch it in, only "Exception ignored on flushing sys.stdout"
        # and exit 120. Flushing while we still have a handler, then pointing
        # the fd at /dev/null so the shutdown flush has somewhere to go, keeps
        # `x3ctl audit … | head` silent and zero-status.
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            try:
                sys.stdout.flush()
            except OSError:
                pass


def _audit_read(args) -> int:
    try:
        af = audit.AuditFile(args.path)
    except (OSError, ValueError) as e:
        raise WinboxError(str(e)) from e

    schema = None
    if args.decode and args.schema_dir:
        schema = sch.Schema.load(args.schema_dir)

    want = None
    if args.path_filter:
        want = tuple(int(x, 0) for x in args.path_filter.split(",") if x.strip())

    if args.no_polls and args.only_polls:
        raise WinboxError("--no-polls and --only-polls contradict each other")

    if not (args.list or args.decode or args.json):
        return _audit_summary(af)

    t0 = af.header.get("started") or 0.0
    # A reply carries no SYS_COMMAND, so --writes cannot recognise one on its
    # own. Remember each write's request id and let its answer through -- "did
    # the device accept it" is most of what the filter is for.
    write_rids: set[int] = set()
    for rec in af:
        if rec.is_wire:
            try:
                msg = rec.message()
            except Exception as e:  # noqa: BLE001
                msg = None
                if not args.json:
                    print(f"  !! undecodable {rec.name} payload: {e}", file=sys.stderr)
            where = _audit_where(rec, msg)
            cmd = msg.get_u32(m.SYS_COMMAND) if msg is not None else None
            rid = msg.get_u32(m.SYS_REQUEST_ID) if msg is not None else None
            poll = bool(rec.flags & audit.F_POLL)
            if args.no_polls and poll:
                continue
            if args.only_polls and not poll:
                continue
            if want is not None and where[: len(want)] != want:
                continue
            if args.writes:
                if cmd in audit.WRITE_COMMANDS:
                    if rid is not None:
                        write_rids.add(rid)
                elif not (rec.type == audit.REC_RECV and rid in write_rids):
                    continue
        else:
            if want is not None or args.writes or args.only_polls:
                continue
            msg = None
            where = ()
            cmd = None

        if args.json:
            print(json.dumps(_audit_json(rec, msg, where, cmd)))
            continue

        arrow = {audit.REC_SENT: "-->", audit.REC_RECV: "<--"}.get(rec.type, "   ")
        marks = "".join((
            "P" if rec.flags & audit.F_POLL else "",
            "D" if rec.flags & audit.F_DUP else "",
            "R" if rec.flags & audit.F_REDACTED else "",
        ))
        stamp = f"{rec.ts - t0:9.3f}s" if t0 else time.strftime(
            "%H:%M:%S", time.localtime(rec.ts)
        )
        if not rec.is_wire:
            body = rec.text() if rec.type == audit.REC_NOTE else repr(rec.json())
            print(f"{stamp} {rec.name:<10} {body}")
            continue
        where_s = ",".join(str(x) for x in where) or "-"
        err = ""
        if msg is not None and msg.has_error():
            err = f"  ERROR {msg.error_string()}"
        print(
            f"{stamp} {arrow} {('[' + where_s + ']'):<12}"
            f" {audit.command_name(cmd):<11} {len(rec.payload):>6}B"
            f" {marks:<3}{err}"
        )
        if args.decode and msg is not None:
            if schema is not None and where:
                _print_decoded(schema, where, msg, indent="             ")
            else:
                for code, t, v in msg:
                    print(f"             {code:#08x} tag {t:#04x} = {_pretty(v)}")
    return 0


def _audit_json(rec, msg, where, cmd) -> dict:
    out = {
        "type": rec.name,
        "ts": rec.ts,
        "flags": rec.flags,
        "bytes": len(rec.payload),
    }
    if rec.is_wire:
        out["path"] = list(where)
        out["command"] = audit.command_name(cmd)
        out["poll"] = bool(rec.flags & audit.F_POLL)
        out["m2"] = rec.payload.hex()
        if msg is not None and msg.has_error():
            out["error"] = msg.error_string()
    elif rec.type == audit.REC_NOTE:
        out["text"] = rec.text()
    else:
        out["data"] = rec.json()
    return out


def _audit_summary(af) -> int:
    hdr = af.header
    print(f"{af.path}")
    print(f"  container       wbaudit v{af.version}")
    print(f"  device          {hdr.get('user')}@{hdr.get('host')}:{hdr.get('port')}")
    print(f"  started         {hdr.get('started_local')}")
    print(f"  level           {hdr.get('level')}")
    for key in ("client_ident", "transport", "readonly", "command", "schema"):
        if key in hdr:
            print(f"  {key:<15} {hdr[key]}")

    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    per_path: dict[tuple, list[int]] = {}
    errors: list[str] = []
    bad = 0
    footer = None
    first = last = None
    for rec in af:
        counts[rec.name] = counts.get(rec.name, 0) + 1
        sizes[rec.name] = sizes.get(rec.name, 0) + len(rec.payload)
        first = rec.ts if first is None else first
        last = rec.ts
        if rec.type == audit.REC_END:
            footer = rec.json()
        if not rec.is_wire:
            continue
        try:
            msg = rec.message()
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        where = _audit_where(rec, msg)
        cmd = msg.get_u32(m.SYS_COMMAND)
        slot = per_path.setdefault((where, audit.command_name(cmd)), [0, 0])
        slot[0] += 1
        slot[1] += len(rec.payload)
        if msg.has_error():
            errors.append(
                f"    [{','.join(str(x) for x in where)}] "
                f"{audit.command_name(cmd)}: {msg.error_string()}"
            )

    if first is not None and last is not None:
        print(f"  span            {last - first:.1f}s")
    print("\n  records")
    for name in sorted(counts):
        print(f"    {name:<12} {counts[name]:>7}  {sizes[name]:>12,} payload bytes")
    if bad:
        print(f"    !! {bad} undecodable frame(s)")
    if footer:
        print("\n  suppressed (recorded as counts, not frames)")
        for key in ("suppressed_polls", "suppressed_unchanged", "suppressed_reads"):
            print(f"    {key[11:]:<12} {footer.get(key, 0):>7}")
        if footer.get("reason"):
            print(f"    reason       {footer['reason']}")
    else:
        print("\n  !! no END record: the writer did not close cleanly")

    if per_path:
        print("\n  busiest handlers")
        top = sorted(per_path.items(), key=lambda kv: -kv[1][1])[:15]
        for (where, cmd), (n, nbytes) in top:
            where_s = ",".join(str(x) for x in where) or "-"
            print(f"    [{where_s:<10}] {cmd:<12} {n:>6} frames {nbytes:>12,} B")
    if errors:
        print(f"\n  {len(errors)} device error(s)")
        for line in errors[:20]:
            print(line)
    return 0
