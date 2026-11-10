"""``x3 --change-window <change-id>``: the x3 change window.

A process of its own, started by the MCP server when an assistant proposes a
change. The server only ever holds read-only sessions; the write-capable one
is opened here, after a person has pressed Approve in a window the assistant
cannot reach. There is no flag that approves without the window, on purpose.
"""

from __future__ import annotations

import argparse
import sys
import threading
from types import SimpleNamespace
from typing import Any, Callable

from x3.proto.device import DeviceClient

from ..ui.services import split_host_port
from . import change as C
from .tools import _MASKED_PASSWORD, SavedDevices


def make_run_factory(cdb: str | None) -> Callable[..., C.ChangeRun]:
    """Builds the run for an approved job from the address book: the same
    entry the proposal named, looked up again now."""

    def make_run(job: dict[str, Any], emit: Callable[[dict[str, Any]], None],
                 cancel: threading.Event) -> C.ChangeRun:
        def connect(write: bool) -> DeviceClient:
            conn = SavedDevices(cdb).resolve(job["device"])
            if (conn.host, conn.login) != (job["host"], job["login"]):
                # The name now points somewhere else than what was approved.
                raise C.DeviceError(
                    f"{job['device']} is now {conn.login}@{conn.host} in the address "
                    f"book, not the {job['login']}@{job['host']} that was approved"
                )
            host, port = split_host_port(conn.host)
            password = "" if conn.password == _MASKED_PASSWORD else conn.password
            client = DeviceClient(host, conn.login, password, port=port, timeout=12.0,
                                  readonly=not write)
            client.keep_console = True
            client.connect()
            return client

        def snapshot(result: Any, client: DeviceClient, reason: str) -> None:
            from ..ui import autoexport

            who = SimpleNamespace(identity=client.info.identity or "", host=job["host"])
            autoexport.store(result, session=who, reason=reason)

        return C.ChangeRun(job, connect=connect, emit=emit, cancel=cancel, snapshot=snapshot)

    return make_run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="x3 --change-window", description=__doc__)
    p.add_argument("change_id")
    p.add_argument("--cdb", help="Addresses.cdb to read (default: the one x3 finds).")
    p.add_argument("--lang", help="Language for this window (default: the saved one).")
    args = p.parse_args(argv)

    job = C.load(args.change_id)
    if job.get("status") != C.WAITING:
        print(f"change {args.change_id} is {job.get('status')}, not waiting", file=sys.stderr)
        return 2

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ..ui import applog, i18n, qthreads, style
    from ..ui.settings import load_language

    applog.setup(None)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv[:1])
    # Before the first widget, as in the main window: install() wraps the Qt
    # calls that put text on screen.
    i18n.load(i18n.resolve(args.lang or load_language()))
    i18n.install()
    app.setApplicationName("x3")
    app.setOrganizationName("x3")
    app.setDesktopFileName("x3")
    app.setStyle("Fusion")
    style.apply_theme(app, None, persist=False)
    try:
        from ..ui.icons import window_icon

        app.setWindowIcon(window_icon("app", size=32))
    except Exception:  # noqa: BLE001 -- an icon is a nicety
        pass

    from ..ui.widgets.change_window import ChangeWindow

    first_time = C.policy_for(job["host"], job["login"]) != C.ALWAYS
    win = ChangeWindow(job, make_run=make_run_factory(args.cdb), first_time=first_time)
    win.show()
    win.raise_()
    win.activateWindow()
    app.exec()
    qthreads.retire_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
