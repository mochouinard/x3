"""Terminal session over the modern Winbox transport.

Opens a remote-shell-style session against a router device using the
(76) cmd 0xa0065 handler — the same one Winbox 3 uses for "New Terminal".

Flow control (the missing piece for /export):

  Each WB→DEV terminal-data message carries field 0x000003, a u32 holding
  the **cumulative byte count of output received from the device** so far.
  The device drains its 8 KB output window as that counter advances. Real
  Winbox 3 sends an ack frame after every received chunk; without it we
  hung at exactly 7649 bytes (the buffer minus the welcome banner).
  Decoded plaintext from a real Winbox-3.42→v7.21.1 session through
  our MITM proxy in --shell-only mode confirmed the field semantics:

    WB→DEV  cmd 0xa0067 (no rid)
      0xff001c (string-array) = [b"winbox-3.42"]    # client identity
      0xfe0001 (u32)          = sid                 # from open reply
      0x000003 (u32)          = cumulative bytes received from device
      0x000002 (raw)          = optional input bytes (omit for ack-only)

    DEV→WB  cmd 0xa0067 (no rid)
      0xff001c (string-array) = [b"msg-proxy-X.Y.Z"]
      0xfe0001 (u32)          = sid
      0x000002 (raw)          = output bytes (counted toward 0x000003)

  Requests are fire-and-forget — no SYS_REQUEST_ID, no reply_expected.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import threading
import time
from typing import Callable

from . import message as m
from .client_ident import CLIENT_IDENT_FIELD, resolve_client_ident
from .modern import ModernSession
from .session import WinboxError


_TERMINAL_FROM = (0, 488)        # value real Winbox 3 uses on terminal sock
_TERMINAL_TO = (76,)
_CMD_TERMINAL_OPEN = 0xA0065     # client -> device, opens session
_CMD_TERMINAL_DATA = 0xA0067     # bidirectional: input + output + acks
_CMD_TERMINAL_WINCH = 0xA0068    # client -> device, runtime resize


def open_terminal_session(
    sess: ModernSession,
    password: str,
    cols: int = 80,
    rows: int = 24,
    term: str = "vt102",
    client_ident: str | None = None,
    from_path: tuple[int, ...] = _TERMINAL_FROM,
) -> tuple[m.Message, int | None]:
    """Send the terminal-open request. Returns (reply, session_id).

    ``from_path`` is this terminal's own SYS_FROM. Real Winbox uses a distinct
    one per terminal window (from=[0,496] then [0,488] in
    a session capture), which is what lets several
    terminals -- and Safe Mode -- share one connection without stealing each
    other's output.
    """
    ident = resolve_client_ident(
        client_ident if client_ident is not None else getattr(sess, "client_ident", None)
    )
    inner = m.Message().add_u32(0x000001, 0)
    msg = (
        m.Message()
        .add_u32_array(m.SYS_TO, list(_TERMINAL_TO))
        .add_u32_array(m.SYS_FROM, list(from_path))
        .set_command(_CMD_TERMINAL_OPEN)
        .set_request_id(sess.next_request_id())
        .set_reply_expected(True)
        .add_string_array(CLIENT_IDENT_FIELD, [ident.encode("utf-8")])
        .add_string(0x000001, password)
        .add_u32(0x000005, cols)
        .add_u32(0x000006, rows)
        .add_u32(0x000008, 0)
        .add_message(0x00000B, inner)
        .add_string(0x000007, term)
    )
    reply = sess.request(msg)
    sid = reply.get_u32(0xFE0001)
    return reply, sid


def _ident_bytes(sess: ModernSession) -> bytes:
    return resolve_client_ident(getattr(sess, "client_ident", None)).encode("utf-8")


def _build_terminal_data(
    sess: ModernSession, sid: int, ack: int, data: bytes | None,
    *, from_path: tuple[int, ...] = _TERMINAL_FROM,
) -> m.Message:
    """Build a fire-and-forget terminal-data message (no rid, no reply).
    `ack` is the cumulative bytes-received counter; `data` is optional input."""
    msg = (
        m.Message()
        .add_u32_array(m.SYS_TO, list(_TERMINAL_TO))
        .add_u32_array(m.SYS_FROM, list(from_path))
        .set_command(_CMD_TERMINAL_DATA)
        .add_string_array(CLIENT_IDENT_FIELD, [_ident_bytes(sess)])
        .add_u32(0xFE0001, sid)
        .add_u32(0x000003, ack)
    )
    if data:
        msg.add_raw(0x000002, data)
    return msg


def send_input(
    sess: ModernSession, sid: int, ack: int, data: bytes,
    *, from_path: tuple[int, ...] = _TERMINAL_FROM,
) -> None:
    sess.send(_build_terminal_data(sess, sid, ack, data, from_path=from_path))


def _send_ack(
    sess: ModernSession, sid: int, ack: int,
    *, from_path: tuple[int, ...] = _TERMINAL_FROM,
) -> None:
    sess.send(_build_terminal_data(sess, sid, ack, None, from_path=from_path))


def send_winch(
    sess: ModernSession, sid: int, cols: int, rows: int,
    *, from_path: tuple[int, ...] = _TERMINAL_FROM,
) -> None:
    """Send a runtime window-size update. Mirrors `cmd 0xa0068` that real
    Winbox 3 emits on terminal-window resize (observed in
    a session capture)."""
    msg = (
        m.Message()
        .add_u32_array(m.SYS_TO, list(_TERMINAL_TO))
        .add_u32_array(m.SYS_FROM, list(from_path))
        .set_command(_CMD_TERMINAL_WINCH)
        .add_string_array(CLIENT_IDENT_FIELD, [_ident_bytes(sess)])
        .add_u32(0x000005, cols)
        .add_u32(0x000006, rows)
        .add_u32(0xFE0001, sid)
    )
    sess.send(msg)


class TerminalChannel:
    """One the router console on a demultiplexed session.

    Owns the session id and the flow-control ack so callers only deal in bytes.
    ``on_output`` is called from the reader thread, immediately *after* the ack
    for that chunk has gone out -- routing the ack through another thread
    reintroduces the 8 KB stall described at the top of this module.
    """

    def __init__(
        self,
        sess: ModernSession,
        *,
        password: str,
        from_path: tuple[int, ...],
        cols: int = 80,
        rows: int = 24,
        term: str = "vt102",
        client_ident: str | None = None,
        on_output: Callable[[bytes], None] | None = None,
        on_closed: Callable[[BaseException | None], None] | None = None,
    ) -> None:
        self.sess = sess
        self.password = password
        self.from_path = tuple(from_path)
        self.cols = cols
        self.rows = rows
        self.term = term
        self.client_ident = client_ident
        self.on_output = on_output
        self.on_closed = on_closed
        self.sid: int | None = None
        self.bytes_in = 0
        self.bytes_out = 0
        self._ack = 0
        self._sub = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def alive(self) -> bool:
        t = self._thread
        return self.sid is not None and t is not None and t.is_alive()

    def open(self, *, timeout: float | None = None) -> int:
        demux = getattr(self.sess, "demux", None)
        if demux is None:
            raise WinboxError(
                "terminal needs a demultiplexed session (call start_demux)"
            )
        # Subscribe first: the banner starts streaming interleaved with the open
        # reply, so subscribing afterwards loses the first chunks.
        self._sub = demux.subscribe(self.from_path)
        try:
            reply, sid = open_terminal_session(
                self.sess, self.password, self.cols, self.rows, self.term,
                self.client_ident, from_path=self.from_path,
            )
        except BaseException:
            demux.unsubscribe(self._sub)
            self._sub = None
            raise
        if reply.has_error() or sid is None:
            demux.unsubscribe(self._sub)
            self._sub = None
            raise WinboxError(
                reply.error_string() or "terminal open returned no session id"
            )
        self.sid = sid
        self._ack = 0
        return sid

    def start_reader(self) -> None:
        if self._thread is not None or self.sid is None:
            return
        self._thread = threading.Thread(
            target=self._read_loop, name=f"winbox-term-{self.sid}", daemon=True
        )
        self._thread.start()

    def _read_loop(self) -> None:
        err: BaseException | None = None
        try:
            while not self._stop.is_set():
                msg = self._sub.get(timeout=0.5)
                if msg is None:
                    continue
                data = msg.get_raw(0x000002)
                if not data:
                    continue
                data = bytes(data)
                self.bytes_in += len(data)
                self._ack = (self._ack + len(data)) & 0xFFFFFFFF
                # Ack first, then hand the bytes up: the device drains its 8 KB
                # output window as this counter advances.
                _send_ack(self.sess, self.sid, self._ack, from_path=self.from_path)
                if self.on_output is not None:
                    self.on_output(data)
        except BaseException as e:  # noqa: BLE001
            if not self._stop.is_set():
                err = e
        finally:
            self._stop.set()
            if self.on_closed is not None:
                self.on_closed(err)

    def write(self, data: bytes) -> None:
        if self.sid is None or not data:
            return
        self.bytes_out += len(data)
        send_input(self.sess, self.sid, self._ack, data, from_path=self.from_path)

    def resize(self, cols: int, rows: int) -> None:
        if self.sid is None or (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        send_winch(self.sess, self.sid, cols, rows, from_path=self.from_path)

    def close(self, *, quit_cmd: bool = True, grace: float = 0.2) -> None:
        """Shut this console down, leaving the TCP session alone.

        There is no terminal-close command: every ``to=[76]`` frame in the
        captures is open/data/winch. So ask the router to close the console the
        way a user would, then stop listening.
        """
        if self.sid is not None and quit_cmd and not self._stop.is_set():
            try:
                self.write(b"/quit\r")
                time.sleep(grace)
            except Exception:  # noqa: BLE001
                pass
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if self._sub is not None:
            demux = getattr(self.sess, "demux", None)
            if demux is not None:
                demux.unsubscribe(self._sub)
            self._sub = None
        self.sid = None


def run_terminal(
    host: str, port: int, user: str, password: str,
    cols: int = 80, rows: int = 24,
    client_ident: str | None = None,
) -> int:
    """Interactive terminal session. Reads stdin, sends as terminal input;
    receives async output events and writes to stdout. Ctrl-] exits.

    POSIX only: it puts the local terminal in raw mode and follows SIGWINCH.
    On Windows the desktop client's Terminal window is the console instead.
    The channel itself (:class:`TerminalChannel`) is portable -- config
    snapshots and ``x3ctl export`` read the console through it everywhere.
    """
    try:
        import termios
        import tty
    except ImportError:
        print("x3ctl terminal needs a POSIX terminal; on Windows use the "
              "Terminal window in the desktop client.", file=sys.stderr)
        return 2
    with ModernSession(host, port, timeout=15.0, client_ident=client_ident) as sess:
        sess.authenticate(user, password)
        print(
            f"# x3ctl terminal — connected to {host}:{port} "
            f"(client-ident={sess.client_ident})",
            file=sys.stderr,
        )
        sess.start_demux()

        def to_stdout(data: bytes) -> None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            if os.environ.get("X3_TERMINAL_DEBUG"):
                sys.stderr.write(f"\r\n[recv {len(data)}B]\r\n")
                sys.stderr.flush()

        chan = TerminalChannel(
            sess, password=password, from_path=_TERMINAL_FROM,
            cols=cols, rows=rows, client_ident=client_ident,
            on_output=to_stdout,
        )
        try:
            chan.open()
        except WinboxError as e:
            print(f"!! terminal-open failed: {e}", file=sys.stderr)
            return 2
        print(f"# terminal session id={chan.sid}", file=sys.stderr)
        print("# Ctrl-] to quit", file=sys.stderr)
        chan.start_reader()

        stdin_fd = sys.stdin.fileno()
        old = termios.tcgetattr(stdin_fd)
        try:
            tty.setraw(stdin_fd)
            _interactive_loop(chan)
        finally:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
            chan.close(quit_cmd=False)
            sys.stderr.write("\r\n# disconnected\r\n")
            sys.stderr.flush()
        return 0


def _interactive_loop(chan: "TerminalChannel") -> None:
    """Pump the local tty into an already-open channel.

    The channel owns the reader thread, the flow-control ack and the write
    serialisation, so this is just stdin forwarding plus SIGWINCH.
    """

    # Forward local terminal-resize events. Important for `/export` and other
    # commands that line-wrap to the terminal width.
    def on_winch(_signum, _frame):
        try:
            size = os.get_terminal_size()
        except OSError:
            return
        try:
            chan.resize(size.columns, size.lines)
        except (OSError, ConnectionError):
            pass

    prev_winch = signal.signal(signal.SIGWINCH, on_winch)
    try:
        while chan.alive:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not ready:
                continue
            data = os.read(sys.stdin.fileno(), 1024)
            if not data:
                break
            # Ctrl-] (0x1d) escapes
            if b"\x1d" in data:
                break
            chan.write(data)
    finally:
        signal.signal(signal.SIGWINCH, prev_winch)
