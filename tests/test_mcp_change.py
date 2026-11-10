"""Changes proposed over MCP: what may be proposed, and what the run does.

No device and no pytest required:

    python tests/test_mcp_change.py

The MCP server never writes. It writes a job file and opens the x3 change
window, where a person approves; the run then takes Safe Mode, sets a revert
timer on the router itself, types the commands, runs the checklist and keeps
the change only if every check passes. These tests are that sentence, one
clause at a time, against a router that is a few lists in memory:

  * what a command may be: one write verb on a menu, nothing computed;
  * every way a run ends, and that only one of them keeps the change;
  * the timer goes on first and comes off last, and a router too old for it
    is not touched;
  * a change that cuts its own connection is reverted by the router, and the
    run says "reverted" only once it has seen the old config again;
  * the tools refuse a router the user closed, and report what happened;
  * the window: Enter cannot approve, closing it rejects, closing it
    mid-change reverts.
"""

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = tempfile.mkdtemp(prefix="x3-test-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "data")

from x3.proto.configexport import ConfigExport  # noqa: E402
from x3.proto.device import DeviceError  # noqa: E402
from x3.mcp import change as C  # noqa: E402
from x3.mcp import tools as T  # noqa: E402
from x3.mcp.protocol import ToolError  # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}\n     got:  {got!r}\n     want: {want!r}")


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label}" + (f"\n     {detail}" if detail else ""))


# --- what a command may be -------------------------------------------------------

for good in [
    "/ip address add address=192.0.2.1/24 interface=ether1 comment=\"a; b\"",
    "/routing/filter/rule/set [find chain=bgp-out] rule=\"set distance 10; accept\"",
    "/ip firewall filter remove [find comment~\"^x[0-9]\"]",
    "/interface bridge port move 3 0",
    "/ip firewall address-list add list=x address=192.0.2.0/24 comment=\"why\\3F\"",
]:
    try:
        ok(f"a change is accepted: {good[:40]}", C.check_command(good) == good)
    except ToolError as e:
        ok(f"a change is accepted: {good[:40]}", False, str(e))

for bad, why in [
    ("/system reboot", "not a change verb"),
    ("/ip address print", "a read is not a change"),
    ("/ip address add address=192.0.2.1/24; /system reboot", "a second command"),
    ("/ip address add address=[/system reboot]", "a command in brackets"),
    ("/ip address add address=[find] comment=[/system reboot]", "a command after a find"),
    ("/ip address add address=[find [find]]", "nested brackets"),
    ("/ip address add comment=$x", "a variable"),
    (":put 1", "scripting"),
    ("/ip address add comment=\"abc", "an unclosed quote"),
    ("/ip address add comment=\"what?\"", "a ? opens help"),
    ("/ip route add comment=café", "not ASCII"),
    ("/ip address add address=192.0.2.1/24\n/system reboot", "a second line"),
    ("/ip address add a=1 { }", "braces"),
    ("/user add name=x", "users"),
    ("/user group set full policy=x", "user groups"),
    ("/file remove x", "files"),
    ("/system script add name=x source=y", "scripts"),
    ("/system scheduler add name=x on-event=y", "the scheduler"),
    ("/system package enable x", "packages"),
    ("/certificate remove x", "certificates"),
    ("/import x.rsc", "import"),
    ("/tool fetch url=x", "fetch"),
    ("/system/reset-configuration", "reset"),
    ("", "empty"),
]:
    try:
        C.check_command(bad)
        ok(f"refused: {why}", False, bad)
    except ToolError:
        ok(f"refused: {why}", True)

ok("/user-manager is not /user", C.check_command("/user-manager user add name=x"))

c = C.check_check({"type": "rows", "menu": "bgp session", "where": ["established"],
                   "min": 2, "within_s": 120})
check("a rows check, in words",
      c["name"], "/routing bgp session: at least 2 row(s) where established, within 120s")
check("a value in a check is typed as a literal: quoted, with $ and ? escaped",
      C.check_check({"type": "rows", "menu": "ip firewall nat",
                     "where": ["dst-address=203.0.113.0/24", "comment~end$", "disabled=no"]})["where"],
      ['dst-address="203.0.113.0/24"', 'comment~"end\\$"', "!disabled"])
check("a ping check defaults to no loss of five",
      C.check_check({"type": "ping", "address": "192.0.2.9"})["name"],
      "ping 192.0.2.9: at most 0% of 5 lost")
for bad in [{"type": "script"}, {"type": "ping", "address": "x; y"},
            {"type": "rows", "menu": "/file"}, {"type": "rows", "menu": "ip route", "where": ["a b c"]},
            {"type": "export"}, {"type": "export", "match": "("},
            "ping 1.1.1.1"]:
    try:
        C.check_check(bad)
        ok(f"a check that is not one is refused: {bad}", False)
    except ToolError:
        ok(f"a check that is not one is refused: {bad}", True)


# --- a router in memory -------------------------------------------------------------

import re  # noqa: E402

_LEN = r':put \[:len \[/system scheduler get \[find name=\S+\] on-event\]\]'
_HELD = r':local s \[/system scheduler get \[find name=(?P<name>\S+)\] on-event\]; '
_COMBINED = re.compile(
    r':local i \[(?P<cmd>.*)\]; ' + _HELD + r'/system scheduler set \[find name=\S+\] '
    r'on-event=\("(?P<pre>.*)" \. \$i \. "(?P<post>.*)" \. \[:pick \$s 19 \[:len \$s\]\]\); '
    r':put \$i; ' + _LEN)
_PREPEND = re.compile(
    _HELD + r'/system scheduler set \[find name=\S+\] '
    r'on-event=\("(?P<pre>.*)" \. \[:pick \$s 19 \[:len \$s\]\]\); ' + _LEN)
#: What the run types ahead of every line, and what the router prints for it.
_MARK_TYPED, _MARK_PRINTED = ':put ("x3" . "-out"); ', "x3-out\n"
_GET = re.compile(r':put \[(?P<menu>/[a-z -]+) get (?P<id>\*[0-9A-F]+) disabled\]')
_EACH = re.compile(r':foreach i in=\[(?P<menu>/[a-z -]+) find (?P<cond>[^\]]+)\] do=')
_ONE = re.compile(r':put \("(?P<id>\*[0-9A-F]+) " \. \[(?P<menu>/[a-z -]+) get')


class Router:
    """Objects with ids, a scheduler entry, and Safe Mode as a list of ways
    back. ``gaps`` names what this router's undo does not take back, the way
    real ones do not: "add /ip route" (7.19.4), "disabled /ip firewall filter"."""

    def __init__(self, version="7.21.1"):
        self.version = version
        self.objects = [
            {"id": "*1", "menu": "/ip address", "disabled": False,
             "line": "/ip address add address=10.0.0.1/24 interface=ether1"},
            {"id": "*2", "menu": "/ip firewall filter", "disabled": False,
             "line": "/ip firewall filter add chain=input action=accept comment=mgmt"},
        ]
        self.next_id = 3
        self.timer: dict | None = None
        self.floating: list = []          # undo closures, oldest first
        self.gaps: set[str] = set()
        self.safe = False
        self.typed: list[str] = []
        self.refuse: dict[str, str] = {}  # command -> what the console says
        self.unrecorded: set[str] = set()  # commands History says nothing about
        self.cuts: set[str] = set()       # commands that kill the path
        self.cut = False
        self.unreachable_ping: set[str] = set()
        self.rows: dict[str, int] = {}    # menu -> count
        self.released = self.unrolled = self.dropped = 0
        self.opened: list[bool] = []
        self.miscount = 0                 # the router reports a script this much off

    @property
    def config(self) -> list[str]:
        out = [o["line"] + (" disabled=yes" if o["disabled"] else "") for o in self.objects]
        if self.timer is not None:
            out.append(f"/system scheduler add name={self.timer['name']}")
        return out

    def _record(self, undo, gap=""):
        if self.safe:
            self.floating.append((lambda: None) if gap in self.gaps else undo)

    def unroll(self):
        while self.floating:
            self.floating.pop()()
        self.safe = False

    def execute(self, command: str) -> str:
        """One console command, as the router would run it. Returns what it prints."""
        m = re.fullmatch(r'/system scheduler add name=(\S+) interval=\d+s on-event="(.*)" comment=".*"', command)
        if m:
            self.timer = {"name": m.group(1), "script": m.group(2)}
            self._record(lambda: setattr(self, "timer", None))
            return ""
        m = re.fullmatch(r'/system scheduler set \[find name=(\S+)\] on-event="(.*)"', command)
        if m:
            if self.timer is not None:
                old = self.timer["script"]
                self.timer["script"] = m.group(2)
                self._record(lambda: self.timer and self.timer.update(script=old))
            return ""
        if re.fullmatch(r"/system scheduler remove \[find name=\S+\]", command):
            if self.timer is not None:
                old, self.timer = self.timer, None
                self._record(lambda: setattr(self, "timer", old))
            return ""
        m = re.fullmatch(r"(/[a-z -]+) remove (\*[0-9A-F]+)", command)
        if m:
            obj = next((o for o in self.objects if o["id"] == m.group(2) and o["menu"] == m.group(1)), None)
            if obj is None:
                return "no such item"
            at = self.objects.index(obj)
            self.objects.remove(obj)
            self._record(lambda: self.objects.insert(at, obj))
            return ""
        m = re.fullmatch(r"(/[a-z -]+) set (\*[0-9A-F]+) disabled=(yes|no)", command)
        if m:
            obj = next((o for o in self.objects if o["id"] == m.group(2) and o["menu"] == m.group(1)), None)
            if obj is None:
                return "no such item"
            old = obj["disabled"]
            obj["disabled"] = m.group(3) == "yes"
            self._record(lambda: obj.update(disabled=old), f"disabled {m.group(1)}")
            return ""
        m = re.fullmatch(r"(/[a-z -]+) (enable|disable) \[find (\S+)\]", command)
        if m:
            for obj in [o for o in self.objects if o["menu"] == m.group(1) and m.group(3) in o["line"]]:
                old = obj["disabled"]
                obj["disabled"] = m.group(2) == "disable"
                self._record(lambda obj=obj, old=old: obj.update(disabled=old), f"disabled {m.group(1)}")
            return ""
        m = re.fullmatch(r"(/[a-z -]+) add .*", command)
        if m:
            obj = {"id": f"*{self.next_id:X}", "menu": m.group(1), "disabled": False, "line": command}
            self.next_id += 1
            self.objects.append(obj)
            if command not in self.unrecorded:
                self._record(lambda: obj in self.objects and self.objects.remove(obj), f"add {m.group(1)}")
            return obj["id"]
        raise AssertionError(f"the test router does not know this command: {command}")

    def timer_fires(self):
        """The scheduler entry runs its script: Safe Mode's rollback, then
        each of the run's own ways back. The path comes back with it."""
        script = self.timer["script"] if self.timer else "/safe-mode unroll"
        for step in script.split("; "):
            if step == "/safe-mode unroll":
                self.unroll()
                continue
            m = re.fullmatch(r":do \{(.*)\} on-error=\{\}", step)
            self.execute(m.group(1))
        self.cut = False


class Client:
    def __init__(self, router: Router, write: bool):
        if router.cut:
            raise DeviceError("timed out")
        self.r = router
        self.write = write
        self.info = type("I", (), {"version": router.version, "identity": "edge"})()
        router.opened.append(write)

    def interface_choices(self):
        return [(1, "ether1", "Ethernet")]

    def _gone(self):
        if self.r.cut:
            raise DeviceError("connection lost")

    def export_config(self, *, show_sensitive=True):
        self._gone()
        text = "\n".join(self.r.config)
        return ConfigExport(text=text, body=text, complete=True, command="/export terse")

    def safe_mode_enter(self):
        self._gone()
        assert self.write, "Safe Mode taken on a read-only session"
        self.r.safe = True

    def safe_mode_pending(self):
        self._gone()
        return len(self.r.floating)

    def safe_mode_leave(self, *, keep_changes=True):
        self._gone()
        if keep_changes:
            self.r.released += 1
            self.r.floating.clear()
            self.r.safe = False
        else:
            self.r.unrolled += 1
            self.r.unroll()

    def drop_session(self):
        self.r.dropped += 1
        if not self.r.cut:
            self.r.unroll()       # the router sees the session go, and rolls back

    def close(self):
        # What DeviceClient.close does with Safe Mode still held: releases,
        # which KEEPS. A run that gets here with changes held has a bug.
        if self.r.safe and self.r.floating and not self.r.cut:
            self.r.kept_by_close = True

    def run_console(self, command, *, timeout):
        self._gone()
        r = self.r
        # A terminal echoes the line, and past its width echoes it in pieces:
        # the capture opens with the line's own tail, which is not output.
        typed = command
        echo = typed[-40:] + "\n" if len(typed) > 512 else ""
        marker = ""
        if command.startswith(_MARK_TYPED):
            command, marker = command[len(_MARK_TYPED):], _MARK_PRINTED
        r.typed.append(command)

        def said(text="", complete=True):
            return ConfigExport(text=echo + marker + text, complete=complete, command=typed)

        if command.startswith("/ping"):
            addr = command.split("address=")[1].split()[0]
            n = int(command.split("count=")[1].split()[0])
            got = 0 if addr in r.unreachable_ping else n
            return said(f"sent={n} received={got} packet-loss={100 - 100 * got // n}%")
        if " print count-only" in command:
            return said(str(r.rows.get(command.split(" print")[0], 0)))
        m = _GET.fullmatch(command)
        if m:
            obj = next((o for o in r.objects if o["id"] == m["id"] and o["menu"] == m["menu"]), None)
            return said("no such item" if obj is None else str(obj["disabled"]).lower())
        m = _EACH.match(command) or _ONE.match(command)
        if m:
            if "cond" in m.groupdict():
                objs = [o for o in r.objects if o["menu"] == m["menu"] and m["cond"] in o["line"]]
            else:
                objs = [o for o in r.objects if o["menu"] == m["menu"] and o["id"] == m["id"]]
            return said("\n".join(f"{o['id']} {str(o['disabled']).lower()}" for o in objs))
        assert self.write, f"a change typed on a read-only session: {command}"
        m = _COMBINED.fullmatch(command)
        if m:
            assert r.safe, f"a change typed outside Safe Mode: {command}"
            inner = m["cmd"]
            if inner in r.refuse:
                return said(r.refuse[inner])
            new_id = r.execute(inner)
            script = m["pre"] + new_id + m["post"] + r.timer["script"][19:]
            r.execute(f'/system scheduler set [find name={m["name"]}] on-event="{script}"')
            if inner in r.cuts:
                r.cut = True
                return said("", complete=False)
            return said(f"{new_id}\n{len(r.timer['script']) + r.miscount}")
        m = _PREPEND.fullmatch(command)
        if m:
            script = m["pre"] + r.timer["script"][19:]
            r.execute(f'/system scheduler set [find name={m["name"]}] on-event="{script}"')
            return said(str(len(r.timer["script"])))
        if command in r.refuse:
            return said(r.refuse[command])
        text = r.execute(command)
        if command in r.cuts:
            r.cut = True
            return said("", complete=False)
        return said(text)


ADD = "/ip address add address=192.0.2.1/24 interface=ether2"
PING = {"type": "ping", "address": "192.0.2.9", "count": 2}


def job_for(commands=(ADD,), checks=(PING,), settle=0):
    return C.new_job(device="edge", host="10.9.9.9", login="admin", summary="test",
                     commands=[C.check_command(x) for x in commands],
                     checks=[C.check_check(x) for x in checks], settle_s=settle)


def run(router, job=None, *, cancel=None, sleep=None, events=None):
    job = job or job_for()
    said = events if events is not None else []
    result = C.ChangeRun(
        job, connect=lambda write: Client(router, write), emit=said.append,
        cancel=cancel, sleep=sleep or (lambda s: None),
    ).run()
    return result, [e["step"] for e in said]


# --- kept ------------------------------------------------------------------------------

r = Router()
before = list(r.config)
res, steps = run(r)
check("every check passes: kept", res["status"], C.KEPT)
check("the config is the old one plus the change, and no timer left behind",
      r.config, before + [ADD])
check("released once, never discarded", (r.released, r.unrolled, r.dropped), (1, 0, 0))
writes = [t for t in r.typed if not t.startswith("/ping")]
ok("the revert timer is the first thing typed under Safe Mode, the removal the last",
   writes[0].startswith("/system scheduler add name=x3-change-")
   and 'on-event="/safe-mode unroll; ' in writes[0]
   and writes[-1].startswith("/system scheduler remove [find name=x3-change-"),
   writes)
ok("an add is one line: the add, its own removal put in the timer by the router, the id",
   writes[1].startswith(f":local i [{ADD}]; :local s [/system scheduler get [find name=x3-change-")
   and ":do {/ip address remove \" . $i . \"} on-error={}; \" . [:pick $s 19 [:len $s]])" in writes[1]
   and "; :put $i; :put [:len [/system scheduler get" in writes[1], writes[1])
ok("what is typed for an add does not grow with the adds before it",
   len(writes[1]) < len(ADD) + 330, len(writes[1]))
ok("the timer's script ends by removing the timer itself",
   writes[0].split('on-event="')[1].split('"')[0].endswith(
       ":do {/system scheduler remove [find name=" + writes[0].split("name=")[1].split()[0] + "]} on-error={}"),
   writes[0])
check("each command is reported, with what it added",
      res["commands"], [{"n": 1, "command": ADD, "ok": True, "said": "added *3"}])
check("the order of the run", [s for i, s in enumerate(steps) if i == 0 or steps[i - 1] != s],
      ["connect", "baseline", "snapshot", "safe-mode", "timer", "apply", "check", "keep"])
check("the report carries what the export gained", res["diff"], ["+" + ADD])
ok("the checklist ran before the change too", res["baseline"] == [
    {"name": "ping 192.0.2.9: at most 0% of 2 lost", "ok": True, "seen": "2/2 replies (0% lost)",
     "mark": "pass"}], res["baseline"])
ok("one write-capable session, no other", r.opened == [True], r.opened)

# --- reverted, every way -------------------------------------------------------------------

r = Router()
r.unreachable_ping.add("192.0.2.9")
before = list(r.config)
res, _ = run(r)
check("a failed check: reverted", res["status"], C.REVERTED)
check("and the config is exactly what it was", r.config, before)
check("discarded, not released", (r.released, r.unrolled), (0, 1))
ok("the reason names the check", "check failed" in res["why"], res["why"])
ok("the revert was checked against the snapshot, and the undo had done it all",
   res["revert"] == {"orderly": True, "verified": "same", "repaired": []}, res["revert"])
ok("no timer is left on the router", r.timer is None)

r = Router()
second = "/ip address add address=192.0.2.2/24 interface=nosuch"
r.refuse[second] = "input does not match any value of interface"
before = list(r.config)
res, _ = run(r, job_for(commands=(ADD, second)))
check("a command the console answers: reverted", res["status"], C.REVERTED)
check("the first command went back with it", r.config, before)
check("the report says which command and what the console said",
      res["failed_command"], {"n": 2, "command": second,
                              "said": "input does not match any value of interface"})
ok("the checklist is not run on a change that did not apply", res["checks"] == [])

r = Router()
r.unrecorded.add(ADD)
before = list(r.config)
res, _ = run(r)
ok("a command History records nothing for is not trusted: the run stops there",
   res["status"] == C.REVERTED and "no undoable change" in res["why"], res)
ok("and what it added is removed by id, since no undo will",
   r.config == before and res["revert"]["repaired"] == [
       "removed /ip address *3: Safe Mode's undo had left it in place"], res["revert"])

# --- what Safe Mode's undo does not take back ------------------------------------------------

ROUTE = "/ip route add dst-address=10.4.0.0/16 gateway=10.0.0.254"
FW_OFF = "/ip firewall filter disable [find comment=mgmt]"

r = Router()
r.gaps = {"add /ip route", "disabled /ip firewall filter"}
r.unreachable_ping.add("192.0.2.9")
before = list(r.config)
res, _ = run(r, job_for(commands=(ROUTE, FW_OFF, ADD)))
check("an undo that leaves a route and a disabled rule: still reverted", res["status"], C.REVERTED)
check("with the router exactly as it was", r.config, before)
check("and the run says what it had to undo itself",
      res["revert"]["repaired"],
      ["set /ip firewall filter *2 back to disabled=no: Safe Mode's undo had not restored it",
       "removed /ip route *3: Safe Mode's undo had left it in place"])
writes = [t for t in r.typed if not t.startswith("/ping")]
at = next(i for i, t in enumerate(writes) if t == FW_OFF)
ok("the state a disable is about to change is in the timer before the disable is typed",
   writes[at - 1].startswith(":local s [/system scheduler get [find name=x3-change-")
   and ":do {/ip firewall filter set *2 disabled=no} on-error={}" in writes[at - 1]
   and writes[at - 2].startswith(":foreach i in=[/ip firewall filter find comment=mgmt]"), writes[at - 2:at + 1])
ok("the report lists it", "undone by x3 itself: removed /ip route *3" in C.report(
    {**job_for(), "status": res["status"], "result": res}))

r = Router()
r.gaps = {"add /ip route", "disabled /ip firewall filter"}
CUT = "/ip address add address=192.0.2.77/24 interface=ether1"
r.cuts.add(CUT)
before = list(r.config)
scripts, naps = [], []


def timer_fires_on_third_nap(seconds):
    naps.append(seconds)
    if len(naps) == 3:
        scripts.append(r.timer["script"])
        r.timer_fires()


res, _ = run(r, job_for(commands=(ROUTE, FW_OFF, CUT)), sleep=timer_fires_on_third_nap)
check("the same gaps, and the path cut: the router's timer alone puts it all back",
      (res["status"], r.config), (C.REVERTED, before))
ok("because the timer carried the ways back, newest first, the cutting add's own included",
   scripts and scripts[0].startswith(
       "/safe-mode unroll; :do {/ip address remove *4} on-error={}; "
       ":do {/ip firewall filter set *2 disabled=no} on-error={}; "
       ":do {/ip route remove *3} on-error={}; :do {/system scheduler remove"), scripts)
ok("the session that made the change never touched the router again", r.opened == [True, False], r.opened)

r = Router()
for n in range(12):
    r.objects.append({"id": f"*A{n:X}", "menu": "/ip firewall filter", "disabled": False,
                      "line": f"/ip firewall filter add chain=forward comment=bulk-{n}"})
res, _ = run(r, job_for(commands=("/ip firewall filter disable [find comment=bulk]",)))
writes = [t for t in r.typed if t.startswith(":local s [")]
ok("many objects' ways back go in a few to a line, never one long line",
   res["status"] == C.KEPT and len(writes) == 3 and max(len(t) for t in writes) < 512, [len(t) for t in writes])


class LeavesAChange(Client):
    """A router whose undo misses something the run has no way back for."""

    def safe_mode_leave(self, *, keep_changes=True):
        super().safe_mode_leave(keep_changes=keep_changes)
        if not keep_changes:
            self.r.objects.append({"id": "*F0", "menu": "/ip dns", "disabled": False,
                                   "line": "/ip dns set servers=192.0.2.53"})


r = Router()
r.unreachable_ping.add("192.0.2.9")
job = job_for(commands=(ADD, "/ip address add address=192.0.2.2/24 interface=ether2"))
r.refuse[job["commands"][1]] = "failure: already have such address"
res = C.ChangeRun(job, connect=lambda w: LeavesAChange(r, w), emit=lambda e: None,
                  sleep=lambda s: None, reconnect_for_s=0.0).run()
check("something still different that nothing here can put back: not called reverted",
      res["status"], C.UNCERTAIN)
check("and the lines that differ are in the result", res["leftover"], ["+/ip dns set servers=192.0.2.53"])
text = C.report({**job, "status": res["status"], "result": res})
ok("the report shows them, and each command's own result",
   "+/ip dns set servers=192.0.2.53" in text
   and f"  ok    1. {ADD}" in text
   and "  FAIL  2. /ip address add address=192.0.2.2/24 interface=ether2 -> failure: already have such address" in text,
   text)

# --- what a check is expected to do before the change ----------------------------------------

GUARD = {"type": "ping", "address": "192.0.2.9", "count": 2, "before": "pass", "name": "upstream answers"}
GOAL = {"type": "rows", "menu": "ip address", "where": ["comment=new"], "before": "fail", "name": "the new address is there"}
PLAIN = {"type": "rows", "menu": "ip route", "name": "a route, nobody said what to expect"}

r = Router()
said = []
res, _ = run(r, job_for(checks=(GUARD, GOAL, PLAIN)), events=said)
check("before the change: a check meant to fail there reads as expected, not as a failure",
      [e["text"] for e in said if e["step"] == "baseline"][1:],
      ["pass: upstream answers (2/2 replies (0% lost))",
       "not yet, as expected before the change: the new address is there (0 row(s))",
       "not passing before the change: a route, nobody said what to expect (0 row(s))"])
check("and the result keeps which was which", [b["mark"] for b in res["baseline"]],
      ["pass", "expected", "not yet"])
text = C.report({**job_for(), "status": res["status"], "result": res})
ok("the report says so in words",
   "not yet (expected: the change should make it pass)  the new address is there" in text
   and "not passing yet  a route, nobody said what to expect" in text, text)

r = Router()
r.unreachable_ping.add("192.0.2.9")
before = list(r.config)
res, _ = run(r, job_for(checks=(GUARD, GOAL)))
check("a guard that already fails stops the run before anything is touched",
      (res["status"], r.safe, r.config == before, [t for t in r.typed if not t.startswith(("/ping", "/ip address print"))]),
      (C.NOT_APPLIED, False, True, []))
ok("and says which check, and that it was failing before the change",
   "upstream answers" in res["why"] and "already fails before the change" in res["why"], res["why"])

r = Router()
r.rows["/ip address"] = 1
said = []
res, _ = run(r, job_for(checks=(GOAL,)), events=said)
ok("a check meant to fail before that already passes is called out: it proves nothing",
   res["baseline"][0]["mark"] == "proves nothing"
   and any("already passes before the change" in e["text"] for e in said), res["baseline"])
check("a check says what it is there for, to the person approving",
      [C.check_label(C.check_check(c)) for c in (GUARD, GOAL, PLAIN)],
      ["upstream answers  (must hold before and after)",
       "the new address is there  (the change should make this pass)",
       "a route, nobody said what to expect"])
try:
    C.check_check({**GUARD, "before": "maybe"})
    ok("'before' is pass or fail", False)
except ToolError:
    ok("'before' is pass or fail", True)

# --- a check that only warns ------------------------------------------------------------------

WARN = {"type": "ping", "address": "192.0.2.50", "count": 2, "on_fail": "warn", "name": "the monitor answers"}
r = Router()
r.unreachable_ping.add("192.0.2.50")
said = []
res, _ = run(r, job_for(checks=(PING, WARN)), events=said)
check("a warning that fails does not revert: the change is kept", (res["status"], r.released, r.unrolled),
      (C.KEPT, 1, 0))
check("and it is in the result as a warning, not lost",
      (res["warnings"], res["checks"][1]["warn"]), (["the monitor answers (0/2 replies (100% lost))"], True))
ok("said as it happens, and in the reason", any(e.get("warn") for e in said)
   and "Warnings, which do not revert: the monitor answers" in res["why"], res["why"])
text = C.report({**job_for(), "status": res["status"], "result": res})
ok("the report opens with it and marks the check",
   "KEPT, WITH WARNINGS" in text and "WARNING (does not revert)  the monitor answers" in text, text)

r = Router()
r.unreachable_ping.update({"192.0.2.50", "192.0.2.9"})
res, _ = run(r, job_for(checks=(WARN, PING)))
check("a warning does not shield the check after it: that one still reverts", res["status"], C.REVERTED)

r = Router()
r.unreachable_ping.add("192.0.2.50")
res, _ = run(r, job_for(checks=({**WARN, "before": "pass"}, PING)))
check("a warning that already fails before the change does not stop the run either", res["status"], C.KEPT)
ok("it is labelled for the person approving",
   C.check_label(C.check_check(WARN)) == "the monitor answers  (warning only: does not revert)")
try:
    C.check_check({**WARN, "on_fail": "ignore"})
    ok("'on_fail' is revert or warn", False)
except ToolError:
    ok("'on_fail' is revert or warn", True)

# --- long lines, and a timer that is not what was sent -----------------------------------------

LONG = "/ip address add address=192.0.2.9/24 interface=ether2 comment=\"" + "takes over " * 50 + "\""
r = Router()
res, _ = run(r, job_for(commands=(ADD, LONG)))
ok("a command longer than the terminal is wide is not mistaken for its own echo",
   res["status"] == C.KEPT and res["commands"][1] == {"n": 2, "command": LONG, "ok": True,
                                                       "said": "added *4"}, res)


class OldParser(Client):
    """Checks a whole line before running any of it: a syntax error prints no
    marker, only the message, after the echo."""

    def run_console(self, command, *, timeout):
        if "nosuchparam" in command:
            self.r.typed.append(command)
            column = command.index("nosuchparam") + 1
            return ConfigExport(text=command[-60:] + f"\nbad parameter nosuchparam (line 1 column {column})",
                                complete=True, command=command)
        return super().run_console(command, timeout=timeout)


r = Router()
BAD = "/ip address add address=192.0.2.9/24 nosuchparam=1 interface=ether2"
res = C.ChangeRun(job_for(commands=(BAD,)), connect=lambda w: OldParser(r, w),
                  emit=lambda e: None, sleep=lambda s: None).run()
check("a syntax error with no marker is still the router's message, its column in the command's own terms",
      (res["status"], res["commands"][0]["said"]),
      (C.REVERTED, f"bad parameter nosuchparam (line 1 column {BAD.index('nosuchparam') + 1})"))

r = Router()
r.miscount = 7
before = list(r.config)
res, _ = run(r)
ok("a timer the router holds differently from what was sent stops the run, and it is undone",
   res["status"] == C.REVERTED and "not what was sent" in res["why"] and r.config == before, res)

ok("an export check counts matching lines, here and not on the router",
   C.run_check(Client(Router(), False), C.check_check(
       {"type": "export", "match": "chain=input.*comment=mgmt", "section": "/ip firewall filter"}))
   == (True, "1 line(s)")
   and C.run_check(Client(Router(), False), C.check_check(
       {"type": "export", "match": "comment=nothere"})) == (False, "0 line(s)"))

r = Router()
cancel = threading.Event()
slept = []


def press_revert_while_settling(seconds):
    slept.append(seconds)
    cancel.set()


before = list(r.config)
res, _ = run(r, job_for(settle=5), cancel=cancel, sleep=press_revert_while_settling)
check("Revert pressed while it settles: reverted", res["status"], C.REVERTED)
check("and the router is as it was", r.config, before)
ok("within one slice of the wait, not after all of it", len(slept) == 1 and slept[0] <= 0.5, slept)

r = Router()
cancel = threading.Event()
cancel.set()
res, _ = run(r, cancel=cancel)
check("Revert pressed before Safe Mode: nothing applied", res["status"], C.NOT_APPLIED)
ok("and nothing typed but the baseline", all(t.startswith("/ping") for t in r.typed), r.typed)

# --- before anything is changed ---------------------------------------------------------------

r = Router(version="7.15.3")
res, _ = run(r)
check("a router too old for the timer is not touched", res["status"], C.NOT_APPLIED)
ok("and is told why", "7.18" in res["why"], res["why"])
ok("no Safe Mode, no change typed", not r.safe and all(t.startswith("/ping") for t in r.typed))

r = Router()
r.cut = True
res, _ = run(r)
check("cannot connect: nothing applied", res["status"], C.NOT_APPLIED)

r = Router()
res, _ = run(r, job_for(checks=({"type": "ping", "address": "192.0.2.9", "interface": "ether9"},)))
check("a check naming an interface the router lacks stops before any change",
      (res["status"], r.safe, r.config == Router().config), (C.NOT_APPLIED, False, True))

r = Router()
timer_cmd = None


class NoTimer(Client):
    def run_console(self, command, *, timeout):
        if command.removeprefix(_MARK_TYPED).startswith("/system scheduler add"):
            self.r.typed.append(command)
            return ConfigExport(text=_MARK_PRINTED + "failure: not enough permissions",
                                complete=True, command=command)
        return super().run_console(command, timeout=timeout)


said = []
res = C.ChangeRun(job_for(), connect=lambda w: NoTimer(r, w), emit=said.append,
                  sleep=lambda s: None).run()
ok("no timer, no change: the commands are never typed",
   res["status"] == C.REVERTED and ADD not in r.typed and ADD not in r.config, (res, r.typed))

# --- the change cuts its own path -----------------------------------------------------------------

r = Router()
r.cuts.add(ADD)
before = list(r.config)
naps = []


def router_timer_fires_on_third_nap(seconds):
    naps.append(seconds)
    if len(naps) == 3:
        r.timer_fires()


res, _ = run(r, sleep=router_timer_fires_on_third_nap)
check("a change that cuts the connection: reverted, by the router's own timer",
      res["status"], C.REVERTED)
check("the session was cut, not released", (r.released, r.dropped >= 1), (0, True))
check("and the old config was seen again before saying so", r.config, before)
ok("looked at with a fresh read-only session", r.opened == [True, False], r.opened)
ok("an orderly discard was not claimed",
   res["revert"] == {"orderly": False, "verified": "same", "repaired": []}, res["revert"])
ok("close() never ran with changes held", not getattr(r, "kept_by_close", False))

r = Router()
r.cuts.add(ADD)
job = job_for()
res = C.ChangeRun(job, connect=lambda w: Client(r, w), emit=lambda e: None,
                  sleep=lambda s: None, reconnect_for_s=0.0).run()
check("never reachable again: not called reverted", res["status"], C.UNCERTAIN)
ok("and says a person must look", "could not be reached" in res["why"], res["why"])

# --- the timer fired under a run that was still going ----------------------------------------------

r = Router()


class TimerFiresDuringChecks(Client):
    def run_console(self, command, *, timeout):
        if command.removeprefix(_MARK_TYPED).startswith("/ping") and self.r.safe and ADD in self.r.config:
            self.r.timer_fires()   # the scheduler ran its script
        return super().run_console(command, timeout=timeout)


before = list(r.config)
res = C.ChangeRun(job_for(), connect=lambda w: TimerFiresDuringChecks(r, w),
                  emit=lambda e: None, sleep=lambda s: None).run()
ok("the timer fired mid-run: never reported as kept",
   res["status"] != C.KEPT and r.released == 0, res)

check("the deadline covers what the run may wait for, with a margin",
      C.deadline_s(job_for(checks=({"type": "ping", "address": "192.0.2.9", "count": 5,
                                    "within_s": 120},), settle=30)),
      30 + 8 + (15 + 10 + 120) + C.DEADLINE_MARGIN_S)
check("and is never shorter than three minutes", C.deadline_s(job_for()), C.DEADLINE_MIN_S)

# --- the job file and the policy -------------------------------------------------------------------

job = job_for()
C.save(job)
check("a job reads back as written", C.load(job["id"])["commands"], [ADD])
ok("a waiting job with a fresh heartbeat is this device's active one",
   C.active_for("10.9.9.9", "admin")["id"] == job["id"])
job["beat"] = time.time() - 60
C.save(job)
ok("one whose window died is not", C.active_for("10.9.9.9", "admin") is None)
ok("and its report says the window is gone", "window is gone" in C.report(job))
try:
    C.load("../../etc/passwd")
    ok("a job id is not a path", False)
except ToolError:
    ok("a job id is not a path", True)

check("no policy until someone decides", C.policy_for("10.9.9.9", "admin"), "")
C.set_policy("10.9.9.9", "admin", C.NEVER)
check("never is remembered, whatever the case", C.policy_for("10.9.9.9", "ADMIN"), C.NEVER)
check("per login as well as per host", C.policy_for("10.9.9.9", "other"), "")

# --- the tools ---------------------------------------------------------------------------------------


class Conn:
    def __init__(self, host, login="admin"):
        self.host, self.login = host, login


class Book:
    def devices(self):
        return []

    def resolve(self, device):
        return Conn({"edge": "10.9.9.9", "core": "10.8.8.8"}[device])

    def run(self, device, fn):
        raise AssertionError("propose_change must not open a session")

    def close(self):
        pass


launched: list[str] = []
box = T.ToolBox(Book(), launch_change=launched.append)
names = [d["name"] for d, _f in box.definitions()]
ok("the change tools are offered when there is a window to approve in",
   names[-2:] == ["propose_change", "change_status"])
ok("and not otherwise", "propose_change" not in [d["name"] for d, _f in T.ToolBox(Book()).definitions()])

ask = {"device": "core", "summary": "why", "commands": [ADD], "checks": [PING]}
try:
    box.propose_change({**ask, "device": "edge"})
    ok("a router the user closed is refused without a window", False)
except ToolError as e:
    ok("a router the user closed is refused without a window",
       "closed" in str(e) and launched == [], str(e))
for missing, label in [("summary", "a summary"), ("commands", "commands"), ("checks", "a checklist")]:
    try:
        box.propose_change({k: v for k, v in ask.items() if k != missing})
        ok(f"a proposal needs {label}", False)
    except ToolError:
        ok(f"a proposal needs {label}", launched == [])
try:
    box.propose_change({**ask, "commands": [ADD, "/system reboot"]})
    ok("one bad command refuses the whole proposal", False)
except ToolError:
    ok("one bad command refuses the whole proposal", launched == [])

try:
    box.propose_change({**ask, "checks": [{**PING, "on_fail": "warn"}]})
    ok("a proposal with only warnings is refused: nothing would decide", False)
except ToolError as e:
    ok("a proposal with only warnings is refused: nothing would decide",
       "At least one" in str(e) and launched == [], str(e))

said = box.propose_change(ask)
job_id = launched[0]
ok("a good proposal opens the window and says nothing was sent",
   job_id in said and "Nothing has been sent" in said, said)
check("the job waits, with the commands as they will be typed",
      (C.load(job_id)["status"], C.load(job_id)["commands"]), (C.WAITING, [ADD]))
try:
    box.propose_change(ask)
    ok("one change at a time per router", False)
except ToolError as e:
    ok("one change at a time per router", job_id in str(e), str(e))
ok("change_status: waiting on the user", "waiting for the user" in box.change_status({"id": job_id}))

job = C.load(job_id)
job["status"], job["note"] = C.REJECTED, "not during business hours"
job["result"] = {"status": C.REJECTED, "why": "The user rejected."}
C.save(job)
status = box.change_status({"id": job_id})
ok("a rejection comes back with the user's note",
   "REJECTED" in status and "not during business hours" in status, status)


def no_window(job_id):
    raise ToolError("there is no desktop session here to show it on")


try:
    T.ToolBox(Book(), launch_change=no_window).propose_change(ask)
    ok("no desktop: the proposal fails and says so", False)
except ToolError as e:
    ok("no desktop: the proposal fails and says so", "could not be opened" in str(e))
ok("and leaves no job waiting", C.active_for("10.8.8.8", "admin") is None)

# --- the window ------------------------------------------------------------------------------------------

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from x3.ui.widgets import change_window as W  # noqa: E402

app = QApplication.instance() or QApplication([])
W.ARM_AFTER_MS = 50


def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def until(cond, ms=5000):
    end = time.monotonic() + ms / 1000
    while not cond() and time.monotonic() < end:
        spin(20)
    return cond()


def window_for(router, job=None, first_time=True):
    job = job or job_for()
    job["host"] = f"10.7.7.{len(launched) + window_for.n}"
    window_for.n += 1
    C.save(job)
    win = W.ChangeWindow(
        job, first_time=first_time,
        make_run=lambda j, emit, cancel: C.ChangeRun(
            j, connect=lambda w: Client(router, w), emit=emit, cancel=cancel,
            sleep=lambda s: time.sleep(min(s, 0.01))),
    )
    win.show()
    return win, job


window_for.n = 0

r = Router()
win, job = window_for(r)
ok("the buttons are dead when the window appears, and it says for how long",
   not win.approve_btn.isEnabled() and not win.reject_btn.isEnabled()
   and "unlock" in win.lock_note.text(), win.lock_note.text())
win.reject()                     # an Esc that was on its way to another window
win.close()                      # and a close meant for one
ok("neither Esc nor a close decides anything while it is locked",
   win.job["status"] == C.WAITING and win.isVisible() and C.load(job["id"])["status"] == C.WAITING)
ok("the steps pane is there from the start, beside the proposal, waiting",
   win.log.isVisible() and win.state.text() == "Waiting for your decision"
   and not win.revert_btn.isEnabled())
ok("the commands are shown as they will be typed", ADD in win.commands.toPlainText())
spin(120)
ok("and live a moment later", win.approve_btn.isEnabled())
ok("Enter presses nothing: no button is the default",
   not any(b.isDefault() or b.autoDefault()
           for b in (win.reject_btn, win.approve_btn, win.never_btn, win.close_btn, win.revert_btn)))
size = win.size()
win.note.setText("go ahead")
win.approve_btn.click()
ok("approved: the run ends kept", until(lambda: win.job["status"] in C.FINAL)
   and win.job["status"] == C.KEPT, win.job["status"])
on_disk = C.load(job["id"])
ok("the job file has the result, the note and the steps",
   on_disk["status"] == C.KEPT and on_disk["note"] == "go ahead"
   and any(e["step"] == "timer" for e in on_disk["events"]), on_disk)
ok("Revert is dead once it is kept, Close is live",
   not win.revert_btn.isEnabled() and win.close_btn.isEnabled())
ok("the window is the size it opened at: approving resized nothing", win.size() == size)
check("approving once does not open the router for good", C.policy_for(job["host"], "admin"), "")
win.close()

r = Router()
win, job = window_for(r)
spin(120)
win.remember.setChecked(True)
win.approve_btn.click()
until(lambda: win.job["status"] in C.FINAL)
check("ticking the box stops the first-time flag", C.policy_for(job["host"], "admin"), C.ALWAYS)
win.close()

r = Router()
win, job = window_for(r)
spin(120)
win.note.setText("wrong router")
win.reject_btn.click()
on_disk = C.load(job["id"])
check("rejected: written, with the note, and nothing typed",
      (on_disk["status"], on_disk["note"], r.typed), (C.REJECTED, "wrong router", []))

r = Router()
win, job = window_for(r)
spin(120)
win.never_btn.click()
check("Never: rejected, and the router closed to proposals",
      (C.load(job["id"])["status"], C.policy_for(job["host"], "admin")), (C.REJECTED, C.NEVER))

r = Router()
win, job = window_for(r)
spin(120)
win.close()
check("closing the window without deciding rejects",
      (C.load(job["id"])["status"], r.typed), (C.REJECTED, []))

r = Router()
gate = threading.Event()


class Slow(Client):
    def run_console(self, command, *, timeout):
        if command == ADD:
            got = super().run_console(command, timeout=timeout)
            gate.wait(5)    # the change is in; the run is now mid-flight
            return got
        return super().run_console(command, timeout=timeout)


job = job_for(settle=30)
job["host"] = "10.7.7.200"
C.save(job)
before = list(r.config)
win = W.ChangeWindow(job, first_time=False, make_run=lambda j, emit, cancel: C.ChangeRun(
    j, connect=lambda w: Slow(r, w), emit=emit, cancel=cancel))
win.show()
spin(120)
win.approve_btn.click()
until(lambda: ADD in r.config)
win.close()                      # the person closes the window mid-change
ok("closing mid-change does not close: it reverts first", win.isVisible() and win.job["status"] == C.RUNNING)
gate.set()
ok("and then the window goes", until(lambda: not win.isVisible()))
check("with the router as it was", (C.load(job["id"])["status"], r.config), (C.REVERTED, before))

print()
if failures:
    print(f"{len(failures)} failure(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("all change checks passed")
