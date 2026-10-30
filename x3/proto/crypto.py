"""Cryptographic primitives for the modern (firmware 6.43+) Winbox handshake.

The modern handshake is the router's variant of EC-SRP5 (an elliptic-curve
formulation of SRP) over a Montgomery curve whose underlying field is the
same prime used by Curve25519. After the password-authenticated key exchange
completes, both sides derive AES-128-CBC and HMAC-SHA1 keys from the shared
secret using a custom HKDF-style expansion.

This module is a faithful re-implementation of the maths described and
reverse-engineered by Margin Research. The class and function structure
mirrors theirs so the code is easy to cross-check against the public reference.

Portions of this file are derived from ``src/elliptic_curves.py`` and
``src/encryption.py`` in https://github.com/MarginResearch/mikrotik_authentication,
licensed under the Apache License, Version 2.0 (see ``LICENSES/Apache-2.0.txt``).
Changes from that code: type hints, credential bytes taken verbatim, the
minimal big-endian scalar encoding the router hashes (:func:`ros_scalar_bytes`),
and the session-key derivation folded into :func:`derive_session_keys`.
"""

from __future__ import annotations

import hashlib
import hmac

import ecdsa


# ---------------------------------------------------------------------------
# Field arithmetic helpers (Tonelli–Shanks square root, modular inverse)
# ---------------------------------------------------------------------------


def _egcd(a: int, b: int) -> tuple[int, int, int]:
    if a == 0:
        return (b, 0, 1)
    g, y, x = _egcd(b % a, a)
    return (g, x - (b // a) * y, y)


def _modinv(a: int, p: int) -> int:
    if a < 0:
        a = a % p
    g, x, _ = _egcd(a, p)
    if g != 1:
        raise ValueError("modular inverse does not exist")
    return x % p


def _legendre_symbol(a: int, p: int) -> int:
    l = pow(a, (p - 1) // 2, p)
    return -1 if l == p - 1 else l


def _prime_mod_sqrt(a: int, p: int) -> list[int]:
    a %= p
    if a == 0:
        return [0]
    if p == 2:
        return [a]
    if _legendre_symbol(a, p) != 1:
        return []
    if p % 4 == 3:
        x = pow(a, (p + 1) // 4, p)
        return [x, p - x]

    q, s = p - 1, 0
    while q % 2 == 0:
        s += 1
        q //= 2

    z = 1
    while _legendre_symbol(z, p) != -1:
        z += 1
    c = pow(z, q, p)

    x = pow(a, (q + 1) // 2, p)
    t = pow(a, q, p)
    m = s
    while t != 1:
        i, e = 0, 2
        for i in range(1, m):
            if pow(t, e, p) == 1:
                break
            e *= 2
        b = pow(c, 2 ** (m - i - 1), p)
        x = (x * b) % p
        t = (t * b * b) % p
        c = (b * b) % p
        m = i

    return [x, p - x]


def _as_cred_bytes(value: str | bytes) -> bytes:
    """A credential part as the bytes to hash: str is UTF-8, bytes pass through.

    Passing bytes through is the whole point -- see
    :meth:`WCurve.gen_password_validator_priv`. It lets the address book feed
    the password exactly as the router stored it, without a lossy UTF-8 round
    trip mangling any byte a single-byte codepage put above 0x7f.
    """
    return bytes(value) if isinstance(value, (bytes, bytearray)) else value.encode("utf-8")


# ---------------------------------------------------------------------------
# The router Weierstrass curve (the EC underlying Curve25519, parameterised
# differently). All point arithmetic is delegated to the `ecdsa` library;
# this class encapsulates the conversion between Montgomery and Weierstrass
# forms and the EC-SRP5-specific helpers.
# ---------------------------------------------------------------------------


class WCurve:
    # Field prime and group order from Curve25519.
    _P = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFED
    _R = 0x1000000000000000000000000000000014DEF9DEA2F79CD65812631A5CF5D3ED
    _MONT_A = 486662
    _COFACTOR = 8

    # Equivalent Weierstrass form (a, b derived from MONT_A in F_P).
    _A_W = 0x2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA984914A144
    _B_W = 0x7B425ED097B425ED097B425ED097B425ED097B425ED097B4260B5E9C7710C864

    def __init__(self) -> None:
        self._conversion_from_m = self._MONT_A * _modinv(3, self._P) % self._P
        self._conversion = (self._P - self._MONT_A * _modinv(3, self._P)) % self._P
        self._curve = ecdsa.ellipticcurve.CurveFp(self._P, self._A_W, self._B_W, self._COFACTOR)
        self._g = self.lift_x(9, 0)

    # --- conversions --------------------------------------------------

    def to_montgomery(self, pt) -> tuple[bytes, int]:
        x = (pt.x() + self._conversion) % self._P
        return int(x).to_bytes(32, "big"), pt.y() & 1

    def lift_x(self, x: int, parity: int):
        """Recover an affine point from its Montgomery x-coordinate + parity bit."""
        x = x % self._P
        y_squared = (x ** 3 + self._MONT_A * x ** 2 + x) % self._P
        x = (x + self._conversion_from_m) % self._P
        ys = _prime_mod_sqrt(y_squared, self._P)
        if not ys:
            return -1
        pt1 = ecdsa.ellipticcurve.PointJacobi(self._curve, x, ys[0], 1, self._R)
        pt2 = ecdsa.ellipticcurve.PointJacobi(self._curve, x, ys[1], 1, self._R)
        if pt1.y() & 1 == 1 and parity != 0:
            return pt1
        if pt2.y() & 1 == 1 and parity != 0:
            return pt2
        if pt1.y() & 1 == 0 and parity == 0:
            return pt1
        return pt2

    # --- EC-SRP5 specific ---------------------------------------------

    def gen_public_key(self, priv: bytes) -> tuple[bytes, int]:
        if len(priv) != 32:
            raise ValueError("private scalar must be 32 bytes")
        s = int.from_bytes(priv, "big")
        return self.to_montgomery(s * self._g)

    def redp1(self, x: bytes, parity: int):
        """Map a hash to a curve point (the router-flavoured REDP-1).

        The retry counter is a bignum, and the bytes hashed on each attempt
        are its minimal big-endian encoding. Winbox's serializer drops a
        leading 0x00, so a fixed 32-byte pad (what the public writeups do)
        selects a different point for about 1 in 256 validators — the client
        confirmation then fails against a router that used the short form.
        """
        n = int.from_bytes(hashlib.sha256(x).digest(), "big")
        while True:
            blob = n.to_bytes((n.bit_length() + 7) // 8, "big")
            x2 = hashlib.sha256(blob).digest()
            pt = self.lift_x(int.from_bytes(x2, "big"), parity)
            if pt == -1:
                n += 1
            else:
                return pt

    def gen_password_validator_priv(
        self, username: str | bytes, password: str | bytes, salt: bytes
    ) -> bytes:
        if len(salt) != 0x10:
            raise ValueError("salt must be 16 bytes")
        # The validator hashes the exact credential bytes the router hashed when
        # the password was set. A password saved in a single-byte codepage --
        # 0xe9 for 'é', common on a French-locale Winbox -- is not valid UTF-8,
        # so decoding it to str and re-encoding turns that one byte into the
        # three bytes of U+FFFD and derives a *different* validator than the
        # device stored: the login is then rejected with "invalid user name or
        # password (6)" against the right password. A caller holding the stored
        # bytes (the address book) passes them through untouched; a str that a
        # human typed is UTF-8, exactly as before.
        cred = _as_cred_bytes(username) + b":" + _as_cred_bytes(password)
        return hashlib.sha256(salt + hashlib.sha256(cred).digest()).digest()

    def check(self, pt) -> bool:
        left = (pt.y() ** 2) % self._P
        right = (pt.x() ** 3 + self._A_W * pt.x() + self._B_W) % self._P
        return left == right

    def finite_field_value(self, a: int) -> int:
        return a % self._R


# ---------------------------------------------------------------------------
# Key derivation after EC-SRP5 completes
# ---------------------------------------------------------------------------


_MAGIC_SEND = (
    b"On the client side, this is the send key; "
    b"on the server side, it is the receive key."
)
_MAGIC_RECV = (
    b"On the client side, this is the receive key; "
    b"on the server side, it is the send key."
)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def ros_scalar_bytes(b: bytes) -> bytes:
    """Minimal big-endian encoding of a curve scalar/coordinate, as the router
    hashes it.

    The router/Winbox serialize a bignum in its shortest big-endian form -- a
    leading 0x00 is dropped -- wherever they feed one to SHA256 locally. A
    fixed 32-byte zero pad (what the public writeups, and this code, used to do)
    diverges for about 1 in 256 values, so the client confirmation fails
    against a device that hashed the short form. This mirrors the retry-counter
    handling in :meth:`WCurve.redp1` and is applied to the shared secret ``z``
    in the confirmation hashes and the session-key seed (see modern.py /
    server.py).
    """
    n = int.from_bytes(b, "big")
    return n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")


def derive_session_keys(secret: bytes, server: bool = False):
    """From the shared SRP secret derive (send_aes, recv_aes, send_hmac, recv_hmac).

    Layout of each direction's key material (36 bytes):
        bytes  0..15  -- AES-128-CBC key
        bytes 16..35  -- HMAC-SHA1 key (20 bytes)
    """
    if server:
        tx_seed = secret + b"\x00" * 40 + _MAGIC_RECV + b"\xf2" * 40
        rx_seed = secret + b"\x00" * 40 + _MAGIC_SEND + b"\xf2" * 40
    else:
        tx_seed = secret + b"\x00" * 40 + _MAGIC_SEND + b"\xf2" * 40
        rx_seed = secret + b"\x00" * 40 + _MAGIC_RECV + b"\xf2" * 40

    tx = hashlib.sha1(tx_seed).digest()[:16]
    rx = hashlib.sha1(rx_seed).digest()[:16]

    tx_key = _hkdf(tx)
    rx_key = _hkdf(rx)

    return (
        tx_key[:0x10],   # send_aes
        rx_key[:0x10],   # recv_aes
        tx_key[0x10:],   # send_hmac
        rx_key[0x10:],   # recv_hmac
    )


def _hkdf(message: bytes) -> bytes:
    """Custom KDF used by the router — HMAC-SHA1 driven, expanded to 36 bytes."""
    h1 = hmac.new(b"\x00" * 0x40, message, hashlib.sha1).digest()
    out = b""
    h2 = b""
    for i in range(2):
        h2 = hmac.new(h1, h2 + bytes([i + 1]), hashlib.sha1).digest()
        out += h2
    return out[:0x24]
