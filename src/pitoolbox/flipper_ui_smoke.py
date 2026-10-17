"""``ui-test``, subcommando van ``pitoolbox-flipper`` (flipper_ui_cli.py): de rooktest.

De app starten via de echte bridge, HELLO/WELCOME, een STATUS van de ``status``-tool end-to-end
afleveren, de app netjes sluiten; met tijden per stap. Er wordt niets uitgezonden: de runner
laat alleen ``status`` toe en heeft een lege scope.

``--confirm-check``: daarna twee test-vragen op de Flipper ("Flipper verplicht"). Vraag 1: eerst
moet ``confirm_action`` uit een chat geweigerd worden (``confirm_on_flipper``), dan drukt de
mens OK. Vraag 2: de mens drukt Back. De test-actie is ``ir_send`` naar een apparaat dat nergens
in scope staat; ook na OK weigert de policy hem bij de nieuwe controle, twee keer: de allowlist
(alleen ``status``) en de lege scope. Er kan dus niets uitgezonden worden.

``--confirm-check --auto``: de Pi drukt die knoppen zelf, op afstand via de GUI-RPC
(`FlipperDevice.gui_press`), pas nadat de bridge CONFIRM_PROMPT voor die vraag meldde en de
vraag gewapend is. De tijdlijn zegt dat er op afstand gedrukt is. Dat mag alleen hier, in deze
testopzet: met invoer op afstand kan de Pi zelf "Flipper verplicht" bevestigen.
"""

from __future__ import annotations

import argparse
import queue
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import flipper_ui_harness as harness
from .config import Settings
from .core.errors import ToolboxError
from .core.tools.flipper_tools import IrSendArgs
from .core.tools.runner import (
    CONFIRM_ON_FLIPPER,
    CONFIRM_TOOL,
    PHYSICAL_SOURCE,
    PendingResolved,
    Resolution,
)
from .flipper_ui_harness import (
    _LOCKED_HELP,
    StepFailed,
    _bring_up,
    _empty_scope_dir,
    _err,
    _Events,
    _make_bridge,
    _out,
    _runner_settings,
    _Timeline,
    _wait_delivered,
    _wait_loader_free,
)

if TYPE_CHECKING:
    from .core.devices.flipper import FlipperDevice
    from .core.tools.runner import ToolRunner
    from .frontends.flipper_ui.bridge import FlipperUiBridge

# --confirm-check: de test-actie. Het apparaat staat in geen enkele scope (de runner heeft een
# lege), en ir_send staat niet in de allowlist van de test: OK op de Flipper zendt nooit iets.
CHECK_TOOL = "ir_send"
CHECK_DEVICE = "pitoolbox_confirm_check"
CHECK_BUTTON = "Power"
CHECK_FRONTEND = "ui-test-chat"  # doet zich voor als chat, om "Flipper verplicht" te testen
# De pending leeft zo veel langer dan de wachttijd: de test geeft op vóór de TTL verloopt.
CHECK_TTL_MARGIN_S = 10
CHECK_STEPS_TOTAL = 2


@dataclass(frozen=True)
class _CheckStep:
    number: int
    button: str  # wat de mens moet drukken: "OK" of "Back"
    expected: Resolution
    try_chat_first: bool  # eerst confirm_action uit de chat proberen (moet geweigerd worden)


_CHECK_STEPS = (
    _CheckStep(1, "OK", Resolution.CONFIRMED, try_chat_first=True),
    _CheckStep(2, "Back", Resolution.REJECTED, try_chat_first=False),
)

_NO_ANSWER_HELP = (
    "Kijk op de Flipper: staat de vraag 'Test {number}/{total}' in beeld? Zo niet: staat de "
    "PiToolbox-app nog open, zonder 'Verbinding met Pi verbroken'? Drukte je wel en zie je in "
    "Status 'Bevestiging: ja gestuurd' of 'nee gestuurd', dan kwam het antwoord niet aan bij "
    "de Pi."
)


def cmd_ui_test(args: argparse.Namespace, settings: Settings) -> int:
    if args.auto and not args.confirm_check:
        _err("--auto werkt alleen samen met --confirm-check.")
        return 2
    with _empty_scope_dir() as scope_dir:
        run_settings = _runner_settings(settings, args, args.transport, scope_dir)
        if args.confirm_check > 0:
            ttl = int(args.confirm_check) + CHECK_TTL_MARGIN_S
            run_settings = run_settings.model_copy(update={"confirm_ttl_s": ttl})
        return _run_ui_test(args, run_settings)


def _run_ui_test(args: argparse.Namespace, run_settings: Settings) -> int:
    runner = harness._make_runner(run_settings)
    device: FlipperDevice = runner.ctx.flipper
    events = _Events()
    bridge: FlipperUiBridge | None = None
    timeline = _Timeline(harness._clock())
    _out(f"Rooktest PiToolbox-app ({run_settings.flipper_ui_app}); er wordt niets uitgezonden.")
    try:
        info = device.system_info()
        timeline.mark("verbinden + system_info", harness._clock())
        kind = device.status().get("transport_kind") or "?"
        _out(f"Verbonden via {kind}; firmware {info.get('firmware_version', '?')}.")
        if device.app_lock_status():
            _err(_LOCKED_HELP)
            return 1
        bridge = _make_bridge(runner, run_settings.flipper_ui_app, events.put)
        bridge.start()
        _bring_up(bridge, events, timeline)
        status = _wait_delivered(events, {"Status"}, harness.TIMING.step_s)
        if status is None:
            raise StepFailed("STATUS (status-tool via de runner) kwam niet aan bij de app")
        timeline.mark("STATUS afgeleverd (status-tool -> app)", status.at)
        if args.wait_action > 0:
            _wait_for_action(events, timeline, args.wait_action)
        if args.confirm_check > 0:
            _confirm_check(_Check(runner, events, timeline, args.confirm_check, args.auto))
        bridge.stop()
        bridge = None
        if device.rpc_app_state != "off" or not _wait_loader_free(device, harness.TIMING.exit_s):
            raise StepFailed("de app sloot niet netjes (loader bleef bezet)")
        timeline.mark("app gesloten, loader vrij", harness._clock())
    except StepFailed as exc:
        _err(f"Rooktest MISLUKT: {exc}")
        if timeline.steps:
            _err("Wel gelukt:\n" + timeline.render())
        tail = events.tail(timeline.started)
        if tail:
            _err("Laatste gebeurtenissen van de bridge:\n" + "\n".join(tail))
        return 1
    finally:
        if bridge is not None:
            bridge.stop()
        device.close()
    _out("Rooktest geslaagd:")
    _out(timeline.render())
    return 0


def _wait_for_action(events: _Events, timeline: _Timeline, wait_s: float) -> None:
    """--wait-action: de mens kiest op de Flipper 'Status'; het antwoord moet aankomen."""
    _out(
        f"Kies nu op de Flipper: Snelle acties > Status (binnen {wait_s:g} s). "
        "Andere acties weigert deze test."
    )
    answer = _wait_delivered(events, {"ResultChunk", "Error"}, wait_s)
    if answer is None:
        raise StepFailed("geen actie van de Flipper ontvangen (of het antwoord kwam niet aan)")
    if answer.detail == "Error":
        raise StepFailed("de app kreeg een foutmelding terug (andere actie dan Status gekozen?)")
    timeline.mark("actie Status: antwoord afgeleverd", answer.at)


# --- --confirm-check -------------------------------------------------------------------------


class _Resolutions:
    """Uitkomsten van pendings (`runner.subscribe_resolutions`), met hun aankomsttijd.

    `put` draait soms onder de lock van de PendingStore: alleen in de wachtrij zetten.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[PendingResolved, float]] = queue.Queue()

    def put(self, event: PendingResolved) -> None:
        self._queue.put_nowait((event, harness._clock()))

    def wait(self, pending_id: str, timeout: float) -> tuple[PendingResolved, float] | None:
        deadline = harness._clock() + timeout
        while True:
            remaining = deadline - harness._clock()
            if remaining <= 0:
                return None
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if item[0].pending_id == pending_id:
                return item


@dataclass(frozen=True)
class _Check:
    runner: ToolRunner
    events: _Events
    timeline: _Timeline
    wait_s: float
    auto: bool = False  # --auto: de Pi drukt de knoppen op afstand


def _confirm_check(check: _Check) -> None:
    """--confirm-check: OK en Back op twee test-vragen; zie de module-docstring."""
    _out(
        f"Bevestigingstest: {CHECK_STEPS_TOTAL} test-vragen op de Flipper. Er wordt niets "
        "uitgezonden: de test-actie staat niet in de allowlist en niet in de (lege) scope."
    )
    resolutions = _Resolutions()
    unsubscribe = check.runner.subscribe_resolutions(resolutions.put)
    try:
        for step in _CHECK_STEPS:
            _check_step(check, resolutions, step)
    finally:
        unsubscribe()


def _check_step(check: _Check, resolutions: _Resolutions, step: _CheckStep) -> None:
    store = check.runner.ctx.pending
    check.events.reset()
    summary = (
        f"Test {step.number}/{CHECK_STEPS_TOTAL}: druk {step.button}. Er wordt niets uitgezonden."
    )
    args = IrSendArgs(device=CHECK_DEVICE, button=CHECK_BUTTON)
    pending = store.create(CHECK_TOOL, args, CHECK_FRONTEND, summary)
    try:
        prompt = _wait_delivered(check.events, {"ConfirmPrompt"}, harness.TIMING.step_s)
        if prompt is None:
            raise StepFailed(f"vraag {step.number} kwam niet aan op de Flipper (CONFIRM_PROMPT)")
        check.timeline.mark(f"vraag {step.number} op de Flipper (CONFIRM_PROMPT)", prompt.at)
        if step.try_chat_first:
            _check_chat_refused(check, pending.id)
        if check.auto:
            _press_remotely(check, step, prompt.at)
        else:
            _out(
                f"Vraag {step.number}/{CHECK_STEPS_TOTAL}: druk nu {step.button} op de Flipper "
                f"(binnen {check.wait_s:g} s)."
            )
        found = resolutions.wait(pending.id, check.wait_s)
        if found is None:
            raise StepFailed(
                f"geen antwoord op vraag {step.number} binnen {check.wait_s:g} s. "
                + _NO_ANSWER_HELP.format(number=step.number, total=CHECK_STEPS_TOTAL)
            )
        check.timeline.mark(_verify_resolution(step, found[0]), found[1])
    finally:
        store.discard(pending.id, CHECK_FRONTEND)  # na een fout: de vraag niet laten staan
    closed = _wait_delivered(check.events, {"ConfirmClosed"}, harness.TIMING.step_s)
    if closed is None:
        raise StepFailed(f"de Flipper kreeg na vraag {step.number} geen CONFIRM_CLOSED")
    check.timeline.mark(f"vraag {step.number} gesloten (CONFIRM_CLOSED)", closed.at)


def _press_remotely(check: _Check, step: _CheckStep, shown_at: float) -> None:
    """--auto: de knop van deze stap op afstand drukken. Pas `auto_press_s` na CONFIRM_PROMPT:
    eerder telt OK op de Flipper niet (PTB_CONFIRM_ARM_MS in ptb_confirm_view.h)."""
    wait = shown_at + harness.TIMING.auto_press_s - harness._clock()
    if wait > 0:
        harness._sleep(wait)
    _out(f"Vraag {step.number}/{CHECK_STEPS_TOTAL}: de Pi drukt {step.button} op afstand.")
    # Tijd van vóór het drukken: de app reageert al terwijl gui_press nog bezig is (RELEASE),
    # dus een tijd van erna zou ná de bevestiging liggen en een negatieve stap geven.
    pressed_at = harness._clock()
    try:
        check.runner.ctx.flipper.gui_press(step.button.lower())
    except ToolboxError as exc:  # ook RemoteInputRefused (een DeviceError)
        raise StepFailed(
            f"{step.button} op afstand drukken mislukte bij vraag {step.number}: {exc}"
        ) from exc
    check.timeline.mark(f"{step.button} op afstand gedrukt (vraag {step.number})", pressed_at)


def _check_chat_refused(check: _Check, pending_id: str) -> None:
    """Zolang de app verbonden is, moet confirm_action uit een chat geweigerd worden."""
    raw = check.runner.call(CHECK_FRONTEND, CONFIRM_TOOL, {"pending_id": pending_id})
    if raw.get("error") != CONFIRM_ON_FLIPPER:
        raise StepFailed(
            "'Flipper verplicht' werkt niet: confirm_action uit de chat gaf "
            f"{raw.get('error')!r} in plaats van {CONFIRM_ON_FLIPPER!r}. Er is niets "
            "uitgezonden (de policy weigert de test-actie)."
        )
    check.timeline.mark("chat-bevestiging geweigerd (Flipper verplicht)", harness._clock())


def _verify_resolution(step: _CheckStep, event: PendingResolved) -> str:
    """Klopt de uitkomst bij de knop die de mens moest drukken? Geeft het label voor de
    tijdlijn, of StepFailed met wat er misging."""
    pressed = {Resolution.CONFIRMED: "OK", Resolution.REJECTED: "Back"}.get(event.resolution)
    if pressed is None:
        what = "verliep" if event.resolution is Resolution.EXPIRED else "werd gewist (noodstop?)"
        raise StepFailed(f"vraag {step.number} {what} zonder antwoord van de Flipper")
    if event.resolution is not step.expected:
        raise StepFailed(
            f"op vraag {step.number} kwam {pressed} binnen, verwacht was {step.button}. "
            "Er is niets uitgezonden."
        )
    if event.resolution is Resolution.REJECTED:
        return f"Back op vraag {step.number}: afgewezen"
    if event.confirmed_on != PHYSICAL_SOURCE:
        raise StepFailed(
            f"vraag {step.number} werd bevestigd buiten de Flipper om ({event.confirmed_on!r})"
        )
    result = event.result or {}
    if result.get("ok") is not False or result.get("error") != "denied":
        raise StepFailed(
            f"VEILIGHEIDSFOUT: de policy weigerde de test-actie niet na OK ({result!r}). "
            "Gebruik de Flipper niet voor transmits tot dit uitgezocht is."
        )
    return f"OK op vraag {step.number}: bevestigd, policy weigert"
