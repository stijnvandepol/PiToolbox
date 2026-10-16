"""Framing van de Flipper-RPC: nanopb 'delimited' = varint(lengte) + PB.Main-bytes.

Bron: flipperzero-firmware applications/services/rpc/rpc.c (PB_ENCODE_DELIMITED /
PB_DECODE_DELIMITED). De lengteprefix is een standaard protobuf-varint (LEB128, 7 bits per
byte, laagste groep eerst). Afgeleid van busse/flipperzero-mcp (MIT), `_encode_varint` en
`_read_varint`, maar incrementeel en met harde foutmeldingen in plaats van `None`.
"""

from __future__ import annotations

from pitoolbox.core.errors import DeviceError

# Een lengteprefix past in 32 bit (nanopb size_t op de Flipper) = maximaal 5 varint-bytes.
_MAX_VARINT_BYTES = 5

# Echte Flipper-berichten zijn klein (storage-chunks ~512 B, device-info per sleutel/waarde).
DEFAULT_MAX_FRAME_SIZE = 65536


class FrameError(DeviceError):
    """Onleesbare of onredelijke frame: stream is vermoedelijk uit de pas (CLI-tekst als RPC)."""

    code = "frame_error"


def encode_varint(value: int) -> bytes:
    """Codeer een niet-negatieve int als protobuf-varint."""
    if value < 0:
        raise ValueError("varint kan niet negatief zijn")
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def encode_frame(payload: bytes) -> bytes:
    """Verpak een geserialiseerd PB.Main als varint(lengte) + payload."""
    return encode_varint(len(payload)) + payload


class FrameDecoder:
    """Incrementele decoder: bytes erin, complete payloads eruit.

    Een fout (te lange varint, lengte 0 of boven `max_frame_size`) leegt de buffer en gooit
    `FrameError`; het is aan de aanroeper om de sessie dan als verloren te beschouwen, want
    na een desync is niet meer te bepalen waar het volgende frame begint.
    """

    def __init__(self, max_frame_size: int = DEFAULT_MAX_FRAME_SIZE) -> None:
        if max_frame_size < 1:
            raise ValueError("max_frame_size moet >= 1 zijn")
        self._max = max_frame_size
        self._buf = bytearray()

    @property
    def buffered(self) -> int:
        """Aantal bytes dat nog op een volledig frame wacht."""
        return len(self._buf)

    def reset(self) -> None:
        self._buf.clear()

    def _fail(self, message: str) -> FrameError:
        self._buf.clear()
        return FrameError(message)

    def feed(self, data: bytes) -> list[bytes]:
        """Voeg bytes toe en geef alle nu complete payloads terug (mogelijk 0)."""
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            parsed = self._parse_length()
            if parsed is None:
                break  # varint nog niet compleet
            length, header = parsed
            if length == 0:
                # De firmware zet altijd een oneof-inhoud, dus lengte 0 komt niet voor.
                raise self._fail("frame met lengte 0 (stream waarschijnlijk uit de pas)")
            if length > self._max:
                raise self._fail(f"frame van {length} bytes overschrijdt limiet {self._max}")
            end = header + length
            if len(self._buf) < end:
                break  # payload nog niet compleet
            frames.append(bytes(self._buf[header:end]))
            del self._buf[:end]
        return frames

    def _parse_length(self) -> tuple[int, int] | None:
        """Lees de varint vooraan de buffer: (lengte, aantal headerbytes) of None."""
        value = 0
        for i, byte in enumerate(self._buf[:_MAX_VARINT_BYTES]):
            value |= (byte & 0x7F) << (7 * i)
            if not byte & 0x80:
                return value, i + 1
        if len(self._buf) >= _MAX_VARINT_BYTES:
            raise self._fail("varint langer dan 5 bytes (stream waarschijnlijk uit de pas)")
        return None
