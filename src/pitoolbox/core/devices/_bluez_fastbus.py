"""`BluezBus` op dbus-fast (system bus): de echte D-Bus-verbinding met BlueZ.

dbus-fast bestaat alleen op Linux en wordt daarom pas bij gebruik geimporteerd; zie
_ble_pairing.py.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections.abc import Sequence
from typing import Any

from pitoolbox.core.devices._bluez_agent import PairingAgent, _build_agent_interface
from pitoolbox.core.devices._bluez_dbus import (
    BLUEZ_SERVICE,
    PROPERTIES_IFACE,
    BlePairingError,
    BluezBus,
    BluezDBusError,
    BusFactory,
    ManagedObjects,
)

# --- echte bus (dbus-fast) --------------------------------------------------------------


class _DbusFastBus:
    """`BluezBus` bovenop dbus_fast.aio.MessageBus (system bus)."""

    def __init__(self, bus: Any, modules: dict[str, Any]) -> None:
        self._bus = bus
        self._m = modules

    @classmethod
    async def connect(cls) -> _DbusFastBus:
        if sys.platform != "linux":
            raise BlePairingError(
                "Bluetooth-koppeling werkt alleen op Linux met BlueZ (de Raspberry Pi); "
                f"dit is {sys.platform}."
            )
        try:
            modules = {
                "aio": importlib.import_module("dbus_fast.aio"),
                "constants": importlib.import_module("dbus_fast.constants"),
                "message": importlib.import_module("dbus_fast.message"),
                "signature": importlib.import_module("dbus_fast.signature"),
                "root": importlib.import_module("dbus_fast"),
            }
        except ImportError as exc:
            raise BlePairingError(
                "dbus-fast is niet geinstalleerd (komt mee met bleak op Linux): draai 'uv sync'."
            ) from exc
        try:
            bus = (
                await modules["aio"]
                .MessageBus(bus_type=modules["constants"].BusType.SYSTEM)
                .connect()
            )
        except Exception as exc:  # socket-, auth- en policy-fouten van dbus-fast
            raise BlePairingError(
                f"Geen verbinding met de system D-Bus ({exc}). Draait dbus en BlueZ?"
            ) from exc
        return cls(bus, modules)

    async def call(
        self,
        path: str,
        interface: str,
        member: str,
        signature: str = "",
        body: Sequence[Any] = (),
    ) -> list[Any]:
        message = self._m["message"].Message(
            destination=BLUEZ_SERVICE,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=list(body),
        )
        reply = await self._bus.call(message)
        if reply.message_type == self._m["constants"].MessageType.ERROR:
            text = str(reply.body[0]) if reply.body else ""
            raise BluezDBusError(reply.error_name or "org.bluez.Error.Failed", text)
        return list(reply.body)

    async def get_managed_objects(self) -> ManagedObjects:
        body = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        unpacked: ManagedObjects = self._m["root"].unpack_variants(body[0])
        return unpacked

    async def set_property(
        self, path: str, interface: str, name: str, signature: str, value: Any
    ) -> None:
        variant = self._m["signature"].Variant(signature, value)
        await self.call(path, PROPERTIES_IFACE, "Set", "ssv", [interface, name, variant])

    async def get_property(self, path: str, interface: str, name: str) -> Any:
        body = await self.call(path, PROPERTIES_IFACE, "Get", "ss", [interface, name])
        return self._m["root"].unpack_variants(body[0])

    def export_agent(self, path: str, agent: PairingAgent) -> None:
        self._bus.export(path, _build_agent_interface(agent))

    def unexport_agent(self, path: str) -> None:
        self._bus.unexport(path)

    async def close(self) -> None:
        self._bus.disconnect()
        with contextlib.suppress(Exception):
            await self._bus.wait_for_disconnect()


async def connect_bus(bus_factory: BusFactory | None = None) -> BluezBus:
    """De system bus via `bus_factory`, anders dbus-fast; gooit `BlePairingError`."""
    if bus_factory is not None:
        return await bus_factory()
    return await _DbusFastBus.connect()
