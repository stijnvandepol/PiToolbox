"""RPC-client voor een Flipper-sessie bovenop een `ByteTransport`.

Aanpak afgeleid van busse/flipperzero-mcp (MIT), `protobuf_rpc.py` (framing, command_id,
`has_next`-verzameling), maar synchroon en met uitzonderingen in plaats van `None`:

* `request()` verstuurt een `PB.Main` met een eigen command_id en verzamelt alle frames met
  dat command_id tot `has_next == False` (device info, storage list en read komen zo binnen).
* Frames met een ander command_id (ongevraagde `app_state_response`, command_id 0, of een
  laat antwoord op een eerdere timeout) gaan naar een eventqueue; `wait_event()` haalt ze
  daar weer uit. Volgorde van aankomst maakt dus niet uit (event vóór of na het antwoord).
* Een niet-OK `command_status` wordt `RpcError` met de statusnaam uit flipper.proto.

De Flipper verwerkt app-commando's strikt na elkaar (rpc_app.c: `last_command_id == 0`
wordt afgedwongen), dus deze client is bewust niet voor gelijktijdige requests bedoeld.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pitoolbox.core.devices._flipper_rpc.framing import (
    DEFAULT_MAX_FRAME_SIZE,
    FrameDecoder,
    FrameError,
    encode_frame,
)
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2
from pitoolbox.core.errors import Aborted, DeviceError, DeviceTimeout

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pitoolbox.core.devices.flipper import ByteTransport

# PB.Main is gegenereerd; mypy ziet de pb-modules als Any (zie pyproject.toml).
Main = Any

# Nederlandse toelichting bij veelvoorkomende statussen (namen uit flipper.proto).
_STATUS_HINTS: dict[str, str] = {
    "ERROR": "onbekende fout op de Flipper",
    "ERROR_DECODE": "Flipper kon het bericht niet decoderen (sessie wordt gesloten)",
    "ERROR_NOT_IMPLEMENTED": "commando niet ondersteund door deze firmware",
    "ERROR_BUSY": "Flipper is bezet (globale lock)",
    "ERROR_INVALID_PARAMETERS": "ongeldige parameters",
    "ERROR_STORAGE_NOT_READY": "opslag (SD-kaart) niet beschikbaar",
    "ERROR_STORAGE_NOT_EXIST": "pad bestaat niet",
    "ERROR_STORAGE_INVALID_NAME": "ongeldige naam of ongeldig pad",
    "ERROR_STORAGE_DENIED": "toegang geweigerd",
    "ERROR_APP_CANT_START": "app kon niet starten",
    "ERROR_APP_SYSTEM_LOCKED": "er draait al een andere app op de Flipper",
    "ERROR_APP_NOT_RUNNING": "app draait niet of ondersteunt geen RPC",
    "ERROR_APP_CMD_ERROR": "app meldde een fout bij het commando",
}


def status_name(status: int) -> str:
    """Naam van een CommandStatus-waarde, ook voor onbekende waarden."""
    try:
        return str(flipper_pb2.CommandStatus.Name(status))
    except ValueError:
        return f"STATUS_{status}"


class RpcError(DeviceError):
    """Het apparaat antwoordde met een niet-OK `command_status`."""

    code = "rpc_error"

    def __init__(self, status: int, request: str = "") -> None:
        self.status = status
        self.status_name = status_name(status)
        self.request = request
        hint = _STATUS_HINTS.get(self.status_name, "")
        text = f"Flipper-RPC {request or 'commando'} mislukt: {self.status_name}"
        super().__init__(f"{text} ({hint})" if hint else text)


class RpcClient:
    """Eén RPC-sessie. Maak een nieuwe client na elke (her)start van de sessie."""

    def __init__(
        self,
        transport: ByteTransport,
        *,
        max_frame_size: int = DEFAULT_MAX_FRAME_SIZE,
        read_slice_s: float = 0.05,
        max_events: int = 256,
    ) -> None:
        self._transport = transport
        self._decoder = FrameDecoder(max_frame_size)
        self._read_slice_s = read_slice_s
        self._inbox: deque[Main] = deque()
        # Begrensd tegen een apparaat dat blijft sturen; bij vol valt het oudste frame weg en
        # telt `dropped_events` op (gemeten: een FAP kan ~10 DataExchange-frames/s sturen over BLE).
        self._events: deque[Main] = deque(maxlen=max_events)
        self._dropped_events = 0
        self._next_id = 0

    # --- eventqueue -------------------------------------------------------------------

    @property
    def events(self) -> list[Main]:
        """Momentopname van de nog niet afgehandelde ongevraagde frames."""
        return list(self._events)

    def clear_events(self) -> None:
        self._events.clear()

    @property
    def dropped_events(self) -> int:
        """Aantal ongevraagde frames dat wegviel omdat de eventqueue vol was."""
        return self._dropped_events

    def _queue_event(self, frame: Main) -> None:
        if self._events.maxlen is not None and len(self._events) >= self._events.maxlen:
            self._dropped_events += 1
            # Eerste keer en daarna elke 100e: zichtbaar zonder de log te laten vollopen.
            if self._dropped_events % 100 == 1:
                log.warning(
                    "RPC-eventqueue vol (%d); oudste ongevraagde frame weggegooid (totaal %d)",
                    self._events.maxlen,
                    self._dropped_events,
                )
        self._events.append(frame)

    # --- versturen en ontvangen -------------------------------------------------------

    def _alloc_id(self) -> int:
        # command_id 0 is voor ongevraagde berichten van het apparaat; nooit gebruiken.
        self._next_id = self._next_id % 0xFFFFFFFF + 1
        return self._next_id

    def send(self, main: Main) -> int:
        """Verstuur één bericht zonder te wachten; geeft het toegekende command_id terug."""
        cid = self._alloc_id()
        main.command_id = cid
        self._transport.write(encode_frame(main.SerializeToString()))
        return cid

    def _next_frame(self, deadline: float, cancel: Callable[[], bool] | None) -> Main | None:
        """Eén geparsed frame, of None als de deadline verstrijkt."""
        while True:
            if self._inbox:
                return self._inbox.popleft()
            if cancel is not None and cancel():
                raise Aborted("RPC-wachttijd afgebroken")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = self._transport.read(4096, min(self._read_slice_s, remaining))
            if not chunk:
                continue
            for payload in self._decoder.feed(chunk):
                main = flipper_pb2.Main()
                try:
                    main.ParseFromString(payload)
                except Exception as exc:
                    raise FrameError(f"PB.Main niet te parsen: {exc}") from exc
                self._inbox.append(main)

    def request(
        self,
        main: Main,
        timeout: float,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> list[Main]:
        """Verstuur `main` en verzamel alle antwoordframes tot `has_next` False is.

        `timeout` is een inactiviteitstime-out: elk antwoordframe op dit verzoek geeft opnieuw
        `timeout` seconden. Over BLE komt er per verbindingsinterval (~100 ms gemeten op een
        Pi 4) maar één indicatie binnen, dus een antwoord met veel frames (DeviceInfo: ~37)
        zou een vaste deadline niet halen.

        Raises:
            RpcError: niet-OK status in een antwoordframe (of sessiefout met command_id 0).
            DeviceTimeout: `timeout` seconden lang geen (volgend) antwoordframe.
            Aborted: `cancel()` werd waar tijdens het wachten.
        """
        kind = str(main.WhichOneof("content") or "leeg")
        cid = self.send(main)
        deadline = time.monotonic() + timeout
        responses: list[Main] = []
        while True:
            frame = self._next_frame(deadline, cancel)
            if frame is None:
                raise DeviceTimeout(f"geen antwoord van de Flipper op {kind} binnen {timeout:g} s")
            if frame.command_id == cid:
                if frame.command_status != flipper_pb2.OK:
                    raise RpcError(frame.command_status, kind)
                responses.append(frame)
                if not frame.has_next:
                    return responses
                deadline = time.monotonic() + timeout
                continue
            self._queue_event(frame)
            if frame.command_id == 0 and frame.command_status != flipper_pb2.OK:
                # Bijv. ERROR_DECODE: de firmware sluit de sessie hierna zelf.
                raise RpcError(frame.command_status, kind)

    def wait_event(
        self,
        predicate: Callable[[Main], bool],
        timeout: float,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> Main | None:
        """Wacht op een ongevraagd frame waarvoor `predicate` waar is (None bij timeout).

        Eerst wordt de eventqueue doorzocht; niet-passende frames blijven daarin staan.
        """
        for event in list(self._events):
            if predicate(event):
                self._events.remove(event)
                return event
        deadline = time.monotonic() + timeout
        while True:
            frame = self._next_frame(deadline, cancel)
            if frame is None:
                return None
            if predicate(frame):
                return frame
            self._queue_event(frame)
