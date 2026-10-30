"""``clockdate`` / ``clocktime`` / ``timezone`` / ``dateandtime`` on the wire.

No device and no pytest required:

    .venv/bin/python test_clock_fields.py

Four .jg types arrive as a bare u32 and mean four different things. System ▸
Clock declares three of them at once, and a capture of a live 7.23 device in
America/Toronto settles every unit::

    0x1f Date        clockdate   1785888000   Unix seconds at midnight
    0x1e Time        clocktime        50751   seconds since midnight, 14:05:51
    0x1b GMT Offset  timezone    4294952896   signed seconds, -14400 = -04:00
    0x17 DST Delta   timezone          3600   +01:00
    0x07 (undeclared)            1785938751   the two added: date + time

None of the three had a renderer, so Date read as 1785888000, Time as 50751 and
the GMT offset as 4294952896 -- and, worse, the *write* path sent a
``dateandtime`` as its display string, which the device wants as a number and
therefore ignored. Scheduler ▸ Start Date and Start Time are the same types.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QLineEdit

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.device import RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import DurationEdit, PropertyWindow

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
CLOCK = (24, 0)
clock = next(mn for mn in schema.menus if mn.path == CLOCK)

DATE, TIME, GMT, TZ, DST_DELTA = 0x1F, 0x1E, 0x1B, 0x12, 0x17
DST_START, TZ_NAME, TZ_AUTO = 0x18, 0x1A, 0x11

#: The live values, exactly as the device sent them.
LIVE_DATE, LIVE_TIME = 1785888000, 50751
LIVE_GMT, LIVE_NOW = 4294952896, 1785938751


# --- what the types mean ----------------------------------------------------

check("the three clock types are declared where the capture found them",
      [(clock.fields[c].name, clock.fields[c].jg_type)
       for c in (DATE, TIME, GMT)],
      [("Date", "clockdate"), ("Time", "clocktime"),
       ("GMT Offset", "timezone")])

check("a clockdate is a timestamp at midnight",
      clock.fields[DATE].render(LIVE_DATE), "aug/05/2026")
check("a clocktime is a time of day",
      clock.fields[TIME].render(LIVE_TIME), "14:05:51")
check("a timezone is signed seconds",
      clock.fields[GMT].render(LIVE_GMT), "-04:00")
check("...positive ones too", clock.fields[DST_DELTA].render(3600), "+01:00")
check("...and zero is not negative", clock.fields[TZ].render(0), "+00:00")
check("date plus time is the device's own clock",
      LIVE_DATE + LIVE_TIME, LIVE_NOW)
check("a dateandtime is the pair at once",
      clock.fields[DST_START].render(LIVE_NOW), "2026-08-05 14:05:51")


# --- and back onto the wire -------------------------------------------------

def encoded(code, text):
    return sch.encode_form_value(clock.fields[code], text)


check("a date round trips", encoded(DATE, "aug/05/2026"), (m.T_U32, LIVE_DATE))
check("...however it is typed", encoded(DATE, "2026-08-05"),
      (m.T_U32, LIVE_DATE))
check("a time round trips", encoded(TIME, "14:05:51"), (m.T_U32, LIVE_TIME))
check("...and a short one fills in the rest", encoded(TIME, "14:05"),
      (m.T_U32, 14 * 3600 + 5 * 60))
check("an offset round trips", encoded(GMT, "-04:00"), (m.T_U32, LIVE_GMT))
check("...and so does a positive one", encoded(GMT, "+05:30"),
      (m.T_U32, 5 * 3600 + 30 * 60))
check("a dateandtime goes back as a number, not as its own text",
      encoded(DST_START, "2026-08-05 14:05:51"), (m.T_U32, LIVE_NOW))
ok("...which is what it used to send",
   sch.encode_form_value(clock.fields[TZ_NAME], "America/Toronto")[0]
   == m.T_STRING)

for code, text in ((DATE, ""), (TIME, ""), (GMT, ""), (DST_START, "")):
    check(f"an empty {clock.fields[code].jg_type} is zero, not an error",
          encoded(code, text), (m.T_U32, 0))

check("a scaled dateandtime multiplies back",
      sch.parse_dateandtime("2026-08-05 14:05:51", scale=100),
      LIVE_NOW * 100)
check("a clocktime wraps rather than overflowing the day",
      sch.parse_clock_time("25:00:00"), 3600)


# --- the form ---------------------------------------------------------------

class StubClient:
    schema = schema

    def __init__(self, row):
        self._row = row

    def menu_for(self, path, type_value=None, values=None, **kw):
        return clock

    def alloc_from(self):
        return (0, 12)

    def get(self, path):
        return self._row

    def getobj(self, path, obj_id, **kw):
        return self._row

    def prefetch_enums(self, paths):
        pass

    def enum_map(self, path):
        return {}

    def enum_choices(self, fld):
        return sorted((fld.enum or {}).items())

    def interface_choices(self):
        return []


app = QApplication.instance() or QApplication(sys.argv)

msg = m.Message()
msg.add_u32(DATE, LIVE_DATE)
msg.add_u32(TIME, LIVE_TIME)
msg.add_u32(GMT, LIVE_GMT)
msg.add_u32(DST_DELTA, 0)
msg.add_u32(DST_START, LIVE_NOW)
msg.add_string(TZ_NAME, "America/Toronto")
msg.add_bool(TZ_AUTO, True)
row = RowObject(
    path=CLOCK, raw=msg, fields=schema.decode(CLOCK, msg),
    form_fields=schema.decode_form(CLOCK, msg),
    values_by_code={c: v for c, _t, v in msg},
)
win = PropertyWindow(
    "Clock", CLOCK, row, client=StubClient(row),
    edit_session=EditSession(EditMode.QUEUE, client=None),
    menu=clock, row_key="clock", full_row=True,
)
app.processEvents()


def find(label):
    for spec, editor in win._rows:
        if spec.get("name") == label:
            return spec, editor
    return None, None


_dspec, date_editor = find("Date")
ok("Date is on the form", date_editor is not None)
check("...showing the date, not the timestamp",
      date_editor.text() if isinstance(date_editor, QLineEdit) else None,
      "aug/05/2026")

_tspec, time_editor = find("Time")
ok("Time gets the hh:mm:ss box an interval gets",
   isinstance(time_editor, DurationEdit), f"{type(time_editor).__name__}")
if isinstance(time_editor, DurationEdit):
    check("...showing the time of day", time_editor.text(), "14:05:51")
    check("...and reporting the wire's own seconds", time_editor.value(),
          LIVE_TIME)

_gspec, gmt_editor = find("GMT Offset")
ok("GMT Offset is read-only, as declared", _gspec.get("always_locked"))
check("...and reads as an offset",
      gmt_editor.text() if hasattr(gmt_editor, "text") else None, "-04:00")

win._allow_close = True
win.close()


if failures:
    print(f"\nFAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nall clock-field tests passed")
