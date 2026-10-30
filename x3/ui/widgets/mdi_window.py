"""Standard MDI subwindow base for x3.

Every child window is a native ``QMdiSubWindow``: the app's proxy style
(:class:`style._DenseMdiStyle`) paints a flat title bar and keeps a resize grab
band via ``PM_MdiSubWindowFrameWidth``, so the border, edge/corner resize, drag,
maximize and per-theme corner radius are all native and free — nothing here
reimplements window management.

What this base adds is the shared setup (delete-on-close, Esc-to-close) and one
reusable hook, :meth:`add_title_bar_widget`, that floats a real control into the
native title bar just left of the min/max/close buttons, positioned from the
same style metrics the buttons use. QuickSet uses it for its mode dropdown; any
window (or plugin) can drop a control in the same way, so every window stays
visually identical whether or not it uses it.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QMdiArea,
    QMdiSubWindow,
    QStyle,
    QStyleOption,
    QTabBar,
    QWidget,
)

from .. import style as style_mod
from ..i18n import tr

_UNMAX_SLOP = 6  # px of title-bar drag before a maximized window pops back


def mdi_area_of(win) -> QMdiArea | None:
    """The area a subwindow sits in — *not* via ``QMdiSubWindow.mdiArea()``.

    That binding hands the area's ownership from Python to C++ the moment it
    is called: an area built in Python and held only by a Python name then
    belongs to nobody, and the interpreter segfaults collecting it on the way
    out. It reproduces in a dozen lines of plain PySide6 with none of this
    code in sight -- ``Shiboken.ownedByPython(area)`` goes True -> False
    across the call -- so the answer is not to call it.

    The parent chain says the same thing and touches no ownership: Qt parents
    a subwindow to the area's viewport, which puts the area one step further
    up.
    """
    parent = win.parentWidget()
    area = parent.parentWidget() if parent is not None else None
    return area if isinstance(area, QMdiArea) else None


class MdiWindow(QMdiSubWindow):
    """A native MDI subwindow that can host a widget in its title bar."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # Qt only takes the two shortcuts that keep this app responsive -- blit
        # a window's pixels when it moves, and skip a window that another one
        # covers -- for a subwindow it knows is opaque, and nothing about a
        # stylesheet background tells it that. Without this, every mouse-move
        # of a drag repaints the full contents of the window being dragged and
        # of the ones under it (~20ms a frame for a 55-row, 12-column list, on
        # the one thread that also has the keyboard to serve), and a list
        # buried behind another window pays for its 1 Hz repaint in full. The
        # promise the attribute makes -- that paintEvent covers every pixel --
        # is kept below.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._title_widget: QWidget | None = None  # right-aligned control
        self._title_fill: QWidget | None = None  # fills the title band
        self._area_hooked = False
        # Tab strip this window would move into its title bar, if asked.
        self._tab_host = None            # the QTabWidget
        self._tab_keys_done = False      # Ctrl+PgUp/PgDn installed once
        self._tab_group = ""             # strip's own label
        self._tab_open_single = None     # "open this tab in its own window"
        self._title_tabs = None          # the live TitleTabBar, when installed
        self._plain_title = ""           # title text while the strip owns the bar
        self._title_parked = False       # we hid the floated controls (no band)
        # A press in the title bar of a maximized window, which a drag turns
        # into "restore, then keep dragging".
        self._unmax_press = None
        self._unmax_drag = False
        self._unmax_win = None

    # -- Esc closes ---------------------------------------------------------

    def keyPressEvent(self, e) -> None:  # noqa: N802
        """Close on Escape, however the window happens to be focused.

        This was a ``QShortcut`` with ``WidgetWithChildrenShortcut``, which
        only fires when the focus widget is this window or inside it -- and a
        window opened from the nav tree does not necessarily take the keyboard
        with it, so Esc went to the tree and nothing happened. Key events
        propagate from the focus widget up through its parents instead, so a
        child that wants Escape for itself (an open cell editor, a completer
        popup) still gets first refusal and this only sees what nobody used.
        """
        if (
            e.key() == Qt.Key.Key_Escape
            and e.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            e.accept()
            self.close()
            return
        super().keyPressEvent(e)

    def focus_target(self):
        """The widget a freshly opened window should hand the keyboard to.

        Subclasses that know better say so -- a list window wants its pane,
        not the container it happens to sit in.
        """
        return self.widget()

    def take_focus(self) -> None:
        """Put the keyboard in this window, the way opening one should.

        Activates the subwindow first: focus set on a window the MDI area does
        not consider active does not stick. Then walks down to something that
        will actually accept it -- ``setFocus`` on a plain container is a
        no-op, and a window whose keyboard focus stayed behind in the nav tree
        does not answer Esc.
        """
        area = mdi_area_of(self)
        if area is not None and area.activeSubWindow() is not self:
            area.setActiveSubWindow(self)
        reason = Qt.FocusReason.ActiveWindowFocusReason
        self.setFocus(reason)
        for w in self._focus_candidates():
            if w is None:
                continue
            try:
                if not (w.isVisible() and w.isEnabled()):
                    continue
                if w.focusPolicy() == Qt.FocusPolicy.NoFocus:
                    continue
            except RuntimeError:
                continue
            w.setFocus(reason)
            if self._has_focus_inside():
                return

    def _focus_candidates(self):
        """What to try, best first.

        The nominated target may nominate one of its own -- a list pane wants
        the keyboard in its filter box, so you can type to narrow the list the
        moment the window appears, the way Winbox does. Then the target
        itself, then anything under it that will take focus.
        """
        target = self.focus_target()
        hop = target
        for _ in range(4):                     # bounded: no nomination loops
            nominate = getattr(hop, "focus_target", None) if hop is not None else None
            nxt = nominate() if callable(nominate) else None
            if nxt is None or nxt is hop:
                break
            yield nxt
            hop = nxt
        yield target
        if target is not None:
            yield from target.findChildren(QWidget)

    def _has_focus_inside(self) -> bool:
        from PySide6.QtWidgets import QApplication

        fw = QApplication.focusWidget()
        return fw is not None and (fw is self or self.isAncestorOf(fw))

    # -- title text ---------------------------------------------------------

    def setWindowTitle(self, text: str) -> None:  # noqa: N802
        """Route the title to whoever is drawing the bar.

        With a tab strip installed the native title text is off (the two would
        overlap), so the strip carries it instead -- otherwise a property
        form's "· 2 edited" badge silently disappears the moment the setting
        is switched on.
        """
        self._plain_title = tr(text or "")
        if self._title_tabs is not None:
            self._title_tabs.set_group_name(self._plain_title)
            super().setWindowTitle("")
            return
        super().setWindowTitle(self._plain_title)

    # -- tabs in the title bar ----------------------------------------------

    def register_tab_strip(self, tabs, group_title: str = "", *,
                           on_open_single=None) -> None:
        """Declare the tab widget that may be moved into the title bar.

        Declaring it also gives this window its tab keys (see
        :meth:`_install_tab_keys`), so every window with tabs answers the same
        two -- wherever the strip happens to be drawn.
        """
        self._tab_host = tabs
        self._tab_group = group_title or self._plain_title
        self._tab_open_single = on_open_single
        self._install_tab_keys()

    # -- keyboard: the tabs -------------------------------------------------

    def _install_tab_keys(self) -> None:
        """Ctrl+PageDown/PageUp on the tabs, ahead of whatever has the keyboard.

        Installed once per window: a second registration would leave two
        shortcuts on the same key, which Qt answers by firing neither
        ("Ambiguous shortcut overload").

        Shortcuts rather than ``keyPressEvent`` cases, unlike Escape above: a
        QComboBox takes PageUp/PageDown for its own list whatever modifiers are
        held, and half a property form's rows are dropdowns -- so the key would
        be swallowed on the way up and the tab would never move. Nothing types
        Ctrl+PageUp, so claiming it ahead of the editors costs nothing.
        """
        if getattr(self, "_tab_keys_done", False):
            return
        self._tab_keys_done = True
        for seq, delta in (("Ctrl+PgDown", 1), ("Ctrl+PgUp", -1)):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda d=delta: self.step_tab(d))

    def step_tab(self, delta: int) -> bool:
        """Show the next (or previous) tab, and take the keyboard with it.

        True when a tab actually changed. Walking the strip itself leaves the
        keyboard on the strip, so the next key keeps browsing tabs; walking it
        from inside a page moves on into the new page -- for a property form
        that is its first editable field, for a list group its filter box, both
        of them by way of :meth:`focus_target`.

        The page is built on the way where a window builds them on demand: this
        goes through ``setCurrentIndex``, the same as clicking the tab.
        """
        tabs = self._tab_host
        if tabs is None or tabs.count() < 2:
            return False
        count = tabs.count()
        index = tabs.currentIndex()
        for _ in range(count):
            index = (index + delta) % count
            if tabs.isTabEnabled(index):
                break
        if index == tabs.currentIndex():
            return False
        on_strip = isinstance(QApplication.focusWidget(), QTabBar)
        tabs.setCurrentIndex(index)
        if not on_strip:
            self.take_focus()
            if QApplication.focusWidget() in (None, self):
                self._focus_unshown_page()
        return True

    def _focus_unshown_page(self) -> bool:
        """Focus the new tab's first control even though Qt has not shown it.

        A page built during ``setCurrentIndex`` is not visible yet -- Qt shows
        it when the layout is next activated, which is after this call stack --
        and :meth:`take_focus` skips what it cannot see, leaving the keyboard on
        the window frame. ``isHidden`` is the test that still answers here: it
        is about the widget itself, not about its parents being on screen. Only
        real tab stops count, so this lands in the box rather than on the
        container holding it (``setFocus`` works on a NoFocus widget too, which
        is how the keyboard ended up nowhere useful).
        """
        for w in self._focus_candidates():
            if w is None:
                continue
            try:
                if w.isHidden() or not w.isEnabled():
                    continue
                if not (w.focusPolicy() & Qt.FocusPolicy.TabFocus):
                    continue
            except RuntimeError:      # the page went away under us
                continue
            w.setFocus(Qt.FocusReason.TabFocusReason)
            if QApplication.focusWidget() is w:
                return True
        return False

    def titlebar_tabs_enabled(self) -> bool:
        return self._title_tabs is not None

    def set_titlebar_tabs(self, on: bool) -> None:
        """Move this window's tabs into the title bar, or back into the body.

        Live, both ways: the setting used to take effect only on windows
        opened afterwards, which reads as the toggle not working.
        """
        tabs = self._tab_host
        if tabs is None or bool(on) == (self._title_tabs is not None):
            return
        try:
            if on:
                self._install_title_tabs(tabs)
            else:
                self._remove_title_tabs(tabs)
        except RuntimeError:      # the tab widget went away with its window
            self._title_tabs = None
            self._tab_host = None

    def _install_title_tabs(self, tabs) -> None:
        from .title_tab_bar import TitleTabBar

        if tabs.count() < 2:
            return
        names = [tabs.tabText(i) for i in range(tabs.count())]
        strip = TitleTabBar(self._tab_group or self._plain_title, names)
        strip.set_active(tabs.currentIndex())
        strip.tabSelected.connect(tabs.setCurrentIndex)
        tabs.currentChanged.connect(strip.set_active)
        if self._tab_open_single is not None:
            strip.openTabWindow.connect(self._tab_open_single)
        self._title_tabs = strip
        tabs.tabBar().hide()
        self.set_title_bar_content(strip)
        # Re-route the title now that the strip owns the bar.
        self.setWindowTitle(self._plain_title)
        self._notify_active(self._is_native_active())

    def _remove_title_tabs(self, tabs) -> None:
        strip = self._title_tabs
        # Both handles go first: detaching the strip runs hide/resize events,
        # and anything that repositions the title bar while _title_fill still
        # points at a half-destroyed widget is a crash, not an exception.
        self._title_tabs = None
        self._title_fill = None
        tabs.tabBar().show()
        self.setWindowTitle(self._plain_title)
        if strip is not None:
            for sig, slot in ((tabs.currentChanged, strip.set_active),
                              (strip.tabSelected, tabs.setCurrentIndex)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            strip.hide()
            strip.setParent(None)
            strip.deleteLater()

    # -- maximize / restore -------------------------------------------------

    def title_band_height(self) -> int:
        """Height of the title-bar band, or 0 when the style is not drawing one.

        The reserved top margin *is* the band; the ``PM_`` metric is only a
        fallback for styles that answer with an unset margin.
        """
        m = self.contentsMargins()
        if m.top() > 0:
            return m.top()
        if self.isMaximized():
            return 0          # a bar-less maximized window really has no band
        return self.style().pixelMetric(
            QStyle.PixelMetric.PM_TitleBarHeight, None, self
        )

    def toggle_maximized(self) -> None:
        """What a double-click on the title bar does.

        Native for the bar itself; the inline tab strip covers the bar, so it
        calls this instead of the double-click falling into a dead spot.
        """
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def restore_under_cursor(self, gp) -> bool:
        """Pop a maximized window back to its old size mid-drag, keeping the
        grabbed point of the title bar under the cursor.

        Qt has no such gesture -- dragging a maximized subwindow slides the
        full-size window around instead of restoring it, which is not what
        anyone means by dragging a window off the top. Returns True if this
        call did restore, so the caller can re-base its own drag origin.
        """
        if not self.isMaximized():
            return False
        local = self.mapFromGlobal(gp)
        # Where along the bar the cursor sits, as a fraction, so a window
        # grabbed by its middle comes back under the middle of the cursor.
        frac = min(max(local.x() / max(1, self.width()), 0.0), 1.0)
        grab_y = min(max(local.y(), 0), max(0, self.title_band_height() - 1))
        parent = self.parentWidget()
        self.showNormal()
        cp = parent.mapFromGlobal(gp) if parent is not None else gp
        self.move(
            max(0, cp.x() - round(frac * self.width())),
            max(0, cp.y() - grab_y),
        )
        return True

    def _in_title_drag_zone(self, pos) -> bool:
        """A point in the title band, clear of the min/restore/close buttons."""
        band = self.title_band_height()
        if band <= 0 or not (0 <= pos.y() < band):
            return False
        m = self.contentsMargins()
        fw = m.left() or self.style().pixelMetric(
            QStyle.PixelMetric.PM_MdiSubWindowFrameWidth, None, self
        )
        # Same button arithmetic the floated title controls use.
        return pos.x() < self.width() - fw - (3 * band + 6)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        # Only remember the press; it still goes to QMdiSubWindow, because
        # swallowing it here would kill the title-bar buttons.
        self._unmax_press = None
        self._unmax_drag = False
        if (
            e.button() == Qt.MouseButton.LeftButton
            and self.isMaximized()
            and self._in_title_drag_zone(e.position().toPoint())
        ):
            self._unmax_press = e.globalPosition().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        start = self._unmax_press
        if start is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            gp = e.globalPosition().toPoint()
            if self._unmax_drag:
                self.move(self._unmax_win + (gp - start))
                e.accept()
                return
            d = gp - start
            if abs(d.x()) > _UNMAX_SLOP or abs(d.y()) > _UNMAX_SLOP:
                if self.restore_under_cursor(gp):
                    # We own the rest of this drag: Qt never started a move
                    # operation, the window was maximized when it pressed.
                    self._unmax_drag = True
                    self._unmax_press = gp
                    self._unmax_win = self.pos()
                    e.accept()
                    return
                self._unmax_press = None
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._unmax_press = None
        self._unmax_drag = False
        super().mouseReleaseEvent(e)

    # -- title-bar control --------------------------------------------------

    def add_title_bar_widget(self, w: QWidget) -> None:
        """Float ``w`` onto the native title bar (right side, left of the
        window buttons). It is repositioned on resize and window-state change."""
        w.setParent(self)
        self._title_widget = w
        self._title_fill = None
        w.show()
        w.raise_()
        self._reposition_title_widget()

    def set_title_bar_content(self, w: QWidget) -> None:
        """Fill the title-bar band (from just after the window icon to the
        window buttons) with ``w`` -- e.g. an inline tab strip. The native
        title text should be cleared (``setWindowTitle("")``) so the two do not
        overlap; the icon stays."""
        w.setParent(self)
        self._title_fill = w
        self._title_widget = None
        w.show()
        w.raise_()
        self._reposition_title_widget()

    def _reposition_title_widget(self) -> None:
        if self._title_widget is None and self._title_fill is None:
            return
        s = self.style()
        # The reserved top margin IS the title-bar band ([0 .. top]); the side
        # margin is the frame width. Using the actual margins (not just the
        # PM_ metrics) is what makes the control sit centred in the bar rather
        # than dropping onto its bottom edge.
        m = self.contentsMargins()
        th = self.title_band_height()
        if th <= 0:
            # No band to sit in: a style that hides a maximized subwindow's
            # title bar (and re-homes its buttons into the menu bar) leaves
            # these nowhere to go, and y=0 is then the window's *content*.
            # Park them until the bar comes back rather than paint over it.
            self._title_parked = True
            for c in (self._title_widget, self._title_fill):
                if c is not None:
                    c.hide()
            return
        if self._title_parked:
            self._title_parked = False
            for c in (self._title_widget, self._title_fill):
                if c is not None:
                    c.show()
                    c.raise_()
        fw = m.left() or s.pixelMetric(
            QStyle.PixelMetric.PM_MdiSubWindowFrameWidth, None, self
        )
        # min / restore-or-max / close each take ~one title-height on the right.
        buttons_w = 3 * th + 6

        # The right-aligned control (mode combo / Configuration button) is placed
        # first; a title fill (tab strip) then stops just before it so the two
        # never overlap.
        right_edge = self.width() - fw - buttons_w
        w = self._title_widget
        if w is not None:
            h = max(14, th - 6)
            cw = w.sizeHint().width()
            if cw <= 0:
                cw = 150
            # Do not let it swallow the title text on a narrow window.
            cw = min(cw, max(90, self.width() - 2 * fw - buttons_w - 140))
            x = right_edge - cw - 4
            y = max(0, (th - h) // 2)  # centre in the title-bar band
            w.setGeometry(QRect(max(x, fw + 60), y, cw, h))
            right_edge = w.x() - 6

        fill = self._title_fill
        if fill is not None:
            # From just after the window icon (~one title-height in) to the
            # right-aligned control (or the window buttons).
            x = fw + th + 2
            fill.setGeometry(QRect(x, 0, max(40, right_edge - x - 2), th))

    # -- keep the floated control aligned -----------------------------------

    def paintEvent(self, e) -> None:  # noqa: N802 - Qt name
        """Style painting, then the theme's 1px window edge on top.

        Why paint it rather than declare ``border: 1px`` on ``QMdiSubWindow``:
        QStyleSheetStyle derives ``PM_MdiSubWindowFrameWidth`` from that rule's
        box, and that metric *is* the resize grab band. A 1px border would
        shrink the band from 4px to 1px and make an edge nearly ungrabbable —
        the same trap the "border: none" note in style.py records from the other
        direction. Painting leaves the metric alone.

        Drawn one pixel inside the band, not at the window's outer edge, so the
        line hugs the content instead of floating with 3px of backdrop colour
        between the two. On a skin with rounded corners the mask nicks the line
        at each corner; the flat skins that ask for this are square.

        Three sides, not four: left, bottom, right, and starting below the title
        band. The title strip is a solid block of colour that already reads as
        an edge against the backdrop, so ruling it as well boxes it in and turns
        the bar into a button. What needs the line is the pale content under it.

        The pen is **cosmetic**, which is what makes this a hairline rather than
        a rule. A pen width is in logical units, so ``QPen(colour, 1)`` is two
        device pixels on a 2x display and visibly heavy — the thing it is meant
        to be is one *device* pixel, on any screen. A cosmetic pen is defined as
        exactly that and needs no arithmetic, so it also survives the window
        being dragged to a monitor with a different ratio.

        The background comes first, and by hand: the opaque attribute set in
        __init__ also turns off Qt's own background pass, which is where the
        stylesheet's ``QMdiSubWindow { background: ... }`` was being drawn.
        QMdiSubWindow insets its child by the whole grab band, so that band is
        the one place the colour shows -- and left unpainted it kept whatever
        the backing store held before, which after a drag is a strip of the
        window that used to be there.
        """
        option = QStyleOption()
        option.initFrom(self)
        background = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, background, self
        )
        background.end()
        super().paintEvent(e)
        colour = style_mod.MDI_BORDER
        if not colour:
            return
        band = self.style().pixelMetric(
            QStyle.PixelMetric.PM_MdiSubWindowFrameWidth, None, self
        )
        inset = max(0, band - 1)
        left = inset
        right = self.width() - 1 - inset
        bottom = self.height() - 1 - inset
        # Where the child widget starts: the reserved top margin is frame band
        # plus title bar, so this is the first row of content and the line rises
        # no further. Falls back to the band alone if no bar is being drawn.
        top = self.contentsMargins().top() or band
        if right <= left or bottom <= top:
            return
        pen = QPen(QColor(colour), 1)
        pen.setCosmetic(True)
        painter = QPainter(self)
        painter.setPen(pen)
        painter.drawLine(left, top, left, bottom)
        painter.drawLine(right, top, right, bottom)
        painter.drawLine(left, bottom, right, bottom)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._reposition_title_widget()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        area = mdi_area_of(self)
        if area is not None and not self._area_hooked:
            self._area_hooked = True
            area.subWindowActivated.connect(self._on_area_activated)
        self._notify_active(self._is_native_active())
        self._reposition_title_widget()

    def _is_native_active(self) -> bool:
        """True when the native title bar paints *active*: this is the area's
        active subwindow AND the top-level window is active. Using only the
        former left the bar grey (top window unfocused) while the strip still
        painted its active, white-on-blue text -- unreadable on grey."""
        area = mdi_area_of(self)
        if area is None:
            return True
        return area.activeSubWindow() is self and self.window().isActiveWindow()

    def _on_area_activated(self, _win) -> None:
        self._notify_active(self._is_native_active())

    def _notify_active(self, active: bool) -> None:
        # A title-bar fill widget (the inline tab strip) paints its own text, so
        # it needs to follow the native bar between active and inactive colours.
        fill = self._title_fill
        setter = getattr(fill, "set_window_active", None) if fill is not None else None
        if setter is not None:
            setter(active)

    def changeEvent(self, e) -> None:  # noqa: N802
        super().changeEvent(e)
        t = e.type()
        if t in (
            QEvent.Type.WindowStateChange,
            QEvent.Type.ActivationChange,
            QEvent.Type.WindowActivate,
            QEvent.Type.WindowDeactivate,
        ):
            self._notify_active(self._is_native_active())
            self._reposition_title_widget()
