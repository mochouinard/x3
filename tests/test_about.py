"""Help ▸ About: the licence it shows is the repository's, and it names what
x3 runs on.

    python tests/run.py about

``LICENSE`` and the dialog's text are one licence written down twice, so a
change to either has to reach the other. The third-party list has to cover
every package ``pyproject.toml`` installs, or the About box under-reports
what a user is running.
"""

import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from x3.ui.widgets.about_dialog import (
    LICENSE_SUMMARY, THIRD_PARTY, AboutDialog, license_text,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures: list[str] = []


def ok(label, cond, detail=""):
    if cond:
        print(f"ok  {label}")
    else:
        failures.append(label)
        print(f"FAIL {label} {detail}")


with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
    license_file = fh.read()
ok("the dialog shows the LICENSE file itself", license_text() == license_file)
ok("which is PolyForm Strict with the copyright notice",
   license_file.startswith("Required Notice: Copyright (c) 2026 Marc O. Chouinard")
   and "# PolyForm Strict License 1.0.0" in license_file)
ok("plus the contribution permission and the LGPL / Apache carve-outs",
   all(h in license_file for h in ("# Additional Permission: Contributions",
                                    "# Other Licenses", "GNU LGPL",
                                    "LICENSES/Apache-2.0.txt")))
with open(os.path.join(ROOT, "LICENSE.fr.md"), encoding="utf-8") as fh:
    french = fh.read()
ok("the French version mirrors every section",
   all(h in french for h in ("# Licence PolyForm Strict 1.0.0",
                             "# Permission additionnelle : contributions",
                             "# Autres licences", "GNU LGPL",
                             "Avis requis : Copyright (c) 2026 Marc O. Chouinard"))
   and "LICENSE.fr.md" in license_file)
ok("the Apache text it points to is there",
   os.path.isfile(os.path.join(ROOT, "LICENSES", "Apache-2.0.txt")))

with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
    project = fh.read()
declared = {
    m.lower() for m in re.findall(r'^\s*"([A-Za-z0-9_-]+)\s*>=', project, re.M)
}
named = " ".join(c.name.lower() for c in THIRD_PARTY)
aliases = {"ecdsa": "python-ecdsa"}
missing = sorted(d for d in declared if aliases.get(d, d) not in named)
ok("every dependency in pyproject.toml is listed", not missing, f"missing {missing}")

with open(os.path.join(ROOT, "THIRD-PARTY-NOTICES.md"), encoding="utf-8") as fh:
    notices = fh.read()
ok("and the notices file names the same components",
   all(c.name in notices for c in THIRD_PARTY))

app = QApplication.instance() or QApplication(sys.argv[:1])
dlg = AboutDialog(session_lines=["Schema: 7.23.3"])
ok("the dialog has About, License and Third-party tabs",
   dlg.tab_titles() == ["About", "License", "Third-party"], str(dlg.tab_titles()))
from PySide6.QtWidgets import QLabel  # noqa: E402

ok("its header names the licence in one line",
   any(LICENSE_SUMMARY in lab.text() for lab in dlg.findChildren(QLabel)))

if failures:
    print(f"\n{len(failures)} failure(s)")
    raise SystemExit(1)
print("\nall about checks passed")
