"""Main Winbox-style session window: toolbar + tree + MDI."""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QEvent, QPoint, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QMainWindow,
    QMdiArea,
    QMdiSubWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, a11y, applog, autoexport, hazard, i18n, menu_usage, qthreads, style
from ..i18n import tr
from ..edit_mode import EditMode, EditSession
from ..icons import icon_for_kind, icon_size, reset_icon_cache
from ..session import SessionInfo
from ..settings import (
    ICON_SOURCE_CHOICES,
    load_language,
    save_language,
    icon_source_label,
    load_danger_ink,
    load_icon_source,
    load_inline_comments,
    LIST_REFRESH_CHOICES,
    load_list_refresh_secs,
    load_live_updates,
    load_most_used_auto,
    load_auto_export,
    load_auto_export_idle_secs,
    load_auto_export_sensitive,
    load_safe_mode_idle_secs,
    load_show_passwords,
    load_start_mode,
    load_theme_id,
    load_submenu_order,
    load_byte_units,
    load_dropdown_marks,
    load_form_status_line,
    load_key_hints,
    load_titlebar_tabs,
    load_update_check,
    save_danger_ink,
    save_icon_source,
    save_inline_comments,
    save_list_refresh_secs,
    save_live_updates,
    save_most_used_auto,
    save_auto_export,
    save_auto_export_idle_secs,
    save_auto_export_sensitive,
    save_safe_mode_idle_secs,
    save_show_passwords,
    save_start_mode,
    save_submenu_order,
    save_byte_units,
    save_dropdown_marks,
    save_form_status_line,
    save_key_hints,
    save_titlebar_tabs,
    save_update_check,
)
from .about_dialog import AboutDialog
from .update_dialog import UpdateDialog, check_in_background, notifier
from .welcome_wizard import WelcomeWizard
from .command_dialog import CommandDialog
from .key_hints import KeyHints
from .list_window import ListWindow
from .log_window import SessionLogWindow
from .remote_change_window import RemoteChangeWindow
from .menu_tree import MenuTree, _payload_from_nav
from .multi_page_window import MultiPageListWindow
from .quickset_window import QuickSetWindow
from .safe_mode_dialog import SafeModeReminder
from .status_marks import reset_mark_cache
from .query_window import QueryWindow
from .property_window import PropertyWindow
from .terminal_window import TerminalWindow
from .update_window import UpdateWindow
from .wheel_guard import guard_wheel

#: /system package update. Imported rather than re-spelled so the UI and the
#: device layer cannot drift apart.
from x3.proto.device import PKG_UPDATE_PATH, list_read_mask
from x3.proto.schema import NavNode

if TYPE_CHECKING:
    from x3.proto.device import DeviceClient


#: Secondary device windows opened via "New x3", kept referenced so Python
#: does not garbage-collect a live top-level window. Pruned when one closes.
_OPEN_WINDOWS: list = []

#: A window centred where another window already sits steps this far down and
#: right, so the one underneath stays visible. A title bar's height, roughly:
#: enough to read the older window's title, small enough that both stay near
#: the middle. Written at 100%; scaled where it is used, because the title bar
#: it stands for grows with the zoom.
_STACK_STEP = 26

#: How long a menu's window has to stay open before the open counts toward the
#: "most used" group. A wrong row is one click away in a dense menu, so an
#: open-and-close-again must not promote a menu nobody meant to open.
USAGE_DWELL_MS = 5000

#: Settings ▸ Safe Mode Idle Alarm. Five minutes is what a real session
#: runs at; the 10-second entry exists because an alarm you can only test
#: once every five minutes is an alarm nobody tests.
#: Settings ▸ Config Snapshot. The quiet interval after the last change; the
#: short entries exist for the same reason the Safe Mode ones do -- a 15-minute
#: debounce is untestable in a 15-minute sitting.
SNAPSHOT_IDLE_CHOICES: tuple[tuple[int, str], ...] = (
    (30, "30 seconds (test)"),
    (120, "2 minutes"),
    (300, "5 minutes"),
    (900, "15 minutes"),
    (1800, "30 minutes"),
)

SAFE_IDLE_CHOICES: tuple[tuple[int, str], ...] = (
    (0, "Off"),
    (10, "10 seconds (test)"),
    (60, "1 minute"),
    (300, "5 minutes"),
    (900, "15 minutes"),
)

#: How long input is ignored after the idle alarm goes off. Somebody coming
#: back to a window that is blaring reaches for the mouse before they have read
#: anything, and the pointer is still wherever they left it -- which may be
#: over Remove in a list, or over "Unroll & leave" once the reminder is up. So
#: the window takes its hands off the controls for a moment first.
IDLE_GUARD_SECS = 4

#: How often the guard repaints. It drives a draining bar, so it ticks far
#: faster than the once-a-second the countdown itself needs.
GUARD_TICK_MS = 60

#: What the guard swallows. Shortcut is in the list because a key that is bound
#: to an action never arrives as a KeyPress at all.
_GUARDED_EVENTS = frozenset({
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease,
    QEvent.Type.Wheel,
    QEvent.Type.Shortcut,
    QEvent.Type.ShortcutOverride,
    QEvent.Type.ContextMenu,
})

#: Input that counts as "somebody is here" for the idle watchdog.
_ACTIVITY_EVENTS = frozenset({
    QEvent.Type.MouseButtonPress,
    QEvent.Type.KeyPress,
    QEvent.Type.Wheel,
})

#: How often the toolbar re-reads CPU / memory / uptime. Winbox refreshes these
#: about once a second; every few seconds still reads as live and costs one
#: small GET per tick, which matters on a slow link.
STATS_POLL_MS = 3000

#: How often the window asks whether the connection is still there. A local
#: check (is the frame reader running?), not a request.
LINK_CHECK_MS = 1000
#: First reconnect attempt after a drop, doubling per failure up to the max.
RECONNECT_FIRST_MS = 2000
RECONNECT_MAX_MS = 30_000


def _nearest_existing(path: Path) -> Path:
    """The deepest part of *path* that is actually on disk.

    The per-device snapshot directory is created by the first capture, not by
    the session -- so on a device that has never changed anything, the folder
    named in the dialog does not exist yet. Handing that to a file manager gets
    a shrug or an error box, so open the closest real ancestor instead and say
    which one it was. Nothing is created just because somebody asked where the
    files would go.
    """
    here = path
    while not here.is_dir() and here.parent != here:
        here = here.parent
    return here


class _CallWorker(QThread):
    """Run one blocking device call off the UI thread and hand back its result.

    Used for anything the main window itself asks the device for: Safe Mode
    enter/leave (which opens a terminal channel) and the toolbar's resource
    poll. Both would otherwise freeze the window for the length of a round trip.
    """

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(
        self,
        session: SessionInfo,
        *,
        client: DeviceClient,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.client = client
        self.readonly = getattr(client, "readonly", True)
        # Read-only, queue, or live. Queue collects edits locally; Live sends
        # them. The client stays read-only until Live is picked (_apply_mode),
        # so the device layer refuses a write the UI never asked for.
        # Live by default (Settings ▸ Start In Live Mode). The client itself
        # is still read-only until _apply_mode runs at the end of __init__, so
        # nothing can be written before the mode is actually established.
        self._start_mode = {
            "live": EditMode.LIVE,
            "queue": EditMode.QUEUE,
            "readonly": EditMode.READONLY,
        }.get(load_start_mode(), EditMode.LIVE)
        self.edit = EditSession(
            self._start_mode,
            client=client,
            write_guard=self._write_guard,
        )
        self.setWindowTitle(session.title())
        # Scaled by the active zoom, so a session opened at a saved zoom starts
        # proportionally larger rather than at the 100% size with big content
        # crammed into it. scale_px reads the zoom applied at startup.
        self.resize(style.scale_px(1100), style.scale_px(700))
        self.setMinimumSize(style.scale_px(800), style.scale_px(480))

        self._open: dict[str, Any] = {}
        # Set for the duration of one open_menu call: the "most used" key the
        # window about to be registered belongs to (None = do not count).
        self._pending_open_id: str | None = None
        self._safe_mode_on = False
        self._safe_worker: _CallWorker | None = None
        #: The toolbar's Safe Mode button, once the bar has been built.
        self._safe_btn: QWidget | None = None
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._on_safe_mode_idle)
        self._idle_notified = False
        self._idle_secs = load_safe_mode_idle_secs()
        #: monotonic clock reading from when Safe Mode was armed, so the
        #: reminder can say how long ago that was rather than just "a while".
        self._safe_since: float | None = None
        #: monotonic deadline the input guard runs to; None = not guarding.
        #: A deadline rather than a countdown so the bar can drain smoothly and
        #: a late timer tick cannot stretch the wait.
        self._guard_end: float | None = None
        self._guard_timer = QTimer(self)
        self._guard_timer.setInterval(GUARD_TICK_MS)
        self._guard_timer.timeout.connect(self._tick_input_guard)
        #: How many Safe Mode dialogs are open. Answering one is being at the
        #: keyboard, so the watchdog must not fire behind it.
        self._modal_depth = 0
        #: The reminder dialog while it is up, so the guard can keep its
        #: answers locked and drain the bar in it.
        self._idle_box: SafeModeReminder | None = None
        # Winbox v3 Settings options. Loaded once here and pushed as booleans:
        # settings.py has no cache, so re-reading per pane would re-parse the
        # file once per window.
        self._show_passwords = load_show_passwords()
        self._inline_comments = load_inline_comments()
        # Toolbar resource poll (see _start_stats_poll). One worker at a time.
        self._stats_timer: QTimer | None = None
        self._stats_worker: _CallWorker | None = None
        self._stats_errors = 0
        #: While the connection is down: why, and since when (monotonic).
        #: See _check_link.
        self._link_down: tuple[str, float] | None = None
        self._link_down_in_safe_mode = False
        self._reconnect_worker: _CallWorker | None = None
        self._reconnect_tries = 0
        self._link_timer = QTimer(self)
        self._link_timer.setInterval(LINK_CHECK_MS)
        self._link_timer.timeout.connect(self._check_link)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._try_reconnect)

        # Automatic config snapshots. Built before the chrome, because the
        # toolbar's warning button reads its state as it is created.
        self.auto_export = autoexport.AutoExport(
            client=client,
            session=session,
            idle_secs=load_auto_export_idle_secs(),
            enabled=load_auto_export(),
            show_sensitive=load_auto_export_sensitive(),
            parent=self,
        )
        self.auto_export.state_changed.connect(self._refresh_snapshot_chrome)
        # Redraws the countdown. Runs only while a snapshot is actually
        # pending: a session that changed nothing must not wake up every
        # 10 seconds to repaint an empty label.
        self._snapshot_tick = QTimer(self)
        self._snapshot_tick.setInterval(10_000)
        self._snapshot_tick.timeout.connect(self._refresh_snapshot_chrome)
        self.auto_export.exported.connect(self._on_snapshot_taken)
        self.auto_export.failed.connect(self._on_snapshot_failed)
        # Every local write, from any window, arrives here.
        self.edit.on_applied = self._on_local_change
        # Every list draws the queue over the router's rows (ListPane.
        # _overlay_queue), so any change to the queue redraws them all.
        self.edit.listeners.append(self._on_queue_changed)
        #: The review window while it is open, so it is reused rather than
        #: stacked.
        self._remote_win: Any = None

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_status()

        # Live updates off means "an idle window makes no requests", so the
        # toolbar's poll obeys it too.
        if load_live_updates():
            self._start_stats_poll()

        # Push the starting mode through the normal path so the client's
        # read-only flag, the form locks and the status label all agree.
        self._apply_mode(self.edit.mode)
        if self.client is not None:
            self._link_timer.start()

        self._reload_nav()

        self._refresh_snapshot_chrome()

        # Track idle time while Safe Mode is on (app-wide input events).
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _build_menu(self) -> None:
        mb = self.menuBar()
        file_m = mb.addMenu("&File")
        act_disconnect = QAction("&Disconnect", self)
        act_disconnect.setShortcut(QKeySequence("Ctrl+D"))
        act_disconnect.triggered.connect(self.close)
        file_m.addAction(act_disconnect)
        file_m.addSeparator()
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_m.addAction(act_quit)

        win_m = mb.addMenu("&Windows")
        # Cycling, and a list of what is open. An MDI floor is navigated by
        # pointing at it: Qt gives a sub-window no keyboard route in or out, so
        # without these a window that is not on top cannot be reached at all
        # without a mouse -- and "which windows are open" is a question only
        # answerable by looking. F6 as well as Ctrl+Tab, because F6 is the key
        # that means "next pane" to anyone who navigates by keyboard.
        act_next = QAction("&Next Window", self)
        act_next.setShortcuts([QKeySequence("Ctrl+Tab"), QKeySequence("F6")])
        act_next.triggered.connect(self._next_window)
        act_prev = QAction("&Previous Window", self)
        act_prev.setShortcuts(
            [QKeySequence("Ctrl+Shift+Tab"), QKeySequence("Shift+F6")]
        )
        act_prev.triggered.connect(self._prev_window)
        win_m.addAction(act_next)
        win_m.addAction(act_prev)
        win_m.addSeparator()
        act_cascade = QAction("&Cascade", self)
        act_cascade.triggered.connect(self._cascade)
        act_tile = QAction("&Tile", self)
        act_tile.triggered.connect(self._tile)
        act_close_all = QAction("Close &All", self)
        act_close_all.triggered.connect(self._close_all_windows)
        win_m.addAction(act_cascade)
        win_m.addAction(act_tile)
        win_m.addAction(act_close_all)
        win_m.addSeparator()
        #: Where the list of open windows starts; everything from here down is
        #: rebuilt each time the menu opens.
        self._window_list_from = len(win_m.actions())
        self._window_menu = win_m
        win_m.aboutToShow.connect(self._rebuild_window_list)
        self.addAction(act_next)
        self.addAction(act_prev)

        # View is what the app looks like -- the skin, the typeface, the glyphs,
        # how a counter is rendered, where a window keeps its tabs. Settings is
        # what it does: what it writes, how often it reads, what it remembers.
        # (Show Passwords and Inline Comment are the exception, and stay under
        # Settings because Winbox v3 puts them there.)
        view_m = mb.addMenu("&View")
        theme_m = view_m.addMenu("&Theme")
        self._theme_actions: list[QAction] = []
        from .. import style as style_mod

        group_parent = theme_m  # QAction group via exclusive checks
        active_id = style_mod.active_theme().id
        for th in style_mod.list_themes():
            act = QAction(th.name, self)
            act.setCheckable(True)
            act.setChecked(th.id == active_id)
            act.setToolTip(th.description)
            act.setData(th.id)
            act.triggered.connect(lambda checked=False, tid=th.id: self._set_theme(tid))
            theme_m.addAction(act)
            self._theme_actions.append(act)
        theme_m.addSeparator()
        act_reload = QAction("Re-apply current theme", self)
        act_reload.triggered.connect(
            lambda: self._set_theme(style_mod.active_theme().id)
        )
        theme_m.addAction(act_reload)

        # Font is its own submenu rather than a per-theme fact: Tahoma (v3),
        # Segoe UI (v4) and Microsoft Sans Serif all resolve to the same
        # substitute on a stock Linux box, so the only way to see which is
        # which is to pin one and look. x3.ui.fonts does the loading.
        self._font_menu = view_m.addMenu("&Font")
        self._font_actions: list[QAction] = []
        self._build_font_menu()

        # Zoom is neither a skin nor a typeface: it is this skin at another
        # size. One factor multiplies every pixel a theme declares -- type, row
        # height, icon edge, every pad, the nav strip, and the stylesheet's own
        # hard-coded lengths -- so the window grows as one piece instead of the
        # text outgrowing the rows it sits in. See style.SCALE_STEPS.
        self._zoom_menu = view_m.addMenu("&Zoom")
        self._zoom_menu.setToolTipsVisible(True)
        self._zoom_in_act = QAction("Zoom &In", self)
        # Three bindings for one chord, because "Ctrl and plus" is not one key.
        # Ctrl+= is the one people press -- the + is shifted on a US layout and
        # every browser answers the unshifted key -- while Ctrl++ and
        # Ctrl+Shift+= are what Qt reports when they do hold Shift, and Ctrl++
        # is also what the keypad's own plus arrives as.
        self._zoom_in_act.setShortcuts([
            QKeySequence("Ctrl+="),
            QKeySequence("Ctrl++"),
            QKeySequence("Ctrl+Shift+="),
        ])
        self._zoom_in_act.setToolTip("Everything a step larger")
        self._zoom_in_act.triggered.connect(lambda: self._zoom_step(1))
        self._zoom_menu.addAction(self._zoom_in_act)
        self._zoom_out_act = QAction("Zoom &Out", self)
        self._zoom_out_act.setShortcuts([
            QKeySequence("Ctrl+-"),
            QKeySequence("Ctrl+_"),
        ])
        self._zoom_out_act.setToolTip("Everything a step smaller")
        self._zoom_out_act.triggered.connect(lambda: self._zoom_step(-1))
        self._zoom_menu.addAction(self._zoom_out_act)
        self._zoom_reset_act = QAction("&Normal Size", self)
        self._zoom_reset_act.setShortcut(QKeySequence("Ctrl+0"))
        self._zoom_reset_act.setToolTip(
            "Back to the sizes the theme was written at"
        )
        self._zoom_reset_act.triggered.connect(lambda: self._set_ui_scale(1.0))
        self._zoom_menu.addAction(self._zoom_reset_act)
        # Also hung on the window itself, the way the window-walk chords are:
        # these have to answer while the keyboard is in a list, a form or the
        # terminal, and the terminal in particular reads raw keys.
        for act in (self._zoom_in_act, self._zoom_out_act, self._zoom_reset_act):
            self.addAction(act)
        self._sync_zoom_menu()

        # Icons sit beside Theme and Font because they are the same kind of
        # choice, and because the symbolic set is inked from the theme's text
        # colour -- picking a skin and picking a set is one decision. Two of
        # them: the colour originals and the same glyphs as a single ink.
        icons_m = view_m.addMenu("&Icon Set")
        icons_m.setToolTipsVisible(True)
        self._icon_source_actions: list[QAction] = []
        current_icons = load_icon_source()
        icon_tips = {
            "builtin": "The original vector set shipped with this app: fixed "
                       "colours, semantic red/green on Add and Remove.",
            "mono": "The same glyphs as one ink, tinted to the theme's text "
                    "colour — silhouettes that match any chrome, like GNOME "
                    "symbolic icons.",
        }
        for value, label in ICON_SOURCE_CHOICES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(value)
            act.setChecked(value == current_icons)
            act.setToolTip(icon_tips.get(value, ""))
            act.triggered.connect(
                lambda checked=False, v=value: self._set_icon_source(v)
            )
            icons_m.addAction(act)
            self._icon_source_actions.append(act)
        icons_m.addSeparator()
        self._danger_ink_act = QAction("&Red for Remove and Disable", self)
        self._danger_ink_act.setCheckable(True)
        self._danger_ink_act.setChecked(load_danger_ink())
        self._danger_ink_act.setToolTip(
            "Monochrome only: keep the two buttons that take something away "
            "in the theme's warning red. The colour set always draws them red."
        )
        self._danger_ink_act.setEnabled(current_icons == "mono")
        self._danger_ink_act.toggled.connect(self._set_danger_ink)
        icons_m.addAction(self._danger_ink_act)

        # How a byte counter reads. Winbox's own rounding is the default
        # because matching it is the point, but a counter is also something
        # people paste into a calculator.
        from ..settings import BYTE_UNIT_CHOICES

        bytes_m = view_m.addMenu("&Byte Units")
        bytes_m.setToolTipsVisible(True)
        self._byte_units = load_byte_units()
        self._byte_unit_actions: list[QAction] = []
        for value, label in BYTE_UNIT_CHOICES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(value == self._byte_units)
            act.setData(value)
            act.setToolTip({
                "compact": "A new unit only once the "
                          "number would need a fifth digit.",
                "binary": "A new unit at every 1024, the way most tools round.",
                "exact": "The counter itself, with no rounding.",
            }.get(value, ""))
            act.triggered.connect(
                lambda checked=False, v=value: self._set_byte_units(v)
            )
            bytes_m.addAction(act)
            self._byte_unit_actions.append(act)

        # Every language x3 ships a table for. Taking effect means rebuilding
        # every window, so the choice is saved and applies on the connection
        # page, which is built anew once the session windows are closed.
        lang_m = view_m.addMenu("&Language")
        lang_m.setToolTipsVisible(True)
        chosen = load_language()
        for label, code in i18n.choices():
            act = QAction(label, lang_m)
            act.setCheckable(True)
            act.setChecked(code == chosen)
            act.setToolTip("Applies when you return to the connection page")
            act.triggered.connect(
                lambda checked=False, c=code, a=act: self._set_language(c, a)
            )
            lang_m.addAction(act)

        view_m.addSeparator()
        self._titlebar_tabs_act = QAction("&Tabs In Title Bar", self)
        self._titlebar_tabs_act.setCheckable(True)
        self._titlebar_tabs_act.setChecked(load_titlebar_tabs())
        self._titlebar_tabs_act.setToolTip(
            "On: multi-tab windows render their tabs inline in the title bar "
            "(compact).  Off: the classic tab strip.  Applies to windows "
            "opened after the change."
        )
        self._titlebar_tabs_act.toggled.connect(self._on_titlebar_tabs_toggled)
        view_m.addAction(self._titlebar_tabs_act)

        self._status_line_act = QAction("Form &Status Line", self)
        self._status_line_act.setCheckable(True)
        self._status_line_act.setChecked(load_form_status_line())
        self._status_line_act.setToolTip(
            "The grey line across the top of a property form: the mode it is "
            "in, and which fields you have changed but not written -- a list "
            "with how many entries it gained and lost.  Off: the line is gone "
            "and the title bar carries the mode and the count."
        )
        self._status_line_act.toggled.connect(self._on_status_line_toggled)
        view_m.addAction(self._status_line_act)

        self._dropdown_marks_act = QAction("Dropdown Status &Marks", self)
        self._dropdown_marks_act.setCheckable(True)
        self._dropdown_marks_act.setChecked(load_dropdown_marks())
        self._dropdown_marks_act.setToolTip(
            "A dot and the comment beside every entry of an interface picker: "
            "green running, hollow red down, grey disabled.  The read that "
            "fills the box already carries both, so this costs nothing on the "
            "wire.  Off is the plain list of bare names.  Applies "
            "to forms opened after the change."
        )
        self._dropdown_marks_act.toggled.connect(self._on_dropdown_marks_toggled)
        view_m.addAction(self._dropdown_marks_act)

        from ..settings import load_find_open, save_find_open

        self._find_open_act = QAction("&Find Box Open in New Lists", self)
        self._find_open_act.setCheckable(True)
        self._find_open_act.setChecked(load_find_open())
        self._find_open_act.setToolTip(
            "Lists open with their Find box showing.  Off, it stays hidden "
            "until its toolbar button (or Ctrl+F) opens it.  Applies to lists "
            "opened after the change."
        )
        self._find_open_act.toggled.connect(lambda on: save_find_open(bool(on)))
        view_m.addAction(self._find_open_act)

        act_keys = QAction("&Keyboard Keys", self)
        act_keys.setShortcut(QKeySequence("F1"))
        act_keys.setToolTip(
            "The keys that work in the window in front — or just hold Ctrl for "
            "half a second to peek at them."
        )
        act_keys.triggered.connect(self._toggle_key_hints_panel)
        view_m.addAction(act_keys)
        self._key_hints_act = QAction("Key Hints On &Ctrl", self)
        self._key_hints_act.setCheckable(True)
        self._key_hints_act.setChecked(load_key_hints())
        self._key_hints_act.setToolTip(
            "On: holding Ctrl for half a second floats the key panel over the "
            "window in front, until you let go.  Off: only F1 shows it."
        )
        self._key_hints_act.toggled.connect(self._on_key_hints_toggled)
        view_m.addAction(self._key_hints_act)

        view_m.addSeparator()
        act_log = QAction("Session &Log", self)
        act_log.setShortcut(QKeySequence("Ctrl+L"))
        act_log.setToolTip("Everything sent to and received from this device")
        act_log.triggered.connect(self._show_session_log)
        view_m.addAction(act_log)

        # Real Winbox reaches Check For Updates through System -> Packages, but
        # the .jg marks that node hide:1 and the Packages folder resolves to a
        # tabbed page group, which drops hidden children -- so there is no tree
        # affordance to hang it on. A menu entry keeps it reachable without
        # reworking how page groups are built.
        sys_m = mb.addMenu("S&ystem")
        act_update = QAction("Check For &Updates…", self)
        act_update.setToolTip("Firmware package update — /system package update")
        act_update.triggered.connect(self._open_check_for_updates)
        sys_m.addAction(act_update)

        # Winbox v3 keeps these two under Settings, and Qt orders menus by the
        # addMenu call order -- so this has to sit before Help.
        set_m = mb.addMenu("&Settings")
        self._pw_act = QAction("Show &Passwords", self)
        self._pw_act.setCheckable(True)
        self._pw_act.setChecked(self._show_passwords)
        self._pw_act.setToolTip("Reveal secret fields in lists and property forms")
        self._pw_act.toggled.connect(self._on_show_passwords_toggled)
        set_m.addAction(self._pw_act)
        self._inline_act = QAction("&Inline Comment", self)
        self._inline_act.setCheckable(True)
        self._inline_act.setChecked(self._inline_comments)
        self._inline_act.setToolTip(
            "On: comment as a trailing column.  Off: ';;; comment' above the item"
        )
        self._inline_act.toggled.connect(self._on_inline_comments_toggled)
        set_m.addAction(self._inline_act)
        set_m.addSeparator()
        self._live_start_act = QAction("Start In &Live Mode", self)
        self._live_start_act.setCheckable(True)
        self._live_start_act.setChecked(self._start_mode is EditMode.LIVE)
        self._live_start_act.setToolTip(
            "New sessions open in Live mode, writing straight to the device. "
            "Off means they open read-only."
        )
        self._live_start_act.toggled.connect(self._on_live_start_toggled)
        set_m.addAction(self._live_start_act)
        self._update_check_act = QAction("Check for x3 Updates &Daily", self)
        self._update_check_act.setCheckable(True)
        self._update_check_act.setChecked(load_update_check())
        self._update_check_act.setToolTip(
            "Ask GitHub for the newest x3 release when x3 starts, at most once a day"
        )
        self._update_check_act.toggled.connect(save_update_check)
        set_m.addAction(self._update_check_act)
        set_m.addSeparator()
        self._alpha_submenu_act = QAction("&Alphabetical Submenus", self)
        self._alpha_submenu_act.setCheckable(True)
        self._alpha_submenu_act.setChecked(load_submenu_order() == "alphabetical")
        self._alpha_submenu_act.setToolTip(
            "On: submenu entries A→Z.  Off: the device's .jg order."
        )
        self._alpha_submenu_act.toggled.connect(self._on_submenu_order_toggled)
        set_m.addAction(self._alpha_submenu_act)
        self._auto_most_used_act = QAction("Auto &Most-Used", self)
        self._auto_most_used_act.setCheckable(True)
        self._auto_most_used_act.setChecked(load_most_used_auto())
        self._auto_most_used_act.setToolTip(
            "On: a submenu's most-opened items float to its top.  "
            "Off: only items you pin show there."
        )
        self._auto_most_used_act.toggled.connect(self._on_auto_most_used_toggled)
        set_m.addAction(self._auto_most_used_act)
        self._live_updates_act = QAction("&Live Updates (new lists)", self)
        self._live_updates_act.setCheckable(True)
        self._live_updates_act.setChecked(load_live_updates())
        self._live_updates_act.setToolTip(
            "Default for new list windows: on = subscribe + auto-refresh; off = "
            "low-bandwidth (an idle window makes no requests until you Refresh). "
            "Also keeps the toolbar's CPU / memory / uptime readings ticking."
        )
        self._live_updates_act.toggled.connect(self._on_live_updates_toggled)
        set_m.addAction(self._live_updates_act)
        # How often a live list re-reads. Separate from Live because the two
        # answer different questions: Live is "may this window talk to the
        # device at all", this is "how often", and a 1s Interface list on a
        # slow link is a reason to slow down, not to go silent.
        refresh_m = set_m.addMenu("List Auto &Refresh")
        refresh_m.setToolTipsVisible(True)
        self._list_refresh_secs = load_list_refresh_secs()
        self._list_refresh_actions: list[QAction] = []
        for secs, label in LIST_REFRESH_CHOICES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(secs == self._list_refresh_secs)
            act.setData(secs)
            if secs is None:
                tip = "Each list polls at the rate its own menu declares"
            elif secs == 0:
                tip = ("No polling. The list still follows edits made "
                       "elsewhere, and Refresh still reads.")
            else:
                tip = f"Every open list re-reads the device {label.lower()}"
            act.setToolTip(tip)
            act.triggered.connect(
                lambda checked=False, sec=secs: self._set_list_refresh(sec)
            )
            refresh_m.addAction(act)
            self._list_refresh_actions.append(act)
        # How long an armed session may sit untouched before it starts asking
        # to be dealt with. A submenu rather than one checkbox: the right
        # interval depends on how you work, and Off is a legitimate answer for
        # someone who never leaves a Safe Mode session unattended.
        idle_m = set_m.addMenu("Safe Mode &Idle Alarm")
        idle_m.setToolTipsVisible(True)
        self._idle_actions: list[QAction] = []
        for secs, label in SAFE_IDLE_CHOICES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(secs == self._idle_secs)
            act.setData(secs)
            act.setToolTip(
                "Never remind me that Safe Mode is still on"
                if secs == 0
                else "Blink the banner, ring, and ask, once the session has "
                     f"been idle this long ({label.split(' (')[0]}) in Safe Mode"
            )
            act.triggered.connect(
                lambda checked=False, s=secs: self._set_idle_secs(s)
            )
            idle_m.addAction(act)
            self._idle_actions.append(act)

        # A snapshot is only ever taken because something changed, so this
        # costs nothing on a session that reads. Off is still offered: on a
        # device with no console access it would fail every time.
        set_m.addSeparator()
        self._auto_export_act = QAction("&Auto Config Snapshot", self)
        self._auto_export_act.setCheckable(True)
        self._auto_export_act.setChecked(self.auto_export.enabled)
        self._auto_export_act.setToolTip(
            "Take a text /export once the session has been quiet for a while "
            "AFTER something changed -- by you or by anyone else. Read into "
            "memory over the console: nothing is written to the router's flash."
        )
        self._auto_export_act.toggled.connect(self._on_auto_export_toggled)
        set_m.addAction(self._auto_export_act)
        self._auto_export_secret_act = QAction("Snapshot Includes &Secrets", self)
        self._auto_export_secret_act.setCheckable(True)
        self._auto_export_secret_act.setChecked(self.auto_export.show_sensitive)
        self._auto_export_secret_act.setToolTip(
            "On: /export show-sensitive — keys and passwords included, so the "
            "file can actually restore the device, and every snapshot is a "
            "credential dump for it. Off: secrets blanked; good for diffing, "
            "cannot rebuild a tunnel."
        )
        self._auto_export_secret_act.toggled.connect(
            self._on_auto_export_secret_toggled
        )
        set_m.addAction(self._auto_export_secret_act)
        snap_m = set_m.addMenu("Snapshot &Quiet Period")
        snap_m.setToolTipsVisible(True)
        self._snapshot_actions: list[QAction] = []
        for secs, label in SNAPSHOT_IDLE_CHOICES:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(secs == self.auto_export.idle_secs)
            act.setData(secs)
            act.setToolTip(
                f"Wait {label.split(' (')[0]} of no further change before "
                "taking the snapshot. Each new change restarts the wait."
            )
            act.triggered.connect(
                lambda checked=False, sec=secs: self._set_snapshot_secs(sec)
            )
            snap_m.addAction(act)
            self._snapshot_actions.append(act)
        act_snap_now = QAction("Take Snapshot &Now", self)
        act_snap_now.setToolTip("Export the running config immediately.")
        act_snap_now.triggered.connect(self._snapshot_now)
        set_m.addAction(act_snap_now)
        act_snap_dir = QAction("Snapshot &Folder…", self)
        act_snap_dir.setToolTip("Where the snapshots for this device are kept.")
        act_snap_dir.triggered.connect(self._show_snapshot_folder)
        set_m.addAction(act_snap_dir)

        help_m = mb.addMenu("&Help")
        tour = QAction("&Welcome Tour…", self)
        tour.setToolTip("Theme, start mode, config snapshots and audit, explained")
        tour.triggered.connect(self._welcome_tour)
        help_m.addAction(tour)
        check = QAction("Check for x3 &Updates…", self)
        check.setToolTip("Look for a newer release of x3 itself (not the router's firmware)")
        check.triggered.connect(self._check_updates_now)
        help_m.addAction(check)
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_m.addAction(about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setIconSize(icon_size())
        # Icon-only buttons (tooltips carry the name) — denser, Winbox-like
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(tb)

        # Undo / Redo at the far left, where Winbox v3 has them. They move the
        # router's own history -- router-wide, whoever made the change -- so
        # each says what it took back, and neither has a key: a stray Ctrl+Z
        # in a list must not quietly undo somebody's config.
        from PySide6.QtWidgets import QStyle

        self._undo_act = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack), "Undo", self
        )
        self._undo_act.setToolTip(
            "Undo the router's latest change (System ▸ History) — router-wide, "
            "whoever made it"
        )
        self._undo_act.triggered.connect(lambda: self._history_step(undo=True))
        tb.addAction(self._undo_act)
        self._redo_act = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward), "Redo", self
        )
        self._redo_act.setToolTip("Redo the change Undo last took back")
        self._redo_act.triggered.connect(lambda: self._history_step(undo=False))
        tb.addAction(self._redo_act)
        tb.addSeparator()

        # Row verbs (Add/Remove/Enable/Disable/Comment) live on each list
        # window's own toolbar, as in Winbox. This bar stays session-scoped.
        term = QAction(icon_for_kind("terminal"), "Terminal", self)
        term.setToolTip("New Terminal")
        term.triggered.connect(lambda: self.open_menu({"kind": "terminal", "id": "terminal", "title": "New Terminal"}))
        tb.addAction(term)

        tb.addSeparator()
        tb.addWidget(QLabel(" Mode: "))
        self._mode_combo = QComboBox()
        for mode, text, tip in (
            (EditMode.READONLY, "Read-only", "Forms are locked; nothing can change."),
            (EditMode.LIVE, "Live",
             "Write straight to the device: Apply, Remove, Enable/Disable and "
             "Comment take effect immediately. Safe Mode is recommended."),
            (EditMode.QUEUE, "Queue",
             "Edits are collected locally and never sent."),
        ):
            self._mode_combo.addItem(tr(text), mode)
            idx = self._mode_combo.count() - 1
            self._mode_combo.setItemData(idx, tr(tip), Qt.ItemDataRole.ToolTipRole)
        start = self._mode_combo.findData(self.edit.mode)
        self._mode_combo.setCurrentIndex(start if start >= 0 else 0)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_selected)
        # Of every dropdown in the app this is the one a stray wheel roll must
        # not move: two notches over it is Read-only -> Live (see wheel_guard).
        guard_wheel(self._mode_combo)
        tb.addWidget(self._mode_combo)

        self._mode_lab = QLabel()
        tb.addWidget(self._mode_lab)

        # Every menu entry by name, at any depth: "serv" Enter is IP ▸ Services.
        # Ctrl+K from anywhere, or just start typing with the nav focused.
        from .menu_search import MenuSearch

        tb.addSeparator()
        self._menu_search = MenuSearch(
            self._nav_roots, host=self, usage=menu_usage.usage_counts
        )
        self._menu_search.chosen.connect(self.open_menu)
        tb.addWidget(self._menu_search)
        search_act = QAction("Search Menus", self)
        search_act.setShortcut(QKeySequence("Ctrl+K"))
        search_act.triggered.connect(lambda: self._menu_search.start())
        self.addAction(search_act)

        # Kept as attributes and refreshed by the resource poll: built once from
        # the connect-time reading, these three sat frozen for the whole session.
        self._stat_labels: dict[str, QLabel] = {}
        for key in ("cpu", "memory", "uptime"):
            lab = QLabel()
            lab.setObjectName("toolbarStat")
            tb.addWidget(lab)
            self._stat_labels[key] = lab
        self._refresh_stat_labels()

        if self.client and self.client.schema:
            lab = QLabel(f"  schema: {self.client.schema.version}")
            lab.setObjectName("toolbarStat")
            tb.addWidget(lab)

        # Safe Mode stands alone on the right, past a stretch. It is the only
        # control on this bar that changes what a disconnect *costs*, and sat
        # among the others it read as one more button; in the corner it has the
        # weight it should, next to the readings that say what session this is.
        # Armed, the button goes yellow-and-black and the banner below it opens
        # -- both from _refresh_safe_mode_chrome, which is the one switch.
        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        tb.addWidget(spacer)

        # Pending changes sits next to Safe Mode because it is the same kind of
        # control: session-scoped, and about what this connection has cost so
        # far. It also earns that space only when it holds something -- in
        # Read-only or Live with an empty queue there is nothing to open, so the
        # button and its separator go away entirely rather than sitting greyed
        # out all session. In Queue mode it stays put, greyed, because an empty
        # queue is news there. _refresh_mode_label is the one switch.
        # Somebody else's change. Deliberately a button and nothing else: no
        # modal, no beep, no focus grab. The whole point of this warning is
        # that it arrives while the user may be part-way through fixing
        # whatever caused it -- a dialog demanding an answer would take the UI
        # away exactly when it is needed. It earns its space only when it has
        # something to say, and clicking it opens a review window that is also
        # non-modal.
        self._remote_act = QAction("Changed elsewhere", self)
        self._remote_act.triggered.connect(self._show_remote_changes)
        tb.addAction(self._remote_act)
        self._remote_btn = tb.widgetForAction(self._remote_act)
        self._remote_sep = tb.addSeparator()

        # Quiet by design: a label, not a button. It says what the snapshot
        # timer is doing for anyone who wonders, and says nothing at all when
        # there is nothing pending.
        self._snapshot_lab = QLabel()
        self._snapshot_lab.setObjectName("toolbarStat")
        tb.addWidget(self._snapshot_lab)

        self._queue_act = QAction("Pending changes", self)
        self._queue_act.triggered.connect(self._show_queue)
        tb.addAction(self._queue_act)
        self._queue_sep = tb.addSeparator()

        self._safe_act = QAction("Safe Mode", self)
        self._safe_act.setCheckable(True)
        self._safe_act.setEnabled(self.client is not None)
        self._safe_act.setToolTip(
            "Safe Mode (firmware 7.18+): changes unrolled if this session disconnects. "
            "Toggle on before risky edits; leave with keep/unroll."
        )
        self._safe_act.toggled.connect(self._on_safe_mode_toggled)
        tb.addAction(self._safe_act)
        # The button only exists once the action is on a bar, and only a widget
        # can carry the hazard colours -- a QAction has no stylesheet.
        self._safe_btn = tb.widgetForAction(self._safe_act)

        # Last, not up beside the combo where it used to sit: it paints
        # _queue_act and its separator too, and the bar only grows those here.
        self._refresh_mode_label()

    def _show_session_log(self) -> None:
        log = getattr(self.client, "session_log", None)
        if log is None:
            QMessageBox.information(
                self, "Session Log", "No transcript for this connection."
            )
            return
        if self._focus_existing("session-log"):
            return
        win = SessionLogWindow(log)
        self._show_child(win)
        self._register("session-log", win)

    def _refresh_stat_labels(self) -> None:
        """Redraw the toolbar readings from what the session currently holds."""
        text = {
            "cpu": f"{tr('CPU:')} {self.session.cpu_load}",
            "memory": f"{tr('Memory:')} {self._memory_text()}",
            "uptime": f"{tr('Uptime:')} {self.session.uptime}",
        }
        for key, lab in self._stat_labels.items():
            lab.setText(text[key])

    def _start_stats_poll(self) -> None:
        """Keep the toolbar's CPU / memory / uptime moving.

        A resource reply is the only place these come from, and nothing re-read
        it after connect, so the toolbar showed the load and uptime from the
        moment of login until the session ended. The tick is skipped while the
        window is minimised, and while a previous read is still out — a device
        slower than STATS_POLL_MS must not accumulate threads.
        """
        if self._stats_timer is not None or self.client is None:
            return
        if getattr(self.client, "refresh_resources", None) is None:
            return
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(STATS_POLL_MS)
        self._stats_timer.timeout.connect(self._poll_stats)
        self._stats_timer.start()

    def _stop_stats_poll(self) -> None:
        timer, self._stats_timer = self._stats_timer, None
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _poll_stats(self) -> None:
        if self._stats_worker is not None or self.client is None or self.isMinimized():
            return
        worker = _CallWorker(self.client.refresh_resources, self)
        self._stats_worker = worker
        worker.finished_ok.connect(self._on_stats_read)
        worker.finished_err.connect(self._on_stats_error)
        worker.finished.connect(self._clear_stats_worker)
        # One thread object per tick would otherwise pile up as children of this
        # window for the life of the session.
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_stats_worker(self) -> None:
        self._stats_worker = None

    def _on_stats_read(self, info: object) -> None:
        """A resource reply landed. Copy the four moving readings and redraw."""
        self._stats_errors = 0
        for name in ("cpu_load", "free_memory", "total_memory", "uptime"):
            val = getattr(info, name, "")
            if val:
                setattr(self.session, name, val)
        self._refresh_stat_labels()

    def _on_stats_error(self, msg: str) -> None:
        """Leave the last good reading on screen; give up after a few failures.

        A dead session fails every tick, and this poll is the one request the
        user never asked for -- it must not be what fills the log or keeps
        knocking on a router that has gone away.
        """
        self._stats_errors += 1
        if self._stats_errors < 3:
            return
        self._stop_stats_poll()
        applog.note(f"toolbar resource poll stopped after: {msg}")

    # --- a dropped connection -------------------------------------------
    #
    # A router that goes away (a reboot, a dead uplink) left the session
    # looking alive: each window failed on its own, a background poll knocked
    # on the closed socket for hours, and nothing said the connection was
    # gone. Winbox v3 says "disconnected" and reconnects with the windows
    # still open; so does this, by putting a new session under the same
    # client every window already holds (DeviceClient.reconnect).

    def _check_link(self) -> None:
        if self.client is None or self._link_down is not None:
            return
        lost = getattr(self.client, "connection_lost", None)
        why = lost() if callable(lost) else None
        if why is not None:
            self._on_link_lost(f"{type(why).__name__}: {why}")

    def _on_link_lost(self, why: str) -> None:
        self._link_down = (why, time.monotonic())
        self._reconnect_tries = 0
        self._link_down_in_safe_mode = self._safe_mode_on
        if self._safe_mode_on:
            # The router unrolled Safe Mode the moment the link dropped; the
            # window must stop saying it is on.
            self._safe_mode_on = False
            self._safe_act.blockSignals(True)
            self._safe_act.setChecked(False)
            self._safe_act.blockSignals(False)
            self._idle_timer.stop()
            self._idle_notified = False
            self._safe_since = None
            self._end_input_guard()
            self._hazard_bar.set_alarm(False)
            self._refresh_safe_mode_chrome()
        self._stop_stats_poll()
        applog.note(f"connection lost: {why}")
        self._link_label.setText("Disconnected — reconnecting…")
        self._link_label.setToolTip(why)
        self._link_label.show()
        self._reconnect_timer.start(RECONNECT_FIRST_MS)

    def _try_reconnect(self) -> None:
        if self.client is None or self._link_down is None:
            return
        if self._reconnect_worker is not None:
            return
        self._reconnect_tries += 1
        self._link_label.setText(
            f"Disconnected — reconnecting (try {self._reconnect_tries})…"
        )
        worker = _CallWorker(self.client.reconnect, self)
        self._reconnect_worker = worker
        worker.finished_ok.connect(lambda _r: self._on_reconnected())
        worker.finished_err.connect(self._on_reconnect_failed)
        worker.finished.connect(self._clear_reconnect_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _clear_reconnect_worker(self) -> None:
        self._reconnect_worker = None

    def _on_reconnect_failed(self, msg: str) -> None:
        if self._link_down is None:
            return
        # 2, 4, 8 ... seconds, then every RECONNECT_MAX_MS: a router that is
        # rebooting is back within a minute, one that is gone is not hammered.
        delay = min(RECONNECT_MAX_MS, RECONNECT_FIRST_MS * 2 ** self._reconnect_tries)
        self._link_label.setText(
            f"Disconnected — retrying in {delay // 1000}s"
        )
        self._link_label.setToolTip(msg)
        self._reconnect_timer.start(delay)

    def _on_reconnected(self) -> None:
        why, since = self._link_down or ("", time.monotonic())
        self._link_down = None
        self._reconnect_tries = 0
        self._link_label.hide()
        down = int(time.monotonic() - since)
        applog.note(f"reconnected after {down}s")
        self.statusBar().showMessage(f"Reconnected after {down}s", 8000)
        if load_live_updates():
            self._stats_errors = 0
            self._start_stats_poll()
        # Every window re-reads from the new session: its subscription and
        # poll died with the old one.
        from .list_pane import ListPane

        for sub in self.mdi.subWindowList():
            for pane in sub.findChildren(ListPane):
                pane.resume_after_reconnect()
            for form in [sub, *sub.findChildren(PropertyWindow)]:
                if isinstance(form, PropertyWindow):
                    form.resume_after_reconnect()
        if self._link_down_in_safe_mode:
            self._link_down_in_safe_mode = False
            QMessageBox.warning(
                self,
                "Safe Mode",
                "The connection dropped while Safe Mode was on, so the router "
                "undid every change made since it was turned on.\n\n"
                "The open windows have been re-read and show what the router "
                "holds now.",
            )

    def _memory_text(self) -> str:
        """Winbox reads free / used / total, e.g. '883.4 MB / 140.3 MB / 1024 MB'."""
        free_text = self.session.free_memory or "?"
        total_text = self.session.total_memory or "?"

        def as_kb(text: str) -> int | None:
            """Recover the kilobyte count from a rendered size."""
            t = str(text).strip()
            try:
                if t.upper().endswith("GB"):
                    return int(float(t[:-2]) * 1024 * 1024)
                if t.upper().endswith("MB"):
                    return int(float(t[:-2]) * 1024)
                if t.upper().endswith("KB"):
                    return int(float(t[:-2]))
                return int(float(t))
            except (TypeError, ValueError):
                return None

        free_kb, total_kb = as_kb(free_text), as_kb(total_text)
        if free_kb is None or total_kb is None or total_kb < free_kb:
            return free_text
        from x3.proto.schema import format_kbytes

        used = format_kbytes(total_kb - free_kb)
        return f"{free_text} / {used} / {total_text}"

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(style.scale_px(3))

        self.tree = MenuTree()
        # Flat nav + flyout: keep the left strip narrow; flyouts open over MDI
        self.tree.setMinimumWidth(style.TREE_WIDTH)
        self.tree.setMaximumWidth(style.scale_px(200))
        self.tree.menu_activated.connect(self.open_menu)
        self.tree.type_ahead.connect(self._search_from_tree)
        self.tree.search_takes_typing = True
        splitter.addWidget(self.tree)

        self.mdi = QMdiArea()
        # Hold Ctrl -> the keys this window answers to. Built here because it
        # floats over the area; it watches the application for a held modifier
        # and scopes what it sees to this session (see KeyHints._owns).
        self._key_hints = KeyHints(self, self.mdi, enabled=load_key_hints())
        self.mdi.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.mdi.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.mdi.setViewMode(QMdiArea.ViewMode.SubWindowView)
        # Closing a window hands the area to the one used last, not the one
        # opened first. With Qt's creation order, OK on a Graphing rule's form
        # brought up Interfaces -- the oldest window, often the one sitting
        # under the button -- and buried the Graphing window it came from.
        self.mdi.setActivationOrder(QMdiArea.WindowOrder.ActivationHistoryOrder)
        self.mdi.setTabsClosable(True)
        self.mdi.setDocumentMode(True)
        splitter.addWidget(self.mdi)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([style.TREE_WIDTH, style.scale_px(900)])

        # The banner is part of the window, not an overlay: it takes its own
        # height so an armed session is visibly *shorter*, and nothing it
        # covers can be clicked by accident. Hidden until Safe Mode is on, so
        # an ordinary session loses no room to it.
        self._hazard_bar = hazard.HazardBar()
        self._hazard_bar.setVisible(False)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._hazard_bar)
        lay.addWidget(splitter, 1)
        self.setCentralWidget(body)

    def _build_status(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        # Every message this window shows goes through one signal, which makes
        # this the one place to say them out loud: Safe Mode arming, a theme
        # change, a menu with no dialog, a commit that failed. A sighted user
        # catches the bar changing out of the corner of an eye; nothing tells a
        # screen reader that it did.
        sb.messageChanged.connect(self._speak_status)
        # Shown only while the connection is down (see _on_link_lost).
        self._link_label = QLabel()
        self._link_label.setStyleSheet("color: #c00000; font-weight: bold;")
        self._link_label.hide()
        sb.addPermanentWidget(self._link_label)
        # Shown only while a newer x3 is known (update_dialog.notifier).
        self._update_button = QPushButton()
        self._update_button.setFlat(True)
        self._update_button.hide()
        self._update_button.clicked.connect(self._open_update)
        sb.addPermanentWidget(self._update_button)
        notifier.available.connect(self._show_update)
        if notifier.release is not None:
            self._show_update(notifier.release)
        via = (
            f"MAC-Winbox {self.session.mac or self.session.host}"
            if self.session.transport == "mac"
            else f"TCP {self.session.host}"
        )
        sch = self.client.schema.version if self.client.schema else "?"
        sb.showMessage(
            f"Live {via} as {self.session.user} — schema {sch} — "
            f"{'READ-ONLY' if self.readonly else 'read-write'}"
        )
        self._refresh_safe_mode_chrome()

    def _speak_status(self, text: str) -> None:
        """Read a status-bar message out, for whoever cannot see the bar.

        Clearing it says nothing -- ``announce`` ignores an empty message --
        so a temporary message expiring is silent rather than a pause anyone
        has to listen through.
        """
        a11y.announce(self, text)

    # --- Safe Mode ----------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        et = event.type()
        if self._guard_left > 0:
            # Deliberately does not count as somebody being back: the guard
            # runs its full course, and a click landing during it is exactly
            # the click it exists to catch.
            if et in _GUARDED_EVENTS and self._owns(obj):
                return True
            return super().eventFilter(obj, event)
        if self._safe_mode_on and et in _ACTIVITY_EVENTS:
            # Not scoped to this window on purpose: working in another session
            # means the human is at the desk, which is what "idle" is about.
            self._bump_idle_timer()
        return super().eventFilter(obj, event)

    def _owns(self, obj) -> bool:
        """Is this event on its way to this session, or a dialog of it?

        The filter is installed on the application, so without this a guard in
        one session window would freeze every other one -- and on a fleet the
        other window is a different router someone may be working in.
        """
        widget = obj if isinstance(obj, QWidget) else None
        if widget is None:
            return False
        top = widget.window()
        while top is not None:
            if top is self:
                return True
            parent = top.parentWidget()
            top = parent.window() if parent is not None else None
        return False

    # --- the post-alarm input guard -------------------------------------

    @property
    def _guard_left(self) -> int:
        """Whole seconds still to wait; 0 when the guard is not running."""
        if self._guard_end is None:
            return 0
        return max(0, math.ceil(self._guard_end - time.monotonic() - 1e-9))

    @property
    def _guard_secs_left(self) -> float:
        if self._guard_end is None:
            return 0.0
        return max(0.0, self._guard_end - time.monotonic())

    def _start_input_guard(self) -> None:
        self._guard_end = time.monotonic() + IDLE_GUARD_SECS
        self._guard_timer.start()
        self._hazard_bar.set_text(self._hazard_text())
        self._sync_idle_box()

    def _tick_input_guard(self) -> None:
        if self._guard_left <= 0:
            self._end_input_guard()
            return
        self._hazard_bar.set_text(self._hazard_text())
        self._sync_idle_box()

    def _end_input_guard(self) -> None:
        self._guard_timer.stop()
        self._guard_end = None
        self._hazard_bar.set_text(self._hazard_text())
        self._sync_idle_box()

    def _sync_idle_box(self) -> None:
        """Keep the reminder's buttons locked while the guard runs.

        Disabled buttons with no explanation read as a broken dialog, so the
        informative line says what is being waited for and counts it down.
        """
        if self._idle_box is not None:
            self._idle_box.set_guard(self._guard_secs_left, IDLE_GUARD_SECS)

    # --- who this window is ---------------------------------------------

    def _device_label(self) -> str:
        """"MyRouter (192.168.88.24)" — enough to tell two sessions apart.

        Every Safe Mode warning carries it. A modal that says only "the session
        has been idle" is useless with more than one router open, which is the
        normal way this app gets used.
        """
        where = self.session.connect_label()
        name = (self.session.identity or "").strip()
        return f"{name} ({where})" if name and name != where else where

    def _hazard_text(self) -> str:
        who = self._device_label()
        if self._guard_left > 0:
            return f"INPUT HELD {self._guard_left}s — {who} — read before you click"
        return f"SAFE MODE — {who} — a dropped session unrolls every change made here"

    @contextmanager
    def _modal(self):
        """Mark a Safe Mode dialog as open, and hold the watchdog while it is.

        Reading one of these *is* being at the keyboard. Without this the
        reminder stacked a second copy of itself on top of the one somebody was
        still deciding on: clicking inside the dialog counted as activity, which
        re-armed the very timer that had opened it. The new box then sat modal
        over the old one, so the first could not be answered at all — and each
        fresh alarm started another input guard, so the buttons kept locking.
        """
        self._modal_depth += 1
        self._idle_timer.stop()
        try:
            yield
        finally:
            self._modal_depth = max(0, self._modal_depth - 1)
            if self._modal_depth == 0:
                self._bump_idle_timer()

    def _nudge_open_dialog(self) -> None:
        """Ring again and raise the dialog that is already asking.

        The answer to "the alarm went off while a dialog was up" is to bring
        that dialog forward, not to open another one. activeModalWidget is what
        picks the topmost of a nested pair — raising the reminder over its own
        unroll confirmation would bury the box that actually wants an answer.
        """
        app = QApplication.instance()
        if app is None:
            return
        app.beep()
        app.alert(self, 0)
        top = app.activeModalWidget() or self._idle_box
        if top is not None:
            top.raise_()
            top.activateWindow()

    def _bump_idle_timer(self) -> None:
        """Any input restarts the watchdog and calls off the alarm."""
        self._idle_notified = False
        if self._modal_depth > 0:
            # Held deliberately: see _modal.
            self._idle_timer.stop()
            return
        if self._hazard_bar.alarming():
            # Only when an alarm was actually up: this runs on every keystroke
            # and every click, and clearing unconditionally would eat whatever
            # the rest of the window is trying to say.
            self._hazard_bar.set_alarm(False)
            self.statusBar().clearMessage()
        if not self._safe_mode_on or self._idle_secs <= 0:
            self._idle_timer.stop()
            return
        self._idle_timer.start(self._idle_secs * 1000)

    @staticmethod
    def _idle_text(secs: int) -> str:
        """Spell an interval the way it was set: `10 seconds`, `5 minutes`.

        The reminder used to round every interval to whole minutes, so the
        10-second test setting announced itself as one minute.
        """
        secs = max(1, int(secs))
        if secs < 60:
            return f"{secs} second{'s' if secs != 1 else ''}"
        mins = round(secs / 60)
        return f"{mins} minute{'s' if mins != 1 else ''}"

    def _raise_idle_alarm(self) -> None:
        """Everything short of the dialog: banner, bell, taskbar, status bar.

        Split out so the alarm lands *before* the modal opens. A QMessageBox
        blocks this thread until it is answered, and a session left armed on a
        screen nobody is looking at is exactly the case this watchdog is for --
        the window has to be shouting on its own before anyone comes back.
        """
        self._hazard_bar.set_alarm(True)
        self._start_input_guard()
        app = QApplication.instance()
        if app is not None:
            app.beep()
            # Flashes the taskbar entry / dock icon: the one attention request
            # Qt has that works when this window is not the one in front.
            app.alert(self, 0)
        self.statusBar().showMessage(
            f"Safe Mode idle for {self._idle_text(self._idle_secs)} on "
            f"{self._device_label()} — a disconnect now unrolls your changes",
            0,
        )
        applog.note(f"safe mode idle alarm after {self._idle_secs}s")

    def _confirm_unroll(self, action: str) -> bool:
        """Second gate in front of a revert. True means go ahead.

        Unrolling is the only thing in this window that destroys work rather
        than making it: it walks back every change the session made, and once
        the buffer is released there is no redo. It sits one button away from
        "keep changes" in three separate dialogs, and in two of them it is the
        middle button -- so it gets its own confirmation, with the default and
        the Escape key both on Cancel.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(f"Discard changes — {self._device_label()}")
        box.setText(
            f"Discard every change made on {self._device_label()} "
            "since Safe Mode was armed?"
        )
        lines = []
        if self._safe_since is not None:
            armed = self._idle_text(int(time.monotonic() - self._safe_since))
            lines.append(f"Safe Mode has been on for {armed}.")
        lines.append(
            "This cannot be undone — there is no redo once the buffer is "
            "released."
        )
        # Straight from the handler's limits: undo depth is counted here, not
        # on the router, so promising more than this would be a lie.
        lines.append(
            "Only writes this client made while Safe Mode was held can be "
            "wound back; anything changed from another session or the console "
            "stays."
        )
        box.setInformativeText("\n\n".join(lines))
        go = box.addButton(action, QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        with self._modal():
            box.exec()
        confirmed = box.clickedButton() is go
        applog.note(
            f"unroll {'confirmed' if confirmed else 'cancelled'} "
            f"on {self._device_label()}"
        )
        return confirmed

    def _idle_rows(self) -> list[tuple[str, str]]:
        """What the reminder reports about this session.

        It used to say "this session has been idle" and nothing else -- no
        router, no user, no way to tell which of the open windows was asking,
        and no sense of how much work was riding on the answer.
        """
        rows = [
            ("Session", f"{self.session.user}@{self.session.connect_label()}"),
            ("Device", f"{self.session.board}, firmware {self.session.ros_version}"),
        ]
        if self._safe_since is not None:
            armed = self._idle_text(int(time.monotonic() - self._safe_since))
            rows.append(("Armed", f"Safe Mode on for {armed}"))
        rows.append(
            ("Idle", f"no input here for {self._idle_text(self._idle_secs)}")
        )
        return rows

    def _on_safe_mode_idle(self) -> None:
        if not self._safe_mode_on:
            return
        app = QApplication.instance()
        if self._modal_depth or (
            app is not None and app.activeModalWidget() is not None
        ):
            # Somebody is already looking at a dialog -- this one, its unroll
            # confirmation, or anything else the app put up. Ring and raise it.
            self._nudge_open_dialog()
            return
        if self._idle_notified:
            return
        self._idle_notified = True
        self._raise_idle_alarm()
        box = SafeModeReminder(
            heading=self._device_label(),
            rows=self._idle_rows(),
            consequence=(
                "If this connection drops now, every change made since Safe "
                "Mode was armed is unrolled."
            ),
            legend=(
                "Leave — keep commits everything done here.  "
                "Leave — discard unrolls it.  "
                "Stay in Safe Mode asks again later."
            ),
            parent=self,
        )
        # Held by the guard, which locks its answers and drains the bar above
        # them for as long as it runs.
        self._idle_box = box
        self._sync_idle_box()
        with self._modal():
            try:
                box.exec()
            finally:
                self._idle_box = None
            self._hazard_bar.set_alarm(False)
            self._finish_idle_choice(box.choice())

    def _finish_idle_choice(self, choice: str | None) -> None:
        """Act on the reminder's answer. Split out so the dialog's own scope
        ends before anything it triggers opens a window of its own."""
        if choice == SafeModeReminder.KEEP and self.client is not None:
            self._run_safe_mode(lambda: self.client.safe_mode_leave(keep_changes=True))
        elif choice == SafeModeReminder.DISCARD and self.client is not None:
            if self._confirm_unroll("Unroll — discard my changes"):
                self._run_safe_mode(
                    lambda: self.client.safe_mode_leave(keep_changes=False)
                )
            else:
                self._bump_idle_timer()
        else:
            # Dismissed — arm timer again, so a session left armed and walked
            # away from asks again rather than going quiet for good.
            self._bump_idle_timer()

    def _on_safe_mode_toggled(self, checked: bool) -> None:
        if self.client is None:
            self._safe_act.blockSignals(True)
            self._safe_act.setChecked(False)
            self._safe_act.blockSignals(False)
            return
        if self._safe_worker is not None and self._safe_worker.isRunning():
            # Revert check until worker finishes
            self._safe_act.blockSignals(True)
            self._safe_act.setChecked(not checked)
            self._safe_act.blockSignals(False)
            self.statusBar().showMessage("Safe Mode operation already in progress…", 3000)
            return
        if checked:
            self.statusBar().showMessage("Enabling Safe Mode…")
            self._run_safe_mode(self.client.safe_mode_enter, want_on=True)
        else:
            if not self._safe_mode_on:
                return
            # Nothing written under Safe Mode, nothing to keep or unroll: leave
            # without asking. Every config write counts itself (device.py,
            # _note_safe_mode_change), so zero is a fact here, not a guess.
            pending = getattr(self.client, "safe_mode_pending", lambda: None)()
            if pending == 0:
                self.statusBar().showMessage("Leaving Safe Mode (no changes)…")
                self._run_safe_mode(
                    lambda: self.client.safe_mode_leave(keep_changes=True),
                    want_on=False,
                )
                return
            # Ask keep vs unroll
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Leave Safe Mode")
            box.setText(
                "Leave Safe Mode?\n\n"
                "• Keep changes — commit everything done under Safe Mode\n"
                "• Unroll — discard those changes (like a disconnect)"
            )
            keep_btn = box.addButton("Keep changes", QMessageBox.ButtonRole.AcceptRole)
            unroll_btn = box.addButton("Unroll changes", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
            with self._modal():
                box.exec()
            clicked = box.clickedButton()
            if clicked is cancel_btn or clicked is None:
                self._safe_act.blockSignals(True)
                self._safe_act.setChecked(True)
                self._safe_act.blockSignals(False)
                return
            keep = clicked is keep_btn
            if not keep and not self._confirm_unroll("Unroll — discard my changes"):
                # Backing out of the revert leaves the session as it was:
                # armed, with the changes still in the buffer.
                self._safe_act.blockSignals(True)
                self._safe_act.setChecked(True)
                self._safe_act.blockSignals(False)
                return
            self.statusBar().showMessage(
                "Leaving Safe Mode (keep)…" if keep else "Unrolling Safe Mode…"
            )
            self._run_safe_mode(
                lambda: self.client.safe_mode_leave(keep_changes=keep),
                want_on=False,
            )

    def _run_safe_mode(self, fn: Callable[[], Any], *, want_on: bool | None = None) -> None:
        worker = _CallWorker(fn, self)
        self._safe_worker = worker

        def ok(st) -> None:
            active = bool(getattr(st, "active_here", False))
            self._safe_mode_on = active
            self._safe_act.blockSignals(True)
            self._safe_act.setChecked(active)
            self._safe_act.blockSignals(False)
            self._refresh_safe_mode_chrome()
            if active:
                if self._safe_since is None:
                    self._safe_since = time.monotonic()
                self._bump_idle_timer()
                self.statusBar().showMessage(
                    "Safe Mode ON — disconnect will unroll changes", 5000
                )
            else:
                self._idle_timer.stop()
                self._idle_notified = False
                self._safe_since = None
                self._end_input_guard()
                self._hazard_bar.set_alarm(False)
                self.statusBar().showMessage("Safe Mode off", 3000)

        def err(msg: str) -> None:
            self._safe_act.blockSignals(True)
            self._safe_act.setChecked(self._safe_mode_on)
            self._safe_act.blockSignals(False)
            QMessageBox.warning(
                self,
                "Safe Mode",
                f"{msg}\n\nRequires firmware 7.18+ with /safe-mode support.",
            )
            self.statusBar().showMessage(f"Safe Mode failed: {msg}", 5000)

        worker.finished_ok.connect(ok)
        worker.finished_err.connect(err)
        worker.start()

    def _refresh_safe_mode_chrome(self) -> None:
        """Dress (or undress) the whole window for Safe Mode.

        Called from everywhere Safe Mode can change -- the toggle's worker, a
        theme change, the status bar being built -- so the banner, the MDI
        floor, the toolbar button and the title can never disagree about
        whether this session is armed.
        """
        on = self._safe_mode_on
        desktop = style.active_theme().mdi_bg

        self._hazard_bar.set_text(self._hazard_text())
        self._hazard_bar.setVisible(on)
        if not on:
            self._hazard_bar.set_alarm(False)

        # setBackground beats the stylesheet's QMdiArea rule either way, so the
        # off case has to put the theme's own desktop colour back by hand.
        self.mdi.setBackground(
            hazard.mdi_brush(desktop) if on else QBrush(QColor(desktop))
        )
        self.mdi.viewport().update()

        if self._safe_btn is not None:
            self._safe_btn.setStyleSheet(hazard.button_qss() if on else "")

        if on:
            title = self.session.title()
            if "SAFE" not in title:
                self.setWindowTitle(f"{title} [SAFE]")
        else:
            self.setWindowTitle(self.session.title())

    def _set_theme(self, theme_id: str) -> None:
        from PySide6.QtWidgets import QApplication

        from .. import style as style_mod

        theme = style_mod.apply_theme(QApplication.instance(), theme_id, persist=True)
        for act in getattr(self, "_theme_actions", []):
            act.setChecked(act.data() == theme.id)
        # The theme carries a font of its own, so "Theme default (…)" and the
        # substitution notes have to be rewritten with it.
        self._build_font_menu()
        # A skin brings its own row height, icon edge and nav width, and it is
        # also an icon change -- the symbolic set is inked with the theme's text
        # colour. Both are what _refresh_open_metrics is for. (Icon cache
        # entries are keyed on the ink, so the old theme's glyphs are still
        # there if you switch back; only a change of *edge* drops them.)
        self._refresh_open_metrics()
        self.statusBar().showMessage(f"Theme: {theme.name}", 3000)

    #: How long after a re-skin further zoom presses are gathered into one.
    _ZOOM_SETTLE_MS = 250

    def _zoom_step(self, delta: int) -> None:
        """One stop in (+1) or out (-1) along :data:`style.SCALE_STEPS`.

        A re-skin costs most of a second with a few lists open -- nearly all of
        it Qt restyling every widget under the new sheet -- and it blocks, so
        presses made meanwhile queue up behind it. Applied one by one, three
        quick Ctrl+= were three re-skins in a row. The first press still lands
        at once; any that follow within the settle window only move the target,
        and one re-skin then takes it there.
        """
        timer = getattr(self, "_zoom_timer", None)
        if timer is None:
            timer = self._zoom_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self._ZOOM_SETTLE_MS)
            timer.timeout.connect(self._zoom_settled)
        pending = getattr(self, "_zoom_target", None)
        target = style.step_scale(delta, pending if pending is not None else None)
        if timer.isActive():
            self._zoom_target = target
            self.statusBar().showMessage(f"Zoom: {round(target * 100)}%", 3000)
            timer.start()
            return
        self._zoom_target = None
        self._set_ui_scale(target)
        timer.start()

    def _zoom_settled(self) -> None:
        target, self._zoom_target = getattr(self, "_zoom_target", None), None
        if target is not None:
            self._set_ui_scale(target)
            self._zoom_timer.start()

    def _set_ui_scale(self, scale: float) -> None:
        """Re-skin the app at ``scale``, then re-measure what is already open."""
        old = style.ui_scale()
        if abs(scale - old) < 1e-6:
            return
        style.apply_theme(
            QApplication.instance(), style.base_theme().id, scale=scale
        )
        self._refresh_open_metrics()
        self._sync_zoom_menu()
        # Grow the window with the zoom (unless it is maximized), so zooming in
        # makes room rather than shrinking the view. Ratio, not the factor: one
        # step compounds on the size the window already has.
        style.scale_window(self, scale / old)
        self.statusBar().showMessage(f"Zoom: {round(scale * 100)}%", 3000)

    def _sync_zoom_menu(self) -> None:
        """Show the factor in the submenu's title; grey out the two ends.

        The percentage has nowhere else to live -- there is no zoom widget in
        the chrome -- and a Zoom In that is already at the top of the ladder
        should read as spent rather than quietly do nothing. Disabling it takes
        its shortcut out with it, which is the same answer.
        """
        scale = style.ui_scale()
        self._zoom_menu.setTitle(f"&Zoom ({round(scale * 100)}%)")
        self._zoom_in_act.setEnabled(scale < style.SCALE_STEPS[-1])
        self._zoom_out_act.setEnabled(scale > style.SCALE_STEPS[0])
        self._zoom_reset_act.setEnabled(scale != 1.0)

    def _refresh_open_metrics(self) -> None:
        """Re-measure the open windows after the theme's *sizes* moved.

        A zoom or a skin change both land here. The stylesheet reaches every
        widget by itself, so type, colours and padding follow the moment it is
        applied. What it cannot reach is anything a widget set on itself, once,
        from a style constant, at the moment it was built: a table's row
        height, a toolbar's icon edge, the nav strip's width band, the column
        widths a ``.jg`` declares in pixels. Those are asked to re-read.

        Fixed sizes deeper inside a form are left to the next time that window
        opens. A form is opened and closed constantly; the session window and
        its lists are what somebody keeps sitting in front of.
        """
        tb = self.findChild(QToolBar, "mainToolbar")
        if tb is not None and tb.iconSize() != icon_size():
            # Rasterised icons are cached per size; a new edge would only add
            # entries, but dropping the cache keeps it from growing per zoom.
            reset_icon_cache()
            reset_mark_cache()
        if tb is not None:
            tb.setIconSize(icon_size())
        if hasattr(self, "tree"):
            self.tree.refresh_metrics()
            # The window owns the nav's band, not the widget: it wants a
            # narrower strip than MenuTree's own default, so it gets the last
            # word here as it does at build time.
            self.tree.setMinimumWidth(style.TREE_WIDTH)
            self.tree.setMaximumWidth(style.scale_px(200))
        for pane in self._for_each_list_pane():
            refresh = getattr(pane, "refresh_metrics", None)
            if callable(refresh):
                refresh()
        self._reapply_icons()
        self._refresh_mode_label()
        self._refresh_safe_mode_chrome()

    def _build_font_menu(self) -> None:
        """(Re)fill View ▸ Font.

        The labels carry the substitution ("Segoe UI — not installed, drawn as
        Noto Sans"), so they go stale whenever the theme or the set of
        loadable font files changes.
        """
        from .. import fonts as fonts_mod
        from .. import style as style_mod

        menu = getattr(self, "_font_menu", None)
        if menu is None:
            return
        menu.clear()
        self._font_actions = []
        theme = style_mod.active_theme()
        pinned = fonts_mod.override()
        for choice in fonts_mod.choices(
            theme.font_family, point_size=theme.font_pt, pixel_size=theme.font_px
        ):
            act = QAction(choice.label, self)
            act.setCheckable(True)
            act.setChecked(choice.family.casefold() == pinned.casefold())
            act.setData(choice.family)
            if choice.substituted and choice.standin:
                act.setToolTip(
                    f"Not installed — {choice.standin} stands in for it"
                )
            elif choice.substituted:
                act.setToolTip(f"Not installed — Qt draws {choice.resolved} instead")
            act.triggered.connect(
                lambda checked=False, fam=choice.family: self._set_ui_font(fam)
            )
            menu.addAction(act)
            self._font_actions.append(act)
        menu.addSeparator()
        act_cmp = QAction("&Compare…", self)
        act_cmp.setToolTip("Same text in each family, side by side")
        act_cmp.triggered.connect(self._open_font_compare)
        menu.addAction(act_cmp)

    def _set_ui_font(self, family: str) -> None:
        from PySide6.QtWidgets import QApplication

        from .. import fonts as fonts_mod

        got = fonts_mod.apply(family, QApplication.instance())
        self._build_font_menu()
        self._refresh_mode_label()
        if not family:
            self.statusBar().showMessage(f"Font: theme default ({got})", 3000)
        elif got.casefold() != family.casefold():
            self.statusBar().showMessage(
                f"Font: {family} is not installed — drawn as {got}", 6000
            )
        else:
            self.statusBar().showMessage(f"Font: {got}", 3000)

    def _open_font_compare(self) -> None:
        from .font_compare import FontCompareDialog

        dlg = FontCompareDialog(self)
        dlg.fontChosen.connect(self._on_font_chosen)
        dlg.exec()

    def _on_font_chosen(self, family: str) -> None:
        self._build_font_menu()
        self._refresh_mode_label()

    def _history_step(self, *, undo: bool) -> None:
        """Winbox's Undo / Redo: say what is about to move, then move it."""
        verb = "Undo" if undo else "Redo"
        if self.client is None:
            return
        try:
            next_undo, next_redo = self.client.history_next()
            target = next_undo if undo else next_redo
            if target is None:
                self.statusBar().showMessage(f"Nothing to {verb.lower()}", 4000)
                return
            (self.client.history_undo if undo else self.client.history_redo)()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, verb, f"{verb} failed:\n{e}")
            return
        done = "Undid" if undo else "Redid"
        self.statusBar().showMessage(f"{done}: {target}", 8000)
        applog.note(f"{done.lower()} {target}")

    def _nav_roots(self) -> list:
        """The nav tree's top-level payloads, for the menu search."""
        tree = getattr(self, "tree", None)
        if tree is None:
            return []
        return [
            tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(tree.topLevelItemCount())
        ]

    def _nav_breadcrumbs(self, path) -> list[list[str]]:
        """Every nav route to the list at ``path``, as titles from the top.

        What Command Line… makes its CLI path candidates from: the .jg names
        none, and IP ▸ Firewall ▸ Filter Rules is most of /ip firewall filter.
        """
        from .menu_search import menu_entries

        want = tuple(path or ())
        out = []
        for crumbs, target in menu_entries(self._nav_roots()):
            got = target.get("active_path") or target.get("path")
            if got is not None and tuple(got) == want:
                out.append(list(crumbs))
        return out

    def _search_from_tree(self, text: str) -> None:
        search = getattr(self, "_menu_search", None)
        if search is not None:
            search.start(text)

    def _next_window(self) -> None:
        self._cycle_window(1)

    def _prev_window(self) -> None:
        self._cycle_window(-1)

    def _cycle_window(self, step: int) -> None:
        """Ctrl+Tab / F6 walk the windows in the order they were opened.

        Not QMdiArea's own next/previous: those follow the area's activation
        order, which is history now (so closing a form returns to the window it
        came from), and history cycling just toggles between the last two.
        """
        subs = [s for s in self.mdi.subWindowList(QMdiArea.WindowOrder.CreationOrder)
                if s.isVisible()]
        if not subs:
            return
        active = self.mdi.activeSubWindow()
        i = subs.index(active) if active in subs else -step
        self.mdi.setActiveSubWindow(subs[(i + step) % len(subs)])
        self._say_active_window()

    def _say_active_window(self) -> None:
        """Say which window the keyboard just landed in.

        Activating an MDI child moves focus inside it, and what a reader makes
        of that is the widget it landed on -- a table, a text box -- with no
        mention of the window that appeared around it. The title is the part
        that answers "where am I now".
        """
        sub = self.mdi.activeSubWindow()
        if sub is not None:
            a11y.announce(self, sub.windowTitle())

    def _rebuild_window_list(self) -> None:
        """Put every open window in the menu, with the active one marked.

        Rebuilt on each open rather than kept in step with the MDI area:
        windows come and go from a dozen places (a row opening its form, a
        menu, Close All, a device push closing a stale one), and a list built
        the moment it is read cannot be out of date.
        """
        menu = self._window_menu
        for act in menu.actions()[self._window_list_from:]:
            menu.removeAction(act)
        # Opening order: the menu's numbers stay put while focus moves around.
        subs = self.mdi.subWindowList(QMdiArea.WindowOrder.CreationOrder)
        if not subs:
            act = QAction("(no windows open)", self)
            act.setEnabled(False)
            menu.addAction(act)
            return
        active = self.mdi.activeSubWindow()
        for i, sub in enumerate(subs, start=1):
            title = sub.windowTitle() or "(untitled)"
            # &1..&9 give the first nine a mnemonic inside the open menu; past
            # that the arrow keys are the way there, as in any long menu.
            label = f"&{i} {title}" if i < 10 else f"{i} {title}"
            act = QAction(label, self)
            act.setCheckable(True)
            act.setChecked(sub is active)
            act.triggered.connect(lambda _c=False, w=sub: self._raise_window(w))
            menu.addAction(act)

    def _raise_window(self, sub) -> None:
        self.mdi.setActiveSubWindow(sub)
        sub.raise_()
        self._say_active_window()

    def _cascade(self) -> None:
        self.mdi.cascadeSubWindows()

    def _tile(self) -> None:
        self.mdi.tileSubWindows()

    def _close_all_windows(self) -> None:
        self.mdi.closeAllSubWindows()
        self._open.clear()

    def _open_new_session(self) -> None:
        """Open the connect dialog and, on success, a new device window."""
        from .connect_dialog import ConnectDialog

        _OPEN_WINDOWS[:] = [w for w in _OPEN_WINDOWS if w.isVisible()]
        # Parented, so it comes up over this window instead of in the middle of
        # whichever screen Qt picks for an orphan dialog. The parent then owns
        # it for as long as this window lives, which is what deleteLater is
        # for -- the answers are read out of it first.
        dlg = ConnectDialog(self)
        ok = (
            dlg.exec() == ConnectDialog.DialogCode.Accepted
            and dlg.session is not None
            and dlg.client is not None
        )
        session, client = dlg.session, dlg.client
        dlg.deleteLater()
        if not ok:
            return
        win = MainWindow(session, client=client)
        _OPEN_WINDOWS.append(win)
        win.show()

    def _reload_nav(self) -> None:
        """(Re)build the left nav — filter to the device's plugins, prepend
        New x3, apply submenu order."""
        from x3.proto import schema as sch

        nav = list(self.client.nav_tree())
        # Hide menus for plugins this device does not have (e.g. legacy Wireless
        # on a wifi-only or wireless-less box), so clicking them can't hang on a
        # dead handler.
        plugins = getattr(self.client, "device_plugins", None)
        if plugins:
            nav = sch.filter_nav_by_plugins(nav, plugins)
        # Menus the .jg guards on hardware this device does not have: Wireless
        # ▸ W60G only exists on a 60GHz board, System ▸ Console only on x86.
        info = getattr(self.client, "info", None)
        board = getattr(info, "board", "") if info is not None else ""
        if board:
            nav = sch.filter_nav_by_pred(
                nav, board=board,
                arch=getattr(info, "architecture", "") or None,
                # Capability bits from the session handler: VETH is syscap 2,
                # and the switch-chip menus are gated the same way.
                syscaps=self._syscaps(),
            )
        # Bottom of the tree, under everything the device offers: it is an
        # app action, not one of this device's menus.
        nav.append(NavNode(name="New x3", title="New x3", kind="new_session"))
        self.tree.load_nav(nav)
        self._warn_on_schema_mismatch()

    def _warn_on_schema_mismatch(self) -> None:
        """Say so when the menus came from another firmware's plugin set.

        A cache directory is picked by version when the device's plugin index
        did not arrive, and one firmware version can name several cached sets -- so
        the tree can be a different build's, which reads as menus that simply
        do not exist (Routing ▸ GMP, on a 7.21.1 box that matched a stub). The
        device knows its own build id, so the disagreement is checkable.
        """
        pair = getattr(self.client, "schema_build_mismatch", None)
        if not pair:
            return
        got, want = pair
        self.statusBar().showMessage(
            f"Schema is build {got}, device is build {want} — "
            "menus may not match this firmware",
            15000,
        )

    def _window_title(self, path, title: str, *, name: str | None = None) -> str:
        """The title Winbox puts on this menu's window.

        The tree label and the window title are not always the same thing: the
        .jg calls one entry "Static" in the menu and "DNS Static" on the window
        it opens, and says so with ``fulltitle``.
        """
        if self.client is None:
            return title
        try:
            menu = self.client.menu_for(tuple(path), name=name)
        except Exception:  # noqa: BLE001
            return title
        full = getattr(menu, "full_title", None) if menu is not None else None
        return str(full) if full else title

    def _syscaps(self):
        """The device's capability bits, or None when it did not say."""
        getter = getattr(self.client, "system_caps", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None

    def _on_submenu_order_toggled(self, checked: bool) -> None:
        save_submenu_order("alphabetical" if checked else "jg")
        self._reload_nav()

    def _on_auto_most_used_toggled(self, checked: bool) -> None:
        save_most_used_auto(bool(checked))
        # The flyout reads this setting when it next opens; nothing to rebuild.

    def _set_idle_secs(self, secs: int) -> None:
        """Pick the idle interval, and re-arm the session already running.

        Saved as well as applied: a setting that only takes hold on the next
        connection reads as a setting that did nothing.
        """
        self._idle_secs = max(0, int(secs))
        save_safe_mode_idle_secs(self._idle_secs)
        for act in self._idle_actions:
            act.setChecked(act.data() == self._idle_secs)
        self._bump_idle_timer()
        self.statusBar().showMessage(
            "Safe Mode idle alarm off"
            if not self._idle_secs
            else f"Safe Mode idle alarm after {self._idle_text(self._idle_secs)}",
            3000,
        )

    def _on_live_updates_toggled(self, checked: bool) -> None:
        # Sets the default for newly-opened list windows; open windows keep
        # their own Live toggle. The toolbar's resource poll is this window's
        # own idle traffic, so it follows the setting immediately.
        save_live_updates(bool(checked))
        if checked:
            self._stats_errors = 0
            self._start_stats_poll()
        else:
            self._stop_stats_poll()

    def _set_list_refresh(self, secs: float | None) -> None:
        """Save the poll cadence and hand it to every list already open.

        A setting that only reaches windows opened afterwards reads as a
        setting that did not work.
        """
        from .list_pane import ListPane

        self._list_refresh_secs = secs
        save_list_refresh_secs(secs)
        for act in self._list_refresh_actions:
            act.setChecked(act.data() == secs)
        for sub in self.mdi.subWindowList():
            for pane in sub.findChildren(ListPane):
                pane.apply_refresh_setting()
        label = next(
            (t for s, t in LIST_REFRESH_CHOICES if s == secs), str(secs)
        )
        self.statusBar().showMessage(f"List auto refresh: {label.lower()}", 3000)

    def _set_language(self, code: str, picked: QAction) -> None:
        """Save the language, and say when it applies: every window would have
        to be rebuilt to change it now, so on the next connection page."""
        save_language(code)
        i18n.picked = code
        for act in picked.parent().actions():   # the Language menu
            act.setChecked(act is picked)
        target = i18n.resolve(code)
        if target == i18n.active():
            self.statusBar().showMessage(tr("Language saved."), 4000)
            return
        QMessageBox.information(
            self, "Language",
            f"{i18n.available().get(target, target)}: "
            + tr("x3 switches to this language when you return to the "
                 "connection page (File ▸ Disconnect)."),
        )

    def _set_byte_units(self, mode: str) -> None:
        """Save the choice and restate every value already on screen.

        A setting that only reaches windows opened afterwards reads as a
        setting that did not work -- and this one costs no device traffic to
        apply: the objects are already here, only their rendering changed.
        """
        from ..settings import apply_byte_units
        from .list_pane import ListPane
        from .property_window import PropertyWindow

        self._byte_units = mode
        save_byte_units(mode)
        apply_byte_units(mode)
        for act in self._byte_unit_actions:
            act.setChecked(act.data() == mode)
        for sub in self.mdi.subWindowList():
            inner = sub.widget() if hasattr(sub, "widget") else None
            for win in ([inner] if isinstance(inner, PropertyWindow) else []):
                win.rerender_values()
            for pane in sub.findChildren(ListPane):
                pane.reload(quiet=True)

    def _toggle_key_hints_panel(self) -> None:
        """F1 / View ▸ Keyboard Keys: pin the key panel open, or close it."""
        hints = getattr(self, "_key_hints", None)
        if hints is not None:
            hints.toggle_pinned()

    def _on_key_hints_toggled(self, checked: bool) -> None:
        """View ▸ Key Hints On Ctrl. F1 keeps working either way."""
        save_key_hints(bool(checked))
        hints = getattr(self, "_key_hints", None)
        if hints is not None:
            hints.set_enabled(bool(checked))

    def _on_status_line_toggled(self, checked: bool) -> None:
        """View ▸ Form Status Line, on every form already open as well.

        A setting that only takes effect on windows opened afterwards reads as
        a setting that did not work.
        """
        save_form_status_line(bool(checked))
        for sub in self.mdi.subWindowList():
            setter = getattr(sub, "set_status_line", None)
            if setter is None:
                continue
            try:
                setter(bool(checked))
            except RuntimeError:
                pass
        self.statusBar().showMessage(
            "Form status line shown" if checked else "Form status line hidden",
            3000,
        )

    def _on_dropdown_marks_toggled(self, checked: bool) -> None:
        """View ▸ Dropdown Status Marks.

        Forms already open keep the dropdowns they built -- the marks are hung
        on the items as a box is filled, and re-walking every open form's
        editors to strip them would be a lot of reach for a preference. The
        message says so rather than leaving it looking broken.
        """
        save_dropdown_marks(bool(checked))
        self.statusBar().showMessage(
            "Dropdown status marks on — in forms opened from now on"
            if checked else
            "Dropdown status marks off — in forms opened from now on",
            3000,
        )

    def _on_titlebar_tabs_toggled(self, checked: bool) -> None:
        """Move every open window's tabs, not just the next one's.

        A setting that only takes effect on windows opened afterwards reads as
        a setting that did not work.
        """
        save_titlebar_tabs(bool(checked))
        moved = 0
        for sub in self.mdi.subWindowList():
            setter = getattr(sub, "set_titlebar_tabs", None)
            if setter is None:
                continue
            try:
                was = sub.titlebar_tabs_enabled()
                setter(bool(checked))
                moved += int(sub.titlebar_tabs_enabled() != was)
            except RuntimeError:      # closed under us
                continue
        where = "title bar" if checked else "tab strip"
        self.statusBar().showMessage(
            f"Tabs in the {where}" + (f" — {moved} window(s) updated" if moved else ""),
            4000,
        )

    def _set_icon_source(self, value: str) -> None:
        save_icon_source(value)
        # Read it back rather than trusting the click: an unknown value falls
        # back to the colour set, and the checkmark has to say which set is
        # actually being drawn.
        got = load_icon_source()
        for act in getattr(self, "_icon_source_actions", []):
            act.setChecked(act.data() == got)
        if hasattr(self, "_danger_ink_act"):
            self._danger_ink_act.setEnabled(got == "mono")
        reset_icon_cache()
        reset_mark_cache()
        self._reapply_icons()
        self.statusBar().showMessage(f"Icons: {icon_source_label(got)}", 3000)

    def _set_danger_ink(self, on: bool) -> None:
        save_danger_ink(on)
        reset_icon_cache()
        self._reapply_icons()

    def _reapply_icons(self) -> None:
        term = next(
            (a for a in self.findChildren(QAction) if a.text() == tr("Terminal")),
            None,
        )
        if term is not None:
            term.setIcon(icon_for_kind("terminal"))
        if hasattr(self, "tree"):
            self.tree.refresh_icons()
        for pane in self._for_each_list_pane():
            if hasattr(pane, "reapply_icons"):
                pane.reapply_icons()

    def open_menu(self, payload: object) -> None:
        """Open what a nav entry points at, and count it if the window sticks.

        The count is not taken here: :meth:`_register` takes it once a window
        actually exists, and only after :data:`USAGE_DWELL_MS`. Re-clicking a
        menu that is already open just raises its window and counts nothing.
        """
        assert isinstance(payload, dict)
        countable = payload.get("kind") in (
            "map", "item", "page_group", "query", "action", "doit",
        )
        # Saved and restored, not just cleared: a page-group tab can call back
        # into open_menu, and the inner call must not disarm the outer one.
        previous = self._pending_open_id
        self._pending_open_id = payload.get("id") if countable else None
        try:
            self._dispatch_menu(payload)
        finally:
            self._pending_open_id = previous

    def _dispatch_menu(self, payload: dict) -> None:
        kind = payload.get("kind")
        title = payload.get("title") or "Menu"
        path = payload.get("path")
        if isinstance(path, list):
            path = tuple(path)

        # "New x3" opens a fresh connection in its own window.
        if kind == "new_session":
            self._open_new_session()
            return

        # Page groups handle their own focus + active tab
        if kind == "page_group":
            self._open_page_group(payload)
            return

        key = payload.get("id") or payload.get("title") or str(payload.get("path"))
        if self._focus_existing(key):
            return

        if kind == "terminal":
            # Terminals are deliberately not deduplicated -- several shells at
            # once is normal. Every other menu reuses its window.
            self._terminal_seq = getattr(self, "_terminal_seq", 0) + 1
            # Each terminal opens its own console on its own SYS_FROM, so
            # several at once do not steal each other's output.
            win = TerminalWindow(self.client, seq=self._terminal_seq)
            self._show_child(win)
            self._register(("terminal", self._terminal_seq), win)
            return

        # /system package update needs live polling and issues commands rather
        # than queueing edits, so it gets its own window ahead of the generic
        # item form. Checked before the page_group branch because the schema
        # stamps the Packages group onto this node too.
        if path is not None and tuple(path) == PKG_UPDATE_PATH:
            if self._open_update_window(tuple(path), title, key):
                return

        if path is not None and kind in ("map", "item"):
            pg = payload.get("page_group")
            if pg and len(pg.get("pages") or []) >= 2:
                self._open_page_group({
                    "kind": "page_group",
                    "title": pg["title"],
                    "id": pg["id"],
                    "pages": pg["pages"],
                    "active_path": path,
                    # A dozen tabs can share one path; without the type (or the
                    # family, for a base tab) the window always opened on the
                    # first of them however the nav got there.
                    "active_type_value": payload.get("type_value"),
                    "active_family": payload.get("family"),
                })
                return
            if kind == "item":
                # A form opened on its own keeps the buttons the schema puts
                # on it (Tools ▸ Email ▸ Send Email).
                self._open_item_dialog(
                    tuple(path), title, buttons=payload.get("actions") or None,
                    name=payload.get("name"),
                )
                return
            # map → list window with live fetcher
            client = self.client
            p = tuple(path)
            # Interface-type overlays all share path (20,0); the type value is
            # what makes the Ethernet window show only ethernet interfaces.
            tv = payload.get("type_value")
            tv = tv if isinstance(tv, int) else None
            fam = payload.get("family")
            fam = fam if isinstance(fam, str) and fam else None
            # Which menu at this path, for the paths that host several with no
            # type between them (Web Proxy ▸ Connections vs the Settings form,
            # both (96,1)). See Schema.record.
            nm = payload.get("name")
            nm = nm if isinstance(nm, str) and nm else None

            def fetch():
                return client.table_rows(p, type_value=tv, family=fam, name=nm)

            icon_idx = payload.get("icon")
            if not isinstance(icon_idx, int):
                icon_idx = None
            # `fulltitle`: the window's own title where the .jg gives one
            # ("DNS Static" for the tree entry called "Static").
            title = self._window_title(p, title, name=nm)
            win = ListWindow(
                title,
                readonly=self.edit.readonly,
                live_fetcher=fetch,
                path=p,
                type_value=tv,
                family=fam,
                menu_name=nm,
                on_open_properties=self.open_property_window,
                on_context_action=self.run_context_action,
                # A menu opened on its own still has the buttons the schema
                # declares under it (LTE eSIM ▸ Provision).
                on_open_action=self.open_menu,
                actions=payload.get("actions") or [],
                edit_session=self.edit,
                icon_index=icon_idx,
                show_passwords=self._show_passwords,
                inline_comments=self._inline_comments,
            )
            self._wire_pane(win.pane)
            self._show_child(win)
            self._register(key, win)
            self.statusBar().showMessage(f"Opened {title} {p}", 3000)
            return

        # A `termopen` dialog (Tools ▸ Telnet) has no handler path: it collects
        # a type, an address and a port, and the session is opened with them.
        if kind == "termopen":
            self._open_termopen(payload, title)
            return

        if kind in ("query", "action", "doit") and path is not None:
            self._open_query_window(payload, title, kind, tuple(path))
            return
        QMessageBox.information(self, title, f"No handler for {kind!r} {path!r}")

    def _open_termopen(self, payload: dict, title: str) -> None:
        """Winbox's Telnet / SSH / MAC Telnet dialog, and what OK does.

        The .jg gives the whole form -- Type (telnet / ssh / mac telnet), the
        address or MAC, port, user -- and no handler to send it to: Winbox
        connects from the PC itself. This app's one console is the router's, so
        the session is opened *from the router* with the matching the router
        command, which is the same destination by a different route (and the
        only route to a host only the router can see). The dialog says so.
        """
        schema = getattr(self.client, "schema", None)
        menu = (getattr(schema, "dialogs", None) or {}).get(title)
        if menu is None:
            self.statusBar().showMessage(f"{title}: no dialog declared", 4000)
            return
        if self.client is None:
            QMessageBox.information(self, title, "Not connected.")
            return

        def accept(values: dict) -> None:
            command = _termopen_command(values)
            if not command:
                raise ValueError(
                    "Fill in the address (or MAC address) to connect to."
                )
            self._terminal_seq = getattr(self, "_terminal_seq", 0) + 1
            win = TerminalWindow(self.client, seq=self._terminal_seq)
            self._show_child(win)
            self._register(("terminal", self._terminal_seq), win)
            win.run_line(command)
            self.statusBar().showMessage(f"{title}: {command}", 4000)

        CommandDialog(
            title, menu, client=self.client,
            on_accept=accept, prefill=payload.get("prefill"),
            note="Opened from the router's console, not from this computer.",
            parent=self,
        ).exec()

    def _open_query_window(
        self, payload: dict, title: str, kind: str, path: tuple[int, ...]
    ) -> None:
        """Open a tool window (Ping, Traceroute, …).

        Deliberately not deduplicated, like terminals: Winbox's own Ping window
        has a New Window button, and the device tells concurrent runs apart by
        session id, so several at once are fine.
        """
        if self.client is None or self.client.schema is None:
            QMessageBox.information(self, title, "Not connected.")
            return
        menu = self.client.schema.command_menu(path, kind, payload.get("title"))
        if menu is None:
            menu = self.client.schema.command_menu(path, kind)
        if menu is None:
            QMessageBox.information(
                self, title, f"No {kind} menu declared at {path}."
            )
            return
        if menu.start_command is None:
            # A doit fires once and reports nothing, so no run window applies:
            # it takes its arguments in a modal and then runs. The schema is
            # what supplies the fields — System ▸ Password declares Old / New /
            # Confirm, so that is what the form asks for.
            if not self.edit.live:
                QMessageBox.information(
                    self, title,
                    f"{title} writes to the device.\n\n"
                    "Switch to Live mode to run it.",
                )
                return
            CommandDialog(title, menu, client=self.client, parent=self).exec()
            return
        self._query_seq = getattr(self, "_query_seq", 0) + 1
        win = QueryWindow(
            title, menu, client=self.client, seq=self._query_seq,
            icon_index=payload.get("icon") if isinstance(payload.get("icon"), int) else None,
            # Set when a .jg row action opened this (Neighbors ▸ Tools ▸ Ping).
            prefill=payload.get("prefill"),
            autostart=bool(payload.get("autostart")),
            # A fresh window for the same tool -- not the prefill or autostart
            # a row action brought this one.
            on_new_window=lambda: self._open_query_window(
                {k: v for k, v in payload.items()
                 if k not in ("prefill", "autostart")},
                title, kind, path,
            ),
        )
        self._show_child(win)
        self._register(("query", self._query_seq), win)
        # Into Ping To, so open / 8.8.8.8 / Enter is the whole gesture.
        self._focus_window(win)
        self.statusBar().showMessage(f"Opened {title} {path}", 3000)

    @staticmethod
    def _page_first(page: dict) -> int | None:
        path = page.get("path")
        if isinstance(path, (list, tuple)) and path:
            return path[0]
        return None

    def _is_quickset(self, pages: list[dict]) -> bool:
        """QuickSet is the one page group whose maps all live under [120,*]."""
        real = [p for p in pages if self._page_first(p) is not None]
        return bool(real) and all(self._page_first(p) == 120 for p in real)

    @staticmethod
    def _mode_capability(title: str) -> str:
        """The hardware a QuickSet mode needs: ether | wireless | lte | w60g."""
        t = (title or "").strip().lower()
        if "lte" in t:
            return "lte"
        if "w60g" in t or "60g" in t:
            return "w60g"
        if t in ("ethernet", "port mapping"):
            return "ether"
        return "wireless"

    def _filter_quickset_modes(self, pages: list[dict]) -> list[dict]:
        """De-dupe modes by title and drop ones this device cannot run.

        Winbox shows only the modes a box supports; we mirror that from the
        interface list. If the capability probe fails (empty set) we hide
        nothing, so a probe glitch never strips a mode the user needs.
        """
        try:
            caps = self.client.quickset_capabilities() if self.client else set()
        except Exception:  # noqa: BLE001
            caps = set()
        out: list[dict] = []
        seen: set[str] = set()
        for p in pages:
            title = (p.get("title") or "").strip()
            if not title or title == "?":
                continue
            low = title.lower()
            if low in seen:
                continue
            need = self._mode_capability(title)
            if caps and need not in caps:
                continue
            seen.add(low)
            out.append(p)
        return out

    def _open_quickset(self, payload: dict) -> None:
        """QuickSet: one window, a mode dropdown, one mode loaded at a time."""
        title = payload.get("title") or "Quick Set"
        key = payload.get("id") or f"group:{title}"
        active = payload.get("active_path")
        if isinstance(active, list):
            active = tuple(active)

        if self._focus_existing(key):
            win = self._open.get(key)
            if isinstance(win, QuickSetWindow) and active is not None:
                win.select_path(tuple(active))
            return

        if self.client is None:
            QMessageBox.information(self, title, "Not connected.")
            return

        client = self.client
        modes = self._filter_quickset_modes(payload.get("pages") or [])
        if not modes:  # fail-safe: never open an empty dropdown
            modes = payload.get("pages") or []

        def make_fetcher(
            path: tuple[int, ...],
            type_value: int | None = None,
            family: str | None = None,
            menu_name: str | None = None,
        ):
            def fetch():
                return client.table_rows(
                    path, type_value=type_value, family=family, name=menu_name,
                )
            return fetch

        icon_idx = payload.get("icon")
        if not isinstance(icon_idx, int):
            icon_idx = None
        win = QuickSetWindow(
            title,
            modes,
            readonly=self.edit.readonly,
            make_fetcher=make_fetcher,
            on_open_properties=self.open_property_window,
            on_context_action=self.run_context_action,
            on_pane_created=self._wire_pane,
            edit_session=self.edit,
            active_path=tuple(active) if active is not None else None,
            icon_index=icon_idx,
            show_passwords=self._show_passwords,
            inline_comments=self._inline_comments,
        )
        self._show_child(win)
        self._register(key, win)
        self.statusBar().showMessage(f"Opened {title} ({len(modes)} mode(s))", 3000)

    @staticmethod
    def _form_group_title(group_title: str, page_title: str) -> str:
        """The title Winbox puts on the form a menu folder opens.

        The .jg names the two halves apart -- the folder is "DNS", the item
        under it is "Settings" -- and v3 shows them joined ("DNS Settings").
        Neither half is dropped unless one already spells the other: System ▸
        Resources is "Resources", not "Resources Resources", and IP ▸ Web Proxy
        keeps the item's own longer "Web Proxy Settings".
        """
        group = (group_title or "").strip()
        page = (page_title or "").strip()
        if not group or not page:
            return page or group
        if group.lower() in page.lower() or page.lower() in group.lower():
            return page if len(page) >= len(group) else group
        return f"{group} {page}"

    @staticmethod
    def _form_group_buttons(pages: list[dict]) -> list[dict]:
        """The buttons on a form a menu folder opens: its every later sibling.

        The window is ``pages[0]``; the rest are its buttons in .jg order, each
        carrying the menu it opens (a list, another form, a command) and that
        menu's own buttons for when it gets a window. The commands the folder
        declared among them ride on the window page's action list (RouterBOARD ▸
        USB Power Reset), and a leading `item` is both the window and one of
        those declarations -- so anything already named, the window included, is
        dropped rather than shown twice.
        """
        if not pages:
            return []
        keys = (
            "kind", "path", "title", "name", "id", "type_value", "family",
            "actions",
        )
        seen = {pages[0].get("id")}
        out: list[dict] = []
        for page in list(pages[1:]) + list(pages[0].get("actions") or []):
            if page.get("id") in seen:
                continue
            seen.add(page.get("id"))
            out.append({k: page.get(k) for k in keys if page.get(k) is not None})
        return out

    def _open_page_group(self, payload: dict) -> None:
        """Open the window a menu folder's children make.

        Which window that is comes out of the .jg's own order: the folder's
        first child is the window and the rest are reached from it, so a group
        led by an `item` opens that form with its siblings as buttons (System ▸
        Resources ▸ Hardware / CPU / IRQ), and one led by a `map` opens the
        tabbed list window below. See ``menu_tree._page_group``.
        """
        title = payload.get("title") or "Menu"
        key = payload.get("id") or f"group:{title}"
        pages = payload.get("pages") or []
        if self._is_quickset(pages):
            self._open_quickset(payload)
            return
        primary = pages[0] if pages else None
        if (
            primary is not None
            and primary.get("kind") == "item"
            and primary.get("path") is not None
        ):
            if self.client is None:
                QMessageBox.information(self, title, "Not connected.")
                return
            self._open_item_dialog(
                tuple(primary["path"]),
                self._form_group_title(title, primary.get("title") or title),
                buttons=self._form_group_buttons(pages),
                name=primary.get("name"),
            )
            return
        active = payload.get("active_path")
        if isinstance(active, list):
            active = tuple(active)
        active_tv = payload.get("active_type_value")
        active_tv = active_tv if isinstance(active_tv, int) else None
        active_fam = payload.get("active_family")
        active_fam = active_fam if isinstance(active_fam, str) and active_fam else None

        if self._focus_existing(key):
            win = self._open.get(key)
            if isinstance(win, MultiPageListWindow) and active is not None:
                win.select_path(tuple(active), active_tv, active_fam)
            return

        if self.client is None:
            QMessageBox.information(self, title, "Not connected.")
            return

        client = self.client

        def make_fetcher(
            path: tuple[int, ...],
            type_value: int | None = None,
            family: str | None = None,
            menu_name: str | None = None,
        ):
            def fetch():
                return client.table_rows(
                    path, type_value=type_value, family=family, name=menu_name,
                )
            return fetch

        icon_idx = payload.get("icon")
        if not isinstance(icon_idx, int):
            icon_idx = None
        win = MultiPageListWindow(
            title,
            pages,
            readonly=self.edit.readonly,
            make_fetcher=make_fetcher,
            on_open_properties=self.open_property_window,
            on_context_action=self.run_context_action,
            # Tabs build their pane the first time they are looked at, so the
            # queue-count signal has to be wired as each is born -- a loop over
            # panes() after construction would only ever catch the first.
            on_pane_created=self._wire_pane,
            on_open_single=self._open_single_tab,
            # Every toolbar button on a tab is a menu, so opening one is the
            # same dispatch a nav click makes.
            on_open_action=self.open_menu,
            edit_session=self.edit,
            active_path=tuple(active) if active is not None else None,
            active_type_value=active_tv,
            active_family=active_fam,
            icon_index=icon_idx,
            show_passwords=self._show_passwords,
            inline_comments=self._inline_comments,
        )
        self._show_child(win)
        self._register(key, win)
        n = win.tabs.count()
        self.statusBar().showMessage(f"Opened {title} ({n} page(s))", 3000)

    # --- .jg row actions (type:contextmenu) ----------------------------

    def run_context_action(self, action: dict, row, source_menu) -> None:
        """Carry out a right-click row action from the .jg.

        `{name:'Ping',open:'Ping',tab:'Ping',autostart:1,
          link:[{dst:'Ping To',src:'Address'}]}` means: open the menu titled
        Ping, put this row's Address in its Ping To box, and start it. That is
        the whole of Winbox's right-click-to-a-tool behaviour, and it is all
        declared -- nothing about Ping or Torch is hardcoded here.
        """
        # A hyperlink row names its target by handler path rather than by
        # title (a route's Immediate Gateway is a row of (44,16)).
        path = action.get("path")
        if isinstance(path, (list, tuple)) and path:
            self._dispatch_menu({
                "kind": "map",
                "title": str(action.get("name") or "Menu"),
                "path": tuple(path),
            })
            return
        target = self._nav_node_for(action.get("tab")) or self._nav_node_for(
            action.get("open")
        )
        if target is None:
            self.statusBar().showMessage(
                f"{action.get('name')}: no menu named "
                f"{action.get('open') or action.get('tab')!r}", 4000,
            )
            return
        payload = dict(_payload_from_nav(target))
        prefill = self._context_prefill(action, row, source_menu)
        if prefill:
            payload["prefill"] = prefill
        if action.get("autostart"):
            payload["autostart"] = True
        self.open_menu(payload)

    def _nav_node_for(self, title: str | None):
        """The nav entry with this title, anywhere in the tree.

        A context action names its target by title (`open:'Ping'`,
        `tab:'Access List'`), which is the only handle the .jg gives.
        """
        if not isinstance(title, str) or not title.strip():
            return None
        want = title.strip()
        if self.client is None:
            return None
        try:
            roots = list(self.client.nav_tree())
        except Exception:  # noqa: BLE001
            return None
        stack = list(roots)
        fallback = None
        while stack:
            node = stack.pop(0)
            if (getattr(node, "title", "") or "").strip() == want:
                # A leaf with a handler path beats a folder of the same name:
                # 'Wireless' is both a folder and a menu, and only one of them
                # can be opened. (`openable` is narrower -- it means map/item,
                # and half these targets are query tools.)
                if getattr(node, "path", None) is not None:
                    return node
                fallback = fallback or node
            stack.extend(list(getattr(node, "children", None) or []))
        return fallback

    def _context_prefill(self, action: dict, row, source_menu) -> dict:
        """Target field name → what to put in it, per the action's `link:`.

        Both forms the .jg uses: copy this row's field (`src`) or write a
        literal (`conv:'put'` with `val`). Raw and rendered are both carried --
        an interface goes across as the id its dropdown holds, an address as
        the text its box shows.
        """
        out: dict[str, dict] = {}
        by_name: dict[str, tuple] = {}
        for code, name, rendered, _ok in getattr(row, "fields", None) or []:
            by_name.setdefault(str(name).strip(), (code, rendered))
        for link in action.get("link") or []:
            dst = str(link.get("dst") or "").strip()
            if not dst:
                continue
            if link.get("src") is None:
                if link.get("val") is not None:
                    out[dst] = {"raw": link["val"], "text": str(link["val"])}
                continue
            src = str(link["src"]).strip()
            hit = by_name.get(src)
            if hit is None:
                continue
            code, rendered = hit
            raw = row.raw_value(code) if hasattr(row, "raw_value") else None
            out[dst] = {"raw": raw, "text": rendered}
        return out

    def _open_single_tab(
        self,
        path: tuple[int, ...],
        type_value: int | None = None,
        title: str = "",
        family: str | None = None,
        actions: list[dict] | None = None,
    ) -> None:
        """Open one tab of a page group as its own list window (from the
        title-bar tab menu's per-tab 'open in its own window')."""
        if self.client is None:
            return
        client = self.client
        p = tuple(path)
        tv = type_value if isinstance(type_value, int) else None
        fam = family if isinstance(family, str) and family else None
        key = ("single", p, tv, fam)
        if self._focus_existing(key):
            return

        def fetch():
            return client.table_rows(p, type_value=tv, family=fam)

        win = ListWindow(
            title or str(p),
            readonly=self.edit.readonly,
            live_fetcher=fetch,
            path=p,
            type_value=tv,
            family=fam,
            on_open_properties=self.open_property_window,
            on_context_action=self.run_context_action,
            on_open_action=self.open_menu,
            actions=actions or [],
            edit_session=self.edit,
            show_passwords=self._show_passwords,
            inline_comments=self._inline_comments,
        )
        self._wire_pane(win.pane)
        self._show_child(win)
        self._register(key, win)
        self.statusBar().showMessage(f"Opened {title} {p}", 3000)

    def _open_check_for_updates(self) -> None:
        if self.client is None:
            QMessageBox.information(
                self,
                "Check For Updates",
                "Connect to a device first.",
            )
            return
        key = ("update", PKG_UPDATE_PATH)
        if self._focus_existing(key):
            return
        if not self._open_update_window(PKG_UPDATE_PATH, "Check For Updates", key):
            QMessageBox.information(
                self,
                "Check For Updates",
                "This device's schema does not describe /system package "
                "update (handler 24,24), so there is nothing to drive the "
                "window from.",
            )

    def _open_update_window(
        self, path: tuple[int, ...], title: str, key
    ) -> bool:
        """Open Check For Updates. False means fall back to the generic form.

        Older firmware is the reason for the fallback rather than an error: the
        menu is present on every build we have cached (6.0rc14 onward), but a
        device whose schema we could not load, or a future layout without the
        state field the buttons key on, should still get a plain read-only
        form instead of a broken window.
        """
        if self.client is None:
            return False
        try:
            menu = self.client.package_update_menu()
        except Exception:  # noqa: BLE001
            return False
        if menu is None or not (menu.form_actions or []):
            return False
        from x3.proto.device import PKG_UPDATE_STATE

        if PKG_UPDATE_STATE not in (menu.fields or {}):
            return False
        win = UpdateWindow(self.client, menu=menu, edit_session=self.edit)
        self._show_child(win)
        self._register(key, win)
        return True

    def _open_item_dialog(
        self,
        path: tuple[int, ...],
        title: str,
        buttons: list[dict] | None = None,
        *,
        name: str | None = None,
    ) -> None:
        """Open a single-object form.

        ``name`` is the .jg name of the menu the nav clicked, which is what
        picks the form on a path that hosts several: (20,17) is one settings
        object that IP Settings, IPv6 Settings, Bridge Settings, Connection
        Tracking, Discovery Settings and L3 Hw Settings each show a disjoint
        slice of, so without it every one of them opened the largest (L3 Hw).
        """
        assert self.client is not None
        try:
            # Decode the reply as the menu we are about to draw, not as
            # whichever of this path's menus the heuristic likes best.
            row = self.client.get(
                path, menu=self.client.menu_for(path, name=name)
            )
        except Exception as e:
            QMessageBox.warning(self, title, f"GET failed:\n{e}")
            return
        self.open_property_window(
            path, row, title, menu_buttons=buttons, menu_name=name,
        )

    def open_property_window(
        self,
        path,
        row,
        title: str,
        row_key: str = "",
        *,
        menu_buttons: list[dict] | None = None,
        menu_name: str | None = None,
    ) -> None:
        """Show an object's settings as an MDI child, not a separate window.

        A QDialog would float outside the multi-window area; Winbox keeps
        property forms inside it, and so do we.
        """
        key = ("prop", tuple(path), row_key)
        if self._focus_existing(key):
            return
        from x3.proto.schema import wire_type_value

        # The menu to read and decode this object against is not the bare base
        # but the base merged with the object's *type overlay*: a bridge's MAC
        # Address, ARP and DHCP Snooping -- and an ethernet's MAC Address --
        # are the overlay's fields, not the generic Interface's. Read against
        # the base and every one of them comes back as a bare <0x…> row with a
        # raw value (a MAC as 525400123456), because the base has no field of
        # that id to name or type it. The base still names the type field
        # ('type' on Interface, 'rtype' on Route); the list row we already hold
        # carries that value, so the overlay is known before the full read.
        base = (
            self.client.menu_for(tuple(path), name=menu_name)
            if self.client is not None else None
        )
        type_value = (
            wire_type_value(row, base)
            if (row is not None and base is not None) else None
        )
        values = getattr(row, "values_by_code", None) if row is not None else None
        menu = (
            self.client.menu_for(
                tuple(path), type_value, values=values, name=menu_name,
            )
            if self.client is not None else None
        )

        # List GETALL rows are sparse (DHCP Status IP/Gateway often missing).
        # Refresh the full object when we have a .id — same idea as Winbox open.
        #
        # "Unfiltered" is not "everything": a handler does not volunteer the
        # fields behind its `refreshfilter`, so sending no 0xFE000C at all left
        # the tray's link status (Ethernet 0x44C) out of the reply until the
        # Traffic tab's own masked poll happened to fetch it. Send the mask the
        # menu declares, which is what Winbox opens a row with.
        full_row = False
        if self.client is not None and row is not None:
            oid = row.object_id() if hasattr(row, "object_id") else None
            if oid is not None:
                try:
                    read_menu = menu or base
                    mask = list_read_mask(read_menu)
                    row = self.client.getobj(
                        tuple(path), oid, filter_mask=mask, menu=read_menu
                    )
                    full_row = True
                    # The full read can carry the slot a nested overlay keys on
                    # (a wireless card's `hwtype`) that the sparse list row did
                    # not, so re-resolve now that the object is complete. The
                    # top-level type does not change on a read-back.
                    menu = self.client.menu_for(
                        tuple(path), type_value,
                        values=getattr(row, "values_by_code", None),
                        name=menu_name,
                    ) or menu
                except Exception:
                    pass
        win = PropertyWindow(
            title,
            tuple(path),
            row,
            client=self.client,
            edit_session=self.edit,
            menu=menu,
            row_key=row_key,
            show_passwords=self._show_passwords,
            # Already fetched above; the form must not fetch it again.
            full_row=full_row,
            # Quick Set's `type:toolbar` verbs (Port Mapping, System ▸ Reboot)
            # belong on this form's button column too, not only on the pane's
            # toolbar: the form is where Winbox shows them.
            on_context_action=self.run_context_action,
            # The sibling menus this form is the window for (System ▸ Resources
            # ▸ Hardware / CPU / IRQ). Each is a menu like any other, so the
            # button opens it the same way a nav click would.
            menu_buttons=menu_buttons,
            on_open_menu=self.open_menu,
        )
        win.queued.connect(self._on_property_change)
        self._show_child(win)
        self._register(key, win)

    #: System ▸ Identity. Its value is in this window's title, so a change
    #: there has to be read back and retitled.
    IDENTITY_PATH = (24, 1)

    def _on_property_change(self, change) -> None:
        """A property form queued or applied a change."""
        self._refresh_mode_label()
        if not self.edit.live:
            return  # held locally; there is nothing new on the device to read
        path = getattr(change, "path", None)
        self.reload_lists_for(path)
        if path is not None and tuple(path) == self.IDENTITY_PATH:
            self._refresh_identity()

    def _refresh_identity(self) -> None:
        """Re-read the device identity and retitle the window.

        The identity is part of the session title, so renaming the device left
        this window still announcing the old name until reconnect.
        """
        if self.client is None:
            return
        try:
            info = self.client.refresh_info()
        except Exception:  # noqa: BLE001
            return
        name = (info.identity or "").strip()
        if not name or name == self.session.identity:
            return
        self.session.identity = name
        self.setWindowTitle(self.session.title())
        self.statusBar().showMessage(f"Identity is now {name}", 4000)

    def reload_lists_for(self, path) -> None:
        """Re-read every open list showing `path`.

        The row-level actions (Remove, Enable/Disable) already reload after a
        live write; a property form's Apply did not, so the list it was opened
        from kept displaying the old values -- an edited address still read as
        its previous one until some other action forced a refresh.
        """
        if path is None:
            return
        from .list_pane import ListPane

        want = tuple(path)
        for sub in self.mdi.subWindowList():
            # findChildren rather than a type switch: this has to reach panes
            # inside a MultiPageListWindow's tabs as well as a plain
            # ListWindow, and those tabs share one path.
            for pane in sub.findChildren(ListPane):
                if getattr(pane, "path", None) == want:
                    pane.reload()

    def _show_child(self, win) -> None:
        """Add `win` to the MDI area, place it, and show it.

        Qt's own placer fills the area from the top-left corner: the first
        window landed in the corner and every later one stepped away towards
        the bottom edge -- never where the user is looking. Every window this
        app opens comes through here instead and opens in the middle of the
        area.

        A caller that knows where its window goes says so by moving it before
        this call: `move()` sets ``WA_Moved``, and that position is what the
        window gets. The move has to be replayed after ``addSubWindow``,
        because the placer overrides a position set before the window has an
        area to sit in -- which is also why the size is only read here: a
        window that never called ``resize()`` is sized from its hint by the
        add, not before it.
        """
        wanted = win.pos() if win.testAttribute(Qt.WidgetAttribute.WA_Moved) else None
        self.mdi.addSubWindow(win)
        win.move(wanted if wanted is not None else self._centre_pos(win.size()))
        win.show()

    def _centre_pos(self, size) -> QPoint:
        """The middle of the visible area for a window of `size`.

        Two windows of the same size would centre on the same pixel and the
        second would hide the first completely, so a spot already taken steps
        down-right by a title bar's worth -- until the window would hang off
        the area, at which point the middle is the better of the two answers.
        """
        area = self.mdi.viewport().rect()
        x = max(0, (area.width() - size.width()) // 2)
        y = max(0, (area.height() - size.height()) // 2)
        # This window is in the list already (the add precedes the placing),
        # but it is not shown yet, so it cannot collide with itself.
        taken = {(w.x(), w.y()) for w in self.mdi.subWindowList() if w.isVisible()}
        step = style.scale_px(_STACK_STEP)
        while (x, y) in taken:
            if (
                x + size.width() + step > area.width()
                or y + size.height() + step > area.height()
            ):
                break
            x += step
            y += step
        return QPoint(x, y)

    def _focus_existing(self, key) -> bool:
        """Raise an already-open window for `key`. True if one was found.

        Most menus should have a single window: reopening the same object was
        stacking identical forms on top of each other.
        """
        existing = self._open.get(key)
        if existing is None:
            return False
        try:
            # Closed but not yet deleted -- the delete is deferred to the event
            # loop -- is gone too. Raising it brought back nothing: a second
            # Add straight after a first was "focusing" the closed form of the
            # first, and no new form opened.
            if (
                isinstance(existing, QMdiSubWindow)
                and existing not in self.mdi.subWindowList()
            ):
                self._open.pop(key, None)
                return False
            existing.showNormal()
            existing.raise_()
            self._focus_window(existing)
            return True
        except RuntimeError:      # C++ side already deleted
            self._open.pop(key, None)
            return False

    def _register(self, key, win) -> None:
        self._open[key] = win
        win.destroyed.connect(lambda *_a, k=key: self._open.pop(k, None))
        self._focus_window(win)
        self._count_open_after_dwell(win)

    def _focus_window(self, win) -> None:
        """Hand the keyboard to a window we just opened or raised.

        Clicking the nav tree leaves focus in the tree, so the new window came
        up without it -- and Esc (which every window closes on) went to the
        tree instead of to the window the user was looking at.
        """
        try:
            if isinstance(win, QMdiSubWindow):
                self.mdi.setActiveSubWindow(win)
            take = getattr(win, "take_focus", None)
            if take is not None:
                take()
            else:
                win.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        except RuntimeError:      # closed under us
            pass

    def _count_open_after_dwell(self, win) -> None:
        """Count the open that produced ``win`` toward "most used" -- if it sticks.

        Nothing is counted at click time: the tally lands USAGE_DWELL_MS later
        and only if the window is still open, so a row hit by mistake and shut
        again leaves the list alone. Only :meth:`open_menu` arms this (via
        ``_pending_open_id``); windows opened any other way never count.
        """
        open_id = self._pending_open_id
        if not open_id:
            return

        def commit() -> None:
            try:
                still_open = win.isVisible()
            except RuntimeError:   # C++ side gone: it was closed and deleted
                return
            if still_open:
                menu_usage.record_open(open_id)

        QTimer.singleShot(USAGE_DWELL_MS, commit)

    @staticmethod
    def _coerce_mode(data: object) -> EditMode | None:
        """Qt hands back the enum's *value*, not the enum.

        EditMode subclasses str, so QComboBox round-trips it through QVariant
        and currentData() answers a plain 'live'. `isinstance(data, EditMode)`
        is False for that — which is why picking a mode in the toolbar used to
        do nothing whatsoever, Live included.
        """
        if isinstance(data, EditMode):
            return data
        try:
            return EditMode(data)
        except (ValueError, TypeError):
            return None

    def _on_mode_selected(self, _index: int) -> None:
        mode = self._coerce_mode(self._mode_combo.currentData())
        if mode is None:
            return
        if mode is EditMode.LIVE and not self.edit.write_supported:
            QMessageBox.warning(
                self,
                "Live mode",
                "This session has no device connection, so nothing can be "
                "written. Changes are held locally instead.",
            )
        elif mode is EditMode.LIVE and not self._safe_mode_on:
            ans = QMessageBox.question(
                self,
                "Live mode",
                "Live mode writes straight to the router: Apply, Remove, "
                "Enable/Disable and Comment take effect immediately.\n\n"
                "Safe Mode is off. With it on, everything this session changes "
                "is unrolled if the connection drops \u2014 which is what saves "
                "you from an edit that locks you out.\n\nTurn Safe Mode on now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans == QMessageBox.StandardButton.Yes:
                self._safe_act.setChecked(True)
        self._apply_mode(mode)

    def _write_guard(self) -> str | None:
        """Why a live write must not happen right now, or None to allow it.

        Safe Mode is recommended, not required. Winbox warns and then lets you
        proceed, and refusing every write here meant Live mode could not do
        the one thing it exists for. The warning belongs where the decision is
        made -- switching into Live offers to arm Safe Mode
        (see _on_mode_selected) -- and declining it is allowed to mean what it
        says.

        Kept as the hook for a genuine blocker; the transcript records writes
        made without Safe Mode so an unrolled-vs-permanent change is still
        answerable after the fact.
        """
        if not self._safe_mode_on:
            log = getattr(self.client, "session_log", None)
            if log is not None:
                log.note("live write with Safe Mode off (not unrolled on disconnect)")
        return None

    def _apply_mode(self, mode: EditMode) -> None:
        self.edit.set_mode(mode)
        # Last line before the wire: the client refuses to write unless Live is
        # the selected mode, whatever else in the UI asks it to.
        if self.client is not None:
            self.client.readonly = mode is not EditMode.LIVE
        log = getattr(self.client, "session_log", None)
        if log is not None:
            log.note(f"edit mode -> {mode.value}")
        checked = self.edit.readonly
        # Re-lock the forms / list toolbars that are already open.
        for sub in self.mdi.subWindowList():
            if isinstance(sub, PropertyWindow):
                sub.set_readonly(checked)
            elif isinstance(sub, ListWindow):
                sub.pane.set_readonly(checked)
            elif isinstance(sub, MultiPageListWindow):
                sub.set_readonly(checked)
        self._refresh_mode_label()
        if checked:
            note = "Read-only"
        elif mode is EditMode.LIVE:
            note = (
                "Live mode — Apply / Remove / Enable / Disable / Comment are "
                "sent to the router immediately"
            )
        else:
            note = (
                "Queue mode — select row(s), then Add/Remove/Enable/Disable "
                "(local only)"
            )
        self.statusBar().showMessage(note, 4000)

    def _on_queue_changed(self) -> None:
        self._refresh_mode_label()
        for pane in self._for_each_list_pane():
            try:
                pane._rebuild_view()
            except RuntimeError:      # a pane whose window is going away
                pass

    def _for_each_list_pane(self):
        """Every open list pane, including the tabs of grouped windows."""
        for sub in self.mdi.subWindowList():
            if isinstance(sub, ListWindow):
                yield sub.pane
            elif isinstance(sub, MultiPageListWindow):
                yield from sub.panes()

    def _on_live_start_toggled(self, on: bool) -> None:
        """Remember which mode the next session opens in."""
        save_start_mode("live" if on else "readonly")
        self.statusBar().showMessage(
            "New sessions will open in Live mode" if on
            else "New sessions will open read-only",
            4000,
        )

    def _on_show_passwords_toggled(self, checked: bool) -> None:
        self._show_passwords = bool(checked)
        save_show_passwords(self._show_passwords)
        for pane in self._for_each_list_pane():
            pane.set_show_passwords(self._show_passwords)
        for sub in self.mdi.subWindowList():
            if isinstance(sub, PropertyWindow):
                sub.set_show_passwords(self._show_passwords)
        self.statusBar().showMessage(
            "Passwords shown" if checked else "Passwords hidden", 3000
        )

    def _on_inline_comments_toggled(self, checked: bool) -> None:
        self._inline_comments = bool(checked)
        save_inline_comments(self._inline_comments)
        for pane in self._for_each_list_pane():
            pane.set_inline_comments(self._inline_comments)
        self.statusBar().showMessage(
            "Comments as a trailing column"
            if checked
            else "Comments as a ';;; ' line above each item",
            3000,
        )

    def _refresh_mode_label(self) -> None:
        # Undo and Redo write to the router directly; there is no queueing a
        # history step, so they are Live mode's only.
        live = self.client is not None and self.edit.mode is EditMode.LIVE
        for act in (getattr(self, "_undo_act", None), getattr(self, "_redo_act", None)):
            if act is not None:
                act.setEnabled(live)
        self._mode_lab.setText(f"  [{tr(self.edit.label())}]  ")
        self._mode_lab.setStyleSheet(
            "color: #600000; font-weight: bold;" if self.edit.readonly
            else "color: #806000; font-weight: bold;"
        )
        n = len(self.edit.queue)
        self._queue_act.setText(f"Pending changes ({n})" if n else "Pending changes")
        self._queue_act.setEnabled(bool(n))
        self._queue_act.setToolTip(
            "Review the changes held locally: send them all, or discard them."
            if n else "No changes are held locally."
        )
        show_queue = bool(n) or self.edit.mode is EditMode.QUEUE
        self._queue_act.setVisible(show_queue)
        self._queue_sep.setVisible(show_queue)

    def _on_local_change(self, change: Any, summary: str) -> None:
        """A write this session made just landed. Arm the snapshot."""
        title = getattr(change, "title", "") or ""
        path = tuple(getattr(change, "path", ()) or ())
        where = title or (",".join(str(x) for x in path) if path else "session")
        self.auto_export.note_local(where, summary, path=path)

    def _on_pane_content_changed(self, pane: Any, delta: Any) -> None:
        """A list's configuration moved. Decide whether it was us.

        The device notifies every subscriber of a change, including the session
        that made it, so our own edit comes back as a push a second later. Any
        movement on a path this session wrote to recently is therefore ours,
        and was already recorded by :meth:`_on_local_change`; only the rest is
        somebody else.
        """
        path = tuple(getattr(pane, "path", ()) or ())
        if self.auto_export.owns_recent_write(path):
            return
        where = self._pane_label(pane)
        self.auto_export.note_remote(where, delta.summary())
        # Transient, so it cannot pile up in front of anything.
        self.statusBar().showMessage(
            f"Changed elsewhere — {where}: {delta.summary()}", 8000
        )

    @staticmethod
    def _pane_label(pane: Any) -> str:
        """The best name available for the list a change arrived on."""
        for attr in ("title", "_title"):
            text = getattr(pane, attr, "")
            if isinstance(text, str) and text:
                return text
        win = pane.window() if hasattr(pane, "window") else None
        title = win.windowTitle() if win is not None else ""
        if title:
            return title
        path = getattr(pane, "path", None)
        return f"[{','.join(str(x) for x in path)}]" if path else "device"

    def _wire_pane(self, pane: Any) -> None:
        """Connect one list pane to the session-wide bookkeeping.

        Both signals are session-scoped rather than window-scoped, and panes
        are created in four different places, so they are hooked up here in one
        function instead of four times over.
        """
        try:
            pane.queue_changed.connect(self._refresh_mode_label)
            pane.content_changed.connect(self._on_pane_content_changed)
        except (AttributeError, RuntimeError):
            pass
        self._offer_wizards(pane, getattr(pane, "path", None))

    def _offer_wizards(self, pane: Any, path: Any) -> None:
        """Put a `type:setup` wizard's button on the window it belongs to.

        Winbox shows DHCP Setup on the DHCP Server window and Hotspot Setup on
        the Hotspot one -- a button beside Add, not a menu entry of its own. The
        .jg says which window by the handler the wizard writes to; see
        Schema.wizards_for.
        """
        schema = getattr(self.client, "schema", None)
        finder = getattr(schema, "wizards_for", None)
        if not callable(finder) or not path:
            return
        try:
            wizards = finder(tuple(path))
        except Exception:  # noqa: BLE001
            return
        for menu in wizards:
            try:
                pane.add_toolbar_action(
                    str(menu.title or "Setup"),
                    f"{menu.title}: a guided add on handler "
                    f"{tuple(menu.path or ())}",
                    lambda mn=menu: self._open_setup_wizard(mn),
                )
            except (AttributeError, RuntimeError):
                return

    def _open_setup_wizard(self, menu: Any) -> None:
        """Run one setup wizard (see widgets/setup_wizard.py)."""
        from .setup_wizard import SetupWizard

        if self.client is None:
            QMessageBox.information(
                self, str(menu.title or "Setup"), "Not connected."
            )
            return
        SetupWizard(
            menu, client=self.client, edit_session=self.edit, parent=self
        ).exec()

    def _refresh_snapshot_chrome(self) -> None:
        """Redraw the warning button and the snapshot label. The one switch."""
        auto = self.auto_export
        n = len(auto.remote_notes)
        self._remote_act.setVisible(bool(n))
        self._remote_sep.setVisible(bool(n))
        if n:
            self._remote_act.setText(
                f"Changed elsewhere ({n})" if n > 1 else "Changed elsewhere"
            )
            self._remote_act.setToolTip(
                f"{n} change{'s' if n != 1 else ''} arrived from another "
                "session or the console. Click to see what, and to dismiss."
            )
        if self._remote_btn is not None:
            # Amber, not red: this is news, not an emergency, and the session
            # is still perfectly usable -- which is the point.
            self._remote_btn.setStyleSheet(
                "QToolButton { background: #ffe08a; color: #4a3200;"
                " border: 1px solid #b8860b; border-radius: 2px;"
                " padding: 1px 6px; font-weight: bold; }"
                if n else ""
            )
        if auto.armed or auto.busy:
            if not self._snapshot_tick.isActive():
                self._snapshot_tick.start()
        elif self._snapshot_tick.isActive():
            self._snapshot_tick.stop()
        text = ""
        if auto.busy:
            text = "  snapshot…"
        elif auto.armed:
            left = auto.seconds_left()
            text = f"  snapshot in {left // 60}:{left % 60:02d}"
        elif auto.last_error:
            text = "  snapshot failed"
        self._snapshot_lab.setText(text)
        self._snapshot_lab.setToolTip(auto.status_line())

    def _on_auto_export_toggled(self, checked: bool) -> None:
        self.auto_export.set_enabled(checked)
        save_auto_export(bool(checked))
        self.statusBar().showMessage(
            "Automatic config snapshots on — one is taken after a change, "
            "once the session goes quiet"
            if checked else "Automatic config snapshots off",
            4000,
        )

    def _on_auto_export_secret_toggled(self, checked: bool) -> None:
        self.auto_export.show_sensitive = bool(checked)
        save_auto_export_sensitive(bool(checked))
        self.statusBar().showMessage(
            "Snapshots now include keys and passwords — treat the snapshot "
            f"folder accordingly ({autoexport.device_dir(self.session)})"
            if checked
            else "Snapshots now blank out secrets (they cannot restore a device)",
            8000,
        )

    def _set_snapshot_secs(self, secs: int) -> None:
        self.auto_export.set_idle_secs(secs)
        save_auto_export_idle_secs(secs)
        for act in self._snapshot_actions:
            act.setChecked(act.data() == secs)
        self.statusBar().showMessage(
            f"Config snapshot taken after {self._idle_text(secs)} of quiet "
            "following a change",
            4000,
        )

    def _snapshot_now(self) -> None:
        """Take one on demand, whether or not anything changed."""
        if self.client is None:
            return
        if self.auto_export.busy:
            self.statusBar().showMessage("A snapshot is already running", 3000)
            return
        self.statusBar().showMessage("Taking a config snapshot…", 3000)
        worker = _CallWorker(
            lambda: self.auto_export._capture("manual"), self
        )
        worker.finished_ok.connect(self.auto_export._on_done)
        worker.finished_err.connect(self.auto_export._on_fail)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_snapshot_taken(self, record: object) -> None:
        if getattr(record, "unchanged", False):
            self.statusBar().showMessage(
                "Config snapshot: identical to the last one, nothing stored",
                6000,
            )
            return
        path = getattr(record, "path", None)
        name = path.name if path is not None else "?"
        extra = " (log lines were spliced into the capture)" if getattr(
            record, "disturbed", False
        ) else ""
        self.statusBar().showMessage(f"Config snapshot saved: {name}{extra}", 8000)

    def _on_snapshot_failed(self, msg: str) -> None:
        # Status bar only. A failed snapshot is not a reason to interrupt.
        self.statusBar().showMessage(f"Config snapshot failed: {msg}", 8000)

    def _show_snapshot_folder(self) -> None:
        directory = autoexport.device_dir(self.session)
        target = _nearest_existing(directory)
        pending = target != directory
        body = (
            f"Config snapshots for {self._device_label()}:\n\n{directory}\n\n"
            "One .rsc per capture, plus index.jsonl listing every capture with "
            "its digest and what triggered it. Identical captures are not "
            "stored twice."
        )
        if pending:
            body += (
                "\n\nNothing has been captured for this device yet, so the "
                "folder does not exist. The first snapshot creates it."
            )
        box = QMessageBox(self)
        box.setWindowTitle("Snapshot Folder")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(body)
        # A path somebody has to retype into a file manager is a path they will
        # mistype -- so offer the trip itself. ActionRole keeps it away from the
        # Escape/Enter answer, which stays "close the box".
        open_btn = box.addButton("&Open Folder", QMessageBox.ButtonRole.ActionRole)
        open_btn.setToolTip(
            f"Open {target} in the desktop's file manager"
            + (" — the closest folder that exists" if pending else "")
        )
        close = box.addButton(QMessageBox.StandardButton.Close)
        box.setDefaultButton(close)
        box.setEscapeButton(close)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_in_file_manager(target)

    def _open_in_file_manager(self, path: Path) -> None:
        """Hand a directory to whatever the desktop browses files with.

        ``QDesktopServices`` is the portable one: xdg-open on Linux, Explorer on
        Windows, Finder on macOS. It can fail quietly -- a session over SSH with
        no desktop has nothing to hand it to -- so the status bar reports either
        way, and the path is repeated on failure for whoever has to go there by
        hand.
        """
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self.statusBar().showMessage(
            f"Opened {path} in the file manager"
            if ok
            else f"No file manager answered — the folder is {path}",
            8000,
        )

    def _show_remote_changes(self) -> None:
        """Open the non-modal review window for changes made elsewhere."""
        notes = list(self.auto_export.remote_notes)
        if not notes:
            return
        if self._remote_win is not None:
            try:
                self._remote_win.close()
            except RuntimeError:
                pass
            self._remote_win = None
        win = RemoteChangeWindow(notes, self._device_label(), self)
        win.dismissed.connect(self.auto_export.clear_remote)
        win.destroyed.connect(lambda *_: setattr(self, "_remote_win", None))
        self._remote_win = win
        self._show_child(win)

    def _confirm_drop_queue(self) -> bool:
        """Ask before closing over queued changes. True to close anyway."""
        n = len(self.edit.queue)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Pending changes")
        box.setText(
            f"{n} queued change(s) have not been sent to the router.\n\n"
            "Closing this session discards them."
        )
        show = box.addButton("Show changes", QMessageBox.ButtonRole.ActionRole)
        drop = box.addButton("Discard and close", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        # Enter and Esc both keep the work.
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        with self._modal():
            box.exec()
        clicked = box.clickedButton()
        if clicked is show:
            self._show_queue()
            return False
        return clicked is drop

    def _show_queue(self) -> None:
        """Pending changes: every queued change on its own, each droppable."""
        from .pending_window import open_pending

        if not self.edit.queue:
            QMessageBox.information(self, "Pending changes", "The queue is empty.")
            return
        # Not a Safe Mode dialog, but reading it is still being at the desk.
        with self._modal():
            open_pending(
                self.edit, self,
                can_send=self.edit.flush_reason(),
                apply=self._flush_queue,
            )
        self._refresh_mode_label()

    def _flush_queue(self) -> None:
        """Send the whole queue, then put the UI back in step with the device.

        The queue's only exit apart from Discard. Which changes go and in what
        order is EditSession.flush()'s business; what is here is the decision
        to send and the clean-up after -- the lists that now read differently,
        and the forms that built these changes and would otherwise never learn
        they landed.
        """
        n = len(self.edit.queue)
        # The last gate before the wire, so it says what Safe Mode is worth
        # right now rather than assuming somebody remembers -- and holds the
        # idle watchdog while it is up, like every other hazard dialog here.
        with self._modal():
            ans = QMessageBox.question(
                self,
                "Apply all",
                f"Send {n} queued change(s) to the router now, in the order "
                "they were made?\n\n"
                + (
                    "Safe Mode is on: everything this session changes is "
                    "unrolled if the connection drops."
                    if self._safe_mode_on else
                    "Safe Mode is off: these changes are permanent — "
                    "including the one that locks you out."
                )
                + "\n\nSending stops at the first change the router refuses. "
                "That change and everything after it stay queued.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        if ans != QMessageBox.StandardButton.Yes:
            return
        log = getattr(self.client, "session_log", None)
        if log is not None:
            log.note(f"apply all: sending {n} queued change(s)")
        try:
            with self._modal():
                landed, failed = self.edit.flush()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Apply all", f"Nothing was sent:\n\n{e}")
            self._refresh_mode_label()
            return

        # A form whose change went out from here still shows it as unsent: its
        # editors stay highlighted, and a New form still has no object id, so
        # its next Apply would add that object a second time.
        for change, _summary in landed:
            for sub in self.mdi.subWindowList():
                if isinstance(sub, PropertyWindow) and sub.owns_queued(change):
                    sub.sync_after_external_write(change)
        # One reload per menu, however many of its rows changed.
        for path in dict.fromkeys(tuple(c.path) for c, _ in landed):
            self.reload_lists_for(path)
            if path == self.IDENTITY_PATH:
                self._refresh_identity()
        self._refresh_mode_label()

        if failed is not None:
            change, reason = failed
            where = ",".join(str(x) for x in change.path)
            QMessageBox.warning(
                self,
                "Apply all",
                f"{len(landed)} of {n} change(s) were sent.\n\n"
                # Not "the router refused": a value this side cannot encode
                # stops here too, before anything is sent.
                f"Stopped at [{change.action}] ({where}) "
                f"{change.title}:\n\n{reason}\n\n"
                f"{len(self.edit.queue)} change(s) are still queued, that one "
                "first.",
            )
            return
        self.statusBar().showMessage(f"Applied {len(landed)} queued change(s)", 4000)
        QMessageBox.information(
            self, "Apply all", f"All {len(landed)} queued change(s) were sent."
        )

    def _show_update(self, release) -> None:
        self._update_button.setText(f"x3 {release.version} available…")
        self._update_button.setToolTip("A newer x3 is out: see what changed, and update")
        self._update_button.show()

    def _open_update(self) -> None:
        if notifier.release is not None:
            UpdateDialog(notifier.release, self).exec()

    def _check_updates_now(self) -> None:
        """Help ▸ Check for x3 Updates: asks now, whatever the setting says."""
        self.statusBar().showMessage("Checking for a newer x3…", 4000)

        def done(release, error) -> None:
            if error:
                QMessageBox.warning(self, "Check for x3 Updates", error)
            elif release is None:
                QMessageBox.information(
                    self, "Check for x3 Updates", f"x3 {__version__} is the newest release."
                )
            else:
                self._open_update()

        self._update_check = check_in_background(self, manual=True, on_result=done)

    def _welcome_tour(self) -> None:
        """Rerun the first-start tour, then bring this window up to date with it.

        The theme was applied by the tour itself; re-applying it here is what
        refreshes this window's menus and metrics. Every other switch the tour
        shares with a menu goes through that menu's own toggle, so it takes
        effect here at once; the start mode and audit apply to the next session.
        """
        if WelcomeWizard(self).exec() != WelcomeWizard.DialogCode.Accepted:
            return
        self._set_theme(load_theme_id())
        self._sync_zoom_menu()
        self._set_icon_source(load_icon_source())
        self._set_idle_secs(load_safe_mode_idle_secs())
        for act, value in (
            (self._auto_export_act, load_auto_export()),
            (self._auto_export_secret_act, load_auto_export_sensitive()),
            (self._key_hints_act, load_key_hints()),
            (self._dropdown_marks_act, load_dropdown_marks()),
            (self._status_line_act, load_form_status_line()),
            (self._titlebar_tabs_act, load_titlebar_tabs()),
            (self._auto_most_used_act, load_most_used_auto()),
            (self._live_updates_act, load_live_updates()),
            (self._update_check_act, load_update_check()),
        ):
            act.setChecked(value)
        self._live_start_act.blockSignals(True)
        self._live_start_act.setChecked(load_start_mode() == "live")
        self._live_start_act.blockSignals(False)
        self.statusBar().showMessage(
            "Welcome Tour saved — start mode and audit apply to the next session",
            6000,
        )

    def _about(self) -> None:
        lines: list[str] = []
        if self.client and self.client.schema:
            s = self.client.schema
            where = getattr(self.client, "schema_dir", None) or "?"
            mismatch = getattr(self.client, "schema_build_mismatch", None)
            lines = [
                f"Schema: {s.version}",
                f"From: {where}"
                + (f"  (device build {mismatch[1]})" if mismatch else ""),
                f"Menus: {len(s.menus)}  Nav: {len(s.nav)}  Fields: {s.field_count}",
            ]
        AboutDialog(self, session_lines=lines).exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        # The queue lives in this window and nowhere else: closing (Disconnect
        # and Quit both land here) threw it away without a word.
        if self.edit.queue and not self._confirm_drop_queue():
            event.ignore()
            return
        if self._safe_mode_on and self.client is not None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Safe Mode active")
            box.setText(
                "You are in Safe Mode.\n\n"
                "Quitting will typically unroll changes made under Safe Mode "
                "unless you leave Safe Mode first (keep changes)."
            )
            keep_btn = box.addButton(
                "Leave Safe Mode (keep) and quit", QMessageBox.ButtonRole.AcceptRole
            )
            unroll_btn = box.addButton(
                "Quit (unroll on disconnect)", QMessageBox.ButtonRole.DestructiveRole
            )
            cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
            with self._modal():
                box.exec()
            clicked = box.clickedButton()
            if clicked is cancel_btn or clicked is None:
                event.ignore()
                return
            if clicked is unroll_btn and not self._confirm_unroll(
                "Quit — discard my changes"
            ):
                event.ignore()
                return
            if clicked is keep_btn:
                try:
                    self.client.safe_mode_leave(keep_changes=True)
                except Exception as e:
                    QMessageBox.warning(self, "Safe Mode", f"Could not release: {e}")
                    event.ignore()
                    return
            # unroll_btn: just disconnect — device unrolls
        self._idle_timer.stop()
        self._guard_timer.stop()
        self._stop_stats_poll()
        self._link_timer.stop()
        self._reconnect_timer.stop()
        self._link_down = None
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        # Close consoles while the socket is still up: closing one sends /quit.
        for sub in self.mdi.subWindowList():
            if isinstance(sub, TerminalWindow):
                try:
                    sub.close()
                except Exception:
                    pass
        # The last snapshot, while there is still a console to take it on. Any
        # change this session made or saw is owed one, and after client.close()
        # below there is no way to ask. Best-effort by design: a disconnect must
        # never be the thing that fails, and a session killed by a dropped link
        # never reaches this line at all -- which is why the debounce exists
        # rather than relying on shutdown.
        if self.client is not None:
            record = self.auto_export.flush_now("disconnect")
            if record is not None and record.path is not None:
                applog.note(f"disconnect snapshot: {record.path}")
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        # Order matters: the client is down, so every list pane still waiting
        # on a reply has just been failed and will unwind at once. Join them
        # here -- Qt destroying a running QThread on the way out is a fatal
        # abort, and closing the window while a pane was loading used to hit
        # exactly that.
        stubborn = qthreads.retire_all(self)
        if stubborn:
            applog.note(f"{stubborn} worker thread(s) would not join on close")
        super().closeEvent(event)


def _termopen_command(values: dict) -> str:
    """The router console command a `termopen` dialog's values describe.

    Type 1 telnet, 2 ssh, 3 mac telnet -- the vocabulary the .jg declares.
    """
    def text(name: str) -> str:
        value = values.get(name)
        return str(value).strip() if value not in (None, "") else ""

    kind = values.get("Type")
    try:
        kind = int(kind)
    except (TypeError, ValueError):
        kind = 1
    if kind == 3:
        mac = text("MAC Address")
        return f"/tool mac-telnet {mac}" if mac else ""
    address = text("Address")
    if not address:
        return ""
    port = text("Port")
    user = text("User")
    if kind == 2:
        parts = ["/system ssh", address]
        if user:
            parts += ["user=" + user]
        if port and port not in ("0", "22"):
            parts += ["port=" + port]
        return " ".join(parts)
    parts = ["/system telnet", address]
    if port and port not in ("0", "23"):
        parts += ["port=" + port]
    return " ".join(parts)
