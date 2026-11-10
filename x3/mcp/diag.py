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
         src_address: str | None = None, routing_table: str | None = None,
         interval_ms: int | None = None) -> str:
    parts = ["/ping", f"address={dest}", f"count={count}"]
    if interval_ms is not None:
        parts.append(f"interval={interval_ms}ms")
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


# --- console_print ----------------------------------------------------------

#: Menus console_print may print, keyed by their console path. ``print`` is
#: only harmless on a menu: on a command it becomes an argument
#: (``/file remove print`` removes a file called "print"), so the path is
#: never free text. Menus whose rows hold keys (IPsec installed SAs, PPP
#: secrets, users) are left out, whatever the redaction would catch.
PRINT_MENUS: dict[str, str] = {
    "/routing bgp advertisements": "prefixes sent to each BGP peer",
    "/routing bgp session": "BGP sessions, state and counters",
    "/routing bgp connection": "BGP connection settings",
    "/routing bgp template": "BGP templates",
    "/routing route": "every route of every protocol, active or not",
    "/routing table": "routing tables and VRFs",
    "/routing filter rule": "routing filter rules",
    "/routing ospf neighbor": "OSPF neighbours",
    "/routing ospf lsa": "OSPF link-state database",
    "/routing ospf interface": "OSPF interfaces",
    "/routing rule": "policy routing rules",
    "/ip route": "IPv4 routes",
    "/ipv6 route": "IPv6 routes",
    "/ip address": "IPv4 addresses",
    "/ipv6 address": "IPv6 addresses",
    "/ip arp": "ARP table",
    "/ipv6 neighbor": "IPv6 neighbour table",
    "/ip neighbor": "discovered neighbours (MNDP/CDP/LLDP)",
    "/ip pool used": "addresses handed out from pools",
    "/ip dhcp-server lease": "DHCP leases",
    "/ip dhcp-client": "DHCP clients",
    "/ip firewall connection": "connection tracking table",
    "/ip firewall address-list": "firewall address lists",
    "/ip firewall filter": "firewall filter rules with counters",
    "/ip firewall nat": "NAT rules with counters",
    "/ip firewall mangle": "mangle rules with counters",
    "/ip firewall raw": "raw rules with counters",
    "/ipv6 firewall filter": "IPv6 firewall filter rules",
    "/ip ipsec active-peers": "IPsec peers that are up",
    "/interface": "all interfaces",
    "/interface bridge host": "bridge MAC table",
    "/interface bridge vlan": "bridge VLAN table",
    "/interface vrrp": "VRRP instances",
    "/ppp active": "PPP sessions that are up",
    "/mpls ldp neighbor": "LDP neighbours",
    "/mpls forwarding-table": "MPLS forwarding table",
}

_FIELD = r"[a-z][a-z0-9.-]{0,63}"
#: A value typed bare: an address, a number, a name. Nothing the console
#: reads as an operator, a variable or a command.
_BARE_VALUE = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}")
#: A value typed in double quotes. Inside quotes the router still evaluates
#: ``$``, ``[...]`` and ``\\``, and ``?`` opens help at the terminal, so none
#: of those (nor a quote) may appear.
_QUOTED_VALUE = re.compile(r"[A-Za-z0-9 ._:/@+,()-]{1,128}")
_COND_EQ = re.compile(rf"({_FIELD})\s*(!=|=)\s*(.+)")
_COND_IN = re.compile(rf"(\S+)\s+in\s+(\S+)")
_COND_FLAG = re.compile(rf"(!?)\s*({_FIELD})")


def print_menu(value: object) -> str:
    """``value`` as a key of :data:`PRINT_MENUS`: by full path in any
    spelling (``routing/bgp/advertisements``), else by a unique ending."""
    words = re.split(r"[\s/]+", str(value or "").strip().lower())
    words = [w for w in words if w]
    if not words:
        raise ToolError("menu is required. One of: " + ", ".join(PRINT_MENUS))
    want = "/" + " ".join(words)
    if want in PRINT_MENUS:
        return want
    hits = [m for m in PRINT_MENUS if m.endswith(" " + " ".join(words))]
    if len(hits) == 1:
        return hits[0]
    raise ToolError(
        f"console_print cannot print {want!r}"
        + (f" (it matches {', '.join(hits)})" if hits else "")
        + ". It prints only: " + ", ".join(PRINT_MENUS)
        + ". For anything Winbox shows, use read_menu."
    )


def _network(text: str) -> str:
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        raise ToolError(f"{text!r} is not an address or prefix.") from None


def _value(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if _BARE_VALUE.fullmatch(text):
        return text
    if _QUOTED_VALUE.fullmatch(text):
        return f'"{text}"'
    raise ToolError(
        f"The value {text!r} has characters this tool will not type at a "
        "console ($ [ ] \\ \" ? and control characters are refused)."
    )


def condition(text: object) -> str:
    """One where-condition, rebuilt from its checked parts:

    ``field=value`` / ``field!=value``, ``field in 10.0.0.0/8`` (field's
    address inside the prefix), ``10.1.2.3 in field`` (field's prefix
    covers the address), ``flag`` or ``!flag`` (``active``, ``!disabled``).
    """
    raw = str(text or "").strip()
    m = _COND_IN.fullmatch(raw)
    if m:
        left, right = m.groups()
        if re.fullmatch(_FIELD, left) and not re.fullmatch(_FIELD, right):
            return f"{left} in {_network(right)}"
        if re.fullmatch(_FIELD, right) and not re.fullmatch(_FIELD, left):
            return f"{_network(left)} in {right}"
    m = _COND_EQ.fullmatch(raw)
    if m:
        field, op, value = m.groups()
        return f"{field}{op}{_value(value)}"
    m = _COND_FLAG.fullmatch(raw)
    if m:
        bang, field = m.groups()
        return f"{bang}{field}"
    raise ToolError(
        f"Cannot read the condition {raw!r}. Use field=value, field!=value, "
        "'field in 10.0.0.0/8', '10.1.2.3 in field', 'flag' or '!flag'."
    )


def print_command(menu: str, conditions: list[str], *, detail: bool = False,
                  count_only: bool = False, stats: bool = False) -> str:
    parts = [menu, "print"]
    if count_only:
        parts.append("count-only")
    elif stats:
        # The counters view. The router prints it as a table whatever else
        # is asked, so terse would only be ignored.
        parts.append("stats")
    else:
        parts.append("detail" if detail else "terse")
    parts.append("without-paging")
    if conditions:
        parts += ["where", " and ".join(conditions)]
    return " ".join(parts)


# --- link and health ----------------------------------------------------------

def ethernet_monitor(quoted_iface: str) -> str:
    return f"/interface ethernet monitor {quoted_iface} once"


HEALTH_COMMAND = "/system health print without-paging"


def torch(quoted_iface: str, *, seconds: int, src: str | None = None,
          dst: str | None = None, port: int | None = None) -> str:
    parts = ["/tool torch", f"interface={quoted_iface}", f"duration={seconds}s"]
    if src:
        parts.append(f"src-address={_network(src)}")
    if dst:
        parts.append(f"dst-address={_network(dst)}")
    if port is not None:
        parts.append(f"port={port}")
    return " ".join(parts)


def last_screen(lines: list[str], marker: str = "Columns:") -> list[str]:
    """What a redrawing tool showed last: torch prints a fresh table every
    second, and only the final one is the answer."""
    starts = [i for i, ln in enumerate(lines) if ln.lstrip().startswith(marker)]
    kept = lines[starts[-1]:] if starts else lines
    return [ln for ln in kept if not ln.lstrip().startswith("-- [")]


# --- ping_watch -----------------------------------------------------------------

_PROBE = re.compile(r"\s*(\d+)\s+(\S+)\s*(.*)")
_RTT = re.compile(r"(\d+(?:\.\d+)?)(us|ms)\b")
_LOST = re.compile(r"timeout|unreachable|no route|could not|packet filtered", re.I)


def _rtt_ms(rest: str) -> float | None:
    m = _RTT.search(rest)
    if not m:
        return None
    return float(m.group(1)) / (1000.0 if m.group(2) == "us" else 1.0)


def ping_timeline(lines: list[str], interval_ms: int) -> list[str]:
    """``/ping`` output folded into runs of answered and lost probes, timed
    from the first probe. Thousands of probe lines would bury the one thing
    asked for: when replies stopped and for how long."""
    probes: dict[int, float | None] = {}
    summary = ""
    for ln in lines:
        if "sent=" in ln and "received=" in ln:
            summary = ln.strip()
            continue
        m = _PROBE.fullmatch(ln)
        if not m or m.group(2) == "HOST":
            continue
        seq, rest = int(m.group(1)), m.group(3)
        rtt = None if _LOST.search(rest) else _rtt_ms(rest)
        if rtt is None and not _LOST.search(rest):
            continue  # a wrapped or partial line, not a probe
        probes[seq] = rtt
    if not probes:
        return ["No probe lines came back.", *([summary] if summary else [])]
    step = interval_ms / 1000.0
    runs: list[tuple[bool, int, int, list[float]]] = []
    for seq in sorted(probes):
        rtt = probes[seq]
        ok = rtt is not None
        if runs and runs[-1][0] == ok and runs[-1][2] == seq - 1:
            runs[-1] = (ok, runs[-1][1], seq, runs[-1][3] + ([rtt] if ok else []))
        else:
            runs.append((ok, seq, seq, [rtt] if ok else []))
    lost = sum(1 for r in probes.values() if r is None)
    out = [f"{len(probes)} probes every {interval_ms}ms: {lost} lost "
           f"({100.0 * lost / len(probes):.1f}%), about {lost * step:.1f}s without replies"]
    outages = [r for r in runs if not r[0]]
    if outages:
        worst = max(outages, key=lambda r: r[2] - r[1])
        out.append(f"longest outage: {(worst[2] - worst[1] + 1) * step:.1f}s "
                   f"from t={worst[1] * step:.1f}s")
    out.append("timeline (t from the first probe):")
    shown = runs if len(runs) <= 60 else runs[:30] + runs[-30:]
    for i, (ok, a, b, rtts) in enumerate(shown):
        if len(runs) > 60 and i == 30:
            out.append(f"  ... {len(runs) - 60} runs left out ...")
        span = f"  {a * step:7.1f}s - {(b + 1) * step:7.1f}s"
        if ok:
            out.append(f"{span}  ok    {b - a + 1} replies, rtt avg "
                       f"{sum(rtts) / len(rtts):.1f}ms max {max(rtts):.1f}ms")
        else:
            out.append(f"{span}  LOST  {b - a + 1} probes")
    if summary:
        out.append(summary)
    return out
