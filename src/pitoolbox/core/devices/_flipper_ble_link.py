"""Verbinden met de Flipper over BLE: toestand van de transport, linkopbouw en bleak-callbacks.

Basis van `BleTransport` in _flipper_ble.py (daar ook de bronnen en meetwaarden).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from bleak import BleakClient
from bleak.args.bluez import BlueZClientArgs
from bleak.backends.device import BLEDevice
from bleak.exc import BleakBluetoothNotAvailableError, BleakDeviceNotFoundError, BleakError

from pitoolbox.core.devices._ble_pairing import BluezBus, normalize_adapter
from pitoolbox.core.devices._flipper_ble_gatt import (
    _MAX_RX_BUFFER,
    _MIN_ATT_PAYLOAD,
    NOT_PAIRED_MESSAGE,
    BleNotPaired,
    _drop_stale_link,
    _known_device,
    _parse_flow_credit,
    _read_initial_credit,
    _serial_rx_char,
    _write_limit,
    is_auth_error,
)
from pitoolbox.core.devices._flipper_ble_scan import (
    FLOW_CONTROL_UUID,
    TX_CHAR_UUID,
    _find_flipper,
    _is_missing_adapter,
    _missing_adapter_message,
)
from pitoolbox.core.errors import DeviceError, DeviceNotConnected

# Loggernaam van _flipper_ble.py: alle logregels van de BLE-transport onder één naam.
log = logging.getLogger("pitoolbox.core.devices._flipper_ble")

T = TypeVar("T")

# Verbinden met een gebonden Flipper zonder scan: de Flipper adverteert in de spaarstand nog
# elke 1..2,5 s (gap.c `gap_advertise_start`), dus dit is ruim; een Flipper die uit staat of
# buiten bereik is, faalt zo niet pas na de volle `connect_timeout_s`.
_KNOWN_DEVICE_CONNECT_EXTRA_S = 4.0


def _client_args(adapter: str | None) -> BlueZClientArgs:
    return {"adapter": adapter} if adapter else {}


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


class BleLink:
    """Toestand en verbindingsopbouw van `BleTransport` (zie daar)."""

    def __init__(  # noqa: PLR0913 keyword-only afhankelijkheden (werkt als een struct)
        self,
        address: str | None = None,
        name_prefix: str = "Flipper",
        *,
        adapter: str | None = None,
        scan_timeout_s: float = 8.0,
        connect_timeout_s: float = 20.0,
        credit_timeout_s: float = 10.0,
        write_timeout_s: float = 5.0,
        write_with_response: bool = False,
    ) -> None:
        self._address = address
        self._name_prefix = name_prefix
        self._adapter = normalize_adapter(adapter)
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

    def describe(self) -> str:
        address = self._address or "(auto)"
        name = self._name or f"{self._name_prefix}*"
        via = f" via {self._adapter}" if self._adapter else ""
        return f"ble:{address} ({name}){via}"

    # --- verbinden --------------------------------------------------------------------

    async def _async_connect(self, bus: BluezBus | None) -> None:
        device, timeout = await self._choose_device(bus)
        self._address = device.address
        self._name = device.name or None
        client = BleakClient(
            device,
            disconnected_callback=self._on_disconnected,
            timeout=timeout,
            bluez=_client_args(self._adapter),
        )
        self._client = client
        await client.connect()

        rx_char = _serial_rx_char(client.services)
        credit = await _read_initial_credit(client)
        with self._cond:
            self._credit = credit

        await client.start_notify(TX_CHAR_UUID, self._on_tx)
        await client.start_notify(FLOW_CONTROL_UUID, self._on_flow)

        max_chunk = await _write_limit(bus, rx_char)
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

    async def _choose_device(self, bus: BluezBus | None) -> tuple[BLEDevice, float]:
        """Het apparaat en de connect-time-out: zonder scan als BlueZ de gebonden Flipper al
        kent op onze adapter, anders via een scan."""
        known = await _known_device(bus, self._adapter, self._address)
        timeout = self._connect_timeout_s
        if known is not None and self._address:
            path, props = known
            if props.get("Connected") and bus is not None:
                await _drop_stale_link(bus, path, self._address)
            # Zelfde vorm als de BlueZ-scanner van bleak: dan slaat connect() de scan over.
            name = props.get("Alias") or props.get("Name")
            device = BLEDevice(
                self._address.upper(), str(name) if name else None, {"path": path, "props": props}
            )
            timeout = min(timeout, self._scan_timeout_s + _KNOWN_DEVICE_CONNECT_EXTRA_S)
            log.debug("BLE: %s is bekend bij BlueZ (%s); verbinden zonder scan", device, path)
            return device, timeout
        device = await _find_flipper(
            self._address, self._name_prefix, self._adapter, self._scan_timeout_s
        )
        return device, timeout

    def _translate_open_error(self, exc: Exception) -> DeviceError:
        if isinstance(exc, DeviceError):
            return exc
        if isinstance(exc, BleakDeviceNotFoundError):
            return DeviceNotConnected(
                f"Flipper niet gevonden door BlueZ ({exc}); staat hij aan en in de buurt?"
            )
        if is_auth_error(exc):
            return BleNotPaired(NOT_PAIRED_MESSAGE)
        if _is_missing_adapter(exc):
            return DeviceNotConnected(_missing_adapter_message(self._adapter))
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
        with self._cond:
            was_open = self._open
            self._link_lost = True
        # Bij een eigen close() is _open al False: geen waarschuwing voor een gewone afsluiting.
        log.log(
            logging.WARNING if was_open else logging.DEBUG,
            "BLE: verbinding met %s verbroken",
            self.describe(),
        )
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
