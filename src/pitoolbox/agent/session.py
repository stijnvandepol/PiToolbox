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
  `PendingStore` blijven gelden. Is de Flipper-app verbonden ("Flipper verplicht"), dan
  weigert de runner dat (`ConfirmStatus.ON_FLIPPER`) en bevestigt de mens op de Flipper. De
  sessie luistert naar de uitkomsten van de runner (`subscribe_resolutions`) en hoort zo ook
  wat er buiten haar om met haar eigen pendings gebeurde (OK/Back op de Flipper, een noodstop
  elders, de TTL); `take_external()` verwerkt die (ook aan het begin van elke beurt). Het model
  kan daarmee nog steeds niets bevestigen: het hoort alleen de uitkomst. De uitkomst gaat als
  `[PiToolbox-melding]` mee in het volgende gebruikersbericht (niet als los bericht: sommige
  chat-templates eisen dat user en assistant elkaar afwisselen).
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

Indeling: de publieke typen staan in `types`, het uitvoeren van tool-calls in `_tool_calls`, de
administratie van open bevestigingen in `_confirmations` en het begrenzen van de geschiedenis in
`_history`. Deze module heeft de loop en is het publieke importpad voor alles.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pitoolbox.agent._confirmations import OpenConfirmations, confirm_status, reject
from pitoolbox.agent._history import history_cut
from pitoolbox.agent._tool_calls import (
    MODEL_HIDDEN_TOOLS,
    Cancelled,
    ToolCallExecutor,
    TurnState,
    check_cancel,
    model_tool_defs,
    serialize_view,
)
from pitoolbox.agent.prompt import NOTE_PREFIX, SYSTEM_PROMPT
from pitoolbox.agent.text import strip_think
from pitoolbox.agent.types import (
    AgentEvent,
    AgentLimits,
    AssistantText,
    ConfirmOutcome,
    ConfirmStatus,
    EventSink,
    PendingConfirmation,
    ToolCallFinished,
    ToolCallStarted,
    TurnResult,
    TurnStop,
)
from pitoolbox.core.tools.runner import (
    CONFIRM_ON_FLIPPER,
    CONFIRM_TOOL,
    PendingResolved,
    Resolution,
)
from pitoolbox.llm.errors import LLMError
from pitoolbox.llm.types import (
    Message,
    StopReason,
    ToolDef,
    Usage,
    UserMessage,
)

if TYPE_CHECKING:
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider

__all__ = [
    "MODEL_HIDDEN_TOOLS",
    "AgentEvent",
    "AgentLimits",
    "AgentSession",
    "AssistantText",
    "ConfirmOutcome",
    "ConfirmStatus",
    "EventSink",
    "PendingConfirmation",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnResult",
    "TurnStop",
    "model_tool_defs",
    "note",
]

log = logging.getLogger(__name__)

EMERGENCY_STOP_TOOL = "emergency_stop"

_MODEL_STOPS = {
    StopReason.END_TURN: TurnStop.END_TURN,
    StopReason.OTHER: TurnStop.END_TURN,
    StopReason.TOOL_USE: TurnStop.END_TURN,  # alleen zonder calls, dus feitelijk klaar
    StopReason.MAX_TOKENS: TurnStop.MAX_TOKENS,
    StopReason.REFUSAL: TurnStop.REFUSAL,
}


def note(text: str) -> str:
    return f"{NOTE_PREFIX} {text}"


class AgentSession:
    """Eén gesprek: geschiedenis, provider, runner en de bevestigingen die nog openstaan.

    Niet thread-safe: één frontend roept `run_turn`, `confirm`, `take_external` en
    `emergency_stop` na elkaar aan. Alleen de luisteraar voor uitkomsten
    (`OpenConfirmations.on_resolved`) draait in andere threads; die zet alleen iets in een eigen
    wachtrij. `close()` meldt hem af.
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
        self._tools = model_tool_defs(runner.allowed_specs())
        self._history: list[Message] = []
        self._notes: list[str] = []
        self._session_id = secrets.token_hex(4)
        self._turns = 0
        self._confirmations = OpenConfirmations()
        self._calls = ToolCallExecutor(runner, frontend, self.limits, clock, self._confirmations)
        self._unsubscribe: Callable[[], None] | None = runner.subscribe_resolutions(
            self._confirmations.on_resolved
        )

    def close(self) -> None:
        """Meldt de luisteraar voor uitkomsten af (bij het weggooien van de sessie)."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    @property
    def history(self) -> tuple[Message, ...]:
        return tuple(self._history)

    @property
    def tools(self) -> tuple[ToolDef, ...]:
        return self._tools

    @property
    def open_confirmations(self) -> tuple[PendingConfirmation, ...]:
        return self._confirmations.pending

    # --- een beurt ------------------------------------------------------------------------

    def run_turn(
        self,
        text: str,
        on_event: EventSink | None = None,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> TurnResult:
        """Eén gebruikersbeurt. `cancel()` waar: stoppen bij de volgende stap of tool-call
        (`TurnStop.INTERRUPTED`); een lopende modelcall of tool wordt niet onderbroken."""
        emit: EventSink = on_event or (lambda _e: None)
        self.take_external()  # wat er intussen buiten deze sessie gebeurde, hoort het model nu
        self._turns += 1
        t0 = self._clock()
        turn = TurnState(deadline=t0 + self.limits.turn_timeout_s, cancel=cancel)
        self._trim_history()
        start_len = len(self._history)
        notes, self._notes = self._notes, []
        self._history.append(UserMessage("\n".join([*notes, text]) if notes else text))
        self._audit_event("agent_turn_start", model=self.provider.model)

        error: str | None = None
        try:
            stop = self._loop(turn, emit)
        except (KeyboardInterrupt, Cancelled):
            stop = TurnStop.INTERRUPTED
        except LLMError as exc:
            stop = TurnStop.LLM_ERROR
            error = f"{exc.code}: {exc}"
            log.warning("LLM-fout in beurt %d: %s", self._turns, exc.code)

        rolled_back = self._settle_history(start_len, notes)
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
        self._audit_turn_end(turn, stop, error, t0)
        return result

    def _settle_history(self, start_len: int, notes: list[str]) -> bool:
        """Laat de geschiedenis geldig achter; True als de beurt is teruggedraaid."""
        if len(self._history) == start_len + 1:
            # Het model heeft nog niets gezegd: beurt terugdraaien, meldingen bewaren.
            del self._history[start_len:]
            self._notes = [*notes, *self._notes]
            return True
        self._calls.close_open_calls(self._history, "niet uitgevoerd: beurt afgebroken")
        return False

    def _audit_turn_end(
        self, turn: TurnState, stop: TurnStop, error: str | None, t0: float
    ) -> None:
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

    def _loop(self, turn: TurnState, emit: EventSink) -> TurnStop:
        for step in range(self.limits.max_steps + 1):
            check_cancel(turn)
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
                self._calls.refuse_all(
                    self._history,
                    calls,
                    "niet uitgevoerd: het antwoord van het model was afgekapt",
                )
                return _MODEL_STOPS[completion.stop_reason]
            if step == self.limits.max_steps:
                self._calls.refuse_all(
                    self._history,
                    calls,
                    f"niet uitgevoerd: maximum van {self.limits.max_steps} stappen per beurt "
                    "bereikt; vraag de gebruiker of je verder mag",
                )
                return TurnStop.MAX_STEPS
            if turn.pending:
                self._calls.refuse_all(
                    self._history,
                    calls,
                    "niet uitgevoerd: wacht eerst op de bevestiging door de gebruiker",
                )
                return TurnStop.AWAITING_CONFIRMATION

            self._calls.run(self._history, calls, turn, emit)
            if turn.timed_out:
                return TurnStop.TIMEOUT
        raise AssertionError("onbereikbaar: de laatste stap eindigt altijd")  # pragma: no cover

    # --- geschiedenis begrenzen ------------------------------------------------------------

    def _trim_history(self) -> None:
        """Laat de oudste hele beurten vallen tot de geschiedenis onder het budget zit.

        Alleen afknippen vlak vóór een `UserMessage`: een beurt bevat al zijn tool calls en de
        bijbehorende resultaten, dus de geschiedenis blijft geldig (geen resultaat zonder call).
        Draait vóór de nieuwe gebruikersbeurt erbij komt; die blijft dus altijd staan, ook als
        daarvoor alle eerdere beurten weg moeten. Het model krijgt een melding dat er iets weg is.
        """
        cut = history_cut(self._history, self.limits.max_history_chars)
        if cut == 0:
            return
        dropped_turns = sum(1 for m in self._history[:cut] if isinstance(m, UserMessage))
        del self._history[:cut]
        self._notes.append(
            note(
                f"{dropped_turns} oudere gespreksbeurt(en) zijn weggelaten om binnen de "
                "context te blijven; vraag de gebruiker als je die informatie nodig hebt."
            )
        )
        self._audit_event("agent_history_trimmed", dropped_turns=dropped_turns)

    def reset(self) -> int:
        """Begin een nieuw gesprek: de hele geschiedenis weg (op verzoek van de gebruiker).

        Uitweg als de provider een item uit de geschiedenis blijft weigeren (bijv. een
        reasoning-item dat hij niet meer kent). Open bevestigingen en nog niet gemelde
        uitkomsten blijven staan: die horen bij acties, niet bij het gesprek. Geeft het
        aantal weggegooide beurten.
        """
        dropped_turns = sum(1 for m in self._history if isinstance(m, UserMessage))
        self._history.clear()
        self._audit_event("agent_history_reset", dropped_turns=dropped_turns)
        return dropped_turns

    # --- alleen voor de frontend (na een expliciete menselijke keuze) ---------------------

    def confirm(self, pending: PendingConfirmation, *, approved: bool) -> ConfirmOutcome:
        """Voert een bevestiging uit of wijst hem af. Alleen aanroepen op menselijke invoer.

        Gaat via `runner.call(self.frontend, "confirm_action", ...)`: dezelfde frontend-id als
        de call die de pending maakte, dus binding, TTL en eenmalig gebruik blijven gelden.
        """
        known = self._confirmations.pop(pending.pending_id)
        if known is None:
            outcome = ConfirmOutcome(ConfirmStatus.EXPIRED, pending)
        elif not approved:
            outcome = reject(self.runner, self.frontend, known)
        else:
            raw = self.runner.call(self.frontend, CONFIRM_TOOL, {"pending_id": known.pending_id})
            if raw.get("error") == CONFIRM_ON_FLIPPER:
                # Niets gebeurd: de pending staat nog (de mens bevestigt op de Flipper).
                self._confirmations.add(known)
                return ConfirmOutcome(ConfirmStatus.ON_FLIPPER, known, raw)
            outcome = ConfirmOutcome(confirm_status(raw), known, raw)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def take_external(self) -> list[ConfirmOutcome]:
        """Verwerk wat er buiten deze sessie met haar open bevestigingen gebeurde.

        Uitgevoerd of afgewezen op de Flipper, gewist door een noodstop elders, of verlopen. Per
        stuk een melding voor het model (zoals bij `confirm`); de frontend kan de uitkomsten
        tonen. Eerst wordt de `PendingStore` opgeschoond, zodat een verlopen TTL ook meetelt.
        """
        if self._confirmations.pending:
            self.runner.ctx.pending.list()  # verlopen pendings melden (lui opgeschoond)
        outcomes: list[ConfirmOutcome] = []
        for event in self._confirmations.take_resolved():
            outcome = self._apply_resolution(event)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _apply_resolution(self, event: PendingResolved) -> ConfirmOutcome | None:
        if event.resolution is Resolution.CONFIRMED:
            return self.external_outcome(event.pending_id, approved=True, result=event.result)
        if event.resolution is Resolution.REJECTED:
            return self.external_outcome(event.pending_id, approved=False, result={"ok": True})
        known = self._confirmations.drop(event.pending_id)
        if known is None:
            return None
        status = (
            ConfirmStatus.CLEARED
            if event.resolution is Resolution.CLEARED
            else ConfirmStatus.EXPIRED
        )
        outcome = ConfirmOutcome(status, known, None)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def external_outcome(
        self, pending_id: str, *, approved: bool, result: dict[str, Any] | None
    ) -> ConfirmOutcome | None:
        """Een pending van deze sessie is buiten de sessie afgehandeld (OK/Back op de Flipper).

        Werkt alleen de eigen administratie bij en geeft het model in de volgende beurt een
        melding, net als `confirm`. None als deze sessie de pending niet (meer) kent.
        """
        known = self._confirmations.pop(pending_id)
        if known is None:
            return None
        if not approved:
            status = (
                ConfirmStatus.REJECTED
                if result is not None and result.get("ok")
                else ConfirmStatus.EXPIRED
            )
            outcome = ConfirmOutcome(status, known, None)
        else:
            outcome = ConfirmOutcome(confirm_status(result or {}), known, result)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def _confirm_note(self, outcome: ConfirmOutcome) -> str:
        what = f"{outcome.pending.tool}: {outcome.pending.summary}"
        if outcome.status is ConfirmStatus.REJECTED:
            return note(f"De gebruiker heeft afgewezen ({what}). Er is niets uitgevoerd.")
        if outcome.status is ConfirmStatus.EXPIRED:
            return note(f"Bevestiging verlopen of al gebruikt ({what}). Er is niets uitgevoerd.")
        if outcome.status is ConfirmStatus.CLEARED:
            return note(f"Bevestiging gewist door een noodstop ({what}). Er is niets uitgevoerd.")
        result = serialize_view(outcome.result or {}, self.limits.max_result_chars)
        if outcome.status is ConfirmStatus.DENIED:
            return note(f"Bevestigd, maar alsnog geweigerd door de policy ({what}): {result}")
        if outcome.status is ConfirmStatus.FAILED:
            return note(f"Bevestigd en uitgevoerd, maar met een fout ({what}): {result}")
        return note(f"De gebruiker heeft bevestigd en het is uitgevoerd ({what}): {result}")

    def emergency_stop(self) -> dict[str, Any]:
        """Noodstop op verzoek van de mens (`/stop`); het model hoort het in de volgende beurt."""
        raw = self.runner.call(self.frontend, EMERGENCY_STOP_TOOL, {})
        self._confirmations.clear()  # PendingStore is gewist; deze id's zijn waardeloos
        self._notes.append(
            note(
                "De gebruiker heeft een noodstop uitgevoerd: "
                f"{serialize_view(raw, self.limits.max_result_chars)}"
            )
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
