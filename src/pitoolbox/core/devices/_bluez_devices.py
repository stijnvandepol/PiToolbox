"""Apparaten die BlueZ kent: opvragen, ontkoppelen (bonding weg) en de link verbreken.

Bronnen (Device.rst, Adapter.rst): zie _ble_pairing.py.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from pitoolbox.core.devices._bluez_dbus import (
    ADAPTER_IFACE,
    DEVICE_IFACE,
    BusFactory,
    _adapter_arg,
    _call,
    _devices,
    _mac,
    find_adapter,
    find_device,
)
from pitoolbox.core.devices._bluez_fastbus import connect_bus


@dataclass(frozen=True)
class PairedDevice:
    address: str
    name: str | None
    paired: bool
    bonded: bool
    trusted: bool
    connected: bool


def _as_paired_device(props: dict[str, Any]) -> PairedDevice:
    name = props.get("Alias") or props.get("Name")
    return PairedDevice(
        address=str(props.get("Address", "")).upper(),
        name=str(name) if name else None,
        paired=bool(props.get("Paired", False)),
        bonded=bool(props.get("Bonded", False)),
        trusted=bool(props.get("Trusted", False)),
        connected=bool(props.get("Connected", False)),
    )


async def unpair_async(
    address: str, *, bus_factory: BusFactory | None = None, adapter: str | None = None
) -> bool:
    """Verwijder het apparaat en zijn bonding uit BlueZ (Adapter1.RemoveDevice).

    Geeft False terug als het apparaat niet bekend was (er viel niets te verwijderen). Met
    `adapter` alleen het apparaat onder die adapter.
    """
    mac = _mac(address)
    adapter = _adapter_arg(adapter)
    bus = await connect_bus(bus_factory)
    try:
        objects = await bus.get_managed_objects()
        only = find_adapter(objects, adapter) if adapter is not None else None
        path = find_device(objects, mac, only)
        if path is None:
            return False
        owner = str(objects[path][DEVICE_IFACE].get("Adapter") or find_adapter(objects))
        await _call(bus, "Ontkoppelen", owner, ADAPTER_IFACE, "RemoveDevice", "o", [path])
        return True
    finally:
        with contextlib.suppress(Exception):
            await bus.close()


async def disconnect_async(
    address: str, *, bus_factory: BusFactory | None = None, adapter: str | None = None
) -> bool:
    """Verbreek de BLE-link met een apparaat (Device1.Disconnect; Device.rst: "Disconnects all
    connected profiles and then terminates low-level ACL connection"). De bonding blijft staan.

    Geeft False als het apparaat niet bekend of niet verbonden was. Werkt zonder root met de
    D-Bus-policy van BlueZ (groep `bluetooth`), net als koppelen. Bedoeld voor de valtest
    (`pitoolbox-flipper ui-droptest`); het proces dat de link had, ziet een gewone verbreking.
    """
    mac = _mac(address)
    adapter = _adapter_arg(adapter)
    bus = await connect_bus(bus_factory)
    try:
        objects = await bus.get_managed_objects()
        only = find_adapter(objects, adapter) if adapter is not None else None
        path = find_device(objects, mac, only)
        if path is None or not objects[path][DEVICE_IFACE].get("Connected", False):
            return False
        await _call(bus, "Verbinding verbreken", path, DEVICE_IFACE, "Disconnect")
        return True
    finally:
        with contextlib.suppress(Exception):
            await bus.close()


async def list_devices_async(
    prefix: str = "",
    *,
    only_paired: bool = True,
    bus_factory: BusFactory | None = None,
    adapter: str | None = None,
    service_uuids: Collection[str] = (),
) -> list[PairedDevice]:
    """Door BlueZ gekende apparaten waarvan de naam met `prefix` begint (hoofdletterongevoelig),
    of die een van `service_uuids` in hun `UUIDs`-property hebben (Device.rst: "List of 128-bit
    UUIDs that represents the available remote services"); zo vind je ook een hernoemde Flipper.

    Met `adapter` alleen de apparaten onder die adapter (die moet dan bestaan).
    """
    adapter = _adapter_arg(adapter)
    bus = await connect_bus(bus_factory)
    try:
        objects = await bus.get_managed_objects()
    finally:
        with contextlib.suppress(Exception):
            await bus.close()
    only = find_adapter(objects, adapter) if adapter is not None else None
    wanted = prefix.casefold()
    uuids = {u.lower() for u in service_uuids}
    result = []
    for _path, props in _devices(objects):
        if only is not None and props.get("Adapter", only) != only:
            continue
        device = _as_paired_device(props)
        if only_paired and not (device.paired or device.bonded):
            continue
        by_uuid = bool(uuids & {str(u).lower() for u in props.get("UUIDs", ())})
        if wanted and not by_uuid and not (device.name or "").casefold().startswith(wanted):
            continue
        result.append(device)
    return result


async def paired_devices_async(
    prefix: str = "", *, bus_factory: BusFactory | None = None, adapter: str | None = None
) -> list[PairedDevice]:
    return await list_devices_async(
        prefix, only_paired=True, bus_factory=bus_factory, adapter=adapter
    )


def unpair(
    address: str, *, bus_factory: BusFactory | None = None, adapter: str | None = None
) -> bool:
    return asyncio.run(unpair_async(address, bus_factory=bus_factory, adapter=adapter))


def disconnect(
    address: str, *, bus_factory: BusFactory | None = None, adapter: str | None = None
) -> bool:
    return asyncio.run(disconnect_async(address, bus_factory=bus_factory, adapter=adapter))


def list_devices(
    prefix: str = "",
    *,
    only_paired: bool = True,
    bus_factory: BusFactory | None = None,
    adapter: str | None = None,
    service_uuids: Collection[str] = (),
) -> list[PairedDevice]:
    return asyncio.run(
        list_devices_async(
            prefix,
            only_paired=only_paired,
            bus_factory=bus_factory,
            adapter=adapter,
            service_uuids=service_uuids,
        )
    )


def paired_devices(
    prefix: str = "", *, bus_factory: BusFactory | None = None, adapter: str | None = None
) -> list[PairedDevice]:
    return asyncio.run(paired_devices_async(prefix, bus_factory=bus_factory, adapter=adapter))
