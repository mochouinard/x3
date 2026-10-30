"""Offscreen tests for Live mode in the main window. No device, no pytest:

    .venv/bin/python test_live_mode_ui.py

Pins the things that decide whether a keystroke reaches a router:

  * picking Live clears the client's read-only flag, and picking anything else
    sets it again — so the device layer refuses a write the UI never asked for,
  * Safe Mode is advisory, not a gate: the offer is made when Live is chosen
    and declining it means what it says, the way Winbox behaves,
  * and an open list pane says which of the three modes it is in, since the
    tag beside its row count is what tells you where Remove is about to go.

Sessions start in Live (Settings ▸ Start In Live Mode).

A stub client stands in for the device and records what it was asked to do.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from x3.ui.edit_mode import EditMode, QueuedChange
from x3.ui.session import SessionInfo

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


class StubClient:
    """Enough DeviceClient for MainWindow to build against."""

    readonly = True
    schema = None
    session_log = None

    def __init__(self):
        self.writes: list[tuple] = []

    def nav_tree(self):
        return []

    def set_obj(self, path, obj_id, values, **kw):
        if self.readonly:
            raise RuntimeError("refusing SETOBJ: this client is read-only")
        self.writes.append((path, obj_id, values))
        return None

    # Safe Mode runs on a worker thread against a terminal channel; the stub
    # only has to exist so accepting the toolbar's offer does not blow up.
    def safe_mode_enter(self):
        return None

    def safe_mode_leave(self, *, keep_changes=True):
        return None


app = QApplication.instance() or QApplication(sys.argv)

# Never let a modal block the run; record what would have been shown.
shown: list[tuple] = []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warn", a[1])))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[1])))
answer = [QMessageBox.StandardButton.No]
QMessageBox.question = staticmethod(
    lambda *a, **k: (shown.append(("question", a[1])), answer[0])[1]
)

from x3.ui.widgets.main_window import MainWindow

client = StubClient()
win = MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=client)

# Live by default now: this is a tool for changing configuration, and every
# session used to begin with the same two clicks. Settings ▸ Start In Live Mode
# is the switch; the toolbar and every form title still say which mode is on.
check("a session starts in Live", win.edit.mode, EditMode.LIVE)
check("and the client may write", client.readonly, False)
check("the combo agrees", win._mode_combo.currentData(), EditMode.LIVE)


def pick(mode):
    """Choose a mode the way clicking the combo does."""
    combo = win._mode_combo
    index = combo.findData(mode)
    assert index >= 0, f"no combo entry for {mode}"
    combo.setCurrentIndex(index)


# Qt stores the str-subclassing enum through QVariant and gives back its value.
# The handler used to reject that with an isinstance check and return, so no
# mode change of any kind took effect.
check("the combo hands back a plain str, not the enum",
      isinstance(win._mode_combo.itemData(0), EditMode), False)
check("which the window coerces back",
      MainWindow._coerce_mode("live"), EditMode.LIVE)
check("and refuses when it is not a mode at all",
      MainWindow._coerce_mode("nonsense"), None)


# --- the client flag tracks the mode ------------------------------------

pick(EditMode.QUEUE)
check("queue mode leaves the client read-only", client.readonly, True)

shown.clear()
pick(EditMode.LIVE)
check("picking Live offers Safe Mode", [k for k, _t in shown], ["question"])
check("declining still enters Live", win.edit.mode, EditMode.LIVE)
check("and the client may now write", client.readonly, False)

pick(EditMode.READONLY)
check("leaving Live locks the client again", client.readonly, True)

# --- Safe Mode is checked at write time ---------------------------------

change = QueuedChange(
    path=(20, 1), title="Address", row_key="192.168.88.1", obj_id=9,
    edits=[{"code": 0xFE0009, "name": "Comment", "new": "hello"}],
)

pick(EditMode.LIVE)
check("Safe Mode is still off", win._safe_mode_on, False)
# Safe Mode is advice, not a gate: Winbox warns when you go Live and then
# lets you work. Refusing every write here left Live mode unable to write at
# all, which is the one thing it is for. The offer to arm Safe Mode is made
# when the mode is picked (see below); declining it means what it says.
win.edit.apply(change)
check("a write with Safe Mode off is allowed", client.writes,
      [((20, 1), 9, {0xFE0009: "hello"})])
check("nothing blocks it", win.edit.blocked_reason(), None)

# Arm Safe Mode the way the toolbar toggle does, without touching a device.
win._safe_mode_on = True
check("and it is still allowed with Safe Mode on", win.edit.blocked_reason(), None)
win.edit.apply(change)
check("the second change is sent too", len(client.writes), 2)

# Read-only is still a real gate, Safe Mode or not.
win._safe_mode_on = False
pick(EditMode.READONLY)
try:
    win.edit.apply(change)
    failures.append("a write went through in read-only mode")
except Exception as e:  # noqa: BLE001
    check("read-only still refuses, and says why",
          "Live mode is not selected" in str(e), True)
check("and no third write was sent", len(client.writes), 2)
pick(EditMode.LIVE)

# --- accepting the Safe Mode offer arms it ------------------------------

pick(EditMode.READONLY)
answer[0] = QMessageBox.StandardButton.Yes
armed: list[bool] = []
win._safe_act.toggled.connect(armed.append)
pick(EditMode.LIVE)
check("saying yes to the offer toggles Safe Mode on", armed, [True])

# --- a property form's Apply refreshes the list it came from -------------
#
# Remove and Enable/Disable reload the pane after a live write; applying from
# a property form did not, so the list kept showing the old values. Editing an
# address left the previous one on screen until something else forced a
# refresh.

from x3.ui.widgets.list_pane import ListPane
from x3.ui.widgets.list_window import ListWindow

reloads: list[tuple] = []
lw = ListWindow("Address", readonly=False, live_fetcher=lambda: (), path=(20, 1))
lw.pane.reload = lambda *a, **k: reloads.append((20, 1))  # type: ignore[method-assign]
win.mdi.addSubWindow(lw)

other = ListWindow("Routes", readonly=False, live_fetcher=lambda: (), path=(21, 2))
other.pane.reload = lambda *a, **k: reloads.append((21, 2))  # type: ignore[method-assign]
win.mdi.addSubWindow(other)

edited = QueuedChange(
    path=(20, 1), title="Address", row_key="192.168.88.1", obj_id=1,
    edits=[{"code": 0x000001, "name": "Address", "new": "192.168.88.2"}],
)

pick(EditMode.LIVE)
win._on_property_change(edited)
check("applying from a property form reloads that list", reloads, [(20, 1)])
check("...and leaves other lists alone", (21, 2) in reloads, False)

# A queued change wrote nothing, so there is nothing to re-read.
reloads.clear()
pick(EditMode.QUEUE)
win._on_property_change(edited)
check("a queued change does not refetch", reloads, [])

# Panes inside a tabbed window are found too — they share one path.
reloads.clear()
pick(EditMode.LIVE)
check("panes are located by path, whatever holds them",
      len([p for p in lw.findChildren(ListPane) if p.path == (20, 1)]), 1)
win.reload_lists_for((20, 1))
check("...and reload", reloads, [(20, 1)])

# --- Apply all: the queue's only way onto the wire -----------------------
#
# Queue mode holds the client read-only, so the flush has to lift that flag and
# put it back; and since the write happens from the toolbar rather than a form,
# the window is what has to re-read the lists afterwards.

pick(EditMode.QUEUE)
reloads.clear()
shown.clear()
client.writes.clear()

queued = QueuedChange(
    path=(20, 1), title="Address", row_key="192.168.88.9", obj_id=9,
    edits=[{"code": 0x000001, "name": "Address", "new": "10.9.9.9"}],
)
win.edit.enqueue(queued)
win._refresh_mode_label()
check("the toolbar button says how many are held",
      win._queue_act.text(), "Pending changes (1)")

answer[0] = QMessageBox.StandardButton.No
win._flush_queue()
check("declining the confirmation sends nothing", client.writes, [])
check("...and leaves the queue where it was", len(win.edit.queue), 1)

answer[0] = QMessageBox.StandardButton.Yes
win._flush_queue()
check("Apply all sends the queued change, from Queue mode",
      client.writes, [((20, 1), 9, {0x000001: "10.9.9.9"})])
check("...and empties the queue behind it", win.edit.queue, [])
check("...puts the client's read-only flag back", client.readonly, True)
check("...re-reads the list it changed", reloads, [(20, 1)])
check("...and the count comes off the button",
      win._queue_act.text(), "Pending changes")

# A refusal has to say how far it got: claiming success for a queue that only
# half landed is worse than not sending it at all.
for key, oid, value in (("a", 1, "10.1.1.1"), ("b", 2, "10.1.1.2")):
    win.edit.enqueue(QueuedChange(
        path=(20, 1), title="Address", row_key=key, obj_id=oid,
        edits=[{"code": 0x000001, "name": "Address", "new": value}],
    ))
win._refresh_mode_label()
real_set = client.set_obj


def refuse_second(path, obj_id, values, **kw):
    if obj_id == 1:
        return real_set(path, obj_id, values, **kw)
    raise RuntimeError("cannot add: address already in use")


client.set_obj = refuse_second
shown.clear()
client.writes.clear()
reloads.clear()
win._flush_queue()
client.set_obj = real_set
check("a refusal mid-flush warns instead of claiming success",
      [k for k, _t in shown], ["question", "warn"])
check("...only what preceded it was written", len(client.writes), 1)
check("...the refused change stays queued, at the head",
      [c.row_key for c in win.edit.queue], ["b"])
check("...and the list that did change is re-read anyway", reloads, [(20, 1)])
win.edit.clear()
win._refresh_mode_label()

# --- Where a list's writes go -------------------------------------------
#
# The toolbar names the mode once for every window; a tag beside each list's
# row count repeated it ("[queue]" next to Filter) and was dropped on request.
# What still has to answer for all three modes is each verb's own tooltip --
# it once read "queued" in Live, while the same button wrote to the router.

tagged = ListWindow(
    "Address", readonly=False, live_fetcher=lambda: (), path=(20, 1),
    edit_session=win.edit,
)
tagged.pane.reload = lambda *a, **k: None  # type: ignore[method-assign]
win.mdi.addSubWindow(tagged)


def tag_now():
    return tagged.pane._status.text().split("row(s)")[-1].strip()


pick(EditMode.LIVE)
check("no mode tag beside the row count", tag_now(), "")
check("...and its Remove says where it goes",
      tagged.pane._verb_tip("Remove"),
      "Remove selected item(s) — sent to the router now")

pick(EditMode.QUEUE)
check("...in Queue mode either", tag_now(), "")
check("...and its Remove says local",
      tagged.pane._verb_tip("Remove"),
      "Remove selected item(s) (queued locally, not sent)")

pick(EditMode.READONLY)
check("...or Read-only", tag_now(), "")

# A pane with no session at all is not a live one.
loose = ListPane("Loose", path=(20, 1))
check("a session-less pane does not claim live", loose._live(), False)

win.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all live-mode UI tests passed")
