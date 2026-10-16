"""Hardwaretests voor de PiToolbox-app op de Flipper (fase 4); subcommando's van pitoolbox-flipper.

- ``ui-install [--fap PAD] [--force]``: de FAP controleren (ELF, manifest, API van de firmware)
  en via de storage-RPC op ``PITOOLBOX_FLIPPER_UI_APP`` zetten, met voortgang en MD5-controle.
- ``ui-test``: de app starten via de echte bridge, HELLO/WELCOME, een STATUS van de
  ``status``-tool end-to-end afleveren, de app netjes sluiten; met tijden per stap.
- ``ui-droptest [--rounds N]``: valtest over BLE. Terwijl de Pi STATUS-berichten stuurt, wordt de
  link via BlueZ verbroken (``org.bluez.Device1.Disconnect`` over D-Bus, geen root). Daarna moet
  de Flipper antwoorden en de app opnieuw starten. Stopt bij de eerste fout.

**Niets hiervan zendt radio uit.** ui-test en ui-droptest draaien de bridge met een runner die
alleen ``status`` toelaat (``tool_allowlist``): kiest iemand op de Flipper toch iets anders
(wifi-scan, noodstop, preset), dan weigert de policy dat. **Eén eigenaar:** draait
``pitoolbox-mcp`` of ``pitoolbox-agent`` met de Flipper, stop die dan eerst; twee processen
drukken elkaars BLE-link weg.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .core.devices import _ble_pairing as ble_pairing
from .core.devices._fap import FAP_APP_NAME, FAP_MAX_BYTES, FapError, check_compatible, inspect_fap
from .core.errors import ToolboxError
from .core.scope import normalize_mac

if TYPE_CHECKING:
    from .core.devices.flipper import FlipperDevice
    from .core.tools.runner import ToolRunner
    from .frontends.flipper_ui.bridge import BridgeEvent, BridgeTiming, FlipperUiBridge

# Vervangbaar in tests.
_input = input
_clock = time.monotonic
_sleep = time.sleep

# src/pitoolbox/flipper_ui_cli.py -> de root van de checkout.
DEFAULT_FAP = Path(__file__).resolve().parents[2] / "flipper_app/pitoolbox_ui/dist/pitoolbox_ui.fap"
# Alleen deze tool mag de bridge in ui-test/ui-droptest aanroepen: niets zendt, niets wijzigt.
TEST_ALLOWLIST = ["status"]
DROP_ROUNDS_MAX = 200


@dataclass(frozen=True)
class UiTiming:
    """Wachttijden in seconden; tests verkleinen ze."""

    connect_s: float = 30.0  # tot APP_STARTED (inclusief BLE-verbinden)
    hello_s: float = 15.0  # de FAP probeert HELLO 3x per 3 s
    step_s: float = 10.0  # WELCOME/STATUS afgeleverd
    exit_s: float = 5.0  # app dicht en loader vrij
    detach_s: float = 10.0  # de Pi merkt de verbroken link
    reconnect_s: float = 60.0  # app weer verbonden (backoff van de bridge begint op 2 s)
    locked_wait_s: float = 8.0  # oude app sluit na "Verbinding met Pi verbroken" (2 s)
    poll_s: float = 0.25
    traffic_gap_s: float = 0.3  # STATUS-berichten tijdens de valtest (~3/s, onder de 5/s)
    traffic_s: float = 1.0  # minimaal zo lang verkeer vóór de val
    traffic_step_s: float = 0.35  # plus (ronde % 4) x dit: de val valt elke keer anders


TIMING = UiTiming()
BRIDGE_TIMING: BridgeTiming | None = None  # None = de standaard van de bridge


class UiCliError(ToolboxError):
    code = "cli_error"


class StepFailed(Exception):
    """Een stap van ui-test/ui-droptest mislukte; de melding is voor de gebruiker."""


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


# --- argumenten ------------------------------------------------------------------------------


def _mac_arg(value: str) -> str:
    try:
        return normalize_mac(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ongeldig MAC-adres {value!r} (verwacht AA:BB:CC:DD:EE:FF)"
        ) from exc


def _drop_rounds_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen geheel getal: {value!r}") from exc
    if not 1 <= number <= DROP_ROUNDS_MAX:
        raise argparse.ArgumentTypeError(f"moet tussen 1 en {DROP_ROUNDS_MAX} liggen")
    return number


def _pause_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 0 <= number <= 60:
        raise argparse.ArgumentTypeError("moet tussen 0 en 60 seconden liggen")
    return number


def _wait_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 0 <= number <= 600:
        raise argparse.ArgumentTypeError("moet tussen 0 en 600 seconden liggen")
    return number


def add_parsers(sub: Any, common: argparse.ArgumentParser, settings: Settings) -> None:
    """Voeg ui-install, ui-test en ui-droptest toe aan de subparsers van pitoolbox-flipper."""
    link = argparse.ArgumentParser(add_help=False)
    link.add_argument(
        "--address",
        type=_mac_arg,
        default=None,
        help="BLE-adres (standaard PITOOLBOX_FLIPPER_BLE_ADDRESS)",
    )

    p_install = sub.add_parser(
        "ui-install", parents=[common, link], help="de PiToolbox-app (FAP) op de Flipper zetten"
    )
    p_install.add_argument(
        "--fap",
        type=Path,
        default=None,
        help=f"het .fap-bestand (standaard {DEFAULT_FAP.relative_to(DEFAULT_FAP.parents[3])})",
    )
    p_install.add_argument(
        "--force", action="store_true", help="ook schrijven als dezelfde versie er al staat"
    )
    p_install.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default=settings.flipper_transport,
        help="standaard PITOOLBOX_FLIPPER_TRANSPORT (USB is sneller dan BLE)",
    )

    p_test = sub.add_parser(
        "ui-test", parents=[common, link], help="rooktest: app starten, handdruk, STATUS, sluiten"
    )
    p_test.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default=settings.flipper_transport,
        help="standaard PITOOLBOX_FLIPPER_TRANSPORT",
    )
    p_test.add_argument(
        "--wait-action",
        type=_wait_arg,
        default=0.0,
        metavar="S",
        help="daarna S seconden wachten tot je op de Flipper 'Status' kiest (standaard 0: niet)",
    )

    p_drop = sub.add_parser(
        "ui-droptest",
        parents=[common, link],
        help="valtest: BLE-link N keer verbreken terwijl de app berichten krijgt",
    )
    p_drop.add_argument(
        "--rounds", type=_drop_rounds_arg, default=20, help="aantal keer verbreken (standaard 20)"
    )
    p_drop.add_argument(
        "--pause",
        type=_pause_arg,
        default=3.0,
        help="seconden tussen de rondes (standaard 3)",
    )
    p_drop.add_argument("--yes", action="store_true", help="niet om bevestiging vragen")


def run(args: argparse.Namespace, settings: Settings) -> int | None:
    """Voer een ui-commando uit; None als `args.command` geen ui-commando is."""
    if args.command == "ui-install":
        return cmd_install(args, settings)
    if args.command == "ui-test":
        return cmd_ui_test(args, settings)
    if args.command == "ui-droptest":
        return cmd_droptest(args, settings)
    return None


# --- opbouw (vervangbaar in tests) -----------------------------------------------------------


def _device_settings(settings: Settings, args: argparse.Namespace, transport: str) -> Settings:
    update: dict[str, Any] = {
        "flipper_transport": transport,
        "flipper_ble_adapter": args.adapter,
    }
    if args.address:
        update["flipper_ble_address"] = args.address
    return settings.model_copy(update=update)


def _make_device(settings: Settings) -> FlipperDevice:
    from .core.devices.flipper import FlipperDevice

    return FlipperDevice.from_settings(settings)


def _make_runner(settings: Settings) -> ToolRunner:
    from .app import build_runner

    return build_runner(settings)


def _make_bridge(
    runner: ToolRunner, app_path: str, observer: Callable[[BridgeEvent], None]
) -> FlipperUiBridge:
    from .frontends.flipper_ui.bridge import FlipperUiBridge

    return FlipperUiBridge(runner, app_path=app_path, timing=BRIDGE_TIMING, observer=observer)


def _disconnect(address: str, adapter: str | None) -> bool:
    return ble_pairing.disconnect(address, adapter=adapter)


# --- ui-install ------------------------------------------------------------------------------


def _read_fap(path: Path) -> bytes:
    if not path.is_file():
        raise UiCliError(
            f"{path} bestaat niet. Bouw de app eerst (flipper_app/pitoolbox_ui/README.md, "
            "'ufbt') of geef het bestand met --fap."
        )
    size = path.stat().st_size
    if not 0 < size <= FAP_MAX_BYTES:
        raise UiCliError(f"{path} is {size} bytes; een FAP mag 1..{FAP_MAX_BYTES} bytes zijn.")
    return path.read_bytes()


class _Progress:
    """Eén regel per 10 %: over BLE duurt ~24 KiB zo'n 20 s."""

    def __init__(self) -> None:
        self._started = _clock()
        self._shown = -1

    def __call__(self, done: int, total: int) -> None:
        step = done * 10 // max(total, 1)
        if step <= self._shown:
            return
        self._shown = step
        elapsed = max(_clock() - self._started, 1e-6)
        _out(f"  {step * 10:3d}%  {done:>7}/{total} B  {done / elapsed / 1024:5.1f} KiB/s")


def cmd_install(args: argparse.Namespace, settings: Settings) -> int:
    from .core.devices.flipper import AppLocked

    fap_path: Path = args.fap if args.fap is not None else DEFAULT_FAP
    data = _read_fap(fap_path)
    try:
        info = inspect_fap(data)
    except FapError as exc:
        raise UiCliError(f"{fap_path} is geen geldige FAP: {exc}") from exc
    if info.name != FAP_APP_NAME:
        raise UiCliError(f"{fap_path} is niet de PiToolbox-app maar {info.name!r}.")
    dest = settings.flipper_ui_app
    _out(
        f"FAP: {fap_path} ({len(data)} B, {info.name} v{info.version}, API {info.api}, "
        f"target {info.target})"
    )
    _out(f"Doel op de Flipper: {dest} (PITOOLBOX_FLIPPER_UI_APP)")
    device = _make_device(_device_settings(settings, args, args.transport))
    try:
        started = _clock()
        device_info = device.system_info()
        kind = device.status().get("transport_kind") or "?"
        _out(
            f"Verbonden via {kind} in {_clock() - started:.1f} s; firmware "
            f"{device_info.get('firmware_version', '?')} "
            f"(API {device_info.get('firmware_api_major', '?')}."
            f"{device_info.get('firmware_api_minor', '?')})"
        )
        problems = check_compatible(info, device_info)
        if problems:
            for problem in problems:
                _err(f"Past niet: {problem}.")
            _err(
                "Niet geïnstalleerd. Bouw de app tegen de SDK van precies deze firmware "
                "(flipper_app/pitoolbox_ui/README.md). Een app met de verkeerde API kan de "
                "Flipper laten crashen als hij via RPC start (fase4-spike.md §2)."
            )
            return 1
        if "firmware_api_major" not in device_info:
            _err("Let op: de firmware meldt geen API-versie; de app is daar niet op gecontroleerd.")
        if kind == "ble":
            _out(f"Over BLE gaat dit met ~1 KiB/s: reken op ~{len(data) // 1024 + 5} s.")
        try:
            result = device.install_app_file(
                dest, data, progress=_Progress(), skip_if_same=not args.force
            )
        except AppLocked as exc:
            _err(f"Niet geïnstalleerd: {exc}.")
            return 1
    finally:
        device.close()
    if result.skipped:
        _out(f"Staat er al (zelfde MD5 {result.md5}); niets geschreven. --force schrijft toch.")
    else:
        rate = result.size / max(result.seconds, 1e-6) / 1024
        _out(
            f"Geïnstalleerd: {result.path} ({result.size} B in {result.seconds:.1f} s, "
            f"{rate:.1f} KiB/s); MD5 {result.md5} op de Flipper gecontroleerd."
        )
    _out("Volgende stap: pitoolbox-flipper ui-test")
    return 0


# --- gebeurtenissen van de bridge ------------------------------------------------------------


class _Events:
    """Verzamelt `BridgeEvent`s (observer). `wait` pakt het eerste passende, nog niet gebruikte
    event, ook als het al eerder binnenkwam (de bridge meldt HELLO soms vóór APP_STARTED)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._unused: list[BridgeEvent] = []
        self.history: list[BridgeEvent] = []

    def put(self, event: BridgeEvent) -> None:
        with self._cond:
            self._unused.append(event)
            self.history.append(event)
            del self.history[:-200]
            self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._unused.clear()

    def wait(
        self,
        kinds: Iterable[str],
        timeout: float,
        predicate: Callable[[BridgeEvent], bool] | None = None,
    ) -> BridgeEvent | None:
        wanted = set(kinds)
        deadline = _clock() + timeout
        with self._cond:
            while True:
                for event in self._unused:
                    if event.kind in wanted and (predicate is None or predicate(event)):
                        self._unused.remove(event)
                        return event
                remaining = deadline - _clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.5))

    def tail(self, since: float, count: int = 10) -> list[str]:
        with self._cond:
            items = self.history[-count:]
        return [
            f"  +{e.at - since:6.2f} s  {e.kind}"
            + (f" {e.detail}" if e.detail else "")
            + (f" (instance {e.instance:08x})" if e.instance else "")
            for e in items
        ]


@dataclass
class _Timeline:
    """Tijden per stap, ten opzichte van de vorige stap en van het begin."""

    started: float
    steps: list[tuple[str, float]] = field(default_factory=list)

    def mark(self, label: str, at: float) -> None:
        self.steps.append((label, at))

    def render(self) -> str:
        rows = []
        prev = self.started
        for label, at in self.steps:
            rows.append(f"  {label:<44} +{at - prev:6.2f} s  (totaal {at - self.started:6.2f} s)")
            prev = at
        return "\n".join(rows)


_LOCKED_HELP = (
    "Er is een app open op de Flipper. Sluit die op de Flipper (Back tot het bureaublad) en "
    "probeer opnieuw; de Pi sluit nooit een app van jou."
)


def _bring_up(
    bridge: FlipperUiBridge, events: _Events, timeline: _Timeline, *, previous: int | None = None
) -> int:
    """Start de app via de bridge en wacht tot WELCOME afgeleverd is. Geeft de instance."""
    timing = TIMING
    bridge.request_start()
    started = events.wait({"started", "start_failed"}, timing.connect_s)
    if started is None:
        raise StepFailed(f"de app meldde binnen {timing.connect_s:g} s niet dat hij gestart is")
    if started.kind == "start_failed":
        if started.detail == "locked":
            raise StepFailed(_LOCKED_HELP)
        raise StepFailed(
            f"de app startte niet: {started.detail}. Staat hij op "
            f"{bridge.app_path}? (pitoolbox-flipper ui-install)"
        )
    timeline.mark("app gestart (APP_STARTED)", started.at)
    hello = events.wait(
        {"hello", "detached"},
        timing.hello_s,
        lambda e: e.kind == "detached" or e.instance != previous,
    )
    if hello is None or hello.kind == "detached":
        why = hello.detail if hello is not None else f"niets binnen {timing.hello_s:g} s"
        raise StepFailed(f"geen HELLO van de app ({why}). Is het de juiste build?")
    timeline.mark(f"HELLO ontvangen ({hello.detail})", hello.at)
    connected = events.wait(
        {"connected", "detached"},
        timing.step_s,
        lambda e: e.kind == "detached" or e.instance == hello.instance,
    )
    if connected is None or connected.kind == "detached":
        why = connected.detail if connected is not None else "time-out"
        raise StepFailed(f"WELCOME kwam niet aan bij de app ({why})")
    timeline.mark("WELCOME afgeleverd (handdruk rond)", connected.at)
    assert hello.instance is not None  # noqa: S101 - HELLO heeft altijd een instance
    return hello.instance


def _wait_delivered(events: _Events, types: set[str], timeout: float) -> BridgeEvent | None:
    found = events.wait(
        {"delivered", "detached"}, timeout, lambda e: e.kind == "detached" or e.detail in types
    )
    return None if found is None or found.kind == "detached" else found


def _wait_loader_free(device: FlipperDevice, timeout: float) -> bool:
    deadline = _clock() + timeout
    while True:
        try:
            if not device.app_lock_status():
                return True
        except ToolboxError as exc:
            _err(f"  loader-status opvragen mislukte: {exc}")
        if _clock() >= deadline:
            return False
        _sleep(TIMING.poll_s)


def _runner_settings(settings: Settings, args: argparse.Namespace, transport: str) -> Settings:
    return _device_settings(settings, args, transport).model_copy(
        update={"tool_allowlist": list(TEST_ALLOWLIST)}
    )


# --- ui-test ---------------------------------------------------------------------------------


def cmd_ui_test(args: argparse.Namespace, settings: Settings) -> int:
    run_settings = _runner_settings(settings, args, args.transport)
    runner = _make_runner(run_settings)
    device: FlipperDevice = runner.ctx.flipper
    events = _Events()
    bridge: FlipperUiBridge | None = None
    timeline = _Timeline(_clock())
    _out(f"Rooktest PiToolbox-app ({run_settings.flipper_ui_app}); er wordt niets uitgezonden.")
    try:
        info = device.system_info()
        timeline.mark("verbinden + system_info", _clock())
        kind = device.status().get("transport_kind") or "?"
        _out(f"Verbonden via {kind}; firmware {info.get('firmware_version', '?')}.")
        if device.app_lock_status():
            _err(_LOCKED_HELP)
            return 1
        bridge = _make_bridge(runner, run_settings.flipper_ui_app, events.put)
        bridge.start()
        _bring_up(bridge, events, timeline)
        status = _wait_delivered(events, {"Status"}, TIMING.step_s)
        if status is None:
            raise StepFailed("STATUS (status-tool via de runner) kwam niet aan bij de app")
        timeline.mark("STATUS afgeleverd (status-tool -> app)", status.at)
        if args.wait_action > 0:
            _out(
                f"Kies nu op de Flipper: Snelle acties > Status (binnen {args.wait_action:g} s). "
                "Andere acties weigert deze test."
            )
            answer = _wait_delivered(events, {"ResultChunk", "Error"}, args.wait_action)
            if answer is None:
                raise StepFailed(
                    "geen actie van de Flipper ontvangen (of het antwoord kwam niet aan)"
                )
            if answer.detail == "Error":
                raise StepFailed(
                    "de app kreeg een foutmelding terug (andere actie dan Status gekozen?)"
                )
            timeline.mark("actie Status: antwoord afgeleverd", answer.at)
        bridge.stop()
        bridge = None
        if device.rpc_app_state != "off" or not _wait_loader_free(device, TIMING.exit_s):
            raise StepFailed("de app sloot niet netjes (loader bleef bezet)")
        timeline.mark("app gesloten, loader vrij", _clock())
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


# --- ui-droptest -----------------------------------------------------------------------------


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
                self._stop.wait(TIMING.traffic_gap_s)


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


def _drop_round(
    number: int,
    *,
    address: str,
    adapter: str | None,
    bridge: FlipperUiBridge,
    device: FlipperDevice,
    events: _Events,
    traffic: _Traffic,
    instance: int,
) -> tuple[_Round, int]:
    timing = TIMING
    notes: list[str] = []
    events.reset()
    traffic.on.set()
    try:
        if _wait_delivered(events, {"Status"}, timing.step_s) is None:
            raise StepFailed("de app kreeg vóór de val geen STATUS meer binnen")
        _sleep(timing.traffic_s + (number % 4) * timing.traffic_step_s)
        dropped = _clock()
        try:
            if not _disconnect(address, adapter):
                raise StepFailed("BlueZ meldt de Flipper als niet verbonden: de link was al weg")
        except ToolboxError as exc:
            raise StepFailed(f"verbreken via BlueZ mislukte: {exc}") from exc
    finally:
        traffic.on.clear()
    detached = events.wait({"detached"}, timing.detach_s)
    if detached is None:
        raise StepFailed(f"de Pi merkte de verbroken link niet binnen {timing.detach_s:g} s")
    deadline = dropped + timing.reconnect_s
    while True:
        event = events.wait(
            {"connected", "start_failed", "detached"},
            max(deadline - _clock(), 0.0),
            lambda e: e.kind != "connected" or e.instance != instance,
        )
        if event is None:
            raise StepFailed(
                f"de app kwam niet terug binnen {timing.reconnect_s:g} s na het verbreken"
            )
        if event.kind == "connected":
            break
        if event.kind == "detached":
            raise StepFailed(f"de app viel na het herstarten weer weg: {event.detail}")
        if event.detail != "locked":
            notes.append(f"herstart mislukte eerst ({event.detail}); de bridge probeert opnieuw")
            continue
        # De oude app sluit pas 2 s na "Verbinding met Pi verbroken"; de bridge geeft bij een
        # bezette loader op (hij sluit nooit een app), dus hier zelf even wachten en opnieuw.
        notes.append("loader nog bezet bij herstart")
        if not _wait_loader_free(device, timing.locked_wait_s):
            raise StepFailed(
                "de loader bleef bezet: de oude app sloot niet na het verbreken (hangt hij?)"
            )
        bridge.request_start()
    back = event.at
    if event.instance is None:  # pragma: no cover - connected heeft altijd een instance
        raise StepFailed("verbonden zonder instance")
    try:
        ping_ms = device.ping() * 1000.0
    except ToolboxError as exc:
        raise StepFailed(f"de Flipper antwoordt niet op een ping: {exc}") from exc
    result = _Round(number, detached.at - dropped, back - dropped, ping_ms, tuple(notes))
    return result, event.instance


def cmd_droptest(args: argparse.Namespace, settings: Settings) -> int:
    address = args.address or settings.flipper_ble_address
    if not address:
        _err("ui-droptest heeft het BLE-adres nodig: --address of PITOOLBOX_FLIPPER_BLE_ADDRESS.")
        return 2
    if not _confirm_drop(args, address):
        _out("Niet gestart.")
        return 1
    run_settings = _runner_settings(settings, args, "ble").model_copy(
        update={"flipper_ble_address": address}
    )
    runner = _make_runner(run_settings)
    device: FlipperDevice = runner.ctx.flipper
    events = _Events()
    bridge: FlipperUiBridge | None = None
    traffic: _Traffic | None = None
    timeline = _Timeline(_clock())
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
        for number in range(1, args.rounds + 1):
            current = number
            result, instance = _drop_round(
                number,
                address=address,
                adapter=args.adapter,
                bridge=bridge,
                device=device,
                events=events,
                traffic=traffic,
                instance=instance,
            )
            rounds.append(result)
            note = f"  ({'; '.join(result.notes)})" if result.notes else ""
            _out(
                f"Ronde {number:>3}/{args.rounds}: OK  verbreking gezien na "
                f"{result.detect_s:5.2f} s, app terug na {result.back_s:5.2f} s, "
                f"ping {result.ping_ms:4.0f} ms{note}"
            )
            if number < args.rounds:
                _sleep(args.pause)
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
    _out("")
    _out(f"Valtest geslaagd: {len(rounds)} van {args.rounds} rondes.")
    if rounds:
        _out(
            "  verbreking gezien: mediaan "
            f"{statistics.median(r.detect_s for r in rounds):.2f} s; app terug: mediaan "
            f"{statistics.median(r.back_s for r in rounds):.2f} s, max "
            f"{max(r.back_s for r in rounds):.2f} s"
        )
    _out("Controleer de Flipper: bureaublad, geen open app, geen crashmelding.")
    return 0
