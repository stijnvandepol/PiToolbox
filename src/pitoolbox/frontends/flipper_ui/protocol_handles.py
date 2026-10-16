"""Bevestigings-handles: welk antwoord van de FAP bij welke open bevestiging hoort (§6)."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from pitoolbox.frontends.flipper_ui.protocol_spec import _U32_MAX, ErrorCode, ProtocolError

# --- bevestigings-handles ---------------------------------------------------------------------

T = TypeVar("T")


@dataclass
class _Issued(Generic[T]):
    token: T
    instance: int
    expires: float


class ConfirmHandles(Generic[T]):
    """Handles voor CONFIRM_PROMPT/CONFIRM_ANSWER.

    Een handle is een willekeurig u32 (nooit 0, nooit hergebruikt binnen dit object) die de
    Pi uitgeeft voor één open bevestiging (`token`, bijv. de `PendingConfirmation` van de
    agent-loop). Een antwoord telt alleen als de handle door deze Pi is uitgegeven, aan
    dezelfde FAP-instance, nog niet gebruikt is en de TTL (Pi-klok) niet verstreken is. De
    FAP ziet nooit de `pending_id` zelf.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_open: int = 8) -> None:
        self._clock = clock
        self._max_open = max_open
        self._open: dict[int, _Issued[T]] = {}
        self._issued: set[int] = set()

    def __len__(self) -> int:
        return len(self._open)

    def issue(self, token: T, *, instance: int, ttl_s: float) -> int:
        if ttl_s <= 0:
            raise ValueError("ttl_s moet > 0 zijn")
        self.expire()
        if len(self._open) >= self._max_open:
            raise ProtocolError("te veel open bevestigingen", code=ErrorCode.BUSY)
        while True:
            handle = secrets.randbelow(_U32_MAX) + 1
            if handle not in self._issued:
                break
        self._issued.add(handle)
        self._open[handle] = _Issued(token, instance, self._clock() + ttl_s)
        return handle

    def take(self, handle: int, *, instance: int) -> T | None:
        """Eenmalig ophalen. None bij onbekend, al gebruikt, verlopen of andere instance.

        Een antwoord van een andere instance laat de handle staan: een oude of vervalste
        FAP kan zo de bevestiging van de huidige niet "opmaken".
        """
        issued = self._open.get(handle)
        if issued is None or issued.instance != instance:
            return None
        del self._open[handle]
        if self._clock() >= issued.expires:
            return None
        return issued.token

    def revoke(self, handle: int) -> T | None:
        issued = self._open.pop(handle, None)
        return None if issued is None else issued.token

    def revoke_all(self) -> list[tuple[int, T]]:
        """Bij sessieverlies of een nieuwe HELLO: alle open handles ongeldig."""
        out = [(h, i.token) for h, i in self._open.items()]
        self._open.clear()
        return out

    def expire(self) -> list[tuple[int, T]]:
        """Verlopen handles weghalen; de bridge stuurt per stuk CONFIRM_CLOSED(EXPIRED)."""
        now = self._clock()
        gone = [(h, i.token) for h, i in self._open.items() if now >= i.expires]
        for h, _ in gone:
            del self._open[h]
        return gone
