"""Local UI preferences (last connection, etc.).

Stored as JSON in ``settings.json`` under :func:`x3.paths.config_dir`
(``~/.config/x3`` on Linux, ``%APPDATA%\\x3`` on Windows). The password is saved in
plain text, as Addresses.cdb does — keep the file private
(``0600`` on Unix).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from x3 import paths


@dataclass
class LastConnection:
    host: str = "192.168.88.1"
    user: str = "admin"
    password: str = ""
    transport: str = "tcp"  # tcp | mac
    mac: str = ""


#: config_dir() keyed on $XDG_CONFIG_HOME, so a test (or a user) that moves it
#: still gets the right answer while a steady process pays for the Path
#: arithmetic once. Building three Path objects per call added up: every
#: toolbar icon asks for the icon source, so opening one 17-tab window did it
#: about 140 times.
_DIR_CACHE: tuple[str | None, Path] | None = None


def config_dir() -> Path:
    global _DIR_CACHE
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if _DIR_CACHE is not None and _DIR_CACHE[0] == xdg:
        return _DIR_CACHE[1]
    out = paths.config_dir()
    _DIR_CACHE = (xdg, out)
    return out


def data_dir() -> Path:
    return paths.data_dir()


#: Which drawing of each icon the UI uses, in menu order.
ICON_SOURCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("builtin", "&Color"),
    ("mono", "&Monochrome"),
)

_ICON_SOURCES = frozenset(v for v, _ in ICON_SOURCE_CHOICES)


def icon_source_label(value: str) -> str:
    """Menu label for an icon source id, with its mnemonic stripped."""
    for v, label in ICON_SOURCE_CHOICES:
        if v == value:
            return label.replace("&", "")
    return value


def load_icon_source() -> str:
    """``builtin`` (default, our colour SVGs) or ``mono`` (the same set as one
    tintable ink)."""
    v = str(load_settings().get("icon_source", "builtin")).lower()
    return v if v in _ICON_SOURCES else "builtin"


def save_icon_source(value: str) -> None:
    v = str(value).lower()
    data = load_settings()
    data["icon_source"] = v if v in _ICON_SOURCES else "builtin"
    save_settings(data)


def load_danger_ink() -> bool:
    """Whether the monochrome set keeps Remove and Disable red. On by default:
    one ink for everything makes the button that deletes look like the one
    that adds."""
    return bool(load_settings().get("danger_ink", True))


def save_danger_ink(value: bool) -> None:
    data = load_settings()
    data["danger_ink"] = bool(value)
    save_settings(data)


_PATH_CACHE: tuple[Path, Path] | None = None


def settings_path() -> Path:
    global _PATH_CACHE
    base = config_dir()
    if _PATH_CACHE is not None and _PATH_CACHE[0] == base:
        return _PATH_CACHE[1]
    out = base / "settings.json"
    _PATH_CACHE = (base, out)
    return out


#: (stat identity, parsed settings). The file is read once per version of
#: itself rather than once per question asked of it -- there are ~35 of those
#: per window opened, and JSON-parsing the file each time was a quarter of the
#: cost of opening a many-tab list window. Keyed on the file's own
#: (path, mtime, size, inode) so an edit from outside this process is still
#: picked up; save_settings() refreshes it directly.
_SETTINGS_CACHE: tuple[tuple, dict[str, Any]] | None = None


def _stat_key(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None, None, None)
    return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)


def load_settings() -> dict[str, Any]:
    """The saved preferences.

    A shallow copy: callers read-modify-write the top level and hand the
    result back to :func:`save_settings`, and they must not be editing the
    cache in place while they do it.
    """
    global _SETTINGS_CACHE
    path = settings_path()
    key = _stat_key(path)
    if _SETTINGS_CACHE is not None and _SETTINGS_CACHE[0] == key:
        return dict(_SETTINGS_CACHE[1])
    data: dict[str, Any] = {}
    if key[1] is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    _SETTINGS_CACHE = (key, data)
    return dict(data)


def save_settings(data: dict[str, Any]) -> None:
    global _SETTINGS_CACHE
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    # Adopt what we just wrote rather than dropping the cache: the next read is
    # usually immediate (save a column width, redraw the header).
    _SETTINGS_CACHE = (_stat_key(path), dict(data))


def load_last_connection() -> LastConnection:
    data = load_settings()
    raw = data.get("last_connection") or {}
    if not isinstance(raw, dict):
        raw = {}
    return LastConnection(
        host=str(raw.get("host") or "192.168.88.1"),
        user=str(raw.get("user") or "admin"),
        password=str(raw.get("password") or ""),
        transport=str(raw.get("transport") or "tcp"),
        mac=str(raw.get("mac") or ""),
    )


def save_last_connection(conn: LastConnection) -> None:
    data = load_settings()
    data["last_connection"] = asdict(conn)
    save_settings(data)


def load_theme_id() -> str:
    data = load_settings()
    tid = data.get("theme")
    return str(tid) if tid else "light"


def save_theme_id(theme_id: str) -> None:
    data = load_settings()
    data["theme"] = theme_id
    save_settings(data)


def load_language() -> str:
    """``auto`` (follow the desktop, when x3 has that language) or a code
    from :func:`x3.ui.i18n.available`. Read once, at start."""
    return str(load_settings().get("language") or "auto")


def save_language(code: str) -> None:
    data = load_settings()
    data["language"] = str(code or "auto")
    save_settings(data)


def load_ui_scale() -> float:
    """UI zoom factor (View ▸ Zoom, Ctrl+= / Ctrl+-). 1.0 = the theme's own sizes.

    Not clamped or snapped here -- :func:`x3.ui.style.clamp_scale` and
    :func:`x3.ui.style.step_scale` own the ladder, and a file naming a
    factor between two stops should still draw at that factor.
    """
    data = load_settings()
    try:
        return float(data.get("ui_scale", 1.0))
    except (TypeError, ValueError):
        return 1.0


def save_ui_scale(scale: float) -> None:
    """Save the zoom factor, dropping the key entirely at 1.0.

    1.0 is the absence of a zoom, not a zoom of one: leaving the key out means
    a settings file only mentions the UI scale once somebody has changed it.
    """
    data = load_settings()
    try:
        v = round(float(scale), 4)
    except (TypeError, ValueError):
        v = 1.0
    if v == 1.0:
        data.pop("ui_scale", None)
    else:
        data["ui_scale"] = v
    save_settings(data)


def load_ui_font() -> str:
    """UI font family override; "" follows the theme.

    Exists because Tahoma / Segoe UI / Microsoft Sans Serif all substitute to
    one family on a stock Linux box -- see :mod:`x3.ui.fonts`.
    """
    data = load_settings()
    fam = data.get("ui_font")
    return str(fam) if fam else ""


def save_ui_font(family: str) -> None:
    data = load_settings()
    fam = str(family or "").strip()
    if fam:
        data["ui_font"] = fam
    else:
        data.pop("ui_font", None)
    save_settings(data)


def load_safe_mode_idle_secs() -> int:
    """Idle seconds before Safe Mode reminder (0 = disabled). Default 300."""
    data = load_settings()
    raw = data.get("safe_mode_idle_secs")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 300
    return max(0, n)


def save_safe_mode_idle_secs(secs: int) -> None:
    data = load_settings()
    data["safe_mode_idle_secs"] = max(0, int(secs))
    save_settings(data)


#: Edit mode a new session starts in. Live by default: this is a tool for
#: changing device configuration, and starting locked meant every session began
#: with the same two clicks. Safe Mode is offered when Live is *chosen*, so
#: defaulting to it means that offer is not made — the mode is shown in the
#: toolbar and in every form's title instead.
DEFAULT_EDIT_MODE = "live"


def load_addresses_cdb() -> str | None:
    """The address book the user picked, or None to look in the usual places.

    The reference client keeps ``Addresses.cdb`` in a different folder per
    version and platform, and people move it, so the path someone chose wins
    over every guess (only ``$X3_ADDRESSES_CDB`` outranks it).
    """
    value = load_settings().get("addresses_cdb")
    return str(value) if value else None


def save_addresses_cdb(path: str | os.PathLike | None) -> None:
    data = load_settings()
    if path:
        data["addresses_cdb"] = str(path)
    else:
        data.pop("addresses_cdb", None)
    save_settings(data)


def load_update_check() -> bool:
    """Whether x3 looks for a newer release of itself, at most once a day.

    Off unless the user turns it on (welcome tour, Settings): it is a request
    to GitHub on every start of a day, and that is the user's call to make.
    """
    return bool(load_settings().get("update_check", False))


def update_check_chosen() -> bool:
    """Whether the user has answered the update-check question at all."""
    return "update_check" in load_settings()


def save_update_check(value: bool) -> None:
    data = load_settings()
    data["update_check"] = bool(value)
    save_settings(data)


def load_update_state() -> tuple[float | None, str | None]:
    """When the last automatic check ran, and the version the user skipped."""
    data = load_settings()
    last = data.get("update_last_check")
    try:
        last = float(last) if last is not None else None
    except (TypeError, ValueError):
        last = None
    skipped = data.get("update_skipped")
    return last, (str(skipped) if skipped else None)


def save_update_state(*, last_check: float | None = None, skipped: str | None = None) -> None:
    data = load_settings()
    if last_check is not None:
        data["update_last_check"] = float(last_check)
    if skipped is not None:
        data["update_skipped"] = skipped
    save_settings(data)


#: Bumped when the welcome tour gains something an existing user should see.
WELCOME_VERSION = 1


def load_welcome_seen() -> bool:
    """Whether the first-start welcome has been shown (finished or cancelled)."""
    try:
        return int(load_settings().get("welcome_seen", 0)) >= WELCOME_VERSION
    except (TypeError, ValueError):
        return False


def save_welcome_seen() -> None:
    data = load_settings()
    data["welcome_seen"] = WELCOME_VERSION
    save_settings(data)


def load_start_mode() -> str:
    """``live`` | ``queue`` | ``readonly`` — what a new session starts in."""
    value = str(load_settings().get("start_mode", DEFAULT_EDIT_MODE)).lower()
    return value if value in ("live", "queue", "readonly") else DEFAULT_EDIT_MODE


def save_start_mode(value: str) -> None:
    data = load_settings()
    data["start_mode"] = str(value).lower()
    save_settings(data)


def load_show_passwords() -> bool:
    """Winbox v3 Settings > Show Passwords. Off by default."""
    return bool(load_settings().get("show_passwords", False))


def save_show_passwords(value: bool) -> None:
    data = load_settings()
    data["show_passwords"] = bool(value)
    save_settings(data)


def load_connect_fold(name: str) -> bool:
    """Whether a fold on the connection page (``entry``, ``session``) is open."""
    folds = load_settings().get("connect_folds")
    return bool(folds.get(name, False)) if isinstance(folds, dict) else False


def save_connect_fold(name: str, expanded: bool) -> None:
    data = load_settings()
    folds = data.get("connect_folds")
    if not isinstance(folds, dict):
        folds = {}
    folds[str(name)] = bool(expanded)
    data["connect_folds"] = folds
    save_settings(data)


TERMINAL_COLOURS = ("auto", "light", "dark")


def load_terminal_colours() -> str:
    """``auto`` (follow the theme) | ``light`` (Winbox v3) | ``dark`` (Winbox 4)."""
    value = str(load_settings().get("terminal_colours", "auto")).lower()
    return value if value in TERMINAL_COLOURS else "auto"


def save_terminal_colours(value: str) -> None:
    data = load_settings()
    data["terminal_colours"] = str(value).lower()
    save_settings(data)


def load_find_open() -> bool:
    """Whether a list opens with its Find box showing. On by default."""
    return bool(load_settings().get("find_open", True))


def save_find_open(value: bool) -> None:
    data = load_settings()
    data["find_open"] = bool(value)
    save_settings(data)


def load_inline_comments() -> bool:
    """Winbox v3 Settings > Inline Comment. On = comment as a trailing column."""
    return bool(load_settings().get("inline_comments", True))


def save_inline_comments(value: bool) -> None:
    data = load_settings()
    data["inline_comments"] = bool(value)
    save_settings(data)


# ---------------------------------------------------------------------------
# Per-list column state
# ---------------------------------------------------------------------------
#
# Keyed by schema path (plus the interface-type overlay, since several
# MultiPageListWindow tabs share one path). Everything inside is keyed by
# *column name*, not index: the synthetic Comment column comes and goes with
# the Inline Comment toggle, and a firmware upgrade can add fields, either of
# which would silently shift saved indices onto the wrong columns.
#
# Widths are 1x pixels, the unit a .jg width is in, and are scaled by the UI
# zoom when applied -- so a width keeps fitting what it was dragged to fit.
#
#   "column_prefs": {
#     "(2, 1)": {
#       "hidden": ["MTU", "L2 MTU"],
#       "widths": {"Name": 140},
#       "order":  ["Flags", "Name", "Type"],
#       "sort":   ["Name", "asc"]
#     }
#   }


def load_column_prefs(key: str) -> dict[str, Any]:
    prefs = load_settings().get("column_prefs")
    if not isinstance(prefs, dict):
        return {}
    one = prefs.get(key)
    return one if isinstance(one, dict) else {}


def save_column_prefs(key: str, prefs: dict[str, Any]) -> None:
    data = load_settings()
    store = data.get("column_prefs")
    if not isinstance(store, dict):
        store = {}
    # An entry that carries nothing is noise in the file; drop it instead.
    if any(prefs.get(k) for k in ("hidden", "widths", "order", "sort")):
        store[key] = prefs
    else:
        store.pop(key, None)
    data["column_prefs"] = store
    save_settings(data)


def clear_column_prefs(key: str) -> None:
    save_column_prefs(key, {})


#: Fastest the Traffic tab may refresh. The router recomputes an interface's
#: traffic once a second, so a faster poll only re-reads the same numbers; the
#: header offers nothing below this and a settings file that names something
#: faster -- ours did, while the header still offered 4 / s -- is brought up to
#: it rather than honoured.
TRAFFIC_POLL_MIN_MS = 1000


def load_traffic_poll_ms() -> int:
    """How often the Traffic tab refreshes, in milliseconds.

    1 Hz is the device's own tick and the fastest we ask; slower keeps a quiet
    link cheap. Clamped to the range the UI offers.
    """
    raw = load_settings().get("traffic_poll_ms", TRAFFIC_POLL_MIN_MS)
    try:
        return max(TRAFFIC_POLL_MIN_MS, min(60000, int(raw)))
    except (TypeError, ValueError):
        return TRAFFIC_POLL_MIN_MS


def save_traffic_poll_ms(value: int) -> None:
    data = load_settings()
    data["traffic_poll_ms"] = max(TRAFFIC_POLL_MIN_MS, min(60000, int(value)))
    save_settings(data)


# ---------------------------------------------------------------------------
# Navigation preferences
# ---------------------------------------------------------------------------


def load_submenu_order() -> str:
    """How submenu entries are ordered: ``alphabetical`` (default, like Winbox)
    or ``jg`` (the native ``.jg`` prio/declaration order). Top-level order is
    always the ``.jg`` prio regardless."""
    v = str(load_settings().get("submenu_order", "alphabetical")).lower()
    return v if v in ("alphabetical", "jg") else "alphabetical"


def save_submenu_order(value: str) -> None:
    v = str(value).lower()
    data = load_settings()
    data["submenu_order"] = v if v in ("alphabetical", "jg") else "alphabetical"
    save_settings(data)


def load_most_used_auto() -> bool:
    """Whether a submenu's "most used" group is filled automatically by usage
    count (True, default) or from manual pins only (False)."""
    return bool(load_settings().get("most_used_auto", True))


def save_most_used_auto(value: bool) -> None:
    data = load_settings()
    data["most_used_auto"] = bool(value)
    save_settings(data)


def load_live_updates() -> bool:
    """Default for list windows: subscribe + auto-refresh (True), or the
    low-bandwidth mode where an idle window makes no requests until you Refresh
    or re-enable Live (False). Default on."""
    return bool(load_settings().get("live_updates", True))


def save_live_updates(value: bool) -> None:
    data = load_settings()
    data["live_updates"] = bool(value)
    save_settings(data)


#: The refresh rates offered wherever this setting is asked for -- the connect
#: dialog before a session, Settings ▸ List Auto Refresh during one. None keeps
#: each list on the rate its own .jg menu declares (1s for Interface, 2s for the
#: firewall chains, 5s for Connections), which is what Winbox does and what the
#: counter columns were drawn for. The fixed intervals are for a slow link or a
#: busy device, where one cadence for every list is easier to reason about.
LIST_REFRESH_CHOICES: tuple[tuple[float | None, str], ...] = (
    (None, "Menu default (1s on Interfaces)"),
    (1.0, "Every second"),
    (2.0, "Every 2 seconds"),
    (5.0, "Every 5 seconds"),
    (10.0, "Every 10 seconds"),
    (0.0, "Off (Refresh only)"),
)


def load_list_refresh_secs() -> float | None:
    """How often a live list re-reads the device.

    ``None`` -- the default -- means "whatever the menu says": the .jg gives
    each list its own rate (1s for Interface, 2s for the firewall chains, 5s
    for Connections) and that is the cadence its counters were drawn for.
    A number overrides every list, and 0 turns the polling off while leaving
    Live (the change subscription) alone.
    """
    raw = load_settings().get("list_refresh_secs", None)
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def save_list_refresh_secs(secs: float | None) -> None:
    data = load_settings()
    if secs is None:
        data.pop("list_refresh_secs", None)
    else:
        data["list_refresh_secs"] = max(0.0, float(secs))
    save_settings(data)


def load_audit() -> bool:
    """Whether new connections record an audit file.

    Off unless asked for: an audit holds every configuration value that
    crossed the wire, so turning it on is the user's decision to make, not a
    default to inherit.
    """
    return bool(load_settings().get("audit", False))


def save_audit(value: bool) -> None:
    data = load_settings()
    data["audit"] = bool(value)
    save_settings(data)


def load_audit_level() -> str:
    """How much of the traffic to keep. See x3.proto.audit.AuditPolicy."""
    from x3.proto import audit as _audit

    raw = load_settings().get("audit_level", "config")
    return raw if raw in _audit.LEVELS else "config"


def save_audit_level(value: str) -> None:
    from x3.proto import audit as _audit

    data = load_settings()
    data["audit_level"] = value if value in _audit.LEVELS else "config"
    save_settings(data)


def load_auto_export() -> bool:
    """Whether a change arms an automatic config snapshot.

    On by default. It costs nothing on a session that changes nothing -- the
    timer is only ever armed by an actual change -- and the one session where
    somebody wishes they had a "before" is never the one they remembered to
    switch it on for.
    """
    return bool(load_settings().get("auto_export", True))


def save_auto_export(value: bool) -> None:
    data = load_settings()
    data["auto_export"] = bool(value)
    save_settings(data)


def load_auto_export_sensitive() -> bool:
    """Whether automatic snapshots include keys and passwords.

    **Off by default, deliberately.** The sensitive form is what makes an export
    restorable rather than merely readable, but it also turns every file in the
    snapshot directory into a credential dump for the router it came from -- and
    this runs unattended, on every session, across a whole fleet. Turning that
    on is a decision about where those files live, so it is one somebody has to
    make on purpose.
    """
    return bool(load_settings().get("auto_export_sensitive", False))


def save_auto_export_sensitive(value: bool) -> None:
    data = load_settings()
    data["auto_export_sensitive"] = bool(value)
    save_settings(data)


def load_auto_export_idle_secs() -> int:
    """Quiet interval before the snapshot is taken. 0 disables the countdown."""
    from .autoexport import DEFAULT_IDLE_SECS

    try:
        secs = int(load_settings().get("auto_export_idle_secs", DEFAULT_IDLE_SECS))
    except (TypeError, ValueError):
        return DEFAULT_IDLE_SECS
    return secs if secs > 0 else DEFAULT_IDLE_SECS


def save_auto_export_idle_secs(secs: int) -> None:
    data = load_settings()
    data["auto_export_idle_secs"] = max(1, int(secs))
    save_settings(data)


def load_titlebar_tabs() -> bool:
    """Multi-tab windows render their tabs inline in the title bar (Winbox 4
    style) instead of a tab strip below it. Default off — the classic tab strip
    stays the default; this is opt-in."""
    return bool(load_settings().get("titlebar_tabs", False))


def save_titlebar_tabs(value: bool) -> None:
    data = load_settings()
    data["titlebar_tabs"] = bool(value)
    save_settings(data)


def load_form_status_line() -> bool:
    """The grey line across the top of a property form -- which mode it is in,
    and what has been changed but not written yet. On by default: an unsaved
    edit is worth a line. Off gives the form its topmost pixels back; the title
    bar still carries the mode and the edit count."""
    return bool(load_settings().get("form_status_line", True))


def save_form_status_line(value: bool) -> None:
    data = load_settings()
    data["form_status_line"] = bool(value)
    save_settings(data)


def load_dropdown_marks() -> bool:
    """A status dot and the row's comment beside every entry of a dynamic
    dropdown -- an interface picker says which links are up and what someone
    wrote on them. On by default: the read that fills the box already carries
    both, and picking an interface that turns out to be down is the mistake
    the mark exists to stop.

    Winbox v3 shows bare names here, so this is a deliberate divergence and
    therefore a setting. Off restores the plain list."""
    return bool(load_settings().get("dropdown_marks", True))


def save_dropdown_marks(value: bool) -> None:
    data = load_settings()
    data["dropdown_marks"] = bool(value)
    save_settings(data)


def load_key_hints() -> bool:
    """Holding Ctrl floats the key panel over the window in front. On by
    default: it is how anyone finds out the keys exist. The setting is only
    about it appearing unasked -- F1 opens the same panel either way."""
    return bool(load_settings().get("key_hints", True))


def save_key_hints(value: bool) -> None:
    data = load_settings()
    data["key_hints"] = bool(value)
    save_settings(data)


#: Menu labels for the byte-unit choice, in the order they are offered.
BYTE_UNIT_CHOICES = (
    ("compact", "&Compact (2404.8 MiB, 27.7 GiB)"),
    ("binary", "&Binary units (2.3 GiB)"),
    ("exact", "&Exact bytes (2521615565 B)"),
)


def load_byte_units() -> str:
    """How byte counters read: ``winbox`` (default), ``binary`` or ``exact``.

    Winbox changes unit only when the number would need a fifth digit, which
    is why a 2.3 GiB counter still reads in MiB there. That is the default
    because matching Winbox is the point, but a counter is also something
    people copy into a calculator -- hence ``exact`` -- or want rounded the
    ordinary way -- hence ``binary``.
    """
    v = str(load_settings().get("byte_units", "compact")).lower()
    return v if v in {k for k, _label in BYTE_UNIT_CHOICES} else "compact"


def save_byte_units(value: str) -> None:
    data = load_settings()
    value = str(value).lower()
    data["byte_units"] = (
        value if value in {k for k, _label in BYTE_UNIT_CHOICES} else "compact"
    )
    save_settings(data)


def apply_byte_units(value: str | None = None) -> str:
    """Push the preference into the renderer that every value goes through."""
    from x3.proto.schema import set_byte_unit_mode

    return set_byte_unit_mode(load_byte_units() if value is None else value)


def load_advanced_mode() -> bool:
    """Whether forms with a Winbox ``type:toggle`` open in Advanced Mode.

    Winbox v3 puts a Simple Mode / Advanced Mode button on the wireless
    interface and PPP client dialogs, and remembers the choice rather than
    reverting to Simple every time a window opens. Default off, like Winbox.
    """
    return bool(load_settings().get("advanced_mode", False))


def save_advanced_mode(value: bool) -> None:
    data = load_settings()
    data["advanced_mode"] = bool(value)
    save_settings(data)
