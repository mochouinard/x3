"""``unit`` / ``bigunit`` suffixes, and ``netmask`` in either notation.

No device and no pytest required:

    .venv/bin/python test_unit_and_netmask.py

Two more node types the parser walked past, both settled against Winbox 3
rather than guessed at.

**``unit`` (45) and ``bigunit`` (58)** -- a Hotspot user's Limit Bytes, a
Simple Queue's Max Limit, a firewall Limit's Burst. Winbox takes ``5K``, ``5M``
and ``5G`` in the box, and shows the number back *plainly*: type 1048576 and it
stays 1048576. So this is an input shorthand and not a rendering, and
:meth:`Field.render` deliberately grows no branch -- what it already does is
right. Only :func:`encode_form_value` changes.

The multiplier is decimal, and the .jg settles that on its own: a firewall
rule's Burst is declared ``max:2000000000``, which is exactly 2G on this scale
and nothing round at all on the 1024 one.

**``netmask`` (9)** -- rendered as the raw u32 before this, so a DHCP network
mask read ``16777215``. Two things had to be pinned:

* *Which encoding.* The same as ``ipaddr``, and the .jg says so outright: a
  switch rule's Src. Address is a ``/``-separated tuple whose second half is
  ``{union,single:1,c:[{ipaddr,id:'u1c'},{netmask,id:'u1c'}]}`` -- one wire
  code declared both ways, which is the union offering the two notations for
  one value. That is also the whole argument for accepting both in the box.
* *Which of the two stored forms.* Most declarations carry the mask (three
  default to ``4294967295``, which is 255.255.255.255 and no prefix length),
  but the router's CLI takes 0-32 for a DHCP network. Both are read, and no
  guessing is needed: the domains are disjoint. The smallest non-zero mask is
  128 -- that is /1 -- so nothing in 1..32 can be one, and 0 means the same
  thing either way.
"""

import os
import sys

from x3.proto.schema import (
    Schema,
    encode_form_value,
    parse_netmask,
    parse_unit,
    parse_ipv4,
    render_netmask,
)
from x3.proto import message as m

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"FAIL {label}: {detail}")


SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "7.21.1-151146055")
if not os.path.isdir(SCHEMA_DIR):
    print(f"skipped: no plugin cache at {SCHEMA_DIR}")
    raise SystemExit(0)

schema = Schema.load(SCHEMA_DIR)


def a_field(jg_type, title=None):
    for mn in schema.menus:
        if title is not None and mn.title != title:
            continue
        for fld in mn.fields.values():
            if fld.jg_type == jg_type:
                return mn, fld
    raise AssertionError(f"no {jg_type} field")


# --- unit / bigunit ----------------------------------------------------------

check("a plain number is itself", parse_unit("1048576"), 1048576)
check("5K", parse_unit("5K"), 5_000)
check("5M", parse_unit("5M"), 5_000_000)
check("5G", parse_unit("5G"), 5_000_000_000)
check("lower case reads the same", parse_unit("5m"), parse_unit("5M"))
check("a space before the suffix is still a suffix", parse_unit("5 M"), 5_000_000)
check("a fraction scales", parse_unit("1.5M"), 1_500_000)
check("an empty box is zero, not an error", parse_unit(""), 0)
check("an int passes through", parse_unit(7), 7)

# The .jg's own number is the argument for decimal over binary.
burst = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.jg_type == "unit" and f.name == "Burst" and f.maximum
)
check("a firewall Burst's declared max is exactly 2G decimal",
      (burst.maximum, parse_unit("2G")), (2_000_000_000, 2_000_000_000))
ok("...which 2G binary is not", parse_unit("2G") != 2 ** 31)

hs_menu, limit = a_field("bigunit", "Users")
check("Hotspot's byte limit is a bigunit", limit.name.startswith("Limit Bytes"),
      True)
check("...and shows a number back plainly, as Winbox does",
      limit.render(1048576), "1048576")
check("...so a round trip through the box changes nothing",
      encode_form_value(limit, limit.render(1048576))[1], 1048576)
check("...while 5M in the same box is five million",
      encode_form_value(limit, "5M"), (m.T_U32, 5_000_000))
check("...and 5G needs the wide tag",
      encode_form_value(limit, "5G"), (m.T_U64, 5_000_000_000))

_m, u32 = a_field("unit")
check("a u32 unit encodes as one",
      encode_form_value(u32, "5K"), (m.T_U32, 5_000))


# --- netmask -----------------------------------------------------------------

check("dotted", parse_netmask("255.255.255.0"), parse_ipv4("255.255.255.0"))
check("...and slash-prefix are the same value", parse_netmask("/24"),
      parse_netmask("255.255.255.0"))
check("...as is a bare prefix", parse_netmask("24"), parse_netmask("/24"))
check("/0 is no mask at all", parse_netmask("/0"), 0)
check("/32 is every bit", parse_netmask("/32"), 0xFFFFFFFF)
check("/1 is 128.0.0.0", render_netmask(parse_netmask("/1")), "128.0.0.0")
check("an empty box is zero", parse_netmask(""), 0)

try:
    parse_netmask("/33")
    failures.append("a prefix over 32 should be refused")
    print("FAIL a prefix over 32 should be refused")
except ValueError:
    print("ok  a prefix over 32 is refused, not wrapped")

check("a stored mask reads as its dotted quad",
      render_netmask(parse_ipv4("255.255.0.0")), "255.255.0.0")
check("a stored prefix length reads as the same mask",
      render_netmask(24), "255.255.255.0")
check("...and 0 means the same in both", render_netmask(0), "0.0.0.0")
check("255.255.255.255, the value three declarations default to",
      render_netmask(4294967295), "255.255.255.255")

# Disjointness is the reason no guessing is involved, so it is worth pinning.
masks = {parse_netmask(f"/{n}") for n in range(33)}
ok("no contiguous mask is also a plausible prefix length",
   not (masks & set(range(1, 33))), f"{sorted(masks & set(range(1, 33)))}")

nm_menu, nm = a_field("netmask")
check("a netmask field renders through it",
      nm.render(parse_ipv4("255.255.255.0")), "255.255.255.0")
check("...and encodes as a bare u32, like the ipaddr it shares a code with",
      encode_form_value(nm, "/24"), (m.T_U32, parse_ipv4("255.255.255.0")))
check("...from either notation",
      encode_form_value(nm, "255.255.255.0"), encode_form_value(nm, "/24"))

# The switch rule's union is the evidence for that shared encoding. Check the
# .jg still declares it that way, so the reasoning above cannot quietly stop
# being true under a later the router.
raw = open(os.path.join(SCHEMA_DIR, "roteros.jg")).read()
ok("the .jg still declares one code as both ipaddr and netmask",
   "{type:'ipaddr',id:'u1c'},{type:'netmask',id:'u1c'}" in raw,
   "the switch-rule union that says the two share an encoding is gone")


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall good")
