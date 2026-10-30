"""Closing the app while a worker is still blocked must not abort it.

No device and no pytest required:

    .venv/bin/python test_exit_stuck_worker.py

A connect to a router that drops the SYN (a 7.25 box behind a dead link) sits
in the TCP connect, where closing the client cannot reach it. Closing the app
then joined for four seconds, gave up, and let the interpreter destroy the
still-running QThread:

    QThread: Destroyed while thread '' is still running

which Qt makes a fatal abort. The exit path now leaves without the teardown
when a worker will not finish. Each case runs in a child process, since the
failure is the process dying.
"""

import os
import subprocess
import sys
import time

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


CHILD = r"""
import os, sys, tempfile, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QWidget
from x3.ui import app as wapp, qthreads

class Blocked(QThread):
    def run(self):
        time.sleep(30)      # a TCP connect to a host that never answers

qa = QApplication([])
holder = QWidget()
worker = Blocked(holder)
worker.start()
qthreads.retire(worker, wait_ms=200)   # what Cancel / close does: gives up, keeps it
del worker
holder.deleteLater()
sys.exit(wapp.finish() if sys.argv[1] == "finish" else 0)
"""


def run(mode: str):
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, mode],
        capture_output=True, text=True, timeout=60,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    return proc, time.monotonic() - started


proc, took = run("finish")
check("the app leaves cleanly with a worker still blocked", proc.returncode, 0)
check("...without Qt's destroyed-while-running abort",
      "Destroyed while thread" in proc.stderr, False)
check("...and without waiting the blocked call out", took < 20, True)

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all exit tests passed")
