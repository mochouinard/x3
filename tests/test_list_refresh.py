"""Live counters in list columns: the read mask, and how often it is read.

No device and no pytest required:

    .venv/bin/python test_list_refresh.py

Two separate things kept the Interface list's Tx/Rx columns blank, and both are
pinned here:

* **The mask.** A handler does not volunteer its counters. Winbox 3.42 opening
  Interfaces sends GETALL with ``0xFE000C = 262159`` (0x4000F), which is the
  menu's own ``STD_MASK_BASE | refreshfilter`` -- see REQ#12 in
  captured-winbox-session.log. Reading with no mask at all, which is what this
  client did, returns the rows without their rates. The same number is 0x7 on
  every firewall chain, where it fills Bytes and Packets.
* **The cadence.** The .jg says how often each list is re-read --
  ``autorefresh`` is 1000ms on Interface, 2000 on the firewall chains -- and a
  counter column is only live if something re-reads it. One setting overrides
  the lot, including turning it off, and it is offered on both sides of a
  session: the connect dialog's Auto Refresh picker before, Settings ▸ List
  Auto Refresh during.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto.device import list_read_mask
from x3.proto.schema import Schema
from x3.ui import settings, style
from x3.ui.settings import LIST_REFRESH_CHOICES
from x3.ui.widgets.connect_dialog import ConnectDialog
from x3.ui.widgets.list_pane import ListPane

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if not cond:
        failures.append(f"{label} {detail}".strip())
        print(f"FAIL {label} {detail}".strip())
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "dark", persist=False)
schema = Schema.load("7.21.1-151146055")

iface = next(
    m for m in schema.menus
    if m.path == (20, 0) and (m.title or m.name) == "Interface"
)
firewall = next(m for m in schema.menus if m.path == (20, 42))   # Filter Rules
plain = next(
    m for m in schema.menus
    if m.is_collection and not m.refresh_filter and not m.autorefresh_ms
    and m.has_age_field
)

# --- the mask ------------------------------------------------------------

check("Interface reads with the mask Winbox sent", list_read_mask(iface), 0x4000F)
check("...which is the menu's own two numbers",
      list_read_mask(iface), 5 | iface.refresh_filter)
check("a firewall chain asks for its counters too",
      list_read_mask(firewall), 0x7)
check("a menu with no refreshfilter is read exactly as before",
      list_read_mask(plain), None)
check("and so is a list with no menu at all", list_read_mask(None), None)

# --- the cadence ---------------------------------------------------------

COLS = ["", "Name", "Tx", "Rx"]
ROWS = [["R", "ether1", "5.8 Mbps", "118.4 kbps"]]


def pane_for(menu) -> ListPane:
    pane = ListPane(
        "Interface", path=(20, 0), type_value=None,
        live_fetcher=lambda: (menu, COLS, ROWS, [None], None),
    )
    pane._on_loaded((menu, COLS, ROWS, [None], None))
    return pane


def timer_ms(pane) -> int:
    t = pane._poll_timer
    return t.interval() if (t is not None and t.isActive()) else 0


settings.save_list_refresh_secs(None)
p = pane_for(iface)
check("Interfaces polls at the 1s its menu declares", timer_ms(p), 1000)
p.close()

p = pane_for(firewall)
check("a firewall chain polls at its own 2s", timer_ms(p), 2000)
p.close()

p = pane_for(plain)
check("a list that declares no rate keeps the age-field default",
      timer_ms(p), int(ListPane.AUTO_REFRESH_SECS * 1000))
p.close()

# The preference overrides every list, and reaches windows already open.
p = pane_for(iface)
settings.save_list_refresh_secs(5.0)
p.apply_refresh_setting()
check("the setting reaches a list already open", timer_ms(p), 5000)

settings.save_list_refresh_secs(0.0)
p.apply_refresh_setting()
check("Off stops the polling", timer_ms(p), 0)

settings.save_list_refresh_secs(None)
p.apply_refresh_setting()
check("...and Menu default gives the list its rate back", timer_ms(p), 1000)

# Live is the other switch: no polling at all while a window is paused.
p._on_live_toggled(False)
check("pausing Live stops the poll", timer_ms(p), 0)
p._on_live_toggled(True)
check("resuming Live starts it again", timer_ms(p), 1000)
p.close()

# --- the same question, before the session --------------------------------
#
# The rate is worth choosing before connecting: it depends on the link, and a
# list opens the moment a session does. The connect dialog and Settings offer
# one setting, from one list of choices.

settings.save_list_refresh_secs(None)
dlg = ConnectDialog()
picker = dlg.refresh_rate
check("the dialog offers the same choices as Settings",
      [picker.itemText(i) for i in range(picker.count())],
      [label for _secs, label in LIST_REFRESH_CHOICES])
check("...defaulting to the menu's own rate", picker.currentData(), None)
check("...which on Interfaces is one second", iface.autorefresh_ms, 1000)

# A type-filtered window -- PPP > Interface, and the 43 other interface types
# -- merges a type overlay onto that menu, and the merge has to carry the rate
# with it. It used to leave autorefresh_ms off the merged Menu entirely, which
# is not "keep the base's" but "take the dataclass default", so the overlay
# came back declaring nothing and the list fell through to the 5s age-field
# fallback: PPP interface counters moved every five seconds instead of every
# second.
ppp_iface = schema.record_for((20, 0), 73)
check("a type-filtered interface list keeps the menu's rate",
      ppp_iface.autorefresh_ms, 1000)
lost = [
    (base.path, tv)
    for base in schema.records()
    if getattr(base, "generic", None) and base.autorefresh_ms
    for tv in {getattr(o, "typevalue", None) for o in schema.overlay_list}
    if tv is not None
    and schema.record_for(base.path, tv) is not base
    and not schema.record_for(base.path, tv).autorefresh_ms
]
ok("...and so does every other overlay of a menu that declares one",
   not lost, f"{len(lost)} lost it: {lost[:4]}")

open_pane = pane_for(iface)
check("a list open while it is changed polls at the old rate",
      timer_ms(open_pane), 1000)
picker.setCurrentIndex(next(
    i for i in range(picker.count()) if picker.itemData(i) == 5.0
))
check("picking a rate saves it", settings.load_list_refresh_secs(), 5.0)
check("...and reaches the list already open", timer_ms(open_pane), 5000)

picker.setCurrentIndex(next(
    i for i in range(picker.count()) if picker.itemData(i) is None
))
check("...and back to the menu default", timer_ms(open_pane), 1000)
open_pane.close()

reopened = ConnectDialog()
check("a dialog opened later shows the saved choice",
      reopened.refresh_rate.currentData(), None)
dlg.close()
reopened.close()
settings.save_list_refresh_secs(None)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all list-refresh tests passed")
