"""wifi_tools: scope_checks, handlers en de weg via ToolRunner + Policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import MakeCtx

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.errors import InvalidState
from pitoolbox.core.policy import Policy
from pitoolbox.core.scope import Scope
from pitoolbox.core.tools import wifi_tools
from pitoolbox.core.tools.base import Registry, Risk, ToolSpec
from pitoolbox.core.tools.runner import ToolRunner

IN_SCOPE = "AA:BB:CC:DD:EE:01"  # kanaal 6 in conftest.fixed_scope
OUT_OF_SCOPE = "11:22:33:44:55:66"


def spec(name: str) -> ToolSpec:
    return next(s for s in wifi_tools.TOOLS if s.name == name)


def runner_for(ctx: Any, tmp_path: Path) -> ToolRunner:
    policy = Policy(ctx.scope_store, [])
    return ToolRunner(Registry(wifi_tools.TOOLS), ctx, policy, AuditLog(tmp_path / "a.jsonl"))


def test_tool_names_and_risks() -> None:
    by = {s.name: s.risk for s in wifi_tools.TOOLS}
    assert by == {
        "alfa_set_mode": Risk.CAPTURE,
        "wifi_scan": Risk.READ,
        "capture_start": Risk.CAPTURE,
        "capture_stop": Risk.CAPTURE,
    }


# -- alfa_set_mode ---------------------------------------------------------------------


def test_set_mode_scope_always_in_scope(fixed_scope: Scope) -> None:
    # Uplink-guard zit in AlfaAdapter.set_mode (zie test_wifi_adapter.py).
    args = wifi_tools.AlfaSetModeArgs(mode="monitor")
    assert spec("alfa_set_mode").scope_check is not None
    assert spec("alfa_set_mode").scope_check(args, fixed_scope).in_scope  # type: ignore[misc]


def test_set_mode_handler(make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.list_active.return_value = []
    ctx.wifi.set_mode.return_value = {"iface": "wlan1", "mode": "monitor"}
    out = spec("alfa_set_mode").handler(ctx, wifi_tools.AlfaSetModeArgs(mode="monitor"))
    ctx.wifi.set_mode.assert_called_once_with("monitor")
    assert out["mode"] == "monitor"


def test_set_managed_refused_while_capturing(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.list_active.return_value = [{"capture_id": "abcdef"}]
    with pytest.raises(InvalidState):
        spec("alfa_set_mode").handler(ctx, wifi_tools.AlfaSetModeArgs(mode="managed"))
    ctx.wifi.set_mode.assert_not_called()


def test_set_mode_invalid_arg_rejected(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    r = runner_for(make_ctx(tmp_path, fixed_scope), tmp_path)
    res = r.call("test", "alfa_set_mode", {"mode": "ap"})
    assert res["error"] == "invalid_arguments"


# -- wifi_scan -------------------------------------------------------------------------


def fake_scan() -> dict[str, Any]:
    return {
        "method": "iw",
        "band": "abg",
        "aps": [
            {"bssid": OUT_OF_SCOPE, "ssid": "Buur", "signal_dbm": -80.0},
            {"bssid": IN_SCOPE, "ssid": "MijnLab", "signal_dbm": -40.0},
            {"bssid": "11:22:33:44:55:77", "ssid": "", "signal_dbm": None},
        ],
        "stations": [],
    }


def test_wifi_scan_marks_scope_and_sorts(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.list_active.return_value = []
    ctx.wifi.scan.return_value = fake_scan()
    ctx.wifi.iface = "wlan1"
    out = spec("wifi_scan").handler(ctx, wifi_tools.WifiScanArgs())
    ctx.wifi.scan.assert_called_once_with("iw", 10, "abg")
    nets = out["networks"]
    assert [n["bssid"] for n in nets] == [IN_SCOPE, OUT_OF_SCOPE, "11:22:33:44:55:77"]
    assert [n["in_scope"] for n in nets] == [True, False, False]
    assert out["count"] == 3
    assert out["in_scope_count"] == 1
    assert "client_count" not in out


def test_wifi_scan_airodump_hides_client_macs(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.list_active.return_value = []
    scan = fake_scan()
    scan["method"] = "airodump"
    scan["stations"] = [{"mac": "66:55:44:33:22:11"}, {"mac": "66:55:44:33:22:12"}]
    ctx.wifi.scan.return_value = scan
    out = spec("wifi_scan").handler(ctx, wifi_tools.WifiScanArgs(method="airodump", duration_s=5))
    assert out["client_count"] == 2
    assert "66:55:44:33:22:11" not in str(out)


def test_wifi_scan_airodump_refused_during_capture(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.list_active.return_value = [{"capture_id": "abcdef"}]
    with pytest.raises(InvalidState):
        spec("wifi_scan").handler(ctx, wifi_tools.WifiScanArgs(method="airodump"))
    ctx.wifi.scan.assert_not_called()


def test_wifi_scan_arg_limits() -> None:
    from pydantic import ValidationError

    for bad in [{"duration_s": 2}, {"duration_s": 61}, {"band": "x"}, {"method": "sh"}]:
        with pytest.raises(ValidationError):
            wifi_tools.WifiScanArgs(**bad)
    a = wifi_tools.WifiScanArgs()
    assert (a.method, a.duration_s, a.band) == ("iw", 10, "abg")


# -- capture_start ---------------------------------------------------------------------


def start_check(fixed_scope: Scope, **kw: Any) -> Any:
    args = wifi_tools.CaptureStartArgs(**kw)
    return spec("capture_start").scope_check(args, fixed_scope)  # type: ignore[misc]


def test_capture_start_scope_in_scope(fixed_scope: Scope) -> None:
    assert start_check(fixed_scope, bssid=IN_SCOPE).in_scope
    assert start_check(fixed_scope, bssid=IN_SCOPE, channel=6).in_scope
    # MAC wordt genormaliseerd (kleine letters / streepjes)
    assert start_check(fixed_scope, bssid="aa-bb-cc-dd-ee-01").in_scope


def test_capture_start_scope_rejects_out_of_scope(fixed_scope: Scope) -> None:
    check = start_check(fixed_scope, bssid=OUT_OF_SCOPE)
    assert not check.in_scope
    assert OUT_OF_SCOPE in check.reason


def test_capture_start_scope_rejects_channel_mismatch(fixed_scope: Scope) -> None:
    check = start_check(fixed_scope, bssid=IN_SCOPE, channel=11)
    assert not check.in_scope
    assert "6" in check.reason


def test_capture_start_handler_uses_scope_channel(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.start.return_value = {"capture_id": "abcdef"}
    out = spec("capture_start").handler(
        ctx, wifi_tools.CaptureStartArgs(bssid=IN_SCOPE, duration_s=120, session="lab-1")
    )
    ctx.captures.start.assert_called_once_with(IN_SCOPE, 6, 120, "lab-1")
    assert out == {"capture_id": "abcdef"}


def test_capture_start_handler_scope_vanished(make_ctx: MakeCtx, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, Scope())
    with pytest.raises(InvalidState):
        spec("capture_start").handler(ctx, wifi_tools.CaptureStartArgs(bssid=IN_SCOPE))
    ctx.captures.start.assert_not_called()


def test_capture_start_arg_validation() -> None:
    from pydantic import ValidationError

    for bad in [
        {"bssid": "zz"},
        {"bssid": IN_SCOPE, "duration_s": 9},
        {"bssid": IN_SCOPE, "duration_s": 3601},
        {"bssid": IN_SCOPE, "channel": 0},
        {"bssid": IN_SCOPE, "session": "../etc"},
        {"bssid": IN_SCOPE, "session": "Upper"},
        {"bssid": IN_SCOPE, "extra": 1},
    ]:
        with pytest.raises(ValidationError):
            wifi_tools.CaptureStartArgs(**bad)
    assert wifi_tools.CaptureStartArgs(bssid=IN_SCOPE).duration_s == 300


def test_capture_start_via_runner_denies_out_of_scope(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    r = runner_for(ctx, tmp_path)
    res = r.call("test", "capture_start", {"bssid": OUT_OF_SCOPE})
    assert res["ok"] is False and res["error"] == "denied"
    res = r.call("test", "capture_start", {"bssid": IN_SCOPE, "channel": 1})
    assert res["ok"] is False and res["error"] == "denied"
    ctx.captures.start.assert_not_called()
    ctx.captures.start.return_value = {"capture_id": "abcdef"}
    res = r.call("test", "capture_start", {"bssid": IN_SCOPE})
    assert res["ok"] is True
    ctx.captures.start.assert_called_once()


# -- capture_stop ----------------------------------------------------------------------


def test_capture_stop(make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.captures.stop.return_value = {"capture_id": "abcdef", "files": []}
    s = spec("capture_stop")
    assert s.scope_check is not None
    assert s.scope_check(wifi_tools.CaptureStopArgs(capture_id="abcdef"), fixed_scope).in_scope
    assert s.handler(ctx, wifi_tools.CaptureStopArgs(capture_id="abcdef"))["capture_id"] == "abcdef"
    ctx.captures.stop.assert_called_once_with("abcdef")


def test_capture_stop_id_pattern() -> None:
    from pydantic import ValidationError

    for bad in ["", "ABCDEF", "abc", "abcdefg", "../../x", "abcde;"]:
        with pytest.raises(ValidationError):
            wifi_tools.CaptureStopArgs(capture_id=bad)
