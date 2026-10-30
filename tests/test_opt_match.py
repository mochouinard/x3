"""Optional firewall matches — the ``opt -> not -> value`` chain.

No device and no pytest required:

    .venv/bin/python test_opt_match.py

Firewall matches are optional, which the .jg models as three nested fields::

    opt   0x1a2  'Src. Address'   <- the "+" in Winbox: is this match set?
     └─ not   200                 <- the "!" negate toggle
         └─ network6  50          <- the address itself

The device does not send them nested. It flattens all three into sibling codes
on one row, so the opt code carries only a bare True. Rendering that field on
its own is what made ``defconf: accept to local loopback (for CAPsMAN)`` show
``True`` for Src. and Dst. Address where Winbox shows 127.0.0.1.

The row below is copied out of a session transcript: a real ``[20,3]`` GETALL.
"""

from x3.proto.schema import Schema, _render, opt_chain

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


schema = Schema.load("7.21.1-151146055")
rules = next(m for m in schema.menus if m.path == (20, 3) and m.kind == "map")

SRC, DST, IN_IF = 0x1A2, 0x1A3, 0x190
LOOPBACK = 16777343  # 127.0.0.1, first dotted octet in the low byte

# defconf: accept to local loopback (for CAPsMAN)
ROW = {
    SRC: True, DST: True, IN_IF: True,
    0xC8: False,        # Src. Address not-negated
    0x19: False,        # In. Interface not-negated
    0x32: LOOPBACK, 0x33: LOOPBACK,   # Src. address from / to
    0x34: LOOPBACK, 0x35: LOOPBACK,   # Dst. address from / to
    0x01: 1,            # In. Interface -> interface id 1
    0xFE0001: 4,
}

INTERFACES = {1: "lo0", 2: "ether1"}


def resolve(path):
    return INTERFACES if tuple(path) == (20, 0) else {}


def render(code, row=None):
    row = ROW if row is None else row
    fld = rules.fields[code]
    return _render(fld, row.get(code), resolve, None, row)


# --- the rule as Winbox shows it -----------------------------------------

check("Src. Address follows the chain to the address", render(SRC), "127.0.0.1")
check("Dst. Address too", render(DST), "127.0.0.1")
check("In. Interface resolves the id to a name", render(IN_IF), "lo0")

# --- an unset match says nothing, not "false" ----------------------------

bare = {SRC: False}
check("an unset match renders empty", render(SRC, bare), "")
check("...and a missing one too", render(SRC, {}), "")

# --- negation ------------------------------------------------------------

negated = {**ROW, 0xC8: True}
check("a negated match keeps Winbox's !", render(SRC, negated), "!127.0.0.1")

# --- an address range shows both ends ------------------------------------

span = {**ROW, 0x32: 16777343, 0x33: 33554559}  # 127.0.0.1 - 127.0.0.2
check("a from/to pair renders as a range",
      render(SRC, span), "127.0.0.1-127.0.0.2")

# --- set, but the value did not come with it -----------------------------

check("a match set with no value says so, rather than 'true'",
      render(SRC, {SRC: True}), "set")

# --- the chain itself ----------------------------------------------------

neg, inner = opt_chain(rules.fields[SRC])
check("the negate toggle is found", neg.code, 0xC8)
check("...and the value field under it", inner.code, 0x32)
check("...which knows its paired end", inner.maskid, 0x33)

neg, inner = opt_chain(rules.fields[IN_IF])
check("the interface chain resolves too", (neg.code, inner.code), (0x19, 0x01))

# --- an address range that is one subnet reads as that subnet ----------------
# The router keeps a match address as low/high; 192.168.88.0-192.168.88.255 is
# what the CLI prints and Winbox shows as 192.168.88.0/24.

def ip(a, b, c, d):
    return a | b << 8 | c << 16 | d << 24     # first octet in the low byte


net = {**ROW, 0x32: ip(192, 168, 88, 0), 0x33: ip(192, 168, 88, 255)}
check("a range that is exactly a subnet shows as that subnet",
      render(SRC, net), "192.168.88.0/24")
odd = {**ROW, 0x32: ip(10, 0, 0, 1), 0x33: ip(10, 0, 0, 9)}
check("...and any other range as its two ends", render(SRC, odd), "10.0.0.1-10.0.0.9")

from x3.proto.schema import parse_ipv4_range  # noqa: E402

check("a typed subnet is the same range on the wire",
      parse_ipv4_range("192.168.88.0/24"), ("192.168.88.0", "192.168.88.255"))

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all opt-match tests passed")
