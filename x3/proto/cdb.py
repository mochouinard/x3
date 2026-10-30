"""Reader (and lossless writer) for address-book ``.cdb`` files.

The reference client stores its saved connections in ``Addresses.cdb``, in
the folder :data:`CLIENT_DIR` names below an AppData-style root. The container is dead simple: a fixed
signature followed by a flat sequence of length-prefixed blocks.

    "\\x0d\\xf0\\x1d\\xc0"            4-byte file signature
    repeated, one per saved entry:
        block_len : u32 LE           length of the M2 block that follows
        <block_len bytes>            an M2 message ("M2" magic + entries)

Each block is a single address-book entry serialized in the exact same M2 wire
format x3ctl already speaks, so we reuse :mod:`x3.proto.message` verbatim.
Every block in real files round-trips byte-for-byte through that codec, which
makes the writer here lossless: reading a file and writing it back reproduces
the original bytes.

Read is fully implemented. Editing (``add`` / ``set`` / ``del``) is not wired
into the CLI yet, but :class:`CdbFile` already models the file as a mutable list
of entries and can re-serialize it, so those are small additions.

Field codes carried inside each entry (low 24 bits of the M2 marker):

    1  host          6  session       (0xff0001 to / 0xff0002 from,
    2  login         8  group          0xff0005 reply-expected,
    3  password      0x0b romon-agent  0xff0006 request-id, 0xff0007 command,
    4  note                            0xfe0001 row-id ... are M2 plumbing)

The password (code 3) is stored in plaintext. It is therefore redacted in all
output unless ``--show-passwords`` is given.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from typing import Iterable, Iterator

from . import message as m

SIGNATURE = b"\x0d\xf0\x1d\xc0"

# Per-record field codes and their human names.
F_HOST = 0x01
F_LOGIN = 0x02
F_PASSWORD = 0x03
F_NOTE = 0x04
F_SESSION = 0x06
F_GROUP = 0x08
F_ROMON_AGENT = 0x0B

# Presentation order matches Winbox's own "Manage" dialog.
FIELD_NAMES: dict[int, str] = {
    F_HOST: "host",
    F_LOGIN: "login",
    F_PASSWORD: "password",
    F_NOTE: "note",
    F_SESSION: "session",
    F_GROUP: "group",
    F_ROMON_AGENT: "romon-agent",
}
NAME_TO_CODE: dict[str, int] = {v: k for k, v in FIELD_NAMES.items()}

# Fields whose values must never be printed by default.
SECRET_CODES: frozenset[int] = frozenset({F_PASSWORD})

REDACTED = "********"

#: The folder the reference client keeps ``Addresses.cdb`` in, below an
#: AppData-style root. A path on disk, so it is spelled the way the client
#: writes it, and it writes it two ways: version 3 creates the first spelling
#: and version 4 the second (it renames the folder when it finds the old one,
#: and both versions share the one address book inside, in this format). That
#: only matters on a case-sensitive filesystem -- Wine prefixes and native
#: Linux builds -- but there both have to be tried. This is the only place
#: either is spelled.
CLIENT_DIRS = ("Mikrotik/Winbox", "MikroTik/WinBox")
CLIENT_DIR = CLIENT_DIRS[0]


class CdbError(Exception):
    pass


class CdbEntry:
    """One address-book entry, backed by an M2 :class:`~x3.proto.message.Message`.

    The full message is preserved so nothing is lost on rewrite; named accessors
    cover the string fields a human cares about.
    """

    def __init__(self, msg: m.Message) -> None:
        self.message = msg

    def get(self, field: int | str) -> str | None:
        """Return a string field by code or name, or ``None`` if absent."""
        code = self._code(field)
        return self.message.get_string(code)

    @property
    def host(self) -> str | None:
        return self.get(F_HOST)

    @property
    def login(self) -> str | None:
        return self.get(F_LOGIN)

    @property
    def password(self) -> str | None:
        return self.get(F_PASSWORD)

    @property
    def password_bytes(self) -> bytes | None:
        """The password exactly as stored, undecoded.

        :attr:`password` runs it through ``get_string`` -- UTF-8 with
        replacement -- which is fine for display but lossy for a secret saved
        in a single-byte codepage: 0xe9 ('é' in CP1252/Latin-1) is not valid
        UTF-8 and comes back as U+FFFD. The login *hashes* the password, so it
        has to see the original bytes, not the replaced ones (see
        :meth:`x3.proto.crypto.WCurve.gen_password_validator_priv`). This is
        those bytes; ``None`` when the entry carries no password field.
        """
        return self.message.get_raw(F_PASSWORD)

    @property
    def note(self) -> str | None:
        return self.get(F_NOTE)

    @property
    def session(self) -> str | None:
        return self.get(F_SESSION)

    @property
    def group(self) -> str | None:
        return self.get(F_GROUP)

    def named_fields(self, *, show_passwords: bool = False) -> dict[str, str]:
        """Known string fields present in this entry, in dialog order.

        Password is replaced with :data:`REDACTED` unless ``show_passwords``.
        """
        out: dict[str, str] = {}
        for code, name in FIELD_NAMES.items():
            val = self.message.get_string(code)
            if val is None:
                continue
            if code in SECRET_CODES and not show_passwords:
                val = REDACTED
            out[name] = val
        return out

    def raw_fields(self, *, show_passwords: bool = False) -> list[dict]:
        """Every M2 entry as ``{code, tag, name, value}``, values redacted."""
        rows: list[dict] = []
        for code, tag, value in self.message:
            name = FIELD_NAMES.get(code)
            if code in SECRET_CODES and not show_passwords:
                shown: object = REDACTED
            else:
                shown = _jsonable(value)
            rows.append({"code": code, "tag": tag, "name": name, "value": shown})
        return rows

    @staticmethod
    def _code(field: int | str) -> int:
        if isinstance(field, int):
            return field
        try:
            return NAME_TO_CODE[field]
        except KeyError:
            raise CdbError(f"unknown field name {field!r}") from None

    def __repr__(self) -> str:
        host = self.host or "?"
        note = self.note or ""
        return f"CdbEntry(host={host!r}, note={note!r})"


class CdbFile:
    """A parsed ``.cdb`` file: an ordered, mutable list of entries."""

    def __init__(self, entries: list[CdbEntry] | None = None) -> None:
        self.entries: list[CdbEntry] = list(entries or [])

    # --- parsing --------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes) -> "CdbFile":
        if data[:4] != SIGNATURE:
            raise CdbError(
                f"bad file signature: expected {SIGNATURE.hex()}, got {data[:4].hex()}"
            )
        entries: list[CdbEntry] = []
        i = 4
        n = len(data)
        block_no = 0
        while i < n:
            if i + 4 > n:
                raise CdbError(f"truncated block length at block #{block_no}")
            (size,) = struct.unpack_from("<I", data, i)
            i += 4
            if i + size > n:
                raise CdbError(
                    f"block #{block_no} claims {size} bytes but only "
                    f"{n - i} remain"
                )
            block = data[i : i + size]
            i += size
            try:
                msg = m.Message.from_bytes(block)
            except m.CodecError as e:
                raise CdbError(f"block #{block_no} is not a valid M2 message: {e}") from e
            entries.append(CdbEntry(msg))
            block_no += 1
        return cls(entries)

    @classmethod
    def load(cls, path: str) -> "CdbFile":
        with open(path, "rb") as fh:
            return cls.from_bytes(fh.read())

    # --- serialization --------------------------------------------------

    def to_bytes(self) -> bytes:
        out = bytearray(SIGNATURE)
        for entry in self.entries:
            block = entry.message.to_bytes()
            out += struct.pack("<I", len(block))
            out += block
        return bytes(out)

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(self.to_bytes())

    # --- container sugar ------------------------------------------------

    def __iter__(self) -> Iterator[CdbEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _jsonable(value: object) -> object:
    """Best-effort conversion of an M2 value into something printable/JSON-safe."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, m.Message):
        return "<nested M2 message>"
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render_table(entries: Iterable[CdbEntry], *, show_passwords: bool) -> str:
    cols = ["#", "host", "login", "note", "session", "group"]
    if show_passwords:
        cols.insert(3, "password")
    rows: list[list[str]] = []
    for idx, e in enumerate(entries):
        f = e.named_fields(show_passwords=show_passwords)
        row = [str(idx)]
        for c in cols[1:]:
            row.append(f.get(c, ""))
        rows.append(row)
    widths = [len(c) for c in cols]
    for row in rows:
        for j, cell in enumerate(row):
            widths[j] = max(widths[j], len(cell))
    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
    lines = [fmt(cols), fmt(["-" * w for w in widths])]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def _cmd_show(args) -> int:
    try:
        cdb = CdbFile.load(args.file)
    except (OSError, CdbError) as e:
        print(f"x3cdb: {e}", file=sys.stderr)
        return 2

    entries = cdb.entries
    if args.grep:
        needle = args.grep.lower()
        entries = [
            e
            for e in entries
            if any(
                needle in (v or "").lower()
                for v in e.named_fields(show_passwords=True).values()
            )
        ]

    if args.format == "json":
        payload = [e.named_fields(show_passwords=args.show_passwords) for e in entries]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif args.format == "raw":
        for idx, e in enumerate(entries):
            print(f"# entry {idx}")
            for r in e.raw_fields(show_passwords=args.show_passwords):
                label = r["name"] or f"code {r['code']:#08x}"
                print(f"  {r['code']:#08x} tag {r['tag']:#04x} {label:>12} = {r['value']!r}")
    else:  # table
        print(_render_table(entries, show_passwords=args.show_passwords))
        print(f"\n{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x3cdb",
        description="Read address-book (Addresses.cdb) files. Passwords are "
        "redacted unless --show-passwords is given.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="List / dump entries from a .cdb file.")
    p.add_argument("file", help="Path to an Addresses.cdb file.")
    p.add_argument(
        "--format",
        choices=["table", "json", "raw"],
        default="table",
        help="Output style (default: table).",
    )
    p.add_argument(
        "--show-passwords",
        action="store_true",
        help="Reveal plaintext passwords (they are redacted by default).",
    )
    p.add_argument(
        "--grep",
        metavar="TEXT",
        help="Only show entries whose host/login/note/etc. contain TEXT.",
    )
    p.set_defaults(func=_cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
