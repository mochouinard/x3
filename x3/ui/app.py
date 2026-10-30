"""Application bootstrap."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from . import applog, qthreads, style
from .widgets.connect_dialog import ConnectDialog
from .widgets.main_window import MainWindow


def _opt(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv[1:]:
        from .. import __version__

        print(f"x3 {__version__}")
        return 0

    # Before anything else: a GUI has no terminal to fail into, so open the
    # log first and let it catch whatever startup does.
    #   --log PATH   write there instead of ~/.local/state/x3/
    #   --log -      no log file
    applog.setup(_opt(argv, "--log"))

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(argv)
    # The language goes in before the first widget is built: install() wraps
    # the Qt calls that put text on screen, so nothing made earlier would be
    # translated. English leaves Qt untouched.
    #   --lang CODE   this language for this run only (en, fr, ...), until
    #                 the user picks one in it (see the connection loop below)
    from . import i18n
    from .settings import load_language

    i18n.load(i18n.resolve(_opt(argv, "--lang") or load_language()))
    i18n.install()
    app.setApplicationName("x3")
    app.setOrganizationName("x3")
    # Wayland app_id / X11 desktop-file match: lets GNOME's dock find
    # x3.desktop (and its icon) instead of showing a blank window icon.
    app.setDesktopFileName("x3")
    app.setStyle("Fusion")
    applog.start_watchdog()

    # Optional CLI override:
    #   --theme light|dark|roomy|classic|high-contrast
    theme_id = None
    if "--theme" in argv:
        i = argv.index("--theme")
        if i + 1 < len(argv):
            theme_id = argv[i + 1]
    style.apply_theme(app, theme_id, persist=True)
    # How byte counters read (Settings ▸ Byte Units). Set before anything is
    # rendered: it lives in the schema, under every value that goes on screen.
    from .settings import apply_byte_units

    apply_byte_units()
    try:
        from .icons import window_icon
        app.setWindowIcon(window_icon("app", size=32))
    except Exception:
        pass

    # First start: say what x3 does on its own and let the user switch it,
    # before the connection page reads any of those settings.
    from .widgets.welcome_wizard import run_first_start

    run_first_start()

    # Once a day at most, and only if the user turned it on (tour, Settings);
    # re-armed hourly so a window left open for days still hears of a release.
    from . import updates as _updates
    from .widgets.update_dialog import DailyCheck

    _updates.clean_downloads()

    _update_check = DailyCheck(app)  # noqa: F841 -- keep it alive with the app

    cdb_path = None
    schema_dir = None
    if "--cdb" in argv:
        i = argv.index("--cdb")
        if i + 1 < len(argv):
            cdb_path = argv[i + 1]
    if "--schema-dir" in argv:
        i = argv.index("--schema-dir")
        if i + 1 < len(argv):
            schema_dir = argv[i + 1]

    # Loop the connection page: connecting opens the session window, and closing
    # it (Disconnect / Quit) returns here instead of exiting the app -- so the
    # user can connect to another device. Cancelling the connection page itself
    # is the way out.
    carried = None
    while True:
        # A language picked in this run, on the page or under View ▸ Language,
        # takes over here: no window built in the old one is left on screen.
        if i18n.picked is not None and i18n.resolve(i18n.picked) != i18n.active():
            i18n.load(i18n.resolve(i18n.picked))
            i18n.install()
        connect = ConnectDialog(cdb_path=cdb_path, start_page=True)
        connect.set_schema_dir(schema_dir)
        if carried is not None:
            connect.restore(carried)
            carried = None
        result = connect.exec()
        if result == ConnectDialog.LANGUAGE_CHANGED:
            # Built again in the new language, as the user left it.
            carried = connect.carried()
            continue
        if (
            result != ConnectDialog.DialogCode.Accepted
            or connect.session is None
            or connect.client is None
        ):
            # Cancelled the connection page -> quit. Sweep first: a worker the
            # dialog could not join is kept alive in a module-level set, and
            # returning would let the interpreter clear that set -- destroying a
            # running QThread, which aborts.
            break

        win = MainWindow(connect.session, client=connect.client)
        win.show()
        app.exec()  # returns when the session window is closed

        # Back to the connection page: drop this session cleanly first.
        try:
            connect.client.close()
        except Exception:  # noqa: BLE001
            pass
        win.deleteLater()
        qthreads.retire_all()

    return finish()


def finish() -> int:
    """Leave the app: join what workers will join, and never abort on the rest."""
    # Backstop for any worker not caught by a window's closeEvent: a QThread
    # still running when the interpreter tears down aborts the process.
    stubborn = qthreads.retire_all()
    if stubborn:
        applog.note(f"{stubborn} worker thread(s) would not join")
    applog.shutdown(0)
    if qthreads.still_running():
        # Returning would let the interpreter destroy a QThread that is still
        # running, which Qt answers with a fatal abort -- closing the app while
        # a connect waited on an unreachable router ended that way. Nothing is
        # left to save (settings and logs are written as they go), so leave
        # without the teardown.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0
