"""Telling a configuration change from a reading.

A live list is fed two very different things down one subscription. Most of
what arrives is measurement -- an Interface feed pushes rx/tx counters every
second, a queue pushes rates, a wireless registration table pushes signal --
and none of it means anybody configured anything. Occasionally the same channel
carries the other kind: somebody in another Winbox session, or on the console,
changed a setting.

Only the second kind is worth waking a backup for, or telling the user about.
Getting that wrong in either direction is bad in a specific way: count the
counters and a busy router exports every 15 minutes forever, having recorded
nothing; miss the real edits and the feature does not work at all.

The discriminator is the schema, not a list of field names:

* ``Field.ro`` -- the ``.jg`` marks every reading read-only. Rates, counters,
  signal, last-seen, current-state: all ``ro:1``, none of them settings.
* ``menu.age_codes()`` -- ``type:'age'`` stamps tick on their own.
* ``menu.statusbar_codes()`` -- window-tray readings, never config.
* ``nonpublic`` fields are internal plumbing the user never sees, so a change
  in one must not raise a user-facing warning.
* ``type:'flag'`` fields are the letter column -- Interface's running (R),
  slave (S), passthrough (P), inactive (I). Every letter there is state, and
  ``running`` flips whenever a link bounces, so a flapping uplink would
  otherwise look like somebody editing the router all afternoon.
* the two standard codes that *are* settings but are not declared per-menu:
  **Disabled** (0xFE000A) and **comment** (0xFE0009). Disabling a firewall rule
  from another session is exactly the event this module exists to catch, and it
  arrives on a code no menu lists.

Rows carrying the **dynamic** flag (0xFE0007) are skipped whole. A DHCP lease,
a learned ARP entry, a route from a routing protocol and a connected wireless
station all appear and vanish on their own; treating those as configuration
would make an ARP table into a change siren.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from x3.proto import schema as sch

#: Settings that ride on every menu without being declared on any. Both are
#: real configuration: "someone disabled this rule", "someone edited a comment".
UNIVERSAL_CONFIG_CODES = frozenset({sch.FLAG_DISABLED_CODE, 0xFE0009})

#: Never configuration, wherever it appears: object id, the dynamic and invalid
#: flags, and the device's own status lines.
NEVER_CONFIG_CODES = frozenset(
    {0xFE0001, sch.FLAG_DYNAMIC_CODE, sch.FLAG_INVALID_CODE, 0xFE001C}
)

#: How many changed row labels a summary spells out before it counts the rest.
_NAMED = 3


def config_codes(menu: Any) -> frozenset[int]:
    """The wire codes on ``menu`` that carry configuration.

    Everything the schema calls read-only, aged, status-bar or non-public is
    left out; the two universal settings codes are added in.
    """
    if menu is None:
        return UNIVERSAL_CONFIG_CODES
    out: set[int] = set()
    fields = getattr(menu, "fields", None) or {}
    for code, fld in fields.items():
        if not isinstance(code, int):
            continue
        variants = list((getattr(fld, "variants", None) or {}).values()) or [fld]
        # A code declared several times counts as config only if some
        # declaration of it is writable -- DHCP Lease's 0x7b is Client ID in
        # one variant and Age in the other.
        if all(getattr(v, "ro", False) for v in variants):
            continue
        if all(getattr(v, "nonpublic", False) for v in variants):
            continue
        if all(getattr(v, "jg_type", "") == "flag" for v in variants):
            # ``type:'flag'`` is the letter column, and every letter in it is
            # device state rather than configuration: Interface declares
            # running (R), slave (S), passthrough (P) and inactive (I), and
            # `running` alone flips every time a link bounces. Counting those
            # would make a flapping uplink look like somebody editing the
            # router all afternoon.
            #
            # Disabled is a flag too, and it *is* configuration -- but it rides
            # on the universal 0xFE000A code, which is unioned back in below
            # and so is unaffected by this.
            continue
        out.add(code)
    for dropper in ("age_codes", "statusbar_codes"):
        fn = getattr(menu, dropper, None)
        if callable(fn):
            try:
                out -= set(fn())
            except Exception:  # noqa: BLE001
                pass
    out |= set(UNIVERSAL_CONFIG_CODES)
    return frozenset(out - NEVER_CONFIG_CODES)


def _is_dynamic_row(row: Any) -> bool:
    """Whether the device generated this row rather than someone configuring it."""
    try:
        return bool(row.raw_value(sch.FLAG_DYNAMIC_CODE))
    except Exception:  # noqa: BLE001
        return False


def _cell(row: Any, code: int) -> str:
    """One field as a stable string, or "" when the row does not carry it.

    Rendered through ``repr`` rather than the display formatter: this is only
    ever compared with itself, and a formatter would fold two different values
    onto one string (and cost a schema lookup per field per row).
    """
    try:
        value = row.raw_value(code)
    except Exception:  # noqa: BLE001
        return ""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return repr(bytes(value))
    return repr(value)


def row_label(menu: Any, row: Any) -> str:
    """A short human name for a row, for the warning list.

    Prefers whatever the menu calls its name column, falls back to the comment,
    then to the object id -- a warning that says "1 change" is much less use
    than one that says which rule moved.
    """
    for getter in ("get_rendered",):
        fn = getattr(row, getter, None)
        if not callable(fn):
            continue
        names = getattr(menu, "nameval", None)
        codes = []
        fields = getattr(menu, "fields", None) or {}
        for code, fld in fields.items():
            fname = (getattr(fld, "name", "") or "").lower()
            if fname in ("name", "interface", "address", "chain") or (
                names and fname == str(names).lower()
            ):
                codes.append((0 if fname == "name" else 1, code))
        for _rank, code in sorted(codes):
            try:
                text = fn(code)
            except Exception:  # noqa: BLE001
                continue
            if text:
                return str(text)[:60]
    try:
        comment = row.comment()
        if comment:
            return f"({comment[:50]})"
    except Exception:  # noqa: BLE001
        pass
    try:
        oid = row.object_id()
        if oid is not None:
            return f".id {oid}"
    except Exception:  # noqa: BLE001
        pass
    return "?"


def snapshot(menu: Any, rows: Iterable[Any]) -> dict[Any, tuple[str, str]]:
    """Configuration-only fingerprint per row: ``{key: (label, digest)}``.

    Keyed on the device's object id where there is one -- the only identity that
    survives a re-read, since two rows can render identically. Dynamic rows are
    left out entirely.
    """
    codes = sorted(config_codes(menu))
    out: dict[Any, tuple[str, str]] = {}
    for index, row in enumerate(rows):
        if row is None or _is_dynamic_row(row):
            continue
        try:
            key = row.object_id()
        except Exception:  # noqa: BLE001
            key = None
        if key is None:
            # No id: a settings singleton, or a handler that does not issue
            # them. Position is all there is, and it is stable for a re-read of
            # the same list.
            key = f"#{index}"
        blob = "\x1f".join(f"{code:x}={_cell(row, code)}" for code in codes)
        out[key] = (
            row_label(menu, row),
            hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16],
        )
    return out


@dataclass
class ContentDelta:
    """What moved between two snapshots of one list."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def __bool__(self) -> bool:
        return self.total > 0

    def summary(self) -> str:
        """One line: what happened, and to which rows where it fits."""
        parts: list[str] = []
        for verb, items in (
            ("added", self.added),
            ("removed", self.removed),
            ("changed", self.changed),
        ):
            if not items:
                continue
            named = ", ".join(items[:_NAMED])
            if len(items) > _NAMED:
                named += f" +{len(items) - _NAMED} more"
            parts.append(f"{len(items)} {verb} ({named})")
        return "; ".join(parts) if parts else "no change"


def diff(
    before: dict[Any, tuple[str, str]] | None,
    after: dict[Any, tuple[str, str]],
) -> ContentDelta:
    """Compare two :func:`snapshot` results.

    ``before`` of None means there is nothing to compare against yet -- the
    first load of a list is not a change -- and yields an empty delta.
    """
    delta = ContentDelta()
    if before is None:
        return delta
    for key, (label, digest) in after.items():
        if key not in before:
            delta.added.append(label)
        elif before[key][1] != digest:
            delta.changed.append(label)
    for key, (label, _digest) in before.items():
        if key not in after:
            delta.removed.append(label)
    return delta
