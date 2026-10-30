"""A password saved in a single-byte codepage still logs in.

The bug: Winbox's address book stores a password as the exact bytes the user
typed. A password with a byte above 0x7f -- 0xe9 for 'é' on a French-locale
Winbox, the ordinary case here -- is not valid UTF-8. x3ctl read it with
``get_string`` (UTF-8, ``errors="replace"``), turning 0xe9 into U+FFFD, then
hashed *that* into the EC-SRP5 validator. The device had hashed the real byte
when the password was set, so the two never matched and the router answered
stage 4 with "invalid user name or password (6)" -- against the right
password. Winbox 3 feeds the stored bytes untouched and logs in fine, which is
why the same address-book entry worked there and failed here.

The fix threads the password's *stored bytes* from the address book to the SRP
validator, so x3ctl hashes exactly what the device did. A str a human
typed is still UTF-8, so nothing changes for an ASCII password (which is why
most of the fleet, and the switch on the same host, always worked).

No device and no real credential:

    .venv/bin/python -m pytest test_cred_bytes.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from x3.proto import cdb, device, fleet
from x3.proto import message as m
from x3.proto.crypto import WCurve, sha256

# A password whose only non-ASCII character is 'é' as a single-byte codepage
# stores it: 0xe9 (CP1252 / Latin-1). This is a synthetic value, not anyone's
# real password.
RAW_PW = b"caf\xe9"                       # what the address book stores
SALT = bytes(range(16))                   # any 16 bytes; the device picks it live

# What the OLD lossy read produced: UTF-8/replace turns the lone 0xe9 into
# U+FFFD, whose UTF-8 is three bytes. This is the value x3ctl used to hash.
MANGLED_STR = RAW_PW.decode("utf-8", errors="replace")
assert MANGLED_STR == "caf�"
assert MANGLED_STR.encode("utf-8") == b"caf\xef\xbf\xbd" != RAW_PW


# --- the validator hashes the exact bytes -------------------------------------

def test_validator_bytes_match_what_the_device_stored():
    """Passing the stored bytes reproduces the device's own validator."""
    curve = WCurve()
    got = curve.gen_password_validator_priv("admin", RAW_PW, SALT)
    # The device set its verifier from SHA256(salt || SHA256("admin:" + bytes)).
    expected = sha256(SALT + sha256(b"admin:" + RAW_PW))
    assert got == expected


def test_validator_bytes_differ_from_the_old_mangled_string():
    """The lossy read derived a *different* validator -- this is the bug."""
    curve = WCurve()
    correct = curve.gen_password_validator_priv("admin", RAW_PW, SALT)
    mangled = curve.gen_password_validator_priv("admin", MANGLED_STR, SALT)
    assert correct != mangled


def test_ascii_password_unchanged_str_or_bytes():
    """An ASCII password hashes the same either way -- the working fleet is
    untouched, and a typed str keeps its old behaviour."""
    curve = WCurve()
    as_str = curve.gen_password_validator_priv("admin", "plainpw", SALT)
    as_bytes = curve.gen_password_validator_priv("admin", b"plainpw", SALT)
    assert as_str == as_bytes
    # And it equals the pre-fix formula, so no regression for these.
    assert as_str == sha256(SALT + sha256(b"admin:plainpw"))


# --- the address book hands over the stored bytes -----------------------------

def _entry(host: str, login: str, password: bytes) -> cdb.CdbEntry:
    msg = m.Message()
    msg.add_string(cdb.F_HOST, host)
    msg.add_string(cdb.F_LOGIN, login)
    msg.add_string(cdb.F_PASSWORD, password)   # add_string keeps bytes verbatim
    return cdb.CdbEntry(msg)


def test_password_bytes_preserves_what_password_mangles():
    entry = _entry("10.0.0.1", "admin", RAW_PW)
    assert entry.password_bytes == RAW_PW          # exact, for the login
    assert entry.password == MANGLED_STR           # lossy, for display only


def test_password_bytes_none_without_field():
    msg = m.Message()
    msg.add_string(cdb.F_HOST, "10.0.0.1")
    assert cdb.CdbEntry(msg).password_bytes is None


# --- the exact bytes ride all the way to the target ---------------------------

def test_load_targets_carries_password_bytes():
    book = cdb.CdbFile([_entry("10.0.0.1", "admin", RAW_PW)])
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "Addresses.cdb"
        path.write_bytes(book.to_bytes())
        targets, _skipped = fleet.load_targets(str(path))
    assert len(targets) == 1
    t = targets[0]
    assert t.password_bytes == RAW_PW              # the exact bytes survive
    assert t.password == MANGLED_STR               # str view is still lossy


# --- the client logs in with the bytes, not the mangled str -------------------

class _StopAfterAuth(Exception):
    """Cuts connect() off the instant authenticate() has been called."""


class _FakeSession:
    """Stands in for ModernSession: records what authenticate() was handed."""

    seen: tuple | None = None

    def __init__(self, *a, **k):
        pass

    def connect(self, *a, **k):
        pass

    def authenticate(self, username, password):
        _FakeSession.seen = (username, password)
        raise _StopAfterAuth

    def close(self):
        pass


def _connect_and_capture(password, *, password_bytes):
    """Run connect() far enough to see what authenticate() was handed."""
    real = device.ModernSession
    device.ModernSession = _FakeSession
    _FakeSession.seen = None
    try:
        client = device.DeviceClient(
            "10.0.0.1", "admin", password,
            password_bytes=password_bytes, readonly=True,
        )
        try:
            client.connect(need_schema=False)
        except _StopAfterAuth:
            pass
        else:
            raise AssertionError("authenticate() was never reached")
    finally:
        device.ModernSession = real
    return _FakeSession.seen


def test_client_authenticates_with_bytes_when_it_has_them():
    assert _connect_and_capture(MANGLED_STR, password_bytes=RAW_PW) == (
        "admin", RAW_PW
    )


def test_client_falls_back_to_str_without_bytes():
    """A typed password (no stored bytes) still authenticates as a str."""
    assert _connect_and_capture("plainpw", password_bytes=None) == (
        "admin", "plainpw"
    )


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
