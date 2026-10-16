"""ToolRunner: het enige pad van een frontend naar een actie.

validate -> policy -> (bevestigen) -> uitvoeren -> audit. Elke frontend (MCP, later de
Flipper-bridge) roept `call()` aan; niemand roept handlers direct aan.

Resultaatvorm (altijd een dict):
    {"ok": true,  "result": {...}}
    {"ok": false, "error": "<code>", "message": "..."}
    {"ok": false, "error": "confirmation_required", "pending_id": "...", "summary": "...",
     "expires_in_s": 60}
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import Field, ValidationError

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.errors import ToolboxError
from pitoolbox.core.policy import Policy, Verdict
from pitoolbox.core.tools.base import Registry, Risk, ToolArgs, ToolContext, ToolSpec

log = logging.getLogger(__name__)

CONFIRM_TOOL = "confirm_action"


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
        pending = self.ctx.pending.take(args.pending_id, frontend)
        if pending is None:
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
        )

    def _execute(self, spec: ToolSpec, args: ToolArgs) -> dict[str, Any]:
        try:
            return {"ok": True, "result": spec.handler(self.ctx, args)}
        except ToolboxError as exc:
            return {"ok": False, "error": exc.code, "message": str(exc)}
        except Exception as exc:
            log.exception("tool %s faalde", spec.name)
            return {"ok": False, "error": "internal_error", "message": type(exc).__name__}

    def _finish(
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
