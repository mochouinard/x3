"""``x3ctl mcp [--cdb PATH]`` (or ``python -m x3.mcp``): the x3 MCP server on stdio."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from . import change as chg
from .protocol import Server, ToolError, serve
from .tools import INSTRUCTIONS, SavedDevices, ToolBox


def _window_command() -> list[str]:
    """How to start x3's desktop program from here: the packaged build has
    it as a binary beside this one, a source checkout as ``python -m x3``."""
    if getattr(sys, "frozen", False):
        here = Path(sys.executable)
        gui = here.with_name("x3" + here.suffix)
        if not gui.is_file():
            raise ToolError(f"x3's desktop program is not beside {here.name}")
        return [str(gui)]
    return [sys.executable, "-m", "x3"]


def launch_change_window(job_id: str, cdb: str | None) -> None:
    """Start the x3 change window for one job, as a process of its own.

    Its own session, so it outlives this server: a change that is running
    when the assistant's session ends still finishes, or reverts, in front
    of the person who approved it.
    """
    if sys.platform not in ("win32", "darwin") and not (
        os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
    ):
        raise ToolError("there is no desktop session here to show it on")
    cmd = _window_command() + ["--change-window", job_id]
    if cdb:
        cmd += ["--cdb", cdb]
    # Detached from this server either way; the two systems spell it differently.
    if sys.platform == "win32":
        detach = {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    else:
        detach = {"start_new_session": True}
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **detach,
    )
    # The window writes to the job file as soon as it is up. Wait for that,
    # so "it is in front of the user" is only said when it is.
    created = chg.load(job_id).get("beat", 0)
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if chg.load(job_id).get("beat", 0) > created:
            return
        code = proc.poll()
        if code is not None:
            raise ToolError(f"the window process ended at once (exit {code}); see x3's log")
        time.sleep(0.2)
    raise ToolError("the window did not come up within 15s")


def main(argv: list[str] | None = None, *, prog: str = "python -m x3.mcp") -> int:
    p = argparse.ArgumentParser(prog=prog, description=__doc__)
    p.add_argument("--cdb", help="Addresses.cdb to read (default: the one x3 finds).")
    args = p.parse_args(argv)
    # The package's own version, not its metadata: a packaged build has none.
    from .. import __version__ as ver

    source = SavedDevices(args.cdb)
    box = ToolBox(source, launch_change=lambda job_id: launch_change_window(job_id, args.cdb))
    try:
        serve(Server(box.definitions(), version=ver, instructions=INSTRUCTIONS))
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
