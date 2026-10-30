"""The doit-command form — System ▸ Password.

No device and no pytest required:

    .venv/bin/python test_command_dialog.py

A schema ``kind:'doit'`` is a one-shot action that takes arguments and returns
no result view, so neither the property form nor the query window fits it.
There was no third option: clicking one answered "Running it is a write
action, which this build does not do."

The fields are the schema's, not this dialog's. ``(13,1)`` cmd 3 declares
Old Password / New Password / Confirm Password in that declaration order, and
that is what the form asks for.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from x3.proto.schema import Schema
from x3.ui.widgets.command_dialog import CommandDialog

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


app = QApplication.instance() or QApplication(sys.argv)

shown: list[tuple] = []
QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(("warn", a[2])))
QMessageBox.information = staticmethod(lambda *a, **k: shown.append(("info", a[2])))


class StubClient:
    def __init__(self, fail: str | None = None):
        self.ran: list[tuple] = []
        self._fail = fail

    user = "admin"

    def default_command_target(self, path):
        # Mirrors DeviceClient: the logged-in user for the Users collection,
        # id 0 for a singleton menu (captured: IP ▸ Cloud Force Update sends
        # [123,1] cmd=0x1 with 0xFE0001 = 0).
        if tuple(path) == (13, 1):
            return 7
        return 0 if tuple(path) == (123, 1) else None

    # Same signature as DeviceClient.run_command: a command may carry no
    # fields at all, which is what a form action button sends.
    def run_command(self, path, command, values=None, *, obj_id=None):
        if self._fail:
            raise RuntimeError(self._fail)
        self.ran.append((path, command, values or {}, obj_id))


schema = Schema.load("7.21.1-151146055")
pwd_menu = next(
    m for m in schema.menus
    if m.path == (13, 1) and m.kind == "doit" and m.command == 3
)

OLD, NEW, CONFIRM = 0x1B, 0x03, 0x16


def dialog(client):
    return CommandDialog("Change Password", pwd_menu, client=client)


# --- what it asks for -----------------------------------------------------

d = dialog(StubClient())
labels = [pwd_menu.fields[c].name for c in d._editors]
check("it asks for old, new and confirm — in that order",
      labels, ["Old Password", "New Password", "Confirm Password"])
ok("every one of them is masked",
   all(e.echoMode() == QLineEdit.EchoMode.Password for e in d._editors.values()))

# --- a mismatched confirmation never reaches the router -------------------

client = StubClient()
d = dialog(client)
d._editors[OLD].setText("oldpw")
d._editors[NEW].setText("newpw")
d._editors[CONFIRM].setText("typo")
shown.clear()
d._run()
check("a mismatched confirmation is refused here", client.ran, [])
ok("...and says so", shown and "do not match" in shown[-1][1])
check("...leaving the dialog open to fix it", d.isVisible(), False)  # never shown

# --- the good path --------------------------------------------------------

client = StubClient()
d = dialog(client)
d._editors[OLD].setText("oldpw")
d._editors[NEW].setText("newpw")
d._editors[CONFIRM].setText("newpw")
shown.clear()
d._run()
check("a matching pair runs the command", len(client.ran), 1)
path, command, values, obj_id = client.ran[0]
check("...at the menu's own path", path, (13, 1))
check("...with the schema's verb", command, 3)
check("...carrying all three fields",
      values, {OLD: "oldpw", NEW: "newpw", CONFIRM: "newpw"})
# The Users map is a collection: a made-up id like 0 gets UNKNOWNID back,
# so the command has to name the account it is actually about.
check("...aimed at the logged-in user, not id 0", obj_id, 7)

# --- the router's own wording survives ------------------------------------

client = StubClient(fail="Bad old password!")
d = dialog(client)
d._editors[OLD].setText("wrong")
d._editors[NEW].setText("newpw")
d._editors[CONFIRM].setText("newpw")
shown.clear()
d._run()
ok("a refusal reaches the user unedited",
   shown and "Bad old password!" in shown[-1][1])

# --- blank fields are omitted, not sent as empty --------------------------

client = StubClient()
d = dialog(client)
d._editors[NEW].setText("newpw")
d._editors[CONFIRM].setText("newpw")
d._run()
check("an untouched field is left out", client.ran[0][2],
      {NEW: "newpw", CONFIRM: "newpw"})


# --- a singleton menu's doit targets object 0 -----------------------------
#
# Captured from Winbox: IP ▸ Cloud's Force Update is [123,1] cmd=0x1 carrying
# 0xFE0001 = 0. A singleton has one object and it is numbered 0 — unlike the
# Users collection, where 0 is nobody and the device answers UNKNOWNID.

cloud = next(
    m for m in schema.menus
    if m.path == (123, 1) and m.kind == "doit" and m.command == 1
)
client = StubClient()
d = CommandDialog("Force Update", cloud, client=client)
d._run()
check("a singleton command runs", len(client.ran), 1)
check("...against object 0", client.ran[0][3], 0)
check("...with the captured verb", client.ran[0][1], 1)


# --- the form's own action buttons run their command ----------------------
#
# A doit is a verb, not a field. The property form staged it as a pretend edit
# carrying the command number as a value, so the send path refused it with
# "no wire code for: Force Update" — the entry had no wire code because a
# command does not have one.

from x3.ui.edit_mode import EditMode, EditSession, QueuedChange


class ActionClient(StubClient):
    def default_command_target(self, path):
        return 0 if tuple(path) == (123, 1) else None


client = ActionClient()
session = EditSession(EditMode.LIVE, client=client)

session.apply(QueuedChange(
    path=(123, 1), title="Cloud — Force Update", row_key="", obj_id=None,
    values={"_doit": "Force Update", "_cmd": 1},
    action="doit",
))
check("a form action runs its command", len(client.ran), 1)
path, command, values, obj_id = client.ran[0]
check("...at the menu's path", path, (123, 1))
check("...with the schema's verb", command, 1)
check("...against the singleton's object 0", obj_id, 0)
check("...carrying no fields", values, {})

# A row-level action (Reset Counters on a rule) targets that row.
client = ActionClient()
session = EditSession(EditMode.LIVE, client=client)
session.apply(QueuedChange(
    path=(20, 3), title="Filter Rules — Reset Counters", row_key="4", obj_id=4,
    values={"_doit": "Reset Counters", "_cmd": 2},
    action="doit",
))
check("a row action targets that row", client.ran[0][3], 4)

# An action with no command is refused rather than sent as nonsense.
failed = None
try:
    session.apply(QueuedChange(
        path=(20, 3), title="x", row_key="", obj_id=1,
        values={"_doit": "x"}, action="doit",
    ))
except Exception as e:  # noqa: BLE001
    failed = str(e)
ok("an action with no command is refused", failed and "no command" in failed)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all command-dialog tests passed")
