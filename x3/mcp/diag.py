"""Console commands the diagnostics tools may type, built from checked parts.

The router console runs anything it is given -- the session's ``readonly``
flag guards the CRUD verbs, not the terminal -- so nothing a model sends is
ever typed as it came. Each tool has one fixed command shape, and every value
that goes into it is parsed into something that cannot carry a second
command: an address through :mod:`ipaddress` (or a strict DNS name), a number
through ``int`` and a range, an interface through the device's own list.

Interface names are the one free-form string, and the router evaluates ``$``,
``[...]`` and ``\\`` even inside double quotes. So a name has to exist on the
device *and* stay inside :data:`_IFACE_SAFE` before it is quoted.
"""

from __future__ import annotations

import ipaddress
import re

from .protocol import ToolError

_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?"
)
_IFACE_SAFE = re.compile(r"[A-Za-z0-9 ._@:+/()-]{1,64}")
_TABLE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def address(value: object, what: str = "address") -> str:
    """An IPv4/IPv6 address or a DNS name, in the form the console takes."""
    text = str(value or "").strip()
    if not text:
        raise ToolError(f"{what} is required.")
    try:
        # No scope ids: "fe80::1%ether1" names an interface inside the value.
        if "%" not in text:
            return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if _HOSTNAME_RE.fullmatch(text) and not text.replace(".", "").isdigit():
        return text.lower()
    raise ToolError(f"{what} must be an IP address or a host name, not {text!r}.")


def number(value: object, what: str, lo: int, hi: int, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{what} must be a whole number.") from None
    if not lo <= n <= hi:
        raise ToolError(f"{what} must be between {lo} and {hi}.")
    return n


def interface(value: object, known: list[str]) -> str:
    """``value`` as the device spells it, quoted, if it is one of ``known``."""
    want = str(value or "").strip()
    by_lower = {k.lower(): k for k in known}
    name = by_lower.get(want.lower())
    if name is None:
        raise ToolError(
            f"No interface {want!r} on this device. It has: " + ", ".join(sorted(known))
        )
    if not _IFACE_SAFE.fullmatch(name):
        raise ToolError(
            f"The interface name {name!r} has characters this tool will not "
            "type at a console."
        )
    return f'"{name}"'


def routing_table(value: object) -> str:
    text = str(value or "").strip()
    if not _TABLE_RE.fullmatch(text):
        raise ToolError("routing_table must be a table or VRF name like 'main'.")
    return text


def ping(dest: str, *, count: int, size: int | None = None, interface: str | None = None,
         src_address: str | None = None, routing_table: str | None = None) -> str:
    parts = ["/ping", f"address={dest}", f"count={count}"]
    if size is not None:
        parts.append(f"size={size}")
    if interface:
        parts.append(f"interface={interface}")
    if src_address:
        parts.append(f"src-address={src_address}")
    if routing_table:
        parts.append(f"vrf={routing_table}")
    return " ".join(parts)


def traceroute(dest: str, *, count: int, max_hops: int, src_address: str | None = None,
               routing_table: str | None = None) -> str:
    parts = ["/tool traceroute", f"address={dest}", f"count={count}", f"max-hops={max_hops}"]
    if src_address:
        parts.append(f"src-address={src_address}")
    if routing_table:
        parts.append(f"vrf={routing_table}")
    return " ".join(parts)


def monitor_traffic(quoted_iface: str) -> str:
    return f"/interface monitor-traffic {quoted_iface} once"


#: The log is filtered here, not with a console ``where``: a where-clause is
#: an expression, and an expression is exactly what must not be built from a
#: model's text.
LOG_COMMAND = "/log print without-paging"
