"""Policy: allowlist, scope-check en bevestiging.

Beslisregels (in deze volgorde):
1. Tool niet in de allowlist (als die gezet is)      -> DENY
2. CONTROL (emergency_stop, confirm_action)           -> ALLOW
3. scope_check zegt buiten scope                      -> DENY  (voor elke risicoklasse)
4. TRANSMIT binnen scope                              -> CONFIRM (pending_id, eenmalig, TTL)
5. READ / CAPTURE binnen scope                        -> ALLOW
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pitoolbox.core.scope import ScopeStore
from pitoolbox.core.tools.base import Risk, ToolArgs, ToolSpec


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""


class Policy:
    def __init__(self, scope_store: ScopeStore, allowlist: list[str] | None = None) -> None:
        self._scope_store = scope_store
        self._allowlist = set(allowlist or [])

    def decide(self, spec: ToolSpec, args: ToolArgs) -> Decision:
        if self._allowlist and spec.name not in self._allowlist:
            return Decision(Verdict.DENY, f"tool {spec.name} staat niet in de allowlist")
        if spec.risk is Risk.CONTROL:
            return Decision(Verdict.ALLOW)

        if spec.scope_check is not None:
            check = spec.scope_check(args, self._scope_store.get())
            if not check.in_scope:
                return Decision(Verdict.DENY, check.reason or "buiten scope.yaml")
        elif spec.risk is not Risk.READ:
            # Vangnet: ToolSpec dwingt dit al af, maar fail closed.
            return Decision(Verdict.DENY, "geen scope-check gedefinieerd")

        if spec.risk is Risk.TRANSMIT:
            return Decision(Verdict.CONFIRM, "transmit binnen scope vereist bevestiging")
        return Decision(Verdict.ALLOW)


@dataclass
class PendingAction:
    id: str
    tool: str
    args: ToolArgs
    frontend: str
    summary: str
    created: float
    expires: float
    extra: dict[str, Any] = field(default_factory=dict)


class PendingStore:
    """Openstaande transmit-acties. Eenmalig te gebruiken, gebonden aan de frontend."""

    def __init__(self, ttl_s: float = 60.0, clock: Any = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, PendingAction] = {}

    @property
    def ttl_s(self) -> float:
        return self._ttl

    def create(self, tool: str, args: ToolArgs, frontend: str, summary: str) -> PendingAction:
        now = self._clock()
        action = PendingAction(
            id=secrets.token_urlsafe(9),
            tool=tool,
            args=args,
            frontend=frontend,
            summary=summary,
            created=now,
            expires=now + self._ttl,
        )
        with self._lock:
            self._purge(now)
            self._items[action.id] = action
        return action

    def take(self, pending_id: str, frontend: str) -> PendingAction | None:
        """Haalt de actie op en verwijdert hem. None bij onbekend/verlopen/andere frontend."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            action = self._items.get(pending_id)
            if action is None or action.frontend != frontend:
                return None
            del self._items[pending_id]
            return action

    def list(self) -> list[PendingAction]:
        with self._lock:
            self._purge(self._clock())
            return list(self._items.values())

    def clear(self) -> int:
        with self._lock:
            n = len(self._items)
            self._items.clear()
            return n

    def _purge(self, now: float) -> None:
        for k in [k for k, v in self._items.items() if v.expires <= now]:
            del self._items[k]
