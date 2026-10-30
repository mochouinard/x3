"""redp1 hashes the minimal big-endian retry counter, not a zero-padded word.

Winbox's bignum serializer drops a leading 0x00. About 1 in 256 validators
hit that on the first hash, and a fixed 32-byte pad then lands on a different
curve point. The short vector below is one of those; the full vector is not.
"""

import hashlib

from x3.proto.crypto import WCurve


def check(name, got, want):
    if got != want:
        raise SystemExit(f"FAIL {name}: got {got!r} want {want!r}")
    print(f"ok  {name}")


def padded_redp1(curve, data, parity):
    n = hashlib.sha256(data).digest()
    while True:
        x2 = hashlib.sha256(n).digest()
        pt = curve.lift_x(int.from_bytes(x2, "big"), parity)
        if pt == -1:
            n = (int.from_bytes(n, "big") + 1).to_bytes(32, "big")
        else:
            return pt


def u_of(curve, pt):
    u, parity = curve.to_montgomery(pt)
    return u.hex(), parity


curve = WCurve()

# sha256(005c) starts with 0x00, so the first retry input is 31 bytes.
short = bytes.fromhex("005c")
check(
    "leading-zero preimage",
    hashlib.sha256(short).digest()[:1],
    b"\x00",
)
got_u, got_par = u_of(curve, curve.redp1(short, 0))
check(
    "short form point",
    (got_u, got_par),
    ("0e6a3c114c5df545018464ce86cd37c9262a6a2875f0220376b7963ef44ed999", 0),
)
old_u, _ = u_of(curve, padded_redp1(curve, short, 0))
check("padded form disagrees", got_u == old_u, False)
neg_u, neg_par = u_of(curve, curve.redp1(short, 1))
check("parity 1 is the negation", (neg_u, neg_par), (got_u, 1))

# sha256(0000) has no leading zero; both encodings hash the same 32 bytes.
full = bytes.fromhex("0000")
check("full-width preimage", hashlib.sha256(full).digest()[:1] != b"\x00", True)
check(
    "full-width matches pad",
    u_of(curve, curve.redp1(full, 0)),
    u_of(curve, padded_redp1(curve, full, 0)),
)
