"""Offline tests for the fleet backup: the archive, and the run around it.

No device and no pytest required:

    .venv/bin/python test_backup.py

A synthetic ``Addresses.cdb`` is built with the real writer, and the routers
are a fake :class:`DeviceClient` injected through ``client_factory`` -- so the
inventory, the filtering, the archive and the commit are all exercised end to
end while nothing is connected to. Git runs for real, in a temp directory.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from x3.proto import cdb, fleet
from x3.proto import message as m
from x3.proto.archive import Archive, slug
from x3.proto.device import DeviceError

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    if not cond:
        failures.append(label)


HAVE_GIT = shutil.which("git") is not None

CONFIG_A = "/ip address add address=192.168.88.1/24 interface=bridge1"
CONFIG_B = "/ip address add address=10.0.0.1/24 interface=ether1"


# --- building an address book ---------------------------------------------

def entry(*, note, host, login="admin", password="pw", group=None):
    msg = m.Message()
    msg.add_string(cdb.F_HOST, host)
    msg.add_string(cdb.F_LOGIN, login)
    msg.add_string(cdb.F_PASSWORD, password)
    msg.add_string(cdb.F_NOTE, note)
    if group is not None:
        msg.add_string(cdb.F_GROUP, group)
    return cdb.CdbEntry(msg)


def write_book(path, entries):
    cdb.CdbFile(entries).save(str(path))
    return path


# --- a fake device ---------------------------------------------------------

class FakeExport:
    def __init__(self, text):
        # A real export opens with a header whose first line is the capture
        # time. It is here so the archive is tested against the thing that
        # would otherwise commit every router every night.
        self.text = f"# 2026-08-20 03:14:07 by RouterOS 7.23.3\n#\n{text}"
        self.body = text
        self.command = "/export terse show-sensitive"
        self.disturbed = False

    @property
    def stable_text(self):
        from x3.proto.configexport import HEADER_DATE_RE
        return HEADER_DATE_RE.sub(r"\1", self.text)


class FakeClient:
    """Stands in for DeviceClient. Records what it was asked to do."""

    #: host -> config text, or an Exception to raise on connect.
    responses: dict = {}
    #: Every (host, port, user) that was constructed.
    seen: list = []
    #: The ``need_schema`` each connect() was called with.
    schema_asks: list = []
    #: The ``readonly`` each client was constructed with.
    readonly_asks: list = []

    def __init__(self, host, user, password, *, port=8291, timeout=10.0,
                 readonly=True, **kw):
        self.host, self.user, self.password, self.port = host, user, password, port
        self.readonly = readonly
        self.needed_schema = None
        self.closed = False
        FakeClient.seen.append((host, port, user))
        FakeClient.readonly_asks.append(readonly)

    def connect(self, progress=None, *, need_schema=True):
        self.needed_schema = need_schema
        FakeClient.schema_asks.append(need_schema)
        answer = FakeClient.responses.get(self.host)
        if isinstance(answer, Exception):
            raise answer

    def export_config(self, *, show_sensitive=True, timeout=90.0, **kw):
        self.show_sensitive = show_sensitive
        return FakeExport(FakeClient.responses.get(self.host, ""))

    def read_fingerprint(self):
        class FP:
            identity, model, serial, system_id = "rtr", "EDGE10", "SN123", ""
            macs, ips = ["aa:bb:cc:dd:ee:ff"], ["192.168.88.1"]
        return FP()

    @property
    def info(self):
        class Info:
            identity, version, board = "rtr", "7.23.3", "EDGE10"
            architecture, platform = "arm", "vendor"
        return Info()

    def close(self):
        self.closed = True


def run(tmp, book, **kw):
    FakeClient.seen = []
    FakeClient.schema_asks = []
    FakeClient.readonly_asks = []
    return fleet.run_backup(
        book, tmp / "archive", client_factory=FakeClient, concurrency=2, **kw
    )


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


# --- slug ------------------------------------------------------------------

check("slug keeps a plain name", slug("router1"), "router1")
check("slug replaces separators", slug("Client / Office #1"), "Client-Office-1")
check("slug trims edge punctuation", slug("--x--"), "x")
check("slug falls back on an empty name", slug(""), "device")
check("slug falls back on pure punctuation", slug("///"), "device")
check("slug refuses a bare dot", slug("."), "device")
check("slug refuses a double dot", slug(".."), "device")
ok("slug cannot escape the directory", "/" not in slug("../../etc/passwd"))
ok("slug bounds the length", len(slug("x" * 500)) <= 80)


# --- split_host ------------------------------------------------------------

check("host with port", fleet.split_host("1.2.3.4:41002"), ("1.2.3.4", 41002))
check("host without port", fleet.split_host("rtr.example.com"),
      ("rtr.example.com", fleet.DEFAULT_PORT))
check("bracketed v6 with port", fleet.split_host("[2001:db8::1]:8291"),
      ("2001:db8::1", 8291))
check("bare v6 is not split", fleet.split_host("2001:db8::1"),
      ("2001:db8::1", fleet.DEFAULT_PORT))
check("a non-numeric port falls back", fleet.split_host("1.2.3.4:winbox"),
      ("1.2.3.4", fleet.DEFAULT_PORT))
check("whitespace is trimmed", fleet.split_host("  1.2.3.4:1  "), ("1.2.3.4", 1))
# The Winbox port is used as written: this connects over Winbox, not SSH.
check("the address-book port is not adjusted",
      fleet.split_host("host:41002")[1], 41002)


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # --- inventory and filtering ------------------------------------------

    book = write_book(tmp / "Addresses.cdb", [
        entry(note="alpha", host="10.0.0.1:41002"),
        entry(note="beta", host="10.0.0.2"),
        entry(note="old-one", host="10.0.0.3", group="gone"),
        entry(note="off", host="10.0.0.4", group="disabled"),
        entry(note="lan-only", host="aa:bb:cc:dd:ee:ff"),
        entry(note="nologin", host="10.0.0.6", login=""),
        entry(note="nohost", host=""),
    ])

    targets, skipped = fleet.load_targets(book)
    check("live targets", sorted(t.note for t in targets), ["alpha", "beta"])
    check("everything is accounted for", len(targets) + len(skipped), 7)
    reasons = {s.target.note: s.reason for s in skipped}
    ok("gone is skipped", "skip list" in reasons["old-one"])
    ok("disabled is skipped", "skip list" in reasons["off"])
    ok("a MAC entry is skipped", "MAC-Winbox" in reasons["lan-only"])
    ok("a login-less entry is skipped", "no login" in reasons["nologin"])
    ok("a host-less entry is skipped", "no host" in reasons["nohost"])
    ok("every skip is marked skipped",
       all(s.status == "skipped" for s in skipped))

    check("the port is read from the entry",
          [t.port for t in targets if t.note == "alpha"], [41002])
    check("the default port is used otherwise",
          [t.port for t in targets if t.note == "beta"], [fleet.DEFAULT_PORT])

    mac_ok, _ = fleet.load_targets(book, allow_mac=True)
    ok("--allow-mac lets a MAC entry through",
       "lan-only" in {t.note for t in mac_ok})

    matched, _ = fleet.load_targets(book, match="^al")
    check("--match filters by note", [t.note for t in matched], ["alpha"])

    kept, _ = fleet.load_targets(book, skip_groups=[])
    ok("an empty skip list keeps the disabled ones",
       {"old-one", "off"} <= {t.note for t in kept})

    # --- a first run -------------------------------------------------------

    FakeClient.responses = {"10.0.0.1": CONFIG_A, "10.0.0.2": CONFIG_B}
    events = []
    first = run(tmp, book, on_event=lambda k, r: events.append((k, r.status)))

    check("both routers were reached", len(first.succeeded), 2)
    check("both count as changed on a first run", len(first.changed), 2)
    check("nothing failed", len(first.failed), 0)
    check("the run exits clean", first.exit_code, 0)
    ok("an event fired for every router and skip",
       len([e for e in events if e[0] == "done"]) == 2 and
       len([e for e in events if e[0] == "skipped"]) == 5)

    root = first.archive_root
    ok("the config file is written", (root / "alpha.rsc").is_file())
    ok("the info file is written", (root / "alpha.info.json").is_file())
    ok("the config is the device's text",
       CONFIG_A in (root / "alpha.rsc").read_text())

    info = json.loads((root / "alpha.info.json").read_text())
    check("the model is recorded", info["model"], "EDGE10")
    check("the serial is recorded", info["serial"], "SN123")
    check("the firmware is recorded", info["firmware"], "7.23.3")
    # Anything that moves on its own would put a commit on every router every
    # night; see the fleet module docstring.
    for volatile in ("uptime", "cpu_load", "free_memory"):
        ok(f"{volatile} is not archived", volatile not in info)

    if os.name != "nt":  # Windows: the profile's ACL protects it, not a mode
        check("the archive is private", oct(root.stat().st_mode & 0o777), "0o700")
        check("the config file is private",
              oct((root / "alpha.rsc").stat().st_mode & 0o777), "0o600")

    # --- connecting is read-only and schema-free ---------------------------

    # A backup must not need a mirrored .jg set: the firmware least likely to
    # have one cached is exactly the one nobody has looked at in a year.
    ok("no router was asked to load a schema",
       FakeClient.schema_asks == [False, False])
    ok("every client was opened read-only",
       FakeClient.readonly_asks == [True, True])

    # --- a second, unchanged run -------------------------------------------

    second = run(tmp, book)
    check("nothing changed the second time", len(second.changed), 0)
    check("both were still reached", len(second.succeeded), 2)
    ok("both are reported unchanged",
       all(r.status == "unchanged" for r in second.results))

    if HAVE_GIT:
        ok("no empty commit was made", not second.commit.committed)

    # --- one router's config changes ---------------------------------------

    FakeClient.responses["10.0.0.1"] = CONFIG_A + "\n/ip firewall filter add chain=input action=drop"
    third = run(tmp, book)
    check("exactly one router changed", [r.target.note for r in third.changed],
          ["alpha"])
    ok("its config is marked changed",
       third.changed[0].config_changed)
    ok("its info did not change", not third.changed[0].info_changed)
    ok("the new line is on disk",
       "action=drop" in (root / "alpha.rsc").read_text())

    # --- a router that cannot be reached -----------------------------------

    FakeClient.responses["10.0.0.2"] = DeviceError("connection refused")
    fourth = run(tmp, book, retries=1)
    check("the failure is reported", [r.target.note for r in fourth.failed],
          ["beta"])
    ok("the reason is kept", "connection refused" in fourth.failed[0].reason)
    check("the run exits non-zero", fourth.exit_code, 1)
    check("the healthy router still succeeded", len(fourth.succeeded), 1)
    ok("the failed router's last-good config is left alone",
       CONFIG_B in (root / "beta.rsc").read_text())

    retried = run(tmp, book, retries=3)
    check("retries are attempted", retried.failed[0].attempts, 3)

    # --- a capture that is the login banner, not a config -------------------

    FakeClient.responses["10.0.0.2"] = (
        "  MikroTik RouterOS 6.44.3 (c) 1999-2019       http://www.mikrotik.com/\n"
        "[?]             Gives the list of available commands\n"
        "/               Move up to base level\n"
        "..              Move up one level\n"
        "/command        Use command at the base level"
    )
    banner = run(tmp, book, retries=1)
    check("a banner-only capture fails", [r.target.note for r in banner.failed],
          ["beta"])
    ok("the reason says why", "no config" in banner.failed[0].reason)
    ok("the last-good config is left alone",
       CONFIG_B in (root / "beta.rsc").read_text())

    FakeClient.responses["10.0.0.2"] = CONFIG_B

    # --- git ---------------------------------------------------------------

    if HAVE_GIT:
        ok("git is active", not first.vcs_error)
        ok("the first run committed", first.commit.committed)
        ok("a sha is reported", len(first.commit.sha) >= 4)
        log = git(root, "log", "--format=%s")
        ok("the subject counts what happened", "changed" in log.splitlines()[-1])
        body = git(root, "log", "-1", "--format=%b", "--skip=0")
        ok("the archive is a repo", (root / ".git").is_dir())
        ok("committed with its own identity",
           git(root, "config", "user.email") == "x3ctl@localhost")
        ok("a README explains the directory", (root / "README.md").is_file())
        ok("the README warns about secrets",
           "secret" in (root / "README.md").read_text().lower())
        # The point of the whole design: one router's history, on its own.
        history = git(root, "log", "--oneline", "--", "alpha.rsc")
        ok("one router's history is separable", len(history.splitlines()) >= 2)
        ok("the working tree is clean after a run",
           git(root, "status", "--porcelain") == "")

    # --- --no-git ----------------------------------------------------------

    plain = fleet.run_backup(
        book, tmp / "plain", client_factory=FakeClient, use_git=False,
    )
    ok("files are written without git", (plain.archive_root / "alpha.rsc").is_file())
    ok("no repo is made", not (plain.archive_root / ".git").exists())
    check("no commit is attempted", plain.commit, None)
    check("changes are still detected", len(plain.changed), 2)


# --- the archive on its own ------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "arch"
    archive = Archive(root, use_git=False).open()

    first_write = archive.write("r1", ".rsc", "line")
    ok("a new file is created", first_write.created and first_write.changed)
    again = archive.write("r1", ".rsc", "line")
    ok("an identical write is not a change", not again.changed)
    ok("an identical write is not a creation", not again.created)
    changed = archive.write("r1", ".rsc", "other")
    ok("a different write is a change", changed.changed)
    ok("a different write is not a creation", not changed.created)
    check("a trailing newline is added",
          (root / "r1.rsc").read_text(), "other\n")
    ok("a text that already ends in a newline is not doubled",
       archive.write("r2", ".rsc", "x\n") and (root / "r2.rsc").read_text() == "x\n")

    # Two routers with the same name must not overwrite each other, or the
    # file would alternate between two configs on every run.
    a = archive.stem_for("Office", key="10.0.0.1:8291")
    b = archive.stem_for("Office", key="10.0.0.2:8291")
    ok("a colliding name is disambiguated", a != b)
    check("the first keeps the plain name", a, "Office")
    check("the same router keeps its stem",
          archive.stem_for("Office", key="10.0.0.1:8291"), a)
    check("the other router keeps its stem",
          archive.stem_for("Office", key="10.0.0.2:8291"), b)

    missing = Archive(Path(td) / "nogit", use_git=True)
    missing.open()
    if HAVE_GIT:
        ok("git initialises when present", missing.git_active)


if not HAVE_GIT:
    print("note: git not installed; version-control checks were skipped")

if failures:
    print(f"FAIL ({len(failures)})")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("test_backup: all checks passed")
