"""OAuth 2.1-autorisatielaag voor de remote MCP-server (Claude.ai custom connector).

Publieke API (gebruikt door de HTTP-serverkant):

    bundle = build_auth(settings)
    MCPServer(..., auth=bundle.auth_settings, auth_server_provider=bundle.provider)
    # plus elke route uit bundle.routes als custom route (de SDK voegt de OAuth-routes zelf toe)
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from starlette.routing import Route

from ..config import Settings
from .login import FAIL_DELAY_S, LoginHandlers
from .provider import (
    DEFAULT_SCOPES,
    SCOPE_TOOLBOX,
    SUPPORTED_SCOPES,
    PitoolboxOAuthProvider,
)
from .store import AuthStore

__all__ = ["AuthBundle", "build_auth"]

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


@dataclass
class AuthBundle:
    auth_settings: AuthSettings
    provider: PitoolboxOAuthProvider
    routes: list[Route]


def _validated_public_url(public_url: str | None, *, allow_insecure_localhost: bool) -> str:
    if not public_url:
        raise ValueError(
            "PITOOLBOX_PUBLIC_URL is niet ingesteld (bijv. https://toolbox.mijndomein.nl)"
        )
    url = public_url.rstrip("/")
    parts = urlsplit(url)
    if parts.path or parts.query or parts.fragment or not parts.hostname:
        raise ValueError(
            "PITOOLBOX_PUBLIC_URL moet alleen schema en host bevatten (geen pad, query of fragment)"
        )
    if parts.scheme == "https":
        return url
    if allow_insecure_localhost and parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS:
        return url
    raise ValueError(
        "PITOOLBOX_PUBLIC_URL moet met https:// beginnen (OAuth vereist TLS, de tunnel levert dat)"
    )


def build_auth(
    settings: Settings,
    *,
    clock: Callable[[], float] | None = None,
    extra_redirect_uris: Iterable[str] = (),
    allow_insecure_localhost: bool = False,
    fail_delay_s: float = FAIL_DELAY_S,
) -> AuthBundle:
    """Bouw store, provider, SDK-instellingen en loginroutes uit de ``Settings``.

    ``extra_redirect_uris`` en ``allow_insecure_localhost`` zijn alleen voor tests;
    in productie blijft de allowlist beperkt tot de twee Claude-callbacks.
    """
    public_url = _validated_public_url(
        settings.public_url, allow_insecure_localhost=allow_insecure_localhost
    )
    now = clock or time.time
    store = AuthStore(settings.auth_db, now)
    provider = PitoolboxOAuthProvider(
        store,
        public_url=public_url,
        access_token_ttl_s=settings.access_token_ttl_s,
        refresh_token_ttl_s=settings.refresh_token_ttl_s,
        auth_code_ttl_s=settings.auth_code_ttl_s,
        clock=now,
        extra_redirect_uris=extra_redirect_uris,
    )
    auth_settings = AuthSettings(
        issuer_url=public_url,  # type: ignore[arg-type]  # pydantic zet str om naar AnyHttpUrl
        resource_server_url=f"{public_url}/mcp",  # type: ignore[arg-type]
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=list(SUPPORTED_SCOPES),
            default_scopes=list(DEFAULT_SCOPES),
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE_TOOLBOX],
        validate_token_resource=True,
    )
    login = LoginHandlers(provider, store, now, fail_delay_s=fail_delay_s)
    return AuthBundle(auth_settings=auth_settings, provider=provider, routes=login.routes())
