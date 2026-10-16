"""End-to-end OAuth-flow tegen een echte Starlette-app uit MCPServer + onze provider.

Doorloopt wat Claude.ai doet: 401 met resource_metadata, metadata ophalen, DCR, /authorize met
PKCE, inloggen met wachtwoord + TOTP, /token, MCP-aanroepen met de bearer en refresh-rotatie.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient
from test_auth_support import (
    CLAUDE_CB,
    CLAUDE_COM_CB,
    PASSWORD,
    PUBLIC_URL,
    RESOURCE,
    FakeClock,
    make_settings,
    pkce_pair,
)

from pitoolbox.auth import AuthBundle, build_auth
from pitoolbox.auth.crypto import generate_totp_secret, hash_password, totp
from pitoolbox.auth.store import TokenRow

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@dataclass
class Env:
    client: TestClient
    bundle: AuthBundle
    clock: FakeClock
    secret: str

    def code(self) -> str:
        return totp(self.secret, self.clock())


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    clock = FakeClock()
    bundle = build_auth(make_settings(tmp_path), clock=clock, fail_delay_s=0)
    secret = generate_totp_secret()
    bundle.provider.store.set_user(hash_password(PASSWORD), secret)

    server = MCPServer("auth-test", auth=bundle.auth_settings, auth_server_provider=bundle.provider)

    @server.tool()
    def ping() -> str:
        """Triviale tool."""
        return "pong"

    # Zo registreert de HTTP-frontend de loginroutes.
    for route in bundle.routes:
        server.custom_route(route.path, methods=sorted(route.methods or ["GET"]))(route.endpoint)

    app = server.streamable_http_app(
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["toolbox.example.com"],
            allowed_origins=["https://toolbox.example.com"],
        ),
    )
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as client:
        yield Env(client, bundle, clock, secret)


# --- hulpfuncties ---------------------------------------------------------------------------------


def register(env: Env, redirect: str = CLAUDE_CB, **extra: Any) -> dict[str, Any]:
    body = {
        "client_name": "Claude",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **extra,
    }
    resp = env.client.post("/register", json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def start_authorize(
    env: Env,
    client_id: str,
    challenge: str,
    *,
    state: str = "xyz",
    scope: str = "pitoolbox offline_access",
) -> str:
    resp = env.client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CB,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": scope,
            "resource": RESOURCE,
        },
    )
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith(f"{PUBLIC_URL}/login?request=")
    return parse_qs(urlsplit(location).query)["request"][0]


def login_form(env: Env, request_id: str) -> dict[str, str]:
    """Haal de loginpagina op en lees de verborgen velden uit."""
    resp = env.client.get("/login", params={"request": request_id})
    assert resp.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', resp.text)
    assert csrf
    return {"request": request_id, "csrf": csrf.group(1)}


def post_login(
    env: Env, request_id: str, *, password: str = PASSWORD, code: str | None = None, **extra: str
) -> Any:
    form = login_form(env, request_id)
    form.update(
        {"password": password, "code": env.code() if code is None else code, "action": "approve"}
    )
    form.update(extra)
    return env.client.post("/login", data=form)


def complete_login(env: Env, request_id: str) -> tuple[str, str | None]:
    resp = post_login(env, request_id)
    assert resp.status_code == 302, resp.text
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    return query["code"][0], (query.get("state") or [None])[0]


def exchange_code(
    env: Env, client_id: str, code: str, verifier: str, redirect: str = CLAUDE_CB
) -> Any:
    return env.client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )


def full_login(env: Env) -> tuple[str, dict[str, Any]]:
    client_id = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    request_id = start_authorize(env, client_id, challenge)
    code, _ = complete_login(env, request_id)
    resp = exchange_code(env, client_id, code, verifier)
    assert resp.status_code == 200, resp.text
    return client_id, dict(resp.json())


def mcp(env: Env, token: str | None, payload: dict[str, Any], session: str | None = None) -> Any:
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session:
        headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = "2025-06-18"
    return env.client.post("/mcp", headers=headers, content=json.dumps(payload))


def initialize(env: Env, token: str | None) -> Any:
    return mcp(
        env,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )


# --- discovery ---------------------------------------------------------------------------------


def test_mcp_without_token_is_401_with_resource_metadata(env: Env) -> None:
    resp = initialize(env, None)
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert env.client.get("/mcp", headers=MCP_HEADERS).status_code == 401


def test_protected_resource_metadata(env: Env) -> None:
    resp = env.client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["resource"] == RESOURCE
    assert doc["authorization_servers"][0].rstrip("/") == PUBLIC_URL
    assert doc["scopes_supported"] == ["pitoolbox"]
    assert doc["bearer_methods_supported"] == ["header"]


def test_authorization_server_metadata(env: Env) -> None:
    resp = env.client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["issuer"].rstrip("/") == PUBLIC_URL
    assert doc["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert doc["token_endpoint"] == f"{PUBLIC_URL}/token"
    assert doc["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert doc["revocation_endpoint"] == f"{PUBLIC_URL}/revoke"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["response_types_supported"] == ["code"]
    assert set(doc["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert doc["scopes_supported"] == ["pitoolbox", "offline_access"]


# --- registratie ---------------------------------------------------------------------------------


@pytest.mark.parametrize("uri", [CLAUDE_CB, CLAUDE_COM_CB])
def test_register_claude_redirects(env: Env, uri: str) -> None:
    info = register(env, uri)
    assert info["redirect_uris"] == [uri]
    assert info["token_endpoint_auth_method"] == "none"
    assert info.get("client_secret") is None
    assert info["client_id"]


def test_register_without_auth_method_still_gives_public_client(env: Env) -> None:
    resp = env.client.post(
        "/register", json={"client_name": "Claude", "redirect_uris": [CLAUDE_CB]}
    )
    assert resp.status_code == 201
    info = resp.json()
    assert info["token_endpoint_auth_method"] == "none"
    assert info.get("client_secret") is None


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.com/cb",
        "https://claude.ai.evil.com/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback/",
        "https://claude.ai/api/mcp/auth_callback?x=1",
        "http://localhost:3118/callback",
    ],
)
def test_register_evil_redirect_is_400(env: Env, uri: str) -> None:
    resp = env.client.post(
        "/register",
        json={"client_name": "x", "redirect_uris": [uri], "token_endpoint_auth_method": "none"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


# --- /authorize en /login ----------------------------------------------------------------------


def test_authorize_redirects_to_login_and_login_shows_hostname(env: Env) -> None:
    info = register(env, CLAUDE_COM_CB)
    _, challenge = pkce_pair()
    resp = env.client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": info["client_id"],
            "redirect_uri": CLAUDE_COM_CB,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "abc",
            "resource": RESOURCE,
        },
    )
    assert resp.status_code == 302
    request_id = parse_qs(urlsplit(resp.headers["location"]).query)["request"][0]
    page = env.client.get("/login", params={"request": request_id})
    assert page.status_code == 200
    assert '<div class="host">claude.com</div>' in page.text
    assert CLAUDE_COM_CB in page.text
    assert "Claude" in page.text
    assert "pitoolbox" in page.text
    assert "Weigeren" in page.text
    assert "http" not in page.text.replace(CLAUDE_COM_CB, "").replace("viewport", "")


def test_login_page_security_headers(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    resp = env.client.get("/login", params={"request": request_id})
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp
    # form-action: alleen onszelf en de oorsprong van de redirect van dit verzoek (Chromium past
    # form-action ook op de redirect na de POST toe).
    assert "form-action 'self' https://claude.ai;" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    assert "<script" not in resp.text.lower()


def test_login_page_escapes_client_name(env: Env) -> None:
    info = register(env, client_name="<script>alert(1)</script>")
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    page = env.client.get("/login", params={"request": request_id}).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_login_unknown_or_missing_request(env: Env) -> None:
    assert env.client.get("/login").status_code == 400
    assert env.client.get("/login", params={"request": "bestaat-niet"}).status_code == 400
    assert env.client.post("/login", data={"request": "bestaat-niet"}).status_code == 400


def test_login_wrong_code_does_not_redirect_and_is_counted(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    resp = post_login(env, request_id, code="000000" if env.code() != "000000" else "111111")
    assert resp.status_code == 401
    assert "location" not in resp.headers
    assert "Onjuist" in resp.text
    user = env.bundle.provider.store.get_user()
    assert user is not None
    assert user.failed_attempts == 1
    # Het verzoek blijft bruikbaar voor een nieuwe poging.
    assert env.bundle.provider.store.get_auth_request(request_id) is not None


def test_login_wrong_password_is_counted(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    resp = post_login(env, request_id, password="verkeerd wachtwoord!!")
    assert resp.status_code == 401
    user = env.bundle.provider.store.get_user()
    assert user is not None
    assert user.failed_attempts == 1


def test_login_csrf_is_required(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    resp = env.client.post(
        "/login",
        data={
            "request": request_id,
            "csrf": "fout",
            "password": PASSWORD,
            "code": env.code(),
            "action": "approve",
        },
    )
    assert resp.status_code == 403
    user = env.bundle.provider.store.get_user()
    assert user is not None
    assert user.failed_attempts == 0  # CSRF-fouten tellen niet als inlogpoging
    assert "location" not in resp.headers


def test_deny_redirects_with_access_denied(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1], state="st-1")
    form = login_form(env, request_id)
    resp = env.client.post("/login", data={**form, "action": "deny"})
    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE_CB
    query = parse_qs(location.query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["st-1"]
    assert "code" not in query
    # Verzoek is verbruikt.
    assert env.client.get("/login", params={"request": request_id}).status_code == 400


def test_lockout_after_five_failures_and_recovery(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    for _ in range(4):
        assert post_login(env, request_id, password="fout fout fout 1").status_code == 401
    fifth = post_login(env, request_id, password="fout fout fout 1")
    assert fifth.status_code == 429
    assert "retry-after" in fifth.headers
    # Tijdens de blokkade werken zelfs juiste gegevens niet.
    blocked = post_login(env, request_id)
    assert blocked.status_code == 429
    assert "location" not in blocked.headers
    # Het inlogverzoek zelf verloopt na 10 min; na de blokkade begint de gebruiker opnieuw.
    env.clock.advance(15 * 60 - 1)
    still = start_authorize(env, info["client_id"], pkce_pair()[1])
    assert post_login(env, still).status_code == 429
    env.clock.advance(2)
    fresh = start_authorize(env, info["client_id"], pkce_pair()[1])
    ok = post_login(env, fresh)
    assert ok.status_code == 302
    user = env.bundle.provider.store.get_user()
    assert user is not None
    assert (user.failed_attempts, user.locked_until) == (0, 0)


def test_success_resets_failure_counter(env: Env) -> None:
    info = register(env)
    request_id = start_authorize(env, info["client_id"], pkce_pair()[1])
    for _ in range(3):
        post_login(env, request_id, password="fout fout fout 1")
    assert post_login(env, request_id).status_code == 302
    user = env.bundle.provider.store.get_user()
    assert user is not None
    assert user.failed_attempts == 0


def test_totp_code_cannot_be_replayed(env: Env) -> None:
    info = register(env)
    _verifier, challenge = pkce_pair()
    first = start_authorize(env, info["client_id"], challenge)
    code = env.code()
    assert post_login(env, first, code=code).status_code == 302
    second = start_authorize(env, info["client_id"], challenge)
    replay = post_login(env, second, code=code)
    assert replay.status_code == 401
    assert "location" not in replay.headers
    # Een volgende tijdstap werkt weer.
    env.clock.advance(30)
    assert post_login(env, second).status_code == 302


def test_login_without_user_configured(tmp_path: Path) -> None:
    bundle = build_auth(make_settings(tmp_path), clock=FakeClock(), fail_delay_s=0)
    server = MCPServer("t", auth=bundle.auth_settings, auth_server_provider=bundle.provider)
    for route in bundle.routes:
        server.custom_route(route.path, methods=sorted(route.methods or ["GET"]))(route.endpoint)
    app = server.streamable_http_app(json_response=True)
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as client:
        info = client.post(
            "/register", json={"redirect_uris": [CLAUDE_CB], "token_endpoint_auth_method": "none"}
        ).json()
        resp = client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": info["client_id"],
                "redirect_uri": CLAUDE_CB,
                "code_challenge": pkce_pair()[1],
                "code_challenge_method": "S256",
                "resource": RESOURCE,
            },
        )
        request_id = parse_qs(urlsplit(resp.headers["location"]).query)["request"][0]
        page = client.get("/login", params={"request": request_id})
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert csrf
        post = client.post(
            "/login",
            data={
                "request": request_id,
                "csrf": csrf.group(1),
                "password": "x",
                "code": "123456",
                "action": "approve",
            },
        )
        assert post.status_code == 503


# --- volledige flow -----------------------------------------------------------------------------


def test_full_flow_to_mcp_call(env: Env) -> None:
    client_id = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    request_id = start_authorize(env, client_id, challenge, state="state-123")

    resp = post_login(env, request_id)
    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE_CB
    query = parse_qs(location.query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]
    assert len(code) >= 43

    token_resp = exchange_code(env, client_id, code, verifier)
    assert token_resp.status_code == 200, token_resp.text
    assert token_resp.headers["cache-control"] == "no-store"
    tokens = token_resp.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 900
    assert set(tokens["scope"].split()) == {"pitoolbox", "offline_access"}
    assert tokens["refresh_token"]

    # MCP: initialize, initialized, tools/list, tools/call.
    init = initialize(env, tokens["access_token"])
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "auth-test"
    session = init.headers.get("mcp-session-id")
    note = mcp(
        env,
        tokens["access_token"],
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session,
    )
    assert note.status_code in (200, 202)
    listing = mcp(
        env, tokens["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session
    )
    assert listing.status_code == 200, listing.text
    assert [t["name"] for t in listing.json()["result"]["tools"]] == ["ping"]
    call = mcp(
        env,
        tokens["access_token"],
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
        session,
    )
    assert call.status_code == 200, call.text
    assert call.json()["result"]["content"][0]["text"] == "pong"


def test_code_cannot_be_exchanged_twice(env: Env) -> None:
    client_id = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    code, _ = complete_login(env, start_authorize(env, client_id, challenge))
    first = exchange_code(env, client_id, code, verifier)
    assert first.status_code == 200
    second = exchange_code(env, client_id, code, verifier)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    # Hergebruik trekt ook de eerder uitgegeven tokens in.
    assert initialize(env, first.json()["access_token"]).status_code == 401


def test_wrong_pkce_verifier_is_invalid_grant(env: Env) -> None:
    client_id = register(env)["client_id"]
    _, challenge = pkce_pair()
    code, _ = complete_login(env, start_authorize(env, client_id, challenge))
    resp = exchange_code(env, client_id, code, pkce_pair()[0])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_expired_code_is_invalid_grant(env: Env) -> None:
    client_id = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    code, _ = complete_login(env, start_authorize(env, client_id, challenge))
    env.clock.advance(301)
    resp = exchange_code(env, client_id, code, verifier)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_code_for_other_client_is_invalid_grant(env: Env) -> None:
    one = register(env)["client_id"]
    two = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    code, _ = complete_login(env, start_authorize(env, one, challenge))
    resp = exchange_code(env, two, code, verifier)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_endpoint_redirect_uri_must_match(env: Env) -> None:
    client_id = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    code, _ = complete_login(env, start_authorize(env, client_id, challenge))
    resp = exchange_code(env, client_id, code, verifier, redirect=CLAUDE_COM_CB)
    assert resp.status_code == 400


def test_authorize_with_foreign_redirect_is_not_redirected(env: Env) -> None:
    client_id = register(env)["client_id"]
    resp = env.client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://evil.com/cb",
            "code_challenge": pkce_pair()[1],
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_authorize_requires_pkce_s256(env: Env) -> None:
    client_id = register(env)["client_id"]
    base = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLAUDE_CB,
        "resource": RESOURCE,
    }
    no_pkce = env.client.get("/authorize", params=base)
    assert no_pkce.status_code in (302, 400)
    assert "/login" not in no_pkce.headers.get("location", "")
    plain = env.client.get(
        "/authorize", params={**base, "code_challenge": "x" * 43, "code_challenge_method": "plain"}
    )
    assert "/login" not in plain.headers.get("location", "")


def test_authorize_with_foreign_resource_is_rejected(env: Env) -> None:
    client_id = register(env)["client_id"]
    resp = env.client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CB,
            "code_challenge": pkce_pair()[1],
            "code_challenge_method": "S256",
            "resource": "https://evil.example.com/mcp",
            "state": "s",
        },
    )
    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE_CB
    assert parse_qs(location.query)["error"] == ["invalid_target"]


def test_refresh_rotation_and_reuse_detection(env: Env) -> None:
    client_id, tokens = full_login(env)

    def refresh(token: str) -> Any:
        return env.client.post(
            "/token",
            data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id},
        )

    first = refresh(tokens["refresh_token"])
    assert first.status_code == 200, first.text
    rotated = first.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert rotated["access_token"] != tokens["access_token"]
    assert initialize(env, rotated["access_token"]).status_code == 200

    # Hergebruik van het oude refresh-token: invalid_grant.
    reuse = refresh(tokens["refresh_token"])
    assert reuse.status_code == 400
    assert reuse.json()["error"] == "invalid_grant"

    # ... en de hele familie is ingetrokken, ook het nieuwe refresh- en access-token.
    again = refresh(rotated["refresh_token"])
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"
    assert initialize(env, rotated["access_token"]).status_code == 401


def test_refresh_unknown_token_is_invalid_grant(env: Env) -> None:
    client_id = register(env)["client_id"]
    resp = env.client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": client_id},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_refresh_after_expiry_is_invalid_grant(env: Env) -> None:
    client_id, tokens = full_login(env)
    env.clock.advance(7 * 86400 + 1)
    resp = env.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_expired_access_token_is_401(env: Env) -> None:
    _, tokens = full_login(env)
    assert initialize(env, tokens["access_token"]).status_code == 200
    env.clock.advance(901)
    resp = initialize(env, tokens["access_token"])
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


def test_garbage_bearer_is_401(env: Env) -> None:
    assert initialize(env, "geen-token").status_code == 401


def test_token_for_other_resource_is_refused(env: Env) -> None:
    """validate_token_resource=True: een token met een andere resource geeft 401."""
    client_id, _ = full_login(env)
    store = env.bundle.provider.store
    store.add_access_token(
        "vreemd-token",
        TokenRow(
            client_id,
            "fam-x",
            ["pitoolbox"],
            "https://elders.example.com/mcp",
            "owner",
            env.clock() + 600,
        ),
    )
    assert initialize(env, "vreemd-token").status_code == 401


def test_token_without_required_scope_is_403(env: Env) -> None:
    client_id, _ = full_login(env)
    env.bundle.provider.store.add_access_token(
        "zonder-scope",
        TokenRow(client_id, "fam-y", ["offline_access"], RESOURCE, "owner", env.clock() + 600),
    )
    assert initialize(env, "zonder-scope").status_code == 403


def test_revoke_endpoint_kills_the_family(env: Env) -> None:
    client_id, tokens = full_login(env)
    resp = env.client.post(
        "/revoke",
        # SDK-eigenaardigheid: RevocationRequest.client_secret is verplicht (ook publiek).
        data={"token": tokens["refresh_token"], "client_id": client_id, "client_secret": ""},
    )
    assert resp.status_code == 200
    assert initialize(env, tokens["access_token"]).status_code == 401
    refresh = env.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refresh.status_code == 400
    assert refresh.json()["error"] == "invalid_grant"


def test_dns_rebinding_protection_needs_public_host(env: Env) -> None:
    """Productie moet allowed_hosts op de publieke hostnaam zetten (anders 421 op /mcp)."""
    _, tokens = full_login(env)
    headers = {
        **MCP_HEADERS,
        "Authorization": f"Bearer {tokens['access_token']}",
        "Host": "evil.example.org",
    }
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert env.client.post("/mcp", headers=headers, content=body).status_code == 421


def test_urlencoded_body_helper_sanity() -> None:
    # /token en /revoke verwachten application/x-www-form-urlencoded (Claude-documentatie).
    assert urlencode({"a": "b c"}) == "a=b+c"
