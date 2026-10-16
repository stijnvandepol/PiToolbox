"""Volgnummers (zenden en ontvangen) en rate limiting per richting (protocol §4)."""

from __future__ import annotations

import time
from collections.abc import Callable

from pitoolbox.frontends.flipper_ui.protocol_spec import _U16_MAX, RATE_BURST, RATE_PER_S

# --- volgnummers, rate limit ------------------------------------------------------------------

_SEQ_MOD = _U16_MAX  # seq loopt 1..65535; 0 komt niet voor


class SeqCounter:
    """Volgnummers van één afzender: 1, 2, ..., 65535, 1, ..."""

    def __init__(self, start: int = 0) -> None:
        self._n = start

    def next(self) -> int:
        self._n = self._n % _SEQ_MOD + 1
        return self._n


class SeqTracker:
    """Ontvangstkant: herkent duplicaten en oude berichten (serieel rekenen, RFC 1982).

    `accept(seq)` geeft het aantal overgeslagen nummers (0 = precies de volgende), of None
    als het bericht niet nieuwer is dan het laatst geaccepteerde (duplicaat: weggooien).
    """

    def __init__(self) -> None:
        self._last: int | None = None

    def reset(self) -> None:
        self._last = None

    def accept(self, seq: int) -> int | None:
        if not 1 <= seq <= _U16_MAX:
            raise ValueError(f"seq {seq} buiten 1..65535")
        if self._last is None:
            self._last = seq
            return 0
        distance = (seq - self._last) % _SEQ_MOD
        if distance == 0 or distance >= _SEQ_MOD // 2:
            return None
        self._last = seq
        return distance - 1


class RateLimiter:
    """Token bucket: gemiddeld `rate` berichten per seconde, pieken tot `burst`."""

    def __init__(
        self,
        rate: float = RATE_PER_S,
        burst: int = RATE_BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate > 0 en burst >= 1 vereist")
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._stamp = clock()
        self.dropped = 0

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._stamp) * self._rate)
        self._stamp = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self.dropped += 1
        return False
