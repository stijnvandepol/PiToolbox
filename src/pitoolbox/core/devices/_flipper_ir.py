"""Infrared via de Infrared-app in RPC-modus, met handoff van de gekoppelde PiToolbox-app."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pitoolbox.core.devices._flipper_app_channel import FlipperAppChannel
from pitoolbox.core.devices._flipper_link import _is_app_state
from pitoolbox.core.devices._flipper_rpc.client import Main, RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2
from pitoolbox.core.devices._flipper_types import _Attempt
from pitoolbox.core.errors import DeviceError, DeviceTimeout


class FlipperIr(FlipperAppChannel):
    """`ir_send`: sluit zo nodig eerst de gekoppelde app en start hem daarna opnieuw."""

    def ir_send(self, file: str, button: str) -> dict[str, Any]:
        """Verstuur één knop uit een .ir-bestand via de Infrared-app in RPC-modus.

        Volgorde (rpc_app.c, infrared_scene_rpc.c): app_start(Infrared, "RPC") -> event
        APP_STARTED -> app_load_file -> app_button_press_release(naam) -> app_exit.
        """

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            client = self._ensure_rpc(gen)
            cancel = self._cancel(gen)
            timeout = self._timing.rpc_timeout_s
            # De Infrared-app heeft de loader nodig: de PiToolbox-app eerst sluiten (handoff).
            handoff = self._suspend_rpc_app(client)
            try:
                try:
                    start = flipper_pb2.Main()
                    start.app_start_request.name = "Infrared"
                    start.app_start_request.args = "RPC"  # rpc_app.c vervangt dit door de context
                    client.request(start, timeout, cancel=cancel)
                    self._app_active = True
                    started = client.wait_event(
                        lambda m: _is_app_state(m, application_pb2.APP_STARTED),
                        self._timing.app_event_timeout_s,
                        cancel=cancel,
                    )
                    if started is None:
                        raise DeviceTimeout("Infrared-app meldde niet dat ze gestart is")
                    load = flipper_pb2.Main()
                    load.app_load_file_request.path = file
                    self._request_app_cmd(client, load, timeout, cancel, f"bestand {file} laden")
                    attempt.committed = True
                    self._press_button(client, button, timeout, cancel)
                finally:
                    if self._app_active:
                        self._exit_app(client, timeout=self._timing.app_exit_timeout_s)
            finally:
                if handoff is not None:
                    self._resume_rpc_app(client, handoff)
            return {"file": file, "button": button, "sent": True}

        return self._run("ir_send", op)

    # --- RPC-hulpjes voor de Infrared-app ---------------------------------------------

    def _request_app_cmd(
        self,
        client: RpcClient,
        main: Main,
        timeout: float,
        cancel: Callable[[], bool] | None,
        what: str,
    ) -> None:
        try:
            client.request(main, timeout, cancel=cancel)
        except RpcError as exc:
            if exc.status_name != "ERROR_APP_CMD_ERROR":
                raise
            raise DeviceError(f"Flipper-app: {what} mislukt{self._app_error_text(client)}") from exc

    def _app_error_text(self, client: RpcClient) -> str:
        """Foutmelding van de app (app_get_error_request), best effort."""
        try:
            main = flipper_pb2.Main()
            main.app_get_error_request.SetInParent()
            frames = client.request(main, 2.0)
            text = str(frames[0].app_get_error_response.text).replace("\n", " ").strip()
            return f": {text}" if text else ""
        except DeviceError:
            return ""

    def _press_button(
        self,
        client: RpcClient,
        button: str,
        timeout: float,
        cancel: Callable[[], bool] | None,
    ) -> None:
        main = flipper_pb2.Main()
        main.app_button_press_release_request.args = button
        try:
            self._request_app_cmd(client, main, timeout, cancel, f"knop {button!r} versturen")
            return
        except RpcError as exc:
            if exc.status_name != "ERROR_NOT_IMPLEMENTED":
                raise
        # Oudere firmware kent app_button_press_release_request niet: los indrukken + loslaten.
        press = flipper_pb2.Main()
        press.app_button_press_request.args = button
        self._request_app_cmd(client, press, timeout, cancel, f"knop {button!r} indrukken")
        release = flipper_pb2.Main()
        release.app_button_release_request.SetInParent()
        client.request(release, timeout)  # loslaten altijd, ook na een abort
