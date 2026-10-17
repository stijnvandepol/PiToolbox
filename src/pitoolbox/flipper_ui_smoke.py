"""``ui-test``, subcommando van ``pitoolbox-flipper`` (flipper_ui_cli.py): de rooktest.

De app starten via de echte bridge, HELLO/WELCOME, een STATUS van de ``status``-tool end-to-end
afleveren, de app netjes sluiten; met tijden per stap. Er wordt niets uitgezonden: de runner
laat alleen ``status`` toe.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from . import flipper_ui_harness as harness
from .config import Settings
from .flipper_ui_harness import (
    _LOCKED_HELP,
    StepFailed,
    _bring_up,
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
    from .frontends.flipper_ui.bridge import FlipperUiBridge


def cmd_ui_test(args: argparse.Namespace, settings: Settings) -> int:
    run_settings = _runner_settings(settings, args, args.transport)
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
