"""Turn a scraped the router console ``/export`` into a stable text artifact.

Measured 2026-08-20 against a wireless router on firmware 7.23.3 and a 10-port router on 7.21.1,
driving :mod:`x3.proto.terminal` and comparing ANSI-stripped output:

``/export terse`` is the only form worth scraping. 37 escape sequences in a
27 KB export, and a comment-stripped body **byte-identical** across
``term="vt102"``/``"dumb"`` and ``cols`` 80/512 -- the router does not wrap terse
output to the terminal width (longest line was 302 chars at cols=80).

Plain ``/export`` is the opposite: ~7000 colour escapes on the same config
(plus ``\\x1b[2K`` erase-line and a ``\\x1b[0;37;44m`` status bar), and after
stripping all of it the body *still* changes with width -- 24318 B / 505 lines
at cols=80 against 23280 B / 333 lines at cols=512, because its multi-line
continuation form is laid out to the terminal. Scraping that form is not
reproducible, so :data:`EXPORT_COMMAND` uses terse.

``term="dumb"`` is not a lever, incidentally: it shrinks the login banner and
leaves command output byte-identical.

What still has to come off the stream:

* the command echo, which comes back **doubled** (``/export terse/export terse``)
* the trailing shell prompt, itself full of escapes
* the ``:put`` sentinel that marks the end of output, and its echo
* ANSI escapes -- which live in the echo and prompt, never in the config text
* async log lines. The router splices ``2026-05-08 17:47:33 system,error ...``
  into the console when a logged event fires mid-export. Rare -- none in 8
  probe runs -- but a session capture caught one,
  and a config line arriving cut in half is worse than no capture at all. Those
  are lifted into :attr:`ConfigExport.spliced_logs` rather than quietly
  dropped, so a caller can report the capture as disturbed.

Secrets, and why the backup form asks for them
----------------------------------------------

From firmware 6.43 ``/export`` **hides** sensitive values by default: PSKs,
PPP secrets, SNMP communities and RADIUS keys come out as bare ``password=""``.
An export like that diffs beautifully and cannot rebuild the device -- restore
it and every tunnel and every wireless link stays down. So
:data:`EXPORT_COMMAND_SENSITIVE` is what an archive should capture, and
:func:`export_command` picks it whenever the firmware is new enough to
understand the word.

Older firmware has no ``show-sensitive`` -- it predates hiding and prints the
secrets regardless -- so on <6.43 the plain form is already the complete one.
The version is not always known before the console is open, so
:func:`console_error` also spots the syntax complaint a wrong guess produces
and lets the caller drop back a form rather than archive an error message.

The consequence belongs to the caller: an archive taken this way holds
plaintext keys and is exactly as sensitive as the routers it came from. See
:func:`redact` for showing such text to somewhere less private than the
archive itself.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: The one form that scrapes reproducibly. See the module docstring.
EXPORT_COMMAND = "/export terse"

#: The same form, secrets included -- what an archive wants. Understood from
#: Firmware 6.43; a syntax error on older firmware, which needs no such word.
EXPORT_COMMAND_SENSITIVE = "/export terse show-sensitive"

#: First release whose ``/export`` hides secrets, and so the first that has a
#: ``show-sensitive`` to turn them back on.
SENSITIVE_SINCE = (6, 43)

#: Marks the end of output. There is no other reliable terminator: the prompt
#: string varies with identity and user and is itself escape-laden.
SENTINEL = "==WBEOF=="

#: Console width to ask for. Terse output proved width-independent, so this is
#: belt-and-braces against a future release that formats it -- pinning a
#: constant costs nothing and keeps two captures comparable either way.
EXPORT_COLS = 512

#: CSI/OSC and the short two-byte forms. Broad on purpose: anything left in the
#: text would show up as a spurious diff later.
ANSI_RE = re.compile(rb"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][B0]|[=>]|.)")

#: A log line the router echoed into the console mid-capture.
LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [a-z][a-z0-9,_-]* ")

#: ``[admin@sm-test] > `` -- the console prompt, once its colours are gone.
PROMPT_RE = re.compile(r"^\[[^\]\n]{1,80}\] > ?")

#: The stamp the router opens an export with, in both the forms it writes it:
#: ``# 2026-08-20 06:55:38 by firmware 7.23.3`` on firmware 7 and
#: ``# jan/02/2026 15:04:05 by firmware 6.48.6`` on firmware 6. Only the time is
#: matched -- ``by the router <version>`` is kept, because a firmware change is
#: a real change and should show up as one.
HEADER_DATE_RE = re.compile(
    r"^(#\s+)(?:\d{4}-\d{2}-\d{2}|[a-z]{3}/\d{1,2}/\d{4})\s+\d{1,2}:\d{2}:\d{2}\s+",
    re.IGNORECASE | re.MULTILINE,
)

#: How the console complains. ``expected end of command`` is the one a
#: ``show-sensitive`` on pre-6.43 firmware produces; the others are here so a
#: refused or misspelled command is never mistaken for an empty config.
CONSOLE_ERROR_RE = re.compile(
    r"^(?:expected end of command"
    r"|bad command name [^\s]+"
    r"|no such (?:command|item)"
    r"|syntax error"
    r"|invalid (?:command|value|argument)"
    r"|.*\bnot enough permissions\b.*)",
    re.IGNORECASE,
)

#: Assignments whose value is a secret. Used only by :func:`redact` -- the
#: archive itself keeps what the device said. ``\S+`` stops at whitespace, and
#: the quoted alternative comes first so a value with spaces is covered whole.
SECRET_RE = re.compile(
    r"\b((?:password|secret|pre-shared-key|pre-shared-key-xauth|encryption-key"
    r"|private-key|auth-key|passphrase|wpa-pre-shared-key|wpa2-pre-shared-key"
    r"|community|authentication-password|encryption-password|key)\s*=\s*)"
    r"(\"[^\"]*\"|\S+)",
    re.IGNORECASE,
)


#: The router asks this on a console nobody has logged into yet, and waits.
LICENSE_PROMPT_RE = re.compile(
    r"software license.*\[\s*y\s*/\s*n\s*\]\s*:?\s*$", re.IGNORECASE
)

#: The console asking for a *new* password -- a fresh device, or one whose
#: password policy has expired. Never answered automatically: see
#: :func:`console_state`.
PASSWORD_PROMPT_RE = re.compile(
    r"(?:new password|repeat (?:new )?password|change (?:your )?password)"
    r"\s*\??\s*[>:]?\s*(?:\[\s*y\s*/\s*n\s*\]\s*:?)?\s*$",
    re.IGNORECASE,
)


def console_state(text: bytes | str) -> str:
    """What the console is waiting for, judged from the tail of its output.

    A console is not always sitting at a shell prompt when a session opens. A
    device nobody has logged into interactively is still holding its
    first-login questions, and typing a command at one of those *answers the
    question* -- it does not run the command. At the licence prompt that costs
    a capture. At a change-your-password prompt it sets the account password to
    whatever was typed, which is how a backup run turns into an outage.

    Returns ``"password"`` (do not type anything), ``"license"`` (answerable),
    or ``"ready"``. Only the two known prompts are called out; anything else
    reads as ready, so a console this function has never seen behaves exactly
    as it did before it existed.
    """
    tail = (strip_ansi(text) if isinstance(text, bytes) else text).rstrip()
    if not tail:
        return "ready"
    last = tail.splitlines()[-1].strip()
    if PASSWORD_PROMPT_RE.search(last):
        return "password"
    if LICENSE_PROMPT_RE.search(last):
        return "license"
    return "ready"


def parse_version(version: str) -> tuple[int, int]:
    """``"7.21.1 (stable)"`` -> ``(7, 21)``. ``(0, 0)`` when unreadable.

    Only the first two components decide whether ``show-sensitive`` exists, so
    the patch level and any suffix are deliberately ignored.
    """
    match = re.match(r"\s*(\d+)\.(\d+)", version or "")
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def export_command(version: str = "", *, show_sensitive: bool = True) -> str:
    """The ``/export`` form to type for this firmware.

    An unknown version is treated as new enough to ask: every the router in
    support is, and a wrong guess is recoverable through :func:`console_error`
    while silently archiving blanked secrets is not.
    """
    if not show_sensitive:
        return EXPORT_COMMAND
    version_pair = parse_version(version)
    if version_pair != (0, 0) and version_pair < SENSITIVE_SINCE:
        return EXPORT_COMMAND
    return EXPORT_COMMAND_SENSITIVE


def redact(text: str) -> str:
    """The same text with secret values replaced by ``******``.

    For anywhere the config is shown that is less protected than the archive --
    a log line, a summary, a diff pasted into a ticket.
    """
    return SECRET_RE.sub(lambda mt: mt.group(1) + "******", text)


def strip_ansi(raw: bytes | str) -> str:
    """Escape-free text with newlines normalised to ``\\n``.

    A **bare** CR is not a line break: it is the console returning to column 0
    to redraw the line it is already on. That is how a typed command comes
    back over a slow link -- one character at a time, each keystroke redrawing
    the whole line -- so treating CR as a newline turns one echoed command into
    a staircase of its own prefixes (``/``, ``/e``, ``/ex``, ...) sitting in
    the middle of the captured config. A terminal shows only what the last
    redraw left, and so does this. Frames coalesce on a fast link, which is why
    a LAN device never shows it and a customer on a WAN link does.

    A run of CRs *before* a newline is still just the newline. Traceroute ends
    each row with ``\r\r\n`` -- column 0, then down -- and taking that first
    CR as a redraw of nothing blanked every hop it printed.
    """
    data = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else raw
    text = ANSI_RE.sub(b"", data).decode("utf-8", "replace")
    text = re.sub(r"\r+\n", "\n", text)
    if "\r" not in text:
        return text
    return "\n".join(line.split("\r")[-1] for line in text.split("\n"))


@dataclass
class ConfigExport:
    """One captured ``/export``, split into the parts a caller needs.

    ``text`` is what to archive -- the device's own words, header included.
    ``body`` is what to compare: every ``#`` line dropped, because the header's
    first line carries the capture time and would otherwise make two captures
    of an untouched device differ every single time.
    """

    text: str = ""
    body: str = ""
    header: tuple[str, ...] = ()
    #: Log lines the device spliced into the stream, in the order seen.
    spliced_logs: tuple[str, ...] = ()
    #: Bytes read off the wire, before any cleaning.
    raw_len: int = 0
    #: False when the sentinel never arrived -- the capture may be truncated.
    complete: bool = True
    #: The console's complaint, if it refused the command outright. A
    #: ``show-sensitive`` sent to pre-6.43 firmware lands here; see
    #: :func:`export_command`.
    console_error: str = ""
    #: The command this capture was taken with, so a caller that fell back
    #: knows which form actually produced the text.
    command: str = ""
    _digest: str = field(default="", repr=False)

    @property
    def digest(self) -> str:
        """SHA-256 of ``body`` -- the identity of this configuration."""
        if not self._digest:
            self._digest = hashlib.sha256(self.body.encode("utf-8")).hexdigest()
        return self._digest

    @property
    def disturbed(self) -> bool:
        """Whether anything got in the way of a clean read."""
        return bool(self.spliced_logs) or not self.complete

    @property
    def refused(self) -> bool:
        """Whether the console rejected the command instead of running it.

        Distinct from :attr:`disturbed`: a refusal means there is no config
        here at all, so a caller should retry with another form rather than
        archive the result.
        """
        return bool(self.console_error)

    def redacted(self) -> str:
        """:attr:`text` with secret values masked. See :func:`redact`."""
        return redact(self.text)

    @property
    def stable_text(self) -> str:
        """:attr:`text` with the header's capture time removed.

        This is what to *store*. :attr:`text` is the device's words verbatim,
        header and all, and that header opens with the moment the export ran --
        so archiving it writes a new file for every router on every run, and a
        history of nothing but clocks. Dropping the time leaves the rest of the
        header (software id, ``by the router <version>``) intact, so the things
        in it that do mean something still show up as a diff when they change.
        """
        return HEADER_DATE_RE.sub(r"\1", self.text)

    def __len__(self) -> int:
        return len(self.text)


def _is_echo(line: str, command: str, sentinel: str) -> bool:
    """Whether this line is the console repeating something we typed.

    The echo arrives doubled, so the command shows up as itself twice over;
    match on containment rather than equality to catch both halves and the
    partial line a cursor-move escape leaves behind.

    A *prefix* counts too. :func:`strip_ansi` collapses a redrawn line to its
    final state, but an echo split across frames can leave a half-typed
    command standing on its own -- ``/export ters`` -- and no the router config
    line is a prefix of the command we just sent, so this is safe to claim.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if sentinel in stripped:
        return True
    if stripped in (command, command * 2, command + command):
        return True
    if stripped.replace(command, "") == "":
        return True
    # A partially-echoed command or `:put` line, from a redraw cut off by a
    # frame boundary. Strict prefixes only -- the whole strings are above.
    put_line = f':put "{sentinel}"'
    return any(
        len(stripped) < len(whole) and whole.startswith(stripped)
        for whole in (command, put_line)
    )


def parse(
    raw: bytes | str,
    *,
    command: str = EXPORT_COMMAND,
    sentinel: str = SENTINEL,
    split_logs: bool = True,
) -> ConfigExport:
    """Clean one console capture into a :class:`ConfigExport`.

    Pure text work, no device: this is the half of the export path that can be
    tested against a recorded stream.

    ``split_logs`` moves log lines the device spliced into the stream out of
    the text. Right for an export, where they are not config; wrong for a
    command whose output *is* log lines, or where they are part of the answer.
    """
    raw_len = len(raw)
    text = strip_ansi(raw)
    complete = sentinel in text

    kept: list[str] = []
    logs: list[str] = []
    errors: list[str] = []
    for line in text.split("\n"):
        if split_logs and LOG_LINE_RE.match(line):
            logs.append(line)
            continue
        if _is_echo(line, command, sentinel):
            continue
        if CONSOLE_ERROR_RE.match(line.strip()):
            errors.append(line.strip())
            continue
        if PROMPT_RE.match(line):
            # The prompt can carry a trailing fragment when output and prompt
            # land in one frame; keep whatever followed it.
            rest = PROMPT_RE.sub("", line, count=1).strip()
            if rest and not _is_echo(rest, command, sentinel):
                kept.append(rest)
            continue
        kept.append(line)

    # Trim the blank runs the echo and prompt leave behind, top and bottom,
    # without touching blank lines inside the config.
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()

    header = tuple(x for x in kept if x.startswith("#"))
    body_lines = [x for x in kept if not x.startswith("#")]
    # Any `#` line is device commentary -- the dated header, or a note like
    # "automatic dns option advertising is not started". None of it is config,
    # and the dated line alone would defeat every comparison.
    body = "\n".join(body_lines).strip()

    return ConfigExport(
        text="\n".join(kept).strip(),
        body=body,
        header=header,
        spliced_logs=tuple(logs),
        raw_len=raw_len,
        complete=complete,
        console_error="; ".join(errors),
        command=command,
    )
