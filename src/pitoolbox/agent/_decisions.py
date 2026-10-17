"""Uitkomsten van bevestigingen door een mens, en de noodstop: alleen voor de frontend.

Alleen een mens bevestigt (zie `session`): de frontend roept `confirm` na een expliciete
menselijke keuze aan, en `take_external` verwerkt wat er buiten de sessie met haar open
bevestigingen gebeurde (OK/Back op de Flipper, een noodstop elders, de TTL). Het model hoort
alleen de uitkomst, als melding in de volgende beurt. Zie `_session_core` voor hoe de delen van
`AgentSession` op elkaar bouwen.
"""

from __future__ import annotations

from typing import Any

from pitoolbox.agent._confirmations import confirm_status, reject
from pitoolbox.agent._session_core import _SessionCore, note
from pitoolbox.agent._tool_calls import serialize_view
from pitoolbox.agent.types import ConfirmOutcome, ConfirmStatus, PendingConfirmation
from pitoolbox.core.tools.runner import (
    CONFIRM_ON_FLIPPER,
    CONFIRM_TOOL,
    PendingResolved,
    Resolution,
)

EMERGENCY_STOP_TOOL = "emergency_stop"


class _HumanDecisions(_SessionCore):
    """Zie `_session_core` voor hoe de delen van `AgentSession` op elkaar bouwen."""

    # --- alleen voor de frontend (na een expliciete menselijke keuze) ---------------------

    def confirm(self, pending: PendingConfirmation, *, approved: bool) -> ConfirmOutcome:
        """Voert een bevestiging uit of wijst hem af. Alleen aanroepen op menselijke invoer.

        Gaat via `runner.call(self.frontend, "confirm_action", ...)`: dezelfde frontend-id als
        de call die de pending maakte, dus binding, TTL en eenmalig gebruik blijven gelden.
        """
        known = self._confirmations.pop(pending.pending_id)
        if known is None:
            outcome = ConfirmOutcome(ConfirmStatus.EXPIRED, pending)
        elif not approved:
            outcome = reject(self.runner, self.frontend, known)
        else:
            raw = self.runner.call(self.frontend, CONFIRM_TOOL, {"pending_id": known.pending_id})
            if raw.get("error") == CONFIRM_ON_FLIPPER:
                # Niets gebeurd: de pending staat nog (de mens bevestigt op de Flipper).
                self._confirmations.add(known)
                return ConfirmOutcome(ConfirmStatus.ON_FLIPPER, known, raw)
            outcome = ConfirmOutcome(confirm_status(raw), known, raw)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def take_external(self) -> list[ConfirmOutcome]:
        """Verwerk wat er buiten deze sessie met haar open bevestigingen gebeurde.

        Uitgevoerd of afgewezen op de Flipper, gewist door een noodstop elders, of verlopen. Per
        stuk een melding voor het model (zoals bij `confirm`); de frontend kan de uitkomsten
        tonen. Eerst wordt de `PendingStore` opgeschoond, zodat een verlopen TTL ook meetelt.
        """
        if self._confirmations.pending:
            self.runner.ctx.pending.list()  # verlopen pendings melden (lui opgeschoond)
        outcomes: list[ConfirmOutcome] = []
        for event in self._confirmations.take_resolved():
            outcome = self._apply_resolution(event)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _apply_resolution(self, event: PendingResolved) -> ConfirmOutcome | None:
        if event.resolution is Resolution.CONFIRMED:
            return self.external_outcome(event.pending_id, approved=True, result=event.result)
        if event.resolution is Resolution.REJECTED:
            return self.external_outcome(event.pending_id, approved=False, result={"ok": True})
        known = self._confirmations.drop(event.pending_id)
        if known is None:
            return None
        status = (
            ConfirmStatus.CLEARED
            if event.resolution is Resolution.CLEARED
            else ConfirmStatus.EXPIRED
        )
        outcome = ConfirmOutcome(status, known, None)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def external_outcome(
        self, pending_id: str, *, approved: bool, result: dict[str, Any] | None
    ) -> ConfirmOutcome | None:
        """Een pending van deze sessie is buiten de sessie afgehandeld (OK/Back op de Flipper).

        Werkt alleen de eigen administratie bij en geeft het model in de volgende beurt een
        melding, net als `confirm`. None als deze sessie de pending niet (meer) kent.
        """
        known = self._confirmations.pop(pending_id)
        if known is None:
            return None
        if not approved:
            status = (
                ConfirmStatus.REJECTED
                if result is not None and result.get("ok")
                else ConfirmStatus.EXPIRED
            )
            outcome = ConfirmOutcome(status, known, None)
        else:
            outcome = ConfirmOutcome(confirm_status(result or {}), known, result)
        self._notes.append(self._confirm_note(outcome))
        return outcome

    def _confirm_note(self, outcome: ConfirmOutcome) -> str:
        what = f"{outcome.pending.tool}: {outcome.pending.summary}"
        if outcome.status is ConfirmStatus.REJECTED:
            return note(f"De gebruiker heeft afgewezen ({what}). Er is niets uitgevoerd.")
        if outcome.status is ConfirmStatus.EXPIRED:
            return note(f"Bevestiging verlopen of al gebruikt ({what}). Er is niets uitgevoerd.")
        if outcome.status is ConfirmStatus.CLEARED:
            return note(f"Bevestiging gewist door een noodstop ({what}). Er is niets uitgevoerd.")
        result = serialize_view(outcome.result or {}, self.limits.max_result_chars)
        if outcome.status is ConfirmStatus.DENIED:
            return note(f"Bevestigd, maar alsnog geweigerd door de policy ({what}): {result}")
        if outcome.status is ConfirmStatus.FAILED:
            return note(f"Bevestigd en uitgevoerd, maar met een fout ({what}): {result}")
        return note(f"De gebruiker heeft bevestigd en het is uitgevoerd ({what}): {result}")

    def emergency_stop(self) -> dict[str, Any]:
        """Noodstop op verzoek van de mens (`/stop`); het model hoort het in de volgende beurt."""
        raw = self.runner.call(self.frontend, EMERGENCY_STOP_TOOL, {})
        self._confirmations.clear()  # PendingStore is gewist; deze id's zijn waardeloos
        self._notes.append(
            note(
                "De gebruiker heeft een noodstop uitgevoerd: "
                f"{serialize_view(raw, self.limits.max_result_chars)}"
            )
        )
        return raw
