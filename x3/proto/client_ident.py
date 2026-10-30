"""Winbox client identity (field 0xFF001C).

Firmware 7 gates many handlers (especially mproxy file fetch) on this
string-array field. Real clients differ:

  Winbox 3.x  — always sends ``["winbox-3.42"]`` (or similar 3.x build).
  Winbox 4.x  — omits the field entirely on the wire. The router still expects
                *something* after a handful of file opens; without it
                opens hang (e.g. on ``ipv6.jg.gz``). The identity string
                ``winbox-4.0`` is accepted by firmware 7.21.1 the same way
                ``winbox-3.42`` is. Bare ``winbox-4`` is **not** enough.

Default is **Winbox 3.42** (most reliable across firmware 7.21–7.23 for
mproxy file fetch). Prefer ``winbox-4.0`` when you know the firmware
accepts it, or when the firmware stops accepting the 3.x identity:

  --client-ident winbox-4.0
  $X3_CLIENT_IDENT=winbox-4.0

DeviceClient will also fall back from 4.0 → 3.42 for the plugin ``list``
fetch if 4.0 hangs. See README "Winbox 3 vs 4".
"""

from __future__ import annotations

import os

# Wire field code (string-array on modern sessions).
CLIENT_IDENT_FIELD = 0xFF001C

# Known-good identities verified against live firmware 7.x.
CLIENT_IDENT_WINBOX3 = "winbox-3.42"
CLIENT_IDENT_WINBOX4 = "winbox-4.0"

# Reliable default today. Switch to CLIENT_IDENT_WINBOX4 when firmware
# stops accepting Winbox 3 (or set X3_CLIENT_IDENT=winbox-4.0).
CLIENT_IDENT_DEFAULT = CLIENT_IDENT_WINBOX3

# Aliases accepted by --client-ident / env.
_ALIASES: dict[str, str] = {
    "3": CLIENT_IDENT_WINBOX3,
    "v3": CLIENT_IDENT_WINBOX3,
    "winbox3": CLIENT_IDENT_WINBOX3,
    "winbox-3": CLIENT_IDENT_WINBOX3,
    "winbox-3.42": CLIENT_IDENT_WINBOX3,
    "4": CLIENT_IDENT_WINBOX4,
    "v4": CLIENT_IDENT_WINBOX4,
    "winbox4": CLIENT_IDENT_WINBOX4,
    "winbox-4": CLIENT_IDENT_WINBOX4,
    "winbox-4.0": CLIENT_IDENT_WINBOX4,
}


def normalize_client_ident(value: str | None) -> str:
    """Map aliases / free-form strings to a wire identity.

    Empty / None → default (Winbox 4.0). Unknown non-empty strings are
    passed through so lab experiments stay possible.
    """
    if value is None:
        return CLIENT_IDENT_DEFAULT
    s = value.strip()
    if not s:
        return CLIENT_IDENT_DEFAULT
    key = s.lower().replace(" ", "")
    return _ALIASES.get(key, s)


def resolve_client_ident(
    explicit: str | None = None,
    *,
    env: bool = True,
) -> str:
    """Resolve identity: explicit arg → $X3_CLIENT_IDENT → default."""
    if explicit is not None and str(explicit).strip():
        return normalize_client_ident(explicit)
    if env:
        return normalize_client_ident(os.environ.get("X3_CLIENT_IDENT"))
    return CLIENT_IDENT_DEFAULT


def is_winbox4_ident(ident: str) -> bool:
    """True if this identity is in the Winbox 4 family."""
    n = normalize_client_ident(ident)
    return n == CLIENT_IDENT_WINBOX4 or n.lower().startswith("winbox-4")
