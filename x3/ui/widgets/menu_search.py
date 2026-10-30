"""Search every menu entry by name, from a box on the toolbar or by typing.

The left-hand tree is a flyout menu: a nested entry -- IP ▸ Services, a DHCP
Client tab -- is two or three clicks away, and the tree's own type-ahead only
reaches its top level. This finds any entry by any word of its path ("dhcp
opt") and opens it with Enter.

The result list is a child of the main window laid over its content, not a
popup window: a popup takes the keyboard from the box being typed in, and on
Wayland a plain top-level window cannot be placed under it at all.
"""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QLineEdit, QListWidget, QListWidgetItem, QSizePolicy, QWidget,
)

from .. import style
from .menu_tree import _click_target, _lone_child, menu_label, visible_children

#: How many matches the list shows; past that, keep typing.
MAX_RESULTS = 20

#: Between the parts of an entry's path, as the flyouts read.
SEP = " ▸ "


def menu_entries(roots: Iterable[dict]) -> list[tuple[list[str], dict]]:
    """Every openable entry under the tree's top-level payloads, with its path.

    Mirrors what a click does (see ``MenuTree._handle``): a folder wrapping a
    single menu is that menu, and a tab group opens as one window. The tabs of
    a group are listed as entries of their own too -- each opens the group on
    that tab -- since "DHCP Client Options" is what somebody looking for it
    will type.
    """
    out: list[tuple[list[str], dict]] = []
    seen: set[tuple[str, ...]] = set()

    def walk(payload: dict, crumbs: list[str]) -> None:
        # Listed under the name the menu shows (IP ▸ Addresses), which on some
        # firmware is not the caption of the window it opens (Address List).
        title = menu_label(payload).strip()
        if not title:
            return
        path = crumbs + [title]
        lone = _lone_child(payload)
        target = _click_target(lone) if lone is not None else _click_target(payload)
        if target is not None and tuple(path) not in seen:
            seen.add(tuple(path))
            out.append((path, target))
        if lone is None:
            for child in visible_children(payload):
                walk(child, path)

    for root in roots:
        if isinstance(root, dict):
            walk(root, [])
    return out


def rank(
    entries: list[tuple[list[str], dict]],
    query: str,
    usage: dict[str, int] | None = None,
) -> list[tuple[list[str], dict]]:
    """The entries every word of ``query`` appears in, best first.

    Best is the entry whose own name starts with what was typed, then one with
    a word starting with it, then one merely containing it. Within a kind, the
    menu opened most often comes first -- "serv" is IP ▸ Services for whoever
    lives in it, not Dot1X ▸ Server -- then shallower and shorter.
    """
    usage = usage or {}
    words = query.lower().split()
    if not words:
        return []
    scored = []
    for path, target in entries:
        hay = " ".join(path).lower()
        if not all(w in hay for w in words):
            continue
        leaf = path[-1].lower()
        whole = " ".join(words)
        if leaf.startswith(whole):
            score = 0
        elif leaf.startswith(words[0]):
            score = 1
        elif any(part.startswith(words[0]) for part in leaf.split()):
            score = 2
        elif words[0] in leaf:
            score = 3
        else:
            score = 4
        used = usage.get(str(target.get("id") or ""), 0)
        scored.append(
            ((score, -used, len(path), len(leaf), SEP.join(path).lower()), path, target)
        )
    scored.sort(key=lambda s: s[0])
    return [(p, t) for _k, p, t in scored[:MAX_RESULTS]]


class MenuSearch(QLineEdit):
    """The toolbar box. ``chosen`` carries the payload to hand ``open_menu``."""

    chosen = Signal(object)

    def __init__(
        self,
        roots: Callable[[], Iterable[dict]],
        host: QWidget,
        parent: QWidget | None = None,
        usage: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._roots = roots
        self._usage = usage
        self._host = host
        self.setPlaceholderText("Search menus…  (Ctrl+K)")
        self.setClearButtonEnabled(True)
        self.setMaximumWidth(style.scale_px(260))
        self.setMinimumWidth(style.scale_px(160))
        # A fixed slot, not a stretch: the toolbar's one Expanding spacer is
        # what holds Safe Mode against the right edge.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._list = QListWidget(host)
        self._list.setObjectName("menuSearchResults")
        self._list.setFrameShape(QFrame.Shape.Box)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.itemClicked.connect(self._open_item)
        self._list.hide()
        #: A search begun from the nav is under way: hand-offs add to it.
        self._typing = False
        self.textEdited.connect(self._refresh)
        self.installEventFilter(self)

    # --- entry points ---------------------------------------------------

    def start(self, text: str = "") -> None:
        """Take the keyboard, optionally carrying the keys typed elsewhere."""
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        if text:
            # Keys still queued for the tree after the first one moved the
            # keyboard here are part of the same word: "serv", not "v".
            self.setText(self.text() + text if self._typing else text)
            self._typing = True
            self._refresh(self.text())
        else:
            self.selectAll()
            if self.text():
                self._refresh(self.text())

    # --- results --------------------------------------------------------

    def _refresh(self, text: str) -> None:
        hits = rank(menu_entries(self._roots()), text,
                    self._usage() if self._usage is not None else None)
        self._list.clear()
        for path, target in hits:
            item = QListWidgetItem(SEP.join(path))
            item.setData(Qt.ItemDataRole.UserRole, target)
            self._list.addItem(item)
        if not hits:
            if text.strip():
                item = QListWidgetItem("No menu matches")
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._list.addItem(item)
            else:
                self._list.hide()
                return
        self._list.setCurrentRow(0)
        self._place()
        self._list.show()
        self._list.raise_()

    def _place(self) -> None:
        """Under the box, as wide as the longest result allows."""
        below = self.mapTo(self._host, QPoint(0, self.height()))
        rows = max(1, self._list.count())
        row_h = max(self._list.sizeHintForRow(0), style.ROW_HEIGHT + 2)
        width = max(self.width(), min(style.scale_px(520), self._list.sizeHintForColumn(0) + 24))
        x = min(below.x(), max(0, self._host.width() - width - 4))
        self._list.setGeometry(x, below.y() + 1, width, row_h * min(rows, 12) + 4)

    def _open_item(self, item: QListWidgetItem | None) -> None:
        target = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(target, dict):
            return
        self._close()
        self.chosen.emit(target)

    def _close(self) -> None:
        self._typing = False
        self._list.hide()
        self.clear()
        self.clearFocus()

    # --- keys -----------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._list.isVisible():
                step = 1 if key == Qt.Key.Key_Down else -1
                row = self._list.currentRow() + step
                self._list.setCurrentRow(max(0, min(self._list.count() - 1, row)))
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._open_item(self._list.currentItem() if self._list.isVisible() else None)
                return True
            if key == Qt.Key.Key_Escape:
                self._close()
                return True
        if obj is self and event.type() == QEvent.Type.FocusOut:
            # A click in the list arrives after focus left the box: let it
            # land before the list goes away.
            if not self._list.underMouse():
                self._list.hide()
                self._typing = False
        return super().eventFilter(obj, event)
