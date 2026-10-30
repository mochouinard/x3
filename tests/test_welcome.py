"""The first-start welcome: it shows once, starts from the current settings,
saves only on Finish, and leaves the theme alone on Cancel.

    python tests/run.py welcome

The tour is the only place a new user hears that x3 can keep config snapshots
and audit files, and that sessions start in Live. So it has to show on a fresh
config, and only there; every switch it offers has to be pre-set to what the
setting already is, so that pressing Finish without touching anything changes
nothing; and a Cancel must not leave a previewed theme on screen.
"""

import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-welcome-")
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="x3-welcome-data-")
# Nothing on this machine's real paths may leak into what the tour finds.
os.environ["HOME"] = tempfile.mkdtemp(prefix="x3-welcome-home-")
os.environ.pop("X3_ADDRESSES_CDB", None)
os.environ.pop("X3_CDB", None)

from PySide6.QtWidgets import QApplication, QDialog

from x3.ui import settings, style
from x3.ui.widgets import welcome_wizard as ww

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv[:1])


def finish(wiz, code):
    """What exec() does once the dialog closes: done(), then the theme."""
    wiz.done(code)
    wiz.settle_theme(code)

style.apply_theme(app, "light", persist=False, scale=1.0)

# --- first start ------------------------------------------------------------
check("a fresh config has not seen the welcome", settings.load_welcome_seen(), False)
check("...nor answered the update question", settings.update_check_chosen(), False)

wiz = ww.WelcomeWizard()
check("eleven pages, welcome to done", len(wiz.pages), 11)
check("the theme starts on the active one", wiz.look.choice(), ("light", 1.0))
check("the start mode starts on the saved one", wiz.changes.choice(), "live")
check("snapshots start on (their default)", wiz.snapshots.enabled.isChecked(), True)
check("secrets in snapshots start off", wiz.snapshots.sensitive.isChecked(), False)
check("audit starts off", wiz.audit.enabled.isChecked(), False)
check("the update check is asked on the first page, ticked for a new user",
      (wiz.pages[0] is wiz.welcome, wiz.welcome.update_check.isChecked()), (True, True))
check("the switches start on their saved values",
      (wiz.look.icons.currentData(), wiz.keys.key_hints.isChecked(),
       wiz.safety.idle.currentData(), wiz.lists.dropdown_marks.isChecked(),
       wiz.lists.form_status_line.isChecked(), wiz.lists.titlebar_tabs.isChecked(),
       wiz.lists.most_used.isChecked(), wiz.lists.live_updates.isChecked()),
      (settings.load_icon_source(), settings.load_key_hints(),
       settings.load_safe_mode_idle_secs(), settings.load_dropdown_marks(),
       settings.load_form_status_line(), settings.load_titlebar_tabs(),
       settings.load_most_used_auto(), settings.load_live_updates()))
check("the audit level is greyed while audit is off", wiz.audit.level.isEnabled(), False)

# Finish with nothing touched: the settings read back exactly as before.
def everything():
    return (settings.load_start_mode(), settings.load_auto_export(),
            settings.load_auto_export_sensitive(), settings.load_audit(),
            settings.load_icon_source(), settings.load_key_hints(),
            settings.load_safe_mode_idle_secs(), settings.load_dropdown_marks(),
            settings.load_form_status_line(), settings.load_titlebar_tabs(),
            settings.load_most_used_auto(), settings.load_live_updates())


before = everything()
finish(wiz, QDialog.DialogCode.Accepted)
after = everything()
check("Finish untouched changes nothing", after, before)
check("...and marks the welcome seen", settings.load_welcome_seen(), True)
check("...and records the update answer it showed",
      (settings.update_check_chosen(), settings.load_update_check()), (True, True))
wiz = ww.WelcomeWizard()
wiz.welcome.update_check.setChecked(False)
finish(wiz, QDialog.DialogCode.Accepted)
check("unticking it turns the check off", settings.load_update_check(), False)
check("...and the tour shows that choice next time, not the recommendation",
      ww.WelcomeWizard().welcome.update_check.isChecked(), False)
check("run_first_start does not show it again", ww.run_first_start(), False)

# --- choices are saved on Finish --------------------------------------------
wiz = ww.WelcomeWizard()
wiz.look.theme.setCurrentIndex(wiz.look.theme.findData("dark"))
check("picking a theme previews it at once", style.base_theme().id, "dark")
for button in wiz.changes.group.buttons():
    if button.property("mode") == "queue":
        button.setChecked(True)
wiz.snapshots.sensitive.setChecked(True)
wiz.audit.enabled.setChecked(True)
check("ticking audit enables its level", wiz.audit.level.isEnabled(), True)
wiz.audit.level.setCurrentIndex(wiz.audit.level.findData("all"))
wiz.look.icons.setCurrentIndex(wiz.look.icons.findData("mono"))
wiz.keys.key_hints.setChecked(False)
wiz.safety.idle.setCurrentIndex(wiz.safety.idle.findData(900))
wiz.lists.titlebar_tabs.setChecked(True)
wiz.lists.live_updates.setChecked(False)
wiz.done_page.enter()
summary = wiz.done_page.summary.text()
check("the summary names every choice",
      all(w in summary for w in ("Dark", "Queue", "with passwords and keys", "on (everything)",
                                 "monochrome icons", "after 15 minutes", "title-bar tabs")),
      True)
finish(wiz, QDialog.DialogCode.Accepted)
check("the theme is saved", settings.load_theme_id(), "dark")
check("the start mode is saved", settings.load_start_mode(), "queue")
check("secrets in snapshots are saved", settings.load_auto_export_sensitive(), True)
check("audit is saved", (settings.load_audit(), settings.load_audit_level()), (True, "all"))
check("the new pages' switches are saved",
      (settings.load_icon_source(), settings.load_key_hints(),
       settings.load_safe_mode_idle_secs(), settings.load_titlebar_tabs(),
       settings.load_live_updates()),
      ("mono", False, 900, True, False))

# Secrets ride on snapshots: with snapshots off, the secret switch saves off.
wiz = ww.WelcomeWizard()
wiz.snapshots.enabled.setChecked(False)
check("turning snapshots off greys the secrets box", wiz.snapshots.sensitive.isEnabled(), False)
finish(wiz, QDialog.DialogCode.Accepted)
check("no snapshots means no secrets either",
      (settings.load_auto_export(), settings.load_auto_export_sensitive()), (False, False))

# --- Cancel ------------------------------------------------------------------
style.apply_theme(app, "light", persist=True, scale=1.0)
settings.save_audit(False)
wiz = ww.WelcomeWizard()
wiz.look.theme.setCurrentIndex(wiz.look.theme.findData("classic"))
wiz.audit.enabled.setChecked(True)
finish(wiz, QDialog.DialogCode.Rejected)
check("Cancel puts the previewed theme back", style.base_theme().id, "light")

# The theme is settled after the dialog closes, never from inside done().
calls = []
real_apply = style.apply_theme
style.apply_theme = lambda *a, **k: calls.append(k) or real_apply(*a, **k)
wiz = ww.WelcomeWizard()
wiz.done(QDialog.DialogCode.Rejected)
wiz.done(QDialog.DialogCode.Accepted)
style.apply_theme = real_apply
check("done() itself never restyles the application", calls, [])

# Paging: Back is off on the first page, Finish only on the last, and the
# last page's summary is rebuilt every time it is entered.
wiz = ww.WelcomeWizard()
check("first page: no way back", wiz.back_button.isEnabled(), False)
check("...and no Finish yet", wiz.finish_button.isVisibleTo(wiz), False)
for _ in range(10):
    wiz.next()
check("Next stops at the last page", wiz.current(), len(wiz.pages) - 1)
check("the last page offers Finish instead of Next",
      (wiz.finish_button.isVisibleTo(wiz), wiz.next_button.isVisibleTo(wiz)), (True, False))
check("...and Enter presses it", wiz.finish_button.isDefault(), True)
check("the summary was built on the way in", "Theme" in wiz.done_page.summary.text(), True)
wiz.back()
check("Back goes one page back", wiz.current(), len(wiz.pages) - 2)
finish(wiz, QDialog.DialogCode.Rejected)
check("...and saves no choice", (settings.load_theme_id(), settings.load_audit()),
      ("light", False))

# --- the address book ----------------------------------------------------------
from pathlib import Path  # noqa: E402

from x3.proto import cdb, message as m  # noqa: E402
from x3.ui import services  # noqa: E402


def book(path, notes):
    entries = []
    for note in notes:
        msg = m.Message()
        msg.add_string(cdb.F_HOST, "192.0.2.1")
        msg.add_string(cdb.F_LOGIN, "admin")
        msg.add_string(cdb.F_PASSWORD, "pw")
        msg.add_string(cdb.F_NOTE, note)
        entries.append(cdb.CdbEntry(msg))
    cdb.CdbFile(entries).save(str(path))
    return path


settings.save_addresses_cdb(None)
wiz = ww.WelcomeWizard()
check("nothing found and nothing chosen", wiz.book.in_use(), (None, 0))
check("...and the page says so", "No address book found" in wiz.book.where.text(), True)

elsewhere = Path(tempfile.mkdtemp(prefix="x3-welcome-cdb-")) / "office.cdb"
book(elsewhere, ["r1", "r2", "r3"])
broken = elsewhere.with_name("broken.cdb")
broken.write_bytes(b"not a cdb")

from PySide6.QtWidgets import QFileDialog  # noqa: E402

real_open = QFileDialog.getOpenFileName
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(broken), ""))
wiz.book._choose()
check("a file that does not read is not taken", wiz.book.choice(), None)
check("...and the page says why", "could not be read" in wiz.book.where.text(), True)
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(elsewhere), ""))
wiz.book._choose()
QFileDialog.getOpenFileName = real_open
check("a chosen book is shown with its count", wiz.book.in_use(), (str(elsewhere), 3))
finish(wiz, QDialog.DialogCode.Accepted)
check("Finish saves the choice", settings.load_addresses_cdb(), str(elsewhere))
check("...and it is the first place every lookup tries",
      services.find_addresses_cdb(), elsewhere)

wiz = ww.WelcomeWizard()
check("the tour reopens on the saved choice", wiz.book.choice(), str(elsewhere))
wiz.book._auto()
finish(wiz, QDialog.DialogCode.Accepted)
check("Find automatically forgets it", settings.load_addresses_cdb(), None)

# --- reachable later -----------------------------------------------------------
from x3.ui.session import SessionInfo  # noqa: E402
from x3.ui.widgets.main_window import MainWindow  # noqa: E402

class StubClient:
    readonly = True
    schema = None
    session_log = None

    def nav_tree(self):
        return []

    def close(self):
        pass


win = MainWindow(SessionInfo(host="192.0.2.1", user="admin"), client=StubClient())
help_act = next(a for a in win.menuBar().actions() if a.text() == "&Help")
check("Help offers the tour", [a.text() for a in help_act.menu().actions()][:1],
      ["&Welcome Tour…"])

ww.WelcomeWizard.exec = lambda self: (self.done_page.enter(),
                                      self.changes.group.buttons()[2].setChecked(True),
                                      finish(self, QDialog.DialogCode.Accepted),
                                      QDialog.DialogCode.Accepted)[-1]
settings.save_start_mode("live")
win._live_start_act.setChecked(True)
win._welcome_tour()
check("after the tour the start-mode toggle follows it",
      (settings.load_start_mode(), win._live_start_act.isChecked()), ("readonly", False))
check("...and so do the menu toggles the tour shares",
      (win._titlebar_tabs_act.isChecked(), win._live_updates_act.isChecked(),
       win._key_hints_act.isChecked(), win._idle_secs),
      (settings.load_titlebar_tabs(), settings.load_live_updates(),
       settings.load_key_hints(), settings.load_safe_mode_idle_secs()))
win.close()

if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall welcome checks passed")
