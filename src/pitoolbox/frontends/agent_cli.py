"""Terminal-frontend voor de agent (entry point ``pitoolbox-agent``).

Een REPL: je typt een vraag, het model antwoordt en roept tools aan via de `ToolRunner`.
Tool-calls verschijnen compact (``→ naam {args}`` en ``← ok`` / ``← fout``).

Bevestigen: vraagt het model een transmit-actie binnen scope aan, dan toont deze frontend
na de beurt de samenvatting en vraagt ``Bevestigen? [j/N]``. Alleen ``j`` of ``ja`` telt als
ja; al het andere (ook leeg, Ctrl+C of Ctrl+D) is nee. Een "ja" in een gewoon chatbericht
bevestigt niets: alleen deze prompt roept ``confirm_action`` aan, met frontend-id
``agent-cli``. Is de PiToolbox-app op de Flipper verbonden (in dit proces, zie
``PITOOLBOX_FLIPPER_UI``), dan vraagt deze frontend niets en bevestig je op de Flipper.

Commando's: ``/stop`` (noodstop: captures stoppen, Flipper-actie afbreken, bevestigingen
wissen), ``/help`` en ``/exit``.

Ctrl+C tijdens een beurt breekt alleen de beurt af: er volgt geen modelcall of tool meer, en
een openstaande bevestiging wordt afgewezen. Het is **geen** noodstop: een lopende capture
blijft lopen (gebruik ``/stop``). Een tool die op dat moment liep, kan halverwege gestopt
zijn. Ctrl+C of Ctrl+D aan de prompt sluit af.

Zonder LLM-configuratie (PITOOLBOX_LLM_PROVIDER/MODEL, ...) stopt het programma met de
melding van `LLMConfigError` en exitcode 2.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pitoolbox.agent.session import (
    AgentEvent,
    AgentLimits,
    AgentSession,
    AssistantText,
    ConfirmOutcome,
    ConfirmStatus,
    PendingConfirmation,
    ToolCallFinished,
    ToolCallStarted,
    TurnResult,
    TurnStop,
)
from pitoolbox.agent.text import for_terminal
from pitoolbox.app import build_runner
from pitoolbox.config import Settings
from pitoolbox.llm.errors import LLMConfigError
from pitoolbox.llm.providers import build_provider

if TYPE_CHECKING:
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider

log = logging.getLogger(__name__)

FRONTEND = "agent-cli"
PROMPT = "> "
CONFIRM_PROMPT = "Bevestigen? [j/N] "
YES = frozenset({"j", "ja"})
ARGS_PREVIEW = 160

# Vervangbaar in tests.
_input = input
_clock = time.monotonic

HELP = """\
Commando's:
  /stop   noodstop: captures stoppen, Flipper-actie afbreken, bevestigingen wissen
  /help   deze hulp
  /exit   afsluiten (ook Ctrl+D)
Ctrl+C tijdens een beurt breekt alleen de beurt af (geen noodstop)."""

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
}
ON_FLIPPER_NOTICE = (
    "  De PiToolbox-app op de Flipper is verbonden: bevestig daar (OK = ja, Back = nee)."
)


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _compact(value: Any, limit: int = ARGS_PREVIEW) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    text = for_terminal(text)
    return text if len(text) <= limit else text[:limit] + "…"


def print_event(event: AgentEvent) -> None:
    if isinstance(event, AssistantText):
        _out(for_terminal(event.text))
    elif isinstance(event, ToolCallStarted):
        _out(f"  → {for_terminal(event.name) or '?'} {_compact(event.arguments or {})}")
    elif isinstance(event, ToolCallFinished):
        if event.ok:
            _out("  ← ok")
        elif event.error == "confirmation_required":
            _out("  ← wacht op jouw bevestiging")
        else:
            detail = for_terminal(event.message or "")[:ARGS_PREVIEW]
            _out(f"  ← fout: {for_terminal(event.error or '?')}{': ' + detail if detail else ''}")
    elif isinstance(event, PendingConfirmation):
        _out(f"  ! bevestiging nodig: {for_terminal(event.summary)}")


def ask_yes_no(question: str) -> bool:
    """Alleen een expliciete 'j' of 'ja' is ja; al het andere (ook Ctrl+C/Ctrl+D) is nee."""
    try:
        answer = _input(question)
    except (EOFError, KeyboardInterrupt):
        _out()
        return False
    return answer.strip().lower() in YES


def handle_confirmations(session: AgentSession, result: TurnResult) -> list[ConfirmOutcome]:
    outcomes: list[ConfirmOutcome] = []
    for pending in result.pending:
        if result.stop is TurnStop.INTERRUPTED:
            outcomes.append(session.confirm(pending, approved=False))
            _out(f"Afgewezen (beurt afgebroken): {for_terminal(pending.summary)}")
            continue
        remaining = max(0, int(pending.remaining_s(_clock())))
        _out()
        _out(f"Actie: {for_terminal(pending.tool)}")
        _out(f"  {for_terminal(pending.summary)}")
        _out(f"  (verloopt over ongeveer {remaining} s)")
        if session.runner.ctx.pending.physical_confirmer is not None:
            # "Flipper verplicht": hier vragen heeft geen zin, de runner zou weigeren.
            _out(ON_FLIPPER_NOTICE)
            continue
        approved = ask_yes_no(CONFIRM_PROMPT)
        outcome = session.confirm(pending, approved=approved)
        outcomes.append(outcome)
        line = _CONFIRM_MESSAGES[outcome.status]
        if outcome.status in (ConfirmStatus.EXECUTED, ConfirmStatus.FAILED) and outcome.result:
            line += f" {_compact(outcome.result, 400)}"
        _out(line)
    return outcomes


def run_turn(session: AgentSession, text: str) -> TurnResult:
    result = session.run_turn(text, on_event=print_event)
    if result.stop is TurnStop.LLM_ERROR:
        retry = " Je bericht is niet verwerkt; probeer het opnieuw." if result.rolled_back else ""
        _out(f"Fout van het model ({for_terminal(result.error or '?')}).{retry}")
    elif (notice := _STOP_NOTICES.get(result.stop)) is not None:
        _out(notice)
    handle_confirmations(session, result)
    return result


def emergency_stop(session: AgentSession) -> None:
    raw = session.emergency_stop()
    if raw.get("ok"):
        _out(f"Noodstop uitgevoerd: {_compact(raw.get('result'), 400)}")
    else:
        _out(f"Noodstop gaf een fout: {_compact(raw, 400)}")


def repl(session: AgentSession) -> int:
    while True:
        try:
            line = _input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            _out()
            return 0
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return 0
        if text == "/help":
            _out(HELP)
        elif text == "/stop":
            emergency_stop(session)
        elif text.startswith("/"):
            _out(f"Onbekend commando: {for_terminal(text)} (zie /help)")
        else:
            run_turn(session, text)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitoolbox-agent",
        description="PiToolbox-agent in de terminal (LLM uit PITOOLBOX_LLM_*)",
    )
    parser.add_argument(
        "--scope", type=Path, help="pad naar scope.yaml (overschrijft PITOOLBOX_SCOPE_FILE)"
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        help="pad naar het audit-logbestand (overschrijft PITOOLBOX_AUDIT_LOG)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="logging op INFO (stderr)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    updates: dict[str, Any] = {}
    if args.scope is not None:
        updates["scope_file"] = args.scope.expanduser()
    if args.audit_log is not None:
        updates["audit_log"] = args.audit_log.expanduser()
    try:
        settings = Settings.from_env().model_copy(update=updates)
        limits = AgentLimits.from_settings(settings)
        provider: LLMProvider = build_provider(settings)
    except (LLMConfigError, ValueError) as exc:
        # LLMConfigError: geen of ongeldige provider; ValueError: ongeldige PITOOLBOX_*-waarde.
        _err(f"Fout: {exc}")
        return 2

    from pitoolbox.frontends.flipper_ui.bridge import start_from_settings

    runner: ToolRunner | None = None
    bridge = None
    try:
        runner = build_runner(settings)
        session = AgentSession(provider, runner, frontend=FRONTEND, limits=limits)
        # PITOOLBOX_FLIPPER_UI=on: de Flipper-app wordt in dit proces bediend. Draai de bridge
        # nooit ook nog in pitoolbox-mcp: maar één proces mag de Flipper bezitten.
        bridge = start_from_settings(runner, settings)
        _out(
            f"PiToolbox-agent: model {for_terminal(provider.model)}, "
            f"{len(session.tools)} tools. /help voor commando's, /exit om te stoppen."
        )
        return repl(session)
    finally:
        if bridge is not None:
            bridge.stop()
        provider.close()
        if runner is not None:
            runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
