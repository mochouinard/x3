"""An ``addr`` field is a small record, and Route's columns are made of them.

No device and no pytest required:

    .venv/bin/python test_addr_record.py

IP ▸ Routes showed blank Dst. Address and Gateway columns on every firmware we
have screenshots of. The values were on the wire all along -- read live off a
7.25 router -- as nested messages whose every code sits above 0xFE0000:

    {0xfeff25 prefix 24, 0xfeff1f kind 1, 0xfeff20 v4}   10.18.50.0/24
    {0xfeff22 iface 9, 0xfeff1f kind 9}                  bridge

and the nested-message summary skipped that whole range as plumbing.
"""

import os
import sys

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, DeviceError

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def addr(**kw) -> m.Message:
    x = m.Message()
    for code, key in ((sch.ADDR_IFACE, "iface"), (sch.ADDR_PREFIX, "prefix"),
                      (sch.ADDR_KIND, "kind"), (sch.ADDR_INNER_V4, "v4")):
        if key in kw:
            x.add_u32(code, kw[key])
    if "v6" in kw:
        x.add_typed(sch.ADDR_INNER_V6, 0x18, kw["v6"])
    return x


# The values below are the ones a 7.25 router sent for its own routes.
NAMES = {4: "ether1", 9: "bridge"}
check("a destination is address and prefix",
      sch.format_addr(addr(prefix=24, kind=1, v4=3281418), NAMES), "10.18.50.0/24")
check("...the default route included",
      sch.format_addr(addr(prefix=0, kind=1, v4=0), NAMES), "0.0.0.0/0")
check("a gateway is a bare address",
      sch.format_addr(addr(kind=1, v4=24182987), NAMES), "203.0.113.1")
check("...scoped to an interface when it carries one",
      sch.format_addr(addr(iface=4, kind=1, v4=24182987), NAMES),
      "203.0.113.1%ether1")
check("kind 9 is the interface alone",
      sch.format_addr(addr(iface=9, kind=9), NAMES), "bridge")
check("...and one we cannot name reads as the router's *id",
      sch.format_addr(addr(iface=0x1A, kind=9), NAMES), "*1A")
check("IPv6 puts the zone before the prefix",
      sch.format_addr(addr(iface=9, prefix=64, kind=2,
                           v6=bytes.fromhex("fe80" + "00" * 14)), NAMES),
      "fe80::%bridge/64")

# Without a kind it is still low byte first. Read live off a 7.23.3 router: a
# disabled route, a routing rule, a Netwatch host, an NTP server and a
# WireGuard endpoint all come without one. Taken as network order, a disabled
# route's 10.99.1.0/24 via 192.168.1.3 read 0.1.99.10/24 via 3.1.168.192.
check("a disabled route's destination has no kind and is low byte first",
      sch.format_addr(addr(prefix=24, v4=0x0001630A)), "10.99.1.0/24")
check("...and so is its gateway",
      sch.format_addr(addr(v4=0x0301A8C0)), "192.168.1.3")
check("...scoped or not",
      sch.format_addr(addr(iface=4, v4=0x0301A8C0), NAMES), "192.168.1.3%ether1")

# --- a bare ip6addr ----------------------------------------------------------
# Traceroute's Host arrives as one, an IPv4 hop IPv4-mapped (its IPv4 low byte
# first, measured with a trace to the router's own address); it rendered as 32
# hex digits, as did every ip6addr in the schema.
v6 = sch.Field(code=1, prefix="a", name="Host", jg_type="ip6addr")
check("an IPv4-mapped hop reads as its IPv4, low byte first",
      v6.render(bytes.fromhex("00000000000000000000ffff01dba8c0")),
      "192.168.219.1")
check("...and an IPv6 one in the usual notation",
      v6.render(bytes.fromhex("fe800000000000000000000000000001")), "fe80::1")

# --- through the schema: what the Route list shows --------------------------

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped the rest: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(1 if failures else 0)
schema = sch.Schema.load(SCHEMA_DIR)
ROUTE = (44, 21)

row = m.Message()
row.add_message(0x105, addr(prefix=24, kind=1, v4=3281418))
row.add_message(0x10D, addr(iface=9, kind=9))
decoded = {name: text for _c, name, text, _ok in schema.decode(ROUTE, row)}
check("the Route list fills Dst. Address", decoded.get("Dst. Address"),
      "10.18.50.0/24")

asked: list[tuple] = []


def resolver(path):
    asked.append(tuple(path))
    return NAMES if tuple(path) == sch.ADDR_IFACE_PATH else {}


menu =next(x for x in schema.menus if x.path == ROUTE and x.kind == "map")
gateway = menu.fields[0x10D]
check("...and names the gateway's interface off (20, 0)",
      sch._render(gateway, addr(iface=9, kind=9), resolver), "bridge")
asked.clear()
sch._render(gateway, addr(kind=1, v4=24182987), resolver)
check("...asking nothing for a gateway with no interface in it", asked, [])

# --- a disabled route's other columns -----------------------------------------
# Contribution says how a route takes part, and a disabled one takes none: the
# router sends all ones, which read 4294967295 where Winbox shows nothing.
contribution = sch.Field(code=0x22, prefix="u", name="Contribution", jg_type="enm",
                         enum={0: "filtered", 1: "unreachable", 4: "active"})  # as 7.23
check("a disabled route's Contribution is blank, not 4294967295",
      sch._render(contribution, 0xFFFFFFFF, resolver), "")
check("...while an active one still reads its word",
      sch._render(contribution, 4, resolver), "active")

# Immediate Gateway names a row of the next-hop table, which has no name of
# its own -- only the hop's address -- so it read "#539501504".
HOPS = (44, 16)
hop = m.Message().add_u32(0xFE0001, 539501504)
hop.add_message(0x10D, addr(kind=1, v4=0x0202000A))
client = DeviceClient("192.0.2.1", "admin", "", schema_dir=SCHEMA_DIR)
client.schema = schema
client._enum_cache[sch.ADDR_IFACE_PATH] = NAMES
hops = client._enum_from_rows(HOPS, [client._row_from_msg(HOPS, hop)])
check("a next hop is named by its address, not its id", hops, {539501504: "10.0.2.2"})

# --- and nothing is written in the wrong shape ------------------------------

try:
    client._encode(ROUTE, 0x10D, "203.0.113.1")
except DeviceError as e:
    check("editing a record's addr is refused, not sent byte-swapped",
          "not supported" in str(e), True)
else:
    check("editing a record's addr is refused, not sent byte-swapped", False, True)
check("...while a field beside it still encodes",
      client._encode(ROUTE, 0x10B, 5)[1], 5)

# --- a tool's target: byte order follows the verb family ---------------------
# Ping and Traceroute start with the global 0xFE000F and take the address in
# network order; Bandwidth Test and Speed Test start with their handler's own
# verb and take it low byte first -- Winbox sends 127.0.0.1 as 16777343. Sent
# the other way, a bandwidth test to 127.0.0.1 went to 1.0.0.127.


def started_with(title):
    sent = []

    def fake_request(path, command, *, from_path=None, extra=None):
        msg = m.Message()
        extra(msg)
        sent.append(msg)
        return m.Message().add_u32(0xFE0001, 1)

    client._request = fake_request
    menu = next(x for x in schema.menus if x.title == title and x.request_fields)
    code = next(c for c, f in menu.request_fields.items() if f.jg_type == "addr")
    client.start_query(menu, {code: "127.0.0.1"})
    inner = next(v for c, _t, v in sent[0] if c == code)
    return inner.get_u32(sch.ADDR_INNER_V4)


check("Ping's target goes in network order", started_with("Ping"), 0x7F000001)
check("Bandwidth Test's goes low byte first, as Winbox sends it",
      started_with("Bandwidth Test"), 16777343)

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all addr-record tests passed")
