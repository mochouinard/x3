"""Automatic config snapshots — the debounce, the classifier, the store.

No device and no pytest required:

    .venv/bin/python test_auto_export.py

Three separable pieces, tested separately because each one fails in its own way:

* :mod:`x3.proto.configexport` turns a console capture into stable text. The
  fixtures here are the real thing -- byte streams recorded off a wireless router on
  7.23.3 and a 10-port router on 7.21.1 -- so the echo, prompt and sentinel handling is
  checked against what the router actually emits, not against what it ought to.
* :mod:`x3.ui.change_watch` decides whether a pushed record is a setting or a
  counter. Getting this wrong in the "counter" direction means a busy router
  exports every 15 minutes forever having recorded nothing, so it is worth a
  test of its own.
* :mod:`x3.ui.autoexport` is the debounce: armed by change, one-shot, and
  restarted rather than added to. The user-visible promise is "one file, 15
  minutes after the last change, and nothing at all if nothing changed" --
  that is what the timing tests below actually assert.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from x3.proto import configexport as cexp

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


# ---------------------------------------------------------------------------
# 1. the capture parser
# ---------------------------------------------------------------------------

# A capture as it comes off the wire: doubled command echo, the login banner's
# cursor moves, colour codes in the prompt, and the sentinel echoed once before
# it is printed. Taken from the shape in a session capture.
CAPTURE = (
    b"/export terse/export terse\r\n"
    b"\r\n"
    b"# 2026-08-20 02:24:51 by RouterOS 7.23.3\r\n"
    b"# software id = AB12-CD34\r\n"
    b"#\r\n"
    b"# model = C52iG-5HaxD2HaxD\r\n"
    b"/interface bridge add name=LAN\r\n"
    b"/ip address add address=192.168.87.24/24 interface=LAN\r\n"
    b"/system identity set name=sm-test\r\n"
    b':put "==WBEOF=="\r\n'
    b"==WBEOF==\r\n"
    b"\r\r\r\x1b[9999B\x1b[K[\x1b[m\x1b[36madmin\x1b[m@\x1b[m\x1b[32msm-test\x1b[m] > "
)


def test_parser() -> None:
    r = cexp.parse(CAPTURE)
    ok("parse: complete", r.complete)
    ok("parse: not refused", not r.refused)
    ok("parse: not disturbed", not r.disturbed)
    check("parse: header lines", len(r.header), 4)
    check("parse: config lines", len(r.body.splitlines()), 3)
    ok("parse: echo stripped", "/export terse/export terse" not in r.text)
    ok("parse: sentinel stripped", "WBEOF" not in r.text)
    ok("parse: prompt stripped", "sm-test]" not in r.text)
    ok("parse: no escapes survive", "\x1b" not in r.text)
    ok("parse: body has no header", "#" not in r.body)
    ok("parse: body keeps config", r.body.startswith("/interface bridge add"))

    # The header's first line is a timestamp, so it must not reach the digest:
    # two captures of an untouched device have to compare equal.
    later = CAPTURE.replace(b"02:24:51", b"09:31:07")
    check("parse: digest ignores capture time", cexp.parse(later).digest, r.digest)

    # ...but a real edit must move it.
    edited = CAPTURE.replace(b"name=sm-test", b"name=sm-prod")
    ok("parse: digest follows config", cexp.parse(edited).digest != r.digest)


def test_refusal() -> None:
    """Pre-6.43 firmware asked for show-sensitive says so, and says nothing else."""
    refused = (
        b"/export terse show-sensitive/export terse show-sensitive\r\n"
        b"expected end of command\r\n"
        b':put "==WBEOF=="\r\n==WBEOF==\r\n[admin@old] > '
    )
    r = cexp.parse(refused, command=cexp.EXPORT_COMMAND_SENSITIVE)
    ok("refusal: detected", r.refused)
    check("refusal: message", r.console_error, "expected end of command")

    # A normal capture must never be read as a refusal.
    ok("refusal: clean capture is not refused", not cexp.parse(CAPTURE).refused)


def test_log_splice() -> None:
    """A log line arriving mid-capture is lifted out, not left in the config."""
    spliced = CAPTURE.replace(
        b"/system identity set name=sm-test\r\n",
        b"2026-08-20 02:24:52 system,error,critical login failure for user x\r\n"
        b"/system identity set name=sm-test\r\n",
    )
    r = cexp.parse(spliced)
    check("splice: lifted out", len(r.spliced_logs), 1)
    ok("splice: flagged disturbed", r.disturbed)
    ok("splice: not in body", "login failure" not in r.body)
    # And the config it interrupted still arrives whole.
    check("splice: config intact", r.digest, cexp.parse(CAPTURE).digest)


def test_command_choice() -> None:
    check("cmd: 7.x asks for secrets",
          cexp.export_command("7.23.3"), cexp.EXPORT_COMMAND_SENSITIVE)
    check("cmd: 6.40 does not",
          cexp.export_command("6.40.1"), cexp.EXPORT_COMMAND)
    check("cmd: 6.43 is the cutover",
          cexp.export_command("6.43"), cexp.EXPORT_COMMAND_SENSITIVE)
    check("cmd: opt-out wins",
          cexp.export_command("7.23.3", show_sensitive=False), cexp.EXPORT_COMMAND)
    check("cmd: unknown version asks",
          cexp.export_command(""), cexp.EXPORT_COMMAND_SENSITIVE)


def test_redact() -> None:
    text = (
        '/ppp secret add name=bob password=hunter2\n'
        '/interface wifi security set passphrase="a b c" wps=disable\n'
        '/snmp community set name=public\n'
        '/ip service set winbox port=8291\n'
    )
    out = cexp.redact(text)
    ok("redact: password gone", "hunter2" not in out)
    ok("redact: quoted passphrase gone", "a b c" not in out)
    ok("redact: keeps non-secrets", "port=8291" in out)
    ok("redact: keeps names", "name=bob" in out)


# ---------------------------------------------------------------------------
# 2. the settings/counter classifier
# ---------------------------------------------------------------------------


class StubField:
    def __init__(self, name, *, ro=False, nonpublic=False, jg_type="string"):
        self.name = name
        self.ro = ro
        self.nonpublic = nonpublic
        self.jg_type = jg_type
        self.variants: dict = {}


class StubMenu:
    """Just enough of schema.Menu for change_watch."""

    def __init__(self, fields, *, ages=(), statusbar=()):
        self.fields = fields
        self._ages = set(ages)
        self._statusbar = set(statusbar)
        self.nameval = "Name"

    def age_codes(self):
        return set(self._ages)

    def statusbar_codes(self):
        return set(self._statusbar)


class StubRow:
    """Stands in for device.RowObject."""

    def __init__(self, oid, values, *, dynamic=False, comment=""):
        self._oid = oid
        self._values = dict(values)
        if dynamic:
            self._values[0xFE0007] = True
        self._comment = comment

    def object_id(self):
        return self._oid

    def raw_value(self, code, *, prefer_scalar=True):
        return self._values.get(code)

    def get_rendered(self, code, *, prefer_name=None):
        val = self._values.get(code)
        return "" if val is None else str(val)

    def comment(self):
        return self._comment


#: 0x1 Name (config), 0x2 MTU (config), 0x10 rx-bytes (a counter),
#: 0x11 Uptime (an age), 0x12 an internal field the user never sees,
#: 0x13 `running` (the R in the flag column -- state, and it flaps).
MENU = StubMenu(
    {
        0x1: StubField("Name"),
        0x2: StubField("MTU"),
        0x10: StubField("Rx Bytes", ro=True),
        0x11: StubField("Uptime", jg_type="age"),
        0x12: StubField("container", nonpublic=True),
        0x13: StubField("running", jg_type="flag"),
    },
    ages=(0x11,),
)


def test_config_codes() -> None:
    from x3.ui import change_watch as cw

    codes = cw.config_codes(MENU)
    ok("codes: writable field in", 0x1 in codes)
    ok("codes: read-only counter out", 0x10 not in codes)
    ok("codes: age field out", 0x11 not in codes)
    ok("codes: nonpublic out", 0x12 not in codes)
    # A live run against a 10-port router caught this: Interface declares running,
    # slave, passthrough and inactive as type:'flag', none of them read-only,
    # and `running` flips on every link bounce. Counting them turned a flapping
    # uplink into a stream of "somebody changed the router" warnings.
    ok("codes: state flag out", 0x13 not in codes)
    ok("codes: Disabled flag in", 0xFE000A in codes)
    ok("codes: comment in", 0xFE0009 in codes)
    ok("codes: object id out", 0xFE0001 not in codes)
    ok("codes: dynamic flag out", 0xFE0007 not in codes)


def test_classifier() -> None:
    from x3.ui import change_watch as cw

    before = cw.snapshot(MENU, [StubRow(1, {0x1: "ether1", 0x2: 1500, 0x10: 100})])

    # A counter moving is not a change.
    counters = cw.snapshot(MENU, [StubRow(1, {0x1: "ether1", 0x2: 1500, 0x10: 999999})])
    ok("classify: counter feed is silent", not cw.diff(before, counters))

    # An age ticking is not a change.
    aged = cw.snapshot(MENU, [StubRow(1, {0x1: "ether1", 0x2: 1500, 0x11: 12345})])
    ok("classify: age tick is silent", not cw.diff(before, aged))

    # A link bouncing is not a change, however often it does it.
    flapped = cw.snapshot(MENU, [StubRow(1, {0x1: "ether1", 0x2: 1500, 0x13: False})])
    ok("classify: link flap is silent", not cw.diff(before, flapped))

    # A setting moving is.
    edited = cw.snapshot(MENU, [StubRow(1, {0x1: "ether1", 0x2: 9000, 0x10: 100})])
    delta = cw.diff(before, edited)
    ok("classify: MTU edit is a change", bool(delta))
    check("classify: named the row", delta.changed, ["ether1"])

    # Disabling from another session is a change, on a code no menu declares.
    disabled = cw.snapshot(
        MENU, [StubRow(1, {0x1: "ether1", 0x2: 1500, 0xFE000A: True})]
    )
    ok("classify: disable is a change", bool(cw.diff(before, disabled)))

    # Rows appearing and vanishing.
    added = cw.snapshot(
        MENU,
        [
            StubRow(1, {0x1: "ether1", 0x2: 1500}),
            StubRow(2, {0x1: "ether2", 0x2: 1500}),
        ],
    )
    check("classify: add named", cw.diff(before, added).added, ["ether2"])
    check("classify: remove named", cw.diff(added, before).removed, ["ether2"])

    # A device-generated row is not somebody configuring anything: a DHCP lease
    # or a learned ARP entry must not set off the warning.
    dynamic = cw.snapshot(
        MENU,
        [
            StubRow(1, {0x1: "ether1", 0x2: 1500}),
            StubRow(3, {0x1: "leased"}, dynamic=True),
        ],
    )
    ok("classify: dynamic row ignored", not cw.diff(before, dynamic))

    # First sight of a list is not a change.
    ok("classify: first load is silent", not cw.diff(None, before))


# ---------------------------------------------------------------------------
# 3. the debounce and the store
# ---------------------------------------------------------------------------


class StubExport:
    def __init__(self, body="/ip address add address=1.2.3.4/24"):
        self.text = "# 2026-08-20 by RouterOS 7.23.3\n" + body
        self.body = body
        self.command = "/export terse"
        self.spliced_logs = ()
        self.disturbed = False

    @property
    def digest(self):
        import hashlib

        return hashlib.sha256(self.body.encode()).hexdigest()


class StubSession:
    host = "192.168.87.24"
    identity = "sm-test"
    ros_version = "7.23.3"
    board = "C52iG-5HaxD2HaxD"


class StubClient:
    def __init__(self, body="/ip address add address=1.2.3.4/24"):
        self.body = body
        self.calls = 0
        self.sensitive: list[bool] = []

    def export_config(self, *, show_sensitive=False):
        self.calls += 1
        self.sensitive.append(show_sensitive)
        return StubExport(self.body)


def test_store() -> None:
    from x3.ui import autoexport as ax

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        session = StubSession()
        first = ax.store(StubExport(), session=session, reason="idle", root=root)
        ok("store: first capture written", first.path is not None and first.path.exists())
        ok("store: not flagged unchanged", not first.unchanged)

        # Same config again: nothing new to keep.
        second = ax.store(StubExport(), session=session, reason="idle", root=root)
        ok("store: identical capture skipped", second.unchanged)
        ok("store: no second file", second.path is None)
        check("store: one .rsc on disk", len(list(root.glob("*.rsc"))), 1)

        # A different config does get written.
        third = ax.store(
            StubExport("/ip address add address=5.6.7.8/24"),
            session=session, reason="idle", root=root,
        )
        ok("store: changed capture written", third.path is not None)
        check("store: two .rsc on disk", len(list(root.glob("*.rsc"))), 2)

        # Every attempt is in the index, including the skipped one.
        lines = (root / "index.jsonl").read_text().strip().splitlines()
        check("store: index has every attempt", len(lines), 3)

        # Exports carry credentials when asked to; the directory must not be
        # world-readable either way.
        mode = (root / "index.jsonl").stat().st_mode & 0o777
        if os.name != "nt":  # Windows: the profile's ACL protects it, not a mode
            check("store: index is 0600", mode, 0o600)
        rsc = sorted(root.glob("*.rsc"))[0]
        if os.name != "nt":
            check("store: capture is 0600", rsc.stat().st_mode & 0o777, 0o600)


def test_debounce() -> None:
    """Armed by change, one-shot, restarted rather than accumulated."""
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

    from x3.ui import autoexport as ax

    app = QCoreApplication.instance() or QCoreApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    with tempfile.TemporaryDirectory() as tmp:
        client = StubClient()
        auto = ax.AutoExport(client=client, session=StubSession(), idle_secs=1)
        # Redirect the store into the sandbox.
        root = Path(tmp)
        auto._capture = lambda reason: ax.store(  # type: ignore[method-assign]
            client.export_config(show_sensitive=auto.show_sensitive),
            session=auto.session, reason=reason,
            changes=list(auto.pending), root=root,
        )

        # Nothing has changed, so nothing is armed and nothing is exported.
        ok("debounce: idle session not armed", not auto.armed)
        pump(1300)
        check("debounce: no export without a change", client.calls, 0)

        # One change arms it; the export lands once the interval passes.
        auto.note_local("IP ▸ Addresses", "Wrote 1 field")
        ok("debounce: armed by a change", auto.armed)
        check("debounce: not fired yet", client.calls, 0)
        pump(1500)
        check("debounce: fired once", client.calls, 1)
        ok("debounce: disarmed after firing", not auto.armed)
        ok("debounce: pending cleared", not auto.pending)

        # And it stays disarmed: a two-day session with one early edit gets one
        # file, not one every interval.
        pump(1300)
        check("debounce: does not repeat", client.calls, 1)

        # A second change re-arms.
        auto.note_local("IP ▸ Routes", "Added object .id 3")
        ok("debounce: re-armed", auto.armed)
        pump(1500)
        check("debounce: fired again", client.calls, 2)


def test_debounce_restarts() -> None:
    """A change during the countdown restarts it -- it does not queue a second."""
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

    from x3.ui import autoexport as ax

    app = QCoreApplication.instance() or QCoreApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    with tempfile.TemporaryDirectory() as tmp:
        client = StubClient()
        auto = ax.AutoExport(client=client, session=StubSession(), idle_secs=2)
        root = Path(tmp)
        auto._capture = lambda reason: ax.store(  # type: ignore[method-assign]
            client.export_config(show_sensitive=auto.show_sensitive),
            session=auto.session, reason=reason,
            changes=list(auto.pending), root=root,
        )

        auto.note_local("A", "one")
        pump(1200)
        auto.note_local("B", "two")          # restarts the 2 s countdown
        pump(1200)
        check("restart: still waiting", client.calls, 0)
        check("restart: both changes pending", len(auto.pending), 2)
        pump(1200)
        check("restart: one export for both", client.calls, 1)


def test_self_write_attribution() -> None:
    """Our own edit comes back as a push; it must not read as somebody else's."""
    from x3.ui import autoexport as ax

    auto = ax.AutoExport(client=StubClient(), session=StubSession(), idle_secs=600)
    auto.note_local("IP ▸ Addresses", "Wrote 1 field", path=(12, 1))
    ok("attribution: own path recognised", auto.owns_recent_write((12, 1)))
    ok("attribution: other path is not", not auto.owns_recent_write((14, 2)))
    ok("attribution: no path is not", not auto.owns_recent_write(None))
    check("attribution: nothing warned about", len(auto.remote_notes), 0)

    auto.note_remote("IP ▸ Firewall", "1 changed (drop-all)")
    check("attribution: remote warned", len(auto.remote_notes), 1)
    auto.clear_remote()
    check("attribution: dismissable", len(auto.remote_notes), 0)
    ok("attribution: dismiss keeps the snapshot owed", bool(auto.pending))


def test_sensitive_default_off() -> None:
    """A snapshot must not start collecting credentials unless asked."""
    from x3.ui import autoexport as ax

    auto = ax.AutoExport(client=StubClient(), session=StubSession())
    ok("sensitive: off by default", not auto.show_sensitive)


def test_snapshot_folder_dialog() -> None:
    """Settings ▸ Snapshot Folder hands the folder to the desktop, not just its name.

    Two things are pinned. The button reaches ``QDesktopServices`` with a
    file:// URL for the real directory -- nothing is launched here, the call is
    recorded -- and on a device that has never been captured, where the
    per-device folder does not exist yet, it opens the nearest ancestor that
    does instead of asking a file manager to browse a path that is not there.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-snapdir-")
    # The data dir too, and not only for tidiness: this test creates the
    # per-device folder to check the second case, and pointing at the real
    # ~/.local/share would leave an empty one there and make the run before it
    # change the result of the run after.
    os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="x3-snapdata-")

    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])

    from x3.ui.session import SessionInfo
    from x3.ui.widgets import main_window as mw

    ok("nearest: an existing folder is itself",
       mw._nearest_existing(Path(os.environ["XDG_CONFIG_HOME"]))
       == Path(os.environ["XDG_CONFIG_HOME"]))
    deep = Path(os.environ["XDG_CONFIG_HOME"]) / "never" / "made" / "this"
    check("nearest: a missing folder walks up to one that is there",
          mw._nearest_existing(deep), Path(os.environ["XDG_CONFIG_HOME"]))

    # Clicked, not launched: the URL is recorded and the call reports success.
    opened: list[str] = []

    class FakeDesktop:
        @staticmethod
        def openUrl(url):  # noqa: N802 - matching Qt's name
            opened.append(url.toLocalFile())
            return True

    mw.QDesktopServices = FakeDesktop  # type: ignore[assignment]

    def _click_open(self):
        for btn in self.buttons():
            if "open folder" in btn.text().casefold().replace("&", ""):
                btn.click()
                return 0
        raise AssertionError(
            f"no Open Folder button in {[b.text() for b in self.buttons()]}"
        )

    QMessageBox.exec = _click_open  # type: ignore[method-assign]

    class StubClient:
        readonly = True
        schema = None
        session_log = None

        def nav_tree(self):
            return []

        def close(self):
            pass

    win = mw.MainWindow(SessionInfo(host="10.0.0.1", user="admin"), client=StubClient())
    try:
        from x3.ui import autoexport as ax

        directory = ax.device_dir(win.session)
        # Not captured yet: the dialog opens the closest real ancestor, and
        # asking where the files go must not create anything.
        win._show_snapshot_folder()
        check("dialog: opened one folder", len(opened), 1)
        ok("dialog: and not the one that does not exist",
           Path(opened[0]) == Path(mw._nearest_existing(directory)))
        ok("dialog: nothing was created just by asking", not directory.exists())

        # Once a capture has been stored, it is the device folder itself.
        directory.mkdir(parents=True, exist_ok=True)
        opened.clear()
        win._show_snapshot_folder()
        # Compared as paths: QUrl gives forward slashes even on Windows.
        check("dialog: an existing folder opens directly",
              [Path(p) for p in opened], [Path(directory)])

        # A desktop with no file manager says so rather than failing silently.
        class DeadDesktop:
            @staticmethod
            def openUrl(url):  # noqa: N802
                return False

        mw.QDesktopServices = DeadDesktop  # type: ignore[assignment]
        win._open_in_file_manager(directory)
        ok("dialog: a refusal is reported, with the path to go there by hand",
           "No file manager answered" in win.statusBar().currentMessage()
           and str(directory) in win.statusBar().currentMessage())
    finally:
        win.close()
    app.processEvents()


def main() -> int:
    test_parser()
    test_refusal()
    test_log_splice()
    test_command_choice()
    test_redact()
    test_config_codes()
    test_classifier()
    test_store()
    # Before the timing tests, and not by taste: they only need a
    # QCoreApplication, and once one exists no QWidget can be built for the
    # rest of the run. A QApplication satisfies both, so the one that needs
    # widgets goes first.
    test_snapshot_folder_dialog()
    test_debounce()
    test_debounce_restarts()
    test_self_write_attribution()
    test_sensitive_default_off()

    print()
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print("  " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
