"""Een Flipper over BLE herkennen en vinden: UUID's van de seriële service en scannen.

Bronnen (serial_service_uuid.inc, serial_profile.c, gap.c, bleak): zie _flipper_ble.py.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

from bleak import BleakScanner
from bleak.args.bluez import BlueZScannerArgs
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakBluetoothNotAvailableError, BleakError

from pitoolbox.core.devices._ble_pairing import normalize_adapter
from pitoolbox.core.errors import DeviceError, DeviceNotConnected

# Loggernaam van _flipper_ble.py: alle logregels van de BLE-transport onder één naam.
log = logging.getLogger("pitoolbox.core.devices._flipper_ble")

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
# Alles waaraan een Flipper te herkennen is, ook als hij hernoemd is: BlueZ zet deze UUID's na
# het verbinden ook in de `UUIDs`-property van Device1 (gezien op de Pi: 0x3082 en de serial-UUID).
FLIPPER_UUIDS = _ADV_SERVICE_UUIDS | {SERVICE_UUID}


@dataclass(frozen=True)
class BleCandidate:
    """Een gevonden Flipper in de buurt."""

    address: str
    name: str
    rssi: int | None


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


def _scanner_args(adapter: str | None) -> BlueZScannerArgs:
    return {"adapter": adapter} if adapter else {}


def _is_missing_adapter(exc: BaseException) -> bool:
    """bleak: `BleakError("adapter 'hciN' not found")` (manager.py `_check_adapter`)."""
    text = str(exc).lower()
    return isinstance(exc, BleakError) and "adapter" in text and "not found" in text


def _missing_adapter_message(adapter: str | None) -> str:
    return (
        f"Bluetooth-adapter {adapter or '(standaard)'} bestaat niet in BlueZ. Controleer "
        "PITOOLBOX_FLIPPER_BLE_ADAPTER of --adapter en 'bluetoothctl list'."
    )


async def _scan(name_prefix: str, timeout: float, adapter: str | None) -> list[BleCandidate]:
    found = await BleakScanner.discover(
        timeout=timeout, return_adv=True, bluez=_scanner_args(adapter)
    )
    candidates: list[BleCandidate] = []
    for device, adv in found.values():
        if _is_flipper(device, adv, name_prefix):
            candidates.append(BleCandidate(device.address, _adv_name(device, adv), adv.rssi))
    candidates.sort(key=lambda c: c.rssi if c.rssi is not None else -999, reverse=True)
    return candidates


def scan_flippers(
    name_prefix: str = "Flipper", timeout: float = 8.0, adapter: str | None = None
) -> list[BleCandidate]:
    """Zoek Flippers in de buurt (sterkste signaal eerst), optioneel via adapter `hciN`.

    Een Flipper adverteert alleen als hij BLE aan heeft en niet al met een telefoon/ander
    apparaat verbonden is.
    """
    try:
        adapter = normalize_adapter(adapter)
    except ValueError as exc:
        raise DeviceError(str(exc)) from exc
    try:
        return _run_blocking(_scan(name_prefix, timeout, adapter), timeout + 20.0)
    except BleakBluetoothNotAvailableError as exc:
        raise DeviceError(f"Bluetooth is niet beschikbaar: {exc}") from exc
    except (BleakError, OSError, TimeoutError) as exc:
        if _is_missing_adapter(exc):
            raise DeviceError(_missing_adapter_message(adapter)) from exc
        raise DeviceError(f"BLE-scan mislukt: {exc}") from exc


async def _find_flipper(
    address: str | None, prefix: str, adapter: str | None, timeout: float
) -> BLEDevice:
    """Zoek de Flipper met een scan: op `address`, anders de eerste met naamprefix `prefix`."""
    if address:
        device = await BleakScanner.find_device_by_address(
            address, timeout=timeout, bluez=_scanner_args(adapter)
        )
        if device is None:
            raise DeviceNotConnected(
                f"Flipper {address} niet gevonden via BLE. Staat Bluetooth aan op de "
                "Flipper en is hij niet al verbonden met een telefoon?"
            )
        return device
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: _is_flipper(d, ad, prefix),
        timeout=timeout,
        bluez=_scanner_args(adapter),
    )
    if device is None:
        raise DeviceNotConnected(
            f"Geen Flipper gevonden via BLE (naam begint met {prefix!r}). Staat Bluetooth "
            "aan op de Flipper? Stel PITOOLBOX_FLIPPER_BLE_ADDRESS in voor een vast adres."
        )
    log.info("BLE: Flipper %s gekozen op naamprefix", device.address)
    return device
