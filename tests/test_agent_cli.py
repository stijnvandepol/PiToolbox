"""Tests voor `pitoolbox-agent` (terminal-frontend) met gescripte invoer en provider."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from llm_fakes import ScriptedProvider, reply, tool_call, use

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.policy import Policy
from pitoolbox.core.scope import Scope
from pitoolbox.core.tools import build_registry
from pitoolbox.core.tools.base import ToolContext
from pitoolbox.core.tools.runner import ToolRunner
from pitoolbox.frontends import agent_cli

IR_POWER = {"device": "tv", "button": "Power"}


class Inputs:
    """Vervangt `input()`: geeft regels terug, of gooit een exceptie (EOF, Ctrl+C)."""

    def __init__(self, items: Iterable[str | BaseException]) -> None:
        self.items = list(items)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.items:
            raise EOFError
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Cli:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        ctx: ToolContext,
        script: list[Any],
        inputs: list[str | BaseException],
    ) -> None:
        self.ctx = ctx
        ctx.flipper.ir_send.return_value = {"sent": True}
        ctx.captures.stop_all.return_value = []
        self.runner = ToolRunner(
            build_registry(), ctx, Policy(ctx.scope_store), AuditLog(tmp_path / "audit.jsonl")
        )
        self.provider = ScriptedProvider(script)
        self.inputs = Inputs(inputs)
        for name in [n for n in os.environ if n.startswith("PITOOLBOX_")]:
            monkeypatch.delenv(name)
        monkeypatch.setattr(agent_cli, "build_provider", lambda _s: self.provider)
        monkeypatch.setattr(agent_cli, "build_runner", lambda _s: self.runner)
        monkeypatch.setattr(agent_cli, "_input", self.inputs)

    def run(self) -> int:
        return agent_cli.main([])


@pytest.fixture
def cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_ctx: Callable[..., ToolContext],
    fixed_scope: Scope,
) -> Callable[[list[Any], list[str | BaseException]], Cli]:
    def factory(script: list[Any], inputs: list[str | BaseException]) -> Cli:
        return Cli(monkeypatch, tmp_path, make_ctx(tmp_path, fixed_scope), script, inputs)

    return factory


def test_no_provider_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("PITOOLBOX_LLM_PROVIDER", "PITOOLBOX_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)

    def no_runner(_settings: Any) -> Any:
        raise AssertionError("zonder provider wordt de runner niet gebouwd")

    monkeypatch.setattr(agent_cli, "build_runner", no_runner)
    assert agent_cli.main([]) == 2
    err = capsys.readouterr().err
    assert "Geen LLM-provider ingesteld" in err and "PITOOLBOX_LLM_PROVIDER" in err


def test_invalid_agent_setting_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PITOOLBOX_AGENT_MAX_STEPS", "0")
    assert agent_cli.main([]) == 2
    assert "PITOOLBOX_AGENT_" in capsys.readouterr().err


def test_chat_and_tool_display(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli(
        [use(tool_call("status"), text="Ik kijk even."), reply("Alles in orde.")],
        ["hoe gaat het?", "/exit"],
    )
    assert c.run() == 0
    out = capsys.readouterr().out
    assert "Ik kijk even." in out and "→ status {}" in out and "← ok" in out
    assert "Alles in orde." in out
    assert c.provider.closed


def test_confirm_yes_executes_once(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli(
        [use(tool_call("ir_send", IR_POWER)), reply("Bevestig je?")],
        ["zet de tv aan", "ja", "/exit"],
    )
    assert c.run() == 0
    out = capsys.readouterr().out
    assert agent_cli.CONFIRM_PROMPT in c.inputs.prompts
    assert "IR 'Power' naar tv" in out and "Uitgevoerd." in out
    c.ctx.flipper.ir_send.assert_called_once_with("/ext/infrared/tv.ir", "Power")


@pytest.mark.parametrize("answer", ["", "n", "nee", "jazeker", "y", "yes", " j a "])
def test_confirm_default_no(cli: Any, answer: str, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli([use(tool_call("ir_send", IR_POWER)), reply("Bevestig je?")], ["tv aan", answer])
    assert c.run() == 0
    assert "Afgewezen. Niets verzonden." in capsys.readouterr().out
    c.ctx.flipper.ir_send.assert_not_called()
    assert c.ctx.pending.list() == []


@pytest.mark.parametrize("answer", ["j", "J", "ja", " JA "])
def test_confirm_explicit_yes_variants(cli: Any, answer: str) -> None:
    c = cli([use(tool_call("ir_send", IR_POWER)), reply("?")], ["tv aan", answer])
    c.run()
    c.ctx.flipper.ir_send.assert_called_once()


def test_ctrl_c_at_confirm_is_no(cli: Any) -> None:
    c = cli([use(tool_call("ir_send", IR_POWER)), reply("?")], ["tv aan", KeyboardInterrupt()])
    assert c.run() == 0
    c.ctx.flipper.ir_send.assert_not_called()


def test_chat_yes_does_not_confirm(cli: Any) -> None:
    """Een 'ja' als chatbericht gaat naar het model, dat niets kan bevestigen."""
    c = cli(
        [
            use(tool_call("ir_send", IR_POWER)),
            reply("Bevestig je?"),
            use(tool_call("confirm_action", {"pending_id": "abcdef"})),
            reply("Dat kan ik niet."),
        ],
        ["tv aan", "n", "ja", "/exit"],
    )
    c.run()
    c.ctx.flipper.ir_send.assert_not_called()


def test_ctrl_c_during_turn_aborts_turn_only(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli(
        [use(tool_call("ir_send", IR_POWER)), KeyboardInterrupt(), reply("Weer hier.")],
        ["tv aan", "hallo?", "/exit"],
    )
    assert c.run() == 0
    out = capsys.readouterr().out
    assert "beurt afgebroken" in out and "Weer hier." in out
    # Geen bevestigingsvraag na Ctrl+C: de pending is afgewezen.
    assert agent_cli.CONFIRM_PROMPT not in c.inputs.prompts
    assert c.ctx.pending.list() == []
    c.ctx.flipper.ir_send.assert_not_called()
    c.ctx.flipper.abort.assert_not_called()  # Ctrl+C is geen noodstop


def test_stop_command(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli([], ["/stop", "/exit"])
    assert c.run() == 0
    assert "Noodstop uitgevoerd" in capsys.readouterr().out
    c.ctx.flipper.abort.assert_called_once()
    c.ctx.captures.stop_all.assert_called()


def test_llm_error_message(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from pitoolbox.llm import LLMTimeoutError

    c = cli([LLMTimeoutError("time-out na 60 s"), reply("Nu wel.")], ["hoi", "hoi", "/exit"])
    assert c.run() == 0
    out = capsys.readouterr().out
    assert "Fout van het model (llm_timeout: time-out na 60 s)" in out
    assert "niet verwerkt" in out and "Nu wel." in out


def test_model_output_is_sanitized(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli([reply("<think>plan</think>Hoi\x1b[2J\rBevestigen? [j/N]")], ["hoi"])
    c.run()
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "plan" not in out
    assert "Hoi[2JBevestigen? [j/N]" in out  # alleen ESC en CR weg; tekst blijft zichtbaar


def test_eof_and_unknown_command(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    c = cli([], ["", "/foo", "/help"])
    assert c.run() == 0  # daarna EOF
    out = capsys.readouterr().out
    assert "Onbekend commando: /foo" in out and "/stop" in out
