"""Field defaults learned from the router's own exports, kept per firmware.

The .jg declares a default for some fields only -- MTU yes, ARP, Speed and
Loop Protect no -- so a "what was set here" summary needs the router's word,
and asking costs a console read. An export names only what differs from the
default; so every field a row carries that its export line leaves out is at
its default, and its value *is* the default. Learned once from one list, that
holds for every router on the same firmware build, and is kept on disk:

    ~/.cache/x3/defaults/<version>-<build>.json
      { "20,0|ethernet": { "1002": 1, "1003": 0, ... }, ... }

A field seen at "default" with two different values does not have one -- an
interface's own MAC, its name -- and is marked so, never hidden.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

#: Stored for a field whose "default" differed between rows.
VARIES = {"varies": True}

_cache: dict[str, dict[str, dict[str, Any]]] = {}


def cli_name(display: str) -> str:
    """The CLI property a field's display name stands for.

    The router's own pattern: "Loop Protect" -> loop-protect, "Dst. Address" ->
    dst-address, "ARP Timeout" -> arp-timeout.
    """
    s = re.sub(r"[^a-z0-9 -]", "", str(display).lower().replace(".", ""))
    return re.sub(r"\s+", "-", s.strip())


def _dir() -> str:
    from x3 import paths

    return str(paths.cache_dir() / "defaults")


def _file(firmware: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", firmware or "unknown")
    return os.path.join(_dir(), f"{safe}.json")


def _load(firmware: str) -> dict[str, dict[str, Any]]:
    if firmware not in _cache:
        try:
            with open(_file(firmware), encoding="utf-8") as fh:
                data = json.load(fh)
            _cache[firmware] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache[firmware] = {}
    return _cache[firmware]


def _save(firmware: str) -> None:
    os.makedirs(_dir(), exist_ok=True)
    tmp = _file(firmware) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_cache.get(firmware, {}), fh, indent=1, sort_keys=True)
    os.replace(tmp, _file(firmware))


def _key(menu_key: Any) -> str:
    path, kind = menu_key
    return ",".join(str(p) for p in path) + "|" + str(kind or "")


def _storable(value: Any) -> Any:
    """A wire value as JSON, or None when it is not a plain scalar."""
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return "b:" + bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def known(firmware: str, menu_key: Any) -> dict[int, Any] | None:
    """The learned defaults for a list (and kind), or None if never learned."""
    entry = _load(firmware).get(_key(menu_key))
    if entry is None:
        return None
    return {int(code): v for code, v in entry.items()}


def is_default(defaults: dict[int, Any] | None, code: int, value: Any) -> bool:
    """Whether ``value`` is what this field was learned to default to."""
    if not defaults or code not in defaults:
        return False
    learned = defaults[code]
    return learned != VARIES and learned == _storable(value)


def learn(
    firmware: str,
    menu_key: Any,
    rows: list[tuple[list[tuple[int, str]], dict[int, Any], str]],
) -> int:
    """Learn from rows of one list: ``(fields, values_by_code, export_line)``.

    ``fields`` is ``(code, display name)`` for the settable fields the row
    carries. Every one whose CLI name is not in its export line is at its
    default. Returns how many fields now have a default on record.
    """
    from .widgets.cli_line import line_settings

    data = _load(firmware)
    entry = data.setdefault(_key(menu_key), {})
    for fields, values, line in rows:
        exported = {k for k, _v in line_settings(line)}
        for code, name in fields:
            if cli_name(name) in exported:
                continue
            value = _storable(values.get(code))
            if value is None:
                continue
            seen = entry.get(str(code))
            if seen is None:
                entry[str(code)] = value
            elif seen != value and seen != VARIES:
                entry[str(code)] = VARIES
    _save(firmware)
    return sum(1 for v in entry.values() if v != VARIES)
