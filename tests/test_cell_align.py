"""Column alignment and cell density in the data tables.

No device and no pytest required:

    .venv/bin/python test_cell_align.py

Winbox lines its numbers up on the right, so the eye can compare magnitudes
down a column instead of reading each value. Three pieces have to agree for
that to work, and each is pinned here:

* the model decides alignment per *column*, from what the column holds -- a
  rendered ``5.8 Mbps`` is as much a number as ``1500``, while an address, a
  MAC and an uptime are not, however many digits they contain;
* the header follows its column, so a right-aligned heading sits over the
  right-aligned values;
* ColumnRuleDelegate reclaims most of the 3px Qt pads every cell with, and
  DataHeaderView reserves the lane the sort arrow is drawn in rather than the
  whole section height Qt would.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
)

from x3.ui import style
from x3.ui.models.table_model import SimpleTableModel
from x3.ui.sortkey import is_quantity
from x3.ui.widgets.column_rule import ColumnRuleDelegate, reclaimed_padding
from x3.ui.widgets.data_header import DataHeaderView
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

RIGHT = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
LEFT = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def side(model, col) -> str:
    return "R" if model.column_alignment(col) == RIGHT else "L"


# --- what reads as a number ----------------------------------------------

for text in ("1500", "5.8 Mbps", "118.4 kbps", "87%", "-73dBm", "883.4 MB", "0"):
    ok(f"{text!r} is a quantity", is_quantity(text))
for text in ("ether1", "10.0.0.1", "48:A9:8A:0F:1C:22", "3d 04:15:00", "00:05:00",
             "true", "", "1500 bytes free"):
    ok(f"{text!r} is not", not is_quantity(text))

# --- the Interfaces list --------------------------------------------------

COLS = ["", "Name", "Type", "Actual MTU", "L2 MTU", "Tx", "Rx", "MAC Address", "Uptime"]
ROWS = [
    ["R", "bridge1", "bridge", "1500", "", "5.8 Mbps", "118.4 kbps", "48:A9:8A:0F:1C:22", "3d 04:15:00"],
    ["R", "ether1", "ether", "1500", "1596", "118.4 kbps", "5.8 Mbps", "48:A9:8A:0F:1C:23", "00:05:00"],
    ["X", "ether10", "ether", "9000", "1596", "", "", "48:A9:8A:0F:1C:24", ""],
]

pane = ListPane("Interface", path=(20, 0), type_value=None)
pane._on_loaded((None, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
model = pane._model

check("numbers right, everything else left",
      "".join(side(model, c) for c in range(model.columnCount())),
      "LLLRRRRLL")
check("a blank cell does not make a numeric column text",
      side(model, 4), "R")
ok("the flags column stays left even though it is one letter",
   side(model, 0) == "L")

check("the header follows its column",
      [model.headerData(c, Qt.Orientation.Horizontal,
                        Qt.ItemDataRole.TextAlignmentRole)
       for c in (1, 3)],
      [LEFT, RIGHT])

# One non-numeric value is enough to make the whole column text: a ragged
# column is worse than a left-aligned one.
mixed = [list(r) for r in ROWS]
mixed[1][3] = "auto"
model.set_rows(mixed)
check("one 'auto' and the MTU column reads as text", side(model, 3), "L")

model.set_rows([list(r) for r in ROWS])
check("...and it goes back when the value does", side(model, 3), "R")

model.replace_row(0, ROWS[0][:3] + ["n/a"] + ROWS[0][4:])
check("an edited row re-decides the column too", side(model, 3), "L")

model.set_rows([list(r) for r in ROWS])
model.set_secret_columns([3])
check("a masked column is bullets, not a number", side(model, 3), "L")
model.set_secret_columns([])

# --- cell density ---------------------------------------------------------
#
# Qt insets a cell's text by PM_FocusFrameHMargin + 1 and offers no way to
# style it. Under the modern skins the answer comes from our own proxy style,
# which is free at paint time; a skin with no proxy leaves Fusion's 2 standing
# and ColumnRuleDelegate reclaims the difference per cell instead.

table = pane.table
opt = QStyleOptionViewItem()
opt.initFrom(table)
opt.rect = QRect(0, 0, 120, style.ROW_HEIGHT)
opt.widget = table
index = model.index(0, 1)

qstyle = table.style()
inset = qstyle.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, opt, table) + 1
check("a cell is inset by CELL_PAD, not by Qt's own 3px", inset, style.CELL_PAD)
check("...so the delegate has nothing left to take back",
      reclaimed_padding(opt), 0)
check("...and it hands back the same width Qt's own delegate would",
      ColumnRuleDelegate(table).sizeHint(opt, index).width(),
      QStyledItemDelegate(table).sizeHint(opt, index).width())

# Whichever end does it, a cell ends up inset by exactly CELL_PAD: a skin whose
# style answers the metric leaves nothing to reclaim, and one that does not
# (no proxy style installed) has the delegate make up the difference per cell.
check("style inset and delegate together come to CELL_PAD",
      inset - reclaimed_padding(opt), style.CELL_PAD)

# Painting is per cell per frame, so the delegate that costs a Python call for
# every one of them is only installed when a row actually needs one. With
# inline comments on -- the default -- a comment is a column, not a banner.
ok("a plain list paints through Qt's own delegate",
   table.itemDelegate() is pane._plain_delegate)
model.set_comment(0, "uplink")
pane._choose_item_delegate()
ok("a comment in its own column still needs no delegate",
   table.itemDelegate() is pane._plain_delegate)
pane.set_inline_comments(False)
model.set_comment(0, "uplink")
pane._choose_item_delegate()
ok("...but a comment *banner* brings the delegate in",
   table.itemDelegate() is pane._banner)
model.set_comment(0, "")
pane._choose_item_delegate()
ok("...and it steps back out again",
   table.itemDelegate() is pane._plain_delegate)
pane.set_inline_comments(True)

# --- the sort-arrow lane --------------------------------------------------

hdr = table.horizontalHeader()
ok("the list pane uses the data header", isinstance(hdr, DataHeaderView))
ok("sort indicators are on (nothing to reserve otherwise)",
   hdr.isSortIndicatorShown())

plain_table = QTableView()
plain_table.setModel(model)
plain_hdr = QHeaderView(Qt.Orientation.Horizontal, plain_table)
plain_table.setHorizontalHeader(plain_hdr)
plain_hdr.setSortIndicatorShown(True)
wide = [plain_hdr.sectionSizeHint(c) for c in range(model.columnCount())]
lean = [hdr.sectionSizeHint(c) for c in range(model.columnCount())]
ok("every column reserves less for the arrow than Qt would",
   all(a < b for a, b in zip(lean, wide)), f"{lean} vs {wide}")
ok("...but still enough to draw it in",
   all(a > 0 for a in lean) and hdr.arrow_lane(style.ROW_HEIGHT + 2) > 6,
   f"lane {hdr.arrow_lane(style.ROW_HEIGHT + 2)}")

# The header only steps aside for the column it is actually sorting, and only
# when that column's label is right-aligned.
pane._on_header_clicked(3)
check("clicking sorts the column we think", hdr.sortIndicatorSection(), 3)
ok("the sorted numeric column gives up the lane", pane._model.column_alignment(3) == RIGHT)

# --- row height: a theme's density is not negotiable ----------------------
#
# QHeaderView derives a minimum section size from the font -- 16px for an 11px
# face -- and clamps every section to it *silently*. Any theme asking for a
# shorter row got 16 and no warning, which is why the flat skins could not be
# made denser than their font. The minimum has to be lowered before the height
# goes on; that it happens is what this checks, because the failure mode is
# invisible: the rows just quietly stop matching the theme.
for theme_id in ("light", "classic", "roomy", "high-contrast"):
    style.apply_theme(app, theme_id, persist=False)
    dense = ListPane("Interface", path=(20, 0), type_value=None)
    dense._on_loaded((None, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
    dense.show()
    want = style.ROW_HEIGHT
    got = [dense.table.rowHeight(r) for r in range(dense._model.rowCount())]
    ok(
        f"{theme_id}: rows are the theme's {want}px, not QHeaderView's minimum",
        all(h == want for h in got),
        f"got {got}, vh.minimumSectionSize()={dense.table.verticalHeader().minimumSectionSize()}",
    )
    dense.close()
    dense.deleteLater()

# The check above passes for the wrong reason if a theme happens to ask for
# exactly the minimum, so the mechanism is asserted on its own: the pane's
# header must be willing to go under what a fresh one would refuse, and a row
# below the natural floor must actually stick.
style.apply_theme(app, "light", persist=False)
probe = ListPane("Interface", path=(20, 0), type_value=None)
probe._on_loaded((None, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
probe.show()
natural = QHeaderView(Qt.Orientation.Vertical).minimumSectionSize()
vh = probe.table.verticalHeader()
ok(
    "the pane lowers its header's floor below Qt's font-derived one",
    vh.minimumSectionSize() < natural,
    f"pane floor={vh.minimumSectionSize()}, a fresh header's={natural}",
)
vh.setDefaultSectionSize(natural - 3)
for r in range(probe._model.rowCount()):
    probe.table.setRowHeight(r, natural - 3)
tight = [probe.table.rowHeight(r) for r in range(probe._model.rowCount())]
ok(
    f"so a {natural - 3}px row is reachable at all",
    all(h == natural - 3 for h in tight),
    f"got {tight} against a natural floor of {natural}",
)
probe.close()
probe.deleteLater()

# --- cell padding both ways, and the rules switch ------------------------
#
# Asserted on what lands on the pixels, not on the delegate's internal shift:
# the shift is normally zero because QStyleSheetStyle already answers
# PM_FocusFrameHMargin from the padding our own sheet sets, so a test of the
# shift would have "passed" while proving nothing about the cell.
from dataclasses import replace as _replace

from PySide6.QtGui import QColor, QImage, QPainter

_base = _replace(style.LIGHT, id="_pads")


def _pane_for(**kw):
    theme = _replace(_base, id="_p" + "_".join(f"{k}{v}" for k, v in sorted(kw.items())), **kw)
    style.THEMES[theme.id] = theme
    style.apply_theme(app, theme.id, persist=False)
    p = ListPane("Interface", path=(20, 0), type_value=None)
    p._on_loaded((None, list(COLS), [list(r) for r in ROWS], [None] * len(ROWS), None))
    p.resize(420, 120)
    p.show()
    style.THEMES.pop(theme.id, None)
    return p


def _shot(pane):
    vp = pane.table.viewport()
    img = QImage(vp.size(), QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    vp.render(img)
    return img


def _first_ink_x(img, row_y):
    for x in range(img.width()):
        c = img.pixelColor(x, row_y)
        if (c.red() + c.green() + c.blue()) / 3 < 128:
            return x
    return -1


insets = {}
for pad in (0, 2, 6, 10):
    p = _pane_for(cell_pad=pad)
    img = _shot(p)
    y = p.table.rowViewportPosition(0) + p.table.rowHeight(0) // 2
    insets[pad] = _first_ink_x(img, y)
    p.close()
    p.deleteLater()
ok("cell_pad moves the cell's text inset, and keeps moving it past Qt's own",
   insets[0] <= insets[2] < insets[6] < insets[10],
   f"first-ink x by cell_pad: {insets}")

# The rules are painted by CommentTableView.paint_column_rules, so that is
# where the switch has to be honoured -- checked by counting ruled columns.
def _rule_columns(pane):
    img = _shot(pane)
    tbl = pane.table
    y = tbl.rowViewportPosition(0) + tbl.rowHeight(0) // 2
    grid = QColor(style.GRIDLINE)
    hits = 0
    for c in range(tbl.model().columnCount()):
        x = tbl.columnViewportPosition(c) + tbl.columnWidth(c) - 1
        if 0 <= x < img.width() and img.pixelColor(x, y) == grid:
            hits += 1
    return hits

on = _pane_for(column_rules=True)
off = _pane_for(column_rules=False)
ruled_on, ruled_off = _rule_columns(on), _rule_columns(off)
ok("a theme can switch the column rules off entirely",
   ruled_on > 0 and ruled_off == 0,
   f"ruled columns on={ruled_on} off={ruled_off}")
ok("and Qt's own grid goes with them", on.table.showGrid() and not off.table.showGrid())
for p in (on, off):
    p.close()
    p.deleteLater()

style.apply_theme(app, "dark", persist=False)

pane.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all cell-alignment tests passed")
