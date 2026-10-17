"""ToolRunner: het enige pad van een frontend naar een actie.

validate -> policy -> (bevestigen) -> uitvoeren -> audit. Elke frontend (MCP, later de
Flipper-bridge) roept `call()` aan; niemand roept handlers direct aan.

Resultaatvorm (altijd een dict):
    {"ok": true,  "result": {...}}
    {"ok": false, "error": "<code>", "message": "..."}
    {"ok": false, "error": "confirmation_required", "pending_id": "...", "summary": "...",
     "expires_in_s": 60}

Bevestigen: `confirm_action` met de pending_id, vanuit dezelfde frontend. Zolang de
Flipper-app verbonden is (fysieke bevestiger gekoppeld, zie `PendingStore`), weigert dat voor
elke frontend met `confirm_on_flipper` en bevestigt alleen de bridge via `confirm_physical`.

Uitkomst van een pending (`subscribe_resolutions`): elke frontend kan horen hoe een pending
afliep, ook als een ander hem afhandelde (OK/Back op de Flipper, een noodstop, de TTL). Zo
hoort bijv. een `AgentSession` van `pitoolbox-agent` het resultaat van een OK op de Flipper.
`CONFIRMED` (met de ruwe uitkomst) meldt de runner na het uitvoeren; `REJECTED`, `EXPIRED` en
`CLEARED` komen rechtstreeks van de `PendingStore` (onder zijn lock). Luisteraars mogen dus
niet blokkeren en de store of runner niet aanroepen: alleen iets in een eigen wachtrij zetten.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import Field, ValidationError

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.errors import ToolboxError
from pitoolbox.core.policy import (
    ClaimRefusal,
    PendingAction,
    PendingChange,
    PendingEvent,
    PhysicalConfirmer,
    Policy,
    Verdict,
)
from pitoolbox.core.tools.base import Registry, Risk, ToolArgs, ToolContext, ToolSpec

log = logging.getLogger(__name__)

CONFIRM_TOOL = "confirm_action"
# Foutcode zolang de Flipper-app verbonden is ("Flipper verplicht"): de pending blijft staan.
CONFIRM_ON_FLIPPER = "confirm_on_flipper"
CONFIRM_ON_FLIPPER_MESSAGE = (
    "Bevestig op de Flipper: zolang de PiToolbox-app op de Flipper verbonden is, kan een "
    "transmit alleen met OK op de Flipper bevestigd worden (Back = nee). De actie staat nog "
    "open tot de TTL verloopt."
)
# Waarde van `confirmed_on` in de audit bij een bevestiging met de fysieke lease.
PHYSICAL_SOURCE = "flipper"


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


def _physical_refusal(refusal: ClaimRefusal | None) -> dict[str, Any]:
    if refusal is ClaimRefusal.STALE_CONFIRMER:
        message = "de Flipper-app is niet (meer) als bevestiger gekoppeld"
    else:
        message = "pending onbekend, verlopen of al afgehandeld"
    return {"ok": False, "error": "unknown_pending", "message": message}


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


class ToolRunner:
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

    def call(self, frontend: str, name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
        t0 = time.monotonic()
        raw_args = raw_args or {}
        spec = self.registry.get(name)
        if spec is None:
            return self._finish(
                t0,
                frontend,
                name,
                None,
                raw_args,
                "deny",
                {"ok": False, "error": "unknown_tool", "message": f"onbekende tool: {name}"},
            )

        try:
            args = spec.params.model_validate(raw_args)
        except ValidationError as exc:
            return self._finish(
                t0,
                frontend,
                name,
                spec.risk,
                raw_args,
                "invalid",
                {
                    "ok": False,
                    "error": "invalid_arguments",
                    "message": "argumenten ongeldig",
                    "details": exc.errors(include_url=False, include_context=False),
                },
            )

        if name == CONFIRM_TOOL and isinstance(args, ConfirmArgs):
            return self._confirm(t0, frontend, args)

        decision = self.policy.decide(spec, args)
        dumped = args.model_dump(mode="json")
        if decision.verdict is Verdict.DENY:
            return self._finish(
                t0,
                frontend,
                name,
                spec.risk,
                dumped,
                "deny",
                {"ok": False, "error": "denied", "message": decision.reason},
            )
        if decision.verdict is Verdict.CONFIRM:
            summary = spec.summary(args, self.ctx.scope) if spec.summary else f"{name}({dumped})"
            pending = self.ctx.pending.create(name, args, frontend, summary)
            return self._finish(
                t0,
                frontend,
                name,
                spec.risk,
                dumped,
                "confirm",
                {
                    "ok": False,
                    "error": "confirmation_required",
                    "pending_id": pending.id,
                    "summary": summary,
                    "expires_in_s": int(self.ctx.pending.ttl_s),
                    "message": (
                        "Vraag de gebruiker expliciet om bevestiging en roep daarna "
                        f"{CONFIRM_TOOL} aan met deze pending_id."
                    ),
                },
                pending_id=pending.id,
            )
        return self._finish(
            t0, frontend, name, spec.risk, dumped, "allow", self._execute(spec, args)
        )

    def _confirm(self, t0: float, frontend: str, args: ConfirmArgs) -> dict[str, Any]:
        claim = self.ctx.pending.claim(args.pending_id, frontend)
        if claim.action is None:
            if claim.refusal is ClaimRefusal.PHYSICAL_REQUIRED:
                # De pending blijft staan: de mens kan nog OK drukken op de Flipper.
                return self._finish(
                    t0,
                    frontend,
                    CONFIRM_TOOL,
                    Risk.CONTROL,
                    args.model_dump(),
                    "deny",
                    {
                        "ok": False,
                        "error": CONFIRM_ON_FLIPPER,
                        "message": CONFIRM_ON_FLIPPER_MESSAGE,
                    },
                    pending_id=args.pending_id,
                )
            return self._finish(
                t0,
                frontend,
                CONFIRM_TOOL,
                Risk.CONTROL,
                args.model_dump(),
                "deny",
                {
                    "ok": False,
                    "error": "unknown_pending",
                    "message": "pending_id onbekend, verlopen of van een andere frontend",
                },
            )
        return self._run_confirmed(t0, frontend, claim.action)

    def _run_confirmed(
        self, t0: float, frontend: str, pending: PendingAction, **extra: Any
    ) -> dict[str, Any]:
        result = self._run_confirmed_inner(t0, frontend, pending, **extra)
        confirmed_on = extra.get("confirmed_on")
        self._resolved(
            PendingResolved(
                pending.id,
                pending.tool,
                pending.frontend,
                Resolution.CONFIRMED,
                result,
                confirmed_on if isinstance(confirmed_on, str) else None,
            )
        )
        return result

    def _run_confirmed_inner(
        self, t0: float, frontend: str, pending: PendingAction, **extra: Any
    ) -> dict[str, Any]:
        spec = self.registry.get(pending.tool)
        if spec is None:  # registry is onveranderlijk na start; puur defensief
            raise RuntimeError(f"pending tool {pending.tool} niet meer geregistreerd")
        # Scope kan sinds het voorstel gewijzigd zijn: opnieuw beslissen.
        decision = self.policy.decide(spec, pending.args)
        dumped = pending.args.model_dump(mode="json")
        if decision.verdict is Verdict.DENY:
            return self._finish(
                t0,
                frontend,
                spec.name,
                spec.risk,
                dumped,
                "deny",
                {"ok": False, "error": "denied", "message": decision.reason},
                pending_id=pending.id,
                confirmed=True,
                **extra,
            )
        return self._finish(
            t0,
            frontend,
            spec.name,
            spec.risk,
            dumped,
            "confirmed",
            self._execute(spec, pending.args),
            pending_id=pending.id,
            confirmed=True,
            **extra,
        )

    # --- fysieke bevestiger (alleen voor de Flipper-bridge) -------------------------------

    def attach_physical_confirmer(self, frontend: str) -> PhysicalConfirmer:
        """Koppel de fysieke bevestiger (de Flipper-app is verbonden). Geaudit.

        Vanaf nu weigert `confirm_action` uit elke frontend (`confirm_on_flipper`); alleen
        `confirm_physical` met deze lease bevestigt nog.
        """
        lease = self.ctx.pending.attach_physical_confirmer(frontend)
        self._audit_confirmer(lease.frontend, attached=True, reason="app verbonden")
        return lease

    def detach_physical_confirmer(self, lease: PhysicalConfirmer, reason: str) -> None:
        """Loskoppelen (app weg, link weg, afsluiten). Geaudit; idempotent."""
        if self.ctx.pending.detach_physical_confirmer(lease):
            self._audit_confirmer(lease.frontend, attached=False, reason=reason)

    def confirm_physical(self, lease: PhysicalConfirmer, pending_id: str) -> dict[str, Any]:
        """Voer een pending uit na OK op de Flipper. Alleen de bridge roept dit aan, en alleen
        op een geldig CONFIRM_ANSWER (handle van deze bridge, deze app-instance, eenmalig, TTL).

        Werkt voor een pending van elke frontend; TTL, eenmalig gebruik en de nieuwe
        scope-check gelden zoals altijd.
        """
        t0 = time.monotonic()
        claim = self.ctx.pending.claim(pending_id, lease.frontend, physical=lease)
        if claim.action is None:
            return self._finish(
                t0,
                lease.frontend,
                CONFIRM_TOOL,
                Risk.CONTROL,
                {"pending_id": pending_id},
                "deny",
                _physical_refusal(claim.refusal),
                confirmed_on=PHYSICAL_SOURCE,
            )
        return self._run_confirmed(
            t0,
            lease.frontend,
            claim.action,
            confirmed_on=PHYSICAL_SOURCE,
            created_by=claim.action.frontend,
        )

    def reject_physical(self, lease: PhysicalConfirmer, pending_id: str) -> dict[str, Any]:
        """Back op de Flipper: de pending vervalt zonder iets uit te voeren. Geaudit."""
        t0 = time.monotonic()
        action = self.ctx.pending.discard(pending_id, lease.frontend, physical=lease)
        if action is None:
            return self._finish(
                t0,
                lease.frontend,
                CONFIRM_TOOL,
                Risk.CONTROL,
                {"pending_id": pending_id},
                "deny",
                _physical_refusal(ClaimRefusal.UNKNOWN),
                confirmed_on=PHYSICAL_SOURCE,
            )
        spec = self.registry.get(action.tool)
        return self._finish(
            t0,
            lease.frontend,
            action.tool,
            spec.risk if spec else None,
            action.args.model_dump(mode="json"),
            "rejected",
            {"ok": True, "result": {"rejected": True, "tool": action.tool}},
            pending_id=action.id,
            confirmed_on=PHYSICAL_SOURCE,
            created_by=action.frontend,
        )

    def _audit_confirmer(self, frontend: str, *, attached: bool, reason: str) -> None:
        try:
            self.audit.write(
                event="physical_confirmer", frontend=frontend, attached=attached, reason=reason
            )
        except OSError:
            log.exception("audit-regel physical_confirmer kon niet worden geschreven")
        log.info("fysieke bevestiger %s: %s (%s)", frontend, "aan" if attached else "uit", reason)

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
