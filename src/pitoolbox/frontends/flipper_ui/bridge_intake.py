"""Wat de hoofdlus van de bridge binnenkrijgt: verzoeken, pendings, de lezer en FAP-berichten.

`request_start()` en `send_status()` (thread-safe), de luisteraar van de `PendingStore` en de
lezer (`flipper-ui-reader`) zetten alleen werk in de inbox van de hoofdlus (zie de
module-docstring van `bridge.py`). Een FAP-bericht wordt hier gedecodeerd, aan de rate limit
getoetst (de noodstop gaat altijd door) en naar de juiste flow verdeeld; de handdruk (HELLO) en
de toestand bij app-gebeurtenissen staan in `bridge.py`.
"""

from __future__ import annotations

import logging
import time

from pitoolbox.core.policy import PendingEvent
from pitoolbox.frontends.flipper_ui.bridge_actions import _ActionFlow
from pitoolbox.frontends.flipper_ui.bridge_confirm import _ConfirmFlow
from pitoolbox.frontends.flipper_ui.bridge_core import _Fap
from pitoolbox.frontends.flipper_ui.bridge_types import LOG_NAME
from pitoolbox.frontends.flipper_ui.protocol import (
    Action,
    ActionKind,
    Cancel,
    ConfirmAnswer,
    ErrorCode,
    FapMessage,
    PresetsRequest,
    ProtocolError,
    QuickAction,
    StatusRequest,
    decode_from_fap,
)

log = logging.getLogger(LOG_NAME)


class _Intake(_ConfirmFlow, _ActionFlow):
    """Zie `bridge_core.py` voor hoe de delen van de bridge op elkaar bouwen."""

    # --- ingangen (thread-safe) ---------------------------------------------------------

    def request_start(self) -> None:
        """Start de app nu (als hij niet al draait en de loader vrij is). Thread-safe."""
        self._post(lambda: self._request_start("op verzoek"))

    def send_status(self) -> None:
        """Stuur een verse STATUS naar de app (als de handdruk rond is). Thread-safe."""
        self._post(self._send_status)

    # --- pendings -------------------------------------------------------------------

    def _on_pending_event(self, event: PendingEvent) -> None:
        # Met de lock van de PendingStore: alleen doorzetten.
        self._post(lambda: self._handle_pending_event(event), external=True)

    # --- lezer ---------------------------------------------------------------------------------

    def _reader_main(self) -> None:
        while not self._stopping.is_set():
            if self.device.rpc_app_state != "active" or not self.device.poll_app_events(
                self.timing.poll_slice_s
            ):
                time.sleep(self.timing.poll_idle_s)

    # --- berichten van de FAP -------------------------------------------------------

    def _decode_limited(self, data: bytes) -> FapMessage | None:
        """Decoderen en de rate limit toepassen; None: het bericht vervalt (soms met ERROR)."""
        try:
            msg = decode_from_fap(data)
        except ProtocolError as exc:
            if self._rx_rate.allow():
                log.info("ongeldig bericht van de app: %s", exc)
                self._send_error(exc.seq or 0, exc.code, str(exc), instance=None)
            return None
        # De noodstop gaat altijd door, ook boven de rate limit.
        if not _is_emergency_stop(msg) and not self._rx_rate.allow():
            log.info("bericht van de app gedropt (rate limit, totaal %d)", self._rx_rate.dropped)
            if isinstance(msg, Action):
                self._send_error(msg.seq, ErrorCode.RATE_LIMITED, "te snel", instance=None)
            return None
        return msg

    def _dispatch(self, fap: _Fap, msg: FapMessage) -> None:
        match msg:
            case StatusRequest():
                self._send_status()
            case PresetsRequest():
                self._send_presets(fap)
            case Action():
                self._on_action(fap, msg)
            case ConfirmAnswer():
                self._on_confirm_answer(fap, msg)
            case Cancel():
                self._on_cancel(fap, msg)


def _is_emergency_stop(msg: FapMessage) -> bool:
    return (
        isinstance(msg, Action)
        and msg.kind is ActionKind.QUICK
        and msg.action_id == QuickAction.EMERGENCY_STOP
    )
