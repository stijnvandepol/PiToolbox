"""Vaste koppeling van snelle-actie-id naar tool-call (protocol §7). De FAP stuurt alleen
het id; tool en argumenten liggen hier vast."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pitoolbox.frontends.flipper_ui.protocol_messages import Action
from pitoolbox.frontends.flipper_ui.protocol_spec import (
    ActionKind,
    ErrorCode,
    ProtocolError,
    QuickAction,
)

# --- vaste koppeling id -> tool-call ----------------------------------------------------------


@dataclass(frozen=True)
class QuickActionPlan:
    """Wat de bridge voor een snelle actie via de ToolRunner aanroept (frontend `flipper-ui`).

    `per_active_capture`: roep `tool` aan voor elke capture uit `status.captures`, met
    `{"capture_id": <id>}` (de ids komen van de Pi zelf, nooit van de FAP).
    """

    label: str
    tool: str
    args: Mapping[str, Any]
    per_active_capture: bool = False


def _plan(label: str, tool: str, per_active_capture: bool = False, **args: Any) -> QuickActionPlan:
    return QuickActionPlan(label, tool, MappingProxyType(dict(args)), per_active_capture)


QUICK_ACTIONS: Mapping[QuickAction, QuickActionPlan] = MappingProxyType(
    {
        QuickAction.STATUS: _plan("Status", "status"),
        QuickAction.WIFI_SCAN: _plan("Wifi-scan", "wifi_scan", method="iw", band="abg"),
        QuickAction.STOP_CAPTURES: _plan("Stop captures", "capture_stop", per_active_capture=True),
        QuickAction.EMERGENCY_STOP: _plan("Noodstop", "emergency_stop"),
    }
)


def quick_action_plan(action: Action) -> QuickActionPlan:
    """Plan voor een ACTION met `kind=QUICK`; onbekend id -> ProtocolError(UNKNOWN_ACTION)."""
    if action.kind is not ActionKind.QUICK:
        raise ValueError("geen snelle actie")
    try:
        return QUICK_ACTIONS[QuickAction(action.action_id)]
    except ValueError:
        raise ProtocolError(
            f"onbekende snelle actie {action.action_id}",
            code=ErrorCode.UNKNOWN_ACTION,
            seq=action.seq,
        ) from None
