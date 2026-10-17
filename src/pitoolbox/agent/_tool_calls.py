"""Tool-calls van het model: welke tools het ziet, en uitvoeren via de `ToolRunner`.

Elke call krijgt een `ToolResult` in de geschiedenis, ook als hij niet wordt uitgevoerd (limiet,
tijd, onleesbare call, een tool die alleen de gebruiker mag); anders weigert de provider het
volgende verzoek. Zie `session` voor de ontwerpkeuzes.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pitoolbox.agent.types import (
    AgentLimits,
    EventSink,
    PendingConfirmation,
    ToolCallFinished,
    ToolCallStarted,
)
from pitoolbox.core.tools.base import Risk
from pitoolbox.core.tools.runner import CONFIRM_TOOL
from pitoolbox.llm._common import replay_name
from pitoolbox.llm.types import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolCallError,
    ToolDef,
    ToolResult,
    Usage,
)

if TYPE_CHECKING:
    from pitoolbox.agent._confirmations import OpenConfirmations
    from pitoolbox.core.tools.base import ToolSpec
    from pitoolbox.core.tools.runner import ToolRunner

log = logging.getLogger(__name__)

# Tools die het model nooit krijgt en die de loop altijd weigert: alleen een mens bevestigt.
MODEL_HIDDEN_TOOLS = frozenset({CONFIRM_TOOL})

_CALL_ERROR_MESSAGES = {
    ToolCallError.INVALID_JSON: "de argumenten zijn geen geldige JSON",
    ToolCallError.NOT_AN_OBJECT: "de argumenten moeten een JSON-object zijn",
    ToolCallError.MISSING_NAME: "de toolnaam ontbreekt",
}

_TRANSMIT_NOTE = (
    "Risicoklasse: transmit. Zendt alleen naar apparaten in scope.yaml; buiten scope wordt "
    "altijd geweigerd. Binnen scope voert deze tool niets direct uit: PiToolbox legt de "
    "samenvatting aan de gebruiker voor, en alleen de gebruiker kan bevestigen."
)


@dataclass
class TurnState:
    deadline: float
    cancel: Callable[[], bool] | None = None
    steps: int = 0
    tool_calls: int = 0
    executed: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    text: str = ""
    timed_out: bool = False
    pending: list[PendingConfirmation] = field(default_factory=list)

    def add_usage(self, usage: Usage) -> None:
        if usage.input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + usage.output_tokens


class Cancelled(Exception):
    """Interne stop: de frontend vroeg via `cancel()` om af te breken."""


def check_cancel(turn: TurnState) -> None:
    if turn.cancel is not None and turn.cancel():
        raise Cancelled


def model_tool_defs(specs: Iterable[ToolSpec]) -> tuple[ToolDef, ...]:
    """Tooldefinities voor het model: alles behalve `MODEL_HIDDEN_TOOLS`.

    Hetzelfde schema als de MCP-tools; alleen de transmit-uitleg zegt niet "roep
    confirm_action aan" maar dat de gebruiker bevestigt.
    """
    defs: list[ToolDef] = []
    for spec in specs:
        if spec.name in MODEL_HIDDEN_TOOLS:
            continue
        description = (
            f"{spec.description}\n\n{_TRANSMIT_NOTE}"
            if spec.risk is Risk.TRANSMIT
            else spec.full_description()
        )
        defs.append(
            ToolDef(
                name=spec.name,
                description=description,
                parameters=copy.deepcopy(spec.json_schema()),
            )
        )
    return tuple(defs)


def serialize_view(view: dict[str, Any], max_chars: int) -> str:
    """Compacte JSON voor het model, hoogstens `max_chars` tekens plus een inkortmelding."""
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= max_chars:
        return text
    return (
        f"{text[:max_chars]}…[INGEKORT door PiToolbox: resultaat was {len(text)} tekens, "
        f"alleen de eerste {max_chars} staan hier]"
    )


class ToolCallExecutor:
    """Voert de tool-calls van één modelantwoord uit en zet de resultaten in de geschiedenis."""

    def __init__(
        self,
        runner: ToolRunner,
        frontend: str,
        limits: AgentLimits,
        clock: Callable[[], float],
        confirmations: OpenConfirmations,
    ) -> None:
        self._runner = runner
        self._frontend = frontend
        self._limits = limits
        self._clock = clock
        self._confirmations = confirmations

    def run(
        self,
        history: list[Message],
        calls: tuple[ToolCall, ...],
        turn: TurnState,
        emit: EventSink,
    ) -> None:
        limit = self._limits.max_calls_per_step
        for i, call in enumerate(calls):
            if i >= limit:
                self._add_result(
                    history,
                    call,
                    _error(
                        "too_many_calls", f"niet uitgevoerd: hoogstens {limit} tool-calls per stap"
                    ),
                )
            elif self._clock() >= turn.deadline:
                turn.timed_out = True
                self._add_result(
                    history,
                    call,
                    _error("turn_timeout", "niet uitgevoerd: tijdslimiet van de beurt bereikt"),
                )
            else:
                check_cancel(turn)
                self._execute(history, call, turn, emit)

    def _execute(
        self, history: list[Message], call: ToolCall, turn: TurnState, emit: EventSink
    ) -> None:
        emit(ToolCallStarted(call.id, call.name, call.arguments))
        if call.error is not None:
            view = _error(str(call.error), f"niet uitgevoerd: {_CALL_ERROR_MESSAGES[call.error]}")
        elif call.name in MODEL_HIDDEN_TOOLS:
            view = self._refuse_hidden(call)
        else:
            try:
                raw = self._runner.call(self._frontend, call.name, call.arguments)
            except Exception as exc:  # de runner hoort altijd een dict te geven
                log.exception("runner.call(%s) faalde onverwacht", call.name)
                raw = _error("internal_error", type(exc).__name__)
            turn.executed += 1
            view = self._model_view(raw, call, turn, emit)
        self._add_result(history, call, view)
        emit(
            ToolCallFinished(
                call.id,
                call.name,
                ok=bool(view.get("ok")),
                error=_opt_str(view.get("error")),
                message=_opt_str(view.get("message")),
            )
        )

    def _model_view(
        self, raw: dict[str, Any], call: ToolCall, turn: TurnState, emit: EventSink
    ) -> dict[str, Any]:
        """Wat het model van een runner-resultaat te zien krijgt."""
        pending_id = raw.get("pending_id")
        if raw.get("error") != "confirmation_required" or not isinstance(pending_id, str):
            return raw
        pending = PendingConfirmation(
            pending_id=pending_id,
            tool=call.name,
            summary=str(raw.get("summary", call.name)),
            expires_in_s=int(raw.get("expires_in_s", 0)),
            created=self._clock(),
        )
        self._confirmations.add(pending)
        turn.pending.append(pending)
        emit(pending)
        # Bewust zonder pending_id: het model kan en hoeft niets te bevestigen.
        return {
            "ok": False,
            "error": "confirmation_required",
            "summary": pending.summary,
            "expires_in_s": pending.expires_in_s,
            "message": (
                "Nog niet uitgevoerd. PiToolbox vraagt de gebruiker nu zelf om bevestiging; "
                "jij kunt dit niet bevestigen. Leg kort uit wat er gaat gebeuren."
            ),
        }

    def _refuse_hidden(self, call: ToolCall) -> dict[str, Any]:
        message = f"{call.name} is alleen voor de gebruiker; jij kunt niets bevestigen"
        log.warning("model probeerde %s aan te roepen; geweigerd", call.name)
        self._runner.audit.write(
            frontend=self._frontend,
            tool=call.name,
            risk=None,
            args=call.arguments,
            decision="deny",
            ok=False,
            error="not_allowed_for_model",
            result=message,
            duration_ms=0,
            source="agent",
        )
        return _error("not_allowed_for_model", message)

    def refuse_all(self, history: list[Message], calls: tuple[ToolCall, ...], message: str) -> None:
        for call in calls:
            self._add_result(history, call, _error("not_executed", message))

    def close_open_calls(self, history: list[Message], message: str) -> None:
        """Geeft elke tool-call van het laatste assistant-bericht zonder resultaat een fout."""
        for idx in range(len(history) - 1, -1, -1):
            msg = history[idx]
            if not isinstance(msg, AssistantMessage):
                continue
            answered = {m.call_id for m in history[idx + 1 :] if isinstance(m, ToolResult)}
            for call in msg.tool_calls:
                if call.id not in answered:
                    self._add_result(history, call, _error("not_executed", message))
            return

    def _add_result(self, history: list[Message], call: ToolCall, view: dict[str, Any]) -> None:
        is_error = not view.get("ok") and view.get("error") != "confirmation_required"
        history.append(
            ToolResult(
                call_id=call.id,
                name=replay_name(call),
                content=serialize_view(view, self._limits.max_result_chars),
                is_error=is_error,
            )
        )


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
