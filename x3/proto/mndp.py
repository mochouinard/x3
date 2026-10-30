"""The vendor Neighbor Discovery Protocol (MNDP) — LAN device discovery.

This is the protocol behind Winbox's "Neighbors" tab and the router
``/ip neighbor``. It needs no credentials: routers announce
themselves over UDP broadcast to port 5678, and reply to a broadcast request.

Wire format
-----------
A packet is a 4-byte header followed by a series of TLVs::

    header : u16  (big-endian; 0 in practice)
    seqno  : u16  (big-endian)
    repeated:
        type   : u16 (big-endian)
        length : u16 (big-endian)
        value  : <length> bytes

A discovery *request* is simply an empty frame — four zero bytes (header 0,
seqno 0, no TLVs) — broadcast to 255.255.255.255:5678 and to each interface's
directed-broadcast address. Devices answer with a populated frame.

Only IPv4 discovery is implemented; that is what the router broadcasts.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from dataclasses import dataclass, field

MNDP_PORT = 5678
_REQUEST = b"\x00\x00\x00\x00"

# TLV type codes (see Wireshark's MNDP dissector).
TLV_MAC = 1
TLV_IDENTITY = 5
TLV_VERSION = 7
TLV_PLATFORM = 8
TLV_UPTIME = 10
TLV_SOFTWARE_ID = 11
TLV_BOARD = 12
TLV_UNPACK = 14
TLV_IPV6 = 15
TLV_INTERFACE = 16
TLV_IPV4 = 17

_STRING_TLVS = {
    TLV_IDENTITY: "identity",
    TLV_VERSION: "version",
    TLV_PLATFORM: "platform",
    TLV_SOFTWARE_ID: "software_id",
    TLV_BOARD: "board",
    TLV_INTERFACE: "interface",
}

# Linux ioctls for enumerating interface broadcast addresses.
_SIOCGIFBRDADDR = 0x8919


@dataclass
class Neighbor:
    """One discovered device, keyed on its MAC address."""

    source_ip: str = ""
    mac: str = ""
    identity: str = ""
    version: str = ""
    platform: str = ""
    board: str = ""
    software_id: str = ""
    interface: str = ""
    ipv4: str = ""
    ipv6: str = ""
    uptime: int | None = None
    unpack: int | None = None
    unknown: dict[int, bytes] = field(default_factory=dict)

    def key(self) -> str:
        return self.mac or self.source_ip


def _fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    mnt, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    if mnt or h or d:
        parts.append(f"{mnt}m")
    parts.append(f"{s}s")
    return "".join(parts)


def parse_packet(data: bytes, source_ip: str = "") -> Neighbor | None:
    """Parse one MNDP reply. Returns ``None`` for a request/empty frame."""
    if len(data) < 4:
        return None
    n = Neighbor(source_ip=source_ip)
    i = 4  # skip header + seqno
    saw_tlv = False
    total = len(data)
    while i + 4 <= total:
        ttype, tlen = struct.unpack_from(">HH", data, i)
        i += 4
        if i + tlen > total:
            break
        value = data[i : i + tlen]
        i += tlen
        saw_tlv = True
        if ttype == TLV_MAC and tlen == 6:
            n.mac = ":".join(f"{b:02x}" for b in value)
        elif ttype in _STRING_TLVS:
            setattr(n, _STRING_TLVS[ttype], value.decode("utf-8", "replace"))
        elif ttype == TLV_UPTIME and tlen >= 4:
            # The router encodes uptime little-endian.
            n.uptime = struct.unpack_from("<I", value, 0)[0]
        elif ttype == TLV_IPV4 and tlen == 4:
            n.ipv4 = socket.inet_ntoa(value)
        elif ttype == TLV_IPV6 and tlen == 16:
            n.ipv6 = socket.inet_ntop(socket.AF_INET6, value)
        elif ttype == TLV_UNPACK and tlen >= 1:
            n.unpack = value[0]
        else:
            n.unknown[ttype] = value
    if not saw_tlv:
        return None
    return n


def _iface_broadcasts() -> list[str]:
    """Best-effort list of per-interface IPv4 broadcast addresses.

    ioctl on Linux; elsewhere the interface list MAC connections use, which
    comes from psutil. Empty is fine: the all-ones broadcast is always sent.
    """
    out: list[str] = []
    try:
        import fcntl  # Linux-only

        names = [name for _, name in socket.if_nameindex()]
    except (ImportError, OSError):
        from .macwinbox import list_ifaces

        return sorted({i.broadcast for i in list_ifaces()} - {"255.255.255.255"})
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            try:
                res = fcntl.ioctl(
                    s.fileno(),
                    _SIOCGIFBRDADDR,
                    struct.pack("256s", name.encode()[:15]),
                )
                ip = socket.inet_ntoa(res[20:24])
                if ip not in ("0.0.0.0",):
                    out.append(ip)
            except OSError:
                continue
    finally:
        s.close()
    return out


def discover(
    timeout: float = 3.0,
    *,
    requests: int = 2,
    targets: list[str] | None = None,
) -> list[Neighbor]:
    """Broadcast an MNDP request and collect replies for ``timeout`` seconds.

    ``targets`` overrides the broadcast address list; by default we use the
    global broadcast plus every interface's directed broadcast.
    """
    if targets is None:
        targets = ["255.255.255.255", *_iface_broadcasts()]
        targets = list(dict.fromkeys(targets))  # de-dupe, keep order

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", MNDP_PORT))

    found: dict[str, Neighbor] = {}
    deadline = time.monotonic() + timeout
    next_send = 0.0
    sends_left = max(1, requests)

    while True:
        now = time.monotonic()
        if sends_left > 0 and now >= next_send:
            for tgt in targets:
                try:
                    sock.sendto(_REQUEST, (tgt, MNDP_PORT))
                except OSError:
                    pass
            sends_left -= 1
            next_send = now + 1.0
        remaining = deadline - now
        if remaining <= 0:
            break
        sock.settimeout(min(remaining, 0.5))
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if data == _REQUEST:
            continue  # our own (or another scanner's) request echoed back
        nb = parse_packet(data, source_ip=addr[0])
        if nb is None:
            continue
        found[nb.key()] = nb

    sock.close()
    return sorted(found.values(), key=lambda n: (n.identity.lower(), n.source_ip))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_table(neighbors: list[Neighbor]) -> str:
    cols = ["identity", "address", "mac", "board", "version", "uptime", "iface"]
    rows: list[list[str]] = []
    for n in neighbors:
        rows.append([
            n.identity,
            n.source_ip,
            n.mac,
            n.board or n.platform,
            n.version,
            _fmt_uptime(n.uptime),
            n.interface,
        ])
    widths = [len(c) for c in cols]
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))
    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
    lines = [fmt(cols), fmt(["-" * w for w in widths])]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def run(args) -> int:
    targets = args.target or None
    try:
        neighbors = discover(
            timeout=args.timeout, requests=args.requests, targets=targets
        )
    except PermissionError as e:
        print(f"x3ctl neighbors: {e}", file=sys.stderr)
        return 3

    if args.format == "json":
        import json
        from dataclasses import asdict

        payload = []
        for n in neighbors:
            d = asdict(n)
            d["uptime_human"] = _fmt_uptime(n.uptime)
            d.pop("unknown", None)
            payload.append(d)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if neighbors:
            print(_render_table(neighbors))
        print(
            f"\n{len(neighbors)} neighbor(s) discovered", file=sys.stderr
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x3ctl neighbors",
        description="Discover routers on the LAN via MNDP (UDP 5678). "
        "No credentials required.",
    )
    add_arguments(parser)
    return parser


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Seconds to listen for replies (default: 3).",
    )
    p.add_argument(
        "--requests",
        type=int,
        default=2,
        help="Number of broadcast requests to send (default: 2).",
    )
    p.add_argument(
        "--target",
        action="append",
        metavar="ADDR",
        help="Broadcast address to send to (repeatable). Default: global "
        "broadcast + each interface's broadcast.",
    )
    p.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table).",
    )
    p.set_defaults(func=run)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
