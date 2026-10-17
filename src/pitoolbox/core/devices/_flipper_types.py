"""Fouttypes, `AppEvent` en wachttijden van `FlipperDevice` (overzicht en bronnen: flipper.py)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pitoolbox.core.errors import DeviceError


class DeviceBusy(DeviceError):
    """Niet-blokkerende operatie: het device is bezet of iemand wacht er al op."""

    code = "device_busy"


class AppLocked(DeviceError):
    """De loader is bezet (er is een app open); de Pi sluit nooit een app van de gebruiker."""

    code = "app_locked"


class AppNotRunning(DeviceError):
    """De RPC-gekoppelde app draait niet (meer)."""

    code = "app_not_running"


class AppRejected(DeviceError):
    """De app weigerde een DataExchange (`ERROR_APP_CMD_ERROR`: ongeldig of wachtrij vol)."""

    code = "app_rejected"


class RemoteInputRefused(DeviceError):
    """Knopdruk op afstand geweigerd: er is een andere app open dan de gekoppelde PiToolbox-app."""

    code = "remote_input_refused"


@dataclass(frozen=True)
class AppEvent:
    """Ongevraagd bericht van de RPC-gekoppelde app, voor de sink (de Flipper-bridge).

    `data`: één DataExchange-payload. `closed`: de app sloot zelf (gebruiker of crash).
    `lost`: de sessie of de link viel weg (de app sluit dan zelf; opnieuw starten kan).
    """

    kind: Literal["data", "closed", "lost"]
    data: bytes = b""
    reason: str = ""


AppEventSink = Callable[[AppEvent], None]


@dataclass(frozen=True)
class FlipperTiming:
    """Wachttijden in seconden; tests verkleinen deze."""

    prompt_timeout_s: float = 3.0
    rpc_timeout_s: float = 5.0
    probe_timeout_s: float = 1.5
    settle_quiet_s: float = 0.15
    settle_max_s: float = 1.0
    read_slice_s: float = 0.05
    stop_grace_s: float = 5.0  # extra tijd na Ctrl+C voordat de prompt terug moet zijn
    command_timeout_s: float = 10.0
    app_event_timeout_s: float = 3.0
    app_exit_timeout_s: float = 3.0
    probe_attempts: int = 3
    # Na APP_CLOSED kan app_lock_status nog even `locked` geven (spike, test 2).
    loader_free_wait_s: float = 1.5
    loader_poll_s: float = 0.25


@dataclass
class _Attempt:
    """Wordt waar zodra een actie met bijwerking is gestart; daarna geen automatische retry."""

    committed: bool = False
