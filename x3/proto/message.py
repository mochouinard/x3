"""M2 message-block codec for the Winbox protocol.

Wire layout of a serialized message:

    "M2"                                       (2-byte magic)
    repeated entries:
        marker:u32 (little-endian)
            bits  0..23 -- field code
            bits 24..31 -- type tag
        value bytes (length depends on type)

Type tags observed on the wire:
    0x00  bool false              (no payload; value carried by tag)
    0x01  bool true               (no payload)
    0x08  u32                     (4 bytes LE)
    0x09  u8                      (1 byte)
    0x10  u64                     (8 bytes LE)
    0x18  ipv6 / 128-bit          (16 bytes)
    0x20  string  (2-byte length-prefix)
    0x21  string  (1-byte length-prefix; "short" variant of 0x20)
    0x28  message (2-byte length-prefix; nested M2)
    0x29  message (1-byte length-prefix)
    0x30  raw bytes (2-byte length-prefix)
    0x31  raw bytes (1-byte length-prefix)
    0x88  array of u32  (2-byte count, then count*4 bytes)
    0xA0  array of strings (2-byte count, then per-element 2-byte size + bytes)
    0xA8  array of nested messages
    0xB0  array of raw blobs

We always *emit* the short-length variant when the payload fits in a byte, since
that's what the router itself does. We accept both on receive.

See the Credits section of README.md for sources.
"""

from __future__ import annotations

import struct
from typing import Any, Iterator

# Type tag constants
T_BOOL_FALSE = 0x00
T_BOOL_TRUE = 0x01
T_U32 = 0x08
T_U64 = 0x10
T_IP6 = 0x18
T_STRING = 0x20
T_MESSAGE = 0x28
T_RAW = 0x30

T_BOOL_ARRAY = 0x80
T_U32_ARRAY = 0x88
T_U64_ARRAY = 0x90
T_IP6_ARRAY = 0x98
T_STRING_ARRAY = 0xA0
T_MESSAGE_ARRAY = 0xA8
T_RAW_ARRAY = 0xB0

# When the 0x01 bit is set on a variable-size scalar tag, the length-prefix is
# 1 byte instead of 2. We strip this on decode and re-add on encode.
_SHORT_FLAG = 0x01

# System (cross-cutting) field codes.
SYS_TO = 0xFF0001
SYS_FROM = 0xFF0002
SYS_REPLY_EXPECTED = 0xFF0005
SYS_REQUEST_ID = 0xFF0006
SYS_COMMAND = 0xFF0007
SYS_ERROR_CODE = 0xFF0008
SYS_ERROR_STRING = 0xFF0009
SYS_SESSION_ID = 0xFE0001

_M2_MAGIC = b"M2"


class CodecError(Exception):
    pass


class Message:
    """An ordered list of (field_code, type, value) entries."""

    def __init__(self) -> None:
        self._entries: list[tuple[int, int, Any]] = []

    # --- builders -------------------------------------------------------

    def add_bool(self, code: int, value: bool) -> "Message":
        self._entries.append((code, T_BOOL_TRUE if value else T_BOOL_FALSE, bool(value)))
        return self

    def add_u32(self, code: int, value: int) -> "Message":
        self._entries.append((code, T_U32, value & 0xFFFFFFFF))
        return self

    def add_u64(self, code: int, value: int) -> "Message":
        self._entries.append((code, T_U64, value & 0xFFFFFFFFFFFFFFFF))
        return self

    def add_string(self, code: int, value: str | bytes) -> "Message":
        b = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        self._entries.append((code, T_STRING, b))
        return self

    def add_raw(self, code: int, value: bytes) -> "Message":
        self._entries.append((code, T_RAW, bytes(value)))
        return self

    def add_u32_array(self, code: int, values) -> "Message":
        self._entries.append((code, T_U32_ARRAY, [v & 0xFFFFFFFF for v in values]))
        return self

    def add_string_array(self, code: int, values) -> "Message":
        items = [v.encode("utf-8") if isinstance(v, str) else bytes(v) for v in values]
        self._entries.append((code, T_STRING_ARRAY, items))
        return self

    def add_message(self, code: int, value: "Message") -> "Message":
        self._entries.append((code, T_MESSAGE, value))
        return self

    def add_typed(self, code: int, tag: int, value) -> "Message":
        """Append a value whose wire tag was decided elsewhere.

        Lets a caller encode from the schema (see ``encode_form_value``) without
        having to pick the matching ``add_*`` by hand.
        """
        if tag in (T_BOOL_TRUE, T_BOOL_FALSE):
            return self.add_bool(code, bool(value))
        if tag == T_STRING:
            return self.add_string(code, value)
        if tag == T_MESSAGE:
            return self.add_message(code, value)
        if tag == T_RAW:
            return self.add_raw(code, value)
        if tag == T_IP6:
            self._entries.append((code, T_IP6, bytes(value)))
            return self
        if tag == T_U64:
            return self.add_u64(code, int(value))
        if tag == T_U32:
            return self.add_u32(code, int(value))
        self._entries.append((code, tag, value))
        return self

    # --- convenience for system fields ---------------------------------

    def set_to(self, *handlers: int) -> "Message":
        return self.add_u32_array(SYS_TO, list(handlers))

    def set_command(self, value: int) -> "Message":
        return self.add_u32(SYS_COMMAND, value)

    def set_request_id(self, value: int) -> "Message":
        return self.add_u32(SYS_REQUEST_ID, value)

    def set_session_id(self, value: int) -> "Message":
        return self.add_u32(SYS_SESSION_ID, value)

    def set_reply_expected(self, value: bool = True) -> "Message":
        return self.add_bool(SYS_REPLY_EXPECTED, value)

    # --- accessors ------------------------------------------------------

    def find(self, code: int) -> tuple[int, Any] | None:
        for c, t, v in self._entries:
            if c == code:
                return (t, v)
        return None

    def get_u32(self, code: int) -> int | None:
        hit = self.find(code)
        return int(hit[1]) if hit else None

    def get_string(self, code: int) -> str | None:
        hit = self.find(code)
        if not hit:
            return None
        v = hit[1]
        return v.decode("utf-8", errors="replace") if isinstance(v, (bytes, bytearray)) else str(v)

    def get_raw(self, code: int) -> bytes | None:
        hit = self.find(code)
        if hit is None:
            return None
        v = hit[1]
        # The field may not be the raw/string type the caller assumed --
        # The router reuses low field codes across handlers. bytes(int) would
        # fabricate that many zero bytes and read downstream as a real
        # payload, so treat a type mismatch as absent.
        if not isinstance(v, (bytes, bytearray)):
            return None
        return bytes(v)

    def get_bool(self, code: int) -> bool | None:
        hit = self.find(code)
        return bool(hit[1]) if hit else None

    def has_error(self) -> bool:
        return self.find(SYS_ERROR_CODE) is not None or self.find(SYS_ERROR_STRING) is not None

    def error_code(self) -> int | None:
        return self.get_u32(SYS_ERROR_CODE)

    def error_string(self) -> str:
        s = self.get_string(SYS_ERROR_STRING)
        c = self.get_u32(SYS_ERROR_CODE)
        # Cisco-Talos Winbox dissector vocabulary (see Credits in README.md)
        names = {
            0xFE0001: "EXISTS",
            0xFE0002: "UNKNOWN",
            0xFE0003: "NOTIMP",
            0xFE0004: "UNKNOWNID",
            0xFE0005: "MISSING",
            0xFE0006: "FAILED",
            0xFE0007: "BUSY",
            0xFE0008: "TIMEOUT",
            0xFE0009: "NOTALLOWED",
            0xFE000A: "TOOBIG",
        }
        name = names.get(c or -1)
        if s and name:
            return f"{s} ({name} {c:#x})"
        if s:
            return s
        if c is not None and name:
            return f"{name} ({c:#x} / {c})"
        if c is not None:
            return f"error code {c:#x} ({c})"
        return "unknown error"

    def session_id(self) -> int | None:
        return self.get_u32(SYS_SESSION_ID)

    def __iter__(self) -> Iterator[tuple[int, int, Any]]:
        return iter(self._entries)

    def __repr__(self) -> str:
        parts = []
        for code, t, v in self._entries:
            parts.append(f"{code:#08x}/{t:#04x}={v!r}")
        return "Message(" + ", ".join(parts) + ")"

    # --- serialization --------------------------------------------------

    def to_bytes(self) -> bytes:
        out = bytearray(_M2_MAGIC)
        for code, t, v in self._entries:
            out += _encode_entry(code, t, v)
        return bytes(out)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Message":
        # A zero-length nested message is a value the router really sends -- the
        # File handler (72,*) emits field 0x33 as an empty message -- and it
        # carries no magic. Decode it as an empty record rather than failing
        # the whole enclosing reply.
        if not data:
            return cls()
        if data[:2] != _M2_MAGIC:
            raise CodecError(f"missing M2 magic: got {data[:2]!r}")
        msg = cls()
        i = 2
        n = len(data)
        while i + 4 <= n:
            marker = struct.unpack_from("<I", data, i)[0]
            i += 4
            code = marker & 0xFFFFFF
            tag = (marker >> 24) & 0xFF
            value, used = _decode_value(tag, data, i)
            i += used
            msg._entries.append((code, _normalize_tag(tag), value))
        if i != n:
            raise CodecError(f"trailing {n - i} bytes after last entry")
        return msg


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _marker(tag: int, code: int) -> bytes:
    return struct.pack("<I", ((tag & 0xFF) << 24) | (code & 0xFFFFFF))


def _encode_entry(code: int, tag: int, value: Any) -> bytes:
    if tag in (T_BOOL_FALSE, T_BOOL_TRUE):
        on_wire = T_BOOL_TRUE if bool(value) else T_BOOL_FALSE
        return _marker(on_wire, code)

    if tag == T_U32:
        v = int(value) & 0xFFFFFFFF
        # Real the router encodes u32 values that fit in a byte using the
        # "short" tag 0x09 (= T_U32 | _SHORT_FLAG) followed by 1 byte.
        # Matching this is required for byte-identical round-trip and for
        # any code path where we re-encode and send (e.g. proxy bump mode).
        if v <= 0xFF:
            return _marker(T_U32 | _SHORT_FLAG, code) + bytes([v])
        return _marker(T_U32, code) + struct.pack("<I", v)

    if tag == T_U64:
        return _marker(T_U64, code) + struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)

    if tag in (T_STRING, T_RAW):
        v = value if isinstance(value, (bytes, bytearray)) else bytes(value)
        return _encode_var_scalar(tag, code, bytes(v))

    if tag == T_MESSAGE:
        nested = value.to_bytes() if isinstance(value, Message) else bytes(value)
        return _encode_var_scalar(T_MESSAGE, code, nested)

    if tag == T_IP6:
        v = value if isinstance(value, (bytes, bytearray)) else bytes(value)
        if len(v) != 16:
            raise CodecError(f"IP6 value must be 16 bytes, got {len(v)}")
        return _marker(T_IP6, code) + bytes(v)

    if tag == T_IP6_ARRAY:
        items = [bytes(x) for x in value]
        for x in items:
            if len(x) != 16:
                raise CodecError(f"IP6_ARRAY element must be 16 bytes, got {len(x)}")
        out = _marker(T_IP6_ARRAY, code) + struct.pack("<H", len(items))
        for x in items:
            out += x
        return out

    if tag == T_U32_ARRAY:
        out = _marker(T_U32_ARRAY, code) + struct.pack("<H", len(value))
        for n in value:
            out += struct.pack("<I", int(n) & 0xFFFFFFFF)
        return out

    if tag == T_STRING_ARRAY:
        out = _marker(T_STRING_ARRAY, code) + struct.pack("<H", len(value))
        for s in value:
            b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
            out += struct.pack("<H", len(b)) + b
        return out

    if tag == T_MESSAGE_ARRAY:
        out = _marker(T_MESSAGE_ARRAY, code) + struct.pack("<H", len(value))
        for item in value:
            blob = item.to_bytes() if isinstance(item, Message) else bytes(item)
            if len(blob) > 0xFFFF:
                raise CodecError(
                    f"message-array element too large for 16-bit length: {len(blob)}"
                )
            out += struct.pack("<H", len(blob)) + blob
        return out

    if tag == T_RAW_ARRAY:
        out = _marker(T_RAW_ARRAY, code) + struct.pack("<H", len(value))
        for item in value:
            b = bytes(item)
            if len(b) > 0xFFFF:
                raise CodecError(
                    f"raw-array element too large for 16-bit length: {len(b)}"
                )
            out += struct.pack("<H", len(b)) + b
        return out

    raise CodecError(f"encoding for tag {tag:#x} not implemented")


def _encode_var_scalar(base_tag: int, code: int, payload: bytes) -> bytes:
    if len(payload) <= 0xFF:
        return _marker(base_tag | _SHORT_FLAG, code) + bytes([len(payload)]) + payload
    if len(payload) <= 0xFFFF:
        return _marker(base_tag, code) + struct.pack("<H", len(payload)) + payload
    raise CodecError(f"value too large for 16-bit length: {len(payload)}")


def _normalize_tag(tag: int) -> int:
    if tag in (T_BOOL_FALSE, T_BOOL_TRUE):
        return tag
    if tag & 0x80:
        return tag
    if tag & _SHORT_FLAG:
        base = tag & ~_SHORT_FLAG
        if base in (T_U32, T_U64, T_STRING, T_MESSAGE, T_RAW):
            return base
    return tag


def _decode_value(tag: int, data: bytes, i: int) -> tuple[Any, int]:
    if tag in (T_BOOL_FALSE, T_BOOL_TRUE):
        return tag == T_BOOL_TRUE, 0

    if tag & 0x80:
        return _decode_array(tag, data, i)

    is_short = bool(tag & _SHORT_FLAG)
    base = tag & ~_SHORT_FLAG

    if base == T_U32:
        # tag 0x09 is the 1-byte ("short") variant of u32; bare 0x08 is 4 bytes.
        if is_short:
            return data[i], 1
        return struct.unpack_from("<I", data, i)[0], 4
    if base == T_U64:
        # tag 0x11 (if it occurs) is the 4-byte "short" variant of u64.
        if is_short:
            return struct.unpack_from("<I", data, i)[0], 4
        return struct.unpack_from("<Q", data, i)[0], 8
    if base == T_IP6:
        return bytes(data[i : i + 16]), 16

    if base in (T_STRING, T_RAW, T_MESSAGE):
        if is_short:
            n = data[i]
            payload = data[i + 1 : i + 1 + n]
            used = 1 + n
        else:
            n = struct.unpack_from("<H", data, i)[0]
            payload = data[i + 2 : i + 2 + n]
            used = 2 + n
        if base == T_MESSAGE:
            return Message.from_bytes(bytes(payload)), used
        return bytes(payload), used

    raise CodecError(f"unsupported scalar tag on decode: {tag:#x}")


def _decode_array(tag: int, data: bytes, i: int) -> tuple[list, int]:
    base = tag & 0x7F
    count = struct.unpack_from("<H", data, i)[0]
    pos = i + 2
    out: list[Any] = []

    if base == 0x08:  # u32 array
        for _ in range(count):
            out.append(struct.unpack_from("<I", data, pos)[0])
            pos += 4
        return out, pos - i

    if base == 0x10:  # u64 array
        for _ in range(count):
            out.append(struct.unpack_from("<Q", data, pos)[0])
            pos += 8
        return out, pos - i

    if base == 0x00:  # bool array (1 byte per element, 0=false / 1=true)
        for _ in range(count):
            out.append(bool(data[pos]))
            pos += 1
        return out, pos - i

    if base == 0x18:  # ipv6 array (16 bytes per element)
        for _ in range(count):
            out.append(bytes(data[pos : pos + 16]))
            pos += 16
        return out, pos - i

    if base in (0x20, 0x30):  # string / raw array
        for _ in range(count):
            n = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            out.append(bytes(data[pos : pos + n]))
            pos += n
        return out, pos - i

    if base == 0x28:  # message array
        # The wire format here is the same per-element 2-byte size header used
        # for string/raw arrays; we decode it that way and treat each blob as a
        # nested M2 record.
        for _ in range(count):
            n = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            out.append(Message.from_bytes(bytes(data[pos : pos + n])))
            pos += n
        return out, pos - i

    raise CodecError(f"unsupported array tag on decode: {tag:#x}")
