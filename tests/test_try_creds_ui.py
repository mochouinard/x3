"""The 'Try all saved' password sweep in the connect dialog.

    .venv/bin/python test_try_creds_ui.py

No device, no pytest. Two properties matter most and are the reason the feature
exists at all:

  1. EC-SRP5 GATE. If the device does not present the secure firmware 6.43+
     handshake, NOT ONE password is tried -- the sweep stops at the salt peek,
     which sends only a username. A reused password must never reach a box over
     a weaker handshake.
  2. The sweep finds the first working saved password and hands it to the
     normal connect path; if none work it says so, having exposed nothing.

The worker's run() is driven directly on the main thread (it is a plain method)
with a fake ModernSession, so the logic is deterministic and thread-free. The
fake raises the REAL Modern* exceptions so the worker's except-clauses behave
exactly as in production.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-test-")

import x3.proto.modern as modern
from x3.proto.modern import ModernAuthError, ModernProtocolError
from PySide6.QtWidgets import QApplication
import x3.ui.widgets.connect_dialog as cd
from x3.ui.widgets.connect_dialog import ConnectDialog, _TryCredsWorker


# The dialog pops modal message boxes (warning/information); exec() would block
# forever headless. Replace with recording no-ops so the flow stays testable.
class _FakeMB:
    calls: list[str] = []

    @staticmethod
    def warning(*a, **k):
        _FakeMB.calls.append("warning")

    @staticmethod
    def information(*a, **k):
        _FakeMB.calls.append("information")


cd.QMessageBox = _FakeMB

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    check(label, bool(cond), True)


app = QApplication.instance() or QApplication(sys.argv)


# --- a fake ModernSession that records what it was asked to authenticate -----

class FakeSess:
    #: password that "works"; None => every login is rejected
    winner: str | None = "good"
    #: what peek_salt does: "ok" | "not_srp" | "oserr"
    gate = "ok"
    #: every (user, pw) authenticate() saw, across all instances
    tried: list[tuple[str, str]] = []
    peeks: int = 0

    def __init__(self, *a, **k):
        pass

    def connect(self):
        pass

    def peek_salt(self, user="admin"):
        FakeSess.peeks += 1
        if FakeSess.gate == "not_srp":
            raise ModernProtocolError("stage 2 payload wrong size: 12 (expected 49)")
        if FakeSess.gate == "oserr":
            raise OSError("connection refused")
        return "aa" * 16

    def authenticate(self, user, pw):
        FakeSess.tried.append((user, pw))
        if FakeSess.winner is not None and pw == FakeSess.winner:
            return b"z" * 32
        raise ModernAuthError("bad")

    def close(self):
        pass


def _reset(**over):
    FakeSess.winner = "good"
    FakeSess.gate = "ok"
    FakeSess.tried = []
    FakeSess.peeks = 0
    for k, v in over.items():
        setattr(FakeSess, k, v)


def run_worker(candidates):
    """Drive a worker's run() synchronously and collect its signals."""
    w = _TryCredsWorker(
        host="10.0.0.9", user="admin", port=8291, transport="tcp",
        candidates=candidates, delay=0.0,
    )
    got = {"found": [], "exhausted": [], "not_secure": [], "error": []}
    w.found.connect(lambda p: got["found"].append(p))
    w.exhausted.connect(lambda n: got["exhausted"].append(n))
    w.not_secure.connect(lambda s: got["not_secure"].append(s))
    w.error.connect(lambda s: got["error"].append(s))
    w.run()
    return got


modern.ModernSession = FakeSess  # the worker imports this name inside run()


# === 1. EC-SRP5 gate: a non-EC-SRP5 device is never sprayed ================
print("--- gate: device does not offer EC-SRP5 ---")
_reset(gate="not_srp")
got = run_worker(["a", "b", "good"])
ok("emitted not_secure", len(got["not_secure"]) == 1)
check("ZERO passwords tried", FakeSess.tried, [])
ok("did peek the salt once", FakeSess.peeks == 1)
ok("no found/exhausted", not got["found"] and not got["exhausted"])

# === 2. unreachable device -> error, no passwords tried ====================
print("\n--- gate: unreachable ---")
_reset(gate="oserr")
got = run_worker(["a", "good"])
ok("emitted error", len(got["error"]) == 1)
check("ZERO passwords tried", FakeSess.tried, [])

# === 3. secure device, a saved password works -> found, stops at first hit =
print("\n--- spray: one works ---")
_reset(winner="good")
got = run_worker(["wrong1", "good", "wrong2"])
check("found the winner", got["found"], ["good"])
check("stopped at the hit (did not try wrong2)",
      FakeSess.tried, [("admin", "wrong1"), ("admin", "good")])
ok("no exhausted signal", not got["exhausted"])

# === 4. secure device, nothing works -> exhausted(n) =======================
print("\n--- spray: none work ---")
_reset(winner=None)
got = run_worker(["wrong1", "wrong2", "wrong3"])
check("exhausted reports count", got["exhausted"], [3])
ok("tried all three", len(FakeSess.tried) == 3)
ok("no found signal", not got["found"])

# === 5. dialog: _candidate_passwords dedups, typed one leads ===============
print("\n--- dialog: candidate list ---")
dlg = ConnectDialog()


class _Conn:
    def __init__(self, pw):
        self.password = pw


dlg._merged = [_Conn("p1"), _Conn("p2"), _Conn("p1"), _Conn(""), _Conn("p3")]
check("typed password leads, then distinct saved, blanks dropped",
      dlg._candidate_passwords("typed"), ["typed", "p1", "p2", "p3"])
check("no typed password -> just distinct saved",
      dlg._candidate_passwords(""), ["p1", "p2", "p3"])

# === 6. dialog: found adopts the password and hands off to the real connect =
print("\n--- dialog: found -> real connect ---")
launched = {"n": 0}
dlg._start_connect_worker = lambda: launched.__setitem__("n", launched["n"] + 1)
dlg._pending_connect = {"password": "", "user": "admin"}
dlg._on_tryall_found("winner-pw")
check("password field adopted the winner", dlg.password.text(), "winner-pw")
check("pending_connect carries the winner",
      dlg._pending_connect["password"], "winner-pw")
check("real connect worker was launched once", launched["n"], 1)

# === 7. dialog: not-secure resets the form, launches no connect ============
print("\n--- dialog: not-secure resets form ---")
launched["n"] = 0
_FakeMB.calls = []
dlg._set_connect_ui_busy(True)
dlg._on_tryall_not_secure("stage 2 payload wrong size")
ok("controls re-enabled", dlg._btn_connect.isEnabled())
check("no real connect launched", launched["n"], 0)
check("warned the user it was skipped", _FakeMB.calls, ["warning"])

# The constructor starts a neighbour-scan QThread; retire it (and any others)
# before the interpreter tears the dialog down, or Qt aborts on a live thread.
dlg.close()

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all try-creds UI tests passed")
