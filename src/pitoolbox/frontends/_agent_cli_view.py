"""Weergave van `pitoolbox-agent` in de terminal: events, meldingen en uitkomsten als tekst.

Alleen opmaak; vragen en bevestigen doet `agent_cli`. Alles wat van het model of een tool komt,
gaat door `for_terminal` voordat het op het scherm komt.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pitoolbox.agent.session import (
    AgentEvent,
    AssistantText,
    ConfirmOutcome,
    ConfirmStatus,
    PendingConfirmation,
    ToolCallFinished,
    ToolCallStarted,
    TurnStop,
)
from pitoolbox.agent.text import for_terminal

ARGS_PREVIEW = 160

HELP = """\
Commando's:
  /stop   noodstop: captures stoppen, Flipper-actie afbreken, bevestigingen wissen
  /new    nieuw gesprek (geschiedenis weg; open bevestigingen blijven staan)
  /help   deze hulp
  /exit   afsluiten (ook Ctrl+D)
Ctrl+C tijdens een beurt breekt alleen de beurt af (geen noodstop)."""

RESET_HINT = " Blijft dit gebeuren, typ dan /new (nieuw gesprek)."

_STOP_NOTICES = {
    TurnStop.MAX_TOKENS: "(antwoord afgekapt: maximum aantal tokens bereikt)",
    TurnStop.REFUSAL: "(het model of een contentfilter weigerde)",
    TurnStop.MAX_STEPS: "(gestopt: maximum aantal stappen per beurt bereikt; typ 'ga door')",
    TurnStop.TIMEOUT: "(gestopt: tijdslimiet van de beurt bereikt)",
    TurnStop.AWAITING_CONFIRMATION: "(gestopt: eerst de bevestiging hieronder)",
    TurnStop.INTERRUPTED: "(beurt afgebroken; dit is geen noodstop, gebruik /stop)",
}

_CONFIRM_MESSAGES = {
    ConfirmStatus.EXECUTED: "Uitgevoerd.",
    ConfirmStatus.FAILED: "Uitgevoerd, maar met een fout.",
    ConfirmStatus.DENIED: "Alsnog geweigerd door de policy (scope gewijzigd?). Niets verzonden.",
    ConfirmStatus.EXPIRED: "Bevestiging verlopen of al gebruikt. Niets verzonden.",
    ConfirmStatus.REJECTED: "Afgewezen. Niets verzonden.",
    ConfirmStatus.ON_FLIPPER: (
        "Niet uitgevoerd: de PiToolbox-app op de Flipper is verbonden. "
        "Bevestig daar (OK = ja, Back = nee)."
    ),
    ConfirmStatus.CLEARED: "Gewist door een noodstop. Niets verzonden.",
}


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _compact(value: Any, limit: int = ARGS_PREVIEW) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    text = for_terminal(text)
    return text if len(text) <= limit else text[:limit] + "…"


def print_event(event: AgentEvent) -> None:
    match event:
        case AssistantText():
            _out(for_terminal(event.text))
        case ToolCallStarted():
            args = (
                "<ongeldige argumenten>" if event.arguments is None else _compact(event.arguments)
            )
            _out(f"  → {for_terminal(event.name) or '?'} {args}")
        case ToolCallFinished():
            _out(_finished_line(event))
        case PendingConfirmation():
            _out(f"  ! bevestiging nodig: {for_terminal(event.summary)}")


def _finished_line(event: ToolCallFinished) -> str:
    if event.ok:
        return "  ← ok"
    if event.error == "confirmation_required":
        return "  ← wacht op jouw bevestiging"
    detail = for_terminal(event.message or "")[:ARGS_PREVIEW]
    return f"  ← fout: {for_terminal(event.error or '?')}{': ' + detail if detail else ''}"


def _outcome_text(outcome: ConfirmOutcome) -> str:
    line = _CONFIRM_MESSAGES[outcome.status]
    if outcome.status in (ConfirmStatus.EXECUTED, ConfirmStatus.FAILED) and outcome.result:
        line += f" {_compact(outcome.result, 400)}"
    return line
