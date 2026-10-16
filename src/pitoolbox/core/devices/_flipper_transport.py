"""Bytestroom naar de Flipper: het `ByteTransport`-protocol en de USB-CDC-transport (pyserial).

Bronnen (VID:PID, DTR, CLI na het openen): zie flipper.py. De BLE-transport staat in
`_flipper_ble`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import serial
from serial.tools import list_ports

from pitoolbox.core.errors import DeviceError, DeviceNotConnected

# Loggernaam van flipper.py: alle logregels van FlipperDevice en zijn transport onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

FLIPPER_VID = 0x0483
FLIPPER_PID = 0x5740


class TransportError(DeviceError):
    """I/O-fout op de transportlaag (kabel eruit, poort weg). Reden voor één reconnect."""

    code = "transport_error"


class ByteTransport(Protocol):
    """Minimale bytestroom naar de Flipper; in tests vervangen door een simulator."""

    @property
    def kind(self) -> str:
        """'usb' of 'ble'."""
        ...

    @property
    def starts_in_rpc(self) -> bool:
        """True: de verbinding staat na `open()` al in een RPC-sessie (geen CLI), zoals BLE."""
        ...

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, data: bytes) -> None: ...

    def read(self, max_bytes: int, timeout: float) -> bytes:
        """Lees 1..max_bytes bytes; leeg resultaat betekent: niets binnen `timeout` s."""
        ...

    def reset_input(self) -> None: ...

    def describe(self) -> str: ...


class UsbSerialTransport:
    """pyserial-transport. Zonder expliciete poort wordt op VID:PID 0483:5740 gezocht."""

    kind = "usb"
    starts_in_rpc = False  # de poort start in de CLI (cli_vcp.c)

    def __init__(self, port: str | None = None, baudrate: int = 115200) -> None:
        self._port = port
        self._baudrate = baudrate  # CDC negeert de baudrate; pyserial wil er wel een
        self._ser: Any = None
        self._opened_port: str | None = None

    @staticmethod
    def find_port() -> str:
        found = sorted(
            (p for p in list_ports.comports() if p.vid == FLIPPER_VID and p.pid == FLIPPER_PID),
            key=lambda p: str(p.device),
        )
        if not found:
            raise DeviceNotConnected(
                "Geen Flipper gevonden op USB (VID:PID 0483:5740). Is hij aangesloten en "
                "niet in gebruik door qFlipper of een andere terminal?"
            )
        if len(found) > 1:
            log.warning("meerdere Flippers gevonden, eerste gekozen: %s", found[0].device)
        return str(found[0].device)

    @staticmethod
    def is_present(port: str | None = None) -> bool:
        """Zit er een Flipper op USB (VID:PID), of staat de opgegeven poort in de lijst?"""
        try:
            UsbSerialTransport.find_port()
            return True
        except (DeviceNotConnected, OSError):
            pass
        if port:
            try:
                return any(str(p.device) == port for p in list_ports.comports())
            except OSError:
                return False
        return False

    @property
    def is_open(self) -> bool:
        return self._ser is not None and bool(self._ser.is_open)

    def open(self) -> None:
        if self.is_open:
            return
        port = self._port or self.find_port()
        try:
            # DTR hoog bij openen = "verbonden" voor de firmware (cli_vcp.c).
            self._ser = serial.Serial(port, self._baudrate, timeout=0.1, write_timeout=2.0)
        except (serial.SerialException, OSError) as exc:
            self._ser = None
            raise DeviceNotConnected(
                f"kan seriële poort {port} niet openen: {exc} (rechten? zit je in 'dialout'?)"
            ) from exc
        self._opened_port = port

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except (serial.SerialException, OSError) as exc:
                log.debug("fout bij sluiten van de poort: %s", exc)

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportError("seriële poort is niet open")
        try:
            self._ser.write(data)
            self._ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"schrijffout op seriële poort: {exc}") from exc

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if not self.is_open:
            raise TransportError("seriële poort is niet open")
        try:
            self._ser.timeout = timeout
            data: bytes = self._ser.read(1)
            if data and max_bytes > 1:
                waiting = int(self._ser.in_waiting)
                if waiting:
                    data += self._ser.read(min(waiting, max_bytes - 1))
            return data
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"leesfout op seriële poort: {exc}") from exc

    def reset_input(self) -> None:
        if self.is_open:
            try:
                self._ser.reset_input_buffer()
            except (serial.SerialException, OSError) as exc:
                raise TransportError(f"kan invoerbuffer niet leegmaken: {exc}") from exc

    def describe(self) -> str:
        return f"usb-serieel {self._opened_port or self._port or '(auto)'}"
