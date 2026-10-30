"""MAC-Winbox transport — Winbox-over-MAC-Telnet (UDP 20561).

Classic Winbox can connect by MAC when a device has no usable IP (or you
prefer L2). On the wire that is the MAC-Telnet framing with
**client type 0x0f90 (Winbox)** instead of 0x0015 (MAC-Telnet shell). After
session setup, the TCP-style Winbox frames (modern 0x06 handshake +
encrypted M2) ride as plain DATA payloads.

Header (22 bytes, client → server layout)::

    ver:1 = 1
    ptype:1          # 0=SESSIONSTART, 1=DATA, 2=ACK, 255=END
    src_mac:6
    dst_mac:6
    session_key:2 BE
    client_type:2 BE # 0x0f90 Winbox / 0x0015 MAC-Telnet
    counter:4 BE     # bytes sent so far (DATA/ACK)

Control sub-packets (inside DATA) start with magic ``56 34 12 ff``; plain
Winbox bytes are sent without that magic (PLAINDATA).

References: Wireshark ``packet-mactelnet.c`` (client types), Håkon Nessjøen
MAC-Telnet ``protocol.h`` / ``protocol.c``.
"""

from __future__ import annotations

import os
import random
import select
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAC_WINBOX_PORT = 20561

PTYPE_SESSIONSTART = 0
PTYPE_DATA = 1
PTYPE_ACK = 2
PTYPE_PING = 4
PTYPE_PONG = 5
PTYPE_END = 255

CLIENT_TYPE_MACTELNET = 0x0015
CLIENT_TYPE_WINBOX = 0x0F90

HEADER_LEN = 22
CONTROL_MAGIC = b"\x56\x34\x12\xff"


class MacWinboxError(Exception):
    pass


def parse_mac(text: str) -> bytes:
    """Parse ``aa:bb:cc:dd:ee:ff`` / ``aa-bb-…`` / ``aabbccddeeff`` to 6 bytes."""
    s = text.strip().lower().replace("-", ":")
    if ":" not in s:
        if len(s) != 12 or any(c not in "0123456789abcdef" for c in s):
            raise MacWinboxError(f"invalid MAC address: {text!r}")
        s = ":".join(s[i : i + 2] for i in range(0, 12, 2))
    parts = s.split(":")
    if len(parts) != 6:
        raise MacWinboxError(f"invalid MAC address: {text!r}")
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError as e:
        raise MacWinboxError(f"invalid MAC address: {text!r}") from e


def format_mac(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


def looks_like_mac(text: str) -> bool:
    try:
        parse_mac(text)
        return True
    except MacWinboxError:
        return False


@dataclass
class LocalIface:
    name: str
    mac: bytes
    ipv4: str
    broadcast: str


def list_ifaces() -> list[LocalIface]:
    """Interfaces that have both a MAC and an IPv4 address.

    Linux reads ``/sys/class/net``; elsewhere (Windows, macOS) the list comes
    from :func:`_psutil_ifaces`.
    """
    out: list[LocalIface] = []
    net = Path("/sys/class/net")
    if not net.is_dir():
        return _psutil_ifaces()
    for entry in sorted(net.iterdir()):
        name = entry.name
        if name == "lo":
            continue
        addr_f = entry / "address"
        if not addr_f.is_file():
            continue
        try:
            mac = parse_mac(addr_f.read_text().strip())
        except MacWinboxError:
            continue
        if mac == b"\x00" * 6:
            continue
        # IPv4 via ip addr or kernel
        ipv4, bcast = _iface_ipv4(name)
        if not ipv4:
            continue
        out.append(LocalIface(name=name, mac=mac, ipv4=ipv4, broadcast=bcast or "255.255.255.255"))
    return out


def _psutil_ifaces() -> list[LocalIface]:
    """The same list from psutil, for platforms without ``/sys/class/net``.

    psutil is a dependency only where it is needed (see pyproject.toml); if it
    is missing the list is empty and MAC connections say there is no usable
    interface, as before.
    """
    try:
        import psutil
    except ImportError:
        return []
    out: list[LocalIface] = []
    for name, addrs in sorted(psutil.net_if_addrs().items()):
        mac = b""
        ipv4 = bcast = ""
        for a in addrs:
            if a.family == psutil.AF_LINK and a.address:
                try:
                    mac = parse_mac(a.address.replace("-", ":"))
                except MacWinboxError:
                    pass
            elif a.family == socket.AF_INET and a.address and not ipv4:
                ipv4 = a.address
                bcast = a.broadcast or _broadcast_of(a.address, a.netmask)
        if not mac or mac == b"\x00" * 6 or not ipv4 or ipv4.startswith("127."):
            continue
        out.append(LocalIface(name=name, mac=mac, ipv4=ipv4, broadcast=bcast))
    return out


def _broadcast_of(ip: str, netmask: str | None) -> str:
    """The directed broadcast of ``ip``/``netmask``; all-ones when unknown."""
    if not netmask:
        return "255.255.255.255"
    try:
        import ipaddress

        return str(ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False).broadcast_address)
    except ValueError:
        return "255.255.255.255"


def _iface_ipv4(name: str) -> tuple[str, str]:
    """Return (ipv4, broadcast) for interface, or ('','') if none."""
    try:
        import fcntl

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ifreq = struct.pack("256s", name.encode()[:15])
            res = fcntl.ioctl(s.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
            ip = socket.inet_ntoa(res[20:24])
            try:
                resb = fcntl.ioctl(s.fileno(), 0x8919, ifreq)  # SIOCGIFBRDADDR
                bcast = socket.inet_ntoa(resb[20:24])
            except OSError:
                bcast = "255.255.255.255"
            if ip == "0.0.0.0":
                return "", ""
            return ip, bcast
        finally:
            s.close()
    except (ImportError, OSError):
        return "", ""


def build_header(
    ptype: int,
    src_mac: bytes,
    dst_mac: bytes,
    session_key: int,
    counter: int,
    *,
    client_type: int = CLIENT_TYPE_WINBOX,
    from_server: bool = False,
) -> bytes:
    """Build the 22-byte MAC-Telnet/MAC-Winbox header."""
    if len(src_mac) != 6 or len(dst_mac) != 6:
        raise MacWinboxError("MAC addresses must be 6 bytes")
    buf = bytearray(HEADER_LEN)
    buf[0] = 1
    buf[1] = ptype & 0xFF
    buf[2:8] = src_mac
    buf[8:14] = dst_mac
    if from_server:
        # server→client: client_type at 14, session at 16
        struct.pack_into(">H", buf, 14, client_type & 0xFFFF)
        struct.pack_into(">H", buf, 16, session_key & 0xFFFF)
    else:
        # client→server: session at 14, client_type at 16
        struct.pack_into(">H", buf, 14, session_key & 0xFFFF)
        struct.pack_into(">H", buf, 16, client_type & 0xFFFF)
    struct.pack_into(">I", buf, 18, counter & 0xFFFFFFFF)
    return bytes(buf)


def parse_header(data: bytes) -> dict:
    if len(data) < HEADER_LEN:
        raise MacWinboxError(f"short MAC-Winbox header: {len(data)}")
    ver = data[0]
    ptype = data[1]
    src = data[2:8]
    dst = data[8:14]
    # Detect direction by client type slot
    a = struct.unpack_from(">H", data, 14)[0]
    b = struct.unpack_from(">H", data, 16)[0]
    if a in (CLIENT_TYPE_WINBOX, CLIENT_TYPE_MACTELNET):
        client_type, session_key = a, b
        from_server = True
    elif b in (CLIENT_TYPE_WINBOX, CLIENT_TYPE_MACTELNET):
        session_key, client_type = a, b
        from_server = False
    else:
        # Prefer client→server layout
        session_key, client_type = a, b
        from_server = False
    counter = struct.unpack_from(">I", data, 18)[0]
    return {
        "ver": ver,
        "ptype": ptype,
        "src": src,
        "dst": dst,
        "session_key": session_key,
        "client_type": client_type,
        "counter": counter,
        "from_server": from_server,
        "payload": data[HEADER_LEN:],
    }


class MacWinboxTransport:
    """UDP MAC-Winbox tunnel exposing a TCP-socket-like ``sendall`` / ``recv``.

    After :meth:`connect`, Winbox wire bytes can be written and read as if on
    TCP 8291 — they are framed as MAC-Winbox DATA packets underneath.
    """

    def __init__(
        self,
        dst_mac: str | bytes,
        *,
        timeout: float = 5.0,
        client_type: int = CLIENT_TYPE_WINBOX,
    ) -> None:
        if isinstance(dst_mac, str):
            self.dst_mac = parse_mac(dst_mac)
        else:
            self.dst_mac = dst_mac
        self.timeout = timeout
        self.client_type = client_type
        self.session_key = random.randint(1, 0xFFFE)
        self.src_mac = b"\x00" * 6
        self.iface: LocalIface | None = None
        self._sock: socket.socket | None = None
        self._out_counter = 0
        # Next inbound byte offset we expect. None until the first DATA tells
        # us where the device started counting. See _handle_incoming.
        self._in_counter: int | None = None
        #: Retransmissions dropped, and packets skipped over a gap.
        self.duplicates = 0
        self.gaps = 0
        #: counter -> payload for datagrams that arrived ahead of a hole.
        self._held: dict[int, bytes] = {}
        self._recv_buf = bytearray()
        self._connected = False
        #: Set by cancel() from another thread to abandon a connect in
        #: progress. connect() walks every local interface waiting
        #: `timeout` on each, so without this a cancelled connect keeps a
        #: worker thread alive for up to timeout x interfaces.
        self._cancelled = False

    # --- lifecycle ----------------------------------------------------

    def connect(self) -> None:
        if self._connected:
            return
        ifaces = list_ifaces()
        if not ifaces:
            raise MacWinboxError(
                "no usable interface (need IPv4 + MAC; static IP often required for MAC-Winbox)"
            )

        last_err: Exception | None = None
        for iface in ifaces:
            if self._cancelled:
                raise MacWinboxError("connect cancelled")
            try:
                self._try_iface(iface)
                self._connected = True
                return
            except MacWinboxError as e:
                last_err = e
                self._close_sock()
                continue
        raise MacWinboxError(
            f"no MAC-Winbox response from {format_mac(self.dst_mac)} "
            f"on any interface: {last_err}"
        )

    def _try_iface(self, iface: LocalIface) -> None:
        # The router MAC-Winbox replies often use UDP src 0.0.0.0 and dst
        # 255.255.255.255, so the client socket must be bound to INADDR_ANY
        # (not the unicast interface address) or those packets are dropped.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(("", 0))
        except OSError as e:
            sock.close()
            raise MacWinboxError(f"bind: {e}") from e

        self._sock = sock
        self.src_mac = iface.mac
        self.iface = iface
        self._out_counter = 0
        self._in_counter = None
        self.duplicates = 0
        self.gaps = 0
        self._held.clear()
        self._recv_buf.clear()

        # SESSIONSTART — device answers with ACK (ptype=2) on success
        pkt = build_header(
            PTYPE_SESSIONSTART,
            self.src_mac,
            self.dst_mac,
            self.session_key,
            0,
            client_type=self.client_type,
        )
        self._broadcast(pkt)

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._cancelled:
                raise MacWinboxError("connect cancelled")
            remaining = deadline - time.monotonic()
            # Capped so a cancel is noticed promptly rather than at the
            # deadline: this select is where a connect spends its time.
            r, _, _ = select.select([sock], [], [], min(0.25, max(0.05, remaining)))
            if not r:
                continue
            try:
                data, _addr = sock.recvfrom(2048)
            except OSError:
                continue
            if len(data) < HEADER_LEN:
                continue
            try:
                hdr = parse_header(data)
            except MacWinboxError:
                continue
            if hdr["src"] != self.dst_mac:
                continue
            if hdr["dst"] != self.src_mac:
                continue
            # Session key may appear in the server-layout slot
            if hdr["session_key"] not in (self.session_key, 0):
                continue
            if hdr["ptype"] in (PTYPE_ACK, PTYPE_SESSIONSTART, PTYPE_DATA):
                if hdr["ptype"] == PTYPE_DATA and hdr["payload"]:
                    self._handle_incoming(hdr)
                return

        raise MacWinboxError(f"timeout waiting for {format_mac(self.dst_mac)} on {iface.name}")

    def close(self) -> None:
        if self._sock is not None and self._connected:
            try:
                pkt = build_header(
                    PTYPE_END,
                    self.src_mac,
                    self.dst_mac,
                    self.session_key,
                    self._out_counter,
                    client_type=self.client_type,
                )
                self._broadcast(pkt)
            except OSError:
                pass
        self._close_sock()
        self._connected = False

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "MacWinboxTransport":
        self.connect()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # --- socket-like API for ModernSession ----------------------------

    def sendall(self, data: bytes) -> None:
        if not self._connected or self._sock is None:
            raise MacWinboxError("not connected")
        # Chunk into MTU-friendly DATA packets
        view = memoryview(data)
        mtu_payload = 1400
        offset = 0
        while offset < len(view):
            chunk = bytes(view[offset : offset + mtu_payload])
            hdr = build_header(
                PTYPE_DATA,
                self.src_mac,
                self.dst_mac,
                self.session_key,
                self._out_counter,
                client_type=self.client_type,
            )
            self._broadcast(hdr + chunk)
            self._out_counter += len(chunk)
            offset += len(chunk)
            # Opportunistically drain ACKs / fill recv buffer
            self._pump(timeout=0.0)

    def cancel(self) -> None:
        """Abandon a connect in progress. Safe to call from another thread."""
        self._cancelled = True
        self._close_sock()   # wakes a select blocked on it

    def recv(self, n: int) -> bytes:
        if not self._connected or self._sock is None:
            raise MacWinboxError("not connected")
        deadline = time.monotonic() + self.timeout
        while len(self._recv_buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._recv_buf:
                    # return what we have (TCP-like short read) — but
                    # ModernSession needs exact sizes; raise timeout
                    raise TimeoutError(
                        f"MAC-Winbox recv timeout waiting for {n} bytes "
                        f"(have {len(self._recv_buf)})"
                    )
                return b""
            self._pump(timeout=min(0.5, remaining))
        out = bytes(self._recv_buf[:n])
        del self._recv_buf[:n]
        return out

    def fileno(self) -> int:
        if self._sock is None:
            raise MacWinboxError("not connected")
        return self._sock.fileno()

    def settimeout(self, value: float | None) -> None:
        if value is not None:
            self.timeout = float(value)

    # --- internals ----------------------------------------------------

    def _broadcast(self, packet: bytes) -> None:
        assert self._sock is not None and self.iface is not None
        targets = list(dict.fromkeys([
            self.iface.broadcast,
            "255.255.255.255",
        ]))
        for tgt in targets:
            try:
                self._sock.sendto(packet, (tgt, MAC_WINBOX_PORT))
            except OSError:
                pass

    def _pump(self, timeout: float) -> None:
        assert self._sock is not None
        r, _, _ = select.select([self._sock], [], [], timeout)
        if not r:
            return
        try:
            data, _addr = self._sock.recvfrom(4096)
        except OSError:
            return
        if len(data) < HEADER_LEN:
            return
        try:
            hdr = parse_header(data)
        except MacWinboxError:
            return
        if hdr["src"] != self.dst_mac:
            return
        if hdr["dst"] != self.src_mac and hdr["dst"] != b"\xff" * 6:
            return
        self._handle_incoming(hdr)

    #: Out-of-order datagrams held aside. Bounded so a peer that never fills a
    #: hole cannot grow this without limit.
    _MAX_HELD = 64

    def _drain_held(self) -> None:
        """Append any held datagrams that are now contiguous."""
        while self._in_counter in self._held:
            seg = self._held.pop(self._in_counter)
            self._recv_buf += _extract_plain(seg)
            self._in_counter += len(seg)

    def _handle_incoming(self, hdr: dict) -> None:
        ptype = hdr["ptype"]
        payload = hdr["payload"]
        if ptype == PTYPE_DATA:
            # MAC-Winbox is an ARQ protocol: the device keeps resending a DATA
            # packet until it sees an ACK covering it. `counter` is a byte
            # offset into the stream, which is the only thing that says where
            # a packet belongs -- and it used to be ignored here, so a
            # retransmission was appended a second time. That splices
            # duplicate bytes into the Winbox stream, and the framing layer
            # then runs off the end of the current frame and finds the *next*
            # frame's first-segment marker where a 0xff continuation should
            # be: "bad continuation marker: 0x6".
            #
            # It only bites on big replies (a full interface or address list),
            # because those take many datagrams and only then is a dropped ACK
            # likely. Small GETs fit in one packet and always looked fine.
            counter = hdr["counter"]
            if self._in_counter is None:
                self._in_counter = counter  # adopt the device's origin
            if counter == self._in_counter:
                self._recv_buf += _extract_plain(payload)
                self._in_counter = counter + len(payload)
                self._drain_held()
            elif counter < self._in_counter:
                self.duplicates += 1  # already consumed; re-ACK below
            else:
                # Ahead of what we have: something is missing or arrived out of
                # order. Hold it rather than splice it in at the wrong offset —
                # dropping it outright would stall the stream until the device
                # gave up on us and ended the session.
                self.gaps += 1
                if len(self._held) < self._MAX_HELD:
                    self._held[counter] = payload
                self._drain_held()
            # Cumulative ACK: what we have contiguously, not merely what
            # turned up. This is what makes a lost packet get resent.
            ack = build_header(
                PTYPE_ACK,
                self.src_mac,
                self.dst_mac,
                self.session_key,
                self._in_counter,
                client_type=self.client_type,
            )
            try:
                self._broadcast(ack)
            except OSError:
                pass
        elif ptype == PTYPE_END:
            raise MacWinboxError("remote closed MAC-Winbox session")
        # ACK / SESSIONSTART: nothing to buffer


def _extract_plain(payload: bytes) -> bytes:
    """Pull PLAINDATA out of a DATA payload (skip control sub-packets)."""
    if not payload:
        return b""
    # Fast path: no control magic → entire payload is plain Winbox bytes
    if not payload.startswith(CONTROL_MAGIC) and CONTROL_MAGIC not in payload:
        return payload
    out = bytearray()
    i = 0
    n = len(payload)
    while i < n:
        if i + 9 <= n and payload[i : i + 4] == CONTROL_MAGIC:
            # cptype = payload[i+4]; length at i+5 big-endian u32
            length = struct.unpack_from(">I", payload, i + 5)[0]
            i += 9 + length
            continue
        # plain byte
        # find next magic or end
        nxt = payload.find(CONTROL_MAGIC, i)
        if nxt < 0:
            out += payload[i:]
            break
        out += payload[i:nxt]
        i = nxt
    return bytes(out)


def open_mac_winbox(dst_mac: str, timeout: float = 5.0) -> MacWinboxTransport:
    """Connect a MAC-Winbox tunnel to ``dst_mac`` and return the transport."""
    t = MacWinboxTransport(dst_mac, timeout=timeout)
    t.connect()
    return t
