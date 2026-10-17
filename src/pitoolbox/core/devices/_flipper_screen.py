"""Schermbeeld van de Flipper (`gui_screen_frame`) ontleden en tonen: puur, zonder I/O.

Alleen voor de ontwikkelaarsmodus "op afstand" (`_flipper_remote`, `pitoolbox-flipper remote`).

Indeling van het framebuffer (geverifieerd, niet aangenomen):

* Grootte: `canvas_get_buffer_size` = tegelbreedte x tegelhoogte x 8 van de u8g2-buffer
  (Momentum mntm-012 `applications/services/gui/canvas.c`); voor het 128x64-scherm 16 x 8 x 8 =
  1024 bytes. `rpc_gui.c` kopieert die buffer ongewijzigd in `ScreenFrame.data`.
* Pixel (x, y) staat in byte `x + (y // 8) * 128`, bit `y % 8` (LSB = bovenste rij van de
  pagina); een gezette bit is een donkere pixel. Bron: qFlipper
  `application/screencanvas.cpp` `ScreenCanvas::setFrame` (`i = y / 8 * width + x`,
  `z = y % 8`, `data[i] & (1 << z)` = voorgrond), de officiële desktopclient.
* `orientation` (gui.proto `ScreenOrientation`) zegt hoe de app het beeld bedoelt (bijv.
  linkshandig gedraaid). Hier wordt het buffer getoond zoals het in het geheugen staat, dus zoals
  het LCD het in de standaardstand laat zien; de aanroeper meldt een afwijkende stand.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

SCREEN_WIDTH = 128
SCREEN_HEIGHT = 64
FRAME_BYTES = SCREEN_WIDTH * SCREEN_HEIGHT // 8
PNG_SCALE_MAX = 16

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_GRAYSCALE = 0  # kleurtype 0, bitdiepte 8 (PNG-specificatie, IHDR)
_PNG_BIT_DEPTH = 8
_DARK = 0x00
_LIGHT = 0xFF

# Halve blokken: één tekenregel toont twee pixelrijen (boven, onder).
_HALF_BLOCKS = {
    (False, False): " ",
    (True, False): "▀",
    (False, True): "▄",
    (True, True): "█",
}

Grid = list[list[bool]]


@dataclass(frozen=True)
class ScreenFrame:
    """Eén schermbeeld: het ruwe framebuffer en de stand (naam uit gui.proto)."""

    data: bytes
    orientation: str

    def pixels(self) -> Grid:
        return decode_frame(self.data)


def decode_frame(data: bytes) -> Grid:
    """Framebuffer -> 64 rijen van 128 pixels (True = donker). Zie de moduledocstring.

    Raises:
        ValueError: `data` is geen 1024 bytes (ander scherm of een kapot frame).
    """
    if len(data) != FRAME_BYTES:
        raise ValueError(f"schermbeeld van {len(data)} bytes; verwacht {FRAME_BYTES}")
    return [
        [bool(data[x + (y // 8) * SCREEN_WIDTH] & (1 << (y % 8))) for x in range(SCREEN_WIDTH)]
        for y in range(SCREEN_HEIGHT)
    ]


def render_text(grid: Grid) -> str:
    """Tekstweergave met halve blokken: 32 regels van 128 tekens, met een rand eromheen."""
    width = len(grid[0]) if grid else 0
    lines = ["+" + "-" * width + "+"]
    for top in range(0, len(grid), 2):
        upper = grid[top]
        lower = grid[top + 1] if top + 1 < len(grid) else [False] * width
        cells = (_HALF_BLOCKS[pair] for pair in zip(upper, lower, strict=True))
        lines.append("|" + "".join(cells) + "|")
    lines.append("+" + "-" * width + "+")
    return "\n".join(lines)


def render_png(grid: Grid, scale: int = 1) -> bytes:
    """PNG (8-bit grijs, donker = zwart, licht = wit), elke pixel `scale` x `scale` groot.

    Alleen de standaardbibliotheek: IHDR, één IDAT (zlib) en IEND, elk met CRC-32, rijen met
    filter 0 (PNG-specificatie, W3C, hoofdstuk 5 en 11).
    """
    if not 1 <= scale <= PNG_SCALE_MAX:
        raise ValueError(f"schaal moet tussen 1 en {PNG_SCALE_MAX} liggen")
    height = len(grid) * scale
    width = (len(grid[0]) if grid else 0) * scale
    raw = bytearray()
    for row in grid:
        line = b"".join(bytes([_DARK if on else _LIGHT]) * scale for on in row)
        for _ in range(scale):
            raw.append(0)  # filtertype 0 (None)
            raw.extend(line)
    header = struct.pack(">IIBBBBB", width, height, _PNG_BIT_DEPTH, _PNG_GRAYSCALE, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
