"""Offline tests for Winbox 3/4 client identity resolution."""

from x3.proto.client_ident import (
    CLIENT_IDENT_DEFAULT,
    CLIENT_IDENT_WINBOX3,
    CLIENT_IDENT_WINBOX4,
    is_winbox4_ident,
    normalize_client_ident,
    resolve_client_ident,
)


def check(name, got, want):
    if got != want:
        raise SystemExit(f"FAIL {name}: got {got!r} want {want!r}")
    print(f"ok  {name}")


check("default is winbox-3.42", CLIENT_IDENT_DEFAULT, CLIENT_IDENT_WINBOX3)
check("alias 3", normalize_client_ident("3"), CLIENT_IDENT_WINBOX3)
check("alias v3", normalize_client_ident("v3"), CLIENT_IDENT_WINBOX3)
check("alias winbox-3.42", normalize_client_ident("winbox-3.42"), CLIENT_IDENT_WINBOX3)
check("alias 4", normalize_client_ident("4"), CLIENT_IDENT_WINBOX4)
check("alias winbox-4", normalize_client_ident("winbox-4"), CLIENT_IDENT_WINBOX4)
check("alias winbox-4.0", normalize_client_ident("winbox-4.0"), CLIENT_IDENT_WINBOX4)
check("empty → default", normalize_client_ident(""), CLIENT_IDENT_DEFAULT)
check("None → default", normalize_client_ident(None), CLIENT_IDENT_DEFAULT)
check("passthrough lab string", normalize_client_ident("lab-client-1"), "lab-client-1")
check("is_winbox4", is_winbox4_ident("winbox-4.0"), True)
check("not is_winbox4", is_winbox4_ident("winbox-3.42"), False)

# explicit beats env
import os

os.environ["X3_CLIENT_IDENT"] = "winbox-3.42"
check("explicit wins over env", resolve_client_ident("4"), CLIENT_IDENT_WINBOX4)
check("env when no explicit", resolve_client_ident(None), CLIENT_IDENT_WINBOX3)
del os.environ["X3_CLIENT_IDENT"]
check("default when env cleared", resolve_client_ident(None), CLIENT_IDENT_DEFAULT)

print("all client_ident tests passed")
