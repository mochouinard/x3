"""Is there a newer x3, and how does this copy update itself?

The feed is the newest GitHub release of :data:`REPO` (``$X3_UPDATE_FEED``
points it elsewhere, for testing). A release carries one file per kind of
install plus ``SHA256SUMS``, and nothing is installed whose digest is not in
that file and does not match it.

How a copy updates depends on how it was installed (:func:`install_kind`):

* ``windows`` -- the per-user setup program. The new ``setup.exe`` is run
  silently over the old install and starts x3 again when it is done.
* ``appimage`` -- one file on Linux. The new AppImage is written beside the
  running one, made executable, renamed over it, and started.
* ``pip`` -- anything else (pip, pipx, a source checkout). x3 cannot replace
  itself there; it says which command will.

Checking is opt-in (Settings, and the welcome tour), at most once a day, and
sends nothing but the request for the release.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import __version__

REPO = "mochouinard/x3"
FEED_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
SUMS_NAME = "SHA256SUMS"
#: Seconds between automatic checks.
CHECK_EVERY = 24 * 3600
_TIMEOUT = 15.0


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    notes: str = ""
    page: str = RELEASES_PAGE
    #: asset name -> download URL
    assets: dict[str, str] = field(default_factory=dict)


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.12.3"`` -> ``(0, 12, 3)``. Stops at the first part that is not a
    plain number, so ``1.2.0rc1`` reads as ``(1, 2)`` and sorts before 1.2.0."""
    parts: list[int] = []
    for piece in text.strip().lstrip("vV").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def install_kind() -> str:
    """``windows``, ``appimage`` or ``pip``: see the module docstring."""
    if sys.platform.startswith("linux") and os.environ.get("APPIMAGE"):
        return "appimage"
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return "windows"
    return "pip"


def asset_name(kind: str, version: str) -> str | None:
    """The release file this kind of install updates from."""
    if kind == "windows":
        return f"x3-{version}-win64-setup.exe"
    if kind == "appimage":
        return f"x3-{version}-x86_64.AppImage"
    return None


def upgrade_command() -> str:
    """What to run when x3 cannot replace itself."""
    if "pipx" in Path(sys.prefix).parts:
        return "pipx upgrade x3"
    return f'"{sys.executable}" -m pip install --upgrade "x3[gui]"'


def feed_url() -> str:
    return os.environ.get("X3_UPDATE_FEED") or FEED_URL


def _get(url: str, timeout: float = _TIMEOUT):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"x3/{__version__}",
    })
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 -- https feed


def fetch_latest(url: str | None = None) -> Release:
    """The newest release, from the feed. Raises :class:`UpdateError`."""
    try:
        with _get(url or feed_url()) as resp:
            data = json.load(resp)
    except (OSError, ValueError) as e:
        raise UpdateError(f"could not read the release feed: {e}") from e
    tag = str(data.get("tag_name") or "")
    if not parse_version(tag):
        raise UpdateError(f"the release feed has no version ({tag!r})")
    return Release(
        version=tag.lstrip("vV"),
        notes=str(data.get("body") or ""),
        page=str(data.get("html_url") or RELEASES_PAGE),
        assets={
            str(a.get("name")): str(a.get("browser_download_url"))
            for a in data.get("assets") or []
            if a.get("name") and a.get("browser_download_url")
        },
    )


def sha256_for(sums: str, name: str) -> str | None:
    """The digest ``SHA256SUMS`` gives for ``name`` (``sha256sum`` format)."""
    for line in sums.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m and m.group(2) == name:
            return m.group(1).lower()
    return None


def download(url: str, dest: Path, progress: Callable[[int, int], None] | None = None) -> Path:
    """Fetch ``url`` to ``dest``; ``progress(done, total)`` as it goes."""
    try:
        with _get(url, timeout=60.0) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except OSError as e:
        raise UpdateError(f"download failed: {e}") from e
    return dest


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_verified(release: Release, kind: str, folder: Path,
                   progress: Callable[[int, int], None] | None = None) -> Path:
    """Download this install's file from ``release`` into ``folder`` and check
    it against the release's ``SHA256SUMS``. Raises :class:`UpdateError`."""
    name = asset_name(kind, release.version)
    if name is None:
        raise UpdateError("this copy of x3 cannot update itself")
    if name not in release.assets or SUMS_NAME not in release.assets:
        raise UpdateError(f"release {release.version} has no {name} with a {SUMS_NAME}")
    sums_path = download(release.assets[SUMS_NAME], folder / SUMS_NAME)
    want = sha256_for(sums_path.read_text(encoding="utf-8", errors="replace"), name)
    if want is None:
        raise UpdateError(f"{SUMS_NAME} does not list {name}")
    path = download(release.assets[name], folder / name, progress)
    got = file_sha256(path)
    if got != want:
        path.unlink(missing_ok=True)
        raise UpdateError(f"{name} does not match {SUMS_NAME} (got {got[:12]}…, "
                          f"expected {want[:12]}…); nothing was installed")
    return path


def apply_windows(setup: Path) -> None:
    """Run the new setup silently; it closes this x3, installs, and restarts it.
    The caller quits right after."""
    subprocess.Popen(  # noqa: S603 -- our own verified installer
        [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"],
        close_fds=True,
    )


def apply_appimage(new: Path, current: Path | None = None) -> Path:
    """Put the new AppImage where the running one is, and start it.

    Written beside the target first, so the final rename is atomic on the same
    filesystem: a crash halfway leaves the old AppImage, never half a file. The
    running process keeps its own (now unlinked) image until it exits.
    """
    target = Path(current or os.environ["APPIMAGE"])
    staged = target.with_name(f".{target.name}.update")
    shutil.copyfile(new, staged)
    staged.chmod(staged.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(staged, target)
    subprocess.Popen([str(target)], close_fds=True, start_new_session=True)  # noqa: S603
    return target


def download_folder() -> Path:
    return Path(tempfile.mkdtemp(prefix=_DOWNLOAD_PREFIX))


_DOWNLOAD_PREFIX = "x3-update-"


def clean_downloads(min_age: float = 600.0) -> int:
    """Delete the folders earlier updates downloaded into. Returns how many.

    The installer or AppImage that was downloaded is still in its folder when
    the new copy starts (the old one exits too fast to tidy up after it), so
    the next start does it. Only folders older than ``min_age`` seconds: a
    second x3 may be downloading into a fresh one right now.
    """
    removed = 0
    now = time.time()
    for folder in Path(tempfile.gettempdir()).glob(f"{_DOWNLOAD_PREFIX}*"):
        try:
            young = now - folder.stat().st_mtime < min_age
        except OSError:
            continue
        if folder.is_dir() and not young:
            shutil.rmtree(folder, ignore_errors=True)
            removed += not folder.exists()
    return removed


def due(last_check: float | None, now: float | None = None) -> bool:
    """Whether the daily automatic check should run."""
    now = time.time() if now is None else now
    return last_check is None or now - last_check >= CHECK_EVERY
