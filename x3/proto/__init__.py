"""The Winbox protocol: transport, authentication, the M2 codec and the .jg schema."""

from .client_ident import (  # noqa: F401
    CLIENT_IDENT_DEFAULT,
    CLIENT_IDENT_FIELD,
    CLIENT_IDENT_WINBOX3,
    CLIENT_IDENT_WINBOX4,
    normalize_client_ident,
    resolve_client_ident,
)
