"""Back up every router in a Winbox address book.

Reads ``Addresses.cdb`` -- the same file the Winbox client saves connections
in, so the inventory is the one already being maintained by hand -- and for
each entry captures a ``/export terse`` and a small identity record into a
git-backed :mod:`x3.proto.archive`.

Everything here is a pure read. Nothing is created on the device and nothing
is written to its flash: the export comes off the console channel in memory
(see :meth:`DeviceClient.export_config`) and the identity comes from GETs
addressed by field code. There is nothing to clean up if a run dies halfway.

What goes in the archive, and what deliberately does not
--------------------------------------------------------

Two artifacts per router: ``<name>.rsc``, the export, and ``<name>.info.json``,
the identity -- model, serial, firmware, MACs, addresses.

Both are things that change when *someone changes them*, which is what makes a
commit per run readable: ``git log`` is a list of the nights something actually
happened. Running state is the opposite and is left out on purpose. CPU load,
free memory and uptime are in :class:`DeviceInfo` and are not recorded; a port
going up and down, a DHCP lease renewing or a traffic counter advancing would
put a commit on every router every night, and the signal -- someone edited a
firewall rule -- would be lost in it. State like that is worth collecting, but
into something built for a time series, not into a config history.

Notes on the address book itself
--------------------------------

The ``host`` field carries the Winbox port (``1.2.3.4:41002``), and this
connects over the Winbox protocol, so that port is used as written. The old
SSH-based backup had to subtract one from it to find a paired SSH port; that
convention is gone with the transport that needed it.

An entry whose host is a MAC address is a MAC-Winbox connection, reachable only
from the router's own layer-2 segment. Those are skipped unless ``allow_mac``
is set, because a nightly run against remote customers would otherwise spend a
transport timeout per entry probing every local interface for something that
was never going to answer.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .archive import Archive, CommitResult
from .cdb import CdbFile
from .device import DeviceClient, DeviceError
from .macwinbox import looks_like_mac

#: Winbox's own default port, used when the address book entry names none.
DEFAULT_PORT = 8291

#: Address-book groups that mean "do not connect to this any more".
DEFAULT_SKIP_GROUPS = ("gone", "disabled")

#: Per-router ceiling. An export is one console round trip plus however long
#: The router takes to print a few thousand lines; a minute and a half is
#: generous for that and still bounds a stuck run.
DEFAULT_TIMEOUT = 90.0


@dataclass
class Target:
    """One router to back up, as the address book describes it."""

    note: str
    host: str
    port: int = DEFAULT_PORT
    login: str = ""
    password: str = ""
    #: The password exactly as the book stored it, when it has one. The login
    #: hashes these bytes rather than ``password`` so a secret in a single-byte
    #: codepage survives -- ``password`` is a lossy UTF-8 view fit for display.
    #: See cdb.CdbEntry.password_bytes and ModernSession.authenticate.
    password_bytes: bytes | None = None
    group: str = ""
    via_mac: bool = False

    @property
    def label(self) -> str:
        """What to call this router in output. The note, or the address."""
        return self.note or f"{self.host}:{self.port}"

    @property
    def key(self) -> str:
        """What distinguishes this entry from another of the same name.

        The address, not the device serial: a stem has to be decided before
        anything is connected to, and it has to come out the same next run.
        """
        return f"{self.host}:{self.port}"


@dataclass
class RouterResult:
    """The outcome for one router."""

    target: Target
    #: ``ok`` | ``unchanged`` | ``failed`` | ``skipped``
    status: str = "failed"
    reason: str = ""
    config_changed: bool = False
    info_changed: bool = False
    version: str = ""
    model: str = ""
    serial: str = ""
    identity: str = ""
    config_lines: int = 0
    command: str = ""
    #: Log lines the router spliced into the console mid-capture, if any.
    disturbed: bool = False
    elapsed: float = 0.0
    attempts: int = 1

    @property
    def changed(self) -> bool:
        return self.config_changed or self.info_changed

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "unchanged"}


@dataclass
class Summary:
    """The outcome for a whole run."""

    results: list[RouterResult] = field(default_factory=list)
    skipped: list[RouterResult] = field(default_factory=list)
    commit: CommitResult | None = None
    archive_root: Path | None = None
    vcs_error: str = ""
    elapsed: float = 0.0

    @property
    def succeeded(self) -> list[RouterResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[RouterResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def changed(self) -> list[RouterResult]:
        return [r for r in self.results if r.changed]

    @property
    def disturbed(self) -> list[RouterResult]:
        return [r for r in self.results if r.disturbed]

    @property
    def exit_code(self) -> int:
        """0 when every router was reached, 1 when any failed.

        A timer reports failure through the exit status, so this is the whole
        interface between a nightly run and ``systemctl --user status``.
        """
        return 1 if self.failed else 0


def split_host(raw: str) -> tuple[str, int]:
    """``"1.2.3.4:41002"`` -> ``("1.2.3.4", 41002)``.

    IPv6 in brackets is understood; a bare IPv6 address is not split, since
    every colon in it would look like a port separator.
    """
    text = (raw or "").strip()
    bracketed = re.match(r"^\[(?P<host>[^\]]+)\](?::(?P<port>\d+))?$", text)
    if bracketed:
        port = bracketed.group("port")
        return bracketed.group("host"), int(port) if port else DEFAULT_PORT
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            return host, int(port)
        # A non-numeric port is a malformed entry, not a hostname containing a
        # colon. Keep the host and fall back rather than failing the router.
        return host, DEFAULT_PORT
    return text, DEFAULT_PORT


def load_targets(
    cdb_path: str | Path,
    *,
    skip_groups: Sequence[str] = DEFAULT_SKIP_GROUPS,
    match: str = "",
    allow_mac: bool = False,
) -> tuple[list[Target], list[RouterResult]]:
    """Read the address book into targets, and the entries left out.

    Returns ``(targets, skipped)`` -- skipped entries come back as finished
    :class:`RouterResult` records so a report can account for every line of the
    address book rather than silently shrinking.
    """
    book = CdbFile.load(str(cdb_path))
    pattern = re.compile(match) if match else None
    lowered = {g.strip().lower() for g in skip_groups if g.strip()}

    targets: list[Target] = []
    skipped: list[RouterResult] = []

    for entry in book:
        note = entry.note or ""
        raw_host = entry.host or ""
        group = entry.group or ""
        host, port = split_host(raw_host)
        target = Target(
            note=note,
            host=host,
            port=port,
            login=entry.login or "",
            password=entry.password or "",
            password_bytes=entry.password_bytes,
            group=group,
            via_mac=looks_like_mac(host),
        )

        def skip(reason: str) -> None:
            skipped.append(
                RouterResult(target=target, status="skipped", reason=reason)
            )

        if not raw_host:
            skip("address book entry has no host")
        elif group.strip().lower() in lowered:
            skip(f"group {group!r} is in the skip list")
        elif pattern is not None and not pattern.search(note):
            skip(f"note does not match {match!r}")
        elif target.via_mac and not allow_mac:
            skip("MAC-Winbox entry (reachable only on its own LAN segment)")
        elif not target.login:
            skip("address book entry has no login")
        else:
            targets.append(target)

    return targets, skipped


def _info_record(client: DeviceClient) -> dict:
    """The identity fields worth keeping, and nothing that moves on its own.

    See the module docstring: anything that changes without a human is left
    out, so a diff of this file always means something.
    """
    info = client.info
    fingerprint = client.read_fingerprint()
    return {
        "identity": fingerprint.identity or info.identity,
        "model": fingerprint.model or info.board,
        "serial": fingerprint.serial,
        "system_id": fingerprint.system_id,
        "firmware": info.version,
        "architecture": info.architecture,
        "platform": info.platform,
        "macs": sorted(fingerprint.macs),
        "addresses": sorted(fingerprint.ips),
    }


#: A line of an export: a menu path (``/ip address add ...``). The console's
#: help also starts lines with ``/`` -- ``/               Move up to base
#: level``, ``/command        Use command at the base level`` -- so a
#: separating run of spaces right after the path rules a line out.
_MENU_LINE_RE = re.compile(r"^/(?!command\s)[a-z0-9-]")


def backup_target(
    target: Target,
    *,
    show_sensitive: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = 10.0,
    retries: int = 1,
    client_factory: Callable[..., DeviceClient] | None = None,
) -> tuple[RouterResult, str, dict]:
    """Connect to one router and read it.

    Returns ``(result, config_text, info)``. Nothing is written here -- the
    caller owns the archive, so that all of its file handling stays on one
    thread while these run on many.
    """
    started = time.monotonic()
    result = RouterResult(target=target)
    make = client_factory or DeviceClient
    last_error = ""

    for attempt in range(1, max(1, retries) + 1):
        result.attempts = attempt
        client = make(
            target.host,
            target.login,
            target.password,
            password_bytes=target.password_bytes,
            port=target.port,
            timeout=connect_timeout,
            readonly=True,
        )
        try:
            # No schema: an export needs no menu map, and demanding one would
            # fail every router whose firmware was never mirrored.
            client.connect(need_schema=False)
            export = client.export_config(
                show_sensitive=show_sensitive, timeout=timeout
            )
            if not any(_MENU_LINE_RE.match(x) for x in export.body.splitlines()):
                # Every export line is a menu path. A capture without one is
                # the login banner and console help, not a config (seen on
                # a 6.44 router); archiving it reads as a change.
                raise DeviceError(
                    f"export held no config ({len(export.body.splitlines())} "
                    "lines, no menu path)"
                )
            info = _info_record(client)

            result.status = "ok"
            result.version = info.get("firmware", "")
            result.model = info.get("model", "")
            result.serial = info.get("serial", "")
            result.identity = info.get("identity", "")
            result.config_lines = len(export.body.splitlines())
            result.command = export.command
            result.disturbed = export.disturbed
            result.elapsed = time.monotonic() - started
            # stable_text, not text: the header opens with the capture time,
            # which would commit every router every night. See configexport.
            return result, export.stable_text, info
        except (DeviceError, OSError, TimeoutError) as e:
            last_error = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            # A protocol bug on one router must not take the fleet down with
            # it; record it like any other failure and move on.
            last_error = f"{type(e).__name__}: {e}"
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if attempt < max(1, retries):
            time.sleep(min(5.0, 2.0 * attempt))

    result.status = "failed"
    result.reason = last_error or "unknown error"
    result.elapsed = time.monotonic() - started
    return result, "", {}


def run_backup(
    cdb_path: str | Path,
    archive_root: str | Path,
    *,
    skip_groups: Sequence[str] = DEFAULT_SKIP_GROUPS,
    match: str = "",
    allow_mac: bool = False,
    concurrency: int = 4,
    show_sensitive: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = 10.0,
    retries: int = 1,
    use_git: bool = True,
    commit: bool = True,
    on_event: Callable[[str, RouterResult], None] | None = None,
    client_factory: Callable[..., DeviceClient] | None = None,
) -> Summary:
    """Back up every matching router into ``archive_root``.

    ``on_event(kind, result)`` is called on the calling thread as each router
    finishes -- ``kind`` being ``done`` or ``skipped`` -- so a CLI can print
    progress without this module knowing anything about output.
    """
    started = time.monotonic()
    targets, skipped = load_targets(
        cdb_path, skip_groups=skip_groups, match=match, allow_mac=allow_mac
    )
    archive = Archive(Path(archive_root), use_git=use_git).open()
    summary = Summary(
        skipped=skipped,
        archive_root=archive.root,
        vcs_error=archive.vcs_error,
    )

    if on_event is not None:
        for item in skipped:
            on_event("skipped", item)

    # Stems are assigned up front, single-threaded, so two routers that slug to
    # the same name get disambiguated the same way on every run rather than by
    # whichever thread happened to finish first.
    stems = {
        id(t): archive.stem_for(t.label, key=t.key) for t in targets
    }

    def work(target: Target):
        return backup_target(
            target,
            show_sensitive=show_sensitive,
            timeout=timeout,
            connect_timeout=connect_timeout,
            retries=retries,
            client_factory=client_factory,
        )

    if targets:
        # Results are written here, on this thread, as they arrive: the archive
        # is not thread-safe and does not need to be, and an interrupted run
        # still leaves every router that finished before it on disk.
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            for result, config_text, info in pool.map(work, targets):
                stem = stems[id(result.target)]
                if result.status == "ok":
                    written = archive.write(stem, ".rsc", config_text)
                    result.config_changed = written.changed
                    info_written = archive.write(
                        stem, ".info.json", json.dumps(info, indent=2, sort_keys=True)
                    )
                    result.info_changed = info_written.changed
                    if not result.changed:
                        result.status = "unchanged"
                summary.results.append(result)
                if on_event is not None:
                    on_event("done", result)

    if commit and archive.git_active:
        subject, body = _commit_message(summary)
        summary.commit = archive.commit(subject, body=body)
    summary.elapsed = time.monotonic() - started
    return summary


def _commit_message(summary: Summary) -> tuple[str, str]:
    """Subject and body for this run's commit.

    The subject counts what happened; the body names the routers, so
    ``git log`` skims and ``git show`` explains.
    """
    changed = summary.changed
    failed = summary.failed
    reached = len(summary.succeeded)
    parts = [f"backup: {len(changed)} changed"]
    parts.append(f"{reached} reached")
    if failed:
        parts.append(f"{len(failed)} unreachable")
    subject = ", ".join(parts)

    lines: list[str] = []
    if changed:
        lines.append("Changed:")
        lines += [f"  {r.target.label}" for r in changed]
    if failed:
        if lines:
            lines.append("")
        lines.append("Unreachable:")
        lines += [f"  {r.target.label}: {r.reason}" for r in failed]
    return subject, "\n".join(lines)
