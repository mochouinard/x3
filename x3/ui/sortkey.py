"""Type-aware sort keys for list columns.

The router renders everything as text, so a plain string sort is wrong almost
everywhere a router cares: ``ether10`` lands before ``ether2``, ``10.0.0.9``
before ``10.0.0.10``, and ``883.4 MB`` before ``9 KB``. :func:`sort_key`
recognises the shapes the router actually emits and returns a tuple that orders
them the way a network engineer reads them.

Every key starts with a rank, so a column holding a mix (``10.0.0.1`` next to
``dhcp``) still has a total order instead of raising TypeError: values of one
kind group together, and the groups sit in a fixed sequence.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

# Rank buckets. Values only ever compare against the same rank, so the numbers
# just fix the order the groups appear in.
RANK_EMPTY = 0
RANK_NUMBER = 1
RANK_DURATION = 2
RANK_ADDRESS = 3
RANK_MAC = 4
RANK_TEXT = 5

#: Unit prefix -> multiplier. Base 1024 vs 1000 does not matter: only the
#: ordering between prefixes does, and both bases agree on that.
_SCALE = {"k": 1024.0, "m": 1024.0**2, "g": 1024.0**3, "t": 1024.0**4, "p": 1024.0**5}

#: Sub-second units, checked first so ``500ms`` does not read as 500 megabytes.
_SUBSEC = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3}

_NUM_UNIT = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([a-zA-Zµ%/]*)$")

#: ``3d 04:15:00``, ``00:05:00``, ``1w2d03:04:05`` — format_interval() output
#: plus the week/day prefixes the router uses for long uptimes.
_CLOCK = re.compile(
    r"^(?:(\d+)w)?\s*(?:(\d+)d)?\s*(\d{1,3}):([0-5]\d):([0-5]\d(?:\.\d+)?)$"
)

#: ``1w2d3h4m5s`` — at least one unit, no clock part.
_SPANS = re.compile(r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")

_MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

_DIGITS = re.compile(r"(\d+)")

_SPAN_SECS = (604800.0, 86400.0, 3600.0, 60.0, 1.0)


def _as_number(text: str) -> float | None:
    """``1500``, ``883.4 MB``, ``10.5Mbps``, ``87%``, ``250ms`` -> a float."""
    m = _NUM_UNIT.match(text)
    if m is None:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit in _SUBSEC:
        return value * _SUBSEC[unit]
    if unit and unit[0] in _SCALE:
        # 'm' is mega in MB/Mbps but milli in ms — _SUBSEC above already took
        # the milli case, so a leading scale letter here is safe.
        return value * _SCALE[unit[0]]
    return value


def _as_duration(text: str) -> float | None:
    """Clock and span forms -> seconds."""
    m = _CLOCK.match(text)
    if m is not None:
        weeks, days, hours, mins, secs = m.groups()
        return (
            int(weeks or 0) * 604800
            + int(days or 0) * 86400
            + int(hours) * 3600
            + int(mins) * 60
            + float(secs)
        )
    m = _SPANS.match(text)
    if m is not None and any(m.groups()):
        return sum(
            float(g) * mult
            for g, mult in zip(m.groups(), _SPAN_SECS)
            if g is not None
        )
    return None


def _as_address(text: str) -> tuple | None:
    """IP, CIDR network, or ``host:port`` -> a comparable tuple."""
    host, _, port = text.rpartition(":")
    port_num = -1
    # Only treat a trailing :N as a port when the head still looks like IPv4;
    # IPv6 is full of colons and its last group is not a port.
    if host and port.isdigit() and host.count(":") == 0:
        text, port_num = host, int(port)

    if "/" in text:
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            try:
                iface = ipaddress.ip_interface(text)
            except ValueError:
                return None
            return (iface.version, int(iface.ip), iface.network.prefixlen, port_num)
        return (net.version, int(net.network_address), net.prefixlen, port_num)

    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    return (addr.version, int(addr), -1, port_num)


def _natural(text: str) -> tuple:
    """``ether10`` after ``ether2``: digit runs compare as numbers.

    Each chunk is itself a tuple so a numeric chunk never has to compare
    against a text one.
    """
    parts = _DIGITS.split(text.casefold())
    out: list[tuple[int, float, str]] = []
    for i, part in enumerate(parts):
        if i % 2:  # split() puts the captured digit runs on odd indices
            out.append((0, float(part), ""))
        elif part:
            out.append((1, 0.0, part))
    return tuple(out)


def sort_key(value: Any) -> tuple:
    """Ordering key for one rendered cell.

    Keys are always mutually comparable, whatever the column holds.
    """
    if value is None:
        return (RANK_EMPTY, ())
    if isinstance(value, bool):
        # Rendered as true/false; keep them with the text so a Disabled column
        # reads the same whether the model handed us a bool or its rendering.
        return (RANK_TEXT, _natural("true" if value else "false"))
    if isinstance(value, (int, float)):
        return (RANK_NUMBER, (float(value),))

    text = str(value).strip()
    if not text:
        return (RANK_EMPTY, ())

    if _MAC.match(text):
        return (RANK_MAC, (int(re.sub(r"[:-]", "", text), 16),))

    dur = _as_duration(text)
    if dur is not None:
        return (RANK_DURATION, (dur,))

    num = _as_number(text)
    if num is not None:
        return (RANK_NUMBER, (num,))

    addr = _as_address(text)
    if addr is not None:
        return (RANK_ADDRESS, addr)

    return (RANK_TEXT, _natural(text))


def column_key(values: list[Any], column: int):
    """Key function for sorting row lists by one column index."""

    def key(row_index: int):
        row = values[row_index]
        cell = row[column] if column < len(row) else None
        return sort_key(cell)

    return key


def is_quantity(value: Any) -> bool:
    """True when a rendered cell reads as a number the eye wants right-aligned.

    Defined as "sorts as a number", so the rule can never disagree with the
    column's own ordering: ``1500``, ``5.8 Mbps``, ``87%`` and ``-73dBm`` are
    quantities; ``ether1``, ``10.0.0.1``, a MAC and an uptime clock are not,
    even though every one of them contains digits.
    """
    return sort_key(value)[0] == RANK_NUMBER
