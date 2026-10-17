"""Genereer de tabel met glyph-breedtes van het TextBox-font van de Flipper.

Waarom: de TextBox van de firmware breekt regels op pixelbreedte, midden in een woord
(`text_box_seek_next_line` in applications/services/gui/modules/text_box.c). De Pi breekt
daarom zelf op woordgrenzen en moet daarvoor de breedte van elk teken kennen.

Bron: Momentum firmware, tag mntm-012 (commit in FIRMWARE_COMMIT),
https://github.com/Next-Flip/Momentum-Firmware
- applications/services/gui/modules/text_box.c: TextBoxFontText tekent met FontSecondary,
  regelbreedte TEXT_BOX_TEXT_WIDTH = 120, breedte per teken via `canvas_glyph_width`.
- applications/services/gui/canvas.c: FontSecondary = `u8g2_font_haxrcorp4089_tr`,
  `canvas_glyph_width` = `u8g2_GetGlyphWidth`.
- lib/u8g2/u8g2_font.c: `u8g2_GetGlyphWidth` geeft de delta-x (advance) van de glyph; de
  layout van kop en glyph-records staat daar en in
  https://github.com/olikraus/u8g2/wiki/u8g2fontformat.
- lib/u8g2/u8g2_fonts.c: de bytes van het font (C-stringliteral).

Gebruik:
    git clone --filter=blob:none --sparse https://github.com/Next-Flip/Momentum-Firmware /tmp/mntm
    git -C /tmp/mntm sparse-checkout set lib/u8g2
    git -C /tmp/mntm checkout <FIRMWARE_COMMIT>
    uv run python scripts/gen_flipper_font_widths.py /tmp/mntm/lib/u8g2/u8g2_fonts.c

Het script controleert de SHA-256 van de fontbytes (FONT_SHA256), zodat een ander font of
een andere firmwareversie niet stilletjes een andere tabel geeft.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

FIRMWARE_TAG = "mntm-012"
FIRMWARE_COMMIT = "e1784e7418d8b074e971983ceb6fef0f37e52ae4"
FONT_NAME = "u8g2_font_haxrcorp4089_tr"
FONT_SHA256 = "9d5ddc185cc43d0a57d76a2465aaf36a17098a398b2c5db5bed874fb417e9d5a"
OUT = Path(__file__).resolve().parent.parent / "src/pitoolbox/frontends/flipper_ui/_font_widths.py"

HEADER_SIZE = 23  # U8G2_FONT_DATA_STRUCT_SIZE in u8g2_font.c
FIRST_PRINTABLE = 0x20
LAST_PRINTABLE = 0x7E

_LITERAL = r'"(?:[^"\\]|\\.)*"'
# Declaratie tot aan de ';' na de laatste stringliteral (een ';' kan ook ín de data staan).
_DECL_RE = re.compile(rf"const uint8_t {FONT_NAME}\[(\d+)\][^=]*=((?:\s*{_LITERAL})+)\s*;")
_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
_ESCAPES = dict(zip("ntrabfv\\\"'?", b"\n\t\r\a\b\f\v\\\"'?", strict=True))


def _c_string_bytes(body: str) -> bytes:
    """Bytes van aaneengesloten C-stringliterals (octale, hex- en enkele escapes)."""
    out = bytearray()
    for literal in _LITERAL_RE.findall(body):
        i = 0
        while i < len(literal):
            c = literal[i]
            if c != "\\":
                out.append(ord(c))
                i += 1
                continue
            nxt = literal[i + 1]
            if nxt in "01234567":
                digits = re.match(r"[0-7]{1,3}", literal[i + 1 :])
                octal = digits.group() if digits else nxt
                out.append(int(octal, 8))
                i += 1 + len(octal)
            elif nxt == "x":
                hexdigits = re.match(r"[0-9a-fA-F]+", literal[i + 2 :])
                if hexdigits is None:
                    raise ValueError("\\x zonder hexcijfers")
                out.append(int(hexdigits.group(), 16) & 0xFF)
                i += 2 + len(hexdigits.group())
            else:
                out.append(_ESCAPES[nxt])
                i += 2
    return bytes(out)


def extract_font(source: str) -> bytes:
    """De fontbytes uit u8g2_fonts.c, zonder de afsluitende NUL van de C-string."""
    match = _DECL_RE.search(source)
    if match is None:
        raise ValueError(f"{FONT_NAME} niet gevonden")
    declared = int(match.group(1))
    data = _c_string_bytes(match.group(2))
    if len(data) + 1 != declared:  # de array bevat ook de NUL van de stringliteral
        raise ValueError(f"{len(data)} bytes gelezen, array is {declared} (incl. NUL)")
    return data


class _Bits:
    """`u8g2_font_decode_get_unsigned_bits`/`_signed_bits`: LSB eerst, per byte."""

    def __init__(self, data: bytes, pos: int) -> None:
        self._data = data
        self._pos = pos
        self._bit = 0

    def unsigned(self, count: int) -> int:
        value = self._data[self._pos] >> self._bit
        end = self._bit + count
        if end >= 8:
            self._pos += 1
            value |= self._data[self._pos] << (8 - self._bit)
            end -= 8
        self._bit = end
        return value & ((1 << count) - 1)

    def signed(self, count: int) -> int:
        return self.unsigned(count) - (1 << (count - 1))


def glyph_advances(font: bytes) -> dict[int, int]:
    """Delta-x per teken uit het 8-bit-deel van het font, zoals `u8g2_GetGlyphWidth`."""
    bits_w, bits_h, bits_x, bits_y, bits_dx = font[4:9]
    advances: dict[int, int] = {}
    pos = HEADER_SIZE
    # Het 8-bit-deel: records [encoding u8, lengte u8, bitstroom]; lengte 0 sluit af.
    while font[pos + 1] != 0:
        encoding, size = font[pos], font[pos + 1]
        bits = _Bits(font, pos + 2)
        bits.unsigned(bits_w)  # glyph_width (bitmap)
        bits.unsigned(bits_h)  # glyph_height
        bits.signed(bits_x)  # x-offset
        bits.signed(bits_y)  # y-offset
        advances[encoding] = bits.signed(bits_dx)
        pos += size
    if font[0] != len(advances):
        raise ValueError(f"kop zegt {font[0]} glyphs, gevonden {len(advances)}")
    return advances


def _key(code: int) -> str:
    char = chr(code)
    if char == '"':
        return "'\"'"
    if char == "\\":
        return '"\\\\"'
    return f'"{char}"'


def render_module(advances: dict[int, int], digest: str) -> str:
    printable = {c: w for c, w in advances.items() if FIRST_PRINTABLE <= c <= LAST_PRINTABLE}
    missing = set(range(FIRST_PRINTABLE, LAST_PRINTABLE + 1)) - set(printable)
    if missing:
        raise ValueError(f"font mist printbare ASCII: {sorted(missing)}")
    others = sorted(set(advances) - set(printable))
    rows = "\n".join(f"    {_key(c)}: {w}," for c, w in sorted(printable.items()))
    return f'''"""Advance (delta-x, pixels) per teken van het TextBox-font van de Flipper.

Gegenereerd door scripts/gen_flipper_font_widths.py; niet handmatig wijzigen.
Font: {FONT_NAME} (FontSecondary in applications/services/gui/canvas.c), uit
lib/u8g2/u8g2_fonts.c van Momentum {FIRMWARE_TAG}, commit {FIRMWARE_COMMIT}
(https://github.com/Next-Flip/Momentum-Firmware).
SHA-256 van de fontbytes: {digest}.
De breedte is wat `canvas_glyph_width` (= `u8g2_GetGlyphWidth`) teruggeeft; de TextBox telt
die per teken op (`text_box_seek_next_line`, text_box.c).
Glyphs buiten printbare ASCII in het font: {others or "geen"}.
"""

from __future__ import annotations

from typing import Final

TEXT_BOX_WIDTH: Final = 120  # TEXT_BOX_TEXT_WIDTH in text_box.c

ADVANCE: Final[dict[str, int]] = {{
{rows}
}}
'''


def main(path: str) -> int:
    font = extract_font(Path(path).read_text(encoding="utf-8"))
    digest = hashlib.sha256(font).hexdigest()
    if digest != FONT_SHA256:
        print(f"fontbytes wijken af: sha256 {digest}, verwacht {FONT_SHA256}", file=sys.stderr)
        return 1
    OUT.write_text(render_module(glyph_advances(font), digest), encoding="utf-8")
    print(f"ok: {OUT}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
