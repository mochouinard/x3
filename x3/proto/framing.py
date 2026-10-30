"""TCP wire framing for the Winbox protocol on port 8291.

Each M2 message is shipped as one or more on-wire segments:

    Single segment (M2 length < 0xFE):
        [seg_len:1][0x01][len_be:2][m2_payload]
        seg_len = m2_len + 2  (covers the 2-byte length field and payload)

    Multi-segment (M2 length >= 0xFE):
        [0xFF][0x01][len_be:2][m2_payload[0:0xFD]]
        [chunk_len:1][0xFF][m2_payload[next chunk]]
        ... repeat until exhausted ...

`len_be` is the total M2 length, used by the receiver to know how many payload
bytes to reassemble across continuation segments.

See the Credits section of README.md for the source this was reconstructed from.
"""

from __future__ import annotations

import socket
import struct

_FIRST_MARKER = 0x01
_CONT_MARKER = 0xFF
_FIRST_SEG_PAYLOAD = 0xFD   # M2 bytes carried by the first segment when split
_CONT_SEG_PAYLOAD = 0xFF    # M2 bytes per continuation segment


class FramingError(Exception):
    pass


def encode_frame(message: bytes) -> bytes:
    n = len(message)
    if n >= 0x10000:
        raise FramingError(f"message exceeds 16-bit length: {n}")

    out = bytearray()
    if n < 0xFE:
        out.append(n + 2)
        out.append(_FIRST_MARKER)
        out += struct.pack(">H", n)
        out += message
        return bytes(out)

    out.append(0xFF)
    out.append(_FIRST_MARKER)
    out += struct.pack(">H", n)
    out += message[:_FIRST_SEG_PAYLOAD]

    i = _FIRST_SEG_PAYLOAD
    while i < n:
        chunk = message[i : i + _CONT_SEG_PAYLOAD]
        out.append(len(chunk))
        out.append(_CONT_MARKER)
        out += chunk
        i += _CONT_SEG_PAYLOAD
    return bytes(out)


def read_frame(sock: socket.socket) -> bytes:
    header = _recv_exact(sock, 4)
    seg_len = header[0]
    if header[1] != _FIRST_MARKER:
        raise FramingError(f"bad first-segment marker: {header[1]:#x}")
    total = struct.unpack(">H", header[2:4])[0]

    if total < 0xFE:
        if seg_len != total + 2:
            raise FramingError(
                f"single-segment length mismatch: header {seg_len} vs total {total}"
            )
        return bytes(_recv_exact(sock, total))

    if seg_len != 0xFF:
        raise FramingError(
            f"multi-segment frame missing 0xFF first byte: {seg_len:#x}"
        )

    payload = bytearray(_recv_exact(sock, _FIRST_SEG_PAYLOAD))
    remaining = total - _FIRST_SEG_PAYLOAD
    while remaining > 0:
        sep = _recv_exact(sock, 2)
        chunk_len = sep[0]
        if sep[1] != _CONT_MARKER:
            raise FramingError(f"bad continuation marker: {sep[1]:#x}")
        if chunk_len == 0 or chunk_len > remaining:
            raise FramingError(
                f"continuation chunk len {chunk_len} invalid (remaining {remaining})"
            )
        payload += _recv_exact(sock, chunk_len)
        remaining -= chunk_len
    return bytes(payload)


def _recv_exact(sock: socket.socket, n: int) -> bytearray:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise FramingError("peer closed connection during frame read")
        buf += chunk
    return buf
