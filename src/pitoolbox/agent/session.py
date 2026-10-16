"""Agent-loop (fase 4): een gesprek met een LLM dat tools via de `ToolRunner` aanroept.

Eén gebruikersbeurt (`AgentSession.run_turn`):

    gebruiker -> model -> (tool-calls -> ToolRunner.call -> ToolResults -> model)* -> klaar

Ontwerpkeuzes:
- **Alles via de ToolRunner.** Tool-calls van het model gaan via `runner.call(frontend, ...)`
  (valideren, policy, scope, audit), nooit rechtstreeks naar een handler. Calls die de
  providerlaag niet kon parsen (`ToolCall.error`) worden niet uitgevoerd maar als fout
  teruggemeld.
- **Alleen een mens bevestigt.** `confirm_action` staat niet in de toollijst van het model,
  en een call ernaar wordt hier geweigerd (en geaudit) voordat hij de runner bereikt. Een
  transmit binnen scope geeft een `PendingConfirmation`-event aan de frontend; het model
  krijgt de samenvatting, maar niet de `pending_id`. Alleen de frontend roept na een
  expliciet "ja" van de mens `confirm()` aan. Die gebruikt dezelfde frontend-id als de
  call die de pending maakte, dus de binding, de TTL en het eenmalig gebruik van
  `PendingStore` blijven gelden. De uitkomst gaat als `[PiToolbox-melding]` mee in het
  volgende gebruikersbericht (niet als los bericht: sommige chat-templates eisen dat
  user en assistant elkaar afwisselen).
- `emergency_stop` mag het model wel: hij stopt alleen (captures, Flipper-actie, open
  bevestigingen) en kan niets zenden. Het ergste wat het model ermee kan, is een open
  bevestiging wissen.
- **Begrensd.** Hoogstens `max_steps` tool-rondes per beurt, `max_calls_per_step` calls per
  modelantwoord en `turn_timeout_s` per beurt. De tijdslimiet wordt tussen stappen gecontroleerd:
  een lopende modelcall of tool wordt niet onderbroken (die hebben hun eigen time-outs).
  Tool-resultaten gaan compact (JSON) en hoogstens `max_result_chars` tekens naar het model.
- **Geschiedenis blijft geldig.** Elke tool-call in de geschiedenis krijgt een `ToolResult`,
  ook bij een limiet, Ctrl+C of een fout; anders weigert de provider het volgende verzoek.
  Faalt al de eerste modelcall van een beurt, dan wordt de beurt teruggedraaid.
  `Completion.message` (met `ProviderState`, bijv. thinking-blokken of `reasoning_content`)
  gaat ongewijzigd de geschiedenis in; `<think>`-blokken verdwijnen alleen uit de weergave.
- **Audit.** De runner schrijft elke toolcall. Daarnaast komen per beurt een start- en
  eindregel (`event: agent_turn_start/agent_turn_end`) in dezelfde audit-log: model,
  stopreden, aantallen en tokens, nooit berichtinhoud of sleutels. Zo staat in één
  bestand welk model welke toolcalls aanstuurde (ze staan tussen start en eind).
"""

from __future__ import annotations

import copy
import json
import logging
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pitoolbox.agent.prompt import NOTE_PREFIX, SYSTEM_PROMPT
from pitoolbox.agent.text import strip_think
from pitoolbox.core.tools.base import Risk
from pitoolbox.core.tools.runner import CONFIRM_TOOL
from pitoolbox.llm._common import replay_name
from pitoolbox.llm.errors import LLMError
from pitoolbox.llm.types import (
    AssistantMessage,
    Message,
    StopReason,
    ToolCall,
    ToolCallError,
    ToolDef,
    ToolResult,
    Usage,
    UserMessage,
)

if TYPE_CHECKING:
    from pitoolbox.config import Settings
    from pitoolbox.core.tools.base import ToolSpec
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider

log = logging.getLogger(__name__)

# Tools die het model nooit krijgt en die de loop altijd weigert: alleen een mens bevestigt.
MODEL_HIDDEN_TOOLS = frozenset({CONFIRM_TOOL})
EMERGENCY_STOP_TOOL = "emergency_stop"

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


class AgentLimits(BaseModel):
    """Grenzen van één beurt; uit PITOOLBOX_AGENT_* (zie `Settings`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_steps: int = Field(default=8, ge=1, le=50)
    max_calls_per_step: int = Field(default=8, ge=1, le=50)
    turn_timeout_s: float = Field(default=300.0, gt=0, le=3600)
    max_result_chars: int = Field(default=8000, ge=500, le=200_000)
    # Grootte van de geschiedenis (in tekens, ruwe schatting van de context) voordat de oudste
    # hele beurten wegvallen. Tekens i.p.v. tokens: providerneutraal en zonder tokenizer;
    # ~4 tekens per token, dus 120 000 tekens is ruim binnen een context van 32k+ tokens.
    max_history_chars: int = Field(default=120_000, ge=10_000, le=2_000_000)

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentLimits:
        try:
            return cls(
                max_steps=settings.agent_max_steps,
                max_calls_per_step=settings.agent_max_calls_per_step,
                turn_timeout_s=settings.agent_turn_timeout_s,
                max_result_chars=settings.agent_max_result_chars,
                max_history_chars=settings.agent_max_history_chars,
            )
        except ValidationError as exc:
            fields = ", ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise ValueError(f"ongeldige agent-instelling (PITOOLBOX_AGENT_*): {fields}") from None


class TurnStop(StrEnum):
    """Waarom een beurt eindigde."""

    END_TURN = "end_turn"  # het model is klaar
    MAX_TOKENS = "max_tokens"  # antwoord afgekapt
    REFUSAL = "refusal"  # model of contentfilter weigerde
    MAX_STEPS = "max_steps"  # te veel tool-rondes
    TIMEOUT = "timeout"  # wandkloklimiet van de beurt
    # het model wilde verder terwijl een mens nog moet bevestigen
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    LLM_ERROR = "llm_error"  # de provider gaf een fout
    INTERRUPTED = "interrupted"  # Ctrl+C


_MODEL_STOPS = {
    StopReason.END_TURN: TurnStop.END_TURN,
    StopReason.OTHER: TurnStop.END_TURN,
    StopReason.TOOL_USE: TurnStop.END_TURN,  # alleen zonder calls, dus feitelijk klaar
    StopReason.MAX_TOKENS: TurnStop.MAX_TOKENS,
    StopReason.REFUSAL: TurnStop.REFUSAL,
}


# --- events voor de frontend ----------------------------------------------------------------


@dataclass(frozen=True)
class AssistantText:
    """Tekst van het model, zonder redeneerblokken (nog niet terminal-veilig)."""

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    call_id: str
    name: str
    arguments: dict[str, Any] | None


@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str
    name: str
    ok: bool
    error: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class PendingConfirmation:
    """Een transmit-actie die alleen een mens kan bevestigen (via `AgentSession.confirm`)."""

    pending_id: str
    tool: str
    summary: str
    expires_in_s: int
    created: float  # monotone klok van de sessie

    def remaining_s(self, now: float) -> float:
        return self.created + self.expires_in_s - now


AgentEvent = AssistantText | ToolCallStarted | ToolCallFinished | PendingConfirmation
EventSink = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class TurnResult:
    stop: TurnStop
    text: str = ""  # laatste getoonde modeltekst (zonder <think>)
    pending: tuple[PendingConfirmation, ...] = ()
    steps: int = 0  # modelcalls in deze beurt
    tool_calls: int = 0  # door het model gevraagd
    executed: int = 0  # via de runner uitgevoerd
    usage: Usage = field(default_factory=Usage)
    error: str | None = None  # bij LLM_ERROR: code en (geredigeerde) melding
    rolled_back: bool = False  # gebruikersbericht is niet in de geschiedenis gebleven


class ConfirmStatus(StrEnum):
    EXECUTED = "executed"  # bevestigd en uitgevoerd, tool meldde ok
    FAILED = "failed"  # bevestigd, maar de tool gaf een fout
    DENIED = "denied"  # bevestigd, maar de policy weigerde (scope intussen gewijzigd)
    EXPIRED = "expired"  # verlopen, al gebruikt, of onbekend
    REJECTED = "rejected"  # de mens zei nee


@dataclass(frozen=True)
class ConfirmOutcome:
    status: ConfirmStatus
    pending: PendingConfirmation
    result: dict[str, Any] | None = None  # ruwe uitkomst van de runner


@dataclass
class _Turn:
    deadline: float
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


def _message_chars(message: Message) -> int:
    """Ruwe grootte van een bericht in tekens, inclusief tool calls en providerstate."""
    if isinstance(message, UserMessage):
        return len(message.text)
    if isinstance(message, ToolResult):
        return len(message.content) + len(message.name)
    size = len(message.text) + sum(len(c.name) + len(c.raw_arguments) for c in message.tool_calls)
    if message.state is not None:
        size += len(json.dumps(message.state.data, default=str, ensure_ascii=False))
    return size


def note(text: str) -> str:
    return f"{NOTE_PREFIX} {text}"


class AgentSession:
    """Eén gesprek: geschiedenis, provider, runner en de bevestigingen die nog openstaan.

    Niet thread-safe: één frontend roept `run_turn`, `confirm` en `emergency_stop` na
    elkaar aan.
    """

    def __init__(
        self,
        provider: LLMProvider,
        runner: ToolRunner,
        *,
        frontend: str,
        limits: AgentLimits | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.runner = runner
        self.frontend = frontend
        self.limits = limits or AgentLimits()
        self.system_prompt = system_prompt
        self._clock = clock
        # Least privilege: tools buiten PITOOLBOX_TOOL_ALLOWLIST ziet het model niet eens (de
        # runner zou ze toch weigeren).
        self._tools = model_tool_defs(
            spec for spec in runner.specs() if runner.policy.allows_tool(spec.name)
        )
        self._history: list[Message] = []
        self._notes: list[str] = []
        self._surfaced: dict[str, PendingConfirmation] = {}
        self._session_id = secrets.token_hex(4)
        self._turns = 0

    @property
    def history(self) -> tuple[Message, ...]:
        return tuple(self._history)

    @property
    def tools(self) -> tuple[ToolDef, ...]:
        return self._tools

    @property
    def open_confirmations(self) -> tuple[PendingConfirmation, ...]:
        return tuple(self._surfaced.values())

    # --- een beurt ------------------------------------------------------------------------

    def run_turn(self, text: str, on_event: EventSink | None = None) -> TurnResult:
        emit: EventSink = on_event or (lambda _e: None)
        self._turns += 1
        t0 = self._clock()
        turn = _Turn(deadline=t0 + self.limits.turn_timeout_s)
        self._trim_history()
        start_len = len(self._history)
        notes, self._notes = self._notes, []
        self._history.append(UserMessage("\n".join([*notes, text]) if notes else text))
        self._audit_event("agent_turn_start", model=self.provider.model)

        error: str | None = None
        try:
            stop = self._loop(turn, emit)
        except KeyboardInterrupt:
            stop = TurnStop.INTERRUPTED
        except LLMError as exc:
            stop = TurnStop.LLM_ERROR
            error = f"{exc.code}: {exc}"
            log.warning("LLM-fout in beurt %d: %s", self._turns, exc.code)

        rolled_back = False
        if len(self._history) == start_len + 1:
            # Het model heeft nog niets gezegd: beurt terugdraaien, meldingen bewaren.
            del self._history[start_len:]
            self._notes = [*notes, *self._notes]
            rolled_back = True
        else:
            self._close_open_calls("niet uitgevoerd: beurt afgebroken")

        result = TurnResult(
            stop=stop,
            text=turn.text,
            pending=tuple(turn.pending),
            steps=turn.steps,
            tool_calls=turn.tool_calls,
            executed=turn.executed,
            usage=Usage(turn.input_tokens, turn.output_tokens),
            error=error,
            rolled_back=rolled_back,
        )
        self._audit_event(
            "agent_turn_end",
            model=turn.model or self.provider.model,
            stop=str(stop),
            steps=turn.steps,
            tool_calls=turn.tool_calls,
            executed=turn.executed,
            pending=len(turn.pending),
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            error=error.split(":", 1)[0] if error else None,
            duration_ms=round((self._clock() - t0) * 1000),
        )
        return result

    def _loop(self, turn: _Turn, emit: EventSink) -> TurnStop:
        for step in range(self.limits.max_steps + 1):
            if self._clock() >= turn.deadline:
                return TurnStop.TIMEOUT
            completion = self.provider.complete(
                self._history, system=self.system_prompt, tools=self._tools
            )
            turn.steps += 1
            turn.add_usage(completion.usage)
            turn.model = completion.model or turn.model
            # Ongewijzigd (incl. ProviderState): adapters eisen dat thinking/reasoning terugkomt.
            self._history.append(completion.message)

            shown = strip_think(completion.text)
            if shown:
                turn.text = shown
                emit(AssistantText(shown))

            calls = completion.tool_calls
            turn.tool_calls += len(calls)
            if not calls:
                return _MODEL_STOPS[completion.stop_reason]
            if completion.stop_reason is not StopReason.TOOL_USE:
                self._refuse_all(calls, "niet uitgevoerd: het antwoord van het model was afgekapt")
                return _MODEL_STOPS[completion.stop_reason]
            if step == self.limits.max_steps:
                self._refuse_all(
                    calls,
                    f"niet uitgevoerd: maximum van {self.limits.max_steps} stappen per beurt "
                    "bereikt; vraag de gebruiker of je verder mag",
                )
                return TurnStop.MAX_STEPS
            if turn.pending:
                self._refuse_all(
                    calls, "niet uitgevoerd: wacht eerst op de bevestiging door de gebruiker"
                )
                return TurnStop.AWAITING_CONFIRMATION

            self._run_calls(calls, turn, emit)
            if turn.timed_out:
                return TurnStop.TIMEOUT
        raise AssertionError("onbereikbaar: de laatste stap eindigt altijd")  # pragma: no cover

    def _run_calls(self, calls: tuple[ToolCall, ...], turn: _Turn, emit: EventSink) -> None:
        limit = self.limits.max_calls_per_step
        for i, call in enumerate(calls):
            if i >= limit:
                self._add_result(
                    call,
                    _error(
                        "too_many_calls", f"niet uitgevoerd: hoogstens {limit} tool-calls per stap"
                    ),
                )
            elif self._clock() >= turn.deadline:
                turn.timed_out = True
                self._add_result(
                    call,
                    _error("turn_timeout", "niet uitgevoerd: tijdslimiet van de beurt bereikt"),
                )
            else:
                self._execute(call, turn, emit)

    def _execute(self, call: ToolCall, turn: _Turn, emit: EventSink) -> None:
        emit(ToolCallStarted(call.id, call.name, call.arguments))
        if call.error is not None:
            view = _error(str(call.error), f"niet uitgevoerd: {_CALL_ERROR_MESSAGES[call.error]}")
        elif call.name in MODEL_HIDDEN_TOOLS:
            view = self._refuse_hidden(call)
        else:
            try:
                raw = self.runner.call(self.frontend, call.name, call.arguments)
            except Exception as exc:  # de runner hoort altijd een dict te geven
                log.exception("runner.call(%s) faalde onverwacht", call.name)
                raw = _error("internal_error", type(exc).__name__)
            turn.executed += 1
            view = self._model_view(raw, call, turn, emit)
        self._add_result(call, view)
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
        self, raw: dict[str, Any], call: ToolCall, turn: _Turn, emit: EventSink
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
        self._surfaced[pending_id] = pending
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
        self.runner.audit.write(
            frontend=self.frontend,
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

    def _refuse_all(self, calls: tuple[ToolCall, ...], message: str) -> None:
        for call in calls:
            self._add_result(call, _error("not_executed", message))

    def _add_result(self, call: ToolCall, view: dict[str, Any]) -> None:
        is_error = not view.get("ok") and view.get("error") != "confirmation_required"
        self._history.append(
            ToolResult(
                call_id=call.id,
                name=replay_name(call),
                content=self._serialize(view),
                is_error=is_error,
            )
        )

    def _serialize(self, view: dict[str, Any]) -> str:
        text = json.dumps(view, ensure_ascii=False, separators=(",", ":"), default=str)
        cap = self.limits.max_result_chars
        if len(text) <= cap:
            return text
        return (
            f"{text[:cap]}…[INGEKORT door PiToolbox: resultaat was {len(text)} tekens, "
            f"alleen de eerste {cap} staan hier]"
        )

    def _close_open_calls(self, message: str) -> None:
        """Geeft elke tool-call van het laatste assistant-bericht zonder resultaat een fout."""
        for idx in range(len(self._history) - 1, -1, -1):
            msg = self._history[idx]
            if isinstance(msg, AssistantMessage):
                answered = {
                    m.call_id for m in self._history[idx + 1 :] if isinstance(m, ToolResult)
                }
                for call in msg.tool_calls:
                    if call.id not in answered:
                        self._add_result(call, _error("not_executed", message))
                return

    # --- geschiedenis begrenzen ------------------------------------------------------------

    def _trim_history(self) -> None:
        """Laat de oudste hele beurten vallen tot de geschiedenis onder het budget zit.

        Alleen afknippen vlak vóór een `UserMessage`: een beurt bevat al zijn tool calls en de
        bijbehorende resultaten, dus de geschiedenis blijft geldig (geen resultaat zonder call).
        Draait vóór de nieuwe gebruikersbeurt erbij komt; die blijft dus altijd staan, ook als
        daarvoor alle eerdere beurten weg moeten. Het model krijgt een melding dat er iets weg is.
        """
        budget = self.limits.max_history_chars
        sizes = [_message_chars(m) for m in self._history]
        total = sum(sizes)
        if total <= budget:
            return
        starts = [i for i, m in enumerate(self._history) if isinstance(m, UserMessage)]
        cut = 0
        for start in [*starts[1:], len(self._history)]:
            cut = start
            if total - sum(sizes[:cut]) <= budget:
                break
        if cut == 0:
            return
        dropped_turns = sum(1 for i in starts if i < cut)
        del self._history[:cut]
        self._notes.append(
            note(
                f"{dropped_turns} oudere gespreksbeurt(en) zijn weggelaten om binnen de "
                "context te blijven; vraag de gebruiker als je die informatie nodig hebt."
            )
        )
        self._audit_event("agent_history_trimmed", dropped_turns=dropped_turns)

    # --- alleen voor de frontend (na een expliciete menselijke keuze) ---------------------

    def confirm(self, pending: PendingConfirmation, *, approved: bool) -> ConfirmOutcome:
        """Voert een bevestiging uit of wijst hem af. Alleen aanroepen op menselijke invoer.

        Gaat via `runner.call(self.frontend, "confirm_action", ...)`: dezelfde frontend-id als
        de call die de pending maakte, dus binding, TTL en eenmalig gebruik blijven gelden.
        """
        known = self._surfaced.pop(pending.pending_id, None)
        if known is None:
            outcome = ConfirmOutcome(ConfirmStatus.EXPIRED, pending)
        elif not approved:
            outcome = self._reject(known)
        else:
            raw = self.runner.call(self.frontend, CONFIRM_TOOL, {"pending_id": known.pending_id})
            if raw.get("ok"):
                status = ConfirmStatus.EXECUTED
            elif raw.get("error") == "unknown_pending":
                status = ConfirmStatus.EXPIRED
            elif raw.get("error") == "denied":
                status = ConfirmStatus.DENIED
            else:
                status = ConfirmStatus.FAILED
            outcome = ConfirmOutcome(status, known, raw)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def _reject(self, pending: PendingConfirmation) -> ConfirmOutcome:
        action = self.runner.ctx.pending.take(pending.pending_id, self.frontend)
        if action is None:
            return ConfirmOutcome(ConfirmStatus.EXPIRED, pending)
        self.runner.audit.write(
            frontend=self.frontend,
            tool=action.tool,
            risk=str(Risk.TRANSMIT),
            args=action.args.model_dump(mode="json"),
            decision="rejected",
            ok=False,
            error="rejected_by_user",
            result=None,
            duration_ms=0,
            pending_id=action.id,
            source="agent",
        )
        return ConfirmOutcome(ConfirmStatus.REJECTED, pending)

    def _confirm_note(self, outcome: ConfirmOutcome) -> str:
        what = f"{outcome.pending.tool}: {outcome.pending.summary}"
        if outcome.status is ConfirmStatus.REJECTED:
            return note(f"De gebruiker heeft afgewezen ({what}). Er is niets uitgevoerd.")
        if outcome.status is ConfirmStatus.EXPIRED:
            return note(f"Bevestiging verlopen of al gebruikt ({what}). Er is niets uitgevoerd.")
        result = self._serialize(outcome.result or {})
        if outcome.status is ConfirmStatus.DENIED:
            return note(f"Bevestigd, maar alsnog geweigerd door de policy ({what}): {result}")
        if outcome.status is ConfirmStatus.FAILED:
            return note(f"Bevestigd en uitgevoerd, maar met een fout ({what}): {result}")
        return note(f"De gebruiker heeft bevestigd en het is uitgevoerd ({what}): {result}")

    def emergency_stop(self) -> dict[str, Any]:
        """Noodstop op verzoek van de mens (`/stop`); het model hoort het in de volgende beurt."""
        raw = self.runner.call(self.frontend, EMERGENCY_STOP_TOOL, {})
        self._surfaced.clear()  # PendingStore is gewist; deze id's zijn waardeloos
        self._notes.append(
            note(f"De gebruiker heeft een noodstop uitgevoerd: {self._serialize(raw)}")
        )
        return raw

    # --- audit ----------------------------------------------------------------------------

    def _audit_event(self, event: str, **fields: Any) -> None:
        """Beurtregel in de audit-log; nooit berichtinhoud of sleutels."""
        try:
            self.runner.audit.write(
                event=event,
                frontend=self.frontend,
                session=self._session_id,
                turn=self._turns,
                **fields,
            )
        except OSError:
            log.exception("audit-regel %s kon niet worden geschreven", event)
        log.info("%s turn=%d %s", event, self._turns, fields)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
