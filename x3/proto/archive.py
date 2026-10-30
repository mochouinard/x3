"""A git-backed archive of device configurations.

One file per router, one commit per run. Git is doing real work here rather
than being ceremony: a fleet's configs are near-identical between runs, so
storing a fresh timestamped copy of every device every night is almost all
duplicate bytes, and finding *when* something changed means diffing filenames
by hand. A repository stores the delta, and ``git log -p -- rtr.rsc`` is the
question everyone actually asks.

Three details are load-bearing for something that runs unattended:

**Identity.** ``git commit`` fails outright when ``user.email`` is unset, and a
systemd timer does not inherit the shell's global gitconfig. The repo gets its
own local identity at init, so the archive never depends on the ambient
environment of whatever spawned it.

**Permissions.** A ``show-sensitive`` export holds PSKs and PPP secrets in
plain text, so the directory is created ``0700`` and files ``0600``. This is
not a substitute for putting it on an encrypted disk -- it is the floor.

**Git is optional.** If git is missing or the repo cannot be created, the files
are still written and :attr:`Archive.vcs_error` explains what was lost. A
backup that stops because a *version control* tool is absent has its priorities
backwards.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Anything that is awkward in a filename, a URL or a shell word.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: Identity used for the archive's own commits. Local to the repo.
COMMIT_NAME = "x3ctl"
COMMIT_EMAIL = "x3ctl@localhost"

#: Dropped in at init so whoever finds the directory knows what it holds.
README = """\
# Device configuration archive

Written by `x3ctl backup`. One `.rsc` per router (a `/export terse`
capture) and one `.info.json` (model, serial, firmware). One commit per run.

    git log --oneline                  what ran, and when
    git log -p -- <router>.rsc         how one router's config changed
    git show --stat HEAD               what last night's run touched

**These files contain secrets.** The exports are taken with `show-sensitive`,
so they carry wireless PSKs, PPP secrets and RADIUS keys in plain text -- that
is what makes them restorable. The directory is mode 0700. Treat a copy of it
as equivalent to router admin access, and do not push it to a remote you would
not give that access to.
"""


def slug(name: str, *, fallback: str = "device") -> str:
    """A filesystem-safe stem that still reads like the router's name.

    Collisions are the caller's problem to notice, not this function's to
    paper over -- see :meth:`Archive.stem_for`, which disambiguates against the
    names it has already handed out.
    """
    cleaned = _UNSAFE_RE.sub("-", (name or "").strip()).strip("-.")
    # A name that is only separators, or one of the two directory entries that
    # cannot be a filename, would otherwise produce an unusable path.
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:80]


@dataclass
class WriteResult:
    """What one :meth:`Archive.write` did."""

    path: Path
    changed: bool
    created: bool


@dataclass
class CommitResult:
    """What one :meth:`Archive.commit` did."""

    committed: bool = False
    sha: str = ""
    files_changed: int = 0
    error: str = ""


@dataclass
class Archive:
    """A directory of config files, optionally under git.

    ``use_git=False`` writes the files and nothing else, which is what the
    tests and a ``--no-git`` run want.
    """

    root: Path
    use_git: bool = True
    #: Why version control is not active, if it is not. Empty when all is well
    #: or when git was never asked for.
    vcs_error: str = ""
    _stems: dict[str, str] = field(default_factory=dict, repr=False)
    _opened: bool = False

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    # --- lifecycle ------------------------------------------------------

    @property
    def git_active(self) -> bool:
        return self.use_git and not self.vcs_error

    def open(self) -> "Archive":
        """Create the directory, and the repository if git is wanted.

        Safe to call on an archive that already exists; that is the normal
        case, since every run after the first finds one.
        """
        # 0700 from the moment it exists: creating it world-readable and
        # tightening it afterwards leaves a window with secrets already in it.
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self._opened = True
        if self.use_git:
            self._init_git()
        return self

    def _init_git(self) -> None:
        if shutil.which("git") is None:
            self.vcs_error = "git is not installed; wrote plain files only"
            return
        try:
            if not (self.root / ".git").exists():
                self._git("init", "-q")
                # Local identity: a timer does not inherit a global gitconfig,
                # and commit refuses to run without one.
                self._git("config", "user.name", COMMIT_NAME)
                self._git("config", "user.email", COMMIT_EMAIL)
                readme = self.root / "README.md"
                if not readme.exists():
                    self._write_private(readme, README)
        except (OSError, subprocess.SubprocessError) as e:
            self.vcs_error = f"git init failed: {e}"

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    # --- writing --------------------------------------------------------

    def stem_for(self, name: str, key: str = "") -> str:
        """A stable, unique file stem for one router.

        ``key`` is the device's own identity (serial or host) and is only
        consulted when two routers slug to the same stem -- two entries both
        called "Office" would otherwise overwrite each other's history every
        run, alternating the file between two configurations.
        """
        stem = slug(name)
        owner = key or name
        seen = self._stems.get(stem)
        if seen is None:
            self._stems[stem] = owner
            return stem
        if seen == owner:
            return stem
        suffix = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:6]
        unique = f"{stem}-{suffix}"
        self._stems[unique] = owner
        return unique

    @staticmethod
    def _write_private(path: Path, text: str) -> None:
        """Write 0600, creating with those bits rather than relaxing later."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def write(self, stem: str, suffix: str, text: str) -> WriteResult:
        """Write one artifact, reporting whether it differs from last time.

        The comparison is against the file on disk, not against git, so a
        ``--no-git`` archive reports change just as accurately.
        """
        if not self._opened:
            self.open()
        path = self.root / f"{stem}{suffix}"
        if not text.endswith("\n"):
            text += "\n"
        previous = None
        if path.exists():
            try:
                previous = path.read_text(encoding="utf-8")
            except OSError:
                previous = None
        if previous == text:
            return WriteResult(path=path, changed=False, created=False)
        self._write_private(path, text)
        return WriteResult(path=path, changed=True, created=previous is None)

    # --- committing -----------------------------------------------------

    def commit(self, message: str, *, body: str = "") -> CommitResult:
        """Stage everything and commit, if anything actually changed.

        A run where every router was reachable and none had been touched is the
        normal case, and it should leave no commit behind: an archive whose
        history is mostly empty commits cannot be skimmed.
        """
        if not self.git_active:
            return CommitResult(error=self.vcs_error)
        try:
            self._git("add", "-A")
            staged = self._git("diff", "--cached", "--name-only")
            names = [x for x in staged.stdout.splitlines() if x.strip()]
            if not names:
                return CommitResult(committed=False, files_changed=0)
            full = message if not body else f"{message}\n\n{body}"
            self._git("commit", "-q", "-m", full)
            sha = self._git("rev-parse", "--short", "HEAD").stdout.strip()
            return CommitResult(
                committed=True, sha=sha, files_changed=len(names)
            )
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip().splitlines()
            return CommitResult(error=f"git {detail[-1] if detail else e}")
        except (OSError, subprocess.SubprocessError) as e:
            return CommitResult(error=f"git failed: {e}")
