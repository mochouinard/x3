"""Offscreen tests for the Safe Mode dress code, its idle watchdog, and the
gate in front of a revert:

    .venv/bin/python test_safe_mode_chrome.py

Safe Mode used to announce itself with one checked button in the middle of the
toolbar and a small red label beside it — the same amount of chrome an ordinary
session wears, for a mode where a dropped connection throws away every change
made since it was armed. A session left armed and walked away from looked
exactly like one that was not, and the reminder that eventually appeared said
only "this session has been idle": no router, no user, no way to tell which of
several open windows was asking.

What is pinned here:

  * the toggle sits alone on the right of the bar, past a stretch;
  * an armed session wears construction tape, and takes all of it off again;
  * every Safe Mode warning names the device it is about;
  * the idle watchdog raises a real alarm — blinking banner, bell, taskbar —
    and then takes the window's hands off the controls for a few seconds, so
    that somebody returning to a blaring window cannot answer it, or click
    Remove in whatever was underneath the pointer, before reading anything;
  * that guard is scoped to the window that raised it, because the other
    window on a fleet is a different router someone may be working in;
  * unrolling asks a second time, everywhere it is reachable from.
"""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-safe-")

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QToolBar

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


class _State:
    def __init__(self, active):
        self.active_here = active


class StubClient:
    """Enough DeviceClient for MainWindow, with a Safe Mode that always works."""

    readonly = True
    schema = None
    session_log = None

    def __init__(self):
        self.calls: list[str] = []

    def nav_tree(self):
        return []

    def close(self):
        pass

    def safe_mode_enter(self):
        self.calls.append("enter")
        return _State(True)

    def safe_mode_leave(self, *, keep_changes=True):
        self.calls.append(f"leave keep={keep_changes}")
        return _State(False)


app = QApplication.instance() or QApplication(sys.argv)

# Nothing modal may block the run. The Safe Mode dialogs build their own
# QMessageBox rather than calling a static, so exec() is what has to stand in
# for the user: it records the box, presses whatever button the next entry of
# `answers` names (matched on label), and returns. An empty queue means the box
# was dismissed without a choice.
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.No
)

from x3.ui.widgets.safe_mode_dialog import SafeModeReminder  # noqa: E402

answers: list[str] = []
#: One entry per modal that went up, in order.
seen: list[dict] = []
#: Does the stand-in user wait for the input guard before answering? Real ones
#: have no choice; the guard test is the one place this is turned off.
patient = [True]
#: Run once from inside the next exec(), i.e. with the dialog notionally up.
during_modal: list = [None]


def _visible_text(dlg) -> str:
    """Everything the dialog puts on screen, whatever kind of dialog it is."""
    if isinstance(dlg, QMessageBox):
        return f"{dlg.text()}\n{dlg.informativeText()}"
    return "\n".join(lab.text() for lab in dlg.findChildren(QLabel) if lab.text())


def _fake_exec(self, *a, **k):
    bar = getattr(self, "guard_bar", None)
    seen.append({
        "title": self.windowTitle(),
        "text": _visible_text(self),
        "buttons": [b.text() for b in self.buttons()],
        "enabled": [b.isEnabled() for b in self.buttons()],
        # The dialog blocks the UI thread until it is answered, so this is the
        # only place from which "the alarm was already up" can be observed.
        "alarming": win._hazard_bar.alarming(),
        "guard": win._guard_left,
        "bar": bar is not None,
        "bar_secs": bar.remaining() if bar is not None else None,
        "depth": win._modal_depth,
    })
    # Anything the caller wants to happen "while the dialog is up" — a timer
    # firing behind it, say — has to run from in here: exec is where the real
    # dialog would be blocking.
    if during_modal[0] is not None:
        fn, during_modal[0] = during_modal[0], None
        fn()
    if patient[0]:
        win._end_input_guard()
    want = answers.pop(0) if answers else None
    if want:
        for btn in self.buttons():
            if want.casefold() in btn.text().casefold():
                btn.click()
                break
        else:
            raise AssertionError(
                f"no button matching {want!r} in {[b.text() for b in self.buttons()]}"
            )
    return 0


QMessageBox.exec = _fake_exec  # type: ignore[method-assign]
SafeModeReminder.exec = _fake_exec  # type: ignore[method-assign]

# The alarm's two out-of-window signals, recorded instead of made.
beeps: list[int] = []
alerts: list[int] = []
QApplication.beep = staticmethod(lambda: beeps.append(1))
QApplication.alert = staticmethod(lambda *a, **k: alerts.append(1))


# --- the tile ------------------------------------------------------------
#
# The MDI floor repaints on every window drag, so the stripes are a cached
# pixmap tiled by QBrush rather than anything drawn per paint. That only works
# if the tile has no seam.

from x3.ui import hazard  # noqa: E402

tile = hazard.stripe_pixmap()
check("the tile is square", (tile.width(), tile.height()),
      (hazard.PERIOD, hazard.PERIOD))
ok("and cached, not rebuilt per call", hazard.stripe_pixmap() is tile)

img = tile.toImage()
P = img.width()
# 45-degree stripes one tile-width apart are invariant under a (1, 1) slide,
# which wraps at the edges — so this is the seam test.
worst = 0
for x in range(P):
    for y in range(P):
        a, b = img.pixelColor(x, y), img.pixelColor((x + 1) % P, (y + 1) % P)
        worst = max(worst, abs(a.red() - b.red()), abs(a.blue() - b.blue()))
ok(f"and tiles without a seam (worst delta {worst})", worst <= 8)

dark = sum(
    img.pixelColor(x, y).lightness() < 128 for x in range(P) for y in range(P)
)
ok(f"black and yellow get about half each ({dark}/{P * P})",
   0.4 < dark / (P * P) < 0.6)

muted = hazard.mdi_brush("#808080")
ok("the MDI brush is a texture, not a colour",
   muted.style() == Qt.BrushStyle.TexturePattern)
ok("and is mixed towards the desktop it sits on, so windows still win",
   hazard._mix("#808080", hazard.HAZARD_YELLOW, 0.42).name() != hazard.HAZARD_YELLOW)


# --- the toolbar ---------------------------------------------------------

from x3.ui.session import SessionInfo  # noqa: E402
from x3.ui.edit_mode import EditMode, QueuedChange  # noqa: E402
from x3.ui.widgets.main_window import (  # noqa: E402
    GUARD_TICK_MS,
    IDLE_GUARD_SECS,
    SAFE_IDLE_CHOICES,
    MainWindow,
)

session = SessionInfo(
    host="10.0.0.1", user="admin", identity="core-rtr1",
    board="ax-ap", ros_version="7.21.1",
)
client = StubClient()
win = MainWindow(session, client=client)
win.show()

bar = win.findChild(QToolBar, "mainToolbar")
actions = bar.actions()
check("Safe Mode is the last thing on the bar", actions[-1] is win._safe_act, True)

# Past the stretch, Safe Mode has one neighbour: Pending changes, which shares
# its corner because it is the same kind of session-scoped state.
check("Pending changes sits immediately left of it",
      actions[-3] is win._queue_act, True)
stretches = [
    a for a in actions
    if (w := bar.widgetForAction(a)) is not None
    and w.sizePolicy().horizontalPolicy().name.startswith("Expanding")
]
ok("with a stretch in front of them holding them against the right edge",
   len(stretches) == 1 and actions.index(stretches[0]) < actions.index(win._queue_act))
ok("the button exists, so it can be dressed", win._safe_btn is not None)

# An empty queue in Live mode has nothing to open, so the button is not there
# to be clicked; in Queue mode an empty queue is itself the news.
ok("an empty queue in Live mode hides Pending changes entirely",
   not win._queue_act.isVisible())
win._apply_mode(EditMode.QUEUE)
ok("...but Queue mode keeps it, greyed",
   win._queue_act.isVisible() and not win._queue_act.isEnabled())
win.edit.enqueue(QueuedChange(path=(1, 2), title="Bridge", obj_id=7))
win._refresh_mode_label()
ok("a queued change enables it and puts the count on it",
   win._queue_act.isEnabled() and win._queue_act.text() == "Pending changes (1)")
win._apply_mode(EditMode.READONLY)
ok("...and it stays visible in Read-only, since the queue is still held",
   win._queue_act.isVisible())
check("but Apply all is refused there, and says why",
      win.edit.flush_reason(),
      "Read-only mode is selected: nothing can be written.")
win.edit.clear()
win._apply_mode(EditMode.LIVE)


# --- who is this window? -------------------------------------------------

check("a session names itself by router and address",
      win._device_label(), "core-rtr1 (10.0.0.1)")
check("...falling back to the address when the router has no name",
      MainWindow._device_label.__get__(
          type("X", (), {"session": SessionInfo(host="10.0.0.2", identity="")})()
      )(),
      "10.0.0.2")


# --- the dress code ------------------------------------------------------

def settle():
    """Let the Safe Mode worker finish and its queued signal be delivered."""
    worker = win._safe_worker
    if worker is not None:
        worker.wait(3000)
    app.processEvents()


ok("an idle session shows no banner", win._hazard_bar.isHidden())
check("and its MDI floor is a plain colour",
      win.mdi.background().style(), Qt.BrushStyle.SolidPattern)
check("the toolbar button is undressed", win._safe_btn.styleSheet(), "")

win._safe_act.setChecked(True)
settle()

check("arming asks the device", client.calls, ["enter"])
ok("the session knows it is armed", win._safe_mode_on)
ok("the banner opens", not win._hazard_bar.isHidden())
ok("saying what is at stake", "SAFE MODE" in win._hazard_bar.text())
ok("and which router it is about — two open windows look identical otherwise",
   "core-rtr1 (10.0.0.1)" in win._hazard_bar.text())
check("the MDI floor puts on hazard stripes",
      win.mdi.background().style(), Qt.BrushStyle.TexturePattern)
ok("the toolbar button goes yellow-and-black",
   hazard.HAZARD_YELLOW in win._safe_btn.styleSheet())
ok("and the title says so", win.windowTitle().endswith("[SAFE]"))
ok("the moment it was armed is remembered, so the reminder can say how long",
   win._safe_since is not None)


# --- the idle watchdog ---------------------------------------------------

win._set_idle_secs(10)
check("the chosen interval is what the watchdog waits", win._idle_timer.interval(), 10000)
ok("and it is running", win._idle_timer.isActive())

beeps.clear()
alerts.clear()
seen.clear()
answers.clear()  # walk away from the reminder without answering it
win._on_safe_mode_idle()

check("exactly one reminder went up", len(seen), 1)
box = seen[0]
ok("blinking before the modal blocked the thread", box["alarming"])
check("rings the bell", len(beeps), 1)
check("and asks for attention outside the window", len(alerts), 1)

ok("the reminder's title names the router", "core-rtr1 (10.0.0.1)" in box["title"])
ok("its body names the router too", "core-rtr1 (10.0.0.1)" in box["text"])
ok("...and the session behind it", "admin@10.0.0.1" in box["text"])
ok("...and the hardware, so it is not one of the other five",
   "ax-ap" in box["text"] and "7.21.1" in box["text"])
ok("...and how long Safe Mode has been on", "Safe Mode on for" in box["text"])
ok("...and how long it has been idle, in the units that were set",
   "10 seconds" in box["text"])
ok("...and what it costs to walk away", "unrolled" in box["text"])

ok("the status bar names it as well",
   "core-rtr1 (10.0.0.1)" in win.statusBar().currentMessage())
check("the wording follows the interval", MainWindow._idle_text(10), "10 seconds")
check("...and rounds to minutes once that reads better",
      MainWindow._idle_text(300), "5 minutes")
check("...singular where it should be", MainWindow._idle_text(60), "1 minute")

# Dismissed without an answer: still armed, still idle, so it goes round again.
ok("dismissing it re-arms the watchdog", win._idle_timer.isActive())
ok("and calls off the blink, since dismissing it is someone being there",
   not win._hazard_bar.alarming())

# The timeout is really wired to the alarm, not just reachable by hand.
seen.clear()
win._idle_timer.start(30)
deadline = time.monotonic() + 3.0
while not seen and time.monotonic() < deadline:
    app.processEvents()
check("the timer's own timeout raises it too", len(seen), 1)


# --- the input guard -----------------------------------------------------
#
# Somebody coming back to a blaring window reaches for the mouse before they
# have read anything, and the pointer is still wherever they left it.

patient[0] = False  # this stand-in user does not wait
seen.clear()
win._idle_notified = False
win._bump_idle_timer()
win._on_safe_mode_idle()

check("the alarm takes the window's hands off the controls",
      win._guard_left, IDLE_GUARD_SECS)
ok("the reminder's buttons are all locked while it runs",
   seen[0]["enabled"] == [False] * len(seen[0]["buttons"]))
ok("and the bar above them says what is being waited for, so a locked "
   "dialog does not read as a broken one",
   seen[0]["bar"] and seen[0]["bar_secs"] == IDLE_GUARD_SECS)
ok("the banner explains the freeze too", win._hazard_bar.text().startswith("INPUT HELD"))
ok("counting down", str(IDLE_GUARD_SECS) in win._hazard_bar.text())


# Whether an event was swallowed is not readable from sendEvent's return, which
# only says whether something handled it. A filter on the target itself is the
# real oracle: application-level filters run first, so a swallowed event never
# reaches this one.
class KeySpy(QObject):
    def __init__(self):
        super().__init__()
        self.n = 0

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            self.n += 1
        return False


def press(widget):
    """A fresh event each time — Qt keeps accept state on the object."""
    app.sendEvent(
        widget,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                  Qt.KeyboardModifier.NoModifier, "a"),
    )


# The filter lives on the application, so without scoping, one window's guard
# would freeze every other session too — a different router, possibly mid-edit.
other = MainWindow(SessionInfo(host="10.0.0.9", user="admin"), client=StubClient())
spy_here, spy_there = KeySpy(), KeySpy()
win.mdi.installEventFilter(spy_here)
other.mdi.installEventFilter(spy_there)

win._idle_timer.stop()  # so a bump is visible as the clock restarting
press(win.mdi)
check("a keystroke aimed at the guarded window never reaches it", spy_here.n, 0)
ok("and does not count as somebody being back — the guard runs its course",
   not win._idle_timer.isActive())

press(other.mdi)
check("while another session's window keeps working", spy_there.n, 1)
ok("without that counting as activity here either",
   not win._idle_timer.isActive())

# A deadline, not a countdown: a late tick cannot stretch the wait, and the
# bar can drain smoothly between whole seconds.
win._guard_end -= 1.0
win._tick_input_guard()
check("the guard counts down", win._guard_left, IDLE_GUARD_SECS - 1)
ok("and the banner with it", f"{IDLE_GUARD_SECS - 1}" in win._hazard_bar.text())
ok("it repaints many times a second, so the bar drains rather than steps",
   GUARD_TICK_MS <= 100 and win._guard_timer.interval() == GUARD_TICK_MS)

win._guard_end -= IDLE_GUARD_SECS
win._tick_input_guard()
check("it lets go on its own", win._guard_left, 0)
ok("the banner goes back to saying why the tape is up",
   win._hazard_bar.text().startswith("SAFE MODE"))
ok("and is not still running", not win._guard_timer.isActive())

press(win.mdi)
check("input lands again once it lets go", spy_here.n, 1)
ok("and restarts the idle clock", win._idle_timer.isActive())
patient[0] = True

win.mdi.removeEventFilter(spy_here)
other.close()


# --- the wait has a shape ------------------------------------------------
#
# A greyed-out button reads as a dialog that has hung, and a number ticking
# down in a paragraph of body text is not where anyone is looking.

seen.clear()
win._bump_idle_timer()
win._idle_notified = False
answers.clear()
win._on_safe_mode_idle()

ok("the reminder carries a draining bar", seen[0]["bar"])
check("showing the whole wait when it opens", seen[0]["bar_secs"], IDLE_GUARD_SECS)
ok("and the dialog is let go of once it closes", win._idle_box is None)

gb = hazard.GuardBar()
gb.set_remaining(IDLE_GUARD_SECS, IDLE_GUARD_SECS)
check("the bar rounds up, so it never claims less time than is left",
      gb.remaining(), IDLE_GUARD_SECS)
gb.set_remaining(1.2, IDLE_GUARD_SECS)
check("...still 2 with 1.2s to go", gb.remaining(), 2)
gb.set_remaining(0.0, IDLE_GUARD_SECS)
check("and reads zero exactly at the end", gb.remaining(), 0)
ok("it keeps its height either way, so nothing reflows at the moment it "
   "unlocks", gb.height() == hazard.GuardBar().height())


# The dialog itself, driven directly: locked means locked, and the answer the
# keyboard is aimed at is the one that changes nothing.
dlg = SafeModeReminder(
    heading="core-rtr1 (10.0.0.1)",
    rows=[("Session", "admin@10.0.0.1")],
    consequence="…unrolled.",
    legend="…",
)
dlg.set_guard(IDLE_GUARD_SECS, IDLE_GUARD_SECS)
check("every answer is locked while the guard runs",
      [b.isEnabled() for b in dlg.buttons()], [False, False, False])
check("and the bar says so", dlg.guard_bar.remaining(), IDLE_GUARD_SECS)
dlg.set_guard(1.5, IDLE_GUARD_SECS)
check("still locked part-way through",
      [b.isEnabled() for b in dlg.buttons()], [False, False, False])
dlg.set_guard(0.0, IDLE_GUARD_SECS)
check("and all three unlock together",
      [b.isEnabled() for b in dlg.buttons()], [True, True, True])
ok("with Return aimed at Stay, not at a button that changes the device",
   dlg.btn_stay.isDefault() and not dlg.btn_keep.isDefault()
   and not dlg.btn_discard.isDefault())
ok("...and neither of those may claim default just by taking focus",
   not dlg.btn_keep.autoDefault() and not dlg.btn_discard.autoDefault())
dlg.btn_discard.click()
check("clicking one records which", dlg.choice(), SafeModeReminder.DISCARD)
dlg.deleteLater()


# --- one dialog at a time ------------------------------------------------
#
# Clicking inside the reminder counted as activity, which re-armed the very
# timer that had opened it. A second reminder then sat modal over the first,
# so the first could not be answered at all, and each fresh alarm started
# another input guard — the buttons never stayed unlocked.

seen.clear()
beeps.clear()
win._bump_idle_timer()
win._idle_notified = False


def _fires_behind_the_dialog():
    """The watchdog going off while somebody is reading the reminder."""
    check("the countdown is held while a dialog is up",
          win._idle_timer.isActive(), False)
    win._bump_idle_timer()  # a click inside the dialog
    check("...and a click inside it does not re-arm the countdown",
          win._idle_timer.isActive(), False)
    win._idle_notified = False
    win._on_safe_mode_idle()


during_modal[0] = _fires_behind_the_dialog
answers.clear()
win._on_safe_mode_idle()

check("the alarm firing behind an open reminder opens no second one", len(seen), 1)
ok("it rings again instead", len(beeps) >= 2)
check("and the watchdog runs again once the dialog is answered",
      win._idle_timer.isActive(), True)
check("with nothing left held", win._modal_depth, 0)

# Nesting: the unroll confirmation must not be buried under a fresh reminder
# either, and depth has to unwind exactly.
seen.clear()
client.calls.clear()
answers[:] = ["leave — discard", "cancel"]
win._idle_notified = False
win._on_safe_mode_idle()
settle()
check("a reminder and its confirmation are two dialogs, not three", len(seen), 2)
check("the confirmation knows it is the second one", seen[1]["depth"], 2)
check("and the count unwinds", win._modal_depth, 0)
check("cancelling still sends nothing", client.calls, [])


# --- unrolling asks twice ------------------------------------------------

win._bump_idle_timer()
win._idle_notified = False
seen.clear()
client.calls.clear()
answers[:] = ["leave — discard", "cancel"]
win._on_safe_mode_idle()
settle()

check("reverting from the reminder opens a second, dangerous dialog", len(seen), 2)
ok("which names the router", "core-rtr1 (10.0.0.1)" in seen[1]["title"])
ok("and asks in as many words", "Discard every change" in seen[1]["text"])
ok("says it cannot be undone", "cannot be undone" in seen[1]["text"])
ok("and does not overpromise what the buffer holds",
   "another session or the console" in seen[1]["text"])
check("cancelling sends nothing to the device", client.calls, [])
ok("and leaves the session armed", win._safe_mode_on)
ok("with the tape still on", not win._hazard_bar.isHidden())

seen.clear()
answers[:] = ["leave — discard", "unroll — discard"]
win._idle_notified = False
win._on_safe_mode_idle()
settle()
check("confirming is what actually reverts", client.calls, ["leave keep=False"])
ok("and the tape comes off", win._hazard_bar.isHidden())

# The toolbar toggle reaches the same revert, so it gets the same gate.
win._safe_act.setChecked(True)
settle()
seen.clear()
client.calls.clear()
answers[:] = ["unroll changes", "cancel"]
win._safe_act.setChecked(False)
settle()
check("the toggle's revert is gated as well", len(seen), 2)
check("and cancelling there sends nothing either", client.calls, [])
ok("leaving the toggle on, because nothing changed", win._safe_act.isChecked())
ok("and the session still armed", win._safe_mode_on)

# Keeping changes is not destructive, so it is not gated.
seen.clear()
answers[:] = ["keep changes"]
win._safe_act.setChecked(False)
settle()
check("keeping changes asks once and goes", len(seen), 1)
check("and leaves with keep", client.calls, ["leave keep=True"])


# --- Settings ▸ Safe Mode Idle Alarm -------------------------------------

check("the menu offers a test interval as well as real ones",
      [s for s, _ in SAFE_IDLE_CHOICES], [0, 10, 60, 300, 900])

from x3.ui.settings import load_safe_mode_idle_secs  # noqa: E402

win._set_idle_secs(300)
check("picking one saves it", load_safe_mode_idle_secs(), 300)
ticked = [a.data() for a in win._idle_actions if a.isChecked()]
check("with the menu ticking exactly that entry", ticked, [300])

win._safe_act.setChecked(True)
settle()
win._set_idle_secs(300)
check("and re-arms the running session at the new interval",
      win._idle_timer.interval(), 300000)
win._set_idle_secs(0)
ok("Off stops the watchdog outright", not win._idle_timer.isActive())
win._set_idle_secs(10)


# --- taking it all off again ---------------------------------------------

win._hazard_bar.set_alarm(True)
client.calls.clear()
seen.clear()
answers[:] = ["keep changes"]
win._safe_act.setChecked(False)
settle()

check("leaving with keep needs no second opinion", client.calls, ["leave keep=True"])
ok("the session is disarmed", not win._safe_mode_on)
ok("the banner closes", win._hazard_bar.isHidden())
ok("the blinking stops with it", not win._hazard_bar.alarming())
check("the MDI floor goes back to the theme's colour",
      win.mdi.background().style(), Qt.BrushStyle.SolidPattern)
check("the button is undressed", win._safe_btn.styleSheet(), "")
ok("the title drops the marker", not win.windowTitle().endswith("[SAFE]"))
ok("and the watchdog stands down", not win._idle_timer.isActive())
ok("the armed-at clock is forgotten too", win._safe_since is None)


# --- a theme change keeps the tape on ------------------------------------
#
# The MDI stripes are mixed against the theme's desktop colour, and applying a
# theme repaints that colour underneath them.

win._safe_act.setChecked(True)
settle()
win._set_theme("classic")
check("re-theming an armed session leaves it wearing stripes",
      win.mdi.background().style(), Qt.BrushStyle.TexturePattern)
ok("and the banner stays open", not win._hazard_bar.isHidden())

# --- nothing changed, nothing to ask ------------------------------------------
# The client counts every config write made under Safe Mode. With none, keep
# and unroll are the same thing, and the question was noise.

client.safe_mode_pending = lambda: 0
win._safe_act.setChecked(True)
settle()
client.calls.clear()
seen.clear()
answers.clear()
win._safe_act.setChecked(False)
settle()
check("no changes: Safe Mode is left without asking", seen, [])
check("...simply released", client.calls, ["leave keep=True"])
ok("...and disarmed", not win._safe_mode_on)

client.safe_mode_pending = lambda: 2
win._safe_act.setChecked(True)
settle()
client.calls.clear()
seen.clear()
answers[:] = ["keep changes"]
win._safe_act.setChecked(False)
settle()
check("with changes made, it still asks keep or unroll",
      [d["title"] for d in seen], ["Leave Safe Mode"])
del client.safe_mode_pending

if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(f"  {f}")
    sys.exit(1)
print("\nall safe-mode chrome tests passed")
