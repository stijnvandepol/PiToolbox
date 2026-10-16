"""Systeemtools: `status` (alles in één call) en `emergency_stop`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from pitoolbox.core.tools.base import Risk, ToolArgs, ToolContext, ToolSpec

log = logging.getLogger(__name__)


class StatusArgs(ToolArgs):
    pass


class EmergencyStopArgs(ToolArgs):
    pass


def _section(fn: Callable[[], Any]) -> Any:
    """Voert één statusonderdeel uit; een fout wordt {"error": ...} i.p.v. de hele status."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - elk onderdeel moet los van de rest falen
        log.warning("status-onderdeel faalde: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _wifi_section(ctx: ToolContext) -> dict[str, Any]:
    try:
        mode: str = ctx.wifi.get_mode()
    except Exception:  # noqa: BLE001 - mode onbekend is geen fout voor de status
        mode = "unknown"
    return {"iface": ctx.settings.alfa_iface, "mode": mode}


def _scope_section(ctx: ToolContext) -> dict[str, Any]:
    scope = ctx.scope  # laadt (indien nodig) opnieuw; zet daarna pas scope_store.error
    return {
        "networks": len(scope.wifi),
        "ir_devices": len(scope.infrared),
        "error": ctx.scope_store.error,
    }


# Achtergrondwerk van de Flipper-bridge: geen operatie die de gebruiker of het model start, en
# het wijkt meteen voor elke tool-call (`FlipperDevice.poll_app_events`). Dus niet "bezet".
_BACKGROUND_OPS = frozenset({"poll_app_events"})


def _flipper_section(ctx: ToolContext) -> dict[str, Any]:
    status = dict(ctx.flipper.status())
    if status.get("operation") in _BACKGROUND_OPS:
        status["busy"] = False
        status["operation"] = None
    return status


def _status(ctx: ToolContext, args: StatusArgs) -> dict[str, Any]:
    return {
        "power": _section(ctx.system.throttled),
        "uplink": _section(ctx.system.uplink),
        "disk": _section(ctx.system.disk),
        "flipper": _section(lambda: _flipper_section(ctx)),
        "wifi": _section(lambda: _wifi_section(ctx)),
        "captures": _section(ctx.captures.list_active),
        "scope": _section(lambda: _scope_section(ctx)),
    }


def _emergency_stop(ctx: ToolContext, args: EmergencyStopArgs) -> dict[str, Any]:
    """Elke stap in een eigen try: een falende stap blokkeert de rest nooit."""
    result: dict[str, Any] = {
        "captures_stopped": [],
        "flipper_aborted": False,
        "pending_cleared": 0,
        "errors": {},
    }
    errors: dict[str, str] = result["errors"]

    try:
        result["captures_stopped"] = ctx.captures.stop_all()
    except Exception as exc:
        log.exception("emergency_stop: captures.stop_all faalde")
        errors["captures"] = f"{type(exc).__name__}: {exc}"

    try:
        ctx.flipper.abort()
        result["flipper_aborted"] = True
    except Exception as exc:
        log.exception("emergency_stop: flipper.abort faalde")
        errors["flipper"] = f"{type(exc).__name__}: {exc}"

    try:
        result["pending_cleared"] = ctx.pending.clear()
    except Exception as exc:
        log.exception("emergency_stop: pending.clear faalde")
        errors["pending"] = f"{type(exc).__name__}: {exc}"

    return result


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="status",
        description=(
            "Overzicht: voeding/throttling, uplink, schijfruimte, Flipper, Wi-Fi-modus, "
            "lopende captures en scope. Onderdelen falen los van elkaar."
        ),
        risk=Risk.READ,
        params=StatusArgs,
        handler=_status,
    ),
    ToolSpec(
        name="emergency_stop",
        description=(
            "Noodstop: stopt alle captures, breekt de Flipper-actie af en wist openstaande "
            "bevestigingen. Altijd toegestaan."
        ),
        risk=Risk.CONTROL,
        params=EmergencyStopArgs,
        handler=_emergency_stop,
    ),
]
