"""Local connection store layered over a read-only ``Addresses.cdb``.

Why we do not write the cdb
---------------------------
The container itself is not the problem: :class:`x3.proto.cdb.CdbFile`
round-trips a real 342-entry file byte for byte, so we could write valid cdb
whenever we liked. The problem is that Winbox keeps the whole address book in
memory and rewrites the *entire file* on every change, with no lock, no journal
and no per-record update. Two writers therefore cannot share it:

* Winbox is open, we write, Winbox saves later (Add/Set, or on exit with
  Autosave Session) — our entry is gone, with nothing to show it ever existed.
* Or the reverse: we load at startup, you add three entries in Winbox, we
  write, and your three entries are gone.

That is inherent to whole-file rewrite without a lock protocol, and no care on
our side fixes it — file locking would not help either, since Winbox takes no
lock to observe.

So Addresses.cdb is treated as read-only upstream, and everything we change
lives here instead:

* **overrides** — edits to an entry that came from the cdb. Stored as only the
  fields that differ, matched back to the cdb entry by ``(host, login)``.
  Verified unique across a real 342-entry book; ``host`` alone is not (it
  collides 9 times there).
* **hidden** — cdb entries removed locally. The cdb keeps them; we stop
  showing them.
* **own** — connections we created, which have no cdb entry at all.

On load the cdb is read fresh and the overlay applied on top, so upstream edits
show through for every field we are *not* overriding. Where both changed the
same field the local value wins, and the row is flagged so the divergence is
visible rather than silent.

An override whose ``(host, login)`` no longer appears in the cdb is inert: it
matches nothing and is not displayed. :func:`Store.prune` drops those.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

#: Fields of a connection the UI can read or write.
FIELDS = ("host", "login", "password", "note", "session", "group")

#: Flag letters for the origin column, in the Winbox spirit: blank is the
#: uneventful case, so only a row that is *not* plain upstream gets a mark.
ORIGIN_FLAG = {"cdb": "", "local": "L", "new": "N"}

ORIGIN_HELP = {
    "cdb": "From Addresses.cdb",
    "local": "From Addresses.cdb, edited locally — local values win",
    "new": "Added here; not in Addresses.cdb",
}

STORE_VERSION = 1


def entry_key(host: str, login: str) -> tuple[str, str]:
    """Identity of a saved connection.

    Hostnames are case-insensitive, the router logins are not.
    """
    return ((host or "").strip().lower(), (login or "").strip())


@dataclass(frozen=True)
class Connection:
    """One row of the merged address book."""

    host: str = ""
    login: str = ""
    password: str = ""
    note: str = ""
    session: str = ""
    group: str = ""
    #: "cdb" (upstream, untouched) | "local" (upstream + our edits) | "new"
    origin: str = "cdb"

    @property
    def key(self) -> tuple[str, str]:
        return entry_key(self.host, self.login)

    @property
    def flag(self) -> str:
        return ORIGIN_FLAG.get(self.origin, "")

    @property
    def origin_help(self) -> str:
        return ORIGIN_HELP.get(self.origin, "")

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in FIELDS}


def _clean(values: dict[str, Any]) -> dict[str, str]:
    return {k: str(values.get(k) or "") for k in FIELDS}


@dataclass
class Store:
    """The local overlay. Pure data — :func:`load`/:func:`save` do the I/O."""

    #: original (host, login) -> the fields we override on it
    overrides: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    #: cdb entries removed locally
    hidden: set[tuple[str, str]] = field(default_factory=set)
    #: connections that exist only here
    own: list[dict[str, str]] = field(default_factory=list)
    #: device fingerprints (TOFU / Neighbors), keyed by serial-or-system-id.
    #: Each: {key, serial, system_id, model, identity, macs[], ips[], salt,
    #: host, login, verify, seen_at}. Independent of (host, login) so a device
    #: is recognised by MAC even after its IP changes.
    devices: list[dict[str, Any]] = field(default_factory=list)
    #: What the cdb said at the last merge, keyed by cdb identity. Not
    #: persisted — it is a snapshot of upstream, refreshed on every load, and
    #: it is what lets set_entry() record a *minimal* patch instead of copying
    #: every field into the override.
    _upstream: dict[tuple[str, str], dict[str, str]] = field(
        default_factory=dict, repr=False
    )

    # --- merge -----------------------------------------------------------

    def merge(self, cdb_rows: Iterable[Any]) -> list[Connection]:
        """Overlay the local store on a freshly read cdb.

        ``cdb_rows`` is anything with ``.host``/``.login``/… attributes
        (:class:`x3.ui.services.AddressRow`). cdb order is preserved and our
        own entries follow, so a Winbox-ordered book still reads the way Winbox
        shows it.
        """
        out: list[Connection] = []
        self._upstream.clear()
        for row in cdb_rows:
            key = entry_key(getattr(row, "host", ""), getattr(row, "login", ""))
            base = {name: str(getattr(row, name, "") or "") for name in FIELDS}
            self._upstream[key] = dict(base)
            if key in self.hidden:
                continue
            patch = self.overrides.get(key)
            if patch:
                # Only the fields we actually changed; everything else keeps
                # tracking upstream, so a note edited in Winbox still shows.
                base.update({k: v for k, v in patch.items() if k in FIELDS})
            out.append(Connection(**base, origin="local" if patch else "cdb"))

        for rec in self.own:
            out.append(Connection(**_clean(rec), origin="new"))
        return out

    # --- mutation ---------------------------------------------------------

    def set_entry(
        self, values: dict[str, Any], *, original: Connection | None = None
    ) -> Connection:
        """Save ``values``, as an override or as one of our own.

        ``original`` is the row being edited, when there is one. Editing a cdb
        row records only the fields that differ from it; editing one of ours
        rewrites it in place. With no ``original`` (or one that is ours), the
        result is a new local entry.
        """
        clean = _clean(values)
        if original is not None and original.origin in ("cdb", "local"):
            # The override is keyed on the row's *original* identity, so
            # editing the host or login does not orphan it.
            base_key = self._origin_key(original)
            upstream = self._upstream_of(original)
            patch = {k: v for k, v in clean.items() if v != upstream.get(k, "")}
            if patch:
                self.overrides[base_key] = patch
            else:
                self.overrides.pop(base_key, None)
            self.hidden.discard(base_key)
            return Connection(**clean, origin="local" if patch else "cdb")

        key = entry_key(clean["host"], clean["login"])
        for i, rec in enumerate(self.own):
            if entry_key(rec.get("host", ""), rec.get("login", "")) == key:
                self.own[i] = clean
                break
        else:
            self.own.append(clean)
        return Connection(**clean, origin="new")

    def remove(self, conn: Connection) -> None:
        """Drop a row from the merged view. The cdb file is never touched."""
        if conn.origin == "new":
            key = conn.key
            self.own = [
                r
                for r in self.own
                if entry_key(r.get("host", ""), r.get("login", "")) != key
            ]
            return
        base_key = self._origin_key(conn)
        self.overrides.pop(base_key, None)
        self.hidden.add(base_key)

    def revert(self, conn: Connection) -> bool:
        """Forget local edits to a cdb row. True if there were any."""
        base_key = self._origin_key(conn)
        had_override = self.overrides.pop(base_key, None) is not None
        had_hidden = base_key in self.hidden
        self.hidden.discard(base_key)
        return had_override or had_hidden

    def unhide_all(self) -> int:
        """Bring locally removed cdb entries back. Returns how many."""
        n = len(self.hidden)
        self.hidden.clear()
        return n

    def prune(self, cdb_rows: Iterable[Any]) -> int:
        """Drop overrides/hides that no longer match any cdb entry."""
        live = {
            entry_key(getattr(r, "host", ""), getattr(r, "login", ""))
            for r in cdb_rows
        }
        stale = [k for k in self.overrides if k not in live]
        for k in stale:
            del self.overrides[k]
        gone = {k for k in self.hidden if k not in live}
        self.hidden -= gone
        return len(stale) + len(gone)

    def is_empty(self) -> bool:
        return not (self.overrides or self.hidden or self.own or self.devices)

    # --- device fingerprints (TOFU / Neighbors) --------------------------

    def record_device(self, rec: dict) -> None:
        """Upsert a device fingerprint, merging its MAC/IP sets. Matches an
        existing record by key (serial/system-id) or by an overlapping MAC, so
        one physical box stays one record even at a new IP."""
        key = rec.get("key") or ""
        macs = {m.lower() for m in (rec.get("macs") or [])}
        found = None
        for d in self.devices:
            if key and d.get("key") == key:
                found = d
                break
            if macs and macs & {m.lower() for m in (d.get("macs") or [])}:
                found = d
                break
        rec = dict(rec)
        rec.setdefault("seen_at", int(time.time()))
        if found is None:
            found = {"verify": True}
            self.devices.append(found)
        for k, v in rec.items():
            if k not in ("macs", "ips") and v not in (None, ""):
                found[k] = v
        found["macs"] = sorted(set(found.get("macs") or []) | set(rec.get("macs") or []))
        found["ips"] = sorted(set(found.get("ips") or []) | set(rec.get("ips") or []))

    def device_by_mac(self, mac: str) -> dict | None:
        m = (mac or "").lower()
        if not m:
            return None
        for d in self.devices:
            if m in {x.lower() for x in (d.get("macs") or [])}:
                return d
        return None

    def device_for(self, host: str, login: str) -> dict | None:
        """The device last connected to as this saved (host, login), if any."""
        want = entry_key(host, login)
        for d in self.devices:
            if entry_key(d.get("host", ""), d.get("login", "")) == want:
                return d
        return None

    # --- internals --------------------------------------------------------

    def _origin_key(self, conn: Connection) -> tuple[str, str]:
        """The cdb identity an override for ``conn`` hangs off.

        A row whose host/login we already edited is displayed under the *new*
        values, so its own key no longer matches the cdb entry. Find the
        override that produced it.
        """
        for key, patch in self.overrides.items():
            host = patch.get("host", key[0])
            login = patch.get("login", key[1])
            if entry_key(host, login) == conn.key:
                return key
        return conn.key

    def _upstream_of(self, conn: Connection) -> dict[str, str]:
        """What the cdb says for this row, straight from the merge snapshot."""
        return self._upstream.get(self._origin_key(conn), {})


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def store_path() -> Path:
    from .settings import config_dir

    return config_dir() / "connections.json"


def load(path: Path | None = None) -> Store:
    p = path or store_path()
    if not p.is_file():
        return Store()
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return Store()
    if not isinstance(data, dict):
        return Store()

    store = Store()
    # Records rather than a keyed dict: the key is a (host, login) pair, and
    # packing that into a JSON string would make the file unreadable for
    # something the user is explicitly meant to be able to inspect and trust.
    for rec in data.get("overrides") or []:
        if not isinstance(rec, dict):
            continue
        match = rec.get("match") or {}
        patch = rec.get("set") or {}
        if not isinstance(match, dict) or not isinstance(patch, dict):
            continue
        key = entry_key(match.get("host", ""), match.get("login", ""))
        kept = {k: str(v) for k, v in patch.items() if k in FIELDS}
        if kept:
            store.overrides[key] = kept
    for rec in data.get("hidden") or []:
        if isinstance(rec, dict):
            store.hidden.add(entry_key(rec.get("host", ""), rec.get("login", "")))
    for rec in data.get("own") or []:
        if isinstance(rec, dict):
            store.own.append(_clean(rec))
    for rec in data.get("devices") or []:
        if isinstance(rec, dict):
            store.devices.append(rec)
    return store


def save(store: Store, path: Path | None = None) -> None:
    p = path or store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": STORE_VERSION,
        "_comment": (
            "Local overlay on Addresses.cdb, which x3 only ever "
            "reads. Entries here win over the cdb."
        ),
        "overrides": [
            {"match": {"host": host, "login": login}, "set": patch}
            for (host, login), patch in sorted(store.overrides.items())
        ],
        "hidden": [
            {"host": host, "login": login} for host, login in sorted(store.hidden)
        ],
        "own": list(store.own),
        "devices": list(store.devices),
    }
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, p)
    try:
        # Same reasoning as settings.json: passwords are stored in the clear,
        # matching what Addresses.cdb already does.
        os.chmod(p, 0o600)
    except OSError:
        pass
