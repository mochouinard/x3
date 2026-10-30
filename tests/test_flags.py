"""The universal row flags: X (disabled), I (invalid), D (dynamic).

No device and no pytest required:

    .venv/bin/python test_flags.py

The flags column was built purely from schema-declared ``jg_type: flag``
fields. The .jg does not declare dynamic or invalid -- only per-menu letters
like S (slave) and d (deprecated) -- so a DHCP-client address rendered exactly
like a statically configured one, with nothing to say the router owns it and
that editing it cannot work.

The row data below is copied verbatim out of a session transcript: a real
``[20,1]`` GETALL reply carrying 192.168.88.1 (static, comment "defconf") and
192.168.87.104 (handed out by the DHCP client). The only difference between
the two rows on the wire is 0xFE0007.
"""

from x3.proto.schema import (
    FLAG_DISABLED_CODE,
    FLAG_DYNAMIC_CODE,
    FLAG_INVALID_CODE,
    Schema,
    format_flag_letters,
    is_disabled_flags,
    is_dynamic_flags,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


schema = Schema.load("7.21.1-151146055")
addresses = next(m for m in schema.menus if m.path == (20, 1))

# 192.168.88.1 — configured by hand.
STATIC = {0x0B: False, 0xFE0007: False, 0xFE0008: False, 0xFE000A: False,
          0x000001: 22587584}
# 192.168.87.104 — from the DHCP client.
DYNAMIC = {0x0B: False, 0xFE0007: True, 0xFE0008: False, 0xFE000A: False,
           0x000001: 1750575296}

check("a static address carries no flags", format_flag_letters(addresses, STATIC), "")
check("a dynamic address is marked D", format_flag_letters(addresses, DYNAMIC), "D")
check("...which is what the UI asks about", is_dynamic_flags("D"), True)
check("...and a static one is not", is_dynamic_flags(""), False)

# --- the universal flags stack, in Winbox's legend order ------------------
# The legend reads "X - disabled, I - invalid, D - dynamic", so the letters
# appear in that order however the row happens to arrive.

both = {**DYNAMIC, FLAG_DISABLED_CODE: True}
check("disabled and dynamic read XD", format_flag_letters(addresses, both), "XD")
check("...and disabled is still detected", is_disabled_flags("XD"), True)

allthree = {**DYNAMIC, FLAG_DISABLED_CODE: True, FLAG_INVALID_CODE: True}
check("all three read XID", format_flag_letters(addresses, allthree), "XID")

# --- a menu's own letters still follow the universal ones -----------------

ifaces = next(m for m in schema.menus if m.path == (20, 0))
iface_flags = {f.hint: f.code for f in ifaces.flag_fields() if f.hint}
if "R" in iface_flags:
    running = {iface_flags["R"]: True, FLAG_DYNAMIC_CODE: True}
    check("a dynamic running interface reads DR",
          format_flag_letters(ifaces, running), "DR")

# --- no menu, no schema: the universal flags still show -------------------

check("universal flags survive an unknown menu",
      format_flag_letters(None, DYNAMIC), "D")
check("...and an empty row says nothing", format_flag_letters(None, {}), "")

# --- a flag the device reports false must not appear ----------------------

check("false flags are omitted", format_flag_letters(addresses, STATIC), "")
check("missing flags are omitted too", format_flag_letters(addresses, {}), "")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all flag tests passed")
