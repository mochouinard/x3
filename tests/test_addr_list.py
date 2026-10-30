"""Address lists on the wire: (address, mask) pairs, not two numbers.

No device and no pytest required:

    .venv/bin/python test_addr_list.py

IP ▸ Services "Available From" is a `multi` whose nested entries are `network`
(code 1) and `network6` (code 3). The netmask does not sit beside them as a
nested field — it lives under the address field's own ``maskid`` (2 and 4).
The renderer had no notion of that, so it emitted the mask as a bare integer
and the field read

    192.168.0.0, 65535, 10.10.10.0, 16777215

where Winbox shows 192.168.0.0/16, 10.10.10.0/24.

The router sends 255.255.0.0 as 0x0000FFFF, so the prefix length is counted from the
set bits rather than assuming a byte order.
"""

from x3.proto import message as m
from x3.proto.schema import Schema, mask_prefix_len

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


schema = Schema.load("7.21.1-151146055")
services = next(x for x in schema.menus if x.path == (68, 1) and x.kind == "map")
avail = services.fields[0x6]


def entry(addr: int, mask: int):
    msg = m.Message()
    msg.add_u32(1, addr)
    msg.add_u32(2, mask)
    return msg


# --- prefix lengths -------------------------------------------------------

check("255.255.0.0 is /16", mask_prefix_len(65535), 16)
check("255.255.255.0 is /24", mask_prefix_len(16777215), 24)
check("a full mask is /32", mask_prefix_len(0xFFFFFFFF), 32)
check("an empty mask is /0", mask_prefix_len(0), 0)
check("a non-contiguous mask is not a prefix", mask_prefix_len(0x0000FF7F), None)
check("nonsense is not a prefix", mask_prefix_len("x"), None)

# --- the field as the device sends it ------------------------------------

check("an address list reads as CIDR",
      avail.render([entry(0x0000A8C0, 65535), entry(0x000A0A0A, 16777215)]),
      "192.168.0.0/16, 10.10.10.0/24")

check("a single host keeps its bare address, the way Winbox shows it",
      avail.render([entry(0x0100A8C0, 0xFFFFFFFF)]), "192.168.0.1")

check("an empty list says nothing", avail.render([]), "")

# A mask that is not a prefix must not invent one.
check("an odd mask falls back to the bare address",
      avail.render([entry(0x0000A8C0, 0x0000FF7F)]), "192.168.0.0")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all address-list tests passed")
