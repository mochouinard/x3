"""Undo / Redo: The router's own history, as Winbox v3's toolbar buttons.

No device and no pytest required:

    .venv/bin/python test_history_undo.py

The verbs are Safe Mode's -- [17] cmd 0x80001 undo, 0x80002 redo -- and they
work outside Safe Mode too: confirmed on a 7.23.3 virtual router, a comment changed then
undone came back, and redo put the change back. The same handler's GETALL is
System ▸ History, newest first, each row flagged U (undoable) or R (redoable).
"""

from x3.proto import message as m
from x3.proto.device import DeviceClient, DeviceError
from x3.proto.safemode import CMD_REDO, CMD_UNDO, SafeModeChannel

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


class Row:
    def __init__(self, state, action, when):
        self.values_by_code = {0x4: state}
        self._text = {0x3: action, 0x6: "admin", 0x7: when}

    def get_rendered(self, code, prefer_name=None):
        return self._text.get(code, "")


class Client(DeviceClient):
    """A DeviceClient with the history and the wire stubbed out."""

    def __init__(self, rows, *, readonly=False):
        super().__init__("192.0.2.1", "admin", "")
        self.readonly = readonly
        self._rows = rows
        self.sent: list[int] = []

    def getall(self, path, **kw):
        return list(self._rows)

    def _request(self, path, command, **kw):
        self.sent.append(command)
        return m.Message()


# Newest first, as the device lists it: two changes, the newest one undone.
history = [
    Row(1, "address changed", "05:34:41"),
    Row(0, "address changed", "05:34:40"),
    Row(0, "route added", "05:33:37"),
]
c = Client(history)
undo, redo = c.history_next()
check("the next undo is the newest undoable change", undo,
      "address changed (admin, 05:34:40)")
check("...and the next redo the change undo last took back", redo,
      "address changed (admin, 05:34:41)")
check("nothing in the history, nothing named",
      Client([]).history_next(), (None, None))

c.history_undo()
c.history_redo()
check("outside Safe Mode the verbs go straight to [17]", c.sent, [CMD_UNDO, CMD_REDO])

ro = Client(history, readonly=True)
try:
    ro.history_undo()
    refused = False
except DeviceError:
    refused = True
check("a read-only session cannot undo", (refused, ro.sent), (True, []))

# Under Safe Mode the same verbs move its buffer, and its count with it.
sm = Client(history)
sm._safe_mode = SafeModeChannel(sm._request, from_path=(0, 9))
sm._safe_mode.session_id = 7
sm._safe_mode.note_change(2)
sm.history_undo()
check("under Safe Mode an undo takes one off its pending count",
      sm.safe_mode_pending(), 1)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all history-undo tests passed")
