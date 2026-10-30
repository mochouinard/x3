"""Saving twice from a New form must not create two objects.

No device and no pytest required:

    .venv/bin/python test_add_then_edit.py

A New form has no object id, so its save is an ADDOBJ. Nothing adopted the id
the router handed back, so the form stayed a New form: every later Apply sent
another ADDOBJ and the firewall grew a duplicate rule each time. The editors'
baselines were never re-seeded either, so the form also still counted as
modified and warned about pending changes on close.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from x3.ui.edit_mode import EditMode, EditSession, QueuedChange

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


class StubClient:
    """Records the verb used for each write."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.next_id = 11

    def add_obj(self, path, values):
        self.calls.append(("add", path, values))
        oid, self.next_id = self.next_id, self.next_id + 1
        return oid, None

    def set_obj(self, path, obj_id, values, **kw):
        self.calls.append(("set", path, obj_id, values))
        return None


CHAIN = 0x27


def change(action, obj_id, port):
    return QueuedChange(
        path=(20, 3), title="Filter Rules", row_key="", obj_id=obj_id,
        edits=[{"code": CHAIN, "name": "Chain", "new": "input"},
               {"code": 0x63, "name": "Dst. Port", "new": port}],
        action=action,
    )


client = StubClient()
session = EditSession(EditMode.LIVE, client=client)

# --- the Add reports the id it was given ---------------------------------

first = change("add", None, "41001")
session.apply(first)
check("the first save adds", client.calls[0][0], "add")
check("...and the new id comes back on the change", first.obj_id, 11)

# --- which is what makes the next save an edit ---------------------------
#
# The form asks _object_id() again; adopting 11 turns action into "set".

second = change("set", first.obj_id, "41002")
session.apply(second)
check("the second save edits", client.calls[1][0], "set")
check("...the object the add created", client.calls[1][2], 11)
check("only one object was ever added",
      [c[0] for c in client.calls].count("add"), 1)

# --- without adoption it would add again ---------------------------------

again = change("add", None, "41003")
session.apply(again)
check("an add with no id really does create another",
      [c[0] for c in client.calls].count("add"), 2)
check("...with its own fresh id", again.obj_id, 12)

# --- the form-side link: an adopted id wins over the row's ----------------
#
# This is where the bug actually sat. The session above was always willing to
# send a SET; the form kept asking as if it were still a New form.

from x3.ui.widgets.property_window import PropertyWindow

object_id = PropertyWindow._object_id


class FakeRow:
    def __init__(self, oid):
        self._oid = oid

    def object_id(self):
        return self._oid


class FakeForm:
    def __init__(self, row=None, adopted=None):
        self.row = row
        self._added_obj_id = adopted


check("a New form has no id, so it adds", object_id(FakeForm()), None)
check("an existing row reports its own id", object_id(FakeForm(FakeRow(4))), 4)
check("an adopted id turns a New form into an edit",
      object_id(FakeForm(None, 11)), 11)
check("...and takes precedence over a stale row",
      object_id(FakeForm(FakeRow(4), 11)), 11)

# --- and the same link when Apply all sends from the toolbar --------------
#
# A queued Add flushed from the main window is the same duplicate waiting to
# happen: the form that built it never saw the write, so it still has no id.
# owns_queued is identity-keyed because two New forms on one menu share both
# path and row_key -- the wrong one adopting the id is how the *other* goes on
# to add a second object.

owns = PropertyWindow.owns_queued
sync = PropertyWindow.sync_after_external_write


class FakeQueuer:
    def __init__(self, queued):
        self._queued_changes = list(queued)
        self.adopted = None
        self.resynced = "not called"

    def _adopt_new_object(self, obj_id):
        self.adopted = obj_id

    def _resync_after_write(self, obj_id):
        self.resynced = obj_id


mine, theirs = change("add", None, "41010"), change("add", None, "41011")
form = FakeQueuer([mine])
check("a form knows the change it queued", owns(form, mine), True)
check("...and disowns one it did not", owns(form, theirs), False)

mine.obj_id = 31
sync(form, mine)
check("a flushed add hands the new id to the form that queued it",
      form.adopted, 31)
check("...and drops the change, so a second flush cannot re-adopt",
      owns(form, mine), False)

edit = change("set", 31, "41012")
form = FakeQueuer([edit])
sync(form, edit)
check("a flushed edit only re-seeds the baselines", form.resynced, 31)
check("...and adopts nothing", form.adopted, None)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all add-then-edit tests passed")
