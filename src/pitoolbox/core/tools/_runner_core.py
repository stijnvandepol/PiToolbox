"""Kern van `ToolRunner`: confirm_action-spec, pending-uitkomsten, uitvoeren en audit.

`ToolRunner` (`runner`) bouwt op `_RunnerCore` (hier): de registry, het afsluiten, de
luisteraars voor de uitkomst van pendings, het uitvoeren van een handler en de auditregel per
aanroep. De beslisstroom zelf (`call`, `confirm_action`, de fysieke bevestiger) staat in
`runner`; dat is ook het publieke importpad voor alles hieronder.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import Field

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.errors import ToolboxError
from pitoolbox.core.policy import PendingChange, PendingEvent, Policy
from pitoolbox.core.tools.base import Registry, Risk, ToolArgs, ToolContext, ToolSpec

log = logging.getLogger("pitoolbox.core.tools.runner")

CONFIRM_TOOL = "confirm_action"


class Resolution(StrEnum):
    """Hoe een pending afliep (zie `PendingResolved`)."""

    CONFIRMED = "confirmed"  # bevestigd; `result` is de uitkomst (ok, fout of policy-deny)
    REJECTED = "rejected"  # afgewezen door een mens (chat of Back op de Flipper)
    EXPIRED = "expired"  # TTL verstreken
    CLEARED = "cleared"  # gewist (noodstop of afsluiten)


@dataclass(frozen=True)
class PendingResolved:
    pending_id: str
    tool: str
    created_by: str  # frontend die de pending maakte
    resolution: Resolution
    result: dict[str, Any] | None = None  # alleen bij CONFIRMED: ruwe uitkomst van de runner
    confirmed_on: str | None = None  # "flipper" bij OK op de Flipper


ResolutionListener = Callable[[PendingResolved], None]

_STORE_RESOLUTIONS = {
    PendingChange.REJECTED: Resolution.REJECTED,
    PendingChange.EXPIRED: Resolution.EXPIRED,
    PendingChange.CLEARED: Resolution.CLEARED,
}


class ConfirmArgs(ToolArgs):
    pending_id: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


def _confirm_placeholder(ctx: ToolContext, args: Any) -> dict[str, Any]:
    raise AssertionError("confirm_action wordt door ToolRunner zelf afgehandeld")


CONFIRM_SPEC = ToolSpec(
    name=CONFIRM_TOOL,
    description=(
        "Voer een eerder voorgestelde transmit-actie uit. Alleen aanroepen nadat de gebruiker "
        "in dit gesprek expliciet 'ja' heeft gezegd op de getoonde samenvatting."
    ),
    risk=Risk.CONTROL,
    params=ConfirmArgs,
    handler=_confirm_placeholder,
)


class _RunnerCore:
    """Zie de module-docstring."""

    def __init__(
        self, registry: Registry, ctx: ToolContext, policy: Policy, audit: AuditLog
    ) -> None:
        self.registry = registry
        self.ctx = ctx
        self.policy = policy
        self.audit = audit
        self._res_lock = threading.Lock()
        self._res_listeners: list[ResolutionListener] = []
        ctx.pending.subscribe(self._on_pending_event)

    def specs(self) -> list[ToolSpec]:
        return self.registry.all()

    def allowed_specs(self) -> list[ToolSpec]:
        """De tools die een frontend aanbiedt: alles wat `call()` niet al op de allowlist weigert.

        Spiegelt `call()`: `confirm_action` wordt vóór de policy afgehandeld (de pending-tool
        zelf wordt bij het uitvoeren opnieuw beslist) en blijft dus altijd staan; de rest volgt
        `Policy.allows_tool` (een lege allowlist staat alles toe). Alleen voor de lijst: `call()`
        blijft zelf afdwingen.
        """
        return [
            spec
            for spec in self.registry.all()
            if spec.name == CONFIRM_TOOL or self.policy.allows_tool(spec.name)
        ]

    def shutdown(self) -> None:
        """Bij afsluiten: geen achtergebleven captures, Flipper-poort netjes dicht."""
        for name, fn in (
            ("captures.stop_all", self.ctx.captures.stop_all),
            ("pending.clear", self.ctx.pending.clear),
            ("flipper.close", self.ctx.flipper.close),
        ):
            try:
                fn()
            except Exception:
                log.exception("shutdown-stap %s faalde", name)

    # --- uitkomst van pendings -------------------------------------------------------------

    def subscribe_resolutions(self, listener: ResolutionListener) -> Callable[[], None]:
        """Luister naar de uitkomst van elke pending; geeft een functie om af te melden.

        `listener` wordt aangeroepen in de thread die de pending afhandelde, soms onder de lock
        van de `PendingStore`: niet blokkeren, de store/runner niet aanroepen.
        """
        with self._res_lock:
            self._res_listeners.append(listener)

        def unsubscribe() -> None:
            with self._res_lock:
                if listener in self._res_listeners:
                    self._res_listeners.remove(listener)

        return unsubscribe

    def _resolved(self, event: PendingResolved) -> None:
        with self._res_lock:
            listeners = list(self._res_listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                log.exception("luisteraar voor pending-uitkomsten faalde")

    def _on_pending_event(self, event: PendingEvent) -> None:
        # Onder de lock van de PendingStore. CONFIRMED meldt _run_confirmed zelf (met resultaat).
        resolution = _STORE_RESOLUTIONS.get(event.change)
        if resolution is not None:
            action = event.action
            self._resolved(PendingResolved(action.id, action.tool, action.frontend, resolution))

    def _execute(self, spec: ToolSpec, args: ToolArgs) -> dict[str, Any]:
        try:
            return {"ok": True, "result": spec.handler(self.ctx, args)}
        except ToolboxError as exc:
            return {"ok": False, "error": exc.code, "message": str(exc)}
        except Exception as exc:
            log.exception("tool %s faalde", spec.name)
            return {"ok": False, "error": "internal_error", "message": type(exc).__name__}

    def _finish(  # noqa: PLR0913 één auditregel: alle velden verplicht
        self,
        t0: float,
        frontend: str,
        tool: str,
        risk: Risk | None,
        args: dict[str, Any],
        decision: str,
        result: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        self.audit.write(
            frontend=frontend,
            tool=tool,
            risk=str(risk) if risk else None,
            args=args,
            decision=decision,
            ok=result.get("ok", False),
            error=result.get("error"),
            result=result.get("result") if result.get("ok") else result.get("message"),
            duration_ms=round((time.monotonic() - t0) * 1000),
            **extra,
        )
        return result
