"""Administratie van de open bevestigingen van één agent-sessie.

Alleen een mens bevestigt (zie `session`). Hier staat wat de sessie daarvoor bijhoudt: welke
pendings ze aan de frontend gaf, welke uitkomsten van buitenaf binnenkwamen (OK/Back op de
Flipper, een noodstop elders, de TTL), en het afwijzen zonder iets te zenden.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from pitoolbox.agent.types import ConfirmOutcome, ConfirmStatus, PendingConfirmation
from pitoolbox.core.tools.base import Risk

if TYPE_CHECKING:
    from pitoolbox.core.tools.runner import PendingResolved, ToolRunner


class OpenConfirmations:
    """De bevestigingen die de sessie aan de frontend gaf en die nog openstaan.

    `on_resolved` draait in willekeurige threads, soms onder de lock van de PendingStore; die
    zet alleen iets in een eigen wachtrij. De rest draait in de thread van de frontend.
    """

    def __init__(self) -> None:
        self._surfaced: dict[str, PendingConfirmation] = {}
        # Uitkomsten van buitenaf (andere threads): alleen voor pendings in `_watch`.
        self._lock = threading.Lock()
        self._watch: set[str] = set()
        self._external: list[PendingResolved] = []

    @property
    def pending(self) -> tuple[PendingConfirmation, ...]:
        return tuple(self._surfaced.values())

    def add(self, pending: PendingConfirmation) -> None:
        """Open, en luister naar wat er buiten de sessie mee gebeurt."""
        self._surfaced[pending.pending_id] = pending
        with self._lock:
            self._watch.add(pending.pending_id)

    def pop(self, pending_id: str) -> PendingConfirmation | None:
        """Niet meer open en niet meer luisteren; None als hij niet (meer) open stond."""
        known = self._surfaced.pop(pending_id, None)
        with self._lock:
            self._watch.discard(pending_id)
        return known

    def drop(self, pending_id: str) -> PendingConfirmation | None:
        """Niet meer open, voor een uitkomst uit `take_resolved` (`on_resolved` luistert al niet
        meer naar deze id)."""
        return self._surfaced.pop(pending_id, None)

    def on_resolved(self, event: PendingResolved) -> None:
        # Willekeurige thread, soms onder de lock van de PendingStore: alleen bewaren.
        with self._lock:
            if event.pending_id in self._watch:
                self._watch.discard(event.pending_id)
                self._external.append(event)

    def take_resolved(self) -> list[PendingResolved]:
        """De uitkomsten van buitenaf sinds de vorige keer."""
        with self._lock:
            events, self._external = self._external, []
        return events

    def clear(self) -> None:
        """Alles vergeten (na een noodstop is de PendingStore leeg)."""
        self._surfaced.clear()
        with self._lock:
            self._watch.clear()
            self._external.clear()


def reject(runner: ToolRunner, frontend: str, pending: PendingConfirmation) -> ConfirmOutcome:
    """De mens zei nee: de pending vervalt en de afwijzing komt in de audit-log."""
    # discard, niet claim: afwijzen zendt niets en mag dus ook met een verbonden Flipper.
    action = runner.ctx.pending.discard(pending.pending_id, frontend)
    if action is None:
        return ConfirmOutcome(ConfirmStatus.EXPIRED, pending)
    runner.audit.write(
        frontend=frontend,
        tool=action.tool,
        risk=str(Risk.TRANSMIT),
        args=action.args.model_dump(mode="json"),
        decision="rejected",
        ok=False,
        error="rejected_by_user",
        result=None,
        duration_ms=0,
        pending_id=action.id,
        source="agent",
    )
    return ConfirmOutcome(ConfirmStatus.REJECTED, pending)


def confirm_status(raw: dict[str, Any]) -> ConfirmStatus:
    if raw.get("ok"):
        return ConfirmStatus.EXECUTED
    if raw.get("error") == "unknown_pending":
        return ConfirmStatus.EXPIRED
    if raw.get("error") == "denied":
        return ConfirmStatus.DENIED
    return ConfirmStatus.FAILED
