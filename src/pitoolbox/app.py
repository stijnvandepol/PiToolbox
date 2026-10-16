"""Bouwt de ToolRunner uit Settings. Gedeeld door alle frontends."""

from __future__ import annotations

from pitoolbox.config import Settings
from pitoolbox.core.audit import AuditLog
from pitoolbox.core.policy import PendingStore, Policy
from pitoolbox.core.scope import ScopeStore
from pitoolbox.core.tools import build_registry
from pitoolbox.core.tools.base import ToolContext
from pitoolbox.core.tools.runner import ToolRunner


def build_runner(settings: Settings | None = None) -> ToolRunner:
    from pitoolbox.core.devices.flipper import FlipperDevice
    from pitoolbox.core.devices.system import SystemProbe
    from pitoolbox.core.devices.wifi import AlfaAdapter, CaptureManager
    from pitoolbox.core.proc import SubprocessRunner

    settings = settings or Settings.from_env()
    scope_store = ScopeStore(settings.scope_file)
    proc = SubprocessRunner()
    ctx = ToolContext(
        settings=settings,
        scope_store=scope_store,
        flipper=FlipperDevice.from_settings(settings),
        wifi=AlfaAdapter.from_settings(settings, proc),
        captures=CaptureManager.from_settings(settings, proc),
        system=SystemProbe.from_settings(settings, proc),
        pending=PendingStore(ttl_s=settings.confirm_ttl_s),
    )
    policy = Policy(scope_store, settings.tool_allowlist)
    return ToolRunner(build_registry(), ctx, policy, AuditLog(settings.audit_log))
