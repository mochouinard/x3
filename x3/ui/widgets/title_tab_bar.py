"""Winbox-4-style inline tab strip for an MDI window's title bar.

Renders a multi-tab window's tabs *in* the title bar::

    [icon] Interfaces  ▾  Interface | Interface List | Ethernet | …   [_ □ x]

The group name and the ▾ (full-list) button are fixed on the left; the tabs
live in a hand-clipped, transparent viewport (a plain child widget positioned
inside a clip widget -- no QScrollArea, so nothing paints a grey background
behind the tabs and there is no scrollbar state to go stale on rebuild). The
strip can be wider than the bar: tabs that run past the edge show their start;
pan with the **mouse wheel** or by **click-dragging**. Selecting a tab re-frames
the strip around it -- the tab before it leads, then the selection, then the
tabs that follow -- and every tab shows its full name.

The native title text is cleared and this widget fills the title band (see
:meth:`MdiWindow.set_title_bar_content`). The ▾ menu holds the full list, a
per-tab **⧉ open-in-own-window** action, and per-tab checkboxes plus **Show all**
for pinning a subset. Text colour follows the native bar (white on the active
accent, dark on the light inactive bar) via :meth:`set_window_active`.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
    QWidgetAction,
)

from .. import a11y, style

_DRAG_SLOP = 4  # px of movement before a press becomes a pan (vs a click)


class _Clip(QWidget):
    """Transparent viewport that clips the tab row; wheel pans it sideways."""

    def __init__(self, owner: "TitleTabBar") -> None:
        super().__init__(owner)
        self._owner = owner

    def wheelEvent(self, e):  # noqa: N802
        self._owner._pan(-(e.angleDelta().y() or e.angleDelta().x()))
        e.accept()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._owner._layout_row()


class TitleTabBar(QWidget):
    """Inline, pannable tabs for the title bar."""

    tabSelected = Signal(int)      # user picked a tab
    openTabWindow = Signal(int)    # user asked to open a tab in its own window

    def __init__(self, group_name: str, tab_names: list[str], parent=None) -> None:
        super().__init__(parent)
        self._group = group_name or ""
        self._names = list(tab_names)
        self._active = 0
        self._pinned: set[int] = set(range(len(self._names)))
        self._window_active = True
        self._tab_btns: list[QToolButton] = []
        self._offset = 0
        self._max_offset = 0
        # A single drag gesture: undecided until it moves past the slop, then
        # "pan" (horizontal drag on a tab) or "move" (the window -- a drag on the
        # name/empty bar, or a mostly-vertical drag on a tab).
        self._press_gp: QPoint | None = None
        self._drag_mode: str | None = None
        self._dragging = False
        self._drag_off = 0
        self._win_start: QPoint | None = None
        self._on_tab = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._name = QLabel(self._group)
        self._name.setObjectName("ttbGroup")
        self._name.installEventFilter(self)  # drag the name to move the window
        # Always in the layout, hidden when empty: set_group_name() has to be
        # able to give a strip a title it did not start with.
        self._name.setVisible(bool(self._group))
        lay.addWidget(self._name)
        lay.addSpacing(6)

        self._arrow = QToolButton()
        self._arrow.setObjectName("ttbArrow")
        self._arrow.setText("▾")  # ▾
        self._arrow.setToolTip("All tabs")
        self._arrow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._populate_menu)
        self._arrow.setMenu(self._menu)
        lay.addWidget(self._arrow)
        lay.addSpacing(8)

        self._clip = _Clip(self)
        lay.addWidget(self._clip, 1)
        self._row = QWidget(self._clip)
        self._rowlay = QHBoxLayout(self._row)
        self._rowlay.setContentsMargins(0, 0, 0, 0)
        self._rowlay.setSpacing(0)
        self._clip.installEventFilter(self)

        self._build_tabs()

    # -- state --------------------------------------------------------------

    def set_active(self, index: int) -> None:
        self._active = index
        self._restyle()
        self._ensure_active_visible()

    def set_group_name(self, text: str) -> None:
        """Retitle the strip in place.

        The strip replaces the native title text, so whatever the window would
        have written there has to land here instead -- a property form's
        "· 2 edited" badge is the reason this exists.
        """
        text = text or ""
        if text == self._group:
            return
        self._group = text
        self._name.setText(text)
        self._name.setVisible(bool(text))

    def set_window_active(self, active: bool) -> None:
        if active != self._window_active:
            self._window_active = active
            self._restyle()

    def _visible_indexes(self) -> list[int]:
        return [i for i in range(len(self._names)) if i in self._pinned]

    def _fg(self) -> str:
        if self._window_active:
            bg = QColor(style.TITLE_ACTIVE)
            return style.SEL_FG if bg.lightnessF() < 0.6 else "#0b1220"
        # Inactive bar is the light header colour; use the full (dark) text, not
        # the dim grey the native title uses -- a whole tab strip in dim grey on
        # light grey is unreadable.
        return style.TEXT

    # -- tab row build + pan ------------------------------------------------

    def _build_tabs(self) -> None:
        while self._rowlay.count():
            item = self._rowlay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self._tab_btns = []
        vis = self._visible_indexes()
        for n, idx in enumerate(vis):
            b = QToolButton()
            b.setObjectName("ttbTab")
            b.setText(self._names[idx])
            b.setAutoRaise(True)
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            b.setProperty("tabIndex", idx)
            b.clicked.connect(lambda _=False, i=idx: self.tabSelected.emit(i))
            b.installEventFilter(self)  # drag-to-pan starting on a tab
            self._rowlay.addWidget(b)
            self._tab_btns.append(b)
        # Style first (padding affects the size), then pin each tab to its text
        # width (computed from font metrics, so it does not depend on
        # style-polish timing). Without a hard width the layout can shrink a
        # button to its small minimumSizeHint and elide it to "…". The width is
        # measured *bold*, the weight the selected tab uses, so selecting a tab
        # cannot nudge its neighbours sideways -- tabs stay put and only the
        # strip's scroll offset moves.
        self._restyle()
        for b in self._tab_btns:
            f = QFont(b.font())
            f.setBold(True)
            b.setFixedWidth(QFontMetrics(f).horizontalAdvance(b.text()) + 26)
        self._rowlay.activate()
        self._row.adjustSize()
        self._layout_row()
        self._ensure_active_visible()

    def _tab_width(self, b: QToolButton) -> int:
        return b.minimumWidth() or b.sizeHint().width()

    def _tab_left(self, pos: int) -> int:
        """Left edge of the ``pos``-th tab within the row, from the pinned
        widths rather than from ``geometry()``. Fresh buttons still sit at x=0
        right after a rebuild -- the layout only places them on the next pass --
        so geometry cannot be trusted here.
        """
        total = 0
        for b in self._tab_btns[:pos]:
            total += self._tab_width(b) + self._rowlay.spacing()
        return total

    def _content_width(self) -> int:
        # Widths are pinned in _build_tabs, so this sum is exact and stays
        # constant across re-styles (see _build_tabs).
        total = sum(self._tab_width(b) for b in self._tab_btns)
        if len(self._tab_btns) > 1:
            total += self._rowlay.spacing() * (len(self._tab_btns) - 1)
        return total

    def _layout_row(self) -> None:
        cw = self._content_width()
        self._max_offset = max(0, cw - self._clip.width())
        self._offset = max(0, min(self._offset, self._max_offset))
        self._row.setGeometry(-self._offset, 0, max(cw, self._clip.width()), self._clip.height())

    def _pan(self, delta: int) -> None:
        self._offset = max(0, min(self._offset + delta, self._max_offset))
        self._layout_row()

    def _ensure_active_visible(self) -> None:
        pos = None
        for n, b in enumerate(self._tab_btns):
            if b.property("tabIndex") == self._active:
                pos = n
                break
        if pos is None:
            return
        # Re-frame the strip around the selection: the tab immediately *before*
        # it leads (one tab of context), then the selection, then everything
        # that follows. Parking the selection at the viewport's right instead
        # scrolls its successors off, which reads as a reordered strip.
        self._offset = max(0, min(self._tab_left(max(0, pos - 1)), self._max_offset))
        self._layout_row()

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        """One drag gesture, decided by direction: a mostly-horizontal drag on a
        tab pans the strip; anything else (a drag on the name/empty bar, or a
        mostly-vertical/diagonal drag on a tab) moves the whole window, like the
        native title bar it covers. A drag on a tab is never a click."""
        t = ev.type()
        left = Qt.MouseButton.LeftButton
        if t == QEvent.Type.MouseButtonPress and ev.button() == left:
            win = self.parentWidget()
            self._press_gp = ev.globalPosition().toPoint()
            self._on_tab = obj in self._tab_btns
            self._drag_mode = None
            self._dragging = False
            self._drag_off = self._offset
            self._win_start = win.pos() if win is not None else None
        elif (
            t == QEvent.Type.MouseMove
            and self._press_gp is not None
            and (ev.buttons() & left)
        ):
            gp = ev.globalPosition().toPoint()
            dx = gp.x() - self._press_gp.x()
            dy = gp.y() - self._press_gp.y()
            if self._drag_mode is None and (abs(dx) > _DRAG_SLOP or abs(dy) > _DRAG_SLOP):
                self._dragging = True
                # Tab + mostly-horizontal -> pan; otherwise move the window.
                self._drag_mode = "pan" if (self._on_tab and abs(dx) >= abs(dy)) else "move"
            if self._drag_mode == "pan":
                self._offset = max(0, min(self._drag_off - dx, self._max_offset))
                self._layout_row()
            elif self._drag_mode == "move" and self._win_start is not None:
                win = self.parentWidget()
                if win is not None:
                    # Dragging a maximized window pops it back to its old size
                    # under the cursor, then the drag carries on from there.
                    unmax = getattr(win, "restore_under_cursor", None)
                    if unmax is not None and unmax(gp):
                        self._press_gp = gp
                        self._win_start = win.pos()
                    else:
                        win.move(self._win_start + QPoint(dx, dy))
        elif (
            t == QEvent.Type.MouseButtonDblClick
            and ev.button() == left
            and obj not in self._tab_btns
        ):
            # The strip covers the title bar, so the native double-click to
            # maximize/restore would land in a dead spot. Not on a tab: there,
            # a double click is two selections.
            win = self.parentWidget()
            toggle = getattr(win, "toggle_maximized", None) if win is not None else None
            if toggle is not None:
                self._press_gp = None
                self._drag_mode = None
                self._dragging = False
                toggle()
                return True
        elif t == QEvent.Type.MouseButtonRelease:
            dragged = self._dragging
            on_tab = self._on_tab
            self._press_gp = None
            self._drag_mode = None
            self._dragging = False
            if dragged and on_tab:
                return True  # a drag that started on a tab is not a click
        return False

    # -- ▾ menu -------------------------------------------------------------

    def _populate_menu(self) -> None:
        self._menu.clear()
        # Readable rows + hover, independent of the strip's on-accent styling
        # (the menu is a child of this widget, so it sets its own colours).
        self._menu.setStyleSheet(
            f"QMenu {{ background: {style.BG_PANEL}; color: {style.TEXT};"
            f" border: 1px solid {style.BORDER}; }}"
            f"QMenu QToolButton {{ background: transparent; color: {style.TEXT};"
            " border: none; padding: 2px 8px; }"
            f"QMenu QToolButton:hover {{ background: {style.SEL_BG};"
            f" color: {style.SEL_FG}; border-radius: 3px; }}"
            f"QMenu QCheckBox {{ color: {style.TEXT}; }}"
        )
        self._row_checks: list[QCheckBox] = []
        for i, nm in enumerate(self._names):
            act = QWidgetAction(self._menu)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(6, 1, 6, 1)
            h.setSpacing(8)
            chk = QCheckBox()
            chk.setChecked(i in self._pinned)
            chk.setToolTip("Show this tab in the title bar")
            chk.toggled.connect(lambda on, idx=i: self._toggle_pin(idx, on))
            self._row_checks.append(chk)
            h.addWidget(chk)
            lbl = QToolButton()
            lbl.setText(nm)
            lbl.setAutoRaise(True)
            lbl.clicked.connect(lambda _=False, idx=i: self._pick(idx))
            h.addWidget(lbl)
            h.addStretch(1)
            openb = QToolButton()
            openb.setText("⧉")  # ⧉
            openb.setToolTip("Open this tab in its own window")
            openb.clicked.connect(lambda _=False, idx=i: self._open(idx))
            h.addWidget(openb)
            act.setDefaultWidget(row)
            self._menu.addAction(act)
        self._menu.addSeparator()
        sa_act = QWidgetAction(self._menu)
        sa_row = QWidget()
        sh = QHBoxLayout(sa_row)
        sh.setContentsMargins(6, 1, 6, 1)
        sa = QToolButton()
        sa.setText("Show all")
        sa.setAutoRaise(True)
        sa.clicked.connect(self._show_all_clicked)
        sh.addWidget(sa)
        sh.addStretch(1)
        sa_act.setDefaultWidget(sa_row)
        self._menu.addAction(sa_act)

    def _pick(self, idx: int) -> None:
        self._menu.close()  # picking a tab is terminal -> close
        self.tabSelected.emit(idx)

    def _open(self, idx: int) -> None:
        self._menu.close()  # opening a window is terminal -> close
        self.openTabWindow.emit(idx)

    def _toggle_pin(self, idx: int, on: bool) -> None:
        # Keep the menu open so several tabs can be curated in one visit.
        if on:
            self._pinned.add(idx)
        else:
            self._pinned.discard(idx)
        self._build_tabs()

    def _show_all_clicked(self) -> None:
        self._pinned = set(range(len(self._names)))
        for cb in getattr(self, "_row_checks", []):
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self._build_tabs()

    # -- styling ------------------------------------------------------------

    def _restyle(self) -> None:
        fg = self._fg()
        self.setStyleSheet(
            f"#ttbGroup {{ color: {fg}; font-weight: 700; background: transparent; }}"
            f"#ttbArrow {{ color: {fg}; border: none; background: transparent; }}"
            "#ttbArrow::menu-indicator { image: none; width: 0; }"
        )
        for b in self._tab_btns:
            idx = b.property("tabIndex")
            # Which tab you are on is a white pill and a bold weight. Said as a
            # word rather than by making the buttons checkable: a checked
            # QToolButton draws itself sunken under Fusion, and the pill is
            # pixel-matched to Winbox 4.
            a11y.state(b, "selected", idx == self._active)
            if idx == self._active:
                # Winbox-4 selected tab: a raised white pill with dark text.
                b.setStyleSheet(
                    "QToolButton#ttbTab { color:#0b1220; background:#ffffff;"
                    f" border:1px solid {style.BORDER}; border-radius:4px;"
                    " padding:0 10px; font-weight:700; }"
                )
            else:
                b.setStyleSheet(
                    f"QToolButton#ttbTab {{ color:{fg}; background:transparent;"
                    " border:none; padding:0 10px; font-weight:600; }"
                    "QToolButton#ttbTab:hover { text-decoration: underline; }"
                )
