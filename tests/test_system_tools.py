"""system_tools: status (onderdelen falen los) en emergency_stop (elke stap eigen try)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from conftest import MakeCtx
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport, wait_until

from pitoolbox.core.devices.flipper import FlipperDevice
from pitoolbox.core.errors import DeviceNotConnected
from pitoolbox.core.scope import Scope, ScopeStore
from pitoolbox.core.tools import system_tools
from pitoolbox.core.tools.base import Risk, ToolContext, ToolSpec


def spec(name: str) -> ToolSpec:
    return next(s for s in system_tools.TOOLS if s.name == name)


def call(ctx: ToolContext, name: str) -> dict[str, Any]:
    return spec(name).handler(ctx, spec(name).params())


def test_risks() -> None:
    assert spec("status").risk is Risk.READ
    assert spec("emergency_stop").risk is Risk.CONTROL


def test_status_all_sections(make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.system.throttled.return_value = {"available": True, "raw": "0x0"}
    ctx.system.uplink.return_value = {"iface": "wlan0"}
    ctx.system.disk.return_value = {"free": 1}
    ctx.flipper.status.return_value = {"connected": True}
    ctx.wifi.get_mode.return_value = "monitor"
    ctx.captures.list_active.return_value = [{"capture_id": "abcdef"}]
    out = call(ctx, "status")
    assert out["power"] == {"available": True, "raw": "0x0"}
    assert out["uplink"] == {"iface": "wlan0"}
    assert out["disk"] == {"free": 1}
    assert out["flipper"] == {"connected": True}
    assert out["wifi"] == {"iface": "wlan1", "mode": "monitor"}
    assert out["captures"] == [{"capture_id": "abcdef"}]
    assert out["scope"] == {"networks": 1, "ir_devices": 1, "error": None}


def test_status_sections_fail_independently(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.system.throttled.side_effect = RuntimeError("vcgencmd stuk")
    ctx.system.uplink.return_value = {"iface": "wlan0"}
    ctx.system.disk.side_effect = OSError("disk weg")
    ctx.flipper.status.side_effect = DeviceNotConnected("geen flipper")
    ctx.wifi.get_mode.side_effect = RuntimeError("iw weg")
    ctx.captures.list_active.side_effect = RuntimeError("manager stuk")
    out = call(ctx, "status")
    assert "vcgencmd stuk" in out["power"]["error"]
    assert out["uplink"] == {"iface": "wlan0"}  # onaangetast
    assert "disk weg" in out["disk"]["error"]
    assert "geen flipper" in out["flipper"]["error"]
    assert out["wifi"] == {"iface": "wlan1", "mode": "unknown"}  # mode: geen error-sectie
    assert "manager stuk" in out["captures"]["error"]
    assert out["scope"]["networks"] == 1


def test_status_scope_error_is_reported(make_ctx: MakeCtx, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, Scope())
    bad = tmp_path / "scope.yaml"
    bad.write_text("version: 2\nwifi: nonsense\n", encoding="utf-8")
    ctx.scope_store = ScopeStore(bad)
    out = call(ctx, "status")
    assert out["scope"]["networks"] == 0
    assert out["scope"]["error"]


def test_emergency_stop_all_steps(make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.stop_all.return_value = [{"capture_id": "abcdef"}]
    ctx.pending.create("t", spec("status").params(), "fe", "s")
    out = call(ctx, "emergency_stop")
    ctx.captures.stop_all.assert_called_once_with()
    ctx.flipper.abort.assert_called_once_with()
    assert out["captures_stopped"] == [{"capture_id": "abcdef"}]
    assert out["flipper_aborted"] is True
    assert out["pending_cleared"] == 1
    assert out["errors"] == {}
    assert ctx.pending.list() == []


def test_emergency_stop_continues_when_captures_fail(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.stop_all.side_effect = RuntimeError("airodump hangt")
    ctx.pending.create("t", spec("status").params(), "fe", "s")
    out = call(ctx, "emergency_stop")
    ctx.flipper.abort.assert_called_once_with()
    assert out["flipper_aborted"] is True
    assert out["pending_cleared"] == 1
    assert "airodump hangt" in out["errors"]["captures"]


def test_emergency_stop_continues_when_flipper_fails(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.flipper.abort.side_effect = DeviceNotConnected("niet verbonden")
    ctx.pending.create("t", spec("status").params(), "fe", "s")
    out = call(ctx, "emergency_stop")
    ctx.captures.stop_all.assert_called_once_with()
    assert out["flipper_aborted"] is False
    assert out["pending_cleared"] == 1  # pending is tóch gewist
    assert "niet verbonden" in out["errors"]["flipper"]


def test_emergency_stop_all_fail(make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.stop_all.side_effect = RuntimeError("a")
    ctx.flipper.abort.side_effect = RuntimeError("b")
    ctx.pending = _BrokenPending()  # type: ignore[assignment]
    out = call(ctx, "emergency_stop")
    assert set(out["errors"]) == {"captures", "flipper", "pending"}


class _BrokenPending:
    def clear(self) -> int:
        raise RuntimeError("c")


def test_status_does_not_report_bridge_poll_as_busy(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    """De lezer van de Flipper-bridge wijkt voor elke tool-call: geen "bezet" naar het model.
    Met een echt device waarop de lezer continu pollt; een echte operatie blijft "bezet"."""
    ctx = make_ctx(tmp_path, fixed_scope)
    fake = FakeFlipperTransport(start_in_rpc=True, kind="ble")
    fake.attach_fap(FakeFap())
    device = FlipperDevice(lambda: fake, timing=FAST_TIMING)
    ctx.flipper = device
    device.start_rpc_app(fake.fap_path)
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            if not device.poll_app_events(0.2):
                time.sleep(0.001)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert wait_until(device._lock.locked)
        for _ in range(5):
            out = call(ctx, "status")["flipper"]
            assert out["busy"] is False and out["operation"] is None
            assert out["ui_app"] == "active"
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join()
    with device._operation("ir_send"):
        out = call(ctx, "status")["flipper"]
    assert out["busy"] is True and out["operation"] == "ir_send"
    device.close()
