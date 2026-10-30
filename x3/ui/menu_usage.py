"""Per-user "most used" tracking for submenu entries.

Usage counts, manual pins, and an *exclude* ("never show at top") list, keyed by
a menu item's **open id** — the id of the thing a click actually opens, so the
key matches whether the item is reached directly or through a one-list folder
(IP → Addresses). Stored per-user in ``settings.json``, so the same counts
follow the user to every host they connect to. Structured so a per-connection
scope can be layered on later without changing callers.

Policy (see :func:`most_used`): a submenu pins its manual pins first, then — when
``most_used_auto`` is on — the items used most, up to ``DEFAULT_TOP_K`` total and
only once used ``DEFAULT_MIN_USES`` times. The inline "X" on a pinned row calls
:func:`remove_from_top` (unpin, or reset the count); a separate "never show at
top" action calls :func:`set_excluded`.

What counts as an *open* is decided by the caller, not here: MainWindow only
calls :func:`record_open` once the window has stayed open for
``main_window.USAGE_DWELL_MS``, so a mis-click that is shut again never climbs
the list.
"""

from __future__ import annotations

from .settings import load_most_used_auto, load_settings, save_settings

_USAGE = "menu_usage"        # {open_id: count}
_PINNED = "menu_pinned"      # [open_id]  manual pins (always shown at top)
_EXCLUDED = "menu_excluded"  # [open_id]  never shown at top, even if used a lot

DEFAULT_TOP_K = 3
DEFAULT_MIN_USES = 2


def _read() -> tuple[dict, dict, list, list]:
    d = load_settings()
    usage = d.get(_USAGE)
    pinned = d.get(_PINNED)
    excluded = d.get(_EXCLUDED)
    return (
        d,
        usage if isinstance(usage, dict) else {},
        pinned if isinstance(pinned, list) else [],
        excluded if isinstance(excluded, list) else [],
    )


def record_open(open_id: str | None) -> None:
    """Count one open of ``open_id``."""
    if not open_id:
        return
    d, usage, _p, _e = _read()
    usage[open_id] = int(usage.get(open_id, 0)) + 1
    d[_USAGE] = usage
    save_settings(d)


def usage_counts() -> dict[str, int]:
    return _read()[1]


def pinned_ids() -> set[str]:
    return set(_read()[2])


def excluded_ids() -> set[str]:
    return set(_read()[3])


def is_pinned(open_id: str | None) -> bool:
    return bool(open_id) and open_id in pinned_ids()


def is_excluded(open_id: str | None) -> bool:
    return bool(open_id) and open_id in excluded_ids()


def _set_member(key: str, open_id: str, present: bool) -> None:
    d = load_settings()
    lst = d.get(key)
    lst = list(lst) if isinstance(lst, list) else []
    if present and open_id not in lst:
        lst.append(open_id)
    elif not present and open_id in lst:
        lst = [x for x in lst if x != open_id]
    d[key] = lst
    save_settings(d)


def set_pinned(open_id: str | None, on: bool) -> None:
    if open_id:
        _set_member(_PINNED, open_id, on)


def set_excluded(open_id: str | None, on: bool) -> None:
    if not open_id:
        return
    _set_member(_EXCLUDED, open_id, on)
    if on:
        # An excluded item must not sit on top, so drop any manual pin too.
        _set_member(_PINNED, open_id, False)


def reset_count(open_id: str | None) -> None:
    if not open_id:
        return
    d, usage, _p, _e = _read()
    if open_id in usage:
        usage.pop(open_id, None)
        d[_USAGE] = usage
        save_settings(d)


def remove_from_top(open_id: str | None) -> None:
    """The inline "X": drop this item from the pinned group now.

    Unpin it if it is a manual pin; otherwise reset its usage count so it falls
    back below the auto threshold. Either way it leaves the top group but stays
    in the menu at its normal position.
    """
    if not open_id:
        return
    if is_pinned(open_id):
        set_pinned(open_id, False)
    else:
        reset_count(open_id)


def most_used(
    candidate_ids,
    *,
    auto: bool | None = None,
    k: int = DEFAULT_TOP_K,
    min_uses: int = DEFAULT_MIN_USES,
) -> list[str]:
    """Ordered ``open_id``s to pin at the top of a submenu.

    ``candidate_ids`` are the open ids of that submenu's own entries. Manual
    pins come first (most-used first); when ``auto`` is on the remaining slots
    up to ``k`` are filled by the most-used non-excluded entries with at least
    ``min_uses`` opens. ``auto`` defaults to the ``most_used_auto`` setting.
    """
    if auto is None:
        auto = load_most_used_auto()
    _d, usage, pinned, excluded = _read()
    cand = [c for c in candidate_ids if c]
    pinset, exset = set(pinned), set(excluded)

    top = [c for c in cand if c in pinset]
    top.sort(key=lambda c: -int(usage.get(c, 0)))

    if auto:
        autos = [
            c for c in cand
            if c not in pinset
            and c not in exset
            and int(usage.get(c, 0)) >= min_uses
        ]
        autos.sort(key=lambda c: -int(usage.get(c, 0)))
        for c in autos:
            if len(top) >= max(k, len(pinset & set(cand))):
                break
            top.append(c)
    return top
