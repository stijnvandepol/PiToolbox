"""AlfaAdapter en SubprocessRunner: exacte argv, moduswissel, scans, allowlist."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from wifi_fakes import FakeHandle, FakeRunner, fixture

from pitoolbox.config import Settings
from pitoolbox.core.devices.wifi import AlfaAdapter, iface_driver, require_driver
from pitoolbox.core.errors import CommandFailed, InvalidState
from pitoolbox.core.proc import (
    ALLOWED_BINARIES,
    RunResult,
    SubprocessRunner,
    stop_process,
    validate_iface,
)

# -- SubprocessRunner ------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "id"],
        ["bash"],
        ["/bin/sh"],
        ["/usr/sbin/iw", "dev"],  # pad is niet toegestaan, alleen de kale naam
        ["rm", "-rf", "/"],
        ["python", "-c", "print(1)"],
        ["IW", "dev"],
        [],
    ],
)
def test_subprocess_runner_refuses_non_allowlisted(argv: list[str]) -> None:
    runner = SubprocessRunner(which=lambda name: f"/usr/bin/{name}")
    with pytest.raises(CommandFailed):
        runner.run(argv, timeout=1)
    with pytest.raises(CommandFailed):
        runner.spawn(argv, None)


def test_allowlist_is_exact() -> None:
    assert frozenset({"iw", "ip", "airodump-ng", "vcgencmd"}) == ALLOWED_BINARIES


def test_subprocess_runner_missing_binary() -> None:
    runner = SubprocessRunner(which=lambda name: None)
    with pytest.raises(CommandFailed, match="niet gevonden"):
        runner.run(["iw", "dev"], timeout=1)


def test_subprocess_runner_nul_byte_refused() -> None:
    runner = SubprocessRunner(which=lambda name: f"/usr/bin/{name}")
    with pytest.raises(CommandFailed):
        runner.run(["iw", "dev\x00x"], timeout=1)


def test_subprocess_runner_never_uses_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr("pitoolbox.core.proc.subprocess.run", fake_run)
    runner = SubprocessRunner(which=lambda name: f"/usr/sbin/{name}")
    res = runner.run(["iw", "dev", "wlan1", "info"], timeout=7)
    assert res.stdout == "ok"
    assert seen["cmd"] == ["/usr/sbin/iw", "dev", "wlan1", "info"]
    assert isinstance(seen["cmd"], list)
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["timeout"] == 7
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL


def test_subprocess_runner_spawn_devnull_and_stderr_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    class FakePopen:
        pid = 99

        def __init__(self, cmd: Any, **kwargs: Any) -> None:
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr("pitoolbox.core.proc.subprocess.Popen", FakePopen)
    runner = SubprocessRunner(which=lambda name: f"/usr/bin/{name}")
    handle = runner.spawn(["airodump-ng", "wlan1"], tmp_path, "x.log")
    kw = seen["kwargs"]
    assert kw["shell"] is False
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL
    assert kw["cwd"] == tmp_path
    assert handle.pid == 99
    assert handle.stderr_path == tmp_path / "x.log"
    assert (tmp_path / "x.log").exists()
    kw["stderr"].close()


def test_stop_process_escalates_to_kill() -> None:
    h = FakeHandle(["airodump-ng"], None)
    h.ignore_interrupt = True
    rc = stop_process(h, grace_s=5)
    assert h.events[0] == "interrupt"
    assert "kill" in h.events
    assert rc == -9


def test_validate_iface() -> None:
    assert validate_iface("wlan1") == "wlan1"
    for bad in ["", "wlan1; id", "a b", "../x", "wlan1\n", "x" * 16]:
        with pytest.raises(InvalidState):
            validate_iface(bad)


# -- AlfaAdapter -----------------------------------------------------------------------


def make_adapter(runner: FakeRunner, tmp_path: Path, **kw: Any) -> AlfaAdapter:
    return AlfaAdapter("wlan1", "wlan0", runner, tmp_root=tmp_path, **kw)


def test_from_settings() -> None:
    a = AlfaAdapter.from_settings(Settings(alfa_iface="wlan5", uplink_iface="wlan0"), FakeRunner())
    assert a.iface == "wlan5"


def test_get_mode(tmp_path: Path) -> None:
    r = FakeRunner("monitor")
    assert make_adapter(r, tmp_path).get_mode() == "monitor"
    assert r.calls == [["iw", "dev", "wlan1", "info"]]


def test_set_mode_exact_argv_sequence(tmp_path: Path) -> None:
    r = FakeRunner("managed")
    res = make_adapter(r, tmp_path).set_mode("monitor")
    assert r.calls == [
        ["ip", "link", "set", "dev", "wlan1", "down"],
        ["iw", "dev", "wlan1", "set", "type", "monitor"],
        ["ip", "link", "set", "dev", "wlan1", "up"],
        ["iw", "dev", "wlan1", "info"],
    ]
    assert res == {"iface": "wlan1", "mode": "monitor"}
    # nooit de 'active' monitor-flag (mt76 issue #839)
    assert all("active" not in c and "flags" not in c for c in r.calls)


def test_set_mode_managed_back(tmp_path: Path) -> None:
    r = FakeRunner("monitor")
    assert make_adapter(r, tmp_path).set_mode("managed")["mode"] == "managed"
    assert r.calls[1] == ["iw", "dev", "wlan1", "set", "type", "managed"]


def test_set_mode_refuses_uplink(tmp_path: Path) -> None:
    r = FakeRunner()
    adapter = AlfaAdapter("wlan0", "wlan0", r, tmp_root=tmp_path)
    with pytest.raises(InvalidState, match="uplink"):
        adapter.set_mode("monitor")
    assert r.calls == []  # niets uitgevoerd
    with pytest.raises(InvalidState):
        adapter.scan("iw", 10, "abg")
    assert r.calls == []


def test_set_mode_rejects_bad_mode_and_bad_iface(tmp_path: Path) -> None:
    r = FakeRunner()
    with pytest.raises(InvalidState):
        make_adapter(r, tmp_path).set_mode("ap")
    with pytest.raises(InvalidState):
        AlfaAdapter("wlan1; reboot", "wlan0", r).set_mode("monitor")
    assert r.calls == []


def test_set_mode_failure_brings_iface_up_again(tmp_path: Path) -> None:
    r = FakeRunner()
    r.fail_prefixes.append(("iw", "dev", "wlan1", "set", "type"))
    with pytest.raises(CommandFailed):
        make_adapter(r, tmp_path).set_mode("monitor")
    assert r.calls[-1] == ["ip", "link", "set", "dev", "wlan1", "up"]


def test_set_mode_verify_mismatch(tmp_path: Path) -> None:
    r = FakeRunner("managed")
    # set type 'lukt' maar info blijft managed melden
    r.responses[("iw", "dev", "wlan1", "set", "type", "monitor")] = RunResult(0, "", "")
    with pytest.raises(CommandFailed, match="verwacht"):
        make_adapter(r, tmp_path).set_mode("monitor")


def test_scan_iw_requires_managed(tmp_path: Path) -> None:
    r = FakeRunner("monitor")
    with pytest.raises(InvalidState, match="managed"):
        make_adapter(r, tmp_path).scan("iw", 10, "abg")


def test_scan_iw(tmp_path: Path) -> None:
    r = FakeRunner("managed")
    res = make_adapter(r, tmp_path).scan("iw", 10, "abg")
    assert r.calls == [["iw", "dev", "wlan1", "info"], ["iw", "dev", "wlan1", "scan"]]
    assert res["method"] == "iw"
    assert len(res["aps"]) == 8
    assert res["stations"] == []


def test_scan_iw_band_filter(tmp_path: Path) -> None:
    r = FakeRunner("managed")
    a = make_adapter(r, tmp_path)
    assert [n["bssid"] for n in a.scan("iw", 10, "a")["aps"]] == ["AA:BB:CC:44:55:66"]
    assert len(a.scan("iw", 10, "bg")["aps"]) == 7


def test_scan_iw_busy_hint(tmp_path: Path) -> None:
    r = FakeRunner("managed")
    r.responses[("iw", "dev", "wlan1", "scan")] = RunResult(
        240, "", "command failed: Device or resource busy (-16)"
    )
    with pytest.raises(CommandFailed, match="bezig"):
        make_adapter(r, tmp_path).scan("iw", 10, "abg")


def test_scan_unknown_method_or_band(tmp_path: Path) -> None:
    r = FakeRunner()
    with pytest.raises(InvalidState):
        make_adapter(r, tmp_path).scan("shell", 10, "abg")
    with pytest.raises(InvalidState):
        make_adapter(r, tmp_path).scan("iw", 10, "x")


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_scan_airodump_requires_monitor(tmp_path: Path) -> None:
    r = FakeRunner("managed")
    with pytest.raises(InvalidState, match="monitor"):
        make_adapter(r, tmp_path).scan("airodump", 10, "abg")
    assert r.spawned == []


def test_scan_airodump(tmp_path: Path) -> None:
    r = FakeRunner("monitor")
    clock = Clock()
    seen: dict[str, Any] = {}

    def hook(h: FakeHandle) -> None:
        # airodump schrijft <prefix>-01.csv; de prefix staat achter '-w'
        prefix = h.argv[h.argv.index("-w") + 1]
        Path(prefix + "-01.csv").write_text(fixture("airodump-01.csv"), encoding="utf-8")
        seen["tmpdir"] = h.cwd

    r.spawn_hook = hook
    a = make_adapter(r, tmp_path, clock=clock.now, sleep=clock.sleep)
    res = a.scan("airodump", 5, "bg")

    argv = r.spawned[0].argv
    prefix = argv[argv.index("-w") + 1]
    assert argv == [
        "airodump-ng",
        "--output-format",
        "csv",
        "--write-interval",
        "1",
        "-w",
        prefix,
        "--band",
        "bg",
        "wlan1",
    ]
    assert Path(prefix).name == "scan"
    assert r.spawned[0].events[0] == "interrupt"
    assert clock.t == pytest.approx(5.0)  # duur gerespecteerd zonder echt te slapen
    assert len(res["aps"]) == 5
    assert len(res["stations"]) == 2
    assert not seen["tmpdir"].exists()  # tijdelijke map opgeruimd
    assert list(tmp_path.iterdir()) == []


def test_scan_airodump_early_exit(tmp_path: Path) -> None:
    r = FakeRunner("monitor")

    def hook(h: FakeHandle) -> None:
        h.returncode = 1
        assert h.stderr_path is not None
        h.stderr_path.write_text("ioctl(SIOCSIWMODE) failed", encoding="utf-8")

    r.spawn_hook = hook
    clock = Clock()
    a = make_adapter(r, tmp_path, clock=clock.now, sleep=clock.sleep)
    with pytest.raises(CommandFailed, match="SIOCSIWMODE"):
        a.scan("airodump", 5, "abg")
    assert list(tmp_path.iterdir()) == []  # ook bij een fout opgeruimd


# -- driver-guard ----------------------------------------------------------------------


def fake_sysfs(root: Path, drivers: dict[str, str]) -> Path:
    """Bouwt /sys/class/net/<iface>/device/driver -> .../drivers/<naam> na."""
    net = root / "class" / "net"
    for iface, driver in drivers.items():
        target = root / "bus" / "drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        device = net / iface / "device"
        device.mkdir(parents=True)
        (device / "driver").symlink_to(target)
    return net


def test_iface_driver_reads_sysfs_symlink(tmp_path: Path) -> None:
    net = fake_sysfs(tmp_path, {"wlan0": "brcmfmac", "wlan1": "mt7921u"})
    assert iface_driver("wlan0", net) == "brcmfmac"
    assert iface_driver("wlan1", net) == "mt7921u"
    assert iface_driver("wlan9", net) is None


def test_require_driver_off_when_not_configured(tmp_path: Path) -> None:
    require_driver("wlan9", None, tmp_path)
    require_driver("wlan9", "", tmp_path)


def test_set_mode_refuses_wrong_driver_before_any_command(tmp_path: Path) -> None:
    # Namen verwisseld: "wlan1" is in werkelijkheid de ingebouwde wifi (de uplink).
    net = fake_sysfs(tmp_path / "sys", {"wlan0": "mt7921u", "wlan1": "brcmfmac"})
    runner = FakeRunner()
    adapter = make_adapter(runner, tmp_path, expected_driver="mt7921u", sysfs_net=net)
    with pytest.raises(InvalidState, match="brcmfmac"):
        adapter.set_mode("monitor")
    with pytest.raises(InvalidState, match="brcmfmac"):
        adapter.scan("iw")
    assert runner.calls == []


def test_set_mode_refuses_missing_iface_with_driver_check(tmp_path: Path) -> None:
    net = fake_sysfs(tmp_path / "sys", {"wlan0": "brcmfmac"})
    adapter = make_adapter(FakeRunner(), tmp_path, expected_driver="mt7921u", sysfs_net=net)
    with pytest.raises(InvalidState, match="onbekend"):
        adapter.set_mode("monitor")


def test_from_settings_checks_driver_by_default(tmp_path: Path) -> None:
    assert Settings().alfa_driver == "mt7921u"
    adapter = AlfaAdapter.from_settings(Settings(), FakeRunner())
    assert adapter._expected_driver == "mt7921u"
    off = AlfaAdapter.from_settings(Settings(alfa_driver=""), FakeRunner())
    assert off._expected_driver is None
