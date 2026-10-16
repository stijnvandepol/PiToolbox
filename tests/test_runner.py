"""Tests voor ToolRunner met nep-tools in alle risicoklassen."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.errors import Aborted, DeviceNotConnected
from pitoolbox.core.policy import PhysicalConfirmer, Policy
from pitoolbox.core.scope import Scope, ScopeStore
from pitoolbox.core.tools.base import (
    Registry,
    Risk,
    ScopeCheck,
    ToolArgs,
    ToolContext,
    ToolSpec,
)
from pitoolbox.core.tools.runner import (
    CONFIRM_ON_FLIPPER,
    CONFIRM_SPEC,
    CONFIRM_TOOL,
    PendingResolved,
    Resolution,
    ToolRunner,
)

IN_SCOPE = "AA:BB:CC:DD:EE:01"
OUT_OF_SCOPE = "11:22:33:44:55:66"


class EchoArgs(ToolArgs):
    text: str = Field(default="hoi", min_length=1, max_length=10)
    n: int = Field(default=1, ge=1, le=5)


class TargetArgs(ToolArgs):
    bssid: str = IN_SCOPE


class NoArgs(ToolArgs):
    pass


def _bssid_check(args: TargetArgs, scope: Scope) -> ScopeCheck:
    if args.bssid in scope.bssids():
        return ScopeCheck(True)
    return ScopeCheck(False, f"{args.bssid} staat niet in scope.yaml")


class Fixture:
    """Bundelt runner, audit en de lijst met handleraanroepen."""

    def __init__(self, tmp_path: Path, ctx: ToolContext, allowlist: list[str] | None = None):
        self.calls: list[tuple[str, Any]] = []
        self.ctx = ctx
        self.audit_path = tmp_path / "audit.jsonl"
        specs = [
            self._spec("read_tool", Risk.READ, EchoArgs),
            self._spec("capture_tool", Risk.CAPTURE, TargetArgs, _bssid_check),
            self._spec("transmit_tool", Risk.TRANSMIT, TargetArgs, _bssid_check),
            self._spec("control_tool", Risk.CONTROL, NoArgs),
            ToolSpec(
                name="aborting",
                description="d",
                risk=Risk.READ,
                params=NoArgs,
                handler=self._raise(Aborted("afgebroken door stop")),
            ),
            ToolSpec(
                name="disconnected",
                description="d",
                risk=Risk.READ,
                params=NoArgs,
                handler=self._raise(DeviceNotConnected("Flipper niet verbonden")),
            ),
            ToolSpec(
                name="crash",
                description="d",
                risk=Risk.READ,
                params=NoArgs,
                handler=self._raise(RuntimeError("geheim intern pad /etc/shadow")),
            ),
            ToolSpec(
                name="tx_disconnected",
                description="d",
                risk=Risk.TRANSMIT,
                params=TargetArgs,
                handler=self._raise(DeviceNotConnected("Flipper weg")),
                scope_check=_bssid_check,
            ),
        ]
        self.runner = ToolRunner(
            Registry([*specs, CONFIRM_SPEC]),
            ctx,
            Policy(ctx.scope_store, allowlist),
            AuditLog(self.audit_path),
        )

    def _spec(
        self, name: str, risk: Risk, params: type[ToolArgs], scope_check: Any = None
    ) -> ToolSpec:
        def handler(ctx: ToolContext, args: Any) -> dict[str, Any]:
            self.calls.append((name, args))
            return {"tool": name, "args": args.model_dump()}

        return ToolSpec(
            name=name,
            description=f"{name} beschrijving",
            risk=risk,
            params=params,
            handler=handler,
            scope_check=scope_check,
            summary=(lambda a, _s: f"{name} naar {a.bssid}") if risk is Risk.TRANSMIT else None,
        )

    @staticmethod
    def _raise(exc: Exception) -> Any:
        def handler(ctx: ToolContext, args: Any) -> dict[str, Any]:
            raise exc

        return handler

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [
            json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]


@pytest.fixture
def fx(tmp_path: Path, make_ctx: Any) -> Fixture:
    scope = Scope.model_validate({"wifi": [{"bssid": IN_SCOPE, "channel": 6}]})
    return Fixture(tmp_path, make_ctx(tmp_path, scope))


# --- basispaden ---


def test_specs_lists_registry(fx: Fixture) -> None:
    names = {s.name for s in fx.runner.specs()}
    assert {"read_tool", "capture_tool", "transmit_tool", "control_tool", CONFIRM_TOOL} <= names


def test_read_allowed_and_audited(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "read_tool", {"text": "yo", "n": 2})
    assert result == {"ok": True, "result": {"tool": "read_tool", "args": {"text": "yo", "n": 2}}}
    (rec,) = fx.audit()
    assert rec["decision"] == "allow" and rec["ok"] is True
    assert rec["tool"] == "read_tool" and rec["risk"] == "read" and rec["frontend"] == "mcp"
    assert rec["args"] == {"text": "yo", "n": 2}
    assert rec["error"] is None
    assert isinstance(rec["duration_ms"], int)


def test_defaults_applied_and_none_args_accepted(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "read_tool", None)
    assert result["ok"] is True
    assert result["result"]["args"] == {"text": "hoi", "n": 1}


def test_capture_in_scope_allowed(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "capture_tool", {"bssid": IN_SCOPE})
    assert result["ok"] is True
    (rec,) = fx.audit()
    assert rec["decision"] == "allow" and rec["risk"] == "capture"


def test_control_allowed(fx: Fixture) -> None:
    assert fx.runner.call("mcp", "control_tool", {})["ok"] is True
    (rec,) = fx.audit()
    assert rec["decision"] == "allow" and rec["risk"] == "control"


# --- fouten vóór uitvoering ---


def test_unknown_tool(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "bestaat_niet", {"x": 1})
    assert result["ok"] is False and result["error"] == "unknown_tool"
    assert fx.calls == []
    (rec,) = fx.audit()
    assert rec["decision"] == "deny" and rec["risk"] is None
    assert rec["tool"] == "bestaat_niet" and rec["error"] == "unknown_tool"


@pytest.mark.parametrize(
    "args",
    [
        {"extra": 1},  # onbekend veld
        {"text": ""},  # te kort
        {"text": "x" * 11},  # te lang
        {"n": 0},  # onder ge
        {"n": 6},  # boven le
        {"n": "veel"},  # verkeerd type
    ],
)
def test_invalid_arguments(fx: Fixture, args: dict[str, Any]) -> None:
    result = fx.runner.call("mcp", "read_tool", args)
    assert result["ok"] is False and result["error"] == "invalid_arguments"
    assert result["details"]
    assert fx.calls == []
    (rec,) = fx.audit()
    assert rec["decision"] == "invalid" and rec["risk"] == "read"
    assert rec["error"] == "invalid_arguments"


def test_extra_field_on_transmit_does_not_create_pending(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE, "force": True})
    assert result["error"] == "invalid_arguments"
    assert fx.ctx.pending.list() == []


def test_deny_out_of_scope(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "capture_tool", {"bssid": OUT_OF_SCOPE})
    assert result["ok"] is False and result["error"] == "denied"
    assert OUT_OF_SCOPE in result["message"]
    assert fx.calls == []
    (rec,) = fx.audit()
    assert rec["decision"] == "deny" and rec["ok"] is False and rec["error"] == "denied"


def test_transmit_out_of_scope_denied_without_pending(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "transmit_tool", {"bssid": OUT_OF_SCOPE})
    assert result["error"] == "denied"
    assert fx.ctx.pending.list() == []
    assert [r["decision"] for r in fx.audit()] == ["deny"]


def test_allowlist_denies(tmp_path: Path, make_ctx: Any, fixed_scope: Scope) -> None:
    fx = Fixture(tmp_path, make_ctx(tmp_path, fixed_scope), allowlist=["read_tool"])
    assert fx.runner.call("mcp", "read_tool", {})["ok"] is True
    denied = fx.runner.call("mcp", "control_tool", {})
    assert denied["error"] == "denied" and "allowlist" in denied["message"]
    assert [r["decision"] for r in fx.audit()] == ["allow", "deny"]


# --- confirm-flow ---


def test_confirm_flow_end_to_end(fx: Fixture) -> None:
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    assert first["ok"] is False and first["error"] == "confirmation_required"
    assert first["summary"] == f"transmit_tool naar {IN_SCOPE}"
    assert first["expires_in_s"] == 60
    assert first["pending_id"] and "confirm_action" in first["message"]
    assert fx.calls == []  # nog niets uitgevoerd

    confirmed = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert confirmed["ok"] is True
    assert confirmed["result"]["tool"] == "transmit_tool"
    assert [c[0] for c in fx.calls] == ["transmit_tool"]

    again = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert again["ok"] is False and again["error"] == "unknown_pending"
    assert len(fx.calls) == 1  # niet nogmaals uitgevoerd

    recs = fx.audit()
    assert [r["decision"] for r in recs] == ["confirm", "confirmed", "deny"]
    assert recs[0]["pending_id"] == first["pending_id"] and recs[0]["tool"] == "transmit_tool"
    assert recs[1]["pending_id"] == first["pending_id"] and recs[1]["confirmed"] is True
    assert recs[1]["tool"] == "transmit_tool" and recs[1]["risk"] == "transmit"
    assert recs[2]["tool"] == CONFIRM_TOOL and recs[2]["error"] == "unknown_pending"


def test_confirm_with_unknown_pending_id(fx: Fixture) -> None:
    result = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": "nietbestaand"})
    assert result["error"] == "unknown_pending"
    (rec,) = fx.audit()
    assert rec["decision"] == "deny" and rec["risk"] == "control"


@pytest.mark.parametrize("pending_id", ["abc", "bevat spatie", "x" * 65, "a;b;c;d"])
def test_confirm_rejects_malformed_id(fx: Fixture, pending_id: str) -> None:
    result = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": pending_id})
    assert result["error"] == "invalid_arguments"
    assert [r["decision"] for r in fx.audit()] == ["invalid"]


def test_confirm_from_other_frontend_fails(fx: Fixture) -> None:
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    stolen = fx.runner.call("flipper", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert stolen["error"] == "unknown_pending"
    assert fx.calls == []
    # De eigenaar-frontend kan nog gewoon bevestigen.
    ok = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert ok["ok"] is True
    assert [r["frontend"] for r in fx.audit()] == ["mcp", "flipper", "mcp"]


def test_confirm_expired(tmp_path: Path, make_ctx: Any, fixed_scope: Scope) -> None:
    now = [0.0]
    ctx = make_ctx(tmp_path, fixed_scope, ttl_s=30, clock=lambda: now[0])
    fx = Fixture(tmp_path, ctx)
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": "AA:BB:CC:DD:EE:01"})
    assert first["expires_in_s"] == 30
    now[0] = 31.0
    late = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert late["error"] == "unknown_pending"
    assert fx.calls == []


def test_confirm_rechecks_scope(tmp_path: Path, make_ctx: Any) -> None:
    scope_file = tmp_path / "scope.yaml"
    scope_file.write_text(f'wifi:\n  - bssid: "{IN_SCOPE}"\n    channel: 6\n', encoding="utf-8")
    ctx = make_ctx(tmp_path)
    ctx.scope_store = ScopeStore(scope_file)
    fx = Fixture(tmp_path, ctx)

    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    assert first["error"] == "confirmation_required"

    # Netwerk uit scope halen na het voorstel (mtime expliciet vooruit).
    scope_file.write_text("wifi: []\n", encoding="utf-8")
    st = scope_file.stat()
    os.utime(scope_file, (st.st_atime, st.st_mtime + 10))

    result = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert result["ok"] is False and result["error"] == "denied"
    assert fx.calls == []
    recs = fx.audit()
    assert [r["decision"] for r in recs] == ["confirm", "deny"]
    assert recs[1]["confirmed"] is True and recs[1]["tool"] == "transmit_tool"
    assert recs[1]["pending_id"] == first["pending_id"]
    # En de actie is verbruikt: opnieuw bevestigen kan niet.
    assert fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})["error"] == (
        "unknown_pending"
    )


def test_pending_cleared_cannot_be_confirmed(fx: Fixture) -> None:
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    assert fx.ctx.pending.clear() == 1
    assert fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})["error"] == (
        "unknown_pending"
    )
    assert fx.calls == []


def test_confirmed_handler_error_is_mapped(fx: Fixture) -> None:
    first = fx.runner.call("mcp", "tx_disconnected", {"bssid": IN_SCOPE})
    result = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert result == {"ok": False, "error": "device_not_connected", "message": "Flipper weg"}
    recs = fx.audit()
    assert recs[1]["decision"] == "confirmed" and recs[1]["ok"] is False
    assert recs[1]["error"] == "device_not_connected"


def test_default_summary_without_summary_fn(fx: Fixture) -> None:
    first = fx.runner.call("mcp", "tx_disconnected", {"bssid": IN_SCOPE})
    assert first["summary"].startswith("tx_disconnected(")


# --- foutafhandeling in handlers ---


def test_toolbox_error_mapping(fx: Fixture) -> None:
    result = fx.runner.call("mcp", "disconnected", {})
    assert result == {
        "ok": False,
        "error": "device_not_connected",
        "message": "Flipper niet verbonden",
    }
    aborted = fx.runner.call("mcp", "aborting", {})
    assert aborted["error"] == "aborted" and aborted["message"] == "afgebroken door stop"
    recs = fx.audit()
    assert [r["decision"] for r in recs] == ["allow", "allow"]
    assert [r["ok"] for r in recs] == [False, False]
    assert recs[0]["result"] == "Flipper niet verbonden"


def test_unexpected_exception_does_not_leak_message(
    fx: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    result = fx.runner.call("mcp", "crash", {})
    assert result == {"ok": False, "error": "internal_error", "message": "RuntimeError"}
    assert "geheim" not in json.dumps(result)
    audit_raw = fx.audit_path.read_text(encoding="utf-8")
    assert "geheim" not in audit_raw and "/etc/shadow" not in audit_raw
    (rec,) = fx.audit()
    assert rec["decision"] == "allow" and rec["error"] == "internal_error" and rec["ok"] is False


# --- audit: precies één regel per pad ---


def test_exactly_one_audit_line_per_call(fx: Fixture) -> None:
    pending = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})["pending_id"]
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("mcp", "read_tool", {}),
        ("mcp", "read_tool", {"n": 99}),
        ("mcp", "nope", {}),
        ("mcp", "capture_tool", {"bssid": OUT_OF_SCOPE}),
        ("mcp", "transmit_tool", {"bssid": IN_SCOPE}),
        ("mcp", "crash", {}),
        ("mcp", CONFIRM_TOOL, {"pending_id": pending}),
        ("mcp", CONFIRM_TOOL, {"pending_id": pending}),
        ("mcp", CONFIRM_TOOL, {"pending_id": "ab"}),
    ]
    before = len(fx.audit())
    expected = [
        "allow",
        "invalid",
        "deny",
        "deny",
        "confirm",
        "allow",
        "confirmed",
        "deny",
        "invalid",
    ]
    for frontend, name, args in calls:
        fx.runner.call(frontend, name, args)
    recs = fx.audit()[before:]
    assert [r["decision"] for r in recs] == expected
    assert len(recs) == len(calls)


# --- fysieke bevestiger ("Flipper verplicht") -----------------------------------------------


def test_confirm_action_refused_while_flipper_attached(fx: Fixture) -> None:
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    refused = fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})
    assert refused["ok"] is False and refused["error"] == CONFIRM_ON_FLIPPER
    assert "Flipper" in refused["message"]
    assert fx.calls == []
    assert [p.id for p in fx.ctx.pending.list()] == [first["pending_id"]]  # blijft staan
    fx.runner.detach_physical_confirmer(lease, "test")
    assert fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})["ok"]
    recs = fx.audit()
    assert [r.get("event") or r["decision"] for r in recs] == [
        "confirm",
        "physical_confirmer",
        "deny",
        "physical_confirmer",
        "confirmed",
    ]
    assert recs[2]["error"] == CONFIRM_ON_FLIPPER and recs[2]["pending_id"] == first["pending_id"]
    assert [recs[1]["attached"], recs[3]["attached"]] == [True, False]


def test_confirm_physical_executes_pending_of_any_frontend_once(fx: Fixture) -> None:
    first = fx.runner.call("mcp-http:client", "transmit_tool", {"bssid": IN_SCOPE})
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    done = fx.runner.confirm_physical(lease, first["pending_id"])
    assert done["ok"] is True and [c[0] for c in fx.calls] == ["transmit_tool"]
    again = fx.runner.confirm_physical(lease, first["pending_id"])
    assert again["error"] == "unknown_pending" and len(fx.calls) == 1
    rec = next(r for r in fx.audit() if r.get("decision") == "confirmed")
    assert rec["frontend"] == "flipper-ui" and rec["created_by"] == "mcp-http:client"
    assert rec["confirmed_on"] == "flipper" and rec["confirmed"] is True


def test_confirm_physical_rechecks_scope(tmp_path: Path, make_ctx: Any) -> None:
    scope = Scope.model_validate({"wifi": [{"bssid": IN_SCOPE, "channel": 6}]})
    ctx = make_ctx(tmp_path, scope)
    fx = Fixture(tmp_path, ctx)
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    ctx.scope_store = ScopeStore.fixed(Scope())
    fx.runner.ctx.scope_store = ctx.scope_store
    fx.runner.policy = Policy(ctx.scope_store)
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    denied = fx.runner.confirm_physical(lease, first["pending_id"])
    assert denied["error"] == "denied" and fx.calls == []


def test_confirm_physical_with_stale_lease_does_nothing(fx: Fixture) -> None:
    """Een OK die nog onderweg was toen de app wegviel: niets uitvoeren, pending blijft."""
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    fx.runner.detach_physical_confirmer(lease, "link weg")
    late = fx.runner.confirm_physical(lease, first["pending_id"])
    assert late["error"] == "unknown_pending" and "gekoppeld" in late["message"]
    assert fx.calls == [] and len(fx.ctx.pending.list()) == 1
    assert fx.runner.reject_physical(lease, first["pending_id"])["error"] == "unknown_pending"
    assert len(fx.ctx.pending.list()) == 1


def test_reject_physical(fx: Fixture) -> None:
    first = fx.runner.call("agent-cli", "transmit_tool", {"bssid": IN_SCOPE})
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    rejected = fx.runner.reject_physical(lease, first["pending_id"])
    assert rejected["ok"] is True and rejected["result"]["rejected"] is True
    assert fx.calls == [] and fx.ctx.pending.list() == []
    rec = next(r for r in fx.audit() if r.get("decision") == "rejected")
    assert rec["created_by"] == "agent-cli" and rec["confirmed_on"] == "flipper"


def test_detach_with_foreign_lease_is_noop(fx: Fixture) -> None:
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    fx.runner.detach_physical_confirmer(PhysicalConfirmer("flipper-ui"), "nep")
    assert fx.ctx.pending.physical_confirmer == "flipper-ui"
    fx.runner.detach_physical_confirmer(lease, "echt")
    fx.runner.detach_physical_confirmer(lease, "nog eens")  # idempotent, geen tweede regel
    events = [r for r in fx.audit() if r.get("event") == "physical_confirmer"]
    assert [e["attached"] for e in events] == [True, False]


# --- uitkomst van pendings (subscribe_resolutions) ---------------------------------------------


def test_resolutions_confirmed_rejected_cleared(fx: Fixture) -> None:
    seen: list[PendingResolved] = []
    unsubscribe = fx.runner.subscribe_resolutions(seen.append)
    a = fx.runner.call("agent-cli", "transmit_tool", {"bssid": IN_SCOPE})["pending_id"]
    b = fx.runner.call("agent-cli", "transmit_tool", {"bssid": IN_SCOPE})["pending_id"]
    c = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})["pending_id"]
    lease = fx.runner.attach_physical_confirmer("flipper-ui")
    done = fx.runner.confirm_physical(lease, a)
    fx.runner.reject_physical(lease, b)
    fx.ctx.pending.clear()
    assert [(e.pending_id, e.resolution) for e in seen] == [
        (a, Resolution.CONFIRMED),
        (b, Resolution.REJECTED),
        (c, Resolution.CLEARED),
    ]
    assert seen[0].result == done and seen[0].confirmed_on == "flipper"
    assert seen[0].created_by == "agent-cli" and seen[0].tool == "transmit_tool"
    assert seen[1].result is None and seen[2].created_by == "mcp"
    unsubscribe()
    fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    fx.ctx.pending.clear()
    assert len(seen) == 3


def test_resolution_expired_and_chat_confirm(tmp_path: Path, make_ctx: Any) -> None:
    now = [100.0]
    scope = Scope.model_validate({"wifi": [{"bssid": IN_SCOPE, "channel": 6}]})
    fx = Fixture(tmp_path, make_ctx(tmp_path, scope, ttl_s=10.0, clock=lambda: now[0]))
    seen: list[PendingResolved] = []
    fx.runner.subscribe_resolutions(seen.append)
    old = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})["pending_id"]
    now[0] += 11
    new = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})["pending_id"]  # purge
    fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": new})
    assert [(e.pending_id, e.resolution) for e in seen] == [
        (old, Resolution.EXPIRED),
        (new, Resolution.CONFIRMED),
    ]
    assert seen[1].confirmed_on is None and seen[1].result is not None and seen[1].result["ok"]


def test_failing_resolution_listener_does_not_break_runner(fx: Fixture) -> None:
    def boom(_event: PendingResolved) -> None:
        raise RuntimeError("kapot")

    fx.runner.subscribe_resolutions(boom)
    first = fx.runner.call("mcp", "transmit_tool", {"bssid": IN_SCOPE})
    assert fx.runner.call("mcp", CONFIRM_TOOL, {"pending_id": first["pending_id"]})["ok"]
