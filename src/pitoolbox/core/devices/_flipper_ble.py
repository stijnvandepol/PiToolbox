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
  `BleakGATTCharacteristic.max_write_without_response_size` leest de `MTU`-property uit de cache
  van bleak (manager.py `get_max_write_without_response_size`: MTU - 3). Die cache blijft bij
  een gebonden Flipper op 23 staan: BlueZ exporteert de karakteristieken uit zijn GATT-cache
  vóór de verbinding, en `characteristic_create` registreert de `att_exchange`-callback (die
  PropertiesChanged voor "MTU" stuurt) alleen als er al een ATT-bearer is (BlueZ 5.87
  src/gatt-client.c). `MTU` zelf wordt live gelezen (`characteristic_get_mtu`:
  `bt_att_get_mtu`). Daarom vragen we de property na het verbinden zelf op via D-Bus
  (gemeten op de Pi: bleak 23, D-Bus 414 = `CFG_BLE_MAX_ATT_MTU` van de Flipper).
* Adapterkeuze: `bluez={"adapter": "hciN"}` op `BleakScanner` en `BleakClient`
  (bleak/args/bluez.py `BlueZScannerArgs`/`BlueZClientArgs`, nieuw in 3.0; het oude `adapter=`
  is deprecated). Een onbekende adapter geeft `BleakError("adapter 'hciN' not found")`
  (manager.py `_check_adapter`).
* `BleakClient(<adres-string>)` doet bij `connect()` eerst `find_device_by_address` (een scan).
  Met een `BLEDevice` waarvan `details` het BlueZ-pad en de properties bevat (zoals de BlueZ-
  scanner ze maakt: scanner.py `_handle_advertising_data`, client.py `__init__`) wordt direct
  `Device1.Connect` aangeroepen. Voor een gebonden Flipper die BlueZ al kent slaan we de scan
  dus over (gemeten: verbinden 1,3 s zonder scan; met scan 1,5 s tot 5 s).
* bleak slaat `Connect` over als BlueZ het apparaat al verbonden heeft (client.py: "skipping
  calling Connect", BlueZ issue #89), bijv. na een gecrasht proces. Dan zouden we in de oude
  RPC-sessie van de Flipper verder schrijven; we verbreken zo'n link daarom eerst
  (`Device1.Disconnect`).
* `Device1.Disconnect` wacht in BlueZ altijd `DISCONNECT_TIMER` (2 s) voordat de link echt
  verbroken wordt (src/device.c `device_request_disconnect`); sluiten duurt daarom ~2,5 s.
* `start_notify` regelt notificaties én indicaties; BlueZ `StartNotify` kiest zelf op basis van
  de flags (TX heeft alleen INDICATE). Indicaties worden door BlueZ bevestigd.
* GATT-fouten komen als `BleakGATTProtocolError` (nieuw in 3.0) met `.code`; BlueZ-weigeringen
  zonder ATT-code (bijv. "Not paired") blijven `BleakDBusError`.
* Een lezing van een karakteristiek waarop al `start_notify` staat, geeft op BlueZ een dubbele
  melding in de notify-callback; daarom lezen we het initiële krediet vóór het abonneren.

Snelheid (~100 ms per RPC-frame, gemeten op een Pi 4 met BCM43455): de TX-karakteristiek
gebruikt indicaties, en bt.c (`bt_rpc_send_bytes_callback`) wacht na elk pakket op de
bevestiging (`SerialServiceEventTypeDataSent` bij `ACI_GATT_SERVER_CONFIRMATION_VSEVT_CODE`).
Elk antwoordframe kost dus minstens een heen-en-terug over de link, in de praktijk twee
verbindingsintervallen. Het interval kiest de central: Linux begint met
`le_conn_min/max_interval` 0x0018..0x0028 (30..50 ms, net/bluetooth/hci_core.c) of de per-
apparaat opgeslagen parameters (hci_sync.c `hci_le_create_conn_sync`). Momentum mntm-012
(targets/f7/ble_glue/gap.c `gap_verify_connection_parameters`, serial_profile.c 0x06..0x24 =
7,5..45 ms) vraagt alleen een update aan als het interval onder 7,5 ms ligt, of als de link al
beveiligd is en het interval boven het maximum ligt. Die controle draait alleen bij
"connection complete" (dan is `is_secure` nog false) en bij "connection update complete", dus
bij een verbinding vanaf Linux wordt er nooit om een korter interval gevraagd. Zonder root kan
een central op Linux het interval niet beïnvloeden (geen D-Bus-API; alleen main.conf [LE]
Min/MaxConnectionInterval, mgmt of debugfs).

Opbouw: `_flipper_ble_scan` (UUID's, herkennen, scannen), `_flipper_ble_gatt` (pairingfouten,
BlueZ-opzoekingen, MTU, krediet), `_flipper_ble_link` (toestand, verbinden, callbacks) en hier de
`ByteTransport`-kant (lezen, schrijven met flow control). Andere code importeert uit deze module.
"""

from __future__ import annotations

import contextlib
import logging
import time

from bleak.exc import BleakError

from pitoolbox.core.devices import _ble_pairing
from pitoolbox.core.devices._ble_pairing import BlePairingError, BluezBus, BusFactory
from pitoolbox.core.devices._flipper_ble_gatt import (
    _STALE_DISCONNECT_WAIT_S,
    GATT_CHAR_IFACE,
    MAX_WRITE_CHUNK,
    NOT_PAIRED_MESSAGE,
    BleNotPaired,
    is_auth_error,
)
from pitoolbox.core.devices._flipper_ble_link import BleLink, _LoopThread
from pitoolbox.core.devices._flipper_ble_scan import (
    FLIPPER_UUIDS,
    FLOW_CONTROL_UUID,
    FW_FLOW_CONTROL_UUID_BYTES,
    FW_RPC_STATUS_UUID_BYTES,
    FW_RX_CHAR_UUID_BYTES,
    FW_SERVICE_UUID_BYTES,
    FW_TX_CHAR_UUID_BYTES,
    RPC_STATUS_UUID,
    RX_CHAR_UUID,
    SERVICE_UUID,
    TX_CHAR_UUID,
    BleCandidate,
    scan_flippers,
    uuid_from_firmware_bytes,
)
from pitoolbox.core.devices._flipper_transport import TransportError
from pitoolbox.core.errors import Aborted

__all__ = [
    "FLIPPER_UUIDS",
    "FLOW_CONTROL_UUID",
    "FW_FLOW_CONTROL_UUID_BYTES",
    "FW_RPC_STATUS_UUID_BYTES",
    "FW_RX_CHAR_UUID_BYTES",
    "FW_SERVICE_UUID_BYTES",
    "FW_TX_CHAR_UUID_BYTES",
    "GATT_CHAR_IFACE",
    "MAX_WRITE_CHUNK",
    "NOT_PAIRED_MESSAGE",
    "RPC_STATUS_UUID",
    "RX_CHAR_UUID",
    "SERVICE_UUID",
    "TX_CHAR_UUID",
    "BleCandidate",
    "BleNotPaired",
    "BleTransport",
    "is_auth_error",
    "scan_flippers",
    "uuid_from_firmware_bytes",
]

log = logging.getLogger(__name__)

# Testnaad: de D-Bus-verbinding voor de BlueZ-opzoekingen (None = dbus-fast, system bus).
_bluez_bus_factory: BusFactory | None = None


# --- transport ----------------------------------------------------------------------------


class BleTransport(BleLink):
    """`ByteTransport` over de Flipper-BLE-serial-service (sync API bovenop async bleak).

    Zonder `address` wordt de eerste Flipper gezocht waarvan de naam met `name_prefix` begint;
    een vast adres is aanbevolen (en nodig als er meerdere Flippers in de buurt zijn). Een
    gebonden Flipper die BlueZ al kent wordt zonder scan verbonden. `adapter` (`hciN`) kiest de
    Bluetooth-adapter; None = de standaardadapter van BlueZ/bleak.
    """

    kind = "ble"
    starts_in_rpc = True

    # --- ByteTransport ----------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

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
            total_timeout = (
                self._scan_timeout_s + self._connect_timeout_s + _STALE_DISCONNECT_WAIT_S + 15.0
            )
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

    async def _open_bluez(self) -> BluezBus | None:
        """Eigen D-Bus-verbinding voor de BlueZ-opzoekingen; None als die er niet is (dan doet
        bleak alles zelf, zoals op een systeem zonder dbus-fast)."""
        try:
            return await _ble_pairing.connect_bus(_bluez_bus_factory)
        except BlePairingError as exc:
            log.debug("BLE: BlueZ niet via D-Bus te bevragen (%s); alleen bleak", exc)
            return None

    async def _async_open(self) -> None:
        bus = await self._open_bluez()
        try:
            await self._async_connect(bus)
        finally:
            if bus is not None:
                with contextlib.suppress(Exception):
                    await bus.close()
