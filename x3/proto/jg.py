"""Parser for the JavaScript-object-literal grammar used by Winbox .jg files.

Winbox downloads its entire UI definition from the router as a set of plugin
files -- ``roteros.jg`` plus one per feature package -- and caches them under
``<version>-<build_id>/``. They are not compiled: each file is a single
JavaScript array literal describing every menu, dialog and field the UI knows
about, including the handler paths and wire field ids used to talk to the router.

The grammar is a JSON5 subset:

    - bare identifier keys              {name:'IP Scan',type:'query'}
    - single-quoted strings
    - decimal and 0x integers
    - arrays, nested objects, trailing commas

``x3ctl mirror`` already fetches these files off the device, so a client
can load its own schema from whatever box it happens to be talking to.
"""

from __future__ import annotations

import gzip
import re
from typing import Any

_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_NUM = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_WORDS = {"true": True, "false": False, "null": None}


class JgError(Exception):
    pass


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def _ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def _at(self) -> str:
        if self.i >= len(self.s):
            raise JgError(f"unexpected end of input at offset {self.i}")
        return self.s[self.i]

    def _expect(self, ch: str) -> None:
        self._ws()
        if self._at() != ch:
            raise JgError(f"expected {ch!r} at offset {self.i}, got {self.s[self.i:self.i + 24]!r}")
        self.i += 1

    def value(self) -> Any:
        self._ws()
        c = self._at()
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "'\"":
            return self.string(c)
        m = _NUM.match(self.s, self.i)
        if m:
            self.i = m.end()
            tok = m.group(0)
            if tok[:2].lower() == "0x" or tok[:3].lower() == "-0x":
                return int(tok, 16)
            if "." in tok or "e" in tok.lower():
                return float(tok)
            return int(tok)
        m = _IDENT.match(self.s, self.i)
        if m:
            self.i = m.end()
            word = m.group(0)
            return _WORDS.get(word, word)
        raise JgError(f"unparsable value at offset {self.i}: {self.s[self.i:self.i + 24]!r}")

    def string(self, quote: str) -> str:
        self.i += 1
        out: list[str] = []
        while True:
            c = self._at()
            if c == "\\":
                nxt = self.s[self.i + 1]
                out.append(_ESCAPES.get(nxt, nxt))
                self.i += 2
                continue
            self.i += 1
            if c == quote:
                return "".join(out)
            out.append(c)

    def arr(self) -> list[Any]:
        self._expect("[")
        out: list[Any] = []
        self._ws()
        if self._at() == "]":
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self._ws()
            if self._at() == ",":
                self.i += 1
                self._ws()
                if self._at() == "]":  # trailing comma
                    self.i += 1
                    return out
                continue
            self._expect("]")
            return out

    def obj(self) -> dict[str, Any]:
        self._expect("{")
        out: dict[str, Any] = {}
        self._ws()
        if self._at() == "}":
            self.i += 1
            return out
        while True:
            self._ws()
            c = self._at()
            if c in "'\"":
                key = self.string(c)
            else:
                m = _IDENT.match(self.s, self.i) or _NUM.match(self.s, self.i)
                if not m:
                    raise JgError(f"unparsable key at offset {self.i}: {self.s[self.i:self.i + 24]!r}")
                self.i = m.end()
                key = m.group(0)
            self._expect(":")
            out[key] = self.value()
            self._ws()
            if self._at() == ",":
                self.i += 1
                self._ws()
                if self._at() == "}":  # trailing comma
                    self.i += 1
                    return out
                continue
            self._expect("}")
            return out


def loads(text: str) -> Any:
    p = _Parser(text)
    value = p.value()
    p._ws()
    if p.i != len(p.s):
        raise JgError(f"trailing data at offset {p.i}: {p.s[p.i:p.i + 32]!r}")
    return value


def load(path: str) -> Any:
    """Parse a .jg file. Transparently handles the gzipped .jg.gz wire form."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return loads(raw.decode("utf-8", "replace"))
