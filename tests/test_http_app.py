"""Integratie: build_http_app (onze productiewiring) + echte OAuth + ToolRunner.

Controleert wat alleen in de samengestelde app zichtbaar is: 401 met resource_metadata,
Host-header-check, dat een transmit-bevestiging gebonden is aan de OAuth-client en dat onze
/revoke die van de SDK overschaduwt.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_auth_flow import (
    Env,
    complete_login,
    exchange_code,
    full_login,
    initialize,
    register,
    start_authorize,
)
from test_auth_support import PASSWORD, PUBLIC_URL, FakeClock, make_settings, pkce_pair
from test_mcp_server import BSSID, Harness
from uvicorn.logging import AccessFormatter

from pitoolbox.auth import build_auth
from pitoolbox.auth.crypto import generate_totp_secret, hash_password
from pitoolbox.core.scope import Scope
from pitoolbox.frontends import mcp_server
from pitoolbox.frontends.mcp_server import (
    _require_loopback,
    build_http_app,
    build_server,
    transport_security_for,
    uvicorn_log_config,
)

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
PROTOCOL = "2025-06-18"


@pytest.fixture
def setup(tmp_path: Path, make_ctx: Any) -> Iterator[tuple[Env, Harness]]:
    clock = FakeClock()
    settings = make_settings(tmp_path)
    bundle = build_auth(settings, clock=clock, fail_delay_s=0)
    secret = generate_totp_secret()
    bundle.provider.store.set_user(hash_password(PASSWORD), secret)
    scope = Scope.model_validate({"wifi": [{"bssid": BSSID, "channel": 6}]})
    harness = Harness(tmp_path, make_ctx(tmp_path, scope))
    app = build_http_app(harness.runner, settings, bundle)
    with TestClient(app, base_url=PUBLIC_URL, follow_redirects=False) as client:
        yield Env(client, bundle, clock, secret), harness


def login(env: Env) -> tuple[str, str]:
    client_id = register(env)["client_id"]
    verifier, challenge = pkce_pair()
    code, _ = complete_login(env, start_authorize(env, client_id, challenge))
    resp = exchange_code(env, client_id, code, verifier)
    assert resp.status_code == 200, resp.text
    return client_id, str(resp.json()["access_token"])


def _rpc_result(resp: Any) -> Any:
    """Lees één JSON-RPC-antwoord uit een JSON- of SSE-respons."""
    assert resp.status_code == 200, resp.text
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    raise AssertionError(f"geen data in SSE-respons: {resp.text!r}")


class Session:
    def __init__(self, env: Env, token: str) -> None:
        self.env = env
        self.headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        resp = env.client.post(
            "/mcp",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        _rpc_result(resp)
        self.headers["Mcp-Session-Id"] = resp.headers["mcp-session-id"]
        self.headers["MCP-Protocol-Version"] = PROTOCOL
        env.client.post(
            "/mcp",
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self._id = 1

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self._id += 1
        resp = self.env.client.post(
            "/mcp",
            headers=self.headers,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
        )
        return _rpc_result(resp)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return dict(result["structuredContent"])


def test_unauthenticated_mcp_is_401_with_resource_metadata(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    resp = env.client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1})
    assert resp.status_code == 401
    expected = f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"'
    assert expected in resp.headers["www-authenticate"]


def test_tools_listed_and_callable_over_oauth(setup: tuple[Env, Harness]) -> None:
    env, harness = setup
    _, token = login(env)
    s = Session(env, token)
    names = {t["name"] for t in s.request("tools/list", {})["result"]["tools"]}
    assert {spec.name for spec in harness.specs} == names
    assert s.call("read_tool", {"text": "hoi"})["ok"] is True


def test_confirmation_is_bound_to_oauth_client(setup: tuple[Env, Harness]) -> None:
    env, harness = setup
    client_a, token_a = login(env)
    env.clock.advance(30)  # volgende TOTP-stap: dezelfde code mag niet twee keer (replay)
    _, token_b = login(env)
    a, b = Session(env, token_a), Session(env, token_b)

    pending = a.call("transmit_tool", {"bssid": BSSID})
    assert pending["error"] == "confirmation_required"

    # Een andere OAuth-client kan deze pending_id niet gebruiken...
    stolen = b.call("confirm_action", {"pending_id": pending["pending_id"]})
    assert stolen["error"] == "unknown_pending"
    assert "transmit_tool" not in harness.calls

    # ...en de mislukte poging verbruikt hem niet: PendingStore.take() verwijdert alleen bij
    # een match, dus client A kan nog bevestigen.
    ok = a.call("confirm_action", {"pending_id": pending["pending_id"]})
    assert ok["ok"] is True
    assert harness.calls == ["transmit_tool"]

    frontends = {line["frontend"] for line in harness.audit()}
    assert f"mcp-http:{client_a}" in frontends
    assert all(f.startswith("mcp-http:") for f in frontends)


def test_foreign_host_header_is_rejected(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    _, token = login(env)
    resp = env.client.post(
        "/mcp",
        headers={**HEADERS, "Authorization": f"Bearer {token}", "Host": "evil.example.net"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert resp.status_code == 421


def test_transport_security_allows_public_and_loopback(tmp_path: Path) -> None:
    sec = transport_security_for(make_settings(tmp_path))
    assert "toolbox.example.com" in sec.allowed_hosts
    assert "127.0.0.1:8765" in sec.allowed_hosts
    assert "https://claude.ai" in sec.allowed_origins


def test_transport_security_requires_public_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        transport_security_for(make_settings(tmp_path, public_url=None))


def _access_log_line(path: str) -> str:
    """Eén accessregel zoals uvicorn hem logt, door de formatter en de filters die onze logconfig
    aan de access-handler hangt. Op een eigen logger en zonder dictConfig (dat sluit alle
    bestaande handlers, ook die van pytest)."""
    config = uvicorn_log_config()
    filters = {name: spec["()"]() for name, spec in config["filters"].items()}
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter(fmt=config["formatters"]["access"]["fmt"], use_colors=False)
    )
    for name in config["handlers"]["access"]["filters"]:
        handler.addFilter(filters[name])
    logger = logging.getLogger("test.access")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        # Exact de aanroep uit uvicorn/protocols/http/h11_impl.py.
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 200)
    finally:
        logger.removeHandler(handler)
    return stream.getvalue()


def test_access_log_redacts_query_string() -> None:
    line = _access_log_line("/login?request=geheim-request-id")
    assert "geheim-request-id" not in line
    assert "request=" not in line
    assert '127.0.0.1:5555 - "GET /login?[weggelaten] HTTP/1.1" 200' in line


def test_access_log_keeps_path_without_query() -> None:
    assert '"GET /.well-known/oauth-authorization-server HTTP/1.1" 200' in _access_log_line(
        "/.well-known/oauth-authorization-server"
    )


def test_run_http_passes_redacting_log_config(
    tmp_path: Path, make_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    harness = Harness(tmp_path, make_ctx(tmp_path, Scope()))
    mcp_server._run_http(harness.runner, make_settings(tmp_path))
    assert "redact_query" in seen["log_config"]["handlers"]["access"]["filters"]
    assert seen["log_config"]["loggers"]["uvicorn.access"]["handlers"] == ["access"]
    assert "filters" not in LOGGING_CONFIG["handlers"]["access"]  # uvicorn's dict ongemoeid


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_allowed(host: str) -> None:
    _require_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "toolbox.example.com"])  # noqa: S104 - test van de weigering
def test_non_loopback_hosts_refused(host: str) -> None:
    with pytest.raises(SystemExit):
        _require_loopback(host)


# --- /revoke (RFC 7009) -----------------------------------------------------------------------


def _refresh(env: Env, client_id: str, refresh_token: str) -> Any:
    return env.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def test_revoke_public_client_without_secret(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    client_id, tokens = full_login(env)
    resp = env.client.post(
        "/revoke", data={"token": tokens["access_token"], "client_id": client_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["pragma"] == "no-cache"
    # De hele familie is weg: ook het refresh-token.
    assert initialize(env, tokens["access_token"]).status_code == 401
    assert _refresh(env, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_revoke_refresh_token_with_wrong_hint_still_found(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    client_id, tokens = full_login(env)
    resp = env.client.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "token_type_hint": "access_token",
            "client_id": client_id,
        },
    )
    assert resp.status_code == 200
    assert initialize(env, tokens["access_token"]).status_code == 401


def test_revoke_unknown_token_is_200(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    client_id, _ = full_login(env)
    resp = env.client.post("/revoke", data={"token": "bestaat-niet", "client_id": client_id})
    assert resp.status_code == 200


def test_revoke_token_of_other_client_is_ignored(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    _, tokens_a = full_login(env)
    env.clock.advance(30)  # volgende TOTP-stap (replay-bescherming)
    client_b, _ = full_login(env)
    resp = env.client.post(
        "/revoke", data={"token": tokens_a["access_token"], "client_id": client_b}
    )
    # Zelfde antwoord als bij een onbekend token (geen orakel), maar het token van A leeft nog.
    assert resp.status_code == 200
    assert initialize(env, tokens_a["access_token"]).status_code == 200


@pytest.mark.parametrize(
    ("form", "status", "error"),
    [
        ({"token": "x"}, 401, "invalid_client"),
        ({"token": "x", "client_id": "onbekend"}, 401, "invalid_client"),
        ({"client_id": "<geregistreerd>"}, 400, "invalid_request"),
    ],
)
def test_revoke_rejects_bad_requests(
    setup: tuple[Env, Harness], form: dict[str, str], status: int, error: str
) -> None:
    env, _ = setup
    if form.get("client_id") == "<geregistreerd>":
        form = {"client_id": register(env)["client_id"]}
    resp = env.client.post("/revoke", data=form)
    assert resp.status_code == status
    assert resp.json()["error"] == error
    assert resp.headers["cache-control"] == "no-store"


def test_revoke_cors_and_body_limit_match_sdk_token_route(setup: tuple[Env, Harness]) -> None:
    env, _ = setup
    preflight = {
        "Origin": "https://inspector.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "mcp-protocol-version",
    }
    ours = env.client.options("/revoke", headers=preflight)
    sdk = env.client.options("/token", headers=preflight)
    assert ours.status_code == sdk.status_code == 200
    for header in ("access-control-allow-origin", "access-control-allow-headers"):
        assert ours.headers[header] == sdk.headers[header]
    too_big = b"x" * (4 * 1024 * 1024 + 1)  # DEFAULT_MAX_REQUEST_BODY_SIZE + 1
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://a.example"}
    resp = env.client.post("/revoke", content=too_big, headers=headers)
    assert resp.status_code == 413
    assert resp.headers["access-control-allow-origin"] == "*"


def test_sdk_revoke_route_alone_still_rejects_public_clients(setup: tuple[Env, Harness]) -> None:
    """Zonder onze override geeft de SDK (mcp 2.3.0) een 400. Faalt deze test na een
    SDK-update, dan kan RevocationEndpoint mogelijk weg (python-sdk issue #3508)."""
    env, harness = setup
    client_id, tokens = full_login(env)
    server = build_server(harness.runner, auth=env.bundle)
    with TestClient(server.streamable_http_app(), base_url=PUBLIC_URL) as sdk_only:
        resp = sdk_only.post(
            "/revoke", data={"token": tokens["access_token"], "client_id": client_id}
        )
    assert resp.status_code == 400
    assert "client_secret" in resp.text
