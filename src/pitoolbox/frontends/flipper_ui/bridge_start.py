"""De PiToolbox-app starten (protocol §8): op verzoek, na een verloren link, of als een
handmatig geopende app daarom vraagt (verzoekbestand).

Het verzoekbestand wordt gepold als het device in RPC staat; zonder verbinding opent de
bridge er zelf één (zie de module-docstring van `bridge.py`). Starten en pollen lopen in de
device-lane; de uitkomst gaat via `_post` terug naar de hoofdlus.
"""

from __future__ import annotations

import logging
import re
from functools import partial

from pitoolbox.core.devices.flipper import AppLocked, DeviceBusy
from pitoolbox.core.errors import Aborted, DeviceError, ToolboxError
from pitoolbox.frontends.flipper_ui.bridge_output import _FapOutput
from pitoolbox.frontends.flipper_ui.bridge_types import LOG_NAME, BridgeState
from pitoolbox.frontends.flipper_ui.protocol import ErrorCode

log = logging.getLogger(LOG_NAME)

START_REQUEST_PATH = "/ext/apps_data/pitoolbox_ui/start_request"
START_REQUEST_MAX_BYTES = 64
START_REQUEST_MAX_AGE_S = 30
_START_REQUEST_RE = re.compile(rb"^PTBUI 1 ([0-9]{1,10})\n?$")


class _AppStarter(_FapOutput):
    """Zie `bridge_core.py` voor hoe de delen van de bridge op elkaar bouwen."""

    # --- starten (§8) -----------------------------------------------------------------

    def _request_start(
        self, why: str, *, retry: bool = False, lock_wait_s: float | None = None
    ) -> None:
        if self._state is not BridgeState.IDLE:
            return
        self._state = BridgeState.STARTING
        self._hello_deadline = None
        self._start_deadline = self._clock() + self.timing.start_timeout_s
        log.info("PiToolbox-app starten (%s)", why)

        def job() -> None:
            # Draait hij nog (deze kant koppelde los, de app niet), dan is starten een no-op
            # en moet de app opnieuw groeten.
            running = self.device.rpc_app_state == "active"
            try:
                self.device.start_rpc_app(self.app_path, lock_wait_s=lock_wait_s)
            except AppLocked as exc:
                log.info("app niet gestart: %s", exc)
                self._notify("start_failed", "locked")
                self._post(lambda: self._on_start_failed(retry=retry, locked=True))
            except Aborted as exc:  # noodstop terwijl de start op de device-lock wachtte
                log.info("app starten afgebroken: %s", exc)
                self._notify("start_failed", str(exc))
                self._post(lambda: self._on_start_failed(retry=False))
            except (ToolboxError, ValueError) as exc:  # DeviceError is ook een ToolboxError
                log.warning("app starten mislukte: %s", exc)
                self._notify("start_failed", str(exc))
                self._post(lambda: self._on_start_failed(retry=retry))
            except Exception as exc:
                log.exception("app starten faalde onverwacht")
                self._notify("start_failed", type(exc).__name__)
                self._post(lambda: self._on_start_failed(retry=False))
            else:
                self._notify("started")
                self._post(partial(self._on_started, rehello=running))

        if not self._lanes["device"].submit(job):
            self._state = BridgeState.IDLE
            self._start_deadline = None

    def _on_started(self, *, rehello: bool = False) -> None:
        self._start_deadline = None
        if self._state is BridgeState.STARTING:
            self._state = BridgeState.HANDSHAKE
        if self._state is BridgeState.HANDSHAKE:
            self._hello_deadline = self._clock() + self.timing.hello_timeout_s
            if rehello:
                # §5: de FAP groet opnieuw op ERROR NOT_READY (zelfde instance, nieuwe sessie
                # hier: oude handles bestaan aan deze kant niet meer).
                log.info("PiToolbox-app draait nog; opnieuw groeten gevraagd")
                self._send_error(0, ErrorCode.NOT_READY, "Opnieuw verbinden", instance=None)

    def _on_start_failed(self, *, retry: bool, locked: bool = False) -> None:
        """`retry`: herstart na een verloren link. Was de loader bezet (`locked`), dan alleen
        binnen `locked_retry_s` na het verlies: kort erna kan dat nog onze eigen, sluitende app
        zijn; later is het een app van de gebruiker. Er wordt nooit een app gesloten."""
        self._start_deadline = None
        if self._state is BridgeState.STARTING:
            self._state = BridgeState.IDLE
        if retry and locked and self._clock() >= self._locked_until:
            log.info("PiToolbox-app: loader blijft bezet; opnieuw starten opgegeven")
            retry = False
        if retry:
            self._schedule_restart()
        else:
            self._restart_at = None
            self._restart_step = 0  # een volgend verlies begint zijn vensters opnieuw

    def _poll_start_request(self) -> None:
        """Device-lane: verzoekbestand van een handmatig gestarte FAP (§8). Alleen als het
        device in RPC staat, vrij is en er geen andere app open is; een oud verzoek (> 30 s)
        telt niet.

        Zonder verbinding opent de bridge er zelf een (een RPC-ping), met de backoff van
        `_link_failed`: de FAP schrijft alleen een verzoek als er een RPC-sessie van een host
        is, dus zonder link kan niemand de app handmatig openen. Een open link in CLI-modus
        (een Sub-GHz/NFC/RFID-actie) blijft met rust: die zetten we niet terug naar RPC.
        """
        if self.device.rpc_app_state != "off":
            return
        status = self.device.status()  # zonder lock en zonder te verbinden
        if not status.get("connected"):
            if not self._open_link():
                return
        elif status.get("mode") != "rpc":
            return
        try:
            if self.device.app_lock_status(idle_only=True):
                return
            raw = self.device.take_small_file(
                START_REQUEST_PATH, START_REQUEST_MAX_BYTES, idle_only=True
            )
            if raw is None:
                self._post(self._link_ok)
                return
            match = _START_REQUEST_RE.fullmatch(raw)
            if match is None:
                log.warning("verzoekbestand van de app ongeldig; genegeerd")
                return
            age = self.device.rtc_timestamp() - int(match.group(1))
        except DeviceBusy:
            return
        except (DeviceError, ToolboxError) as exc:
            log.debug("verzoekbestand pollen: %s", exc)
            self._post(self._link_failed)
            return
        self._post(self._link_ok)
        if not 0 <= age <= START_REQUEST_MAX_AGE_S:
            log.info("verzoek om de app te openen is %d s oud; genegeerd", age)
            return
        self._post(
            lambda: self._request_start(
                "handmatig geopend", lock_wait_s=self.timing.manual_lock_wait_s
            )
        )

    def _open_link(self) -> bool:
        """Device-lane: verbinden en in RPC zetten (ping); bij een fout de backoff in."""
        try:
            self.device.ping()
        except DeviceBusy:
            return False
        except (DeviceError, ToolboxError) as exc:
            log.info("PiToolbox-app: geen verbinding met de Flipper (%s)", exc)
            self._post(self._link_failed)
            return False
        return True

    def _link_ok(self) -> None:
        self._link_step = 0

    def _link_failed(self) -> None:
        backoff = self.timing.link_backoff_s
        delay = backoff[min(self._link_step, len(backoff) - 1)]
        self._link_step += 1
        self._next_poll = self._clock() + delay
