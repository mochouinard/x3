"""Run every test script, each in its own interpreter.

    python tests/run.py            # all of them
    python tests/run.py fonts menu # only files whose name contains a word

Each ``test_*.py`` is a standalone script that exits non-zero on failure, so
they run one process apiece with the repository root on ``sys.path``, Qt on
its offscreen platform, and the XDG directories pointed at a scratch folder
so nothing touches your own settings.

Each script runs through :data:`LAUNCH`, which takes its exit code and leaves
with ``os._exit``. A script that leaves Qt windows alive at the end would
otherwise have them torn down during Python's own shutdown, where garbage
collection re-enters their Python event filters and, depending on collection
order, crashes after every check has passed. A failed check still exits
non-zero, and a crash while the checks run is still a crash. On Windows
``os._exit`` is ExitProcess, which still unloads the Qt libraries with Qt's
threads running and crashed 15 scripts that had passed; there the script
ends with TerminateProcess, which unloads nothing.

Many tests read a real router's ``.jg`` plugin set. That set belongs to the
router vendor and is not in this repository: mirror one from a device with
``x3ctl mirror`` into the repository root (``7.21.1-151146055/`` is the set
these tests were written against). A test that names a plugin set that is not
on disk is reported as skipped rather than failed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
PLUGIN_SET = re.compile(r"\b\d+\.\d+(?:\.\d+)?-\d{6,}\b")

#: Runs one test script as ``python script.py`` would, then exits without
#: interpreter teardown (see the module docstring).
LAUNCH = """\
import os, runpy, sys, traceback
path = sys.argv[1]
sys.argv = [path]
sys.path.insert(0, os.path.dirname(path))
code = 0
try:
    runpy.run_path(path, run_name="__main__")
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
except BaseException:
    traceback.print_exc()
    code = 1
sys.stdout.flush()
sys.stderr.flush()
if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(-1), code)
os._exit(code)
"""


def missing_plugin_sets(path: Path) -> list[str]:
    """The plugin sets a test names, when none of them is on disk.

    Some tests also name made-up sets to exercise directory matching, so one
    present set is enough to run the file.
    """
    names = set(PLUGIN_SET.findall(path.read_text(encoding="utf-8")))
    if any((ROOT / n).is_dir() for n in names):
        return []
    return sorted(names)


def main(argv: list[str]) -> int:
    words = argv[1:]
    files = sorted(
        p for p in TESTS.glob("test_*.py") if not words or any(w in p.name for w in words)
    )
    scratch = tempfile.mkdtemp(prefix="x3-tests-")
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(ROOT),
        QT_QPA_PLATFORM="offscreen",
        PYTHONDONTWRITEBYTECODE="1",
        # The tests print and read UTF-8 text; Windows would otherwise use its
        # ANSI code page (cp1252) and fail on the first "▸".
        PYTHONUTF8="1",
        XDG_CONFIG_HOME=os.path.join(scratch, "config"),
        XDG_DATA_HOME=os.path.join(scratch, "data"),
        XDG_CACHE_HOME=os.path.join(scratch, "cache"),
        XDG_STATE_HOME=os.path.join(scratch, "state"),
    )
    if sys.platform == "win32" and not env.get("QT_QPA_FONTDIR"):
        # The offscreen platform looks for fonts beside PySide6 and finds none
        # on Windows, and then every text measures as nothing.
        env["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    passed, failed, skipped = [], [], []
    for path in files:
        missing = missing_plugin_sets(path)
        if missing:
            skipped.append(path.name)
            print(f"skip  {path.name}  (no plugin set: {', '.join(missing)})")
            continue
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", LAUNCH, str(path)], cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        took = time.monotonic() - start
        if proc.returncode == 0:
            passed.append(path.name)
            print(f"ok    {path.name}  ({took:.1f}s)")
        else:
            failed.append(path.name)
            print(f"FAIL  {path.name}  (exit {proc.returncode}, {took:.1f}s)")
            print("\n".join("      " + line for line in proc.stdout.splitlines()[-25:]))
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
