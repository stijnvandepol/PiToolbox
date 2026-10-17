"""Tekst voor de TextBox van de Flipper: breedtetabel van het font en afbreken op woordgrenzen.

De verwachte breedtes zijn nagemeten met de eigen u8g2-code van de firmware (Momentum
mntm-012, `u8g2_GetGlyphWidth` uit lib/u8g2/u8g2_font.c op de host gecompileerd met de
fontbytes van `u8g2_font_haxrcorp4089_tr`): voor alle 95 tekens dezelfde waarde als de tabel.
`_firmware_lines` doet na wat de TextBox van de firmware met een tekst doet.
"""

from __future__ import annotations

import random

import pytest

from pitoolbox.frontends.flipper_ui import protocol as p
from pitoolbox.frontends.flipper_ui._font_widths import ADVANCE, TEXT_BOX_WIDTH

WIDTH = 120


def _rng(seed: int) -> random.Random:
    # Vaste seed: reproduceerbare fuzz, geen cryptografie.
    return random.Random(seed)  # noqa: S311


def _firmware_lines(text: str) -> list[str]:
    """Regels zoals `text_box_seek_next_line` (Momentum mntm-012, text_box.c) ze maakt: per
    teken de breedte optellen, nieuwe regel bij '\\n' of als het volgende teken niet past."""
    lines: list[str] = []
    i = 0
    while True:
        start, width = i, 0
        while i < len(text) and text[i] != "\n":
            if width + ADVANCE[text[i]] > TEXT_BOX_WIDTH:
                break
            width += ADVANCE[text[i]]
            i += 1
        lines.append(text[start:i])
        if i < len(text) and text[i] == "\n":
            i += 1
        if i >= len(text):
            return lines


def _random_text(rng: random.Random, size: int) -> str:
    words = ["de", "uplink", "eth0", "Flipper", "192.168.1.23", "is", "m", "WWWWWW", "a/b-c_d"]
    words += ["iiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiii", "x" * 40]
    seps = [" ", " ", " ", "  ", "\n", "\n\n"]
    return "".join(rng.choice(words) + rng.choice(seps) for _ in range(size)).strip()


# --- breedtetabel -----------------------------------------------------------------------------


def test_width_table_covers_printable_ascii_only() -> None:
    assert set(ADVANCE) == {chr(c) for c in range(0x20, 0x7F)}
    assert TEXT_BOX_WIDTH == WIDTH


def test_width_table_known_glyphs() -> None:
    known = {" ": 2, "i": 2, "l": 2, "m": 8, "W": 8, "M": 8, "a": 5, "e": 5, "0": 6, "@": 9}
    assert {c: ADVANCE[c] for c in known} == known
    assert {ADVANCE[c] for c in "/-_"} == {8, 6}
    assert max(ADVANCE.values()) == 9


def test_space_is_the_narrowest_glyph() -> None:
    # Voorwaarde voor het opvullen met spaties in velden van één regel (`to_text_box`).
    assert ADVANCE[" "] == min(ADVANCE.values())


def test_text_width_counts_unknown_chars_as_widest() -> None:
    assert p.text_width("uplink eth0") == 46
    assert p.text_width("") == 0
    assert p.text_width("é") == 9


# --- afbreken ---------------------------------------------------------------------------------


def test_wrap_breaks_at_spaces_not_inside_words() -> None:
    text = "De uplink eth0 heeft adres 192.168.1.23 en de Flipper is via BLE verbonden."
    lines = p.wrap_lines(text)
    assert lines == [
        "De uplink eth0 heeft adres",
        "192.168.1.23 en de Flipper is",
        "via BLE verbonden.",
    ]
    assert [p.text_width(line) for line in lines] == [108, 114, 80]
    assert _firmware_lines("\n".join(lines)) == lines


def test_wrap_keeps_fitting_lines_newlines_and_indent() -> None:
    text = "3 netwerken\nSterkst:\n MijnLab -40dBm [scope]\n\nklaar"
    assert p.wrap_lines(text) == text.split("\n")
    lines = p.wrap_lines("    " + " ".join(["woord"] * 30))
    assert lines[0].startswith("    woord") and not lines[1].startswith(" ")


def test_wrap_long_word_prefers_break_chars_then_hard_break() -> None:
    lines = p.wrap_lines("Bestand: /ext/subghz/garage/poort_links_boven_2024.sub werkt.")
    assert lines == ["Bestand: /ext/subghz/", "garage/poort_links_", "boven_2024.sub werkt."]
    assert [p.text_width(line) for line in lines] == [105, 90, 101]
    assert p.wrap_lines("x" * 50) == ["x" * 24, "x" * 24, "xx"]  # 24 * 5 px = 120
    assert p.wrap_lines("i" * 61) == ["i" * 60, "i"]


def test_wrap_prefix_on_first_line() -> None:
    prefix = p.text_width("Mislukt: ")
    assert prefix == 34
    text = "de wifi-adapter wlan1 staat niet in monitor mode"
    lines = p.wrap_lines(text, prefix_px=prefix)
    assert p.text_width(lines[0]) <= WIDTH - prefix
    assert all(p.text_width(line) <= WIDTH for line in lines[1:])
    assert " ".join(lines) == text
    # Past het eerste woord niet meer achter het voorvoegsel: eerste regel leeg.
    assert p.wrap_lines("Bevestiging", prefix_px=100)[0] == ""
    with pytest.raises(ValueError):
        p.wrap_lines("x", prefix_px=WIDTH)


def test_wrap_fuzz_fits_keeps_words_and_is_idempotent() -> None:
    rng = _rng(7)
    for _ in range(300):
        text = _random_text(rng, rng.randint(1, 40))
        lines = p.wrap_lines(text)
        assert all(p.text_width(line) <= WIDTH for line in lines)
        wrapped = "\n".join(lines)
        assert _firmware_lines(wrapped) == lines  # de firmware breekt nergens zelf meer af
        assert p.wrap_lines(wrapped) == lines
        # Elk woord dat op een regel past, staat heel op één regel.
        whole = {w for w in text.split() if p.text_width(w) <= WIDTH and "/" not in w}
        assert whole <= set(wrapped.split())
        assert "".join(wrapped.split()) == "".join(text.split())  # alleen witruimte anders


# --- to_text_box ------------------------------------------------------------------------------


def test_to_text_box_multiline_truncates_with_ellipsis_on_a_fitting_line() -> None:
    out = p.to_text_box("woord " * 200, max_len=100)
    assert len(out) <= 100 and out.endswith("...")
    assert all(p.text_width(line) <= WIDTH for line in out.split("\n"))
    long_word = p.to_text_box("W" * 300, max_len=60)  # breken voegt newlines toe
    assert len(long_word) <= 60 and long_word.endswith("...")
    assert all(p.text_width(line) <= WIDTH for line in long_word.split("\n"))
    assert p.to_text_box("", max_len=10) == ""
    with pytest.raises(ValueError):
        p.to_text_box("x", max_len=2)


def test_to_text_box_converts_to_ascii_first() -> None:
    assert p.to_text_box("Café “thuis” – 5 €", max_len=100) == 'Cafe "thuis" - 5 EUR'


@pytest.mark.parametrize("prefix", ["", "Mislukt: ", "Agent niet beschikbaar: ", "Fout 12: "])
def test_to_text_box_single_line_pads_so_the_firmware_breaks_between_words(prefix: str) -> None:
    rng = _rng(len(prefix))
    for _ in range(200):
        text = _random_text(rng, rng.randint(1, 12)).replace("\n", " ")
        out = p.to_text_box(
            text, max_len=p.MAX_ERROR_TEXT, multiline=False, prefix_px=p.text_width(prefix)
        )
        assert "\n" not in out and len(out) <= p.MAX_ERROR_TEXT
        p.Error(seq=1, ref=1, code=p.ErrorCode.TOOL_FAILED, text=out)  # geldig veld
        shown = [line.rstrip(" ") for line in _firmware_lines(prefix + out)]
        if out.endswith("..."):
            continue  # afgekapt: de woorden zijn niet meer dezelfde
        expected = p.wrap_lines(
            p.to_display_text(text, max_len=p.MAX_ERROR_TEXT), prefix_px=p.text_width(prefix)
        )
        expected[0] = prefix + expected[0]
        assert shown == [line.rstrip(" ") for line in expected]


def test_to_text_box_single_line_example() -> None:
    prefix = "Agent niet beschikbaar: "
    out = p.to_text_box(
        "Geen LLM ingesteld op de Pi (PITOOLBOX_LLM_*)",
        max_len=p.MAX_ERROR_TEXT,
        multiline=False,
        prefix_px=p.text_width(prefix),
    )
    assert [line.rstrip() for line in _firmware_lines(prefix + out)] == [
        "Agent niet beschikbaar: Geen",
        "LLM ingesteld op de Pi",
        "(PITOOLBOX_LLM_*)",
    ]
