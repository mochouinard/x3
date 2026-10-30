"""x3 updating itself: reading the release feed, checking what it downloads,
and replacing itself only the way its install allows.

    python tests/run.py updates

A real local HTTP server stands in for GitHub: it serves the release JSON, the
files, and SHA256SUMS. What matters most is the refusal path -- a file whose
digest does not match, or that SHA256SUMS does not list, is never installed --
and that a pip install is told the command instead of being overwritten.
"""

import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-upd-")

from x3.ui import settings, updates  # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


# --- versions ------------------------------------------------------------------
check("v-prefixed tags read", updates.parse_version("v0.12.3"), (0, 12, 3))
check("a newer minor is newer", updates.is_newer("0.2.0", "0.1.9"), True)
check("numbers compare as numbers", updates.is_newer("0.10.0", "0.9.0"), True)
check("the same version is not newer", updates.is_newer("0.1.0", "0.1.0"), False)
check("a pre-release sorts before its release", updates.is_newer("0.2.0rc1", "0.2.0"), False)
check("checks are daily", (updates.due(None), updates.due(1000.0, 1000.0 + 3600),
                           updates.due(1000.0, 1000.0 + 24 * 3600)), (True, False, True))

# --- what kind of install this is ------------------------------------------------
saved = (sys.platform, os.environ.get("APPIMAGE"), getattr(sys, "frozen", None))
try:
    sys.platform = "linux"
    os.environ.pop("APPIMAGE", None)
    check("a plain Linux install is pip", updates.install_kind(), "pip")
    os.environ["APPIMAGE"] = "/home/ann/Apps/x3.AppImage"
    check("an AppImage says so through $APPIMAGE", updates.install_kind(), "appimage")
    os.environ.pop("APPIMAGE")
    sys.platform = "win32"
    check("Windows from source is pip", updates.install_kind(), "pip")
    sys.frozen = True
    check("the frozen Windows build is the installer", updates.install_kind(), "windows")
finally:
    sys.platform = saved[0]
    if saved[1] is None:
        os.environ.pop("APPIMAGE", None)
    else:
        os.environ["APPIMAGE"] = saved[1]
    if saved[2] is None and hasattr(sys, "frozen"):
        del sys.frozen
check("asset names", (updates.asset_name("windows", "1.2.3"), updates.asset_name("appimage", "1.2.3"),
                      updates.asset_name("pip", "1.2.3")),
      ("x3-1.2.3-win64-setup.exe", "x3-1.2.3-x86_64.AppImage", None))

SUMS = ("a" * 64 + "  x3-9.9.9-win64-setup.exe\n"
        + "B" * 64 + " *x3-9.9.9-x86_64.AppImage\n")
check("SHA256SUMS lines are read, in both sha256sum forms",
      (updates.sha256_for(SUMS, "x3-9.9.9-win64-setup.exe"),
       updates.sha256_for(SUMS, "x3-9.9.9-x86_64.AppImage"),
       updates.sha256_for(SUMS, "missing")),
      ("a" * 64, "b" * 64, None))

# --- a fake GitHub ------------------------------------------------------------------
root = Path(tempfile.mkdtemp(prefix="x3-feed-"))
new_image = b"#!/bin/sh\necho new x3\n" * 50
(root / "x3-9.9.9-x86_64.AppImage").write_bytes(new_image)
(root / "x3-9.9.9-win64-setup.exe").write_bytes(b"MZ fake installer")
good_sums = "".join(
    f"{hashlib.sha256((root / n).read_bytes()).hexdigest()}  {n}\n"
    for n in ("x3-9.9.9-x86_64.AppImage", "x3-9.9.9-win64-setup.exe")
)
(root / "SHA256SUMS").write_text(good_sums)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(root), **k)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
feed = {
    "tag_name": "v9.9.9",
    "body": "## What's new\n\n- everything",
    "html_url": f"{base}/release",
    "assets": [{"name": n, "browser_download_url": f"{base}/{n}"}
               for n in ("x3-9.9.9-x86_64.AppImage", "x3-9.9.9-win64-setup.exe", "SHA256SUMS")],
}
(root / "latest.json").write_text(json.dumps(feed))
os.environ["X3_UPDATE_FEED"] = f"{base}/latest.json"

release = updates.fetch_latest()
check("the feed gives the version without its v", release.version, "9.9.9")
check("...the notes and the assets", (release.notes.startswith("## What's new"),
                                       sorted(release.assets)),
      (True, ["SHA256SUMS", "x3-9.9.9-win64-setup.exe", "x3-9.9.9-x86_64.AppImage"]))

got = updates.fetch_verified(release, "appimage", Path(tempfile.mkdtemp()))
check("a file that matches SHA256SUMS is handed over", got.read_bytes(), new_image)

(root / "SHA256SUMS").write_text(good_sums.replace(good_sums[:8], "00000000"))
dest = Path(tempfile.mkdtemp())
try:
    updates.fetch_verified(release, "appimage", dest)
    check("a mismatched download is refused", "accepted", "refused")
except updates.UpdateError as e:
    check("a mismatched download is refused", "does not match" in str(e), True)
check("...and deleted, so nothing half-trusted is left",
      (dest / "x3-9.9.9-x86_64.AppImage").exists(), False)
(root / "SHA256SUMS").write_text(good_sums)

no_sums = updates.Release("9.9.9", assets={k: v for k, v in release.assets.items()
                                           if k != "SHA256SUMS"})
try:
    updates.fetch_verified(no_sums, "appimage", Path(tempfile.mkdtemp()))
    check("a release without SHA256SUMS is refused", "accepted", "refused")
except updates.UpdateError:
    check("a release without SHA256SUMS is refused", "refused", "refused")
try:
    updates.fetch_verified(release, "pip", Path(tempfile.mkdtemp()))
    check("a pip install never downloads a replacement", "downloaded", "refused")
except updates.UpdateError:
    check("a pip install never downloads a replacement", "refused", "refused")

# --- putting it in place ------------------------------------------------------------
launched = []
real_popen = updates.subprocess.Popen
updates.subprocess.Popen = lambda args, **k: launched.append(args)
try:
    apps = Path(tempfile.mkdtemp(prefix="x3-apps-"))
    current = apps / "x3.AppImage"
    current.write_bytes(b"old")
    current.chmod(0o644)
    target = updates.apply_appimage(got, current)
    check("the AppImage is replaced in place", (target, current.read_bytes()), (current, new_image))
    check("...made executable", os.access(current, os.X_OK), True)
    check("...with no staging file left behind", sorted(p.name for p in apps.iterdir()),
          ["x3.AppImage"])
    check("...and started again", launched[-1], [str(current)])
    updates.apply_windows(Path("C:/Temp/x3-9.9.9-win64-setup.exe"))
    check("the Windows setup runs silently and restarts x3",
          launched[-1][1:], ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"])
finally:
    updates.subprocess.Popen = real_popen

# Leftover downloads are cleared on the next start.
leftover = Path(tempfile.gettempdir()) / "x3-update-leftover-test"
leftover.mkdir(exist_ok=True)
(leftover / "x3-9.9.9-win64-setup.exe").write_bytes(b"MZ")
check("a download younger than ten minutes is left alone (another x3 may own it)",
      (updates.clean_downloads(), leftover.exists()), (0, True))
check("leftover update downloads are cleaned up",
      (updates.clean_downloads(min_age=0) >= 1, leftover.exists()), (True, False))

# --- the GUI side -----------------------------------------------------------------------
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from x3.ui.widgets import update_dialog as ud  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])


def run_check(manual):
    result = {}
    loop = QEventLoop()

    def done(rel, err):
        result.update(release=rel, error=err)
        loop.quit()

    worker = ud.check_in_background(None, manual=manual, on_result=done)
    QTimer.singleShot(10000, loop.quit)
    loop.exec()
    worker.wait(2000)
    return result


offered = []
ud.notifier.available.connect(offered.append)
check("the automatic check is off until the user turns it on",
      ud.maybe_check_at_start(None), None)
res = run_check(manual=False)
check("a newer release is found", (res["release"].version, res["error"]), ("9.9.9", None))
check("...and announced", [r.version for r in offered], ["9.9.9"])
check("...and the check is remembered, so it waits a day",
      updates.due(settings.load_update_state()[0]), False)

settings.save_update_state(skipped="9.9.9")
offered.clear()
run_check(manual=False)
check("a skipped version is not announced again", offered, [])
run_check(manual=True)
check("...unless asked for by hand", [r.version for r in offered], ["9.9.9"])

# The hourly tick: checks only while the user has it on and a day has passed.
settings.save_update_check(False)
daily = ud.DailyCheck()
check("the daily check does nothing while off", daily._worker, None)
settings.save_update_check(True)
settings.save_update_state(last_check=0.0, skipped="")  # forget the skip above
offered.clear()
daily.tick()
check("...and checks once it is on and a day has passed", daily._worker is not None, True)
loop = QEventLoop()
daily._worker.finished.connect(loop.quit)
QTimer.singleShot(10000, loop.quit)
loop.exec()
check("...which announces the release", [r.version for r in offered], ["9.9.9"])
daily._worker = None
daily.tick()
check("...and not again the same day", daily._worker, None)
daily._timer.stop()

dlg = ud.UpdateDialog(release)
check("a pip install is offered the command, not an install",
      (dlg.kind, dlg.action_button.text()), ("pip", "&Copy Command"))
check("...and shown what to run", updates.upgrade_command() in dlg.how.text(), True)
os.environ["APPIMAGE"] = "/tmp/x3.AppImage"
real_platform, sys.platform = sys.platform, "linux"  # AppImages are Linux-only
dlg = ud.UpdateDialog(release)
sys.platform = real_platform
os.environ.pop("APPIMAGE")
check("an AppImage is offered Install and Restart",
      (dlg.kind, dlg.action_button.text()), ("appimage", "&Install and Restart"))

server.shutdown()
if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall update checks passed")
