"""Reusable list/table pane (embeddable in MDI windows or multi-page tabs)."""

from __future__ import annotations

import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import (
    QItemSelection,
    QItemSelectionModel,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QStyledItemDelegate,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from x3.proto import schema as sch
from x3.proto.schema import is_ordered_path as sch_is_ordered_path

from .. import a11y, applog, learned_defaults, qthreads, settings, style
from ..edit_mode import EditSession, QueuedChange
from ..icons import action_icon, icon_size
from .. import change_watch
from ..models.table_model import FRESH_SECONDS, SECRET_MASK, SimpleTableModel
from ..settings import load_column_prefs, save_column_prefs
from ..sortkey import sort_key
from .comment_delegate import CommentBannerDelegate, CommentTableView
from .data_header import DataHeaderView
from .property_dialog import PropertyDialog
from .wheel_guard import guard_wheel

#: Repaint cadence for the arrival fade. Fast enough to look continuous, slow
#: enough to stay off the CPU when a list is idle.
_FADE_MS = 250

#: Keys for the toolbar verbs, so a list can be worked without the mouse. They
#: are hung on the table rather than on the pane (see the constructor): Delete
#: has to keep deleting a character while the filter box is being typed into,
#: and Ctrl+E its end-of-line.
#:
#: Ctrl+D is not Disable: the main window already owns it (Disconnect), and a
#: key that means "drop the session" in every other window must not mean "turn
#: this interface off" in this one.
_ROW_KEYS = {
    "Add": "Ins",
    "Remove": "Del",
    "Enable": "Ctrl+E",
    "Disable": "Ctrl+Shift+E",
    "Comment": "Ctrl+M",
    "Move Up": "Alt+Up",
    "Move Down": "Alt+Down",
}

#: 0xFE000C a subscription carries when its menu declares no refreshfilter of
#: its own -- every list SUBSCRIBE in the capture sends at least this.
SUBSCRIBE_MASK_BASE = sch.STD_MASK_BASE

#: A pushed feed is the same rows over and over, so a run of them can only ever
#: change values, never tell us a row appeared or went away. This many merged
#: pushes and the list re-reads anyway, which is the floor on how stale the row
#: *set* can be while the counters stay live.
PUSH_REREAD_EVERY = 20

#: How far a push defers the next poll, in intervals. A device that really is
#: feeding us keeps the poll from ever firing -- it is pushed back before it
#: reaches zero -- and one that goes quiet costs this much staleness and no
#: more. It replaces standing the poll down on the first push and waiting out a
#: separate silence watchdog, which is what left the Interface window still for
#: four seconds; see _note_push.
PUSH_GRACE = 1.5

#: Settle time before a column drag is written to disk — one drag emits a
#: sectionResized per pixel.
_PREF_SAVE_MS = 400

#: Header label for the unnamed Winbox flag-letter column. The column itself
#: stays blank; this is only what the column chooser and the saved prefs call
#: it, since "" is a poor dictionary key.
FLAGS_LABEL = "Flags"

def move_anchor(ids: list, src: int, target: int):
    """The object a MOVE should insert before, moving `src` to `target`.

    MOVE is expressed as "put this one ahead of that one", so the anchor is
    whatever ends up at `target` once the moved row is taken out of the list.
    A target past the end has no anchor at all, which is exactly how
    ``move_obj(before=None)`` reads.
    """
    rest = [oid for i, oid in enumerate(ids) if i != src]
    return rest[target] if 0 <= target < len(rest) else None


#: True while a load-failure dialog is up anywhere. A modal runs a nested
#: event loop, so without this a burst of failures stacks one box inside the
#: next and the whole UI becomes unclickable. See ListPane._on_error.
_error_box_open = False

if TYPE_CHECKING:
    from x3.proto.device import RowObject


def _flag_legend(menu) -> dict[str, str]:
    """Letter -> what it means, for a flags column that has to be read aloud.

    The inverse of ``schema.format_flag_letters``, which is what fills the
    column: every letter in it comes either from the three universal flags or
    from one of the menu's own ``type:flag`` fields, and each of those carries
    the name Winbox puts in its legend ("R - running", "S - slave").

    The universal three win a collision. A menu that reuses D for something of
    its own leaves the column ambiguous to a sighted reader too, and "dynamic"
    is the reading that is right on every menu.
    """
    legend = {
        sch.FLAG_DISABLED_HINT: "disabled",
        sch.FLAG_INVALID_HINT: "invalid",
        sch.FLAG_DYNAMIC_HINT: "dynamic",
    }
    # Asked for rather than called outright: a menu here can be any object a
    # caller has that answers like one (the pane is fed stand-ins in tests),
    # and a list must still open when it cannot name its own letters.
    fields = getattr(menu, "flag_fields", None)
    for fld in (fields() if callable(fields) else ()):
        if fld.hint and fld.hint not in legend:
            legend[fld.hint] = (fld.name or fld.hint).strip().lower()
    return legend


class _FilterEdit(QLineEdit):
    """The list's filter box, with the two keys a keyboard-only walk needs.

    **Down** (or Enter) hands the keyboard to the list. Typing to narrow a list
    and then reaching for the mouse to pick a row is exactly the mouse this is
    trying to do without, and the box is where every list window opens.

    **Escape** empties the box instead of closing the window -- but only while
    there is something in it. Emptied, it falls through to the window, which
    closes on Escape like every other one, so nothing is taken away.
    """

    go_to_list = Signal()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        key = e.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            e.accept()
            self.go_to_list.emit()
            return
        if key == Qt.Key.Key_Escape and self.text():
            e.accept()
            self.clear()
            return
        super().keyPressEvent(e)


class _FetchWorker(QThread):
    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, fetcher: Callable[[], tuple], parent=None) -> None:
        super().__init__(parent)
        self._fetcher = fetcher

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(self._fetcher())
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class _NotifyWorker(QThread):
    """Waits on a device subscription and hands each push to the pane.

    The device pushes NOTIFY frames for as long as the subscription is open, so
    this thread only ever blocks on a queue. It carries the frame across rather
    than a bare "something changed": on an interval subscription most pushes
    are the same rows with new counters, and patching those in costs one
    repaint where a re-read costs a round trip and a model reset. The pane
    still re-reads whenever a push cannot account for itself.
    """

    pushed = Signal(object)

    def __init__(self, sub, parent=None) -> None:
        super().__init__(parent)
        self._sub = sub
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: N802
        while not self._stop:
            try:
                msg = self._sub.get(timeout=1.0)
            except Exception:  # noqa: BLE001
                return  # subscription closed, or the session died
            if msg is None:
                continue  # nothing this tick
            if not self._stop:
                self.pushed.emit(msg)


def _rate_text(col, was: tuple[int, float], value: int, now: float) -> str:
    """One rate cell: how far the counter moved, over how long.

    ``bigbitchangerate`` watches a *byte* counter and reads as a bit rate --
    Hotspot ▸ Active's Rx Rate off Bytes In -- so the difference is multiplied
    by eight and handed to the same renderer an interface's Rx Rate uses. The
    other two are counts, and take the .jg's own postfix ("p/s").
    """
    old_value, then = was
    dt = now - then
    if dt <= 0 or value < old_value:
        return ""
    delta = value - old_value
    if col.rate_bits:
        return sch.render_bitrate(int(delta * 8 / dt))
    return f"{delta / dt:,.0f}{col.postfix}"


#: Read-only types the router keeps counting -- bytes, packets, rates. Find
#: skips them; a plain read-only number (Actual MTU) stays searchable.
_COUNTER_TYPES = frozenset((
    "bytes", "bigbytes", "bigdecimal", "bignumber", "bitrate", "bigbitrate",
    "multibignumber", "decimal",
))


class ListPane(QWidget):
    """Toolbar + filter + table for one schema map path."""

    queue_changed = Signal()
    #: This list's *configuration* moved -- not its counters. Carries
    #: ``(pane, ContentDelta)``; MainWindow decides whether it was us.
    content_changed = Signal(object, object)
    #: The columns were just sized against real rows; carries the pixel width
    #: the header needs. A window opened at a fixed size widens to it once, so
    #: Routes opens showing Pref. Source instead of a scroll bar.
    columns_fitted = Signal(int)

    def __init__(
        self,
        title: str,
        *,
        readonly: bool = True,
        live_fetcher: Callable[[], tuple] | None = None,
        path: tuple[int, ...] | None = None,
        type_value: int | None = None,
        family: str | None = None,
        menu_name: str | None = None,
        on_open_properties: Callable[..., None] | None = None,
        on_context_action: Callable[..., None] | None = None,
        edit_session: EditSession | None = None,
        show_passwords: bool = False,
        inline_comments: bool = True,
        auto_refresh_secs: float | None = None,
        highlight_arrivals: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.title = title
        #: Last row count said out loud, so a poll that changes nothing is
        #: silent. None until the first load.
        self._said_count: int | None = None
        self.path = path
        # Winbox interface-type overlay this pane is filtered to, if any.
        # Several MultiPageListWindow tabs share one path and are told apart by
        # this, so it has to be known at construction: the saved column layout
        # is keyed on it.
        self.type_value: int | None = type_value
        # Overlay family this pane lists instead of a single type (Wireless ▸
        # WiFi Interfaces is every 'wlan' type). Same role as type_value for
        # identity and for the saved column layout.
        self.family: str | None = family
        #: The .jg ``name`` of the menu this pane lists, where a path hosts
        #: more than one (Web Proxy ▸ Connections shares (96,1) with the Web
        #: Proxy Settings form). Carried so a row's property form resolves to
        #: the same menu the list did instead of guessing from the path.
        self.menu_name: str | None = menu_name
        self._on_open_properties = on_open_properties
        #: Right-click row actions from the .jg (type:contextmenu). The owner
        #: window opens the target menu; a pane cannot -- it does not know
        #: about the nav tree or the MDI area.
        self._on_context_action = on_context_action
        self.edit_session = edit_session
        self.readonly = readonly if edit_session is None else edit_session.readonly
        self._raw_rows: list = []
        self._all_rows: list[list[Any]] = []
        self._columns: list[str] = []
        self._live_fetcher = live_fetcher
        self._worker: _FetchWorker | None = None
        # Device-pushed change notifications.
        self._notify: _NotifyWorker | None = None
        #: Last configuration-only fingerprint of the rows, from change_watch.
        #: None until the first load: a list appearing is not a list changing.
        self._content: dict | None = None
        self._sub = None
        self._sub_from: tuple[int, ...] | None = None
        self._notify_timer: QTimer | None = None
        # The device's own clock, when it is running one for us: the interval
        # asked for in the subscription, whether pushes are actually arriving,
        # and how many have been patched in since the last full read.
        self._push_interval_ms: int | None = None
        self._push_seen = False
        #: The cadence set_auto_refresh was asked for, kept so a push can widen
        #: the interval and the next poll can put it back.
        self._poll_base_ms: int | None = None
        self._pushes_since_read = 0
        self._menu = None  # schema Menu when known
        #: `type:toolbar` buttons, built once the menu arrives (it comes with
        #: the first load, not with the constructor). (widget, action) pairs so
        #: an `on:` condition can hide one as the selection moves.
        self._jg_buttons: list[tuple[Any, dict]] = []
        self._jg_toolbar_done = False
        #: (combo, filter spec) for the `type:filter` dropdowns above the list.
        self._jg_filters: list[tuple[Any, dict]] = []
        #: What a `maxobjs` cap left out of the last read, if anything.
        self._truncated: str | None = None
        # Column definitions for _menu, so a pushed row can be re-rendered
        # without asking the schema for them again on every frame.
        self._col_defs = None
        #: (cell index, Column) for every `changerate` column this list shows.
        #: Rebuilt with _col_defs; empty on the lists that declare none.
        self._rate_cols: list[tuple[int, Any]] = []
        #: Columns whose value ticks on its own -- rates and the router's
        #: counters -- which Find leaves out (see _index_rate_columns).
        self._ticking_cols: set[int] = set()
        #: The router's export lines for this list, by CLI path then .id; the
        #: keys being fetched; and the ones it could not place (_exact_summary).
        self._cli_lines: dict[str, dict[int, str]] = {}
        self._cli_fetching: set = set()
        self._cli_failed: set = set()
        self._cli_workers: set = set()
        #: row id -> {counter code: (value, monotonic seconds)}. The previous
        #: sample, which is the whole of what a rate needs that a single reply
        #: does not carry.
        self._rate_seen: dict[Any, dict[int, tuple[int, float]]] = {}
        self._write_actions: list = []
        # Verbs that act on the selected row(s): disabled when nothing is
        # selected (Add is not one of them -- it creates a new row).
        self._selection_actions: list = []
        # Winbox v3 view options.
        self._show_pw = bool(show_passwords)
        self._inline = bool(inline_comments)
        # Columns exactly as table_rows returned them, before the synthetic
        # Comment column; _columns is what the table actually shows.
        self._base_columns: list[str] = []
        self._all_comments: list[str] = []
        # Read-only status lines the device attaches to a row (STD_INFO), one
        # tuple per row of _all_rows. Winbox draws them in red where a comment
        # goes -- that is how a CAPsMAN-managed wlan says so in Interface.
        self._all_info: list[tuple[str, ...]] = []
        self._col_secret: list[bool] = []
        self._comment_available = False
        # Arrival highlight. Row identity is the router's own object id, so a
        # lease is tracked across polls even while 29 rows all render 0.0.0.0.
        self._highlight_arrivals = bool(highlight_arrivals) and live_fetcher is not None
        self._known_ids: set[Any] | None = None
        self._fresh_ids: dict[Any, float] = {}
        self._device_uptime: float | None = None
        #: Local monotonic stamp for the reading above, so an age column can be
        #: run forward between reads instead of standing still (_tick_ages).
        self._device_uptime_at: float | None = None
        self._age_timer: QTimer | None = None
        self._fade_timer: QTimer | None = None
        self._poll_timer: QTimer | None = None
        #: Live updates (subscribe + poll). Off = an idle list makes no requests
        #: until Refresh or re-enable — the low-bandwidth mode. Default from the
        #: global setting; toggled per-window with the Live toolbar button.
        #: Not ``_live``: that is :meth:`_live`, "writes reach the device", and
        #: an attribute of the same name shadowed the method -- every write verb
        #: in a list died on `'bool' object is not callable`.
        self._live_updates = settings.load_live_updates()
        #: Defer the first fetch/subscribe until the pane is shown (see the
        #: __init__ tail + showEvent) so a many-tab window (QuickSet = 19 tabs)
        #: loads only the active tab, not all panes at once.
        self._pending_first_load = False
        # None → decide from the schema on first load: a collection carrying
        # type:'age' stamps is time-varying, so it is worth polling.
        self._auto_refresh_secs = auto_refresh_secs
        self._auto_refresh_decided = False

        # Column layout + sort, remembered per list. Sort is by column *name*
        # so it survives the Comment column appearing and disappearing.
        self._prefs = load_column_prefs(self._pref_key())
        self._hidden_cols: set[str] = set(self._prefs.get("hidden") or [])
        #: Whether the first load has already applied the default column set.
        self._columns_defaulted = False
        #: Interface-tree parents (by name) whose children are hidden.
        self._collapsed: set[str] = set()
        #: Per-display-row tree state, rebuilt each view refresh.
        self._pending_tree_meta: dict[int, tuple[int, int]] = {}
        self._tree_row_name: dict[int, str] = {}
        self._sort_col: str | None = None
        self._sort_desc = False
        saved_sort = self._prefs.get("sort")
        if isinstance(saved_sort, list) and len(saved_sort) == 2:
            self._sort_col = str(saved_sort[0]) or None
            self._sort_desc = saved_sort[1] == "desc"
        # Non-zero while we drive the header ourselves, so autosizing and
        # restoring a layout don't get written back as user intent.
        self._suppress_prefs = 0
        self._pref_timer: QTimer | None = None
        # Column set the table was last measured for; None = never measured.
        self._sized_for: tuple[str, ...] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QToolBar()
        bar.setIconSize(icon_size())
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        #: Verbs whose key belongs to the table, hooked up once it exists.
        self._key_actions: list = []
        for name in ("Add", "Remove", "Enable", "Disable", "Comment"):
            act = bar.addAction(action_icon(name), name)
            act.setEnabled(not self.readonly)
            tip = self._verb_tip(name)
            act.setToolTip(self._with_key(tip, act, name))
            act.setStatusTip(tip)
            act.triggered.connect(lambda checked=False, n=name: self._toolbar_action(n))
            self._write_actions.append(act)
            if name != "Add":
                self._selection_actions.append(act)
        # Order-sensitive tables get explicit reordering. Firewall rules are
        # evaluated top-down, so which rule wins is a property of position.
        # The .jg names all 61 of them (``ordered:1``), but the menu only
        # arrives with the first load — so the buttons are built here, in
        # their proper place on the bar, and revealed when the menu confirms.
        self._ordered = sch_is_ordered_path(self.path)
        self._move_sep = bar.addSeparator()
        self._move_up_act = None
        self._move_down_act = None
        for name, delta in (("Move Up", -1), ("Move Down", 1)):
            act = bar.addAction(action_icon(name), name)
            act.setEnabled(not self.readonly)
            act.setToolTip(self._with_key(
                f"{name} — changes which rule matches first", act, name
            ))
            act.triggered.connect(
                lambda checked=False, d=delta: self._move_selected(d)
            )
            self._write_actions.append(act)
            self._selection_actions.append(act)
            if delta < 0:
                self._move_up_act = act
            else:
                self._move_down_act = act
        self._show_reordering(self._ordered)
        bar.addSeparator()
        self._refresh_act = bar.addAction(action_icon("Refresh"), "Refresh")
        self._refresh_act.setShortcut(QKeySequence("F5"))
        self._refresh_act.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._refresh_act.setToolTip("Reload list from device  [F5]")
        self._refresh_act.triggered.connect(self.reload)
        # On the pane, not the table: re-reading is safe from the filter box too.
        self.addAction(self._refresh_act)
        # Winbox's funnel: rows of field / operator / value under the Filter
        # box, on every list (see filter_rows). Hidden, they filter nothing.
        self._filter_act = bar.addAction(action_icon("Filter"), "Filter")
        self._filter_act.setCheckable(True)
        self._filter_act.setToolTip(
            "Filter rows: field, is / contains, value — every one must hold"
        )
        self._filter_act.toggled.connect(self._on_filter_toggled)
        # Find: the free-text box, shown only while it is on -- off, the box
        # and its label stay out of the way and search nothing.
        self._find_act = bar.addAction(action_icon("Find"), "Find")
        self._find_act.setCheckable(True)
        self._find_act.setShortcut(QKeySequence("Ctrl+F"))
        self._find_act.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self._find_act.setToolTip("Find: type to narrow the list  [Ctrl+F]")
        self._find_act.toggled.connect(self._on_find_toggled)
        self.addAction(self._find_act)
        self._live_act = bar.addAction(action_icon("beacon"), "Live")
        self._live_act.setCheckable(True)
        self._live_act.setChecked(self._live_updates)
        self._live_act.setToolTip(
            "Live updates: subscribe + auto-refresh. Turn off to save bandwidth "
            "on an idle window — stats stop updating (shown in the status line); "
            "use Refresh for a one-shot read."
        )
        self._live_act.toggled.connect(self._on_live_toggled)
        self._bar = bar
        layout.addWidget(bar)

        filter_row = QWidget()
        fr = QHBoxLayout(filter_row)
        fr.setContentsMargins(2, 1, 2, 1)
        fr.setSpacing(4)
        filter_label = QLabel("Find:")
        fr.addWidget(filter_label)
        self._filter = _FilterEdit()
        self._filter.setPlaceholderText("type to find…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        self._filter.go_to_list.connect(self.focus_rows)
        a11y.pair(filter_label, self._filter)
        fr.addWidget(self._filter)
        self._find_label = filter_label
        #: Where the .jg's own filter dropdowns go, to the right of the text
        #: box: Winbox puts Firewall's Chain filter and Address Lists' List
        #: filter exactly there.
        self._filter_row = fr
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {style.TEXT_DIM};")
        fr.addWidget(self._status)
        layout.addWidget(filter_row)
        # Find off hides the whole row -- the box, the row count beside it and
        # a list's own dropdowns (Firewall's Chain) with it. View ▸ Find Box
        # Open sets where a new list starts; set before the toggle is wired, so
        # opening a list does not snatch the keyboard into the box.
        from ..settings import load_find_open

        self._find_row = filter_row
        start_open = load_find_open()
        filter_row.setVisible(start_open)
        self._find_act.blockSignals(True)
        self._find_act.setChecked(start_open)
        self._find_act.blockSignals(False)
        from .filter_rows import FilterPanel

        self._filter_panel = FilterPanel(self._filter_fields, self._filter_values)
        self._filter_panel.setVisible(False)
        self._filter_panel.changed.connect(self._rebuild_view)
        layout.addWidget(self._filter_panel)

        flags_col = 0 if self._columns and self._columns[0] in ("", " ", "Flags") else None
        self._model = SimpleTableModel(
            self._columns, self._all_rows, flags_column=flags_col
        )
        # Hover a row for a moment: every setting it has, not only the columns
        # on show (see _row_summary).
        self._model.row_tooltip = self._row_summary
        self._model.reset_disabled_from_flags()
        self.table = CommentTableView()
        # Which list this is. A window has a title bar; a table inside it has
        # nothing of its own, so a reader landing on the rows would be told
        # only "table" -- and a page group puts several of them in one window.
        self.table.setAccessibleName(f"{title} list")
        self.table.setModel(self._model)
        # In before any of the chrome below: setHorizontalHeader() drops the
        # header Qt made, and with it everything already set on that one.
        self.table.setHorizontalHeader(DataHeaderView(self.table))
        # Winbox chrome; the banner delegate installed below subclasses the
        # column-rule one, so it keeps the separator.
        style.style_data_table(self.table, delegate=False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.selectionModel().selectionChanged.connect(
            self._update_selection_actions
        )
        self._update_selection_actions()
        # Qt's own sorting would reorder the model behind our back and break the
        # view→raw_rows mapping. We sort the view list instead (see _apply_sort),
        # which keeps `sources` exact and re-applies itself after every poll.
        self.table.setSortingEnabled(False)
        self.table.setShowGrid(style.COLUMN_RULES)
        self.table.verticalHeader().setVisible(False)
        # Before any setDefaultSectionSize below: see _apply_row_heights.
        self.table.verticalHeader().setMinimumSectionSize(1)
        self.table.verticalHeader().setDefaultSectionSize(style.ROW_HEIGHT)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setHighlightSections(False)
        hdr.setSectionsClickable(True)
        hdr.setSectionsMovable(True)
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.sectionResized.connect(self._on_section_resized)
        hdr.sectionMoved.connect(self._on_section_moved)
        hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._header_menu)
        self.table.setWordWrap(False)
        # Both the double-click and Enter arrive as row_activated: Qt's own
        # doubleClicked is dropped when a poll resets the model between the two
        # clicks, so the table works out an opened row itself.
        self.table.row_activated.connect(self._open_properties)
        # Winbox's right-click row menu: Tools ▸ Ping / Telnet / Torch, Make
        # Static, Copy to Access List -- every one of them declared in the .jg.
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.branch_toggled.connect(self._toggle_branch)
        self._banner = CommentBannerDelegate(self.table)
        self._banner.set_enabled(not self._inline)
        self.table.set_banner_delegate(self._banner)
        # Qt's own delegate for the common case; see _choose_item_delegate.
        self._plain_delegate = QStyledItemDelegate(self.table)
        self.table.setItemDelegate(self._plain_delegate)
        # Dragging a row only means something where position decides the
        # outcome, and only while the list is in the router's own order.
        self._set_row_drag(self._ordered and not self.readonly)
        self.table.rows_reordered.connect(self._drop_move)
        # Widget-scoped shortcuts fire only while their own widget has the
        # keyboard, so the row verbs belong to the table: Delete deletes a row
        # here and a character in the filter box, and each keeps its meaning.
        for act in self._key_actions:
            self.table.addAction(act)
        layout.addWidget(self.table)
        self._narrow_flags_column()
        self._apply_row_heights()

        if live_fetcher is not None:
            self._status.setText("loading…")
            # Defer the first fetch/subscribe until the pane is actually shown,
            # so a many-tab page group (QuickSet = 19 tabs) does not fetch and
            # subscribe every pane at once and freeze the UI. Only the visible
            # tab loads now; the rest load when their tab is first selected.
            self._pending_first_load = True
        else:
            self._autosize_columns()
            self._status.setText(f"{len(self._all_rows)} row(s){self._mode_tag()}")

    def _with_key(self, tip: str, act, name: str) -> str:
        """Give `act` its key (if it has one) and name it in the tooltip.

        A shortcut nobody can see is a shortcut nobody uses, and the tooltip is
        where anyone reaching for the mouse is already looking. Keys follow the
        action, so one that is greyed out (read-only mode, nothing selected) or
        hidden (Move on an unordered list) is inert without a second rule
        somewhere else deciding that.
        """
        key = _ROW_KEYS.get(name)
        if not key:
            return tip
        act.setShortcut(QKeySequence(key))
        act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._key_actions.append(act)
        return tip + self._key_hint(name)

    @staticmethod
    def _key_hint(name: str) -> str:
        """``"  [Del]"`` for a verb that has a key, or nothing for one that
        does not. Kept apart from :meth:`_with_key` because every tooltip is
        rewritten when the mode changes (see :meth:`set_readonly`), and that
        must not hand the same action its shortcut a second time."""
        key = _ROW_KEYS.get(name)
        if not key:
            return ""
        shown = QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)
        return f"  [{shown}]"

    def focus_rows(self) -> None:
        """Put the keyboard on the list, on a row it can act on.

        A table with no current row answers Down with its first one and Enter
        with nothing, so arriving from the filter box (or from anywhere else
        that hands the list the keyboard) lands on a row rather than beside one.

        And on a *selected* row: every verb on the toolbar acts on the
        selection, so a current row that is not selected is a list where Remove
        and Disable are greyed out for no reason anyone can see.
        """
        self.table.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self._model.rowCount():
            return
        index = self.table.currentIndex()
        if not index.isValid():
            index = self._model.index(0, self._current_column(0))
            self.table.setCurrentIndex(index)
        sm = self.table.selectionModel()
        if sm is None or not sm.hasSelection():
            self.table.selectRow(index.row())

    def _mode_tag(self) -> str:
        """The edit-mode tag on the status line, beside the row count.

        Three modes, not two. :meth:`set_readonly` is handed a single boolean
        because locking a toolbar needs nothing more, but this line answers a
        different question -- where does a write from this pane go? -- and
        Live and Queue are opposite answers to it. Reading the session here is
        what stops the line from promising "local" while the same toolbar
        sends straight to the router.
        """
        # The toolbar says which mode the session is in, once, for every
        # window; repeating it beside each list's Filter box was noise.
        return ""

    def _verb_tip(self, name: str) -> str:
        """What a toolbar verb does in the mode the session is actually in."""
        if self.readonly:
            return f"{name}: read-only — leave Read-only to edit"
        if self._live():
            return f"{name} selected item(s) — sent to the router now"
        return f"{name} selected item(s) (queued locally, not sent)"

    def set_readonly(self, readonly: bool) -> None:
        self.readonly = readonly
        self._set_row_drag(self._ordered and not readonly)
        for act in self._write_actions:
            act.setEnabled(not readonly)
            name = act.text() or "Action"
            tip = self._verb_tip(name)
            act.setToolTip(tip + self._key_hint(name))
            act.setStatusTip(tip)
        n = len(self._all_rows)
        self._status.setText(f"{n} row(s){self._mode_tag()}")
        # The loop above re-enabled every write verb; re-gate the selection ones.
        self._update_selection_actions()

    def reapply_icons(self) -> None:
        for act in self._write_actions:
            act.setIcon(action_icon(act.text()))
        if getattr(self, "_refresh_act", None) is not None:
            self._refresh_act.setIcon(action_icon("Refresh"))

    def refresh_metrics(self) -> None:
        """Re-read the sizes the theme publishes, after a zoom or a skin change.

        The stylesheet reaches the type and the colours on its own. These four
        do not come from it: the toolbar's icon edge, the table's own font (set
        on the widget because ``styleStrategy`` does not survive
        ``QApplication.setFont`` -- see :func:`style.apply_font_traits`), the
        row height, and the column widths the ``.jg`` declares in pixels.
        Left alone, a zoomed list draws bigger text inside the rows and columns
        it was built with.

        Re-measuring the columns does not lose a width the user dragged:
        :meth:`_autosize_columns` re-applies the saved ones last.
        """
        self._bar.setIconSize(icon_size())
        style.style_data_table(self.table, delegate=False)
        self._apply_row_heights()
        self._autosize_columns(force=True)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # First time this pane becomes visible, do its deferred initial load.
        if self._pending_first_load:
            self._pending_first_load = False
            self.reload()

    def reload(self, *, quiet: bool = False) -> None:
        if self._live_fetcher is None:
            return
        if self._worker is not None and self._worker.isRunning():
            return
        if not quiet:
            # An explicit Refresh means "read it all again", including the
            # second read a `type:lookup` column needs (ARP ▸ Bridge Port).
            client = getattr(self.edit_session, "client", None)
            forget = getattr(client, "forget_lookups", None)
            if callable(forget):
                forget()
        self._refresh_act.setEnabled(False)
        if not quiet:
            self._status.setText("loading…")
        self._worker = _FetchWorker(self._live_fetcher, parent=self)
        self._worker.finished_ok.connect(self._on_loaded)
        self._worker.finished_err.connect(self._on_error)
        self._worker.finished.connect(lambda: self._refresh_act.setEnabled(True))
        self._worker.start()

    def resume_after_reconnect(self) -> None:
        """Carry on over a new session: the old one took this list's
        subscription with it, and its last read may have failed."""
        if self._notify is not None or self._sub is not None:
            self._stop_watching()
            self._start_watching()
        self.reload(quiet=True)

    def _computed_default(self, fld) -> Any:
        """The .jg's computed add-time defaults, worked out at Add time.

        ``todaydef`` on a Scheduler start date, ``nowdef`` on its start time,
        ``randomdef`` on a MAC the user is expected to make up, ``defuser`` on
        a name field that should start as whoever is logged in.
        """
        kind = getattr(fld, "default_kind", None)
        if not kind:
            return None
        if kind == "today":
            import datetime

            today = datetime.datetime.now(datetime.UTC).date()
            return int(
                datetime.datetime(
                    today.year, today.month, today.day, tzinfo=datetime.UTC
                ).timestamp()
            )
        if kind == "now":
            import datetime

            now = datetime.datetime.now()
            return now.hour * 3600 + now.minute * 60 + now.second
        if kind == "random":
            import secrets

            # Locally administered, unicast: the range a made-up MAC belongs in.
            first = (secrets.randbelow(256) & 0xFE) | 0x02
            return bytes([first]) + secrets.token_bytes(5)
        if kind == "user":
            client = getattr(self.edit_session, "client", None)
            user = getattr(client, "user", None)
            return str(user) if user else None
        return None

    def _next_default_name(self, stem: str, name_code: int) -> str:
        """Winbox's suggested name for a new row: the stem plus the smallest
        number not already taken (vlan1, vlan2, …). ``_all_rows`` are the
        displayed string rows, so read the Name column, not a RowObject."""
        existing: set[str] = set()
        name_idx = next(
            (i for i, c in enumerate(self._columns)
             if (c or "").strip().lower() == "name"),
            None,
        )
        if name_idx is not None:
            for row in self._all_rows:
                if name_idx < len(row):
                    nm = str(row[name_idx]).strip()
                    if nm:
                        existing.add(nm)
        n = 1
        while f"{stem}{n}" in existing:
            n += 1
        return f"{stem}{n}"

    def _next_free_nameval(self, name_code: int | None) -> Any | None:
        """The same rule as :meth:`_next_default_name`, for a name that is a
        picker: the first entry of the dropdown no row has taken yet.

        30-odd menus name their rows with an `enm` read off a live table
        (``nameval:'Interface'``) rather than with a string -- a bridge port is
        its interface, a DHCP client is its interface -- so the value is the
        row's identity and the device refuses a second row holding it. Opening
        on entry one is then the one value that cannot be saved: with ether1
        already in bridge1 a new port has to open on ether2, exactly as a
        second VLAN opens on vlan2.

        The *list* is not narrowed, only the value it opens on. What a dropdown
        may hold is the .jg's business, and Bridge Port ▸ Interface declares
        one pred -- ``caps & 64``, which stays set on an interface that is
        already a port (checked on the wire: enslaving one only adds `slave`
        b10029 and the master's name). No dynamic source in 7.21 preds on
        ``slave``, so no Winbox dropdown anywhere hides a taken interface.

        None when the rule does not apply -- not a picker, no client to ask, or
        every entry is taken -- which leaves the form on entry one as before.
        """
        menu = self._menu
        if name_code is None or menu is None:
            return None
        fld = menu.fields.get(name_code)
        # A scalar enm off a live table. A string name is _next_default_name's,
        # and a multi/array name (Log Rule ▸ Topics) is a set, not an identity.
        if fld is None or (fld.jg_type or "") != "enm" or not fld.enum_path:
            return None
        client = getattr(self.edit_session, "client", None)
        if client is None:
            return None
        try:
            # The same table the property window is about to prefetch for this
            # field's dropdown, so this is that read moved a few lines earlier,
            # not an extra one.
            choices = client.enum_choices(fld)
        except Exception:  # noqa: BLE001
            return None
        taken = set()
        for raw in self._raw_rows:
            values = getattr(raw, "values_by_code", None) or {}
            if name_code in values:
                taken.add(values[name_code])
        # A queued Add has not reached the device, so it is not in _raw_rows --
        # and adding two ports before flushing the queue is the case that most
        # needs the second one to move on.
        path = tuple(self.path or ())
        for change in getattr(self.edit_session, "queue", None) or []:
            if getattr(change, "action", "") != "add":
                continue
            if tuple(getattr(change, "path", ()) or ()) != path:
                continue
            try:
                wire = change.wire_values()
            except Exception:  # noqa: BLE001
                continue
            if name_code in wire:
                taken.add(wire[name_code])
        for wire, _label in choices:
            if wire not in taken:
                return wire
        return None

    def _update_selection_actions(self, *args) -> None:
        """Remove/Enable/Disable/Comment/Move act on the selection, so grey them
        out when nothing is selected (or in read-only mode)."""
        sm = self.table.selectionModel() if hasattr(self, "table") else None
        has_sel = bool(sm.hasSelection()) if sm is not None else False
        enable = has_sel and not self.readonly
        for act in self._selection_actions:
            try:
                act.setEnabled(enable)
            except RuntimeError:
                pass
        self._update_jg_buttons()

    def _install_jg_toolbar(self) -> None:
        """Build the buttons this menu declares for its own toolbar.

        Two shapes, both ``contextmenu`` entries naming a menu to open:

        * inside a ``type:toolbar`` -- Winbox's Quick Set window keeps its verbs
          there: Apply Configuration, Port Mapping, and a System group holding
          Reboot, Reset Configuration, Password and Check For Updates.
        * with no ``link:`` at all -- the related list every tab of a window
          offers next to Add/Remove: Interface List ▸ Lists, VXLAN ▸ VTEP and
          FDB, MACsec ▸ Profiles, Packages ▸ Check For Updates. Carrying nothing
          from a row is exactly what makes them window-level rather than
          right-click entries, so :meth:`row_menu_for` leaves them out.

        Deferred to the first load, like the reordering buttons: the menu is
        not known when the toolbar is built.
        """
        if self._jg_toolbar_done or self._menu is None:
            return
        if self._on_context_action is None:
            return
        self._jg_toolbar_done = True
        entries = [
            a for a in (getattr(self._menu, "context_actions", None) or [])
            if a.get("name") and a.get("open")
            and (a.get("toolbar") or not a.get("link"))
        ]
        if not entries:
            return
        flat: list[dict] = []
        groups: dict[str, list[dict]] = {}
        for act in entries:
            # Only a toolbar's group is a dropdown. On a window-level entry it
            # names the main-menu folder the target lives in -- Packages ▸
            # Check For Updates says System -- and Winbox v3 draws those as
            # plain buttons, not a "System ▾" holding two verbs.
            group = act.get("group") if act.get("toolbar") else None
            if group:
                groups.setdefault(str(group), []).append(act)
            else:
                flat.append(act)
        for act in flat:
            btn = self._jg_toolbar_button(str(act["name"]), [act])
            self._jg_buttons.append((btn, act))
        for group, acts in groups.items():
            btn = self._jg_toolbar_button(group, acts, grouped=True)
            # A group is shown while any of its entries applies.
            self._jg_buttons.append((btn, {"group_of": acts}))
        self._update_jg_buttons()

    def _index_rate_columns(self) -> None:
        """Find where this list's rate columns sit in a rendered row.

        A row is ``[flags] + [index?] + one cell per column``, so the offset is
        the whole of the arithmetic -- and it has to be redone whenever the
        column set moves.
        """
        self._rate_cols = []
        # The client hands over the column set it rendered with; without one
        # (or with an older client that has no list_column_defs) the schema's
        # own answer is the same list, and is what table_rows used.
        cols = self._col_defs
        if cols is None and self._menu is not None:
            try:
                cols = sch.list_columns(self._menu)
            except Exception:  # noqa: BLE001
                # Not a schema Menu (a stand-in, an older cache): there are no
                # rate columns to find, and a list with no rates is every list
                # but a handful.
                cols = None
        offset = 1 + (1 if self._ordered else 0)
        self._ticking_cols = set()
        for i, col in enumerate(cols or []):
            if getattr(col, "kind", "") == "rate" and col.rate_code is not None:
                self._rate_cols.append((offset + i, col))
                self._ticking_cols.add(offset + i)
                continue
            fld = getattr(col, "field", None)
            if fld is not None and fld.ro and fld.jg_type in _COUNTER_TYPES:
                self._ticking_cols.add(offset + i)

    def _fill_rates(self, src: int, raw: Any, values: list[str]) -> None:
        """Overwrite this row's rate cells from the previous sample.

        The device sends a counter, not a rate: Winbox's Packet Rate is how
        much Packets moved since the last reply, over how long that took. So
        the first sighting of a row shows nothing -- there is no interval yet
        -- and every later one is a difference. A counter that went backwards
        (the row was replaced, or the device reset it) starts again rather than
        reporting a negative rate.
        """
        if not self._rate_cols or raw is None:
            return
        now = time.monotonic()
        key = self._row_id(raw, src)
        prev = self._rate_seen.get(key)
        cur: dict[int, tuple[int, float]] = {}
        for cell, col in self._rate_cols:
            code = col.rate_code
            try:
                value = raw.raw_value(code)
            except Exception:  # noqa: BLE001
                value = None
            if not isinstance(value, int):
                continue
            cur[code] = (value, now)
            if cell >= len(values):
                continue
            was = (prev or {}).get(code)
            values[cell] = "" if was is None else _rate_text(col, was, value, now)
        if cur:
            self._rate_seen[key] = {**(prev or {}), **cur}

    def _forget_stale_rates(self, keys: set) -> None:
        """Drop samples for rows this list no longer holds.

        Without it a long-lived Connections window accumulates one entry per
        connection it has ever seen.
        """
        for gone in [k for k in self._rate_seen if k not in keys]:
            del self._rate_seen[gone]

    def _disable_add(self, why: str) -> None:
        """Grey the Add button out, with the reason on it."""
        self._disable_verb("Add", why)

    def _disable_verb(self, name: str, why: str) -> None:
        """Grey one toolbar verb out for good, with the reason on it.

        For good, meaning it comes off both lists that switch verbs back on:
        `set_readonly` re-enables every write action when the session leaves
        read-only, and `_update_selection_actions` re-enables every selection
        action as soon as a row is picked. A verb the .jg says this menu does
        not have is neither -- it is not a verb at all here.
        """
        for act in list(self._write_actions):
            try:
                if act.text() != name:
                    continue
            except RuntimeError:
                continue
            act.setEnabled(False)
            act.setToolTip(why)
            act.setStatusTip(why)
            if act in self._write_actions:
                self._write_actions.remove(act)
            if act in self._selection_actions:
                self._selection_actions.remove(act)

    def _apply_jg_verbs(self, menu) -> None:
        """Grey out the standard verbs this menu does not declare.

        Winbox omits the button outright; we grey it and say why, which is the
        same answer to "can I do this here?" and keeps the bar from shuffling
        under the cursor between one list and the next.

        Add and Remove are :attr:`Menu.can_add` / :attr:`Menu.can_remove` --
        see those for which .jg keys decide it. Enable/Disable and Comment are
        simpler: they are template chrome (``type:enable``, ``type:comment``),
        already read for the property form's own button stack, and a list whose
        rows have no Disabled flag has nothing for them to toggle.
        """
        if menu is None:
            return
        if not getattr(menu, "can_add", True):
            self._disable_add("This list is not one you add rows to")
        if not getattr(menu, "can_remove", True):
            self._disable_verb(
                "Remove", "These rows are not yours to remove"
            )
        if not getattr(menu, "has_enable", True):
            why = "These rows have nothing to enable or disable"
            self._disable_verb("Enable", why)
            self._disable_verb("Disable", why)
        if not getattr(menu, "has_comment", True):
            self._disable_verb("Comment", "These rows do not carry a comment")

    def _install_jg_filters(self) -> None:
        """Build the `type:filter` dropdowns this menu declares.

        Winbox puts them on the filter row: Firewall lists filter by Chain
        (with `static` and `dynamic` alongside the chains themselves), Address
        Lists by List, Routes by Routing Table. The choices come from another
        handler, so this waits for the first load like the toolbar does.
        """
        if self._jg_filters or self._menu is None:
            return
        specs = list(getattr(self._menu, "list_filters", None) or [])
        if not specs:
            return
        client = getattr(self.edit_session, "client", None)
        for spec in specs:
            combo = QComboBox()
            combo.addItem(f"all {spec['filteron'].lower()}s", None)
            for cond in spec.get("conds") or []:
                combo.addItem(cond["name"], ("cond", cond["on"]))
            labels: list[str] = []
            path = spec.get("path")
            if path and client is not None:
                try:
                    labels = sorted(
                        str(v) for v in (client.enum_map(tuple(path)) or {}).values()
                    )
                except Exception:  # noqa: BLE001
                    labels = []
            for label in labels:
                combo.addItem(label, ("value", label))
            combo.setToolTip(f"Show only rows whose {spec['filteron']} matches")
            combo.currentIndexChanged.connect(
                lambda _i=0: self._apply_filter(self._filter.text())
            )
            # Built on the first load, long after any guard_wheel over the
            # window: a roll over it silently narrows the list (wheel_guard).
            guard_wheel(combo)
            self._filter_row.addWidget(QLabel(f"{spec['filteron']}:"))
            self._filter_row.addWidget(combo)
            self._jg_filters.append((combo, spec))

    def _jg_filter_keeps(self, index: int, raw: Any) -> bool:
        """Whether the .jg's own filter dropdowns keep this row."""
        if not self._jg_filters:
            return True
        values = getattr(raw, "values_by_code", None) or {}
        for combo, spec in self._jg_filters:
            try:
                choice = combo.currentData()
            except RuntimeError:
                continue
            if not choice:
                continue
            kind, arg = choice
            if kind == "cond":
                try:
                    if not sch.condition_is_true(self._menu, arg, values):
                        return False
                except Exception:  # noqa: BLE001
                    continue
                continue
            # A value: compare against what the row shows for that field, which
            # is the same text the dropdown was filled with (both come from the
            # other handler's names).
            shown = ""
            name = spec["filteron"]
            code = sch.field_code_by_name(self._menu, name)
            if code is not None and raw is not None:
                shown = str(raw.get_rendered(code, prefer_name=name) or "")
            if shown.strip().lower() != str(arg).strip().lower():
                return False
        return True

    def _jg_toolbar_button(
        self, label: str, acts: list[dict], *, grouped: bool = False
    ):
        """One toolbar button: a single action, or a menu over a group."""
        btn = QToolButton()
        btn.setText(label)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setAutoRaise(True)
        if len(acts) == 1 and not grouped:
            act = acts[0]
            btn.setToolTip(f"Open {act.get('open')}")
            btn.clicked.connect(lambda _=False, a=act: self._run_jg_action(a))
        else:
            menu = QMenu(btn)
            for act in acts:
                entry = menu.addAction(str(act["name"]))
                entry.triggered.connect(
                    lambda _=False, a=act: self._run_jg_action(a)
                )
            btn.setMenu(menu)
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._insert_toolbar_widget(btn)
        return btn

    def _run_jg_action(self, act: dict) -> None:
        """Fire a toolbar action against the row it should act on.

        An entry with no ``link:`` needs no object (Reboot, Check For Updates).
        One with links takes them off the selected row -- or off the only row
        there is, which is the Quick Set case: a mode is a single object.
        """
        if self._on_context_action is None:
            return
        row = self._jg_action_row()
        if row is None and act.get("link"):
            QMessageBox.information(
                self, str(act.get("name") or "Action"),
                "Select a row first — this action carries values from it.",
            )
            return
        self._on_context_action(act, row, self._menu)

    def _jg_action_row(self):
        """The row a toolbar action acts on: the selected one, or the only one."""
        sm = self.table.selectionModel() if hasattr(self, "table") else None
        rows = sorted({i.row() for i in sm.selectedRows()}) if sm is not None else []
        if rows:
            return self._raw_for_view_row(rows[0])
        if len(self._raw_rows) == 1:
            return self._raw_rows[0]
        return None

    def _update_jg_buttons(self) -> None:
        """Hide a toolbar button whose ``on:`` condition does not hold.

        Quick Set declares Copy To ACL and Remove From ACL on the same row,
        gated on `noacl` / `acl`: exactly one of them is ever a button.
        """
        if not self._jg_buttons:
            return
        row = self._jg_action_row()
        values = getattr(row, "values_by_code", None) or {}
        for btn, act in self._jg_buttons:
            members = act.get("group_of") or [act]
            shown = False
            for member in members:
                on = member.get("on")
                if not on:
                    shown = True
                    break
                try:
                    if sch.condition_is_true(self._menu, str(on), values):
                        shown = True
                        break
                except Exception:  # noqa: BLE001
                    shown = True
                    break
            try:
                btn.setVisible(shown)
            except RuntimeError:
                pass

    def add_toolbar_action(self, name: str, tooltip: str, callback) -> None:
        """Add a labelled button to this pane's toolbar, right after the
        Add/Remove/Enable/Disable/Comment group. Used for the buttons a tab of a
        window owns (Interfaces ▸ Ethernet ▸ Power Cycle) -- Winbox v3 keeps
        those on the toolbar, not in the tab strip."""
        btn = QToolButton()
        btn.setText(name)
        btn.setToolTip(tooltip)
        btn.setStatusTip(tooltip)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        btn.setAutoRaise(True)
        btn.clicked.connect(lambda _=False: callback())
        self._insert_toolbar_widget(btn)

    def _insert_toolbar_widget(self, widget) -> None:
        """Put one extra button in the run that follows the Comment group.

        Each goes *after* the ones already there: inserting every one at the
        same anchor read the row backwards, so LTE offered Antenna Scan, Modem,
        LTE Scanner rather than the order the schema declares them in.
        """
        n = getattr(self, "_bar_extras", 0)
        acts = self._bar.actions()
        anchor = acts[5 + n] if len(acts) > 5 + n else None
        if anchor is not None:
            self._bar.insertWidget(anchor, widget)
        else:
            self._bar.addWidget(widget)
        self._bar_extras = n + 1

    def shutdown(self) -> None:
        """Retire this pane's workers safely — a still-running fetch QThread is
        reparented and kept alive (qthreads.retire), never destroyed. Called
        from closeEvent, and by a parent window on close: a pane nested in a
        tab widget gets no closeEvent of its own, so without this its running
        fetch would be destroyed with the window and abort the process."""
        self._stop_watching()
        # The client is still up and the fetch cannot be cancelled; cut its
        # signals so a late result cannot reach a dying widget, and let it run
        # itself out. What must not happen is Qt destroying it here.
        qthreads.retire(self._worker, wait_ms=50)
        self._worker = None

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def _on_loaded(self, payload: object) -> None:
        # table_rows grew a trailing uptime; tolerate the old 4-tuple.
        menu, col_names, str_rows, raw_rows = payload[:4]  # type: ignore[index]
        self._device_uptime = payload[4] if len(payload) > 4 else None  # type: ignore[index]
        self._device_uptime_at = time.monotonic()
        self._menu = menu
        self._model.set_flag_legend(_flag_legend(menu))
        # Cached for _apply_push: re-rendering one pushed row must build the
        # same cells table_rows() did, and asking the schema per frame is waste.
        self._col_defs = None
        client = getattr(self.edit_session, "client", None)
        defs = getattr(client, "list_column_defs", None)
        if defs is not None and menu is not None:
            try:
                self._col_defs = defs(menu)
            except Exception:  # noqa: BLE001
                self._col_defs = None
        self._base_columns = list(col_names)
        self._all_rows = [list(r) for r in str_rows]
        self._raw_rows = list(raw_rows)
        # Rate columns arrive from the device as "", because one reply cannot
        # say how fast anything is moving. Fill them from the previous read,
        # and forget the rows that are no longer here.
        self._index_rate_columns()
        if self._rate_cols:
            live = set()
            for i, raw in enumerate(self._raw_rows):
                live.add(self._row_id(raw, i))
                self._fill_rates(i, raw, self._all_rows[i])
            self._forget_stale_rates(live)
        self._all_comments = [self._comment_of(r) for r in self._raw_rows]
        self._all_info = [self._info_of(r) for r in self._raw_rows]
        self._col_secret = self._secret_flags(menu)
        self._comment_available = self._comments_supported(menu)
        if not self._ordered and getattr(menu, "ordered", False):
            self._show_reordering(True)
        # `nonaddable` / `ro` / `removable` / `type:enable` / `type:comment` /
        # `nonmovable`: the .jg says which of the five standard verbs each list
        # actually has, and Winbox draws only those. IP ▸ Services has Enable
        # and Disable alone -- eight fixed rows, neither added nor removed;
        # Active Users has Remove alone -- you can drop a session but not
        # create one, and there is nothing on it to enable; Neighbors and Log
        # have none of them.
        self._apply_jg_verbs(menu)
        if not getattr(menu, "movable", True) and self._ordered:
            self._show_reordering(False)
        self._install_jg_toolbar()
        self._install_jg_filters()
        # `maxobjs`: the client stops reading at the cap the .jg sets, and the
        # list has to say so rather than looking complete.
        client = getattr(self.edit_session, "client", None)
        note = getattr(client, "last_truncated", None)
        self._truncated = note() if callable(note) else None
        self._hide_extra_columns_by_default(menu)
        self._start_watching()
        self._note_arrivals()
        self._note_content()
        self._rebuild_view()
        self._decide_auto_refresh()
        self._ensure_age_timer()
        tag = self._mode_tag()
        fresh = len(self._fresh_ids)
        note = f" — {fresh} new" if fresh else ""
        # A `maxobjs` cap means the list is not the whole table; say which.
        capped = f" — {self._truncated}" if getattr(self, "_truncated", None) else ""
        self._status.setText(
            f"{len(self._all_rows)} row(s){tag}{note}{capped}"
            f"{self._paused_suffix()}"
        )
        self._speak_count(len(self._all_rows))

    def _speak_count(self, n: int) -> None:
        """Say how many rows there are -- when that is news, and not before.

        A live list re-reads on a timer, so announcing the status line every
        time it is written would leave a window talking to itself for as long
        as it is open. A count that has actually moved is worth interrupting
        for (an interface came up, a lease expired), and so is the first load:
        it is the answer to "what did I just open?".
        """
        if n == self._said_count:
            return
        self._said_count = n
        a11y.announce(self, f"{self.title}: {n} {'row' if n == 1 else 'rows'}")

    def _start_watching(self) -> None:
        """Subscribe to this menu so another session's edits show up here.

        Without it a list is only as fresh as the last time something asked. A
        change made in another Winbox — or by the device itself — left this
        window showing stale rows until the user hit Refresh.

        The subscription also carries an interval, which is how Winbox runs a
        live list: ``0xFF0012 = 1000`` on the Interface list and the device
        pushes its counters every second, so the rates come to us instead of
        being fetched. The poll timer stands down for as long as those pushes
        keep arriving (see :meth:`_note_push`).
        """
        if not self._live_updates or self._notify is not None or self.path is None:
            return
        client = getattr(self.edit_session, "client", None)
        watch = getattr(client, "watch", None)
        if watch is None:
            return
        try:
            self._sub, self._sub_from = watch(
                tuple(self.path), **self._watch_kwargs()
            )
        except TypeError:
            # An older client with no interval support: a plain change watch.
            try:
                self._sub, self._sub_from = watch(tuple(self.path))
            except Exception:  # noqa: BLE001
                self._sub = None
                return
        except Exception:  # noqa: BLE001
            self._sub = None          # handler does not support it; polling stays
            return
        self._notify = _NotifyWorker(self._sub, parent=self)
        self._notify.pushed.connect(self._on_pushed)
        self._notify.start()

    def _watch_kwargs(self) -> dict:
        """Record mask and push interval for this menu's subscription.

        The mask is the menu's bare ``refreshfilter`` -- 0x4000A on Interface,
        a plain 5 where none is declared -- and not the wider one a full read
        sends; that is what the capture shows Winbox subscribing with. No
        interval when the poll is switched off: then the subscription goes back
        to meaning "tell me when something changes".
        """
        menu = self._menu
        mask = getattr(menu, "refresh_filter", None) if menu is not None else None
        if not mask:
            mask = SUBSCRIBE_MASK_BASE
        secs = self._menu_refresh_secs()
        interval = int(secs * 1000) if secs > 0 else None
        self._push_interval_ms = interval
        return {"mask": int(mask), "interval_ms": interval}

    def _stop_watching(self) -> None:
        worker, self._notify = self._notify, None
        if worker is not None:
            worker.stop()
        client = getattr(self.edit_session, "client", None)
        if self._sub is not None and self._sub_from is not None:
            unwatch = getattr(client, "unwatch", None)
            if unwatch is not None:
                try:
                    unwatch(tuple(self.path or ()), self._sub, from_path=self._sub_from)
                except Exception:  # noqa: BLE001
                    pass
        self._sub = None
        self._sub_from = None
        self._push_seen = False
        self._push_interval_ms = None
        # No feed to wait for any more: back to this list's own cadence.
        if self._poll_timer is not None and self._poll_base_ms:
            self._poll_timer.setInterval(self._poll_base_ms)
        qthreads.retire(worker, wait_ms=1500)

    def _on_pushed(self, msg: object) -> None:
        """A push arrived: patch it in, or re-read when it cannot be."""
        self._note_push()
        if self._apply_push(msg):
            self._pushes_since_read += 1
            if self._pushes_since_read >= PUSH_REREAD_EVERY:
                self._pushes_since_read = 0
                self.reload(quiet=True)
            return
        self._on_device_changed()

    def _on_device_changed(self) -> None:
        """Something we cannot patch. Coalesce a burst into one re-read."""
        if self._notify_timer is None:
            self._notify_timer = QTimer(self)
            self._notify_timer.setSingleShot(True)
            self._notify_timer.timeout.connect(self._reload_from_push)
        # A single edit can push several frames; one reload covers them all.
        self._notify_timer.start(400)

    def _reload_from_push(self) -> None:
        self._pushes_since_read = 0
        self.reload(quiet=True)

    def _note_push(self) -> None:
        """The device fed us, so the next poll can wait a little longer.

        Deferring the poll rather than standing it down. This used to stop the
        poll dead on the first push and leave the job to a three-interval
        silence watchdog, which made the subscription a bet on the device
        honouring the interval it was given. A 10-port router on 7.21 does not: on the
        wire it answers SUBSCRIBE with exactly one NOTIFY, 30ms later, and then
        pushes nothing more. So opening the Interface window read the list,
        subscribed, took that one NOTIFY as a live feed, and showed still
        counters for 4.2s -- the watchdog's 3s plus a 1s poll -- before anything
        moved.

        A deferred poll costs nothing while a feed really is running, because
        every push pushes it back before it can fire, and costs one interval of
        staleness when the feed is imaginary.
        """
        self._push_seen = True
        if self._poll_timer is None or not self._poll_base_ms:
            return  # polling is off (not live, or the user turned it off)
        interval = self._push_interval_ms or self._poll_base_ms
        self._poll_timer.setInterval(int(interval * PUSH_GRACE))
        self._poll_timer.start()

    def _source_by_object_id(self) -> dict:
        """Device object id -> index into the pane's unfiltered rows."""
        out: dict = {}
        for i, raw in enumerate(self._raw_rows):
            if raw is None:
                continue
            try:
                oid = raw.object_id()
            except Exception:  # noqa: BLE001
                oid = None
            if oid is not None:
                out[oid] = i
        return out

    def _apply_push(self, msg: object) -> bool:
        """Fold a pushed record into the rows already on screen.

        True when every record in the push landed on a row we hold. That is
        the 1 Hz counter feed: the same objects with new numbers, which patch
        in as one repaint each -- no round trip, no model reset, and nothing
        for the selection or the scroll position to survive.

        False for anything else, and the caller re-reads. A push carrying a
        row we have never seen is the list itself changing, and a push with no
        records at all says only "look again"; neither can be answered from
        what we already have. A row *removed* elsewhere is the one case this
        cannot see -- the record still names an id we know -- so a merged run
        re-reads every PUSH_REREAD_EVERY pushes regardless.
        """
        client = getattr(self.edit_session, "client", None)
        menu, path = self._menu, self.path
        if client is None or menu is None or path is None:
            return False
        decode = getattr(client, "notify_objects", None)
        render = getattr(client, "render_row", None)
        if decode is None or render is None:
            return False
        try:
            objs = decode(tuple(path), msg)
        except Exception:  # noqa: BLE001
            return False
        if not objs:
            return False

        by_id = self._source_by_object_id()
        touched: list[int] = []
        for obj in objs:
            try:
                oid = obj.object_id()
            except Exception:  # noqa: BLE001
                oid = None
            src = by_id.get(oid) if oid is not None else None
            if src is None or src >= len(self._raw_rows):
                return False
            held = self._raw_rows[src]
            merge = getattr(held, "merge", None)
            if merge is None:
                return False
            merge(obj)
            touched.append(src)

        inline = self._inline and self._comment_available
        view_of = {
            self._model.source_row(v): v for v in range(self._model.rowCount())
        }
        for src in touched:
            try:
                values = render(
                    menu, self._raw_rows[src],
                    cols=self._col_defs,
                    index=src if self._ordered else None,
                )
            except Exception:  # noqa: BLE001
                return False
            if len(values) != len(self._all_rows[src]):
                return False  # the column set moved: a rebuild, not a patch
            # The 1 Hz counter feed is where a rate is actually worth having,
            # so a patched row recomputes its own rather than blanking it.
            self._fill_rates(src, self._raw_rows[src], values)
            self._all_rows[src] = values
            view_row = view_of.get(src)
            if view_row is None:
                continue  # filtered out of the view; the value is kept anyway
            comment = (
                self._all_comments[src] if src < len(self._all_comments) else ""
            )
            self._model.replace_row(
                view_row, values + ([comment] if inline else [])
            )
        self._note_content()
        return True

    def _note_content(self) -> None:
        """Compare the config content of these rows with what we last saw.

        The two callers are the only points where row content settles: a
        completed load, and a push patched in place. Doing it here rather than
        per-push is what makes an add, a remove and a field edit all detectable
        by the same code -- and what keeps a 1 Hz counter feed invisible, since
        the fingerprint only covers fields the schema calls configuration.
        """
        if self._menu is None:
            return
        try:
            now = change_watch.snapshot(self._menu, self._raw_rows)
        except Exception:  # noqa: BLE001
            return
        delta = change_watch.diff(self._content, now)
        self._content = now
        if delta:
            # What the router would export has moved: ask again next hover.
            self._cli_lines.clear()
            self._cli_failed.clear()
            self.content_changed.emit(self, delta)

    def _paused_suffix(self) -> str:
        return ("" if self._live_updates
                else "  ·  paused — not live (Refresh to update)")

    def _on_live_toggled(self, checked: bool) -> None:
        """Live toolbar toggle: resume or pause this window's live updates
        (subscription + auto-poll). Paused = no bandwidth while idle."""
        self._live_updates = checked
        if checked:
            self._start_watching()
            self._auto_refresh_decided = False
            self._decide_auto_refresh()
            self.reload(quiet=True)
        else:
            self._stop_watching()
            self.set_auto_refresh(0)
            self._status.setText(
                f"{len(self._all_rows)} row(s){self._mode_tag()}"
                f"{self._paused_suffix()}"
            )

    def _hide_extra_columns_by_default(self, menu) -> None:
        """Start with Winbox's column set, keep the rest one click away.

        The device now decodes every field the menu declares, so Dst. Port and
        friends can be switched on from the header menu. Showing all ~40 at
        once would be unusable, so anything past the default set starts
        hidden — unless this pane has saved preferences, which are the user's
        own choice and must win.

        The default set is the .jg's own: every field it gives a ``width``.
        """
        if menu is None or self._columns_defaulted:
            return
        self._columns_defaulted = True
        try:
            from x3.proto.schema import default_column_names

            keep = set(default_column_names(menu))
        except Exception:  # noqa: BLE001
            return
        # Columns this pane has shown before, per its saved preferences. A
        # column outside both that and the default set is one the user has
        # never seen, so it starts hidden. Testing "have they saved anything?"
        # was not enough: every list already had `hidden: []` on disk from when
        # only the eight preferred columns existed, which read as "keep showing
        # everything" once all fifty-seven were carried.
        known = set(self._prefs.get("order") or ())
        known |= set(self._prefs.get("hidden") or ())
        known |= set(self._prefs.get("widths") or {})
        from x3.proto.schema import INDEX_LABEL

        for name in self._base_columns:
            if not name or name in (FLAGS_LABEL, INDEX_LABEL):
                continue  # the rule number is the point of an ordered list
            if name not in keep and name not in known:
                self._hidden_cols.add(name)

    #: Poll cadence for time-varying lists (leases, bindings). Fast enough that
    #: an arrival is still visibly fresh when it first shows up.
    AUTO_REFRESH_SECS = 5.0

    def _show_reordering(self, on: bool) -> None:
        """Show or hide the Move Up/Down pair, and with it row dragging.

        Called once from the toolbar build, where the table does not exist
        yet — __init__ sets the drag state itself once it does.
        """
        self._ordered = bool(on)
        for act in (self._move_sep, self._move_up_act, self._move_down_act):
            if act is not None:
                act.setVisible(bool(on))
        if getattr(self, "table", None) is not None:
            self._set_row_drag(self._ordered and not self.readonly)

    def _decide_auto_refresh(self) -> None:
        """Start polling when the schema says this list changes on its own."""
        if self._auto_refresh_decided or self._live_fetcher is None:
            return
        self._auto_refresh_decided = True
        secs = self._auto_refresh_secs
        if secs is None:
            secs = self._menu_refresh_secs()
        if not self._live_updates:
            secs = 0.0  # low-bandwidth: no auto-poll until Live is re-enabled
        # A subscription does not switch polling off any more: it defers each
        # poll instead, so a feed that stops being a feed costs one interval
        # rather than four seconds (see _note_push).
        self.set_auto_refresh(secs)

    def _menu_refresh_secs(self) -> float:
        """Poll cadence for this list: the user's setting, else the .jg's own.

        Winbox re-reads a list at the rate its menu declares -- ``autorefresh``
        is 1000ms on Interface, 2000 on the firewall chains, 5000 on
        Connections -- and that is what makes the counters look live. A list
        that declares no rate keeps the old rule: poll only if it carries an
        age field, which is how a lease that came or went shows up. The age
        columns themselves no longer need the poll -- they tick off the local
        clock (see :meth:`_tick_ages`).
        """
        override = settings.load_list_refresh_secs()
        if override is not None:
            return max(0.0, float(override))
        menu = self._menu
        ms = getattr(menu, "autorefresh_ms", None) if menu is not None else None
        if ms:
            # A menu asking for 100ms (Flood Ping) would have us re-reading a
            # whole list ten times a second; a quarter second is as fast as a
            # full re-read is worth doing.
            return max(0.25, int(ms) / 1000.0)
        live = bool(
            menu is not None
            and getattr(menu, "has_age_field", False)
            and getattr(menu, "is_collection", False)
        )
        return self.AUTO_REFRESH_SECS if live else 0.0

    def apply_refresh_setting(self) -> None:
        """Re-read the poll cadence after the preference changed.

        Open windows have already decided theirs, so the one-shot guard has to
        come off or the new setting would only reach lists opened later. The
        subscription carries the interval too, so it is re-opened at the new
        one rather than left running at the old.
        """
        self._auto_refresh_decided = False
        self._push_seen = False
        if self._live_updates and self._notify is not None:
            self._stop_watching()
            self._start_watching()
        self._decide_auto_refresh()

    # --- the clock the age columns are measured against ------------------

    #: How often a ``type:'age'`` column is redrawn from the local clock.
    #: Winbox's Expires After counts down once a second on a list (DHCP
    #: Leases) whose .jg asks for no autorefresh at all, so the countdown
    #: cannot be coming from a re-read -- and it is not: the stamp is fixed and
    #: only the clock it is measured against moves. One second per second.
    AGE_TICK_MS = 1000

    def _ensure_age_timer(self) -> None:
        """Start ticking the age columns, if this list has any.

        Costs nothing on the wire, so it runs in low-bandwidth mode too: the
        device is not being asked anything, the local clock is.
        """
        menu = self._menu
        if (
            menu is None
            or self._device_uptime is None
            or not getattr(menu, "has_age_field", False)
            or not self._raw_rows
        ):
            return
        if self._age_timer is None:
            self._age_timer = QTimer(self)
            self._age_timer.setInterval(self.AGE_TICK_MS)
            self._age_timer.timeout.connect(self._tick_ages)
        if not self._age_timer.isActive():
            self._age_timer.start()

    def _device_uptime_now(self) -> float | None:
        """The device's uptime clock as it reads *now*, carried locally.

        The reading came with the last load; a clock that advances at one
        second per second needs no round trip to be run forward.
        """
        if self._device_uptime is None:
            return None
        if self._device_uptime_at is None:
            return self._device_uptime
        return self._device_uptime + max(
            0.0, time.monotonic() - self._device_uptime_at
        )

    def _tick_ages(self) -> None:
        """Redraw the age cells of every row whose text moved.

        Same object, later clock: nothing is read, nothing is merged, and the
        rows that did not change (an age in whole minutes, a stamp the device
        left unset) are not repainted at all.
        """
        menu, uptime = self._menu, self._device_uptime_now()
        if menu is None or uptime is None or not self._raw_rows:
            if self._age_timer is not None:
                self._age_timer.stop()
            return
        client = getattr(self.edit_session, "client", None)
        render = getattr(client, "render_row", None)
        if render is None:
            return
        inline = self._inline and self._comment_available
        view_of = {
            self._model.source_row(v): v for v in range(self._model.rowCount())
        }
        for src, raw in enumerate(self._raw_rows):
            if src >= len(self._all_rows):
                break
            retime = getattr(raw, "retime_ages", None)
            if retime is None:
                return  # not a RowObject: nothing here can tick
            try:
                if not retime(menu, uptime):
                    continue
            except Exception:  # noqa: BLE001
                return
            try:
                values = render(
                    menu, raw,
                    cols=self._col_defs,
                    index=src if self._ordered else None,
                )
            except Exception:  # noqa: BLE001
                return
            if len(values) != len(self._all_rows[src]):
                return  # the column set moved: a rebuild, not a patch
            # A rate is a difference between two replies and this is neither --
            # recomputing one against the sample it was already computed from
            # would report 0. The cell keeps what the last read put in it.
            for cell, _col in getattr(self, "_rate_cols", ()) or ():
                if cell < len(values):
                    values[cell] = self._all_rows[src][cell]
            self._all_rows[src] = values
            view_row = view_of.get(src)
            if view_row is None:
                continue  # filtered out of the view; the value is kept anyway
            comment = (
                self._all_comments[src] if src < len(self._all_comments) else ""
            )
            self._model.replace_row(
                view_row, values + ([comment] if inline else [])
            )

    # --- arrival highlight ---------------------------------------------

    def _row_id(self, raw: Any, index: int) -> Any:
        """Stable identity for a device row across polls."""
        if raw is not None:
            oid = None
            try:
                oid = raw.object_id()
            except Exception:  # noqa: BLE001
                oid = None
            if oid is not None:
                return ("oid", oid)
        # No object id: fall back to the rendered row, which at least detects
        # arrivals for handlers that do not hand out ids.
        if index < len(self._all_rows):
            return ("row", tuple(str(c) for c in self._all_rows[index]))
        return ("idx", index)

    #: Field names whose ``type:'age'`` stamp marks when an object came into
    #: existence. Deliberately excludes 'Last Seen' -- that one is refreshed on
    #: every DHCP renew, so a client connected for a week reads as brand new
    #: seconds after it renews. 'Expires After' is a future stamp, not an
    #: arrival.
    ARRIVAL_FIELD_NAMES = frozenset({"age", "uptime"})

    def _device_arrival_age_of(self, raw: Any) -> float | None:
        """Seconds since this object came into existence, per the device's clock.

        Lets a window opened just after a client joined still show it as new.
        Returns None when the device does not say -- which is most DHCP leases,
        since the router leaves the lease ``Age`` field unset on the majority of them.
        """
        if raw is None or self._device_uptime is None or self._menu is None:
            return None
        try:
            from x3.proto import message as m

            codes = self._menu.age_codes()
        except Exception:  # noqa: BLE001
            return None
        best: float | None = None
        for code in codes:
            fld = self._menu.fields.get(code)
            if fld is None:
                continue
            variants = [fld, *(fld.variants or {}).values()]
            arrival = [
                v for v in variants
                if v.jg_type == "age"
                and (v.name or "").strip().lower() in self.ARRIVAL_FIELD_NAMES
            ]
            if not arrival:
                continue
            for tag, val in (raw.values_by_code_multi.get(code) or []):
                if tag != m.T_U32 or not isinstance(val, int):
                    continue
                # Unset sentinels and previous-boot stamps say nothing.
                if val >= 0x80000000:
                    continue
                delta = self._device_uptime - val
                if delta < 0:
                    continue
                best = delta if best is None else min(best, delta)
        return best

    def _note_arrivals(self) -> None:
        """Stamp rows that appeared since the last poll, keyed by object id."""
        if not self._highlight_arrivals:
            return
        now = time.monotonic()
        ids = [self._row_id(raw, i) for i, raw in enumerate(self._raw_rows)]
        current = set(ids)
        if self._known_ids is None:
            # First load: nothing is "new" just because the window opened. Only
            # trust the device's own age, when it offers one.
            for rid, raw in zip(ids, self._raw_rows):
                age = self._device_arrival_age_of(raw)
                if age is not None and age < FRESH_SECONDS:
                    self._fresh_ids[rid] = now - age
        else:
            for rid, raw in zip(ids, self._raw_rows):
                if rid in self._known_ids or rid in self._fresh_ids:
                    continue
                age = self._device_arrival_age_of(raw)
                start = now - age if (age is not None and age < FRESH_SECONDS) else now
                self._fresh_ids[rid] = start
        self._known_ids = current
        # Forget rows that aged out or left the list.
        self._fresh_ids = {
            rid: t for rid, t in self._fresh_ids.items()
            if rid in current and (now - t) < FRESH_SECONDS
        }

    def _apply_freshness(self, sources: list[int]) -> None:
        """Re-key the model's fade state onto the current view rows."""
        if not self._highlight_arrivals or not self._fresh_ids:
            return
        now = time.monotonic()
        for view_row, src in enumerate(sources):
            raw = self._raw_rows[src] if src < len(self._raw_rows) else None
            started = self._fresh_ids.get(self._row_id(raw, src))
            if started is None:
                continue
            self._model.mark_fresh(view_row, age=now - started)
        self._ensure_fade_timer()

    def _ensure_fade_timer(self) -> None:
        if not self._model.has_fresh_rows():
            return
        if self._fade_timer is None:
            self._fade_timer = QTimer(self)
            self._fade_timer.setInterval(_FADE_MS)
            self._fade_timer.timeout.connect(self._tick_fade)
        if not self._fade_timer.isActive():
            self._fade_timer.start()

    def _tick_fade(self) -> None:
        self._model.repaint_fresh()
        if not self._model.has_fresh_rows() and self._fade_timer is not None:
            self._fade_timer.stop()

    def set_auto_refresh(self, secs: float) -> None:
        """Poll the device every ``secs`` seconds; 0 disables."""
        if secs <= 0:
            self._poll_base_ms = None
            if self._poll_timer is not None:
                self._poll_timer.stop()
            return
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_tick)
        self._poll_base_ms = max(1, int(secs * 1000))
        self._poll_timer.setInterval(self._poll_base_ms)
        self._poll_timer.start()

    def _poll_tick(self) -> None:
        """Background poll: skip while hidden so buried windows cost nothing."""
        # A push may have widened the interval to wait the feed out; once the
        # poll has had to fire, the cadence is ours again (see _note_push).
        if self._poll_base_ms and self._poll_timer is not None:
            self._poll_timer.setInterval(self._poll_base_ms)
        if not self.isVisible():
            return
        self.reload(quiet=True)

    @staticmethod
    def _comment_of(raw: Any) -> str:
        try:
            return raw.comment() if hasattr(raw, "comment") else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _info_of(raw: Any) -> tuple[str, ...]:
        try:
            return raw.info_lines() if hasattr(raw, "info_lines") else ()
        except Exception:  # noqa: BLE001
            return ()

    def _has_info(self) -> bool:
        return any(self._all_info)

    def _secret_flags(self, menu) -> list[bool]:
        """One flag per base column: does it hold a password we should mask?"""
        by_name: dict[str, bool] = {}
        if menu is not None:
            try:
                from x3.proto.schema import all_columns, is_secret

                by_name = {f.name: is_secret(f) for f in all_columns(menu) if f.name}
            except Exception:  # noqa: BLE001
                by_name = {}
        # Align by name, not by position. The table carries synthetic columns
        # the schema knows nothing about — the flags column, and the index on
        # ordered lists — so counting positions went out of step and the
        # length check then disabled masking entirely.
        hints = ("password", "secret", "passphrase", "psk", "private key")
        return [
            by_name.get(name, any(h in (name or "").lower() for h in hints))
            for name in self._base_columns
        ]

    def _comments_supported(self, menu) -> bool:
        if menu is None:
            return any(self._all_comments)
        if not getattr(menu, "has_comment", False):
            return False
        # Two menus declare id:'sfe0009' themselves and already get a real
        # column for it; a synthetic one would duplicate it.
        from x3.proto.device import STD_DESCR

        fields = getattr(menu, "fields", None) or {}
        try:
            return STD_DESCR not in fields
        except TypeError:
            return True

    # --- view assembly -------------------------------------------------
    # _rebuild_view is the only writer of the model's rows and columns, so the
    # comments and the view→source mapping can never drift out of step.

    def _view_columns(self) -> tuple[list[str], list[bool]]:
        cols = list(self._base_columns)
        secret = list(self._col_secret)
        if self._inline and self._comment_available:
            cols.append("Comment")
            secret.append(False)
        return cols, secret

    def _filter_fields(self) -> list[str]:
        """What the filter rows can test: every column the list has, shown or
        hidden, by the name its header gives it."""
        seen: list[str] = []
        for name in self._base_columns or []:
            if name and name not in ("Flags", " ") and name not in seen:
                seen.append(name)
        return seen

    def _filter_values(self, name: str) -> list[str]:
        """The values a column holds right now, for the value dropdown."""
        try:
            col = list(self._base_columns).index(name)
        except ValueError:
            return []
        if col < len(self._col_secret) and self._col_secret[col] and not self._show_pw:
            return []    # no menu of passwords
        found = {str(r[col]).strip() for r in self._all_rows if col < len(r)}
        return sorted((v for v in found if v), key=str.casefold)[:300]

    def _on_find_toggled(self, on: bool) -> None:
        self._find_row.setVisible(on)
        if on:
            self._filter.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self._filter.selectAll()
        elif self._filter.text():
            # Hidden, it must not go on narrowing the list out of sight.
            self._filter.clear()

    def _on_filter_toggled(self, on: bool) -> None:
        self._filter_panel.setVisible(on)
        if on and not self._filter_panel.rows:
            self._filter_panel.add_row(focus=True)
        self._rebuild_view()

    def _filter_rules(self) -> list[tuple[int, str, str]]:
        """The panel's conditions against this list's own column order."""
        index = {n: i for i, n in enumerate(self._base_columns or [])}
        return [
            (index[name], op, value)
            for name, op, value in self._filter_panel.rules()
            if name in index
        ]

    def _view_rows(self) -> tuple[list[list[Any]], list[str], list[int]]:
        needle = self._filter.text().strip().lower()
        from .filter_rows import row_matches

        rules = self._filter_rules() if hasattr(self, "_filter_panel") else []
        inline = self._inline and self._comment_available
        rows: list[list[Any]] = []
        comments: list[str] = []
        sources: list[int] = []
        for i, row in enumerate(self._all_rows):
            comment = self._all_comments[i] if i < len(self._all_comments) else ""
            if needle:
                # Skip masked cells: matching on a hidden password would turn the
                # filter box into an oracle for it. `nofilter` fields are left
                # out too -- the .jg says which ones the filter ignores.
                # Nor the counters: a Firewall rule's Packets reading 1383 one
                # second and 1391 the next made "38" find the rule, then lose
                # it, then find it again. Find is for what a row *is*.
                skip = self._nofilter_columns() | self._ticking_cols
                haystack = [
                    str(cell)
                    for j, cell in enumerate(row)
                    if (self._show_pw
                        or not (j < len(self._col_secret) and self._col_secret[j]))
                    and j not in skip
                ]
                haystack.append(comment)
                # Status lines are on screen, so the filter has to see them:
                # typing "capsman" should find the interfaces that say so.
                haystack.extend(self._all_info[i] if i < len(self._all_info) else ())
                if not any(needle in h.lower() for h in haystack):
                    continue
            if self._jg_filters:
                raw = self._raw_rows[i] if i < len(self._raw_rows) else None
                if not self._jg_filter_keeps(i, raw):
                    continue
            if rules and not row_matches(row, rules):
                continue
            rows.append(list(row) + [comment] if inline else list(row))
            comments.append(comment)
            sources.append(i)
        return rows, comments, sources

    def _nofilter_columns(self) -> set[int]:
        """Column indexes the .jg keeps out of the filter (`nofilter`)."""
        if self._menu is None:
            return set()
        names = {
            (f.name or "") for f in self._menu.fields.values()
            if getattr(f, "no_filter", False)
        }
        if not names:
            return set()
        return {i for i, c in enumerate(self._columns) if c in names}

    def _apply_sort(
        self,
        cols: list[str],
        secret: list[bool],
        rows: list[list[Any]],
        comments: list[str],
        sources: list[int],
    ) -> tuple[list[list[Any]], list[str], list[int]]:
        """Permute the view by the active sort column. Device order if none.

        Only the view moves: `sources` is permuted alongside the rows, so every
        view row still points at the object it came from and nothing is
        reordered on the router.
        """
        if self._sort_col is None or not rows:
            return rows, comments, sources
        col = self._view_index_of(cols, self._sort_col)
        if col is None:
            return rows, comments, sources

        masked = (
            not self._show_pw and col < len(secret) and secret[col]
        )
        if masked:
            # Every cell renders as the same mask, so ordering by the real
            # values would turn the header into an oracle for them — the same
            # reason the filter skips masked cells.
            return rows, comments, sources

        order = sorted(
            range(len(rows)),
            key=lambda r: sort_key(rows[r][col] if col < len(rows[r]) else None),
            reverse=self._sort_desc,
        )
        return (
            [rows[i] for i in order],
            [comments[i] for i in order],
            [sources[i] for i in order],
        )

    #: Source index for a row a queued Add will create: past any real row, so
    #: every per-row lookup (raw object, info lines, icons) finds nothing.
    _PENDING_SOURCE = 10_000_000

    def _overlay_queue(self, cols, rows, comments, sources):
        """Draw what the queue holds for this list over the router's rows.

        The queue is the one record of what will change, so the list is redrawn
        from it on every rebuild rather than patched once: a poll that brought
        the router's rows back used to wipe out a queued comment, and a queued
        Add never showed at all. Rows a change touches come back with
        ``{view_row: (kind, tooltip)}`` for the model to tint.
        """
        session = self.edit_session
        mine = [
            c for c in (getattr(session, "queue", None) or [])
            if tuple(c.path) == tuple(self.path or ())
        ] if session is not None and not getattr(session, "live", False) else []
        if not mine:
            return rows, comments, sources, {}
        index = {name: i for i, name in enumerate(cols) if name}
        flags_col = 0 if cols and cols[0] in ("", " ", "Flags") else None
        rows = [list(r) for r in rows]
        comments = list(comments)
        sources = list(sources)
        by_id: dict[int, int] = {}
        for view_row, src in enumerate(sources):
            raw = self._raw_rows[src] if 0 <= src < len(self._raw_rows) else None
            oid = self._obj_id(raw)
            if oid is not None:
                by_id[oid] = view_row

        def fill(row: list, change) -> None:
            for e in change.edits:
                col = index.get(str(e.get("name") or ""))
                if col is not None:
                    row[col] = str(e.get("label", e.get("new", "")))

        touched: dict[int, list] = {}
        for change in mine:
            if change.action == "add":
                row = [""] * len(cols)
                fill(row, change)
                if flags_col is not None:
                    row[flags_col] = "+"
                rows.append(row)
                comments.append(str(change.values.get("Comment") or ""))
                sources.append(self._PENDING_SOURCE + len(rows))
                touched.setdefault(len(rows) - 1, []).append(change)
                continue
            view_row = by_id.get(change.obj_id)
            if view_row is None:
                continue
            row = rows[view_row]
            if change.action in ("set", "set_settings"):
                fill(row, change)
            elif change.action == "comment":
                text = str(change.values.get("Comment") or "")
                comments[view_row] = text
                if "Comment" in index:
                    row[index["Comment"]] = text
            elif change.action in ("enable", "disable") and flags_col is not None:
                letters = str(row[flags_col] or "").replace("X", "")
                row[flags_col] = ("X" + letters) if change.action == "disable" else letters
            touched.setdefault(view_row, []).append(change)

        pending: dict[int, tuple[str, str]] = {}
        for view_row, changes in touched.items():
            actions = [c.action for c in changes]
            kind = ("remove" if "remove" in actions
                    else "add" if "add" in actions else "edit")
            lines = []
            for c in changes:
                detail = ", ".join(
                    f"{e.get('name')} → {e.get('label', e.get('new'))}"
                    for e in c.edits if e.get("name")
                )
                lines.append(f"{c.action}{': ' + detail if detail else ''}")
            pending[view_row] = (
                kind, "Queued, not sent yet:\n  " + "\n  ".join(lines)
            )
        return rows, comments, sources, pending

    def _rebuild_view(self) -> None:
        # A poll rebuilds the model wholesale, which would otherwise drop the
        # selection and jump the viewport out from under the user mid-scroll.
        keep_ids = self._selected_row_ids()
        current = self._current_row_ref()
        scroll = self.table.verticalScrollBar().value()

        cols, secret = self._view_columns()
        rows, comments, sources = self._view_rows()
        rows, comments, sources = self._apply_sort(cols, secret, rows, comments, sources)
        rows, comments, sources = self._apply_tree_order(cols, rows, comments, sources)
        rows, comments, sources, pending = self._overlay_queue(
            cols, rows, comments, sources
        )
        flags_col = 0 if cols and cols[0] in ("", " ", "Flags") else None
        self._columns = cols
        self._model.set_secret_columns(
            i for i, is_sec in enumerate(secret) if is_sec
        )
        self._model.set_show_secrets(self._show_pw)
        self._model.set_table(
            cols, rows, comments=comments, sources=sources, flags_column=flags_col
        )
        self._model.set_pending(pending)
        self._apply_info(sources)
        self._apply_highlight(sources)
        self._apply_type_icons(cols, sources)
        self._apply_tree_meta(cols)
        # set_table dropped the fade state with the old row numbering.
        self._apply_freshness(sources)
        self._autosize_columns()
        self._narrow_flags_column()
        self._apply_column_visibility()
        self._apply_column_order()
        self._choose_item_delegate()
        # After the delegate and the columns: row heights are measured through
        # the delegate now, and measuring against the one the last rebuild left
        # behind would size a banner row as if it had no banner.
        self._apply_row_heights()
        self._refresh_sort_indicator()
        self._restore_selection(keep_ids, current, sources, scroll)

    def _choose_item_delegate(self) -> None:
        """Put our own delegate in front of every cell only when a row needs it.

        The banner delegate exists for three things: the ``;;; comment`` strip,
        the device's red status lines, and the interface tree's indent and
        arrows. A list with none of them -- which is most lists, most of the
        time -- renders identically through Qt's own delegate, because the cell
        inset now comes from the style and the column rules from the view. It
        also renders a good deal faster: a delegate is a Python call per cell
        per frame, and a wide list being dragged to a new size is thousands of
        them a second.
        """
        model = self._model
        needs = bool(
            (self._banner.enabled and model.has_comments())
            or model.has_info()
            or model.tree_column() is not None
        )
        want = self._banner if needs else self._plain_delegate
        if self.table.itemDelegate() is not want:
            self.table.setItemDelegate(want)

    #: Parent-interface reference on a sub-interface row (VLAN's "Interface").
    _IFACE_PARENT_CODE = 0x7D2

    def _apply_tree_order(self, cols, rows, comments, sources):
        """Nest each row under its parent, as Winbox does: children come right
        after the parent, and the delegate indents them and draws a collapse
        arrow. A collapsed parent keeps its own row and hides its subtree.

        The .jg says which lists are trees and what the parent link is:
        ``treeon`` names the field holding it -- a VLAN's `Interface`, a bridge
        port's `Parent`, a package's `bundle`, a file's `container` -- and the
        value is matched against the other rows' own names, which is
        ``nameval``'s field (List for an address list, Name for a queue). An
        interface list keeps working without a `treeon` of its own: the base
        Interface map declares none and every sub-interface type declares one,
        so a family list falls back to the parent field by code.
        """
        self._pending_tree_meta = {}
        self._tree_row_name = {}
        menu = self._menu
        if menu is None:
            return rows, comments, sources
        tree_on = getattr(menu, "tree_on", None)
        is_iface = getattr(menu, "generic", None) == "iface"
        if not tree_on and not is_iface:
            return rows, comments, sources
        parent_code = sch.field_code_by_name(menu, tree_on) if tree_on else None
        if parent_code is None:
            if not is_iface:
                return rows, comments, sources
            parent_code = self._IFACE_PARENT_CODE

        name_idx = self._tree_label_column(cols, menu)
        if name_idx is None:
            return rows, comments, sources

        names = [str(r[name_idx]).strip() if name_idx < len(r) else "" for r in rows]
        name_to_i = {n: i for i, n in enumerate(names) if n}
        # The link comes in two flavours and the .jg does not say which: a
        # VLAN's `Interface` is the parent's *name*, while a bridge port's
        # `Parent` is the parent row's object *id*. Try the id first and fall
        # back to the name, so each row joins whichever way it was written.
        id_to_i: dict[int, int] = {}
        for i, src in enumerate(sources):
            raw = self._raw_rows[src] if 0 <= src < len(self._raw_rows) else None
            oid = raw.object_id() if hasattr(raw, "object_id") else None
            if isinstance(oid, int):
                id_to_i.setdefault(oid, i)

        children: dict[int, list[int]] = {}
        top: list[int] = []
        for i, src in enumerate(sources):
            raw = self._raw_rows[src] if 0 <= src < len(self._raw_rows) else None
            pi = None
            if raw is not None:
                value = (getattr(raw, "values_by_code", None) or {}).get(parent_code)
                if isinstance(value, int) and not isinstance(value, bool):
                    pi = id_to_i.get(value)
                if pi is None:
                    try:
                        text = (raw.get_rendered(parent_code) or "").strip()
                    except Exception:  # noqa: BLE001
                        text = ""
                    pi = name_to_i.get(text) if text else None
            if pi is not None and pi != i:
                children.setdefault(pi, []).append(i)
            else:
                top.append(i)
        if not any(children.values()):
            return rows, comments, sources  # nothing nests -> leave as-is

        order: list[int] = []
        depth: dict[int, int] = {}

        def emit(i: int, d: int) -> None:
            order.append(i)
            depth[i] = d
            if names[i] in self._collapsed:
                return  # collapsed: show the parent row, hide its subtree
            for c in children.get(i, []):
                emit(c, d + 1)

        for i in top:
            emit(i, 0)

        from ..models.table_model import TREE_COLLAPSED, TREE_EXPANDED, TREE_LEAF

        new_rows, new_comments, new_sources = [], [], []
        for pos, i in enumerate(order):
            new_rows.append(list(rows[i]))
            new_comments.append(comments[i])
            new_sources.append(sources[i])
            d = depth.get(i, 0)
            if children.get(i):
                state = TREE_COLLAPSED if names[i] in self._collapsed else TREE_EXPANDED
            else:
                state = TREE_LEAF
            self._pending_tree_meta[pos] = (d, state)
            self._tree_row_name[pos] = names[i]
        return new_rows, new_comments, new_sources

    def _tree_label_column(self, cols: list[str], menu) -> int | None:
        """The column holding each row's own name, for a tree to match on.

        ``nameval`` names the field an object is called by, and that is what a
        parent link points at: a bridge port is its Interface, an address-list
        entry its List, a queue its Name.
        """
        code = sch.name_field_code(menu)
        fld = (menu.fields or {}).get(code) if code is not None else None
        wanted = (fld.name or "").strip() if fld is not None else ""
        if wanted:
            for i, c in enumerate(cols):
                if (c or "").strip() == wanted:
                    return i
        for i, c in enumerate(cols):
            if (c or "").strip().lower() == "name":
                return i
        for i, c in enumerate(cols):
            if (c or "").strip() and c not in ("", " ", "Flags", sch.INDEX_LABEL):
                return i
        return None

    def _apply_highlight(self, sources: list[int]) -> None:
        """Mark the rows a menu's ``highlight`` condition picks out.

        One menu declares it -- Interface, ``highlight:'notrunning'`` -- and
        Winbox greys those rows, the same treatment a disabled row gets. It is
        applied on top of the flags pass (which greys X), so a row that is both
        stays grey.
        """
        menu = self._menu
        on = getattr(menu, "highlight_on", None) if menu is not None else None
        if not on:
            return
        for view_row, src in enumerate(sources):
            raw = self._raw_rows[src] if 0 <= src < len(self._raw_rows) else None
            values = getattr(raw, "values_by_code", None) or {}
            try:
                if sch.condition_is_true(menu, on, values):
                    self._model.set_row_disabled(view_row, True)
            except Exception:  # noqa: BLE001
                return

    def _apply_info(self, sources: list[int]) -> None:
        """Map the per-object status lines onto the view rows.

        Read-only device text, so unlike the comment it never needs to ride
        along through the sort and tree permutations: `sources` already says
        which object each view row shows.
        """
        self._model.set_info([
            self._all_info[src] if 0 <= src < len(self._all_info) else ()
            for src in sources
        ])

    def _apply_tree_meta(self, cols: list[str]) -> None:
        """Push the tree state built by _apply_tree_order into the model so the
        Name delegate can draw the indent and arrows."""
        if not self._pending_tree_meta:
            return
        name_idx = next(
            (i for i, c in enumerate(cols) if (c or "").strip().lower() == "name"),
            None,
        )
        if name_idx is None:
            return
        self._model.set_tree_meta(self._pending_tree_meta, name_idx)

    def _toggle_branch(self, view_row: int) -> None:
        name = self._tree_row_name.get(view_row)
        if not name:
            return
        if name in self._collapsed:
            self._collapsed.discard(name)
        else:
            self._collapsed.add(name)
        self._rebuild_view()

    def _apply_type_icons(self, cols: list[str], sources: list[int]) -> None:
        """Interface lists show a per-type icon next to the Name (WinBox does the
        same). Keyed on each row's wire type_value (read off the raw row, so it
        does not depend on the rendered Type string); other menus get no icons."""
        menu = self._menu
        if menu is None:
            return
        # `iconon` + `icons`: the row's icon comes from a field's value, which
        # is how a firewall list gets an accept / drop / jump icon per rule.
        if getattr(menu, "icon_on", None) and getattr(menu, "icon_map", None):
            self._apply_value_icons(cols, sources)
            return
        if getattr(menu, "generic", None) != "iface":
            return
        from x3.proto.schema import wire_type_value

        from ..icons import iface_type_icon_for_value

        name_idx = next(
            (i for i, c in enumerate(cols) if (c or "").strip().lower() == "name"),
            None,
        )
        if name_idx is None:
            return
        icons = []
        for src in sources:
            raw = self._raw_rows[src] if 0 <= src < len(self._raw_rows) else None
            tv = wire_type_value(raw, menu) if raw is not None else None
            icons.append(iface_type_icon_for_value(tv))
        self._model.set_row_icons(icons, name_idx)

    def _apply_value_icons(self, cols: list[str], sources: list[int]) -> None:
        """Row icons keyed on a field's value (`iconon` + `icons`).

        Firewall, NAT, Mangle and Raw all say ``iconon:'Action'`` with a map
        from action value to sprite cell, which is how Winbox shows a green
        tick on accept and a red one on drop.
        """
        from ..icons import icon_from_index

        menu = self._menu
        code = sch.field_code_by_name(menu, menu.icon_on)
        if code is None:
            return
        # Winbox puts it on the first column that carries text.
        target = next(
            (i for i, c in enumerate(cols) if (c or "").strip()
             and c not in ("", " ", "Flags", sch.INDEX_LABEL)),
            None,
        )
        if target is None:
            return
        icons = []
        for src in sources:
            raw = self._raw_rows[src] if 0 <= src < len(self._raw_rows) else None
            value = (getattr(raw, "values_by_code", None) or {}).get(code)
            cell = None
            if isinstance(value, bool):
                value = int(value)
            if isinstance(value, int):
                cell = menu.icon_map.get(value)
            icons.append(icon_from_index(cell) if cell is not None else None)
        self._model.set_row_icons(icons, target)

    # --- column layout: chooser, widths, order, sort ---------------------

    def focus_target(self):
        """Where the keyboard should land when this pane's window opens.

        The filter box: Winbox lets you type to narrow a list straight away,
        and the alternative is the first toolbar button, which does nothing
        useful with a keypress.
        """
        return getattr(self, "_filter", None) or getattr(self, "table", None)

    def _pref_key(self) -> str:
        """Identity for the saved layout.

        MultiPageListWindow gives several tabs the same schema path and tells
        them apart with the interface-type overlay, so that has to be part of
        the key or Ethernet and VLAN would share one layout.
        """
        base = str(tuple(self.path)) if self.path else f"title:{self.title}"
        if self.type_value is not None:
            return f"{base}#{self.type_value}"
        return f"{base}#{self.family}" if self.family else base

    @staticmethod
    def _label_of(name: str) -> str:
        """Display/pref name for a column — the flags column has none."""
        return FLAGS_LABEL if name in ("", " ", "Flags") else name

    @staticmethod
    def _view_index_of(cols: list[str], label: str) -> int | None:
        for i, name in enumerate(cols):
            if ListPane._label_of(name) == label:
                return i
        return None

    def _column_labels(self) -> list[str]:
        return [self._label_of(c) for c in self._columns]

    def _save_prefs_soon(self) -> None:
        """Coalesce a burst of header signals into one write."""
        if self._suppress_prefs:
            return
        if self._pref_timer is None:
            self._pref_timer = QTimer(self)
            self._pref_timer.setSingleShot(True)
            self._pref_timer.setInterval(_PREF_SAVE_MS)
            self._pref_timer.timeout.connect(self._save_prefs)
        self._pref_timer.start()

    def _save_prefs(self) -> None:
        self._prefs["hidden"] = sorted(self._hidden_cols)
        if self._sort_col is None:
            self._prefs.pop("sort", None)
        else:
            self._prefs["sort"] = [self._sort_col, "desc" if self._sort_desc else "asc"]
        hdr = self.table.horizontalHeader()
        labels = self._column_labels()
        if labels:
            self._prefs["order"] = [
                labels[hdr.logicalIndex(v)]
                for v in range(len(labels))
                if hdr.logicalIndex(v) < len(labels)
            ]
        save_column_prefs(self._pref_key(), self._prefs)

    def _on_section_resized(self, logical: int, _old: int, new: int) -> None:
        """Record a *user* drag. Our own sizing runs under _suppress_prefs.

        Widths land in self._prefs immediately and only the disk write is
        debounced, so a poll landing mid-drag re-applies the new width rather
        than snapping back to the last one written.
        """
        if self._suppress_prefs:
            return
        labels = self._column_labels()
        if logical < 0 or logical >= len(labels) or new <= 0:
            return
        if logical == self._stretched_section():
            # The header sized that one to fill the window: a window resize
            # or a neighbour's drag, never a width anyone chose. Recording it
            # is how a Comment column came back 1350px wide.
            return
        widths = self._prefs.get("widths")
        if not isinstance(widths, dict):
            widths = {}
            self._prefs["widths"] = widths
        # Kept at 1x, like a .jg width, and scaled on the way back in. Stored
        # as drawn, a width dragged at zoom 1.0 was 30% too narrow at 1.3 for
        # the very address it had been dragged to fit.
        widths[labels[logical]] = max(1, round(new / style.ui_scale()))
        self._save_prefs_soon()

    def _stretched_section(self) -> int | None:
        """The logical section the header stretches to fill the view."""
        hdr = self.table.horizontalHeader()
        if not hdr.stretchLastSection():
            return None
        for visual in range(hdr.count() - 1, -1, -1):
            logical = hdr.logicalIndex(visual)
            if not hdr.isSectionHidden(logical):
                return logical
        return None

    def _on_section_moved(self, *_args) -> None:
        self._save_prefs_soon()

    def _apply_saved_widths(self) -> None:
        widths = self._prefs.get("widths")
        if not isinstance(widths, dict) or not widths:
            return
        hdr = self.table.horizontalHeader()
        self._suppress_prefs += 1
        try:
            for i, label in enumerate(self._column_labels()):
                w = widths.get(label)
                if isinstance(w, int) and w > 0:
                    hdr.resizeSection(i, style.scale_px(w))
        finally:
            self._suppress_prefs -= 1

    def _apply_column_visibility(self) -> None:
        self._suppress_prefs += 1
        try:
            for i, label in enumerate(self._column_labels()):
                self.table.setColumnHidden(i, label in self._hidden_cols)
        finally:
            self._suppress_prefs -= 1

    def _apply_column_order(self) -> None:
        order = self._prefs.get("order")
        if not isinstance(order, list) or not order:
            return
        hdr = self.table.horizontalHeader()
        labels = self._column_labels()
        self._suppress_prefs += 1
        try:
            target = 0
            for label in order:
                if label not in labels:
                    continue
                logical = labels.index(label)
                visual = hdr.visualIndex(logical)
                if visual != target:
                    hdr.moveSection(visual, target)
                target += 1
        finally:
            self._suppress_prefs -= 1

    def _refresh_sort_indicator(self) -> None:
        hdr = self.table.horizontalHeader()
        col = (
            None
            if self._sort_col is None
            else self._view_index_of(self._columns, self._sort_col)
        )
        order = (
            Qt.SortOrder.DescendingOrder if self._sort_desc else Qt.SortOrder.AscendingOrder
        )
        hdr.setSortIndicator(-1 if col is None else col, order)

    def _on_header_clicked(self, logical: int) -> None:
        """Cycle ascending → descending → device order.

        The third state matters: in ordered lists (firewall rules, mangle) the
        device's own order *is* the semantics, so there has to be a way back to
        it that isn't "close and reopen the window".
        """
        labels = self._column_labels()
        if logical < 0 or logical >= len(labels):
            return
        label = labels[logical]
        if self._sort_col != label:
            self._sort_col, self._sort_desc = label, False
        elif not self._sort_desc:
            self._sort_desc = True
        else:
            self._sort_col, self._sort_desc = None, False
        self._rebuild_view()
        self._save_prefs_soon()

    def _row_menu(self, pos) -> None:
        """Winbox's right-click menu on a row, built from the .jg.

        `type:contextmenu` entries name another menu to open and the fields to
        carry into it. They are grouped the way the .jg groups them (Tools,
        IP, Wireless), and an entry whose `on:` condition fails for this row is
        left out -- the same rule the property form's rows follow.
        """
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        raw = self._raw_for_view_row(index.row())
        menu = self.row_menu_for(raw)
        oid = self._obj_id(raw)
        client = getattr(self.edit_session, "client", None)
        if oid is not None and hasattr(client, "command_line_for"):
            if menu is None:
                menu = QMenu(self.table)
            else:
                menu.addSeparator()
            act = menu.addAction("Command Line…")
            act.setToolTip("This row as the router's own CLI line, to copy")
            act.triggered.connect(
                lambda _=False, r=raw, o=oid: self._show_command_line(r, o)
            )
        if menu is None:
            return
        # The row the menu is about is the row it acts on.
        self.table.selectRow(index.row())
        menu.exec(self.table.viewport().mapToGlobal(pos))

    #: Most lines a hover summary shows before saying how many more there are.
    _SUMMARY_LINES = 40

    def _row_summary(self, view_row: int) -> str | None:
        """What was set on this row, one "Name: value" a line.

        The columns are a choice of a few; resting on a row shows the settings
        behind them -- a firewall rule's matchers -- and only those changed
        from their default, so the summary says what makes this row this row. A password stays masked unless the
        list is showing passwords, and a field the schema cannot name is left
        out rather than shown as <0x…>.
        """
        raw = self._raw_for_view_row(view_row)
        exact = self._exact_summary(raw)
        if exact is not None:
            return exact
        fields = getattr(raw, "fields", None) or []
        if not fields:
            return None
        values = getattr(raw, "values_by_code", None) or {}
        menu = self._row_menu_of(raw)
        firmware = self._firmware()
        learned = (
            learned_defaults.known(firmware, self._cli_candidates(raw)[1])
            if firmware and self._obj_id(raw) is not None else None
        )
        rank = self._form_rank(menu)
        lines: list[tuple[int, str]] = []
        seen: set[str] = set()
        for code, name, text, _ok in fields:
            name, text = str(name), str(text if text is not None else "")
            if not text.strip() or not name or name.startswith("<0x") or name in seen:
                continue
            seen.add(name)
            fld = getattr(menu, "fields", {}).get(code) if menu else None
            if fld is not None and (
                fld.ro or fld.nonpublic or fld.jg_type in ("flag", "hyperlink")
            ):
                # What was set, not what the router reports: no readings
                # (read-only -- counters, status, Actual MTU, Last Link Up), no
                # internal fields, no flag words (the flags column says those).
                continue
            if fld is not None:
                value = values.get(code)
                # Left at its default is not a setting either: MTU 1500 where
                # the .jg's default is 1500, a switch that is off when the .jg
                # names no default. What is left is what somebody chose -- the
                # same line `export terse` draws.
                if fld.default is not None and value == fld.default:
                    continue
                if fld.default is None and (
                    learned_defaults.is_default(learned, code, value)
                    or (value is False and code not in (learned or {}))
                ):
                    # A default the router taught us (learned_defaults), or a
                    # switch that is off when nothing better is known.
                    continue
            if fld is not None and sch.is_secret(fld) and not self._show_pw:
                text = "••••••"
            lines.append((rank.get(code, len(rank)), f"{name}: {text}"))
        # In the order the form shows them, tab by tab -- Chain, Src. Address
        # ... Action -- not the order they came off the wire.
        lines = [line for _r, line in sorted(lines, key=lambda x: x[0])]
        comment = self._comment_of(raw)
        if comment and "Comment" not in seen:
            lines.append(f"Comment: {comment}")
        if not lines:
            if learned is None:
                return None
            # Everything it carries is at its default: say so, rather than
            # leave the hover blank as if nothing were known.
            lines = ["Nothing set beyond its defaults"]
        head = self._summary_head(raw, lines)
        more = len(lines) - self._SUMMARY_LINES
        lines = lines[: self._SUMMARY_LINES]
        if more > 0:
            lines.append(f"… and {more} more")
        return (f"{head}\n" if head else "") + "\n".join(lines)

    def _summary_head(self, raw, lines: list[str]) -> str:
        """The row's name over its summary, when it says something the lines do not.

        An interface's "ether1" does, over an export that leaves its default
        name out. A firewall rule's name is its own
        matchers run together -- Src. Address:Src. Port->Dst. Address:Dst. Port
        -- so one matching only Dst. Port 13239 was headed by a bare "13239"
        that named nothing; the "Dst. Port: 13239" line below already says it.
        """
        label = self._row_label([], raw)
        if not label or label == "row":     # _row_label's placeholder, not a name
            return ""
        shown = {ln.partition(": ")[2].strip() for ln in lines}
        parts = [p.strip() for p in re.split(r"->|[:,]", label) if p.strip()]
        if parts and all(p in shown for p in parts):
            return ""
        return label

    def _show_command_line(self, raw, obj_id: int) -> None:
        """Command Line…: ask the router for this row's own CLI line."""
        from .cli_line import CommandLineDialog

        candidates, key = self._cli_candidates(raw)
        client = self.edit_session.client
        dlg = CommandLineDialog(
            self._row_label([], raw) or self.title,
            lambda: client.command_line_for(candidates, obj_id, menu_path=key),
            self,
        )
        dlg.show()

    def _cli_candidates(self, raw) -> tuple[list[str], tuple]:
        """CLI path candidates for a row, and the key its found path is kept by.

        A list of several kinds (Interfaces) keeps each kind's lines under its
        own path -- ether1 is /interface ethernet -- so the row's Type goes on
        the end of each candidate, tried first, and is part of the key.
        """
        from .cli_line import cli_candidates

        crumbs_for = getattr(self.window(), "_nav_breadcrumbs", None)
        crumbs = crumbs_for(self.path) if callable(crumbs_for) else []
        if not crumbs:
            crumbs = [[self.title]]
        candidates = cli_candidates(crumbs)
        kind = ""
        for _code, name, text, _ok in getattr(raw, "fields", None) or []:
            # The shown Type ("Ethernet"), not the internal numeric one some
            # menus also carry as "type".
            if str(name).lower() == "type" and text and not str(text).isdigit():
                kind = re.sub(r"[^a-z0-9-]", "", str(text).lower().replace(" ", "-"))
                break
        if kind:
            candidates = [f"{c} {kind}" for c in candidates] + candidates
        return candidates, (tuple(self.path or ()), kind)

    # --- what was set, as the router says -----------------------------
    #
    # The .jg names a default for some fields only (MTU, not ARP or Speed), so
    # only the router can say what was changed on a row: its export lists
    # nothing else. The first hover on a list asks for the whole list in one
    # console read, in the background; until it lands the hover shows the
    # .jg-based summary, and after it every hover is exact and instant. The
    # lines are dropped when the list's configuration moves (_note_content).

    def _row_menu_of(self, raw):
        """The row's own menu: the list's, merged with the row's type overlay.

        An ether1's ARP, Loop Protect and MAC are the Ethernet overlay's
        fields, not the generic Interface's, and only the overlay knows they
        are read-only or what their default is.
        """
        menu = self._menu
        schema = getattr(getattr(self.edit_session, "client", None), "schema", None)
        values = getattr(raw, "values_by_code", None) or {}
        if schema is not None and menu is not None and raw is not None:
            try:
                tv = sch.wire_type_value(raw, menu)
                menu = schema.record_for(tuple(self.path or ()), tv, values) or menu
            except Exception:  # noqa: BLE001 - fall back to the list's own menu
                pass
        return menu

    def _firmware(self) -> str:
        """Which firmware build learned defaults are filed under."""
        client = getattr(self.edit_session, "client", None)
        directory = getattr(client, "schema_dir", "") or ""
        return os.path.basename(os.path.normpath(directory)) if directory else ""

    @staticmethod
    def _form_rank(menu) -> dict[int, int]:
        """Each field's place on the row's form: tab by tab, as declared."""
        return {code: i for i, (_t, code) in enumerate(getattr(menu, "form_order", None) or [])}

    @staticmethod
    def _settable(fld) -> bool:
        return fld is not None and not (
            fld.ro or fld.nonpublic or fld.jg_type in ("flag", "hyperlink")
        )

    def _exact_summary(self, raw) -> str | None:
        """The row's settings from the router's own export, or None for now."""
        client = getattr(self.edit_session, "client", None)
        oid = self._obj_id(raw)
        if oid is None or not hasattr(client, "export_lines"):
            return None
        _cands, key = self._cli_candidates(raw)
        if key in self._cli_failed:
            return None
        path = client.cli_path_known(key)
        lines = self._cli_lines.get(path) if path else None
        if lines is None:
            # Defaults learned before -- this session or an earlier one, on any
            # router with this firmware -- answer locally: no console read.
            firmware = self._firmware()
            if firmware and learned_defaults.known(firmware, key) is not None:
                return None
            self._fetch_cli_lines(raw, oid)
            return None
        line = lines.get(oid)
        if line is None:
            return None     # a dynamic or default object: nothing exported
        from .cli_line import line_settings

        pairs = self._form_ordered(line_settings(line), self._row_menu_of(raw))
        head = self._summary_head(raw, [f"{k}: {v}" for k, v in pairs])
        body = "\n".join(f"{k}: {v}" for k, v in pairs) or "Nothing set beyond its defaults"
        return f"{head}\n{body}" if head else body

    @classmethod
    def _form_ordered(cls, pairs: list[tuple[str, str]], menu) -> list[tuple[str, str]]:
        """An export's ``key=value`` pairs as the form has them.

        The export goes alphabetically -- action, chain, comment,
        connection-state -- where the form goes Chain, Src. Address, ...,
        Action. Each key is matched to the field whose name it is
        (learned_defaults.cli_name), shown by that name and sorted by its
        place on the form; a key no field claims keeps its export order after
        them, and the comment comes last, as the template summary has it.
        """
        fields = getattr(menu, "fields", None) or {}
        rank = cls._form_rank(menu)
        by_cli: dict[str, tuple[int, str]] = {}
        for code, fld in fields.items():
            key = learned_defaults.cli_name(fld.name)
            if key and key not in by_cli:
                by_cli[key] = (rank.get(code, len(rank)), fld.name)
        placed: list[tuple[int, int, str, str]] = []
        comment: list[tuple[str, str]] = []
        for i, (key, value) in enumerate(pairs):
            if key == "comment":
                comment.append(("Comment", value))
                continue
            at, name = by_cli.get(key, (len(rank) + 1, key))
            placed.append((at, i, name, value))
        placed.sort()
        return [(name, value) for _a, _i, name, value in placed] + comment

    def _fetch_cli_lines(self, raw, oid: int) -> None:
        from .cli_line import _LookupWorker

        candidates, key = self._cli_candidates(raw)
        if key in self._cli_fetching:
            return
        self._cli_fetching.add(key)
        client = self.edit_session.client

        def work():
            path = client.cli_path_known(key)
            if not path:
                client.command_line_for(candidates, oid, menu_path=key)
                path = client.cli_path_known(key)
            return (key, path, client.export_lines(path) if path else {})

        worker = _LookupWorker(work, self)
        worker.done.connect(self._on_cli_lines)
        worker.finished.connect(lambda w=worker: self._cli_workers.discard(w))
        self._cli_workers.add(worker)
        worker.start()

    def _on_cli_lines(self, result, error: str) -> None:
        if not result:
            return
        key, path, lines = result
        self._cli_fetching.discard(key)
        if error or not path:
            self._cli_failed.add(key)       # do not ask again on every hover
            return
        self._cli_lines[path] = lines
        self._learn_defaults(key, lines)

    def _learn_defaults(self, key, lines: dict[int, str]) -> None:
        """File what this export says the defaults are (see learned_defaults)."""
        firmware = self._firmware()
        if not firmware:
            return
        rows = []
        for raw in self._raw_rows:
            oid = self._obj_id(raw)
            if oid is None or oid not in lines or self._cli_candidates(raw)[1] != key:
                continue
            menu = self._row_menu_of(raw)
            fields = [
                (code, str(name))
                for code, name, _text, _ok in getattr(raw, "fields", None) or []
                if self._settable(getattr(menu, "fields", {}).get(code) if menu else None)
            ]
            rows.append((fields, getattr(raw, "values_by_code", None) or {}, lines[oid]))
        if rows:
            try:
                learned_defaults.learn(firmware, key, rows)
            except OSError:
                pass        # a cache that cannot be written is only a cache

    def row_menu_for(self, row) -> "QMenu | None":
        """The right-click menu for one row, or None if it would be empty."""
        actions = list(getattr(self._menu, "context_actions", None) or [])
        if row is None or not actions or self._on_context_action is None:
            return None
        values = getattr(row, "values_by_code", None) or {}
        menu = QMenu(self.table)
        # Ungrouped entries sit at the top, then one submenu per group, in the
        # order the .jg declares them.
        submenus: dict[str, QMenu] = {}
        added = 0
        for act in actions:
            # An entry with no `link:` carries nothing from the row, so it is
            # not about the row: it is one of the window's own buttons, and
            # _install_jg_toolbar has already put it on the toolbar.
            if act.get("open") and not act.get("link"):
                continue
            on = act.get("on")
            if on:
                try:
                    if not sch.condition_is_true(self._menu, str(on), values):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            group = act.get("group")
            target = menu
            if group:
                sub = submenus.get(str(group))
                if sub is None:
                    # Parented explicitly: addMenu(title) hands the new menu
                    # back with Python ownership, and it is collected out from
                    # under the menu that is showing it.
                    sub = QMenu(str(group), menu)
                    menu.addMenu(sub)
                    submenus[str(group)] = sub
                target = sub
            entry = target.addAction(str(act.get("name") or "Action"))
            entry.triggered.connect(
                lambda _checked=False, a=act, r=row: self._on_context_action(a, r, self._menu)
            )
            added += 1
        if not added:
            return None
        return menu

    def _header_menu(self, pos) -> None:
        labels = self._column_labels()
        if not labels:
            return
        hdr = self.table.horizontalHeader()
        menu = QMenu(self.table)
        menu.addAction("Columns").setEnabled(False)
        menu.addSeparator()
        # Walk visual order so the menu reads like the table looks.
        for visual in range(len(labels)):
            logical = hdr.logicalIndex(visual)
            if logical < 0 or logical >= len(labels):
                continue
            label = labels[logical]
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(label not in self._hidden_cols)
            act.toggled.connect(
                lambda shown, name=label: self._set_column_shown(name, shown)
            )
        menu.addSeparator()
        act_all = menu.addAction("Show All Columns")
        act_all.setEnabled(bool(self._hidden_cols))
        act_all.triggered.connect(self._show_all_columns)
        act_sort = menu.addAction("Clear Sort (device order)")
        act_sort.setEnabled(self._sort_col is not None)
        act_sort.triggered.connect(self._clear_sort)
        menu.addAction("Reset Column Layout").triggered.connect(self._reset_columns)
        menu.exec(hdr.mapToGlobal(pos))

    def _set_column_shown(self, label: str, shown: bool) -> None:
        if shown:
            self._hidden_cols.discard(label)
        else:
            # Hiding everything leaves a table you cannot get back from.
            if len(self._hidden_cols) + 1 >= len(self._columns):
                return
            self._hidden_cols.add(label)
        self._apply_column_visibility()
        self._save_prefs_soon()

    def _show_all_columns(self) -> None:
        self._hidden_cols.clear()
        self._apply_column_visibility()
        self._save_prefs_soon()

    def _clear_sort(self) -> None:
        self._sort_col, self._sort_desc = None, False
        self._rebuild_view()
        self._save_prefs_soon()

    def _reset_columns(self) -> None:
        self._hidden_cols.clear()
        self._sort_col, self._sort_desc = None, False
        self._prefs = {}
        # Reset means "back to the menu's own column set", not "show every
        # column the device decodes". The default hiding is one-shot per load,
        # so re-arm it: with the preferences now empty there is nothing of the
        # user's left for it to defer to.
        self._columns_defaulted = False
        self._hide_extra_columns_by_default(self._menu)
        hdr = self.table.horizontalHeader()
        self._suppress_prefs += 1
        try:
            # Undo any drags before the rebuild, or the saved-order restore has
            # nothing to say and the visual order sticks.
            for visual in range(hdr.count()):
                logical = hdr.logicalIndex(visual)
                if logical != visual:
                    hdr.moveSection(visual, logical)
        finally:
            self._suppress_prefs -= 1
        self._rebuild_view()
        save_column_prefs(self._pref_key(), {})

    # --- keeping the user's place across a poll -------------------------

    def _selected_row_ids(self) -> set:
        out = set()
        for view_row in self._selected_view_rows():
            src = self._model.source_row(view_row)
            if src is None:
                continue
            raw = self._raw_rows[src] if src < len(self._raw_rows) else None
            out.add(self._row_id(raw, src))
        return out

    def _current_row_ref(self) -> tuple[Any, int] | None:
        """The row the keyboard is on, as ``(row id, column)``, or None.

        The *current* index is a separate thing from the selection, and it is
        the one the arrow keys move from and Enter acts on. A rebuild resets
        the model, which drops it -- and a view with no current index answers
        the next Up or Down with its first row, so a list being fed counters
        threw the user back to the top every time it re-read.
        """
        sm = self.table.selectionModel()
        if sm is None:
            return None
        index = sm.currentIndex()
        if not index.isValid():
            return None
        src = self._model.source_row(index.row())
        if src is None:
            return None
        raw = self._raw_rows[src] if src < len(self._raw_rows) else None
        return self._row_id(raw, src), index.column()

    def _restore_selection(
        self,
        keep_ids: set,
        current: tuple[Any, int] | None,
        sources: list[int],
        scroll: int,
    ) -> None:
        if not keep_ids and current is None:
            if scroll:
                self.table.verticalScrollBar().setValue(scroll)
            return
        sm = self.table.selectionModel()
        if sm is None:
            return
        last_col = max(0, self._model.columnCount() - 1)
        selection = QItemSelection()
        current_row: int | None = None
        for view_row, src in enumerate(sources):
            raw = self._raw_rows[src] if src < len(self._raw_rows) else None
            row_id = self._row_id(raw, src)
            if row_id in keep_ids:
                selection.select(
                    self._model.index(view_row, 0),
                    self._model.index(view_row, last_col),
                )
            if current is not None and row_id == current[0]:
                current_row = view_row
        if not selection.isEmpty():
            sm.select(
                selection,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        if current_row is not None:
            # NoUpdate: the selection is already exactly what it was, and this
            # only puts the cursor back on the same object.
            sm.setCurrentIndex(
                self._model.index(current_row, self._current_column(current[1])),
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        self.table.verticalScrollBar().setValue(scroll)

    def _current_column(self, col: int) -> int:
        """``col`` if it is still there and visible, else the first that is.

        A poll can arrive after the column set has moved -- a column hidden
        from the header menu, a menu whose fields differ -- and a current index
        on a hidden column is one the user cannot see.
        """
        count = self._model.columnCount()
        if 0 <= col < count and not self.table.isColumnHidden(col):
            return col
        for candidate in range(count):
            if not self.table.isColumnHidden(candidate):
                return candidate
        return 0

    def _masked(self, values: list[Any]) -> list[Any]:
        """Row values with secrets replaced, honouring the current toggle."""
        if self._show_pw:
            return list(values)
        return [
            SECRET_MASK
            if (i < len(self._col_secret) and self._col_secret[i] and v not in (None, ""))
            else v
            for i, v in enumerate(values)
        ]

    def _base_of(self, row_data: list[Any]) -> list[Any]:
        """Drop the synthetic Comment cell so _all_rows keeps its own width."""
        return list(row_data[: len(self._base_columns)])

    def _apply_row_heights(self) -> None:
        """Winbox density when inline, room for a banner line when not.

        Status lines need the room whatever the inline setting is: they have no
        column to fall back to, so a fixed row height would clip them away.
        """
        vh = self.table.verticalHeader()
        # The minimum comes down first in both branches. QHeaderView derives it
        # from the font -- 16px for an 11px face -- and clamps every section to
        # it, so a theme asking for anything shorter than that got 16 with no
        # complaint and its row_height quietly stopped meaning anything. That
        # clamp is also the whole reason a denser row looked impossible.
        vh.setMinimumSectionSize(1)
        # Re-read per rebuild so a theme switch takes it: paint_column_rules
        # draws the rules we see, and Qt's own grid must not outlive a theme
        # that turned them off.
        self.table.setShowGrid(style.COLUMN_RULES)
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setDefaultSectionSize(style.ROW_HEIGHT)
        if self._inline and not self._has_info():
            self._reset_row_heights(vh)
            return
        # Only a row carrying a banner strip -- a ``;;; comment`` while banners
        # are on, or a device status line -- is taller than the theme's row
        # height, so ask those rows their height and leave the rest at the
        # default.
        #
        # The header used to run in ResizeToContents instead, and that is what
        # made a live list stall. In that mode *every* dataChanged re-measures
        # the whole table through the delegate, and a live list patches every
        # row once a second: one interface down on a 30-row Interface window
        # cost 420ms and 111k data() crossings per 1 Hz update against 20ms and
        # 2.5k, and 100 rows cost 4.5s -- a freeze about as long as the update
        # interval, which also swallowed the keyboard and any drag in progress.
        # Measuring here is safe to hold: a banner line is one line of text and
        # is never wrapped, so a row's height does not depend on column widths,
        # and the one edit that can change it while the list stands still
        # re-measures its own row (see the comment action).
        banners = self._banner.enabled and self._model.has_comments()
        for row in range(self._model.rowCount()):
            if not (self._model.info(row)
                    or (banners and self._model.comment(row))):
                # Back to the default, explicitly. A section Qt was told a
                # size keeps it across a model reset -- the size belongs to the
                # position, not the object -- so after a sort the row landing
                # where a commented one used to be inherited its banner's
                # height: an uncommented 10.3.3.2 sat in a blank band twice
                # its height, top of a list sorted by Address.
                if vh.sectionSize(row) != style.ROW_HEIGHT:
                    vh.resizeSection(row, style.ROW_HEIGHT)
                continue
            hint = self.table.sizeHintForRow(row)
            if hint > 0:
                vh.resizeSection(row, hint)

    def _reset_row_heights(self, vh) -> None:
        """Every row back to the theme's height (banners just went inline)."""
        for row in range(self._model.rowCount()):
            if vh.sectionSize(row) != style.ROW_HEIGHT:
                vh.resizeSection(row, style.ROW_HEIGHT)

    def set_show_passwords(self, show: bool) -> None:
        self._show_pw = bool(show)
        self._model.set_show_secrets(self._show_pw)
        if self._filter.text().strip():
            # The filter ignores masked cells, so the match set changes.
            self._rebuild_view()

    def set_inline_comments(self, inline: bool) -> None:
        if bool(inline) == self._inline:
            return
        self._inline = bool(inline)
        self._banner.set_enabled(not self._inline)
        self._rebuild_view()

    def _on_error(self, msg: str) -> None:
        self._status.setText("error")
        # The message box is modal and then gone; the log is what is left to
        # look at afterwards, so record which menu failed and why.
        applog.note(f"{self.title} {self.path or ''} failed to load: {msg}")
        lost = getattr(getattr(self.edit_session, "client", None), "connection_lost", None)
        if callable(lost) and lost() is not None:
            # The window says "Disconnected" and is reconnecting; a box per
            # list saying the same is noise. This list reloads once it is back.
            self._status.setText("disconnected")
            return

        # One dialog at a time, globally. QMessageBox runs a nested event
        # loop, so a second pane failing while the first box is up opens its
        # box *inside* that loop. When a session dies every open pane fails at
        # once: fourteen modals nested fourteen deep, and no way to click out
        # of them. Everything past the first goes to the status line and the
        # log, which is where it is actually useful.
        global _error_box_open
        if _error_box_open:
            return

        hint = ""
        if "NOTIMP" in msg or "0xfe0003" in msg.lower():
            # Handler is not a list (NOTIMP on GETALL).
            hint = (
                "\n\nThis usually means the menu is not a list on this firmware "
                "build (GETALL not implemented). It may be a settings page "
                "(double‑click / open as item) or unsupported here."
            )
        elif "DemuxClosed" in msg or "reader stopped" in msg:
            hint = (
                "\n\nThe connection to the router is gone, so nothing else "
                "will load either. Reconnect to carry on."
            )

        _error_box_open = True
        try:
            QMessageBox.warning(self, self.title, f"Failed to load:\n{msg}{hint}")
        finally:
            _error_box_open = False

    def _autosize_columns(self, *, force: bool = False) -> None:
        """Measure columns to their content, then let saved widths override.

        Only when the column set actually changes. This used to run on every
        rebuild, so a 5s auto-refresh re-measured every column and threw away
        whatever width the user had just dragged.
        """
        labels = tuple(self._column_labels())
        measure = force or labels != self._sized_for
        self._sized_for = labels
        declared = self._declared_widths()
        self._suppress_prefs += 1
        try:
            if measure:
                hdr = self.table.horizontalHeader()
                for i in range(self._model.columnCount()):
                    if (
                        self._model.flags_column is not None
                        and i == self._model.flags_column
                    ):
                        continue  # _narrow_flags_column sizes this one
                    # The .jg gives most columns a width; that is the one
                    # Winbox opens with, and it is why its Protocol column is
                    # narrow and its Message column wide regardless of what
                    # this particular router happens to have in them. Floored
                    # at the header's own text so a 40px column still reads.
                    # Scaled: a .jg width is a Winbox pixel, measured on a
                    # 1x screen, so it means "this many pixels of *this* UI"
                    # rather than a fixed 40. Unscaled, zooming in grew the
                    # type and left the column it sits in the same width.
                    want = declared.get(labels[i] if i < len(labels) else "")
                    if want:
                        # ...and grown to what is actually in it. The .jg
                        # gives DHCP Client's IP Address 80px, which Winbox
                        # clips a routable /24 in, and a zoomed UI clips a
                        # private one too. Capped, so one long comment does
                        # not push every other column off screen.
                        hdr.resizeSection(i, max(
                            style.scale_px(want), self._header_hint(i),
                            min(self._content_width(i),
                                max(style.scale_px(want), style.scale_px(260))),
                        ))
                        continue
                    # Only a column the .jg does not size gets measured, and
                    # measuring is what costs: a measure-every-column pass asks
                    # the delegate for every cell of every row -- 39ms of the
                    # ~100ms it takes to open the Interface list -- and then
                    # threw nearly all of it away, since a column that declares
                    # a width takes that instead, and every Interface column
                    # declares one.
                    self.table.resizeColumnToContents(i)
                    w = hdr.sectionSize(i)
                    wide, narrow = style.scale_px(200), style.scale_px(40)
                    if w > wide:
                        hdr.resizeSection(i, wide)
                    elif w < narrow:
                        hdr.resizeSection(i, narrow)
            if measure and not self._model.rowCount():
                # Sized against nothing: the content fit has yet to happen, so
                # the first rebuild with rows in it measures again.
                self._sized_for = None
            elif measure:
                # After the saved widths and hidden columns land, not now.
                QTimer.singleShot(0, self._emit_columns_fitted)
        finally:
            self._suppress_prefs -= 1
        # Always re-applied: a model reset can hand sections back at their
        # default size even when the column count is unchanged.
        self._apply_saved_widths()
        self._narrow_flags_column()

    def _emit_columns_fitted(self) -> None:
        try:
            self.columns_fitted.emit(self.table.horizontalHeader().length())
        except RuntimeError:
            pass  # the pane went away before the event loop came round

    def _content_width(self, column: int, sample: int = 200) -> int:
        """Width the column's text needs, read off the first ``sample`` rows.

        Text through font metrics, not ``resizeColumnToContents``: that asks
        the delegate for every cell, which is the cost the declared widths
        exist to avoid.
        """
        fm = self.table.fontMetrics()
        widest = 0
        for row in range(min(self._model.rowCount(), sample)):
            text = self._model.index(row, column).data()
            if text:
                widest = max(widest, fm.horizontalAdvance(str(text)))
        if not widest:
            return 0
        return widest + 2 * style.CELL_PAD + style.scale_px(6)

    def _declared_widths(self) -> dict[str, int]:
        """Column name → the pixel width the .jg asks for, if it says."""
        if self._menu is None:
            return {}
        try:
            from x3.proto.schema import default_column_widths

            return default_column_widths(self._menu)
        except Exception:  # noqa: BLE001
            return {}

    def _header_hint(self, index: int) -> int:
        """Width the header section needs for its own label."""
        try:
            return int(
                self.table.horizontalHeader().sectionSizeHint(index)
            )
        except Exception:  # noqa: BLE001
            return 0

    def _narrow_flags_column(self) -> None:
        """Keep the flag-letter column compact (Winbox-style)."""
        fc = self._model.flags_column
        if fc is None:
            return
        if isinstance(self._prefs.get("widths"), dict) and (
            FLAGS_LABEL in self._prefs["widths"]
        ):
            return  # the user chose a width for it; don't stomp on that
        hdr = self.table.horizontalHeader()
        self._suppress_prefs += 1
        try:
            # Width for ~4 bold monospace letters
            hdr.resizeSection(fc, style.scale_px(36))
        finally:
            self._suppress_prefs -= 1

    def _apply_filter(self, text: str) -> None:
        self._rebuild_view()

    # --- selection helpers --------------------------------------------

    def _selected_view_rows(self) -> list[int]:
        sm = self.table.selectionModel()
        if sm is None:
            return []
        return sorted({idx.row() for idx in sm.selectedRows()})

    def _selected_items(self) -> list[tuple[int, list[Any], Any | None]]:
        """(view_row, values, raw_or_None) for each selected row."""
        out = []
        for vr in self._selected_view_rows():
            values = self._model.row_values(vr)
            raw = self._raw_for_view_row(vr, values)
            out.append((vr, values, raw))
        return out

    def _require_selection(self, action: str) -> list[tuple[int, list[Any], Any | None]] | None:
        items = self._selected_items()
        if not items:
            QMessageBox.information(
                self,
                action,
                f"Select one or more rows first, then click {action}.",
            )
            return None
        return items

    def _row_key(self, values: list[Any], raw: Any | None) -> str:
        """Stable id for MDI property windows — one window per list object.

        Prefer the human name (Name / Default Name). Do not use the Flags
        column or session/disabled codes — those collide across rows and made
        ether4 and ether5 reopen the same form.
        """
        label = self._row_label(values, raw)
        if raw is not None:
            # Prefer name fields on the wire object over ambiguous system codes.
            by_code = getattr(raw, "values_by_code", None) or {}
            for code in (0xFE0010, 0x10006, 0x01, 0x02):  # sfe0010 / Interface Name / …
                val = by_code.get(code)
                if isinstance(val, str) and val.strip():
                    return f"name:{val.strip()}"
            for _c, n, v, _ok in getattr(raw, "fields", []) or []:
                if (n or "").lower() in ("name", "default name") and str(v).strip():
                    return f"name:{str(v).strip()}"
            path = getattr(raw, "path", None) or self.path
            return f"{path}:{label}"
        return label

    # --- toolbar actions (queue mode, local only) ---------------------

    def _toolbar_action(self, name: str) -> None:
        if self.readonly:
            QMessageBox.information(
                self,
                name,
                "Read-only mode is on.\n\n"
                "Uncheck Read-only in the toolbar to use queue mode "
                "(changes stay local and are not sent to the router).",
            )
            return
        if name == "Add":
            self._action_add()
            return
        items = self._require_selection(name)
        if items is None:
            return
        if name == "Remove":
            self._action_remove(items)
        elif name == "Enable":
            self._action_set_disabled(items, disabled=False)
        elif name == "Disable":
            self._action_set_disabled(items, disabled=True)
        elif name == "Comment":
            self._action_comment(items)
        else:
            QMessageBox.information(self, name, f"'{name}' is not implemented yet.")

    def _live(self) -> bool:
        """Whether a write from this pane goes to the router now.

        Asked at build time (the toolbar tips and the status tag) as well as
        at write time, so it has to answer for a pane with no session at all.
        Both unknowns fall to False, which is the direction that cannot hurt:
        the pane queues locally instead of sending something nobody asked it
        to send.
        """
        return getattr(self.edit_session, "live", False)

    @staticmethod
    def _obj_id(raw: Any) -> int | None:
        """The router's `.id` for a row, which every write but ADD needs."""
        getter = getattr(raw, "object_id", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None

    def _enqueue(
        self,
        action: str,
        row_key: str,
        values: dict[str, Any],
        edits: list[dict[str, Any]] | None = None,
        title: str | None = None,
        *,
        obj_id: int | None = None,
    ) -> bool:
        """Send the change in Live mode, hold it locally otherwise.

        False means the row was left alone — in Live mode the caller must not
        patch its copy of the row, because nothing on the device changed.
        """
        path = self.path or ()
        change = QueuedChange(
            path=path,
            title=title or f"{self.title} — {action}",
            values=values,
            row_key=row_key,
            obj_id=obj_id,
            edits=edits or [],
            action=action,
        )
        if self._live():
            try:
                self._last_write = self.edit_session.apply(change)
            except Exception as e:  # noqa: BLE001
                # Verbatim: The router answers every business-rule refusal with
                # FAILED, so its string is the only thing carrying the reason.
                QMessageBox.warning(
                    self,
                    action.capitalize(),
                    f"The router refused the change:\n\n{e}",
                )
                return False
            return True
        if self.edit_session is not None:
            self.edit_session.enqueue(change)
        self.queue_changed.emit()
        return True

    # --- reordering ----------------------------------------------------

    def _set_row_drag(self, on: bool) -> None:
        """Turn row dragging on in both halves — the view and the model.

        Qt checks the model's item flags before it will start a drag or allow
        a drop, so configuring only the view leaves dragging silently inert.
        """
        self.table.enable_row_drag(on)
        setter = getattr(self._model, "set_row_drag_enabled", None)
        if setter is not None:
            setter(on)

    def _device_order_ids(self) -> list[int | None]:
        """Object ids in the device's own order, which is what a move works on."""
        return [self._obj_id(raw) for raw in self._raw_rows]

    def _is_dynamic_row(self, index: int) -> bool:
        """Whether the row at `index` is one the device manages itself."""
        if not (0 <= index < len(self._raw_rows)):
            return False
        from x3.proto.schema import FLAG_DYNAMIC_CODE

        vals = getattr(self._raw_rows[index], "values_by_code", None) or {}
        return vals.get(FLAG_DYNAMIC_CODE) in (True, 1)

    def _first_movable_index(self) -> int:
        """The highest position a rule can be moved to.

        The router keeps its own rules at the top of an ordered table and refuses
        to insert ahead of them — ``cannot move builtin``. Nothing on the wire
        marks them beyond the dynamic flag, so the boundary is the first row
        that is not dynamic.
        """
        for i in range(len(self._raw_rows)):
            if not self._is_dynamic_row(i):
                return i
        return len(self._raw_rows)

    def _in_device_order(self) -> bool:
        """Whether what is on screen is the device's order, unsorted, unfiltered.

        A drop target only means something when the rows are in the order the
        router evaluates them. Sorted by Action or filtered to three rows,
        "put it here" has no answer.
        """
        return self._sort_col is None and not self._filter.text().strip()

    def _selected_source_rows(self) -> list[int]:
        """Selected rows as indices into the device-ordered backing list."""
        out = []
        for vr in sorted({i.row() for i in self.table.selectionModel().selectedRows()}):
            src = self._model.source_row(vr)
            if src is not None:
                out.append(src)
        return sorted(set(out))

    def _move_selected(self, delta: int) -> None:
        """Move the selected rule one place up or down."""
        rows = self._selected_source_rows()
        if len(rows) != 1:
            QMessageBox.information(
                self, "Move", "Select exactly one rule to move."
            )
            return
        src = rows[0]
        target = src + delta
        if not (0 <= target < len(self._raw_rows)):
            return
        self._move_row(src, target)

    def _move_row(self, src: int, target: int) -> None:
        """Move the rule at `src` so it sits at `target` in device order."""
        ids = self._device_order_ids()
        if not (0 <= src < len(ids)) or src == target:
            return
        moving = ids[src]
        if moving is None:
            QMessageBox.warning(self, "Move", "That row has no id to move.")
            return
        if self._is_dynamic_row(src):
            QMessageBox.information(
                self,
                "Move",
                "That rule is dynamic — the router manages it and will not "
                "move it.",
            )
            return
        # The router keeps its own rules at the top and refuses to insert
        # ahead of them ("cannot move builtin"). Land on the first position it
        # will accept rather than sending a move it is going to reject.
        floor = self._first_movable_index()
        clamped = max(target, floor)
        if clamped == src:
            # Already as high as the router allows; sending it would be a
            # no-op the device answers with an error.
            self._status.setText(
                f"already first below the dynamic rules (position {src})"
            )
            return
        before = move_anchor(ids, src, clamped)
        if not self._enqueue(
            "move",
            row_key=str(moving),
            values={"_before": before},
            title=f"{self.title} — move",
            obj_id=moving,
        ):
            return
        note = ""
        if clamped != target:
            note = f" — stopped at {clamped}, the rules above it are dynamic"
        if self._live():
            self.reload()
            self._status.setText(f"moved on the device{note}")
        else:
            self._status.setText(
                f"{len(self._all_rows)} row(s) — move queued{note}"
            )

    def _drop_move(self, src_view_row: int, target_view_row: int) -> None:
        """Handle a drag-and-drop reorder from the table."""
        if not self._ordered or self.readonly:
            return
        if not self._in_device_order():
            QMessageBox.information(
                self,
                "Move",
                "Clear the sort and the filter first.\n\n"
                "Dragging only means something while the list is in the "
                "router's own order — that is the order the rules are "
                "evaluated in.",
            )
            return
        src = self._model.source_row(src_view_row)
        target = self._model.source_row(target_view_row)
        if src is None or target is None:
            return
        self._move_row(src, target)

    def _action_add(self) -> None:
        """Open a blank property form for a new object (queued on save)."""
        path = self.path or ()
        if self._on_open_properties is not None:
            # Build an empty RowObject-like shell via a minimal fake if needed
            try:
                from x3.proto.device import RowObject
                from x3.proto import message as m

                empty = RowObject(path=path, raw=m.Message(), fields=[], form_fields=[])
                # Prefill the new row: the interface type (steers
                # open_property_window to the type's menu -- so the editable VLAN
                # fields show, not the generic interface's read-only status --
                # and, carried into the ADD, tells the device which kind to make)
                # and a default name like Winbox's "vlan1" (next free).
                if self._menu is not None:
                    from x3.proto.schema import name_field_code, type_field_code

                    prefill: dict[int, Any] = {}
                    if self.type_value is not None:
                        tfc = type_field_code(self._menu) or 0x10001
                        prefill[tfc] = self.type_value
                    stem = getattr(self._menu, "cli_prefix", None)
                    ncode = name_field_code(self._menu)
                    if stem and ncode is not None:
                        prefill[ncode] = self._next_default_name(stem, ncode)
                    else:
                        # A name that is a dropdown rather than a string: open
                        # on the first entry no row has taken (ether2 once
                        # ether1 is a port). No menu declares both, so the two
                        # rules never fight over the same field.
                        free = self._next_free_nameval(ncode)
                        if free is not None:
                            prefill[ncode] = free
                    # Schema field defaults from the .jg: plain numbers (VLAN ID
                    # 1, MTU 1500), booleans (Store on Disk = 1) and enum
                    # defaults, including a defenum's synthetic entry (Interface
                    # = "all", Frequency = "auto").
                    for code, fld in self._menu.fields.items():
                        if fld.ro:
                            continue
                        if isinstance(fld.default, int):
                            prefill.setdefault(code, fld.default)
                        # The .jg's other add-time defaults: a companion mask
                        # or prefix length (an IPv6 address gets a /64), a
                        # range's two ends, and the four computed ones -- a
                        # date of today, a time of now, a random MAC, the
                        # logged-in user.
                        if fld.default_mask is not None and fld.maskid:
                            prefill.setdefault(fld.maskid, fld.default_mask)
                        if fld.default_low is not None:
                            prefill.setdefault(code, fld.default_low)
                        if fld.default_high is not None and fld.highid:
                            prefill.setdefault(fld.highid, fld.default_high)
                        computed = self._computed_default(fld)
                        if computed is not None:
                            prefill.setdefault(code, computed)
                    for code, val in prefill.items():
                        empty.values_by_code[code] = val
                    # Form skeleton from the schema, showing the prefilled values.
                    form_fields = []
                    for tab, code in self._menu.form_order:
                        fld = self._menu.fields.get(code)
                        if fld is None or fld.ro:
                            continue
                        # Rendered, not str(): the form shows a prefilled
                        # default the way it shows the same field on an object
                        # that already exists -- a bridge port's Priority as
                        # the hex 0x80 the .jg asks for, not as 128.
                        val = prefill.get(code)
                        form_fields.append((
                            tab, code, fld.name or f"<{code:#x}>",
                            "" if val is None else fld.render(val), True,
                        ))
                    empty.form_fields = form_fields
                    empty.fields = [(c, n, v, ok) for _t, c, n, v, ok in form_fields]
                self._on_open_properties(
                    path,
                    empty,
                    f"{self.title} — (new)",
                    "(new)",
                    menu_name=self.menu_name,
                )
                return
            except Exception as e:
                QMessageBox.warning(self, "Add", f"Could not open add form:\n{e}")
                return
        # Fallback: queue an empty add
        self._enqueue("add", "(new)", {"_new": True}, title=f"{self.title} — add")
        QMessageBox.information(
            self,
            "Add",
            "Queued a new object locally (no form available).\n"
            "Nothing was sent to the router.",
        )

    def _action_remove(self, items: list[tuple[int, list[Any], Any | None]]) -> None:
        sent = 0
        for vr, values, raw in items:
            key = self._row_key(values, raw)
            if not self._enqueue(
                "remove",
                key,
                {"_removed": True, "_label": self._row_label(values, raw)},
                title=f"{self.title} — remove {self._row_label(values, raw)}",
                obj_id=self._obj_id(raw),
            ):
                continue
            sent += 1
        if self._live():
            # The reload is the truth; do not also guess locally.
            self.reload()
            self._status.setText(f"removed {sent} of {len(items)} on the device")
            return
        # Kept, not deleted: a queued removal is drawn struck through until
        # it is sent (see _overlay_queue). Deleted here, it looked done -- and
        # the next poll brought the row back as if nothing were queued.
        self._rebuild_view()
        self._status.setText(
            f"{len(self._all_rows)} row(s) — removal of {len(items)} queued"
        )

    def _action_set_disabled(
        self,
        items: list[tuple[int, list[Any], Any | None]],
        *,
        disabled: bool,
    ) -> None:
        action = "disable" if disabled else "enable"
        col = self._find_column("Disabled")
        flags_col = self._model.flags_column
        sent = 0
        for vr, values, raw in items:
            key = self._row_key(values, raw)
            label = "true" if disabled else "false"
            edits = [{
                "code": 0xFE000A,  # the standard disabled flag
                "name": "Disabled",
                "new": disabled,
                "label": label,
            }]
            if not self._enqueue(
                action,
                key,
                {"Disabled": disabled},
                edits=edits,
                title=f"{self.title} — {action} {self._row_label(values, raw)}",
                obj_id=self._obj_id(raw),
            ):
                continue
            sent += 1
            if self._live():
                continue
            row_data = list(self._model.row_values(vr))
            # Update Disabled text column if shown
            if col is not None and col < len(row_data):
                row_data[col] = label
            # Update Winbox flags letter column (X = disabled)
            if flags_col is not None and flags_col < len(row_data):
                flags = str(row_data[flags_col] or "")
                if disabled and "X" not in flags:
                    # X is usually first
                    row_data[flags_col] = "X" + flags.replace("X", "")
                elif not disabled:
                    row_data[flags_col] = flags.replace("X", "")
            src = self._model.source_row(vr)
            if src is not None and src < len(self._all_rows):
                self._all_rows[src] = self._base_of(row_data)
            self._model.replace_row(vr, row_data)
            self._model.set_row_disabled(vr, disabled)
            # Keep wire values so reopened forms / re-render stay consistent
            if raw is not None:
                self._patch_raw_field(raw, "Disabled", label, disabled)
                if hasattr(raw, "values_by_code") and isinstance(raw.values_by_code, dict):
                    from x3.proto.schema import FLAG_DISABLED_CODE
                    raw.values_by_code[FLAG_DISABLED_CODE] = True if disabled else False
        if self._live():
            self.reload()
            self._status.setText(f"{action}d {sent} of {len(items)} on the device")
            return
        self._status.setText(
            f"{len(self._all_rows)} row(s) — {action} of {len(items)} queued"
        )

    def _action_comment(self, items: list[tuple[int, list[Any], Any | None]]) -> None:
        # Start from what is there, as the form's Comment button does: Ctrl+M
        # on a commented row opened an empty box, so fixing a typo meant
        # retyping the whole comment. Several rows prefill only when they
        # agree -- an edit must not quietly copy one row's text onto the rest.
        current = {self._comment_of(raw) for _vr, _values, raw in items}
        start = current.pop() if len(current) == 1 else ""
        text, ok = QInputDialog.getText(self, "Comment", "Comment:", text=start)
        if not ok:
            return
        col = self._find_column("Comment")
        sent = 0
        for vr, values, raw in items:
            key = self._row_key(values, raw)
            if not self._enqueue(
                "comment",
                key,
                {"Comment": text},
                edits=[{
                    "code": 0xFE0009,  # STD_DESCR
                    "name": "Comment",
                    "old": self._comment_of(raw),
                    "new": text,
                    "label": text,
                }],
                title=f"{self.title} — comment {self._row_label(values, raw)}",
                obj_id=self._obj_id(raw),
            ):
                continue
            sent += 1
            if self._live():
                continue
            src = self._model.source_row(vr)
            if src is not None and src < len(self._all_comments):
                self._all_comments[src] = text
            row_data = list(self._model.row_values(vr))
            if col is not None and col < len(row_data):
                row_data[col] = text
                if src is not None and src < len(self._all_rows):
                    self._all_rows[src] = self._base_of(row_data)
                self._model.replace_row(vr, row_data, comment=text)
            else:
                # No column to write: the banner is what shows it.
                self._model.set_comment(vr, text)
            # The first comment on a list is what the banner delegate is for,
            # and it has to be in place before the row is asked for its height.
            self._choose_item_delegate()
            if not self._inline:
                self.table.resizeRowToContents(vr)
            if raw is not None:
                self._patch_raw_field(raw, "Comment", text, text)
        if self._live():
            self.reload()
            self._status.setText(
                f"commented {sent} of {len(items)} on the device"
            )
            return
        self._status.setText(
            f"{len(self._all_rows)} row(s) — comment on {len(items)} queued"
        )

    def _find_column(self, name: str) -> int | None:
        for i, c in enumerate(self._columns):
            if c.lower() == name.lower():
                return i
        return None

    def _patch_raw_field(self, raw: Any, name: str, rendered: str, wire: Any) -> None:
        """Update decoded field lists on a RowObject so reopened forms match."""
        new_fields = []
        found = False
        for code, n, v, ok in raw.fields:
            if n.lower() == name.lower():
                new_fields.append((code, n, rendered, ok))
                found = True
            else:
                new_fields.append((code, n, v, ok))
        if not found:
            new_fields.append((0, name, rendered, True))
        raw.fields = new_fields
        if getattr(raw, "form_fields", None):
            new_form = []
            found = False
            for tab, code, n, v, ok in raw.form_fields:
                if n.lower() == name.lower():
                    new_form.append((tab, code, n, rendered, ok))
                    found = True
                else:
                    new_form.append((tab, code, n, v, ok))
            if not found:
                new_form.append(("General", 0, name, rendered, True))
            raw.form_fields = new_form
        if name.lower() == "comment" and hasattr(raw, "set_comment"):
            # The comment has a real wire code (STD_DESCR); everything else here
            # is menu-local, so we still don't invent codes for those.
            raw.set_comment(rendered)

    # --- properties / filter ------------------------------------------

    def _open_properties(self, index) -> None:
        if not index.isValid():
            return
        row = index.row()
        values = self._model.row_values(row)
        # row_values() is cleartext by design; the read-only fallback dialog has
        # no Field to inspect, so mask here rather than leak past the toggle.
        display = self._masked(values)
        overrides = dict(zip(self._columns, display))
        prop_fields: tuple = ()
        raw = self._raw_for_view_row(row, values)
        item_name = self._row_label(values, raw)
        if raw is not None:
            if self._on_open_properties is not None:
                # Title shows the object name (ether4); row_key must be unique
                # per object so opening ether5 does not focus ether4's window.
                self._on_open_properties(
                    raw.path or self.path,
                    raw,
                    f"{self.title} — {item_name}",
                    self._row_key(values, raw),
                    menu_name=self.menu_name,
                )
                return
            prop_fields = raw.property_tuples()
            overrides = None
        if not prop_fields:
            prop_fields = tuple(("General", c, v) for c, v in zip(self._columns, display))
        dlg = PropertyDialog(
            f"{self.title} — {item_name}",
            prop_fields,
            self,
            overrides=overrides,
            readonly=self.readonly,
        )
        dlg.exec()

    def _row_label(self, values: list, raw: Any = None) -> str:
        """Human name for titles / keys — never the Flags column.

        The .jg says what an object is called: `nameval` names the field, or a
        `concat` tree over several (a DHCP lease is `Address,Active Address`, a
        firewall rule `Src. Address:Src. Port->Dst. Address:Dst. Port`). Only
        when that comes back empty does the guessing below apply.
        """
        if self._menu is not None:
            by_name = {}
            if raw is not None:
                for entry in getattr(raw, "fields", None) or []:
                    by_name.setdefault(str(entry[1]), entry[2])
            for i, col in enumerate(self._columns):
                if i < len(values) and col:
                    by_name.setdefault(str(col), values[i])
            try:
                named = sch.object_label(self._menu, by_name).strip()
            except Exception:  # noqa: BLE001
                named = ""
            if named:
                return named
        prefer = ("Name", "Default Name", "Interface", "Address", "Src. Address")
        for col_name in prefer:
            if col_name in self._columns:
                i = self._columns.index(col_name)
                if i < len(values):
                    text = str(values[i]).strip()
                    if text:
                        return text
        for i, col in enumerate(self._columns):
            if col in ("", " ", "Flags") or i >= len(values):
                continue
            text = str(values[i]).strip()
            if text:
                return text
        for v in values:
            text = str(v).strip()
            if text and text not in ("true", "false"):
                return text
        return "row"

    def _raw_for_view_row(self, row: int, values: list | None = None) -> Any | None:
        """The device object a view row came from.

        The model carries the view→backing mapping, so this is exact even while
        a filter is active. It used to match on rendered cell values, which
        silently returned the *wrong* object whenever two rows rendered alike.
        """
        src = self._model.source_row(row)
        if src is None or src >= len(self._raw_rows):
            return None
        return self._raw_rows[src]
