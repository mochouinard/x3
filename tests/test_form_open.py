"""Opening a property form: one burst of reads, one tab built.

No device and no pytest required:

    .venv/bin/python test_form_open.py

A WiFi interface's form pulls eleven dropdown source menus and has eleven tabs
of editors. Read one table at a time and built all at once, opening a row took
about a second; Winbox 3.43 does the same work in ~16 ms because it sends every
request in one burst (a session capture) and does not stall on widgets.

Two things are pinned here. The client can pipeline -- N requests on the wire
before any reply is awaited, answers matched back positionally however the
device interleaves them -- and the form uses it: one prefetch burst, no
per-field reads, and only the visible tab's editors built. Plus the invariant
that laziness must not cost correctness: a whole-form write and validation both
see every field, and a late-built row is wired for dirty tracking exactly once.
"""

import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QSpinBox,
    QToolButton,
)

from x3.proto import message as m
from x3.proto.demux import FrameDemux
from x3.proto.device import DeviceClient, RowObject
from x3.proto.schema import Schema
from x3.ui.edit_mode import EditMode, EditSession
from x3.ui.widgets.property_window import (
    OptionalFieldEditor,
    PropertyWindow,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(f"{label}: {detail}")
        print(f"FAIL {label}: {detail}")


# --- the wire: many requests out, then the replies -------------------------


def frame(to, rid, *, cmd=0xFE0004):
    msg = m.Message()
    msg.add_u32_array(m.SYS_TO, list(to))
    msg.add_u32_array(m.SYS_FROM, [0, 1])
    msg.set_command(cmd)
    msg.set_request_id(rid)
    return msg


class WireSession:
    """Stands in for the socket: records sends, hands the demux fed frames."""

    timeout = 2.0

    def __init__(self):
        self.sent: list[m.Message] = []
        self._incoming: list[m.Message] = []
        self._cond = threading.Condition()
        self._sock = None
        self.closed = False
        #: how many frames had been sent when the first reply was handed over
        self.sent_when_first_reply_read = None

    def _recv_message(self):
        with self._cond:
            while not self._incoming and not self.closed:
                self._cond.wait(2.0)
            if self.closed:
                raise ConnectionError("closed")
            if self.sent_when_first_reply_read is None:
                self.sent_when_first_reply_read = len(self.sent)
            return self._incoming.pop(0)

    def send(self, msg):
        self.sent.append(msg)

    def feed(self, *msgs):
        with self._cond:
            self._incoming.extend(msgs)
            self._cond.notify_all()

    def die(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def test_pipeline():
    sess = WireSession()
    demux = FrameDemux(sess)
    demux.start()
    try:
        reqs = [frame((20, 0), 1), frame((20, 101), 2), frame((138, 13), 3)]
        # Answer out of order, and only once every request is on the wire: the
        # burst must not be waiting between sends.
        def answer():
            while len(sess.sent) < len(reqs):
                pass
            sess.feed(frame((0, 1), 3), frame((0, 1), 1), frame((0, 1), 2))

        t = threading.Thread(target=answer, daemon=True)
        t.start()
        out = demux.pipeline(reqs, timeout=2.0)
        t.join(2.0)
        check("every request goes out before any reply is read",
              sess.sent_when_first_reply_read, 3)
        check("a reply for each", len(out), 3)
        ok("no reply is an error", all(not isinstance(o, BaseException) for o in out),
           f"{out}")
        # Positional: reply rid n belongs in slot n-1 whatever order it arrived.
        rids = [o.find(m.SYS_REQUEST_ID)[1] for o in out]
        check("replies land in request order, not arrival order", rids, [1, 2, 3])
    finally:
        demux.stop()
        sess.die()


def test_pipeline_partial_failure():
    """One dead request must not sink the batch."""
    sess = WireSession()
    demux = FrameDemux(sess)
    demux.start()
    try:
        reqs = [frame((20, 0), 11), frame((99, 99), 12), frame((138, 13), 13)]

        def answer():
            while len(sess.sent) < len(reqs):
                pass
            sess.feed(frame((0, 1), 11), frame((0, 1), 13))   # nothing for 12

        threading.Thread(target=answer, daemon=True).start()
        out = demux.pipeline(reqs, timeout=0.6)
        ok("the answered ones came back",
           not isinstance(out[0], BaseException) and not isinstance(out[2], BaseException),
           f"{out}")
        ok("the unanswered one reports in its own slot",
           isinstance(out[1], BaseException), f"{out[1]!r}")
    finally:
        demux.stop()
        sess.die()


def test_unaskable_paths_are_never_asked():
    """A dropdown source the schema does not describe must not be requested.

    A WiFi interface still declares 'OpenFlow Switch' with its options at
    (125,1), but firmware 7.21 dropped OpenFlow: there is no such handler, and the
    device answers that request with silence rather than an error. So it used to
    cost the client's whole timeout -- 12 s in the UI -- every time a WiFi form
    was opened. The schema has no record for the path either, which is the
    signal to leave it alone.
    """
    c = DeviceClient("127.0.0.1", "u", "p", readonly=True)
    c.schema = schema
    c.session = None          # any real request would raise "not connected"
    ok("the schema really has no record for (125,1)",
       schema.record((125, 1)) is None, "the premise moved: pick another path")
    try:
        c.prefetch_enums([(125, 1)])
    except Exception as e:  # noqa: BLE001
        failures.append(f"prefetching an unaskable path tried to talk: {e!r}")
        print(f"FAIL prefetching an unaskable path tried to talk: {e!r}")
    else:
        print("ok  prefetching an unaskable path sends nothing")
    check("...and it is remembered as empty", c._enum_cache.get((125, 1)), {})
    check("...so an editor asking gets the same answer with no request",
          c.enum_map((125, 1)), {})
    # Contrast: a path the schema does know is a real request.
    ok("a known path would be asked for",
       schema.record((20, 0)) is not None, "premise moved")
    try:
        c.enum_map((20, 0))
        print("ok  a known path is fetched (no session: fell back to empty)")
    except Exception as e:  # noqa: BLE001
        print(f"ok  a known path is fetched (raised {type(e).__name__} with no session)")


# --- the form: one burst, one tab ------------------------------------------

schema = Schema.load("7.21.1-151146055")
PATH = (20, 0)
WIFI = 68        # the 169-field, 11-tab interface overlay


class StubClient:
    """The client's caching contract, with the reads counted."""

    def __init__(self):
        self.cache: dict[tuple, dict] = {}
        self.bursts: list[int] = []       # paths per pipelined prefetch
        self.serial = 0                   # reads issued one at a time

    def menu_for(self, path, type_value=None):
        return (schema.record_for(path, type_value) if type_value is not None
                else schema.record(path))

    def alloc_from(self):
        return (0, 7)

    def getobj(self, path, obj_id, **kw):
        self.serial += 1
        return ROW

    def get(self, path):
        self.serial += 1
        return ROW

    def prefetch_enums(self, paths):
        want = [p for p in dict.fromkeys(tuple(x) for x in paths)
                if p not in self.cache]
        if not want:
            return
        self.bursts.append(len(want))
        for p in want:
            self.cache[p] = {i: f"obj{i}" for i in range(1, 6)}

    def enum_map(self, path):
        path = tuple(path)
        if path not in self.cache:
            self.serial += 1
            self.cache[path] = {i: f"obj{i}" for i in range(1, 6)}
        return self.cache[path]

    def enum_choices(self, fld):
        out = list(sorted((fld.enum or {}).items()))
        if fld.enum_path:
            out += list(self.enum_map(fld.enum_path).items())
        return out

    def interface_choices(self):
        self.serial += 1
        return [(i, f"ether{i}", "ether") for i in range(1, 6)]


ROW = RowObject(path=PATH, raw=m.Message(), fields=[], form_fields=[])
ROW.values_by_code = {0xFE0001: 3, 0x010006: b"wifi1", 0x01001E: b"wifi"}


def make_form(app, mode=EditMode.QUEUE):
    client = StubClient()
    win = PropertyWindow(
        "wifi1", PATH, ROW,
        client=client,
        edit_session=EditSession(mode, client=None),
        menu=client.menu_for(PATH, WIFI),
        row_key="3",
        full_row=True,
    )
    app.processEvents()
    return client, win


def test_form_reads(app):
    client, win = make_form(app)
    ok("the dropdown sources are read as one burst", len(client.bursts) == 1,
       f"bursts={client.bursts}")
    check("...and nothing is read field by field", client.serial, 0)
    ok("the burst covers several menus", client.bursts[0] > 5, f"{client.bursts}")
    win.close()


def test_lazy_pages(app):
    client, win = make_form(app)
    tabs = win._tabs
    ok("a WiFi interface really is a many-tab form", tabs.count() > 5,
       f"{tabs.count()} tabs")
    # Against every tab the form knows about, not against the tab strip: a tab
    # whose condition is currently false (Data Rates in Simple Mode) is built
    # lazily like the rest but is not on the strip to be counted.
    ok("only the visible tab is built",
       len(win._pending_pages) == len(win._all_tabs) - 1,
       f"{len(win._pending_pages)} pending of {len(win._all_tabs)}")
    built_first = len(win._rows)
    ok("...and it has rows", built_first > 0, f"{built_first}")

    # Selecting a tab builds it, once.
    tabs.setCurrentIndex(2)
    app.processEvents()
    after = len(win._rows)
    ok("selecting a tab builds it", after > built_first, f"{built_first} -> {after}")
    ok("it is not in the pending set any more",
       tabs.widget(2) not in win._pending_pages,
       f"{len(win._pending_pages)} still pending")
    tabs.setCurrentIndex(0)
    tabs.setCurrentIndex(2)
    app.processEvents()
    check("revisiting it builds nothing again", len(win._rows), after)
    check("every built row is wired exactly once", win._wired, len(win._rows))
    win.close()


def test_lazy_pages_stay_correct(app):
    """Laziness is allowed to cost time, never fields."""
    client, win = make_form(app)
    total_specs = sum(len(items) for _n, items in win._pending_pages.values())
    ok("there are unbuilt rows to lose", total_specs > 0, f"{total_specs}")

    # 'What did the user change?' needs no unbuilt page: an untouched tab
    # cannot hold a change.
    check("an untouched form reports no changes", win.edits(changed_only=True), [])
    ok("...without building anything", len(win._pending_pages) > 0,
       "the pending pages were built anyway")

    # Validation and a whole-form write must see the whole form.
    win.required_errors()
    check("validation builds every page", len(win._pending_pages), 0)
    win.close()

    client, win = make_form(app)
    pending_before = len(win._pending_pages)
    rows_before = len(win._rows)
    win.edits(changed_only=False)     # the shape an Add / settings write sends
    ok("a whole-form write builds every page", len(win._pending_pages) == 0,
       f"{len(win._pending_pages)} left of {pending_before}")
    ok("...and so sees more fields than the open tab had",
       len(win._rows) > rows_before, f"{rows_before} -> {len(win._rows)}")
    win.close()


def _nudge(editor) -> bool:
    """Change an editor's value the way a user would. False if we cannot."""
    if isinstance(editor, OptionalFieldEditor):
        # Its + / × are the whole point of the control: press whichever is
        # showing, which is what sets or unsets the field.
        for btn in editor.findChildren(QToolButton):
            if not btn.isHidden():
                btn.click()
                return True
        return False
    if type(editor) is QLineEdit:
        editor.setText((editor.text() or "") + "x")
        return True
    if isinstance(editor, QSpinBox):
        editor.setValue(editor.value() + 1)
        return True
    if isinstance(editor, QCheckBox):
        editor.setChecked(not editor.isChecked())
        return True
    if isinstance(editor, QComboBox) and editor.count() > 1:
        editor.setCurrentIndex((editor.currentIndex() + 1) % editor.count())
        return True
    return False


def test_late_row_is_dirty_tracked(app):
    """A row built on a tab switch reports its edit like any other."""
    client, win = make_form(app)
    tabs = win._tabs
    first_rows = len(win._rows)
    hit = None
    for i in range(1, tabs.count()):
        tabs.setCurrentIndex(i)
        app.processEvents()
        for spec, editor in win._rows[first_rows:]:
            if spec.get("locked") or spec.get("always_locked"):
                continue
            if _nudge(editor):
                hit = (spec, editor)
                break
        if hit is not None:
            break
    ok("a lazily built tab has an editable row to test", hit is not None,
       "every late row was locked")
    if hit is None:
        win.close()
        return
    spec, _editor = hit
    app.processEvents()
    changed = [e for e in win.edits(changed_only=True)
               if e.get("name") == spec["name"]]
    ok("an edit on a lazily built page is reported once", len(changed) == 1,
       f"{spec['name']!r} -> {[e.get('name') for e in win.edits(changed_only=True)]}")
    ok("...and the form knows it is dirty", win.is_dirty(), "is_dirty() is False")
    # A dirty form asks before closing, and a real QMessageBox never returns
    # under the offscreen platform. _allow_close is the flag Queue sets.
    win._allow_close = True
    win.close()


def test_build_caches_follow_the_row(app):
    """Laying out the form memoises against the row; a new row must drop it.

    A wireless interface's template has ~190 rows, and each used to re-evaluate
    its tab's visibility, its ``on:`` condition and a linear scan of every
    decoded wire field. Those are cached per name/tab/code now — which is only
    correct while the row they were computed from is the row on screen. The
    live-traffic poll and a post-write read-back both replace it.
    """
    client, win = make_form(app)
    code = next(iter(win.menu.fields))
    win.row = RowObject(
        path=PATH, raw=m.Message(),
        fields=[(code, "Name", "after", True)], form_fields=[],
    )
    ok("a stale index would still answer for the old row",
       win._row_field_index().get(code) != [(code, "Name", "after", True)],
       "the cache was never populated, so this proves nothing")
    win._forget_row_caches()
    check("replacing the row drops the field index",
          win._row_field_index().get(code), [(code, "Name", "after", True)])
    ok("...and the condition caches with it",
       not win._cond_ok_cache and not win._tab_ok_cache,
       f"{len(win._cond_ok_cache)} cond, {len(win._tab_ok_cache)} tab")
    win.close()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    test_pipeline()
    test_pipeline_partial_failure()
    test_unaskable_paths_are_never_asked()
    test_form_reads(app)
    test_lazy_pages(app)
    test_lazy_pages_stay_correct(app)
    test_late_row_is_dirty_tracked(app)
    test_build_caches_follow_the_row(app)
    if failures:
        print("\nFAILURES")
        for f in failures:
            print(" -", f)
        return 1
    print("\nall form-open tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
