"""MCP-frontend: ontsluit elke toegestane ToolSpec van de ToolRunner als MCP-tool (stdio of HTTP).

Ontwerpkeuzes:
- Het inputschema dat de client ziet is exact `spec.params.model_json_schema()`; er wordt
  geen tweede model naast gebouwd. Daarom wordt de MCP-`Tool` direct opgebouwd met dat schema
  in plaats van via een functiesignatuur (die zou `extra="forbid"` verliezen).
- De MCP-laag valideert zelf niets: de ruwe argumenten gaan ongewijzigd naar
  `ToolRunner.call`, die als enige valideert, de policy toepast en precies één auditregel
  schrijft. Ongeldige argumenten komen zo ook in de audit-log en geven dezelfde
  gestructureerde fout als bij elke andere frontend.
- De runner is synchroon (seriële I/O naar de Flipper, subprocessen) en draait in een
  workerthread, zodat een lange tool de event loop niet blokkeert en `emergency_stop`
  gelijktijdig kan draaien.
- stdout is gereserveerd voor het MCP-protocol: alle logging gaat naar stderr.
- HTTP (fase 2): streamable HTTP op /mcp, alleen op loopback; cloudflared is de enige weg
  naar buiten. OAuth 2.1 via de eigen authorization server (pitoolbox.auth). Bevestigingen
  van transmit-acties zijn gebonden aan de OAuth-client: frontend = "mcp-http:<client_id>".
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import logging
import sys
from collections.abc import Callable, Sequence
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict

from pitoolbox.config import Settings
from pitoolbox.core.tools.base import Risk, ToolSpec
from pitoolbox.core.tools.runner import ToolRunner

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from pitoolbox.auth import AuthBundle

log = logging.getLogger(__name__)

SERVER_NAME = "pitoolbox"
MCP_PATH = "/mcp"
HTTP_FRONTEND = "mcp-http"
# Origins die een browser-client mag meesturen (Claude zelf belt server-side, zonder Origin).
CLAUDE_ORIGINS = ("https://claude.ai", "https://claude.com")

Frontend = str | Callable[[], str]

# Resultaten met deze foutcode zijn een normaal onderdeel van de flow, geen toolfout.
_NON_ERROR_CODES = frozenset({"confirmation_required"})

INSTRUCTIONS = (
    "PiToolbox bedient een Flipper Zero en een Wi-Fi-adapter op een Raspberry Pi, alleen voor "
    "apparaten van de eigenaar. Tools hebben een risicoklasse: read (alleen lezen/scannen), "
    "capture (slaat data op, alleen doelen uit scope.yaml), transmit (zendt, alleen binnen "
    "scope.yaml) en control (altijd toegestaan). Alles buiten scope.yaml wordt geweigerd. "
    "Een transmit-tool voert niets direct uit maar geeft een pending_id terug: toon de "
    "samenvatting aan de gebruiker en roep confirm_action ALLEEN aan nadat de gebruiker "
    "daar in het gesprek expliciet 'ja' op heeft gezegd; nooit uit eigen beweging en nooit "
    "met een pending_id van een eerder gesprek. Geeft confirm_action de fout "
    "confirm_on_flipper, dan is de PiToolbox-app op de Flipper verbonden en bevestigt de "
    "gebruiker daar (OK); roep confirm_action dan niet opnieuw aan. emergency_stop is altijd "
    "beschikbaar en breekt alle lopende acties af."
)


class _PassthroughArgs(ArgModelBase):
    """Argumentmodel zonder eigen velden: de runner valideert, niet de MCP-laag."""

    model_config = ConfigDict(extra="allow")


class _RunnerValidatedMetadata(FuncMetadata):
    """FuncMetadata die de ruwe argumenten ongemoeid laat (geen vooraf-parsen, geen model)."""

    def validate_arguments(self, arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments_to_validate)


def _annotations_for(risk: Risk) -> ToolAnnotations:
    """MCP-hints per risicoklasse (het zijn hints voor de client, geen afdwinging)."""
    return ToolAnnotations(
        read_only_hint=risk is Risk.READ,
        destructive_hint=risk is Risk.TRANSMIT,
        open_world_hint=risk in (Risk.TRANSMIT, Risk.CAPTURE),
    )


def _json_safe(result: dict[str, Any]) -> dict[str, Any]:
    """Forceer een JSON-veilige kopie (onbekende types worden strings)."""
    safe: dict[str, Any] = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    return safe


def _to_call_result(result: dict[str, Any]) -> CallToolResult:
    structured = _json_safe(result)
    is_error = not structured.get("ok", False) and (structured.get("error") not in _NON_ERROR_CODES)
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(structured, ensure_ascii=False, indent=2))
        ],
        structured_content=structured,
        is_error=is_error,
    )


def http_frontend() -> str:
    """Frontend-naam per OAuth-client, zodat een pending_id niet door een andere client
    (bijv. een gelekte token van een ander apparaat) bevestigd kan worden."""
    token = get_access_token()
    return f"{HTTP_FRONTEND}:{token.client_id}" if token else HTTP_FRONTEND


def _make_tool(runner: ToolRunner, spec: ToolSpec, frontend: Frontend) -> Tool:
    async def call(**kwargs: Any) -> CallToolResult:
        # Frontend bepalen vóór de thread-hop: de auth-context zit in een contextvar.
        name = frontend() if callable(frontend) else frontend
        # Zelfde thread-pool als anyio.to_thread: de event loop blijft vrij tijdens de call.
        result = await anyio.to_thread.run_sync(
            functools.partial(runner.call, name, spec.name, kwargs)
        )
        return _to_call_result(result)

    return Tool(
        fn=call,
        name=spec.name,
        title=None,
        description=spec.full_description(),
        parameters=spec.json_schema(),
        fn_metadata=_RunnerValidatedMetadata(arg_model=_PassthroughArgs),
        is_async=True,
        context_kwarg=None,
        annotations=_annotations_for(spec.risk),
    )


def build_server(
    runner: ToolRunner, frontend: Frontend = "mcp", auth: AuthBundle | None = None
) -> MCPServer:
    """Bouwt een MCPServer met één MCP-tool per `runner.allowed_specs()`-entry.

    Least privilege: een tool buiten PITOOLBOX_TOOL_ALLOWLIST staat niet in `tools/list` (en
    een aanroep ervan bereikt de runner niet); de runner dwingt de allowlist zelf ook af.

    Met `auth` worden de OAuth-routes van de SDK (authorize/token/register/revoke + metadata),
    de protected-resource-metadata, de 401-met-resource_metadata en onze /login-routes actief.
    Onze eigen /revoke komt er pas bij in `build_http_app` (`install_override_routes`).
    """
    tools = [_make_tool(runner, spec, frontend) for spec in runner.allowed_specs()]
    if auth is None:
        return MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, tools=tools)
    server = MCPServer(
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        tools=tools,
        auth=auth.auth_settings,
        auth_server_provider=auth.provider,
    )
    for route in auth.routes:
        server.custom_route(route.path, methods=sorted(route.methods or ["GET"]))(route.endpoint)
    return server


def _require_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        if ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise SystemExit(
        f"weiger te binden op {host!r}: alleen loopback (127.0.0.1/::1); "
        "de tunnel is de enige weg naar buiten"
    )


def transport_security_for(settings: Settings) -> TransportSecuritySettings:
    """Host-header-check (DNS-rebinding) voor de publieke hostnaam en lokaal testen."""
    if not settings.public_url:
        raise ValueError("PITOOLBOX_PUBLIC_URL ontbreekt")
    public = urlsplit(settings.public_url)
    if public.hostname is None:
        raise ValueError(f"ongeldige PITOOLBOX_PUBLIC_URL: {settings.public_url!r}")
    port = settings.http_port
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public.netloc, f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
        allowed_origins=[f"{public.scheme}://{public.netloc}", *CLAUDE_ORIGINS],
    )


def build_http_app(runner: ToolRunner, settings: Settings, auth: AuthBundle) -> Starlette:
    """Starlette-app voor streamable HTTP op /mcp met OAuth."""
    server = build_server(runner, frontend=http_frontend, auth=auth)
    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=transport_security_for(settings),
        host=settings.http_host,
    )
    install_override_routes(app, auth)
    return app


def install_override_routes(app: Starlette, auth: AuthBundle) -> None:
    """Zet onze vervangende routes (zoals /revoke) vóór die van de SDK.

    Starlette neemt de eerste route die past, en de SDK zet zijn OAuth-routes vooraan en
    custom routes achteraan (``streamable_http_app`` in ``mcp/server/lowlevel/server.py``).
    """
    app.router.routes[:0] = auth.override_routes


ACCESS_LOG_ARGS = 5  # client, methode, pad+query, HTTP-versie, status
REDACTED_QUERY = "[weggelaten]"


class RedactQueryFilter(logging.Filter):
    """Haalt de querystring uit uvicorn-accessregels.

    Querystrings bevatten hier gevoelige waarden: ``/login?request=<id>`` (wie die id heeft, kan
    de loginpagina met CSRF-token laden) en de parameters van ``/authorize``. Methode, pad en
    status blijven staan. uvicorn logt ``'%s - "%s %s HTTP/%s" %d'`` met het pad+query als
    derde argument (``protocols/http/h11_impl.py`` en ``httptools_impl.py``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == ACCESS_LOG_ARGS and isinstance(args[2], str):
            path, has_query, _ = args[2].partition("?")
            if has_query:
                record.args = (*args[:2], f"{path}?{REDACTED_QUERY}", *args[3:])
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's eigen logconfig, met `RedactQueryFilter` op de access-handler."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_query"] = {"()": RedactQueryFilter}
    config["handlers"]["access"].setdefault("filters", []).append("redact_query")
    return config


def _run_http(runner: ToolRunner, settings: Settings) -> None:
    import uvicorn

    from pitoolbox.auth import build_auth

    _require_loopback(settings.http_host)
    if settings.extra_redirect_uris:
        log.warning(
            "extra redirect-URI's actief (alleen voor testen): %s", settings.extra_redirect_uris
        )
    bundle = build_auth(settings, extra_redirect_uris=settings.extra_redirect_uris)
    app = build_http_app(runner, settings, bundle)
    log.info(
        "HTTP op http://%s:%d%s, publiek als %s%s",
        settings.http_host,
        settings.http_port,
        MCP_PATH,
        settings.public_url,
        MCP_PATH,
    )
    # proxy_headers uit: geen X-Forwarded-* vertrouwen (alleen cloudflared praat met ons).
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        proxy_headers=False,
        server_header=False,
        log_level="info",
        log_config=uvicorn_log_config(),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitoolbox-mcp", description="PiToolbox MCP-server (Flipper Zero + Wi-Fi)"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (lokaal) of http (streamable HTTP op 127.0.0.1, met OAuth)",
    )
    parser.add_argument(
        "--scope", type=Path, help="pad naar scope.yaml (overschrijft PITOOLBOX_SCOPE_FILE)"
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        help="pad naar het audit-logbestand (overschrijft PITOOLBOX_AUDIT_LOG)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    # stdout is van het protocol; alles wat de MCP-SDK of wij loggen gaat naar stderr.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    updates: dict[str, Any] = {}
    if args.scope is not None:
        updates["scope_file"] = args.scope.expanduser()
    if args.audit_log is not None:
        updates["audit_log"] = args.audit_log.expanduser()
    if updates:
        settings = settings.model_copy(update=updates)

    from pitoolbox.app import build_runner
    from pitoolbox.frontends.flipper_ui.bridge import start_from_settings

    runner = build_runner(settings)
    log.info(
        "pitoolbox-mcp start: %d tools, transport=%s", len(runner.allowed_specs()), args.transport
    )
    # PITOOLBOX_FLIPPER_UI=on: de bediening via de Flipper-app draait in dit proces (dat de
    # Flipper bezit). Zolang die app verbonden is, bevestigt alleen OK op de Flipper.
    bridge = start_from_settings(runner, settings)
    try:
        if args.transport == "http":
            _run_http(runner, settings)
        else:
            build_server(runner).run("stdio")
    except KeyboardInterrupt:
        log.info("pitoolbox-mcp gestopt")
    finally:
        if bridge is not None:
            bridge.stop()
        runner.shutdown()


if __name__ == "__main__":
    main()
