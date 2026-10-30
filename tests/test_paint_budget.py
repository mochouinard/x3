"""What a live list is allowed to spend on one 1 Hz update, and on a drag.

No device and no pytest required:

    .venv/bin/python test_paint_budget.py

Both stalls this pins were invisible in every other test, because both are
about *cost*, not about what ends up on screen. Neither is measured in
milliseconds here -- a wall clock makes a flaky test and says nothing about
why. What is counted instead is C++ -> Python ``data()`` crossings, which is
where a repaint's budget goes (~2.6us each, and a full-viewport
list repaint is thousands of them). A crossing count is deterministic, so a
regression shows up as an order of magnitude rather than a slow afternoon.

**A row that needs more height must not make every row re-measure.** An
interface that is down carries a device status line, which needs a taller row
than the theme's. Getting that from ``ResizeToContents`` on the vertical header
means Qt re-measures every row through the delegate on every ``dataChanged`` --
and a live list patches every row once a second. One interface down on a
30-row Interface window cost 448ms and 111k crossings per 1 Hz update against
20ms and 2.5k; at 100 rows it was 4.5s, i.e. longer than the interval. It
swallowed the keyboard, and dragging the window while a tick landed stalled
1.3s at a time. So: the cost of an update must follow the *viewport*, not the
row count, whether or not any row is tall.

**A window being dragged must not repaint its contents.** Qt blits a moving
child window instead of repainting it, and skips one another window fully
covers -- but only for a window it knows is opaque, which needs
``WA_OpaquePaintEvent`` plus a paintEvent that really does cover every pixel.
Without it a drag repainted the full contents of the dragged window and of the
ones under it, every mouse-move.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QMainWindow,
    QMdiArea,
)

from x3.proto import message as m
from x3.proto.device import RowObject
from x3.ui import style
from x3.ui.models.table_model import SimpleTableModel
from x3.ui.widgets.list_window import ListWindow
from x3.ui.widgets.mdi_window import MdiWindow

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
        print(f"ok  {label}{('  (' + detail + ')') if detail else ''}")


app = QApplication.instance() or QApplication(sys.argv)
style.apply_theme(app, "light", persist=False)


# --- counting the crossings -------------------------------------------------

CROSSINGS: dict[int, int] = {}
_real_data = SimpleTableModel.data


def _counting_data(self, index, role=Qt.ItemDataRole.DisplayRole):
    CROSSINGS[id(self)] = CROSSINGS.get(id(self), 0) + 1
    return _real_data(self, index, role)


SimpleTableModel.data = _counting_data


def crossings_of(model, reset=False) -> int:
    if reset:
        CROSSINGS[id(model)] = 0
        return 0
    return CROSSINGS.get(id(model), 0)


# --- a list that looks like the Interface window ----------------------------

OID = 0xFE0001
COLS = ["", "Name", "Type", "MTU", "Tx", "Rx", "Tx Packet", "Rx Packet",
        "FP Tx", "FP Rx", "Comment", "Last Down"]


class StubMenu:
    """Enough Menu for ListPane._on_loaded; a 1 Hz live list, like Interface."""

    autorefresh_ms = 1000
    refresh_filter = 0x4000A
    is_collection = True
    has_age_field = False
    ordered = False
    addable = True
    movable = True
    context_actions = ()
    list_filters = ()
    fields = {}

    def flag_fields(self):
        return ()


class StatusRow(RowObject):
    """A row that may carry a device status line, as a down interface does."""

    def info_lines(self):
        return getattr(self, "status_lines", ())


def rows_for(count: int, *, down=(), tick: int = 0):
    """One read of the list: `down` rows carry a status line."""
    cells, raw = [], []
    for i in range(count):
        msg = m.Message()
        msg.add_u32(OID, i + 1)
        obj = StatusRow(path=(20, 0), raw=msg)
        obj.status_lines = ("link down",) if i in down else ()
        obj.values_by_code[OID] = i + 1
        raw.append(obj)
        # Only the counters move, which is what a 1 Hz push carries.
        cells.append(["R", f"ether{i + 1}", "Ethernet", "1500",
                      f"{(i + tick) % 9}.{(i + tick) % 7} Mbps",
                      f"{i % 5}.{i % 3} Mbps", f"{1000 + i + tick}",
                      f"{2000 + i}", "0 bps", "0 bps", "",
                      "aug/20 03:0%d" % (i % 9)])
    return cells, raw


def open_list(mdi, count, *, down=(), title="Interface", geometry=(10, 10)):
    win = ListWindow(title, path=(20, 0))
    mdi.addSubWindow(win)
    win.resize(1100, 620)
    win.move(*geometry)
    win.show()
    load(win, count, down=down)
    return win


def load(win, count, *, down=()):
    """A full read, as the poll and every Nth push do."""
    win.pane._on_loaded((StubMenu(), COLS, *rows_for(count, down=down), None))
    app.processEvents()


def one_update(win, count, *, down=(), tick=1) -> int:
    """Patch every row the way a 1 Hz push does; return the crossings spent."""
    cells, _ = rows_for(count, down=down, tick=tick)
    model = win.pane._model
    crossings_of(model, reset=True)
    for view_row in range(model.rowCount()):
        model.replace_row(view_row, cells[view_row])
    app.processEvents()
    return crossings_of(model)


main = QMainWindow()
area = QMdiArea()
main.setCentralWidget(area)
main.resize(1600, 1000)
main.show()


# --- 1. the row that needs the room gets it, and only that row -------------
# One window throughout, reloaded rather than reopened: a window opened later
# sits on top of the earlier ones, and comparing a covered window with a
# visible one would measure the stacking order instead of the fix.

live = open_list(area, 30)
check("a list with nothing to say is the theme's row height",
      live.pane.table.verticalHeader().sectionSize(0), style.ROW_HEIGHT)
quiet_cost = one_update(live, 30)

load(live, 30, down=(2, 7))
heights = [live.pane.table.verticalHeader().sectionSize(i) for i in range(30)]
plain = [h for i, h in enumerate(heights) if i not in (2, 7)]

check("a row with nothing to say keeps that height when another row is down",
      set(plain), {style.ROW_HEIGHT})
ok("a row with a status line is taller",
   heights[2] > style.ROW_HEIGHT and heights[2] == heights[7],
   f"status rows {heights[2]}px vs {style.ROW_HEIGHT}px")
check("...and it is the only kind of row that is", len(set(heights)), 2)

# The mechanism, named directly: this is the mode whose cost is unbounded.
check("the vertical header does not re-measure on data changes",
      live.pane.table.verticalHeader().sectionResizeMode(0),
      QHeaderView.ResizeMode.Fixed)


# --- 2. one 1 Hz update costs the viewport, not the table ------------------

noisy_cost = one_update(live, 30, down=(2, 7))
ok("a status line does not multiply the cost of an update",
   noisy_cost <= quiet_cost * 3,
   f"{noisy_cost} crossings with two down vs {quiet_cost} with none")

# Growing the list must not grow the per-update cost: only so much of it is on
# screen. Before the fix this went up with the *square* of the row count.
load(live, 120, down=(2, 7))
big_cost = one_update(live, 120, down=(2, 7))
ok("four times the rows does not cost four times as much",
   big_cost <= noisy_cost * 2,
   f"120 rows: {big_cost} crossings vs 30 rows: {noisy_cost}")

# A number to fail against even if both of the above drift together: a full
# repaint of this viewport is ~7 roles x 12 columns x ~40 visible rows.
ok("an update stays inside one viewport repaint",
   big_cost < 12000, f"{big_cost} crossings")


# --- 3. an update in a window nobody can see costs nothing ----------------

buried = live
load(buried, 30, down=(2,))
cover = open_list(area, 30, title="Interface on top", geometry=(0, 0))
cover.resize(1560, 940)          # right over the top of the buried one
app.processEvents()
buried_cost = one_update(buried, 30, down=(2,))
ok("a list behind another window does not repaint for its 1 Hz update",
   buried_cost == 0, f"{buried_cost} crossings")


# --- 4. dragging a window does not repaint its contents -------------------

ok("child windows tell Qt they are opaque",
   cover.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent),
   "WA_OpaquePaintEvent")

full_repaint = one_update(cover, 30)     # what a whole viewport costs
model = cover.pane._model
crossings_of(model, reset=True)
x, y = cover.x(), cover.y()
FRAMES = 10
for frame in range(FRAMES):
    cover.move(x + (frame % 5) * 7, y + (frame % 4) * 5)
    app.processEvents()
per_frame = crossings_of(model) / FRAMES
ok("a drag frame is blitted, not repainted",
   per_frame < full_repaint / 3,
   f"{per_frame:.0f} crossings a frame vs {full_repaint} for a repaint")

# The window that was behind it is exposed a strip at a time, not wholesale.
ok("...and the window under it repaints only what the move uncovered",
   crossings_of(buried) < full_repaint,
   f"{crossings_of(buried)} crossings over {FRAMES} frames")


# --- 5. the paintEvent keeps the promise the attribute makes --------------
# WA_OpaquePaintEvent turns off Qt's own background pass, so MdiWindow has to
# draw the stylesheet's background itself. Without that the resize grab band
# it insets its child by keeps whatever the backing store held -- a strip of
# whatever window used to be there.
source = MdiWindow.paintEvent.__doc__ or ""
ok("MdiWindow.paintEvent documents drawing its own background",
   "background" in source.lower())
painted = cover.grab().toImage()
band = style_frame = cover.style().pixelMetric(
    __import__("PySide6.QtWidgets", fromlist=["QStyle"]).QStyle.PixelMetric
    .PM_MdiSubWindowFrameWidth, None, cover)
corner = painted.pixelColor(max(0, band // 2), cover.height() // 2)
ok("the grab band is painted, not left as backing store",
   corner.alpha() == 255 and (corner.red(), corner.green(), corner.blue())
   != (0, 0, 0), f"band {band}px, colour {corner.name()}")

main.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all paint-budget tests passed")
