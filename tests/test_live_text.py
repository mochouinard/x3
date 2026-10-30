"""A self-refreshing transcript must not take the reader's place away.

No device and no pytest required:

    .venv/bin/python test_live_text.py

The Log window redraws every 1.5s and the "changed elsewhere" window every 2s,
and both did it with a plain setPlainText. Qt treats that as a wholesale
replacement: caret back to the start, selection gone, scrollbar dropped to the
top. So scrolling up in the Log to read the exchange that went wrong lasted
until the next entry arrived, and dragging over a few lines to copy them lost
the selection the same way. Both windows guarded only the tail case.

What is pinned here (x3.ui.widgets.live_text):

* a selection survives a redraw, and the view does not move while it stands --
  copying is the one thing a read-only transcript is for;
* a reader who has scrolled up stays scrolled up, caret and all;
* a reader at the tail is still carried along with it, which is the one case
  where being moved is the point;
* unchanged text is not redrawn at all, so an idle window is perfectly still.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/settings.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QPlainTextEdit

from x3.ui import style
from x3.ui.widgets import live_text
from x3.ui.widgets.log_window import SessionLogWindow
from x3.ui.widgets.remote_change_window import RemoteChangeWindow

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


def lines(n, start=0):
    return "\n".join(f"line {i}" for i in range(start, n))


def a_view(n=200):
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.resize(300, 120)
    view.show()
    view.setPlainText(lines(n))
    return view


def select(view, start, end):
    cur = view.textCursor()
    cur.setPosition(start)
    cur.setPosition(end, cur.MoveMode.KeepAnchor)
    view.setTextCursor(cur)


# --- the helper ------------------------------------------------------------

print("-- a reader parked in the middle")
view = a_view()
bar = view.verticalScrollBar()
bar.setValue(bar.maximum() // 2)
parked = bar.value()
ok("the test really is parked mid-document", 0 < parked < bar.maximum())
check("a new line arrives", live_text.set_text_keeping_place(view, lines(201)), True)
check("...and the reader is still where they were", bar.value(), parked)

print("-- a reader with a selection")
view = a_view()
select(view, 0, 6)
check("something is selected", view.textCursor().selectedText(), "line 0")
bar = view.verticalScrollBar()
bar.setValue(0)
live_text.set_text_keeping_place(view, lines(201))
check("the selection survives the redraw",
      view.textCursor().selectedText(), "line 0")
check("...and so does the scroll position", bar.value(), 0)

print("-- a selection outranks Follow")
view = a_view()
select(view, 0, 6)
bar = view.verticalScrollBar()
bar.setValue(0)
live_text.set_text_keeping_place(view, lines(201), follow=True)
check("Follow does not yank the text out from under a drag",
      view.textCursor().selectedText(), "line 0")
check("...and does not scroll away from it", bar.value(), 0)

print("-- a reader at the tail is carried along")
view = a_view()
bar = view.verticalScrollBar()
bar.setValue(bar.maximum())
live_text.set_text_keeping_place(view, lines(201))
check("still at the tail", bar.value(), bar.maximum())
ok("...with the caret there too",
   view.textCursor().position() >= len(lines(201)) - 1)

print("-- Follow pins a reader who scrolled up")
view = a_view()
bar = view.verticalScrollBar()
bar.setValue(0)
live_text.set_text_keeping_place(view, lines(201), follow=True)
check("Follow goes to the tail", bar.value(), bar.maximum())

print("-- an idle window does not move at all")
view = a_view()
bar = view.verticalScrollBar()
bar.setValue(bar.maximum() // 2)
parked = bar.value()
check("unchanged text is not redrawn",
      live_text.set_text_keeping_place(view, lines(200)), False)
check("...so nothing moved", bar.value(), parked)

print("-- the caret cannot be left past the end of a shrinking transcript")
view = a_view()
select(view, 1400, 1410)
live_text.set_text_keeping_place(view, "one line only")
ok("the caret is clamped into the new text",
   view.textCursor().position() <= len("one line only"),
   f"position {view.textCursor().position()}")


# --- the two windows, wired to it -----------------------------------------

class Entry:
    kind = "request"

    def __init__(self, text):
        self._text = text

    def as_text(self):
        return self._text


class Log:
    path = "/tmp/session.log"
    suppressed = 0

    def __init__(self, n):
        self.entries = [Entry(f"line {i}") for i in range(n)]


class Note:
    def __init__(self, text):
        self._text = text

    def line(self):
        return self._text


def read_the_middle(view):
    """Scroll to the middle and select a line there, the way anyone copying a
    line out of a transcript ends up. Selecting is what scrolls the view, so
    the parked position is read back afterwards rather than set first."""
    half = len(view.toPlainText()) // 2
    select(view, half, half + 6)
    bar = view.verticalScrollBar()
    return bar.value(), view.textCursor().selectedText()


print("-- Session Log window")
log = Log(200)
win = SessionLogWindow(log)
win.resize(400, 160)
win.show()
win._follow.setChecked(False)
win._refresh()
bar = win._view.verticalScrollBar()
parked, selected = read_the_middle(win._view)
ok("reading the middle of the Log", 0 < parked < bar.maximum(),
   f"parked {parked} of {bar.maximum()}")
log.entries.append(Entry("line 200"))
win._refresh()
check("the Log keeps a selection across a refresh",
      win._view.textCursor().selectedText(), selected)
check("...and keeps the reader's place", bar.value(), parked)
win.close()

print("-- Changed-elsewhere window")
notes = [Note(f"line {i}") for i in range(200)]
rc = RemoteChangeWindow(notes, "ax-ap")
rc.resize(400, 160)
rc.show()
rc._refresh()
bar = rc._view.verticalScrollBar()
parked, selected = read_the_middle(rc._view)
ok("reading the middle of the change list", 0 < parked < bar.maximum(),
   f"parked {parked} of {bar.maximum()}")
notes.append(Note("line 200"))
rc._refresh()
check("the change list keeps a selection across a refresh",
      rc._view.textCursor().selectedText(), selected)
check("...and keeps the reader's place", bar.value(), parked)
rc.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all live-text tests passed")
