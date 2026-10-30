"""MDI child: single list/table window."""

from __future__ import annotations

from typing import Callable

from .. import style
from ..icons import window_icon
from .list_pane import ListPane
from .mdi_window import MdiWindow, mdi_area_of


class ListWindow(MdiWindow):
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
        on_open_action: Callable[..., None] | None = None,
        actions: list[dict] | None = None,
        edit_session=None,
        icon_index: int | None = None,
        show_passwords: bool = False,
        inline_comments: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)  # MdiWindow: delete-on-close + Esc-to-close
        self.setWindowTitle(title)
        self.setWindowIcon(window_icon("list", sprite_index=icon_index))
        self.path = path
        self.pane = ListPane(
            title,
            readonly=readonly,
            live_fetcher=live_fetcher,
            path=path,
            # Interface-type windows all share path (20,0); without the type
            # the pane cannot key its saved columns or prefill an Add.
            type_value=type_value,
            family=family,
            # ...and where two lists share a path with no type between them
            # (Web Proxy ▸ Connections and the Settings form are both (96,1)),
            # the .jg name is what says which one this is.
            menu_name=menu_name,
            on_open_properties=on_open_properties,
            on_context_action=on_context_action,
            edit_session=edit_session,
            show_passwords=show_passwords,
            inline_comments=inline_comments,
        )
        # This menu's own toolbar buttons: the sibling forms and commands the
        # schema declares under it (Ethernet ▸ Power Cycle). A tab opened in a
        # window of its own keeps the buttons it had in the tab strip.
        if on_open_action is not None:
            for act in actions or []:
                label = act.get("title") or "…"
                self.pane.add_toolbar_action(
                    label,
                    f"Open {label}",
                    lambda pl=dict(act): on_open_action(pl),
                )
        self.setWidget(self.pane)
        # Scaled by the active zoom: bigger rows want a bigger window, or a
        # list that showed twenty rows at 100% shows ten.
        self.resize(style.scale_px(640), style.scale_px(320))
        self._opened_size = self.size()
        self.pane.columns_fitted.connect(self._fit_to_columns)

    def _fit_to_columns(self, need: int) -> None:
        """Widen once, on the first load, to show the columns the list opens with.

        640px is a guess made before any column is known; Routes declares
        Dst. Address 100 + Gateway 300 + Distance + Routing Table + Pref.
        Source, and opened with the last one behind a scroll bar. Capped at
        the area, kept centred, and never after the user has sized it.
        """
        self.pane.columns_fitted.disconnect(self._fit_to_columns)
        grow = need - self.pane.table.viewport().width()
        area = mdi_area_of(self)
        if grow <= 0 or area is None or self.size() != self._opened_size:
            return
        room = area.viewport().width()
        width = min(self.width() + grow, room)
        if width <= self.width():
            return
        x = max(0, min(self.x() - (width - self.width()) // 2, room - width))
        self.setGeometry(x, self.y(), width, self.height())

    def focus_target(self):
        return self.pane

    def closeEvent(self, event) -> None:  # noqa: N802
        # A pane is a plain child widget: it gets no closeEvent of its own, so
        # a fetch still in flight would be running against a destroyed widget.
        # MultiPageListWindow does the same for its tabs.
        try:
            self.pane.shutdown()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)
