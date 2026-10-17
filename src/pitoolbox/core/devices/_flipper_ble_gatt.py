"""GATT- en BlueZ-hulpjes voor de BLE-verbinding: pairingfouten, BlueZ-opzoekingen, MTU en krediet.

Bronnen (serial_service.c/.h, app_conf.h, BlueZ device.c/gatt-client.c, bleak): zie
_flipper_ble.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from bleak.exc import (
    BleakDBusError,
    BleakError,
    BleakGATTProtocolError,
    BleakGATTProtocolErrorCode,
)

from pitoolbox.core.devices import _ble_pairing
from pitoolbox.core.devices._ble_pairing import (
    DEVICE_IFACE,
    BlePairingError,
    BluezBus,
    BluezDBusError,
)
from pitoolbox.core.devices._flipper_ble_scan import FLOW_CONTROL_UUID, RX_CHAR_UUID, TX_CHAR_UUID
from pitoolbox.core.errors import DeviceError, DeviceNotConnected

if TYPE_CHECKING:
    from bleak import BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.service import BleakGATTServiceCollection

# Loggernaam van _flipper_ble.py: alle logregels van de BLE-transport onder één naam.
log = logging.getLogger("pitoolbox.core.devices._flipper_ble")

# Maten, zie de docstring van _flipper_ble.
MAX_WRITE_CHUNK = 243  # BLE_SVC_SERIAL_CHAR_VALUE_LEN_MAX
_MIN_ATT_PAYLOAD = 20  # standaard ATT-MTU 23 min 3 bytes header
_MAX_RX_BUFFER = 8 * 1024 * 1024

NOT_PAIRED_MESSAGE = "niet gepaird: draai `pitoolbox-flipper ble-pair`"

GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
# Hoe lang we wachten tot een achtergebleven BlueZ-verbinding weg is (DISCONNECT_TIMER is 2 s).
_STALE_DISCONNECT_WAIT_S = 8.0

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


def _parse_flow_credit(raw: bytes | bytearray) -> int:
    """Flow-control-waarde: uint32 big-endian (REVERSE_BYTES_U32 in serial_service.c)."""
    if len(raw) != 4:
        raise DeviceError(f"onverwachte flow-control-waarde van {len(raw)} bytes (verwacht 4)")
    return int.from_bytes(bytes(raw), "big")


async def _known_device(
    bus: BluezBus | None, adapter: str | None, address: str | None
) -> tuple[str, dict[str, Any]] | None:
    """(pad, properties) van de Flipper als BlueZ hem al gebonden kent op onze adapter.

    Controleert ook de adapter: een opgegeven adapter die niet bestaat of uit staat is een
    duidelijke fout, nog voordat bleak gaat scannen.
    """
    if bus is None:
        return None
    try:
        objects = await bus.get_managed_objects()
    except BluezDBusError as exc:
        log.debug("BLE: GetManagedObjects mislukt (%s)", exc)
        return None
    try:
        adapter_path = _ble_pairing.find_adapter(objects, adapter)
    except BlePairingError as exc:
        raise DeviceNotConnected(str(exc)) from exc
    if not address:
        return None
    path = _ble_pairing.find_device(objects, address.upper(), adapter_path)
    if path is None:
        return None
    props = objects[path][DEVICE_IFACE]
    if not (props.get("Paired") or props.get("Bonded")):
        return None  # ongepaird: via de scan, die geeft de bekende foutmeldingen
    return path, props


async def _drop_stale_link(bus: BluezBus, path: str, address: str | None) -> None:
    """BlueZ heeft de Flipper nog verbonden (gecrasht proces, ander proces): eerst verbreken.

    bleak zou `Connect` overslaan en we zouden midden in de oude RPC-sessie verder schrijven.
    `Disconnect` antwoordt pas als de link weg is (BlueZ src/device.c, DISCONNECT_TIMER 2 s).
    """
    log.warning(
        "BLE: %s was nog verbonden in BlueZ (vorige sessie of ander proces); eerst verbreken "
        "voor een verse RPC-sessie",
        address,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STALE_DISCONNECT_WAIT_S
    try:
        await asyncio.wait_for(bus.call(path, DEVICE_IFACE, "Disconnect"), _STALE_DISCONNECT_WAIT_S)
        while loop.time() < deadline:
            if not await bus.get_property(path, DEVICE_IFACE, "Connected"):
                return
            await asyncio.sleep(0.1)
    except (BluezDBusError, TimeoutError) as exc:
        log.warning("BLE: oude verbinding verbreken mislukte (%s); toch verder", exc)


async def _write_limit(bus: BluezBus | None, rx_char: Any) -> int:
    """Max. bytes per write-without-response: MTU - 3, begrensd op 243 (zie _flipper_ble).

    bleak's waarde komt uit een cache die bij een gebonden Flipper op MTU 23 blijft staan;
    de `MTU`-property via D-Bus is de echte, door BlueZ onderhandelde waarde.
    """
    limit = int(rx_char.max_write_without_response_size)
    obj = getattr(rx_char, "obj", None)
    path = obj[0] if isinstance(obj, tuple) and obj and isinstance(obj[0], str) else None
    if bus is not None and path is not None:
        try:
            mtu = int(await bus.get_property(path, GATT_CHAR_IFACE, "MTU"))
        except (BluezDBusError, TypeError, ValueError) as exc:
            log.debug("BLE: MTU-property niet leesbaar (%s); bleak-waarde %d", exc, limit)
        else:
            limit = max(limit, mtu - 3)
    return max(1, min(limit, MAX_WRITE_CHUNK))


def _serial_rx_char(services: BleakGATTServiceCollection) -> BleakGATTCharacteristic:
    """De RX-karakteristiek; een fout als de seriële Flipper-service (RX/TX/flow) ontbreekt."""
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
    return rx_char


async def _read_initial_credit(client: BleakClient) -> int:
    """Het flow-control-krediet direct na het verbinden (vóór het abonneren op notificaties)."""
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
    return credit
