"""Policy: allowlist, scope-check en bevestiging.

Beslisregels (in deze volgorde):
1. Tool niet in de allowlist (als die gezet is)      -> DENY
2. CONTROL (emergency_stop, confirm_action)           -> ALLOW
3. scope_check zegt buiten scope                      -> DENY  (voor elke risicoklasse)
4. TRANSMIT binnen scope                              -> CONFIRM (pending_id, eenmalig, TTL)
5. READ / CAPTURE binnen scope                        -> ALLOW

Bevestigen gaat via `PendingStore` (eenmalig, TTL, gebonden aan de frontend, of aan de
fysieke bevestiger zolang de Flipper-app verbonden is: zie daar).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pitoolbox.core.scope import ScopeStore
from pitoolbox.core.tools.base import Risk, ToolArgs, ToolSpec

log = logging.getLogger(__name__)


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

    def allows_tool(self, name: str) -> bool:
        """Staat de tool in de allowlist (of is er geen allowlist)? Zonder scope-check."""
        return not self._allowlist or name in self._allowlist

    def decide(self, spec: ToolSpec, args: ToolArgs) -> Decision:
        if not self.allows_tool(spec.name):
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


class ClaimRefusal(StrEnum):
    """Waarom `PendingStore.claim` niets teruggaf."""

    # Onbekend, verlopen, al gebruikt of van een andere frontend. Bewust één waarde: een
    # andere frontend leert zo niet of een pending_id bestaat.
    UNKNOWN = "unknown"
    # De Flipper-app is verbonden: alleen OK op de Flipper bevestigt (de pending blijft staan).
    PHYSICAL_REQUIRED = "physical_required"
    # De lease van de fysieke bevestiger is niet (meer) de actieve, bijv. net losgekoppeld.
    STALE_CONFIRMER = "stale_confirmer"


@dataclass(frozen=True)
class Claim:
    action: PendingAction | None
    refusal: ClaimRefusal | None = None


class PendingChange(StrEnum):
    CREATED = "created"
    CONFIRMED = "confirmed"  # opgehaald om uit te voeren
    REJECTED = "rejected"  # door een mens afgewezen
    CLEARED = "cleared"  # emergency_stop / afsluiten
    EXPIRED = "expired"  # TTL verstreken (lui opgemerkt bij de volgende toegang)


@dataclass(frozen=True)
class PendingEvent:
    change: PendingChange
    action: PendingAction


PendingListener = Callable[[PendingEvent], None]


class PhysicalConfirmer:
    """Lease van de fysieke bevestiger (de Flipper-bridge), uitgegeven door `PendingStore`.

    Alleen wie dit object heeft, kan ermee bevestigen of afwijzen, en het loskoppelen. Een
    frontend-id als string is daarvoor bewust niet genoeg: elke frontend kiest zijn eigen id.
    """

    __slots__ = ("frontend",)

    def __init__(self, frontend: str) -> None:
        self.frontend = frontend

    def __repr__(self) -> str:
        return f"PhysicalConfirmer({self.frontend!r})"


class PendingStore:
    """Openstaande transmit-acties. Eenmalig te gebruiken, gebonden aan de frontend.

    **Fysieke bevestiger (besluit "Flipper verplicht").** Zolang er een lease is
    (`attach_physical_confirmer`, alleen door de Flipper-bridge bij een verbonden app), kan
    geen enkele frontend een pending nog zelf bevestigen: `claim` geeft dan
    `PHYSICAL_REQUIRED` en laat de pending staan. Alleen `claim(..., physical=lease)` met de
    actieve lease haalt hem op, voor elke frontend die hem maakte. Afwijzen (`discard`) mag de
    eigen frontend altijd: dat zendt niets. TTL en eenmalig gebruik gelden altijd.

    Racevrij: koppelen, loskoppelen en ophalen gebeuren onder dezelfde lock. Een bevestiging
    ziet dus óf de lease (en wordt geweigerd) óf geen lease (oude regel); er is geen moment
    waarop een chat een pending ophaalt terwijl de Flipper-app als verbonden geldt. Een
    lease die al losgekoppeld is, haalt niets meer op (`STALE_CONFIRMER`).

    Luisteraars (`subscribe`) krijgen elke wijziging **onder de lock**, dus in de juiste
    volgorde. Ze mogen daarom niet blokkeren en de store niet aanroepen (alleen iets in een
    eigen wachtrij zetten).
    """

    def __init__(self, ttl_s: float = 60.0, clock: Any = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, PendingAction] = {}
        self._physical: PhysicalConfirmer | None = None
        self._listeners: list[PendingListener] = []

    @property
    def ttl_s(self) -> float:
        return self._ttl

    # --- luisteraars -------------------------------------------------------------------

    def subscribe(self, listener: PendingListener) -> Callable[[], None]:
        """Registreer een luisteraar; geeft een functie terug die hem weer afmeldt."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, change: PendingChange, action: PendingAction) -> None:
        # Onder self._lock. Een falende luisteraar mag de store nooit breken.
        event = PendingEvent(change, action)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("PendingStore-luisteraar faalde")

    # --- fysieke bevestiger ------------------------------------------------------------

    def attach_physical_confirmer(self, frontend: str) -> PhysicalConfirmer:
        """Vanaf nu bevestigt alleen deze lease. Er kan er hoogstens één tegelijk zijn."""
        with self._lock:
            if self._physical is not None:
                raise RuntimeError(
                    f"er is al een fysieke bevestiger gekoppeld ({self._physical.frontend})"
                )
            lease = PhysicalConfirmer(frontend)
            self._physical = lease
            return lease

    def detach_physical_confirmer(self, lease: PhysicalConfirmer) -> bool:
        """Loskoppelen; alleen met de actieve lease. Daarna geldt weer de oude regel."""
        with self._lock:
            if self._physical is not lease:
                return False
            self._physical = None
            return True

    @property
    def physical_confirmer(self) -> str | None:
        """Frontend-id van de gekoppelde fysieke bevestiger, of None."""
        lease = self._physical
        return lease.frontend if lease is not None else None

    # --- acties --------------------------------------------------------------------------

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
            self._notify(PendingChange.CREATED, action)
        return action

    def claim(
        self, pending_id: str, frontend: str, *, physical: PhysicalConfirmer | None = None
    ) -> Claim:
        """Haal een actie op om hem uit te voeren (eenmalig).

        Zonder `physical`: alleen de frontend die hem maakte, en alleen als er geen fysieke
        bevestiger gekoppeld is. Met `physical`: alleen de actieve lease, voor elke frontend.
        Een geweigerde poging laat de pending altijd staan.
        """
        now = self._clock()
        with self._lock:
            self._purge(now)
            action = self._items.get(pending_id)
            if physical is not None:
                if physical is not self._physical:
                    return Claim(None, ClaimRefusal.STALE_CONFIRMER)
                if action is None:
                    return Claim(None, ClaimRefusal.UNKNOWN)
            else:
                if action is None or action.frontend != frontend:
                    return Claim(None, ClaimRefusal.UNKNOWN)
                if self._physical is not None:
                    return Claim(None, ClaimRefusal.PHYSICAL_REQUIRED)
            del self._items[pending_id]
            self._notify(PendingChange.CONFIRMED, action)
            return Claim(action)

    def take(self, pending_id: str, frontend: str) -> PendingAction | None:
        """Haalt de actie op en verwijdert hem. None bij onbekend/verlopen/andere frontend,
        of zolang er een fysieke bevestiger gekoppeld is (zie `claim`)."""
        return self.claim(pending_id, frontend).action

    def discard(
        self, pending_id: str, frontend: str, *, physical: PhysicalConfirmer | None = None
    ) -> PendingAction | None:
        """Afwijzen: verwijdert de actie zonder iets uit te voeren.

        Mag de frontend die hem maakte (ook met een gekoppelde Flipper: afwijzen zendt niets)
        of de actieve fysieke lease.
        """
        now = self._clock()
        with self._lock:
            self._purge(now)
            action = self._items.get(pending_id)
            if action is None:
                return None
            if physical is not None:
                if physical is not self._physical:
                    return None
            elif action.frontend != frontend:
                return None
            del self._items[pending_id]
            self._notify(PendingChange.REJECTED, action)
            return action

    def get(self, pending_id: str) -> PendingAction | None:
        with self._lock:
            self._purge(self._clock())
            return self._items.get(pending_id)

    def remaining_s(self, action: PendingAction) -> float:
        """Resterende TTL volgens de klok van deze store (kan negatief zijn)."""
        return float(action.expires - self._clock())

    def list(self) -> list[PendingAction]:
        with self._lock:
            self._purge(self._clock())
            return list(self._items.values())

    def clear(self) -> int:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
            for action in items:
                self._notify(PendingChange.CLEARED, action)
            return len(items)

    def _purge(self, now: float) -> None:
        for k in [k for k, v in self._items.items() if v.expires <= now]:
            self._notify(PendingChange.EXPIRED, self._items.pop(k))
