"""``ui-droptest [--rounds N]``, subcommando van ``pitoolbox-flipper`` (flipper_ui_cli.py).

Valtest over BLE. Terwijl de Pi STATUS-berichten stuurt, wordt de link via BlueZ verbroken
(``org.bluez.Device1.Disconnect`` over D-Bus, geen root). Daarna moet de Flipper antwoorden en
de app opnieuw starten. Stopt bij de eerste fout. Er wordt niets uitgezonden: de runner laat
alleen ``status`` toe.
"""

from __future__ import annotations

import argparse
import statistics
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import flipper_ui_harness as harness
from .config import Settings
from .core.devices import _ble_pairing as ble_pairing
from .core.errors import ToolboxError
from .flipper_ui_harness import (
    _LOCKED_HELP,
    StepFailed,
    UiTiming,
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
    from .frontends.flipper_ui.bridge import BridgeEvent, FlipperUiBridge

# Vervangbaar in tests.
_input = input


def _disconnect(address: str, adapter: str | None) -> bool:
    return ble_pairing.disconnect(address, adapter=adapter)


@dataclass(frozen=True)
class _Round:
    number: int
    detect_s: float
    back_s: float
    ping_ms: float
    notes: tuple[str, ...]


class _Traffic:
    """Laat de bridge steeds een verse STATUS sturen zolang `on` gezet is."""

    def __init__(self, bridge: FlipperUiBridge) -> None:
        self._bridge = bridge
        self.on = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name="ui-droptest-traffic", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.on.clear()
        self._thread.join(2.0)

    def _main(self) -> None:
        while not self._stop.is_set():
            if self.on.wait(0.1) and not self._stop.is_set():
                self._bridge.send_status()
                self._stop.wait(harness.TIMING.traffic_gap_s)


@dataclass(frozen=True)
class _DropSetup:
    """Wat elke ronde van de valtest gebruikt."""

    address: str
    adapter: str | None
    bridge: FlipperUiBridge
    device: FlipperDevice
    events: _Events
    traffic: _Traffic


_SCREEN_HELP = """\
Kijk NU op het scherm van de Flipper en noteer (liefst met een foto):
  - een crashscherm ("BusFault", "furi_check failed", "System halted", ...): de tekst exact;
  - of hij vanzelf herstart (opstartlogo), of vastzit;
  - of "Verbinding met Pi verbroken" of het PiToolbox-menu nog in beeld staat (app hangt);
  - of hij gewoon op het bureaublad staat.
Hangt hij: houd Links + Back ~5 s ingedrukt (reset). Controleer daarna met
'pitoolbox-flipper ble-test' dat hij terug is, op het bureaublad zonder open app.
Noteer ook de ronde, de tijd ({clock}) en deze uitvoer."""


def _confirm_drop(args: argparse.Namespace, address: str) -> bool:
    _out(
        f"Valtest: {args.rounds}x de BLE-link met {address} verbreken terwijl de PiToolbox-app "
        "STATUS-berichten krijgt.\n"
        "Er wordt niets uitgezonden (alleen de status-tool is toegestaan). Crasht de Flipper, dan "
        "herstart hij of blijft hij hangen; de test stopt dan en zegt wat je moet noteren.\n"
        "Stop eerst pitoolbox-mcp/-agent als die de Flipper gebruiken. Houd de Flipper in zicht."
    )
    if args.yes:
        return True
    answer = _input("Doorgaan? [j/N] ").strip().lower()
    return answer in ("j", "ja", "y", "yes")


def _drop_round(number: int, setup: _DropSetup, instance: int) -> tuple[_Round, int]:
    timing = harness.TIMING
    notes: list[str] = []
    dropped = _drop_link(number, setup, timing)
    detached = setup.events.wait({"detached"}, timing.detach_s)
    if detached is None:
        raise StepFailed(f"de Pi merkte de verbroken link niet binnen {timing.detach_s:g} s")
    event = _await_reconnect(setup, dropped, instance, notes, timing)
    back = event.at
    if event.instance is None:  # pragma: no cover - connected heeft altijd een instance
        raise StepFailed("verbonden zonder instance")
    try:
        ping_ms = setup.device.ping() * 1000.0
    except ToolboxError as exc:
        raise StepFailed(f"de Flipper antwoordt niet op een ping: {exc}") from exc
    result = _Round(number, detached.at - dropped, back - dropped, ping_ms, tuple(notes))
    return result, event.instance


def _drop_link(number: int, setup: _DropSetup, timing: UiTiming) -> float:
    """STATUS laten lopen en dan de BLE-link verbreken; geeft het tijdstip van de val."""
    setup.events.reset()
    setup.traffic.on.set()
    try:
        if _wait_delivered(setup.events, {"Status"}, timing.step_s) is None:
            raise StepFailed("de app kreeg vóór de val geen STATUS meer binnen")
        harness._sleep(timing.traffic_s + (number % 4) * timing.traffic_step_s)
        dropped = harness._clock()
        try:
            if not _disconnect(setup.address, setup.adapter):
                raise StepFailed("BlueZ meldt de Flipper als niet verbonden: de link was al weg")
        except ToolboxError as exc:
            raise StepFailed(f"verbreken via BlueZ mislukte: {exc}") from exc
    finally:
        setup.traffic.on.clear()
    return dropped


def _await_reconnect(
    setup: _DropSetup, dropped: float, instance: int, notes: list[str], timing: UiTiming
) -> BridgeEvent:
    """Wacht tot een nieuwe instance verbonden is; `notes` krijgt wat onderweg opviel."""
    deadline = dropped + timing.reconnect_s
    while True:
        event = setup.events.wait(
            {"connected", "start_failed", "detached"},
            max(deadline - harness._clock(), 0.0),
            lambda e: e.kind != "connected" or e.instance != instance,
        )
        if event is None:
            raise StepFailed(
                f"de app kwam niet terug binnen {timing.reconnect_s:g} s na het verbreken"
            )
        if event.kind == "connected":
            return event
        if event.kind == "detached":
            raise StepFailed(f"de app viel na het herstarten weer weg: {event.detail}")
        if event.detail != "locked":
            notes.append(f"herstart mislukte eerst ({event.detail}); de bridge probeert opnieuw")
            continue
        # De oude app sluit pas 2 s na "Verbinding met Pi verbroken". De bridge probeert het na
        # een verloren link zelf `locked_retry_s` lang opnieuw en geeft daarna op (hij sluit nooit
        # een app); hier wachten we tot de loader vrij is of tot de bridge onze app intussen al
        # weer gestart heeft (dan is de loader bezet door ónze app, en dat is goed).
        notes.append("loader nog bezet bij herstart")
        if not _wait_free_or_ours(setup.device, timing.locked_wait_s):
            raise StepFailed(
                "de loader bleef bezet: de oude app sloot niet na het verbreken (hangt hij?)"
            )
        setup.bridge.request_start()  # genegeerd als de bridge al bezig of verbonden is


def _wait_free_or_ours(device: FlipperDevice, timeout: float) -> bool:
    """Loader vrij, of bezet door de PiToolbox-app die de bridge net weer startte."""
    deadline = harness._clock() + timeout
    while True:
        if device.rpc_app_state != "off":
            return True
        if _wait_loader_free(device, 0.0):
            return True
        if harness._clock() >= deadline:
            return False
        harness._sleep(harness.TIMING.poll_s)


def cmd_droptest(args: argparse.Namespace, settings: Settings) -> int:
    address = args.address or settings.flipper_ble_address
    if not address:
        _err("ui-droptest heeft het BLE-adres nodig: --address of PITOOLBOX_FLIPPER_BLE_ADDRESS.")
        return 2
    if not _confirm_drop(args, address):
        _out("Niet gestart.")
        return 1
    with _empty_scope_dir() as scope_dir:
        run_settings = _runner_settings(settings, args, "ble", scope_dir).model_copy(
            update={"flipper_ble_address": address}
        )
        return _run_droptest(args, run_settings, address)


def _run_droptest(args: argparse.Namespace, run_settings: Settings, address: str) -> int:
    runner = harness._make_runner(run_settings)
    device: FlipperDevice = runner.ctx.flipper
    events = _Events()
    bridge: FlipperUiBridge | None = None
    traffic: _Traffic | None = None
    timeline = _Timeline(harness._clock())
    rounds: list[_Round] = []
    current = 0
    try:
        info = device.system_info()
        _out(f"Verbonden; firmware {info.get('firmware_version', '?')}.")
        if device.app_lock_status():
            _err(_LOCKED_HELP)
            return 1
        bridge = _make_bridge(runner, run_settings.flipper_ui_app, events.put)
        bridge.start()
        instance = _bring_up(bridge, events, timeline)
        _out(f"App verbonden (instance {instance:08x}). Start: {args.rounds} rondes.")
        traffic = _Traffic(bridge)
        traffic.start()
        setup = _DropSetup(address, args.adapter, bridge, device, events, traffic)
        for number in range(1, args.rounds + 1):
            current = number
            result, instance = _drop_round(number, setup, instance)
            rounds.append(result)
            _print_round(result, args.rounds)
            if number < args.rounds:
                harness._sleep(args.pause)
    except StepFailed as exc:
        _err("")
        _err(f"RONDE {current}/{args.rounds} MISLUKT: {exc}" if current else f"MISLUKT: {exc}")
        _err(f"Geslaagde rondes vóór de fout: {len(rounds)}.")
        tail = events.tail(timeline.started)
        if tail:
            _err("Laatste gebeurtenissen van de bridge:\n" + "\n".join(tail))
        _err(_SCREEN_HELP.format(clock=time.strftime("%H:%M:%S")))
        return 1
    finally:
        if traffic is not None:
            traffic.stop()
        if bridge is not None:
            bridge.stop()
        device.close()
    _print_summary(rounds, args.rounds)
    return 0


def _print_round(result: _Round, total: int) -> None:
    note = f"  ({'; '.join(result.notes)})" if result.notes else ""
    _out(
        f"Ronde {result.number:>3}/{total}: OK  verbreking gezien na "
        f"{result.detect_s:5.2f} s, app terug na {result.back_s:5.2f} s, "
        f"ping {result.ping_ms:4.0f} ms{note}"
    )


def _print_summary(rounds: list[_Round], total: int) -> None:
    _out("")
    _out(f"Valtest geslaagd: {len(rounds)} van {total} rondes.")
    if rounds:
        _out(
            "  verbreking gezien: mediaan "
            f"{statistics.median(r.detect_s for r in rounds):.2f} s; app terug: mediaan "
            f"{statistics.median(r.back_s for r in rounds):.2f} s, max "
            f"{max(r.back_s for r in rounds):.2f} s"
        )
    _out("Controleer de Flipper: bureaublad, geen open app, geen crashmelding.")
