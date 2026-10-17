"""Tests voor de MCP-frontend, in-process via de SDK-client (geen devicemodules nodig)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
from mcp import Client
from mcp_types import CallToolResult
from pydantic import Field

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.policy import Policy
from pitoolbox.core.scope import Scope
from pitoolbox.core.tools.base import (
    Registry,
    Risk,
    ScopeCheck,
    ToolArgs,
    ToolContext,
    ToolSpec,
)
from pitoolbox.core.tools.runner import CONFIRM_SPEC, CONFIRM_TOOL, ToolRunner
from pitoolbox.frontends.mcp_server import INSTRUCTIONS, build_server

BSSID = "AA:BB:CC:DD:EE:01"


class EchoArgs(ToolArgs):
    text: str = Field(default="hoi", min_length=1, max_length=10, description="Te echoën tekst")
    n: int = Field(default=1, ge=1, le=5, description="Aantal keer")


class TargetArgs(ToolArgs):
    bssid: str = Field(default=BSSID, pattern=r"^[0-9A-F:]{17}$", description="Doel-BSSID")
    channel: int | None = Field(default=None, ge=1, le=196)
    tags: list[str] = Field(default_factory=list, max_length=3)


class NoArgs(ToolArgs):
    pass


def _check(args: TargetArgs, scope: Scope) -> ScopeCheck:
    if args.bssid in scope.bssids():
        return ScopeCheck(True)
    return ScopeCheck(False, f"{args.bssid} staat niet in scope.yaml")


class Harness:
    def __init__(
        self, tmp_path: Path, ctx: ToolContext, allowlist: list[str] | None = None
    ) -> None:
        self.calls: list[str] = []
        self.threads: dict[str, int] = {}
        self.release = threading.Event()
        self.audit_path = tmp_path / "audit.jsonl"

        def make(name: str) -> Any:
            def handler(ctx: ToolContext, args: Any) -> dict[str, Any]:
                self.calls.append(name)
                self.threads[name] = threading.get_ident()
                return {"tool": name, "args": args.model_dump()}

            return handler

        def slow(ctx: ToolContext, args: Any) -> dict[str, Any]:
            # Wacht tot de snelle tool draait; blokkeert de event loop, dan faalt dit op de timeout.
            return {"released": self.release.wait(timeout=10)}

        def fast(ctx: ToolContext, args: Any) -> dict[str, Any]:
            self.release.set()
            return {"fast": True}

        self.specs = [
            ToolSpec("read_tool", "lees iets", Risk.READ, EchoArgs, make("read_tool")),
            ToolSpec(
                "capture_tool", "neem op", Risk.CAPTURE, TargetArgs, make("capture_tool"), _check
            ),
            ToolSpec(
                "transmit_tool",
                "zend iets",
                Risk.TRANSMIT,
                TargetArgs,
                make("transmit_tool"),
                _check,
                summary=lambda a, _s: f"zend naar {a.bssid}",
            ),
            ToolSpec("control_tool", "beheer", Risk.CONTROL, NoArgs, make("control_tool")),
            ToolSpec("slow_tool", "traag", Risk.READ, NoArgs, slow),
            ToolSpec("fast_tool", "snel", Risk.READ, NoArgs, fast),
            CONFIRM_SPEC,
        ]
        self.runner = ToolRunner(
            Registry(self.specs),
            ctx,
            Policy(ctx.scope_store, allowlist),
            AuditLog(self.audit_path),
        )

    def audit(self) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]


@pytest.fixture
def harness(tmp_path: Path, make_ctx: Any) -> Harness:
    scope = Scope.model_validate({"wifi": [{"bssid": BSSID, "channel": 6}]})
    return Harness(tmp_path, make_ctx(tmp_path, scope))


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert isinstance(result.structured_content, dict)
    return result.structured_content


# --- tools/list ---


async def test_list_tools_matches_specs(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        listed = (await client.list_tools()).tools
    by_name = {t.name: t for t in listed}
    assert list(by_name) == [s.name for s in harness.specs]  # volgorde en volledigheid
    for spec in harness.specs:
        tool = by_name[spec.name]
        assert tool.description == spec.full_description()
        assert "Risicoklasse" in (tool.description or "")


async def test_input_schemas_are_identical_to_spec_models(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        listed = (await client.list_tools()).tools
    for tool in listed:
        spec = next(s for s in harness.specs if s.name == tool.name)
        expected = spec.params.model_json_schema()
        assert tool.input_schema == expected
        # Expliciet de onderdelen die het contract noemt:
        assert tool.input_schema["properties"] == expected["properties"]
        assert tool.input_schema.get("required") == expected.get("required")
        assert tool.input_schema["additionalProperties"] is False


async def test_schema_keeps_constraints_defaults_and_descriptions(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    props = tools["read_tool"].input_schema["properties"]
    assert props["text"]["minLength"] == 1 and props["text"]["maxLength"] == 10
    assert props["text"]["default"] == "hoi"
    assert props["text"]["description"] == "Te echoën tekst"
    assert props["n"]["minimum"] == 1 and props["n"]["maximum"] == 5
    target = tools["capture_tool"].input_schema["properties"]
    assert target["bssid"]["pattern"] == r"^[0-9A-F:]{17}$"
    assert target["tags"]["maxItems"] == 3


async def test_annotations_per_risk(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    read = tools["read_tool"].annotations
    assert read is not None and read.read_only_hint is True and read.destructive_hint is False
    assert read.open_world_hint is False
    capture = tools["capture_tool"].annotations
    assert capture is not None and capture.read_only_hint is False
    assert capture.open_world_hint is True and capture.destructive_hint is False
    transmit = tools["transmit_tool"].annotations
    assert transmit is not None and transmit.read_only_hint is False
    assert transmit.destructive_hint is True and transmit.open_world_hint is True
    for name in ("control_tool", CONFIRM_TOOL):
        ann = tools[name].annotations
        assert ann is not None and ann.read_only_hint is False
        assert ann.destructive_hint is False and ann.open_world_hint is False


def test_server_instructions_are_dutch_and_mention_confirmation(harness: Harness) -> None:
    server = build_server(harness.runner)
    assert server.instructions == INSTRUCTIONS
    for word in ("pending_id", "confirm_action", "emergency_stop", "transmit", "expliciet"):
        assert word in INSTRUCTIONS


def test_empty_runner_gives_server_without_tools() -> None:
    class Empty:
        def allowed_specs(self) -> list[ToolSpec]:
            return []

    server = build_server(Empty())  # type: ignore[arg-type]
    assert server.name == "pitoolbox"


# --- tools/call ---


async def test_call_forwards_args_and_returns_dict(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("read_tool", {"text": "yo", "n": 3})
    assert result.is_error is False
    expected = {"ok": True, "result": {"tool": "read_tool", "args": {"text": "yo", "n": 3}}}
    assert _structured(result) == expected
    # Leesbare tekst bevat dezelfde JSON.
    assert json.loads(result.content[0].text) == expected  # type: ignore[union-attr]
    (rec,) = harness.audit()
    assert rec["frontend"] == "mcp" and rec["tool"] == "read_tool" and rec["decision"] == "allow"


async def test_call_without_arguments_uses_defaults(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("read_tool")
    assert _structured(result)["result"]["args"] == {"text": "hoi", "n": 1}


async def test_custom_frontend_name_is_used(harness: Harness) -> None:
    async with Client(build_server(harness.runner, frontend="test-fe")) as client:
        await client.call_tool("read_tool", {})
    assert harness.audit()[0]["frontend"] == "test-fe"


@pytest.mark.parametrize(
    "args",
    [
        {"extra": 1},  # onbekend veld: mag niet stilletjes verdwijnen
        {"text": ""},
        {"text": "x" * 11},
        {"n": 6},
        {"n": "veel"},
    ],
)
async def test_invalid_args_are_rejected(harness: Harness, args: dict[str, Any]) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("read_tool", args)
    assert result.is_error is True
    body = _structured(result)
    assert body["ok"] is False and body["error"] == "invalid_arguments"
    assert body["details"]
    assert harness.calls == []
    # Ook afgewezen calls komen (één keer) in de audit-log.
    assert [r["decision"] for r in harness.audit()] == ["invalid"]


async def test_extra_field_is_not_silently_dropped_on_transmit(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("transmit_tool", {"bssid": BSSID, "force": True})
    assert _structured(result)["error"] == "invalid_arguments"
    assert harness.runner.ctx.pending.list() == []


async def test_json_encoded_string_is_not_coerced(harness: Harness) -> None:
    """De SDK parst normaal JSON-strings voor niet-str velden; hier niet: strikt blijft strikt."""
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("capture_tool", {"tags": '["a"]'})
    assert _structured(result)["error"] == "invalid_arguments"
    assert harness.calls == []


async def test_unknown_tool_is_an_error(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("bestaat_niet", {})
    assert result.is_error is True
    assert harness.calls == []
    # Ook een onbekende tool komt in de audit-log (de runner weigert, niet de SDK).
    assert [(r["tool"], r["decision"]) for r in harness.audit()] == [("bestaat_niet", "deny")]


async def test_out_of_scope_is_denied(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("capture_tool", {"bssid": "11:22:33:44:55:66"})
    assert result.is_error is True
    assert _structured(result)["error"] == "denied"
    assert harness.calls == []


async def test_handler_runs_in_worker_thread(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        await client.call_tool("read_tool", {})
    assert harness.threads["read_tool"] != threading.get_ident()


async def test_runner_crash_does_not_leak_details() -> None:
    class Crashing:
        def allowed_specs(self) -> list[ToolSpec]:
            return [ToolSpec("t", "d", Risk.READ, NoArgs, lambda c, a: {})]

        def call(self, frontend: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("geheim intern detail")

    async with Client(build_server(Crashing())) as client:  # type: ignore[arg-type]
        result = await client.call_tool("t", {})
    assert result.is_error is True
    assert "geheim" not in json.dumps(result.model_dump(mode="json"))


async def test_slow_tool_does_not_block_other_calls(harness: Harness) -> None:
    order: list[str] = []
    results: dict[str, CallToolResult] = {}

    async with Client(build_server(harness.runner)) as client:

        async def run(name: str) -> None:
            results[name] = await client.call_tool(name, {})
            order.append(name)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run, "slow_tool")
            await anyio.sleep(0.2)  # slow_tool zit nu in zijn worker-thread te wachten
            tg.start_soon(run, "fast_tool")

    assert order == ["fast_tool", "slow_tool"]
    assert _structured(results["fast_tool"])["result"] == {"fast": True}
    # slow_tool werd pas vrijgegeven doordat fast_tool tegelijk kon draaien.
    assert _structured(results["slow_tool"])["result"] == {"released": True}


# --- transmit -> confirm_action ---


async def test_transmit_confirm_flow_through_mcp(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        first = await client.call_tool("transmit_tool", {"bssid": BSSID})
        body = _structured(first)
        # Een bevestigingsvraag is een normale uitkomst, geen toolfout.
        assert first.is_error is False
        assert body["error"] == "confirmation_required"
        assert body["summary"] == f"zend naar {BSSID}"
        assert body["pending_id"]
        assert harness.calls == []

        confirmed = await client.call_tool(CONFIRM_TOOL, {"pending_id": body["pending_id"]})
        assert confirmed.is_error is False
        assert _structured(confirmed)["result"]["tool"] == "transmit_tool"
        assert harness.calls == ["transmit_tool"]

        again = await client.call_tool(CONFIRM_TOOL, {"pending_id": body["pending_id"]})
        assert again.is_error is True
        assert _structured(again)["error"] == "unknown_pending"
        assert harness.calls == ["transmit_tool"]

    assert [r["decision"] for r in harness.audit()] == ["confirm", "confirmed", "deny"]


async def test_transmit_out_of_scope_never_reaches_confirmation(harness: Harness) -> None:
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("transmit_tool", {"bssid": "11:22:33:44:55:66"})
    assert result.is_error is True
    assert _structured(result)["error"] == "denied"
    assert "pending_id" not in _structured(result)
    assert harness.runner.ctx.pending.list() == []


# --- PITOOLBOX_TOOL_ALLOWLIST ---


def _allowlisted(tmp_path: Path, make_ctx: Any, allowlist: list[str]) -> Harness:
    scope = Scope.model_validate({"wifi": [{"bssid": BSSID, "channel": 6}]})
    return Harness(tmp_path, make_ctx(tmp_path, scope), allowlist)


async def test_list_tools_follows_allowlist(tmp_path: Path, make_ctx: Any) -> None:
    harness = _allowlisted(tmp_path, make_ctx, ["read_tool"])
    async with Client(build_server(harness.runner)) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    # confirm_action handelt de runner af vóór de allowlist, dus die blijft (zoals altijd) staan.
    assert names == ["read_tool", CONFIRM_TOOL]


async def test_unlisted_tool_cannot_be_called(tmp_path: Path, make_ctx: Any) -> None:
    harness = _allowlisted(tmp_path, make_ctx, ["read_tool"])
    async with Client(build_server(harness.runner)) as client:
        result = await client.call_tool("control_tool", {})
    assert result.is_error is True
    assert harness.calls == []
    # Review 2026-10-17: de weigering moet in de audit-log staan, ook al staat de tool niet
    # in tools/list.
    assert [(r["tool"], r["decision"]) for r in harness.audit()] == [("control_tool", "deny")]


async def test_confirm_flow_with_allowlist_without_confirm_action(
    tmp_path: Path, make_ctx: Any
) -> None:
    harness = _allowlisted(tmp_path, make_ctx, ["transmit_tool"])
    async with Client(build_server(harness.runner)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"transmit_tool", CONFIRM_TOOL}
        pending = _structured(await client.call_tool("transmit_tool", {"bssid": BSSID}))
        assert pending["error"] == "confirmation_required"
        confirmed = await client.call_tool(CONFIRM_TOOL, {"pending_id": pending["pending_id"]})
    assert _structured(confirmed)["ok"] is True
    assert harness.calls == ["transmit_tool"]


def test_main_starts_and_stops_flipper_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """PITOOLBOX_FLIPPER_UI=on: de bridge draait in het MCP-proces en stopt vóór shutdown."""
    import pitoolbox.app
    from pitoolbox.frontends import mcp_server
    from pitoolbox.frontends.flipper_ui import bridge as bridge_mod

    order: list[str] = []
    runner = MagicMock()
    runner.specs.return_value = []
    runner.shutdown.side_effect = lambda: order.append("shutdown")

    class FakeBridge:
        def stop(self) -> None:
            order.append("bridge.stop")

    def fake_start(r: Any, settings: Any) -> Any:
        order.append(f"bridge.start:{settings.flipper_ui}")
        return FakeBridge()

    server = MagicMock()
    server.run.side_effect = lambda _t: order.append("serve")
    monkeypatch.setenv("PITOOLBOX_FLIPPER_UI", "on")
    monkeypatch.setattr(pitoolbox.app, "build_runner", lambda _s: runner)
    monkeypatch.setattr(bridge_mod, "start_from_settings", fake_start)
    monkeypatch.setattr(mcp_server, "build_server", lambda _r: server)
    mcp_server.main([])
    assert order == ["bridge.start:on", "serve", "bridge.stop", "shutdown"]
