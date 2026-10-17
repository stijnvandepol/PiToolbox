"""Ontwikkelaarsmodus op afstand: knoppen op de Flipper indrukken en het scherm lezen.

**Alleen voor ontwikkeling** (`pitoolbox-flipper remote`, `ui-test --confirm-check --auto`).
Met invoer op afstand kan de Pi zelf OK drukken op een "Flipper verplicht"-vraag, en dan is
die bevestiging niets meer waard. Dat mag alleen in de testopzet van `ui-test` (allowlist
alleen `status`, lege scope: niets kan uitzenden), nooit in het product. Daarom is er geen
ToolSpec, geen runner-pad en geen aanroep vanuit MCP, de agent of de bridge;
`tests/test_flipper_remote.py` controleert dat. Zie ook docs/fase4-protocol.md §11.

**Wachter, vóór elke knopdruk opnieuw** (`_check_remote_input_allowed`): drukken mag alleen
als onze eigen gekoppelde PiToolbox-app open is, of als er geen app open is (bureaublad, menu,
Apps-browser). Staat er een andere app open, dan weigert het device (`RemoteInputRefused`): een
OK op afstand in bijvoorbeeld Sub-GHz of Infrared kan uitzenden. Elke knopdruk komt als
WARNING in de log ("invoer op afstand: OK").

Bronnen:

* flipperzero-protobuf `gui.proto` (de commit van pb/, scripts/gen_proto.py):
  `SendInputEventRequest{key, type}`, `Start/StopScreenStreamRequest`,
  `ScreenFrame{data, orientation}`.
* Een klik zoals de officiële Python-client: PRESS, dan SHORT (of LONG), dan RELEASE
  (flipperzero_protobuf_py `flipperzero_protobuf/flipper_gui.py`, `rpc_gui_send_input`).
* Momentum mntm-012 `applications/services/rpc/rpc_gui.c`: alleen PRESS begint een
  toetsreeks en RELEASE sluit hem (`input_key_counter`); een event zonder PRESS publiceert hij
  toch, met een waarschuwing. Start geeft `ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED` als de stream
  al loopt, anders eerst OK en dan de framebuffer-callback; `gui_add_framebuffer_callback` vraagt
  meteen een redraw (`gui.c`), dus er komt altijd een eerste frame. Frames (command_id 0)
  verstuurt een eigen thread bij elke redraw. Stop geeft altijd OK, pas nadat die thread gestopt
  is: na het OK komt er geen frame meer. Sluit de sessie, dan laat de firmware ingedrukte toetsen
  los (`rpc_system_gui_free`).
* Momentum mntm-012 `applications/services/loader/loader.c`: `loader_do_is_locked` is
  `app.thread != NULL`. Het hoofdmenu en de Apps-browser zijn geen app; tijdens het navigeren
  naar Apps > Tools > PiToolbox is de loader dus vrij.

**Schermbeelden** komen via de gewone lezer binnen: `wait_event` pakt het eerste frame na de
start; andere ongevraagde frames (DataExchange van de app, `APP_CLOSED`) gaan intussen gewoon via
de event-hook naar de bridge. Een nagekomen frame gooit de hook weg (`_flipper_link_core`).
"""

from __future__ import annotations

import logging

from pitoolbox.core.devices._flipper_app_channel import FlipperAppChannel
from pitoolbox.core.devices._flipper_rpc.client import Main, RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2, gui_pb2
from pitoolbox.core.devices._flipper_screen import FRAME_BYTES, ScreenFrame
from pitoolbox.core.devices._flipper_types import RemoteInputRefused, _Attempt
from pitoolbox.core.errors import DeviceError

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

REMOTE_KEYS: dict[str, gui_pb2.InputKey] = {
    "up": gui_pb2.UP,
    "down": gui_pb2.DOWN,
    "left": gui_pb2.LEFT,
    "right": gui_pb2.RIGHT,
    "ok": gui_pb2.OK,
    "back": gui_pb2.BACK,
}
SCREEN_TIMEOUT_S = 5.0  # één frame is ~1 KiB; over BLE ~1 s (fase4-protocol.md §8)
SCREEN_TIMEOUT_MAX_S = 60.0
_ALREADY_STREAMING = "ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED"

_REFUSED = (
    "Er is een andere app open op de Flipper dan de PiToolbox-app van deze Pi. Invoer op "
    "afstand weigert dan: een OK in bijvoorbeeld Sub-GHz of Infrared kan uitzenden. Sluit die "
    "app op de Flipper zelf."
)


def _is_screen_frame(main: Main) -> bool:
    return bool(main.WhichOneof("content") == "gui_screen_frame")


def _orientation_name(value: int) -> str:
    try:
        return str(gui_pb2.ScreenOrientation.Name(value))
    except ValueError:
        return f"ORIENTATION_{value}"


class FlipperRemote(FlipperAppChannel):
    """`gui_press` en `gui_screen`; alleen voor de ontwikkelaarsmodus (zie de moduledocstring)."""

    def gui_press(self, key: str, *, long: bool = False) -> None:
        """Eén klik op `key` (up/down/left/right/ok/back): PRESS, SHORT of LONG, RELEASE.

        Raises:
            ValueError: onbekende knop.
            RemoteInputRefused: er is een andere app open (de wachter, vóór elke klik).
            DeviceError: verbinding of RPC mislukt.
        """
        pb_key = REMOTE_KEYS.get(key)
        if pb_key is None:
            raise ValueError(f"onbekende knop {key!r}; kies uit {', '.join(REMOTE_KEYS)}")
        label = key.upper() + (" (lang)" if long else "")

        def op(gen: int, attempt: _Attempt) -> None:
            client = self._ensure_rpc(gen)
            where = self._check_remote_input_allowed(client)
            log.warning("invoer op afstand: %s (%s)", label, where)
            attempt.committed = True  # na een halve reeks nooit vanzelf opnieuw drukken
            self._send_input(client, pb_key, gui_pb2.PRESS)
            try:
                self._send_input(client, pb_key, gui_pb2.LONG if long else gui_pb2.SHORT)
            finally:
                self._send_input(client, pb_key, gui_pb2.RELEASE)  # nooit een knop laten hangen

        self._run("gui_press", op)

    def gui_screen(self, timeout_s: float = SCREEN_TIMEOUT_S) -> ScreenFrame:
        """Eén schermbeeld: stream starten, het eerste frame afwachten, stream altijd stoppen.

        Raises:
            ValueError: `timeout_s` buiten (0, 60].
            DeviceError: geen frame binnen `timeout_s`, een frame van de verkeerde grootte, of
                verbinding/RPC mislukt.
        """
        if not 0 < timeout_s <= SCREEN_TIMEOUT_MAX_S:
            raise ValueError(f"time-out moet tussen 0 en {SCREEN_TIMEOUT_MAX_S:g} s liggen")

        def op(gen: int, attempt: _Attempt) -> ScreenFrame:
            client = self._ensure_rpc(gen)
            self._start_screen_stream(client)
            try:
                frame = client.wait_event(_is_screen_frame, timeout_s, cancel=self._cancel(gen))
            finally:
                self._stop_screen_stream(client)
            # Geen DeviceTimeout: die sluit over BLE de hele link, en de link is niet stuk.
            if frame is None:
                raise DeviceError(f"geen schermbeeld van de Flipper binnen {timeout_s:g} s")
            data = bytes(frame.gui_screen_frame.data)
            if len(data) != FRAME_BYTES:
                raise DeviceError(f"schermbeeld van {len(data)} bytes; verwacht {FRAME_BYTES}")
            return ScreenFrame(data, _orientation_name(frame.gui_screen_frame.orientation))

        return self._run("gui_screen", op)

    # --- intern ---------------------------------------------------------------------------

    def _check_remote_input_allowed(self, client: RpcClient) -> str:
        """Onder de lock, vóór elke klik. Geeft waarom het mag; anders `RemoteInputRefused`.

        Eerst de loader vragen: dat request leest ook alles wat er al binnen was. Sloot de
        gebruiker onze app en opende hij iets anders, dan is die `APP_CLOSED` (die vóór het
        antwoord in de stroom staat) dus verwerkt voordat we naar `_rpc_app_state` kijken.
        """
        if not self._loader_locked(client):
            return "geen app open"
        if self._rpc_app_state == "active":
            return "PiToolbox-app"  # die houdt de loader bezet; een andere app kan dan niet open
        raise RemoteInputRefused(_REFUSED)

    def _send_input(
        self, client: RpcClient, key: gui_pb2.InputKey, kind: gui_pb2.InputType
    ) -> None:
        main = flipper_pb2.Main()
        main.gui_send_input_event_request.key = key
        main.gui_send_input_event_request.type = kind
        client.request(main, self._timing.rpc_timeout_s)

    def _start_screen_stream(self, client: RpcClient) -> None:
        start = flipper_pb2.Main()
        start.gui_start_screen_stream_request.SetInParent()
        try:
            client.request(start, self._timing.rpc_timeout_s)
        except RpcError as exc:
            if exc.status_name != _ALREADY_STREAMING:
                raise
            # Een eerdere stream van deze sessie liep nog (bijv. een stop die niet aankwam).
            # Opnieuw starten, want alleen de start vraagt een redraw en dus een vers frame.
            log.info("schermstream liep nog; opnieuw gestart")
            self._stop_screen_stream(client)
            client.request(start, self._timing.rpc_timeout_s)

    def _stop_screen_stream(self, client: RpcClient) -> None:
        stop = flipper_pb2.Main()
        stop.gui_stop_screen_stream_request.SetInParent()
        client.request(stop, self._timing.rpc_timeout_s)
