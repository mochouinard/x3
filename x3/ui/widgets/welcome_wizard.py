"""First-start welcome: what x3 does on its own, and the switches for it.

Shown once, before the first connection page, and again from
Help ▸ Welcome Tour. Every page explains one behaviour and offers its switch,
pre-set to the current setting, so finishing without touching anything changes
nothing:

* which address book (``Addresses.cdb``) the connection page lists;
* how it looks (theme and zoom previewed live, and the icon set);
* getting around by keyboard, and the Ctrl key-hints panel;
* how changes reach a router (the mode a new session starts in);
* the safety nets, and the Safe Mode idle alarm;
* config snapshots, and whether they carry secrets;
* the audit record of every connection;
* the small differences in lists and forms, each one a switch;
* what x3 offers outside its window.

Comparisons with the official client are made only where the difference is a
documented, deliberate one; everything else is described as what x3 does.

Nothing is saved until Finish. Cancel puts the theme back the way it was, and
either way the welcome is marked as seen, so it does not come back on its own.

It is a plain dialog with its own Back / Next / Finish, not a ``QWizard``: on
Wayland, re-styling the application while a QWizard is on screen crashes the
process (a bare QWizard with one checkbox is enough), and previewing a theme is
exactly that. A QDialog survives it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from x3.proto import audit as audit_mod
from x3.paths import env_var
from x3.proto.session_log import log_dir

from .. import i18n, settings, style
from ..i18n import tr, trf, verbatim
from ..icons import app_icon

#: Safe Mode idle-alarm delays offered, in seconds (0 = off). The menu's own
#: list also has a 10-second entry for testing, which a tour has no use for.
SAFE_IDLE_DELAYS: tuple[tuple[int, str], ...] = (
    (0, "Off"),
    (60, "after 1 minute"),
    (300, "after 5 minutes"),
    (900, "after 15 minutes"),
)

#: The edit modes a session can start in, in the order they are offered.
START_MODES: tuple[tuple[str, str, str], ...] = (
    ("live", "Live",
     "Apply, Remove, Enable/Disable and Comment reach the router as soon as "
     "you press them. The toolbar and every form title say LIVE. Turn on "
     "Safe Mode (right end of the toolbar) before a risky edit: if the "
     "session drops, the router undoes what Safe Mode recorded."),
    ("queue", "Queue",
     "Changes are collected instead of sent. Pending changes lists each one "
     "as old → new, and nothing reaches the router until you press Apply all."),
    ("readonly", "Read-only",
     "Look, don't touch: forms are locked. Pick Live or Queue from the "
     "toolbar's Mode box when you mean to change something."),
)


def _home(path) -> str:
    """A path as a person would type it: ``~/…`` under the home directory."""
    import os

    text, home = str(path), os.path.expanduser("~")
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _text(html: str) -> QLabel:
    # Translated before it is wrapped: the table holds the paragraph, not the
    # div around it, and wrapped it matched nothing -- every paragraph of the
    # tour stayed English under titles and buttons in French.
    lab = QLabel(f"<div style='line-height:125%'>{tr(html)}</div>")
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.TextFormat.RichText)
    lab.setOpenExternalLinks(True)
    return lab


def _note(html: str) -> QLabel:
    lab = _text(html)
    lab.setStyleSheet(f"color: {style.TEXT_DIM};")
    return lab


def _switch(layout, label: str, checked: bool, why: str) -> QCheckBox:
    """A checkbox with its one-line explanation indented under it."""
    box = QCheckBox(label)
    box.setChecked(checked)
    layout.addWidget(box)
    note = _note(why)
    note.setContentsMargins(style.scale_px(22), 0, 0, style.scale_px(4))
    layout.addWidget(note)
    return box


def _points(items: list[tuple[str, str]]) -> QLabel:
    """A list of features: a bold name, then what it does."""
    return _text(verbatim("".join(
        f"<p><b>{tr(name)}.</b> {tr(body)}</p>" for name, body in items
    )))


class _Page(QWidget):
    """One step of the tour: a heading for the dialog's header, and a body."""

    title = ""
    subtitle = ""

    def enter(self) -> None:
        """Called each time the page is shown."""


class WelcomePage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Welcome to x3"
        self.subtitle = "A desktop client for routers managed over the Winbox protocol."
        lay = QVBoxLayout(self)
        lay.addWidget(_text(
            "<p>x3 reads each router's own schema, so its menus, lists and forms "
            "are the ones the device describes. A few things it does on its own "
            "are worth deciding up front: how a change reaches the router, the "
            "config snapshots it can keep for you, and the audit record it can "
            "write of every connection.</p>"
            "<p>If you have used the official client, the menus and forms will "
            "feel familiar, because they come from the same router. The pages "
            "ahead are mostly about what x3 adds on top.</p>"
            "<p>The next pages explain each one and let you switch it on or off. "
            "Everything here can be changed later, and this tour can be reopened "
            "from <b>Help ▸ Welcome Tour</b>.</p>"
        ))
        lay.addWidget(_note(
            "x3 is an independent research project, free for personal and "
            "noncommercial use, and it comes with no warranty. Take a backup "
            "before you change a router with it, and try it on a device you can "
            "afford to lose first. The full terms are under Help ▸ About."
        ))
        # Asked here, first, because it is the one thing x3 does on the
        # network without being asked to. Ticked until the user has chosen:
        # an update check nobody ever hears about leaves them on an old
        # release, and this is where they hear about it.
        chosen = settings.update_check_chosen()
        self.update_check = _switch(
            lay, "Check for x3 &updates (recommended)",
            settings.load_update_check() if chosen else True,
            "Once a day at most, x3 asks GitHub whether a newer release is out. "
            "Nothing else is sent, and nothing is installed until you say so: a "
            "button appears with what is new. Help ▸ Check for x3 Updates asks "
            "at any time, whatever you choose here.",
        )
        lay.addStretch(1)


class AddressBookPage(_Page):
    """Which ``Addresses.cdb`` the connection page lists, found or chosen."""

    def __init__(self) -> None:
        super().__init__()
        self.title = "Your address book"
        self.subtitle = "The saved routers x3 lists on its connection page."
        lay = QVBoxLayout(self)
        lay.addWidget(_text(
            "<p>x3 reads the address book the official client saves "
            "(<code>Addresses.cdb</code>), so the routers you already saved are "
            "listed on the first start. It never rewrites that file: notes, "
            "groups and passwords you change in x3 go to an overlay of its own."
            "</p>"
        ))
        self.where = _text("")
        lay.addWidget(self.where)
        buttons = QHBoxLayout()
        self.choose_button = QPushButton("&Choose…")
        self.auto_button = QPushButton("Find &automatically")
        buttons.addWidget(self.choose_button)
        buttons.addWidget(self.auto_button)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        lay.addWidget(_note(trf(
            "Later: <b>Open…</b> on the connection page's Addresses tab. "
            "<code>{var}</code> outranks both.", var=env_var("X3_ADDRESSES_CDB"),
        )))
        lay.addStretch(1)
        self._chosen = settings.load_addresses_cdb()
        self.choose_button.clicked.connect(self._choose)
        self.auto_button.clicked.connect(self._auto)
        self._describe()

    def choice(self) -> str | None:
        """The picked file, or None to keep looking in the usual places."""
        return self._chosen

    def in_use(self) -> tuple[str | None, int]:
        """The file the connection page will list, and how many routers it holds."""
        from ..services import find_addresses_cdb, load_address_book

        path = self._chosen or find_addresses_cdb(include_chosen=False)
        if path is None:
            return None, 0
        _resolved, rows, err = load_address_book(path, include_passwords=False)
        return str(path), (0 if err else len(rows))

    def _describe(self) -> None:
        path, count = self.in_use()
        if path is None:
            self.where.setText(
                "<p><b>No address book found</b> in the usual places. Choose one, "
                "or carry on: you can type an address on the connection page and "
                "save it there.</p>"
            )
            return
        how = tr("Chosen:") if self._chosen else tr("Found:")
        saved = (trf("1 saved router") if count == 1
                 else trf("{count} saved routers", count=count))
        self.where.setText(verbatim(
            f"<p><b>{how}</b> <code>{_home(path)}</code><br>{saved}</p>"
        ))

    def _choose(self) -> None:
        from ..services import load_address_book

        current, _count = self.in_use()
        start = str(Path(current).parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose Addresses.cdb", start, "Address book (*.cdb);;All files (*)"
        )
        if not path:
            return
        _resolved, _rows, err = load_address_book(path, include_passwords=False)
        if err:
            self.where.setText(f"<p><b>That file could not be read:</b> {err}</p>")
            return
        self._chosen = path
        self._describe()

    def _auto(self) -> None:
        self._chosen = None
        self._describe()


class LookPage(_Page):
    """Language, theme and zoom. Theme and zoom are applied as they are picked
    so the tour is the preview; the language takes effect after the tour."""

    def __init__(self) -> None:
        super().__init__()
        self.title = "How it looks"
        self.subtitle = "Pick a skin and a size. This window redraws as you choose."
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.language = QComboBox()
        for label, code in i18n.choices():
            self.language.addItem(label, code)
        self.language.setCurrentIndex(max(0, self.language.findData(settings.load_language())))
        self.language.setToolTip(
            "Menus, windows and the router's labels. What the router stores "
            "is never translated."
        )
        form.addRow("&Language:", self.language)
        self.theme = QComboBox()
        for t in style.THEMES.values():
            self.theme.addItem(tr(t.name), t.id)
            self.theme.setItemData(self.theme.count() - 1, tr(t.description),
                                   Qt.ItemDataRole.ToolTipRole)
        self.theme.setCurrentIndex(max(0, self.theme.findData(style.base_theme().id)))
        form.addRow("&Theme:", self.theme)
        self.zoom = QComboBox()
        for step in style.SCALE_STEPS:
            self.zoom.addItem(f"{round(step * 100)}%", step)
        here = min(style.SCALE_STEPS, key=lambda s: abs(s - style.ui_scale()))
        self.zoom.setCurrentIndex(style.SCALE_STEPS.index(here))
        form.addRow("&Zoom:", self.zoom)
        self.icons = QComboBox()
        for value, label in settings.ICON_SOURCE_CHOICES:
            self.icons.addItem(tr(label).replace("&", ""), value)
        self.icons.setCurrentIndex(max(0, self.icons.findData(settings.load_icon_source())))
        self.icons.setToolTip(
            "Colour, or one ink that follows the theme's text colour"
        )
        form.addRow("&Icons:", self.icons)
        lay.addLayout(form)
        self.blurb = _note("")
        lay.addWidget(self.blurb)
        lay.addWidget(_note(
            "Monochrome icons are drawn in the theme's text colour, so they "
            "follow a dark skin. Later: <b>View ▸ Theme</b>, <b>View ▸ Font</b>, "
            "<b>View ▸ Icon Set</b>, and <b>Ctrl+=</b> / <b>Ctrl+-</b> / "
            "<b>Ctrl+0</b> for the size."
        ))
        lay.addStretch(1)
        self.theme.currentIndexChanged.connect(self._preview)
        self.zoom.currentIndexChanged.connect(self._preview)
        self._describe()

    def choice(self) -> tuple[str, float]:
        return str(self.theme.currentData()), float(self.zoom.currentData())

    def language_choice(self) -> str:
        return str(self.language.currentData())

    def _describe(self) -> None:
        self.blurb.setText(style.get_theme(self.choice()[0]).description)

    def _preview(self) -> None:
        tid, scale = self.choice()
        style.apply_theme(QApplication.instance(), tid, persist=False, scale=scale)
        self._describe()


class ChangesPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "How changes reach a router"
        self.subtitle = "Pick the mode a new session starts in."
        lay = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        current = settings.load_start_mode()
        for mode, label, help_text in START_MODES:
            radio = QRadioButton(label)
            radio.setProperty("mode", mode)
            radio.setChecked(mode == current)
            self.group.addButton(radio)
            lay.addWidget(radio)
            body = _note(help_text)
            body.setContentsMargins(style.scale_px(22), 0, 0, style.scale_px(6))
            lay.addWidget(body)
        lay.addWidget(_note(
            "The toolbar's <b>Mode</b> box switches between them at any time; "
            "this only decides where a session starts."
        ))
        lay.addStretch(1)

    def choice(self) -> str:
        checked = self.group.checkedButton()
        return str(checked.property("mode")) if checked else settings.DEFAULT_EDIT_MODE


class KeysPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Getting around"
        self.subtitle = "x3 is built to be driven from the keyboard."
        lay = QVBoxLayout(self)
        lay.addWidget(_points([
            ("Type to filter", "A list window opens with the cursor in its "
             "filter box: start typing and the rows narrow. Down or Enter hands "
             "the keyboard to the rows."),
            ("Rows", "Enter opens a row, Ins adds, Del removes, Ctrl+E / "
             "Ctrl+Shift+E enable and disable, Ctrl+M comments, Alt+↑ / Alt+↓ "
             "move a rule."),
            ("Forms", "Tab walks the fields, Ctrl+PageDown changes tab, Enter is "
             "OK and Ctrl+Enter is Apply. Holding Alt underlines every button's "
             "letter, something the official client's forms never had."),
            ("Menus", "Ctrl+K searches every menu by name."),
            ("The wheel", "Scrolling never changes a value; it scrolls the form. "
             "Ctrl+wheel is the deliberate way to step a drop-down or a number."),
        ]))
        self.key_hints = _switch(
            lay, "Show the &key panel while Ctrl is held",
            settings.load_key_hints(),
            "Hold Ctrl for half a second and a panel lists the keys the window "
            "in front answers to. The official client has nothing like it. F1 "
            "opens the same panel either way.",
        )
        lay.addStretch(1)


class SafetyPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Safety nets"
        self.subtitle = "What x3 does so a mistake costs less."
        lay = QVBoxLayout(self)
        lay.addWidget(_points([
            ("Safe Mode you cannot miss", "While Safe Mode is on, the toolbar "
             "button, a banner and the window's backdrop are striped like "
             "hazard tape, and the title says [SAFE]."),
            ("Idle alarm and input guard", "An unattended Safe Mode session "
             "starts blinking and asks to be dealt with. For a few seconds "
             "after it goes off, clicks and keys are ignored, so a hand "
             "reaching for the mouse cannot land on Remove."),
            ("Changed elsewhere", "A change made by someone else, on the "
             "console or another session, lights a toolbar button; click it to "
             "see what moved. No pop-up interrupts you."),
            ("Same router as last time", "x3 remembers each router's identity "
             "and, before opening the session, warns you if a different device "
             "answers at a saved address."),
            ("Dropped connections", "A session that loses its router says so, "
             "reconnects on its own, and keeps your windows open."),
        ]))
        form = QFormLayout()
        self.idle = QComboBox()
        current = settings.load_safe_mode_idle_secs()
        delays = list(SAFE_IDLE_DELAYS)
        if current not in {secs for secs, _ in delays}:
            delays.append((current, f"after {current} seconds"))
        for secs, label in delays:
            self.idle.addItem(tr(label), secs)
        self.idle.setCurrentIndex(max(0, self.idle.findData(current)))
        form.addRow("Safe Mode &idle alarm:", self.idle)
        lay.addLayout(form)
        lay.addStretch(1)


class ListsPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Lists and forms"
        self.subtitle = "Small things x3 does differently. Each one is a switch."
        lay = QVBoxLayout(self)
        self.dropdown_marks = _switch(
            lay, "Status &marks in drop-downs", settings.load_dropdown_marks(),
            "An interface picker shows which links are up, down or disabled, "
            "and their comments. The official client shows bare names.",
        )
        self.form_status_line = _switch(
            lay, "Form &status line", settings.load_form_status_line(),
            "A line across the top of a form says which mode it is in and what "
            "has been changed but not written yet.",
        )
        self.titlebar_tabs = _switch(
            lay, "&Tabs in the title bar", settings.load_titlebar_tabs(),
            "Windows with several tabs show them in their title bar, which "
            "leaves more room for the rows.",
        )
        self.most_used = _switch(
            lay, "Fill \"&most used\" menus automatically", settings.load_most_used_auto(),
            "Submenus put the entries you open most at the top. Off keeps only "
            "the ones you pin.",
        )
        self.live_updates = _switch(
            lay, "&Live updates in new lists", settings.load_live_updates(),
            "Lists follow the router as it changes. Off is the low-bandwidth "
            "mode: an idle window sends nothing until you press Refresh.",
        )
        lay.addStretch(1)


class BeyondPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Beyond the window"
        self.subtitle = "The rest of x3, for when you need it."
        lay = QVBoxLayout(self)
        lay.addWidget(_points([
            ("Try all saved", "On the connection page, x3 can try every password "
             "in your address book against a router you are locked out of. It "
             "only ever does so over the encrypted login."),
            ("x3ctl", "The same protocol on the command line: export a "
             "router's configuration, open its console, or back up every router "
             "in your address book into a git repository on a nightly timer."),
            ("AI assistants", "<code>x3ctl mcp</code> lets an assistant "
             "such as Claude read your saved routers (lists, logs, ping, "
             "traceroute) over MCP. It cannot change anything, and passwords "
             "never reach it."),
            ("Screen readers", "Lists, forms and dialogs are named for "
             "assistive technology through Qt's accessibility bridge."),
        ]))
        lay.addWidget(_note("All of it is described in the README that ships with x3."))
        lay.addStretch(1)


class SnapshotsPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Config snapshots"
        self.subtitle = "A text export of the router, kept for you after a change."
        lay = QVBoxLayout(self)
        lay.addWidget(_text(
            "<p>When a session has changed something, x3 waits until it has been "
            "quiet for 15 minutes, or until you disconnect, and saves the "
            "router's configuration as text. Each router gets its own folder, "
            "and an export identical to the last one is not stored again.</p>"
            "<p>The export is read straight off the router's console: nothing is "
            "written to its flash, and a session that changes nothing takes "
            "no snapshot at all.</p>"
        ))
        self.enabled = QCheckBox("Take a &config snapshot after changes")
        self.enabled.setChecked(settings.load_auto_export())
        lay.addWidget(self.enabled)
        self.sensitive = QCheckBox("Include &passwords and keys")
        self.sensitive.setChecked(settings.load_auto_export_sensitive())
        lay.addWidget(self.sensitive)
        sens_note = _note(
            "With secrets, a snapshot is complete enough to rebuild the router, "
            "and every snapshot file is also a credential file for it. Leave "
            "this off unless the folder below is private."
        )
        sens_note.setContentsMargins(style.scale_px(22), 0, 0, 0)
        lay.addWidget(sens_note)
        lay.addWidget(_note(trf(
            "Saved under <code>{folder}</code>. "
            "Later: <b>Settings ▸ Auto Config Snapshot</b> and "
            "<b>Settings ▸ Take Snapshot Now</b>.",
            folder=_home(settings.data_dir() / "config-exports"),
        )))
        lay.addStretch(1)
        self.enabled.toggled.connect(self.sensitive.setEnabled)
        self.sensitive.setEnabled(self.enabled.isChecked())


class AuditPage(_Page):
    def __init__(self) -> None:
        super().__init__()
        self.title = "Audit log"
        self.subtitle = "An exact record of what each connection sent and received."
        lay = QVBoxLayout(self)
        lay.addWidget(_text(
            "<p>With audit on, every request x3 sends and every reply the router "
            "gives back is written to a file as it happens: what was changed, "
            "when, and what the router answered. It is the answer to \"what did "
            "I actually do to this device?\", and the file can be read back, "
            "listed and diffed with <code>x3ctl audit FILE</code>.</p>"
        ))
        self.enabled = QCheckBox("&Audit every connection")
        self.enabled.setChecked(settings.load_audit())
        lay.addWidget(self.enabled)
        form = QFormLayout()
        self.level = QComboBox()
        for level in audit_mod.LEVELS:
            label = tr(audit_mod.LEVEL_LABEL[level])
            if level == audit_mod.DEFAULT_LEVEL:
                label += " " + tr("(recommended)")
            self.level.addItem(label, level)
            self.level.setItemData(self.level.count() - 1, tr(audit_mod.LEVEL_HELP[level]),
                                   Qt.ItemDataRole.ToolTipRole)
        self.level.setCurrentIndex(max(0, self.level.findData(settings.load_audit_level())))
        form.addRow("&Keep:", self.level)
        lay.addLayout(form)
        self.level_help = _note("")
        lay.addWidget(self.level_help)
        lay.addWidget(_note(trf(
            "Each step keeps everything the one above it does, plus more. "
            "An audit file holds every configuration value that crossed the wire, "
            "passwords included, so keep its folder private. Files go to "
            "<code>{folder}</code>. The connection page also has an "
            "<b>Audit this connection</b> box, so you can decide per connection.",
            folder=_home(log_dir()),
        )))
        lay.addStretch(1)
        self.enabled.toggled.connect(self.level.setEnabled)
        self.level.setEnabled(self.enabled.isChecked())
        self.level.currentIndexChanged.connect(self._describe)
        self._describe()

    def _describe(self) -> None:
        level = str(self.level.currentData())
        self.level_help.setText(f"{tr(audit_mod.LEVEL_LABEL[level])}: "
                                f"{tr(audit_mod.LEVEL_HELP[level])}.")


class DonePage(_Page):
    def __init__(self, wizard: "WelcomeWizard") -> None:
        super().__init__()
        self._wizard = wizard
        self.title = "Ready"
        self.subtitle = "Press Finish to keep these choices."
        lay = QVBoxLayout(self)
        self.summary = _text("")
        lay.addWidget(self.summary)
        lay.addWidget(_note("Reopen this any time from <b>Help ▸ Welcome Tour</b>."))
        lay.addStretch(1)

    def enter(self) -> None:
        w = self._wizard
        tid, scale = w.look.choice()
        mode = tr(dict((m, label) for m, label, _ in START_MODES)[w.changes.choice()])
        on, off = tr("on"), tr("off")
        snap = on if w.snapshots.enabled.isChecked() else off
        if w.snapshots.enabled.isChecked() and w.snapshots.sensitive.isChecked():
            snap += tr(", with passwords and keys")
        level = tr(audit_mod.LEVEL_LABEL[str(w.audit.level.currentData())]).lower()
        audit = f"{on} ({level})" if w.audit.enabled.isChecked() else off
        lists = [tr(name) for box, name in (
            (w.lists.dropdown_marks, "drop-down marks"),
            (w.lists.form_status_line, "form status line"),
            (w.lists.titlebar_tabs, "title-bar tabs"),
            (w.lists.most_used, "most used"),
            (w.lists.live_updates, "live updates"),
        ) if box.isChecked()]
        book, count = w.book.in_use()
        routers = (tr("{n} router") if count == 1 else tr("{n} routers")).format(n=count)
        book_text = f"{_home(book)} ({routers})" if book else tr("none yet")
        rows = (
            ("Language", w.look.language.currentText()),
            ("Address book", book_text),
            ("Theme", f"{tr(style.get_theme(tid).name)}</b>, {round(scale * 100)}%, "
                      + tr("{kind} icons").format(kind=w.look.icons.currentText().lower())
                      + "<b>"),
            ("Key panel on Ctrl", on if w.keys.key_hints.isChecked() else off),
            ("New sessions start in", mode),
            ("Safe Mode idle alarm", w.safety.idle.currentText()),
            ("Config snapshots", snap),
            ("Audit", audit),
            ("Lists and forms", ", ".join(lists) or tr("all off")),
            ("Update check", tr("once a day") if w.welcome.update_check.isChecked() else off),
        )
        self.summary.setText(
            "<table cellpadding='3'>"
            + "".join(f"<tr><td>{tr(label)}</td><td><b>{value}</b></td></tr>"
                      for label, value in rows)
            + "</table>"
        )


class WelcomeWizard(QDialog):
    """The tour. Finish saves the choices; :meth:`exec` settles the theme after.

    The theme is previewed live but only kept, or put back, once the dialog
    has closed (:meth:`settle_theme`), so the window is never re-styled in the
    middle of its own close.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Welcome to x3")
        self.setModal(True)
        self.resize(style.scale_px(620), style.scale_px(500))
        self._theme_before = (style.base_theme().id, style.ui_scale())

        self.welcome = WelcomePage()
        self.book = AddressBookPage()
        self.look = LookPage()
        self.keys = KeysPage()
        self.changes = ChangesPage()
        self.safety = SafetyPage()
        self.snapshots = SnapshotsPage()
        self.audit = AuditPage()
        self.lists = ListsPage()
        self.beyond = BeyondPage()
        self.done_page = DonePage(self)
        self.pages: list[_Page] = [
            self.welcome, self.book, self.look, self.keys, self.changes, self.safety,
            self.snapshots, self.audit, self.lists, self.beyond, self.done_page,
        ]

        root = QVBoxLayout(self)
        head = QHBoxLayout()
        words = QVBoxLayout()
        self._title = QLabel()
        self._title.setTextFormat(Qt.TextFormat.RichText)
        self._subtitle = QLabel()
        self._subtitle.setWordWrap(True)
        words.addWidget(self._title)
        words.addWidget(self._subtitle)
        head.addLayout(words, 1)
        logo = QLabel()
        px = style.scale_px(48)
        logo.setPixmap(app_icon().pixmap(px, px))
        head.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(rule)

        self.stack = QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        foot = QHBoxLayout()
        self._step = QLabel()
        self._step.setStyleSheet(f"color: {style.TEXT_DIM};")
        foot.addWidget(self._step)
        foot.addStretch(1)
        self.back_button = QPushButton("< &Back")
        self.next_button = QPushButton("&Next >")
        self.finish_button = QPushButton("&Finish")
        self.cancel_button = QPushButton("Cancel")
        for button in (self.back_button, self.next_button, self.finish_button,
                       self.cancel_button):
            button.setAutoDefault(False)
            foot.addWidget(button)
        root.addLayout(foot)
        self.back_button.clicked.connect(self.back)
        self.next_button.clicked.connect(self.next)
        self.finish_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self._go(0)

    # --- paging -------------------------------------------------------------

    def current(self) -> int:
        return self.stack.currentIndex()

    def next(self) -> None:  # noqa: A003 - reads as the button it is
        if self.current() < len(self.pages) - 1:
            self._go(self.current() + 1)

    def back(self) -> None:
        if self.current() > 0:
            self._go(self.current() - 1)

    def _go(self, index: int) -> None:
        page = self.pages[index]
        page.enter()
        self.stack.setCurrentIndex(index)
        self._title.setText(
            f"<span style='font-size:large; font-weight:600'>{tr(page.title)}</span>"
        )
        self._subtitle.setText(page.subtitle)
        self._step.setText(tr("Step {n} of {total}").format(n=index + 1, total=len(self.pages)))
        last = index == len(self.pages) - 1
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(not last)
        self.finish_button.setVisible(last)
        # Enter presses Next, and Finish on the last page.
        (self.finish_button if last else self.next_button).setDefault(True)

    # --- closing ------------------------------------------------------------

    def save(self) -> None:
        """Every choice but the theme, which :meth:`settle_theme` applies."""
        settings.save_addresses_cdb(self.book.choice())
        settings.save_language(self.look.language_choice())
        settings.save_icon_source(str(self.look.icons.currentData()))
        settings.save_key_hints(self.keys.key_hints.isChecked())
        settings.save_start_mode(self.changes.choice())
        settings.save_safe_mode_idle_secs(int(self.safety.idle.currentData()))
        settings.save_dropdown_marks(self.lists.dropdown_marks.isChecked())
        settings.save_form_status_line(self.lists.form_status_line.isChecked())
        settings.save_titlebar_tabs(self.lists.titlebar_tabs.isChecked())
        settings.save_most_used_auto(self.lists.most_used.isChecked())
        settings.save_live_updates(self.lists.live_updates.isChecked())
        settings.save_update_check(self.welcome.update_check.isChecked())
        settings.save_auto_export(self.snapshots.enabled.isChecked())
        settings.save_auto_export_sensitive(
            self.snapshots.enabled.isChecked() and self.snapshots.sensitive.isChecked()
        )
        settings.save_audit(self.audit.enabled.isChecked())
        settings.save_audit_level(str(self.audit.level.currentData()))

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        if result == QDialog.DialogCode.Accepted:
            self.save()
        settings.save_welcome_seen()
        super().done(result)

    def settle_theme(self, result: int) -> None:
        """Keep the previewed theme on Finish; put the old one back otherwise."""
        if result == QDialog.DialogCode.Accepted:
            tid, scale = self.look.choice()
            persist = True
        else:
            (tid, scale), persist = self._theme_before, False
        style.apply_theme(QApplication.instance(), tid, persist=persist, scale=scale)

    def exec(self) -> int:  # noqa: A003 - Qt override
        result = super().exec()
        self.settle_theme(result)
        return result


def run_first_start(parent=None) -> bool:
    """Show the welcome if it has not been seen. True when it was shown."""
    if settings.load_welcome_seen():
        return False
    WelcomeWizard(parent).exec()
    # Nothing but the tour has been built yet, so a language picked in it can
    # take over now: the main window comes up in it.
    language = i18n.resolve(settings.load_language())
    if language != i18n.active():
        i18n.load(language)
        i18n.install()
    return True
