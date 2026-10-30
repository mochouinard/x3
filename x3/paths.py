"""Where x3 keeps its files, per platform.

Four kinds, after the XDG base-directory spec:

* **config** -- settings and the address-book overlay;
* **data** -- session logs, audit files and config snapshots;
* **cache** -- parsed schemas, downloaded plugin sets, learned defaults;
* **state** -- the application log.

An ``XDG_*_HOME`` variable wins on every platform (the tests point all four at
a scratch folder that way). Otherwise Linux uses the XDG defaults under the
home directory; Windows keeps settings in the roaming profile (``%APPDATA%``)
and everything bulky or machine-specific in ``%LOCALAPPDATA%``; macOS uses
``~/Library``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "x3"


def _windows(kind: str) -> Path:
    home = Path.home()
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if kind == "config":
        return roaming / APP
    return local / APP / kind


def _macos(kind: str) -> Path:
    lib = Path.home() / "Library"
    if kind == "cache":
        return lib / "Caches" / APP
    if kind == "state":
        return lib / "Logs" / APP
    return lib / "Application Support" / APP


_XDG = {
    "config": ("XDG_CONFIG_HOME", (".config",)),
    "data": ("XDG_DATA_HOME", (".local", "share")),
    "cache": ("XDG_CACHE_HOME", (".cache",)),
    "state": ("XDG_STATE_HOME", (".local", "state")),
}


def env_var(name: str) -> str:
    """An environment variable as this platform's shell writes it:
    ``%X3_ADDRESSES_CDB%`` on Windows, ``$X3_ADDRESSES_CDB`` elsewhere."""
    return f"%{name}%" if sys.platform == "win32" else f"${name}"


def base_dir(kind: str) -> Path:
    """The x3 folder of one kind: ``config``, ``data``, ``cache`` or ``state``."""
    env, default = _XDG[kind]
    xdg = os.environ.get(env)
    if xdg:
        return Path(xdg) / APP
    if sys.platform == "win32":
        return _windows(kind)
    if sys.platform == "darwin":
        return _macos(kind)
    return Path.home().joinpath(*default) / APP


def config_dir() -> Path:
    return base_dir("config")


def data_dir() -> Path:
    return base_dir("data")


def cache_dir() -> Path:
    return base_dir("cache")


def state_dir() -> Path:
    return base_dir("state")
