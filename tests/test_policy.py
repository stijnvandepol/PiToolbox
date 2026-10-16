"""Tests voor Policy.decide en PendingStore."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import Field

from pitoolbox.core.policy import Decision, PendingStore, Policy, Verdict
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
