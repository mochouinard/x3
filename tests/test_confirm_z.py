"""The confirmation hashes the shared secret z in its minimal big-endian form.

Winbox's bignum serializer drops a leading 0x00, so about 1 in 256 handshakes
(those whose z starts with 0x00) diverge from a fixed 32-byte pad and the client
confirmation is rejected. This mirrors the redp1 counter fix, one level up, and
is applied wherever z is hashed (modern.py).
"""

import hashlib

from x3.proto.crypto import ros_scalar_bytes, sha256


def check(name, got, want):
    if got != want:
        raise SystemExit(f"FAIL {name}: got {got!r} want {want!r}")
    print(f"ok  {name}")


# minimal encoding drops every leading zero byte
check("one leading zero", ros_scalar_bytes(bytes.fromhex("00" + "11" * 31)),
      bytes.fromhex("11" * 31))
check("two leading zeros", ros_scalar_bytes(bytes.fromhex("0000" + "22" * 30)),
      bytes.fromhex("22" * 30))
check("no leading zero stays 32B", len(ros_scalar_bytes(bytes.fromhex("3b" + "00" * 31))), 32)
check("all-zero collapses to one byte", ros_scalar_bytes(b"\x00" * 32), b"\x00")

# a z with a leading zero: the confirmation over the minimal form differs from
# the padded form -- which is exactly why the padded code failed against a real
# router that used the short form.
j = sha256(b"context")
z_pad = bytes.fromhex("00" + "ab" * 31)   # 32 bytes, leading zero
z_min = ros_scalar_bytes(z_pad)           # 31 bytes
check("z_min is 31 bytes", len(z_min), 31)
check("confirmation diverges for leading-zero z",
      sha256(j + z_min) == sha256(j + z_pad), False)

# a z with no leading zero: both encodings are identical, confirmation matches
z_full = bytes.fromhex("7f" + "cd" * 31)
check("no divergence for full-width z",
      ros_scalar_bytes(z_full) == z_full and sha256(j + ros_scalar_bytes(z_full)) == sha256(j + z_full),
      True)

print("all confirmation-z checks passed")
