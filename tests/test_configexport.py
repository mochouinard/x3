"""Offline tests for the /export scraper.

No device and no pytest required:

    .venv/bin/python test_configexport.py

The streams below are built the way the console really delivers one -- doubled
command echo, an escape-laden prompt, CRLF line ends, the sentinel appearing
twice -- so the parser is exercised against the shape it has to survive rather
than against clean text.
"""

from x3.proto import configexport as cexp

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    if not cond:
        failures.append(label)


# The prompt as it actually arrives: colour, the bracket form, no newline.
PROMPT = "\x1b[9999B\x1b[0;32m[admin@rtr1]\x1b[0m > "

CONFIG = (
    "# 2026-08-20 03:14:07 by RouterOS 7.23.3\n"
    "# software id = ABCD-1234\n"
    "#\n"
    "/interface bridge add name=bridge1 protocol-mode=rstp\n"
    "/ip address add address=192.168.88.1/24 interface=bridge1\n"
    "/interface wireless security-profiles set [ find default=yes ] "
    "wpa2-pre-shared-key=\"correct horse battery\"\n"
)


def stream(command=cexp.EXPORT_COMMAND_SENSITIVE, body=CONFIG, sentinel=True):
    """A console capture as the terminal channel would hand it over."""
    parts = [PROMPT, command + command, "\r\n", body.replace("\n", "\r\n")]
    if sentinel:
        parts += [PROMPT, f':put "{cexp.SENTINEL}"' * 2, "\r\n",
                  cexp.SENTINEL, "\r\n"]
    parts.append(PROMPT)
    return "".join(parts).encode()


def parsed(command=cexp.EXPORT_COMMAND_SENSITIVE, body=CONFIG, sentinel=True):
    """Parse a capture, told the command it was taken with.

    Always paired, as :meth:`DeviceClient.export_config` pairs them: the echo
    is stripped by matching the command back out of the stream, so parsing a
    ``show-sensitive`` capture as a plain one leaves the echo in the config.
    """
    return cexp.parse(
        stream(command=command, body=body, sentinel=sentinel), command=command
    )


# --- strip_ansi ------------------------------------------------------------

check("strip_ansi removes CSI", cexp.strip_ansi(b"\x1b[0;32mhi\x1b[0m"), "hi")
check("strip_ansi normalises CRLF", cexp.strip_ansi(b"a\r\nb"), "a\nb")
# A bare CR redraws the current line; only the last state survives. Treating
# it as a newline is what put a staircase of command prefixes into 9 real
# archived configs.
check("bare CR overwrites, not breaks", cexp.strip_ansi(b"a\rb"), "b")
check("only the last redraw of a line survives",
      cexp.strip_ansi(b"/\r/e\r/ex\r/export\r\nreal"), "/export\nreal")
check("CRLF is still a line break", cexp.strip_ansi(b"a\r\nb"), "a\nb")
check("so is CR CR LF, the end of a traceroute row, which keeps its text",
      cexp.strip_ansi(b"0  10.0.2.2  0%\r\r\nnext"), "0  10.0.2.2  0%\nnext")
check("a CR-free string is untouched", cexp.strip_ansi("a\nb"), "a\nb")
check("strip_ansi keeps plain text", cexp.strip_ansi("plain"), "plain")
ok("strip_ansi survives invalid utf-8", cexp.strip_ansi(b"\xff\xfeok").endswith("ok"))


# --- a clean capture -------------------------------------------------------

result = parsed()

ok("clean capture is complete", result.complete)
ok("clean capture is not refused", not result.refused)
ok("clean capture is not disturbed", not result.disturbed)
check("command is recorded", result.command, cexp.EXPORT_COMMAND_SENSITIVE)
ok("no escape survived into the text", "\x1b" not in result.text)
ok("no sentinel survived into the text", cexp.SENTINEL not in result.text)
ok("no prompt survived into the text", "[admin@rtr1]" not in result.text)
ok("the echoed command is gone", "/export terse" not in result.text)
ok("config lines are kept", "/ip address add address=192.168.88.1/24" in result.body)
check("header is separated out", len(result.header), 3)
ok("header holds the dated line", result.header[0].startswith("# 2026-08-20"))
ok("body drops every comment", not any(x.startswith("#") for x in result.body.splitlines()))
check("body line count", len(result.body.splitlines()), 3)


# --- the echo as a slow link actually delivers it ---------------------------
#
# Verbatim shape from a CRS112 on firmware 6.49.1, over a WAN link:
# the console redraws the command line once per character, each redraw led by
# a bare CR. Nine real configs were archived with this staircase in them.

CMD = cexp.EXPORT_COMMAND_SENSITIVE
staircase = "".join(f"\r{CMD[:i]}" for i in range(1, len(CMD) + 1))
typed = cexp.parse(
    (PROMPT + staircase + "\r\n" + CONFIG.replace("\n", "\r\n")
     + PROMPT + f'\r:put "{cexp.SENTINEL}"' + "\r\n"
     + cexp.SENTINEL + "\r\n" + PROMPT).encode(),
    command=CMD,
)
ok("no command prefix survives",
   not any(CMD.startswith(x.strip()) and x.strip()
           for x in typed.text.splitlines()))
ok("no bare slash survives", "/e" not in typed.text.splitlines())
ok("the config is intact", "/ip address add address=192.168.88.1/24" in typed.body)
check("the staircase leaves the body clean", typed.body, result.body)
check("and the digest matches a clean capture", typed.digest, result.digest)

# A frame boundary mid-redraw leaves a half-typed command standing alone.
partial = cexp.parse(
    (PROMPT + "\r/export ters\r\n" + CONFIG.replace("\n", "\r\n")
     + f'\r\n:put "{cexp.SENTINEL}"\r\n{cexp.SENTINEL}\r\n').encode(),
    command=CMD,
)
ok("a truncated echo is stripped too", "/export ters" not in partial.text)
check("and leaves the same body", partial.body, result.body)

# The safety rail on that: a real config line must never be eaten as an echo.
for real_line in ("/ip address print", "/export-something", "/interface print",
                  "/exports", "/e1"):
    ok(f"a real line survives: {real_line}",
       real_line in cexp.parse(
           (f"\r\n{real_line}\r\n" + f':put "{cexp.SENTINEL}"\r\n'
            + cexp.SENTINEL + "\r\n").encode(), command=CMD).body)


# --- the digest is what makes a nightly diff meaningful --------------------

later = parsed(body=CONFIG.replace("03:14:07", "22:57:41"))
check("a new capture time does not change the digest", later.digest, result.digest)

edited = parsed(body=CONFIG.replace("192.168.88.1", "10.0.0.1"))
ok("an edited config does change the digest", edited.digest != result.digest)

ok("digest is a sha256", len(result.digest) == 64)


# --- what actually gets archived -------------------------------------------
#
# text carries the header, and the header opens with the capture time. Storing
# that writes a new file for every router on every run, so the archive fills
# with commits that record nothing but the clock. stable_text is the fix.

ok("text keeps the device's own header", "# 2026-08-20 03:14:07" in result.text)
ok("stable_text drops the capture time", "03:14:07" not in result.stable_text)
ok("stable_text keeps the version", "by RouterOS 7.23.3" in result.stable_text)
ok("stable_text keeps the software id", "ABCD-1234" in result.stable_text)
ok("stable_text keeps the config", CONFIG.splitlines()[3] in result.stable_text)
check("two runs of an untouched router archive identically",
      later.stable_text, result.stable_text)
ok("two runs of an untouched router do NOT store identically",
   later.text != result.text)
ok("an edited config still differs", edited.stable_text != result.stable_text)

ros6 = parsed(body="# jan/02/2026 15:04:05 by RouterOS 6.48.6\n/ip address print\n")
ok("the v6 date form is stripped too", "15:04:05" not in ros6.stable_text)
ok("the v6 version survives", "by RouterOS 6.48.6" in ros6.stable_text)

no_header = parsed(body="/ip address print\n")
check("a headerless export is left alone",
      no_header.stable_text, no_header.text)
ok("a time inside a config value is untouched",
   "03:14:07" in parsed(
       body='/system scheduler add name=x start-time=03:14:07\n').stable_text)


# --- truncation ------------------------------------------------------------

cut = parsed(sentinel=False)
ok("a missing sentinel is reported", not cut.complete)
ok("a missing sentinel counts as disturbed", cut.disturbed)


# --- log lines spliced into the stream -------------------------------------

noisy_body = CONFIG.replace(
    "/ip address add",
    "2026-08-20 03:14:08 system,error,critical login failure for user x\n"
    "/ip address add",
)
noisy = parsed(body=noisy_body)
check("the log line is lifted out", len(noisy.spliced_logs), 1)
ok("the log line is reported", "login failure" in noisy.spliced_logs[0])
ok("the log line is not in the config", "login failure" not in noisy.body)
ok("a spliced log marks the capture disturbed", noisy.disturbed)
check("the config is otherwise intact", noisy.body, result.body)


# --- the console refusing the command --------------------------------------

refused = parsed(body="expected end of command\n")
ok("a refusal is detected", refused.refused)
ok("the complaint is kept", "expected end of command" in refused.console_error)
ok("a refusal leaves no body", refused.body == "")

for complaint in (
    "bad command name shw-sensitive (line 1 column 2)",
    "no such command",
    "syntax error (line 1 column 9)",
    "not enough permissions (9)",
):
    ok(f"refusal detected: {complaint[:24]}",
       parsed(body=complaint + "\n").refused)

ok("a config line is not mistaken for an error", not result.refused)
ok("'invalid' inside a value is not an error",
   not parsed(body='/ip dns set servers=1.1.1.1 comment="invalid vlan"\n').refused)


# --- what the console is waiting for ---------------------------------------
#
# A console nobody has logged into is still holding its first-login questions,
# and a command typed at one of those answers the question instead of running.
# The banner below is verbatim from a wireless router on 7.23.1 that had never been
# consoled -- it is why a real router captured 163 bytes and no config.

LICENCE_BANNER = (
    "  MikroTik RouterOS 7.23.1 (c) 1999-2026       https://www.mikrotik.com/"
    "\r\n\r\n\r\n\r\nDo you want to see the software license? [Y/n]: "
)
check("the licence prompt is recognised",
      cexp.console_state(LICENCE_BANNER), "license")
check("it is recognised as raw bytes",
      cexp.console_state(LICENCE_BANNER.encode()), "license")

for asking in (
    "new password> ",
    "repeat new password> ",
    "Change your password? [y/N]: ",
    "\r\nPlease change your password\r\nnew password>",
):
    check(f"password prompt: {asking.strip()[:24]!r}",
          cexp.console_state(asking), "password")

check("a normal prompt is ready", cexp.console_state("[admin@rtr1] > "), "ready")
check("an empty console is ready", cexp.console_state(""), "ready")
check("a console mid-output is ready",
      cexp.console_state("/ip address add address=1.2.3.4\n"), "ready")
# An unknown prompt must not be reclassified as hazardous: that would refuse
# to back up the 89 routers that already work.
check("an unrecognised tail is left alone",
      cexp.console_state("something else entirely: "), "ready")
# ...and a config line that merely mentions a password is not a prompt.
check("a config line naming a password is not a prompt",
      cexp.console_state('/user set admin password="x"\n'), "ready")


# --- choosing the command --------------------------------------------------

check("7.x asks for secrets", cexp.export_command("7.23.3 (stable)"),
      cexp.EXPORT_COMMAND_SENSITIVE)
check("6.43 asks for secrets", cexp.export_command("6.43"),
      cexp.EXPORT_COMMAND_SENSITIVE)
check("6.48.6 asks for secrets", cexp.export_command("6.48.6"),
      cexp.EXPORT_COMMAND_SENSITIVE)
check("6.42.1 does not", cexp.export_command("6.42.1"), cexp.EXPORT_COMMAND)
check("6.40.9 does not", cexp.export_command("6.40.9"), cexp.EXPORT_COMMAND)
check("an unknown version still asks", cexp.export_command(""),
      cexp.EXPORT_COMMAND_SENSITIVE)
check("opting out wins", cexp.export_command("7.23", show_sensitive=False),
      cexp.EXPORT_COMMAND)

check("version parse", cexp.parse_version("7.21.1 (stable)"), (7, 21))
check("version parse without suffix", cexp.parse_version("6.9"), (6, 9))
check("version parse of nonsense", cexp.parse_version("router"), (0, 0))


# --- redaction -------------------------------------------------------------

masked = result.redacted()
ok("the PSK is masked", "correct horse battery" not in masked)
ok("the key name survives", "wpa2-pre-shared-key=******" in masked)
ok("ordinary values are untouched", "address=192.168.88.1/24" in masked)
check("redact leaves plain text alone", cexp.redact("/ip address print"),
      "/ip address print")
check("redact handles an unquoted secret", cexp.redact("password=hunter2 x=1"),
      "password=****** x=1")
ok("the archive text itself keeps the secret",
   "correct horse battery" in result.text)


# --- parse(split_logs=False): a command whose output is the log ----------------

logged = cexp.parse(
    b"/log print\r\n2026-10-28 04:42:56 dhcp,info client1 got 10.0.2.15\r\n[admin@lab] > ",
    command="/log print", sentinel="\x00", split_logs=False,
)
check("with split_logs off, log lines stay in the text",
      (logged.text, logged.spliced_logs), ("2026-10-28 04:42:56 dhcp,info client1 got 10.0.2.15", ()))


if failures:
    print(f"FAIL ({len(failures)})")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("test_configexport: all checks passed")
