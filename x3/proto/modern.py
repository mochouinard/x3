"""Modern (firmware 6.43+) Winbox transport.

Wire shape:

    Handshake stages (plaintext):
        [chunk_len:1][0x06][payload]
        Where 0x06 is the modern "handler index" used throughout the modern
        protocol, and `payload` is the raw key-exchange material (no M2
        framing). Stages:
          1. C->S  username || \\x00 || x_w_a:32 || parity:1
          2. S->C  x_w_b:32 || parity:1 || salt:16
          3. C->S  client_cc:32             (= SHA256(j || z))
          4. S->C  server_cc:32             (= SHA256(j || client_cc || z))
        After step 4 verifies, both sides derive AES-128-CBC and HMAC-SHA1
        keys from the SRP shared secret z.

    Encrypted M2 messages (post-handshake):
        outer chunked frame (same chunking pattern as legacy 0x01 frames,
        but with marker byte 0x06 instead of 0x01) wrapping:
            [inner_len:2 BE][iv:16][AES-CBC(M2 || HMAC-SHA1(M2) || pad_byte)]

        The inner trailing structure is:
            M2_message                    (variable)
            HMAC-SHA1(send_hmac_key, M2)  (20 bytes)
            pad_byte, repeated pad_byte+1 times   (see ros_pad)

        On receive, after AES-decrypt and stripping the trailing byte (which
        is sometimes a true PKCS#7 pad byte and sometimes not), the last 20
        bytes are the HMAC and what precedes them is the M2 message.

The EC-SRP5 maths and KDF live in `crypto.py`. This module owns the wire
format and session lifecycle.

See the Credits section of README.md for sources.
"""

from __future__ import annotations

import hmac as _hmac
import secrets
import socket
import struct
import sys
import threading
import time
from typing import Any

from Crypto.Cipher import AES
from Crypto.Hash import SHA1 as _CRY_SHA1
from Crypto.Util.Padding import unpad as _aes_unpad

from . import message as m
from .client_ident import (
    CLIENT_IDENT_DEFAULT,
    CLIENT_IDENT_FIELD,
    CLIENT_IDENT_WINBOX3,
    CLIENT_IDENT_WINBOX4,
    resolve_client_ident,
)
from .crypto import WCurve, derive_session_keys, ros_scalar_bytes, sha256


_MARKER_FIRST = 0x06
_MARKER_CONT = 0xFF
_AES_BLOCK = 0x10
_INNER_HEADER = 0x12   # 2-byte length + 16-byte IV

# Modern the router expects every authenticated request to carry SYS_FROM. The
# client-side handler id is arbitrary; Margin Research's reference uses 11.
DEFAULT_FROM = (0, 11)

# The mproxy file handler on firmware 7 routes file-fetch replies through a
# different client-side handler than session/menu calls. Real Winbox 3
# uses [0, 8] for file IO. This is wired into `fetch_file` below.
FILE_FETCH_FROM = (0, 8)

# Re-export client identity helpers (see client_ident.py). Default is
# Winbox 4.0 so we keep working if firmware drops Winbox 3 acceptance.


class ModernAuthError(Exception):
    pass


class ModernProtocolError(Exception):
    pass


class ModernSession:
    """Authenticated, encrypted Winbox session for firmware 6.43+.

    ``host`` is an IP/hostname for TCP 8291, or a MAC address
    (``aa:bb:cc:dd:ee:ff``) for MAC-Winbox over UDP 20561 when
    ``transport="mac"`` (or when ``host`` looks like a MAC and
    ``transport="auto"``).
    """

    def __init__(
        self,
        host: str,
        port: int = 8291,
        timeout: float = 10.0,
        *,
        transport: str = "auto",
        client_ident: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.transport_mode = transport  # auto | tcp | mac
        # Winbox 3.x vs 4.x identity on field 0xFF001C (see client_ident.py).
        self.client_ident = resolve_client_ident(client_ident)
        self._sock: socket.socket | object | None = None
        self._owns_sock = True
        self._send_aes_key: bytes = b""
        self._recv_aes_key: bytes = b""
        self._send_hmac_key: bytes = b""
        self._recv_hmac_key: bytes = b""
        self._next_request_id = 1
        self._authenticated = False
        self.via_mac = False
        # All socket writes serialize on this. Concurrent encrypted writes
        # interleave bytes on the wire and corrupt the framing, and with the
        # demux running the reader thread also writes (terminal acks).
        self._write_lock = threading.RLock()
        self._rid_lock = threading.Lock()
        self._demux: Any = None
        self._pre_demux_timeout: float | None = None
        #: The MAC transport while it is still connecting. cancel() needs a
        #: handle on it before _sock exists, which is precisely the window a
        #: blocking connect spends its time in.
        self._transport_obj: Any = None
        #: Optional x3.proto.audit.AuditRecorder. Set it and every frame this
        #: session sends or receives is recorded. Hooked here rather than in
        #: DeviceClient because this is the only place all traffic passes --
        #: menu reads, terminal data and file transfers alike.
        self.audit: Any = None

    def stamp_client_ident(self, msg: m.Message) -> m.Message:
        """Ensure ``msg`` carries 0xFF001C = [self.client_ident]."""
        if msg.find(CLIENT_IDENT_FIELD) is None:
            msg.add_string_array(
                CLIENT_IDENT_FIELD, [self.client_ident.encode("utf-8")]
            )
        return msg

    # --- lifecycle ----------------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        mode = self.transport_mode
        if mode == "auto":
            from .macwinbox import looks_like_mac

            mode = "mac" if looks_like_mac(self.host) else "tcp"
        if mode == "mac":
            from .macwinbox import MacWinboxTransport

            t = MacWinboxTransport(self.host, timeout=self.timeout)
            self._transport_obj = t     # reachable by cancel() during connect
            t.connect()
            self._sock = t
            self.via_mac = True
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.via_mac = False

    def cancel(self) -> None:
        """Abandon a connect in progress, from another thread.

        A blocking connect -- the MAC transport walking every local interface,
        or a TCP connect to an unreachable host -- cannot be interrupted from
        outside, so the worker doing it stays alive until its own deadline. That
        is long enough for Qt to destroy a still-running QThread and abort.
        """
        transport = getattr(self, "_transport_obj", None)
        cancel = getattr(transport, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        # Stop the reader before the socket goes, so it fails its waiters
        # instead of raising out of a blocking read.
        if self._demux is not None:
            try:
                self.stop_demux()
            except Exception:  # noqa: BLE001
                self._demux = None
        self._transport_obj = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        # Rounded off after the reader has stopped, so no frame arrives to be
        # recorded into a closed file.
        if self.audit is not None:
            try:
                self.audit.close(reason="session closed")
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> "ModernSession":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- authentication (EC-SRP5) -------------------------------------

    def peek_salt(self, username: str = "admin") -> str:
        """Run only stages 1-2 of the handshake and return the device's SRP
        salt (hex).

        The salt is a per-install fingerprint: stable across reboot, unique per
        unit, changing only on reset/reinstall. This sends stage 1 (the
        ``username`` and a fresh random public key) and reads stage 2; it does
        **not** send stage 3, so no password-derived material ever leaves the
        client. That makes it safe to identify a device *before* committing any
        credential to it -- the only thing disclosed is the username, which any
        SRP handshake must send regardless.
        """
        if self._sock is None:
            raise ModernAuthError("not connected")
        curve = WCurve()
        s_a = secrets.token_bytes(32)
        x_w_a, x_w_a_parity = curve.gen_public_key(s_a)
        stage1_payload = (
            username.encode("utf-8") + b"\x00" + x_w_a + bytes([x_w_a_parity])
        )
        self._send_handshake(stage1_payload)
        stage2 = self._recv_handshake()
        if len(stage2) != 32 + 1 + 16:
            raise ModernAuthError(
                f"stage 2 payload wrong size: {len(stage2)} (expected 49)"
            )
        self.salt = stage2[33:49].hex()
        return self.salt

    def authenticate(self, username: str, password: str | bytes) -> bytes:
        """Run the EC-SRP5 handshake. Returns the negotiated session secret z
        (32 bytes) so callers can confirm a successful exchange.

        ``password`` may be bytes: the SRP validator hashes it exactly, so a
        caller with the credential as the device stored it (the address book,
        see :meth:`x3.proto.cdb.CdbEntry.password_bytes`) passes those bytes
        rather than a str that a lossy UTF-8 decode may have already mangled.
        A typed str is UTF-8, as before."""
        if self._sock is None:
            raise ModernAuthError("not connected")

        curve = WCurve()

        # --- stage 1: client public key ---
        s_a = secrets.token_bytes(32)
        x_w_a, x_w_a_parity = curve.gen_public_key(s_a)
        if not curve.check(curve.lift_x(int.from_bytes(x_w_a, "big"), x_w_a_parity)):
            raise ModernAuthError("generated public key failed self-check")

        stage1_payload = (
            username.encode("utf-8")
            + b"\x00"
            + x_w_a
            + bytes([x_w_a_parity])
        )
        self._send_handshake(stage1_payload)

        # --- stage 2: server public key + salt ---
        stage2 = self._recv_handshake()
        if len(stage2) != 32 + 1 + 16:
            raise ModernAuthError(
                f"stage 2 payload wrong size: {len(stage2)} (expected 49)"
            )
        x_w_b = stage2[:32]
        x_w_b_parity = stage2[32]
        salt = stage2[33:49]
        # Retain the stage-2 salt: stable per install (survives reboot, changes
        # on reset), so it doubles as a pinnable device fingerprint.
        self.salt = salt.hex()

        # --- compute SRP shared secret z ---
        i = curve.gen_password_validator_priv(username, password, salt)
        x_gamma, gamma_parity = curve.gen_public_key(i)
        v = curve.redp1(x_gamma, 1)
        w_b = curve.lift_x(int.from_bytes(x_w_b, "big"), x_w_b_parity)
        w_b += v
        j = sha256(x_w_a + x_w_b)
        scalar = (
            int.from_bytes(i, "big") * int.from_bytes(j, "big")
            + int.from_bytes(s_a, "big")
        )
        scalar = curve.finite_field_value(scalar)
        z, _ = curve.to_montgomery(scalar * w_b)
        # The router hashes z in its minimal big-endian form; a fixed 32-byte pad
        # diverges for the ~1/256 of handshakes whose z starts with 0x00 and the
        # confirmation is rejected (see crypto.ros_scalar_bytes).
        z_hash = ros_scalar_bytes(z)
        secret = sha256(z_hash)

        # --- stage 3: client confirmation ---
        client_cc = sha256(j + z_hash)
        self._send_handshake(client_cc)

        # --- stage 4: server confirmation ---
        stage4 = self._recv_handshake()
        expected_server_cc = sha256(j + client_cc + z_hash)
        if stage4 != expected_server_cc:
            # A device that rejects the credentials sends its reason as ASCII
            # ("invalid user name or password (6)", 33 bytes) instead of a
            # 32-byte hash. A 32-byte reply that
            # still doesn't match means we derived the handshake differently
            # from the device — a client bug, not the password.
            if len(stage4) != 32 and all(32 <= b < 127 for b in stage4):
                raise ModernAuthError(f"device rejected login: {stage4.decode('ascii')}")
            raise ModernAuthError(
                f"server confirmation hash mismatch: device sent a {len(stage4)}-byte "
                "proof that doesn't match ours (handshake computed differently, "
                "not a rejected password)"
            )

        # --- session-key derivation ---
        (
            self._send_aes_key,
            self._recv_aes_key,
            self._send_hmac_key,
            self._recv_hmac_key,
        ) = derive_session_keys(secret, server=False)
        self._authenticated = True
        return z

    # --- M2 messaging (post-auth) -------------------------------------

    def send(self, msg: m.Message) -> None:
        if not self._authenticated:
            raise ModernProtocolError("not authenticated")
        raw = msg.to_bytes()
        with self._write_lock:
            self._send_encrypted(raw)
            # Stamped under the lock so the audit's frame order matches the
            # wire's; the file write itself is left outside it, since flushing
            # to disk while holding the socket lock would stall a pipelined
            # burst behind every other sender.
            sent_at = time.time() if self.audit is not None else 0.0
        if self.audit is not None:
            # After the write, so a frame that never reached the wire is not
            # recorded as if it had. ``audit_poll`` is stamped on the message
            # by whoever built it -- see DeviceClient._request.
            self.audit.sent(
                raw, msg,
                poll=bool(getattr(msg, "audit_poll", False)),
                when=sent_at,
            )

    def _recv_message(self) -> m.Message:
        """One blocking framed read plus decode. The demux reader owns this."""
        if not self._authenticated:
            raise ModernProtocolError("not authenticated")
        raw = self._recv_encrypted()
        try:
            msg = m.Message.from_bytes(raw)
        except m.CodecError as e:
            _dump_raw(raw, header=f"decode failed: {e}")
            if self.audit is not None:
                # A frame we cannot parse is precisely the one worth having on
                # disk, so record the bytes before letting the reader die.
                self.audit.received(raw)
                self.audit.note(f"undecodable frame ({len(raw)} bytes): {e}")
            raise
        if self.audit is not None:
            self.audit.received(raw, msg)
        import os
        if os.environ.get("X3_DEBUG"):
            _dump_raw(raw, header="reply M2")
            for code, t, v in msg:
                print(f"  field {code:#08x} tag {t:#04x} = {v!r}", flush=True)
        return msg

    def recv(self, timeout: float | None = None) -> m.Message:
        if self._demux is not None:
            # A caller reading raw frames under a running demux only ever sees
            # what nothing else claimed.
            return self._demux.recv_any(timeout=timeout)
        return self._recv_message()

    def request(self, msg: m.Message, *, timeout: float | None = None) -> m.Message:
        if self._demux is not None:
            return self._demux.request(msg, timeout=timeout)
        self.send(msg)
        return self._recv_message()

    def next_request_id(self) -> int:
        with self._rid_lock:
            rid = self._next_request_id
            self._next_request_id += 1
            return rid

    # --- frame demultiplexing -----------------------------------------

    @property
    def demux(self) -> Any:
        return self._demux

    def start_demux(self, *, on_unrouted=None, on_reader_error=None) -> Any:
        """Route incoming frames by (SYS_TO, request id) from a reader thread.

        Until this is called the session behaves exactly as before, so every
        existing caller keeps its code path.

        The socket goes permanently blocking. That is a requirement, not a
        preference: a timeout firing part way through a frame leaves the
        stream desynchronised with no way to resynchronise. Deadlines move up
        to the per-request level, where a late reply is merely late.
        """
        if self._demux is not None:
            return self._demux
        from .demux import FrameDemux

        sock = self._sock
        if sock is not None and hasattr(sock, "gettimeout"):
            try:
                self._pre_demux_timeout = sock.gettimeout()
            except OSError:
                self._pre_demux_timeout = None
        if sock is not None and hasattr(sock, "settimeout"):
            try:
                sock.settimeout(None)
            except OSError:
                pass
        demux = FrameDemux(
            self, on_unrouted=on_unrouted, on_reader_error=on_reader_error
        )
        self._demux = demux
        demux.start()
        return demux

    def stop_demux(self) -> None:
        demux, self._demux = self._demux, None
        if demux is None:
            return
        demux.stop()
        sock = self._sock
        if (
            sock is not None
            and self._pre_demux_timeout is not None
            and hasattr(sock, "settimeout")
        ):
            try:
                sock.settimeout(self._pre_demux_timeout)
            except OSError:
                pass

    def fetch_file(
        self,
        path: str,
        handler: tuple[int, int] = (2, 2),
        client_ident: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Download `path` via mproxy (2,2) — the firmware 7 plugin/file handler.

        Two v7-specific requirements (learned by mock-server inspection
        of real Winbox traffic):
          1. SYS_FROM must be [0, 8] (the file-fetch client subsystem),
             not the [0, 11] used for menu/session calls.
          2. Field 0xFF001C must carry a string-array client identity
             (``winbox-4.0`` default, or ``winbox-3.42``). Without this
             field firmware 7 drops/hangs file opens after a few plugins.

        See client_ident.py / README "Winbox 3 vs 4".

        For self-issued user filespace files use bare names. Paths with
        slashes get rerouted by msg-proxy and need different handling.
        """
        from .session import WinboxError

        ident = resolve_client_ident(
            client_ident if client_ident is not None else self.client_ident,
            env=False,
        )
        to_a, to_b = handler
        from_a, from_b = FILE_FETCH_FROM

        def _new_msg(cmd: int) -> m.Message:
            msg = (
                m.Message()
                .set_to(to_a, to_b)
                .add_u32_array(m.SYS_FROM, [from_a, from_b])
                .set_command(cmd)
                .set_request_id(self.next_request_id())
                .set_reply_expected(True)
            )
            msg.add_string_array(
                CLIENT_IDENT_FIELD, [ident.encode("utf-8")]
            )
            return msg

        open_req = _new_msg(7).add_string(0x000001, path)
        reply = self.request(open_req, timeout=timeout)
        if reply.has_error():
            raise WinboxError(f"open {path!r} failed: {reply.error_string()}")
        session_id = reply.session_id()
        if session_id is None:
            raise WinboxError(f"open {path!r}: server returned no session id")
        size = reply.get_u32(0x000002)
        if size is None:
            raise WinboxError(f"open {path!r}: server returned no file size")
        if size == 0:
            return b""

        # Real the router caps each read at ~32KB. For files larger than that the
        # server doesn't reply (or replies very slowly) to a single big read.
        # Mirror Winbox 3's approach: request 32KB at a time using the same
        # session_id until we've collected the full file.
        CHUNK = 0x8000
        out = bytearray()
        while len(out) < size:
            read_req = (
                _new_msg(4)
                .set_session_id(session_id)
                .add_u32(0x000002, min(CHUNK, size - len(out)))
            )
            data_reply = self.request(read_req, timeout=timeout)
            if data_reply.has_error():
                raise WinboxError(f"read {path!r} failed: {data_reply.error_string()}")
            chunk = data_reply.get_raw(0x000003)
            if chunk is None:
                raise WinboxError(f"read {path!r}: server returned no payload")
            out += chunk
            # 0x000004 means "this was the last chunk", not "the read
            # succeeded": a device omits it on every chunk but the final one.
            # Trusting `size` alone works while the device is honest, but a
            # short transfer would otherwise spin here or return quietly
            # truncated data.
            if data_reply.get_bool(0x000004):
                break
            if not chunk:
                raise WinboxError(
                    f"read {path!r}: empty chunk at {len(out)} of {size} bytes "
                    f"without the final-chunk flag"
                )
        if len(out) != size:
            raise WinboxError(
                f"read {path!r}: got {len(out)} bytes, index declared {size}"
            )
        return bytes(out)

    #: mproxy chunk size. Real the router caps a transfer chunk at ~32KB in both
    #: directions; Winbox splits a 37964-byte upload as 32168 + 5796.
    UPLOAD_CHUNK = 0x8000

    def upload_file(
        self,
        path: str,
        data: bytes,
        handler: tuple[int, int] = (2, 2),
        client_ident: str | None = None,
        timeout: float | None = None,
    ) -> int | None:
        """Upload `data` to `path` via mproxy (2,2). Returns the write handle.

        The inverse of :meth:`fetch_file`, captured from Winbox 4 writing a
        37964-byte file::

            cmd=0x1  0x2=<total size>  0x1=<name>      -> 0xFE0001 = handle
            cmd=0x2  0xFE0001=handle  0x3=<raw chunk>  0x4=False  -> ack
            cmd=0x2  0xFE0001=handle  0x3=<raw chunk>  0x4=True   -> ack

        Two details worth stating because they are easy to get wrong: every
        chunk carries the handle the open returned, in ``0xFE0001`` -- the
        proxy's text log never printed it, and a chunk sent without one is
        refused with ``unknown session id``; the raw ``.m2`` of the same
        capture shows it plainly. And the end of the transfer is the ``0x4``
        bool on the final chunk, not a separate close verb. The size in the
        open must match the bytes sent.
        """
        from .session import WinboxError

        def _new_msg(cmd: int) -> m.Message:
            return self._file_msg(cmd, handler, client_ident)

        open_req = _new_msg(1).add_u32(0x000002, len(data)).add_string(0x000001, path)
        reply = self.request(open_req, timeout=timeout)
        if reply.has_error():
            raise WinboxError(f"open {path!r} for write failed: {reply.error_string()}")
        handle = reply.get_u32(0xFE0001)

        chunk = self.UPLOAD_CHUNK
        sent = 0
        # An empty file still needs one chunk, or nothing marks the transfer
        # complete and the device is left holding an open handle.
        while True:
            body = data[sent : sent + chunk]
            sent += len(body)
            last = sent >= len(data)
            write = _new_msg(2)
            write.add_bool(0x000004, last)
            if handle is not None:
                write.add_u32(0xFE0001, handle)
            write.add_raw(0x000003, body)
            ack = self.request(write, timeout=timeout)
            if ack.has_error():
                raise WinboxError(
                    f"write {path!r} failed after {sent}B: {ack.error_string()}"
                )
            if last:
                break
        return handle

    def download_file(
        self,
        path: str,
        handler: tuple[int, int] = (2, 2),
        client_ident: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Read a user file out of the device via mproxy (2,2), in chunks.

        What Winbox does to save a ``.backup`` off the Files list, read off the
        raw ``.m2`` of the same capture as :meth:`upload_file`::

            cmd=0x3  0x1=<name>                       -> 0xFE0001=handle 0x2=size
            cmd=0x4  0xFE0001=handle  0x2=<max chunk> -> 0x3=<bytes>  0x4=eof

        :meth:`fetch_file`'s one-shot ``0x7`` serves the plugin set and answers
        a file someone uploaded with "cannot open source file".
        """
        from .session import WinboxError

        opened = self.request(
            self._file_msg(3, handler, client_ident).add_string(0x000001, path),
            timeout=timeout,
        )
        if opened.has_error():
            raise WinboxError(f"open {path!r} for read failed: {opened.error_string()}")
        handle = opened.get_u32(0xFE0001)
        size = opened.get_u32(0x000002)
        out = bytearray()
        while True:
            read = self._file_msg(4, handler, client_ident)
            read.add_u32(0xFE0001, handle)
            read.add_u32(0x000002, self.DOWNLOAD_CHUNK)
            reply = self.request(read, timeout=timeout)
            if reply.has_error():
                raise WinboxError(
                    f"read {path!r} failed after {len(out)}B: {reply.error_string()}"
                )
            chunk = reply.get_raw(0x000003) or b""
            out += chunk
            # eof is the device's word; the size is the backstop, so a reply
            # that omits the flag cannot spin this forever.
            if reply.get_bool(0x000004) or not chunk or (
                size is not None and len(out) >= size
            ):
                break
        return bytes(out)

    #: What Winbox asks for per read; the device answers with up to this much.
    DOWNLOAD_CHUNK = 32168

    def _file_msg(
        self, cmd: int, handler: tuple[int, int], client_ident: str | None
    ) -> m.Message:
        """A request to the mproxy file handler, stamped the way it wants."""
        ident = resolve_client_ident(
            client_ident if client_ident is not None else self.client_ident,
            env=False,
        )
        msg = (
            m.Message()
            .set_to(*handler)
            .add_u32_array(m.SYS_FROM, list(FILE_FETCH_FROM))
            .set_command(cmd)
            .set_request_id(self.next_request_id())
            .set_reply_expected(True)
        )
        msg.add_string_array(CLIENT_IDENT_FIELD, [ident.encode("utf-8")])
        return msg

    # ------------------------------------------------------------------
    # Plaintext-handshake wire framing  ([chunk_len:1][0x06][payload])
    # ------------------------------------------------------------------

    def _send_handshake(self, payload: bytes) -> None:
        if len(payload) > 0xFF:
            raise ModernProtocolError(
                f"handshake payload too long for a single chunk: {len(payload)}"
            )
        frame = bytes([len(payload), _MARKER_FIRST]) + payload
        self._sock.sendall(frame)

    def _recv_handshake(self) -> bytes:
        header = _recv_exact(self._sock, 2)
        plen = header[0]
        if header[1] != _MARKER_FIRST:
            raise ModernProtocolError(
                f"unexpected handshake marker: {header[1]:#x}"
            )
        return bytes(_recv_exact(self._sock, plen))

    # ------------------------------------------------------------------
    # Encrypted M2 frame:
    #   inner = [inner_len:2 BE][iv:16][AES-CBC(M2 || HMAC || pad_byte)]
    #   wire  = chunked outer with marker 0x06 on the first chunk
    # ------------------------------------------------------------------

    def _send_encrypted(self, m2_bytes: bytes) -> None:
        if m2_bytes[:2] != b"M2":
            raise ModernProtocolError("payload is not an M2 message")

        mac = _hmac.new(self._send_hmac_key, m2_bytes, "sha1").digest()
        plaintext = ros_pad(m2_bytes, mac)

        iv = secrets.token_bytes(_AES_BLOCK)
        ct = AES.new(self._send_aes_key, AES.MODE_CBC, iv).encrypt(plaintext)
        inner = struct.pack(">H", len(ct)) + iv + ct

        self._sock.sendall(_chunk_outer(inner, _MARKER_FIRST))

    def _recv_encrypted(self) -> bytes:
        inner = _read_known_size_chunks(
            self._sock, _MARKER_FIRST, total_inner_len_from_first_two_bytes=True
        )
        if len(inner) < 2 + _AES_BLOCK + _AES_BLOCK:
            raise ModernProtocolError(f"encrypted frame too short: {len(inner)}")
        ct_len = struct.unpack_from(">H", inner, 0)[0]
        iv = inner[2 : 2 + _AES_BLOCK]
        ct = inner[2 + _AES_BLOCK : 2 + _AES_BLOCK + ct_len]
        if len(ct) != ct_len:
            raise ModernProtocolError(
                f"truncated ciphertext: have {len(ct)}, declared {ct_len}"
            )
        plain = AES.new(self._recv_aes_key, AES.MODE_CBC, iv).decrypt(ct)
        # Strip the router's trailing byte. If the final byte is 0 it was the
        # pad-marker and the rest is unpadded; otherwise PKCS#7 covers it.
        if plain[-1] != 0:
            plain = _aes_unpad(plain, _AES_BLOCK)
        plain = plain[:-1]
        if len(plain) < 20:
            raise ModernProtocolError("decrypted payload missing HMAC tail")
        m2_bytes, mac = plain[:-20], plain[-20:]
        expected = _hmac.new(self._recv_hmac_key, m2_bytes, "sha1").digest()
        if not _hmac.compare_digest(mac, expected):
            raise ModernProtocolError("HMAC verification failed")
        return m2_bytes


# ---------------------------------------------------------------------------
# Outer-chunk framing helpers
# ---------------------------------------------------------------------------


def ros_pad(m2_bytes: bytes, mac: bytes) -> bytes:
    """Build the AES-CBC plaintext for one encrypted frame, the router-style.

    The router appends a count byte plus that many copies of itself, so the
    trailing run is ``pad_byte + 1`` bytes long and the total lands on a
    16-byte boundary. When ``pad_byte`` is 0 the buffer is already aligned and
    the single 0 byte is all that follows.

    That last case is why this is not just ``Crypto.Util.Padding.pad``: PKCS#7
    appends a whole extra 16-byte block to an already-aligned buffer, and real
    Winbox rejects such a frame with "Decrypt failed". It bites one frame in
    16 — whenever ``(len(m2) + 20) % 16 == 15``. The router itself accepts the
    over-padded form, so only traffic we send to a Winbox client (mock server,
    MITM proxy) shows the fault.
    """
    pad_byte = (0xF - (len(m2_bytes) + len(mac))) % _AES_BLOCK
    return m2_bytes + mac + bytes([pad_byte]) * (pad_byte + 1)


def _chunk_outer(inner: bytes, first_marker: int) -> bytes:
    """Wrap `inner` in [chunk_len:1][marker:1] segments. First segment uses
    `first_marker`; continuations use 0xFF."""
    out = bytearray()
    if len(inner) <= 0xFF:
        out.append(len(inner))
        out.append(first_marker)
        out += inner
        return bytes(out)

    out.append(0xFF)
    out.append(first_marker)
    out += inner[:0xFF]
    i = 0xFF
    while i < len(inner):
        chunk = inner[i : i + 0xFF]
        out.append(len(chunk))
        out.append(_MARKER_CONT)
        out += chunk
        i += 0xFF
    return bytes(out)


def _read_known_size_chunks(
    sock: socket.socket,
    expected_first_marker: int,
    total_inner_len_from_first_two_bytes: bool,
) -> bytes:
    """Reassemble a chunked frame whose total inner length is encoded in the
    first two bytes of the inner content.

    For encrypted M2 frames, those two bytes are the AES-CT length, and the
    full inner is `[len:2 BE] + [iv:16] + ct`. We use that to know exactly
    when to stop reading, which is more robust than guessing from chunk_len
    boundary behaviour (a 0xFF chunk could be exactly the last one).
    """
    if not total_inner_len_from_first_two_bytes:
        raise ModernProtocolError("unsupported read mode")

    header = _recv_exact(sock, 2)
    chunk_len = header[0]
    if header[1] != expected_first_marker:
        raise ModernProtocolError(
            f"unexpected first-chunk marker: {header[1]:#x}"
        )

    first_chunk = bytes(_recv_exact(sock, chunk_len))
    if len(first_chunk) < 2:
        raise ModernProtocolError("first chunk too short to carry inner length")
    inner_ct_len = struct.unpack_from(">H", first_chunk, 0)[0]
    total_inner = 2 + _AES_BLOCK + inner_ct_len

    inner = bytearray(first_chunk)
    while len(inner) < total_inner:
        sep = _recv_exact(sock, 2)
        more_len = sep[0]
        if sep[1] != _MARKER_CONT:
            raise ModernProtocolError(f"bad continuation marker: {sep[1]:#x}")
        need = total_inner - len(inner)
        if more_len > need:
            raise ModernProtocolError(
                f"continuation chunk len {more_len} exceeds remaining {need}"
            )
        inner += _recv_exact(sock, more_len)
    return bytes(inner[:total_inner])


def _dump_raw(raw: bytes, header: str = "raw M2") -> None:
    # stderr, not stdout: a GUI run redirects stdout away, and a hex dump of
    # the frame we failed to decode is the one thing worth keeping.
    print(f"[debug] {header} ({len(raw)} bytes):", file=sys.stderr, flush=True)
    for off in range(0, len(raw), 16):
        chunk = raw[off : off + 16]
        print(
            f"  {off:04x}  "
            + " ".join(f"{c:02x}" for c in chunk).ljust(47)
            + "  "
            + "".join(chr(c) if 32 <= c < 127 else "." for c in chunk),
            file=sys.stderr,
            flush=True,
        )


def _recv_exact(sock, n: int) -> bytearray:
    """Read exactly ``n`` bytes from a TCP socket or MAC-Winbox transport."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ModernProtocolError("peer closed during read")
        buf += chunk
    return buf
