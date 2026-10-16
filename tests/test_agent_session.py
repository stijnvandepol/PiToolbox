"""Tests voor de agent-loop met een gescripte provider en een echte ToolRunner.

De runner gebruikt de echte registry (alle tools) met MagicMock-devices uit conftest, dus
policy, scope, PendingStore en audit zijn echt. Geen netwerk, geen hardware.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from llm_fakes import (
    ScriptedProvider,
    assert_valid_history,
    broken_call,
    reply,
    tool_call,
    use,
)

from pitoolbox.agent import (
    MODEL_HIDDEN_TOOLS,
    NOTE_PREFIX,
    SYSTEM_PROMPT,
    AgentEvent,
    AgentLimits,
    AgentSession,
    AssistantText,
    ConfirmStatus,
    PendingConfirmation,
    ToolCallFinished,
    ToolCallStarted,
    TurnStop,
    for_terminal,
    strip_think,
)
from pitoolbox.config import Settings
from pitoolbox.core.audit import AuditLog
from pitoolbox.core.policy import Policy
from pitoolbox.core.scope import Scope
from pitoolbox.core.tools import build_registry
from pitoolbox.core.tools.base import ToolContext
from pitoolbox.core.tools.runner import CONFIRM_TOOL, ToolRunner
from pitoolbox.llm import (
    AssistantMessage,
    LLMServerError,
    Message,
    ProviderState,
    StopReason,
    ToolResult,
    UserMessage,
)

FRONTEND = "agent-cli"
IR_POWER = {"device": "tv", "button": "Power"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Lab:
    """Runner met echte registry/policy/audit en nep-devices, plus een sessie erbovenop."""

    def __init__(
        self,
        tmp_path: Path,
        ctx: ToolContext,
        script: Sequence[Any] = (),
        limits: AgentLimits | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.ctx = ctx
        self.audit_path = tmp_path / "audit.jsonl"
        self.runner = ToolRunner(
            build_registry(), ctx, Policy(ctx.scope_store), AuditLog(self.audit_path)
        )
        self.provider = ScriptedProvider(script)
        self.clock = clock or Clock()
        self.session = AgentSession(
            self.provider, self.runner, frontend=FRONTEND, limits=limits, clock=self.clock
        )
        self.events: list[AgentEvent] = []
        ctx.flipper.ir_send.return_value = {"sent": True}
        ctx.flipper.storage_list.return_value = [{"name": "tv.ir", "type": "file"}]
        ctx.captures.stop_all.return_value = []

    def turn(self, text: str) -> Any:
        result = self.session.run_turn(text, on_event=self.events.append)
        if self.session.history:  # leeg kan: een teruggedraaide eerste beurt
            assert_valid_history(self.session.history)
        return result

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]

    def tool_audit(self) -> list[dict[str, Any]]:
        return [r for r in self.audit() if "event" not in r]

    def results(self) -> list[ToolResult]:
        return [m for m in self.session.history if isinstance(m, ToolResult)]


@pytest.fixture
def lab(tmp_path: Path, make_ctx: Callable[..., ToolContext], fixed_scope: Scope) -> Lab:
    return Lab(tmp_path, make_ctx(tmp_path, fixed_scope))


def _content(result: ToolResult) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.content)
    return data


# --- toollijst en prompt ---------------------------------------------------------------


def test_model_never_sees_confirm_action(lab: Lab) -> None:
    names = {t.name for t in lab.session.tools}
    assert CONFIRM_TOOL in MODEL_HIDDEN_TOOLS
    assert CONFIRM_TOOL not in names
    # emergency_stop mag wel: hij kan alleen stoppen, nooit zenden.
    assert "emergency_stop" in names
    assert names == {s.name for s in lab.runner.specs()} - {CONFIRM_TOOL}
    ir = next(t for t in lab.session.tools if t.name == "ir_send")
    assert "confirm_action" not in ir.description
    assert "alleen de gebruiker" in ir.description
    # Schema is exact dat van de ToolSpec (maar een kopie).
    spec = next(s for s in lab.runner.specs() if s.name == "ir_send")
    assert dict(ir.parameters) == spec.json_schema()


def test_model_does_not_see_tools_outside_allowlist(
    tmp_path: Path, make_ctx: Callable[..., ToolContext], fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    runner = ToolRunner(
        build_registry(),
        ctx,
        Policy(ctx.scope_store, allowlist=["status", "ir_send", "confirm_action"]),
        AuditLog(tmp_path / "audit.jsonl"),
    )
    session = AgentSession(ScriptedProvider(()), runner, frontend=FRONTEND)
    # confirm_action blijft verborgen, ook als hij in de allowlist staat.
    assert {t.name for t in session.tools} == {"status", "ir_send"}


def test_system_prompt_and_tools_are_sent(lab: Lab) -> None:
    lab.provider.add(reply("Hallo."))
    result = lab.turn("hoi")
    assert result.stop is TurnStop.END_TURN
    assert result.text == "Hallo."
    request = lab.provider.requests[0]
    assert request.system == SYSTEM_PROMPT
    assert {t.name for t in request.tools} == {t.name for t in lab.session.tools}
    assert "pending_id" in SYSTEM_PROMPT and NOTE_PREFIX in SYSTEM_PROMPT


# --- loop ------------------------------------------------------------------------------


def test_multi_round_tool_use(lab: Lab) -> None:
    lab.provider.add(
        use(tool_call("status"), text="Even kijken."),
        use(tool_call("storage_list", {"path": "/ext/infrared"})),
        reply("Er staat één bestand: tv.ir."),
    )
    result = lab.turn("wat staat er in /ext/infrared?")

    assert result.stop is TurnStop.END_TURN
    assert (result.steps, result.tool_calls, result.executed) == (3, 2, 2)
    assert result.usage.input_tokens == 50 and result.usage.output_tokens == 19
    lab.ctx.flipper.storage_list.assert_called_once_with("/ext/infrared")
    # Elke call via de runner: precies één auditregel per tool, met onze frontend.
    tools = lab.tool_audit()
    assert [(r["tool"], r["frontend"], r["decision"]) for r in tools] == [
        ("status", FRONTEND, "allow"),
        ("storage_list", FRONTEND, "allow"),
    ]
    # De tweede modelcall ziet het resultaat van de eerste.
    second = lab.provider.requests[1].messages
    assert isinstance(second[-1], ToolResult) and second[-1].name == "status"
    third = lab.provider.requests[2].messages
    assert isinstance(third[-1], ToolResult)
    assert _content(third[-1])["result"]["entries"][0]["name"] == "tv.ir"
    texts = [e.text for e in lab.events if isinstance(e, AssistantText)]
    assert texts == ["Even kijken.", "Er staat één bestand: tv.ir."]


def test_parallel_tool_calls_in_one_round(lab: Lab) -> None:
    a = tool_call("status")
    b = tool_call("storage_list", {"path": "/ext"})
    lab.provider.add(use(a, b), reply("Klaar."))
    result = lab.turn("status en lijst")
    assert result.executed == 2
    request = lab.provider.requests[1].messages
    assert [m.call_id for m in request if isinstance(m, ToolResult)] == [a.id, b.id]
    started = [e.name for e in lab.events if isinstance(e, ToolCallStarted)]
    finished = [(e.name, e.ok) for e in lab.events if isinstance(e, ToolCallFinished)]
    assert started == ["status", "storage_list"]
    assert finished == [("status", True), ("storage_list", True)]


def test_error_results(lab: Lab) -> None:
    bad_json = broken_call()
    unknown = tool_call("rm_rf", {"path": "/"})
    invalid = tool_call("storage_list", {"path": "/ext", "extra": 1})
    lab.provider.add(use(bad_json, unknown, invalid), reply("Dat ging mis."))
    result = lab.turn("doe iets")

    assert result.stop is TurnStop.END_TURN
    r_json, r_unknown, r_invalid = lab.results()
    assert r_json.is_error and _content(r_json)["error"] == "invalid_json"
    assert r_unknown.is_error and _content(r_unknown)["error"] == "unknown_tool"
    assert r_invalid.is_error and _content(r_invalid)["error"] == "invalid_arguments"
    # Kapotte JSON gaat niet naar de runner; de andere twee wel (en worden geaudit).
    assert result.executed == 2
    assert [r["tool"] for r in lab.tool_audit()] == ["rm_rf", "storage_list"]
    lab.ctx.flipper.storage_list.assert_not_called()


def test_step_limit(tmp_path: Path, make_ctx: Callable[..., ToolContext]) -> None:
    lab = Lab(tmp_path, make_ctx(tmp_path), limits=AgentLimits(max_steps=2))
    lab.provider.add(*(use(tool_call("status")) for _ in range(3)))
    result = lab.turn("blijf status opvragen")

    assert result.stop is TurnStop.MAX_STEPS
    assert result.steps == 3 and result.executed == 2
    last = lab.results()[-1]
    assert last.is_error and "maximum van 2 stappen" in _content(last)["message"]
    # Volgende beurt werkt gewoon (geschiedenis is geldig).
    lab.provider.add(reply("Oké, gestopt."))
    assert lab.turn("stop maar").stop is TurnStop.END_TURN


def test_long_history_drops_oldest_whole_turns(
    tmp_path: Path, make_ctx: Callable[..., ToolContext]
) -> None:
    lab = Lab(tmp_path, make_ctx(tmp_path), limits=AgentLimits(max_history_chars=10_000))
    for i in range(6):  # elke beurt ~4000 tekens: tool call + antwoord
        lab.provider.add(use(tool_call("status")), reply(f"antwoord {i} " + "x" * 4000))
        lab.turn(f"vraag {i}")
    # De laatste provider-call kreeg een geldige, begrensde geschiedenis (assert_valid_history
    # draait in de fake) die met een gebruikersbeurt begint.
    sent = lab.provider.requests[-1].messages
    assert isinstance(sent[0], UserMessage)
    assert "vraag 0" not in sent[0].text
    assert "oudere gespreksbeurt(en) zijn weggelaten" in sent[0].text
    assert sum(len(m.text) for m in sent if isinstance(m, AssistantMessage)) < 10_000
    assert any(r.get("event") == "agent_history_trimmed" for r in lab.audit())


def test_history_keeps_last_turn_even_if_over_budget(
    tmp_path: Path, make_ctx: Callable[..., ToolContext]
) -> None:
    lab = Lab(tmp_path, make_ctx(tmp_path), limits=AgentLimits(max_history_chars=10_000))
    lab.provider.add(reply("y" * 20_000), reply("kort"))
    lab.turn("eerste")
    lab.turn("tweede")
    sent = lab.provider.requests[-1].messages
    # De te grote eerste beurt valt weg; de nieuwe beurt staat er altijd.
    assert isinstance(sent[0], UserMessage) and sent[0].text.endswith("tweede")


def test_calls_per_step_limit(tmp_path: Path, make_ctx: Callable[..., ToolContext]) -> None:
    lab = Lab(tmp_path, make_ctx(tmp_path), limits=AgentLimits(max_calls_per_step=2))
    lab.provider.add(use(*(tool_call("status") for _ in range(3))), reply("Klaar."))
    result = lab.turn("drie keer status")
    assert result.executed == 2
    third = lab.results()[2]
    assert _content(third)["error"] == "too_many_calls"
    assert len(lab.tool_audit()) == 2


def test_turn_timeout(tmp_path: Path, make_ctx: Callable[..., ToolContext]) -> None:
    clock = Clock()
    lab = Lab(tmp_path, make_ctx(tmp_path), limits=AgentLimits(turn_timeout_s=10), clock=clock)

    def slow(_messages: Sequence[Message]) -> Any:
        clock.now += 11  # deze modelcall duurde te lang
        return use(tool_call("status"), tool_call("status"))

    lab.provider.add(slow)
    result = lab.turn("status")
    assert result.stop is TurnStop.TIMEOUT
    assert result.executed == 0
    assert all(_content(r)["error"] == "turn_timeout" for r in lab.results())


def test_large_result_is_truncated(lab: Lab) -> None:
    lab.ctx.flipper.storage_read.return_value = b"A" * 50_000
    lab.provider.add(
        use(tool_call("storage_read", {"path": "/ext/groot.txt", "max_bytes": 65536})),
        reply("Het bestand is groot."),
    )
    lab.turn("lees het bestand")
    (result,) = lab.results()
    cap = AgentLimits().max_result_chars
    assert len(result.content) < cap + 200
    assert "INGEKORT door PiToolbox" in result.content
    assert result.content.startswith('{"ok":true,')


def test_think_blocks_hidden_but_kept_in_history(lab: Lab) -> None:
    raw = "<think>de gebruiker wil X, ik roep niets aan</think>\nHallo!"
    lab.provider.add(reply(raw))
    result = lab.turn("hoi")
    assert result.text == "Hallo!"
    assert [e.text for e in lab.events if isinstance(e, AssistantText)] == ["Hallo!"]
    # MiniMax: content moet ongewijzigd terug, dus de geschiedenis houdt de ruwe tekst.
    assert lab.session.history[-1].text == raw  # type: ignore[union-attr]


def test_provider_state_preserved_exactly(lab: Lab) -> None:
    state = ProviderState("anthropic", {"content": [{"type": "thinking", "signature": "s"}]})
    lab.provider.add(use(tool_call("status"), state=state), reply("Klaar."))
    lab.turn("status")
    assistant = lab.provider.requests[1].messages[1]
    assert assistant.state is state  # type: ignore[union-attr]


def test_truncated_answer_with_calls_does_not_execute(lab: Lab) -> None:
    lab.provider.add(use(tool_call("status"), stop=StopReason.MAX_TOKENS))
    result = lab.turn("status")
    assert result.stop is TurnStop.MAX_TOKENS
    assert result.executed == 0
    assert _content(lab.results()[0])["error"] == "not_executed"


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("<think>x</think>Hoi", "Hoi"),
        ("<THINK>\nx\n</THINK>\n\nHoi", "Hoi"),
        ("redeneren zonder opentag</think>Hoi", "Hoi"),  # Qwen3/R1: opentag in het template
        ("Hoi<think>afgekapt midden in het denk", "Hoi"),
        ("<thinking>a</thinking>Hoi", "Hoi"),
        ("Gewoon tekst", "Gewoon tekst"),
    ],
)
def test_strip_think(raw: str, shown: str) -> None:
    assert strip_think(raw) == shown


def test_for_terminal_strips_control_sequences() -> None:
    nasty = "ok\x1b[2J\x1b]0;titel\x07\rBevestigen? [j/N]‮\tklaar\n"
    clean = for_terminal(nasty)
    assert "\x1b" not in clean and "\r" not in clean and "‮" not in clean
    assert clean == "ok[2J]0;titelBevestigen? [j/N]\tklaar\n"


# --- bevestigen: alleen een mens ------------------------------------------------------------


def _start_transmit(lab: Lab) -> PendingConfirmation:
    lab.provider.add(use(tool_call("ir_send", IR_POWER)), reply("Bevestig je dat?"))
    result = lab.turn("zet de tv aan")
    assert result.stop is TurnStop.END_TURN
    (pending,) = result.pending
    assert pending.tool == "ir_send" and "Power" in pending.summary
    assert [e for e in lab.events if isinstance(e, PendingConfirmation)] == [pending]
    lab.ctx.flipper.ir_send.assert_not_called()
    return pending


def test_transmit_gives_pending_without_id_for_model(lab: Lab) -> None:
    pending = _start_transmit(lab)
    (result,) = lab.results()
    view = _content(result)
    assert view["error"] == "confirmation_required" and not result.is_error
    assert "pending_id" not in view
    assert pending.pending_id not in json.dumps([str(m) for m in lab.session.history])
    # Gebonden aan deze frontend: een andere frontend kan hem niet bevestigen.
    other = lab.runner.call("mcp", CONFIRM_TOOL, {"pending_id": pending.pending_id})
    assert other["error"] == "unknown_pending"
    assert len(lab.ctx.pending.list()) == 1


def test_human_yes_executes_once(lab: Lab) -> None:
    pending = _start_transmit(lab)
    outcome = lab.session.confirm(pending, approved=True)
    assert outcome.status is ConfirmStatus.EXECUTED
    lab.ctx.flipper.ir_send.assert_called_once_with("/ext/infrared/tv.ir", "Power")
    confirmed = [r for r in lab.tool_audit() if r.get("confirmed")]
    assert len(confirmed) == 1 and confirmed[0]["frontend"] == FRONTEND
    # Eenmalig: nog een keer bevestigen doet niets.
    again = lab.session.confirm(pending, approved=True)
    assert again.status is ConfirmStatus.EXPIRED
    assert lab.ctx.flipper.ir_send.call_count == 1
    # De uitkomst gaat mee in het volgende gebruikersbericht.
    lab.provider.add(reply("Gedaan."))
    lab.turn("gelukt?")
    user = lab.provider.requests[-1].messages[-1]
    assert user.text.startswith(NOTE_PREFIX)  # type: ignore[union-attr]
    assert "bevestigd en het is uitgevoerd" in user.text  # type: ignore[union-attr]
    assert user.text.endswith("gelukt?")  # type: ignore[union-attr]


def test_human_no_does_not_execute(lab: Lab) -> None:
    pending = _start_transmit(lab)
    outcome = lab.session.confirm(pending, approved=False)
    assert outcome.status is ConfirmStatus.REJECTED
    assert lab.ctx.pending.list() == []
    # Ook later niet meer te bevestigen, door niemand.
    late = lab.runner.call(FRONTEND, CONFIRM_TOOL, {"pending_id": pending.pending_id})
    assert late["error"] == "unknown_pending"
    lab.ctx.flipper.ir_send.assert_not_called()
    rejected = [r for r in lab.audit() if r.get("decision") == "rejected"]
    assert rejected and rejected[0]["error"] == "rejected_by_user"


def test_model_cannot_confirm_even_with_real_pending_id(lab: Lab) -> None:
    pending = _start_transmit(lab)

    def cheat(_messages: Sequence[Message]) -> Any:
        # Het model "weet" de echte id (bijv. via een gelekte log) en probeert te bevestigen.
        return use(tool_call(CONFIRM_TOOL, {"pending_id": pending.pending_id}))

    lab.provider.add(cheat, reply("Dat mag ik niet."))
    result = lab.turn("bevestig het zelf maar")

    refused = lab.results()[-1]
    assert refused.is_error and _content(refused)["error"] == "not_allowed_for_model"
    assert result.executed == 0
    lab.ctx.flipper.ir_send.assert_not_called()
    # De pending staat er nog: alleen de mens kan hem nog bevestigen of afwijzen.
    assert [p.id for p in lab.ctx.pending.list()] == [pending.pending_id]
    denied = [r for r in lab.audit() if r.get("error") == "not_allowed_for_model"]
    assert denied and denied[0]["tool"] == CONFIRM_TOOL and denied[0]["source"] == "agent"


def test_model_cannot_continue_while_confirmation_open(lab: Lab) -> None:
    lab.provider.add(
        use(tool_call("ir_send", IR_POWER)),
        use(tool_call(CONFIRM_TOOL, {"pending_id": "geraden_id"}), tool_call("status")),
    )
    result = lab.turn("zet de tv aan en bevestig")
    assert result.stop is TurnStop.AWAITING_CONFIRMATION
    assert len(result.pending) == 1
    assert result.executed == 1  # alleen ir_send (die een pending gaf)
    assert all(_content(r)["error"] == "not_executed" for r in lab.results()[1:])
    lab.ctx.flipper.ir_send.assert_not_called()


def test_confirmation_ttl_expired(
    tmp_path: Path, make_ctx: Callable[..., ToolContext], fixed_scope: Scope
) -> None:
    store_clock = Clock()
    lab = Lab(tmp_path, make_ctx(tmp_path, fixed_scope, ttl_s=60, clock=store_clock))
    pending = _start_transmit(lab)
    store_clock.now += 61
    outcome = lab.session.confirm(pending, approved=True)
    assert outcome.status is ConfirmStatus.EXPIRED
    lab.ctx.flipper.ir_send.assert_not_called()
    lab.provider.add(reply("Verlopen."))
    lab.turn("en?")
    assert "verlopen" in lab.provider.requests[-1].messages[-1].text  # type: ignore[union-attr]


def test_confirm_unknown_pending_is_expired(lab: Lab) -> None:
    fake = PendingConfirmation("verzonnen", "ir_send", "IR", 60, 0.0)
    assert lab.session.confirm(fake, approved=True).status is ConfirmStatus.EXPIRED
    assert lab.tool_audit() == []


# --- "Flipper verplicht": bevestigen op de Flipper ---------------------------------------------


def test_confirm_while_flipper_attached_is_on_flipper_and_stays_open(lab: Lab) -> None:
    pending = _start_transmit(lab)
    lease = lab.runner.attach_physical_confirmer("flipper-ui")
    outcome = lab.session.confirm(pending, approved=True)
    assert outcome.status is ConfirmStatus.ON_FLIPPER
    lab.ctx.flipper.ir_send.assert_not_called()
    assert lab.session.open_confirmations == (pending,)
    assert [p.id for p in lab.ctx.pending.list()] == [pending.pending_id]
    # Na loskoppelen kan de chat weer bevestigen (oude regel).
    lab.runner.detach_physical_confirmer(lease, "app dicht")
    assert lab.session.confirm(pending, approved=True).status is ConfirmStatus.EXECUTED
    lab.ctx.flipper.ir_send.assert_called_once()


def test_reject_in_chat_allowed_while_flipper_attached(lab: Lab) -> None:
    pending = _start_transmit(lab)
    lab.runner.attach_physical_confirmer("flipper-ui")
    assert lab.session.confirm(pending, approved=False).status is ConfirmStatus.REJECTED
    assert lab.ctx.pending.list() == []


def test_external_outcome_tells_the_model(lab: Lab) -> None:
    pending = _start_transmit(lab)
    lease = lab.runner.attach_physical_confirmer("flipper-ui")
    raw = lab.runner.confirm_physical(lease, pending.pending_id)
    outcome = lab.session.external_outcome(pending.pending_id, approved=True, result=raw)
    assert outcome is not None and outcome.status is ConfirmStatus.EXECUTED
    assert lab.session.open_confirmations == ()
    assert lab.session.external_outcome(pending.pending_id, approved=True, result=raw) is None
    lab.provider.add(reply("Mooi."))
    lab.turn("en?")
    text = lab.provider.requests[-1].messages[-1].text  # type: ignore[union-attr]
    assert "bevestigd en het is uitgevoerd" in text


def test_external_rejection_tells_the_model(lab: Lab) -> None:
    pending = _start_transmit(lab)
    lease = lab.runner.attach_physical_confirmer("flipper-ui")
    raw = lab.runner.reject_physical(lease, pending.pending_id)
    outcome = lab.session.external_outcome(pending.pending_id, approved=False, result=raw)
    assert outcome is not None and outcome.status is ConfirmStatus.REJECTED


def test_cancel_stops_turn_between_steps(lab: Lab) -> None:
    stop = {"now": False}

    def first(_messages: Sequence[Message]) -> Any:
        stop["now"] = True  # de frontend breekt af terwijl het model nog denkt
        return use(tool_call("status"))

    lab.provider.add(first)
    result = lab.session.run_turn("status", cancel=lambda: stop["now"])
    assert result.stop is TurnStop.INTERRUPTED
    assert result.executed == 0
    assert_valid_history(lab.session.history)
    assert _content(lab.results()[-1])["error"] == "not_executed"


# --- fouten en afbreken ----------------------------------------------------------------


def test_llm_error_mid_turn_keeps_history_consistent(lab: Lab) -> None:
    lab.provider.add(use(tool_call("status")), LLMServerError("503 overbelast", status=503))
    result = lab.turn("status")
    assert result.stop is TurnStop.LLM_ERROR
    assert result.error is not None and result.error.startswith("llm_server_error:")
    assert not result.rolled_back and result.executed == 1
    lab.provider.add(reply("Nu wel."))
    assert lab.turn("nog een keer").stop is TurnStop.END_TURN
    end = [r for r in lab.audit() if r.get("event") == "agent_turn_end"]
    assert [r["stop"] for r in end] == ["llm_error", "end_turn"]
    assert end[0]["error"] == "llm_server_error"


def test_llm_error_on_first_call_rolls_back(lab: Lab) -> None:
    lab.provider.add(LLMServerError("time-out"))
    result = lab.turn("hoi")
    assert result.stop is TurnStop.LLM_ERROR and result.rolled_back
    assert lab.session.history == ()
    lab.provider.add(reply("Hallo."))
    assert lab.turn("hoi").text == "Hallo."


def test_rollback_keeps_pending_notes(lab: Lab) -> None:
    pending = _start_transmit(lab)
    lab.session.confirm(pending, approved=False)
    lab.provider.add(LLMServerError("weg"), reply("Ok."))
    assert lab.turn("eerste poging").rolled_back
    lab.turn("tweede poging")
    text = lab.provider.requests[-1].messages[-1].text  # type: ignore[union-attr]
    assert "afgewezen" in text and text.endswith("tweede poging")
    assert "eerste poging" not in text


def test_interrupt_during_tool_closes_open_calls(lab: Lab) -> None:
    lab.ctx.flipper.storage_list.side_effect = KeyboardInterrupt
    a = tool_call("storage_list", {"path": "/ext"})
    b = tool_call("status")
    lab.provider.add(use(a, b))
    result = lab.turn("lijst")
    assert result.stop is TurnStop.INTERRUPTED
    assert [r.call_id for r in lab.results()] == [a.id, b.id]
    assert all(r.is_error for r in lab.results())
    lab.provider.add(reply("Ok."))
    assert lab.turn("verder").stop is TurnStop.END_TURN


def test_interrupt_during_model_call(lab: Lab) -> None:
    lab.provider.add(use(tool_call("status")), KeyboardInterrupt())
    result = lab.turn("status")
    assert result.stop is TurnStop.INTERRUPTED and not result.rolled_back


def test_emergency_stop_from_human(lab: Lab) -> None:
    _start_transmit(lab)
    raw = lab.session.emergency_stop()
    assert raw["ok"] and raw["result"]["pending_cleared"] == 1
    lab.ctx.flipper.abort.assert_called_once()
    assert lab.session.open_confirmations == ()
    lab.provider.add(reply("Gestopt."))
    lab.turn("wat gebeurde er?")
    assert "noodstop" in lab.provider.requests[-1].messages[-1].text  # type: ignore[union-attr]


def test_model_may_call_emergency_stop(lab: Lab) -> None:
    lab.provider.add(use(tool_call("emergency_stop")), reply("Alles gestopt."))
    result = lab.turn("stop alles")
    assert result.executed == 1
    lab.ctx.flipper.abort.assert_called_once()


# --- audit ---------------------------------------------------------------------------


def test_turn_audit_has_no_message_contents(lab: Lab) -> None:
    lab.provider.add(use(tool_call("status")), reply("GEHEIM-ANTWOORD"))
    lab.turn("GEHEIME-VRAAG")
    turn_records = [r for r in lab.audit() if "event" in r]
    assert [r["event"] for r in turn_records] == ["agent_turn_start", "agent_turn_end"]
    end = turn_records[1]
    assert end["model"] == "fake-model-2026" and end["stop"] == "end_turn"
    assert (end["steps"], end["tool_calls"], end["executed"]) == (2, 1, 1)
    assert end["input_tokens"] == 30 and end["output_tokens"] == 12
    assert end["frontend"] == FRONTEND and end["session"] == turn_records[0]["session"]
    text = lab.audit_path.read_text()
    assert "GEHEIM" not in text


def test_limits_from_settings() -> None:
    limits = AgentLimits.from_settings(Settings(agent_max_steps=3, agent_turn_timeout_s=5))
    assert limits.max_steps == 3 and limits.turn_timeout_s == 5
    with pytest.raises(ValueError, match="PITOOLBOX_AGENT_"):
        AgentLimits.from_settings(Settings(agent_max_steps=0))


def test_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PITOOLBOX_AGENT_MAX_STEPS", "4")
    monkeypatch.setenv("PITOOLBOX_AGENT_MAX_RESULT_CHARS", "1000")
    settings = Settings.from_env()
    assert settings.agent_max_steps == 4 and settings.agent_max_result_chars == 1000


# --- uitkomst van buitenaf (subscribe_resolutions) ---------------------------------------------


def test_flipper_ok_reaches_session_without_bridge(lab: Lab) -> None:
    """Elke AgentSession hoort de uitkomst van haar eigen pending, ook als de Flipper hem
    afhandelde (niet alleen de preset-sessie van de bridge)."""
    pending = _start_transmit(lab)
    lease = lab.runner.attach_physical_confirmer("flipper-ui")
    assert lab.session.confirm(pending, approved=True).status is ConfirmStatus.ON_FLIPPER
    lab.runner.confirm_physical(lease, pending.pending_id)
    (outcome,) = lab.session.take_external()
    assert outcome.status is ConfirmStatus.EXECUTED and outcome.pending == pending
    assert outcome.result is not None and outcome.result["ok"] is True
    assert lab.session.open_confirmations == ()
    assert lab.session.take_external() == []
    lab.provider.add(reply("Mooi."))
    lab.turn("en?")
    text = lab.provider.requests[-1].messages[-1].text  # type: ignore[union-attr]
    assert text.count("bevestigd en het is uitgevoerd") == 1


def test_external_rejection_expiry_and_remote_stop(
    tmp_path: Path, make_ctx: Callable[..., ToolContext], fixed_scope: Scope
) -> None:
    clock = Clock()
    lab = Lab(tmp_path, make_ctx(tmp_path, fixed_scope, ttl_s=60.0, clock=clock), clock=clock)
    rejected = _start_transmit(lab)
    lab.events.clear()
    expired = _start_transmit(lab)
    lab.events.clear()
    lease = lab.runner.attach_physical_confirmer("flipper-ui")
    lab.runner.reject_physical(lease, rejected.pending_id)
    clock.now += 61  # TTL voorbij; take_external schoont de store eerst op
    outcomes = {o.pending.pending_id: o.status for o in lab.session.take_external()}
    assert outcomes == {
        rejected.pending_id: ConfirmStatus.REJECTED,
        expired.pending_id: ConfirmStatus.EXPIRED,
    }
    cleared = _start_transmit(lab)
    lab.runner.call("mcp", "emergency_stop", {})  # noodstop vanuit een andere frontend
    (outcome,) = lab.session.take_external()
    assert outcome.pending == cleared and outcome.status is ConfirmStatus.CLEARED
    assert lab.session.open_confirmations == ()
    # De melding over afwijzen en verlopen ging mee met de beurt die `cleared` aanmaakte.
    first = lab.provider.requests[-2].messages[-1].text  # type: ignore[union-attr]
    assert "afgewezen" in first and "verlopen" in first
    lab.provider.add(reply("Ok."))
    lab.turn("en?")
    text = lab.provider.requests[-1].messages[-1].text  # type: ignore[union-attr]
    assert "gewist door een noodstop" in text


def test_own_confirm_is_not_reported_twice_and_close_unsubscribes(lab: Lab) -> None:
    pending = _start_transmit(lab)
    assert lab.session.confirm(pending, approved=True).status is ConfirmStatus.EXECUTED
    assert lab.session.take_external() == []
    lab.session.close()
    lab.session.close()  # idempotent
    lab.events.clear()
    lab.ctx.flipper.ir_send.reset_mock()
    other = _start_transmit(lab)
    lease = lab.runner.attach_physical_confirmer("flipper-ui")
    lab.runner.confirm_physical(lease, other.pending_id)
    assert lab.session.take_external() == []  # afgemeld: hoort niets meer


def test_model_still_cannot_confirm_after_external_events(lab: Lab) -> None:
    pending = _start_transmit(lab)
    lab.runner.attach_physical_confirmer("flipper-ui")
    lab.provider.add(
        use(tool_call(CONFIRM_TOOL, {"pending_id": pending.pending_id})), reply("Kan niet.")
    )
    lab.turn("bevestig maar")
    lab.ctx.flipper.ir_send.assert_not_called()
    assert [p.id for p in lab.ctx.pending.list()] == [pending.pending_id]
