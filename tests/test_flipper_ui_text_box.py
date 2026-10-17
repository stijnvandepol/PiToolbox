"""Tekst voor de TextBox van de Flipper: de breedtetabel van het font.

De verwachte breedtes zijn nagemeten met de eigen u8g2-code van de firmware (Momentum
mntm-012, `u8g2_GetGlyphWidth` uit lib/u8g2/u8g2_font.c op de host gecompileerd met de
fontbytes van `u8g2_font_haxrcorp4089_tr`): voor alle 95 tekens dezelfde waarde als de tabel.
"""

from __future__ import annotations

from pitoolbox.frontends.flipper_ui._font_widths import ADVANCE, TEXT_BOX_WIDTH


def test_width_table_covers_printable_ascii_only() -> None:
    assert set(ADVANCE) == {chr(c) for c in range(0x20, 0x7F)}
    assert TEXT_BOX_WIDTH == 120


def test_width_table_known_glyphs() -> None:
    known = {" ": 2, "i": 2, "l": 2, "m": 8, "W": 8, "M": 8, "a": 5, "e": 5, "0": 6, "@": 9}
    assert {c: ADVANCE[c] for c in known} == known
    assert {ADVANCE[c] for c in "/-_"} == {8, 6}
    assert max(ADVANCE.values()) == 9
