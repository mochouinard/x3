# PyInstaller build of x3: one folder holding both programs, ``x3`` (the
# desktop client, no console window on Windows) and ``x3ctl`` (the command
# line), sharing one copy of Python, Qt and the x3 package.
#
#   pyinstaller --noconfirm packaging/pyinstaller/x3.spec
#
# Output: dist/x3/. The Windows installer (packaging/windows/x3.iss) and the
# Linux AppImage (packaging/linux/build-appimage.sh) are both built from it.
# Qt and the other LGPL libraries stay separate files in that folder, which is
# what keeps them replaceable.

import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 -- set by PyInstaller
HERE = ROOT / "packaging" / "pyinstaller"
RES = ROOT / "x3" / "ui" / "resources"

datas = [
    (str(RES), "x3/ui/resources"),
    (str(ROOT / "x3" / "ui" / "i18n" / "*.json"), "x3/ui/i18n"),
    # The About box shows the license files; in the bundle they sit at the
    # bundle root, where about_dialog.license_text() looks.
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "LICENSE.fr.md"), "."),
    (str(ROOT / "LICENSES"), "LICENSES"),
    (str(ROOT / "THIRD-PARTY-NOTICES.md"), "."),
]
# Qt modules x3 never loads: leaving them out roughly halves the bundle.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick",
    "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth",
    "tkinter",
]
icon = str(RES / "appicon.ico") if sys.platform == "win32" else None


def analysis(script):
    return Analysis(  # noqa: F821
        [str(HERE / script)],
        pathex=[str(ROOT)],
        datas=datas,
        hiddenimports=["PySide6.QtSvg"],
        excludes=excludes,
        noarchive=False,
    )


gui = analysis("x3_gui.py")
cli = analysis("x3ctl.py")

gui_exe = EXE(  # noqa: F821
    PYZ(gui.pure),  # noqa: F821
    gui.scripts,
    exclude_binaries=True,
    name="x3",
    console=False,
    icon=icon,
)
cli_exe = EXE(  # noqa: F821
    PYZ(cli.pure),  # noqa: F821
    cli.scripts,
    exclude_binaries=True,
    name="x3ctl",
    console=True,
    icon=icon,
)
COLLECT(  # noqa: F821
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    name="x3",
)
