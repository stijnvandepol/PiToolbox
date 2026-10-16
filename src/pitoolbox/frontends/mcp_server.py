"""MCP-frontend: ontsluit elke ToolSpec van de ToolRunner als MCP-tool (stdio).

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
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict

from pitoolbox.config import Settings
from pitoolbox.core.tools.base import Risk, ToolSpec
from pitoolbox.core.tools.runner import ToolRunner

log = logging.getLogger(__name__)

SERVER_NAME = "pitoolbox"

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
    "met een pending_id van een eerder gesprek. emergency_stop is altijd beschikbaar en "
    "breekt alle lopende acties af."
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


def _make_tool(runner: ToolRunner, spec: ToolSpec, frontend: str) -> Tool:
    async def call(**kwargs: Any) -> CallToolResult:
        # Zelfde thread-pool als anyio.to_thread: de event loop blijft vrij tijdens de call.
        result = await anyio.to_thread.run_sync(
            functools.partial(runner.call, frontend, spec.name, kwargs)
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


def build_server(runner: ToolRunner, frontend: str = "mcp") -> MCPServer:
    """Bouwt een MCPServer met één MCP-tool per `runner.specs()`-entry."""
    tools = [_make_tool(runner, spec, frontend) for spec in runner.specs()]
    return MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, tools=tools)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitoolbox-mcp", description="PiToolbox MCP-server (Flipper Zero + Wi-Fi)"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="transport (nu alleen stdio)",
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

    runner = build_runner(settings)
    server = build_server(runner)
    log.info("pitoolbox-mcp start: %d tools, transport=%s", len(runner.specs()), args.transport)
    try:
        server.run(args.transport)
    except KeyboardInterrupt:
        log.info("pitoolbox-mcp gestopt")
    finally:
        runner.shutdown()


if __name__ == "__main__":
    main()
