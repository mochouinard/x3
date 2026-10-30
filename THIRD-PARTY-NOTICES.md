# Third-party notices

x3 itself is under the PolyForm Strict License 1.0.0 with an additional
permission for contributions (see `LICENSE`), except for the derived code
listed at the end of this file. Nothing in x3's license limits what the
licenses below allow you to do with these components. Its run-time dependencies are not
bundled or modified: each is installed by pip from its own distribution, under
its own licence, and is listed here so you know what you are running. The same list is in the app under
**Help ▸ About ▸ Third-party**.

| Component | Licence | Used for | Source |
|---|---|---|---|
| Qt 6 | LGPL-3.0-only | GUI toolkit | https://www.qt.io/ |
| PySide6 (Qt for Python) | LGPL-3.0-only | Python bindings for Qt | https://pyside.org |
| shiboken6 | LGPL-3.0-only | Runtime for the PySide6 bindings | https://pyside.org |
| pyte | LGPL-3.0 | VT100 emulation in the terminal window | https://github.com/selectel/pyte |
| wcwidth | MIT | Character widths (a dependency of pyte) | https://github.com/jquast/wcwidth |
| PyCryptodome | BSD-2-Clause and public domain | AES and SHA for the encrypted session | https://www.pycryptodome.org |
| python-ecdsa | MIT | Elliptic-curve maths for EC-SRP5 login | https://github.com/tlsfuzzer/python-ecdsa |
| six | MIT | Python 2/3 shim (a dependency of python-ecdsa) | https://github.com/benjaminp/six |
| psutil | BSD-3-Clause | Network interface addresses (Windows and macOS only) | https://github.com/giampaolo/psutil |

Qt, PySide6, shiboken6 and pyte are only needed for the desktop client
(`pip install x3[gui]`); the protocol library and `x3ctl` need PyCryptodome and
python-ecdsa alone.

## About the LGPL components

x3 imports Qt, PySide6, shiboken6 and pyte as ordinary shared libraries from
their own installed packages. You can replace any of them with a compatible
build of your own (for example `pip install --force-reinstall PySide6==<version>`),
and x3 will use it, and reverse engineer x3 as far as needed to debug such a
change (`LICENSE`, "Other Licenses", says the same). Their licence texts ship
inside their own packages and are available from the links above.

## Artwork and fonts

The icons under `x3/ui/resources/icons/` and the application mark are original
work made for x3 and are covered by the x3 license. No fonts are bundled; the
font menu only uses typefaces already installed on your system.

## Derived code

`x3/proto/crypto.py` derives from `src/elliptic_curves.py` and
`src/encryption.py` in Margin Research's EC-SRP reference client,
<https://github.com/MarginResearch/mikrotik_authentication>, which is licensed
under the Apache License, Version 2.0. Those portions remain under that
licence; its full text is in `LICENSES/Apache-2.0.txt`, and the file's header
says what was changed.
