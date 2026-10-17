"""Terminal-frontend voor de agent (entry point ``pitoolbox-agent``).

Een REPL: je typt een vraag, het model antwoordt en roept tools aan via de `ToolRunner`.
Tool-calls verschijnen compact (``→ naam {args}`` en ``← ok`` / ``← fout``).

Bevestigen: vraagt het model een transmit-actie binnen scope aan, dan toont deze frontend
na de beurt de samenvatting en vraagt ``Bevestigen? [j/N]``. Alleen ``j`` of ``ja`` telt als
ja; al het andere (ook leeg, Ctrl+C of Ctrl+D) is nee. Een "ja" in een gewoon chatbericht
bevestigt niets: alleen deze prompt roept ``confirm_action`` aan, met frontend-id
``agent-cli``. Is de PiToolbox-app op de Flipper verbonden (in dit proces, zie
``PITOOLBOX_FLIPPER_UI``), dan vraagt deze frontend niets en bevestig je op de Flipper: de
terminal wacht dan op OK/Back daar en toont de uitkomst (Ctrl+C = niet wachten; de uitkomst
volgt dan vóór je volgende invoer). Valt de app weg terwijl de bevestiging nog openstaat, dan
vraagt de terminal alsnog ``Bevestigen? [j/N]``. Het model hoort de uitkomst in zijn volgende
beurt, maar kan zelf nooit bevestigen.

Commando's: ``/stop`` (noodstop: captures stoppen, Flipper-actie afbreken, bevestigingen
wissen), ``/new`` (nieuw gesprek: de geschiedenis weg, open bevestigingen blijven),
``/help`` en ``/exit``.

Ctrl+C tijdens een beurt breekt alleen de beurt af: er volgt geen modelcall of tool meer, en
een openstaande bevestiging wordt afgewezen. Het is **geen** noodstop: een lopende capture
blijft lopen (gebruik ``/stop``). Een tool die op dat moment liep, kan halverwege gestopt
zijn. Ctrl+C of Ctrl+D aan de prompt sluit af.

Zonder LLM-configuratie (PITOOLBOX_LLM_PROVIDER/MODEL, ...) stopt het programma met de
melding van `LLMConfigError` en exitcode 2. ``--list-providers`` toont de geldige waarden van
PITOOLBOX_LLM_PROVIDER (presets en wire-formaten) en heeft geen configuratie nodig.
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
from pitoolbox.llm.presets import ALIASES, PRESETS
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

WAIT_POLL_S = 0.2  # wachten op de Flipper: zo vaak kijken

# Vervangbaar in tests.
_input = input
_clock = time.monotonic
_sleep = time.sleep

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
ON_FLIPPER_NOTICE = (
    "  De PiToolbox-app op de Flipper is verbonden: bevestig daar (OK = ja, Back = nee)."
)
WAIT_NOTICE = "  Wacht op de Flipper... (Ctrl+C = niet wachten)"
NO_WAIT_NOTICE = "  Niet meer gewacht; de uitkomst verschijnt hier zodra hij er is."
DETACHED_NOTICE = "  De Flipper-app is niet meer verbonden; bevestig dan hier."


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


def ask_yes_no(question: str) -> bool:
    """Alleen een expliciete 'j' of 'ja' is ja; al het andere (ook Ctrl+C/Ctrl+D) is nee."""
    try:
        answer = _input(question)
    except (EOFError, KeyboardInterrupt):
        _out()
        return False
    return answer.strip().lower() in YES


def _outcome_text(outcome: ConfirmOutcome) -> str:
    line = _CONFIRM_MESSAGES[outcome.status]
    if outcome.status in (ConfirmStatus.EXECUTED, ConfirmStatus.FAILED) and outcome.result:
        line += f" {_compact(outcome.result, 400)}"
    return line


def report_external(session: AgentSession) -> list[ConfirmOutcome]:
    """Toon wat er buiten de terminal met open bevestigingen gebeurde (Flipper, noodstop, TTL)."""
    outcomes = session.take_external()
    for outcome in outcomes:
        _out(f"Bevestiging {for_terminal(outcome.pending.tool)}: {_outcome_text(outcome)}")
    return outcomes


def _flipper_attached(session: AgentSession) -> bool:
    return session.runner.ctx.pending.physical_confirmer is not None


def _still_open(session: AgentSession, pending: PendingConfirmation) -> bool:
    """Nog open in deze sessie én in de PendingStore (dus niet net bevestigd of verlopen)."""
    pid = pending.pending_id
    if pid not in {p.pending_id for p in session.open_confirmations}:
        return False
    return session.runner.ctx.pending.get(pid) is not None


def _wait_on_flipper(session: AgentSession, pending: PendingConfirmation) -> ConfirmOutcome | None:
    """Wacht tot OK/Back op de Flipper (of TTL, noodstop). None: niet meer wachten (Ctrl+C),
    de app is weggevallen, of de uitkomst komt nog (dan toont `report_external` hem later)."""
    _out(WAIT_NOTICE)
    try:
        while True:
            for outcome in report_external(session):
                if outcome.pending.pending_id == pending.pending_id:
                    return outcome
            if not _still_open(session, pending) or not _flipper_attached(session):
                return None
            _sleep(WAIT_POLL_S)
    except KeyboardInterrupt:
        _out()
        _out(NO_WAIT_NOTICE)
        return None


def _confirm_one(session: AgentSession, pending: PendingConfirmation) -> ConfirmOutcome | None:
    if _flipper_attached(session):
        # "Flipper verplicht": hier vragen heeft geen zin, de runner zou weigeren.
        _out(ON_FLIPPER_NOTICE)
        outcome = _wait_on_flipper(session, pending)
        if outcome is not None or _flipper_attached(session) or not _still_open(session, pending):
            return outcome
        _out(DETACHED_NOTICE)  # de app viel weg terwijl hij nog openstond: de oude vraag
    approved = ask_yes_no(CONFIRM_PROMPT)
    outcome = session.confirm(pending, approved=approved)
    _out(_outcome_text(outcome))
    return outcome


def _show_pending(pending: PendingConfirmation) -> None:
    remaining = max(0, int(pending.remaining_s(_clock())))
    _out()
    _out(f"Actie: {for_terminal(pending.tool)}")
    _out(f"  {for_terminal(pending.summary)}")
    _out(f"  (verloopt over ongeveer {remaining} s)")


def handle_confirmations(session: AgentSession, result: TurnResult) -> list[ConfirmOutcome]:
    outcomes: list[ConfirmOutcome] = []
    for pending in result.pending:
        if result.stop is TurnStop.INTERRUPTED:
            outcomes.append(session.confirm(pending, approved=False))
            _out(f"Afgewezen (beurt afgebroken): {for_terminal(pending.summary)}")
            continue
        if not _still_open(session, pending):
            continue  # intussen al afgehandeld (bijv. op de Flipper); report_external meldt het
        _show_pending(pending)
        outcome = _confirm_one(session, pending)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def resolve_open(session: AgentSession) -> list[ConfirmOutcome]:
    """Vóór elke prompt: uitkomsten van buitenaf tonen, en een bevestiging die nog openstaat
    terwijl de Flipper-app niet (meer) verbonden is, alsnog hier vragen."""
    outcomes = report_external(session)
    if _flipper_attached(session):
        return outcomes
    for pending in session.open_confirmations:
        if _flipper_attached(session) or not _still_open(session, pending):
            continue
        _show_pending(pending)
        outcome = _confirm_one(session, pending)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def run_turn(session: AgentSession, text: str) -> TurnResult:
    result = session.run_turn(text, on_event=print_event)
    if result.stop is TurnStop.LLM_ERROR:
        retry = " Je bericht is niet verwerkt; probeer het opnieuw." if result.rolled_back else ""
        _out(f"Fout van het model ({for_terminal(result.error or '?')}).{retry}{RESET_HINT}")
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
        resolve_open(session)
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
        elif text == "/new":
            _out(f"Nieuw gesprek ({session.reset()} beurt(en) gewist).")
        elif text.startswith("/"):
            _out(f"Onbekend commando: {for_terminal(text)} (zie /help)")
        else:
            run_turn(session, text)


def list_providers() -> None:
    """Tabel van alle waarden voor PITOOLBOX_LLM_PROVIDER, zonder iets te verbinden."""
    rows = [("naam", "formaat", "base-URL", "sleutel")]
    for preset in PRESETS.values():
        rows.append(
            (
                preset.name,
                preset.kind.value,
                preset.base_url or "(zelf zetten: PITOOLBOX_LLM_BASE_URL)",
                "verplicht" if preset.requires_key else "optioneel",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        padded = [cell.ljust(width) for cell, width in zip(row[:3], widths, strict=True)]
        _out("  ".join([*padded, row[3]]))
    _out()
    _out("Aliassen: " + ", ".join(f"{alias} = {name}" for alias, name in ALIASES.items()))
    _out(
        "PITOOLBOX_LLM_MODEL is altijd verplicht (er is geen standaardmodel). "
        "PITOOLBOX_LLM_BASE_URL overschrijft de base-URL van een preset."
    )


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
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="toon de geldige waarden van PITOOLBOX_LLM_PROVIDER en stop",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list_providers:
        list_providers()
        return 0
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
    session: AgentSession | None = None
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
        if session is not None:
            session.close()
        provider.close()
        if runner is not None:
            runner.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
