"""Bouwstenen voor de binaire body van een bericht: lezen (u8/u16/u32, tekst met
lengtebyte) en schrijven van een tekstveld. Getallen little-endian (protocol §2)."""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import TypeVar

from pitoolbox.frontends.flipper_ui.protocol_spec import ProtocolError

# --- lezen ------------------------------------------------------------------------------------


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _take(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise ProtocolError("bericht te kort")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return int(struct.unpack("<H", self._take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack("<I", self._take(4))[0])

    def str8(self) -> str:
        return self._ascii(self._take(self.u8()))

    def rest(self) -> str:
        return self._ascii(self._take(len(self._data) - self._pos))

    @staticmethod
    def _ascii(raw: bytes) -> str:
        # Tekens buiten ASCII worden in de modelvalidatie geweigerd; latin-1 decodeert alles.
        return raw.decode("latin-1")

    def done(self) -> None:
        if self._pos != len(self._data):
            raise ProtocolError(f"{len(self._data) - self._pos} bytes over na de body")


def _str8(value: str) -> bytes:
    raw = value.encode("ascii")
    if len(raw) > 0xFF:  # door de modelgrenzen onmogelijk; puur defensief
        raise ProtocolError("tekstveld langer dan 255 bytes")
    return bytes([len(raw)]) + raw


_E = TypeVar("_E", bound=IntEnum)


def _enum(cls: type[_E], value: int, what: str) -> _E:
    try:
        return cls(value)
    except ValueError:
        raise ProtocolError(f"onbekende {what}: {value}") from None
