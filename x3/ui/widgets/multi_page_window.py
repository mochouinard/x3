"""MDI window with schema sibling maps as top tabs (Winbox page group).

Example from dhcp.jg::

    folder 'DHCP Client'
      map 'DHCP Client'         path [43,1]
      map 'DHCP Client Options' path [43,5]

Winbox opens one window with two tabs. We do the same.

``QuickSetWindow`` subclasses this to swap the tab strip for a mode dropdown;
construction is split into overridable hooks (``_make_pane`` / ``_build_body``)
so it can create panes lazily rather than all up front.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QAbstractButton, QTabWidget, QVBoxLayout, QWidget

from .. import style
from ..icons import window_icon
from ..settings import load_titlebar_tabs
from .list_pane import ListPane
from .mdi_window import MdiWindow


class MultiPageListWindow(MdiWindow):
    """One MDI subwindow hosting multiple list panes as tabs."""

    def __init__(
        self,
        title: str,
        pages: list[dict[str, Any]],
        *,
        readonly: bool = True,
        make_fetcher: Callable[[tuple[int, ...]], Callable[[], tuple]] | None = None,
        on_open_properties: Callable[..., None] | None = None,
        on_context_action: Callable[..., None] | None = None,
        on_pane_created: Callable[[ListPane], None] | None = None,
        on_open_single: Callable[..., None] | None = None,
        on_open_action: Callable[..., None] | None = None,
        edit_session=None,
        active_path: tuple[int, ...] | None = None,
        active_type_value: int | None = None,
        active_family: str | None = None,
        icon_index: int | None = None,
        show_passwords: bool = False,
        inline_comments: bool = True,
        parent=None,
    ) -> None:
        """
        ``pages`` entries: ``{title, path, kind?, name?}`` with path as tuple/list.
        ``make_fetcher(path, type_value, family, menu_name)`` returns a
        live_fetcher callable for that page.
        ``on_pane_created(pane)`` fires for each pane the window builds — the
        one hook that also catches panes a subclass creates lazily.
        """
        super().__init__(parent)  # MdiWindow: delete-on-close + Esc-to-close
        self.setWindowTitle(title)
        self.setWindowIcon(window_icon("tabs", sprite_index=icon_index))
        self.group_title = title
        self._pages = pages
        self._panes: list[ListPane] = []
        self._edit_session = edit_session
        # Construction params kept as state so a subclass can build panes
        # lazily (and so set_* toggles reach panes born after those toggles).
        self._readonly = readonly
        self._make_fetcher = make_fetcher
        self._on_open_properties = on_open_properties
        self._on_context_action = on_context_action
        self._on_pane_created = on_pane_created
        self._on_open_single = on_open_single
        self._on_open_action = on_open_action
        # Tab index → the page it stands for, in tab-strip order, and the two
        # halves of lazy construction: what is still to be built, and what has
        # been. Interfaces is 17 tabs; building all 17 list panes to show one
        # of them is most of what made the window slow to appear.
        self._tab_pages: list[dict] = []
        self._pending_panes: dict[int, dict] = {}
        self._tab_panes: dict[int, ListPane] = {}
        self._framed_once = False
        self._show_passwords = show_passwords
        self._inline_comments = inline_comments
        if isinstance(active_path, list):
            active_path = tuple(active_path)
        self._active_path = active_path
        # Which tab on that path: every interface type is (20,0), so the path
        # alone names a dozen of them.
        self._active_type_value = active_type_value
        self._active_family = active_family

        self._build_body()
        # Scaled by the active zoom, like every window that names its own
        # opening size -- see ListWindow.
        self.resize(style.scale_px(720), style.scale_px(380))

    # -- construction hooks (overridable) -----------------------------------

    def _page_path(self, page: dict) -> tuple[int, ...] | None:
        path = page.get("path")
        if isinstance(path, list):
            path = tuple(path)
        return path if isinstance(path, tuple) else None

    @staticmethod
    def _page_type_value(page: dict) -> int | None:
        tv = page.get("type_value")
        return tv if isinstance(tv, int) else None

    @staticmethod
    def _page_family(page: dict) -> str | None:
        fam = page.get("family")
        return fam if isinstance(fam, str) and fam else None

    @staticmethod
    def _page_menu_name(page: dict) -> str | None:
        """The .jg ``name`` of the menu a page shows.

        Tabs of one window can share a path without sharing a type: System ▸
        Resources is (24,2) and so are its CPU list and About form. The name
        is what tells them apart.
        """
        name = page.get("name")
        return name if isinstance(name, str) and name else None

    def _ensure_pane(self, index: int) -> ListPane | None:
        """Build the pane behind a tab, once.

        Called for the tab the window opens on and then for each tab as it is
        first selected, so a 17-tab window costs one list pane instead of 17.
        """
        pane = self._tab_panes.get(index)
        if pane is not None:
            return pane
        page = self._pending_panes.pop(index, None)
        if page is None:
            return None
        pane = self._make_pane(page)
        if pane is None:
            return None
        self._tab_panes[index] = pane
        holder = self.tabs.widget(index)
        layout = holder.layout() if holder is not None else None
        if layout is not None:
            layout.addWidget(pane)
        # Each tab carries its own buttons, so a pane built later gets the ones
        # that belong to *its* tab -- not the whole window's.
        self._install_tab_actions(pane, page)
        return pane

    def _on_tab_changed(self, index: int) -> None:
        self._ensure_pane(index)
        self._frame_tab_strip()

    def _make_pane(self, page: dict) -> ListPane | None:
        """Build one list pane for a page dict, register it, fire the hook."""
        path = self._page_path(page)
        if path is None:
            return None
        ptitle = page.get("title") or page.get("name") or "Page"
        type_value = page.get("type_value")
        type_value = type_value if isinstance(type_value, int) else None
        family = page.get("family")
        family = family if isinstance(family, str) and family else None
        menu_name = self._page_menu_name(page)
        fetcher = (
            self._make_fetcher(path, type_value, family, menu_name)
            if self._make_fetcher else None
        )
        pane = ListPane(
            ptitle,
            readonly=self._readonly,
            live_fetcher=fetcher,
            path=path,
            # 13 Interface tabs share path (20,0), so panes must be
            # identified by path *and* type value (or, for a family base such
            # as WiFi Interfaces, the family it lists).
            type_value=type_value,
            family=family,
            menu_name=menu_name,
            on_open_properties=self._on_open_properties,
            on_context_action=self._on_context_action,
            edit_session=self._edit_session,
            show_passwords=self._show_passwords,
            inline_comments=self._inline_comments,
        )
        self._panes.append(pane)
        if self._on_pane_created is not None:
            try:
                self._on_pane_created(pane)
            except Exception:  # noqa: BLE001
                pass
        return pane

    def _build_body(self) -> None:
        """Default body: one tab per page (all built up front)."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # No documentMode: it paints a stray base line above the tab strip.
        self.tabs = QTabWidget()
        self.tabs.setMovable(False)
        layout.addWidget(self.tabs)

        active_index = 0
        # 0 = nothing matched yet, 1 = a tab on the right path, 2 = the right
        # tab. A dozen tabs share one path, so a path-only hit is a fallback.
        matched = 0
        for page in self._pages:
            # 'item' is a single-object form (Interfaces ▸ Detect Internet), not
            # a list tab. Winbox keeps those out of the tab strip and offers
            # them as a button on the tab they were declared under, which is
            # what `actions` on that tab's page already says -- so here the
            # item is simply not a tab.
            if page.get("kind") == "item":
                continue
            path = self._page_path(page)
            if path is None:
                continue
            ptitle = page.get("title") or page.get("name") or f"Page {self.tabs.count() + 1}"
            # Each tab starts as an empty holder and gets its pane the first
            # time it is looked at (see _ensure_pane).
            holder = QWidget()
            hlay = QVBoxLayout(holder)
            hlay.setContentsMargins(0, 0, 0, 0)
            hlay.setSpacing(0)
            index = self.tabs.count()
            if self._active_path is not None and path == self._active_path:
                rank = 2 if (
                    self._page_type_value(page) == self._active_type_value
                    and self._page_family(page) == self._active_family
                ) else 1
                if rank > matched:
                    active_index = index
                    matched = rank
            self._tab_pages.append(page)
            self._pending_panes[index] = page
            self.tabs.addTab(holder, ptitle)

        if self.tabs.count():
            self.tabs.setCurrentIndex(min(active_index, self.tabs.count() - 1))
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._ensure_pane(self.tabs.currentIndex())

        self.setWidget(container)

        self.register_tab_strip(
            self.tabs, self.group_title,
            on_open_single=(
                self._open_tab_in_window if self._on_open_single is not None else None
            ),
        )
        self.set_titlebar_tabs(load_titlebar_tabs())

    def _frame_tab_strip(self) -> None:
        """Frame the overflowing tab strip around the current tab: the tab
        *before* it leads, then the current tab, then whatever follows.

        Qt's own ``makeVisible()`` parks the selection at the right edge, so
        picking a tab in the middle of a long strip (Interfaces ▸ VRRP) hides
        every tab after it and the strip looks reshuffled. ``QTabBar`` exposes
        no scroll offset, but its scroll buttons do the two moves we need: the
        right one steps forward a tab, and the left one *parks* the tab it
        reveals at the left edge. So overshoot right past the anchor, then walk
        back -- the click that brings the anchor into view leaves it leading.
        Mirrors :meth:`TitleTabBar._ensure_active_visible`.
        """
        tabs = getattr(self, "tabs", None)  # QuickSet's body has no tab strip
        bar = tabs.tabBar() if tabs is not None else None
        if bar is None or not bar.isVisible():
            return  # hidden strip (tabs live in the title bar) or not shown yet
        rb = bar.findChild(QAbstractButton, "ScrollRightButton")
        lb = bar.findChild(QAbstractButton, "ScrollLeftButton")
        if rb is None or lb is None:
            return
        anchor = max(0, tabs.currentIndex() - 1)
        if bar.tabRect(anchor).left() >= 0 and (
            anchor == 0 or bar.tabRect(anchor - 1).left() < 0
        ):
            return  # already leading the visible run

        def step(btn, forward: bool) -> None:
            for _ in range(bar.count()):
                left = bar.tabRect(anchor).left()
                if (left < 0) if forward else (left >= 0):
                    return
                if not (btn.isVisible() and btn.isEnabled()):
                    return  # scrolled to the end: as close as the strip allows
                btn.click()
                now = bar.tabRect(anchor).left()
                if (now >= left) if forward else (now <= left):
                    return  # clamped, no progress

        step(rb, True)
        step(lb, False)

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        # The tab bar has no scroll state until it is laid out, so frame the
        # initially active tab (menu tree ▸ Interfaces ▸ VRRP) once shown.
        if not self._framed_once and getattr(self, "tabs", None) is not None:
            self._framed_once = True
            QTimer.singleShot(0, self._frame_tab_strip)

    def _install_tab_actions(self, pane: ListPane, page: dict) -> None:
        """Put this tab's own buttons on its toolbar, next to Add/Remove/
        Enable/Comment.

        Winbox v3 gives every tab of a window its own set: Interface has Detect
        Internet, Ethernet has Power Cycle, LTE has Modem and Antenna Scan. The
        schema decides which -- see :func:`menu_tree._page_payload` -- and each
        one is a menu, so opening it is the same dispatch a nav click makes.
        """
        if self._on_open_action is None:
            return
        for act in page.get("actions") or []:
            title = act.get("title") or "…"
            pane.add_toolbar_action(
                title,
                f"Open {title}",
                lambda pl=dict(act): self._on_open_action(pl),
            )

    def _open_tab_in_window(self, idx: int) -> None:
        if not (0 <= idx < len(self._tab_pages)) or self._on_open_single is None:
            return
        pane = self._ensure_pane(idx)
        if pane is None:
            return
        self._on_open_single(
            pane.path,
            getattr(pane, "type_value", None),
            self.tabs.tabText(idx),
            getattr(pane, "family", None),
            # The tab keeps its own buttons in a window of its own.
            self._tab_pages[idx].get("actions") or [],
        )

    # -- public API ---------------------------------------------------------

    def select_path(
        self,
        path: tuple[int, ...],
        type_value: int | None = None,
        family: str | None = None,
    ) -> None:
        """Raise the tab a nav click asked for.

        Up to a dozen tabs share one path (every interface type is (20,0)), so
        the type -- or the family, for a base tab like WiFi Interfaces -- is
        what tells them apart.
        """
        for i, page in enumerate(self._tab_pages):
            if (
                self._page_path(page) == path
                and self._page_type_value(page) == type_value
                and self._page_family(page) == family
            ):
                self.tabs.setCurrentIndex(i)
                self._ensure_pane(i)
                return
        # Fall back to the first tab on that path when no type was requested.
        for i, page in enumerate(self._tab_pages):
            if self._page_path(page) == path:
                self.tabs.setCurrentIndex(i)
                self._ensure_pane(i)
                return

    def closeEvent(self, event) -> None:  # noqa: N802
        # Child panes get no closeEvent of their own, so retire their workers
        # here — otherwise a pane whose fetch is still running (QuickSet's slow
        # per-mode reads) is destroyed with the window and aborts the process
        # (SIGABRT). Deferred/never-shown panes have no worker, so this is cheap.
        for pane in self._panes:
            try:
                pane.shutdown()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)

    def panes(self) -> list[ListPane]:
        return list(self._panes)

    def set_readonly(self, readonly: bool) -> None:
        self._readonly = readonly
        for pane in self._panes:
            pane.set_readonly(readonly)

    def set_show_passwords(self, show: bool) -> None:
        self._show_passwords = show
        for pane in self._panes:
            pane.set_show_passwords(show)

    def set_inline_comments(self, inline: bool) -> None:
        self._inline_comments = inline
        for pane in self._panes:
            pane.set_inline_comments(inline)

    def active_pane(self) -> ListPane | None:
        return self._tab_panes.get(self.tabs.currentIndex())

    def focus_target(self):
        """The pane on the visible tab, not the holder Qt put it in."""
        return self.active_pane() or super().focus_target()
