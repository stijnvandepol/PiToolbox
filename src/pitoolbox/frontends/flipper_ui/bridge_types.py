"""Publieke waardetypes van de Flipper-bridge: wachttijden, toestand en waarnemingen.

Hier en niet in `bridge.py`, zodat elk deel van de bridge ze kan gebruiken zonder kringimport.
`bridge.py` exporteert ze ook; daar staat de uitleg van de bridge zelf.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

# Alle delen van de bridge loggen onder deze ene naam (in de log staat `%(name)s`), los van
# de module waarin de code staat.
LOG_NAME = "pitoolbox.frontends.flipper_ui.bridge"

FRONTEND = "flipper-ui"


@dataclass(frozen=True)
class BridgeTiming:
    """Wachttijden in seconden; tests verkleinen ze."""

    tick_s: float = 0.1  # hoofdlus: timers controleren
    poll_slice_s: float = 0.1  # lezer: hoogstens zo lang de lock per keer
    poll_idle_s: float = 0.05  # lezer: pauze als er niets te lezen viel
    send_gap_s: float = 0.2  # Pi -> FAP hoogstens 5 berichten/s
    send_timeout_s: float = 3.0  # geen bevestiging: de app hangt (protocol §4)
    send_retry_s: float = 0.2  # na ERROR_APP_CMD_ERROR
    send_retries: int = 3
    progress_gap_s: float = 1.0
    hello_timeout_s: float = 10.0  # na APP_STARTED: de FAP probeert HELLO 3x per 3 s
    expire_s: float = 1.0  # handles controleren op TTL
    start_poll_s: float = 3.0  # verzoekbestand (§8)
    manual_lock_wait_s: float = 3.0  # de handmatige instance sluit nog
    restart_backoff_s: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0, 60.0)
    restart_window_s: float = 300.0  # na een verloren link zo lang opnieuw proberen
    link_backoff_s: tuple[float, ...] = (3.0, 6.0, 12.0, 30.0, 60.0)  # pollen zonder link
    stop_join_s: float = 2.0  # ook de grens voor het sluiten van de app in stop()
    start_timeout_s: float = 120.0  # vangnet: zo lang hoogstens in STARTING (device-lock bezet)


class BridgeState(StrEnum):
    IDLE = "idle"  # geen app (of nog niet gestart)
    STARTING = "starting"  # StartRequest loopt
    HANDSHAKE = "handshake"  # app draait, wacht op HELLO of tot WELCOME afgeleverd is
    CONNECTED = "connected"  # handdruk rond: bevestigen kan alleen nog op de Flipper
    STOPPED = "stopped"


BridgeEventKind = Literal["started", "start_failed", "hello", "connected", "delivered", "detached"]


@dataclass(frozen=True)
class BridgeEvent:
    """Waarneming voor een `observer` (zie de module-docstring van `bridge.py`).

    - `started`: APP_STARTED ontvangen. `start_failed`: niet gestart; `detail` is "locked" (de
      loader was bezet) of de fout.
    - `hello`: HELLO van een nieuwe instance. `connected`: WELCOME afgeleverd (handdruk rond).
    - `delivered`: de FAP bevestigde een bericht; `detail` is het type (bijv. "Status").
    - `detached`: app-sessie voorbij; `detail` is de reden.
    """

    kind: BridgeEventKind
    detail: str = ""
    instance: int | None = None
    at: float = 0.0  # time.monotonic()


BridgeObserver = Callable[[BridgeEvent], None]
