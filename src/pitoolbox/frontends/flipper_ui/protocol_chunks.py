"""Lange antwoorden in RESULT-delen (§4): opsplitsen (Pi) en weer samenvoegen (referentie)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from pitoolbox.frontends.flipper_ui.protocol_flow import SeqCounter
from pitoolbox.frontends.flipper_ui.protocol_messages import ResultChunk
from pitoolbox.frontends.flipper_ui.protocol_spec import (
    _RESULT_OVERHEAD,
    MAX_CHUNK,
    MAX_MSG,
    MAX_PARTS,
    MAX_RESULT,
    MIN_RX,
    ProtocolError,
)
from pitoolbox.frontends.flipper_ui.protocol_text import to_display_text

# --- resultaten in delen ----------------------------------------------------------------------


def chunk_result(
    ref: int,
    text: str,
    seq: SeqCounter,
    *,
    max_msg: int = MAX_MSG,
    max_parts: int = MAX_PARTS,
) -> list[ResultChunk]:
    """Splits een (lang) antwoord in RESULT-delen voor de FAP.

    De tekst wordt eerst schermklaar gemaakt (`to_display_text`) en afgekapt op wat in
    `max_parts` delen past. `max_msg` is de `max_rx` uit de HELLO van de FAP (hoogstens 512).
    """
    if not MIN_RX <= max_msg <= MAX_MSG:
        raise ValueError(f"max_msg buiten {MIN_RX}..{MAX_MSG}")
    if not 1 <= max_parts <= MAX_PARTS:
        raise ValueError(f"max_parts buiten 1..{MAX_PARTS}")
    per_part = min(MAX_CHUNK, max_msg - _RESULT_OVERHEAD)
    clean = to_display_text(text, max_len=per_part * max_parts, multiline=True)
    pieces = [clean[i : i + per_part] for i in range(0, len(clean), per_part)] or [""]
    total = len(pieces)
    return [
        ResultChunk(seq=seq.next(), ref=ref, part=i, total=total, text=piece)
        for i, piece in enumerate(pieces)
    ]


@dataclass
class _Partial:
    total: int
    started: float
    parts: dict[int, str] = field(default_factory=dict)
    size: int = 0


class Reassembler:
    """Zet RESULT-delen weer in elkaar, met harde grenzen op geheugen en tijd.

    De Pi stuurt delen op volgorde (stop-and-wait via de RPC-bevestiging), dus in de praktijk
    komen ze in volgorde binnen. Deze klasse is de referentie voor de FAP-logica en voor een
    nagebootste FAP in tests, en verdraagt daarom ook: andere volgorde, dubbele delen
    (identiek: genegeerd; anders: fout) en ontbrekende delen (verlopen na `timeout_s`).
    """

    def __init__(
        self,
        *,
        max_parts: int = MAX_PARTS,
        max_bytes: int = MAX_RESULT,
        max_open: int = 4,
        timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_parts = max_parts
        self._max_bytes = max_bytes
        self._max_open = max_open
        self._timeout = timeout_s
        self._clock = clock
        self._open: dict[int, _Partial] = {}
        self._done: deque[int] = deque(maxlen=32)

    @property
    def open_refs(self) -> list[int]:
        return list(self._open)

    def drop(self, ref: int) -> None:
        self._open.pop(ref, None)

    def expire(self) -> list[int]:
        """Gooi onvolledige antwoorden weg die te lang open staan; geeft hun refs."""
        now = self._clock()
        old = [ref for ref, p in self._open.items() if now - p.started > self._timeout]
        for ref in old:
            del self._open[ref]
        return old

    def feed(self, chunk: ResultChunk) -> str | None:
        """Voeg een deel toe. Geeft de volledige tekst als het antwoord compleet is.

        Raises:
            ProtocolError: grens overschreden of tegenstrijdige delen; dat antwoord is weg.
        """
        if chunk.ref in self._done:
            return None  # laat duplicaat van een al afgerond antwoord
        if chunk.total > self._max_parts:
            self.drop(chunk.ref)
            raise ProtocolError(f"antwoord van {chunk.total} delen > {self._max_parts}")
        partial = self._open.get(chunk.ref)
        if partial is None:
            self.expire()
            if len(self._open) >= self._max_open:
                raise ProtocolError(f"te veel open antwoorden (> {self._max_open})")
            partial = self._open[chunk.ref] = _Partial(total=chunk.total, started=self._clock())
        if chunk.total != partial.total:
            self.drop(chunk.ref)
            raise ProtocolError(f"ref {chunk.ref}: total wisselt ({partial.total}/{chunk.total})")
        known = partial.parts.get(chunk.part)
        if known is not None:
            if known == chunk.text:
                return None
            self.drop(chunk.ref)
            raise ProtocolError(f"ref {chunk.ref}: deel {chunk.part} twee keer, met andere tekst")
        if partial.size + len(chunk.text) > self._max_bytes:
            self.drop(chunk.ref)
            raise ProtocolError(f"ref {chunk.ref}: antwoord groter dan {self._max_bytes} bytes")
        partial.parts[chunk.part] = chunk.text
        partial.size += len(chunk.text)
        if len(partial.parts) < partial.total:
            return None
        del self._open[chunk.ref]
        self._done.append(chunk.ref)
        return "".join(partial.parts[i] for i in range(partial.total))
