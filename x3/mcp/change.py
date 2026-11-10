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

Nor does Safe Mode's undo take back everything it holds. Measured on 7.19.4
and 7.23.3, with the router's own rollback as much as with ours: a firewall
rule disabled under Safe Mode stays disabled (both versions), and a route
added under it stays (7.19.4). So the run keeps exact ways back of its own,
none of them written by the assistant: the id of every object an ``add``
made, read back from the router, and the enabled/disabled state of every
object an enable or disable is about to touch, read before it does. They are
used twice. They ride in the timer's script after the unroll, put there
before the command they belong to can cut the path (an ``add`` and its
removal go in as one console line), and after a discard they are applied to
whatever the undo left. What still differs after that is reported line by
line, and the run does not say "reverted".

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
#: The most the timer's script may grow to. Past it a command is refused
#: before it is typed: a way back that did not fit would not be there.
MAX_TIMER_SCRIPT = 12000
#: What the timer's script opens with. Updates are made on the router, by
#: putting new steps right behind it, so its length is counted on.
UNROLL = "/safe-mode unroll; "
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


def _parts(text: str) -> tuple[str, str | None, str]:
    """``/ip/address add address=...`` -> ("/ip address", "add", "address=...").
    The verb is None if the words run out, or stop being words, before one
    is found."""
    words: list[str] = []
    for m in re.finditer(r"[^\s/]+", text):
        word = m.group(0)
        if not _WORD.fullmatch(word):
            break
        if word in WRITE_VERBS and words:
            return "/" + " ".join(words), word, text[m.end():].strip()
        words.append(word)
    return "/" + " ".join(words), None, ""


def _menu_and_verb(text: str) -> tuple[str, str | None]:
    menu, verb, _rest = _parts(text)
    return menu, verb


_ID = re.compile(r"\*[0-9A-Fa-f]+")


def _outside(rest: str) -> str:
    """``rest`` with what is inside quotes and brackets blanked out, so a
    word is only found where the console would read it as an argument."""
    out, quoted, depth, i = [], False, 0, 0
    while i < len(rest):
        c = rest[i]
        if quoted:
            if c == "\\":
                i += 1
            elif c == '"':
                quoted = False
            out.append(" ")
        elif c == '"':
            quoted = True
            out.append(" ")
        elif c == "[":
            depth += 1
            out.append(" ")
        elif c == "]":
            depth -= 1
            out.append(" ")
        else:
            out.append(" " if depth else c)
        i += 1
    return "".join(out)


def _touches_disabled(verb: str | None, rest: str) -> bool:
    return verb in ("enable", "disable") or (
        verb == "set" and re.search(r"(?<![\w-])disabled=", _outside(rest)) is not None)


def _target(rest: str) -> tuple[str, str] | None:
    """What a set-type command acts on: ("find", "find comment=x") for a
    leading ``[find ...]``, ("id", "*1F") for a leading id, else None (a
    name or a number: not something this can turn into ids)."""
    if rest.startswith("["):
        quoted, i = False, 1
        while i < len(rest):
            c = rest[i]
            if quoted:
                if c == "\\":
                    i += 1
                elif c == '"':
                    quoted = False
            elif c == '"':
                quoted = True
            elif c == "]":
                inner = rest[1:i].strip()
                return ("find", inner) if _FIND.fullmatch(inner) else None
            i += 1
        return None
    m = _ID.match(rest)
    if m and (m.end() == len(rest) or rest[m.end()].isspace()):
        return ("id", m.group(0))
    return None


# --- what a check may be --------------------------------------------------------

def check_check(raw: object) -> dict[str, Any]:
    """One checklist entry, normalised. Two kinds:

    ``{"type": "ping", "address": ..., "count": 5, "max_loss_pct": 0}``
        pinged from the router; optional src_address, routing_table, interface.
    ``{"type": "rows", "menu": "/routing bgp session", "where": [...], "min": 1}``
        how many rows a console_print would return; optional max.

    ``{"type": "export", "match": "regex", "section": "/ip firewall nat", "min": 1}``
        how many lines of the export match; matched here, on the text, so no
        field name or value form can make it miss what is there.

    Each takes ``within_s``: keep trying this long before calling it
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
    elif kind == "export":
        pattern = str(raw.get("match") or "")
        if not pattern:
            raise ToolError("An export check needs 'match': a regular expression.")
        try:
            re.compile(pattern)
        except re.error as e:
            raise ToolError(f"match is not a regular expression: {e}") from None
        out["match"] = pattern[:300]
        words = str(raw.get("section") or "").replace("/", " ").split()
        out["section"] = "/" + " ".join(words).lower() if words else ""
        lo, hi = raw.get("min"), raw.get("max")
        if lo is None and hi is None:
            lo = 1
        out["min"] = None if lo is None else diag.number(lo, "min", 0, 10_000_000, 0)
        out["max"] = None if hi is None else diag.number(hi, "max", 0, 10_000_000, 0)
    else:
        raise ToolError("A check's type is 'ping', 'rows' or 'export'.")
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
        if c["type"] == "export":
            text = (f"export{' ' + c['section'] if c.get('section') else ''}: "
                    f"{' and '.join(want)} line(s) matching {c['match']}")
        else:
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


#: Printed by the router before a typed line's own output, and typed in two
#: halves so the echo of the line never holds it whole.
_OUT = "x3-out"
_OUT_PUT = ':put ("x3" . "-out"); '
_AT = re.compile(r"\(line (\d+) column (\d+)\)")


def marked(client: DeviceClient, line: str, *, timeout: float = 45.0,
           at: int = 0) -> tuple[bool, list[str]]:
    """Type ``line`` and return (it came back to the prompt, what it printed).

    A console echoes what is typed, and past the width of the terminal it
    echoes it in pieces, scrolled sideways: a 560-character command came
    back as "output" that was its own tail, and a change that had worked was
    reverted for it. So the line is typed behind a ``:put`` of a marker, and
    only what follows the marker in the capture is the line's output.

    Some versions check a whole line before running any of it (7.19.4), so
    a syntax error prints no marker at all: then the router's own message is
    looked for by its "(line N column M)". ``at`` is where the caller's text
    starts in ``line``, so the column can be given in the caller's terms.
    """
    got = client.run_console(_OUT_PUT + line, timeout=timeout)
    text = got.text or ""
    if _OUT in text:
        lines = [ln.strip() for ln in text.split(_OUT, 1)[1].splitlines()
                 if ln.strip() and not cexp.LOG_LINE_RE.match(ln.strip())]
    elif not got.complete:
        return False, []
    else:
        hit = next((ln.strip() for ln in reversed(text.splitlines()) if _AT.search(ln)), "")
        if len(hit) > 160:      # an echo ran into it: keep the message's end
            hit = hit[hit.rfind("(line") - 60:].lstrip()
        lines = [hit or "the console did not run the line, and did not say why"]
    shift = len(_OUT_PUT) + at

    def in_callers_terms(m: re.Match) -> str:
        column = int(m.group(2)) - shift
        return f"(line {m.group(1)} column {column})" if column > 0 else m.group(0)

    return got.complete, [_AT.sub(in_callers_terms, ln) for ln in lines]


def run_check(client: DeviceClient, c: dict[str, Any]) -> tuple[bool, str]:
    """One try of one check: (passed, what was seen)."""
    if c["type"] == "ping":
        iface = None
        if c.get("interface"):
            names = [name for _id, name, *_rest in client.interface_choices()]
            iface = diag.interface(c["interface"], names)
        command = diag.ping(c["address"], count=c["count"], interface=iface,
                            src_address=c.get("src_address"), routing_table=c.get("routing_table"))
        _done, lines = marked(client, command, timeout=15 + 2 * c["count"])
        for ln in reversed(lines):
            m = _PING_SUMMARY.search(ln)
            if m:
                sent, received = int(m.group(1)), int(m.group(2))
                loss = 100.0 * (sent - received) / sent if sent else 100.0
                return loss <= c["max_loss_pct"], f"{received}/{sent} replies ({loss:.0f}% lost)"
        return False, "no ping summary: " + (lines[-1] if lines else "no output")
    if c["type"] == "export":
        got = client.export_config(show_sensitive=False)
        lines = [ln for ln in cexp.redact(got.text).splitlines()
                 if ln.strip() and not ln.startswith("#")]
        if c.get("section"):
            lines = [ln for ln in lines
                     if ln == c["section"] or ln.startswith(c["section"] + " ")]
        pattern = re.compile(c["match"], re.I)
        n = sum(1 for ln in lines if pattern.search(ln))
        ok = (c.get("min") is None or n >= c["min"]) and (c.get("max") is None or n <= c["max"])
        return ok, f"{n} line(s)"
    command = diag.print_command(c["menu"], c["where"], count_only=True)
    _done, lines = marked(client, command, timeout=60)
    try:
        n = int(lines[-1]) if lines else None
    except ValueError:
        n = None
    if n is None:
        return False, "could not count: " + (lines[-1] if lines else "no output")
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
        #: Ways back that do not depend on Safe Mode's undo, newest first:
        #: ("remove", menu, id) for an object this run added, and
        #: ("disabled", menu, id, "yes"|"no") for a state it was about to change.
        self._repairs: list[tuple[str, ...]] = []
        #: The export last compared with the snapshot, for saying what differs.
        self._seen: Any = None

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

    # -- ways back of our own

    @staticmethod
    def _statement(repair: tuple[str, ...]) -> str:
        if repair[0] == "remove":
            return f"{repair[1]} remove {repair[2]}"
        return f"{repair[1]} set {repair[2]} disabled={repair[3]}"

    def _timer_script(self, repairs: list[tuple[str, ...]] | None = None) -> str:
        """What the router runs at the deadline: Safe Mode's own rollback,
        then each way back of ours, each allowed to fail (the undo usually
        got there first)."""
        steps = [f":do {{{self._statement(r)}}} on-error={{}}"
                 for r in (self._repairs if repairs is None else repairs)]
        # Last, itself: the unroll normally removes the entry (it was added
        # under Safe Mode), and an entry left behind would fire again and
        # again, on whatever Safe Mode someone holds by then.
        steps.append(f":do {{{self._drop_timer()}}} on-error={{}}")
        return UNROLL + "; ".join(steps)

    def _drop_timer(self) -> str:
        return f"/system scheduler remove [find name={timer_name(self.job)}]"

    def _timer_len(self) -> str:
        return f":put [:len [/system scheduler get [find name={timer_name(self.job)}] on-event]]"

    def _timer_is(self, said: str, repairs: list[tuple[str, ...]]) -> None:
        """The router's count of the script against ours: a way back that is
        not in the timer is one the run must not go on without."""
        if not said.isdigit() or int(said) != len(self._timer_script(repairs)):
            raise _Revert("the revert timer on the router is not what was sent to it "
                          f"({said} characters, {len(self._timer_script(repairs))} expected)")

    def _into_timer(self, client: DeviceClient, fresh: list[tuple[str, ...]]) -> None:
        """Put ways back in the timer, ahead of the ones already there.

        The router does the joining, from the script it holds: what is typed
        is only the new steps, a few to a line, however long the script has
        grown.
        """
        after = fresh + self._repairs
        if len(self._timer_script(after)) > MAX_TIMER_SCRIPT:
            raise _Revert("this change touches more objects than the revert timer can "
                          "carry ways back for; split it into smaller changes")
        name = timer_name(self.job)
        steps = [f":do {{{self._statement(r)}}} on-error={{}}" for r in fresh]
        # A few to a line, by length, so no line is wider than the terminal.
        lines: list[list[str]] = [[]]
        for step in steps:
            if lines[-1] and len("; ".join(lines[-1] + [step])) > 240:
                lines.append([])
            lines[-1].append(step)
        said = ""
        for part in reversed(lines):              # last few first: they end up in order
            few = "; ".join(part)
            done, out = marked(client, (
                f":local s [/system scheduler get [find name={name}] on-event]; "
                f'/system scheduler set [find name={name}] on-event=("{UNROLL}{few}; " '
                f". [:pick $s {len(UNROLL)} [:len $s]]); {self._timer_len()}"))
            if not done or len(out) != 1:
                raise _Revert("the revert timer could not be updated: "
                              + ("; ".join(out) or "no answer"))
            said = out[0]
        self._timer_is(said, after)
        self._repairs = after

    def _disabled_now(self, client: DeviceClient, menu: str,
                      target: tuple[str, str]) -> dict[str, str]:
        """id -> "yes"/"no" for every object a command is about to act on."""
        kind, what = target
        if kind == "id":
            command = f':put ("{what} " . [{menu} get {what} disabled])'
        else:
            command = (f':foreach i in=[{menu} {what}] do='
                       f'{{:put ("$i " . [{menu} get $i disabled])}}')
        out: dict[str, str] = {}
        for ln in marked(client, command, timeout=30)[1]:
            m = re.fullmatch(r"(\*[0-9A-Fa-f]+) (true|false)", ln)
            if m:
                out[m.group(1)] = "yes" if m.group(2) == "true" else "no"
        return out

    def _type(self, client: DeviceClient,
              command: str) -> tuple[bool, list[str], str | None, int | None]:
        """Type one of the change's commands: (it came back to the prompt,
        what it printed, the id of what it added, how many History rows the
        command itself made; None if History could not be read).

        A set-type command that turns something on or off first has the
        state it is about to change put in the timer. An ``add`` is typed
        as one line that adds, puts its own removal in the timer and prints
        the new id: if the add itself cuts the path, the removal is already
        on the router.
        """
        menu, verb, rest = _parts(command)
        if _touches_disabled(verb, rest):
            target = _target(rest)
            if target is not None:
                known = {r[2] for r in self._repairs if r[0] == "disabled" and r[1] == menu}
                fresh = [("disabled", menu, oid, old)
                         for oid, old in self._disabled_now(client, menu, target).items()
                         if oid not in known]
                if fresh:
                    self._into_timer(client, fresh)
        held = client.safe_mode_pending()

        def rows(own: int) -> int | None:
            now = client.safe_mode_pending()
            return None if now is None or held is None else now - held - own

        if verb != "add":
            done, said = marked(client, command)
            return done, said, None, rows(0)
        if len(self._timer_script()) + 80 > MAX_TIMER_SCRIPT:
            raise _Revert("this change touches more objects than the revert timer can "
                          "carry ways back for; split it into smaller changes")
        name = timer_name(self.job)
        opening = ":local i ["
        done, said = marked(client, (
            f"{opening}{command}]; "
            f":local s [/system scheduler get [find name={name}] on-event]; "
            f'/system scheduler set [find name={name}] on-event=("{UNROLL}:do {{{menu} remove " '
            f'. $i . "}} on-error={{}}; " . [:pick $s {len(UNROLL)} [:len $s]]); '
            f":put $i; {self._timer_len()}"), at=len(opening))
        if done and len(said) == 2 and _ID.fullmatch(said[0]):
            after = [("remove", menu, said[0])] + self._repairs
            self._repairs = after          # it is on the router, whatever the count says
            self._timer_is(said[1], after)
            # The timer update in the same line is a row of its own.
            return True, [], said[0], rows(1)
        return done, said, None, None

    def _repair(self, client: DeviceClient) -> list[str]:
        """After a discard: apply each way back to whatever the undo left.
        Returns what it had to do, in words; empty when the undo did it all."""
        done: list[str] = []
        # The timer first: it must not outlive the change it was set for.
        marked(client, self._drop_timer(), timeout=30)
        for r in list(self._repairs):
            if r[0] == "remove":
                clean, said = marked(client, self._statement(r), timeout=30)
                if clean and not said:
                    done.append(f"removed {r[1]} {r[2]}: Safe Mode's undo had left it in place")
                continue
            now = marked(client, f":put [{r[1]} get {r[2]} disabled]", timeout=30)[1]
            state = {"true": "yes", "false": "no"}.get(now[-1] if now else "")
            if state is not None and state != r[3]:
                clean, said = marked(client, self._statement(r), timeout=30)
                if clean and not said:
                    done.append(f"set {r[1]} {r[2]} back to disabled={r[3]}: Safe Mode's "
                                "undo had not restored it")
        return done

    def run(self) -> dict[str, Any]:
        """Do it. Returns the result: ``status`` plus what was seen."""
        result: dict[str, Any] = {
            "baseline": [], "checks": [], "applied": 0,
            "commands": [{"n": i, "command": c, "ok": None, "said": ""}
                         for i, c in enumerate(self.job["commands"], 1)],
        }
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
                     f'on-event="{self._timer_script([])}" comment="x3 change {job["id"]}: '
                     'reverts it unless the checks pass"')
            self._say("timer", f"Setting the router's own revert timer: {limit}s from now",
                      deadline_s=limit)
            done, said = marked(client, timer, timeout=30)
            after = client.safe_mode_pending()
            if said or not done or after is None or after <= pending:
                raise DeviceError("the revert timer could not be set: "
                                  + ("; ".join(said) or "no undoable change recorded"))
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
                entry = result["commands"][i - 1]
                done, said, new_id, made = self._type(client, command)
                if said:
                    answer = "; ".join(said)
                    entry.update(ok=False, said=answer)
                    result["failed_command"] = {"n": i, "command": command, "said": answer}
                    raise _Revert(f"command {i} was refused: {answer}")
                if not done:
                    entry.update(ok=False, said="no answer in 45s")
                    result["failed_command"] = {"n": i, "command": command,
                                                "said": "no answer in 45s"}
                    raise _Revert(f"command {i} got no answer in 45s; it may have cut "
                                  "the connection to the router")
                result["applied"] = i
                if made is None or made < 1:
                    entry.update(ok=False, said="no undoable change recorded")
                    result["failed_command"] = {"n": i, "command": command,
                                                "said": "no undoable change recorded"}
                    raise _Revert(
                        f"the router recorded no undoable change for command {i}; "
                        "Safe Mode may not be able to take it back"
                    )
                entry.update(ok=True, said=f"added {new_id}" if new_id else "")
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
            held = client.safe_mode_pending()
            done, said = marked(client, self._drop_timer(), timeout=30)
            now = client.safe_mode_pending()
            if said or not done:
                raise _Revert("the revert timer could not be removed: "
                              + ("; ".join(said) or "no answer"))
            if now is None or held is None or held <= pending or now != held + 1:
                # Fewer held than were made: the timer fired, or someone else
                # undid part of it. Not a state to keep.
                raise _Revert("what Safe Mode holds is not what this change made "
                              f"({now} held after {held})")
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
        repaired: list[str] = []
        if orderly and self._timer_at is not None:
            try:
                # Only when the undo left something: typed in a line of its
                # own, a remove of an id that is already gone prints nothing,
                # so it cannot be told from one that removed.
                marked(client, self._drop_timer(), timeout=30)
                if client.export_config(show_sensitive=False).digest != self._before.digest:
                    repaired = self._repair(client)
            except Exception as e:  # noqa: BLE001 -- the export below is the judge
                self._say("revert", f"Could not finish undoing by hand: {e}")
        verdict = self._verify(orderly)
        if verdict == "differs" and not orderly and self._timer_at is not None:
            # The router rolled back by itself, and left what its undo does
            # not take back. Our own ways back, through a session of its own.
            try:
                again = self._connect(True)
                try:
                    repaired = self._repair(again)
                    self._seen = again.export_config(show_sensitive=False)
                    verdict = "same" if self._seen.digest == self._before.digest else "differs"
                finally:
                    again.close()
            except Exception as e:  # noqa: BLE001
                self._say("revert", f"Could not finish undoing by hand: {e}")
        for line in repaired:
            self._say("revert", line[0].upper() + line[1:])
        result["revert"] = {"orderly": orderly, "verified": verdict, "repaired": repaired}
        if verdict == "same":
            self._say("revert", "Reverted: the configuration is identical to the snapshot")
            return REVERTED, why
        if verdict == "differs":
            left = _diff(self._before.text, self._seen.text) if self._seen is not None else []
            result["leftover"] = left
            self._say("revert", "The configuration still differs from the snapshot:")
            for line in left[:40]:
                self._say("revert", f"  {line}")
            return UNCERTAIN, (f"{why}. The revert was done, but the export still differs "
                               "from the snapshot taken before (see what is left). Safe "
                               "Mode's undo does not take back every kind of change.")
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
                self._seen = now
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
    commands = res.get("commands") or []
    if any(c.get("ok") is not None for c in commands):
        out.append("commands:")
        for c in commands:
            mark = {True: "ok  ", False: "FAIL", None: "-   "}[c.get("ok")]
            tail = "" if c.get("ok") else f" -> {c.get('said') or 'not run'}"
            out.append(f"  {mark}  {c['n']}. {c['command']}{tail}")
    revert = res.get("revert") or {}
    for line in revert.get("repaired") or []:
        out.append(f"undone by x3 itself: {line}")
    if res.get("leftover"):
        out.append("still different from before the change (- was, + is now):")
        out += [f"  {ln}" for ln in res["leftover"]]
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
