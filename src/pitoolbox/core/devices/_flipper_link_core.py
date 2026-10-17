"""Onderste laag van `FlipperDevice`: alle toestand en de haken voor de gekoppelde app.

Alle toestand wordt hier aangemaakt (`_LinkCore.__init__`); `FlipperLink` (`_flipper_link`,
operatiekader, reconnect, keepalive) en de andere `_flipper_*`-lagen bouwen hierop voort.
Hier ook wat de RpcClient en het afsluiten van de RPC-gekoppelde app nodig hebben: de sink, de
event-hook, `_rpc_app_lost` en `_exit_app`. Overzicht en bronnen: zie flipper.py.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Literal

from pitoolbox.core.devices._flipper_cli import CliProfile
from pitoolbox.core.devices._flipper_rpc.client import Main, RpcClient
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2
from pitoolbox.core.devices._flipper_transport import ByteTransport
from pitoolbox.core.devices._flipper_types import AppEvent, AppEventSink, FlipperTiming
from pitoolbox.core.errors import DeviceError

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")


class _LinkCore:
    """Toestand die alle Flipper-lagen delen, plus de haken voor de gekoppelde app."""

    def __init__(
        self,
        transport_factory: Callable[[], ByteTransport],
        cli_profile: str = "auto",
        *,
        timing: FlipperTiming | None = None,
        keepalive_s: float = 0.0,
        usb_probe: Callable[[], bool] | None = None,
    ) -> None:
        """`keepalive_s` > 0: RPC-ping na zoveel seconden idle. `usb_probe` (alleen in 'auto'):
        zit er nu een Flipper op USB? Dan schakelt een CLI-actie over BLE naar USB over."""
        self._factory = transport_factory
        self._profile_setting = cli_profile
        self._timing = timing or FlipperTiming()
        self._keepalive_s = keepalive_s
        self._usb_probe = usb_probe
        self._last_io = time.monotonic()
        self._ka_thread: threading.Thread | None = None
        self._ka_stop = threading.Event()
        self._lock = threading.Lock()  # alle device-I/O
        self._gen_lock = threading.Lock()
        self._abort_gen = 0
        self._waiters = 0  # threads die op `_lock` wachten (onder `_gen_lock` geteld)
        self._op_abortable = False  # de lopende operatie is afbreekbaar (onder `_gen_lock`)
        # RPC-gekoppelde app (PiToolbox-FAP): "off" | "active" | "suspended" (handoff in ir_send).
        self._rpc_app_state: Literal["off", "active", "suspended"] = "off"
        self._rpc_app_path: str | None = None
        self._rpc_app_closing = False  # wij sluiten hem zelf: APP_CLOSED is voor wait_event
        self._app_sink: AppEventSink | None = None
        # Onderstaande toestand wordt alleen onder `_lock` gewijzigd; status() leest ze zonder.
        self._transport: ByteTransport | None = None
        self._mode = "unknown"  # "cli" | "rpc" | "unknown"
        self._client: RpcClient | None = None
        self._profile: CliProfile | None = None
        self._device_info: dict[str, str] | None = None
        self._app_active = False
        self._maybe_rpc = False  # poort kan nog in een RPC-sessie staan die wij kwijt zijn
        self._busy_op: str | None = None

    # --- intern: gekoppelde app ---------------------------------------------------------

    def _emit_app(self, event: AppEvent) -> None:
        sink = self._app_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            log.exception("app-sink faalde")

    def _rpc_app_lost(self, reason: str, kind: Literal["closed", "lost"] = "lost") -> None:
        """De gekoppelde app is weg (ook als hij voor een handoff dicht was)."""
        if self._rpc_app_state == "off":
            return
        self._rpc_app_state = "off"
        self._rpc_app_closing = False
        log.info("PiToolbox-app %s: %s", "gesloten" if kind == "closed" else "kwijt", reason)
        self._emit_app(AppEvent(kind, reason=reason))

    def _on_unsolicited(self, frame: Main) -> bool:
        """Event-hook van de RpcClient (draait in de lezende thread, met de lock)."""
        content = frame.WhichOneof("content")
        if content == "app_data_exchange_request" and frame.command_id == 0:
            if self._rpc_app_state == "active":
                self._emit_app(AppEvent("data", data=bytes(frame.app_data_exchange_request.data)))
            else:
                log.debug("DataExchange zonder gekoppelde app genegeerd")
            return True
        if (
            content == "app_state_response"
            and frame.app_state_response.state == application_pb2.APP_CLOSED
            and self._rpc_app_state == "active"
            and not self._rpc_app_closing
            and not self._app_active
        ):
            self._rpc_app_lost("de app sloot zichzelf", kind="closed")
            return True
        # Schermbeelden wil alleen de ontwikkelaarsmodus op afstand (`_flipper_remote`), en die
        # pakt het ene frame met `wait_event` vóór deze hook. Wat hier aankomt liep na (de
        # stream stopt pas als de Flipper de stop bevestigt): weggooien (True), want ~1 KiB per
        # frame hoort niet in de begrensde eventqueue.
        return bool(content == "gui_screen_frame")

    def _exit_app(self, client: RpcClient, timeout: float) -> None:
        """app_exit_request + wachten op APP_CLOSED; nooit een uitzondering (opruimcode)."""
        try:
            main = flipper_pb2.Main()
            main.app_exit_request.SetInParent()
            client.request(main, timeout)
            client.wait_event(lambda m: _is_app_state(m, application_pb2.APP_CLOSED), timeout)
        except DeviceError as exc:
            log.warning("app afsluiten mislukte: %s", exc)
        finally:
            self._app_active = False


def _is_app_state(main: Main, state: int) -> bool:
    return bool(
        main.WhichOneof("content") == "app_state_response"
        and main.app_state_response.state == state
    )
