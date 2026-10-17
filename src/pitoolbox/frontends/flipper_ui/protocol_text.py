"""Tekst in protocol v1: alleen printbare ASCII (het Flipper-font), validatie van de
tekstvelden (protocol §2), omzetting van willekeurige tekst naar schermtekst en het afbreken
op woordgrenzen voor de TextBox van de FAP."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated

from pydantic import AfterValidator

from pitoolbox.frontends.flipper_ui._font_widths import ADVANCE, TEXT_BOX_WIDTH
from pitoolbox.frontends.flipper_ui.protocol_spec import (
    MAX_CHUNK,
    MAX_ERROR_TEXT,
    MAX_LABEL,
    MAX_PROGRESS,
    MAX_SUMMARY,
    MAX_TOOL_NAME,
    MAX_UPLINK,
)

# --- tekst ------------------------------------------------------------------------------------

# Het Flipper-font kent alleen ASCII. De Pi zet tekst om vóór het versturen; de decoder
# weigert alles daarbuiten. Newline alleen in teksten die de FAP in een TextBox toont
# (samenvatting, resultaat, PROGRESS en ERROR); de Pi breekt die zelf af (`to_text_box`).
_PRINTABLE = frozenset(range(0x20, 0x7F))
_PRINTABLE_NL = _PRINTABLE | {0x0A}


def _check_text(value: str, *, max_len: int, min_len: int = 0, multiline: bool = False) -> str:
    allowed = _PRINTABLE_NL if multiline else _PRINTABLE
    if any(ord(c) not in allowed for c in value):
        raise ValueError("alleen printbare ASCII toegestaan" + (" (en \\n)" if multiline else ""))
    if not min_len <= len(value) <= max_len:
        raise ValueError(f"lengte {len(value)} buiten {min_len}..{max_len}")
    return value


def _v_label(v: str) -> str:
    return _check_text(v, max_len=MAX_LABEL, min_len=1)


def _v_summary(v: str) -> str:
    return _check_text(v, max_len=MAX_SUMMARY, min_len=1, multiline=True)


def _v_progress(v: str) -> str:
    return _check_text(v, max_len=MAX_PROGRESS, multiline=True)


def _v_chunk(v: str) -> str:
    return _check_text(v, max_len=MAX_CHUNK, multiline=True)


def _v_error(v: str) -> str:
    return _check_text(v, max_len=MAX_ERROR_TEXT, multiline=True)


def _v_uplink(v: str) -> str:
    return _check_text(v, max_len=MAX_UPLINK)


def _v_tool(v: str) -> str:
    return _check_text(v, max_len=MAX_TOOL_NAME)


Label = Annotated[str, AfterValidator(_v_label)]
Summary = Annotated[str, AfterValidator(_v_summary)]
ProgressText = Annotated[str, AfterValidator(_v_progress)]
ChunkText = Annotated[str, AfterValidator(_v_chunk)]
ErrorText = Annotated[str, AfterValidator(_v_error)]
UplinkText = Annotated[str, AfterValidator(_v_uplink)]
ToolText = Annotated[str, AfterValidator(_v_tool)]


_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "″": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "…": "...",
        "•": "*",
        "·": "*",
        "→": "->",
        "←": "<-",
        "×": "x",
        "€": "EUR",
        "°": " gr",
        " ": " ",
        "\t": " ",
    }
)


def to_display_text(text: str, *, max_len: int, multiline: bool = False) -> str:
    """Maak willekeurige tekst (toolresultaat, modelantwoord) geschikt voor het Flipper-scherm.

    Typografische tekens worden ASCII, accenten verdwijnen (`ë` -> `e`), al het andere buiten
    printbare ASCII wordt `?`. Te lang wordt afgekapt met `...`. Het resultaat komt altijd
    door de validatie van de berichtvelden met dezelfde `max_len`/`multiline`.
    """
    if max_len < 0:
        raise ValueError("max_len moet >= 0 zijn")
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_TYPOGRAPHY)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    allowed = _PRINTABLE_NL if multiline else _PRINTABLE
    out = "".join(c if ord(c) in allowed else (" " if c == "\n" else "?") for c in text)
    if len(out) > max_len:
        out = out[: max_len - 3] + "..." if max_len >= 3 else out[:max_len]
    return out


# --- regels voor de TextBox -------------------------------------------------------------------

# De TextBox van de firmware (text_box.c, `text_box_seek_next_line`) telt per teken de breedte
# op en begint een nieuwe regel zodra het volgende teken niet meer in TEXT_BOX_WIDTH past, ook
# midden in een woord. Daarom breekt de Pi zelf af: op spaties, en alleen in een woord dat
# breder is dan een hele regel na een van _BREAK_AFTER, of anders midden in het woord.
_UNKNOWN_ADVANCE = max(ADVANCE.values())  # teken buiten het font: reken met het breedste
_SPACE_ADVANCE = ADVANCE[" "]
_BREAK_AFTER = frozenset("/-_")
_ELLIPSIS = "..."
_TOKENS = re.compile(r" +|[^ ]+")


def text_width(text: str) -> int:
    """Breedte in pixels zoals de TextBox hem optelt (zonder newlines)."""
    return sum(ADVANCE.get(c, _UNKNOWN_ADVANCE) for c in text)


def _cut(word: str, room: int, *, hard: bool) -> int:
    """Hoeveel tekens van `word` op `room` pixels: tot en met het laatste breekteken dat past,
    of (`hard`) zoveel als er passen."""
    used = fit = after_break = 0
    for i, c in enumerate(word):
        used += ADVANCE.get(c, _UNKNOWN_ADVANCE)
        if used > room:
            break
        fit = i + 1
        if c in _BREAK_AFTER:
            after_break = fit
    return after_break or (fit if hard else 0)


def _wrap_paragraph(paragraph: str, first_room: int) -> list[str]:
    if text_width(paragraph) <= first_room:
        return [paragraph]
    tokens = _TOKENS.findall(paragraph)
    if len(tokens) > 1 and tokens[0].startswith(" "):
        tokens[:2] = [tokens[0] + tokens[1]]  # inspringing blijft bij het eerste woord
    lines: list[str] = []
    line = gap = ""
    for token in tokens:
        if token.startswith(" ") and (line or lines):
            gap = token  # spaties tussen woorden; bij een regelovergang vallen ze weg
            continue
        rest = token
        while rest:
            room = TEXT_BOX_WIDTH if lines else first_room
            if text_width(line + gap + rest) <= room:
                line, gap, rest = line + gap + rest, "", ""
                break
            if text_width(rest) > TEXT_BOX_WIDTH:  # past nergens heel: hier al afbreken
                cut = _cut(rest, room - text_width(line + gap), hard=not line)
                if cut:
                    line, rest = line + gap + rest[:cut], rest[cut:]
            lines.append(line)
            line = gap = ""
    if gap and text_width(line + gap) <= (TEXT_BOX_WIDTH if lines else first_room):
        line += gap
    lines.append(line)
    return lines


def wrap_lines(text: str, *, prefix_px: int = 0) -> list[str]:
    """Verdeel tekst in regels die elk in de TextBox passen (hoogstens TEXT_BOX_WIDTH pixels).

    Bestaande newlines blijven regelgrenzen. `prefix_px` is wat de FAP zelf vóór de tekst op
    de eerste regel zet (bijv. "Mislukt: "); past het eerste woord daar niet meer achter, dan
    is de eerste regel leeg. Een regel die al past, blijft precies zoals hij was, dus opnieuw
    afbreken verandert niets.
    """
    if not 0 <= prefix_px < TEXT_BOX_WIDTH:
        raise ValueError(f"prefix_px buiten 0..{TEXT_BOX_WIDTH - 1}")
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines += _wrap_paragraph(paragraph, TEXT_BOX_WIDTH - (0 if lines else prefix_px))
    return lines


def to_text_box(text: str, *, max_len: int, prefix_px: int = 0) -> str:
    """Willekeurige tekst als tekst voor een TextBox van de FAP, afgebroken op woordgrenzen.

    Eerst `to_display_text` (ASCII, afkappen), dan `wrap_lines`, met een newline tussen de
    regels. Wordt de tekst daardoor langer dan `max_len`, dan eindigt hij op "..." op een regel
    die nog past. Het resultaat komt altijd door de validatie van de velden die een TextBox
    vullen (samenvatting, RESULT, PROGRESS, ERROR) met dezelfde `max_len`.
    """
    if max_len < len(_ELLIPSIS):
        raise ValueError(f"max_len moet >= {len(_ELLIPSIS)} zijn")
    lines = wrap_lines(to_display_text(text, max_len=max_len, multiline=True), prefix_px=prefix_px)
    rooms = [TEXT_BOX_WIDTH - prefix_px] + [TEXT_BOX_WIDTH] * (len(lines) - 1)
    ends = ["\n"] * (len(lines) - 1) + [""]
    full = "".join(line + end for line, end in zip(lines, ends, strict=True))
    if len(full) <= max_len:
        return full
    budget = max_len - len(_ELLIPSIS)
    done = ""
    for line, room, end in zip(lines, rooms, ends, strict=True):
        if len(done) + len(line) + len(end) > budget:
            # Zoveel van deze regel als er met "..." erachter nog past, in tekens en pixels.
            head = line[: budget - len(done)]
            while head and text_width(head + _ELLIPSIS) > room:
                head = head[:-1]
            return done + head + _ELLIPSIS
        done += line + end
    return full  # niet bereikbaar: `full` is langer dan max_len
