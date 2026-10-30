"""Chain names, string-valued enums, and the full column set.

No device and no pytest required:

    .venv/bin/python test_columns_chain.py

Three faults that showed up adding a firewall filter rule:

* **Chain read "1".** The chain table sends only ``{id, 0xFE0010: b'input'}``
  and the label scan skipped 0xFE0010 as a system code, so every chain came
  back as its id.
* **Chain was written as ``b'1'``.** Chain is an ``s``-prefixed *string* field
  whose enum_path only supplies suggestions; sending the row id put the rule in
  a chain literally named "1".
* **Only 8 columns existed.** Lists decoded just ``preferred_columns``, so
  Dst. Port could never be switched on — the data never got that far.
"""

from x3.proto import message as m
from x3.proto.schema import (
    Schema,
    all_columns,
    enum_map_from_objects,
    preferred_columns,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


schema = Schema.load("7.21.1-151146055")
rules = next(x for x in schema.menus if x.path == (20, 3) and x.kind == "map")

# --- the chain table names its rows on 0xFE0010 --------------------------


def chain_row(oid: int, name: str):
    msg = m.Message()
    msg.add_u32(0xFE0001, oid)
    msg.add_string(0xFE0010, name)
    return msg


rows = [chain_row(1, "input"), chain_row(2, "forward"), chain_row(3, "output")]
check("chains are named, not numbered",
      enum_map_from_objects(None, rows),
      {1: "input", 2: "forward", 3: "output"})

# A table that names nothing still falls back rather than crashing.
bare = m.Message()
bare.add_u32(0xFE0001, 5)
check("an unnamed row keeps its id", enum_map_from_objects(None, [bare]), {5: "#5"})


# --- a string field sends the name, not the row id -----------------------


class StubClient:
    """Just enough DeviceClient to exercise enum_choices."""

    def __init__(self):
        from x3.proto.device import DeviceClient

        self.enum_choices = DeviceClient.enum_choices.__get__(self)

    def enum_map(self, path):
        return {1: "input", 2: "forward", 3: "output"}


client = StubClient()
chain_fld = next(f for f in rules.fields.values() if f.name == "Chain")
check("Chain is a string field", (chain_fld.prefix, chain_fld.jg_type), ("s", "string"))
check("...so its options carry chain names as the wire value",
      client.enum_choices(chain_fld),
      [("forward", "forward"), ("input", "input"), ("output", "output")])

action_fld = next(f for f in rules.fields.values() if f.name == "Action")
choices = client.enum_choices(action_fld)
check("a real enum still sends its number", choices[0], (0, "accept"))

# --- every column is available, the good ones first ----------------------

pref = [f.name for f in preferred_columns(rules)]
every = [f.name for f in all_columns(rules)]

check("the default columns are unchanged", every[: len(pref)], pref)
ok("...and the rest are there to switch on", len(every) > len(pref))
# Comment is deliberately absent: it rides on the standard 0xFE0009 and the
# pane renders it as its own column, not as a schema field.
for want in ("Dst. Port", "Src. Port", "In. Interface", "Connection State"):
    ok(f"{want!r} can be added as a column", want in every)

ok("no duplicates", len(every) == len(set(f.code for f in all_columns(rules))))
ok("nothing unnamed leaks in",
   all(n and not n.startswith("<0x") for n in every))

# --- ordered tables carry their rule number ------------------------------
#
# Firewall rules are evaluated top-down, so the list is meaningless without
# its order. Which tables those are is the .jg's own ``ordered:1`` (see
# test_default_columns.py); these four are the fallback list, for a caller
# holding a path and no menu.

from x3.proto.schema import INDEX_LABEL, is_ordered_path

check("filter rules are ordered", is_ordered_path((20, 3)), True)
check("so is NAT", is_ordered_path((20, 5)), True)
check("addresses are not", is_ordered_path((20, 1)), False)
check("nor is nothing at all", is_ordered_path(None), False)
check("the index column is Winbox's #", INDEX_LABEL, "#")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all column/chain tests passed")
