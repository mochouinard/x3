"""Every x3ctl subcommand loads: its module imports and its --help prints.

    python tests/run.py cli_smoke

The CLI imports most of the protocol library lazily, one subcommand at a time,
so an import that broke (a module moved, a package renamed) only shows when
someone runs that subcommand. Asking each one for --help walks every one of
those paths without a device.
"""

import contextlib
import io

from x3 import cli

failures: list[str] = []


def check(label, ok, detail=""):
    if ok:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


SUBCOMMANDS = (
    "login", "try-creds", "version", "schema", "probe", "inspect", "getall",
    "subscribe", "download", "fetch-plugins", "mirror", "terminal", "export",
    "backup", "neighbors", "audit", "mcp",
)

for sub in ("", *SUBCOMMANDS):
    out = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.main([*([sub] if sub else []), "--help"])
    except SystemExit as e:
        code = e.code
    check(f"x3ctl {sub or '(top)'} --help", code == 0 and "usage:" in out.getvalue(),
          f"exit {code}: {out.getvalue()[-200:]}")

out = io.StringIO()
code = None
try:
    with contextlib.redirect_stdout(out):
        cli.main(["--version"])
except SystemExit as e:
    code = e.code
from x3 import __version__  # noqa: E402

check("x3ctl --version", (code, out.getvalue().strip()), (0, f"x3ctl {__version__}"))

# The help lists exactly these: a new subcommand needs adding above.
out = io.StringIO()
with contextlib.suppress(SystemExit), contextlib.redirect_stdout(out):
    cli.main(["--help"])
listed = out.getvalue()
check("no subcommand is missing from this test",
      all(s in listed for s in SUBCOMMANDS))

# Piped, on a platform whose code page has no "→": Windows writes the ANSI
# code page to a pipe, and `x3ctl export --help > f` died on the arrow in its
# help. An ASCII locale with UTF-8 mode off is the same trap on any system.
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "PYTHONIOENCODING"))}
env.update(LANG="C", LC_ALL="C", PYTHONUTF8="0")
piped = subprocess.run(
    [sys.executable, "-c", "import sys; from x3 import cli; sys.exit(cli.main(sys.argv[1:]))",
     "export", "--help"],
    env=env, capture_output=True, timeout=60,
)
check("x3ctl export --help, piped with no UTF-8 locale",
      piped.returncode == 0 and "→".encode() in piped.stdout,
      f"exit {piped.returncode}: {piped.stderr.decode(errors='replace')[-300:]}")

if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall CLI smoke checks passed")
