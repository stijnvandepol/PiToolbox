"""RPC-client voor een Flipper-sessie bovenop een `ByteTransport`.

Aanpak afgeleid van busse/flipperzero-mcp (MIT), `protobuf_rpc.py` (framing, command_id,
`has_next`-verzameling), maar synchroon en met uitzonderingen in plaats van `None`:

* `request()` verstuurt een `PB.Main` met een eigen command_id en verzamelt alle frames met
  dat command_id tot `has_next == False` (device info, storage list en read komen zo binnen).
* Frames met een ander command_id (ongevraagde `app_state_response`, command_id 0, of een
  laat antwoord op een eerdere timeout) gaan naar een eventqueue; `wait_event()` haalt ze
  daar weer uit. Volgorde van aankomst maakt dus niet uit (event vóór of na het antwoord).
* Een niet-OK `command_status` wordt `RpcError` met de statusnaam uit flipper.proto.
* `request_chunked()` verstuurt één verzoek in meerdere frames met hetzelfde command_id en
  `has_next` (zoals `storage_write_request`, zie de docstring daar).
* `event_hook` (optioneel) ziet elk ongevraagd frame vóór de eventqueue en mag het
  overnemen (True). Zo komen DataExchange-frames van de PiToolbox-app meteen bij de bridge,
  ook als ze binnenkomen terwijl `request()` op een ander antwoord wacht, en niet in de
  begrensde queue (fase4-spike.md, test 4). `pump()` leest ongevraagde frames als er geen
  request loopt.

De Flipper verwerkt app-commando's strikt na elkaar (rpc_app.c: `last_command_id == 0`
wordt afgedwongen), dus deze client is bewust niet voor gelijktijdige requests bedoeld.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
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
        event_hook: Callable[[Main], bool] | None = None,
    ) -> None:
        """`event_hook(frame)` krijgt elk ongevraagd frame (in de lezende thread, die de
        device-lock heeft): True = afgehandeld, niet in de eventqueue. Hij mag niet blokkeren
        en deze client niet aanroepen."""
        self._transport = transport
        self._event_hook = event_hook
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
        if self._event_hook is not None:
            try:
                if self._event_hook(frame):
                    return
            except Exception:
                log.exception("event-hook faalde; frame gaat naar de eventqueue")
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

    def _read_some(self, timeout: float) -> None:
        """Lees hoogstens `timeout` s en zet complete frames in de inbox."""
        chunk = self._transport.read(4096, timeout)
        if not chunk:
            return
        for payload in self._decoder.feed(chunk):
            main = flipper_pb2.Main()
            try:
                main.ParseFromString(payload)
            except Exception as exc:
                raise FrameError(f"PB.Main niet te parsen: {exc}") from exc
            self._inbox.append(main)

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
            self._read_some(min(self._read_slice_s, remaining))

    def pump(self, duration: float, *, stop: Callable[[], bool] | None = None) -> int:
        """Lees ongevraagde frames gedurende hoogstens `duration` s (er loopt geen request).

        Elk frame gaat via `event_hook` of de eventqueue. Stopt eerder zodra `stop()` waar is
        (gecontroleerd per leesslice). Geeft het aantal verwerkte frames.
        """
        deadline = time.monotonic() + duration
        handled = 0
        while True:
            while self._inbox:
                self._queue_event(self._inbox.popleft())
                handled += 1
            if stop is not None and stop():
                return handled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return handled
            self._read_some(min(self._read_slice_s, remaining))

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
        return self._collect(cid, kind, timeout, cancel)

    def request_chunked(
        self,
        mains: Sequence[Main],
        timeout: float,
        *,
        cancel: Callable[[], bool] | None = None,
        on_chunk: Callable[[int, int], None] | None = None,
    ) -> list[Main]:
        """Eén verzoek in meerdere frames: hetzelfde command_id, `has_next` op alle frames
        behalve het laatste. Daarna verzamelen zoals `request()`.

        Zo schrijft de officiële client een bestand (flipperzero_protobuf_py
        `flipper_storage.py` `rpc_write`: blokken van 512 B, één command_id). De firmware
        antwoordt pas na het laatste frame, of eerder met een fout (Momentum mntm-012
        `applications/services/rpc/rpc_storage.c`, `rpc_system_storage_write_process`). Na zo'n
        fout zet hij zijn schrijftoestand terug, en een volgend frame met hetzelfde command_id
        zou het bestand opnieuw aanmaken (`FSOM_CREATE_ALWAYS`). Daarom kijkt deze methode na elk
        frame zonder te wachten of er al een antwoord binnen is, en stopt ze bij een fout.
        `on_chunk(verstuurd, totaal)` na elk frame (over BLE: nadat de flow control het toeliet).

        Raises: zoals `request()`.
        """
        if not mains:
            raise ValueError("request_chunked zonder frames")
        kind = str(mains[0].WhichOneof("content") or "leeg")
        cid = self._alloc_id()
        total = len(mains)
        for index, main in enumerate(mains):
            if cancel is not None and cancel():
                raise Aborted("RPC-verzoek in delen afgebroken")
            main.command_id = cid
            main.has_next = index < total - 1
            self._transport.write(encode_frame(main.SerializeToString()))
            if on_chunk is not None:
                on_chunk(index + 1, total)
            if main.has_next:
                self._read_some(0.0)
                self._check_early(cid, kind)
        return self._collect(cid, kind, timeout, cancel)

    def _check_early(self, cid: int, kind: str) -> None:
        """Verwerk wat al binnen is zonder te wachten: een fout op `cid` stopt het verzoek."""
        keep: deque[Main] = deque()
        while self._inbox:
            frame = self._inbox.popleft()
            if frame.command_id == cid:
                if frame.command_status != flipper_pb2.OK:
                    raise RpcError(frame.command_status, kind)
                keep.append(frame)  # onverwacht vroeg OK: laat het voor _collect staan
                continue
            self._queue_event(frame)
            if frame.command_id == 0 and frame.command_status != flipper_pb2.OK:
                raise RpcError(frame.command_status, kind)
        self._inbox.extend(keep)

    def _collect(
        self, cid: int, kind: str, timeout: float, cancel: Callable[[], bool] | None
    ) -> list[Main]:
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
