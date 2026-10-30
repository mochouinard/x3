"""Winbox session — TCP transport plus high-level operations.

Covers the *legacy* (pre-firmware 6.43) plain-text variant of the protocol.
Two auth states are supported:

  - **No auth**: works only against very old, unpatched the router (prior to
    the 6.42.1 fix for CVE-2018-14847). Useful for understanding the wire
    format and for the rare lab device.
  - **Legacy authenticated** (`login()`): MD5 challenge/response over a
    plain-text M2 session. Pre-6.43 production devices.

The 6.43+ variant uses an SRP-style handshake and AES-encrypted session;
that lives in `modern.py`.
"""

from __future__ import annotations

import hashlib
import socket
from contextlib import contextmanager

from . import message as m
from .framing import encode_frame, read_frame


class WinboxError(Exception):
    pass


class WinboxSession:
    """Plain TCP session to a Winbox listener (port 8291, legacy variant)."""

    def __init__(self, host: str, port: int = 8291, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_request_id = 1
        self._session_id: int = 0

    # --- connection lifecycle ------------------------------------------

    def connect(self) -> None:
        if self._sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "WinboxSession":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- raw send / receive --------------------------------------------

    def send(self, msg: m.Message) -> None:
        if self._sock is None:
            raise WinboxError("session not connected")
        self._sock.sendall(encode_frame(msg.to_bytes()))

    def recv(self) -> m.Message:
        if self._sock is None:
            raise WinboxError("session not connected")
        return m.Message.from_bytes(read_frame(self._sock))

    def request(self, msg: m.Message) -> m.Message:
        self.send(msg)
        return self.recv()

    def next_request_id(self) -> int:
        rid = self._next_request_id
        self._next_request_id += 1
        return rid

    # --- legacy authenticated login (MD5 challenge/response) -----------

    def login(self, username: str, password: str) -> int:
        """Pre-6.43 login flow. Returns the post-login session id.

        Server quirk reproduced here: occasionally the salt response is
        spread across two M2 messages; we receive a second one if the
        first didn't contain a 16-byte salt.
        """
        # Step 1: ask for the salt.
        challenge = (
            m.Message()
            .set_to(13, 4)
            .set_command(4)
            .set_request_id(self.next_request_id())
            .set_session_id(self._session_id)
            .set_reply_expected(True)
        )
        reply = self.request(challenge)
        if reply.has_error():
            raise WinboxError(f"login (challenge) failed: {reply.error_string()}")

        salt = reply.get_raw(0x000009)
        if salt is None or len(salt) != 16:
            reply = self.recv()
            if reply.has_error():
                raise WinboxError(
                    f"login (challenge follow-up) failed: {reply.error_string()}"
                )
            salt = reply.get_raw(0x000009)
            if salt is None or len(salt) != 16:
                raise WinboxError("login: server did not return a 16-byte salt")

        # Step 2: send hashed response.
        h = hashlib.md5(b"\x00" + password.encode("utf-8") + salt).digest()
        hashed = b"\x00" + h

        login_attempt = (
            m.Message()
            .set_to(13, 4)
            .set_command(1)
            .set_request_id(self.next_request_id())
            .set_session_id(self._session_id)
            .set_reply_expected(True)
            .add_string(0x000001, username)
            .add_raw(0x000009, salt)
            .add_raw(0x00000A, hashed)
        )
        result = self.request(login_attempt)
        if result.has_error():
            raise WinboxError(f"login failed: {result.error_string()}")

        sid = result.session_id()
        if sid is None:
            raise WinboxError("login: server did not return a session id")
        self._session_id = sid
        return sid

    # --- file fetch (mproxy 2,2) ---------------------------------------

    def fetch_file(self, path: str) -> bytes:
        """Download `path` from the device using the mproxy file handler.

        On legacy the router this works without authentication, which is what we
        rely on for the initial plugin-download command.
        """
        open_req = (
            m.Message()
            .set_to(2, 2)
            .set_command(7)
            .set_request_id(self.next_request_id())
            .set_reply_expected(True)
            .add_string(0x000001, path)
        )
        reply = self.request(open_req)
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

        read_req = (
            m.Message()
            .set_to(2, 2)
            .set_command(4)
            .set_request_id(self.next_request_id())
            .set_reply_expected(True)
            .set_session_id(session_id)
            .add_u32(0x000002, size)
        )
        data_reply = self.request(read_req)
        if data_reply.has_error():
            raise WinboxError(f"read {path!r} failed: {data_reply.error_string()}")

        body = data_reply.get_raw(0x000003)
        if body is None:
            raise WinboxError(f"read {path!r}: server returned no payload")
        return body


@contextmanager
def open_session(host: str, port: int = 8291, timeout: float = 10.0):
    sess = WinboxSession(host, port, timeout)
    sess.connect()
    try:
        yield sess
    finally:
        sess.close()
