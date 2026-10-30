"""Offline tests for the connect dialog.

No device and no pytest required:

    .venv/bin/python test_connect_dialog.py

Covers, mostly on the Addresses tab:

* **Passwords.** Masked by default; Copy Password hands over the real secret
  even while masked, but Ctrl+C copies only what is displayed. Qt refuses
  clipboard copy out of a Password-echo QLineEdit, so the Show toggle is also
  what makes select-and-copy work on the form field.
* **Layout and chrome.** The connection form sits above the list and only the
  list stretches; tables have no row rules, no stripe, and left-aligned
  headers.
* **The local overlay.** Editing a cdb-backed row records only the fields that
  changed, so anything Winbox alters afterwards still shows through — that is
  the property most worth protecting, since getting it wrong silently freezes
  a row at whatever it looked like when first edited. Removing hides rather
  than deletes; Addresses.cdb is never written.
* **Sorting.** Ascending → descending → file order, with the row lookup
  following the sort so a click still opens the entry it displays.

XDG_CONFIG_HOME is redirected to a temp dir so the run never touches the real
~/.config/x3/ — neither settings.json nor connections.json.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_cfg = tempfile.mkdtemp(prefix="x3-test-")
os.environ["XDG_CONFIG_HOME"] = _cfg

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from x3.ui import connstore as cs
from x3.ui import settings, style
from x3.ui.services import AddressRow, NeighborRow
from x3.ui.widgets.column_rule import ColumnRuleDelegate
from x3.ui.widgets.connect_dialog import (
    COL_ADDRESS,
    COL_ADDR_FLAG,
    COL_ADDR_GROUP,
    COL_ADDR_HOST,
    COL_ADDR_NOTE,
    COL_ADDR_PASS,
    COL_ADDR_LAST,
    COL_ADDR_USER,
    NEIGHBOR_MAX_AGE_S,
    PASSWORD_MASK,
    ConnectDialog,
    ago,
    explain_connect_error,
    split_host_port,
)

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


def ok(label, cond):
    if not cond:
        failures.append(label)
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv)
app.setStyle("Fusion")
style.apply_theme(app, "light", persist=False)

ROWS = [
    AddressRow(0, "192.168.88.24", "admin", "ax-ap", "", "lab", "s3cr3t!"),
    AddressRow(1, "10.0.0.1", "ro", "edge", "", "", ""),
    AddressRow(2, "AA:BB:CC:DD:EE:FF", "admin", "mac-only", "", "lab", "pw2"),
]


def make(*, store: cs.Store | None = None) -> ConnectDialog:
    """A dialog holding ROWS as its cdb, with an empty local overlay."""
    d = ConnectDialog()
    d._store = store if store is not None else cs.Store()
    d._address_rows = list(ROWS)
    d._remerge()
    return d


# --- columns -------------------------------------------------------------

d = make()
headers = [
    d.addresses.horizontalHeaderItem(i).text()
    for i in range(d.addresses.columnCount())
]
check("Addresses columns", headers,
      ["", "Address", "User", "Password", "Note", "Group", "Last Used"])
check("column constants line up with the headers",
      (COL_ADDR_FLAG, COL_ADDR_HOST, COL_ADDR_USER,
       COL_ADDR_PASS, COL_ADDR_NOTE, COL_ADDR_GROUP),
      (0, 1, 2, 3, 4, 5))

# --- masking -------------------------------------------------------------

check("password masked while Show is off",
      d.addresses.item(0, COL_ADDR_PASS).text(), PASSWORD_MASK)
check("an entry with no password shows nothing, not a mask",
      d.addresses.item(1, COL_ADDR_PASS).text(), "")
check("form field starts echo-hidden",
      d.password.echoMode(), QLineEdit.EchoMode.Password)
ok("masked cell does not leak the secret in its tooltip",
   "s3cr3t!" not in (d.addresses.item(0, COL_ADDR_PASS).toolTip() or ""))

d.show_pw.setChecked(True)
check("password revealed by the Show toggle",
      d.addresses.item(0, COL_ADDR_PASS).text(), "s3cr3t!")
check("empty password stays empty when revealed",
      d.addresses.item(1, COL_ADDR_PASS).text(), "")
check("form field becomes selectable/copyable (Normal echo)",
      d.password.echoMode(), QLineEdit.EchoMode.Normal)
check("toggle persists to settings", settings.load_show_passwords(), True)

d.show_pw.setChecked(False)
check("re-masked on toggle off",
      d.addresses.item(0, COL_ADDR_PASS).text(), PASSWORD_MASK)
check("un-toggle persists too", settings.load_show_passwords(), False)

# --- the mask survives filtering ----------------------------------------

d.show_pw.setChecked(True)
d.cdb_filter.setText("mac-only")
check("filter narrows the view", d.addresses.rowCount(), 1)
check("filtered row keeps the revealed password",
      d.addresses.item(0, COL_ADDR_PASS).text(), "pw2")
check("filtered row maps back to the right entry",
      d._visible_address(0).host, "AA:BB:CC:DD:EE:FF")
d.cdb_filter.setText("")
check("clearing the filter restores every row", d.addresses.rowCount(), len(ROWS))
d.show_pw.setChecked(False)

# --- copy ----------------------------------------------------------------

clip = QGuiApplication.clipboard()

clip.setText("")
d._to_clipboard(ROWS[0].password, "password")
check("Copy Password yields the real secret while masked", clip.text(), "s3cr3t!")
ok("copy reports itself in the status line", "Copied password" in d.status.text())

clip.setText("")
d.addresses.setCurrentCell(0, COL_ADDR_HOST)
d._copy_current_cell(d.addresses)
check("Ctrl+C copies the cell under the cursor", clip.text(), "192.168.88.24")

clip.setText("")
d.addresses.setCurrentCell(0, COL_ADDR_PASS)
d._copy_current_cell(d.addresses)
check("Ctrl+C on a masked password copies the mask, not the secret",
      clip.text(), PASSWORD_MASK)

d.show_pw.setChecked(True)
clip.setText("")
d._copy_current_cell(d.addresses)
check("Ctrl+C on a revealed password copies the secret", clip.text(), "s3cr3t!")
d.show_pw.setChecked(False)

clip.setText("")
d.addresses.setCurrentCell(1, COL_ADDR_PASS)   # entry with no password
d._copy_current_cell(d.addresses)
check("copying an empty cell is a no-op", clip.text(), "")

# --- layout: connection info on top, list filling the rest ---------------

order = [
    d.layout().itemAt(i).widget() or d.layout().itemAt(i).layout()
    for i in range(d.layout().count())
]
form_at = next(i for i, w in enumerate(order) if d.host in w.findChildren(QLineEdit))
tabs_at = order.index(d.tabs)
ok("the connection form sits above the device list", form_at < tabs_at)
check("only the list stretches",
      [d.layout().stretch(i) for i in range(d.layout().count())].count(1), 1)
ok("...and it is the list that does", d.layout().stretch(tabs_at) == 1)
ok("theme moved off the connection form onto the button row",
   d.theme_combo not in (order[form_at].findChildren(type(d.theme_combo))))

# --- table chrome ---------------------------------------------------------

for name, table in (("addresses", d.addresses), ("neighbors", d.neighbors)):
    ok(f"{name}: row rules off", not table.showGrid())
    ok(f"{name}: no alternating stripe", not table.alternatingRowColors())
    hdr = table.horizontalHeader()
    check(f"{name}: header left-aligned",
          bool(hdr.defaultAlignment() & Qt.AlignmentFlag.AlignLeft), True)
    misaligned = [
        table.horizontalHeaderItem(c).text()
        for c in range(table.columnCount())
        if table.horizontalHeaderItem(c) is not None
        and not (table.horizontalHeaderItem(c).textAlignment()
                 & Qt.AlignmentFlag.AlignLeft)
    ]
    check(f"{name}: header items left-aligned too", misaligned, [])
    ok(f"{name}: column rule delegate installed",
       isinstance(table.itemDelegate(), ColumnRuleDelegate))

# --- the item count Winbox shows -----------------------------------------

d.cdb_filter.setText("")
check("count with no filter", d._address_count_text(), "3 saved connection(s)")
d.cdb_filter.setText("lab")          # matches the group on rows 0 and 2
check("count says how much the filter hides",
      d._address_count_text(), "2 of 3 saved connections")
ok("...and it reaches the status line", "2 of 3" in d.status.text())
d.cdb_filter.setText("")

# --- picking a row still works ------------------------------------------

d._pick_address(0, COL_ADDR_PASS)
check("clicking the Password cell still selects the entry",
      (d.host.text(), d.user.text(), d.password.text()),
      ("192.168.88.24", "admin", "s3cr3t!"))
check("selecting a MAC entry switches transport",
      (d._pick_address(2, COL_ADDR_HOST), d._transport)[1], "mac")

d.close()

# =========================================================================
# Local overlay on a read-only Addresses.cdb
# =========================================================================

d = make()
check("Note and Group are on the connect form",
      (d.note.text(), d.group.text()), ("", ""))
d._pick_address(0, COL_ADDR_HOST)
check("picking a row fills Note and Group",
      (d.note.text(), d.group.text()), ("ax-ap", "lab"))
d._pick_address(1, COL_ADDR_HOST)
check("...and clears them for a row that has none — no carry-over",
      (d.note.text(), d.group.text()), ("edge", ""))

check("an untouched cdb row carries no flag",
      [d.addresses.item(r, COL_ADDR_FLAG).text() for r in range(3)], ["", "", ""])

# --- editing a cdb row ----------------------------------------------------

d = make()
d.addresses.selectRow(0)
d._pick_address(0, COL_ADDR_HOST)
d.note.setText("renamed locally")
d._save_entry()
check("the edit shows in the table",
      d.addresses.item(0, COL_ADDR_NOTE).text(), "renamed locally")
check("...flagged L", d.addresses.item(0, COL_ADDR_FLAG).text(), "L")
ok("...with an explaining tooltip",
   "edited locally" in d.addresses.item(0, COL_ADDR_FLAG).toolTip())
check("only the changed field is overridden",
      d._store.overrides[cs.entry_key("192.168.88.24", "admin")],
      {"note": "renamed locally"})
ok("Addresses.cdb list is untouched in memory", d._address_rows[0].note == "ax-ap")

# The whole point of a minimal patch: upstream keeps showing through.
d._address_rows[0] = AddressRow(
    0, "192.168.88.24", "admin", "ax-ap", "", "PROD", "rotated-pw"
)
d._remerge()
c = d._filtered_view[0]
check("a group changed in Winbox shows through", c.group, "PROD")
check("a password rotated in Winbox shows through", c.password, "rotated-pw")
check("...while our note still wins", c.note, "renamed locally")

# --- revert ---------------------------------------------------------------

d.addresses.selectRow(0)
ok("Revert is offered for a locally edited row", d.btn_revert.isEnabled())
d._revert_entry()
check("revert restores the cdb note",
      d.addresses.item(0, COL_ADDR_NOTE).text(), "ax-ap")
check("...and drops the flag", d.addresses.item(0, COL_ADDR_FLAG).text(), "")
d.addresses.selectRow(1)
ok("Revert is not offered for an unedited row", not d.btn_revert.isEnabled())

# --- adding one of our own ------------------------------------------------

d = make()
d.addresses.clearSelection()
d.host.setText("lab.local:8291")
d.user.setText("admin")
d.password.setText("labpw")
d.note.setText("bench ap")
d.group.setText("lab")
d._save_entry()
last = d.addresses.rowCount() - 1
check("our entry is appended", d.addresses.rowCount(), len(ROWS) + 1)
check("...flagged N", d.addresses.item(last, COL_ADDR_FLAG).text(), "N")
check("...with its note", d.addresses.item(last, COL_ADDR_NOTE).text(), "bench ap")
ok("count line calls out how many are local", "1 local" in d._address_count_text())

# Retyping the address means a new entry, not an edit of the selected row.
d.addresses.selectRow(0)
d._pick_address(0, COL_ADDR_HOST)
d.host.setText("192.168.88.99")
d.note.setText("a different box")
d._save_entry()
check("changing the address saves a new entry instead of editing",
      d.addresses.rowCount(), len(ROWS) + 2)
check("...leaving the original alone",
      d.addresses.item(0, COL_ADDR_NOTE).text(), "ax-ap")

# --- removing -------------------------------------------------------------

_yes = QMessageBox.StandardButton.Yes
QMessageBox.question = staticmethod(lambda *a, **k: _yes)   # auto-confirm

d = make()
before = len(d._address_rows)
d.addresses.selectRow(1)
d._remove_entry()
check("the row is gone from the list", d.addresses.rowCount(), len(ROWS) - 1)
check("...but not from the cdb we read", len(d._address_rows), before)
ok("...it is recorded as hidden, not deleted", d._store.hidden)
check("unhide brings it back", d._store.unhide_all(), 1)

# --- sorting --------------------------------------------------------------

d = make()
addrs = lambda: [d.addresses.item(r, COL_ADDR_HOST).text()
                 for r in range(d.addresses.rowCount())]
check("file order to begin with", addrs(),
      ["192.168.88.24", "10.0.0.1", "AA:BB:CC:DD:EE:FF"])

d._sort_addresses_by(COL_ADDR_HOST)
check("first click sorts ascending, addresses numerically",
      addrs(), ["10.0.0.1", "192.168.88.24", "AA:BB:CC:DD:EE:FF"])
check("the row lookup follows the sort",
      d._visible_address(0).host, "10.0.0.1")
ok("...and keeps its identity", d._visible_address(0).login == "ro")

d._sort_addresses_by(COL_ADDR_HOST)
check("second click descends",
      addrs(), ["AA:BB:CC:DD:EE:FF", "192.168.88.24", "10.0.0.1"])
d._sort_addresses_by(COL_ADDR_HOST)
check("third click returns to Addresses.cdb order", addrs(),
      ["192.168.88.24", "10.0.0.1", "AA:BB:CC:DD:EE:FF"])
check("...and clears the indicator", d._addr_sort, None)

d._sort_addresses_by(COL_ADDR_USER)
check("sorting by another column works too",
      [d.addresses.item(r, COL_ADDR_USER).text() for r in range(3)],
      ["admin", "admin", "ro"])

# Picking a row after a sort must still select the right connection.
d._sort_addresses_by(COL_ADDR_HOST)      # ascending
d._pick_address(0, COL_ADDR_HOST)
check("picking a sorted row loads that row's entry",
      (d.host.text(), d.user.text(), d.note.text()), ("10.0.0.1", "ro", "edge"))

# --- neighbours sort ------------------------------------------------------

d._neighbor_rows = [
    NeighborRow("r10", "10.0.0.10", "AA:BB:CC:00:00:0A", "hEX", "7.16", "3d 04:00:00", ""),
    NeighborRow("r2", "10.0.0.2", "AA:BB:CC:00:00:02", "ap", "7.19", "00:05:00", ""),
]
d._fill_neighbor_table()
d._sort_neighbors_by(1)     # Address
check("neighbours sort by address numerically",
      [d.neighbors.item(r, 1).text() for r in range(2)], ["10.0.0.2", "10.0.0.10"])
ok("...and the backing list is reordered with the view, so clicks still map",
   d._neighbor_rows[0].address == "10.0.0.2")

# --- Save lives with the form, not inside one tab -------------------------

d = make()
ok("Save is not parented to the Addresses page",
   d.btn_save.parent() is not d.addresses.parent())
ok("Remove and Revert are — they act on the selected row",
   d.btn_remove.parent() is d.addresses.parent())

check("no hint until there is something to save", d._save_hint.text(), "→ adds a new entry")
d.addresses.selectRow(0)
d._pick_address(0, COL_ADDR_HOST)
check("hint says it will update the picked row",
      d._save_hint.text(), "→ updates 192.168.88.24 (admin)")
d.note.setText("still the same box")
check("...editing a descriptive field keeps it an update",
      d._save_hint.text(), "→ updates 192.168.88.24 (admin)")
d.host.setText("192.168.88.99")
check("...retyping the address makes it an add",
      d._save_hint.text(), "→ adds a new entry")
d.host.setText("")
check("...and nothing to say without an address", d._save_hint.text(), "")

# Saving a device discovered on Neighbors, without switching tabs.
d = make()
d.tabs.setCurrentIndex(0)
d.addresses.selectRow(0)
d._pick_address(0, COL_ADDR_HOST)          # leaves note/group filled in
d._neighbor_rows = [
    NeighborRow("bench-ax2", "192.168.88.77", "AA:BB:CC:00:00:77", "ax-ap",
                "7.19", "1d 00:00:00", ""),
]
d._fill_neighbor_table()
d._pick_neighbor_cell(0, COL_ADDRESS)
check("picking a neighbour takes its identity as the note",
      d.note.text(), "bench-ax2")
check("...and clears a group left over from the other tab", d.group.text(), "")
d.user.setText("admin")
d.password.setText("pw")
d.btn_save.click()
saved = d._filtered_view[-1]
check("saved from the Neighbors tab", saved.host, "192.168.88.77")
check("...with the neighbour's own note, not the previous row's",
      (saved.note, saved.group), ("bench-ax2", ""))

# --- guard ----------------------------------------------------------------

d = make()
d.addresses.clearSelection()
d.host.setText("")
d.user.setText("admin")
n_before = d.addresses.rowCount()
_warned = []
QMessageBox.warning = staticmethod(lambda *a, **k: _warned.append(a))
d._save_entry()
check("saving without an address is refused", d.addresses.rowCount(), n_before)
ok("...and says why", bool(_warned))

d.close()

# --- Connect To: host, port and the mode line ----------------------------

check("bare host takes the Winbox port", split_host_port("10.0.0.1"), ("10.0.0.1", 8291))
check("host:port", split_host_port("10.0.0.1:8292"), ("10.0.0.1", 8292))
check("bare IPv6 has no port", split_host_port("fe80::1"), ("fe80::1", 8291))
check("bracketed IPv6 with a port", split_host_port("[fe80::1]:8299"), ("fe80::1", 8299))
check("bracketed IPv6 without one", split_host_port("[fe80::1]"), ("fe80::1", 8291))

d = make()
d.host.setText("10.0.0.1:8292")
ok("the mode line says the port that was typed", "8292" in d.mode_label.text())

# --- picking an entry replaces the secret, never keeps the last one --------

d.show_pw.setChecked(True)
d._pick_address(0, COL_ADDR_HOST)                  # has a password
check("picked entry's password is loaded", d.password.text(), "s3cr3t!")
d._pick_address(1, COL_ADDR_HOST)                  # has none
check("...and an entry without one clears it", d.password.text(), "")
check("...login follows the entry too", d.user.text(), "ro")
d.show_pw.setChecked(False)

# --- hidden cdb rows can come back ----------------------------------------

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
d.addresses.selectRow(0)
d._remove_entry()
check("Remove hides the cdb row", d.addresses.rowCount(), len(ROWS) - 1)
d._restore_hidden()
check("Restore Hidden brings it back", d.addresses.rowCount(), len(ROWS))
check("...and leaves nothing hidden", d._store.hidden, set())
d.close()

# --- folds -----------------------------------------------------------------

settings.save_audit(False)
settings.save_connect_fold("entry", False)
settings.save_connect_fold("session", False)
d = make()
d.resize(700, 600)
d.show()
app.processEvents()
ok("Note is folded away by default", not d.note.isVisible())
ok("...as is Auto Refresh", not d.refresh_rate.isVisible())
ok("Login and Password are not", d.user.isVisible() and d.password.isVisible())
d.entry_fold.set_expanded(True)
app.processEvents()
ok("opening Saved Entry shows Note, Group and Save",
   d.note.isVisible() and d.group.isVisible() and d.btn_save.isVisible())
check("...and is remembered", settings.load_connect_fold("entry"), True)
ok("tab order: Show -> Try all saved", d.show_pw.nextInFocusChain() is d.try_all)
ok("...Group -> Save", d.group.nextInFocusChain() is d.btn_save)
ok("the scan timer runs while the page is up", d._scan_timer.isActive())
d.hide()
ok("...and stops once it is hidden", not d._scan_timer.isActive())
d.close()

settings.save_audit(True)
d = make()
ok("Session opens by itself while auditing is on", d.session_fold.expanded)
settings.save_audit(False)
d.close()

# --- link colour follows the theme ----------------------------------------

style.apply_theme(app, "dark", persist=False)
dark_link = style.LINK
style.apply_theme(app, "light", persist=False)
ok("links lighten on a dark theme", dark_link != style.LINK)

# --- neighbours accumulate, age out, and keep the picked cell --------------

A = NeighborRow("a", "10.0.0.1", "AA:00:00:00:00:01", "hEX", "7.19", "1d", "")
B = NeighborRow("b", "10.0.0.2", "AA:00:00:00:00:02", "ap", "7.19", "1d", "")
d = make()
d._neigh_seen = {}
d._on_neighbors([A, B])
check("first scan lists both", d.neighbors.rowCount(), 2)
d.neighbors.setCurrentCell(1, COL_ADDRESS)
d._on_neighbors([B])
check("a device missing from one scan stays", d.neighbors.rowCount(), 2)
check("...and the picked cell stays on the same device",
      d.neighbors.item(d.neighbors.currentRow(), COL_ADDRESS).text(), "10.0.0.2")
mac_a, (row_a, seen_a) = A.mac, d._neigh_seen[A.mac]
d._neigh_seen[mac_a] = (row_a, seen_a - NEIGHBOR_MAX_AGE_S - 1)
d._on_neighbors([B])
check("a device silent past the age limit goes", d.neighbors.rowCount(), 1)
d.status.setText("Saved something")
d._on_neighbors([B])
check("a timer rescan leaves the status line alone", d.status.text(), "Saved something")
d.close()

# --- a starred neighbour brings its saved login ----------------------------

store = cs.Store()
store.record_device({"key": "SN1", "macs": ["AA:00:00:00:00:24"],
                     "host": "192.168.88.24", "login": "admin"})
d = make(store=store)
d.user.setText("someone-else")
d.password.setText("stale")
d._neighbor_rows = [NeighborRow("hap", "192.168.88.99", "AA:00:00:00:00:24",
                                "ax-ap", "7.19", "1d", "")]
d._fill_neighbor_table()
d._pick_neighbor_cell(0, COL_ADDRESS)
check("★ neighbour fills the login it was saved under", d.user.text(), "admin")
check("...and that entry's password", d.password.text(), "s3cr3t!")
check("...and its note and group", (d.note.text(), d.group.text()), ("ax-ap", "lab"))
check("...but connects to where it is now", d.host.text(), "192.168.88.99")
ok("the saved entry is marked live by its MAC, though its IP moved",
   "On the LAN now" in d.addresses.item(0, COL_ADDR_HOST).toolTip())
ok("...and an entry not on the LAN is not",
   not d.addresses.item(1, COL_ADDR_HOST).toolTip())
d.close()

# --- Last Used -------------------------------------------------------------

check("never used is blank", ago(0), "")
check("minutes", ago(1000, now=1000 + 300), "5m ago")
check("hours", ago(1000, now=1000 + 7200), "2h ago")
check("days", ago(1000, now=1000 + 3 * 86400), "3d ago")
store = cs.Store()
store.record_device({"key": "SN2", "macs": ["AA:00:00:00:00:01"], "host": "10.0.0.1",
                     "login": "ro", "seen_at": 1_700_000_000})
d = make(store=store)
check("Last Used shows a login made from here",
      bool(d.addresses.item(1, COL_ADDR_LAST).text()), True)
check("...and blank where there was none", d.addresses.item(0, COL_ADDR_LAST).text(), "")
d._sort_addresses_by(COL_ADDR_LAST)
d._sort_addresses_by(COL_ADDR_LAST)            # descending: most recent first
check("sorting by Last Used puts the used one first",
      d._visible_address(0).host, "10.0.0.1")
d.close()

# --- Group filter ----------------------------------------------------------

d = make()
groups = [d.group_filter.itemText(i) for i in range(d.group_filter.count())]
check("group filter lists the groups", groups, ["All groups", "lab"])
d.group_filter.setCurrentText("lab")
check("...and narrows to one", d.addresses.rowCount(), 2)
d.cdb_filter.setText("mac")
check("...together with the text filter", d.addresses.rowCount(), 1)
d.cdb_filter.setText("")
d.group_filter.setCurrentIndex(0)
check("All groups shows everything again", d.addresses.rowCount(), len(ROWS))
d.close()

# --- keyboard --------------------------------------------------------------

d = make()
d.resize(700, 600)
d.show()
app.processEvents()
QTest.keyClick(d, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
ok("Ctrl+F lands in the address filter", d.cdb_filter.hasFocus())
QTest.keyClicks(d.cdb_filter, "10.")
QTest.keyClick(d.cdb_filter, Qt.Key.Key_Down)
app.processEvents()
ok("Down walks from the filter into the list", d.addresses.hasFocus())
check("...and loads the row it lands on", d.host.text(), "10.0.0.1")
d.cdb_filter.setText("")
d.addresses.setFocus()
d.addresses.setCurrentCell(0, COL_ADDR_HOST)
QTest.keyClick(d.addresses, Qt.Key.Key_Down)
check("arrowing through the list loads each entry", d.host.text(), d._visible_address(1).host)
QTest.keyClicks(d.addresses, "mac")
ok("typing in the list types into the filter", d.cdb_filter.text() == "mac")
fired = []
d._connect = lambda: fired.append(1)
d.addresses.setFocus()
QTest.keyClick(d.addresses, Qt.Key.Key_Return)
check("Enter on a row connects", fired, [1])
# Enter in a filter used to reach the default button, and so connect to
# whatever the form last held -- not what typing a search means.
clicked = []
d._btn_connect.clicked.connect(lambda: clicked.append(1))
for box in (d.cdb_filter, d.group_filter):
    box.setFocus()
    QTest.keyClick(box, Qt.Key.Key_Return)
    QTest.keyClick(box, Qt.Key.Key_Enter)
app.processEvents()
check("Enter in either filter connects to nothing", (fired, clicked), ([1], []))
ok("Connect stays the dialog's default after focus has visited other buttons",
   (d.btn_save.setFocus(), d.addresses.setFocus(), d._btn_connect.isDefault())[-1])
d._shutdown_workers()
d.hide()
d.close()

# --- connect feedback --------------------------------------------------------

title, _hint = explain_connect_error(
    "DeviceError: device rejected login: invalid user name or password (6)")
check("bad login is named", title, "Login rejected")
check("refused", explain_connect_error("DeviceError: [Errno 111] Connection refused")[0],
      "Connection refused")
_t, hint = explain_connect_error("DeviceError: timed out", has_mac=True)
ok("a timeout on a LAN device suggests MAC", "MAC" in hint)
check("MAC-Winbox timeout says so",
      explain_connect_error("MacWinboxError: timeout waiting for x", transport="mac")[0],
      "No answer")
check("anything else still says it failed", explain_connect_error("Boom: ?")[0],
      "Connect failed")

d = make()
_modal = []
QMessageBox.warning = staticmethod(lambda *a, **k: _modal.append(a))
d._pending_connect = {"transport": "tcp", "tcp_port": 8291}
d._set_connect_ui_busy(True)
d._on_connect_status("Authenticating…")
check("status says which step", d.status.text(), "Authenticating…  (step 2 of 4)")
d._on_connect_err("DeviceError: [Errno 111] Connection refused")
ok("a failed connect explains itself inline", "Connection refused" in d.error_banner.text())
ok("...without a modal box to dismiss", not _modal)
check("...and the raw error is kept in the tooltip",
      d.error_banner.toolTip(), "DeviceError: [Errno 111] Connection refused")
check("Connect is itself again", d._btn_connect.text(), "Connect")
d.close()

# --- the mode line swaps IP and MAC for a device on the LAN ----------------

d = make()
d._on_neighbors([NeighborRow("hap", "192.168.88.50", "AA:00:00:00:00:50",
                             "ap", "7.19", "1d", "")])
d.host.setText("10.99.99.99")
ok("no link for an address nobody on the LAN answers to",
   "use " not in d.mode_label.text())
check("...and nothing for Tab to stop on", d.mode_label.focusPolicy(), Qt.FocusPolicy.NoFocus)
d.host.setText("192.168.88.50:8291")
ok("an address MNDP hears offers its MAC", "use MAC AA:00:00:00:00:50" in d.mode_label.text())
check("...and Tab can reach it", d.mode_label.focusPolicy(), Qt.FocusPolicy.TabFocus)
d.mode_label.linkActivated.emit("swap")
check("following it targets the MAC", (d.host.text(), d._transport),
      ("AA:00:00:00:00:50", "mac"))
ok("...and the line now offers the IP back", "use IP 192.168.88.50" in d.mode_label.text())
d.mode_label.linkActivated.emit("swap")
check("following that goes back to TCP", (d.host.text(), d._transport),
      ("192.168.88.50", "tcp"))
d._pick_neighbor_cell(0, COL_ADDRESS)
d.host.setText("10.99.99.98")
ok("a retyped address never offers the last pick's MAC", "use " not in d.mode_label.text())
d.close()

store = cs.Store()
store.record_device({"key": "SN3", "macs": ["AA:00:00:00:00:24"],
                     "host": "192.168.88.24", "login": "admin"})
d = make(store=store)
d._on_neighbors([NeighborRow("moved", "192.168.88.77", "AA:00:00:00:00:24",
                             "ap", "7.19", "1d", "")])
d._pick_address(0, COL_ADDR_HOST)              # 192.168.88.24 / admin, old IP
ok("a saved entry whose box moved still offers its MAC",
   "use MAC AA:00:00:00:00:24" in d.mode_label.text())
d.close()

# --- Try all saved is one sweep, not a mode --------------------------------

d = make()
d._start_tryall_worker = lambda *a, **k: None
d.host.setText("10.0.0.1")
d.user.setText("admin")
d.try_all.setChecked(True)
d._connect()
ok("Try all saved un-ticks once it has run", not d.try_all.isChecked())
d._set_connect_ui_busy(False)
d.close()

print()
if failures:
    print(f"FAIL ({len(failures)})")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
print("all connect-dialog tests passed")
