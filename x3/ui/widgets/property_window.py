"""MDI property form (tabs, enums as combos) — queue-mode edits stay local."""

from __future__ import annotations

import html
import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QEvent, QPointF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from x3.proto import message as m
from x3.proto import schema as sch
from x3.proto.schema import _implied_enum_path as sch_implied_enum_path
from x3.proto.schema import opt_chain as sch_opt_chain

from .. import a11y, applog, style
from ..i18n import tr
from ..edit_mode import EditMode, EditSession, QueuedChange
from .mdi_window import MdiWindow
from .multi_pick import MultiPickDialog
from .network_array import NetworkArrayEditor
from .number_table import NumberGridEditor, NumberTableView
from .opt_match import OptMatchEditor
from .edit_marks import MarkedRows, apply_dirty_style, edit_marks
from .group_fold import GroupFold
from .radio_group import RadioGroupEditor, section_header
from .range_editor import RangeEditor
from .scalar_array import ScalarArrayEditor, is_scalar_multi
from .signal_bar import SignalBar
from .status_marks import MarkedCombo, apply_marks, marks_for
from .tristate_editor import MultiTristateEditor, SetEditor
from .union_editor import UnionEditor
from .wheel_guard import guard_wheel
from ..sortkey import sort_key
from ..settings import (
    TRAFFIC_POLL_MIN_MS,
    load_advanced_mode,
    load_form_status_line,
    load_titlebar_tabs,
    load_traffic_poll_ms,
    save_advanced_mode,
    save_traffic_poll_ms,
)
from ..icons import window_icon

if TYPE_CHECKING:
    from x3.proto.device import DeviceClient, RowObject
    from x3.proto.schema import Field, Menu

#: Air above and below a ``type:separator`` rule, so it reads as a break
#: between groups of fields rather than as one more (very short) row.
_RULE_PAD = 4

#: Gap between form rows. Winbox v3 leaves a little air between its controls;
#: the themes' own SPACING is tuned for table rows, which is tighter than a
#: form wants.
_FORM_ROW_GAP = 5

#: Left inset of a property form's label column. Winbox v3 keeps the labels
#: clear of the window edge -- unlike a list, which starts flush -- so the
#: General tab reads as a form rather than a table. Fields stretch, so the
#: inset costs nothing but whitespace.
_FORM_LEFT_INSET = 20
#: How close two `set` grids' natural column widths have to be before they are
#: treated as one grid. See :func:`_share_set_pitch`.
_PITCH_KIN = 12


#: Letters the main window's menu bar already answers for (File, Windows,
#: View, System, Settings, Help). An MDI subwindow is not a window of its own as
#: far as Qt's shortcut map is concerned -- its window *is* the main window -- so
#: a button mnemonic landing on one of these would be an ambiguous shortcut and
#: Qt would fire neither. Keep them out of the button stack.
_MENUBAR_MNEMONICS = frozenset("fwvysh")


def plain_label(text: str) -> str:
    """A button's label with its mnemonic marker taken back out.

    ``"Co&mment"`` -> ``"Comment"``, and a real ampersand (``"Download&&Go"``)
    survives as one. Anything measuring or matching a label wants this, not
    ``text()``.
    """
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "&":
            if i + 1 < len(text) and text[i + 1] == "&":
                out.append("&")
                i += 2
                continue
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _with_mnemonic(label: str, used: set[str]) -> str:
    """``"Apply"`` -> ``"&Apply"``: the first letter nobody else has claimed.

    Alt then underlines it, natively, and Alt+A presses the button -- the
    Windows behaviour Winbox itself never had, which is the only affordance on
    this form that says out loud which keys exist. Greedy and first-come, so the
    order the stack is built in decides who gets the obvious letter: OK takes O,
    Cancel takes C, and Comment, being third, ends up on the M.

    Any ampersand already in the label (a schema-declared "Download&Install")
    is doubled first, or Qt would read it as the marker.
    """
    escaped = label.replace("&", "&&")
    for i, ch in enumerate(escaped):
        low = ch.lower()
        if not ch.isalnum() or low in used or low in _MENUBAR_MNEMONICS:
            continue
        used.add(low)
        return escaped[:i] + "&" + escaped[i:]
    return escaped


def _first_tab_stop(widget: QWidget | None) -> QWidget | None:
    """The widget Tab would land on inside `widget`, or None if none would.

    A form row's editor is often a container -- an optional field's box plus
    its triangle, a union's alternatives, a grid of rate buttons -- so the
    thing that actually takes the keyboard is somewhere inside it.
    """
    if widget is None:
        return None
    for w in (widget, *widget.findChildren(QWidget)):
        try:
            if w.isHidden() or not w.isEnabled():
                continue
            if not (w.focusPolicy() & Qt.FocusPolicy.TabFocus):
                continue
        except RuntimeError:      # the C++ side went away with its page
            continue
        return w
    return None


class _TrafficPollWorker(QThread):
    """Background GETOBJ so the UI thread never blocks on the router."""

    finished_ok = Signal(object)
    finished_err = Signal(str)

    def __init__(self, fetcher: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self._fetcher = fetcher

    def run(self) -> None:  # noqa: N802
        try:
            self.finished_ok.emit(self._fetcher())
        except Exception as e:  # noqa: BLE001
            self.finished_err.emit(f"{type(e).__name__}: {e}")


class PropertyWindow(MdiWindow):
    """In-MDI settings form for one object or item menu."""

    queued = Signal(object)  # QueuedChange

    # Smallest the window may be dragged to. Below this the tab content scrolls
    # rather than the window refusing to shrink. Written at 100% and scaled by
    # the active zoom where they are applied.
    _MIN_W = 300
    _MIN_H = 180

    def __init__(
        self,
        title: str,
        path: tuple[int, ...],
        row: RowObject,
        *,
        client: DeviceClient | None,
        edit_session: EditSession,
        menu: Menu | None = None,
        row_key: str = "",
        show_passwords: bool = False,
        full_row: bool = False,
        on_context_action: Callable[..., None] | None = None,
        menu_buttons: list[dict] | None = None,
        on_open_menu: Callable[..., None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Winbox v3 Settings > Show Passwords. Read before the form is built:
        # _make_editor picks the echo mode from it.
        self._show_pw = bool(show_passwords)
        # Winbox's Simple Mode / Advanced Mode button (menu.mode_toggle). Read
        # before the form is built: it decides which rows and tabs exist.
        self._advanced = load_advanced_mode()
        self._mode_btn: QPushButton | None = None
        #: Where a `type:toolbar` button's action goes -- the main window opens
        #: the menu it names, which a form cannot do for itself.
        self._on_context_action = on_context_action
        #: Sibling menus this form is the window for: the .jg declares them
        #: after it, and Winbox v3 puts one button per menu down this form's
        #: right-hand side (System ▸ Resources ▸ Hardware / CPU / IRQ). Each
        #: entry is a nav payload, opened by _on_open_menu.
        self._menu_buttons = [dict(b) for b in (menu_buttons or [])]
        self._on_open_menu = on_open_menu
        #: Caller handed us an already-unfiltered object; see _bootstrap_full_row.
        self._full_row = bool(full_row)
        # Live traffic poll interval for this window, chosen in the graph header.
        self._poll_ms = load_traffic_poll_ms()
        # View > Form Status Line: whether the mode / unsaved-edits line across
        # the top of the form is drawn at all. It is read here, before the line
        # is built, and can be flipped on an open form (see set_status_line).
        self._status_line = load_form_status_line()
        self.path = path
        self.row = row
        self.client = client
        self.edit_session = edit_session
        self.menu = menu
        self.row_key = row_key
        #: Changes this form put on the session queue. Kept so Apply all --
        #: which sends from the main window -- can tell this form its change
        #: landed; see :meth:`sync_after_external_write`.
        self._queued_changes: list[QueuedChange] = []
        self.setWindowIcon(window_icon("props"))
        self._editors: dict[str, QWidget] = {}
        self._field_meta: dict[str, Field | None] = {}
        # (spec, editor) in form order; the authoritative list, since two rows
        # can share a label and a name-keyed dict would lose one.
        self._rows: list[tuple[dict[str, Any], QWidget]] = []
        # code → widget(s) to refresh on live traffic poll
        # code → (field name, widget) to refresh on the live traffic poll. The
        # name is part of the key because one id can be two fields (see
        # Menu.field_for), and a poll must not paint a byte counter into the
        # other one's box.
        self._live_labels: dict[int, list[tuple[str, QLabel]]] = {}
        # (QFormLayout, row index, condition name) for hidedynamicly rows
        self._cond_rows: list[tuple[Any, int, str]] = []
        #: (spec, editor) for rows a flagless / `oron` condition greys out.
        self._gate_rows: list[tuple[dict[str, Any], Any]] = []
        #: (id(QFormLayout), row index) → the GroupFold that hides it. A row
        #: inside a closed fold stays hidden whatever its condition says, so
        #: both passes go through _set_row_visible -- see GroupFold.
        self._folded_rows: dict[tuple[int, int], Any] = {}
        #: The same rows, keeping the layout itself, for the refresh sweep.
        self._fold_row_index: list[tuple[Any, int]] = []
        #: code → numbar widgets. A bar wants the number, not the rendered
        #: text every other live value is repainted from.
        self._live_bars: dict[int, list[Any]] = {}
        #: code → reported-list boxes. Like a bar, these are rebuilt from the
        #: array rather than from the one line the row rendered as.
        self._live_lists: dict[int, list[Any]] = {}
        #: (field, combo) for `queryenum` dropdowns, and the parameters the
        #: device was last asked with. The answer depends on other fields of
        #: this same form (a frequency list is per band, width and country), so
        #: it is re-asked when one of those changes -- and only then.
        self._query_enums: list[tuple[Field, Any]] = []
        self._query_enum_params: tuple | None = None
        # Build-time memoisation. Laying the form out walks the .jg template
        # once per row -- ~190 of them on a wireless interface -- and each row
        # used to re-evaluate its tab's visibility, its on: condition, the
        # chrome-code set, and a linear scan of all ~190 decoded wire fields.
        # None of those change while the form is being built, so they are
        # cached per name/tab/code instead. Dropped whenever the row is
        # replaced (see _forget_row_caches).
        self._cond_ok_cache: dict[str, bool] = {}
        self._cond_dyn_cache: dict[str, bool] = {}
        self._cond_hide_cache: dict[str, bool] = {}
        self._cond_gate_cache: dict[str, str | None] = {}
        self._tab_ok_cache: dict[str, bool] = {}
        self._chrome_cache: set[int] | None = None
        #: The object's inherited values (see _object_defaults), read once.
        self._defaults_cache: dict[int, Any] | None = None
        #: The device's capability bits, asked for once (see _syscaps).
        self._syscaps_cache: Any = _UNSET
        self._chrome_decl_cache: set[tuple[int, str]] | None = None
        #: (QFormLayout, row index, spec) for `hideonempty` counters. Their
        #: values arrive on the refresh poll, not on the read that opens the
        #: form, so whether each row exists is re-decided every poll rather
        #: than once at build -- see _refresh_empty_rows.
        self._empty_rows: list[tuple[Any, int, dict[str, Any]]] = []
        #: Tabs carrying those counters. Like Traffic, they are worth polling
        #: while they are the tab on screen.
        self._live_tabs: set[str] = set()
        self._row_index_cache: dict[int, list[tuple]] | None = None
        # Suppresses _on_tab_changed while we nudge the tab bar's scroll.
        self._tab_scroll_guard = False
        self._graph_widgets: list[TrafficGraphWidget] = []
        self._tabs: QTabWidget | None = None
        #: holder widget → (name, specs) for pages not built yet. Keyed by the
        #: widget, not the tab index: a tab that comes and goes with a
        #: condition (NV2, Data Rates) shifts every index after it.
        self._pending_pages: dict[Any, tuple[str, list[dict[str, Any]]]] = {}
        #: (tab name, holder, label) for every tab in schema order, including
        #: the ones currently removed from the QTabWidget.
        self._all_tabs: list[tuple[str, Any, str]] = []
        #: Tabs whose condition can flip while the form is open.
        self._dynamic_tabs: set[str] = set()
        #: How many of _rows already have their dirty signals connected. Pages
        #: arrive over time, so wiring walks forward from here instead of
        #: re-connecting every editor it has seen before.
        self._wired = 0
        self._from_path: tuple[int, int] | None = None
        self._obj_id: int | None = None
        self._poll_timer: QTimer | None = None
        self._poll_worker: _TrafficPollWorker | None = None
        self._poll_busy = False
        #: The .jg ``autorefresh`` re-read (see _setup_menu_refresh). Its own
        #: timer: the traffic poll's stops with the tab that owns it, and this
        #: one belongs to the whole window.
        self._refresh_timer: QTimer | None = None
        self._refresh_worker: _TrafficPollWorker | None = None
        self._live_status: QLabel | None = None
        # When True, close without the unsaved-changes prompt (after Queue).
        self._allow_close = False
        self._dirty_count = 0
        #: Set once an Add lands, so later saves edit that object rather than
        #: creating another one. See _adopt_new_object.
        self._added_obj_id: int | None = None

        # Prefer a full GETOBJ before building the form: list GETALL is sparse
        # (DHCP Status IP/Gateway, Interface Traffic rates often missing).
        # Phase timings go to the log: "the form is slow to open" is a report
        # about a sum, and the parts (one object read, one burst of dropdown
        # sources, the widgets) are worth telling apart.
        t_open = time.perf_counter()
        self._bootstrap_full_row()
        t_row = time.perf_counter()
        # Every dropdown this form needs, read as one burst before any editor
        # asks for its own.
        self._prefetch_enum_sources()
        t_enums = time.perf_counter()

        # A dynamic row is device-managed -- a DHCP-client address, a route
        # learned from a routing protocol. Winbox lets you copy one and try to
        # remove it, never edit it, and the router refuses the write in any
        # case. So this form is locked whatever mode the session is in, and
        # stays locked when the mode changes (see set_readonly).
        from x3.proto.schema import FLAG_DYNAMIC_CODE

        by_code = getattr(self.row, "values_by_code", None) or {}
        self._dynamic = by_code.get(FLAG_DYNAMIC_CODE) in (True, 1)

        # `.jg` ``ro:1`` on the menu itself — System ▸ Resources, About, Log,
        # Firewall ▸ Connections. The whole menu is device output, so the form
        # is locked for the same reason a dynamic row is: not a mode you can
        # switch out of. Every field under it already came back ``ro``; this
        # is what also takes Cancel and Apply off the button stack (see
        # _build_action_column).
        self._menu_ro = bool(getattr(self.menu, "read_only", False))

        ro = edit_session.readonly or self._dynamic or self._menu_ro
        self._ro = ro
        self._base_title = title
        self.setWindowTitle(title + self._mode_suffix(ro))
        # "I cannot edit this" has several possible causes — session mode, a
        # device-managed row, a schema-read-only field — and the form alone
        # does not say which. Record it so the answer is in the log.
        applog.note(
            f"{title}: form opened "
            f"{'locked' if ro else 'editable'} "
            f"(mode={getattr(edit_session, 'mode', '?')}, dynamic={self._dynamic})"
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Alt+<letter> on the button stack belongs to the form in front; see
        # _sync_mnemonics for why it cannot simply be baked into the labels.
        self.windowStateChanged.connect(self._on_window_state)

        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(style.MARGIN, style.MARGIN, style.MARGIN, style.MARGIN)
        root.setSpacing(style.SPACING)

        self._mode_lab = QLabel()
        self._mode_lab.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)};")
        root.addWidget(self._mode_lab)

        # Winbox layout: form (tabs) on the left, action buttons on the right.
        body = QHBoxLayout()
        body.setSpacing(max(6, style.SPACING))
        form_col = QVBoxLayout()
        form_col.setSpacing(style.SPACING)

        specs = self._build_specs()
        tabs_data: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for spec in specs:
            tabs_data.setdefault(spec["tab"], []).append(spec)
        # Drop empty chrome-only tabs and tabs Winbox would hide (PoE/SFP/…).
        tabs_data = self._filter_visible_tabs(tabs_data)

        if not tabs_data:
            form_col.addWidget(QLabel("(no fields)"))
        elif len(tabs_data) == 1:
            only = next(iter(tabs_data))
            form_col.addWidget(self._scrollable(
                self._form_page(tabs_data[only], ro, tab_name=only)
            ), 1)
        else:
            tabs = QTabWidget()
            self._tabs = tabs
            # Keep schema tabs first; park diagnostics last
            tail = ("Flags", "Extra", "Unknown")
            order = [t for t in tabs_data if t not in tail] + [
                t for t in tail if t in tabs_data
            ]
            # Each tab starts as an empty holder and is filled the first time it
            # is looked at (see _ensure_page). Building all eleven of a WiFi
            # interface's tabs up front is about 120 ms of widget construction
            # the user waits through to look at one of them.
            for tab_name in order:
                items = tabs_data[tab_name]
                label = tab_name
                if tab_name == "Unknown":
                    label = f"Unknown ({len(items)})"
                holder = QWidget()
                hlay = QVBoxLayout(holder)
                hlay.setContentsMargins(0, 0, 0, 0)
                hlay.setSpacing(0)
                self._pending_pages[holder] = (tab_name, items)
                self._all_tabs.append((tab_name, holder, label))
                tabs.addTab(holder, label)
            # Tabs a condition can close again (Data Rates in Simple Mode, NV2
            # on a non-nv2 protocol) are built, then removed if they should not
            # be showing right now.
            self._refresh_conditional_tabs()
            tabs.currentChanged.connect(self._on_tab_changed)
            form_col.addWidget(tabs, 1)
            self._ensure_page(tabs.currentIndex())
            # Winbox-4 tab strip, same opt-in as a list window's. Registered
            # even when the setting is off, so the toggle can act on this
            # window later without reopening it.
            self.register_tab_strip(tabs, "")

        # Winbox has no such line, and everything a healthy poll could report
        # is already on the Traffic tab: the "Update:" combo carries the rate,
        # each graph its own Tx/Rx readout and timestamp. So this stays hidden
        # and only speaks up for what the tab cannot show -- a failed poll, or
        # a counter frozen under the cursor.
        self._live_status = QLabel("")
        self._live_status.setStyleSheet(f"color: {style.TEXT_DIM}; font-size: {style.small_css(8)};")
        self._live_status.setVisible(False)
        form_col.addWidget(self._live_status, 0)
        # No trailing stretch: an expanding spacer here swallowed every spare
        # pixel, so the form never grew with the window and the traffic graphs
        # stayed at their minimum height however large the window got.

        body.addLayout(form_col, 1)
        self._action_btns: list[QPushButton] = []
        self._write_action_btns: list[QPushButton] = []
        self._enable_btn: QPushButton | None = None
        self._queue_btn: QPushButton | None = None  # kept for set_readonly compat
        body.addWidget(self._build_action_column(), 0)
        root.addLayout(body, 1)

        # Winbox footer: "Enabled  ·  Status: bound" (from type:statusbar / enable).
        self._form_footer = QLabel("")
        self._form_footer.setStyleSheet(
            f"color: {style.TEXT}; background: {style.BG_HEADER}; "
            f"border-top: 1px solid {style.BORDER}; padding: 3px 6px; font-size: {style.small_css(9)};"
        )
        # Flag chips are greyed when their bit is clear, so the tray is markup.
        self._form_footer.setTextFormat(Qt.RichText)
        root.addWidget(self._form_footer)

        self.setWidget(container)
        # Winbox clamps a property window at a floor and scrolls the content
        # below it, rather than letting the tallest tab dictate the minimum.
        # Both go through the zoom: a form opened while zoomed in has to open
        # proportionally larger, or the bigger content is crammed into the 100%
        # size. This is the "next time that window opens" that
        # MainWindow._refresh_open_metrics leaves to the constructor.
        self.setMinimumSize(style.scale_px(self._MIN_W), style.scale_px(self._MIN_H))
        self.resize(self._preferred_width(), style.scale_px(440))
        self.set_readonly(ro)
        self._setup_live_traffic()
        self._setup_menu_refresh()
        self._wire_dirty_tracking()
        self._refresh_conditional_rows()
        self._query_enum_params = tuple(
            (fld.code, tuple(sorted(self._query_enum_params_for(fld).items())))
            for fld, _combo in self._query_enums
        )
        self._update_form_footer()
        self._refresh_enable_button()
        # Escape closes the form (with unsaved-change prompt when dirty); the
        # MdiWindow base handles the key.
        if self._tabs is not None:
            self.set_titlebar_tabs(load_titlebar_tabs())

        done = time.perf_counter()
        pages = len(self._pending_pages)
        applog.note(
            f"{title}: opened in {1000 * (done - t_open):.0f} ms"
            f" (object {1000 * (t_row - t_open):.0f}"
            f" + dropdown sources {1000 * (t_enums - t_row):.0f}"
            f" + widgets {1000 * (done - t_enums):.0f} ms"
            + (f", {pages} tab(s) built on demand)" if pages else ")")
        )

    # -- keyboard: change a form without reaching for the mouse -------------

    def keyPressEvent(self, e) -> None:  # noqa: N802
        """Enter is OK and Ctrl+Enter is Apply -- the tab keys are elsewhere.

        Neither of these is a ``QShortcut``, deliberately. Key events
        propagate up from the focus widget, so this only ever sees what the
        editors did not want: a multi-line box keeps its own Return, an open dropdown gets Enter
        first (a popup is a window of its own), and every single-line editor
        *ignores* Return -- which is exactly the mechanism a dialog's default
        button rides on. A shortcut fires ahead of the focus widget instead, and
        would take the key away from whatever is being typed into. The tab keys
        (Ctrl+PageDown/PageUp) are the other way round and are shortcuts for
        exactly that reason -- see ``MdiWindow._install_tab_keys``. What this
        form adds to them is where the keyboard lands afterwards, which is
        :meth:`focus_target`.

        Ctrl+Tab is not ours either: QMdiArea already owns it (it cycles
        windows), which is the more useful thing for it to mean.
        """
        key = e.key()
        # Keypad Enter arrives with KeypadModifier set; it is the same key.
        mods = e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if mods == Qt.KeyboardModifier.NoModifier:
                e.accept()
                # A focused button answers for itself; Enter anywhere else on
                # the form means "commit this form", the way OK does.
                if not self._press_focused_button():
                    self._action_ok()
                return
            if mods == Qt.KeyboardModifier.ControlModifier:
                e.accept()
                self._action_apply()
                return
        super().keyPressEvent(e)

    def _press_focused_button(self) -> bool:
        """Enter on a button in this form presses that button, not OK.

        Checkboxes and radio buttons are left out: Space toggles those, and
        Enter on one should still mean what Enter means everywhere else here.
        """
        fw = QApplication.focusWidget()
        if (
            isinstance(fw, (QPushButton, QToolButton))
            and fw.isEnabled()
            and (fw is self or self.isAncestorOf(fw))
        ):
            fw.click()
            return True
        return False

    def _is_front(self) -> bool:
        """Is this the subwindow the keyboard is aimed at right now?"""
        return bool(self.windowState() & Qt.WindowState.WindowActive)

    def _label_text(self, plain: str) -> str:
        """A button label as it should read right now.

        With its Alt letter marked while this form is the one in front, plain
        otherwise. A label seen for the first time (the Enable button flips to
        "Disable", the mode button between Simple and Advanced) is given its
        letter here, once, so relabelling never drifts onto a new one.
        The letter is picked from the label as it reads in the active language.
        """
        plain = tr(plain)
        text = self._mnemonic_texts.get(plain)
        if text is None:
            text = _with_mnemonic(plain, self._mnemonics)
            self._mnemonic_texts[plain] = text
        return text if self._is_front() else plain

    def _on_window_state(self, _old, new) -> None:
        self._sync_mnemonics(bool(new & Qt.WindowState.WindowActive))

    def _sync_mnemonics(self, active: bool) -> None:
        """Mark (or unmark) the buttons' Alt letters as this form comes forward.

        Qt mnemonics are *window* shortcuts, and an MDI subwindow is not a
        window as far as the shortcut map is concerned -- its window is the main
        one. Two open forms would therefore both answer Alt+O, which Qt calls an
        ambiguous shortcut and settles by moving the focus instead of pressing
        anything: the key would quietly stop working the moment a second form
        was open. Only the form in front wears its letters, so Alt+O always
        presses the OK you are looking at -- and the underlines Alt draws appear
        on that form alone.
        """
        for btn in getattr(self, "_action_btns", None) or ():
            try:
                plain = plain_label(btn.text())
                btn.setText(self._mnemonic_texts.get(plain, plain) if active
                            else plain)
            except RuntimeError:      # button went away with its window
                pass

    def focus_target(self):
        """Where the keyboard lands when the form opens: the first field.

        Qt's own answer is the tab strip, which is the one part of a property
        form nobody opened it to change. Winbox lands in the first box, and the
        tabs are still one key away (Ctrl+PageUp/PageDown, or Shift+Tab back
        onto the strip, where Left/Right walk it).
        """
        first = self._first_field()
        if first is not None:
            return first
        if self._tabs is not None:
            return self._tabs.tabBar()
        return self.widget()

    def _current_tab_name(self) -> str | None:
        """The tab in front, by schema name -- not its label ("Unknown (3)")."""
        tabs = self._tabs
        if tabs is None:
            return None                     # single-page form: every row is on it
        current = tabs.currentWidget()
        for name, holder, _label in self._all_tabs:
            if holder is current:
                return name
        return None

    def _first_field(self) -> QWidget | None:
        """The first row on the tab in front that the keyboard can change.

        Read-only rows are skipped: a Default Name or a byte counter is a tab
        stop -- its text is worth selecting and copying -- but landing on one is
        not what opening the form was for. None means there is nothing editable
        on this tab at all (a locked session, a device-managed row), and the
        caller falls back to the tab strip.
        """
        tab = self._current_tab_name()
        for spec, editor in self._rows:
            if tab is not None and spec.get("tab") != tab:
                continue
            if spec.get("locked") or spec.get("always_locked"):
                continue
            w = _first_tab_stop(editor)
            if w is not None:
                return w
        return None

    def set_readonly(self, ro: bool) -> None:
        """Switch this already-open form between read-only and queue mode.

        Editors are re-locked in place rather than rebuilt, so flipping the
        toolbar toggle does not force you to close and reopen every window.

        Permanently locked fields (schema ``ro``, Unknown-tab wire leftovers)
        stay locked in both modes — they are device output / unmapped TLV
        values, not settings.

        A dynamic row stays locked through a mode change: the device owns it.
        """
        ro = ro or getattr(self, "_dynamic", False) or getattr(self, "_menu_ro", False)
        self._ro = ro
        self.setWindowTitle(self._base_title + self._mode_suffix(ro))
        self._show_mode_text(
            "Read-only — values from device" if ro else self._mode_text()
        )
        for btn in getattr(self, "_write_action_btns", None) or []:
            try:
                btn.setEnabled(not ro)
            except RuntimeError:
                pass
        vals = self._live_values()
        for spec, editor in self._rows:
            locked = (
                ro or bool(spec.get("always_locked")) or self._gate_off(spec, vals)
            )
            spec["locked"] = locked
            _set_locked(editor, locked)
        self._refresh_dirty_highlights()
        self._refresh_enable_button()

    def _show_mode_text(self, txt: str, tip: str = "") -> None:
        """Put the status line up, unless View has it switched off.

        One place decides, so a form that is told mid-edit to hide the line
        hides it: the three callers all say what the line *would* read.
        """
        lab = getattr(self, "_mode_lab", None)
        if lab is None:
            return
        lab.setText(txt)
        lab.setToolTip(tip)
        # No empty box when the line is blank, and none at all when it is off.
        lab.setVisible(bool(txt) and self._status_line)

    def set_status_line(self, show: bool) -> None:
        """View ▸ Form Status Line, on a form that is already open."""
        show = bool(show)
        if show == self._status_line:
            return
        self._status_line = show
        if self._ro:
            self._show_mode_text("Read-only — values from device")
        else:
            self._refresh_dirty_highlights()

    def set_show_passwords(self, show: bool) -> None:
        """Reveal or mask secret fields on an already-open form.

        Echo mode does not affect ``QLineEdit.text()``, so dirty tracking and
        the pending-change list are unaffected. ``ReadOnlyValueEdit`` is a
        QLineEdit subclass, so permanently locked secrets mask too.
        """
        from x3.proto.schema import is_secret

        self._show_pw = bool(show)
        mode = (
            QLineEdit.EchoMode.Normal if self._show_pw else QLineEdit.EchoMode.Password
        )
        for spec, editor in self._rows:
            if not is_secret(spec.get("field")):
                continue
            # An opt-wrapped secret (a tunnel's IPsec Secret) is a whole
            # control, not a line edit; it masks the box it owns.
            if isinstance(editor, OptMatchEditor):
                try:
                    editor.set_show_passwords(self._show_pw)
                except RuntimeError:
                    pass
                continue
            if isinstance(editor, QLineEdit):
                try:
                    editor.setEchoMode(mode)
                except RuntimeError:
                    pass

    def _wire_dirty_tracking(self) -> None:
        """Connect editor signals so changed fields light up immediately.

        Only the rows added since the last call: a lazily built tab appends to
        ``_rows`` long after the form was constructed, and connecting an
        editor twice would fire the highlight pass twice per keystroke.
        """
        for spec, editor in self._rows[self._wired:]:
            if spec.get("always_locked"):
                continue
            self._connect_dirty_signal(editor)
        self._wired = len(self._rows)
        # Same moment, the other cross-row link: lists that may not hold the
        # same entry as each other.
        self._wire_exclusive_arrays()

    def _wire_exclusive_arrays(self) -> None:
        """Link array fields whose entries the router will not let overlap.

        Bridge ▸ VLANs' Tagged and Untagged: a port in both is a write the router
        refuses, so neither list offers what the other holds. *Which* fields
        those are is the schema's answer (:func:`schema.exclusive_array_peers`);
        the link is made here because only the form knows which editor is
        showing which field.

        Idempotent, and safe on a half-built form: tabs are built lazily, so
        the second of a pair may not exist yet when the first does -- the next
        page build calls this again, and ``add_peer`` ignores a link it has.
        """
        arrays: dict[int, MultiEnumArrayEditor] = {}
        for spec, editor in self._rows:
            code = spec.get("code")
            if isinstance(editor, MultiEnumArrayEditor) and isinstance(code, int):
                arrays[code] = editor
        if len(arrays) < 2:
            return
        for spec, editor in self._rows:
            if not isinstance(editor, MultiEnumArrayEditor):
                continue
            for peer in sch.exclusive_array_peers(self.menu, spec.get("field")):
                other = arrays.get(peer.code)
                if other is not None:
                    editor.add_peer(other)

    def _connect_dirty_signal(self, editor: QWidget) -> None:
        def changed(*_a) -> None:
            self._refresh_dirty_highlights()
            # A hidedynamicly condition may read this field (Auto Negotiation
            # drives Speed / Advertise), so re-run visibility on every edit --
            # for whole tabs too: NV2 comes and goes with Wireless Protocol.
            self._refresh_conditional_rows()
            self._refresh_conditional_tabs()
            self._refresh_query_enums()

        if isinstance(editor, MultiEnumArrayEditor):
            editor.changed.connect(changed)
            editor.rebased.connect(lambda e=editor: self._rebase_editor(e))
            return
        # Our own editors (radio group, set, range, array, …) all report
        # through `changed`, and a condition may well be reading one of them.
        signal = getattr(editor, "changed", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(changed)
            return
        if isinstance(editor, QLineEdit):
            editor.textChanged.connect(changed)
        elif isinstance(editor, QSpinBox):
            editor.valueChanged.connect(changed)
        elif isinstance(editor, QComboBox):
            editor.currentIndexChanged.connect(changed)
        elif isinstance(editor, QCheckBox):
            editor.toggled.connect(changed)

    def is_dirty(self) -> bool:
        return bool(self.edits())

    def _chrome_codes(self) -> set[int]:
        """Codes excluded from the form — driven by the .jg template."""
        if self._chrome_cache is not None:
            return self._chrome_cache
        self._chrome_cache = self._compute_chrome_codes()
        return self._chrome_cache

    def _chrome_decls(self) -> set[tuple[int, str]]:
        """(code, wire type) of the tray cells, when the schema knows them."""
        if self._chrome_decl_cache is None:
            decls = set()
            if self.menu is not None and hasattr(self.menu, "statusbar_decls"):
                try:
                    decls = self.menu.statusbar_decls()
                except Exception:
                    decls = set()
            self._chrome_decl_cache = decls
        return self._chrome_decl_cache

    def _is_chrome(self, code: int, prefix: str | None = None) -> bool:
        """Is this row the window tray's rather than the form's?

        By code alone unless the layout row names its wire type: an ethernet
        declares 0x44c twice -- `u44c` is the tray's link status, `Q44c` is
        "Rx Broadcast" on Rx Stats -- and only the tray's one is chrome.
        """
        if code not in self._chrome_codes():
            return False
        try:
            from x3.proto.schema import statusbar_owns
        except Exception:
            return True
        return statusbar_owns(code, prefix, self._chrome_codes(), self._chrome_decls())

    def _compute_chrome_codes(self) -> set[int]:
        if self.menu is not None and hasattr(self.menu, "form_chrome_codes"):
            return self.menu.form_chrome_codes()
        out: set[int] = {0xFE0001}
        for item in getattr(self.menu, "statusbar", None) or []:
            code = item.get("code")
            if isinstance(code, int):
                out.add(code)
            for c in item.get("codes") or []:
                if isinstance(c, int):
                    out.add(c)
        if getattr(self.menu, "has_enable", False):
            out.add(0xFE000A)
        return out

    def _render_field_value(self, code: int, *, capitalize_enum: bool = False) -> str:
        """Render one wire code from the current row (schema Field when known)."""
        rendered = ""
        for _c, _n, r, _ok in self._row_field_index().get(code, ()):
            rendered = r or ""
            break
        if not rendered:
            raw = self.row.raw_value(code) if hasattr(self.row, "raw_value") else None
            if raw is None:
                raw = self._row_values().get(code)
            fld = self.menu.fields.get(code) if self.menu else None
            if fld is not None and raw is not None:
                try:
                    rendered = fld.render(raw)
                except Exception:
                    rendered = str(raw)
            elif raw is not None:
                rendered = str(raw)
        text = (rendered or "—").strip() or "—"
        if capitalize_enum and text != "—":
            fld = self.menu.fields.get(code) if self.menu else None
            if fld is not None and fld.jg_type == "enm":
                text = text[:1].upper() + text[1:]
        return text

    def _footer_flag_fields(self) -> list:
        """The ``type:'flag'`` chips Winbox draws in a form's tray.

        An interface tray reads "enabled | running | slave | passthrough |
        inactive | link ok": the first cell is ``type:enable``, the last is
        ``type:statusbar``, and the four between are the map's flag nodes --
        the same declarations that give the list its XRSPI letters.

        ``flag_fields`` also injects the standard Disabled flag for
        ``type:enable`` templates; the tray already spells that one out as
        Enabled/Disabled, so drop it here rather than say it twice.
        """
        getter = getattr(self.menu, "flag_fields", None)
        if not callable(getter):
            return []
        try:
            flags = getter()
        except Exception:  # noqa: BLE001
            return []
        return [
            f for f in flags
            if f.code != sch.FLAG_DISABLED_CODE
            and f.hint != sch.FLAG_DISABLED_HINT
        ]

    def _flag_is_on(self, fld, vals: dict[int, Any]) -> bool:
        """Whether a flag chip reads set — the wire omits most false flags."""
        raw = vals.get(fld.code)
        if raw is None and hasattr(self.row, "raw_value"):
            raw = self.row.raw_value(fld.code)
        on = raw is True or raw == 1
        if fld.invert:
            # `inv:1` shows the flag when the bit is *clear*.
            on = not on
        return on

    def _update_form_footer(self) -> None:
        """Winbox window tray from ``type:enable`` + flags + ``type:statusbar``."""
        if not hasattr(self, "_form_footer") or self._form_footer is None:
            return
        # (text, greyed) — Winbox dims a flag whose bit is clear.
        parts: list[tuple[str, bool]] = []
        vals = self._row_values()

        # type:enable → Disabled flag 0xFE000A (template-driven).
        if getattr(self.menu, "has_enable", False):
            dis = vals.get(0xFE000A)
            if dis is None and hasattr(self.row, "raw_value"):
                dis = self.row.raw_value(0xFE000A)
            if isinstance(dis, bool):
                parts.append(("Disabled" if dis else "Enabled", False))
            elif isinstance(dis, int):
                parts.append(("Disabled" if dis else "Enabled", False))
            elif dis is None:
                # List rows usually carry the flag; default to Enabled when absent.
                parts.append(("Enabled", False))

        # type:'flag' → one chip each, in .jg declaration order, greyed off.
        for fld in self._footer_flag_fields():
            parts.append((fld.name, not self._flag_is_on(fld, vals)))

        for item in getattr(self.menu, "statusbar", None) or []:
            kind = item.get("kind") or "field"
            withouttitle = bool(item.get("withouttitle"))
            name = item.get("name")
            if kind == "tuple":
                segs: list[str] = []
                for code in item.get("codes") or []:
                    if not isinstance(code, int):
                        continue
                    segs.append(self._render_field_value(code))
                sep = str(item.get("sep") if item.get("sep") is not None else " / ")
                text = sep.join(segs) if segs else "—"
                postfix = item.get("postfix")
                if postfix and text != "—":
                    text = f"{text}{postfix}"
            else:
                code = item.get("code")
                if not isinstance(code, int):
                    continue
                text = self._render_field_value(code, capitalize_enum=True)
                postfix = item.get("postfix")
                if postfix and text != "—" and postfix not in text:
                    text = f"{text}{postfix}"

            if withouttitle or not name:
                parts.append((text, False))
            else:
                parts.append((f"{name}: {text}", False))

        dim = f'<span style="color: {style.TEXT_DIM}">'
        cells = [
            f"{dim}{html.escape(text)}</span>" if greyed else html.escape(text)
            for text, greyed in parts
        ]
        divider = f"{dim}&nbsp;&nbsp;&nbsp;·&nbsp;&nbsp;&nbsp;</span>"
        self._form_footer.setText(divider.join(cells) if cells else "")
        self._form_footer.setVisible(bool(cells))

    def _refresh_dirty_highlights(self) -> None:
        """Visually mark fields that differ from the device-loaded values."""
        details: list[str] = []
        tips: list[str] = []
        n = 0
        for spec, editor in self._rows:
            if spec.get("locked") or spec.get("always_locked"):
                _apply_dirty_style(editor, False)
                continue
            if _owns_baseline(editor):
                dirty = editor.is_dirty()          # these own their own baseline
            else:
                new = _editor_value(editor)
                dirty = new != spec.get("original")
            _apply_dirty_style(editor, dirty)
            if dirty:
                n += 1
                name = str(spec.get("name") or "").strip() or "(unnamed)"
                # A list says how much longer or shorter it is, not merely that
                # it moved: "2 field(s) modified" over a bridge VLAN whose
                # Untagged is three ports up and Tagged two down says none of
                # what is about to be written.
                delta = _change_summary(editor)
                details.append(f"{name} {delta}" if delta else name)
                tips.append(
                    f"{name}: {_baseline_label(spec, editor)}"
                    f" → {_display_label(editor)}"
                )
        self._dirty_count = n
        # Title badge so the MDI bar shows the form has local edits.
        dirty_mark = f" · {n} edited" if n and not self._ro else ""
        self.setWindowTitle(self._base_title + self._mode_suffix(self._ro) + dirty_mark)
        if self._mode_lab is not None and not self._ro:
            self._show_mode_text(
                self._mode_text(n, _joined(details)), "\n".join(tips)
            )

    def _mode_suffix(self, ro: bool) -> str:
        if getattr(self, "_dynamic", False):
            # Say why it is locked; "read-only" alone reads as a mode you can
            # switch out of, and this one you cannot.
            return " [dynamic · read-only]"
        if getattr(self, "_menu_ro", False):
            # Same reasoning, other cause: the .jg calls the whole menu ro,
            # so no mode makes System ▸ Resources or Log writable.
            return " [readings · read-only]"
        if ro:
            return " [read-only]"
        return " [live]" if self.edit_session.live else " [queue]"

    def _mode_text(self, n: int = 0, detail: str = "") -> str:
        """The status line — it must never claim Queue while Live is selected.

        ``detail`` names the fields that changed, a list field with what it
        gained and lost (``Untagged +3``), so the line says what is pending
        rather than only how many rows of the form it sits in.
        """
        what = f"{n} field(s) modified (highlighted)"
        if detail:
            what = f"{n} field(s) modified: {detail}"
        if self.edit_session.live:
            return what if n else ""
        if n:
            return (
                f"Queue mode — {what}; "
                "Queue changes to hold locally, or Escape to close"
            )
        return "Queue mode — edits stay local (not sent to the router)"

    def _confirm_close_if_dirty(self) -> bool:
        """Return True if the window may close. Prompts when there are edits."""
        if self._allow_close or self._ro:
            return True
        edits = self.edits(changed_only=True)
        if not edits:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved changes")
        live = self.edit_session.live
        box.setText(
            f"This window has {len(edits)} unsaved change(s).\n\n"
            + ("Write them to the router, discard them, or keep editing."
               if live else
               "Queue them locally, discard them, or keep editing.")
        )
        lines = []
        for e in edits[:12]:
            lines.append(f"• {e['name']}: {e['old']!r} → {e['label']}")
        if len(edits) > 12:
            lines.append(f"… and {len(edits) - 12} more")
        box.setInformativeText("\n".join(lines))
        btn_queue = box.addButton(
            "Write changes" if live else "Queue changes",
            QMessageBox.ButtonRole.AcceptRole,
        )
        btn_discard = box.addButton(
            "Close without saving", QMessageBox.ButtonRole.DestructiveRole
        )
        btn_cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_cancel or clicked is None:
            return False
        if clicked is btn_queue:
            self._queue_changes(prompt_if_empty=False)
            # _queue_changes closes on success; if nothing queued, stay open.
            return self._allow_close
        # discard
        return True

    def _row_field_index(self) -> dict[int, list[tuple]]:
        """Decoded row fields grouped by wire code.

        The template asks "what did the device send for code X" once per row
        it lays out; answering that by scanning the whole decoded row each
        time is quadratic, and a wireless interface has ~190 of each.
        """
        if self._row_index_cache is None:
            index: dict[int, list[tuple]] = {}
            for entry in getattr(self.row, "fields", None) or []:
                index.setdefault(entry[0], []).append(entry)
            self._row_index_cache = index
        return self._row_index_cache

    def _forget_row_caches(self) -> None:
        """Drop what was memoised against the row we just replaced."""
        self._row_index_cache = None
        self._defaults_cache = None
        self._cond_ok_cache.clear()
        self._tab_ok_cache.clear()

    def _row_values(self) -> dict[int, Any]:
        """Wire values for condition evaluation (caps, sfp, …)."""
        by = getattr(self.row, "values_by_code", None)
        if isinstance(by, dict) and by:
            return by
        # Fall back to raw message scan when values_by_code is empty.
        out: dict[int, Any] = {}
        raw = getattr(self.row, "raw", None)
        if raw is not None and hasattr(raw, "__iter__"):
            try:
                for code, _tag, value in raw:
                    if isinstance(code, int) and code < 0xFE0000:
                        out[code] = value
            except Exception:
                pass
        return out

    def _syscaps(self):
        """Device capability bits, for the switch-chip conds that read them."""
        if self._syscaps_cache is not _UNSET:
            return self._syscaps_cache
        self._syscaps_cache = None
        getter = getattr(self.client, "system_caps", None)
        if callable(getter):
            try:
                self._syscaps_cache = getter()
            except Exception:  # noqa: BLE001
                self._syscaps_cache = None
        return self._syscaps_cache

    def _board_name(self) -> str | None:
        """Device board, needed by type:board conditions (stats tabs)."""
        info = getattr(self.client, "info", None)
        board = getattr(info, "board", None) if info is not None else None
        return str(board) if board else None

    def _ui_slots(self) -> dict[str, Any]:
        """Condition slots that are not wire fields: the mode button.

        `on:'adv'` reads a slot called ``umode``, which is the Simple /
        Advanced Mode button and has no wire code at all. Left unanswered it
        reads as 0 and the advanced half of a wireless interface -- ten rows
        on the Wireless tab plus the Data Rates, Advanced and Tx Power tabs --
        is unreachable.
        """
        toggle = getattr(self.menu, "mode_toggle", None)
        if not isinstance(toggle, dict):
            return {}
        name = toggle.get("name")
        if not isinstance(name, str) or not name:
            return {}
        return {name: 1 if self._advanced else 0}

    def _condition_ok(self, on_name: str | None) -> bool:
        if not on_name or self.menu is None:
            return True
        hit = self._cond_ok_cache.get(on_name)
        if hit is not None:
            return hit
        try:
            from x3.proto.schema import condition_is_true
            out = condition_is_true(
                self.menu, on_name, self._row_values(), board=self._board_name(),
                ui_slots=self._ui_slots(), syscaps=self._syscaps(),
            )
        except Exception:
            out = True
        self._cond_ok_cache[on_name] = out
        return out

    def _condition_is_dynamic(self, on_name: str | None) -> bool:
        if not on_name or self.menu is None:
            return False
        hit = self._cond_dyn_cache.get(on_name)
        if hit is not None:
            return hit
        try:
            from x3.proto.schema import condition_is_dynamic
            out = condition_is_dynamic(self.menu, on_name)
        except Exception:
            out = False
        self._cond_dyn_cache[on_name] = out
        return out

    def _condition_hides(self, on_name: str | None) -> bool:
        """Whether this on: decides the row exists, or only whether it is live.

        Winbox has three kinds (see schema.condition_hides). The flagless kind
        greys its rows instead of removing them: Supported Rates while Rate is
        on `default`, a security profile's pre-shared key with WPA unticked.
        Dropping those rows was wrong in both directions -- they vanished
        rather than greying, and picking `configured` never brought them back.
        """
        if not on_name or self.menu is None:
            return True
        hit = self._cond_hide_cache.get(on_name)
        if hit is not None:
            return hit
        try:
            from x3.proto.schema import condition_hides
            out = condition_hides(self.menu, on_name)
        except Exception:
            out = True
        self._cond_hide_cache[on_name] = out
        return out

    def _condition_gate(self, on_name: str | None) -> str | None:
        """The condition that greys a row gated on ``on_name``, if any."""
        if not on_name or self.menu is None:
            return None
        if on_name in self._cond_gate_cache:
            return self._cond_gate_cache[on_name]
        try:
            from x3.proto.schema import condition_enable_on
            out = condition_enable_on(self.menu, on_name)
        except Exception:
            out = None
        self._cond_gate_cache[on_name] = out
        return out

    def _gate_off(
        self, spec: dict[str, Any], values: dict[int, Any] | None = None
    ) -> bool:
        """True when a row's ``oron`` / flagless condition greys it out now."""
        gate = spec.get("gate_on")
        if not gate:
            return False
        if values is None:
            return not self._condition_ok(str(gate))
        try:
            from x3.proto.schema import condition_is_true
            return not condition_is_true(
                self.menu, str(gate), values, board=self._board_name(),
                ui_slots=self._ui_slots(), syscaps=self._syscaps(),
            )
        except Exception:
            return False

    def _live_values(self) -> dict[int, Any]:
        """Wire values with the form's current edits layered on top.

        Conditions like ``noautoneg`` read another field, so re-evaluating them
        has to see what the user just typed/ticked, not the device snapshot.
        """
        vals = dict(self._row_values())
        for spec, editor in self._rows:
            code = spec.get("code")
            if not isinstance(code, int) or code < 0:
                continue
            try:
                value = _editor_value(editor)
            except RuntimeError:
                continue
            if value is not None:
                vals[code] = value
        return vals

    def _refresh_conditional_rows(self) -> None:
        """Show/hide ``hidedynamicly`` rows against the current field values."""
        if not self._cond_rows and not self._gate_rows:
            return
        try:
            from x3.proto.schema import condition_is_true
        except Exception:
            return
        vals = self._live_values()
        board = self._board_name()
        slots = self._ui_slots()
        for form, index, on_name in self._cond_rows:
            try:
                self._set_row_visible(form, index, condition_is_true(
                    self.menu, on_name, vals, board=board, ui_slots=slots,
                    syscaps=self._syscaps(),
                ))
            except RuntimeError:
                pass
        self._refresh_gated_rows(vals)

    def _refresh_gated_rows(self, values: dict[int, Any] | None = None) -> None:
        """Grey out (or release) rows whose condition gates editing, not shape.

        Picking `configured` in the Data Rates radio has to hand the rate
        checkboxes back straight away, so this runs from the same pass that
        re-evaluates hidedynamicly rows -- on every edit.
        """
        if not self._gate_rows:
            return
        vals = self._live_values() if values is None else values
        moved = False
        for spec, editor in self._gate_rows:
            locked = (
                self._ro
                or bool(spec.get("always_locked"))
                or self._gate_off(spec, vals)
            )
            if locked == spec.get("locked"):
                continue
            spec["locked"] = locked
            moved = True
            try:
                _set_locked(editor, locked)
            except RuntimeError:
                pass
        if moved:
            # A locked row is never counted as dirty, so releasing one changes
            # the answer -- and with it the unsaved-changes badge.
            self._refresh_dirty_highlights()

    def _tab_kept(self, tab: str) -> bool:
        """Whether this tab's rows are worth building.

        Not the same question as "is the tab showing": a tab gated on a
        ``hidedynamicly`` condition (Data Rates on the mode button, NV2 on
        Wireless Protocol) is one the user can open without reopening the
        window, so its rows have to exist even while it is hidden.
        """
        return self._condition_is_dynamic(self._tab_condition(tab)) or self._tab_ok(tab)

    def _tab_ok(self, tab: str) -> bool:
        if self.menu is None:
            return True
        hit = self._tab_ok_cache.get(tab)
        if hit is not None:
            return hit
        try:
            from x3.proto.schema import tab_is_visible
            out = tab_is_visible(
                self.menu, tab, self._row_values(), board=self._board_name(),
                ui_slots=self._ui_slots(), syscaps=self._syscaps(),
            )
        except Exception:
            out = True
        self._tab_ok_cache[tab] = out
        return out

    def _filter_visible_tabs(
        self, tabs_data: OrderedDict[str, list[dict[str, Any]]]
    ) -> OrderedDict[str, list[dict[str, Any]]]:
        """Hide tabs Winbox would (PoE/SFP/…) and tabs with only separators.

        A tab gated on a ``hidedynamicly`` condition is kept whatever the
        condition says right now: NV2 comes and goes with Wireless Protocol,
        and Data Rates with the mode button, both of which the user changes
        with the form open. Those are built and then shown or hidden by
        :meth:`_refresh_conditional_tabs`; only statically-gated tabs are
        dropped here.
        """
        out: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for tab, items in tabs_data.items():
            dynamic = self._condition_is_dynamic(self._tab_condition(tab))
            if not dynamic and not self._tab_ok(tab):
                continue
            content = [it for it in items if it.get("kind") != "separator"]
            if not content:
                continue
            if dynamic:
                self._dynamic_tabs.add(tab)
            out[tab] = items
        return out

    def _tab_condition(self, tab: str) -> str | None:
        try:
            from x3.proto.schema import tab_condition
            return tab_condition(self.menu, tab)
        except Exception:
            return None

    def _refresh_conditional_tabs(self) -> None:
        """Add or remove whole tabs whose condition flipped while open.

        QTabWidget cannot hide a page, so a tab that should not be there is
        removed and re-inserted at its schema position later -- which is why
        every page is addressed by its holder widget rather than by index (see
        :meth:`_ensure_page`).
        """
        tabs = self._tabs
        if tabs is None or not self._dynamic_tabs:
            return
        changed = False
        for pos, (name, holder, label) in enumerate(self._all_tabs):
            want = name not in self._dynamic_tabs or self._tab_ok_live(name)
            at = tabs.indexOf(holder)
            if want and at < 0:
                before = 0
                for other_name, other, _lab in self._all_tabs[:pos]:
                    if tabs.indexOf(other) >= 0:
                        before += 1
                tabs.insertTab(before, holder, label)
                changed = True
            elif not want and at >= 0:
                tabs.removeTab(at)
                holder.setParent(None)
                changed = True
        if changed:
            self._resync_titlebar_tabs()

    def _tab_ok_live(self, tab: str) -> bool:
        """Whether a tab's condition holds against the form's current values."""
        on = self._tab_condition(tab)
        if not on:
            return True
        try:
            from x3.proto.schema import condition_is_true
            return condition_is_true(
                self.menu, on, self._live_values(), board=self._board_name(),
                ui_slots=self._ui_slots(), syscaps=self._syscaps(),
            )
        except Exception:
            return True

    def _resync_titlebar_tabs(self) -> None:
        """Rebuild the title-bar strip after the tab set changed under it.

        It snapshots the tab names when it is installed, so leaving it alone
        would keep offering a tab that is no longer there.
        """
        if not self.titlebar_tabs_enabled():
            return
        self.set_titlebar_tabs(False)
        self.set_titlebar_tabs(True)

    def _object_type_name(self) -> str:
        """The object's type in words, for a ``type:objtype`` row.

        The merged menu carries the title of the type overlay it was built
        from -- interface type 1 is 'Ethernet', 12 'Bridge'. A type the schema
        has no overlay for still gets its number, which beats an empty row.
        """
        title = str(getattr(self.menu, "type_title", "") or "").strip()
        if title:
            return title
        try:
            value = sch.wire_type_value(self.row.raw, self.menu)
        except Exception:  # noqa: BLE001
            value = None
        if value is None:
            code = sch.type_field_code(self.menu)
            value = self._row_values().get(code) if code is not None else None
        return "—" if value is None else f"type {value}"

    def _build_specs(self) -> list[dict[str, Any]]:
        """Merge form layout + schema Field metadata for editors."""
        specs: list[dict[str, Any]] = []
        if self.menu is not None:
            seen: set[tuple[int, str]] = set()
            layout = getattr(self.menu, "form_layout", None) or []
            if layout:
                # Which fold each row belongs to. A layout item can produce
                # no specs or several, so the stamp is applied to whatever the
                # *previous* item appended -- see the group branch below.
                pending: tuple[int, Any] | None = None
                for item in layout:
                    if pending is not None:
                        start, group_key = pending
                        if group_key:
                            for member in specs[start:]:
                                member.setdefault("group", group_key)
                    pending = (len(specs), item.get("group"))
                    kind = item.get("kind")
                    tab = str(item.get("tab") or "General")
                    on_name = item.get("on")
                    # Static ``on:poe`` / ``on:sfp`` rows are decided by board
                    # capability — drop them. ``hidedynamicly`` rows (Speed vs
                    # Auto Negotiation) must be built either way and toggled
                    # later, so keep them and let _refresh_conditional_rows
                    # decide visibility.
                    dynamic = self._condition_is_dynamic(on_name)
                    # A flagless cond does not decide whether the row exists,
                    # only whether it is editable -- see _condition_hides.
                    gate_on = self._condition_gate(on_name)
                    if (
                        self._condition_hides(on_name)
                        and not dynamic
                        and not self._condition_ok(on_name)
                    ):
                        continue
                    if not self._tab_kept(tab):
                        continue
                    if kind == "separator":
                        specs.append({
                            "tab": tab,
                            "kind": "separator",
                            "name": "",
                            "code": -1,
                            "always_locked": True,
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "label":
                        # A caption with no value (Quick Set's "2GHz 5GHz"),
                        # drawn the way a named group is: a rule with the name.
                        specs.append({
                            "tab": tab,
                            "kind": "label",
                            "name": str(item.get("name") or ""),
                            "code": -1,
                            "always_locked": True,
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "alias":
                        # The field of this name, placed here as well -- and
                        # deliberately outside the one-row-per-code rule: the
                        # Mangle action deck shows Passthrough in every pane.
                        base = _field_by_name(self.menu, item.get("name"))
                        if base is None or _is_internal_field(base, self.menu):
                            continue
                        fld = self._with_slot_enum(self._with_device_default(base))
                        rendered = self.row.get_rendered(
                            fld.code, prefer_name=fld.name
                        ) if hasattr(self.row, "get_rendered") else ""
                        specs.append({
                            "tab": tab,
                            "kind": "field",
                            "code": fld.code,
                            "name": _winbox_label(fld.code, fld.name),
                            "rendered": rendered,
                            "raw": self.row.raw_value(fld.code),
                            "field": fld,
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "group":
                        # A named `type:group` is a fold. Its member rows come
                        # next in the layout carrying the same group key; the
                        # header owns the group's flag, where it has one.
                        code = item.get("code")
                        fld = (self.menu.field_for(code, item.get("prefix"))
                               if isinstance(code, int) else None)
                        if fld is not None:
                            seen.add((code, fld.name or ""))
                        specs.append({
                            "tab": tab,
                            "kind": "group",
                            "group": item.get("group"),
                            "name": str(item.get("title") or ""),
                            "code": code if isinstance(code, int) else -1,
                            "field": fld,
                            "raw": (self.row.raw_value(code)
                                    if isinstance(code, int) else None),
                            "rendered": "",
                            "always_locked": fld is None or fld.ro,
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "objtype":
                        # Winbox `type:objtype`: the object's family in words
                        # (ether3 → "Ethernet"). No wire code of its own — the
                        # name is the type overlay the form was merged from.
                        specs.append({
                            "tab": tab,
                            "kind": "field",
                            "code": -1,
                            "name": str(item.get("name") or "Type"),
                            "rendered": self._object_type_name(),
                            "raw": None,
                            "field": None,
                            "always_locked": True,
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "graphbox":
                        specs.append({
                            "tab": tab,
                            "kind": "graphbox",
                            "name": "Graphs",
                            "code": -1,
                            "graphs": item.get("graphs") or [],
                            "always_locked": True,
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "tuple":
                        parts: list[dict[str, Any]] = []
                        codes = list(item.get("codes") or [])
                        prefixes = list(item.get("prefixes") or [])
                        prefixes += [None] * (len(codes) - len(prefixes))
                        for code, prefix in zip(codes, prefixes):
                            base = self.menu.field_for(code, prefix)
                            if base is None or _is_internal_field(base, self.menu):
                                continue
                            n = base.name or f"<{code:#x}>"
                            rendered = ""
                            for _c, name, r, _ok in self._row_field_index().get(code, ()):
                                if name == n or not name or not base.name:
                                    rendered = r
                                    break
                            if not rendered and self.edit_session.readonly:
                                # hide empty status parts entirely when all missing
                                pass
                            seen.add((code, n))
                            parts.append({
                                "code": code,
                                "name": n,
                                "rendered": rendered,
                                "raw": self.row.raw_value(code),
                                "field": base,
                            })
                        if not parts:
                            continue
                        # `hideonempty` (the ethernet/switch counter pairs):
                        # Winbox draws the row only for counters this port
                        # actually keeps, not a column of dashes. The flag sits
                        # on the multituple itself, or -- in the switch-queue
                        # deck -- on each of its members.
                        hide_empty = bool(item.get("hide_on_empty")) or all(
                            bool(getattr(p.get("field"), "hide_on_empty", False))
                            for p in parts
                        )
                        if hide_empty:
                            self._live_tabs.add(tab)
                        # Hide empty status tuples — except on Traffic, where
                        # values arrive via 1 Hz GETOBJ after open. A
                        # `hideonempty` counter is in the same boat and is
                        # shown or hidden per poll instead (hide_when_empty).
                        empty_ro = (
                            self.edit_session.readonly
                            and not hide_empty
                            and all(
                                not (p.get("rendered") or "").strip()
                                for p in parts
                            )
                        )
                        if empty_ro and tab != "Traffic":
                            continue
                        if (
                            empty_ro
                            and tab != "Traffic"
                            and all(
                                not (p.get("rendered") or "").strip()
                                and (p.get("field") is None or p["field"].ro)
                                for p in parts
                            )
                        ):
                            continue
                        specs.append({
                            "tab": tab,
                            "kind": "tuple",
                            "hide_when_empty": hide_empty,
                            "name": item.get("title") or " / ".join(p["name"] for p in parts),
                            "sep": item.get("sep") or " / ",
                            "separate": bool(item.get("separate")),
                            "parts": parts,
                            "code": parts[0]["code"],
                            "field": parts[0].get("field"),
                            "rendered": (item.get("sep") or " / ").join(
                                (p.get("rendered") or "—") for p in parts
                            ),
                            "always_locked": all(
                                bool(p.get("field") and p["field"].ro) for p in parts
                            ),
                            "cond_on": on_name if dynamic else None,
                            "gate_on": gate_on,
                        })
                        continue
                    if kind == "field":
                        code = item.get("code")
                        if not isinstance(code, int):
                            continue
                        # type:statusbar / enable / .id — tray chrome only.
                        if self._is_chrome(code, item.get("prefix")):
                            continue
                        base = self.menu.field_for(code, item.get("prefix"))
                        if base is None:
                            continue
                        if _is_internal_field(base, self.menu):
                            seen.add((code, base.name or ""))
                            continue
                        same_code = self._row_field_index().get(code, ())
                        matches = [
                            e for e in same_code
                            if (e[1] == base.name or not base.name or not e[1])
                        ]
                        primary = self.menu.fields.get(code)
                        if not matches and not (
                            primary is not None and primary.variants
                        ):
                            # Also accept any decoded row for this wire code --
                            # but only where the id means one thing. Where it
                            # is two fields (SFP's u473 link length and Overall
                            # Stats' Q473 counter) the name is the only thing
                            # telling them apart, and taking either would put
                            # one field's value in the other's box.
                            matches = list(same_code)
                        hide_empty = bool(getattr(base, "hide_on_empty", False))
                        if hide_empty:
                            self._live_tabs.add(tab)
                        if not matches:
                            # Schema put this on form_layout — always show the
                            # row (— when RO/missing). No tab-name special cases:
                            # Status, Traffic, Rate, … all come from the template.
                            placeholder = "—" if base.ro or base.jg_type == "flag" else ""
                            matches = [(code, base.name, placeholder, True)]
                        for c, n, r, _ok in matches:
                            key = (c, n)
                            if key in seen:
                                continue
                            seen.add(key)
                            fld = base
                            if base.name != n:
                                for v in base.variants.values():
                                    if v.name == n:
                                        fld = v
                                        break
                            fld = self._with_slot_enum(self._with_device_default(fld))
                            if _is_internal_field(fld, self.menu):
                                continue
                            specs.append({
                                "tab": tab,
                                "kind": "field",
                                "hide_when_empty": hide_empty,
                                "code": c,
                                "name": _winbox_label(c, n or fld.name or f"<{c:#x}>"),
                                "rendered": r if r != "" else ("—" if fld.ro else r),
                                "raw": self.row.raw_value(c),
                                "field": fld,
                                "cond_on": on_name if dynamic else None,
                                "gate_on": gate_on,
                            })
                if pending is not None and pending[1]:
                    for member in specs[pending[0]:]:
                        member.setdefault("group", pending[1])
                # Collapse consecutive / leading / trailing separators per tab.
                specs = _collapse_separators(specs)
            else:
                # Flat form_order fallback (menus without form_layout)
                for tab, code in self.menu.form_order:
                    if code in self._chrome_codes():
                        continue
                    base = self.menu.fields.get(code)
                    if base is None:
                        continue
                    if _is_internal_field(base, self.menu):
                        seen.add((code, base.name or ""))
                        continue
                    matches = [
                        e for e in self._row_field_index().get(code, ())
                        if (e[1] == base.name or not base.name)
                    ]
                    if not matches:
                        # Still declared by the template — show empty / —.
                        placeholder = "—" if base.ro or base.jg_type == "flag" else ""
                        matches = [(code, base.name, placeholder, True)]
                    for c, n, r, _ok in matches:
                        key = (c, n)
                        if key in seen:
                            continue
                        seen.add(key)
                        fld = base
                        if base.name != n:
                            for v in base.variants.values():
                                if v.name == n:
                                    fld = v
                                    break
                        fld = self._with_slot_enum(self._with_device_default(fld))
                        if _is_internal_field(fld, self.menu):
                            continue
                        specs.append({
                            "tab": tab,
                            "kind": "field",
                            "code": c,
                            "name": n or fld.name or f"<{c:#x}>",
                            "rendered": r,
                            "raw": self.row.raw_value(c),
                            "field": fld,
                        })
            # Template chrome (statusbar / enable / .id) never reappear as Extra.
            chrome_codes = self._chrome_codes()

            # Leftovers: prefer form_fields tabs (Unknown / Extra / Flags)
            # when device.py already classified them via decode_form.
            if self.row.form_fields:
                for tab, code, name, rendered, _ok in self.row.form_fields:
                    if code in chrome_codes:
                        continue
                    if (code, name) in seen:
                        continue
                    seen.add((code, name))
                    fld = self.menu.fields.get(code)
                    if fld is not None and name and not name.startswith("<"):
                        if fld.name != name:
                            for v in fld.variants.values():
                                if v.name == name:
                                    fld = v
                                    break
                    if _is_internal_field(fld, self.menu):
                        continue
                    unknown = (
                        (name or "").startswith("<0x")
                        or tab == "Unknown"
                        or fld is None
                    )
                    specs.append({
                        "tab": tab if tab != "General" or not (name or "").startswith("<0x") else "Unknown",
                        "kind": "field",
                        "code": code,
                        "name": name,
                        "rendered": rendered,
                        "raw": self.row.raw_value(code),
                        "field": fld,
                        "always_locked": True if unknown else False,
                    })
            else:
                for c, n, r, _ok in self.row.fields:
                    if c in chrome_codes:
                        continue
                    if (c, n) in seen:
                        continue
                    fld = self.menu.fields.get(c)
                    if _is_internal_field(fld, self.menu):
                        continue
                    unknown = (n or "").startswith("<0x") or fld is None
                    tab = "Unknown" if unknown else (
                        "Flags" if fld and fld.jg_type == "flag" else "Extra"
                    )
                    specs.append({
                        "tab": tab,
                        "kind": "field",
                        "code": c,
                        "name": n,
                        "rendered": r,
                        "raw": self.row.raw_value(c),
                        "field": fld,
                        "always_locked": True if unknown else False,
                    })
            return specs

        for tab, code, name, rendered, _ok in self.row.form_fields or []:
            unknown = (name or "").startswith("<0x") or tab == "Unknown"
            specs.append({
                "tab": "Unknown" if unknown and tab == "General" else tab,
                "kind": "field",
                "code": code,
                "name": name,
                "rendered": rendered,
                "raw": self.row.raw_value(code),
                "field": None,
                "always_locked": True if unknown else False,
            })
        if not specs:
            for c, n, r, _ok in self.row.fields:
                unknown = (n or "").startswith("<0x")
                specs.append({
                    "tab": "Unknown" if unknown else "General",
                    "kind": "field",
                    "code": c,
                    "name": n,
                    "rendered": r,
                    "raw": self.row.raw_value(c),
                    "field": None,
                    "always_locked": True if unknown else False,
                })
        return specs

    @staticmethod
    def _scrollable(page: QWidget) -> QWidget:
        """Wrap a form page so a short window scrolls instead of clamping.

        Winbox has a minimum window size and shows a scrollbar when the content
        no longer fits, rather than refusing to shrink. Without this the tallest
        page (Traffic, with its two graphs) sets a floor on the whole window.
        """
        area = QScrollArea()
        area.setWidget(page)
        area.setWidgetResizable(True)
        # Not a tab stop: Tab out of the last field used to pause on the empty
        # scroller before reaching the buttons. The page still follows the
        # keyboard -- QScrollArea scrolls a focused field into view by itself.
        area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # The viewport must not inherit the themed window background.
        area.viewport().setAutoFillBackground(False)
        area.setStyleSheet("QScrollArea { background: transparent; }")
        return area

    def _form_page(
        self,
        items: list[dict[str, Any]],
        readonly: bool,
        *,
        tab_name: str = "",
    ) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        # Widgets that should absorb leftover height (the traffic graphs). They
        # are hosted below the form rather than inside it -- see the graphbox
        # branch below.
        stretchy: list[QWidget] = []
        # `set` editors on this page, so the rate grids can share one pitch.
        page_sets: list[SetEditor] = []
        m = style.MARGIN
        # Winbox insets the label column rather than jamming it against the
        # window edge. Costs field width, which is fine — the fields stretch.
        form.setContentsMargins(_FORM_LEFT_INSET, m + 2, m, m)
        form.setHorizontalSpacing(max(6, m * 2))
        form.setVerticalSpacing(max(_FORM_ROW_GAP, style.SPACING))
        # Winbox v3: labels right-aligned in a column sized to the longest label;
        # values left-aligned and grow with the window (QFormLayout default).
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        # group key → the fold that owns it, for the rows that follow it.
        folds: dict[str, GroupFold] = {}
        members = Counter(
            spec.get("group") for spec in items
            if spec.get("group") and (spec.get("kind") or "field") != "group"
        )

        def add_row(spec: dict[str, Any], *args) -> None:
            """Add a row, and hide it if the fold it belongs to is closed."""
            form.addRow(*args)
            # The label went in as a widget (``_row_label`` says why), so Qt
            # never made it the field's buddy and the field has no name for a
            # screen reader to read. Hand it the row's name directly.
            if args:
                a11y.label_editor(args[-1], str(spec.get("name") or ""))
                if len(args) == 2:
                    a11y.pair(args[0], args[1])
            fold = folds.get(spec.get("group") or "")
            # A fold's own header carries the group key too; it is the thing
            # doing the hiding, so it never hides itself.
            if fold is None or fold in args:
                return
            index = form.rowCount() - 1
            self._folded_rows[(id(form), index)] = fold
            self._fold_row_index.append((form, index))
            self._set_row_visible(form, index, True)

        for spec in items:
            kind = spec.get("kind") or "field"
            if kind == "group":
                if not members.get(spec.get("group")):
                    # A fold whose rows the template parser drops -- a `not`
                    # wrapping a tuple (firewall Connection Limit) is the only
                    # one in 7.21. An empty fold is worse than no fold.
                    continue
                fold = self._make_fold(spec, readonly)
                folds[spec.get("group") or ""] = fold
                self._rows.append((spec, fold))
                self._editors[spec["name"]] = fold
                add_row(spec, fold)
                self._track_condition(form, spec)
                self._track_gate(spec, fold)
                continue
            if kind == "separator":
                add_row(spec, _hairline())
                self._track_condition(form, spec)
                continue
            if kind == "label":
                # A caption spanning the form, drawn like a named group's rule.
                add_row(spec, section_header(str(spec.get("name") or "")))
                self._track_condition(form, spec)
                continue
            if kind == "graphbox":
                editor = TrafficGraphBox(
                    spec.get("graphs") or [], rate_ms=self._poll_ms
                )
                editor.rate_changed.connect(self._set_poll_rate)
                self._graph_widgets.append(editor)
                if spec.get("cond_on"):
                    # Conditional rows have to stay in the form so the existing
                    # show/hide machinery can address them by row index.
                    add_row(spec, editor)
                    self._track_condition(form, spec)
                else:
                    # Otherwise keep the graph out of the QFormLayout: with
                    # AlignTop a form never hands extra vertical space to a row,
                    # so a graph inside one is stuck at its minimum height and
                    # cannot follow the window.
                    stretchy.append(editor)
                continue
            if kind == "tuple":
                always = True  # traffic-style tuples are device status
                for p in spec.get("parts") or []:
                    fld = p.get("field")
                    if fld is not None and not fld.ro and fld.jg_type != "flag":
                        always = False
                        break
                spec["always_locked"] = always or bool(spec.get("always_locked"))
                locked = readonly or spec["always_locked"] or self._gate_off(spec)
                editor = _tuple_value_row(spec, locked=locked)
                # Register part labels for live refresh by wire code
                for p, lab in zip(spec.get("parts") or [], _tuple_part_labels(editor)):
                    code = p.get("code")
                    if isinstance(code, int):
                        self._live_labels.setdefault(code, []).append(
                            (str(p.get("name") or ""), lab)
                        )
                spec["locked"] = locked
                spec["original"] = _editor_value(editor)
                self._rows.append((spec, editor))
                self._editors[spec["name"]] = editor
                add_row(spec, _row_label(spec["name"]), editor)
                self._track_condition(form, spec)
                self._track_gate(spec, editor)
                self._track_empty(form, spec)
                continue

            fld = spec.get("field")
            # Schema status (ro) + flags + Unknown / unmapped wire fields stay
            # locked even when the toolbar is in queue/edit mode.
            always = (
                bool(spec.get("always_locked"))
                or bool(fld is not None and fld.ro)
                or bool(fld is not None and fld.jg_type == "flag")
                or (spec.get("tab") == "Unknown")
                or (fld is None and (spec.get("name") or "").startswith("<0x"))
            )
            spec["always_locked"] = always
            # A row whose `oron` / flagless condition is false is greyed, not
            # gone: Winbox shows Supported Rates with the router's defaults in
            # them and lets you edit once Rate says `configured`.
            locked = readonly or always or self._gate_off(spec)
            # Always-locked values are device status (Traffic, Rate, Default
            # Name…) — plain labels, not empty-looking text boxes. Flags keep
            # a disabled checkbox so R/S-style booleans stay familiar.
            if fld is not None and fld.jg_type == "numbertable":
                # A reported grid (a registration's signal strength per rate).
                editor = NumberTableView(fld, spec.get("raw"))
            elif fld is not None and fld.jg_type == "numbar":
                # A signal reading, not a number to type: bar + "-70dBm (good)".
                editor = SignalBar(fld, spec.get("raw"))
                code = spec.get("code")
                if isinstance(code, int):
                    self._live_bars.setdefault(code, []).append(editor)
            elif always and _is_reported_list(fld, spec.get("raw")):
                # A reported array -- Advertising, a bridge VLAN's Current
                # Tagged, a port's Registered VLAN IDs: Winbox shows a
                # read-only box with one entry per line, not a comma-run
                # squeezed into a single-line label.
                editor = ReadOnlyValueList(
                    spec.get("raw"), spec.get("rendered"), fld,
                    lookup=self._enum_lookup(fld),
                )
                code = spec.get("code")
                if isinstance(code, int):
                    self._live_lists.setdefault(code, []).append(editor)
            elif always and not (fld and fld.jg_type == "flag"):
                editor = _value_label(spec.get("rendered", "") or "—")
                code = spec.get("code")
                if isinstance(code, int) and isinstance(editor, ReadOnlyValueEdit):
                    self._live_labels.setdefault(code, []).append(
                        (str(spec.get("name") or ""), editor)
                    )
            else:
                editor = self._make_editor(spec, locked)
            spec["field"] = fld
            # Keyed by position, not name -- a menu can show the same label
            # more than once (Quick Set repeats 'IP Address' and 'Netmask'
            # per mode), and a name-keyed dict silently drops the duplicates.
            spec["locked"] = locked
            spec["original"] = _editor_value(editor)
            self._rows.append((spec, editor))
            if isinstance(editor, SetEditor) and editor.columns:
                page_sets.append(editor)
            self._editors[spec["name"]] = editor
            self._field_meta[spec["name"]] = fld
            _fill_row_width(editor)
            if isinstance(editor, RadioGroupEditor) and editor.title:
                # A radio group with no `compact:1` is a section, not a row:
                # its name is in the rule above the buttons, so it spans both
                # columns and the label column stays empty.
                add_row(spec, editor)
            else:
                add_row(
                    spec,
                    _row_label(spec["name"],
                               colon=not isinstance(editor, QCheckBox)),
                    editor,
                )
            self._track_condition(form, spec)
            self._track_gate(spec, editor)
            self._track_empty(form, spec)

        _share_set_pitch(page_sets)

        if not stretchy:
            # Rolling the wheel past a dropdown must scroll the form, not
            # edit it. Guard whatever is handed back, not `page`: the traffic
            # graphs (and their rate dropdown) are hosted beside it below.
            guard_wheel(page)
            return page
        # Form rows keep their natural height at the top; the graphs take the
        # rest and shrink back to their own minimum as the window shrinks.
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(max(2, style.SPACING))
        form.setContentsMargins(_FORM_LEFT_INSET, m + 2, m, 0)
        page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        outer.addWidget(page, 0)
        for widget in stretchy:
            outer.addWidget(widget, 1)
        guard_wheel(host)
        return host

    def _spec_has_value(self, spec: dict[str, Any]) -> bool:
        """Does the row in hand carry a value for this form row?"""
        index = self._row_field_index()
        for part in (spec.get("parts") or [spec]):
            code = part.get("code")
            if not isinstance(code, int):
                continue
            want = str(part.get("name") or "")
            for entry in index.get(code, ()):
                # An id can be two fields; only the one this row names counts.
                if want and entry[1] and entry[1] != want:
                    continue
                if (entry[2] or "").strip():
                    return True
        return False

    def _track_empty(self, form, spec: dict[str, Any]) -> None:
        """Remember a `hideonempty` counter, and hide it while it is empty."""
        if not spec.get("hide_when_empty"):
            return
        index = form.rowCount() - 1
        self._empty_rows.append((form, index, spec))
        self._set_row_visible(form, index, self._spec_has_value(spec))

    def rerender_values(self) -> None:
        """Restate what is on screen after a display preference changed.

        Byte units change how a counter reads, not what it is, so the object
        in hand is decoded again rather than re-read from the device.
        """
        client, row = self.client, self.row
        if client is None or row is None or not hasattr(client, "rerender"):
            return
        try:
            fresh = client.rerender(row)
        except Exception:  # noqa: BLE001 - a repaint must not take a window down
            return
        self.row = fresh
        self._forget_row_caches()
        self._paint_live_values(fresh)
        self._refresh_empty_rows()
        self._update_form_footer()

    def _refresh_empty_rows(self) -> None:
        """Re-decide which counters exist, against the row in hand.

        Winbox lists only the counters a port actually keeps. Which ones those
        are is not known when the form opens: the read that opens it does not
        carry them -- they arrive on the refresh poll (0x4000A, the menu's own
        ``refreshfilter``) -- so this runs again on every poll rather than
        once at build.
        """
        for form, index, spec in self._empty_rows:
            self._set_row_visible(form, index, self._spec_has_value(spec))

    def _make_fold(self, spec: dict[str, Any], readonly: bool) -> GroupFold:
        """The header row of a `type:group`, and what it starts out showing.

        Winbox opens a fold when the group's flag is set -- the firewall's
        "this match is in use" bool -- and leaves it closed otherwise, which is
        why a rule with no limit shows five closed folds. A group with no flag
        (a CAPs Rate's rate grids) always starts closed.
        """
        fld = spec.get("field")
        locked = readonly or bool(spec.get("always_locked")) or self._gate_off(spec)
        fold = GroupFold(
            spec["name"],
            expanded=bool(spec.get("raw")),
            flag=fld is not None,
            readonly=locked,
        )
        spec["locked"] = locked
        spec["original"] = _editor_value(fold)
        fold.folded.connect(self._refresh_folded_rows)
        return fold

    def _set_row_visible(self, form, index: int, visible: bool) -> None:
        """Show or hide one form row, honouring the fold it sits in.

        A row in a closed fold stays hidden however its `on:` condition
        evaluates, so both the fold and the condition pass come through here.
        """
        fold = self._folded_rows.get((id(form), index))
        if fold is not None and not fold.expanded:
            visible = False
        try:
            form.setRowVisible(index, visible)
        except (RuntimeError, AttributeError):
            pass

    def _refresh_folded_rows(self) -> None:
        """Re-decide the rows a fold owns, then let the conditions have a say."""
        for (form, index) in list(self._fold_row_index):
            self._set_row_visible(form, index, True)
        self._refresh_conditional_rows()

    def _track_condition(self, form, spec: dict[str, Any]) -> None:
        """Remember a hidedynamicly row so it can be toggled while open."""
        on_name = spec.get("cond_on")
        if on_name:
            self._cond_rows.append((form, form.rowCount() - 1, str(on_name)))

    def _track_gate(self, spec: dict[str, Any], editor) -> None:
        """Remember a row that greys out, so the grey can lift while open."""
        if spec.get("gate_on"):
            self._gate_rows.append((spec, editor))

    # --- live Traffic tab (1 Hz GETOBJ) --------------------------------

    def _open_read_mask(self) -> int | None:
        """The 0xFE000C a form's own GETOBJ should carry.

        Sending none is not the same as asking for everything: the fields a
        menu hides behind its ``refreshfilter`` (an Ethernet row's link
        status, 0x44C) only come back when the mask does. ``list_read_mask``
        returns None for the menus that declare no filter, where a mask would
        lose fields rather than gain them.
        """
        from x3.proto.device import list_read_mask

        return list_read_mask(self.menu)

    def _bootstrap_full_row(self) -> None:
        """Pull a full object snapshot before the form is built.

        List GETALL rows are sparse for every map menu (status RO fields,
        traffic counters, …). Opening a property form uses unfiltered GETOBJ
        — the same idea as Winbox — whenever the template is a collection and
        the row has a ``.id``. Live Traffic polling still uses its own mask.
        """
        if self.client is None or self.menu is None:
            return
        oid = self.row.object_id() if hasattr(self.row, "object_id") else None
        if oid is None:
            return
        self._obj_id = oid
        # Single-object settings menus use GET, not GETOBJ.
        if not getattr(self.menu, "is_collection", False):
            return
        try:
            self._from_path = self.client.alloc_from()
            if self._full_row:
                # The caller already fetched the full object. Asking again is
                # a second round trip on every double-click, which is most of
                # why opening a row felt slow next to Winbox.
                return
            self.row = self.client.getobj(
                self.path,
                oid,
                filter_mask=self._open_read_mask(),
                from_path=self._from_path,
            )
            self._forget_row_caches()
        except Exception:
            # Keep the list-row snapshot; live poll may still succeed later.
            if self._from_path is None and self.client is not None:
                try:
                    self._from_path = self.client.alloc_from()
                except Exception:
                    pass

    def _enum_lookup(self, fld: Field | None) -> dict | None:
        """``{wire value: name}`` for a field whose names live in another menu.

        The prefetch has already read that table, so this is a cache hit; None
        means there is no source to read, or the read failed, and the caller
        falls back to the text the row was rendered with.
        """
        if fld is None or self.client is None:
            return None
        path = sch_implied_enum_path(fld) or sch_implied_enum_path(
            fld.element_view()
        )
        if not path:
            return dict(fld.enum) if fld.enum else None
        try:
            names = dict(self.client.enum_map(tuple(path)))
        except Exception:  # noqa: BLE001 - a name we cannot read is not a crash
            return None
        # A `pair` enum names a few values itself (none / self) and leaves the
        # rest to the table.
        if fld.enum:
            names = {**fld.enum, **names}
        return names

    def _prefetch_enum_sources(self) -> None:
        """Read every dropdown source this form needs, in one pipelined burst.

        A dropdown's options live in another menu, so filling one costs a round
        trip: a wifi interface references eleven. Built field by field those
        eleven are serial -- the window cannot appear until the last one lands,
        which is most of why opening a row took about a second. Winbox sends
        them all at once instead (a session capture: 21 frames out in
        6.5 ms, everything answered by 15.9 ms), so do the same and let the
        editors read from the cache the burst filled.

        Schema-driven, so it does not matter which editor ends up using which
        table: collect the paths the menu's fields name, plus the interface list
        an optional match implies, and hand the lot to the client.
        """
        if self.client is None or self.menu is None:
            return
        prefetch = getattr(self.client, "prefetch_enums", None)
        if prefetch is None:
            return
        paths: list[tuple[int, ...]] = []
        for fld in self.menu.fields.values():
            if fld.enum_path:
                paths.append(tuple(fld.enum_path))
                continue
            # An optional match's inner field leaves its source off in the .jg;
            # _implied_enum_path is what _make_opt_editor resolves it with.
            _neg, inner = sch_opt_chain(fld)
            if inner is not None and not inner.enum:
                implied = sch_implied_enum_path(inner)
                if implied:
                    paths.append(tuple(implied))
        if not paths:
            return
        try:
            prefetch(paths)
        except Exception:  # noqa: BLE001
            pass    # a failed prefetch costs speed, never correctness: the
                    # editors still read their own tables one at a time

    def _menu_has_traffic_tab(self) -> bool:
        if self._live_tabs:
            return True
        if self.menu is None:
            return False
        for item in getattr(self.menu, "form_layout", None) or []:
            if str(item.get("tab") or "") == "Traffic":
                return True
        return False

    def _setup_live_traffic(self) -> None:
        if self.client is None or not self._menu_has_traffic_tab():
            return
        if self._obj_id is None:
            oid = self.row.object_id() if hasattr(self.row, "object_id") else None
            if oid is None:
                return
            self._obj_id = oid
        if self._from_path is None:
            try:
                self._from_path = self.client.alloc_from()
            except Exception:
                self._from_path = (0, 11)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._poll_ms)
        self._poll_timer.timeout.connect(self._poll_traffic_tick)
        # Start immediately when Traffic is the current tab, else on first switch.
        if self._traffic_tab_active():
            self._poll_timer.start()
            self._poll_traffic_tick()

    def _traffic_tab_active(self) -> bool:
        if self._tabs is None:
            # Single-page form that still has traffic fields
            return self._menu_has_traffic_tab()
        idx = self._tabs.currentIndex()
        if idx < 0:
            return False
        name = self._tabs.tabText(idx)
        # The stats tabs are polled too: their counters come on the same
        # refresh as the traffic rates, and Winbox shows them ticking.
        return name.startswith("Traffic") or name in self._live_tabs

    # -- .jg autorefresh: the form re-reads itself --------------------------

    def _setup_menu_refresh(self) -> None:
        """Re-read this object at the rate its menu asks for.

        ``autorefresh`` is a rate on the menu, and Winbox v3 obeys it on a form
        as much as on a list: System ▸ Resources counts its uptime, free memory
        and CPU load while you watch, RouterBOARD re-reads every 3 s, NTP Client
        shows its status arriving. A list pane already honours it (see
        ``ListPane`` and ``Menu.autorefresh_ms``); a form did not, so an `item`
        menu -- which has no list behind it to refresh -- sat for ever on the
        reading it opened with.

        Only a singleton is polled. An `item` names the whole object, so one
        GET brings all of it back and the reply can stand in for the row; a
        row's form is left to the Traffic tab's poll, which reads one object
        under the mask that tab needs rather than the whole of it.

        A tick repaints only the registered read-only values (see
        :meth:`_paint_live_values`), so it cannot overwrite something being
        typed into one of the editable settings forms that also declare a rate
        (IP ▸ DNS, System ▸ Clock, IP ▸ Cloud).
        """
        if self.client is None or self.menu is None:
            return
        ms = _as_int(getattr(self.menu, "autorefresh_ms", None))
        if not ms or ms <= 0:
            return
        if getattr(self.menu, "is_collection", False) or self._object_id() is not None:
            return
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(ms)
        self._refresh_timer.timeout.connect(self._menu_refresh_tick)
        self._refresh_timer.start()

    def _menu_refresh_tick(self) -> None:
        """One GET on this menu, off the UI thread.

        A window nobody is looking at is not worth a read: an unshown form
        (and a closing one) skips its tick rather than putting a request on the
        wire for values no one can see.
        """
        if self.client is None or not self.isVisible():
            return
        worker = self._refresh_worker
        if worker is not None and worker.isRunning():
            return          # the last one has not answered yet; skip this tick
        client = self.client
        path = self.path
        menu = self.menu

        def fetch():
            # Kept out of the session log, like every other 1 Hz read.
            # `menu`: a refresh that decoded against the path's default menu
            # would turn every label on a shared handler back into a hex code
            # one second after the form opened.
            return client.get(path, quiet=True, menu=menu)

        worker = _TrafficPollWorker(fetch, self)
        self._refresh_worker = worker
        worker.finished_ok.connect(self._on_refresh_row)
        worker.finished_err.connect(self._on_refresh_err)
        # A thread per tick, parented to the window, would be kept alive by
        # that parent: 86400 of them on a Resources window left open for a day.
        # Dropped as each one finishes instead.
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self._refresh_worker_done)
        worker.start()

    def _refresh_worker_done(self) -> None:
        """Let go of a finished poll thread; it is about to be deleted."""
        self._refresh_worker = None

    def _on_refresh_row(self, row: Any) -> None:
        self.row = row
        self._forget_row_caches()
        self._paint_live_values(row)
        # Same reason the traffic poll does it: which values the device reports
        # at all is only knowable from a reply that carries them, and a
        # `hideonempty` row must appear on the read that first brings one.
        self._refresh_empty_rows()
        # A reading the pointer is holding keeps its old text (see
        # _set_live_text), so a number that has visibly stopped has a reason
        # and this is where it is given.
        n_held = sum(
            1 for labels in self._live_labels.values()
            for _name, lab in labels if getattr(lab, "is_held", False)
        )
        self._set_live_status(
            f"{n_held} value(s) held while selected" if n_held else ""
        )
        # The tray reads the same object (Enabled · Status: bound).
        self._update_form_footer()

    def resume_after_reconnect(self) -> None:
        """Pick the auto-refresh back up over a new session.

        A failed read stops it (_on_refresh_err), and while the connection was
        down every read failed. The live-traffic poll never stops, so it simply
        starts answering again.
        """
        timer = getattr(self, "_refresh_timer", None)
        if timer is not None and not timer.isActive():
            self._set_live_status("")
            timer.start()

    def _on_refresh_err(self, err: str) -> None:
        """A failed re-read stops the timer rather than repeating every second.

        The same posture as the toolbar's resource poll: one line saying what
        went wrong, and no stream of identical failures behind it. Reopening
        the window starts a fresh poll.
        """
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._set_live_status(f"Refresh stopped — {err}")
        applog.note(f"{self._base_title}: autorefresh stopped after: {err}")

    def _preferred_width(self) -> int:
        """Open wide enough for the whole tab strip when that is reasonable.

        Winbox scrolls its tabs; not needing to scroll at all is better, so
        size to the strip up to a sane cap and let _reveal_tab_context handle
        the menus that are still too wide (WiFi has eleven tabs).

        Only the literals go through the zoom. The strip's hint and the action
        column are measured from the font that is already scaled, so scaling
        them again would square the factor.
        """
        base = style.scale_px(560)
        if self._tabs is None:
            return base
        try:
            strip = self._tabs.tabBar().sizeHint().width()
        except RuntimeError:
            return base
        actions = getattr(self, "_action_col_width", style.scale_px(150))
        return max(base, min(strip + actions + style.scale_px(40), style.scale_px(1000)))

    def _reveal_tab_context(self, index: int) -> None:
        """Keep one tab visible before the selected one, like Winbox.

        Clicking the leftmost visible tab scrolls its predecessor into view, so
        the clicked tab becomes the second one and repeated clicks walk back to
        the start. QTabBar only exposes scrolling via setCurrentIndex, so step
        onto the previous tab and back with the change handler muted.
        """
        tabs = self._tabs
        if tabs is None or index <= 0:
            return
        bar = tabs.tabBar()
        try:
            prev = bar.tabRect(index - 1)
            if not prev.isValid():
                return
            visible = bar.rect()
            if visible.left() <= prev.left() and prev.right() <= visible.right():
                return  # predecessor already on screen
            self._tab_scroll_guard = True
            try:
                bar.setCurrentIndex(index - 1)
                bar.setCurrentIndex(index)
            finally:
                self._tab_scroll_guard = False
        except RuntimeError:
            pass

    def _ensure_page(self, index: int) -> None:
        """Build one tab's rows, the first time anything needs them.

        Winbox shows a property form the moment you double-click; ours cannot
        while it is constructing every tab's editors first. So a tab holds an
        empty box until it is selected (or until something needs the whole form
        -- see _ensure_all_pages), then its rows are built and registered
        exactly as they would have been up front.
        """
        tabs = self._tabs
        if tabs is None:
            return
        self._ensure_page_widget(tabs.widget(index))

    def _ensure_page_widget(self, holder) -> None:
        pending = self._pending_pages
        if holder is None or holder not in pending:
            return
        tab_name, items = pending.pop(holder)
        layout = holder.layout()
        if layout is None:      # not one of our holders; nothing to fill
            return
        # self._ro, not the constructor's `ro`: the session may have switched
        # between read-only and queue mode since the window opened, and
        # _form_page decides each row's lock state from what it is handed.
        page = self._scrollable(self._form_page(items, self._ro, tab_name=tab_name))
        layout.addWidget(page)
        self._wire_dirty_tracking()
        self._refresh_dirty_highlights()
        self._refresh_conditional_rows()
        # Built from specs rendered when the form opened; a poll may have
        # answered since (and a stats counter only ever arrives that way).
        self._paint_live_values(self.row)

    def _ensure_all_pages(self) -> None:
        """Build every tab. For anything that has to see the whole form: a
        whole-form write, and validation -- a required field two tabs over is
        still required."""
        for holder in list(self._pending_pages):
            self._ensure_page_widget(holder)

    def _on_tab_changed(self, index: int) -> None:
        if self._tab_scroll_guard:
            return
        self._ensure_page(index)
        self._reveal_tab_context(index)
        if self._poll_timer is None:
            return
        if self._traffic_tab_active():
            if not self._poll_timer.isActive():
                self._poll_timer.start()
            self._poll_traffic_tick()
        else:
            self._poll_timer.stop()
            self._set_live_status("")

    def _set_poll_rate(self, ms: int) -> None:
        """Change how often the Traffic tab refreshes, and remember it.

        Floored at the device's own tick -- see TrafficGraphBox.RATES.
        """
        ms = max(TRAFFIC_POLL_MIN_MS, min(60000, int(ms)))
        if ms == self._poll_ms:
            return
        self._poll_ms = ms
        save_traffic_poll_ms(ms)
        if self._poll_timer is not None:
            self._poll_timer.setInterval(ms)
            if self._poll_timer.isActive():
                self._poll_traffic_tick()
        for box in self._graph_widgets:
            try:
                box.set_rate_ms(ms)
            except RuntimeError:
                pass

    def _poll_traffic_tick(self) -> None:
        if self._poll_busy or self.client is None or self._obj_id is None:
            return
        if self._poll_worker is not None and self._poll_worker.isRunning():
            return
        self._poll_busy = True
        client = self.client
        path = self.path
        oid = self._obj_id
        from_path = self._from_path

        def fetch():
            from x3.proto.device import FILTER_IFACE_TRAFFIC

            # Polling is deliberately kept out of the session log.
            return client.getobj(
                path,
                oid,
                filter_mask=FILTER_IFACE_TRAFFIC,
                from_path=from_path,
                quiet=True,
            )

        worker = _TrafficPollWorker(fetch, self)
        self._poll_worker = worker
        worker.finished_ok.connect(self._on_traffic_row)
        worker.finished_err.connect(self._on_traffic_err)
        worker.finished.connect(lambda: setattr(self, "_poll_busy", False))
        worker.start()

    def _set_live_status(self, text: str) -> None:
        """Show the live-traffic line, or take it away when it says nothing."""
        if self._live_status is None:
            return
        self._live_status.setText(text)
        self._live_status.setVisible(bool(text))

    def _on_traffic_err(self, err: str) -> None:
        self._set_live_status(f"Live traffic: error — {err}")

    def _paint_live_values(self, row: Any) -> None:
        """Repaint every registered label from ``row``.

        Used by the poll, and by a tab built after one: its editors are made
        from specs rendered when the form opened, which by then are stale.
        """
        # Update every registered label by field code
        by_code: dict[int, str] = {}
        by_name: dict[tuple[int, str], str] = {}
        for code, name, rendered, _ok in getattr(row, "fields", []) or []:
            by_code[code] = rendered or "—"
            by_name[(code, name)] = rendered or "—"
        # Also fill from form_fields (tab-aware names)
        for _tab, code, name, rendered, _ok in getattr(row, "form_fields", []) or []:
            if rendered:
                by_code[code] = rendered
                by_name[(code, name)] = rendered

        for code, bars in self._live_bars.items():
            raw = row.raw_value(code) if hasattr(row, "raw_value") else None
            for bar in bars:
                try:
                    bar.set_reading(raw)
                except RuntimeError:
                    pass

        for code, boxes in self._live_lists.items():
            raw = row.raw_value(code) if hasattr(row, "raw_value") else None
            if raw is None and code not in by_code:
                continue      # this reply says nothing about that list
            for box in boxes:
                try:
                    box.restate(raw, by_code.get(code, ""))
                except RuntimeError:
                    pass

        for code, labels in self._live_labels.items():
            for name, lab in labels:
                text = by_name.get((code, name))
                if text is None:
                    fld = self.menu.fields.get(code) if self.menu else None
                    if fld is not None and fld.variants:
                        # This id is two fields; without a name match there is
                        # no telling which one the poll answered for.
                        continue
                    text = by_code.get(code)
                if text is None:
                    raw = row.raw_value(code) if hasattr(row, "raw_value") else None
                    if raw is None:
                        continue
                    text = _format_live_value(code, raw, self.menu)
                _set_live_text(lab, text or "—")

    def _on_traffic_row(self, row: Any) -> None:
        self.row = row
        self._forget_row_caches()
        self._paint_live_values(row)
        # Which counters this port keeps is only knowable from a reply that
        # carries counters, and this is that reply.
        self._refresh_empty_rows()

        # Graphs: Byte graph uses rates (bps); packet graph uses packet rates
        rx_bps = _as_int(row.raw_value(0x100D3)) if hasattr(row, "raw_value") else None
        tx_bps = _as_int(row.raw_value(0x100D4)) if hasattr(row, "raw_value") else None
        rx_pps = _as_int(row.raw_value(0x100CB)) if hasattr(row, "raw_value") else None
        tx_pps = _as_int(row.raw_value(0x100CD)) if hasattr(row, "raw_value") else None
        for box in self._graph_widgets:
            box.push_sample(rx_bps or 0, tx_bps or 0, rx_pps or 0, tx_pps or 0)

        n_held = sum(
            1 for labels in self._live_labels.values()
            for _name, lab in labels if getattr(lab, "is_held", False)
        )
        self._set_live_status(
            f"{n_held} value(s) held while selected" if n_held else ""
        )
        # Keep the tray status bar in sync (Enabled · Status: Bound).
        self._update_form_footer()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._confirm_close_if_dirty():
            event.ignore()
            return
        if self._poll_timer is not None:
            self._poll_timer.stop()
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        for worker in (self._poll_worker, self._refresh_worker):
            if worker is not None and worker.isRunning():
                worker.wait(500)
        super().closeEvent(event)

    def _make_editor(
        self, spec: dict[str, Any], readonly: bool, *, wrap_optional: bool = True,
        wrap_negate: bool = True,
    ) -> QWidget:
        fld: Field | None = spec.get("field")
        rendered = spec.get("rendered", "")
        raw = spec.get("raw")

        # `optional: True` is Winbox's "+" field: no value until you add one.
        if wrap_optional and fld is not None and getattr(fld, "optional", False):
            inner = self._make_editor(spec, readonly, wrap_optional=False)
            editor = OptionalFieldEditor(
                fld, inner, present=_optional_is_set(fld, raw), readonly=readonly
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A bare `not`-wrapped value (firewall TOS Value, Discovery Interface
        # List): the inner editor plus a "!" toggle writing the negate code.
        if wrap_negate and fld is not None and getattr(fld, "negate_code", None) is not None:
            inner = self._make_editor(
                spec, readonly, wrap_optional=False, wrap_negate=False
            )
            negated = (getattr(self.row, "values_by_code", {}) or {}).get(
                fld.negate_code
            ) in (True, 1)
            editor = NegatableEditor(
                fld, inner, fld.negate_code, negated=negated, readonly=readonly
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # An optional match (firewall Src. Address, In. Interface, Protocol…)
        # is three sibling wire codes, not one value — see OptMatchEditor.
        if fld and fld.jg_type == "opt":
            return self._make_opt_editor(fld, readonly)

        # A `single` union of address / DNS-name variants (Allow Address, an
        # address-list entry): one box, the text decides which variant is
        # written — see UnionEditor.
        if fld and _is_switchable_union(fld):
            editor = UnionEditor(
                fld,
                getattr(self.row, "values_by_code", {}) or {},
                readonly=readonly,
                is_add=self._write_action(self._object_id()) == "add",
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A multi of networks (IP ▸ Services "Available From") is an array of
        # (address, mask) entries, which Winbox edits as one box per line.
        if fld and fld.jg_type == "multi" and _is_network_multi(fld):
            editor = NetworkArrayEditor(fld, raw, readonly=readonly)
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A `gridmultinumber` is a positional array -- element i is the value
        # for the rate the vocabulary calls i -- so it is edited as one
        # labelled cell per rate, not as a comma-run in a text box.
        if fld and (fld.jg_type or "") == "gridmultinumber" and fld.enum:
            editor = NumberGridEditor(fld, raw, readonly=readonly)
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A scalar array (DHCP Options, SMS Allowed Numbers, DNS Servers) with no
        # enum source: one box per entry with +/×. Enum arrays keep their
        # dropdown-per-row editor below.
        if fld and is_scalar_multi(fld) and not (fld.enum or fld.enum_path):
            editor = ScalarArrayEditor(fld, raw, readonly=readonly)
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A multitristate (firewall Hotspot, Log Topics) is a set of flags each
        # of which can be required-set / required-clear / ignored -- two u32
        # bitfields (value + mask). See MultiTristateEditor.
        if fld and (fld.jg_type or "").startswith("multitristate"):
            editor = MultiTristateEditor(
                fld, getattr(self.row, "values_by_code", {}) or {}, readonly=readonly
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A set is a multi-select bitmask (PPP Authentication, cipher lists) --
        # WinBox shows a checkbox per member. `multibits` (Neighbour System
        # Caps) is the same bit-index bitmask. See SetEditor.
        if fld and (fld.jg_type or "") in ("set", "multibits"):
            editor = SetEditor(
                fld, getattr(self.row, "values_by_code", {}) or {}, readonly=readonly
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # A number range (firewall To Ports, Signal Strength Range): a low-high
        # pair, not a single value. See RangeEditor.
        if (
            fld and (fld.jg_type or "") in ("numberrange", "integerrange")
            and fld.highid is not None
        ):
            editor = RangeEditor(
                fld, getattr(self.row, "values_by_code", {}) or {}, readonly=readonly
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        # Bool
        if fld and fld.jg_type in ("bool", "flag"):
            cb = QCheckBox()
            # `inv:1`: the box shows the opposite of the bit (see Field.invert),
            # and encode_form_value inverts it again on the way out.
            if isinstance(raw, bool):
                cb.setChecked(raw != fld.invert)
            elif isinstance(raw, int):
                cb.setChecked(bool(raw) != fld.invert)
            else:
                cb.setChecked(str(rendered).lower() in ("true", "yes", "1"))
            # Kept on the widget so a re-read can invert it again: a plain
            # QCheckBox has no field to ask (see _checkbox_inversion).
            cb.setProperty("invertedFlag", bool(fld.invert))
            cb.setEnabled(not readonly)
            return cb

        # Static or dynamic enum choices
        choices: list[tuple[Any, str]] = []
        if fld and self.client is not None:
            try:
                choices = self.client.enum_choices(fld)
            except Exception:
                choices = []
        elif fld and fld.enum:
            choices = list(sorted(fld.enum.items()))
        if not choices and fld is not None and getattr(fld, "enum_query", None):
            choices = self._query_enum_choices(fld)
        choices = self._filter_enum_choices(fld, choices, raw)

        # Multi-value enums (Ethernet Advertise…) are an *array* on the wire.
        # Winbox edits them as one dropdown per row with ▲/▼ to insert/remove.
        if choices and _is_multi_enum(fld):
            max_rows = getattr(fld, "maximum", None) or 200
            editor = MultiEnumArrayEditor(
                choices,
                raw,
                rendered,
                readonly=readonly,
                max_rows=int(max_rows) if max_rows else 200,
                name=(fld.name or "") if fld is not None else "",
                marks=self._field_marks(fld),
            )
            path = self._enum_source_path(fld)
            if path is not None:
                editor.set_mark_source(
                    self.client, path, lambda: self._field_marks(fld)
                )
            return editor

        # `radio:1`: Winbox draws these as radio buttons (Rate default /
        # configured, PPP Use IPv6 no / yes / required / default). Every field
        # declaring it has five members or fewer.
        if choices and fld is not None and getattr(fld, "radio", False):
            # Buttons always go on one row. `compact:1` keeps the group in the
            # label column; without it Winbox gives it a titled rule of its own
            # (wireless Rate, PPP Use IPv6) -- see RadioGroupEditor.
            editor = RadioGroupEditor(
                choices, raw,
                compact=True,
                readonly=readonly,
                title=(None if getattr(fld, "compact", False)
                       else (spec.get("name") or fld.name)),
            )
            editor.changed.connect(self._refresh_dirty_highlights)
            return editor

        if choices:
            # MarkedCombo, not QComboBox: it re-reads its source table as it
            # opens so the status dots are about now and not about whenever
            # the form was built. It is a QComboBox everywhere that matters --
            # _editor_value, _restate_combo and the isinstance ladders below.
            combo = MarkedCombo()
            combo.setEnabled(not readonly)
            current_idx = 0
            raw_cmp = raw
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw_cmp = int.from_bytes(raw, "little")
            except Exception:
                pass
            # Only match scalars — a list must not select the first enum by accident.
            if isinstance(raw_cmp, list):
                raw_cmp = raw_cmp[0] if len(raw_cmp) == 1 else object()
            matched = False
            for i, (wire, label) in enumerate(choices):
                combo.addItem(label, wire)
                if not matched and (wire == raw_cmp or label == rendered):
                    current_idx = i
                    matched = True
            # A new object has no value for anything the .jg gave no `def`, and
            # a Winbox dropdown cannot show nothing: the box opens on its first
            # entry, and that entry is what the ADD writes. So a new bridge
            # port reads Learn: auto, Frame Types: admit all, MVRP Registrar
            # State: normal -- exactly v3 -- where ours showed six blank boxes.
            # Only on the Add form: an existing object whose reply left a field
            # out must keep saying nothing rather than offer to write entry one.
            if not matched and self._adding() and raw is None and not rendered:
                current_idx, matched = 0, True
            combo.setCurrentIndex(current_idx if matched else -1)
            # A dynamic-name string (address list Name, packet/connection marks):
            # WinBox lets you pick an existing name OR type a new one, so the box
            # is always editable and its text is the value.
            if fld is not None and getattr(fld, "enum_query", None):
                self._query_enums.append((fld, combo))
            matched = self._add_union_alt_choices(combo, fld, matched)
            name_combo = bool(
                fld and (fld.jg_type or "") == "string" and fld.enum_path
            )
            if name_combo:
                combo.setEditable(True)
                combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
                combo.setCurrentText("" if rendered is None else str(rendered))
                combo.lineEdit().setReadOnly(readonly)
            elif not matched and rendered:
                # Show device text even when it is not a known single enum key.
                combo.setEditable(True)
                combo.setCurrentText(str(rendered))
                combo.lineEdit().setReadOnly(readonly)
            self._mark_combo(combo, fld)
            return combo

        # A clocktime is a time of day in seconds since midnight -- the same
        # hh:mm:ss box an interval gets, and the same wire units.
        if fld and fld.jg_type == "clocktime":
            wire = None
            if raw is not None:
                try:
                    wire = int(raw) % 86400
                except (TypeError, ValueError):
                    wire = None
            dur = DurationEdit(wire, scale=None)
            dur.setReadOnly(readonly)
            return dur

        # Interval (Send Interval, Disable Time, …): hh:mm:ss in the UI, whole
        # seconds on the wire.
        if fld and fld.jg_type == "interval":
            wire = None
            if raw is not None:
                try:
                    iv = int(raw)
                    # The router marks "unset" with a high sentinel; leave the box blank.
                    if iv < 0xFFFFFF00:
                        wire = iv
                except (TypeError, ValueError):
                    wire = None
            dur = DurationEdit(wire, scale=fld.scale)
            dur.setReadOnly(readonly)
            return dur

        # Number. The widget type is chosen from the field, never from the
        # current mode: if read-only built a QLineEdit and queue mode a
        # QSpinBox, switching mode in place would change the value's type and
        # make every number look edited. Scaled fields (an interval stored in
        # ms but shown as '600s') stay text, since a spin box would have to
        # show the unscaled number.
        if (
            fld
            and fld.jg_type in ("number", "bignumber", "decimal", "numberrange")
            and not fld.scale
            and getattr(fld, "radix", 10) != 16
        ):
            try:
                iv = int(raw) if raw is not None else self._add_floor(fld)
            except (TypeError, ValueError):
                iv = None
            # The router marks "unset" with a high sentinel — IP ▸ Services carries
            # Parent = 0xFFFFFFFF — which does not fit a QSpinBox's signed
            # 32-bit range. setValue() raised OverflowError, the form failed to
            # build, and the row simply would not open on double-click. A
            # sentinel is not a number to edit as one, so it falls through to
            # the text editor below, where render() has already blanked it.
            if iv is not None and -2147483648 <= iv <= 2147483647:
                spin = QSpinBox()
                spin.setRange(-2147483648, 2147483647)
                if fld.postfix:
                    spin.setSuffix(f" {fld.postfix}")
                spin.setValue(iv)
                # Winbox has no steppers: a number is typed into a box the same
                # width as the row above it. The spin box stays for the typing
                # validation and the wire-typed value(), without the arrows or
                # the width cap they justified.
                spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
                spin.setEnabled(not readonly)
                return spin

        # A hyperlink is a value plus a way into the menu it belongs to.
        if fld is not None and getattr(fld, "link_path", None):
            return self._link_row(fld, rendered, readonly)

        from x3.proto.schema import is_secret

        # A `private` secret is one the device never sends back, so whatever
        # arrived is not it: start empty rather than showing a stand-in that
        # would be sent back as if it were the password.
        if fld is not None and getattr(fld, "private", False):
            rendered = ""
        edit = QLineEdit("" if rendered is None else str(rendered))
        edit.setReadOnly(readonly)
        # `cols`: the .jg's own width for the box, in characters.
        cols = getattr(fld, "cols", None) if fld is not None else None
        if isinstance(cols, int) and cols > 0:
            edit.setMinimumWidth(
                edit.fontMetrics().horizontalAdvance("0") * cols + 12
            )
        if is_secret(fld):
            edit.setEchoMode(
                QLineEdit.EchoMode.Normal
                if self._show_pw
                else QLineEdit.EchoMode.Password
            )
        return edit

    def _adding(self) -> bool:
        """Whether this form is the Add form for a new object."""
        return self._write_action(self._object_id()) == "add"

    def _add_floor(self, fld: Field | None) -> int:
        """What an unset number starts at: its ``min``, or zero.

        A field the .jg gives no ``def`` still has a floor, and Winbox's box
        cannot go under it -- which is the whole story of PVID. 6.x declared
        ``def:1`` on it; 7.x dropped the def and kept ``min:1``, and v3 still
        opens a new bridge port on PVID 1 because 0 is not a value the box can
        hold. Ours wrote 0, and the router reads PVID 0 as "no VLAN".
        """
        if not self._adding() or fld is None:
            return 0
        low = getattr(fld, "minimum", None)
        return int(low) if isinstance(low, int) and low > 0 else 0

    def _object_defaults(self) -> dict[int, Any]:
        """The values this object would take where it sets nothing itself.

        The menu names a nested message the device sends alongside the object
        (`def:'mfe0024'` on WiFi and WiFi Configuration, `m7008` on a CAP
        interface); inside it, the same field codes carry the inherited value.
        That is what a `showdef` field shows greyed -- a WiFi interface with no
        SSID of its own displays the one its configuration profile gives it.
        """
        if self._defaults_cache is not None:
            return self._defaults_cache
        out: dict[int, Any] = {}
        code = getattr(self.menu, "defaults_code", None)
        if isinstance(code, int):
            nested = self._row_values().get(code)
            if nested is None and hasattr(self.row, "raw_value"):
                nested = self.row.raw_value(code)
            try:
                for c, _tag, value in nested or ():
                    out.setdefault(c, value)
            except TypeError:
                pass        # not a message: nothing to inherit
        self._defaults_cache = out
        return out

    def _with_device_default(self, fld: Field) -> Field:
        """Give a ``showdef`` field the default the device reports for it.

        The editor already knows how to show a default greyed while a field is
        unset (OptionalFieldEditor); all that was missing was the value, which
        is not in the schema -- it is per object.
        """
        if fld is None or not getattr(fld, "show_default", False):
            return fld
        defaults = self._object_defaults()
        if fld.code not in defaults:
            return fld
        return replace(fld, default=defaults[fld.code])

    def _with_slot_enum(self, fld: Field) -> Field:
        """Fill in a ``slotenum`` field's entries from this object's own slots.

        Wireless Tx/Rx Chains has no vocabulary in the schema at all: the card
        reports its chains, so the checkbox list has to be built from the row.
        Two chains on a 2x2 radio, four on a 4x4 -- against a hardcoded four
        the extra boxes wrote bits the card does not have.
        """
        if fld is None or not getattr(fld, "enum_slots", None):
            return fld
        try:
            entries = sch.enum_from_slots(fld, self.menu, self._row_values())
        except Exception:  # noqa: BLE001
            return fld
        return replace(fld, enum=entries) if entries else fld

    def _add_union_alt_choices(
        self, combo: QComboBox, fld: Field | None, matched: bool
    ) -> bool:
        """Add the other members of a ``single`` union to this one dropdown.

        Winbox draws Frequency as one box: 'auto' comes from one member of the
        union and the actual frequencies from the other, which is a
        ``queryenum`` the router answers. Each added entry remembers the code
        it belongs to (see ALT_CODE_ROLE) so picking one writes to the member
        that owns it.
        """
        alts = list(getattr(fld, "union_alts", None) or [])
        if not alts:
            return matched
        values = self._row_values()
        have = {combo.itemData(i) for i in range(combo.count())}
        for alt in alts:
            entries: list[tuple[Any, str]] = []
            if getattr(alt, "enum_query", None):
                entries = self._query_enum_choices(alt)
            elif alt.enum or alt.enum_path:
                try:
                    entries = (
                        self.client.enum_choices(alt) if self.client is not None
                        else sorted((alt.enum or {}).items())
                    )
                except Exception:  # noqa: BLE001
                    entries = []
            if not entries:
                continue
            if getattr(alt, "enum_query", None):
                self._query_enums.append((alt, combo))
            for wire, label in entries:
                if wire in have:
                    continue
                combo.addItem(str(label), wire)
                combo.setItemData(combo.count() - 1, alt.code, ALT_CODE_ROLE)
                have.add(wire)
            # The row's value may live on the alternative rather than on the
            # primary: a wlan on a fixed frequency has u25d set and u25a not.
            raw_alt = values.get(alt.code)
            if not matched and raw_alt is not None:
                idx = combo.findData(raw_alt)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                    matched = True
        return matched

    def _link_row(self, fld: Field, rendered: Any, readonly: bool) -> QWidget:
        """A ``type:hyperlink`` value: what it names, and a way to open it.

        A route's Immediate Gateway is a row of (44,16) and its Session a row
        of (44,33); the value is that object's id, which is also what names it,
        so the box reads as a name and the button beside it opens the menu the
        object lives in.
        """
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        box = ReadOnlyValueEdit(str(rendered or ""))
        row.addWidget(box, 1)
        if self._on_context_action is not None:
            btn = QToolButton()
            btn.setText("→")
            btn.setToolTip(f"Open the menu {fld.name or 'this value'} lives in")
            # A tab stop, so following the link is a keystroke like everything
            # else on the form (Space or Enter on it).
            btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            btn.clicked.connect(
                lambda _=False, f=fld: self._run_context_action({
                    "name": f.name, "path": f.link_path,
                })
            )
            row.addWidget(btn, 0)
        host.editor_value = lambda: str(rendered or "")   # type: ignore[attr-defined]
        host.text = box.text                              # type: ignore[attr-defined]
        host.set_locked = lambda _locked: None            # type: ignore[attr-defined]
        return host

    def _query_enum_params_for(self, fld: Field) -> dict[int, Any]:
        """The parameters a `queryenum` is asked with, off this form.

        Each parameter is (name, code): the *name* finds the field on this
        form, the *code* is what the target handler calls it. They are not the
        same number -- Quick Set keeps Band at 0x259 and the frequency query
        wants it as 0x81a -- so a code-for-code copy asks with nothing.
        """
        spec = getattr(fld, "enum_query", None) or {}
        vals = self._live_values()
        out: dict[int, Any] = {}
        for name, code in spec.get("params") or []:
            if not isinstance(code, int):
                continue
            value = sch.field_value_by_name(self.menu, name, vals)
            if value is not None:
                out[code] = value
        return out

    def _query_enum_choices(self, fld: Field) -> list[tuple[Any, str]]:
        """Ask the device what this dropdown may hold.

        Wireless / Quick Set Frequency is not a table the router keeps but an
        answer it computes from band, channel width and country -- see
        DeviceClient.query_enum. An unanswerable query gives no choices, and
        the field falls back to the plain box it was before.
        """
        client = self.client
        if client is None or not hasattr(client, "query_enum"):
            return []
        try:
            entries = client.query_enum(
                fld.enum_query, self._query_enum_params_for(fld)
            )
        except Exception:  # noqa: BLE001
            return []
        postfix = (fld.postfix or "").strip()
        return [
            (wire, f"{label}{postfix}" if postfix and label.isdigit() else label)
            for wire, label in sorted(entries.items())
        ]

    def _refresh_query_enums(self) -> None:
        """Re-ask a `queryenum` dropdown when its parameters change.

        Changing Band or Country changes which frequencies are legal, and
        Winbox re-reads the list. Guarded on the parameters actually having
        moved: this runs from the same pass as every other edit-time refresh,
        and a device round trip per keystroke is not that.
        """
        if not self._query_enums:
            return
        params = tuple(
            (fld.code, tuple(sorted(self._query_enum_params_for(fld).items())))
            for fld, _combo in self._query_enums
        )
        if params == self._query_enum_params:
            return
        self._query_enum_params = params
        for fld, combo in self._query_enums:
            try:
                current = combo.currentData()
                choices = self._query_enum_choices(fld)
                if not choices:
                    continue
                combo.blockSignals(True)
                combo.clear()
                for wire, label in choices:
                    combo.addItem(label, wire)
                idx = combo.findData(current)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
                combo.blockSignals(False)
            except RuntimeError:
                continue

    def _enum_source_path(self, fld: Field | None) -> tuple[int, ...] | None:
        """The table a field's dropdown offers, named or implied."""
        if fld is None:
            return None
        path = fld.enum_path or sch_implied_enum_path(fld)
        return tuple(path) if path else None

    def _field_marks(self, fld: Field | None) -> dict:
        """This field's dropdown marks, keyed the way its item data is.

        Most enums store the target's id, so the marks come back as they are.
        A string one stores the *name* (``device.enum_choices`` returns the
        label on both sides for those, because the device wants b'input' and
        not the row number), and marks keyed by id would match nothing.
        """
        path = self._enum_source_path(fld)
        if path is None or self.client is None:
            return {}
        marks = marks_for(self.client, path)
        if not marks:
            return {}
        if getattr(fld, "prefix", "") == "s" or (fld.jg_type or "") == "string":
            try:
                names = self.client.enum_map(path)
            except Exception:  # noqa: BLE001
                return {}
            return {names[oid]: mk for oid, mk in marks.items() if oid in names}
        return marks

    def _mark_combo(self, combo, fld: Field | None) -> None:
        """Hang the source rows' state and comments on a filled dropdown."""
        path = self._enum_source_path(fld)
        if path is None:
            return
        setter = getattr(combo, "set_mark_source", None)
        if setter is not None:
            setter(self.client, path, provider=lambda: self._field_marks(fld))
        apply_marks(combo, self._field_marks(fld))

    def _filter_enum_choices(
        self, fld: Field | None, choices: list[tuple[Any, str]], raw: Any
    ) -> list[tuple[Any, str]]:
        """Drop the entries the .jg's ``enumfilter`` says this object cannot use.

        Wireless Band lists every band the router knows; the radio's own
        ``wphymodes`` bits say which of them this card has, and offering the
        rest is offering a write the device will refuse. Whatever the device
        currently reports is always kept -- a value we would not offer is
        still the value in front of the user.
        """
        if not choices or fld is None or not getattr(fld, "enum_filters", None):
            return choices
        try:
            allowed = sch.enum_allowed_ids(
                fld, self.menu, self._row_values(),
                board=self._board_name(), ui_slots=self._ui_slots(),
                syscaps=self._syscaps(),
            )
        except Exception:  # noqa: BLE001
            return choices
        if allowed is None:
            return choices
        keep = [c for c in choices if c[0] in allowed or c[0] == raw]
        # An empty result means we misread the filter, not that the field has
        # no legal value: show the lot rather than an empty dropdown.
        return keep or choices

    def _make_opt_editor(self, fld: Field, readonly: bool) -> QWidget:
        """Editor for an optional match, with its picker filled from the device."""
        _neg, inner = sch_opt_chain(fld)
        choices: list[tuple[Any, str]] | None = None
        groups: list[tuple[int, str, str]] | None = None
        if inner is not None:
            if inner.enum:
                choices = sorted(inner.enum.items())
            elif self.client is not None:
                # An inner enm with no declared source is an interface — the
                # .jg leaves the path off. Winbox groups those by kind.
                implied = sch_implied_enum_path(inner)
                if implied == sch.IFACE_ENUM_PATH:
                    try:
                        groups = self.client.interface_choices()
                    except Exception:  # noqa: BLE001
                        groups = None
                elif implied:
                    # Narrowed if the .jg narrowed it: an inner picker reads
                    # the same source table under the same pred.
                    pred = getattr(inner, "enum_pred", None)
                    try:
                        names = (
                            self.client.enum_source_map(implied, pred) if pred
                            else self.client.enum_map(implied)
                        )
                        choices = sorted(names.items())
                    except Exception:  # noqa: BLE001
                        choices = None
        values = getattr(self.row, "values_by_code", {}) or {}
        # The inner field is what the picker offers, so it is the inner field's
        # source table the marks come from -- not the optional wrapper's.
        marks = self._field_marks(inner) if inner is not None else {}
        mpath = self._enum_source_path(inner) if inner is not None else None
        editor = OptMatchEditor(
            fld, values, choices=choices, groups=groups, readonly=readonly,
            show_passwords=self._show_pw,
            marks=marks,
            mark_source=(
                (self.client, mpath, lambda: self._field_marks(inner))
                if mpath is not None else None
            ),
        )
        editor.changed.connect(self._refresh_dirty_highlights)
        return editor

    def opt_errors(self) -> list[str]:
        """Reasons the optional matches cannot be sent as typed."""
        self._ensure_all_pages()
        out = []
        for _spec, editor in self._rows:
            if isinstance(editor, (OptMatchEditor, NetworkArrayEditor, UnionEditor,
                                   MultiTristateEditor, SetEditor,
                                   ScalarArrayEditor, RangeEditor)):
                why = editor.error()
                if why:
                    out.append(why)
        return out

    def required_errors(self) -> list[str]:
        """Names of `req` fields left empty. WinBox blocks OK until they are
        filled; the device does not, so an empty one otherwise makes a broken
        object (a gateway-less route that silently vanishes)."""
        self._ensure_all_pages()
        out = []
        for spec, editor in self._rows:
            fld = spec.get("field")
            if fld is None or not getattr(fld, "required", False):
                continue
            if spec.get("locked") or spec.get("always_locked"):
                continue
            name = fld.name or f"<{fld.code:#x}>"
            # Composite editors report their own presence; a plain editor is
            # empty when its value is.
            if isinstance(editor, (OptMatchEditor, UnionEditor, OptionalFieldEditor)):
                if not editor.is_present():
                    out.append(name)
            elif not _has_value(_editor_value(editor)):
                out.append(name)
        return out

    def edits(self, *, changed_only: bool = False) -> list[dict[str, Any]]:
        """Only the fields whose editor differs from what the device sent.

        Queueing every field would stage ~25 writes for a one-field change and
        would push device-reported status values back at the router.
        """
        # An Add has nothing on the device to compare against: "unchanged"
        # means "still showing the form's default", and those defaults are
        # exactly what the new object needs. Filtering them out is what left a
        # new filter rule with no chain -- the router refused it outright.
        #
        # A singleton (`item`) menu sends its whole form too, for a different
        # reason: 0xFE000E means "apply this form", so a partial send risks the
        # device defaulting whatever was left out. Captured from Winbox on
        # [123,1] IP ▸ Cloud, which sends every field on Apply.
        # `changed_only` asks the other question: not "what do we send?" but
        # "what has the user altered?". They differ for a whole-form write —
        # a singleton sends every field, so using the send list to decide
        # whether anything is unsaved reported five untouched fields on IP ▸
        # Cloud, each with its old value rendered two different ways.
        if not changed_only:
            # A whole-form write sends every field, so every field has to exist.
            # (`changed_only` asks what the user altered; a tab never opened
            # cannot hold an alteration, so it needs no page built.)
            self._ensure_all_pages()
        adding = (
            not changed_only
            and self._write_action(self._object_id()) in ("add", "set_settings")
        )
        out: list[dict[str, Any]] = []
        for spec, editor in self._rows:
            if spec.get("locked"):
                continue
            # An optional match owns three wire codes, so it reports its own
            # dirtiness and carries the extra codes alongside.
            if isinstance(editor, OptMatchEditor):
                if not editor.is_dirty() and not (adding and editor.is_present()):
                    continue
                wire = editor.wire_values()
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": wire.get(spec["code"]),
                    "extra": {c: v for c, v in wire.items() if c != spec["code"]},
                    "label": editor.label(),
                })
                continue
            # A set owns one bitmask code; report its own dirtiness.
            if isinstance(editor, SetEditor):
                if not editor.is_dirty() and not (adding and editor.is_present()):
                    continue
                wire = editor.wire_values()
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": wire.get(spec["code"]),
                    "label": editor.label(),
                })
                continue
            # A multitristate owns a value + a mask code; report its own
            # dirtiness and carry the mask alongside.
            if isinstance(editor, MultiTristateEditor):
                if not editor.is_dirty() and not (adding and editor.is_present()):
                    continue
                wire = editor.wire_values()
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": wire.get(spec["code"]),
                    "extra": {c: v for c, v in wire.items() if c != spec["code"]},
                    "label": editor.label(),
                })
                continue
            # A number range owns a low + high code.
            if isinstance(editor, RangeEditor):
                if not editor.is_dirty() and not (adding and editor.is_present()):
                    continue
                wire = editor.wire_values()
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": wire.get(spec["code"]),
                    "extra": {c: v for c, v in wire.items() if c != spec["code"]},
                    "label": editor.label(),
                })
                continue
            # A single union owns one variant's codes; like the optional match
            # it reports its own dirtiness and carries the extra codes.
            if isinstance(editor, UnionEditor):
                if not editor.is_dirty() and not (adding and editor.is_present()):
                    continue
                wire = editor.wire_values()
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": wire.get(spec["code"]),
                    "extra": {c: v for c, v in wire.items() if c != spec["code"]},
                    "label": editor.label(),
                })
                continue
            if isinstance(
                editor, (NetworkArrayEditor, ScalarArrayEditor, NumberGridEditor)
            ):
                if not editor.is_dirty() and not (adding and editor.texts()):
                    continue
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": editor.wire_value(),
                    "label": editor.label(),
                })
                continue
            if isinstance(editor, OptionalFieldEditor):
                if not editor.is_dirty() and not (adding and editor.is_present()):
                    continue
                if not editor.is_present():
                    # `optval`: this field says "not set" with a value of its
                    # own, so clearing it is a write, not an omission.
                    unset = getattr(spec.get("field"), "unset_value", None)
                    if unset is not None:
                        out.append({
                            "code": spec["code"],
                            "name": spec["name"],
                            "old": spec.get("original"),
                            "new": unset,
                            "label": "(not set)",
                        })
                    # Otherwise left out of the message: on a whole-form write
                    # that is exactly how "no value" is said.
                    continue
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": editor.value(),
                    "label": editor.label(),
                })
                continue
            # A negatable value writes its value code plus the negate flag.
            if isinstance(editor, NegatableEditor):
                new = editor.value()
                if adding:
                    if not _has_value(new):
                        continue
                elif not editor.is_dirty():
                    continue
                out.append({
                    "code": spec["code"],
                    "name": spec["name"],
                    "old": spec.get("original"),
                    "new": new,
                    "extra": {editor.negate_code(): editor.is_negated()},
                    "label": editor.label(),
                })
                continue
            new = _editor_value(editor)
            if adding:
                if not _has_value(new):
                    continue
            elif new == spec.get("original"):
                continue
            if isinstance(editor, MultiEnumArrayEditor):
                label = editor.label()
            elif isinstance(editor, DurationEdit):
                label = editor.text()  # hh:mm:ss reads better than raw seconds
            elif isinstance(editor, QComboBox):
                label = editor.currentText()
            else:
                label = str(new)
            code = spec["code"]
            if isinstance(editor, QComboBox):
                # A union entry belongs to one member of the union; that is the
                # code it has to be written under.
                alt = editor.itemData(editor.currentIndex(), ALT_CODE_ROLE)
                if isinstance(alt, int):
                    code = alt
            entry = {
                "code": code,
                "name": spec["name"],
                "old": spec.get("original"),
                "new": new,
                "label": label,
            }
            # IP ▸ Addresses: "10.98.0.1/24" is an address and a mask, and
            # the mask is a field of its own. Sent whole, the address encoder
            # choked on "1/24" and the Add never left the queue.
            fld = spec.get("field")
            if (
                isinstance(new, str) and "/" in new and fld is not None
                and fld.jg_type == "network" and fld.maskid is not None
                and not fld.is_range
            ):
                addr, _sep, bits = new.partition("/")
                try:
                    prefix = int(bits)
                except ValueError:
                    prefix = -1
                if 0 <= prefix <= 32:
                    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
                    dotted = ".".join(str((mask >> s) & 0xFF) for s in (24, 16, 8, 0))
                    entry["new"] = addr.strip()
                    entry["extra"] = {
                        fld.maskid: (m.T_U32, sch.parse_ipv4(dotted))
                    }
            out.append(entry)
        return out

    # --- right-side action column (schema chrome + standard dialog buttons) -

    def _build_action_column(self) -> QWidget:
        """Winbox property form button stack (right side).

        Standard chrome: OK / Cancel / Apply, then template-driven Enable,
        Comment, Copy, Remove, then ``type:doit`` actions (Release, Renew, …).

        A `.jg` ``ro:1`` menu gets OK alone. Cancel and Apply are the two
        halves of an edit -- revert it, send it -- and there is no edit to be
        had on a form of readings: System ▸ Resources, About, License, Log.
        Winbox shows the same single button there.
        """
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(max(2, style.SPACING))
        #: Mnemonic letters this stack has handed out, so Alt+<letter> is never
        #: two buttons of the same form.
        self._mnemonics: set[str] = set()
        #: plain label -> the same label with its Alt letter marked. Buttons are
        #: built plain and relabelled when this form comes to the front (see
        #: _sync_mnemonics), which is also what keeps two open forms from both
        #: claiming Alt+O.
        self._mnemonic_texts: dict[str, str] = {}

        def add_btn(
            label: str,
            slot,
            *,
            write: bool = False,
            default: bool = False,
        ) -> QPushButton:
            label = tr(label)
            btn = QPushButton(label)
            self._mnemonic_texts[label] = _with_mnemonic(label, self._mnemonics)
            btn.setMinimumHeight(max(22, style.ROW_HEIGHT + 4))
            btn.clicked.connect(slot)
            if default:
                btn.setDefault(True)
                btn.setAutoDefault(True)
            lay.addWidget(btn)
            self._action_btns.append(btn)
            if write:
                self._write_action_btns.append(btn)
            return btn

        def add_sep() -> None:
            lay.addWidget(_hairline())

        # Standard dialog buttons (not in .jg — every property form, except
        # that a ro:1 menu keeps only the one that closes it). OK is not a
        # write button there either: it must stay live in a read-only session,
        # because closing the window is all it does.
        menu_ro = bool(getattr(self.menu, "read_only", False))
        add_btn("OK", self._action_ok, write=not menu_ro, default=True)
        if not menu_ro:
            add_btn("Cancel", self._action_cancel)
            add_btn("Apply", self._action_apply, write=True)
        add_sep()

        # Template chrome: type:enable / type:comment.
        if getattr(self.menu, "has_enable", False):
            self._enable_btn = add_btn("Disable", self._action_toggle_enable, write=True)
            # Its other spelling claims its letter now, before the template's
            # own verbs take the good ones: an interface that is already
            # disabled should still offer Alt+E, not whatever is left over.
            self._label_text("Enable")
        if getattr(self.menu, "has_comment", False):
            add_btn("Comment", self._action_comment, write=True)

        # Standard list-object tools, on the map rows whose list actually has
        # them -- Copy is an Add with the fields filled in, so it goes where
        # Add goes. IP ▸ Services keeps neither (eight fixed rows), Active
        # Users keeps Remove alone (drop a session, never create one). See
        # Menu.can_add / Menu.can_remove.
        if getattr(self.menu, "can_add", False):
            add_btn("Copy", self._action_copy, write=True)
        if getattr(self.menu, "can_remove", False):
            add_btn("Remove", self._action_remove, write=True)

        # type:doit — Release, Renew, Make Static, …
        actions = list(getattr(self.menu, "form_actions", None) or [])
        if actions:
            add_sep()
            for act in actions:
                if act.get("kind") != "doit":
                    continue
                name = str(act.get("name") or "Action")
                # Respect on: visibility when we can evaluate it.
                if act.get("on") and not self._condition_ok(act.get("on")):
                    continue
                cmd = act.get("cmd")
                btn = add_btn(
                    name,
                    lambda checked=False, a=act: self._action_doit(a),
                    write=True,
                )
                if isinstance(cmd, int) and cmd:
                    btn.setToolTip(f"{name} (cmd {cmd})")

        # The menus this form is the window for. Not template chrome: these are
        # its siblings in the .jg, and that is what makes this form the window
        # and them its buttons (see MainWindow._open_page_group).
        if self._menu_buttons and self._on_open_menu is not None:
            add_sep()
            for entry in self._menu_buttons:
                add_btn(
                    str(entry.get("title") or "Menu"),
                    lambda checked=False, e=entry: self._on_open_menu(dict(e)),
                )

        # type:toolbar — the verbs Winbox keeps on a Quick Set window: Port
        # Mapping, and a System group over Reboot / Reset Configuration /
        # Password / Check For Updates. Each opens another menu, carrying this
        # object's fields into it (see MainWindow.run_context_action).
        #
        # An entry with no `link:` is the same kind of button reached the other
        # way round: it carries nothing, so it is the form's own related list
        # rather than a row action -- IP ▸ Cloud ▸ Back To Home Users and Files,
        # LCD ▸ Screens, Detect Internet ▸ Interface Statuses.
        toolbar_acts = [
            a for a in (getattr(self.menu, "context_actions", None) or [])
            if a.get("name") and a.get("open")
            and (a.get("toolbar") or not a.get("link"))
            and self._condition_ok(a.get("on"))
        ]
        if toolbar_acts and self._on_context_action is not None:
            add_sep()
            groups: dict[str, list[dict]] = {}
            for act in toolbar_acts:
                groups.setdefault(str(act.get("group") or ""), []).append(act)
            for group, acts in groups.items():
                if group and len(acts) > 1:
                    btn = add_btn(group, lambda: None)
                    menu = QMenu(btn)
                    for act in acts:
                        entry = menu.addAction(str(act["name"]))
                        entry.triggered.connect(
                            lambda _=False, a=act: self._run_context_action(a)
                        )
                    btn.setMenu(menu)
                    continue
                for act in acts:
                    add_btn(
                        str(act["name"]),
                        lambda checked=False, a=act: self._run_context_action(a),
                    )

        if getattr(self.menu, "has_preset", False):
            add_btn("Preset", self._action_preset, write=True)

        # type:toggle — Winbox's Simple Mode / Advanced Mode button, below its
        # own separator (the template's `buttonsep`). The label is the mode it
        # switches *to*, which is what Winbox v3 shows.
        if isinstance(getattr(self.menu, "mode_toggle", None), dict):
            add_sep()
            self._mode_btn = add_btn(self._mode_btn_text(), self._action_toggle_mode)
            # Same as Enable/Disable: this button relabels itself with the mode
            # it would switch to, so both spellings are spoken for up front.
            self._advanced = not self._advanced
            self._label_text(self._mode_btn_text())
            self._advanced = not self._advanced

        lay.addStretch(1)
        self._size_action_column(col)
        return col

    def _size_action_column(self, col: QWidget) -> None:
        """Width the button stack to its longest label.

        A hardcoded 96px clipped the template's own actions — "Reset Traffic
        Counters" read as "et Traffic Coun". The floor, the pad and the cap are
        100% numbers and follow the zoom; the measured label width already does,
        because it comes off the scaled font.
        """
        fm = col.fontMetrics()
        widest = max(
            (fm.horizontalAdvance(plain_label(b.text())) for b in self._action_btns),
            default=style.scale_px(60),
        )
        width = max(
            style.scale_px(96),
            min(widest + style.scale_px(26), style.scale_px(220)),
        )
        self._action_col_width = width
        col.setFixedWidth(width)

    def _run_context_action(self, act: dict) -> None:
        """Hand a toolbar action to the window that can open menus."""
        if self._on_context_action is None:
            return
        self._on_context_action(act, self.row, self.menu)

    def _mode_btn_text(self) -> str:
        """Label for the mode button: the mode a click switches to."""
        toggle = getattr(self.menu, "mode_toggle", None) or {}
        modes = [str(m) for m in (toggle.get("modes") or [])]
        want = 0 if self._advanced else 1
        if len(modes) > want:
            return modes[want]
        return "Simple Mode" if self._advanced else "Advanced Mode"

    def _action_toggle_mode(self) -> None:
        """Switch the form between Simple and Advanced Mode.

        Nothing is rebuilt. Every ``on:'adv'`` row is a hidedynamicly one, so
        it was built and hidden already, and the tabs the mode opens (Data
        Rates, Advanced, Tx Power) are built and removed the same way -- the
        button only changes what the ``umode`` pseudo-slot answers and asks
        both passes to run again.
        """
        self._advanced = not self._advanced
        save_advanced_mode(self._advanced)
        # These memoise per condition name against the old mode.
        self._cond_ok_cache.clear()
        self._tab_ok_cache.clear()
        if self._mode_btn is not None:
            self._mode_btn.setText(self._label_text(self._mode_btn_text()))
            self._size_action_column(self._mode_btn.parentWidget())
        self._refresh_conditional_tabs()
        self._refresh_conditional_rows()
        applog.note(
            f"{self._base_title}: "
            f"{'Advanced' if self._advanced else 'Simple'} Mode"
        )

    def _require_write(self, name: str) -> bool:
        if self.edit_session.readonly:
            QMessageBox.information(
                self,
                name,
                "Read-only mode is on.\n\n"
                "Pick Queue in the toolbar's Mode box to collect changes "
                "locally, or Live to write them to the router.",
            )
            return False
        return True

    def _is_disabled(self) -> bool:
        vals = self._row_values()
        dis = vals.get(0xFE000A)
        if dis is None and hasattr(self.row, "raw_value"):
            dis = self.row.raw_value(0xFE000A)
        if isinstance(dis, bool):
            return dis
        if isinstance(dis, int):
            return bool(dis)
        return False

    def _refresh_enable_button(self) -> None:
        if self._enable_btn is None:
            return
        want = "Enable" if self._is_disabled() else "Disable"
        try:
            # Through _label_text, or the button would lose its Alt letter the
            # first time the row's state changed.
            self._enable_btn.setText(self._label_text(want))
        except RuntimeError:
            pass

    def _enqueue_action(
        self,
        action: str,
        values: dict[str, Any] | None = None,
        edits: list[dict[str, Any]] | None = None,
        *,
        title: str | None = None,
        close_after: bool = False,
        notify: bool = True,
    ) -> None:
        change = QueuedChange(
            path=self.path,
            title=title or f"{self._base_title} — {action}",
            values=values or {},
            row_key=self.row_key,
            obj_id=self._object_id(),
            edits=edits or [],
            action=action,
        )
        if not self._commit_or_queue(change, notify=notify):
            return
        if close_after:
            self._allow_close = True
            self.close()

    def _note(self, text: str) -> None:
        """Report a completed write on the main window's status bar.

        Deliberately not a modal: a live edit that worked should not need to be
        dismissed, unlike a queue entry the user has to remember to apply.
        """
        bar = getattr(self.window(), "statusBar", None)
        if callable(bar):
            try:
                bar().showMessage(text, 6000)
                return
            except Exception:  # noqa: BLE001
                pass
        self._update_form_footer()

    def _write_action(self, obj_id: int | None) -> str:
        """``set`` or ``add`` for what this form is looking at.

        "No object id" does not mean "new object": a settings menu — System ▸
        Identity, IP ▸ Cloud — is one object that never had an id. Treating
        those as an Add sent ADDOBJ to (24,1), which the device answered with
        NOTIMP. Only a collection can be added to.
        """
        if obj_id is not None:
            return "set"
        return "add" if getattr(self.menu, "is_collection", False) else "set_settings"

    def _resync_after_write(self, obj_id: int | None) -> None:
        """Re-read the object, show what came back, and call it clean.

        Two things used to be wrong here. Every editor's baseline still held
        the *pre-edit* value, so the fields stayed highlighted as modified
        after Apply and closing warned about changes that had already been
        sent. And the form went on showing what was typed rather than what the
        device made of it: The router normalises what it is sent -- a bridge VLAN
        given ``100-103,105`` plus a new ``99`` answers ``99-103,105`` -- and
        Winbox v3 restates the field from the reply. Ours only did on reopen.
        """
        if self.client is not None:
            try:
                if obj_id is not None:
                    row = self.client.getobj(
                        self.path, obj_id, filter_mask=self._open_read_mask(),
                        menu=self.menu,
                    )
                else:
                    # A settings write (0xFE000E) is one-way: the device
                    # acknowledges nothing and reports the new value on the
                    # next GET. Reading it back is the only confirmation there
                    # is, so do it rather than assume the write landed.
                    row = self.client.get(self.path, menu=self.menu)
            except Exception:  # noqa: BLE001
                row = None
            if row is not None:
                self.row = row
                self._forget_row_caches()
                self._restate_from_row()
        # After the restate, so an editor that could not be restated keeps what
        # it holds and still stops reading as modified.
        self._reset_baselines()

    def _restate_from_row(self) -> None:
        """Re-render every editor from the row in hand.

        What the device answered replaces what was typed, field by field, in
        the widgets already on screen -- rebuilding the form would lose the
        tab, the scroll position and the keyboard. An editor with no way to
        take a value back (a status label, a control this does not know) is
        left alone rather than blanked.

        Tabs not yet built are not walked: they are built from ``self.row``,
        which is the row this just read, so they open on the new values anyway.
        """
        index = self._row_field_index()
        values = getattr(self.row, "values_by_code", None) or {}
        for spec, editor in self._rows:
            code = spec.get("code")
            if not isinstance(code, int) or spec.get("kind") != "field":
                continue
            fld = spec.get("field")
            entries = index.get(code) or ()
            name = getattr(fld, "name", None)
            # One wire code can decode as two named fields (SFP's u473 link
            # length beside Overall Stats' Q473 counter). Match the name the
            # row was built for, so a value does not land in the other's box.
            entry = next(
                (e for e in entries if e[1] == name),
                entries[0] if entries else None,
            )
            raw = self.row.raw_value(code) if hasattr(self.row, "raw_value") else None
            rendered = entry[2] if entry is not None else ""
            if rendered == "" and getattr(fld, "ro", False):
                rendered = "—"
            if _restate_editor(editor, raw, rendered, values):
                # The spec is read again for presence and default decisions
                # (hideonempty rows, an optional field's "+"), so it has to
                # carry the new value too.
                spec["raw"] = raw
                spec["rendered"] = rendered

    def _rebase_editor(self, editor: QWidget) -> None:
        """Take what one editor holds now as its before-image.

        For a list that reordered itself for reading: :meth:`edits` compares
        against the spec's ``original``, so without this a sorted-but-unedited
        field would be queued as a change.
        """
        for spec, held in self._rows:
            if held is editor:
                spec["original"] = _editor_value(editor)
                break
        self._refresh_dirty_highlights()

    def _reset_baselines(self) -> None:
        """Treat what the editors hold now as the values to compare against."""
        for spec, editor in self._rows:
            if _owns_baseline(editor):
                editor.reset_baseline()
            else:
                spec["original"] = _editor_value(editor)
        self._refresh_dirty_highlights()

    def owns_queued(self, change: QueuedChange) -> bool:
        """Whether this form is the one that queued `change`.

        Identity, not path plus row_key: two New forms on the same menu are
        indistinguishable by either, and the wrong one adopting the id of a
        freshly added object is how the *other* one goes on to add a second.
        """
        return any(c is change for c in self._queued_changes)

    def sync_after_external_write(self, change: QueuedChange) -> None:
        """A change this form queued has now been sent from elsewhere.

        Apply all writes from the main window's toolbar, so the form that
        built the change never sees it land: its editors stay highlighted as
        modified, and a New form still has no object id -- so its next save
        would add a second object.
        """
        self._queued_changes = [c for c in self._queued_changes if c is not change]
        if change.action == "add" and change.obj_id is not None:
            self._adopt_new_object(change.obj_id)
        else:
            self._resync_after_write(change.obj_id)

    def _adopt_new_object(self, obj_id: int) -> None:
        """After an Add lands, edit that object instead of adding another.

        A New form has no object id, so every save is an ADDOBJ. Without
        adopting the id the router just created, pressing Apply twice created
        two rules.
        """
        self._added_obj_id = obj_id
        self._resync_after_write(obj_id)

    def _object_id(self) -> int | None:
        """The router's `.id` for the row this form shows, when it has one."""
        if getattr(self, "_added_obj_id", None) is not None:
            return self._added_obj_id
        getter = getattr(self.row, "object_id", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None

    def _commit_or_queue(self, change: QueuedChange, *, notify: bool = True) -> bool:
        """Send the change in Live mode, hold it locally otherwise.

        Returns False when a live write was attempted and refused, so the form
        stays open on the values the user still has in hand rather than closing
        as if the change had landed.
        """
        if self.edit_session.live:
            try:
                summary = self.edit_session.apply(change)
            except Exception as e:  # noqa: BLE001
                # The device's own wording: The router answers every business-rule
                # refusal with FAILED and puts the reason only in the string.
                QMessageBox.warning(
                    self, "Not written", f"The router refused the change:\n\n{e}"
                )
                return False
            # An Add that landed turns this into an edit of the new object;
            # any other write just means the form is now in step with the
            # device and should stop showing itself as modified.
            if change.action == "add" and change.obj_id is not None:
                self._adopt_new_object(change.obj_id)
            else:
                self._resync_after_write(change.obj_id)
            self.queued.emit(change)
            if notify:
                self._note(summary)
            return True
        self.edit_session.enqueue(change)
        self._queued_changes.append(change)
        self.queued.emit(change)
        if notify:
            n = len(self.edit_session.queue)
            QMessageBox.information(
                self,
                "Queued",
                f"Queued “{change.action}” locally — nothing was sent to the "
                f"router.\n\nQueue now holds {n} object(s).",
            )
        return True

    def _action_ok(self) -> None:
        """OK = Apply field edits (if any) and close — Winbox behaviour.

        On a ``ro:1`` menu it is only ever the close: the form has no Apply
        beside it and no unlocked editor to have changed.
        """
        if self.edit_session.readonly or getattr(self, "_menu_ro", False):
            self._allow_close = True
            self.close()
            return
        edits = self.edits()
        if edits:
            self._queue_changes(prompt_if_empty=False, close_after=True, notify=True)
        else:
            self._allow_close = True
            self.close()

    def _action_cancel(self) -> None:
        self.close()

    def _action_apply(self) -> None:
        if not self._require_write("Apply"):
            return
        self._queue_changes(prompt_if_empty=True, close_after=False, notify=True)
        # Stay open: treat current editor values as the new baseline. Through
        # _reset_baselines, so an editor holding its own before-image (an array,
        # a range, an optional match) stops showing as edited too.
        self._reset_baselines()

    def _action_toggle_enable(self) -> None:
        if not self._require_write("Enable/Disable"):
            return
        disabled = not self._is_disabled()
        action = "disable" if disabled else "enable"
        # Reflect locally so footer / button label update immediately.
        by = getattr(self.row, "values_by_code", None)
        if isinstance(by, dict):
            by[0xFE000A] = disabled
        self._enqueue_action(
            action,
            {"Disabled": disabled},
            edits=[{
                "code": 0xFE000A,
                "name": "Disabled",
                "old": not disabled,
                "new": disabled,
                "label": "true" if disabled else "false",
            }],
            notify=True,
        )
        self._refresh_enable_button()
        self._update_form_footer()

    def _action_comment(self) -> None:
        if not self._require_write("Comment"):
            return
        # RowObject.comment() checks the decoded fields first, then STD_DESCR
        # (0xfe0009). This used to read 0xfe0005, which is ERROR_MISSING in the
        # error namespace and is declared by no menu, so it never found anything.
        current = self.row.comment() if hasattr(self.row, "comment") else ""
        text, ok = QInputDialog.getText(self, "Comment", "Comment:", text=current)
        if not ok:
            return
        self._enqueue_action(
            "comment",
            {"Comment": text},
            edits=[{
                "code": 0xFE0009,  # STD_DESCR — the code a live write needs
                "name": "Comment",
                "old": current,
                "new": text,
                "label": text,
            }],
        )

    def _action_copy(self) -> None:
        if not self._require_write("Copy"):
            return
        if self.edit_session.live:
            # Winbox's Copy is an ADD carrying the source row's whole field set.
            # Half of those fields are device-reported status the form never
            # made editable, so sending them back is guesswork -- it stays a
            # queue entry until the shape has been captured off real Winbox.
            QMessageBox.information(
                self,
                "Copy",
                "Copy is not implemented as a live write yet: it needs the full "
                "field set of a new object, and this form only knows the fields "
                "it was given.\n\nSwitch to Queue mode to record the intent, or "
                "use Add and fill the form.",
            )
            return
        self._enqueue_action(
            "copy",
            {"_copy": True, "_from": self.row_key},
            title=f"{self._base_title} — copy",
        )

    def _action_remove(self) -> None:
        if not self._require_write("Remove"):
            return
        ans = QMessageBox.question(
            self,
            "Remove",
            f"Queue removal of this object?\n\n{self._base_title}\n\n"
            "Nothing is sent to the router until you apply the queue.",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._enqueue_action(
            "remove",
            {"_removed": True},
            title=f"{self._base_title} — remove",
            close_after=True,
        )

    def _action_doit(self, act: dict[str, Any]) -> None:
        """Run one of the form's own commands (Force Update, Reset Counters…).

        A doit is a verb, not a field. Staging it as a pretend edit produced an
        entry with no wire code, which the send path then refused outright:
        "no wire code for: Force Update".
        """
        name = str(act.get("name") or "Action")
        if not self._require_write(name):
            return
        cmd = act.get("cmd")
        if cmd is None:
            QMessageBox.warning(self, name, "This action declares no command.")
            return
        live = self.edit_session.live
        ans = QMessageBox.question(
            self,
            name,
            (f"Run “{name}” on the router now?" if live
             else f"Queue “{name}” for this object?")
            + f"\n\nCommand: {cmd}"
            + ("" if live else
               "\nNothing is sent to the router until you apply the queue."),
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._enqueue_action(
            "doit",
            {"_doit": name, "_cmd": cmd},
            title=f"{self._base_title} — {name}",
        )

    def _action_preset(self) -> None:
        if not self._require_write("Preset"):
            return
        QMessageBox.information(
            self,
            "Preset",
            "Preset is declared in the schema for this menu but is not "
            "implemented yet.",
        )

    def _queue_changes(
        self,
        *,
        prompt_if_empty: bool = True,
        close_after: bool = True,
        notify: bool = True,
    ) -> None:
        if self.edit_session.readonly:
            return
        # A half-typed address would otherwise be dropped silently and the rest
        # of the rule written without it.
        problems = self.opt_errors()
        if problems:
            QMessageBox.warning(self, "Check the matches", "\n".join(problems))
            return
        missing = self.required_errors()
        if missing:
            QMessageBox.warning(
                self,
                "Required fields",
                "Fill in before saving:\n  " + "\n  ".join(missing),
            )
            return
        edits = self.edits()
        if not edits:
            if prompt_if_empty:
                QMessageBox.information(self, "No changes", "Nothing was modified.")
            return
        # No object id means this is the Add form: there is nothing on the
        # device to SET, so the same edits become the new object's field set.
        obj_id = self._object_id()
        action = self._write_action(obj_id)
        extra_wire: dict[int, Any] = {}
        if action == "add":
            # The new row's prefilled values -- the interface type (0x10001, no
            # editor produces it, else the device makes a bare interface), the
            # suggested name, and the schema defaults (VLAN ID 1, MTU 1500) --
            # ride into the ADD so accepting them without retyping still sends
            # them. An edit of the same field overrides these (setdefault).
            for code, val in self.row.values_by_code.items():
                extra_wire[code] = val
        change = QueuedChange(
            path=self.path,
            title=self._base_title,
            values={e["name"]: e["new"] for e in edits},
            row_key=self.row_key,
            obj_id=obj_id,
            edits=edits,
            action=action,
            extra_wire=extra_wire,
            # This form, so its own re-save replaces this entry rather than
            # another form's (see EditSession.enqueue).
            origin=f"form-{id(self)}",
        )
        if self.edit_session.live:
            if not self._commit_or_queue(change, notify=notify):
                return
        else:
            self.edit_session.enqueue(change)
            self._queued_changes.append(change)
            self.queued.emit(change)
            if notify:
                # A note, not a dialog: a box to dismiss after every OK made
                # queueing ten changes ten interruptions. The list's tinted row
                # and the toolbar's count say the same, and stay.
                bar = getattr(self.window(), "statusBar", None)
                if callable(bar):
                    bar().showMessage(
                        f"Queued {len(edits)} change(s) to {self._base_title} — "
                        f"{len(self.edit_session.queue)} waiting; nothing sent",
                        6000,
                    )
        if close_after:
            self._allow_close = True
            self.close()


def _collapse_separators(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop leading/trailing/double separators so empty Status isn't a stack of lines."""
    by_tab: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for sp in specs:
        by_tab.setdefault(str(sp.get("tab") or "General"), []).append(sp)
    out: list[dict[str, Any]] = []
    for tab, items in by_tab.items():
        cleaned: list[dict[str, Any]] = []
        for sp in items:
            if sp.get("kind") == "separator":
                if not cleaned or cleaned[-1].get("kind") == "separator":
                    continue
                cleaned.append(sp)
            else:
                cleaned.append(sp)
        while cleaned and cleaned[-1].get("kind") == "separator":
            cleaned.pop()
        out.extend(cleaned)
    return out


# A few fields carry the raw machine id as their .jg name while Winbox shows a
# proper caption. Label only — the schema name still drives wire lookups.
_WINBOX_LABELS: dict[int, str] = {
    0x3F3: "Auto Negotiation",  # autoneg
    0x407: "Tx Flow Control",   # flowcontrol (its pair, 0x408, is named already)
}

# nonpublic + lowercase-named, yet Winbox does put these on the form, so they
# must survive the machine-id filter below.
_FORCE_VISIBLE_CODES = frozenset({0x407})


def _set_live_text(widget: QWidget, text: str) -> None:
    """Repaint a polled value without throwing away what is selected in it.

    A counter refreshes once a second, and a double-click on one is someone
    about to copy it -- but ``setText`` drops the selection, so the copy never
    happens. An unchanged value is not rewritten at all, and a value the user
    is holding selected is left alone until they let go of it.
    """
    held = getattr(widget, "set_held", None)
    try:
        if widget.text() == text:
            if held is not None:
                held(False)     # what is on screen is what the device says
            return
    except (AttributeError, RuntimeError):
        return
    try:
        # Qt drops a line edit's selection when it loses focus, so a live
        # selection means the pointer is still on this value.
        selected = widget.selectedText()
        if selected and selected != widget.text():
            # Part of the value is selected -- someone is dragging across
            # digits. Re-applying that character range to a number that has
            # changed underneath would hand them digits they never picked
            # ("999" selected, "1000" arrives, they copy "100"), so the value
            # is held as it is until they let go, and says so.
            if held is not None:
                held(True, text)
            return
        if held is not None:
            held(False)
        pos = widget.cursorPosition()
        widget.setText(text)
        if selected:
            # The whole value was selected -- someone is about to copy it, and
            # what belongs in their clipboard is the current number.
            widget.selectAll()
        else:
            widget.setCursorPosition(min(pos, len(text)))
    except (AttributeError, RuntimeError):
        try:
            widget.setText(text)
        except RuntimeError:
            pass


def _row_label(text: str, *, colon: bool = True) -> QLabel:
    """A form-row label, with its text painted exactly as it reads.

    "Tx&Rx 64" is a counter's name, not a mnemonic. ``addRow(str, widget)``
    makes the label a buddy of the field, which turns & into a shortcut and
    paints "Tx<u>R</u>x 64"; a label handed over as a widget has no buddy, so
    the ampersand stays an ampersand and must not be doubled either.

    Winbox ends every labelled row with a colon -- ``Name:``, ``MAC Address:``,
    ``Supported Rates A/G:`` -- and so does the rest of this app (the connect
    and command dialogs always did). A check box is the exception: Winbox gives
    a bool no label at all and writes the name beside the box instead, so a
    colon there would be punctuating a caption.
    """
    text = text or ""
    if colon and text and not text.endswith(":"):
        text = f"{text}:"
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return label


def _winbox_label(code: Any, fallback: str) -> str:
    if isinstance(code, int) and code in _WINBOX_LABELS:
        return _WINBOX_LABELS[code]
    return fallback


def _is_internal_field(fld: Field | None, menu: "Menu | None" = None) -> bool:
    """Winbox-hidden bookkeeping fields (Interface type/caps/link, object ids…).

    Schema marks these ``nonpublic`` with a lowercase machine name. Real Winbox
    never shows them on property forms — they drive icons/filters only.
    Title-cased fields can also be nonpublic (e.g. MTU) but are still UI-facing.
    """
    if fld is None:
        return False
    if not fld.nonpublic:
        return False
    if getattr(fld, "code", None) in _FORCE_VISIBLE_CODES:
        return False
    name = fld.name or ""
    # The tree link. ``treeon:'Parent'`` names the field holding each row's
    # parent, which is why the list can nest at all -- and every menu that
    # declares one declares that field nonpublic: a bridge port's Parent, a
    # file's container, a package's bundle. Winbox nests the list on it and
    # never gives it a row; ours put "Parent: 0" at the top of every new
    # bridge port. The nonpublic gate above is what keeps the *public* parent
    # dropdowns (Simple Queue ▸ Parent) on their forms.
    tree_on = getattr(menu, "tree_on", None)
    if tree_on and name == tree_on:
        return True
    # machine ids: type, caps, link, id, status, role, …
    if bool(name) and name == name.lower() and " " not in name:
        return True
    # Structural bookkeeping that keeps a Title-cased name: a nonpublic number
    # defaulting to the 0xFFFFFFFF "unset" sentinel (address-list Parent, the
    # tree link WinBox never shows). MTU and other real numbers keep their own
    # default, so this only catches the bookkeeping ones.
    if fld.default == 0xFFFFFFFF and (fld.jg_type or "") in (
        "number", "enm", "bignumber",
    ):
        return True
    return False


def _is_multi_enum(fld: Field | None) -> bool:
    """True when the field holds several enum values (Advertise speeds, a
    bridge VLAN's Tagged ports, …).

    The vocabulary may be declared inline (``fld.enum``) or resolved from
    another menu (``enum_path`` -- the interface table, the option list, the
    switch's ports). Requiring the inline one put a *single-select* dropdown on
    every "list of interfaces" field in the schema: Bridge ▸ VLANs' Tagged,
    Untagged and MVRP Forbidden, Switch ▸ VLAN's Ports, a VRF's Interfaces --
    82 fields where Winbox shows one dropdown per entry.
    """
    if fld is None:
        return False
    if not (fld.enum or fld.enum_path or getattr(fld, "enum_query", None)):
        return False
    if (fld.jg_type or "").startswith("multi"):
        return True
    try:
        return bool(fld.is_array)
    except Exception:
        return False


def _is_reported_list(fld: Field | None, raw: Any) -> bool:
    """True when this read-only row is a list, and so is drawn one per line.

    Winbox never squeezes a reported array into a comma run on one line: a
    bridge VLAN's Current Tagged / Current Untagged, a bridge port's Declared
    and Registered VLAN IDs, Ethernet's Advertising, System Health's voltages
    all get a box with an entry per line. Whether the values have names is
    :func:`_is_multi_enum`'s question, and decides how each line reads rather
    than whether there are lines.

    Two things have to hold: the field is declared an array, *and* the value in
    hand is one. Twenty-odd switch-port counters (`Tx&Rx 64`, `Drop Byte`) are
    declared ``multibignumber`` and carry a single number, which Winbox shows
    in a plain box; going by the declaration alone turned every one of them
    into a one-line list box that the live poll then could not refresh.
    """
    if fld is None or not isinstance(raw, (list, tuple)):
        return False
    jg = fld.jg_type or ""
    # `set` / `multibits` are a bitmask in one number -- Field.render already
    # spells the members out, and there is one value, not several.
    if jg in ("set", "multibits"):
        return False
    # A `numberrangelist` is the whole list in one box by declaration (QoS
    # PCP/DSCP, PCI address windows); only `multinumberrange` is per-entry.
    if jg == "numberrangelist":
        return False
    if jg.startswith("multi"):
        return True
    try:
        return bool(fld.is_array)
    except Exception:
        return False


def _wire_list(raw: Any) -> list[Any]:
    """Normalize multi / scalar wire values to an ordered list of enum keys."""
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[Any] = []
        for v in raw:
            if isinstance(v, (bytes, bytearray)) and len(v) <= 8:
                out.append(int.from_bytes(v, "little"))
            else:
                out.append(v)
        return out
    if isinstance(raw, (bytes, bytearray)) and len(raw) <= 8:
        return [int.from_bytes(raw, "little")]
    if isinstance(raw, bool):
        return [raw]
    if isinstance(raw, int):
        return [raw]
    return []


class ReadOnlyValueList(QPlainTextEdit):
    """Read-only multi-line box: one value per line.

    Winbox shows a *reported* array this way rather than as a comma run on one
    line -- a bridge VLAN's Current Tagged / Current Untagged, a bridge port's
    Declared and Registered VLAN IDs, Ethernet's Advertising, System Health's
    voltages. :func:`_is_reported_list` is what decides a row is one of these.

    ``lookup`` is the vocabulary a dynamic enum resolves against (interface id
    -> name); without it a field whose names come from another menu falls back
    to splitting the text the row was rendered with, which already carries the
    names, rather than showing bare ids.
    """

    def __init__(self, raw: Any, rendered: Any, fld: Field | None,
                 parent=None, *, lookup: dict | None = None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._lookup = lookup
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Same muted fill as ReadOnlyValueEdit so the column reads as one set.
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {style.BG_ALT}; color: {style.TEXT}; }}"
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        #: True once the reader asked for alphabetical, so a refresh keeps it.
        self._sorted = False
        # QPlainTextEdit's own hint is 256x192. Left alone, QFormLayout treats
        # that as the row's desired height and hands the surplus to every other
        # row in the form, which stretched all the scalar Status rows to ~57px.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._load(self._split(raw, rendered, fld, self._lookup))

    def _load(self, items: list[str]) -> None:
        """Take a freshly read list as the device's own order."""
        #: The order the device reported, to go back to after a sort.
        self._as_reported = list(items)
        self.set_items(sorted(items, key=sort_key) if self._sorted else items)

    def sizeHint(self):  # noqa: N802
        return QSize(220, self._wanted_h)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(80, self._wanted_h)

    def set_items(self, items: list[str]) -> None:
        self._items = list(items)
        self.setPlainText("\n".join(self._items))
        self._fit()

    def _fit(self) -> None:
        """Size the box to the list, up to six lines, then scroll."""
        rows = max(1, min(len(self._items) or 1, 6))
        line = self.fontMetrics().lineSpacing()
        self._wanted_h = rows * line + 10
        self.setFixedHeight(self._wanted_h)
        # Only scroll when the list is genuinely longer than the box.
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if len(self._items) > rows
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.updateGeometry()

    def restate(self, raw: Any, rendered: str = "", values=None) -> None:
        """Re-render from a value just read off the device.

        Current Tagged is what the bridge *did* with the ports Tagged asked
        for, so it moves on Apply and on every refresh -- a box still showing
        the ports of a minute ago is the one thing this row must not do. An
        order the reader chose survives the new reading; the device's own does
        not, because it is the device's to state.
        """
        self._load(self._split(raw, rendered, self._fld, self._lookup))

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        """The reading menu, plus the same ordering this box's editable twin
        has: Current Tagged is read beside Tagged and sorts on its own ask."""
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        act_sort = menu.addAction("Sort By &Name")
        act_sort.setEnabled(len(self._items) > 1)
        act_reported = menu.addAction("&Device Order")
        act_reported.setEnabled(self._items != self._as_reported)
        chosen = menu.exec(ev.globalPos())
        if chosen is act_sort:
            self._sorted = True
            self.set_items(sorted(self._items, key=sort_key))
        elif chosen is act_reported:
            self._sorted = False
            self.set_items(self._as_reported)
        ev.accept()

    @staticmethod
    def _split(raw: Any, rendered: Any, fld: Field | None,
               lookup: dict | None = None) -> list[str]:
        """One label per wire element; fall back to splitting the rendered text."""
        items = ReadOnlyValueList._elements(raw, fld, lookup)
        if items is not None:
            return items
        text = "" if rendered is None else str(rendered).strip()
        if not text or text == "—":
            return []
        return [p.strip() for p in text.split(",") if p.strip()]

    @staticmethod
    def _elements(raw: Any, fld: Field | None,
                  lookup: dict | None) -> list[str] | None:
        """One label per element of the wire array, or None to fall back.

        Rendering the array element by element is what tells `100-101` from
        `100, 101` and a MAC from a blob of hex -- the rendered text has
        already thrown that away. It needs the field's own vocabulary though,
        so a dynamic enum with no ``lookup`` in hand hands back None rather
        than a column of bare object ids.
        """
        if fld is None or not isinstance(raw, (list, tuple)):
            return None
        if lookup is None and (sch_implied_enum_path(fld)
                               or sch_implied_enum_path(fld.element_view())):
            return None
        # A range list is a flat run of (low, high) pairs: one line per range,
        # the way a bridge port's Registered VLAN IDs read (`100-101`, `105`),
        # not one line per number. `id2` splits the ends over two codes, which
        # only the row-level render can reach -- leave that to the text.
        if fld.jg_type in sch.RANGE_LIST_TYPES and fld.highid is None:
            pairs = sch.range_list_pairs(list(raw))
            if pairs is not None:
                return [sch.format_range(low, high) for low, high in pairs]
        elem = fld.element_view()
        out = []
        for item in raw:
            try:
                out.append(elem.render(item, lookup=lookup))
            except Exception:  # noqa: BLE001 - a value we cannot name is not a crash
                out.append(str(item))
        return [t for t in out if t != ""]

    def values(self) -> list[str]:
        return list(self._items)


class DurationEdit(QLineEdit):
    """Winbox ``type:interval`` editor: shows ``00:05:00``, stores wire units.

    Type ``300`` (or ``5m``) and it normalises to ``00:05:00`` on commit, while
    :func:`value` returns the number that goes on the wire — so a queued edit
    carries 300, not the display text.

    ``scale`` is the schema's units-per-second (a few intervals are stored in
    centiseconds or milliseconds). Display always divides by it and
    :func:`value` multiplies back, so a scaled field is not off by 100x.
    """

    def __init__(
        self, wire_value: int | None = None, *, scale: int | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self._scale = int(scale) if scale else 1
        self.setPlaceholderText("hh:mm:ss")
        self.set_wire_value(wire_value)
        self.editingFinished.connect(self._normalize)

    def set_wire_value(self, wire_value: int | None) -> None:
        from x3.proto.schema import format_interval

        if wire_value is None:
            self.setText("")
            return
        self.setText(format_interval(wire_value / self._scale))

    def value(self) -> int | None:
        """Wire value for the current text, or None when it is not a duration."""
        from x3.proto.schema import parse_interval

        secs = parse_interval(self.text())
        if secs is None:
            return None
        return int(round(secs * self._scale))

    def _normalize(self) -> None:
        """Rewrite whatever was typed as hh:mm:ss, leaving junk untouched."""
        from x3.proto.schema import format_interval, parse_interval

        secs = parse_interval(self.text())
        if secs is None:
            return
        text = format_interval(secs)
        if text != self.text():
            self.setText(text)


class _RowGrip(QWidget):
    """The handle a row is dragged by.

    The row's body is a dropdown and pressing a dropdown opens it, so
    reordering needs somewhere else to press. Two columns of dots, the width of
    the spinner on the other side, so a row reads as something with a hold on
    each end.
    """

    _W = 9

    def __init__(self, owner, row, parent=None) -> None:
        super().__init__(parent)
        self._owner = owner
        self._row = row
        self._holding = False
        self.setFixedWidth(self._W)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move this entry (or Alt+↑ / Alt+↓ on the box)")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(style.TEXT_DIM))
        dots = max(2, min(4, (self.height() - 3) // 4))
        left = self.width() // 2 - 2
        top = (self.height() - (dots * 4 - 2)) // 2
        for i in range(dots):
            for dx in (0, 3):
                painter.drawRect(left + dx, top + i * 4, 2, 2)
        painter.end()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() is not Qt.MouseButton.LeftButton:
            return
        self._holding = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self._owner._begin_drag()
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if not self._holding:
            return
        host = self._owner._rows_host
        where = host.mapFromGlobal(ev.globalPosition().toPoint())
        self._owner._drag_to(self._row, where.y())
        ev.accept()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if not self._holding:
            return
        self._holding = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._owner._end_drag()
        ev.accept()


#: ``1G baseT full`` / ``100M baseT half`` -- a rate class in a label.
_RATE_G = re.compile(r"(\d+(?:\.\d+)?)\s*G", re.I)
_RATE_M = re.compile(r"(\d+(?:\.\d+)?)\s*M", re.I)


def _rate_score(label: str) -> float | None:
    """Mbps the label names -- ``1G baseT full`` -> 1000.0 -- or None.

    None for anything that is not a rate, which is what tells a rate ladder
    (Ethernet Advertise, SFP Supported) from a vocabulary of *names* (an
    interface table, an option list). The two are ordered by different
    things; see :meth:`MultiEnumArrayEditor._choice_score`.
    """
    m = _RATE_G.search(label or "")
    if m:
        return float(m.group(1)) * 1000.0
    m = _RATE_M.search(label or "")
    if m:
        return float(m.group(1))
    return None


class MultiEnumArrayEditor(QWidget):
    """Winbox-style multi/array enum editor (Ethernet Advertise, a bridge
    VLAN's Tagged ports, …).

    Always builds dropdown rows (so RO ↔ edit only toggles interactivity).
    ▲ removes the row; ▼ inserts below and picks the value the vocabulary
    suggests: a rate ladder from the gap between neighbours (10M … 1G →
    100M), a list of names from the box's own order (under ether1, ether2).
    New rows flash briefly.

    A dropdown offers what is still free: an entry already in the array is not
    offered a second time, the way Winbox does it. Dirtiness is per row -- only
    the rows that actually differ from what the device sent are marked, and a
    removal shows as a mark in the gap it left.
    """

    changed = Signal()
    #: The rows were reordered, not edited: the form's own copy of the
    #: before-image has to move with ours or it would queue the new order.
    rebased = Signal()

    _SPIN_W = 14
    _FLASH_STEPS = 8
    _FLASH_MS = 280

    def __init__(
        self,
        choices: list[tuple[Any, str]],
        raw: Any,
        rendered: Any = "",
        *,
        readonly: bool = False,
        max_rows: int = 200,
        name: str = "",
        marks: dict | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._choices = list(choices)
        #: Status and comment per entry, keyed by wire value. Applied on every
        #: refill: a row's box is rebuilt whenever a sibling takes a value out
        #: of circulation, and a dot painted once would go with it.
        self._marks = dict(marks or {})
        #: Where to re-read those from when a row's dropdown opens; set by the
        #: form after construction (see :meth:`set_mark_source`).
        self._mark_client = None
        self._mark_path: tuple[int, ...] | None = None
        self._mark_provider = None
        #: The field's own label, for the multi-pick's title bar.
        self._name = name
        #: Sibling lists on the same object whose entries this one may not
        #: repeat -- Tagged and Untagged (see :meth:`add_peer`).
        self._peers: list[MultiEnumArrayEditor] = []
        #: Whether every entry's label names a rate (Advertise: 10M … 10G).
        #: A ladder has arithmetic in it and a list of interface names does
        #: not, which is the whole difference between the two guesses an
        #: inserted row can be given -- see :meth:`_choice_score`.
        self._ladder = bool(self._choices) and all(
            _rate_score(lab) is not None for _w, lab in self._choices
        )
        self._max_rows = max(1, max_rows)
        self._readonly = readonly
        # Track theme density at build time (ROW_HEIGHT is theme-published).
        self._ROW_H = max(15, style.ROW_HEIGHT + 1)
        # (combo, spinner_host, row_widget)
        self._rows: list[tuple[QComboBox, QWidget, QWidget]] = []
        # Set for real once the rows are built; here so a change that somehow
        # fires while they are being built has something to compare against.
        self._original: list[Any] = []
        #: Row order and dirtiness when a drag started, so a drag that ends
        #: where it began changes nothing.
        self._drag_from: list[Any] | None = None
        self._drag_dirty = False
        #: Whether the rows are showing sorted rather than as reported. The
        #: before-image is read in the same order, so an edit made in a sorted
        #: view is marked where the user sees it.
        self._view_sorted = False
        self._flash_timers: list[QTimer] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(1)

        self._rows_host = MarkedRows(lambda: [r for _c, _b, r in self._rows])
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(1)
        root.addWidget(self._rows_host)

        self._empty_host = QWidget()
        empty_lay = QHBoxLayout(self._empty_host)
        empty_lay.setContentsMargins(0, 0, 0, 0)
        empty_lay.setSpacing(2)
        add_first = self._make_spin_btn("▾", "Add entry", self._ROW_H)
        add_first.clicked.connect(lambda: self._on_add_first())
        empty_lay.addWidget(add_first)
        self._add_first = add_first
        hint = QLabel("Add…")
        hint.setStyleSheet(f"color: {style.TEXT_DIM};")
        empty_lay.addWidget(hint)
        empty_lay.addStretch(1)
        root.addWidget(self._empty_host)

        values = _wire_list(raw)
        #: The order the device reported, to go back to after a sort.
        self._as_reported = list(values)
        if values:
            for v in values:
                self._insert_row(len(self._rows), wire=v, highlight=False)
        self._refresh_all_combos()
        # What the device sent, kept so an edit can be shown per row rather
        # than as one wash over the whole list.
        self._original: list[Any] = self.values()
        self.set_locked(readonly)

    def _make_spin_btn(self, text: str, tip: str, height: int) -> QToolButton:
        """Tiny spinner half — QToolButton avoids global QPushButton min sizes."""
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(False)
        b.setFixedSize(self._SPIN_W, height)
        b.setStyleSheet(
            f"QToolButton {{"
            f"  padding: 0px; margin: 0px; font-size: {style.small_css(7)};"
            f"  background: {style.BG_PANEL}; color: {style.TEXT};"
            f"  border: 1px solid {style.BORDER};"
            f"  border-radius: 0px;"
            f"}}"
            f"QToolButton:hover {{ background: {style.BG_HEADER}; }}"
            f"QToolButton:pressed {{ background: {style.SEL_BG}; }}"
            f"QToolButton:disabled {{ color: {style.TEXT_DIM}; }}"
        )
        return b

    def values(self) -> list[Any]:
        """Current array of wire enum keys, in row order (works when locked)."""
        out: list[Any] = []
        for combo, _btns, _row in self._rows:
            data = combo.currentData()
            if data is not None:
                out.append(data)
        return out

    # --- what changed, and where ---------------------------------------

    def is_dirty(self) -> bool:
        return self.values() != self._original

    def change_items(self) -> tuple[list[str], list[str]]:
        """(what the device sent, what the rows say now), as words.

        The wire keys mean nothing on screen -- a Tagged list of `4, 2` is
        `br-test, ether1` -- and the form's status line counts these to say a
        list is three entries longer than the device's.
        """
        return (
            [self._label_of(w) for w in self._original],
            [self._label_of(w) for w in self.values()],
        )

    def reset_baseline(self) -> None:
        self._original = self.values()
        self.show_dirty(False)

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Rebuild the rows from the array just read off the device (see Apply).

        The device's order is the reported order again: a sorted view was a way
        of reading the *old* list, and this is a new one. So the sort is dropped
        with the marks rather than re-applied to rows nobody has seen yet.
        """
        wires = _wire_list(raw)
        while self._rows:
            item = self._rows_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            self._rows.pop(0)
        self._view_sorted = False
        self._as_reported = list(wires)
        for wire in wires:
            self._insert_row(len(self._rows), wire=wire, highlight=False)
        self._refresh_all_combos()
        self._sync_empty()
        self._original = self.values()
        self.show_dirty(False)

    def _baseline_in_view_order(self) -> list[Any]:
        """The before-image, read in the order the rows are in.

        Comparing a sorted view against the device's order would call a reorder
        a removal and an addition: pulling one port out of a sorted twelve-port
        list marked half of it. Sorting both sides first leaves only the edit.
        """
        if not self._view_sorted:
            return self._original
        return sorted(self._original, key=lambda w: sort_key(self._label_of(w)))

    def _edit_marks(self) -> tuple[set[int], dict[int, str]]:
        """Rows that differ from what the device sent, and the gaps a removal
        left, as ``{row index: what was there}``.

        A shortest edit script over the two lists of wire values, so pulling
        ether3 out of the middle marks the seam it left instead of every row
        that shuffled up to close it.
        """
        changed, gone = edit_marks(
            self._baseline_in_view_order(), self.values()
        )
        return changed, {
            at: ", ".join(self._label_of(w) for w in wires)
            for at, wires in gone.items()
        }

    def show_dirty(self, dirty: bool) -> None:
        """Mark the rows that differ, not the whole list.

        Called by :func:`_apply_dirty_style`, which paints every other editor
        as a whole. Removing one port from a bridge VLAN washed twelve rows
        amber and said nothing about which one went.
        """
        changed, gaps = self._edit_marks() if dirty else (set(), {})
        for i, (combo, _btns, _row) in enumerate(self._rows):
            _apply_dirty_style(combo, i in changed)
        self._rows_host.marks.set_gaps(gaps)
        if gaps:
            # The mark is a line on a screen; say it in words too, since a
            # removed row leaves nothing for a screen reader to land on.
            removed = ", ".join(gaps[at] for at in sorted(gaps))
            self._rows_host.setToolTip(f"Removed: {removed}")
            a11y.describe(self, f"{removed} removed")
        else:
            self._rows_host.setToolTip("")
            a11y.describe(self, "")
        # An emptied list has no row left to carry the mark.
        if self._empty_host is not None:
            _apply_dirty_style(self._empty_host, bool(gaps) and not self._rows)

    # --- sibling lists -------------------------------------------------

    def add_peer(self, other: "MultiEnumArrayEditor") -> None:
        """Link another list this one may not repeat an entry from.

        A bridge VLAN's Tagged and Untagged: The router refuses a port in both
        ("interface cannot be in tagged and untagged at the same time"), so a
        port the other list holds is not offered here at all -- the same rule
        that stops one list offering its own entries twice, read across the
        pair. Which fields these are is the schema's answer, not this widget's
        (see :func:`schema.exclusive_array_peers`).

        Symmetric: linking Tagged to Untagged links Untagged to Tagged, so a
        caller cannot leave half a pair wired. Each side refreshes its
        dropdowns when the other's membership changes, and nothing else --
        freeing ether3 next door must not mark this list as edited.
        """
        if other is self or other in self._peers:
            return
        self._peers.append(other)
        other.changed.connect(self._refresh_all_combos)
        other.add_peer(self)
        self._refresh_all_combos()

    def _peer_values(self) -> set:
        """Everything the sibling lists hold -- not on offer here."""
        out: set = set()
        for peer in self._peers:
            try:
                out.update(peer.values())
            except RuntimeError:      # the peer's C++ side went away
                continue
        return out

    def _peer_names(self) -> list[str]:
        """The siblings' field labels, for a tooltip that says where to look."""
        out = []
        for peer in self._peers:
            try:
                if peer._name:
                    out.append(peer._name)
            except RuntimeError:
                continue
        return out

    def _label_of(self, wire: Any) -> str:
        for w, lab in self._choices:
            if w == wire:
                return lab
        return str(wire)

    # --- ordering ------------------------------------------------------

    def reported_order(self) -> list[Any]:
        """What this list holds, in the order the device sent it.

        Kept multiset-safe: a duplicate the device sent (nothing in the UI can
        make one) must not lose its row to a membership test. Anything added
        since goes last, in the order it was added.
        """
        remaining = self.values()
        out: list[Any] = []
        for wire in self._as_reported:
            if wire in remaining:
                remaining.remove(wire)
                out.append(wire)
        return out + remaining

    def sort_by_name(self) -> None:
        """Order the rows by what they say -- ether2 before ether10.

        By the label, whatever the vocabulary is, using the key the list
        columns sort by, so the order reads the same as everywhere else.
        """
        self._view_sorted = True
        self._apply_order(sorted(
            self.values(), key=lambda w: sort_key(self._label_of(w))
        ))

    def restore_reported_order(self) -> None:
        self._view_sorted = False
        self._apply_order(self.reported_order())

    def _index_at(self, y: int) -> int:
        """Which row a pointer at ``y`` in the rows host is over.

        The row the pointer is *inside*, not the nearest boundary: a drag of a
        pixel or two has to leave the order alone, and comparing against row
        centres made a press-and-twitch swap two rows.
        """
        rows = [row for _c, _b, row in self._rows]
        if not rows:
            return -1
        if y < rows[0].geometry().top():
            return 0
        for i, row in enumerate(rows):
            geo = row.geometry()
            if geo.top() <= y <= geo.bottom():
                return i
        return len(rows) - 1

    def _move_row(self, index: int, target: int) -> None:
        """Move one row, layout and bookkeeping together."""
        if index == target or not (0 <= target < len(self._rows)):
            return
        entry = self._rows.pop(index)
        self._rows_layout.takeAt(index)
        self._rows.insert(target, entry)
        self._rows_layout.insertWidget(target, entry[2])
        self._rows_host.marks.update()

    def _begin_drag(self) -> None:
        self._drag_from = self.values()
        self._drag_dirty = self.is_dirty()

    def _drag_to(self, row_widget: QWidget, y: int) -> None:
        """Live reorder under the pointer -- no drop indicator to interpret."""
        if self._drag_from is None:
            return
        index = self._row_index(row_widget)
        if index < 0:
            return
        self._move_row(index, self._index_at(y))

    def _end_drag(self) -> None:
        if self._drag_from is None:
            return
        moved = self.values() != self._drag_from
        was_dirty = bool(self._drag_dirty)
        self._drag_from = None
        if moved:
            # A hand-made order is not the sorted one, whatever it looks like.
            self._view_sorted = False
            self._order_changed(was_dirty)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Alt+↑ / Alt+↓ move the row whose box has focus.

        A row that can only be moved by dragging is a row some people cannot
        move at all. The modifier leaves the dropdown's own arrow keys, which
        pick a value, exactly as they were.
        """
        if (
            event.type() == QEvent.Type.KeyPress
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
            and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)
        ):
            for i, (combo, _btns, _row) in enumerate(self._rows):
                if combo is obj:
                    target = i + (-1 if event.key() == Qt.Key.Key_Up else 1)
                    if 0 <= target < len(self._rows):
                        was_dirty = self.is_dirty()
                        self._view_sorted = False
                        self._move_row(i, target)
                        self._order_changed(was_dirty)
                        combo.setFocus()
                    return True
        return super().eventFilter(obj, event)

    def _order_changed(self, was_dirty: bool) -> None:
        """The rows moved without their contents changing.

        A list nobody had touched re-bases onto the new order and stays clean --
        putting a twelve-port list in a readable order must not leave a pending
        write behind. One that was already edited keeps its before-image, or
        rebasing would swallow the change and Apply would send nothing.
        """
        self._refresh_all_combos()
        self._rows_host.marks.update()
        if not was_dirty:
            self._original = self.values()
            self.rebased.emit()
        self.changed.emit()

    def _apply_order(self, values: list[Any]) -> None:
        """Rebuild the rows in this exact order. See :meth:`_order_changed`."""
        if values == self.values():
            return
        was_dirty = self.is_dirty()
        while self._rows:
            item = self._rows_layout.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            self._rows.pop(0)
        for wire in values:
            self._insert_row(len(self._rows), wire=wire, highlight=False)
        self._sync_empty()
        self._order_changed(was_dirty)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        """Right-click: fill this one list, and order it.

        The device sends these in the order the entries were added, which on a
        twelve-port bridge is no order to read down. Sorting is per field on
        purpose -- the list you asked about is the list that moves. *Add
        Several* is the same offer the ▾ makes, once instead of once per port.
        """
        menu = QMenu(self)
        act_add = menu.addAction("&Add Several…")
        act_add.setToolTip("Pick several at once — Ctrl-click or Shift-click")
        act_add.setEnabled(self._can_add())
        menu.addSeparator()
        act_sort = menu.addAction("Sort By &Name")
        act_sort.setToolTip("This list, by what its rows say (ether2 before ether10)")
        act_sort.setEnabled(len(self._rows) > 1)
        act_reported = menu.addAction("&Device Order")
        act_reported.setToolTip("Back to the order the device sent")
        act_reported.setEnabled(self.values() != self.reported_order())
        chosen = menu.exec(ev.globalPos())
        if chosen is act_add:
            self.open_multi_pick()
        elif chosen is act_sort:
            self.sort_by_name()
        elif chosen is act_reported:
            self.restore_reported_order()
        ev.accept()

    def label(self) -> str:
        """The array as words -- what the queue and the close prompt show.

        Enum *keys* are what goes on the wire and they mean nothing on screen:
        a Tagged list read `4, 2` where the rows say `br-test, ether1`.
        """
        return ", ".join(self._label_of(w) for w in self.values()) or "(none)"

    def _used_by_others(self, combo: QComboBox) -> set:
        """Values this row must not offer: held by another row, or by a peer."""
        return {
            c.currentData() for c, _b, _r in self._rows
            if c is not combo and c.currentData() is not None
        } | self._peer_values()

    def _fill_combo(self, combo: QComboBox, current_wire: Any) -> None:
        """Populate one row dropdown with what is still free to pick.

        Only entries no other row holds: an interface already tagged is not
        offered a second time, which is how Winbox lists these. This row's own
        value is always in the box -- it has to be, for the box to show it --
        even when the vocabulary does not contain it, because a value the
        device reported and we cannot name is still the value in front of the
        user, and quietly selecting a different one would rewrite the row.
        """
        used = self._used_by_others(combo)

        combo.blockSignals(True)
        combo.clear()
        for w, lab in self._choices:
            if w not in used:
                combo.addItem(lab, w)
        sel = -1
        if current_wire is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == current_wire:
                    sel = i
                    break
            if sel < 0:
                # Not in the vocabulary (or filtered out of it): keep it, said
                # plainly, rather than moving the row to something else.
                combo.insertItem(0, self._label_of(current_wire), current_wire)
                combo.setItemData(
                    0, "Reported by the device", Qt.ItemDataRole.ToolTipRole
                )
                sel = 0
        if sel < 0 and combo.count() > 0:
            sel = 0
        if sel >= 0:
            combo.setCurrentIndex(sel)
        combo.blockSignals(False)
        # After the fill, not before: the items were just thrown away and
        # rebuilt, and the marks live on the items.
        apply_marks(combo, self._marks)

    def set_mark_source(self, client, path, provider) -> None:
        """Where each row's dropdown re-reads its status dots from.

        Handed in rather than worked out here: which table a field offers, and
        whether its entries are keyed by id or by name, is the form's question
        and it has already answered it (see ``PropertyWindow._field_marks``).
        """
        self._mark_client = client
        self._mark_path = tuple(path) if path else None
        self._mark_provider = provider
        for combo, _btns, _row in self._rows:
            hook = getattr(combo, "set_mark_source", None)
            if hook is not None and self._mark_path is not None:
                hook(client, self._mark_path, provider=self._pull_marks)

    def _pull_marks(self) -> dict:
        """Fresh marks, kept: a row refilled a moment later needs them too."""
        if self._mark_provider is None:
            return self._marks
        try:
            self._marks = dict(self._mark_provider() or {})
        except Exception:  # noqa: BLE001
            pass
        return self._marks

    def _refresh_all_combos(self) -> None:
        """Rebuild every row's dropdown after the array membership changes."""
        for combo, _btns, _row in self._rows:
            self._fill_combo(combo, combo.currentData())
        self._sync_add_buttons()

    def _on_combo_changed(self, _index: int = 0) -> None:
        # Membership of "already in list" may have changed for sibling rows.
        self._refresh_all_combos()
        self.changed.emit()

    def set_locked(self, locked: bool) -> None:
        """Read-only: keep dropdowns visible but non-interactive; hide ▲▼/Add."""
        self._readonly = locked
        for combo, btns, row in self._rows:
            # Disabled combo still shows the current value; no popup.
            combo.setEnabled(not locked)
            btns.setVisible(not locked)
            btns.setEnabled(not locked)
            grip = row.findChild(_RowGrip)
            if grip is not None:
                grip.setVisible(not locked)
        if self._empty_host is not None:
            self._empty_host.setVisible(not locked and len(self._rows) == 0)
        self._sync_add_buttons()

    def _sync_empty(self) -> None:
        if self._empty_host is None:
            return
        self._empty_host.setVisible(not self._readonly and len(self._rows) == 0)

    def _free_values(self) -> list[Any]:
        """Vocabulary entries no row holds yet — what an added row could be."""
        return [w for w, _lab in self._free_choices()]

    def _free_choices(self) -> list[tuple[Any, str]]:
        """The same, as ``(wire, label)`` — what the multi-pick offers.

        Free of the peer lists too: an interface already untagged on this VLAN
        is not a candidate for its Tagged list, and offering it would only
        produce a write the router refuses.
        """
        held = set(self.values()) | self._peer_values()
        return [(w, lab) for w, lab in self._choices if w not in held]

    def _can_add(self) -> bool:
        """Whether there is both room for another row and something to put
        in it. What greys the ▾ and the *Add Several* item together."""
        if self._readonly or len(self._rows) >= self._max_rows:
            return False
        return bool(self._free_choices()) or not self._choices

    def _sync_add_buttons(self) -> None:
        """Grey the ▾ when there is nothing left to add, and say why.

        A list that has every interface in it has no row left to offer, and an
        offer that cannot be honoured is worse than none: it used to hand back
        a duplicate of the last row.
        """
        if self._readonly:
            return                      # the spinners are hidden anyway
        room = len(self._rows) < self._max_rows
        free = bool(self._free_values()) or not self._choices
        why = ""
        if not room:
            why = f"At most {self._max_rows} entries"
        elif not free:
            why = "Every entry is already in the list"
            peers = self._peer_names()
            if peers:
                why = f"Every entry is already in this list or {' or '.join(peers)}"
        several = "\nCtrl-click to add several at once"
        for _combo, btns, _row in self._rows:
            btn = btns.findChild(QToolButton, "addRow")
            if btn is not None:
                btn.setEnabled(room and free)
                btn.setToolTip(why or "Insert row below" + several)
        if self._add_first is not None:
            self._add_first.setEnabled(room and free)
            self._add_first.setToolTip(why or "Add entry" + several)

    def _choice_score(self, wire: Any) -> float:
        """Where an entry sits in its vocabulary, as a number to compare.

        A rate ladder is read from its labels, so 10M < 100M < 1G < 10G however
        the .jg declared them -- that ordering is what lets an inserted row land
        between its neighbours.

        A vocabulary of names has no arithmetic in it, and the wire key is no
        substitute: an interface id is a row id, not a position. On a virtual router
        ether1 is id 2 and vlan33 is id 3, so "the next id after ether1" is a
        VLAN -- which is what a second Tagged row used to be handed. What
        orders names is the box itself, so the score is the position in it.
        """
        if self._ladder:
            rate = _rate_score(
                next((n for w, n in self._choices if w == wire), "")
            )
            if rate is not None:
                return rate
        for i, (w, _lab) in enumerate(self._choices):
            if w == wire:
                return float(i)
        # Not in the vocabulary at all (an id the enum filter dropped, a row
        # from a device newer than the schema): ahead of every entry, so a row
        # added under it offers the first free one rather than the last.
        return -1.0

    def _guess_wire(self, insert_at: int) -> Any:
        """Pick a sensible enum value for a new row at ``insert_at``.

        Names (an interface table): the next free entry below this row's in the
        box, wrapping to the top -- a second Tagged row under ether1 is ether2,
        and a fresh list starts on the first entry, which is where Winbox's own
        "a dropdown is never empty" lands.

        Rates: neighbouring rows bracket the guess -- between 10M and 1G it
        prefers 100M; at the ends it steps one rate class up/down when it can.
        Either way a value another row already holds is avoided if possible.
        """
        if not self._choices:
            return None
        current = self.values()
        prev = current[insert_at - 1] if insert_at > 0 else None
        nxt = current[insert_at] if insert_at < len(current) else None

        if not self._ladder:
            free = self._free_values()   # in the order the dropdown lists them
            if not free:
                return self._choices[0][0]
            if prev is not None:
                ps = self._choice_score(prev)
                after = [w for w in free if self._choice_score(w) > ps]
                if after:
                    return after[0]
            return free[0]

        scored = [(self._choice_score(w), w, lab) for w, lab in self._choices]
        scored.sort(key=lambda t: (t[0], str(t[1])))
        used = set(current)

        def pick(candidates: list[Any], prefer_unused: bool = True) -> Any:
            if prefer_unused:
                free = [c for c in candidates if c not in used]
                if free:
                    return free[0]
            return candidates[0] if candidates else self._choices[0][0]

        def prefer_full(cands: list[Any]) -> list[Any]:
            """Prefer '* full' modes when several options share a rate class."""
            full = []
            for w in cands:
                lab = next((n for x, n in self._choices if x == w), "")
                if "full" in lab.lower() and "half" not in lab.lower():
                    full.append(w)
            return full or cands

        if prev is not None and nxt is not None:
            lo, hi = self._choice_score(prev), self._choice_score(nxt)
            if lo > hi:
                lo, hi = hi, lo
            between = [w for s, w, _ in scored if lo < s < hi]
            if between:
                # Midpoint of the open interval between neighbours.
                mid = (lo + hi) / 2.0
                between.sort(key=lambda w: abs(self._choice_score(w) - mid))
                # At the closest rate, prefer full duplex (typical Advertise set).
                best_score = self._choice_score(between[0])
                same = [w for w in between if abs(self._choice_score(w) - best_score) < 0.01]
                return pick(prefer_full(same))
            # No rate between — still try unused full-duplex at either side.
            near = [w for s, w, _ in scored if s == lo or s == hi]
            return pick(prefer_full(near) if near else [prev, nxt])

        if prev is not None:
            ps = self._choice_score(prev)
            higher = [w for s, w, _ in scored if s > ps]
            if higher:
                return pick(higher)
            return pick([prev], prefer_unused=False)

        if nxt is not None:
            ns = self._choice_score(nxt)
            lower = [w for s, w, _ in scored if s < ns]
            if lower:
                lower.sort(key=lambda w: -self._choice_score(w))
                return pick(lower)
            return pick([nxt], prefer_unused=False)

        # Empty list: start with a common mid-range full duplex if present.
        prefer_labels = ("1G baseT full", "100M baseT full", "10M baseT full")
        by_lab = {lab: w for w, lab in self._choices}
        for lab in prefer_labels:
            if lab in by_lab:
                return by_lab[lab]
        return self._choices[0][0]

    def _insert_row(
        self,
        index: int,
        wire: Any = None,
        *,
        highlight: bool = False,
    ) -> None:
        if self._rows_layout is None:
            return
        if len(self._rows) >= self._max_rows:
            return
        index = max(0, min(index, len(self._rows)))
        if wire is None:
            free = self._free_values()
            if self._choices and not free:
                # Nothing left to offer. Adding anyway wrote a second row
                # holding an entry the list already had -- a duplicate the
                # device will not keep, sitting in the form looking legitimate.
                # The ▾ is greyed for this, so this is only the last word.
                return
            wire = self._guess_wire(index)
            if free and wire not in free:
                # The guess is taken (a rate already advertised): the nearest
                # free one keeps "between its neighbours" as close as it can.
                score = self._choice_score(wire)
                wire = min(free, key=lambda w: abs(self._choice_score(w) - score))

        h = self._ROW_H
        half = h // 2
        half2 = h - half  # remainder so stack == combo height exactly

        row = QWidget()
        row.setFixedHeight(h)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        grip = _RowGrip(self, row)
        grip.setFixedHeight(h)
        grip.setVisible(not self._readonly)
        a11y.part(grip, "move entry")
        lay.addWidget(grip)

        combo = MarkedCombo()
        combo.setFixedHeight(h)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if self._mark_path is not None:
            combo.set_mark_source(
                self._mark_client, self._mark_path, provider=self._pull_marks
            )
        # Temporary single item so values() sees this row during sibling refill.
        if wire is not None:
            lab = next((n for w, n in self._choices if w == wire), str(wire))
            combo.addItem(lab, wire)
        lay.addWidget(combo, 1)

        # Compact spinner stacked to the same height as the combo (Winbox).
        btns = QWidget()
        btns.setFixedSize(self._SPIN_W, h)
        bl = QVBoxLayout(btns)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        # ▲ remove · ▼ add (classic Winbox multi chrome)
        up = self._make_spin_btn("▴", "Remove this row", half)
        down = self._make_spin_btn("▾", "Insert row below", half2)
        down.setObjectName("addRow")   # found again by _sync_add_buttons
        up.setStyleSheet(
            up.styleSheet()
            + "QToolButton { border-bottom: none; }"
        )
        bl.addWidget(up)
        bl.addWidget(down)
        lay.addWidget(btns)

        up.clicked.connect(lambda _=False, r=row: self._on_remove(r))
        down.clicked.connect(lambda _=False, r=row: self._on_add_after(r))

        self._rows_layout.insertWidget(index, row)
        self._rows.insert(index, (combo, btns, row))
        self._fill_combo(combo, wire)
        # A row built after the form was is a dropdown the form's own
        # guard_wheel never saw -- and an array field is where rows appear
        # latest (see wheel_guard).
        guard_wheel(combo)
        combo.installEventFilter(self)      # Alt+↑ / Alt+↓ move the row
        combo.currentIndexChanged.connect(self._on_combo_changed)
        combo.setEnabled(not self._readonly)
        btns.setVisible(not self._readonly)
        btns.setEnabled(not self._readonly)
        self._sync_empty()
        # Sibling dropdowns need their "already in list" section updated.
        self._refresh_all_combos()
        if highlight:
            self._flash_row(row)
            self.changed.emit()

    def _flash_row(self, row_widget: QWidget) -> None:
        """Brief highlight so a newly inserted row is easy to spot."""
        # Blend selection colour → transparent over a few steps.
        base = style.SEL_BG
        steps = self._FLASH_STEPS

        def _hex_rgb(c: str) -> tuple[int, int, int]:
            c = c.lstrip("#")
            if len(c) >= 6:
                return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
            return 80, 140, 220

        r0, g0, b0 = _hex_rgb(base)
        # Fade toward panel background
        try:
            r1, g1, b1 = _hex_rgb(style.BG_PANEL)
        except Exception:
            r1, g1, b1 = r0, g0, b0

        state = {"i": 0}
        # Parented to the row it paints, not to the list: a row removed (or a
        # whole form torn down) mid-fade takes its timer with it instead of
        # leaving one that ticks on to restyle a deleted widget.
        timer = QTimer(row_widget)
        self._flash_timers.append(timer)

        def _done() -> None:
            if timer in self._flash_timers:
                self._flash_timers.remove(timer)
            try:
                timer.stop()
                timer.deleteLater()
            except RuntimeError:      # the timer went away with its row
                pass

        def tick() -> None:
            i = state["i"]
            try:
                if i >= steps or row_widget.parent() is None:
                    row_widget.setStyleSheet("")
                    _done()
                    return
                t = i / max(1, steps - 1)
                r = int(r0 + (r1 - r0) * t)
                g = int(g0 + (g1 - g0) * t)
                b = int(b0 + (b1 - b0) * t)
                # Early steps more opaque
                alpha = max(0.15, 1.0 - t)
                row_widget.setStyleSheet(
                    f"background-color: rgba({r},{g},{b},{alpha:.2f});"
                )
            except RuntimeError:      # the row's C++ side went away mid-fade
                _done()
                return
            state["i"] = i + 1

        timer.setInterval(self._FLASH_MS)
        timer.timeout.connect(tick)
        tick()  # paint first frame immediately
        timer.start()

    def _row_index(self, row_widget: QWidget) -> int:
        if self._rows_layout is None:
            return -1
        for i in range(self._rows_layout.count()):
            item = self._rows_layout.itemAt(i)
            if item and item.widget() is row_widget:
                return i
        return -1

    def _on_add_after(self, row_widget: QWidget, *, multi: bool | None = None) -> None:
        idx = self._row_index(row_widget)
        if idx < 0:
            return
        if multi if multi is not None else self._ctrl_held():
            self.open_multi_pick(at=idx + 1)
            return
        self._insert_row(idx + 1, wire=None, highlight=True)

    def _on_add_first(self, *, multi: bool | None = None) -> None:
        """The ▾ an empty list offers, same two ways as any other row's."""
        if multi if multi is not None else self._ctrl_held():
            self.open_multi_pick(at=0)
            return
        self._insert_row(0, highlight=True)

    @staticmethod
    def _ctrl_held() -> bool:
        """Whether Ctrl was down for the click being handled.

        ``QToolButton.clicked`` drops the modifiers, and the modifier is the
        whole point: the same button either adds the one row Winbox adds or
        opens the picker for a dozen.
        """
        return bool(
            QApplication.keyboardModifiers()
            & Qt.KeyboardModifier.ControlModifier
        )

    def open_multi_pick(self, at: int | None = None) -> int:
        """Offer everything this list has not got, and add what is picked.

        A bridge VLAN's Tagged list is one dropdown per port, so tagging eight
        interfaces is eight presses of ▾ and eight trips through a dropdown
        that gets shorter each time. This is that, once: Ctrl-click the ports,
        Add, and one row lands per port. Returns how many rows were added, so
        a caller that cares (a test) need not count them itself.
        """
        free = self._free_choices()
        if not free or not self._can_add():
            # Nothing to offer, no room for it, or a locked form: the ▾ and the
            # menu item are already greyed for each of those, so this is the
            # last word rather than the only one.
            return 0
        dlg = MultiPickDialog(
            free,
            title=f"Add to {self._name}" if self._name else "Add Entries",
            marks=self._pull_marks(),
            parent=self.window(),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        return self.add_values(dlg.picked(), at=at)

    def add_values(self, wires: list[Any], *, at: int | None = None) -> int:
        """Insert one row per wire key, in the order given.

        Rows the list already holds are skipped rather than duplicated -- the
        device would not keep a second copy, and a picker can be answered from
        a list that moved on under it. One ``changed`` for the whole batch: the
        form recomputes its status line and its queue on every one, and adding
        twelve ports is one edit to read, not twelve.
        """
        if self._readonly:
            return 0
        # Peer-held entries too: the picker never offers one, but a picker
        # answered while the other list was being edited must not be the way
        # an illegal pair gets in.
        held = set(self.values()) | self._peer_values()
        index = len(self._rows) if at is None else max(0, min(at, len(self._rows)))
        fresh: list[QWidget] = []
        for wire in wires:
            if wire in held or len(self._rows) >= self._max_rows:
                continue
            at_index = index + len(fresh)
            before = len(self._rows)
            self._insert_row(at_index, wire=wire, highlight=False)
            if len(self._rows) == before:
                continue          # _insert_row declined; nothing to remember
            held.add(wire)
            fresh.append(self._rows[at_index][2])
        if not fresh:
            return 0
        for row in fresh:
            self._flash_row(row)
        self._sync_empty()
        self._refresh_all_combos()
        self.changed.emit()
        return len(fresh)

    def _on_remove(self, row_widget: QWidget) -> None:
        if self._rows_layout is None:
            return
        idx = self._row_index(row_widget)
        if idx < 0:
            return
        item = self._rows_layout.takeAt(idx)
        w = item.widget() if item else None
        if w is not None:
            # Off the screen now, not on the next event cycle: taken out of the
            # layout it keeps its last geometry, and deleteLater alone left it
            # painted over the rows that closed up under it.
            w.setParent(None)
            w.deleteLater()
        if 0 <= idx < len(self._rows):
            self._rows.pop(idx)
        self._sync_empty()
        self._refresh_all_combos()
        self.changed.emit()


def _optional_is_set(fld, raw: Any) -> bool:
    """Whether an ``optional`` field currently holds a value.

    The router says "unset" in more than one way: absent, a high sentinel, or — for a
    field with a declared minimum, like DDNS Update Interval's 60s — a zero it
    would never accept as real.
    """
    if raw is None:
        return False
    # `optval`: the field's own way of saying "not set" (Shared Users 0 is
    # unlimited, an RA Lifetime of 0 is none).
    unset = getattr(fld, "unset_value", None)
    if unset is not None:
        try:
            if int(raw) == int(unset):
                return False
        except (TypeError, ValueError):
            pass
    if isinstance(raw, (bytes, bytearray)):
        return len(raw) > 0
    if isinstance(raw, str):
        return bool(raw.strip())
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        if raw >= 0xFFFFFF00:
            return False
        minimum = getattr(fld, "minimum", None)
        if isinstance(minimum, int) and minimum > 0:
            return raw >= minimum
        return raw != 0
    return True


def _is_network_multi(fld) -> bool:
    """A `multi` whose entries are networks, rather than enums or strings."""
    return any(
        (n.jg_type or "") in ("network", "ipaddr", "network6")
        for n in (getattr(fld, "nested", None) or {}).values()
    )


#: jg types the UnionEditor knows how to auto-detect and encode.
_UNION_ADDR_TYPES = ("network", "network6", "ipaddr", "ip6addr", "addr", "string")


#: "not asked yet", distinct from "the device did not say" (None).
_UNSET = object()

#: Item role on a combo entry that belongs to a *union alternative* rather
#: than to the row's primary field: it carries the wire code to write it under.
#: Quick Set Frequency is one box over two codes -- 'auto' is u25a, a specific
#: 2412 MHz is u25d -- and sending 2412 under u25a is not the same request.
ALT_CODE_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _field_by_name(menu, name) -> Field | None:
    """The menu's field of this name -- what a ``type:alias`` row points at."""
    want = str(name or "").strip()
    if menu is None or not want:
        return None
    for fld in (menu.fields or {}).values():
        if (fld.name or "").strip() == want:
            return fld
    return None


def _is_switchable_union(fld) -> bool:
    """A collapsed `single` union whose variants are addresses / DNS names.

    Enum-or-number unions (Frequency) keep their primary combo instead; only
    address/string unions get the one-box auto-detect editor.
    """
    alts = getattr(fld, "union_alts", None)
    if not alts:
        return False
    kinds = [(fld.jg_type or "")] + [(a.jg_type or "") for a in alts]
    return all(k in _UNION_ADDR_TYPES for k in kinds)


def _has_value(value: Any) -> bool:
    """Whether an editor holds something worth sending on an Add."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, tuple, dict)) and not value:
        return False
    return True


#: Editors that keep their own before-image, and so answer `is_dirty()` for
#: themselves instead of having their current value compared with a spec's
#: `original`. A tuple would have to be built after all of them are defined;
#: a function reads the same and can live next to its callers.
def _owns_baseline(editor: QWidget) -> bool:
    return isinstance(editor, (
        OptMatchEditor, NetworkArrayEditor, OptionalFieldEditor, UnionEditor,
        MultiTristateEditor, SetEditor, NegatableEditor, ScalarArrayEditor,
        RangeEditor, MultiEnumArrayEditor,
    ))


#: Editors that hold a *list* of entries answer ``change_items()`` with
#: ``(before, after)`` as display strings -- what the device sent, and what the
#: rows say now. One method, because the status line wants both what the list
#: gained and lost and what it read before.


def _change_items(editor: QWidget) -> tuple[list[str], list[str]] | None:
    hook = getattr(editor, "change_items", None)
    if not callable(hook):
        return None
    try:
        before, after = hook()
    except Exception:
        return None
    return list(before), list(after)


def _change_summary(editor: QWidget) -> str:
    """How many entries a list editor gained and lost — ``+3 −2``.

    A count of dirty *fields* cannot say that a Tagged list is two ports
    shorter than the one the device sent while Untagged is three longer: both
    read as "1 field", and a half-finished edit is mostly a question about how
    many entries are in play. Compared as a multiset, so a row that only
    shuffled up counts as neither.
    """
    items = _change_items(editor)
    if items is None:
        return ""
    before, after = items
    rest = list(before)
    added = 0
    for item in after:
        if item in rest:
            rest.remove(item)
        else:
            added += 1
    parts = []
    if added:
        parts.append(f"+{added}")
    if rest:
        parts.append(f"−{len(rest)}")
    if parts:
        return " ".join(parts)
    # Same entries, another order. Still an edit -- the device is sent the list
    # in row order -- and "+0" would be a lie about what changed.
    return "reordered" if before != after else ""


def _baseline_label(spec: dict[str, Any], editor: QWidget) -> str:
    """What this field read before it was touched, as words.

    A list editor is asked, rather than the spec: its before-image moves when
    the rows are sorted to be read (see MultiEnumArrayEditor._apply_order),
    and the spec's rendered string would then describe an order nobody sees.
    """
    items = _change_items(editor)
    if items is not None:
        return ", ".join(items[0]) or "(none)"
    rendered = spec.get("rendered")
    if isinstance(rendered, str) and rendered.strip():
        return rendered
    original = spec.get("original")
    return "(none)" if original in (None, "") else str(original)


def _display_label(editor: QWidget) -> str:
    """What an editor holds, as words — for a summary line, not for the wire."""
    hook = getattr(editor, "label", None)
    if callable(hook):
        try:
            return str(hook())
        except Exception:
            pass
    if isinstance(editor, DurationEdit):
        return editor.text()
    if isinstance(editor, QComboBox):
        return editor.currentText()
    if isinstance(editor, QCheckBox):
        return "yes" if editor.isChecked() else "no"
    value = _editor_value(editor)
    return "(none)" if value in (None, "") else str(value)


def _joined(details: list[str], cap: int = 4) -> str:
    """The changed fields as one phrase, kept to a line's worth.

    A whole-form edit would otherwise push the line wider than the form and
    take the buttons off the right-hand side; the tooltip still holds all of it.
    """
    if len(details) <= cap:
        return ", ".join(details)
    return ", ".join(details[:cap]) + f", … {len(details) - cap} more"


def _apply_dirty_style(widget: QWidget, dirty: bool) -> None:
    """Highlight an editor that differs from the value loaded from the device."""
    if isinstance(widget, (QLabel, ReadOnlyValueList, ReadOnlyValueEdit)):
        return
    # An array says which of its *rows* moved, and where one was taken out -- a
    # wash over the whole list cannot. The hook, not an isinstance list: three
    # editors are arrays with rows now (see widgets/edit_marks.py).
    shower = getattr(widget, "show_dirty", None)
    if callable(shower):
        shower(dirty)
        return
    apply_dirty_style(widget, dirty)


class ReadOnlyValueEdit(QLineEdit):
    """A device value shown in a read-only text box, the way Winbox does it.

    A box rather than a bare label keeps row spacing uniform down the form and
    makes the value straightforward to select and copy. It is read-only for
    good — unlike a plain QLineEdit editor, flipping the form into queue mode
    must not make device status writable.
    """

    def __init__(self, rendered: Any = "", parent=None) -> None:
        text = "" if rendered is None else str(rendered)
        super().__init__(text if text else "—", parent)
        self.setReadOnly(True)
        self.setCursorPosition(0)
        # Muted fill so it reads as "reported", not "type here".
        self._base_qss = (
            f"QLineEdit {{ background: {style.BG_ALT}; color: {style.TEXT}; }}"
        )
        # Held: the same soft amber wash a modified editor wears. The two
        # never appear on one widget (a read-only box is never dirty), and it
        # is already a skin-proof way of saying "this is not the device's".
        self._held_qss = (
            "QLineEdit { background-color: rgba(245, 158, 11, 0.20);"
            f" color: {style.TEXT}; }}"
        )
        self._held = False
        self.setStyleSheet(self._base_qss)

    @property
    def is_held(self) -> bool:
        return self._held

    def set_held(self, on: bool, pending: str = "") -> None:
        """Say whether this box is showing a value the device has moved past.

        The live poll leaves a value alone while it is selected, so that a
        double-click to copy a counter survives the next tick. Freezing it
        silently would be worse than losing the selection -- you would copy a
        stale number and never know -- so a held box wears the amber wash and
        carries what the device now reports in its tooltip.
        """
        on = bool(on)
        if on:
            self.setToolTip(
                f"Held while selected — the device now reports {pending}"
                if pending else "Held while selected"
            )
        if on == self._held:
            return
        self._held = on
        self.setStyleSheet(self._held_qss if on else self._base_qss)
        if not on:
            self.setToolTip("")


def _hairline() -> QWidget:
    """A ``type:separator`` row: one pixel of rule, no bevel, air either side.

    A sunken HLine is two pixels of engraved groove — chrome that reads as a
    divider between *panels*. Winbox draws a hairline between groups of fields,
    so that is what this is (same recipe as the nav tree's dividers). The rule
    is wrapped rather than spaced by the layout: a bare 1px row sits as close
    to the field below it as two fields sit to each other, which reads as a
    squashed row instead of a break between groups.
    """
    holder = QWidget()
    box = QVBoxLayout(holder)
    box.setContentsMargins(0, _RULE_PAD, 0, _RULE_PAD)
    box.setSpacing(0)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {style.BORDER}; border: none;")
    box.addWidget(line)
    return holder


def _share_set_pitch(editors: list["SetEditor"]) -> None:
    """Put neighbouring ``set`` grids on one column pitch.

    Winbox's Data Rates tab lines its four rate rows up in a single grid:
    1Mbps, 6Mbps and 24Mbps all start at the same x, even though the widest
    label differs from row to row. Sized row by row instead, the columns drift
    a couple of pixels per row and the tab reads as four ragged ones.

    It is not a whole dialog's worth of alignment, though: a CAPs Rate's Basic
    Rates grid runs on a ~64px pitch while the HT Basic MCS grid right below it
    runs on ~38px, because Winbox grids each *container* separately and every
    set there sits in a `group` of its own. Groups are not modelled yet, so the
    stand-in is the pitch itself -- sets that want a column within a dozen
    pixels of each other are the ones Winbox would have drawn as one grid.
    """
    by_columns: dict[int, list["SetEditor"]] = {}
    for editor in editors:
        by_columns.setdefault(editor.columns, []).append(editor)

    def apply(cluster: list["SetEditor"]) -> None:
        if not cluster:
            return
        pitch = max(e.natural_pitch() for e in cluster)
        for editor in cluster:
            editor.set_column_pitch(pitch)

    for group in by_columns.values():
        cluster: list["SetEditor"] = []
        for editor in sorted(group, key=lambda e: e.natural_pitch()):
            if cluster and (editor.natural_pitch()
                            - cluster[0].natural_pitch()) > _PITCH_KIN:
                apply(cluster)
                cluster = []
            cluster.append(editor)
        apply(cluster)


def _fill_row_width(widget: QWidget) -> None:
    """Let a value control span the form's value column, as Winbox v3 does.

    QFormLayout only grows a field that asks to grow: a line edit does
    (Expanding), a dropdown and a spin box do not, which left Loop Protect's
    enum at the width of its longest item while the boxes above it stretched.
    A check box is the exception — Winbox keeps that at its natural size.
    """
    if isinstance(widget, QCheckBox):
        return
    policy = widget.sizePolicy()
    if policy.horizontalPolicy() is QSizePolicy.Policy.Expanding:
        return
    policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
    widget.setSizePolicy(policy)


def _value_label(rendered: Any) -> ReadOnlyValueEdit:
    """Non-editable device value (status, traffic, counters)."""
    return ReadOnlyValueEdit(rendered)


def _tuple_part_labels(row: QWidget) -> list[QLabel]:
    """Labels that hold each part of a Tx/Rx tuple (for live refresh)."""
    stored = getattr(row, "_part_labels", None)
    if isinstance(stored, list):
        return stored
    return []


# Widest rendering the traffic formatters emit, e.g. "958.34 Mbps" / "123456 p/s".
# Used as the floor for a boxed value so a narrow form still shows a full sample
# rather than clipping one mid-digit.
_BOXED_VALUE_SAMPLE = "8888.88 Mbps"
# padding (6+6) + border (1+1), plus a little slack
_BOXED_VALUE_CHROME = 16


def _boxed_value_min_width(widget: QWidget) -> int:
    return widget.fontMetrics().horizontalAdvance(_BOXED_VALUE_SAMPLE) + _BOXED_VALUE_CHROME


def _tuple_value_row(spec: dict[str, Any], *, locked: bool) -> QWidget:
    """Winbox ``type:tuple`` row: ``[tx] / [rx]`` with optional separate boxes.

    Boxed parts share the row's full width equally, the way Winbox draws them.
    """
    sep = spec.get("sep") or " / "
    separate = bool(spec.get("separate"))
    parts = spec.get("parts") or []
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    part_labels: list[ReadOnlyValueEdit] = []
    for i, part in enumerate(parts):
        if i:
            sep_lab = QLabel(sep.strip() or "/")
            sep_lab.setStyleSheet(
                f"color: {style.TEXT_DIM}; background: transparent;"
            )
            lay.addWidget(sep_lab)
        text = part.get("rendered") or "—"
        # A read-only box already, so the parts need no hand-drawn border.
        cell = _value_label(text)
        if separate:
            cell.setMinimumWidth(_boxed_value_min_width(cell))
            cell.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )
            lay.addWidget(cell, 1)  # equal share of the row
        else:
            lay.addWidget(cell)
        part_labels.append(cell)
    if not separate:
        # Plain "tx / rx" text stays left-aligned instead of spreading out.
        lay.addStretch(1)
    row.setEnabled(not locked or True)  # display-only values
    row._part_labels = part_labels  # type: ignore[attr-defined]
    return row


# Winbox draws Tx blue and Rx red.
TX_COLOR = "#2a6fdb"
RX_COLOR = "#c44b2b"

# Keep enough history to fill a wide window at the tightest stride.
_MAX_SAMPLES = 4096

# Winbox spends 3px per sample in bar mode, left to right: 1px gap, 1px Tx,
# 1px Rx. The line/mirror modes read better with one sample per pixel.
_BAR_STRIDE = 3


class TrafficPlot(QWidget):
    """Live Tx/Rx plot, newest sample at the right edge.

    ``bars`` reproduces Winbox: each sample is a 3px group — an empty column,
    then a 1px Tx bar (blue) and a 1px Rx bar (red) side by side, both grown
    from the bottom. ``lines`` and ``mirror`` are alternate takes on the same
    data at one sample per pixel.
    """

    MODES = ("bars", "lines", "mirror")

    #: emitted with the hovered (timestamp, tx, rx), or None when the cursor leaves
    hovered = Signal(object)

    #: Enough to read a trend from; the plot grows with the window past this.
    MIN_HEIGHT = 40

    def __init__(self, fmt: Callable[[float], str], parent=None) -> None:
        super().__init__(parent)
        # Expanding vertically, not Fixed: a fixed policy meant the graph
        # neither grew with the window nor let it shrink, and two of these plus
        # a floor of 84px each set the window's minimum height.
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._fmt = fmt
        self._samples: deque[tuple[float, float, float]] = deque(maxlen=_MAX_SAMPLES)
        self._mode = "bars"
        self._hover_col: int | None = None  # column offset from the right edge

    # --- data ----------------------------------------------------------

    def push(self, tx: float, rx: float, when: float | None = None) -> None:
        ts = time.time() if when is None else when
        self._samples.append((ts, max(0.0, float(tx)), max(0.0, float(rx))))
        self.update()

    def latest(self) -> tuple[float, float, float] | None:
        return self._samples[-1] if self._samples else None

    def set_mode(self, mode: str) -> None:
        if mode in self.MODES and mode != self._mode:
            self._mode = mode
            self.update()

    def mode(self) -> str:
        return self._mode

    # --- hover ----------------------------------------------------------

    def _plot_rect(self):
        return self.rect().adjusted(1, 1, -1, -1)

    def _stride(self) -> int:
        return _BAR_STRIDE if self._mode == "bars" else 1

    def _sample_at_column(self, col_from_right: int, *, drawn: int | None = None):
        """Map a pixel offset from the right edge back to its sample.

        ``drawn`` bounds the lookup to the samples actually on screen, so the
        blank area left of the plotted history reports nothing.
        """
        if col_from_right < 0:
            return None
        back = col_from_right // self._stride()
        if drawn is not None and back >= drawn:
            return None
        idx = len(self._samples) - 1 - back
        if 0 <= idx < len(self._samples):
            return self._samples[idx]
        return None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        r = self._plot_rect()
        x = int(event.position().x())
        col = r.right() - x
        drawn = max(1, r.width() // self._stride())
        sample = (
            self._sample_at_column(col, drawn=drawn)
            if r.left() <= x <= r.right()
            else None
        )
        self._hover_col = col if sample is not None else None
        self.hovered.emit(sample)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_col = None
        self.hovered.emit(None)
        self.update()
        super().leaveEvent(event)

    # --- painting -------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.BG_WINDOW))
        p.setPen(QPen(QColor(style.BORDER)))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        r = self._plot_rect()

        if not self._samples:
            p.setPen(QColor(style.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "waiting for samples…")
            p.end()
            return

        stride = self._stride()
        visible = list(self._samples)[-max(1, r.width() // stride):]
        peak = self._peak(visible)
        if self._mode == "lines":
            self._paint_lines(p, r, visible, peak)
        elif self._mode == "mirror":
            self._paint_mirror(p, r, visible, peak)
        else:
            self._paint_bars(p, r, visible, peak)

        self._paint_hover_cursor(p, r)
        p.setPen(QColor(style.TEXT_DIM))
        p.drawText(r.left() + 4, r.top() + 13, f"peak {self._fmt(peak)}")
        p.end()

    def _peak(self, visible: list[tuple[float, float, float]]) -> float:
        # Tx and Rx are drawn side by side in every mode, so the tallest
        # single series sets the scale.
        return max((max(tx, rx) for _t, tx, rx in visible), default=1.0) or 1.0

    def _paint_bars(self, p, r, visible, peak) -> None:
        """Winbox look: 1px gap, 1px Tx, 1px Rx per sample, grown from the bottom."""
        tx_pen, rx_pen = QPen(QColor(TX_COLOR), 1), QPen(QColor(RX_COLOR), 1)
        h = r.height()
        bottom = r.bottom()
        n = len(visible)
        for i, (_ts, tx, rx) in enumerate(visible):
            # Rightmost pixel of this sample's group holds Rx, Tx sits just
            # left of it, and the third column is left blank as the gap.
            x_rx = r.right() - (n - 1 - i) * _BAR_STRIDE
            x_tx = x_rx - 1
            if x_tx < r.left():
                continue
            tx_px = int(h * (tx / peak))
            rx_px = int(h * (rx / peak))
            if tx_px > 0:
                p.setPen(tx_pen)
                p.drawLine(x_tx, bottom, x_tx, max(r.top(), bottom - tx_px + 1))
            if rx_px > 0:
                p.setPen(rx_pen)
                p.drawLine(x_rx, bottom, x_rx, max(r.top(), bottom - rx_px + 1))

    def _paint_lines(self, p, r, visible, peak) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for series, color in (
            ([s[1] for s in visible], QColor(TX_COLOR)),
            ([s[2] for s in visible], QColor(RX_COLOR)),
        ):
            path = QPainterPath()
            n = len(series)
            for i, v in enumerate(series):
                x = r.right() - (n - 1 - i)
                y = r.bottom() - (r.height() - 2) * (v / peak)
                if i == 0:
                    path.moveTo(QPointF(x, y))
                else:
                    path.lineTo(QPointF(x, y))
            p.setPen(QPen(color, 1.2))
            p.drawPath(path)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_mirror(self, p, r, visible, peak) -> None:
        """Tx grows up from the midline, Rx grows down."""
        mid = r.top() + r.height() // 2
        half = max(1, r.height() // 2)
        tx_color, rx_color = QColor(TX_COLOR), QColor(RX_COLOR)
        for i, (_ts, tx, rx) in enumerate(visible):
            x = r.right() - (len(visible) - 1 - i)
            tx_px = int(half * (tx / peak))
            rx_px = int(half * (rx / peak))
            if tx_px:
                p.setPen(QPen(tx_color, 1))
                p.drawLine(x, mid, x, mid - tx_px)
            if rx_px:
                p.setPen(QPen(rx_color, 1))
                p.drawLine(x, mid, x, mid + rx_px)
        p.setPen(QPen(QColor(style.BORDER), 1))
        p.drawLine(r.left(), mid, r.right(), mid)

    def _paint_hover_cursor(self, p, r) -> None:
        if self._hover_col is None:
            return
        x = r.right() - self._hover_col
        if r.left() <= x <= r.right():
            p.setPen(QPen(QColor(style.TEXT), 1, Qt.PenStyle.DotLine))
            p.drawLine(x, r.top(), x, r.bottom())


def _format_pps(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f} p/s".replace(",", " ")


class _PlotWithReadout(QWidget):
    """One titled plot plus a Tx/Rx readout that follows the mouse."""

    def __init__(
        self, title: str, fmt: Callable[[float], str], parent=None
    ) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        cap = QLabel(title)
        # The themes style bare QWidget with a window background; captions on
        # the graph panel must stay transparent or they paint grey blocks.
        cap.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)}; background: transparent;"
        )
        head.addWidget(cap)
        head.addStretch(1)
        self._readout = QLabel("")
        self._readout.setTextFormat(Qt.TextFormat.RichText)
        self._readout.setStyleSheet(f"font-size: {style.small_css(9)}; background: transparent;")
        head.addWidget(self._readout)
        lay.addLayout(head)

        self.plot = TrafficPlot(fmt)
        self.plot.hovered.connect(self._on_hover)
        lay.addWidget(self.plot, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._fmt = fmt
        self._hover: tuple[float, float, float] | None = None
        self._refresh_readout()

    def push(self, tx: float, rx: float) -> None:
        self.plot.push(tx, rx)
        if self._hover is None:
            self._refresh_readout()

    def _on_hover(self, sample) -> None:
        self._hover = sample
        self._refresh_readout()

    def _refresh_readout(self) -> None:
        sample = self._hover or self.plot.latest()
        if sample is None:
            self._readout.setText(
                f"<span style='color:{style.TEXT_DIM}'>no samples yet</span>"
            )
            return
        ts, tx, rx = sample
        when = time.strftime("%H:%M:%S", time.localtime(ts))
        if self._hover is not None:
            age = max(0, int(time.time() - ts))
            stamp = f"{when} (−{age}s)"
        else:
            stamp = f"{when} · now"
        self._readout.setText(
            f"<span style='color:{TX_COLOR}'>Tx {self._fmt(tx)}</span>"
            f"<span style='color:{style.TEXT_DIM}'> · </span>"
            f"<span style='color:{RX_COLOR}'>Rx {self._fmt(rx)}</span>"
            f"<span style='color:{style.TEXT_DIM}'> · {stamp}</span>"
        )


class TrafficGraphBox(QFrame):
    """Winbox-style dual graphs (byte rate + packet rate) under Traffic."""

    #: Poll intervals offered in the header, in milliseconds. 1 Hz is the
    #: floor, not a Winbox habit we are copying: The router recomputes an
    #: interface's traffic on its own one-second tick, and until it does, the
    #: reply repeats. Polling a virtual router at 200 ms answered all 40 times in 8 s and
    #: every number -- the rates *and* the byte and packet counters behind them
    #: -- stepped only on the second, so there is nothing to derive from a
    #: faster poll either. Sub-second entries bought four identical bars per
    #: second and four times the wire traffic, so the list starts at 1 / s.
    RATES = (
        (1000, "1 / s"),
        (2000, "1 / 2s"),
        (5000, "1 / 5s"),
        (10000, "1 / 10s"),
    )

    #: emitted with the newly chosen interval in milliseconds
    rate_changed = Signal(int)

    def __init__(
        self, graphs: list[dict[str, Any]], parent=None, *, rate_ms: int = 1000
    ) -> None:
        super().__init__(parent)
        self.setObjectName("trafficGraphBox")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # Scope to the box: QLabel is a QFrame, so a bare "QFrame" rule would
        # draw a border around every caption inside it.
        self.setStyleSheet(
            f"QFrame#trafficGraphBox {{ background: {style.BG_PANEL}; "
            f"border: 1px solid {style.BORDER}; }}"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        byte_name = "Byte Graph (Tx / Rx)"
        pkt_name = "Packet Graph (Tx / Rx)"
        for g in graphs or []:
            name = str(g.get("name") or "")
            curves = g.get("curves") or []
            label = name + (f"  ({', '.join(curves)})" if curves else "")
            if "packet" in name.lower():
                pkt_name = label or pkt_name
            elif name:
                byte_name = label or byte_name

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        bar.addStretch(1)
        rate_lab = QLabel("Update:")
        rate_lab.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)}; background: transparent;"
        )
        bar.addWidget(rate_lab)
        self._rate_combo = QComboBox()
        for ms, text in self.RATES:
            self._rate_combo.addItem(text, ms)
        idx = self._rate_combo.findData(rate_ms)
        if idx < 0:
            idx = self._rate_combo.findData(TRAFFIC_POLL_MIN_MS)
        self._rate_combo.setCurrentIndex(max(idx, 0))
        self._rate_combo.setToolTip("How often the graphs and counters refresh")
        self._rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        bar.addWidget(self._rate_combo)
        mode_lab = QLabel("View:")
        mode_lab.setStyleSheet(
            f"color: {style.TEXT_DIM}; font-size: {style.small_css(9)}; background: transparent;"
        )
        bar.addWidget(mode_lab)
        self._mode_combo = QComboBox()
        for mid, text in (
            ("bars", "Bars"),
            ("lines", "Lines"),
            ("mirror", "Mirrored"),
        ):
            self._mode_combo.addItem(text, mid)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        bar.addWidget(self._mode_combo)
        root.addLayout(bar)

        self._byte = _PlotWithReadout(byte_name, _format_bitrate)
        self._pkt = _PlotWithReadout(pkt_name, _format_pps)
        root.addWidget(self._byte)
        root.addWidget(self._pkt)

    def _on_mode_changed(self, _index: int) -> None:
        mode = str(self._mode_combo.currentData() or "bars")
        self._byte.plot.set_mode(mode)
        self._pkt.plot.set_mode(mode)

    def _on_rate_changed(self, _index: int) -> None:
        ms = self._rate_combo.currentData()
        if isinstance(ms, int):
            self.rate_changed.emit(ms)

    def set_rate_ms(self, ms: int) -> None:
        idx = self._rate_combo.findData(ms)
        if idx >= 0 and idx != self._rate_combo.currentIndex():
            self._rate_combo.setCurrentIndex(idx)

    def push_sample(
        self, rx_bps: float, tx_bps: float, rx_pps: float, tx_pps: float
    ) -> None:
        self._byte.push(tx_bps, rx_bps)
        self._pkt.push(tx_pps, rx_pps)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_bitrate(bps: int | float | None) -> str:
    if bps is None:
        return "—"
    try:
        v = float(bps)
    except (TypeError, ValueError):
        return str(bps)
    if v < 1000:
        return f"{v:.0f} bps"
    if v < 1_000_000:
        return f"{v / 1000:.2f} kbps"
    if v < 1_000_000_000:
        return f"{v / 1_000_000:.2f} Mbps"
    return f"{v / 1_000_000_000:.2f} Gbps"


def _format_live_value(code: int, raw: Any, menu: Any) -> str:
    """Render a polled wire value; prefer schema, fall back to bitrate/bytes."""
    if menu is not None:
        fld = menu.fields.get(code) if hasattr(menu, "fields") else None
        if fld is not None:
            try:
                return fld.render(raw)
            except Exception:
                pass
    # Known traffic codes
    if code in (0x100D3, 0x100D4, 0x100D5, 0x100D6):
        return _format_bitrate(_as_int(raw))
    if code in (0x100FC, 0x100FD):
        from x3.proto.schema import format_bytes

        n = _as_int(raw)
        return str(raw) if n is None else format_bytes(n)
    if code in (0x100CB, 0x100CD, 0x100D7, 0x100D8):
        n = _as_int(raw)
        return f"{n} p/s" if n is not None else str(raw)
    if isinstance(raw, (bytes, bytearray)):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    return str(raw) if raw is not None else "—"


class OptionalFieldEditor(QWidget):
    """A field the device may hold no value for at all.

    The schema marks these ``optional: True``. Unset means "no value" — IP ▸
    Cloud's DDNS Update Interval, an interface's ARP Timeout — so a duration
    box reading ``00:00:00`` would state a setting the device does not have.

    Winbox v3 says it with the box itself: greyed out while unset, with a small
    triangle at its right edge. ▼ gives the field a value (the box wakes up on
    its default), ▲ takes the value away again; clicking the greyed box is the
    same as ▼, which is how Winbox lets you just start typing.

    Unlike a firewall ``opt`` match there is no negate toggle and no nested
    chain: it is the same wire code, either sent or left out. On a singleton
    menu — which sends its whole form — leaving it out *is* how "unset" is
    expressed.
    """

    changed = Signal()

    #: Width of the set/unset triangle. Narrow enough to read as part of the
    #: box's frame rather than as a button parked next to it.
    _TRIANGLE_W = 15

    def __init__(self, fld, inner: QWidget, *, present: bool, readonly: bool,
                 parent=None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._inner = inner
        #: Locked by the form (read-only session, dynamic row) — a different
        #: thing from unset, and it must survive a set/unset flip.
        self._form_locked = False
        self._inner_style = inner.styleSheet()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)          # the triangle belongs to the box, not beside it
        a11y.part(inner)
        row.addWidget(inner, 1)
        self._toggle = QToolButton()
        # The triangle is a tab stop. An unset field whose box is disabled -- a
        # combo, a spin box -- has nothing else the keyboard can land on, so
        # without this the field cannot be switched on without a mouse. Space
        # or Enter flips it (see _flip), and only Tab focuses it: a click on the
        # triangle should leave the keyboard in the box it just woke up.
        self._toggle.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._toggle.setFixedWidth(self._TRIANGLE_W)
        self._toggle.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        self._toggle.setStyleSheet(
            f"QToolButton {{ border: 1px solid {style.BORDER};"
            f" border-left: none; background: {style.BG_ALT}; }}"
            f"QToolButton:hover {{ background: {style.BG_HEADER}; }}"
            f"QToolButton:focus {{ border: 1px solid {style.SEL_BG};"
            f" background: {style.BG_HEADER}; }}"
            f"QToolButton:disabled {{ border-color: {style.BORDER_LIGHT}; }}"
        )
        self._toggle.clicked.connect(self._flip)
        a11y.part(self._toggle, "set or unset")
        row.addWidget(self._toggle, 0)
        # A read-only box still reports its clicks; a disabled one (a combo, a
        # spin box) does not, and Qt hands those to us instead — see
        # mousePressEvent.
        inner.installEventFilter(self)
        if not present:
            self._show_default()
        self._set_present(present, notify=False)
        self._original = self.state()
        if readonly:
            self.set_locked(True)

    # --- set / unset -------------------------------------------------------

    def _flip(self) -> None:
        self._set_present(not self._present)
        if self._present:
            # Whatever flipped it -- the triangle, Space on the triangle, a
            # click on the greyed box -- the next thing anyone does is type in
            # the box, so that is where the keyboard goes.
            self._inner.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_present(self, present: bool, *, notify: bool = True) -> None:
        self._present = bool(present)
        self._toggle.setArrowType(
            Qt.ArrowType.UpArrow if self._present else Qt.ArrowType.DownArrow
        )
        self._toggle.setToolTip(
            f"Remove the value — leave {self._fld.name or 'this field'} unset"
            " (Space, or Ctrl+Space in the box)"
            if self._present
            else f"Set {self._fld.name or 'this field'}"
            " (Space, or just type in the box)"
        )
        # Set or unset is an arrow direction on screen. Neither the arrow nor
        # the greyed box survives being spoken, so both the triangle and the
        # box say which it is.
        a11y.describe(self._toggle,
                      "set — press to unset" if self._present
                      else "unset — press to set")
        a11y.state(self._inner, "unset", not self._present)
        self._apply_look()
        if notify:
            self.changed.emit()

    def _apply_look(self) -> None:
        """Grey the box while it is unset (or while the form is locked)."""
        locked = self._form_locked or not self._present
        _set_locked(self._inner, locked)
        self._toggle.setEnabled(not self._form_locked)
        if isinstance(self._inner, (QLineEdit, QSpinBox, QComboBox)):
            self._inner.setStyleSheet(
                self._inner_style if self._present else
                f"QLineEdit, QSpinBox, QComboBox {{ background: {style.BG_ALT};"
                f" color: {style.TEXT_DIM}; }}"
            )

    def _show_default(self) -> None:
        """Put the field's default in the box, the value ▼ would switch on.

        Winbox shows an unset field's default greyed out, so pressing ▼ (or
        clicking the box) leaves you looking at the same number, now editable.
        A field with no declared default keeps whatever the editor built —
        blank, with its placeholder to say what shape the value takes.
        """
        default = getattr(self._fld, "default", None)
        if default is None:
            return
        inner = self._inner
        try:
            if isinstance(inner, DurationEdit):
                inner.set_wire_value(int(default))
            elif isinstance(inner, QSpinBox):
                inner.setValue(int(default))
            elif isinstance(inner, QComboBox):
                idx = inner.findData(default)
                if idx >= 0:
                    inner.setCurrentIndex(idx)
            elif isinstance(inner, QLineEdit) and not inner.text():
                inner.setText(self._fld.render(default))
        except Exception:  # noqa: BLE001
            pass    # a default we cannot express is not worth failing a form over

    # --- clicking (or typing in) the greyed box is pressing ▼ --------------

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._inner and not self._form_locked:
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and not self._present:
                self._set_present(True)
                self._inner.setFocus(Qt.FocusReason.MouseFocusReason)
                return True
            if kind == QEvent.Type.KeyPress:
                mods = event.modifiers()
                # Ctrl+Space is the keyboard's ▼/▲, from inside the box: the
                # one way to take a value away again without leaving the row.
                if (
                    event.key() == Qt.Key.Key_Space
                    and mods == Qt.KeyboardModifier.ControlModifier
                ):
                    self._flip()
                    return True
                if not self._present and self._wakes_on(event):
                    self._set_present(True)
                    # Not consumed: the box is editable now, so the keystroke
                    # that woke it lands in it -- typing into a greyed box just
                    # works, the same way clicking one does.
                    return False
        return super().eventFilter(obj, event)

    @staticmethod
    def _wakes_on(event) -> bool:
        """Does this keystroke mean "give the field a value"?

        Only text does. An unset box is a read-only box, so Tab, the arrows,
        Escape and Enter have to keep meaning what they mean everywhere else on
        the form -- and Space is left out too, because it is the triangle's own
        key and typing a leading space into a freshly woken box is nobody's
        intent.
        """
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        ):
            return False
        text = event.text()
        return bool(text) and text.isprintable() and text != " "

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # A disabled inner editor drops the click; Qt walks it up to us.
        if not self._present and not self._form_locked:
            self._set_present(True)
            self._inner.setFocus(Qt.FocusReason.MouseFocusReason)
            return
        super().mousePressEvent(event)

    # --- the value ---------------------------------------------------------

    def is_present(self) -> bool:
        return self._present

    def inner(self) -> QWidget:
        return self._inner

    def value(self) -> Any:
        return _editor_value(self._inner)

    def state(self) -> tuple:
        return (getattr(self, "_present", False), self.value())

    def is_dirty(self) -> bool:
        return self.state() != self._original

    def reset_baseline(self) -> None:
        self._original = self.state()

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-render from a value just read off the device (see Apply).

        Presence is part of what a write changes: a field the device now holds
        no value for has to grey out again, and one it has just been given has
        to wake up.
        """
        _restate_editor(self._inner, raw, rendered, values)
        present = _optional_is_set(self._fld, raw)
        if not present:
            self._show_default()
        self._set_present(present, notify=False)
        self._original = self.state()

    def label(self) -> str:
        return str(self.value()) if self._present else "(unset)"

    def set_locked(self, locked: bool) -> None:
        self._form_locked = bool(locked)
        self._apply_look()


class NegatableEditor(QWidget):
    """An always-present value with a ``!`` toggle (firewall TOS Value,
    Discovery Interface List). The value is one wire code (edited by the inner
    widget); the negate flag is a sibling bool code the toggle writes."""

    changed = Signal()

    def __init__(self, fld, inner: QWidget, negate_code: int, *,
                 negated: bool, readonly: bool, parent=None) -> None:
        super().__init__(parent)
        self._fld = fld
        self._inner = inner
        self._negate_code = negate_code
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._neg = QToolButton()
        self._neg.setCheckable(True)
        self._neg.setChecked(bool(negated))
        self._neg.setStyleSheet(
            "QToolButton { padding: 1px 6px; border: 1px solid palette(mid);"
            " border-radius: 3px; }"
            "QToolButton:checked { background: palette(highlight);"
            " color: palette(highlighted-text); font-weight: bold; }"
        )
        self._sync_neg()
        self._neg.toggled.connect(self._sync_neg)
        self._neg.toggled.connect(lambda *_: self.changed.emit())
        a11y.part(self._neg, "negate")
        a11y.part(inner)
        row.addWidget(self._neg)
        row.addWidget(inner, 1)
        # Bubble the inner editor's edits up as our own change.
        for sig in ("textChanged", "valueChanged", "currentIndexChanged", "toggled"):
            s = getattr(inner, sig, None)
            if s is not None:
                try:
                    s.connect(lambda *_: self.changed.emit())
                except (TypeError, RuntimeError):
                    pass
        self._original = self.state()
        if readonly:
            self.set_locked(True)

    def _sync_neg(self, *_a) -> None:
        on = self._neg.isChecked()
        self._neg.setText("≠" if on else "=")
        self._neg.setToolTip(
            "Negated: matches everything except this value. Click to match it."
            if on else "Matches this value. Click to negate."
        )

    def inner(self) -> QWidget:
        return self._inner

    def value(self) -> Any:
        return _editor_value(self._inner)

    def is_negated(self) -> bool:
        return self._neg.isChecked()

    def negate_code(self) -> int:
        return self._negate_code

    def is_present(self) -> bool:
        return True

    def state(self) -> tuple:
        return (self._neg.isChecked(), self.value())

    def is_dirty(self) -> bool:
        return self.state() != self._original

    def reset_baseline(self) -> None:
        self._original = self.state()

    def restate(self, raw: Any = None, rendered: str = "", values=None) -> None:
        """Re-render value and negate flag from what was read off the device."""
        _restate_editor(self._inner, raw, rendered, values)
        self._neg.setChecked(
            (values or {}).get(self._negate_code) in (True, 1)
        )
        self._sync_neg()
        self._original = self.state()

    def error(self) -> str | None:
        return None

    def label(self) -> str:
        return ("!" if self._neg.isChecked() else "") + str(self.value())

    def set_locked(self, locked: bool) -> None:
        self._neg.setEnabled(not locked)
        _set_locked(self._inner, locked)


def _restate_editor(
    widget: QWidget, raw: Any, rendered: str, values: dict[int, Any] | None
) -> bool:
    """Show a freshly-read device value in an editor already on screen.

    The mirror of :func:`_editor_value`. An editor that is not one of Qt's own
    takes it through ``restate()`` -- the hook a new one implements instead of
    being added to the isinstance ladder below -- and is handed everything:
    the wire value for its own code, the rendered text, and the whole row, for
    the editors that own more than one code (a range's high end, a match's
    negate flag).

    Returns False for an editor that cannot be restated, so a caller can leave
    it showing what it holds rather than blanking it: a value we cannot render
    back is not the same as no value.
    """
    # A label and its boxed twin are painted by the live poll, by name as well
    # as by code. A ReadOnlyValueList is not: it has its own restate() below,
    # because Current Tagged moves when Tagged is applied.
    if isinstance(widget, (QLabel, ReadOnlyValueEdit)):
        return False                      # device status, painted elsewhere
    setter = getattr(widget, "restate", None)
    if callable(setter):
        setter(raw, rendered, values)
        return True
    # Before QLineEdit: DurationEdit subclasses it but holds wire units, not
    # the text they are shown as.
    if isinstance(widget, DurationEdit):
        widget.set_wire_value(_as_int(raw))
        return True
    if isinstance(widget, QCheckBox):
        if isinstance(raw, (bool, int)):
            widget.setChecked(bool(raw) != _checkbox_inversion(widget))
            return True
        return False
    if isinstance(widget, QComboBox):
        return _restate_combo(widget, raw, rendered)
    if isinstance(widget, QSpinBox):
        value = _as_int(raw)
        if value is None:
            return False
        widget.setValue(value)
        return True
    if isinstance(widget, QLineEdit):
        widget.setText("" if rendered in (None, "—") else str(rendered))
        return True
    return False


def _restate_combo(widget: QComboBox, raw: Any, rendered: str) -> bool:
    """Re-select a dropdown from a value read off the device.

    By wire data first and the rendered text second -- the same two-step the
    form builds it with, and the only one that works for an editable combo
    holding a typed name (an address list, a packet mark) whose text *is* the
    value and which has no item to match.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = int.from_bytes(raw, "little")
    if not isinstance(raw, list):
        index = widget.findData(raw)
        if index >= 0:
            widget.setCurrentIndex(index)
            return True
    text = "" if rendered in (None, "—") else str(rendered)
    index = widget.findText(text)
    if index >= 0:
        widget.setCurrentIndex(index)
        return True
    if widget.isEditable():
        widget.setCurrentText(text)
        return True
    return False


def _checkbox_inversion(widget: QCheckBox) -> bool:
    """Whether this box shows the opposite of its bit (`inv:1`).

    Recorded on the widget when the form builds it: the box has no field to
    ask, and restating an inverted flag without it turns Auto Negotiation off
    every time the form is re-read.
    """
    return bool(widget.property("invertedFlag"))


def _editor_value(widget: QWidget) -> Any:
    """Current value of a form editor, in the widget's natural type.

    An editor that is not one of Qt's own says what it holds through
    ``editor_value()`` -- the hook a new one implements instead of being added
    to the isinstance ladder below.
    """
    getter = getattr(widget, "editor_value", None)
    if callable(getter):
        return getter()
    if isinstance(widget, MultiEnumArrayEditor):
        return widget.values()
    # Before QLineEdit: DurationEdit subclasses it but reports seconds, not text.
    if isinstance(widget, DurationEdit):
        return widget.value()
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QComboBox):
        # An editable combo whose text is a typed new value (an address-list
        # name not in the list) has no item data -- the text itself is the
        # value. A picked item keeps its wire data.
        if widget.isEditable():
            typed = widget.currentText()
            idx = widget.findText(typed)
            return widget.itemData(idx) if idx >= 0 else typed
        return widget.currentData()
    if isinstance(widget, QSpinBox):
        return widget.value()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QListWidget):
        vals = []
        for item in widget.selectedItems():
            vals.append(item.data(Qt.ItemDataRole.UserRole))
        try:
            return sorted(vals)
        except TypeError:
            return vals
    if isinstance(widget, ReadOnlyValueList):
        return widget.values()
    if isinstance(widget, QLabel):
        return widget.text()
    return None


def _set_locked(widget: QWidget, locked: bool) -> None:
    """Lock or unlock an editor without rebuilding it."""
    if isinstance(widget, (QLabel, ReadOnlyValueList, ReadOnlyValueEdit)):
        return  # status values stay non-editable
    # Editors that own their own child widgets have to be told: enabling the
    # container does not re-enable a child that was disabled individually.
    setter = getattr(widget, "set_locked", None)
    if callable(setter):
        setter(locked)
        return
    if isinstance(widget, QLineEdit):
        widget.setReadOnly(locked)
    elif isinstance(widget, QListWidget):
        widget.setEnabled(not locked)
        if locked:
            widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        else:
            widget.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
    else:
        widget.setEnabled(not locked)
