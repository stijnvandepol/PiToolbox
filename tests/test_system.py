"""SystemProbe: throttled, uplink en disk met fixtures en een FakeRunner."""

from __future__ import annotations

from pathlib import Path

import pytest
from wifi_fakes import FakeRunner, fixture

from pitoolbox.config import Settings
from pitoolbox.core.devices.system import SystemProbe, decode_throttled
from pitoolbox.core.errors import CommandFailed
from pitoolbox.core.proc import RunResult


def probe(runner: FakeRunner, tmp_path: Path) -> SystemProbe:
    return SystemProbe("wlan0", tmp_path / "captures", runner)


def test_from_settings(tmp_path: Path) -> None:
    p = SystemProbe.from_settings(Settings(captures_dir=tmp_path), FakeRunner())
    assert isinstance(p, SystemProbe)


def test_throttled_0x50005(tmp_path: Path) -> None:
    r = FakeRunner()
    r.responses[("vcgencmd", "get_throttled")] = RunResult(0, fixture("throttled_0x50005.txt"), "")
    out = probe(r, tmp_path).throttled()
    assert r.calls == [["vcgencmd", "get_throttled"]]
    assert out["available"] is True
    assert out["raw"] == "0x50005"
    assert out["current"] == ["undervoltage", "throttled"]
    assert out["occurred"] == ["undervoltage", "throttled"]
    assert out["undervoltage_now"] is True


def test_throttled_zero(tmp_path: Path) -> None:
    r = FakeRunner()
    r.responses[("vcgencmd", "get_throttled")] = RunResult(0, fixture("throttled_0x0.txt"), "")
    out = probe(r, tmp_path).throttled()
    assert out["current"] == [] and out["occurred"] == []
    assert out["undervoltage_now"] is False


def test_decode_throttled_bits() -> None:
    assert decode_throttled(0x80008) == {
        "current": ["soft_temp_limit"],
        "occurred": ["soft_temp_limit"],
    }
    # alleen 'opgetreden', niet meer actueel
    out = decode_throttled(0x20000)
    assert out == {"current": [], "occurred": ["arm_freq_capped"]}


def test_throttled_unavailable_when_binary_missing(tmp_path: Path) -> None:
    class Missing(FakeRunner):
        def run(self, argv, timeout):  # type: ignore[no-untyped-def]
            raise CommandFailed("binary 'vcgencmd' niet gevonden in PATH")

    out = probe(Missing(), tmp_path).throttled()
    assert out["available"] is False
    assert "vcgencmd" in out["reason"]


def test_throttled_bad_output(tmp_path: Path) -> None:
    r = FakeRunner()
    r.responses[("vcgencmd", "get_throttled")] = RunResult(0, "unexpected", "")
    assert probe(r, tmp_path).throttled()["available"] is False
    r.responses[("vcgencmd", "get_throttled")] = RunResult(1, "", "VCHI initialization failed")
    out = probe(r, tmp_path).throttled()
    assert out["available"] is False
    assert "VCHI" in out["reason"]


def test_uplink(tmp_path: Path) -> None:
    r = FakeRunner()
    r.responses[("ip", "-j", "addr", "show", "dev", "wlan0")] = RunResult(
        0, fixture("ip_addr_wlan0.json"), ""
    )
    r.responses[("ip", "-j", "route", "show", "default")] = RunResult(
        0, fixture("ip_route_default.json"), ""
    )
    out = probe(r, tmp_path).uplink()
    assert r.calls == [
        ["ip", "-j", "addr", "show", "dev", "wlan0"],
        ["ip", "-j", "route", "show", "default"],
    ]
    assert out == {
        "iface": "wlan0",
        "operstate": "UP",
        "ipv4": ["192.168.1.50/24"],
        "is_default_route": True,
    }


def test_uplink_not_default_route(tmp_path: Path) -> None:
    r = FakeRunner()
    r.responses[("ip", "-j", "addr", "show", "dev", "wlan0")] = RunResult(
        0, fixture("ip_addr_wlan0.json"), ""
    )
    r.responses[("ip", "-j", "route", "show", "default")] = RunResult(
        0, fixture("ip_route_default_other.json"), ""
    )
    assert probe(r, tmp_path).uplink()["is_default_route"] is False


def test_uplink_errors(tmp_path: Path) -> None:
    r = FakeRunner()
    r.responses[("ip", "-j", "addr", "show", "dev", "wlan0")] = RunResult(
        1, "", 'Device "wlan0" does not exist.'
    )
    with pytest.raises(CommandFailed, match="does not exist"):
        probe(r, tmp_path).uplink()
    r.responses[("ip", "-j", "addr", "show", "dev", "wlan0")] = RunResult(0, "not json", "")
    with pytest.raises(CommandFailed, match="JSON"):
        probe(r, tmp_path).uplink()
    r.responses[("ip", "-j", "addr", "show", "dev", "wlan0")] = RunResult(0, "[]", "")
    with pytest.raises(CommandFailed, match="niet gevonden"):
        probe(r, tmp_path).uplink()


def test_disk_uses_nearest_existing_parent(tmp_path: Path) -> None:
    out = probe(FakeRunner(), tmp_path).disk()  # captures_dir bestaat nog niet
    assert out["path"] == str(tmp_path)
    assert out["total"] > 0
    assert out["used"] + out["free"] <= out["total"] + 1
    assert 0 <= out["percent"] <= 100
    (tmp_path / "captures").mkdir()
    assert probe(FakeRunner(), tmp_path).disk()["path"] == str(tmp_path / "captures")
