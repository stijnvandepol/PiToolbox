"""Tekst in protocol v1: alleen printbare ASCII (het Flipper-font), validatie van de
tekstvelden (protocol §2) en omzetting van willekeurige tekst naar schermtekst."""

from __future__ import annotations

import unicodedata
from typing import Annotated

from pydantic import AfterValidator

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
# weigert alles daarbuiten. Newline alleen in samenvatting en resultaat.
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
    return _check_text(v, max_len=MAX_PROGRESS)


def _v_chunk(v: str) -> str:
    return _check_text(v, max_len=MAX_CHUNK, multiline=True)


def _v_error(v: str) -> str:
    return _check_text(v, max_len=MAX_ERROR_TEXT)


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
