"""Flipper Zero over Bluetooth LE (seriële GATT-service met flow control), RPC-only.

Bronnen (flipperzero-firmware @3fb1d517):

* UUID's: targets/f7/ble_glue/services/serial_service_uuid.inc. De bytearrays staan daar
  LSB-eerst (ST BLE-stack); `uuid_from_firmware_bytes` draait ze om.
* Eigenschappen en rechten: targets/f7/ble_glue/services/serial_service.c. Alle vier de
  karakteristieken eisen `ATTR_PERMISSION_AUTHEN_*`: de link moet gepaird/gebonden zijn
  (passkey op het Flipper-scherm). Zonder pairing weigert de Flipper elke toegang.
  TX (Flipper -> host): READ | INDICATE. RX (host -> Flipper): WRITE_WITHOUT_RESP | WRITE.
  Flow control: READ | NOTIFY, uint32. RPC-status: READ | WRITE | NOTIFY, uint32.
* Maten: serial_service.h (`BLE_SVC_SERIAL_DATA_LEN_MAX` 486, `..._CHAR_VALUE_LEN_MAX` 243) en
  targets/f7/ble_glue/app_conf.h (`CFG_BLE_MAX_ATT_MTU` = 414).
* Flow control (serial_service.c, bt.c, rpc.c): de karakteristiek bevat de vrije RPC-bufferruimte
  als uint32 met `REVERSE_BYTES_U32`, dus big-endian op de lijn. Initieel `RPC_BUFFER_SIZE`
  (1024, rpc.h), gezet in `ble_svc_serial_set_callbacks` zodra bt.c bij `GapEventTypeConnected`
  de RPC-sessie opent. Bij elke RX-write telt de Flipper `bytes_ready_to_receive` af; pas als dat
  0 is en de RPC-thread de buffer leeg heeft (`ble_svc_serial_notify_buffer_is_empty`) volgt één
  notify met de volle buffergrootte. De host houdt dus zelf een kredietteller bij, schrijft nooit
  meer dan het krediet (ook niet als dat een kleine rest is: alleen bij exact 0 volgt een notify)
  en wacht bij krediet 0 op de notify.
* Op BLE bestaat geen CLI: bt.c opent direct een RPC-sessie (`RpcOwnerBle`). De transport meldt
  daarom `starts_in_rpc = True`; `FlipperDevice` stuurt hier geen `start_rpc_session`.
* Een 0 naar de RPC-statuskarakteristiek triggert `SerialServiceEventTypesBleResetRequest`;
  bt.c zet dan `bt_change_profile` in gang (`furi_hal_bt_change_app`): de hele BLE-link wordt
  afgebroken en opnieuw geadverteerd. Geen sessiereset, dus we gebruiken het niet.
* Advertentie (serial_profile.c, gap.c): naam "Flipper <naam>" en 16-bit service-UUID
  0x3080 | kleur (furi_hal_version.h: kleur 0..3). De 128-bit serial-UUID staat niet in de
  advertentie; de naamprefix is dus de primaire filter.

bleak 3.x (installed: bleak/__init__.py, bleak/backends/bluezdbus/client.py):

* `BleakClient.mtu_size` geeft op BlueZ altijd 23 (alleen via een privé-hack te corrigeren).
  We gebruiken `BleakGATTCharacteristic.max_write_without_response_size` (BlueZ >= 5.62 leest
  daar de `MTU`-property van de karakteristiek: MTU - 3).
* `start_notify` regelt notificaties én indicaties; BlueZ `StartNotify` kiest zelf op basis van
  de flags (TX heeft alleen INDICATE). Indicaties worden door BlueZ bevestigd.
* GATT-fouten komen als `BleakGATTProtocolError` (nieuw in 3.0) met `.code`; BlueZ-weigeringen
  zonder ATT-code (bijv. "Not paired") blijven `BleakDBusError`.
* Een lezing van een karakteristiek waarop al `start_notify` staat, geeft op BlueZ een dubbele
  melding in de notify-callback; daarom lezen we het initiële krediet vóór het abonneren.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import (
    BleakBluetoothNotAvailableError,
    BleakDBusError,
    BleakDeviceNotFoundError,
    BleakError,
    BleakGATTProtocolError,
    BleakGATTProtocolErrorCode,
)

from pitoolbox.core.devices.flipper import TransportError
from pitoolbox.core.errors import Aborted, DeviceError, DeviceNotConnected

log = logging.getLogger(__name__)

T = TypeVar("T")


# --- UUID's uit de firmware ---------------------------------------------------------------


def uuid_from_firmware_bytes(raw: tuple[int, ...] | list[int] | bytes) -> str:
    """Zet een LSB-eerst UUID-bytearray uit serial_service_uuid.inc om naar de gewone string."""
    if len(raw) != 16:
        raise ValueError("een 128-bit UUID heeft 16 bytes")
    return str(uuid.UUID(bytes=bytes(reversed(bytes(raw)))))


# Letterlijk overgenomen uit targets/f7/ble_glue/services/serial_service_uuid.inc.
FW_SERVICE_UUID_BYTES = (
    0x00, 0x00, 0xFE, 0x60, 0xCC, 0x7A, 0x48, 0x2A,
    0x98, 0x4A, 0x7F, 0x2E, 0xD5, 0xB3, 0xE5, 0x8F,
)  # fmt: skip
FW_TX_CHAR_UUID_BYTES = (
    0x00, 0x00, 0xFE, 0x61, 0x8E, 0x22, 0x45, 0x41,
    0x9D, 0x4C, 0x21, 0xED, 0xAE, 0x82, 0xED, 0x19,
)  # fmt: skip
FW_RX_CHAR_UUID_BYTES = (
    0x00, 0x00, 0xFE, 0x62, 0x8E, 0x22, 0x45, 0x41,
    0x9D, 0x4C, 0x21, 0xED, 0xAE, 0x82, 0xED, 0x19,
)  # fmt: skip
FW_FLOW_CONTROL_UUID_BYTES = (
    0x00, 0x00, 0xFE, 0x63, 0x8E, 0x22, 0x45, 0x41,
    0x9D, 0x4C, 0x21, 0xED, 0xAE, 0x82, 0xED, 0x19,
)  # fmt: skip
FW_RPC_STATUS_UUID_BYTES = (
    0x00, 0x00, 0xFE, 0x64, 0x8E, 0x22, 0x45, 0x41,
    0x9D, 0x4C, 0x21, 0xED, 0xAE, 0x82, 0xED, 0x19,
)  # fmt: skip

SERVICE_UUID = uuid_from_firmware_bytes(FW_SERVICE_UUID_BYTES)
TX_CHAR_UUID = uuid_from_firmware_bytes(FW_TX_CHAR_UUID_BYTES)  # Flipper -> host (indicate)
RX_CHAR_UUID = uuid_from_firmware_bytes(FW_RX_CHAR_UUID_BYTES)  # host -> Flipper (write)
FLOW_CONTROL_UUID = uuid_from_firmware_bytes(FW_FLOW_CONTROL_UUID_BYTES)  # vrije RPC-buffer
RPC_STATUS_UUID = uuid_from_firmware_bytes(FW_RPC_STATUS_UUID_BYTES)  # niet gebruikt, zie boven

# 16-bit service-UUID in de advertentie: 0x3080 | kleur (serial_profile.c, furi_hal_version.h).
_ADV_SERVICE_UUIDS = frozenset(
    f"{0x3080 | color:08x}-0000-1000-8000-00805f9b34fb" for color in range(4)
)

# Maten, zie de moduledocstring.
MAX_WRITE_CHUNK = 243  # BLE_SVC_SERIAL_CHAR_VALUE_LEN_MAX
_MIN_ATT_PAYLOAD = 20  # standaard ATT-MTU 23 min 3 bytes header
_MAX_RX_BUFFER = 8 * 1024 * 1024

NOT_PAIRED_MESSAGE = "niet gepaird: draai `pitoolbox-flipper ble-pair`"

_AUTH_ATT_CODES = frozenset(
    {
        BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHENTICATION,
        BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHORIZATION,
        BleakGATTProtocolErrorCode.INSUFFICIENT_ENCRYPTION_KEY_SIZE,
        BleakGATTProtocolErrorCode.INSUFFICIENT_ENCRYPTION,
    }
)
_AUTH_DBUS_ERRORS = frozenset(
    {
        "org.bluez.Error.AuthenticationFailed",
        "org.bluez.Error.AuthenticationCanceled",
        "org.bluez.Error.AuthenticationRejected",
        "org.bluez.Error.AuthenticationTimeout",
        "org.bluez.Error.NotAuthorized",
    }
)
_AUTH_WORDS = (
    "not paired",
    "authentication",
    "authorization",
    "insufficient auth",
    "insufficient enc",
    "encryption",
    "pairing",
)


class BleNotPaired(DeviceError):
    """GATT-toegang geweigerd wegens ontbrekende pairing/versleuteling (ATT 0x05/0x0F/...)."""

    code = "ble_not_paired"


@dataclass(frozen=True)
class BleCandidate:
    """Een gevonden Flipper in de buurt."""

    address: str
    name: str
    rssi: int | None


def is_auth_error(exc: BaseException) -> bool:
    """Is dit een weigering door ontbrekende pairing? (ATT-codes of BlueZ-tekst)."""
    if isinstance(exc, BleakGATTProtocolError):
        return exc.code in _AUTH_ATT_CODES
    if isinstance(exc, BleakDBusError) and exc.dbus_error in _AUTH_DBUS_ERRORS:
        return True
    if isinstance(exc, BleakError):
        text = str(exc).lower()
        return any(word in text for word in _AUTH_WORDS)
    return False


# --- scannen ------------------------------------------------------------------------------


def _adv_name(device: BLEDevice, adv: AdvertisementData | None) -> str:
    return str((adv.local_name if adv is not None else None) or device.name or "")


def _is_flipper(device: BLEDevice, adv: AdvertisementData | None, name_prefix: str) -> bool:
    """Naamprefix, of een van de bekende service-UUID's in de advertentie."""
    name = _adv_name(device, adv)
    if name_prefix and name.casefold().startswith(name_prefix.casefold()):
        return True
    if adv is None:
        return False
    uuids = {u.lower() for u in adv.service_uuids}
    return bool(uuids & _ADV_SERVICE_UUIDS) or SERVICE_UUID in uuids


def _run_blocking(coro: Coroutine[Any, Any, T], timeout: float) -> T:
    """Draai een coroutine synchroon in een eigen thread/loop (ook onder een lopende loop)."""
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ble-scan"
    ) as pool:
        return pool.submit(asyncio.run, coro).result(timeout)


async def _scan(name_prefix: str, timeout: float) -> list[BleCandidate]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    candidates: list[BleCandidate] = []
    for device, adv in found.values():
        if _is_flipper(device, adv, name_prefix):
            candidates.append(BleCandidate(device.address, _adv_name(device, adv), adv.rssi))
    candidates.sort(key=lambda c: c.rssi if c.rssi is not None else -999, reverse=True)
    return candidates


def scan_flippers(name_prefix: str = "Flipper", timeout: float = 8.0) -> list[BleCandidate]:
    """Zoek Flippers in de buurt (sterkste signaal eerst).

    Een Flipper adverteert alleen als hij BLE aan heeft en niet al met een telefoon/ander
    apparaat verbonden is.
    """
    try:
        return _run_blocking(_scan(name_prefix, timeout), timeout + 20.0)
    except BleakBluetoothNotAvailableError as exc:
        raise DeviceError(f"Bluetooth is niet beschikbaar: {exc}") from exc
    except (BleakError, OSError, TimeoutError) as exc:
        raise DeviceError(f"BLE-scan mislukt: {exc}") from exc


# --- eigen event loop ---------------------------------------------------------------------


class _LoopThread:
    """Een asyncio-loop in een daemon-thread; sync code roept er coroutines in aan."""

    def __init__(self, name: str) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro: Coroutine[Any, Any, T], timeout: float) -> T:
        """Voer `coro` uit in de loop; bij een time-out wordt hij geannuleerd."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def stop(self) -> None:
        if self.loop.is_closed():
            return

        async def cancel_all() -> None:
            me = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not me]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(cancel_all(), self.loop).result(3.0)
        except Exception as exc:  # noqa: BLE001 - opruimen mag nooit falen
            log.debug("taken annuleren in BLE-loop mislukte: %s", exc)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3.0)
        if not self._thread.is_alive():
            self.loop.close()


# --- transport ----------------------------------------------------------------------------


def _parse_flow_credit(raw: bytes | bytearray) -> int:
    """Flow-control-waarde: uint32 big-endian (REVERSE_BYTES_U32 in serial_service.c)."""
    if len(raw) != 4:
        raise DeviceError(f"onverwachte flow-control-waarde van {len(raw)} bytes (verwacht 4)")
    return int.from_bytes(bytes(raw), "big")


class BleTransport:
    """`ByteTransport` over de Flipper-BLE-serial-service (sync API bovenop async bleak).

    Zonder `address` wordt de eerste Flipper gezocht waarvan de naam met `name_prefix` begint;
    een vast adres is aanbevolen (en nodig als er meerdere Flippers in de buurt zijn).
    """

    kind = "ble"
    starts_in_rpc = True

    def __init__(
        self,
        address: str | None = None,
        name_prefix: str = "Flipper",
        *,
        scan_timeout_s: float = 8.0,
        connect_timeout_s: float = 20.0,
        credit_timeout_s: float = 10.0,
        write_timeout_s: float = 5.0,
        write_with_response: bool = False,
    ) -> None:
        self._address = address
        self._name_prefix = name_prefix
        self._name: str | None = None
        self._scan_timeout_s = scan_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._credit_timeout_s = credit_timeout_s
        self._write_timeout_s = write_timeout_s
        self._write_with_response = write_with_response
        # open/close en schrijven worden elk apart geserialiseerd.
        self._open_lock = threading.RLock()
        self._write_lock = threading.Lock()
        # Alles hieronder wordt onder `_cond` gewijzigd (ook vanuit de bleak-callbacks).
        self._cond = threading.Condition()
        self._open = False
        self._link_lost = False
        self._reason = "nog niet geopend"
        self._rx = bytearray()
        self._credit = 0
        self._max_chunk = _MIN_ATT_PAYLOAD
        self._abort_seq = 0
        self._use_response = False
        self._loop: _LoopThread | None = None
        self._client: BleakClient | None = None

    # --- ByteTransport ----------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    def describe(self) -> str:
        address = self._address or "(auto)"
        name = self._name or f"{self._name_prefix}*"
        return f"ble:{address} ({name})"

    def open(self) -> None:
        with self._open_lock:
            if self._open:
                return
            self._teardown()  # restanten van een eerdere (mislukte of weggevallen) sessie
            with self._cond:
                self._rx.clear()
                self._credit = 0
                self._link_lost = False
                self._reason = "verbinden"
            loop = _LoopThread("pitoolbox-ble")
            self._loop = loop
            total_timeout = self._scan_timeout_s + self._connect_timeout_s + 15.0
            try:
                loop.call(self._async_open(), total_timeout)
            except Exception as exc:
                self._teardown()
                raise self._translate_open_error(exc) from exc
            log.info("BLE-verbinding met %s open", self.describe())

    def close(self) -> None:
        with self._open_lock:
            self._mark_closed("gesloten")
            self._teardown()

    def read(self, max_bytes: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._rx:
                if not self._open:
                    raise TransportError(f"BLE-verbinding is niet open ({self._reason})")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            chunk = bytes(self._rx[:max_bytes])
            del self._rx[:max_bytes]
            return chunk

    def reset_input(self) -> None:
        with self._cond:
            self._rx.clear()

    def interrupt(self) -> None:
        """Maak een schrijfactie wakker die op flow-control-krediet wacht (abort)."""
        with self._cond:
            self._abort_seq += 1
            self._cond.notify_all()

    def write(self, data: bytes) -> None:
        """Schrijf `data` in stukken van maximaal min(krediet, MTU-3, 243) bytes.

        Blokkeert bij krediet 0 tot de Flipper via een notify weer buffer meldt. Na een
        time-out, abort of fout midden in een bericht is de RPC-stroom van de Flipper niet meer
        in de pas; de link wordt dan als gesloten gemarkeerd zodat er opnieuw verbonden wordt.
        """
        if not data:
            return
        payload = bytes(data)
        with self._write_lock:
            with self._cond:
                abort_seq = self._abort_seq
            offset = 0
            while offset < len(payload):
                size = self._reserve_credit(len(payload) - offset, abort_seq, offset > 0)
                self._send_chunk(payload[offset : offset + size])
                offset += size

    # --- schrijven: flow control ------------------------------------------------------

    def _reserve_credit(self, remaining: int, abort_seq: int, mid_message: bool) -> int:
        """Wacht op krediet en reserveer maximaal `remaining` bytes (telt het krediet af)."""
        deadline = time.monotonic() + self._credit_timeout_s
        with self._cond:
            while True:
                if not self._open:
                    raise TransportError(f"BLE-verbinding is niet open ({self._reason})")
                if self._abort_seq != abort_seq:
                    if mid_message:
                        self._mark_closed_locked("afgebroken midden in een bericht")
                    raise Aborted("BLE-schrijfactie afgebroken")
                if self._credit > 0:
                    break
                wait = deadline - time.monotonic()
                if wait <= 0:
                    self._mark_closed_locked("geen flow-control-krediet van de Flipper")
                    raise TransportError(
                        f"Flipper meldde {self._credit_timeout_s:g} s lang geen vrije "
                        "RPC-buffer (flow control); verbinding wordt opnieuw opgezet"
                    )
                self._cond.wait(wait)
            size = min(remaining, self._credit, self._max_chunk)
            self._credit -= size
            return size

    def _send_chunk(self, chunk: bytes) -> None:
        loop = self._loop
        if loop is None:
            raise TransportError("BLE-verbinding is niet open")
        try:
            loop.call(self._async_write(chunk), self._write_timeout_s)
        except Exception as exc:
            self._mark_closed(f"schrijffout: {exc}")
            if is_auth_error(exc):
                raise BleNotPaired(NOT_PAIRED_MESSAGE) from exc
            raise TransportError(f"BLE-schrijffout: {exc}") from exc
        log.debug("BLE: %d bytes geschreven", len(chunk))

    async def _async_write(self, chunk: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise BleakError("Not connected")
        await client.write_gatt_char(RX_CHAR_UUID, chunk, response=self._use_response)

    # --- verbinden --------------------------------------------------------------------

    async def _resolve_device(self) -> BLEDevice:
        if self._address:
            device = await BleakScanner.find_device_by_address(
                self._address, timeout=self._scan_timeout_s
            )
            if device is None:
                raise DeviceNotConnected(
                    f"Flipper {self._address} niet gevonden via BLE. Staat Bluetooth aan op de "
                    "Flipper en is hij niet al verbonden met een telefoon?"
                )
            return device
        prefix = self._name_prefix
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: _is_flipper(d, ad, prefix), timeout=self._scan_timeout_s
        )
        if device is None:
            raise DeviceNotConnected(
                f"Geen Flipper gevonden via BLE (naam begint met {prefix!r}). Staat Bluetooth "
                "aan op de Flipper? Stel PITOOLBOX_FLIPPER_BLE_ADDRESS in voor een vast adres."
            )
        log.info("BLE: Flipper %s gekozen op naamprefix", device.address)
        return device

    async def _async_open(self) -> None:
        device = await self._resolve_device()
        self._address = device.address
        self._name = device.name or None
        client = BleakClient(
            device,
            disconnected_callback=self._on_disconnected,
            timeout=self._connect_timeout_s,
        )
        self._client = client
        await client.connect()

        services = client.services
        rx_char = services.get_characteristic(RX_CHAR_UUID)
        missing = [
            u
            for u, c in (
                (RX_CHAR_UUID, rx_char),
                (TX_CHAR_UUID, services.get_characteristic(TX_CHAR_UUID)),
                (FLOW_CONTROL_UUID, services.get_characteristic(FLOW_CONTROL_UUID)),
            )
            if c is None
        ]
        if rx_char is None or missing:
            raise DeviceError(
                "Dit BLE-apparaat heeft de seriële Flipper-service niet "
                f"(ontbrekend: {', '.join(missing)}); is dit een Flipper met BLE-RPC?"
            )

        # Eerste GATT-toegang op een karakteristiek met AUTHEN-rechten: zonder pairing faalt
        # dit hier met een duidelijke fout. (Een write-without-response zou stil verdwijnen.)
        credit = _parse_flow_credit(await client.read_gatt_char(FLOW_CONTROL_UUID))
        if credit == 0:
            await asyncio.sleep(0.5)  # de Flipper zet het krediet direct na het verbinden
            credit = _parse_flow_credit(await client.read_gatt_char(FLOW_CONTROL_UUID))
        if credit == 0:
            raise DeviceError(
                "De Flipper heeft geen RPC-sessie over BLE geopend (flow-control-buffer 0). "
                "Is de BLE-RPC bezet door de telefoon-app of qFlipper?"
            )
        with self._cond:
            self._credit = credit

        await client.start_notify(TX_CHAR_UUID, self._on_tx)
        await client.start_notify(FLOW_CONTROL_UUID, self._on_flow)

        limit = rx_char.max_write_without_response_size
        deadline = time.monotonic() + 1.5
        while limit <= _MIN_ATT_PAYLOAD and time.monotonic() < deadline:
            await asyncio.sleep(0.1)  # BlueZ vult de MTU-property soms pas iets later
            limit = rx_char.max_write_without_response_size
        max_chunk = max(1, min(limit, MAX_WRITE_CHUNK))
        use_response = self._write_with_response or "write-without-response" not in (
            rx_char.properties
        )

        with contextlib.suppress(BleakError):
            self._name = self._name or client.name
        with self._cond:
            if self._link_lost or not client.is_connected:
                raise DeviceNotConnected("BLE-verbinding viel direct na het verbinden weg")
            self._max_chunk = max_chunk
            self._use_response = use_response
            self._open = True
            self._reason = "open"
        log.info("BLE: krediet %d bytes, schrijfgrootte max %d bytes", credit, max_chunk)

    def _translate_open_error(self, exc: Exception) -> DeviceError:
        if isinstance(exc, DeviceError):
            return exc
        if isinstance(exc, BleakDeviceNotFoundError):
            return DeviceNotConnected(
                f"Flipper niet gevonden door BlueZ ({exc}); staat hij aan en in de buurt?"
            )
        if is_auth_error(exc):
            return BleNotPaired(NOT_PAIRED_MESSAGE)
        if isinstance(exc, TimeoutError):
            return DeviceNotConnected("BLE-verbinding met de Flipper maken duurde te lang")
        if isinstance(exc, BleakBluetoothNotAvailableError):
            return DeviceNotConnected(f"Bluetooth is niet beschikbaar: {exc}")
        return DeviceNotConnected(f"BLE-verbinding met de Flipper mislukt: {exc}")

    # --- callbacks (draaien in de bleak-loop) ----------------------------------------

    def _on_tx(self, _sender: Any, data: bytearray) -> None:
        with self._cond:
            self._rx.extend(data)
            if len(self._rx) > _MAX_RX_BUFFER:
                log.error("BLE: ontvangstbuffer te groot; verbinding wordt gesloten")
                self._rx.clear()
                self._mark_closed_locked("ontvangstbuffer overschreden")
            self._cond.notify_all()

    def _on_flow(self, _sender: Any, data: bytearray) -> None:
        try:
            credit = _parse_flow_credit(data)
        except DeviceError as exc:
            log.warning("BLE: %s", exc)
            return
        with self._cond:
            # De Flipper meldt de volle bufferruimte pas als zijn teller op 0 stond; de waarde is
            # dus absoluut, niet "erbij".
            self._credit = credit
            self._cond.notify_all()
        log.debug("BLE: flow-control-krediet nu %d", credit)

    def _on_disconnected(self, _client: Any) -> None:
        log.warning("BLE: verbinding met %s verbroken", self.describe())
        with self._cond:
            self._link_lost = True
        self._mark_closed("verbinding verbroken")

    # --- toestand en opruimen ---------------------------------------------------------

    def _mark_closed(self, reason: str) -> None:
        with self._cond:
            self._mark_closed_locked(reason)

    def _mark_closed_locked(self, reason: str) -> None:
        if self._open:
            self._open = False
            self._reason = reason
        self._cond.notify_all()

    def _teardown(self) -> None:
        """Verbreek de link en stop de loop (idempotent; nooit een uitzondering)."""
        loop, client = self._loop, self._client
        self._loop = None
        self._client = None
        if loop is None:
            return
        if client is not None:
            try:
                loop.call(client.disconnect(), 8.0)
            except Exception as exc:  # noqa: BLE001 - opruimen mag de oorspronkelijke fout niet verhullen
                log.debug("BLE: disconnect mislukte: %s", exc)
        loop.stop()
