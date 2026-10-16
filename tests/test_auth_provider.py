"""Tests voor PitoolboxOAuthProvider (rechtstreeks, zonder HTTP)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from test_auth_support import (
    CLAUDE_CB,
    CLAUDE_COM_CB,
    RESOURCE,
    FakeClock,
    client_info,
    make_settings,
    pkce_pair,
)

from pitoolbox.auth import AuthBundle, build_auth
from pitoolbox.auth.provider import MAX_CLIENTS, PitoolboxOAuthProvider


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bundle(tmp_path: Path, clock: FakeClock) -> AuthBundle:
    return build_auth(make_settings(tmp_path), clock=clock)


@pytest.fixture
def provider(bundle: AuthBundle) -> PitoolboxOAuthProvider:
    return bundle.provider


async def _registered(
    provider: PitoolboxOAuthProvider, client_id: str = "c1"
) -> OAuthClientInformationFull:
    info = client_info(client_id)
    await provider.register_client(info)
    stored = await provider.get_client(client_id)
    assert stored is not None
    return stored


def _params(
    redirect: str = CLAUDE_CB,
    *,
    scopes: list[str] | None = None,
    resource: str | None = RESOURCE,
    challenge: str | None = None,
    state: str | None = "st4te",
) -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=scopes,
        code_challenge=challenge or pkce_pair()[1],
        redirect_uri=AnyUrl(redirect),
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


async def _issue_code(
    provider: PitoolboxOAuthProvider, client: OAuthClientInformationFull, **kw: object
) -> str:
    url = await provider.authorize(client, _params(**kw))  # type: ignore[arg-type]
    request_id = parse_qs(urlsplit(url).query)["request"][0]
    issued = provider.create_authorization_code(request_id)
    assert issued is not None
    return issued[0]


async def _tokens(
    provider: PitoolboxOAuthProvider, client: OAuthClientInformationFull
) -> OAuthToken:
    code = await _issue_code(provider, client)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    return await provider.exchange_authorization_code(client, loaded)


# --- registratie en allowlist ---------------------------------------------------------------


@pytest.mark.parametrize("uri", [CLAUDE_CB, CLAUDE_COM_CB])
async def test_allowed_redirects_register(provider: PitoolboxOAuthProvider, uri: str) -> None:
    await provider.register_client(client_info(redirect_uris=[uri]))
    assert await provider.get_client("client-1") is not None


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai.evil.com/api/mcp/auth_callback",
        "https://evil.com/api/mcp/auth_callback",
        "https://claude.ai@evil.com/api/mcp/auth_callback",
        "https://www.claude.ai/api/mcp/auth_callback",
        "https://claude.ai:8443/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/",
        "https://claude.ai/api/mcp/auth_callback?next=https://evil.com",
        "https://claude.ai/api/mcp/auth_callback#frag",
        "https://claude.ai/api/mcp/auth_callbackx",
        "https://claude.ai/api/mcp",
        "https://claude.ai/",
        "https://claude.ai",
        "http://localhost:3118/callback",
        "http://127.0.0.1:3118/callback",
        "http://[::1]:3118/callback",
        "claude://auth_callback",
    ],
)
async def test_near_miss_redirects_are_rejected(provider: PitoolboxOAuthProvider, uri: str) -> None:
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client_info(redirect_uris=[uri]))
    assert exc.value.error == "invalid_redirect_uri"
    assert await provider.get_client("client-1") is None


async def test_one_bad_uri_among_good_ones_rejects_registration(
    provider: PitoolboxOAuthProvider,
) -> None:
    with pytest.raises(RegistrationError):
        await provider.register_client(
            client_info(redirect_uris=[CLAUDE_CB, "https://evil.com/cb"])
        )


async def test_extra_redirect_uris_for_tests(tmp_path: Path, clock: FakeClock) -> None:
    extra = "http://127.0.0.1:9999/cb"
    provider = build_auth(
        make_settings(tmp_path), clock=clock, extra_redirect_uris=[extra]
    ).provider
    await provider.register_client(client_info(redirect_uris=[extra]))
    default = build_auth(make_settings(tmp_path / "other"), clock=clock).provider
    with pytest.raises(RegistrationError):
        await default.register_client(client_info(redirect_uris=[extra]))


async def test_registration_forces_public_client_and_scopes(
    provider: PitoolboxOAuthProvider,
) -> None:
    info = client_info(
        token_endpoint_auth_method="client_secret_post",
        client_secret="geheim",
        client_secret_expires_at=0,
        scope="pitoolbox",
    )
    await provider.register_client(info)
    # Het antwoord van de SDK is dit zelfde (gemuteerde) object.
    assert info.token_endpoint_auth_method == "none"
    assert info.client_secret is None
    stored = await provider.get_client("client-1")
    assert stored is not None
    assert stored.token_endpoint_auth_method == "none"
    assert stored.client_secret is None
    assert stored.client_secret_expires_at is None
    assert stored.scope == "pitoolbox offline_access"
    assert [str(u) for u in stored.redirect_uris or []] == [CLAUDE_CB]


async def test_registration_rejects_overlong_client_name(provider: PitoolboxOAuthProvider) -> None:
    with pytest.raises(RegistrationError) as exc:
        await provider.register_client(client_info(client_name="x" * 500))
    assert exc.value.error == "invalid_client_metadata"


async def test_client_cap_and_idle_purge(
    provider: PitoolboxOAuthProvider, clock: FakeClock
) -> None:
    for i in range(MAX_CLIENTS + 5):
        clock.advance(1)
        await provider.register_client(client_info(f"c{i}"))
    assert provider.store.count_clients() == MAX_CLIENTS
    assert await provider.get_client("c0") is None
    assert await provider.get_client("c4") is None
    assert await provider.get_client("c5") is not None
    clock.advance(31 * 86400)
    await provider.register_client(client_info("fresh"))
    assert provider.store.count_clients() == 1


# --- authorize ----------------------------------------------------------------------------------


async def test_authorize_returns_login_url_with_request(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    url = await provider.authorize(client, _params())
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://toolbox.example.com/login"
    request_id = parse_qs(parts.query)["request"][0]
    row = provider.store.get_auth_request(request_id)
    assert row is not None
    assert row.client_id == client.client_id
    assert row.params["redirect_uri"] == CLAUDE_CB
    assert row.params["state"] == "st4te"
    assert row.params["scopes"] == ["pitoolbox"]  # standaardscope
    assert row.params["resource"] == RESOURCE


async def test_authorize_rejects_unregistered_redirect(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, _params(CLAUDE_COM_CB))  # staat niet bij deze client
    assert exc.value.error == "invalid_request"


async def test_authorize_rejects_allowlist_violation_even_if_registered(
    provider: PitoolboxOAuthProvider,
) -> None:
    client = await _registered(provider)
    # Defense in depth: een verouderde clientrecord wordt ook bij /authorize geweigerd.
    client.redirect_uris = [AnyUrl("https://evil.com/cb")]
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, _params("https://evil.com/cb"))


@pytest.mark.parametrize("scopes", [["admin"], ["pitoolbox", "admin"], ["offline_access"], []])
async def test_authorize_scope_validation(
    provider: PitoolboxOAuthProvider, scopes: list[str]
) -> None:
    client = await _registered(provider)
    if scopes == []:
        # Lege lijst valt terug op de standaardscope.
        await provider.authorize(client, _params(scopes=scopes))
        return
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, _params(scopes=scopes))
    assert exc.value.error == "invalid_scope"


async def test_authorize_accepts_offline_access_with_pitoolbox(
    provider: PitoolboxOAuthProvider,
) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client, scopes=["pitoolbox", "offline_access"])
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    assert loaded.scopes == ["pitoolbox", "offline_access"]


@pytest.mark.parametrize("challenge", ["kort", "a" * 42, "a" * 129, "a" * 43 + "!"])
async def test_authorize_rejects_bad_code_challenge(
    provider: PitoolboxOAuthProvider, challenge: str
) -> None:
    client = await _registered(provider)
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, _params(challenge=challenge))
    assert exc.value.error == "invalid_request"


# --- resource-binding (RFC 8707) ----------------------------------------------------------------


async def test_resource_binding_on_tokens(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    access = await provider.load_access_token(tokens.access_token)
    assert access is not None
    assert access.resource == RESOURCE
    assert access.client_id == client.client_id
    assert access.subject == "owner"
    assert access.scopes == ["pitoolbox"]
    assert access.claims == {"iss": "https://toolbox.example.com"}


async def test_missing_resource_defaults_to_own_resource(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client, resource=None)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    assert loaded.resource == RESOURCE


async def test_trailing_slash_resource_is_normalized(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client, resource=RESOURCE + "/")
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    assert loaded.resource == RESOURCE


@pytest.mark.parametrize(
    "resource",
    [
        "https://evil.example.com/mcp",
        "https://toolbox.example.com",
        "https://toolbox.example.com/other",
    ],
)
async def test_foreign_resource_is_invalid_target(
    provider: PitoolboxOAuthProvider, resource: str
) -> None:
    client = await _registered(provider)
    with pytest.raises(AuthorizeError) as exc:
        await provider.authorize(client, _params(resource=resource))
    assert exc.value.error == "invalid_target"


# --- autorisatiecode -------------------------------------------------------------------------


async def test_code_is_single_use(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    assert loaded.redirect_uri_provided_explicitly
    assert str(loaded.redirect_uri) == CLAUDE_CB
    tokens = await provider.exchange_authorization_code(client, loaded)
    # Tweede keer laden: weg, en de al uitgegeven tokens worden ingetrokken.
    assert await provider.load_authorization_code(client, code) is None
    assert await provider.load_access_token(tokens.access_token) is None
    assert tokens.refresh_token is not None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_concurrent_double_exchange_second_fails(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    first = await provider.exchange_authorization_code(client, loaded)
    with pytest.raises(TokenError) as exc:
        await provider.exchange_authorization_code(client, loaded)
    assert exc.value.error == "invalid_grant"
    assert await provider.load_access_token(first.access_token) is None


async def test_code_expires(provider: PitoolboxOAuthProvider, clock: FakeClock) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client)
    clock.advance(300 - 1)
    assert await provider.load_authorization_code(client, code) is not None
    clock.advance(2)
    assert await provider.load_authorization_code(client, code) is None


async def test_expired_code_cannot_be_exchanged(
    provider: PitoolboxOAuthProvider, clock: FakeClock
) -> None:
    client = await _registered(provider)
    code = await _issue_code(provider, client)
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    clock.advance(301)
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, loaded)


async def test_code_is_bound_to_client(provider: PitoolboxOAuthProvider) -> None:
    one = await _registered(provider, "one")
    two = await _registered(provider, "two")
    code = await _issue_code(provider, one)
    assert await provider.load_authorization_code(two, code) is None
    assert await provider.load_authorization_code(one, code) is not None


async def test_unknown_code(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    assert await provider.load_authorization_code(client, "nope") is None


async def test_auth_request_is_consumed_by_code_creation(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    url = await provider.authorize(client, _params())
    request_id = parse_qs(urlsplit(url).query)["request"][0]
    assert provider.create_authorization_code(request_id) is not None
    assert provider.create_authorization_code(request_id) is None
    assert provider.create_authorization_code("onbekend") is None


async def test_auth_request_expires(provider: PitoolboxOAuthProvider, clock: FakeClock) -> None:
    client = await _registered(provider)
    url = await provider.authorize(client, _params())
    request_id = parse_qs(urlsplit(url).query)["request"][0]
    clock.advance(601)
    assert provider.create_authorization_code(request_id) is None


# --- access-token -----------------------------------------------------------------------------


async def test_token_response_shape(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    assert tokens.token_type == "Bearer"
    assert tokens.expires_in == 900
    assert tokens.scope == "pitoolbox"
    assert tokens.refresh_token
    assert tokens.access_token != tokens.refresh_token


async def test_access_token_expires(provider: PitoolboxOAuthProvider, clock: FakeClock) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    clock.advance(899)
    assert await provider.load_access_token(tokens.access_token) is not None
    clock.advance(2)
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_access_token("onbekend") is None


# --- refresh-rotatie -----------------------------------------------------------------------------


async def _refresh(
    provider: PitoolboxOAuthProvider, client: OAuthClientInformationFull, token: str
) -> OAuthToken:
    loaded = await provider.load_refresh_token(client, token)
    assert loaded is not None
    return await provider.exchange_refresh_token(client, loaded, loaded.scopes)


async def test_refresh_rotation(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    first = await _tokens(provider, client)
    assert first.refresh_token
    second = await _refresh(provider, client, first.refresh_token)
    assert second.refresh_token
    assert second.refresh_token != first.refresh_token
    assert second.access_token != first.access_token
    access = await provider.load_access_token(second.access_token)
    assert access is not None
    assert access.resource == RESOURCE
    # De nieuwe refresh-token werkt; de oude niet meer.
    third = await _refresh(provider, client, second.refresh_token)
    assert third.refresh_token
    assert third.refresh_token not in (first.refresh_token, second.refresh_token)


async def test_reuse_of_rotated_refresh_token_revokes_family(
    provider: PitoolboxOAuthProvider,
) -> None:
    client = await _registered(provider)
    first = await _tokens(provider, client)
    assert first.refresh_token
    second = await _refresh(provider, client, first.refresh_token)
    assert second.refresh_token
    # Aanvaller (of een herhaling) biedt het oude token opnieuw aan.
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    # De hele familie is nu ingetrokken, ook het legitieme nieuwe token.
    assert await provider.load_refresh_token(client, second.refresh_token) is None
    assert await provider.load_access_token(second.access_token) is None
    assert await provider.load_access_token(first.access_token) is None


async def test_exchange_with_stale_loaded_token_is_invalid_grant_and_revokes(
    provider: PitoolboxOAuthProvider,
) -> None:
    client = await _registered(provider)
    first = await _tokens(provider, client)
    assert first.refresh_token
    loaded = await provider.load_refresh_token(client, first.refresh_token)
    assert loaded is not None
    second = await provider.exchange_refresh_token(client, loaded, loaded.scopes)
    with pytest.raises(TokenError) as exc:  # race: dezelfde geladen token nogmaals
        await provider.exchange_refresh_token(client, loaded, loaded.scopes)
    assert exc.value.error == "invalid_grant"
    assert await provider.load_access_token(second.access_token) is None


async def test_families_are_independent(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    a = await _tokens(provider, client)
    b = await _tokens(provider, client)
    assert a.refresh_token
    assert b.refresh_token
    await _refresh(provider, client, a.refresh_token)
    assert await provider.load_refresh_token(client, a.refresh_token) is None  # trekt familie A in
    assert await provider.load_refresh_token(client, b.refresh_token) is not None


async def test_refresh_token_is_bound_to_client(provider: PitoolboxOAuthProvider) -> None:
    one = await _registered(provider, "one")
    two = await _registered(provider, "two")
    tokens = await _tokens(provider, one)
    assert tokens.refresh_token
    assert await provider.load_refresh_token(two, tokens.refresh_token) is None
    assert await provider.load_refresh_token(one, tokens.refresh_token) is not None


async def test_refresh_token_expires(provider: PitoolboxOAuthProvider, clock: FakeClock) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    assert tokens.refresh_token
    clock.advance(7 * 86400 + 1)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_refresh_expiry_slides_with_rotation(
    provider: PitoolboxOAuthProvider, clock: FakeClock
) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    assert tokens.refresh_token
    clock.advance(6 * 86400)
    tokens = await _refresh(provider, client, tokens.refresh_token)
    assert tokens.refresh_token
    clock.advance(6 * 86400)  # 12 dagen na de start, maar 6 na de laatste rotatie
    assert await provider.load_refresh_token(client, tokens.refresh_token) is not None


async def test_refresh_scope_cannot_be_widened_or_drop_pitoolbox(
    provider: PitoolboxOAuthProvider,
) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    assert tokens.refresh_token
    loaded = await provider.load_refresh_token(client, tokens.refresh_token)
    assert loaded is not None
    with pytest.raises(TokenError) as exc:
        await provider.exchange_refresh_token(client, loaded, ["pitoolbox", "offline_access"])
    assert exc.value.error == "invalid_scope"
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, loaded, [])
    # Een mislukte scopecontrole mag het token niet verbruiken.
    assert await provider.load_refresh_token(client, tokens.refresh_token) is not None


# --- intrekken -----------------------------------------------------------------------------------


async def test_revoke_access_token_revokes_family(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    access = await provider.load_access_token(tokens.access_token)
    assert isinstance(access, AccessToken)
    await provider.revoke_token(access)
    assert await provider.load_access_token(tokens.access_token) is None
    assert tokens.refresh_token
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_revoke_refresh_token_revokes_family(provider: PitoolboxOAuthProvider) -> None:
    client = await _registered(provider)
    tokens = await _tokens(provider, client)
    assert tokens.refresh_token
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    assert isinstance(refresh, RefreshToken)
    await provider.revoke_token(refresh)
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_revoke_unknown_token_is_noop(provider: PitoolboxOAuthProvider) -> None:
    await provider.revoke_token(
        AccessToken(token="onbekend", client_id="c", scopes=[], expires_at=None)
    )


# --- build_auth ---------------------------------------------------------------------------------


def test_build_auth_settings(bundle: AuthBundle) -> None:
    s = bundle.auth_settings
    assert str(s.issuer_url) == "https://toolbox.example.com"
    assert str(s.resource_server_url) == RESOURCE
    assert s.validate_token_resource is True
    assert s.required_scopes == ["pitoolbox"]
    assert s.client_registration_options is not None
    assert s.client_registration_options.enabled
    assert s.client_registration_options.valid_scopes == ["pitoolbox", "offline_access"]
    assert s.client_registration_options.default_scopes == ["pitoolbox"]
    assert s.revocation_options is not None
    assert s.revocation_options.enabled
    assert [(r.path, sorted(r.methods or [])) for r in bundle.routes] == [
        ("/login", ["GET", "HEAD", "POST"])
    ]


@pytest.mark.parametrize(
    ("public_url", "insecure", "ok"),
    [
        (None, False, False),
        ("", False, False),
        ("http://toolbox.example.com", False, False),
        ("http://127.0.0.1:8765", False, False),
        ("http://127.0.0.1:8765", True, True),
        ("http://localhost:8765", True, True),
        ("http://toolbox.example.com", True, False),
        ("https://toolbox.example.com/", False, True),
        ("https://toolbox.example.com/sub", False, False),
        ("https://toolbox.example.com?x=1", False, False),
        ("ftp://toolbox.example.com", False, False),
    ],
)
def test_build_auth_public_url_validation(
    tmp_path: Path, public_url: str | None, insecure: bool, ok: bool
) -> None:
    settings = make_settings(tmp_path, public_url=public_url)
    if ok:
        bundle = build_auth(settings, allow_insecure_localhost=insecure)
        assert str(bundle.auth_settings.resource_server_url).endswith("/mcp")
    else:
        with pytest.raises(ValueError, match="PITOOLBOX_PUBLIC_URL"):
            build_auth(settings, allow_insecure_localhost=insecure)
