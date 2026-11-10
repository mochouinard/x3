"""Changes an assistant proposes, a person approves, and Safe Mode guards.

The MCP server itself never writes to a router. What it can do is *propose*:
write a job file and open the x3 change window (:mod:`x3.mcp.approve`),
a separate process with a person in front of it. Nothing is sent until that
person presses Approve, and then the change runs like this::

    connect -> checklist once (baseline) -> export snapshot -> Safe Mode
            -> type each command -> wait -> checklist until it passes
            -> all pass: leave Safe Mode, keeping the change
            -> anything else: discard, and prove it by comparing exports

"Anything else" is every way this can go wrong: a command the console
answers at all (a write that worked prints nothing), a command Safe Mode
recorded nothing for (so it could not take it back), a check that fails, the
person pressing Revert, the connection dying, this process dying.

Safe Mode alone does not cover the last two. The router undoes what Safe Mode
holds when its session drops, but a change that cuts the path to the router
leaves the session looking alive from the router's side: on a 7.23.3 lab
router the change was still held twelve minutes later. So the first thing typed under
Safe Mode is a timer on the router itself, a scheduler entry that runs
``/safe-mode unroll`` at the deadline. It takes back everything Safe Mode
holds, itself included, whoever is or is not still connected. A change that
passes removes the timer just before Safe Mode is released.

This module is the half without Qt: the job file, what a command and a check
may be, the per-device policy, and the run itself.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from x3.proto import configexport as cexp
from x3.proto.device import DeviceClient, DeviceError

from ..ui import settings
from . import diag
from .protocol import ToolError

# --- limits -------------------------------------------------------------------

MAX_COMMANDS = 50
MAX_COMMAND_LEN = 2000
MAX_CHECKS = 20
MAX_SETTLE_S = 300
MAX_WITHIN_S = 600
#: Seconds between tries of a check that has not passed yet.
RETRY_EVERY_S = 3.0
#: A job nobody's window is tending: its heartbeat is this old.
STALE_AFTER_S = 20.0
#: How long to keep trying to get back in after the connection died, to see
#: whether the router rolled the change back. Past the router's own timer.
RECONNECT_AFTER_DEADLINE_S = 60.0
#: Time added to what the run is expected to take, to make the deadline of
#: the router's own revert timer; and the least a deadline may be.
DEADLINE_MARGIN_S = 90
DEADLINE_MIN_S = 180
#: ``/safe-mode unroll`` exists from this version on. Without it there is no
#: router-side timer, and without the timer no change is made.
TIMER_NEEDS = (7, 18)

#: What a command may do. A change is one of these on a menu; everything the
#: console can do besides (reboot, reset, import, fetch, run a script) has no
#: such verb, so it is refused without a list of its own.
WRITE_VERBS = frozenset({"add", "set", "remove", "enable", "disable", "move", "unset", "comment"})

#: Menus no proposal may touch, by path prefix. Either Safe Mode cannot take
#: the change back (files, packages, certificates), or it changes who can log
#: in, or what it adds runs later, outside anything Safe Mode holds.
BLOCKED_MENUS = (
    "/user", "/password", "/file", "/disk", "/certificate", "/console", "/port",
    "/special-login", "/system package", "/system routerboard", "/system backup",
    "/system license", "/system script", "/system scheduler", "/system upgrade",
    "/system reset-configuration", "/system device-mode", "/container", "/partitions",
)

#: Job states. The first two are still moving; the rest are final.
WAITING, RUNNING = "waiting", "running"
REJECTED, KEPT, REVERTED, NOT_APPLIED, UNCERTAIN = (
    "rejected", "kept", "reverted", "not_applied", "uncertain",
)
FINAL = frozenset({REJECTED, KEPT, REVERTED, NOT_APPLIED, UNCERTAIN})

_WORD = re.compile(r"[a-z][a-z0-9-]*")
#: What brackets may hold. Nesting outside quotes is refused by the scan that
#: uses this, so whatever follows the word is the find's own conditions.
_FIND = re.compile(r"\s*find\b.*")


# --- what a command may be ------------------------------------------------------

def check_command(line: object) -> str:
    """``line`` as it will be typed, or a ToolError saying what is wrong.

    The person approving reads the exact text, and that is the real gate.
    This one keeps out what would make the text mean something other than
    what it reads as: a second command behind a ``;``, a value computed on
    the router, a ``?`` that opens help at a terminal instead of being typed.
    """
    text = str(line or "").strip()
    if not text:
        raise ToolError("An empty command.")
    if len(text) > MAX_COMMAND_LEN:
        raise ToolError(f"A command is at most {MAX_COMMAND_LEN} characters.")
    bad = sorted({c for c in text if not " " <= c <= "~"})
    if bad:
        raise ToolError(
            f"{text[:60]!r}: only plain ASCII on one line can be typed at the "
            f"console (found {', '.join(repr(c) for c in bad[:5])})."
        )
    if not text.startswith("/"):
        raise ToolError(f"{text[:60]!r}: a command starts with its full menu path, e.g. '/ip address add ...'.")
    if "?" in text:
        raise ToolError(f"{text[:60]!r}: '?' opens help at a console. Write it as \\3F inside the quotes.")
    if "$" in text:
        raise ToolError(f"{text[:60]!r}: no variables or computed values ($).")
    # Outside quotes: one command, no blocks, and brackets only around a find.
    depth_start: int | None = None
    quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if quoted:
            if c == "\\":
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c in ";{}":
            raise ToolError(f"{text[:60]!r}: one command per entry, without ';' or braces outside quotes.")
        elif c == "[":
            if depth_start is not None:
                raise ToolError(f"{text[:60]!r}: brackets do not nest here.")
            depth_start = i
        elif c == "]":
            if depth_start is None:
                raise ToolError(f"{text[:60]!r}: a ']' without its '['.")
            if not _FIND.fullmatch(text[depth_start + 1:i]):
                raise ToolError(f"{text[:60]!r}: brackets may only hold a find, e.g. [find comment=x].")
            depth_start = None
        i += 1
    if quoted or depth_start is not None:
        raise ToolError(f"{text[:60]!r}: an unclosed quote or bracket. The console would wait for the rest.")
    menu, verb = _menu_and_verb(text)
    if verb is None:
        raise ToolError(
            f"{text[:60]!r}: a change is {', '.join(sorted(WRITE_VERBS))} on a menu. "
            "Nothing else is typed for you."
        )
    for blocked in BLOCKED_MENUS:
        if menu == blocked or menu.startswith(blocked + " "):
            raise ToolError(
                f"{text[:60]!r}: {blocked} is not changed through proposals. "
                "Safe Mode could not be relied on to take it back."
            )
    return text


def _menu_and_verb(text: str) -> tuple[str, str | None]:
    """``/ip/address add ...`` -> ("/ip address", "add"); verb None if the
    words run out, or stop being words, before one is found."""
    words: list[str] = []
    for word in re.split(r"[\s/]+", text.lstrip("/")):
        if not _WORD.fullmatch(word):
            return "/" + " ".join(words), None
        if word in WRITE_VERBS and words:
            return "/" + " ".join(words), word
        words.append(word)
    return "/" + " ".join(words), None


# --- what a check may be --------------------------------------------------------

def check_check(raw: object) -> dict[str, Any]:
    """One checklist entry, normalised. Two kinds:

    ``{"type": "ping", "address": ..., "count": 5, "max_loss_pct": 0}``
        pinged from the router; optional src_address, routing_table, interface.
    ``{"type": "rows", "menu": "/routing bgp session", "where": [...], "min": 1}``
        how many rows a console_print would return; optional max.

    Either takes ``within_s``: keep trying this long before calling it
    failed (a BGP session takes a while to come back), and ``name``.
    """
    if not isinstance(raw, dict):
        raise ToolError("A check is an object with a 'type'.")
    kind = str(raw.get("type") or "").lower()
    out: dict[str, Any] = {"type": kind}
    out["within_s"] = diag.number(raw.get("within_s"), "within_s", 0, MAX_WITHIN_S, 0)
    if kind == "ping":
        out["address"] = diag.address(raw.get("address"))
        out["count"] = diag.number(raw.get("count"), "count", 1, 50, 5)
        out["max_loss_pct"] = diag.number(raw.get("max_loss_pct"), "max_loss_pct", 0, 99, 0)
        if raw.get("src_address"):
            out["src_address"] = diag.address(raw["src_address"], "src_address")
        if raw.get("routing_table"):
            out["routing_table"] = diag.routing_table(raw["routing_table"])
        if raw.get("interface"):
            out["interface"] = str(raw["interface"]).strip()
    elif kind == "rows":
        out["menu"] = diag.print_menu(raw.get("menu"))
        where = raw.get("where") or []
        if isinstance(where, str):
            where = [where]
        out["where"] = [diag.condition(c) for c in where]
        lo = raw.get("min")
        hi = raw.get("max")
        if lo is None and hi is None:
            lo = 1
        out["min"] = None if lo is None else diag.number(lo, "min", 0, 10_000_000, 0)
        out["max"] = None if hi is None else diag.number(hi, "max", 0, 10_000_000, 0)
    else:
        raise ToolError("A check's type is 'ping' or 'rows'.")
    name = str(raw.get("name") or "").strip()
    out["name"] = name[:80] if name else describe_check(out)
    return out


def describe_check(c: dict[str, Any]) -> str:
    """A check in words, for the person approving and for the report."""
    if c["type"] == "ping":
        text = f"ping {c['address']}: at most {c['max_loss_pct']}% of {c['count']} lost"
        extras = [f"{k.replace('_', ' ')} {c[k]}" for k in ("src_address", "routing_table", "interface") if c.get(k)]
        if extras:
            text += " (" + ", ".join(extras) + ")"
    else:
        want = []
        if c.get("min") is not None:
            want.append(f"at least {c['min']}")
        if c.get("max") is not None:
            want.append(f"at most {c['max']}")
        text = f"{c['menu']}: {' and '.join(want)} row(s)"
        if c.get("where"):
            text += " where " + " and ".join(c["where"])
    if c.get("within_s"):
        text += f", within {c['within_s']}s"
    return text


def deadline_s(job: dict[str, Any]) -> int:
    """Seconds after Safe Mode is taken at which the router reverts by
    itself: everything the run can wait for, each check's own time, and a
    margin. The run gives up, and reverts, before this is reached."""
    total = int(job.get("settle_s") or 0) + 8 * len(job.get("commands") or [])
    for c in job.get("checks") or []:
        once = 15 + 2 * c["count"] if c["type"] == "ping" else 20
        total += once + int(c.get("within_s") or 0)
    return max(DEADLINE_MIN_S, total + DEADLINE_MARGIN_S)


def timer_name(job: dict[str, Any]) -> str:
    return f"x3-change-{job['id'][-4:]}"


# --- the job file -----------------------------------------------------------------

def jobs_dir() -> Path:
    return settings.data_dir() / "changes"


def job_path(job_id: str) -> Path:
    if not re.fullmatch(r"c-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}", job_id or ""):
        raise ToolError(f"{job_id!r} is not a change id.")
    return jobs_dir() / f"{job_id}.json"


def new_job(*, device: str, host: str, login: str, summary: str, commands: list[str],
            checks: list[dict[str, Any]], settle_s: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "id": f"c-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
        "created": now.isoformat(timespec="seconds"),
        "device": device, "host": host, "login": login,
        "summary": summary, "commands": commands, "checks": checks,
        "settle_s": settle_s,
        "status": WAITING, "note": "", "events": [], "result": {},
        "beat": time.time(),
    }


def save(job: dict[str, Any]) -> None:
    """Whole file, replaced in one step: a reader never sees half a job."""
    directory = jobs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = directory / f"{job['id']}.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load(job_id: str) -> dict[str, Any]:
    path = job_path(job_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ToolError(f"No change {job_id}.") from None
    except (OSError, ValueError) as e:
        raise ToolError(f"Change {job_id} cannot be read: {e}") from None


def abandoned(job: dict[str, Any]) -> bool:
    """Still marked as moving, but no window has touched it lately."""
    return job.get("status") not in FINAL and time.time() - float(job.get("beat") or 0) > STALE_AFTER_S


def active_for(host: str, login: str) -> dict[str, Any] | None:
    """A change for this device that is still waiting or running."""
    directory = jobs_dir()
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("c-*.json"), reverse=True)[:200]:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (job.get("host"), job.get("login")) == (host, login) \
                and job.get("status") not in FINAL and not abandoned(job):
            return job
    return None


# --- per-device policy --------------------------------------------------------------

ALWAYS, NEVER = "always", "never"


def _policy_path() -> Path:
    return settings.config_dir() / "mcp-change-policy.json"


def _policy_key(host: str, login: str) -> str:
    return f"{login}@{host}".lower()


def policy_for(host: str, login: str) -> str:
    """"always", "never", or "" for a device nobody has decided about."""
    try:
        data = json.loads(_policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(_policy_key(host, login)) if isinstance(data, dict) else ""
    return value if value in (ALWAYS, NEVER) else ""


def set_policy(host: str, login: str, value: str) -> None:
    path = _policy_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    key = _policy_key(host, login)
    if value in (ALWAYS, NEVER):
        data[key] = value
    else:
        data.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --- running a check -----------------------------------------------------------------

_PING_SUMMARY = re.compile(r"sent=(\d+)\s+received=(\d+)")


def _output_lines(result: Any) -> list[str]:
    """What a console command printed, without log lines the router echoed in."""
    return [ln.strip() for ln in (result.text or "").splitlines()
            if ln.strip() and not cexp.LOG_LINE_RE.match(ln.strip())]


def run_check(client: DeviceClient, c: dict[str, Any]) -> tuple[bool, str]:
    """One try of one check: (passed, what was seen)."""
    if c["type"] == "ping":
        iface = None
        if c.get("interface"):
            names = [name for _id, name, *_rest in client.interface_choices()]
            iface = diag.interface(c["interface"], names)
        command = diag.ping(c["address"], count=c["count"], interface=iface,
                            src_address=c.get("src_address"), routing_table=c.get("routing_table"))
        got = client.run_console(command, timeout=15 + 2 * c["count"])
        lines = _output_lines(got)
        for ln in reversed(lines):
            m = _PING_SUMMARY.search(ln)
            if m:
                sent, received = int(m.group(1)), int(m.group(2))
                loss = 100.0 * (sent - received) / sent if sent else 100.0
                return loss <= c["max_loss_pct"], f"{received}/{sent} replies ({loss:.0f}% lost)"
        return False, "no ping summary: " + (lines[-1] if lines else "no output")
    command = diag.print_command(c["menu"], c["where"], count_only=True)
    got = client.run_console(command, timeout=60)
    lines = _output_lines(got)
    try:
        n = int(lines[-1]) if lines else None
    except ValueError:
        n = None
    if n is None or got.console_error:
        return False, "could not count: " + (got.console_error or (lines[-1] if lines else "no output"))
    ok = (c.get("min") is None or n >= c["min"]) and (c.get("max") is None or n <= c["max"])
    return ok, f"{n} row(s)"


# --- the run -----------------------------------------------------------------------------

class _Revert(Exception):
    """Raised inside the run to say: undo it. Carries the reason."""


class ChangeRun:
    """One approved change, from connect to kept or reverted.

    ``connect(write)`` returns a connected DeviceClient: write-capable for
    the change itself, read-only for looking at the router afterwards.
    ``emit(event)`` is told each step as it happens. ``cancel`` is the Revert
    button: set at any time before the change is kept, and it is undone.
    ``snapshot(result, reason)`` files an export in x3's snapshot store.
    """

    def __init__(
        self,
        job: dict[str, Any],
        *,
        connect: Callable[[bool], DeviceClient],
        emit: Callable[[dict[str, Any]], None],
        cancel: threading.Event | None = None,
        snapshot: Callable[[Any, DeviceClient, str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        reconnect_for_s: float | None = None,
    ) -> None:
        self.job = job
        self._connect = connect
        self._emit = emit
        self.cancel = cancel or threading.Event()
        self._snapshot = snapshot
        self._sleep = sleep
        self._reconnect_for_s = reconnect_for_s
        self.client: DeviceClient | None = None
        self._before: Any = None
        self._held = False
        #: When the router's own timer fires (monotonic), once it is set.
        self._timer_at: float | None = None

    # -- reporting

    def _say(self, step: str, text: str, **more: Any) -> None:
        self._emit({"t": time.time(), "step": step, "text": text, **more})

    def _wait(self, seconds: float) -> None:
        """Sleep in short slices so Revert is answered within a second."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._still_time()
            self._sleep(min(0.5, max(0.0, end - time.monotonic())))

    def _still_time(self) -> None:
        """Raise if the person pressed Revert, or the router's own timer is
        about to fire: better to undo it in order than race the timer."""
        if self.cancel.is_set():
            raise _Revert("you pressed Revert")
        if self._timer_at is not None and time.monotonic() > self._timer_at - 30:
            raise _Revert("the checks did not finish before the revert deadline")

    # -- the run

    def run(self) -> dict[str, Any]:
        """Do it. Returns the result: ``status`` plus what was seen."""
        result: dict[str, Any] = {"baseline": [], "checks": [], "applied": 0}
        try:
            status, why = self._run(result)
        finally:
            client, self.client = self.client, None
            if client is not None:
                try:
                    # Safe Mode is never still held here on a kept change;
                    # on any other path it was discarded or the session cut.
                    client.close() if not self._held else client.drop_session()
                except Exception:  # noqa: BLE001
                    pass
        result["status"], result["why"] = status, why
        return result

    def _run(self, result: dict[str, Any]) -> tuple[str, str]:
        job = self.job
        checks = job["checks"]
        # 1. Before anything is changed: every way this can fail here leaves
        #    the router exactly as it was.
        try:
            self._say("connect", f"Connecting to {job['device']}")
            self.client = client = self._connect(True)
            self._say("baseline", "Running the checklist once, before the change")
            for c in checks:
                ok, seen = run_check(client, c)
                result["baseline"].append({"name": c["name"], "ok": ok, "seen": seen})
                self._say("baseline", f"{'pass' if ok else 'FAIL'}: {c['name']} ({seen})",
                          ok=ok, check=c["name"])
            version = cexp.parse_version(getattr(client.info, "version", "") or "")
            if version < TIMER_NEEDS:
                return NOT_APPLIED, (
                    "Nothing was changed: this router runs version "
                    f"{getattr(client.info, 'version', '') or '?'}, and the revert timer "
                    f"needs {TIMER_NEEDS[0]}.{TIMER_NEEDS[1]} or newer."
                )
            self._say("snapshot", "Saving the configuration as it is now")
            self._before = client.export_config(show_sensitive=False)
            if self._snapshot is not None:
                self._snapshot(self._before, client, f"before-{job['id'][-4:]}")
            if self.cancel.is_set():
                return NOT_APPLIED, "Stopped before anything was changed."
            self._say("safe-mode", "Taking Safe Mode")
            client.safe_mode_enter()
            self._held = True
            pending = client.safe_mode_pending()
            if pending is None:
                raise DeviceError("the router's change history cannot be read, so a "
                                  "discard could not be trusted")
            # The router's own way back, before anything else is typed: if
            # this session never returns, the timer undoes all of it.
            limit = deadline_s(job)
            timer = (f'/system scheduler add name={timer_name(job)} interval={limit}s '
                     f'on-event="/safe-mode unroll" comment="x3 change {job["id"]}: '
                     'reverts it unless the checks pass"')
            self._say("timer", f"Setting the router's own revert timer: {limit}s from now",
                      deadline_s=limit)
            got = client.run_console(timer, timeout=30)
            said = _output_lines(got)
            after = client.safe_mode_pending()
            if said or got.console_error or not got.complete or after is None or after <= pending:
                raise DeviceError("the revert timer could not be set: "
                                  + ("; ".join(said) or got.console_error or "no undoable change recorded"))
            self._timer_at = time.monotonic() + limit
            pending = after
        except (DeviceError, OSError, ToolError) as e:
            if self._held:
                return self._undo(result, f"Could not start: {e}")
            return NOT_APPLIED, f"Nothing was changed: {e}"

        # 2. The change, and the checks that decide whether it stays.
        try:
            for i, command in enumerate(job["commands"], 1):
                self._still_time()
                self._say("apply", f"{i}/{len(job['commands'])}: {command}")
                got = client.run_console(command, timeout=45)
                said = _output_lines(got)
                if said or got.console_error:
                    answer = "; ".join(said) or got.console_error
                    result["failed_command"] = {"n": i, "command": command, "said": answer}
                    raise _Revert(f"command {i} was refused: {answer}")
                if not got.complete:
                    result["failed_command"] = {"n": i, "command": command,
                                                "said": "no answer in 45s"}
                    raise _Revert(f"command {i} got no answer in 45s; it may have cut "
                                  "the connection to the router")
                result["applied"] = i
                now = client.safe_mode_pending()
                if now is None or now <= pending:
                    result["failed_command"] = {"n": i, "command": command,
                                                "said": "no undoable change recorded"}
                    raise _Revert(
                        f"the router recorded no undoable change for command {i}; "
                        "Safe Mode may not be able to take it back"
                    )
                pending = now
            if job.get("settle_s"):
                self._say("settle", f"Waiting {job['settle_s']}s for the network to settle")
                self._wait(job["settle_s"])
            for c in checks:
                ok, seen, tries = self._check_until(client, c)
                result["checks"].append({"name": c["name"], "ok": ok, "seen": seen, "tries": tries})
                self._say("check", f"{'pass' if ok else 'FAIL'}: {c['name']} ({seen})",
                          ok=ok, check=c["name"])
                if not ok:
                    raise _Revert(f"check failed: {c['name']} ({seen})")
            self._still_time()
            # 3. Everything passed. The timer goes first: released with it
            #    still there, it would fire later on a Safe Mode that is no
            #    longer this change's.
            got = client.run_console(f"/system scheduler remove [find name={timer_name(job)}]", timeout=30)
            said = _output_lines(got)
            now = client.safe_mode_pending()
            if said or got.console_error or not got.complete:
                raise _Revert("the revert timer could not be removed: "
                              + ("; ".join(said) or got.console_error or "no answer"))
            if now is None or now != pending + 1:
                # Fewer held than were made: the timer fired, or someone else
                # undid part of it. Not a state to keep.
                raise _Revert("what Safe Mode holds is not what this change made "
                              f"({now} held, {pending + 1} expected)")
        except _Revert as e:
            return self._undo(result, str(e))
        except (DeviceError, OSError, ToolError) as e:
            return self._undo(result, f"the connection or the router failed mid-change: {e}")

        try:
            client.safe_mode_leave(keep_changes=True)
            self._held = False
            self._timer_at = None
        except (DeviceError, OSError) as e:
            return self._undo(result, f"Safe Mode could not be left cleanly: {e}")
        self._say("keep", "All checks passed: Safe Mode released, the change is kept")
        try:
            after = client.export_config(show_sensitive=False)
            if self._snapshot is not None:
                self._snapshot(after, client, f"after-{job['id'][-4:]}")
            result["diff"] = _diff(self._before.text, after.text)
        except (DeviceError, OSError) as e:
            result["diff_error"] = str(e)
        return KEPT, "All checks passed."

    def _check_until(self, client: DeviceClient, c: dict[str, Any]) -> tuple[bool, str, int]:
        end = time.monotonic() + c.get("within_s", 0)
        tries = 0
        while True:
            tries += 1
            ok, seen = run_check(client, c)
            if ok or time.monotonic() >= end:
                return ok, seen, tries
            self._say("check", f"not yet: {c['name']} ({seen}); trying again", check=c["name"])
            self._wait(min(RETRY_EVERY_S, max(0.0, end - time.monotonic())))

    # -- undoing

    def _undo(self, result: dict[str, Any], why: str) -> tuple[str, str]:
        """Take the change back, then look at the router to see that it is."""
        self._say("revert", f"Reverting: {why}")
        client = self.client
        orderly = False
        if client is not None:
            try:
                client.safe_mode_leave(keep_changes=False)
                self._held = False
                orderly = True
            except Exception as e:  # noqa: BLE001 -- any failure takes the other exit
                self._say("revert", f"Discard did not complete ({e}); cutting the "
                                    "session so the router rolls Safe Mode back itself")
                try:
                    client.drop_session()
                finally:
                    self._held = False
        verdict = self._verify(orderly)
        result["revert"] = {"orderly": orderly, "verified": verdict}
        if verdict == "same":
            self._say("revert", "Reverted: the configuration is identical to the snapshot")
            return REVERTED, why
        if verdict == "differs":
            self._say("revert", "The configuration still differs from the snapshot")
            return UNCERTAIN, (f"{why}. The revert was asked for, but the export still "
                               "differs from the snapshot taken before. Look at the router.")
        self._say("revert", "Could not get back in to confirm the revert")
        return UNCERTAIN, (f"{why}. The router's own timer reverts the change at its "
                           "deadline, but the router could not be reached again to confirm.")

    def _verify(self, orderly: bool) -> str:
        """"same", "differs", or "unknown": the export now against the one
        taken before the change."""
        if self._before is None:
            return "same"  # nothing was ever applied
        if orderly:
            wait = 20.0
        elif self._reconnect_for_s is not None:
            wait = self._reconnect_for_s
        else:
            # Until the router's own timer has fired, and a while past it.
            left = (self._timer_at - time.monotonic()) if self._timer_at else 0.0
            wait = max(0.0, left) + RECONNECT_AFTER_DEADLINE_S
            if left > 0:
                self._say("revert", f"If the router cannot be reached, its own timer "
                                    f"reverts the change in {left:.0f}s; waiting for that")
        end = time.monotonic() + wait
        last = "unknown"
        own = orderly and self.client is not None
        while True:
            try:
                if own:
                    now = self.client.export_config(show_sensitive=False)
                else:
                    # The session that made the change is gone or not to be
                    # trusted: look with a fresh, read-only one.
                    fresh = self._connect(False)
                    try:
                        now = fresh.export_config(show_sensitive=False)
                    finally:
                        try:
                            fresh.close()
                        except Exception:  # noqa: BLE001
                            pass
                last = "same" if now.digest == self._before.digest else "differs"
            except Exception:  # noqa: BLE001 -- not reachable yet is an answer too
                own = False
            if last == "same" or time.monotonic() >= end:
                return last
            self._sleep(5.0)


def _diff(before: str, after: str) -> list[str]:
    a = [ln for ln in cexp.redact(before).splitlines() if ln.strip() and not ln.startswith("#")]
    b = [ln for ln in cexp.redact(after).splitlines() if ln.strip() and not ln.startswith("#")]
    out = [ln for ln in difflib.unified_diff(a, b, "before", "after", n=0, lineterm="")
           if not ln.startswith(("---", "+++", "@@"))]
    return out[:400]


# --- reading a job back, for the assistant ------------------------------------------------

def report(job: dict[str, Any]) -> str:
    """A job as text for the assistant: where it stands and what was seen."""
    status = job.get("status", "")
    res = job.get("result") or {}
    head = {
        WAITING: "waiting for the user's decision in the x3 change window",
        RUNNING: "approved, running now",
        REJECTED: "REJECTED by the user; nothing was sent to the router",
        KEPT: "KEPT: every check passed and the change is committed",
        REVERTED: "REVERTED: the change was undone and the config matches the snapshot taken before",
        NOT_APPLIED: "NOT APPLIED: it stopped before anything was changed",
        UNCERTAIN: "UNCERTAIN: a revert was triggered but could not be confirmed; a person must look",
    }.get(status, status)
    if abandoned(job):
        head = ("the change window is gone. Nothing was kept: a change that was "
                "mid-flight is undone by the router when the session drops, or at "
                "the latest when its own revert timer fires")
    out = [f"change {job['id']} on {job['device']}: {head}"]
    if res.get("why"):
        out.append(f"why: {res['why']}")
    if job.get("note"):
        out.append(f"note from the user: {job['note']}")
    if res.get("failed_command"):
        f = res["failed_command"]
        out.append(f"command {f['n']} ({f['command']}) -> {f['said']}")
    for title, key in (("before the change", "baseline"), ("after the change", "checks")):
        rows = res.get(key) or []
        if rows:
            out.append(f"checklist {title}:")
            out += [f"  {'pass' if r['ok'] else 'FAIL'}  {r['name']}: {r['seen']}" for r in rows]
    if res.get("diff"):
        out.append("what changed in the export:")
        out += [f"  {ln}" for ln in res["diff"]]
    elif status == KEPT:
        out.append("the export shows no difference (the change may be in something an export does not print)")
    if status in (WAITING, RUNNING) and not abandoned(job):
        events = job.get("events") or []
        if events:
            out.append("latest: " + str(events[-1].get("text", "")))
        out.append("Call change_status again (wait_s makes it wait for the next step).")
    return "\n".join(out)
