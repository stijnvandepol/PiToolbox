"""Zendrij en zender van de bridge: Pi -> FAP (thread `flipper-ui-sender`).

Stop-and-wait (`device.app_send` wacht op de bevestiging), hoogstens één bericht per
`send_gap_s` (5/s), 3x opnieuw na 200 ms bij `AppRejected`. PROGRESS hoogstens 1/s; een
nieuwere vervangt een oudere die nog niet weg is (protocol §4).

De rij heeft een eigen lock. De hoofdlus zet er berichten in; de zender-thread haalt ze eruit
en raakt verder geen toestand van de bridge: wat er na het versturen moet gebeuren
(`on_sent`, `on_failed`, de link is dood), post hij naar de hoofdlus.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from pitoolbox.core.devices.flipper import AppNotRunning, AppRejected, FlipperDevice
from pitoolbox.core.errors import DeviceError, ToolboxError
from pitoolbox.frontends.flipper_ui.bridge_types import LOG_NAME, BridgeTiming
from pitoolbox.frontends.flipper_ui.protocol import Message, ProtocolError, SeqCounter, encode

log = logging.getLogger(LOG_NAME)

_MAX_OUTBOX = 64


class _Delivery(Enum):
    SENT = 1
    DROPPED = 2  # na de retries nog geweigerd; link is in orde
    DEAD = 3  # app of link weg


@dataclass
class _Out:
    # Krijgt de seq pas bij versturen (volgorde = seq-volgorde). None: niet meer sturen (bijv.
    # een vraag waarvan de TTL al op is).
    build: Callable[[int], Message | None]
    instance: int | None  # None: altijd sturen (fouten vóór HELLO)
    on_sent: Callable[[], None] | None = None  # draait in de hoofdlus
    progress_ref: int | None = None
    # Draait in de hoofdlus als het bericht niet afgeleverd kon worden terwijl de link nog in
    # orde leek (zendrij vol, de app weigerde 4x, niet te coderen). Niet bij DEAD.
    on_failed: Callable[[], None] | None = None


class _Sender:
    """De zendrij (`put`, `put_progress`, ...) en de zender-thread (`run`).

    `post` zet werk in de inbox van de hoofdlus; `on_delivered(type, instance)` wordt in de
    zender-thread aangeroepen en mag niet blokkeren; `on_dead` gaat via `post`.
    """

    def __init__(
        self,
        device: FlipperDevice,
        timing: BridgeTiming,
        stopping: threading.Event,
        post: Callable[[Callable[[], None]], None],
        on_delivered: Callable[[str, int | None], None],
        on_dead: Callable[[], None],
    ) -> None:
        self._device = device
        self._timing = timing
        self._stopping = stopping
        self._post = post
        self._on_delivered = on_delivered
        self._on_dead = on_dead
        self._out_cond = threading.Condition()
        self._outbox: deque[_Out] = deque()
        self._progress: _Out | None = None
        self._send_instance: int | None = None
        self._last_send = 0.0
        self._last_progress = 0.0
        self._tx_seq = SeqCounter()
        self.dropped_out = 0

    # --- de rij (vanuit de hoofdlus) ---------------------------------------------------------

    def put(
        self,
        build: Callable[[int], Message | None],
        instance: int | None,
        *,
        on_sent: Callable[[], None] | None = None,
        on_failed: Callable[[], None] | None = None,
    ) -> bool:
        """Achteraan in de rij; False als de rij vol is (het bericht is dan weg)."""
        with self._out_cond:
            full = len(self._outbox) >= _MAX_OUTBOX
            if full:
                self.dropped_out += 1
                log.warning("zendrij naar de app vol; bericht weggegooid")
            else:
                self._outbox.append(_Out(build, instance, on_sent, on_failed=on_failed))
                self._out_cond.notify_all()
        return not full

    def put_progress(self, build: Callable[[int], Message | None], instance: int, ref: int) -> None:
        """De PROGRESS die als volgende mag (vervangt een oudere die nog wacht)."""
        with self._out_cond:
            self._progress = _Out(build, instance, progress_ref=ref)
            self._out_cond.notify_all()

    def drop_progress(self, ref: int) -> None:
        """Een PROGRESS voor `ref` die nog wacht, is achterhaald (het antwoord is er)."""
        with self._out_cond:
            if self._progress is not None and self._progress.progress_ref == ref:
                self._progress = None

    def bind(self, instance: int) -> None:
        """Nieuwe FAP-instance: alleen berichten zonder instance (fouten vóór HELLO) blijven."""
        with self._out_cond:
            self._send_instance = instance
            self._outbox = deque(o for o in self._outbox if o.instance is None)
            self._progress = None

    def unbind(self) -> None:
        """Geen app meer: de rij leeg."""
        with self._out_cond:
            self._send_instance = None
            self._outbox.clear()
            self._progress = None

    def stop(self) -> None:
        """Rij leeg en de zender wekken; `stopping` is al gezet, dus hij eindigt."""
        with self._out_cond:
            self._outbox.clear()
            self._out_cond.notify_all()

    # --- zender-thread -----------------------------------------------------------------------

    def run(self) -> None:
        while True:
            taken = self._take()
            if taken is None:
                return
            item, current = taken
            if item.instance is not None and item.instance != current:
                continue  # bedoeld voor een app die er niet meer is
            self._send_one(item, current)

    def _take(self) -> tuple[_Out, int | None] | None:
        """Wacht op het volgende bericht; None als de bridge stopt."""
        with self._out_cond:
            item = None
            while not self._stopping.is_set():
                item = self._next_out()
                if item is not None:
                    break
                self._out_cond.wait(self._timing.tick_s)
            if self._stopping.is_set():
                return None
            current = self._send_instance
        assert item is not None  # noqa: S101
        return item, current

    def _next_out(self) -> _Out | None:
        """Onder `_out_cond`: eerst de gewone rij, dan (hoogstens 1/s) de laatste PROGRESS."""
        if self._outbox:
            return self._outbox.popleft()
        if self._progress is not None and (
            time.monotonic() - self._last_progress >= self._timing.progress_gap_s
        ):
            item, self._progress = self._progress, None
            self._last_progress = time.monotonic()
            return item
        return None

    def _send_one(self, item: _Out, current: int | None) -> None:
        wait = self._last_send + self._timing.send_gap_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            msg = item.build(self._tx_seq.next())
            data = encode(msg) if msg is not None else None
        except (ProtocolError, ValueError):
            log.exception("bericht naar de app kon niet gecodeerd worden")
            if item.on_failed is not None:
                self._post(item.on_failed)
            return
        if msg is None or data is None:
            return  # achterhaald (bijv. de TTL van een vraag was al op)
        outcome = self._deliver(data)
        self._last_send = time.monotonic()
        self._after_delivery(outcome, item, type(msg).__name__, current)

    def _after_delivery(
        self, outcome: _Delivery, item: _Out, msg_type: str, current: int | None
    ) -> None:
        if outcome is _Delivery.SENT:
            self._on_delivered(msg_type, current)
            if item.on_sent is not None:
                self._post(item.on_sent)
            return
        if outcome is _Delivery.DROPPED:
            if item.on_failed is not None:
                self._post(item.on_failed)
            return
        if outcome is _Delivery.DEAD:
            with self._out_cond:
                self._outbox.clear()
                self._progress = None
            self._post(self._on_dead)

    def _deliver(self, data: bytes) -> _Delivery:
        for attempt in range(self._timing.send_retries + 1):
            try:
                self._device.app_send(data, self._timing.send_timeout_s)
                return _Delivery.SENT
            except AppRejected:
                if attempt == self._timing.send_retries:
                    log.warning("app weigerde een bericht %d keer; laten vallen", attempt + 1)
                    return _Delivery.DROPPED
                time.sleep(self._timing.send_retry_s)
            except (AppNotRunning, DeviceError) as exc:
                log.info("versturen naar de app mislukte: %s", exc)
                return _Delivery.DEAD
            except ToolboxError as exc:  # bijv. Aborted door een noodstop midden in een write
                log.info("versturen naar de app afgebroken: %s", exc)
                return _Delivery.DROPPED
        return _Delivery.DROPPED  # pragma: no cover
