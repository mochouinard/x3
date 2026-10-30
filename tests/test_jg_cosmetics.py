"""The .jg's presentation flags: order, precision, icons, caps, add defaults.

No device and no pytest required:

    .venv/bin/python test_jg_cosmetics.py

The small keys, from a key-by-key sweep of the .jg against the source. Several
turn out not to be cosmetic at all:

* **``sorted`` (61) / ``sortbyvalue`` (68)** are how a dropdown asks to be
  ordered. The .jg only asks where it wants something specific, so the default
  is the order the values came in -- the device's own for a live list. We were
  sorting *everything* alphabetically, which is what 54 fields ask for and the
  rest do not.

* **``trimzeros`` (11)** exists because a fixedpoint otherwise keeps the
  precision its scale implies: 1.50, not 1.5. We were trimming everywhere, so
  the flag's *absence* was what got ignored. Only fixedpoint -- a scaled
  interval with a postfix still reads "600s", which is why the flag lives on
  one type and not the other.

* **``idhigh`` (3)** is a synonym for ``highid``, and reading only one left
  three range fields with no high end at all.

* **``precise`` (2)** wants the byte count, not "2.4 MiB".

* **``iconon`` + ``icons`` (17)**, **``highlight`` (1)**, **``maxobjs`` (3)**,
  **``nofilter`` (2)**, **``priority`` (3)**, and ~34 add-time defaults
  (``deflen``, ``deflow``/``defhigh``, ``todaydef``, ``nowdef``, ``randomdef``,
  ``defuser``, ``defmask``) are the rest of the batch.

``lastcol`` (8) is parsed and deliberately unused: "put this column last" is
the obvious reading and it is wrong -- Interface declares it on Tx / Rx, which
Winbox shows in the middle of the default set (test_default_columns.py pins
that), so something else is meant.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema
from x3.ui.widgets.list_pane import ListPane

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


# --- dropdown order ---------------------------------------------------------

class OrderClient(DeviceClient):
    """A client whose one live list comes back in a deliberate order."""

    def __init__(self):
        super().__init__("127.0.0.1", "u", "p", readonly=True)
        self.schema = schema
        self.session = None

    def enum_map(self, path, **kw):
        # Device order: not alphabetical, not id-sorted.
        return {7: "zulu", 3: "alpha", 5: "mike"}


client = OrderClient()
plain = sch.Field(code=1, prefix="u", name="Plain", jg_type="enm",
                  enum_path=(20, 0))
alpha = sch.replace(plain, sort_alpha=True)
byval = sch.replace(plain, sort_by_value=True)
check("a live list keeps the device's order by default",
      [v for _k, v in client.enum_choices(plain)], ["zulu", "alpha", "mike"])
check("`sorted:1` asks for alphabetical",
      [v for _k, v in client.enum_choices(alpha)], ["alpha", "mike", "zulu"])
check("`sortbyvalue:1` asks for wire order",
      [k for k, _v in client.enum_choices(byval)], [3, 5, 7])

static = sch.Field(code=1, prefix="u", name="S", jg_type="enm",
                   enum={2: "two", 0: "zero", 1: "one"})
check("a static map keeps its declared order",
      [v for _k, v in client.enum_choices(static)], ["two", "zero", "one"])

wireless = schema.record_for((20, 0), 35, {0x0007D1: 12})
band = wireless.fields[0x00081A]
ok("wireless Band is one of the 68 that ask for wire order",
   band.sort_by_value)


# --- precision --------------------------------------------------------------

fixed = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.jg_type == "fixedpoint" and f.scale == 100 and not f.trim_zeros
)
check("a fixedpoint keeps the precision its scale implies",
      fixed.render(150), f"1.50{fixed.postfix or ''}")
trimmed = sch.replace(fixed, trim_zeros=True)
check("...and `trimzeros:1` drops the zeros",
      trimmed.render(150), f"1.5{fixed.postfix or ''}")
interval = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.jg_type == "interval" and f.scale and f.postfix
)
check("a scaled interval with a postfix is untouched by this",
      interval.render(600 * (interval.scale or 1)), f"600{interval.postfix}")

byte_field = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.jg_type == "bigbytes" and not f.precise
)
precise = sch.replace(byte_field, precise=True)
check("bytes round to a unit by default", byte_field.render(2_500_000),
      sch.format_bytes(2_500_000))
check("...and `precise:1` gives the count", precise.render(2_500_000),
      "2,500,000")

ranges = [
    f for mn in list(schema.menus) + list(schema.overlay_list)
    for f in mn.fields.values() if f.jg_type == "integerrange"
]
ok("every integerrange has its high end, `idhigh` or `highid`",
   ranges and all(f.highid is not None for f in ranges),
   f"{[(f.name, f.highid) for f in ranges]}")


# --- tabs, icons, caps, filters ---------------------------------------------

iface = schema.record_for((20, 0), 1)
check("a tab's `priority` puts it last", iface.tab_names()[-2:],
      ["Status", "Traffic"])
check("...and the .jg is what says so",
      sorted(iface.tab_priority), ["Status", "Traffic"])

fw = schema.record((20, 3))
check("a firewall list takes its row icon from the Action field",
      fw.icon_on, "Action")
ok("...with a sprite per action", len(fw.icon_map) > 5, f"{fw.icon_map}")
check("Interface marks rows by condition", schema.record((20, 0)).highlight_on,
      "notrunning")
ok("...and that condition is one it declares",
   "notrunning" in schema.record((20, 0)).conditions)

capped = [(mn.title, mn.max_objects) for mn in schema.menus if mn.max_objects]
ok("three lists cap what they will draw", len(capped) == 3, f"{capped}")
ok("...and each says so in words",
   all(mn.max_objects_message for mn in schema.menus if mn.max_objects))

nofilter = [
    (mn.title, f.name) for mn in schema.menus for f in mn.fields.values()
    if f.no_filter
]
check("two fields are kept out of the text filter", len(nofilter), 2)


# --- add-time defaults ------------------------------------------------------

v6 = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.jg_type == "network6" and f.default_mask == 64
)
check("an IPv6 address starts as a /64", v6.default_mask, 64)
ok("...on the code that carries the length", v6.maskid is not None)
ra = next(
    f for mn in schema.menus for f in mn.fields.values()
    if f.default_low is not None and f.default_high is not None
)
check("a range starts on its declared ends",
      (ra.default_low, ra.default_high), (200, 600))
kinds = {
    f.default_kind
    for mn in list(schema.menus) + list(schema.overlay_list)
    for f in mn.fields.values() if f.default_kind
}
check("the four computed defaults are all recognised", sorted(kinds),
      ["now", "random", "today", "user"])

app = QApplication.instance() or QApplication(sys.argv)


class Session:
    readonly = False

    class client:
        user = "admin"


pane = ListPane("Scheduler", path=(48, 3), edit_session=Session())
sched = schema.record((48, 3))
pane._menu = sched
date_field = next(
    f for f in sched.fields.values() if f.default_kind == "today"
)
today = pane._computed_default(date_field)
ok("a start date defaults to today", isinstance(today, int) and today > 0,
   f"{today}")
check("...as a timestamp at midnight", today % 86400, 0)
time_field = next(
    (f for f in sched.fields.values() if f.default_kind == "now"), None
)
if time_field is not None:
    now = pane._computed_default(time_field)
    ok("a start time defaults to now", 0 <= now < 86400, f"{now}")

mac_field = sch.Field(code=1, prefix="r", name="MAC Address",
                      jg_type="macaddr", default_kind="random")
mac = pane._computed_default(mac_field)
check("a random MAC is six bytes", len(mac), 6)
check("...locally administered and unicast", mac[0] & 0x03, 0x02)
user_field = sch.Field(code=1, prefix="s", name="User", jg_type="string",
                       default_kind="user")
check("a user field starts as whoever is logged in",
      pane._computed_default(user_field), "admin")
ok("a field with no computed default gets none",
   pane._computed_default(sch.Field(code=1, prefix="u", name="n",
                                    jg_type="number")) is None)
pane.shutdown()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall cosmetic-flag tests passed")
