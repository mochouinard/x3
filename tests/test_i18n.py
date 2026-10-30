"""Languages: exact-match tables, and what they must never touch.

    python tests/run.py i18n

A language is a JSON table from the English text to its own. A string the
table holds exactly is shown translated; anything else stays English. The
checks below hold the line that matters most: a value the router stores
(``on``, ``ether1``, what is in an edit box or a drop-down) is never
translated, so a French screen cannot send French to a router.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="x3-i18n-")

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QLabel, QLineEdit, QMenu, QPushButton,
)

from x3.ui import i18n  # noqa: E402

failures: list[str] = []


def check(label, got, want):
    if got != want:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok  {label}")


app = QApplication.instance() or QApplication(sys.argv[:1])
ROOT = Path(__file__).resolve().parents[1]

# --- the shipped tables ----------------------------------------------------------
langs = i18n.available()
check("English is always offered, and first", next(iter(langs)), "en")
check("French ships, under its own name", langs.get("fr"), "Français")
check("the translators' catalog is not a language", "catalog" in langs, False)

catalog = json.loads((ROOT / "tools" / "i18n-catalog.json").read_text(encoding="utf-8"))
known = {e["text"] for e in catalog["strings"]}
_TAG = re.compile(r"</?(\w+)")
_FIELD = re.compile(r"\{\w*\}")
for code in langs:
    if code == "en":
        continue
    data = json.loads((i18n.HERE / f"{code}.json").read_text(encoding="utf-8"))
    strings = data["strings"]
    check(f"{code}: every value is a string",
          all(isinstance(v, str) for v in strings.values()), True)
    check(f"{code}: lists every string the catalog knows (run --update {code})",
          sorted(known - set(strings))[:5], [])
    bad = [k for k, v in strings.items() if v and (
        sorted(_TAG.findall(k)) != sorted(_TAG.findall(v))
        or sorted(_FIELD.findall(k)) != sorted(_FIELD.findall(v))
        or k.count("\n") != v.count("\n"))]
    check(f"{code}: tags, {{fields}} and line breaks survive translation", bad[:5], [])

# --- English: Qt is left exactly as it ships ---------------------------------------
before = QLabel.setText
i18n.load("en")
i18n.install()
check("English installs nothing", QLabel.setText is before, True)
check("...and tr() is the identity", i18n.tr("Address List"), "Address List")

# --- resolving the choice ----------------------------------------------------------
saved = {k: os.environ.pop(k, None) for k in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")}
os.environ["LANG"] = "fr_CA.UTF-8"
check("auto follows a French desktop", i18n.resolve("auto"), "fr")
os.environ["LANG"] = "de_DE.UTF-8"
check("auto falls back to English without a table", i18n.resolve("auto"), "en")
check("an explicit choice wins over the desktop", i18n.resolve("fr"), "fr")
check("an unknown code is English", i18n.resolve("xx"), "en")
os.environ["LANGUAGE"] = "de_DE:fr_CA:en"
check("a preference list is walked to the first language x3 has",
      i18n.resolve("auto"), "fr")
for k, v in saved.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v

# --- the lookup rules, on a table of our own ----------------------------------------
i18n.load("fr")
i18n._table = {
    "Address List": "Liste d'adresses", "Name": "Nom", "&Theme": "&Thème",
    "Settings": "Paramètres", "read-only": "lecture seule", "on": "activé",
    "Changed elsewhere": "Modifié ailleurs", "English": "Anglais",
}
i18n._colon = " :"
tr = i18n.tr
check("exact match", tr("Address List"), "Liste d'adresses")
check("no match stays English", tr("Address Lists"), "Address Lists")
check("case is part of the key", tr("address list"), "address list")
check("a mnemonic the table holds is used as written", tr("&Theme"), "&Thème")
check("a mnemonic the table lacks goes on the first letter", tr("Settin&gs"), "&Paramètres")
check("a colon is the language's own", tr("Name:"), "Nom :")
check("an ellipsis is kept", tr("Settings…"), "Paramètres…")
check("a name in <...> is the router's and is kept",
      tr("Address List <Settings>"), "Liste d'adresses <Settings>")
check("a [suffix] is translated on its own", tr("Address List [read-only]"),
      "Liste d'adresses [lecture seule]")
check("so is each side of a dash", tr("Changed elsewhere — rt1"), "Modifié ailleurs — rt1")
check("verbatim() is never translated", tr(i18n.verbatim("English")), "English")
check("not a string passes through", tr(None), None)

# --- widgets ------------------------------------------------------------------------------
i18n.install()
check("a label is translated", QLabel("Name").text(), "Nom")
lab = QLabel()
lab.setText("Address List")
check("...and so is setText", lab.text(), "Liste d'adresses")
check("a button's mnemonic survives", QPushButton("&Theme").text(), "&Thème")
check("an action's text", QAction("Settings").text(), "Paramètres")
menu = QMenu()
check("a menu entry added by text", menu.addAction("Settings").text(), "Paramètres")
check("a window title", (lab.setWindowTitle("Settings"), lab.windowTitle())[1], "Paramètres")
check("a one-word value in a label is the router's, untouched", QLabel("on").text(), "on")
check("...though x3's own tr('on') still translates", tr("on"), "activé")
edit = QLineEdit()
edit.setText("Name")
check("an edit box holds what the router has", edit.text(), "Name")
combo = QComboBox()
combo.addItem("Name", "name")
check("so does a drop-down", combo.itemText(0), "Name")

# The router's labels outside widgets: menu entries and column headers.
from x3.ui.models.table_model import SimpleTableModel  # noqa: E402
from x3.ui.widgets.menu_tree import menu_label  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

check("a menu entry from the schema", menu_label({"label": "Address List"}), "Liste d'adresses")
model = SimpleTableModel(["Flags", "Name", "Unknown Column"], [])
heads = [model.headerData(i, Qt.Orientation.Horizontal) for i in range(3)]
check("column headers, the flag column staying blank", heads, ["", "Nom", "Unknown Column"])

# --- language pickers ----------------------------------------------------------------
picks = i18n.choices()
check("a picker offers System default, then every language",
      [code for _label, code in picks], ["auto", *i18n.available()])
check("...naming the language System default comes to",
      picks[0][0].endswith(f"({i18n.available()[i18n.system_language()]})"), True)
check("...and no label is ever translated",
      all(isinstance(label, i18n.verbatim) for label, _code in picks), True)

# --- the connection page: a language picked there applies at once -------------------
# No address book from this machine and no LAN scan: the page is fed its rows.
import x3.ui.widgets.connect_dialog as connect_mod  # noqa: E402
from x3.ui import settings  # noqa: E402
from x3.ui.services import AddressRow  # noqa: E402

connect_mod.discover_neighbors = lambda timeout=2.5: []
connect_mod.find_addresses_cdb = lambda: None
connect_mod.load_address_book = lambda path, include_passwords=True: (None, [], "no address book")
ConnectDialog = connect_mod.ConnectDialog
ROWS = [AddressRow(1, "10.0.0.7", "tester", "lab edge", "", "lab", ""),
        AddressRow(2, "10.0.0.8", "tester", "lab core", "", "lab", "")]


def connection_page(**kw):
    page = ConnectDialog(**kw)
    page._address_rows = list(ROWS)
    page._remerge()
    return page


i18n.load("en")
i18n.picked = None
page = connection_page(start_page=True)
page.tabs.setCurrentIndex(1)
page.cdb_filter.setText("core")
page._select_key(page._filtered_view[0].key)
page.host.setText("10.0.0.99")
page.user.setText("someone")
page.language_combo.setCurrentIndex(page.language_combo.findData("fr"))
check("picking a language closes the start page to be rebuilt",
      page.result(), ConnectDialog.LANGUAGE_CHANGED)
check("...saves it", settings.load_language(), "fr")
check("...and marks it picked in this run, over --lang", i18n.picked, "fr")

# What app.py does with that result.
i18n.load(i18n.resolve(i18n.picked))
i18n.install()
again = connection_page(start_page=True)
again.restore(page.carried())
check("the new page is in French", again._btn_connect.text(), "Connecter")
check("...its theme names too", again.theme_combo.itemText(
    again.theme_combo.findData("light")), "Clair")
check("...showing French as picked", again.language_combo.currentData(), "fr")
check("the form comes back as typed",
      (again.host.text(), again.user.text()), ("10.0.0.99", "someone"))
check("...the list as filtered, on its tab",
      (again.tabs.currentIndex(), again.cdb_filter.text(), len(again._filtered_view)),
      (1, "core", 1))
check("...with the same row selected",
      again._selected_connection().host if again._selected_connection() else None,
      "10.0.0.8")

# Opened from a session (New x3), other windows are up in the old language:
# the choice is saved for the next connection page and nothing closes.
side = connection_page()
side.language_combo.setCurrentIndex(side.language_combo.findData("en"))
check("a page opened from a session stays open", side.result(), 0)
check("...and the choice waits for the next page", (settings.load_language(), i18n.active()),
      ("en", "fr"))

if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall i18n checks passed")
