"""Offline tests for the UI edit modes and what Live mode actually sends.

No device and no pytest required:

    .venv/bin/python test_edit_mode.py

EditSession is the seam between a form/list action and the wire: it decides
whether a change is held locally or handed to DeviceClient, and which verb it
becomes. A fake client records the calls, so nothing is sent.

The guards matter as much as the mapping. A live write is refused unless Live
is the selected mode, a client exists, and the window's guard (Safe Mode)
allows it — and a change that cannot name its object, or its fields' wire
codes, is refused rather than sent as a guess.
"""

from x3.ui.edit_mode import EditMode, EditSession, QueuedChange

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def refused(label, fn, *, because=""):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        if because and because not in str(e):
            failures.append(f"{label}: refused with {e!r}, wanted {because!r}")
        else:
            print(f"ok  {label}")
        return
    failures.append(f"{label}: was allowed through")


class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def set_obj(self, path, obj_id, values, **kw):
        self.calls.append(("set", path, obj_id, values))
        return None

    def add_obj(self, path, values, **kw):
        self.calls.append(("add", path, values))
        return 25, None

    def remove_obj(self, path, obj_id, **kw):
        self.calls.append(("remove", path, obj_id))

    def move_obj(self, path, obj_id, *, before=None, **kw):
        self.calls.append(("move", path, obj_id, before))


ADDRESS = (20, 1)


def session(mode=EditMode.LIVE, *, guard=None, client=None):
    return EditSession(mode, client=client if client is not None else FakeClient(),
                       write_guard=guard)


def change(action="set", **kw):
    base = dict(path=ADDRESS, title="Address", row_key="192.168.88.1", obj_id=9)
    base.update(kw)
    return QueuedChange(action=action, **base)


# --- modes ----------------------------------------------------------------

check("read-only is read-only", EditSession(EditMode.READONLY).readonly, True)
check("queue is not live", EditSession(EditMode.QUEUE).live, False)
check("a session with no client cannot write",
      EditSession(EditMode.LIVE).write_supported, False)
check("a session with a client can", session().write_supported, True)
check("the label stops apologising once a write path exists",
      session().label(), "LIVE")
check("...and still says so when there is none",
      EditSession(EditMode.LIVE).label(), "LIVE (no write path)")

# --- the guards -----------------------------------------------------------

refused("queue mode refuses to send",
        lambda: session(EditMode.QUEUE).apply(change()),
        because="Live mode is not selected")
refused("a disconnected session refuses to send",
        lambda: EditSession(EditMode.LIVE).apply(change()),
        because="no device connection")
refused("the window's guard can veto (Safe Mode off)",
        lambda: session(guard=lambda: "Safe Mode is off.").apply(change()),
        because="Safe Mode is off")
check("a guard that allows says nothing",
      session(guard=lambda: None).blocked_reason(), None)

# --- set ------------------------------------------------------------------

s = session()
msg = s.apply(change(edits=[{"code": 0xFE0009, "name": "Comment", "new": "hi"}]))
check("a field edit becomes a SET",
      s.client.calls, [("set", ADDRESS, 9, {0xFE0009: "hi"})])
check("and reports what it did", msg.startswith("Wrote 1 field"), True)

# An edit that only ever had a display name cannot be sent: guessing the code
# would write the wrong field.
refused("an edit with no wire code is refused, not guessed",
        lambda: session().apply(change(edits=[{"name": "Comment", "new": "hi"}])),
        because="no wire code for: Comment")
refused("a row with no object id cannot be edited",
        lambda: session().apply(
            change(obj_id=None, edits=[{"code": 0xFE0009, "new": "x"}])),
        because="no object id")

# --- enable / disable / comment map to SET without needing edits ----------

s = session()
s.apply(change("disable"))
check("disable sets the standard flag",
      s.client.calls, [("set", ADDRESS, 9, {0xFE000A: True})])

s = session()
s.apply(change("enable"))
check("enable clears it", s.client.calls, [("set", ADDRESS, 9, {0xFE000A: False})])

s = session()
s.apply(change("comment", values={"Comment": "note"}))
check("comment writes STD_DESCR",
      s.client.calls, [("set", ADDRESS, 9, {0xFE0009: "note"})])

# --- remove / add / move --------------------------------------------------

s = session()
s.apply(change("remove"))
check("remove is keyed on the id alone",
      s.client.calls, [("remove", ADDRESS, 9)])
refused("remove without an id is refused",
        lambda: session().apply(change("remove", obj_id=None)),
        because="no object id")

s = session()
msg = s.apply(change("add", obj_id=None, edits=[
    {"code": 0x01, "name": "Address", "new": "192.168.234.1"},
]))
check("add sends the field set", s.client.calls,
      [("add", ADDRESS, {0x01: "192.168.234.1"})])
check("and reports the id the device allocated", msg, "Added object .id 25")
refused("an add with nothing to say is refused",
        lambda: session().apply(change("add", obj_id=None, edits=[])),
        because="nothing to add")

s = session()
s.apply(change("move", values={"_before": 11}))
check("move carries the anchor", s.client.calls, [("move", ADDRESS, 9, 11)])
s = session()
s.apply(change("move"))
check("move to the end has none", s.client.calls, [("move", ADDRESS, 9, None)])

# --- the queue still behaves --------------------------------------------

q = EditSession(EditMode.QUEUE)
q.enqueue(change(row_key="a"))
q.enqueue(change(row_key="b"))
check("two rows, two entries", len(q.queue), 2)
q.enqueue(change(row_key="a", title="again"))
check("re-editing a row replaces its entry", len(q.queue), 2)
check("and the newest wins", q.queue[-1].title, "again")
check("the label counts them", q.label(), "QUEUE (2)")
q.clear()

# Keyed only on path + row key, the queue used to drop real work: every new
# object is "(new)", and a Disable replaced the Comment queued on its row.
q.enqueue(change("add", row_key="(new)", obj_id=None, title="10.99.0.1/24"))
q.enqueue(change("add", row_key="(new)", obj_id=None, title="10.98.0.1/24"))
check("two Adds in one list are two objects", len(q.queue), 2)
q.clear()
q.enqueue(change("comment", values={"Comment": "uplink"}))
q.enqueue(change("disable"))
check("a comment and a disable on one row are both kept",
      [c.action for c in q.queue], ["comment", "disable"])
q.enqueue(change("enable"))
check("...while enable and disable replace each other",
      [c.action for c in q.queue], ["comment", "enable"])
q.clear()
q.enqueue(change(edits=[{"code": 1, "name": "Address", "old": "a", "new": "b"}]))
q.enqueue(change(edits=[{"code": 1, "name": "Address", "old": "b", "new": "c"},
                        {"code": 4, "name": "Interface", "old": "x", "new": "y"}]))
merged = q.queue[0]
check("two edits of one object merge into one change", len(q.queue), 1)
check("...holding both fields, the newer value winning",
      sorted((e["code"], e["new"]) for e in merged.edits), [(1, "c"), (4, "y")])
check("...and the first 'old', so the queue still says what it was",
      next(e["old"] for e in merged.edits if e["code"] == 1), "a")
q.clear()
q.enqueue(change("add", row_key="(new)", obj_id=None, origin="form-1", title="first"))
q.enqueue(change("add", row_key="(new)", obj_id=None, origin="form-1", title="again"))
check("a re-save from the same form replaces its own entry",
      [c.title for c in q.queue], ["again"])
q.clear()
check("clearing empties it", q.label(), "QUEUE")

# --- Apply all: the queue's one exit -------------------------------------


class GuardedClient(FakeClient):
    """A FakeClient carrying the read-only flag the real one enforces.

    Queue mode holds the client read-only, so a flush that did not lift the
    flag would be refused by the layer below rather than reaching the wire --
    and one that never put it back would leave the session writable.
    """

    def __init__(self, refuse_at=None):
        super().__init__()
        self.readonly = True
        #: Call index to answer FAILED on, the way the router refuses a rule.
        self.refuse_at = refuse_at
        self.wrote_while_readonly = False

    def _gate(self):
        if self.readonly:
            self.wrote_while_readonly = True
        if self.refuse_at is not None and len(self.calls) == self.refuse_at:
            raise RuntimeError("cannot add: already have such entry")

    def set_obj(self, path, obj_id, values, **kw):
        self._gate()
        return super().set_obj(path, obj_id, values, **kw)

    def add_obj(self, path, values, **kw):
        self._gate()
        return super().add_obj(path, values, **kw)

    def remove_obj(self, path, obj_id, **kw):
        self._gate()
        return super().remove_obj(path, obj_id, **kw)


def addr(row_key, value):
    return change("set", row_key=row_key,
                  edits=[{"code": 0x01, "name": "Address", "new": value}])


f = session(EditMode.QUEUE, client=GuardedClient())
f.enqueue(addr("a", "10.0.0.1"))
f.enqueue(change("remove", row_key="b", obj_id=11))
landed, failed = f.flush()
check("apply all sends every queued change, oldest first",
      [c[0] for c in f.client.calls], ["set", "remove"])
check("...and empties the queue behind it", f.queue, [])
check("...with nothing refused", failed, None)
check("...and hands back a summary per change", len(landed), 2)
check("...paired with the change it describes",
      [c.row_key for c, _ in landed], ["a", "b"])
check("the read-only flag comes off for the flush and goes back after",
      f.client.readonly is True and not f.client.wrote_while_readonly, True)

# A queue is usually a chain -- add a list, then a rule naming it -- so a
# refusal stops the flush rather than writing changes whose premise never
# landed. What failed stays at the head, so Apply all resumes from there.
f = session(EditMode.QUEUE, client=GuardedClient(refuse_at=1))
for key, value in (("a", "10.0.0.1"), ("b", "10.0.0.2"), ("c", "10.0.0.3")):
    f.enqueue(addr(key, value))
landed, failed = f.flush()
check("a refusal stops the flush at that change", len(landed), 1)
check("...and only what preceded it reached the wire", len(f.client.calls), 1)
check("...reporting the router's own wording",
      failed[1], "cannot add: already have such entry")
check("...with that change still at the head of the queue",
      [c.row_key for c in f.queue], ["b", "c"])
check("...and the flag still went back", f.client.readonly, True)

f = session(EditMode.LIVE, client=GuardedClient())
f.client.readonly = False
f.enqueue(change("remove", obj_id=3))
landed, failed = f.flush()
check("a queue left over from Queue mode flushes in Live mode too",
      (len(landed), failed), (1, None))

# The same guards Live writes answer to, minus the one Apply all exists to
# bypass: the mode check.
refused("an empty queue has nothing to flush",
        lambda: session(EditMode.QUEUE, client=GuardedClient()).flush(),
        because="queue is empty")
f = session(EditMode.READONLY, client=GuardedClient())
f.enqueue(addr("a", "10.0.0.1"))
refused("read-only refuses to flush", f.flush,
        because="Read-only mode is selected")
check("...and says so before the wire, not after", f.client.calls, [])
f = EditSession(EditMode.QUEUE)
f.enqueue(addr("a", "10.0.0.1"))
refused("a disconnected session cannot flush", f.flush,
        because="no device connection")
f = session(EditMode.QUEUE, client=GuardedClient(),
            guard=lambda: "Safe Mode is off.")
f.enqueue(addr("a", "10.0.0.1"))
refused("the window's guard vetoes apply all too", f.flush,
        because="Safe Mode is off")

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all edit-mode tests passed")
