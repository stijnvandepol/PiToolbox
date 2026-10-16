"""Modeltekst is onbetrouwbaar: hulpfuncties om hem veilig te tonen.

- `strip_think`: haalt redeneerblokken (`<think>...</think>`, MiniMax/Qwen/DeepSeek-R1-stijl)
  uit de tekst die de gebruiker ziet. Alleen voor weergave: de geschiedenis houdt de ruwe
  tekst, want MiniMax eist dat `content` ongewijzigd terugkomt (zie `llm/_openai_compat.py`).
- `for_terminal`: haalt stuurtekens weg (ANSI-escapes, carriage return, bidi-overrides),
  zodat modeltekst of tool-uitvoer het scherm niet kan herschrijven of een nep-prompt kan
  tonen (bijv. een vervalste "Bevestigen? [j/N]").
"""

from __future__ import annotations

import re

_TAG = r"think(?:ing)?"
_THINK_BLOCK = re.compile(rf"<{_TAG}>.*?</{_TAG}>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE = re.compile(rf"</{_TAG}>", re.IGNORECASE)
_THINK_OPEN = re.compile(rf"<{_TAG}>", re.IGNORECASE)

# C0 (behalve \t en \n), DEL, C1 en de Unicode-bidi-stuurtekens.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")


def strip_think(text: str) -> str:
    """Tekst zonder redeneerblokken, voor weergave."""
    text = _THINK_BLOCK.sub("", text)
    # Alleen een sluittag: de openingstag zat in het chat-template (Qwen3, DeepSeek-R1).
    closes = list(_THINK_CLOSE.finditer(text))
    if closes:
        text = text[closes[-1].end() :]
    # Een openingstag zonder sluittag: afgekapt midden in het redeneren.
    opened = _THINK_OPEN.search(text)
    if opened:
        text = text[: opened.start()]
    return text.strip()


def for_terminal(text: str) -> str:
    """Tekst zonder stuurtekens; tabs en regeleinden blijven staan."""
    return _CONTROL.sub("", text.replace("\r\n", "\n"))
