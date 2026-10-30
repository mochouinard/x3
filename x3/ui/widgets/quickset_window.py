"""QuickSet as a single window with a mode dropdown in the header.

Winbox 4 dropped QuickSet's tab strip for one mode selector in the window's
header: a single form at a time, chosen from a dropdown. We do the same. Only
the selected mode is built and fetched, and panes are created lazily -- so
opening QuickSet (often an accidental click) costs one list read, not twenty,
and the modes offered are filtered to what the box can run (Ethernet-only on a
radioless virtual router, the wireless/LTE modes on hardware that has them).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .list_pane import ListPane
from .wheel_guard import guard_wheel
from .multi_page_window import MultiPageListWindow


class QuickSetWindow(MultiPageListWindow):
    """QuickSet: a mode dropdown over one lazily-built list pane."""

    def _build_body(self) -> None:
        # The mode selector floats in the native title bar (see MdiWindow), so
        # the body is just the selected mode's content -- space-efficient, and
        # identical window chrome to every other list window.
        self.mode_combo = QComboBox()
        for page in self._pages:
            self.mode_combo.addItem(page.get("title") or page.get("name") or "Mode")
        # A roll over it would rebuild the whole window (see wheel_guard).
        guard_wheel(self.mode_combo)
        self.add_title_bar_widget(self.mode_combo)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self._placeholder = QLabel("No QuickSet mode available for this device.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self.stack.addWidget(self._placeholder)

        #: dropdown index -> the pane built for that mode (None until visited)
        self._mode_panes: dict[int, ListPane] = {}

        self.setWidget(container)

        start = self._default_index()
        self.mode_combo.setCurrentIndex(start)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._on_mode_changed(start)

    def _default_index(self) -> int:
        """Open on the requested path, else Ethernet (always applicable)."""
        if self._active_path is not None:
            for i, page in enumerate(self._pages):
                if self._page_path(page) == self._active_path:
                    return i
        for i, page in enumerate(self._pages):
            if (page.get("title") or "").strip().lower() == "ethernet":
                return i
        return 0

    def _on_mode_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._pages):
            self.stack.setCurrentWidget(self._placeholder)
            return
        pane = self._mode_panes.get(index)
        if pane is None:
            pane = self._make_pane(self._pages[index])
            if pane is None:
                self.stack.setCurrentWidget(self._placeholder)
                return
            self._mode_panes[index] = pane
            self.stack.addWidget(pane)
        # Showing the pane fires its showEvent -> deferred first load.
        self.stack.setCurrentWidget(pane)

    # -- API overrides (dropdown/stack replace the tab widget) --------------

    def active_pane(self) -> ListPane | None:
        w = self.stack.currentWidget()
        return w if isinstance(w, ListPane) else None

    def select_path(
        self,
        path: tuple[int, ...],
        type_value: int | None = None,
        family: str | None = None,
    ) -> None:
        for i, page in enumerate(self._pages):
            if self._page_path(page) == path:
                self.mode_combo.setCurrentIndex(i)
                return
