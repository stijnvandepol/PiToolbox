"""Integratie: build_http_app (onze productiewiring) + echte OAuth + ToolRunner.

Controleert wat alleen in de samengestelde app zichtbaar is: 401 met resource_metadata,
Host-header-check, en dat een transmit-bevestiging gebonden is aan de OAuth-client.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_auth_flow import Env, complete_login, exchange_code, register, start_authorize
from test_auth_support import PASSWORD, PUBLIC_URL, FakeClock, make_settings, pkce_pair
from test_mcp_server import BSSID, Harness

from pitoolbox.auth import build_auth
from pitoolbox.auth.crypto import generate_totp_secret, hash_password
from pitoolbox.core.scope import Scope
from pitoolbox.frontends.mcp_server import (
    _require_loopback,
    build_http_app,
    transport_security_for,
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


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_allowed(host: str) -> None:
    _require_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "toolbox.example.com"])  # noqa: S104 - test van de weigering
def test_non_loopback_hosts_refused(host: str) -> None:
    with pytest.raises(SystemExit):
        _require_loopback(host)
