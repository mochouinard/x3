"""Thin adapters over x3ctl discovery / address-book (no device login)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from x3.paths import env_var

from .i18n import trf

#: Winbox's TCP port, used whenever an address names none.
DEFAULT_PORT = 8291


def split_host_port(text: str) -> tuple[str, int]:
    """``host``, ``host:port``, ``[v6]`` or ``[v6]:port`` -> (host, port).

    A bare IPv6 address has colons of its own, so it only carries a port
    inside brackets -- the URL convention.
    """
    t = text.strip()
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return host, int(port) if port.isdigit() else DEFAULT_PORT
    if t.count(":") == 1:
        host, _, port = t.partition(":")
        if port.isdigit():
            return host, int(port)
    return t, DEFAULT_PORT


# ---------------------------------------------------------------------------
# Neighbors (MNDP)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NeighborRow:
    identity: str
    address: str
    mac: str
    board: str
    version: str
    uptime: str
    interface: str


def discover_neighbors(
    timeout: float = 2.5,
    *,
    requests: int = 2,
) -> list[NeighborRow]:
    """Broadcast MNDP and return UI rows. Raises OSError / PermissionError."""
    from x3.proto import mndp

    found = mndp.discover(timeout=timeout, requests=requests)
    rows: list[NeighborRow] = []
    for n in found:
        # A real MNDP reply always carries a MAC (TLV 1). Entries without one
        # are the host's own broadcast echoing back — drop that empty self-row.
        if not n.mac:
            continue
        rows.append(
            NeighborRow(
                identity=n.identity or "",
                address=n.source_ip or n.ipv4 or "",
                mac=n.mac or "",
                board=n.board or n.platform or "",
                version=n.version or "",
                uptime=mndp._fmt_uptime(n.uptime),
                interface=n.interface or "",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Address book (.cdb)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AddressRow:
    index: int
    host: str
    login: str
    note: str
    session: str
    group: str
    password: str  # plaintext when loaded with show_passwords; may be empty


def default_cdb_candidates(*, include_chosen: bool = True) -> list[Path]:
    """Likely locations for Addresses.cdb on this machine.

    The one the user picked (settings) comes first after the environment;
    ``include_chosen=False`` is the search as if nothing had been picked.
    """
    from x3.proto.cdb import CLIENT_DIRS

    home = Path.home()
    vendor = CLIENT_DIRS[0].split("/", 1)[0]
    env = os.environ.get("X3_ADDRESSES_CDB") or os.environ.get("X3_CDB")
    out: list[Path] = []
    if env:
        out.append(Path(env).expanduser())
    from .settings import load_addresses_cdb

    chosen = load_addresses_cdb() if include_chosen else None
    if chosen:
        out.append(Path(chosen).expanduser())
    roots: list[Path] = []
    # Windows: the profile's own roaming folder, read from the environment
    # rather than guessed from the home directory (profiles get redirected).
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))
    roots += [
        home / ".wine/drive_c/users" / home.name / "AppData/Roaming",
        home / "AppData/Roaming",
        home / "Documents" / f"{vendor}Data",
        # Native Linux builds and Flatpak
        home / ".local/share",
        home / ".config",
    ]
    # Wine prefixes: any user under drive_c/users/*/AppData/...
    wine_users = home / ".wine/drive_c/users"
    if wine_users.is_dir():
        roots += [user_dir / "AppData/Roaming" for user_dir in sorted(wine_users.iterdir())]
    out += [root / d / "Addresses.cdb" for root in roots for d in CLIENT_DIRS]
    # De-dupe while preserving order
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def find_addresses_cdb(*, include_chosen: bool = True) -> Path | None:
    for p in default_cdb_candidates(include_chosen=include_chosen):
        if p.is_file():
            return p
    return None


def load_address_book(
    path: str | Path | None = None,
    *,
    grep: str | None = None,
    include_passwords: bool = True,
) -> tuple[Path | None, list[AddressRow], str | None]:
    """Load a .cdb file.

    Returns ``(resolved_path, rows, error)``. On success ``error`` is None.
    """
    from x3.proto.cdb import CdbError, CdbFile

    resolved: Path | None
    if path is None:
        resolved = find_addresses_cdb()
        if resolved is None:
            return None, [], trf("no Addresses.cdb found (set {var})",
                                 var=env_var("X3_ADDRESSES_CDB"))
    else:
        resolved = Path(path).expanduser()

    try:
        cdb = CdbFile.load(str(resolved))
    except (OSError, CdbError) as e:
        return resolved, [], str(e)

    entries = list(cdb.entries)
    if grep:
        needle = grep.lower()
        entries = [
            e
            for e in entries
            if any(
                needle in (v or "").lower()
                for v in e.named_fields(show_passwords=True).values()
            )
        ]

    rows: list[AddressRow] = []
    for idx, e in enumerate(entries):
        fields = e.named_fields(show_passwords=include_passwords)
        rows.append(
            AddressRow(
                index=idx,
                host=fields.get("host", "") or "",
                login=fields.get("login", "") or "",
                note=fields.get("note", "") or "",
                session=fields.get("session", "") or "",
                group=fields.get("group", "") or "",
                password=(fields.get("password", "") or "") if include_passwords else "",
            )
        )
    return resolved, rows, None
