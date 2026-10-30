"""Offline tests for device status lines (STD_INFO 0xfe001c).

No device and no pytest required:

    .venv/bin/python test_info_lines.py

A CAPsMAN-managed wireless interface carries no comment: The router attaches
two read-only strings instead -- "managed by CAPsMAN" and its channel -- which
Winbox draws in red where a comment would go. What matters here is that they
stay glued to the object they came from: the pane keeps them per *object* and
maps them onto view rows through `sources`, so sorting, filtering and the
interface tree can never hand a row somebody else's status.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication

from x3.proto import message as m
from x3.proto.device import STD_DESCR, STD_INFO, RowObject
from x3.ui import style
from x3.ui.models.table_model import COMMENT_ROLE, INFO_ROLE
from x3.ui.widgets.comment_delegate import CommentBannerDelegate
from x3.ui.widgets.list_pane import ListPane

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    if not cond:
        failures.append(label)
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
app.setStyle("Fusion")
style.apply_theme(app, "light", persist=False)

# --- the wire shape, as captured off a 10-port router on (20,0) -------------------
# 0xfe001c is a string array: two lines on a CAPsMAN-managed wlan, an empty
# array on interfaces with nothing to say, absent entirely on most menus.

CAPS = (b"managed by CAPsMAN",
        b"channel: 5180/20-Ceee/ac(27dBm), SSID: Example_5GHz, local forwarding")


def row(*, info=None, comment=None) -> RowObject:
    values = {}
    if info is not None:
        values[STD_INFO] = list(info)
    if comment is not None:
        values[STD_DESCR] = comment
    return RowObject(path=(20, 0), raw=m.Message(), values_by_code=values)


check("both status lines decode, in wire order",
      row(info=CAPS).info_lines(),
      ("managed by CAPsMAN",
       "channel: 5180/20-Ceee/ac(27dBm), SSID: Example_5GHz, local forwarding"))
check("an empty array is no status at all", row(info=[]).info_lines(), ())
check("a menu that never sends the field is no status either",
      row().info_lines(), ())
check("blank lines are dropped rather than reserving empty strips",
      row(info=[b"", b"  ", b"real"]).info_lines(), ("real",))
check("status lines are not a comment", row(info=CAPS).comment(), "")

# --- the pane maps them onto view rows ------------------------------------

PATH = (20, 0)
COLS = ["", "Name", "Type", "MTU"]
ROWS = [
    ["R", "wlan2", "wlan", "1500"],
    ["R", "ether1", "ether", "1500"],
    ["R", "wlan1", "wlan", "1500"],
]
RAW = [
    row(info=[b"managed by CAPsMAN", b"channel: 5180/20-Ceee/ac"]),
    row(comment="uplink"),
    row(info=[b"managed by CAPsMAN", b"channel: 2442/20-Ce/gn"]),
]
NAME_COL = 1


def make(*, inline=False) -> ListPane:
    pane = ListPane("Interface", path=PATH, inline_comments=inline)
    pane._on_loaded((None, list(COLS), [list(r) for r in ROWS], list(RAW), None))
    return pane


def shown(pane) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (pane._model.row_values(r)[NAME_COL], pane._model.info(r))
        for r in range(pane._model.rowCount())
    ]


p = make()
check("each row shows its own status",
      shown(p),
      [("wlan2", ("managed by CAPsMAN", "channel: 5180/20-Ceee/ac")),
       ("ether1", ()),
       ("wlan1", ("managed by CAPsMAN", "channel: 2442/20-Ce/gn"))])

p._on_header_clicked(NAME_COL)  # sort by name: ether1, wlan1, wlan2
check("sorting moves the status with the row",
      shown(p),
      [("ether1", ()),
       ("wlan1", ("managed by CAPsMAN", "channel: 2442/20-Ce/gn")),
       ("wlan2", ("managed by CAPsMAN", "channel: 5180/20-Ceee/ac"))])

p._filter.setText("2442")
check("the filter can see status text", [n for n, _ in shown(p)], ["wlan1"])
check("and the surviving row keeps its own lines",
      shown(p)[0][1], ("managed by CAPsMAN", "channel: 2442/20-Ce/gn"))
p._filter.setText("")

# --- what the delegate makes of them --------------------------------------

banner = CommentBannerDelegate()
banner.set_enabled(True)  # inline comments off: comments get a strip too


def lines_at(pane, view_row):
    return banner.banner_lines(pane._model.index(view_row, 0))


p = make()
check("a status row gets one strip line per status line, prefixed and flagged",
      lines_at(p, 0),
      (("-- managed by CAPsMAN", True), ("-- channel: 5180/20-Ceee/ac", True)))
check("a commented row still gets its ;;; line", lines_at(p, 1),
      ((";;; uplink", False),))

both = ListPane("Interface", path=PATH)
both._on_loaded((None, list(COLS), [list(ROWS[0])],
                 [row(info=[b"managed by CAPsMAN"], comment="mine")], None))
check("comment first, then what the device says",
      banner.banner_lines(both._model.index(0, 0)),
      ((";;; mine", False), ("-- managed by CAPsMAN", True)))

banner.set_enabled(False)  # inline comments on
check("with inline comments on the comment moves to its column...",
      banner.banner_lines(both._model.index(0, 0)),
      (("-- managed by CAPsMAN", True),))
ok("...and the status line stays, because it has no column to move to",
   banner.banner_lines(both._model.index(0, 0)) != ())
banner.set_enabled(True)

# --- roles are answered on every column, or the row height is wrong -------
# A row is as tall as its tallest cell, so every cell has to know the strip is
# there even though only column 0 draws it.

p = make()
for col in range(p._model.columnCount()):
    idx = p._model.index(0, col)
    if idx.data(INFO_ROLE) != ("managed by CAPsMAN", "channel: 5180/20-Ceee/ac"):
        failures.append(f"INFO_ROLE not answered on column {col}")
ok("INFO_ROLE is answered on every column", True)
check("COMMENT_ROLE is untouched by all this",
      p._model.index(1, 0).data(COMMENT_ROLE), "uplink")

# --- room to draw ---------------------------------------------------------

inline = make(inline=True)
ok("status lines force auto row heights even with inline comments on",
   inline._has_info())
fm = inline.table.fontMetrics()
ok("two status lines reserve two lines of height",
   banner.banner_height(fm, 2) == 2 * banner.banner_height(fm, 1))
ok("a row with nothing to say reserves none", banner.banner_height(fm, 0) == 0)
ok("a line leaves a pixel below the text for its rule",
   banner.banner_height(fm, 1) > fm.height())

# --- a local delete keeps the parallel lists in step ----------------------

p = make()
p._all_rows.pop(0)
p._all_comments.pop(0)
p._all_info.pop(0)
p._raw_rows.pop(0)
p._rebuild_view()
check("after dropping wlan2 the rest still own their status",
      shown(p),
      [("ether1", ()),
       ("wlan1", ("managed by CAPsMAN", "channel: 2442/20-Ce/gn"))])

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all status-line tests passed")
