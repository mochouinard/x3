"""Left navigation — flat top-level list + flyout popup (no tree expand).

Top-level entries stay collapsed. Clicking a folder opens a popup panel to
the right (Winbox-ish cascade) instead of expanding the tree in place.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import menu_usage, settings, style
from ..i18n import tr
from ..icons import icon_for_kind, icon_from_index, icon_size


#: Nav kinds that open something when clicked. query/action/doit are the
#: command menus (Ping, Torch, Reboot) -- they open a run window, not a list.
#: ``new_session`` is the synthetic "New x3" entry (opens a connect dialog).
_CLICKABLE_KINDS = (
    "map", "item", "list", "dialog", "terminal", "query", "action", "doit",
    "new_session",
    # Tools ▸ Telnet. MainWindow has opened these since they were parsed, but
    # a click never got that far: the tree dropped the kind it did not know.
    "termopen",
)

#: Item roles on flyout rows: mark the "most used" duplicates and the divider so
#: the plain itemClicked path skips them (they are handled by their widgets).
_ROLE_ROWTYPE = Qt.ItemDataRole.UserRole + 1

#: True on the rows that open a submenu -- the only ones that get an arrow.
_ROLE_CARET = Qt.ItemDataRole.UserRole + 2

#: The submenu arrow and the gap it keeps on either side of itself.
_CARET = "\u203a"
_CARET_PAD = 6


def _caret_span(fm: QFontMetrics) -> int:
    """Width the right-aligned caret reserves on a row."""
    return fm.horizontalAdvance(_CARET) + 2 * _CARET_PAD


def _action_payload(node) -> dict:
    """One toolbar button of a window, as a payload ``open_menu`` can dispatch.

    An `item` opens its form, a `map` its list, a query/action/doit its run
    window -- all of which :meth:`MainWindow._dispatch_menu` already does, so a
    button is nothing more than the menu it stands for.
    """
    return {
        "kind": node.kind,
        "path": node.path,
        "title": node.title,
        # The .jg `name`, which is what tells apart the menus that share a
        # handler path: six live on (20,17) and only the name is unique among
        # them -- Bridge's is *titled* "Settings" and Connection Tracking's
        # "Tracking", so the tree label will not do.
        "name": getattr(node, "name", None),
        "id": node.node_id,
        "type_value": node.type_value,
        "family": node.generic,
        "icon": node.icon,
    }


def _page_payload(child: dict, window_actions: list[dict]) -> dict:
    """One page of a menu group, with the buttons that belong on its toolbar.

    Winbox v3 gives every tab its own toolbar: the buttons a tab shows are the
    ones the schema declares after it (Ethernet ▸ Power Cycle). The ones a
    folder declares before its first map belong to the window itself -- which
    is its first page, never all of them (see :func:`_page_group`).
    """
    return {
        "title": child.get("title"),
        "path": child.get("path"),
        "kind": child.get("kind"),
        "name": child.get("name"),
        "id": child.get("id"),
        "type_value": child.get("type_value"),
        "family": child.get("family"),
        "actions": list(window_actions) + list(child.get("actions") or []),
    }


def _page_group(
    title: str, children: list[dict], window_actions: list[dict] | None = None
) -> dict | None:
    """The one window a folder's openable children make, or None.

    The `.jg` says which window a menu opens purely by order: the folder's
    *first* openable child is the window, and every later sibling is reached
    from it. What "reached from it" means follows from the first child's own
    kind, and both shapes are Winbox v3's:

    * a `map` first -- the window is a list, and its siblings are its tabs
      (DHCP Client + DHCP Client Options).
    * an `item` first -- the window is that form, and its siblings are buttons
      down its right-hand side (System ▸ Resources is the Resources form, with
      Hardware / CPU / IRQ beside it; IP ▸ DNS is the Settings form, with
      Static / Cache / Adlist / Forwarders).

    Pages come back in `.jg` order with the window first, so the caller reads
    ``pages[0]`` to know which of the two it is holding. The group is stamped
    on the children it covers -- all of them for a list window, since reaching
    any tab from the tree opens the same window on that tab, but only the form
    for a form window, because its siblings open windows of their own.
    """
    if any(c.get("kind") == "folder" and c.get("children") for c in children):
        return None
    # `hide:1` children are not pages: Winbox reaches those from a button
    # inside the window (Interface List ▸ Lists), which is why the Interface
    # window shows 17 tabs and not 23.
    openable = [
        c for c in children
        if c.get("kind") in ("map", "item", "list")
        and c.get("path") is not None and not c.get("hidden")
    ]
    if len(openable) < 2:
        return None
    primary = openable[0]
    pages = [
        # The window's own buttons go to the window, which is the first page.
        _page_payload(c, list(window_actions or []) if c is primary else [])
        for c in openable
    ]
    group = {"title": title, "pages": pages, "id": f"group:{title}"}
    for c in (openable if primary.get("kind") != "item" else [primary]):
        c["page_group"] = group
    return group


def visible_children(payload: dict) -> list[dict]:
    """A folder's children minus the ``hide:1`` ones.

    Winbox does not navigate to hidden nodes; it surfaces them from the page
    they belong to. IP → Cloud is the example: Back To Home Users and Files sit
    in the Cloud page's own Configuration panel, not in the menu tree.
    """
    return [c for c in (payload.get("children") or []) if not c.get("hidden")]


def _lone_child(payload: dict) -> dict | None:
    """The single menu a folder stands for, or None if it stands for several.

    The `.jg` wraps a lot of one-list sections in a folder of their own: IP →
    Addresses holds nothing but Address, ARP nothing but ARP List, System →
    License nothing but License — 19 menus on a 7.21 device. Opening a flyout
    with one row in it reads as a dead end, so those folders behave as the leaf
    they describe, which is also what Winbox shows. A folder with real siblings
    (or a page group) is left alone.
    """
    seen = 0
    while payload.get("kind") == "folder" and not payload.get("page_group"):
        # Only the children the user can actually see. IP → Cloud wraps one
        # visible page plus two `hide:1` siblings (Back To Home Users/Files,
        # reached from the Cloud page itself, never from the tree). Counting
        # those made the folder look like a real branch, so clicking Cloud
        # opened a flyout instead of the page Winbox shows.
        kids = visible_children(payload)
        if len(kids) != 1 or kids[0].get("kind") not in _CLICKABLE_KINDS:
            break
        if kids[0].get("path") is None:
            break
        payload = kids[0]
        seen += 1
        if seen > 8:  # nav is device-supplied; do not loop on a malformed one
            break
    return payload if seen else None


def _opens_submenu(payload: dict) -> bool:
    """True when clicking this row opens a nested flyout.

    Winbox v3 draws the arrow only on the entries that lead somewhere deeper,
    so it has to mean exactly what :meth:`MenuTree._handle` does: a folder that
    stands for one menu (IP → Addresses) or one whose maps open as tabs of a
    single window (IP → DHCP Client) opens a window, not a submenu, and gets no
    arrow.
    """
    if payload.get("kind") != "folder" or not payload.get("children"):
        return False
    if payload.get("page_group") or _lone_child(payload) is not None:
        return False
    return True


def _click_target(payload: dict) -> dict | None:
    """What activating a leaf should open: its tab group, or the leaf itself."""
    pg = payload.get("page_group")
    if pg and len(pg.get("pages") or []) >= 2:
        path = payload.get("path")
        if isinstance(path, list):
            path = tuple(path)
        return {
            "kind": "page_group",
            "title": pg["title"],
            "id": pg["id"],
            "pages": pg["pages"],
            "active_path": path,
        }
    return payload if payload.get("kind") in _CLICKABLE_KINDS else None


def _open_id(payload: dict) -> str | None:
    """The id of what activating this nav item actually opens.

    Most-used tracking keys on this rather than the item's own id, so the key
    matches whether the item is reached directly or through a one-list folder
    (IP → Addresses opens the Address map). ``None`` for a folder that only
    opens a deeper flyout — those are not "most used" leaves.
    """
    lone = _lone_child(payload)
    if lone is not None:
        t = _click_target(lone)
        return (t or {}).get("id") if t else None
    kind = payload.get("kind")
    kids = payload.get("children") or []
    if kids and kind == "folder":
        pg = payload.get("page_group")
        return pg["id"] if pg else None
    t = _click_target(payload)
    return (t or {}).get("id") if t else None


def _qicon(payload: dict):
    """The icon a flyout row should show (its own, or its lone child's)."""
    icon = payload.get("icon")
    lone = _lone_child(payload)
    if icon is None and lone is not None:
        icon = lone.get("icon")
    if icon is not None:
        return icon_from_index(icon)
    kind = payload.get("kind")
    eff = lone.get("kind") if lone is not None else kind
    k = "list" if eff in ("map", "list") else (eff or "default")
    return icon_for_kind(k)


def menu_label(payload: dict) -> str:
    """What the menu shows for an entry: its label, which can differ from the
    caption of the window it opens (NavNode.label: 7.9's IP ▸ Addresses opens
    "Address List"). In the active language, when it has the word."""
    return tr(str(payload.get("label") or payload.get("title") or ""))


def _payload_from_nav(node, alpha: bool = True) -> dict:
    children = [_payload_from_nav(c, alpha) for c in (node.children or [])]
    # On a folder these are the window's own buttons (declared before its first
    # tab); on a map, the ones that belong to that tab alone.
    actions = [_action_payload(a) for a in (getattr(node, "actions", None) or [])]
    # Build the group from the .jg order FIRST: Winbox's pages follow the
    # schema order, not the alphabetical submenu sort applied to the tree below.
    # (Sorting before this made the tabs alphabetical too.)
    page_group = _page_group(node.title, children, actions)
    if page_group is None and actions:
        # A folder with one openable child opens that child (see _lone_child),
        # so the buttons the folder declared belong to that window: Tools ▸
        # Email is the folder, the Email Settings form is the window, and Send
        # Email is the button on it (System ▸ License ▸ Renew License likewise).
        # Skipped when the button *is* that window -- every "Settings" folder
        # declares one item and nothing else, and a form must not carry a button
        # that reopens itself.
        only = [
            c for c in children
            if c.get("kind") in ("map", "item", "list")
            and c.get("path") is not None and not c.get("hidden")
        ]
        if len(only) == 1:
            spoken = {only[0].get("id")} | {
                a.get("id") for a in (only[0].get("actions") or [])
            }
            only[0]["actions"] = list(only[0].get("actions") or []) + [
                a for a in actions if a.get("id") not in spoken
            ]
    if alpha:
        # Submenus alphabetical (like Winbox) for the tree display; the top level
        # stays on .jg prio because load_nav iterates the root list unsorted.
        children.sort(key=lambda c: menu_label(c).lower())
    return {
        "kind": node.kind,
        "path": node.path,
        # The window's caption. What the menu shows is "label" (menu_label).
        "title": node.title,
        "label": getattr(node, "label", "") or node.title,
        # See _action_payload: the name is the only thing that distinguishes
        # the menus sharing a path, so it has to survive into the payload.
        "name": getattr(node, "name", None),
        "id": node.node_id,
        "children": children,
        "icon": node.icon,
        "page_group": page_group,
        "type_value": node.type_value,
        # Overlay family (Wireless ▸ WiFi Interfaces is every 'wlan' type at
        # once), which is what this node lists instead of one type_value.
        "family": node.generic,
        "hidden": node.hidden,
        # Buttons for the window this node opens on its own (a single map still
        # gets its own: Interfaces ▸ Ethernet in its own window keeps Power
        # Cycle). In a group these are carried by the page that is the window.
        "actions": actions,
    }


class _CaretDelegate(QStyledItemDelegate):
    """Paints the submenu arrow flush against the row's right edge.

    Winbox v3 hangs it off the right side of the nav panel rather than letting
    it trail the label, so it lines up down the column whatever the names are.
    Rows without :data:`_ROLE_CARET` are drawn untouched.
    """

    CARET = _CARET

    def _caret_width(self, option: QStyleOptionViewItem) -> int:
        return _caret_span(QFontMetrics(option.font))

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        # Elide against the strip the caret owns so a long name never runs under
        # it. sizeHint passes a rect with no width -- skip it there.
        if not index.data(_ROLE_CARET) or option.rect.width() <= 0:
            return
        used = self._caret_width(option) + 8
        if not option.icon.isNull():
            used += option.decorationSize.width() + 4
        avail = max(0, option.rect.width() - used)
        option.text = QFontMetrics(option.font).elidedText(
            option.text, Qt.TextElideMode.ElideRight, avail
        )

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        size = super().sizeHint(option, index)
        if index.data(_ROLE_CARET):
            size.setWidth(size.width() + self._caret_width(option))
        return size

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if not index.data(_ROLE_CARET):
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        role = (
            QPalette.ColorRole.HighlightedText
            if opt.state & QStyle.StateFlag.State_Selected
            else QPalette.ColorRole.Text
        )
        rect = QRect(option.rect)
        rect.setRight(rect.right() - _CARET_PAD)
        painter.save()
        painter.setPen(opt.palette.color(QPalette.ColorGroup.Active, role))
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            self.CARET,
        )
        painter.restore()


class _PinnedRow(QWidget):
    """A "most used" flyout row: icon + label (opens on click) and an X that
    drops it from the top group (:func:`menu_usage.remove_from_top`)."""

    activated = Signal(object)   # the row payload
    removed = Signal(str)        # the open id

    def __init__(self, payload: dict, open_id: str, icon, parent=None) -> None:
        super().__init__(parent)
        self._payload = payload
        self._open_id = open_id
        # Transparent so the row still shows the list's hover/selection paint;
        # an opaque widget over the item read as a permanently selected row.
        self.setStyleSheet("QWidget { background: transparent; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(4)
        ic = QLabel()
        if icon is not None:
            ic.setPixmap(icon.pixmap(icon_size()))
        lay.addWidget(ic)
        lbl = QLabel(menu_label(payload) or "?")
        lay.addWidget(lbl, 1)
        x = QToolButton()
        x.setText("✕")
        x.setAutoRaise(True)
        # Kept square and small: the row's width sets the whole flyout's width,
        # and a default-sized tool button pushed the labels into an ellipsis.
        side = max(12, style.ROW_HEIGHT - 3)
        x.setFixedSize(QSize(side, side))
        # Asked of the theme, not of x.font(): a pixel-sized font answers -1
        # to pointSizeF() and this collapsed to the 6pt floor. See style.size_font.
        x.setFont(style.size_font(x.font(), delta_px=-2))
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setToolTip("Remove from the top of this menu")
        x.clicked.connect(lambda: self.removed.emit(self._open_id))
        lay.addWidget(x)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Any press but the X opens the item; the X button consumes its own.
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._payload)
        super().mousePressEvent(event)


class NavFlyout(QFrame):
    """Popup list of submenu items, positioned to the right of the nav."""

    item_chosen = Signal(object)
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setObjectName("navFlyout")
        self.setFrameShape(QFrame.Shape.Box)
        self.setLineWidth(1)
        self._apply_flyout_style()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._title = QLabel("")
        self._title.setObjectName("flyoutTitle")
        lay.addWidget(self._title)
        self._list = QListWidget()
        self._list.setIconSize(icon_size())
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setItemDelegate(_CaretDelegate(self._list))
        self._list.itemClicked.connect(self._on_click)
        self._list.installEventFilter(self)
        lay.addWidget(self._list)
        self._child: NavFlyout | None = None
        self._items: list[dict] = []
        self._pos = QPoint()
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context)

    def _apply_flyout_style(self) -> None:
        self.setStyleSheet(
            f"""
            QFrame#navFlyout {{
                background: {style.BG_PANEL};
                border: 1px solid {style.BORDER_DARK};
            }}
            QListWidget {{
                background: {style.BG_WINDOW};
                border: none;
                outline: none;
                color: {style.TEXT};
            }}
            QListWidget::item {{
                padding: 2px 8px 2px 4px;
            }}
            QListWidget::item:selected {{
                background: {style.SEL_BG};
                color: {style.SEL_FG};
            }}
            QLabel#flyoutTitle {{
                background: {style.BG_HEADER};
                border-bottom: 1px solid {style.BORDER};
                padding: 2px 6px;
                color: {style.TEXT_DIM};
            }}
            """
        )

    def show_items(
        self,
        title: str,
        items: list[dict],
        global_pos: QPoint,
        *,
        max_height: int = 0,
    ) -> None:
        self._apply_flyout_style()
        self._close_child()
        self._title.setText(title)
        self._list.clear()
        self._items = list(items)
        self._pos = global_pos
        fm = QFontMetrics(self.font())

        # "Most used" group: the pinned entries duplicated at the top, each with
        # an X, then a divider. Keyed by open id so IP → Addresses (a one-list
        # folder) matches the map it actually opens.
        by_open_id: dict[str, dict] = {}
        for payload in items:
            oid = _open_id(payload)
            if oid and oid not in by_open_id:
                by_open_id[oid] = payload
        n_pinned = 0
        for oid in menu_usage.most_used(list(by_open_id.keys())):
            payload = by_open_id.get(oid)
            if payload is None:
                continue
            self._add_pinned_item(payload, oid)
            n_pinned += 1
        if n_pinned:
            self._add_divider()

        for payload in items:
            self._add_normal_item(payload)

        # Width from what the rows actually need, not a fudge factor: a pinned
        # row carries an icon and the X on top of its label, a folder row the
        # right-aligned caret. Guessing clipped both into an ellipsis.
        wanted = 0
        for row in range(self._list.count()):
            item = self._list.item(row)
            widget = self._list.itemWidget(item)
            if isinstance(widget, _PinnedRow):
                wanted = max(wanted, widget.sizeHint().width() + 4)
            elif not item.data(_ROLE_ROWTYPE):  # skip the divider
                need = fm.horizontalAdvance(item.text()) + icon_size().width() + 16
                if item.data(_ROLE_CARET):
                    need += _caret_span(fm)
                wanted = max(wanted, need)

        # The popup is a top-level window: only the screen bounds it, so a long
        # menu is shown whole instead of scrolling inside the nav's height.
        avail = self._available_geometry(global_pos)
        cap = min(max_height or avail.height(), avail.height()) - 8
        chrome = (
            self._title.sizeHint().height()
            + 2 * self.frameWidth()
            + 2 * self._list.frameWidth()
        )
        content = chrome + sum(
            self._list.sizeHintForRow(r) for r in range(self._list.count())
        )
        height = max(60, min(cap, content))
        if content > height:  # scrollbar takes its own strip out of the rows
            wanted += self._list.verticalScrollBar().sizeHint().width()
        width = max(140, min(340, wanted + 2))

        self.setFixedSize(width, height)
        self.move(self._fit_on_screen(global_pos, width, height, avail))
        self.show()
        self.raise_()
        self._list.setFocus()

    @staticmethod
    def _available_geometry(near: QPoint) -> QRect:
        screen = QGuiApplication.screenAt(near) or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1024, 768)

    @staticmethod
    def _fit_on_screen(pos: QPoint, w: int, h: int, avail: QRect) -> QPoint:
        """Slide the popup back onto the screen; flip left if it would hang off
        the right edge, so a deep cascade stays reachable."""
        x, y = pos.x(), pos.y()
        if x + w > avail.right():
            x = max(avail.left(), avail.right() - w)
        if y + h > avail.bottom():
            y = max(avail.top(), avail.bottom() - h)
        return QPoint(x, max(avail.top(), y))

    def _add_normal_item(self, payload: dict) -> None:
        kind = payload.get("kind", "")
        label = menu_label(payload) or "?"
        # A folder standing for one menu is drawn as that menu: no caret, and
        # the leaf's icon when the folder brought none of its own.
        lone = _lone_child(payload)
        has_kids = bool(visible_children(payload)) and lone is None
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, payload)
        item.setData(_ROLE_CARET, _opens_submenu(payload))
        icon = payload.get("icon")
        if icon is None and lone is not None:
            icon = lone.get("icon")
        if icon is not None:
            item.setIcon(icon_from_index(icon))
        else:
            eff_kind = lone.get("kind") if lone is not None else kind
            k = "folder" if has_kids and kind == "folder" else (
                "list" if eff_kind in ("map", "list") else eff_kind
            )
            item.setIcon(icon_for_kind(k if k != "map" else "list"))
        item.setSizeHint(QSize(0, style.ROW_HEIGHT + 4))
        self._list.addItem(item)

    def _add_pinned_item(self, payload: dict, open_id: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, payload)
        item.setData(_ROLE_ROWTYPE, "pinned")
        row = _PinnedRow(payload, open_id, _qicon(payload))
        row.activated.connect(self._pinned_activated)
        row.removed.connect(self._pinned_removed)
        self._list.addItem(item)
        item.setSizeHint(QSize(0, style.ROW_HEIGHT + 4))
        self._list.setItemWidget(item, row)

    def _add_divider(self) -> None:
        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setData(_ROLE_ROWTYPE, "divider")
        # The app stylesheet gives every list item the same height, so ask for
        # that -- a smaller hint just made the popup's height math wrong.
        item.setSizeHint(QSize(10, style.ROW_HEIGHT + 4))
        holder = QWidget()
        holder.setStyleSheet("QWidget { background: transparent; }")
        box = QVBoxLayout(holder)
        box.setContentsMargins(4, 0, 4, 0)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{style.BORDER}; border:none;")
        box.addWidget(sep)
        self._list.addItem(item)
        self._list.setItemWidget(item, holder)

    def _pinned_activated(self, payload: object) -> None:
        if isinstance(payload, dict):
            self._activate_leaf(payload)

    def _pinned_removed(self, open_id: str) -> None:
        menu_usage.remove_from_top(open_id)
        self.show_items(self._title.text(), self._items, self._pos)

    def _activate_leaf(self, payload: dict) -> None:
        """Open a pinned row's target — the leaf cases of _on_click (a pinned
        row is never a nested folder, so no deeper flyout is needed)."""
        lone = _lone_child(payload)
        if lone is not None:
            target = _click_target(lone)
            if target is not None:
                self.item_chosen.emit(target)
            self._close_all()
            return
        kind = payload.get("kind")
        kids = payload.get("children") or []
        if kids and kind == "folder" and payload.get("page_group"):
            pg = payload["page_group"]
            self.item_chosen.emit({
                "kind": "page_group", "title": pg["title"],
                "id": pg["id"], "pages": pg["pages"],
            })
            self._close_all()
            return
        target = _click_target(payload)
        if target is not None:
            self.item_chosen.emit(target)
            self._close_all()

    def _on_context(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        payload = item.data(Qt.ItemDataRole.UserRole) or {}
        oid = _open_id(payload)
        if not oid:
            return
        menu = QMenu(self)
        if menu_usage.is_pinned(oid):
            menu.addAction("Unpin from top", lambda: self._curate(oid, "unpin"))
        else:
            menu.addAction("Pin to top", lambda: self._curate(oid, "pin"))
        if menu_usage.is_excluded(oid):
            menu.addAction("Allow at top", lambda: self._curate(oid, "include"))
        else:
            menu.addAction("Never show at top", lambda: self._curate(oid, "exclude"))
        menu.exec(self._list.viewport().mapToGlobal(pos))

    def _curate(self, open_id: str, what: str) -> None:
        if what == "pin":
            menu_usage.set_pinned(open_id, True)
        elif what == "unpin":
            menu_usage.set_pinned(open_id, False)
        elif what == "exclude":
            menu_usage.set_excluded(open_id, True)
        elif what == "include":
            menu_usage.set_excluded(open_id, False)
        self.show_items(self._title.text(), self._items, self._pos)

    def _on_click(self, item: QListWidgetItem) -> None:
        if item.data(_ROLE_ROWTYPE):  # pinned/divider rows are widget-handled
            return
        payload = item.data(Qt.ItemDataRole.UserRole) or {}
        # Folder wrapping a single menu → open it, rather than a one-row flyout
        lone = _lone_child(payload)
        if lone is not None:
            target = _click_target(lone)
            if target is not None:
                self.item_chosen.emit(target)
            self._close_all()
            return
        kids = payload.get("children") or []
        kind = payload.get("kind")
        # Folder that is only sibling maps → open multi-page window (no deeper flyout)
        if kids and kind == "folder" and payload.get("page_group"):
            self.item_chosen.emit({
                "kind": "page_group",
                "title": payload["page_group"]["title"],
                "id": payload["page_group"]["id"],
                "pages": payload["page_group"]["pages"],
            })
            self._close_all()
            return
        if kids and kind == "folder":
            # Nested flyout further to the right
            rect = self._list.visualItemRect(item)
            pos = self._list.viewport().mapToGlobal(rect.topRight())
            pos.setX(pos.x() + 2)
            if self._child is None:
                self._child = NavFlyout(self)
                self._child.item_chosen.connect(self.item_chosen.emit)
            self._child.show_items(payload.get("title") or "", kids, pos)
            return
        target = _click_target(payload)
        if target is not None:
            self.item_chosen.emit(target)
            self._close_all()

    def _close_child(self) -> None:
        if self._child is not None:
            self._child._close_child()
            self._child.hide()

    def _close_all(self) -> None:
        self._close_child()
        self.hide()
        self.dismissed.emit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Enter (or Right) opens the row typing landed on; Left goes back.

        Typing a name already moves the current row -- the list's own search --
        but only a click opened it, so IP ▸ "serv" ▸ Enter did nothing.
        """
        if obj is self._list and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            item = self._list.currentItem()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and item is not None:
                self._on_click(item)
                return True
            if key == Qt.Key.Key_Right and item is not None:
                payload = item.data(Qt.ItemDataRole.UserRole) or {}
                if payload.get("children") and _lone_child(payload) is None:
                    self._on_click(item)
                    return True
            if key == Qt.Key.Key_Left and isinstance(self.parent(), NavFlyout):
                self.hide()
                self.parent()._list.setFocus()
                return True
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._close_child()
        super().hideEvent(event)


class MenuTree(QTreeWidget):
    """Flat top-level nav; folders open a right-side flyout instead of expanding."""

    menu_activated = Signal(object)
    #: A printable key typed while the tree has the keyboard: the main window
    #: hands it to the menu search, which reaches every entry, not only the
    #: top-level ones the tree's own type-ahead can jump between.
    type_ahead = Signal(str)
    #: Set by whoever listens to ``type_ahead``; off, the tree keeps Qt's own.
    search_takes_typing = False

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Styled as chrome (see QTreeWidget#navTree in style.py): grey frame
        # background, no border. NoFrame also covers the unstyled case, so the
        # strip never comes back as a sunken white well.
        self.setObjectName("navTree")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setAnimated(False)
        self.setIndentation(0)  # flat list — no tree indent
        self.setIconSize(icon_size())
        self.setRootIsDecorated(False)  # no expand arrows
        self.setExpandsOnDoubleClick(False)
        self.setItemsExpandable(False)
        self.setMinimumWidth(style.TREE_WIDTH)
        self.setMaximumWidth(style.scale_px(220))
        self.setAlternatingRowColors(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # One column pinned to the viewport width: the caret is drawn against
        # the row's right edge, so the row has to end where the panel does.
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(_CaretDelegate(self))
        self.itemClicked.connect(self._on_clicked)
        self.itemActivated.connect(self._on_activated)
        self._flyout: NavFlyout | None = None

    def load_nav(self, nodes: list) -> None:
        self.clear()
        self._close_flyout()
        alpha = settings.load_submenu_order() == "alphabetical"
        for n in nodes:
            self._add_top(_payload_from_nav(n, alpha))

    def refresh_metrics(self) -> None:
        """Re-read the sizes the theme publishes, after a zoom or a skin change.

        Only the icon edge and the width band: those are set on the widget, so
        they hold whatever the nav was built at, while the row height and the
        type come from the stylesheet and follow on their own. The flyout is
        closed rather than re-measured -- its rows carry hand-set size hints,
        and it is a popup the next click rebuilds anyway.
        """
        self._close_flyout()
        self.setIconSize(icon_size())
        self.setMinimumWidth(style.TREE_WIDTH)
        self.setMaximumWidth(style.scale_px(220))

    def refresh_icons(self) -> None:
        """Re-read icons after the icon source changes."""
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item is None:
                continue
            payload = item.data(0, Qt.ItemDataRole.UserRole) or {}
            lone = _lone_child(payload)
            kids = visible_children(payload) if lone is None else []
            kind = payload.get("kind")
            icon = payload.get("icon")
            if icon is None and lone is not None:
                icon = lone.get("icon")
            if icon is not None:
                item.setIcon(0, icon_from_index(icon))
            else:
                eff_kind = lone.get("kind") if lone is not None else kind
                k = "folder" if kids and kind == "folder" else (
                    "list" if eff_kind in ("map", "list") else eff_kind or "default"
                )
                item.setIcon(0, icon_for_kind(k if k != "map" else "list"))

    def _add_top(self, payload: dict) -> None:
        title = menu_label(payload) or "?"
        lone = _lone_child(payload)
        kids = visible_children(payload) if lone is None else []
        kind = payload.get("kind")
        item = QTreeWidgetItem([title])
        item.setData(0, Qt.ItemDataRole.UserRole, payload)
        item.setData(0, _ROLE_CARET, _opens_submenu(payload))
        icon = payload.get("icon")
        if icon is None and lone is not None:
            icon = lone.get("icon")
        if icon is not None:
            item.setIcon(0, icon_from_index(icon))
        else:
            eff_kind = lone.get("kind") if lone is not None else kind
            k = "folder" if kids and kind == "folder" else (
                "list" if eff_kind in ("map", "list") else eff_kind or "default"
            )
            item.setIcon(0, icon_for_kind(k if k != "map" else "list"))
        self.addTopLevelItem(item)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        text = event.text()
        mods = event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier
        if (
            text and text.isprintable() and not text.isspace()
            and mods == Qt.KeyboardModifier.NoModifier
            and self.search_takes_typing
        ):
            self.type_ahead.emit(text)
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        self._handle(item)

    def _on_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self._handle(item)

    def _handle(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, Qt.ItemDataRole.UserRole) or {}
        # Folder wrapping a single menu → open it, rather than a one-row flyout
        lone = _lone_child(payload)
        if lone is not None:
            self._close_flyout()
            target = _click_target(lone)
            if target is not None:
                self.menu_activated.emit(target)
            return
        kids = payload.get("children") or []
        kind = payload.get("kind")
        # Top-level folder that is a pure map group → multi-page window
        if kids and kind == "folder" and payload.get("page_group"):
            self._close_flyout()
            pg = payload["page_group"]
            self.menu_activated.emit({
                "kind": "page_group",
                "title": pg["title"],
                "id": pg["id"],
                "pages": pg["pages"],
            })
            return
        if kids and kind == "folder":
            self._show_flyout(item, payload)
            return
        self._close_flyout()
        target = _click_target(payload)
        if target is not None:
            self.menu_activated.emit(target)

    def _show_flyout(self, item: QTreeWidgetItem, payload: dict) -> None:
        # Hidden nodes are reached from the page that owns them, not the tree.
        kids = visible_children(payload) or (payload.get("children") or [])
        if not kids:
            return
        if self._flyout is None:
            self._flyout = NavFlyout(self.window())
            self._flyout.item_chosen.connect(self._on_flyout_chosen)
        # Position at the right edge of this item
        rect: QRect = self.visualItemRect(item)
        top_right = self.viewport().mapToGlobal(rect.topRight())
        # Prefer just outside the tree widget
        tree_right = self.mapToGlobal(self.rect().topRight())
        x = max(top_right.x() + 2, tree_right.x() + 2)
        pos = QPoint(x, top_right.y())
        self._flyout.show_items(payload.get("title") or "", kids, pos)

    def _on_flyout_chosen(self, payload: object) -> None:
        self._close_flyout()
        if isinstance(payload, dict):
            self.menu_activated.emit(payload)

    def _close_flyout(self) -> None:
        if self._flyout is not None:
            self._flyout.hide()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._close_flyout()
        super().hideEvent(event)
