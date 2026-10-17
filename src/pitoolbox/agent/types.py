"""Publieke typen van de agent-loop: grenzen, stopredenen, events en uitkomsten.

Los van `session` zodat frontends en de hulpmodules ze kunnen gebruiken zonder de loop zelf.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pitoolbox.llm.types import Usage

if TYPE_CHECKING:
    from pitoolbox.config import Settings


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
    INTERRUPTED = "interrupted"  # Ctrl+C of cancel() van de frontend


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
    # Niet uitgevoerd: de Flipper-app is verbonden, dus alleen OK op de Flipper bevestigt. De
    # bevestiging blijft openstaan (na loskoppelen kan deze frontend het opnieuw proberen).
    ON_FLIPPER = "on_flipper"
    CLEARED = "cleared"  # gewist door een noodstop (elders); niets uitgevoerd


@dataclass(frozen=True)
class ConfirmOutcome:
    status: ConfirmStatus
    pending: PendingConfirmation
    result: dict[str, Any] | None = None  # ruwe uitkomst van de runner
