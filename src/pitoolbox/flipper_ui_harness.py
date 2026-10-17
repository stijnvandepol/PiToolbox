"""Gedeeld gereedschap voor ``ui-test`` en ``ui-droptest`` (flipper_ui_cli.py): de bridge en
runner opbouwen, gebeurtenissen van de bridge volgen en de app tot de handdruk brengen.

**Niets hiervan zendt radio uit.** De runner laat alleen ``status`` toe (``TEST_ALLOWLIST``) en
krijgt een lege scope (``scope_file`` in een eigen lege tijdelijke map, zie `_runner_settings`):
twee lagen die elk een transmit weigeren.

``TIMING``, ``BRIDGE_TIMING``, ``_make_runner``, ``_clock`` en ``_sleep`` zijn vervangbaar in
tests; de commando's lezen ze daarom via deze module (``harness.TIMING``), niet via een import
van de naam.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Settings
from .core.errors import ToolboxError

if TYPE_CHECKING:
    from .core.devices.flipper import FlipperDevice
    from .core.tools.runner import ToolRunner
    from .frontends.flipper_ui.bridge import BridgeEvent, BridgeTiming, FlipperUiBridge

# Vervangbaar in tests.
_clock = time.monotonic
_sleep = time.sleep

# Alleen deze tool mag de bridge in ui-test/ui-droptest aanroepen: niets zendt, niets wijzigt.
TEST_ALLOWLIST = ["status"]
SCOPE_DIR_PREFIX = "pitoolbox-ui-test-"


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


class StepFailed(Exception):
    """Een stap van ui-test/ui-droptest mislukte; de melding is voor de gebruiker."""


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


# --- opbouw (vervangbaar in tests) -----------------------------------------------------------


def _device_settings(settings: Settings, args: argparse.Namespace, transport: str) -> Settings:
    update: dict[str, Any] = {
        "flipper_transport": transport,
        "flipper_ble_adapter": args.adapter,
    }
    if args.address:
        update["flipper_ble_address"] = args.address
    return settings.model_copy(update=update)


def _make_runner(settings: Settings) -> ToolRunner:
    from .app import build_runner

    return build_runner(settings)


def _make_bridge(
    runner: ToolRunner, app_path: str, observer: Callable[[BridgeEvent], None]
) -> FlipperUiBridge:
    from .frontends.flipper_ui.bridge import FlipperUiBridge

    return FlipperUiBridge(runner, app_path=app_path, timing=BRIDGE_TIMING, observer=observer)


def _empty_scope_dir() -> tempfile.TemporaryDirectory[str]:
    """Eigen lege map (mode 0700) voor `_runner_settings`; zolang hij bestaat, kan niemand
    anders er een scope.yaml in zetten. Gebruik als context manager rond de hele test."""
    return tempfile.TemporaryDirectory(prefix=SCOPE_DIR_PREFIX)


def _runner_settings(
    settings: Settings, args: argparse.Namespace, transport: str, scope_dir: str
) -> Settings:
    """Settings voor de test-runner: alleen ``status`` en een lege scope. ``scope_file`` wijst
    naar een bestand dat niet bestaat in `scope_dir` (van `_empty_scope_dir`), dus de echte
    scope.yaml van de gebruiker telt hier niet mee."""
    return _device_settings(settings, args, transport).model_copy(
        update={
            "tool_allowlist": list(TEST_ALLOWLIST),
            "scope_file": Path(scope_dir) / "scope.yaml",
        }
    )


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
                event = self._take(wanted, predicate)
                if event is not None:
                    return event
                remaining = deadline - _clock()
                if remaining <= 0:
                    return None
                self._cond.wait(min(remaining, 0.5))

    def _take(
        self, wanted: set[str], predicate: Callable[[BridgeEvent], bool] | None
    ) -> BridgeEvent | None:
        """Onder `_cond`: het eerste passende ongebruikte event, en markeer het als gebruikt."""
        for event in self._unused:
            if event.kind in wanted and (predicate is None or predicate(event)):
                self._unused.remove(event)
                return event
        return None

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
