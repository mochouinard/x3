"""Per-connection transcript of what we asked the device and what it replied.

One file per connection under ``~/.local/share/x3/logs`` (override with
``$X3_LOG_DIR``), plus the most recent entries in memory for the UI.

The point is reviewability: what did we change, and what was there before. High
frequency polling (the 1 Hz Traffic tab, bandwidth graphs) is therefore *not*
logged — pass ``quiet=True`` for those. Everything else is.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Field names whose values must never reach a log file. Matching is by name
# rather than code on purpose: 0xfe0009 is STD_DESCR (the object comment), and
# secrets are declared per menu with menu-local codes.
_SECRET_HINT = ("password", "secret", "passphrase", "psk", "key")


def log_dir() -> Path:
    env = os.environ.get("X3_LOG_DIR")
    if env:
        return Path(env).expanduser()
    from x3 import paths

    return paths.data_dir() / "logs"


def _redact(name: str, value: Any) -> Any:
    low = (name or "").lower()
    if any(h in low for h in _SECRET_HINT):
        return "<redacted>"
    return value


@dataclass
class LogEntry:
    """One request/reply exchange, or a UI-level note."""

    when: float
    kind: str  # request | reply | error | note | change
    path: tuple[int, ...] | None = None
    command: int | None = None
    detail: str = ""
    fields: list[tuple[str, Any]] = field(default_factory=list)

    def stamp(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.when))

    def header(self) -> str:
        where = ",".join(str(p) for p in self.path) if self.path else ""
        cmd = f" cmd={self.command:#x}" if isinstance(self.command, int) else ""
        target = f" [{where}]" if where else ""
        return f"{self.stamp()}  {self.kind.upper():<7}{target}{cmd}"

    def as_text(self) -> str:
        lines = [self.header() + (f"  {self.detail}" if self.detail else "")]
        for name, value in self.fields:
            lines.append(f"{'':>10}  {name} = {value}")
        return "\n".join(lines)


class SessionLog:
    """Append-only transcript for one device connection."""

    def __init__(self, host: str, user: str, *, memory: int = 4000) -> None:
        self.host = host
        self.user = user
        self.started = time.time()
        self.entries: deque[LogEntry] = deque(maxlen=memory)
        self.suppressed = 0  # polling exchanges deliberately not recorded
        self._lock = threading.Lock()
        self.path: Path | None = None
        self._fh = None
        self._open_file()

    # --- file ----------------------------------------------------------

    def _open_file(self) -> None:
        try:
            d = log_dir()
            d.mkdir(parents=True, exist_ok=True)
            safe_host = "".join(c if c.isalnum() or c in "-._" else "_" for c in self.host)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
            self.path = d / f"{stamp}-{safe_host}.log"
            self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(
                f"# x3 session {self.user}@{self.host} "
                f"started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}\n"
            )
            self._fh.flush()
        except OSError:
            self.path = None
            self._fh = None

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    if self.suppressed:
                        self._fh.write(
                            f"# {self.suppressed} polling exchanges not recorded\n"
                        )
                    self._fh.flush()
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    # --- recording -----------------------------------------------------

    def add(self, entry: LogEntry) -> None:
        with self._lock:
            self.entries.append(entry)
            if self._fh is not None:
                try:
                    self._fh.write(entry.as_text() + "\n")
                    self._fh.flush()
                except OSError:
                    pass

    def note(self, detail: str) -> None:
        self.add(LogEntry(when=time.time(), kind="note", detail=detail))

    def change(self, detail: str, fields: list[tuple[str, Any]] | None = None) -> None:
        """Record an intended or applied configuration change."""
        self.add(LogEntry(
            when=time.time(), kind="change", detail=detail, fields=list(fields or []),
        ))

    def request(
        self,
        path: tuple[int, ...],
        command: int,
        fields: list[tuple[str, Any]] | None = None,
    ) -> None:
        self.add(LogEntry(
            when=time.time(), kind="request", path=tuple(path), command=command,
            fields=[(n, _redact(n, v)) for n, v in (fields or [])],
        ))

    def reply(
        self,
        path: tuple[int, ...],
        command: int,
        fields: list[tuple[str, Any]] | None = None,
        detail: str = "",
    ) -> None:
        self.add(LogEntry(
            when=time.time(), kind="reply", path=tuple(path), command=command,
            detail=detail, fields=[(n, _redact(n, v)) for n, v in (fields or [])],
        ))

    def error(self, path: tuple[int, ...], command: int, detail: str) -> None:
        self.add(LogEntry(
            when=time.time(), kind="error", path=tuple(path),
            command=command, detail=detail,
        ))

    def skip(self) -> None:
        """Count a polling exchange we deliberately did not record."""
        with self._lock:
            self.suppressed += 1

    def text(self) -> str:
        with self._lock:
            return "\n".join(e.as_text() for e in self.entries)
