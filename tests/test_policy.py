"""Tests voor Policy.decide en PendingStore."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from pydantic import Field

from pitoolbox.core.policy import (
    ClaimRefusal,
    Decision,
    PendingChange,
    PendingEvent,
    PendingStore,
    PhysicalConfirmer,
    Policy,
    Verdict,
)
from pitoolbox.core.scope import Scope, ScopeStore
from pitoolbox.core.tools.base import Risk, ScopeCheck, ToolArgs, ToolSpec

IN_SCOPE = "AA:BB:CC:DD:EE:01"
OUT_OF_SCOPE = "11:22:33:44:55:66"


class TargetArgs(ToolArgs):
    bssid: str = Field(default=IN_SCOPE)


def _noop(ctx: Any, args: Any) -> dict[str, Any]:
    return {}


def _bssid_check(args: TargetArgs, scope: Scope) -> ScopeCheck:
    if args.bssid in scope.bssids():
        return ScopeCheck(True)
    return ScopeCheck(False, f"{args.bssid} staat niet in scope.yaml")


def _spec(name: str, risk: Risk, scope_check: Any = None) -> ToolSpec:
    """CAPTURE/TRANSMIT krijgen standaard de bssid-check; READ/CONTROL geen."""
    if scope_check is None and risk in (Risk.CAPTURE, Risk.TRANSMIT):
        scope_check = _bssid_check
    return ToolSpec(
        name=name,
        description=name,
        risk=risk,
        params=TargetArgs,
        handler=_noop,
        scope_check=scope_check,
    )


@pytest.fixture
def scope() -> Scope:
    return Scope.model_validate({"wifi": [{"bssid": IN_SCOPE, "channel": 6}]})


@pytest.fixture
def policy(scope: Scope) -> Policy:
    return Policy(ScopeStore.fixed(scope))


# --- Policy.decide ---


def test_control_is_always_allowed(policy: Policy) -> None:
    spec = _spec("emergency_stop", Risk.CONTROL)
    assert policy.decide(spec, TargetArgs()) == Decision(Verdict.ALLOW)


def test_control_allowed_even_with_empty_scope() -> None:
    policy = Policy(ScopeStore.fixed(Scope()))
    assert policy.decide(_spec("c", Risk.CONTROL), TargetArgs()).verdict is Verdict.ALLOW


def test_control_ignores_scope_check() -> None:
    """Een CONTROL-tool met een (per ongeluk) falende scope_check blijft toegestaan."""
    spec = _spec("c", Risk.CONTROL, scope_check=lambda a, s: ScopeCheck(False, "nee"))
    policy = Policy(ScopeStore.fixed(Scope()))
    assert policy.decide(spec, TargetArgs()).verdict is Verdict.ALLOW


def test_read_without_scope_check_is_allowed() -> None:
    policy = Policy(ScopeStore.fixed(Scope()))
    assert policy.decide(_spec("r", Risk.READ), TargetArgs()).verdict is Verdict.ALLOW


def test_read_in_scope_allowed_out_of_scope_denied(policy: Policy) -> None:
    spec = _spec("r", Risk.READ, scope_check=_bssid_check)
    assert policy.decide(spec, TargetArgs(bssid=IN_SCOPE)).verdict is Verdict.ALLOW
    denied = policy.decide(spec, TargetArgs(bssid=OUT_OF_SCOPE))
    assert denied.verdict is Verdict.DENY
    assert OUT_OF_SCOPE in denied.reason


def test_capture_in_scope_allow_out_of_scope_deny(policy: Policy) -> None:
    spec = _spec("cap", Risk.CAPTURE)
    assert policy.decide(spec, TargetArgs(bssid=IN_SCOPE)).verdict is Verdict.ALLOW
    assert policy.decide(spec, TargetArgs(bssid=OUT_OF_SCOPE)).verdict is Verdict.DENY


def test_transmit_in_scope_requires_confirmation(policy: Policy) -> None:
    decision = policy.decide(_spec("tx", Risk.TRANSMIT), TargetArgs(bssid=IN_SCOPE))
    assert decision.verdict is Verdict.CONFIRM
    assert decision.reason


def test_transmit_out_of_scope_is_denied_not_confirmed(policy: Policy) -> None:
    decision = policy.decide(_spec("tx", Risk.TRANSMIT), TargetArgs(bssid=OUT_OF_SCOPE))
    assert decision.verdict is Verdict.DENY


def test_empty_reason_falls_back_to_default(policy: Policy) -> None:
    spec = _spec("cap", Risk.CAPTURE, scope_check=lambda a, s: ScopeCheck(False))
    decision = policy.decide(spec, TargetArgs())
    assert decision.verdict is Verdict.DENY
    assert decision.reason == "buiten scope.yaml"


def test_empty_scope_denies_capture_and_transmit() -> None:
    policy = Policy(ScopeStore.fixed(Scope()))
    assert policy.decide(_spec("cap", Risk.CAPTURE), TargetArgs()).verdict is Verdict.DENY
    assert policy.decide(_spec("tx", Risk.TRANSMIT), TargetArgs()).verdict is Verdict.DENY


def test_allowlist_denies_unlisted_tool(scope: Scope) -> None:
    policy = Policy(ScopeStore.fixed(scope), allowlist=["wifi_scan"])
    other = policy.decide(_spec("other", Risk.READ), TargetArgs())
    assert other.verdict is Verdict.DENY
    assert "allowlist" in other.reason
    assert policy.decide(_spec("wifi_scan", Risk.READ), TargetArgs()).verdict is Verdict.ALLOW


def test_allowlist_also_blocks_control_tools(scope: Scope) -> None:
    policy = Policy(ScopeStore.fixed(scope), allowlist=["wifi_scan"])
    assert (
        policy.decide(_spec("emergency_stop", Risk.CONTROL), TargetArgs()).verdict is Verdict.DENY
    )


def test_empty_allowlist_means_everything_allowed(scope: Scope) -> None:
    policy = Policy(ScopeStore.fixed(scope), allowlist=[])
    assert policy.decide(_spec("anything", Risk.READ), TargetArgs()).verdict is Verdict.ALLOW


def test_allowlisted_transmit_still_needs_scope_and_confirm(scope: Scope) -> None:
    policy = Policy(ScopeStore.fixed(scope), allowlist=["tx"])
    spec = _spec("tx", Risk.TRANSMIT)
    assert policy.decide(spec, TargetArgs(bssid=IN_SCOPE)).verdict is Verdict.CONFIRM
    assert policy.decide(spec, TargetArgs(bssid=OUT_OF_SCOPE)).verdict is Verdict.DENY


def test_fail_closed_when_non_read_tool_has_no_scope_check() -> None:
    """ToolSpec dwingt een scope_check af; Policy vangt het ook zelf op (vangnet)."""
    spec = ToolSpec(name="x", description="x", risk=Risk.CONTROL, params=TargetArgs, handler=_noop)
    object.__setattr__(spec, "risk", Risk.CAPTURE)  # dataclass is frozen: omzeil bewust
    policy = Policy(ScopeStore.fixed(Scope()))
    decision = policy.decide(spec, TargetArgs())
    assert decision.verdict is Verdict.DENY
    assert "scope-check" in decision.reason


def test_toolspec_requires_scope_check_for_capture_and_transmit() -> None:
    for risk in (Risk.CAPTURE, Risk.TRANSMIT):
        with pytest.raises(ValueError):
            ToolSpec(name="x", description="x", risk=risk, params=TargetArgs, handler=_noop)


def test_scope_is_reread_on_every_decision() -> None:
    """Policy houdt geen eigen kopie: een gewijzigde scope geldt direct."""

    class SwappableStore(ScopeStore):
        current = Scope()

        def get(self) -> Scope:
            return self.current

    store = SwappableStore(None)
    policy = Policy(store)
    spec = _spec("cap", Risk.CAPTURE)
    assert policy.decide(spec, TargetArgs()).verdict is Verdict.DENY
    store.current = Scope.model_validate({"wifi": [{"bssid": IN_SCOPE, "channel": 1}]})
    assert policy.decide(spec, TargetArgs()).verdict is Verdict.ALLOW


# --- PendingStore ---


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _store(ttl: float = 60.0) -> tuple[PendingStore, FakeClock]:
    clock = FakeClock()
    return PendingStore(ttl_s=ttl, clock=clock), clock


def test_pending_take_is_single_use() -> None:
    store, _ = _store()
    pending = store.create("tx", TargetArgs(), "mcp", "samenvatting")
    taken = store.take(pending.id, "mcp")
    assert taken is not None
    assert taken.tool == "tx" and taken.summary == "samenvatting"
    assert isinstance(taken.args, TargetArgs)
    assert store.take(pending.id, "mcp") is None
    assert store.list() == []


def test_pending_ids_are_unique_and_unguessable_length() -> None:
    store, _ = _store()
    ids = {store.create("tx", TargetArgs(), "mcp", "s").id for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) >= 12 for i in ids)


def test_pending_expires_after_ttl() -> None:
    store, clock = _store(ttl=60.0)
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    clock.now += 59.9
    assert [p.id for p in store.list()] == [pending.id]
    clock.now += 0.2  # nu >= expires
    assert store.take(pending.id, "mcp") is None
    assert store.list() == []


def test_pending_expiry_boundary_is_exclusive() -> None:
    store, clock = _store(ttl=60.0)
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    clock.now += 60.0  # expires <= now -> verlopen
    assert store.take(pending.id, "mcp") is None


def test_pending_expired_items_are_purged_on_create() -> None:
    store, clock = _store(ttl=10.0)
    store.create("tx", TargetArgs(), "mcp", "oud")
    clock.now += 11.0
    fresh = store.create("tx", TargetArgs(), "mcp", "nieuw")
    assert [p.id for p in store.list()] == [fresh.id]


def test_pending_is_bound_to_frontend() -> None:
    store, _ = _store()
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    assert store.take(pending.id, "flipper") is None
    # Een mislukte poging van een andere frontend verbruikt de actie niet.
    assert store.take(pending.id, "mcp") is not None


def test_pending_unknown_id_returns_none() -> None:
    store, _ = _store()
    assert store.take("bestaat-niet", "mcp") is None


def test_pending_clear() -> None:
    store, _ = _store()
    a = store.create("tx", TargetArgs(), "mcp", "a")
    store.create("tx", TargetArgs(), "mcp", "b")
    assert store.clear() == 2
    assert store.list() == []
    assert store.take(a.id, "mcp") is None
    assert store.clear() == 0


def test_pending_ttl_property() -> None:
    assert PendingStore(ttl_s=42).ttl_s == 42
    assert PendingStore().ttl_s == 60.0


def test_allows_tool_follows_allowlist(fixed_scope: Scope) -> None:
    assert Policy(ScopeStore.fixed(fixed_scope)).allows_tool("wat_dan_ook")
    policy = Policy(ScopeStore.fixed(fixed_scope), allowlist=["status"])
    assert policy.allows_tool("status")
    assert not policy.allows_tool("ir_send")


# --- fysieke bevestiger ("Flipper verplicht") ---------------------------------------------


def test_physical_confirmer_blocks_chat_confirm_without_consuming() -> None:
    store, _ = _store()
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    lease = store.attach_physical_confirmer("flipper-ui")
    assert store.physical_confirmer == "flipper-ui"
    claim = store.claim(pending.id, "mcp")
    assert claim.action is None and claim.refusal is ClaimRefusal.PHYSICAL_REQUIRED
    assert store.take(pending.id, "mcp") is None
    # Ook de bridge-frontend zelf komt er zonder lease niet door.
    assert store.claim(pending.id, "flipper-ui").refusal is ClaimRefusal.UNKNOWN
    assert [p.id for p in store.list()] == [pending.id]
    # Met de lease: een pending van een andere frontend, één keer.
    taken = store.claim(pending.id, "flipper-ui", physical=lease)
    assert taken.action is not None and taken.action.frontend == "mcp"
    assert store.claim(pending.id, "flipper-ui", physical=lease).refusal is ClaimRefusal.UNKNOWN


def test_other_frontend_still_sees_unknown_while_attached() -> None:
    store, _ = _store()
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    store.attach_physical_confirmer("flipper-ui")
    # Geen lek: een vreemde frontend hoort niet dat de pending bestaat.
    assert store.claim(pending.id, "agent-cli").refusal is ClaimRefusal.UNKNOWN


def test_detached_lease_is_stale_and_old_rule_returns() -> None:
    store, _ = _store()
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    lease = store.attach_physical_confirmer("flipper-ui")
    assert store.detach_physical_confirmer(lease) is True
    assert store.detach_physical_confirmer(lease) is False
    assert store.physical_confirmer is None
    stale = store.claim(pending.id, "flipper-ui", physical=lease)
    assert stale.action is None and stale.refusal is ClaimRefusal.STALE_CONFIRMER
    assert store.discard(pending.id, "flipper-ui", physical=lease) is None
    assert store.take(pending.id, "mcp") is not None


def test_only_one_physical_confirmer_and_only_holder_detaches() -> None:
    store, _ = _store()
    lease = store.attach_physical_confirmer("flipper-ui")
    with pytest.raises(RuntimeError):
        store.attach_physical_confirmer("iemand-anders")
    impostor = PhysicalConfirmer("flipper-ui")
    assert store.detach_physical_confirmer(impostor) is False
    assert store.physical_confirmer == "flipper-ui"
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    assert store.claim(pending.id, "x", physical=impostor).refusal is (ClaimRefusal.STALE_CONFIRMER)
    assert store.detach_physical_confirmer(lease) is True


def test_ttl_applies_to_physical_confirm() -> None:
    store, clock = _store(ttl=60.0)
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    lease = store.attach_physical_confirmer("flipper-ui")
    clock.now += 60.0
    assert store.claim(pending.id, "flipper-ui", physical=lease).action is None


def test_discard_by_creator_allowed_while_attached() -> None:
    store, _ = _store()
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    store.attach_physical_confirmer("flipper-ui")
    assert store.discard(pending.id, "agent-cli") is None  # niet van hem
    assert store.discard(pending.id, "mcp") is not None  # afwijzen zendt niets
    assert store.list() == []


def test_listeners_see_every_change_in_order() -> None:
    store, clock = _store(ttl=10.0)
    events: list[tuple[PendingChange, str]] = []
    unsubscribe = store.subscribe(lambda e: events.append((e.change, e.action.summary)))
    a = store.create("tx", TargetArgs(), "mcp", "a")
    b = store.create("tx", TargetArgs(), "mcp", "b")
    c = store.create("tx", TargetArgs(), "mcp", "c")
    store.take(a.id, "mcp")
    store.discard(b.id, "mcp")
    clock.now += 11
    store.list()
    store.create("tx", TargetArgs(), "mcp", "d")
    store.clear()
    unsubscribe()
    store.create("tx", TargetArgs(), "mcp", "e")
    assert events == [
        (PendingChange.CREATED, "a"),
        (PendingChange.CREATED, "b"),
        (PendingChange.CREATED, "c"),
        (PendingChange.CONFIRMED, "a"),
        (PendingChange.REJECTED, "b"),
        (PendingChange.EXPIRED, "c"),
        (PendingChange.CREATED, "d"),
        (PendingChange.CLEARED, "d"),
    ]
    assert c.id


def test_failing_listener_does_not_break_store() -> None:
    store, _ = _store()

    def boom(event: PendingEvent) -> None:
        raise RuntimeError("kapot")

    store.subscribe(boom)
    pending = store.create("tx", TargetArgs(), "mcp", "s")
    assert store.take(pending.id, "mcp") is not None


def test_race_chat_confirm_never_succeeds_while_attached() -> None:
    """Stresstest: koppelen/loskoppelen en chat-bevestigingen door elkaar.

    De luisteraar draait onder de store-lock, dus `physical_confirmer` is daar precies de
    toestand op het moment van ophalen. Een chat-bevestiging mag alleen slagen zonder lease.
    """
    store = PendingStore(ttl_s=60.0)
    seen: list[str | None] = []

    def listener(event: PendingEvent) -> None:
        if event.change is PendingChange.CONFIRMED:
            seen.append(store.physical_confirmer)

    store.subscribe(listener)
    ids = [store.create("tx", TargetArgs(), "mcp", str(i)).id for i in range(400)]
    stop = threading.Event()

    def toggler() -> None:
        while not stop.is_set():
            lease = store.attach_physical_confirmer("flipper-ui")
            store.detach_physical_confirmer(lease)

    thread = threading.Thread(target=toggler)
    thread.start()
    try:
        succeeded = sum(store.take(pid, "mcp") is not None for pid in ids for _ in range(3))
    finally:
        stop.set()
        thread.join()
    assert len(seen) == succeeded
    assert all(state is None for state in seen)
