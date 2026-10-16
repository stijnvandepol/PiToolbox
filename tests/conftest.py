"""Gedeelde testfixtures: een ToolContext met nep-devices en een vaste scope."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pitoolbox.config import Settings
from pitoolbox.core.policy import PendingStore
from pitoolbox.core.scope import InfraredDevice, Scope, ScopeStore, WifiNetwork
from pitoolbox.core.tools.base import ToolContext

MakeCtx = Callable[..., ToolContext]


@pytest.fixture
def fixed_scope() -> Scope:
    """Eén wifi-netwerk en één IR-apparaat 'tv' met een beperkte knoppenlijst."""
    return Scope(
        wifi=[WifiNetwork(name="lab-ap", ssid="MijnLab", bssid="AA:BB:CC:DD:EE:01", channel=6)],
        infrared=[
            InfraredDevice(name="tv", file="/ext/infrared/tv.ir", buttons=["Power", "Vol_up"])
        ],
    )


@pytest.fixture
def make_ctx() -> MakeCtx:
    """Factory: `make_ctx(tmp_path, scope)` -> ToolContext met MagicMock-devices."""

    def factory(
        tmp_path: Path,
        scope: Scope | None = None,
        *,
        ttl_s: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> ToolContext:
        settings = Settings(
            scope_file=tmp_path / "scope.yaml",
            audit_log=tmp_path / "audit.jsonl",
            captures_dir=tmp_path / "captures",
        )
        pending = PendingStore(ttl_s=ttl_s, clock=clock) if clock else PendingStore(ttl_s=ttl_s)
        return ToolContext(
            settings=settings,
            scope_store=ScopeStore.fixed(scope or Scope()),
            flipper=MagicMock(),
            wifi=MagicMock(),
            captures=MagicMock(),
            system=MagicMock(),
            pending=pending,
        )

    return factory
