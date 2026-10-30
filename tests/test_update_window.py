"""Offline tests for the Check For Updates window (handler 24,24).

No device and no pytest required:

    .venv/bin/python test_update_window.py

Drives the real widget against a fake DeviceClient, using the real (24,24)
schema menu parsed from the checked-in 7.21.1 plugin mirror. The point is the
state machine: which buttons the schema's own on: conditions expose as the
device moves 0 (up to date) -> 1 (update available) -> 2 (downloading), and
that the reboot-triggering command cannot fire without confirmation.
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPlainTextEdit

from x3.proto import device as dev
from x3.proto import schema as sch
from x3.ui.widgets.update_window import UpdateWindow

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


SCHEMA_DIR = "7.21.1-151146055"
if not os.path.isdir(SCHEMA_DIR):
    print(f"SKIP: {SCHEMA_DIR}/ not present (plugin mirror is gitignored)")
    raise SystemExit(0)

schema = sch.Schema.load(SCHEMA_DIR)
MENU = schema.record(dev.PKG_UPDATE_PATH)
ok("(24,24) is in the 7.21.1 schema", MENU is not None)
if MENU is None:
    raise SystemExit(1)


class FakeInfo:
    identity = "test-router"
    version = "7.21.1"


class FakeRow:
    """Stands in for device.RowObject."""

    def __init__(self, values: dict[int, object]) -> None:
        self.values_by_code = dict(values)

    def get_rendered(self, code: int, *, prefer_name=None) -> str:
        v = self.values_by_code.get(code)
        return "" if v is None else str(v)


class FakeClient:
    """Records what the window sends instead of touching a router."""

    host = "192.0.2.1"

    def __init__(self, state: int = 0) -> None:
        self.info = FakeInfo()
        self.schema = schema
        self.sent: list[int] = []
        self.polls = 0
        self.state = state
        self.raise_on_run: Exception | None = None

    def alloc_from(self):
        return (0, 222)

    def package_update_menu(self):
        return MENU

    def _row(self):
        vals = {
            0x1: "7.21.1",
            0x5: self.state,
            0xA: 1,
        }
        if self.state == 1:
            vals[0x2] = "7.23.3"
            vals[0x3] = "New version is available"
            vals[0x4] = "What's new in 7.23.3\n *) a change\n *) another"
        elif self.state == 2:
            vals[0x3] = "downloading..."
        else:
            vals[0x3] = ""
        return FakeRow(vals)

    def package_update_status(self, *, from_path=None, quiet=True):
        self.polls += 1
        return self._row()

    def package_update_run(self, command, *, from_path=None):
        if self.raise_on_run is not None:
            raise self.raise_on_run
        self.sent.append(command)

    # --- progress subscription -------------------------------------------

    def package_update_subscribe(self, *, from_path):
        self.subscribed = getattr(self, "subscribed", 0) + 1
        self.unsubscribed = getattr(self, "unsubscribed", 0)
        return _FakeSub()

    def package_update_unsubscribe(self, sub, *, from_path):
        self.unsubscribed = getattr(self, "unsubscribed", 0) + 1

    def package_update_notify_row(self, msg):
        return msg          # the fake pushes ready-made rows

    def package_update_set(self, code, value, *, from_path=None):
        if getattr(self, "raise_on_set", None) is not None:
            raise self.raise_on_set
        self.sets = getattr(self, "sets", [])
        self.sets.append((code, value))


class _FakeSub:
    """Subscription stand-in that yields nothing.

    It honours ``timeout`` so the reader thread blocks the way the real
    Subscription makes it block; returning instantly instead would spin the
    thread at full tilt and make the test lie about CPU behaviour.
    """

    def get(self, timeout=None):
        time.sleep(timeout or 0.05)
        return None


class FakeEdit:
    def __init__(self, readonly: bool = False) -> None:
        self.readonly = readonly


app = QApplication.instance() or QApplication([])

import x3.ui.widgets.update_window as uw


class _Boxes:
    """Records modal dialogs instead of showing them.

    A real QMessageBox blocks forever under the offscreen platform because
    there is nobody to click OK, so the read-only path would hang the run.
    """

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def information(self, _parent, title, text, *a, **k):
        self.shown.append(("information", f"{title}: {text}"))

    def warning(self, _parent, title, text, *a, **k):
        self.shown.append(("warning", f"{title}: {text}"))

    def question(self, _parent, title, text, *a, **k):
        self.shown.append(("question", f"{title}: {text}"))
        return None


boxes = _Boxes()
uw.QMessageBox = boxes


def visible_buttons(win) -> set[str]:
    """Labels the schema's conditions currently expose."""
    return {
        b.text() for _act, b in win._action_buttons if b.isVisible()
    }


#: Every window built here, so the run can guarantee none is left holding a
#: live reader thread -- Qt aborts the process if a QThread outlives its parent.
_windows: list = []


def make(state=0, readonly=False):
    client = FakeClient(state)
    win = UpdateWindow(client, menu=MENU, edit_session=FakeEdit(readonly))
    _windows.append(win)
    win.show()          # offscreen; needed for isVisible() to mean anything
    app.processEvents()
    win._on_row(client._row())   # deterministic: skip the worker thread
    return client, win


def close_all() -> None:
    for w in _windows:
        try:
            w.close()
        except RuntimeError:
            pass        # already deleted by WA_DeleteOnClose
    _windows.clear()
    app.processEvents()


# --- the schema drives the form, not hardcoded codes ------------------------

client, win = make(state=1)
labels = {}
for code, w in win._value_widgets.items():
    labels[code] = type(w).__name__
ok("Channel (0xa) rendered as a combo", labels.get(0xA) == "QComboBox")
ok("Installed Version (0x1) rendered as a line edit",
   labels.get(0x1) == "QLineEdit")
ok("changelog (0x4) rendered as a multi-line box",
   labels.get(0x4) == "QPlainTextEdit")
ok("state (0x5) is not shown as a row", 0x5 not in win._value_widgets)
ok("status (0x3) is chrome, not a row", 0x3 not in win._value_widgets)
check("changelog text came from the device",
      win._value_widgets[0x4].toPlainText().splitlines()[0],
      "What's new in 7.23.3")
ok("Channel combo is writable (schema marks it autoset, not ro)",
   win._value_widgets[0xA].isEnabled())
ok("Channel combo shows the device's value",
   win._value_widgets[0xA].currentText() == "stable")

# --- button visibility follows `state` every tick --------------------------

check("state 1 exposes exactly the newver buttons",
      visible_buttons(win), {"Download", "Download&Install"})
ok("status line shows the device's text",
   "New version is available" in win._status.text())

win._on_row(FakeClient(2)._row())
check("state 2 exposes only Cancel", visible_buttons(win), {"Cancel"})

win._on_row(FakeClient(0)._row())
check("state 0 exposes only Check For Updates",
      visible_buttons(win), {"Check For Updates"})
ok("buttons re-evaluate on later ticks too (not frozen at construction)",
   True)
win.close()

# --- non-destructive commands go straight to the wire ---------------------

client, win = make(state=0)
for act, btn in win._action_buttons:
    if btn.text() == "Check For Updates":
        win._run_action(act)
check("Check For Updates sent cmd 4", client.sent, [4])

client, win = make(state=1)
for act, btn in win._action_buttons:
    if btn.text() == "Download":
        win._run_action(act)
check("Download sent cmd 1", client.sent, [1])
win.close()

# --- the reboot needs confirmation ---------------------------------------

client, win = make(state=1)
install = [a for a, b in win._action_buttons if b.text() == "Download&Install"]
ok("Download&Install exists at state 1", len(install) == 1)


class _Reject:
    """Stand-in confirmation dialog that the operator cancels."""

    def __init__(self, *a, **k) -> None:
        pass

    def exec(self):
        return 0            # QDialog.DialogCode.Rejected


real_dialog = uw._ConfirmInstallDialog
uw._ConfirmInstallDialog = _Reject
win._run_action(install[0])
check("cancelling the confirmation sends nothing", client.sent, [])
ok("cancelling leaves the window live", not win._install_sent)


class _Accept(_Reject):
    def exec(self):
        return 1            # Accepted


uw._ConfirmInstallDialog = _Accept
win._run_action(install[0])
check("confirming sends the install command", client.sent, [3])
ok("install marks the window as awaiting reboot", win._install_sent)
ok("buttons disabled once install is under way",
   all(not b.isEnabled() for _a, b in win._action_buttons))
uw._ConfirmInstallDialog = real_dialog
win.close()

# --- a dropped session during install is expected, not an error ----------

client, win = make(state=1)
win._install_sent = True
win._on_err("DeviceError: peer closed during read")
ok("reboot-time disconnect reads as install progress, not a failure",
   "rebooting" in win._status.text().lower())
win.close()

client, win = make(state=0)
win._on_err("DeviceError: something else broke")
ok("an ordinary error is still surfaced",
   "something else broke" in win._status.text())
win.close()

# --- read-only mode blocks every command ---------------------------------

client, win = make(state=1, readonly=True)
ok("read-only disables the command buttons",
   all(not b.isEnabled() for _a, b in win._action_buttons))
before = len(boxes.shown)
for act, btn in win._action_buttons:
    if btn.text() == "Download":
        win._run_action(act)
check("read-only sends nothing", client.sent, [])
ok("read-only explains itself instead of silently doing nothing",
   any("Read-only" in t for _k, t in boxes.shown[before:]))
win.close()

# --- older firmware: the same code must cope with a different menu -------
# 6.30.4 has no Channel field at all and calls cmd 3 'Download&Upgrade';
# the 6.49 branch moves Channel to 0xb. Both must render and both must still
# put a confirmation in front of the reboot.

for older_dir in sorted(
    d for d in os.listdir(".") if d.startswith("6.")
) or []:
    pass    # repo ships only the 7.21.1 mirror; covered by the synthetic case

synthetic = sch.Schema.load(SCHEMA_DIR).record(dev.PKG_UPDATE_PATH)
import copy

old = copy.deepcopy(synthetic)
old.fields.pop(0xA, None)                       # pre-6.31: no Channel
for a in old.form_actions:
    if a.get("cmd") == 3:
        a["name"] = "Download&Upgrade"          # pre-6.31 label
old.form_layout = [e for e in old.form_layout
                   if e.get("code") != 0xA]
client = FakeClient(1)
win = UpdateWindow(client, menu=old, edit_session=FakeEdit())
win.show()
app.processEvents()
win._on_row(client._row())
ok("a menu with no Channel field still builds", 0xA not in win._value_widgets)
check("older label is still exposed at state 1",
      visible_buttons(win), {"Download", "Download&Upgrade"})
upg = [a for a, b in win._action_buttons if b.text() == "Download&Upgrade"]
uw._ConfirmInstallDialog = _Reject
win._run_action(upg[0])
check("'Download&Upgrade' is recognised as the reboot command too",
      client.sent, [])
uw._ConfirmInstallDialog = real_dialog
win.close()

moved = copy.deepcopy(synthetic)
fld = moved.fields.pop(0xA)
moved.fields[0xB] = fld                          # 6.48.6-6.49.19 branch
moved.form_layout = [
    dict(e, code=0xB) if e.get("code") == 0xA else e for e in moved.form_layout
]
client = FakeClient(1)
win = UpdateWindow(client, menu=moved, edit_session=FakeEdit())
win.show()
app.processEvents()
ok("Channel at 0xb is picked up from the schema", 0xB in win._value_widgets)
win.close()

# --- progress subscription ------------------------------------------------
# Real Winbox stops polling and subscribes the moment a download starts; the
# router then pushes NOTIFY frames whose status field carries the percentage.
# A GET never reports progress, so this is the only route to it.

client, win = make(state=1)
ok("no subscription before a download starts",
   getattr(client, "subscribed", 0) == 0)
for act, btn in win._action_buttons:
    if btn.text() == "Download":
        win._run_action(act)
check("Download subscribes for progress", getattr(client, "subscribed", 0), 1)
for act, btn in win._action_buttons:
    if btn.text() == "Download":
        win._run_action(act)
check("subscribing is idempotent", getattr(client, "subscribed", 0), 1)
win.close()
check("closing drops the subscription", getattr(client, "unsubscribed", 0), 1)

# Opening onto an already-running download must subscribe too.
client, win = make(state=2)
check("state 2 on open subscribes without being asked",
      getattr(client, "subscribed", 0), 1)

# A pushed row updates the same widgets the poll does -- that is the whole
# point of reusing the render path.
win._on_row(FakeRow({
    0x1: "7.23.1", 0x2: "7.23.3", 0x5: 2,
    0x3: "Downloaded 89% (22.5MiB)", 0xA: 1,
}))
ok("pushed progress reaches the status line",
   "Downloaded 89% (22.5MiB)" in win._status.text())
check("a pushed row still drives button visibility",
      visible_buttons(win), {"Cancel"})
win.close()

# The subscription dying mid-install is the reboot, not a fault.
client, win = make(state=2)
win._install_sent = True
win._on_push_ended("DemuxClosed: subscription closed")
ok("subscription ending during install reads as the reboot",
   "rebooting" in win._status.text().lower())
win.close()

# A failed subscribe must not take the window down -- polling still works.
client, win = make(state=1)


def _boom(*a, **k):
    raise RuntimeError("no demux")


client.package_update_subscribe = _boom
for act, btn in win._action_buttons:
    if btn.text() == "Download":
        win._run_action(act)
check("a download still goes out when subscribing fails", client.sent, [1])
ok("the window says why live progress is unavailable",
   "no live progress" in win._busy_note)
win.close()

# --- Channel writes straight to the device (type:autoset) ------------------
# Captured from Winbox: command 0xFE000E, no request id, carrying only the
# changed field. The device answers nothing and re-runs its check, so the UI
# must read the value back rather than assume its own choice stuck.

client, win = make(state=0)
combo = win._value_widgets[0xA]
check("combo starts on the device's channel", combo.currentText(), "stable")
combo.setCurrentIndex(combo.findData(3))          # 'development'
win._on_enum_chosen(0xA, combo)
check("choosing a channel writes it", getattr(client, "sets", []), [(0xA, 3)])
ok("the write is reported in the status line", "development" in win._busy_note)

# Re-picking the value the device already reports must not write again.
client.sets = []
combo.setCurrentIndex(combo.findData(1))          # back to the device's value
win._on_enum_chosen(0xA, combo)
check("re-selecting the device's own value writes nothing",
      getattr(client, "sets", []), [])
win.close()

# A refused write must put the combo back, not leave a lie on screen.
client, win = make(state=0)
combo = win._value_widgets[0xA]
client.raise_on_set = RuntimeError("nope")
combo.setCurrentIndex(combo.findData(2))
win._on_enum_chosen(0xA, combo)
check("a failed write snaps the combo back to the device value",
      combo.currentData(), 1)
win.close()

# Read-only mode blocks the write and also restores the combo.
client, win = make(state=0, readonly=True)
combo = win._value_widgets[0xA]
combo.setCurrentIndex(combo.findData(3))
win._on_enum_chosen(0xA, combo)
check("read-only writes nothing", getattr(client, "sets", []), [])
check("read-only restores the combo", combo.currentData(), 1)
win.close()

# A poll refresh must not fight the operator: it only moves the combo when the
# device disagrees, and never while the dropdown is open.
client, win = make(state=0)
combo = win._value_widgets[0xA]
win._on_row(FakeRow({0x1: "7.23.3", 0x5: 0, 0xA: 2}))
check("a poll refresh adopts the device's channel", combo.currentData(), 2)

# --- a read-only enum stays a plain display ------------------------------
ro_menu = copy.deepcopy(synthetic)
ro_menu.fields[0xA].ro = True
client2 = FakeClient(0)
win2 = UpdateWindow(client2, menu=ro_menu, edit_session=FakeEdit())
_windows.append(win2)
win2.show()
app.processEvents()
ok("an ro enum is rendered disabled, not writable",
   not win2._value_widgets[0xA].isEnabled())
ok("an ro enum is not in the writable set", 0xA not in win2._writable_codes)
win2.close()
win.close()

# A poll reply that lands after close must be ignored. Acting on it used to
# re-subscribe on a window Qt was about to delete, starting a thread with no
# live parent, which aborts the whole process rather than raising.

client, win = make(state=2)
win.close()
before = getattr(client, "subscribed", 0)
win._on_row(FakeRow({0x1: "7.23.1", 0x5: 2, 0x3: "Downloaded 5%", 0xA: 1}))
check("a row arriving after close does not re-subscribe",
      getattr(client, "subscribed", 0), before)
win._tick()   # closeEvent cleared _poll_worker; a guarded tick leaves it clear
ok("a tick after close starts no new poll worker", win._poll_worker is None)

close_all()
app.processEvents()          # where the aborting deletion used to happen
ok("closed windows can be reaped without aborting the process", True)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all update-window tests passed")
