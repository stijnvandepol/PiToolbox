"""Tests voor flipper_cli.py: argumentvalidatie, koppelen, scannen en het ble-test-meetscript."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

from pitoolbox import flipper_cli
from pitoolbox.config import Settings
from pitoolbox.core.devices import _ble_pairing as bp
from pitoolbox.core.errors import DeviceError

ADDR = "AA:BB:CC:DD:EE:FF"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeFlipper:
    """Nep-Flipper met een klok die per aanroep vooruit loopt (vaste, rekenbare tijden)."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        bytes_per_s: float = 10 * 1024,
        with_ping: bool = False,
        fail_info: Exception | None = None,
    ) -> None:
        self.clock = clock
        self.bytes_per_s = bytes_per_s
        self.fail_info = fail_info
        self.closed = False
        self.reads: list[tuple[str, int]] = []
        self.pings = 0
        if with_ping:
            self.ping = self._ping
        self.files = {
            "/ext/infrared": [
                {"name": "a.ir", "type": "file", "size": 100},
                {"name": "big.ir", "type": "file", "size": 200_000},
                {"name": "huge.ir", "type": "file", "size": 300_000},
                {"name": "sub", "type": "dir", "size": 0},
            ],
            "/ext/subghz": [{"name": "x.sub", "type": "file", "size": 250_000}],
            "/ext": [{"name": "infrared", "type": "dir", "size": 0}],
        }

    def _ping(self) -> None:
        self.pings += 1
        self.clock.now += 0.02

    def system_info(self) -> dict[str, str]:
        if self.fail_info is not None:
            raise self.fail_info
        self.clock.now += 2.0
        return {
            "firmware_version": "1.4.3",
            "firmware_origin_fork": "Official",
            "hardware_name": "Mymzih",
            "hardware_uid": "DEADBEEF",
        }

    def storage_list(self, path: str) -> list[dict[str, Any]]:
        self.clock.now += 0.1
        if path not in self.files:
            raise DeviceError(f"map bestaat niet: {path}")
        return list(self.files[path])

    def storage_read(self, path: str, max_bytes: int) -> bytes:
        self.reads.append((path, max_bytes))
        parent, name = path.rsplit("/", 1)
        size = next(e["size"] for e in self.files[parent] if e["name"] == name)
        self.clock.now += size / self.bytes_per_s
        return b"x" * size

    def status(self) -> dict[str, Any]:
        return {"transport": "fake"}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(flipper_cli, "_clock", fake)
    return fake


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    value = Settings(flipper_ble_address="11:22:33:44:55:66")
    monkeypatch.setattr(flipper_cli, "_load_settings", lambda: value)
    return value


@pytest.fixture
def devices(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, settings: Settings
) -> dict[str, Any]:
    """Vervang _make_device; houdt per transport de gebruikte instellingen en het device bij."""
    state: dict[str, Any] = {"settings": {}, "device": {}, "factory": {}}
    state["factory"]["ble"] = lambda: FakeFlipper(clock)
    state["factory"]["usb"] = lambda: FakeFlipper(clock, bytes_per_s=100 * 1024)

    def make(used: Settings) -> FakeFlipper:
        transport = used.flipper_transport
        state["settings"][transport] = used
        device: FakeFlipper = state["factory"][transport]()
        state["device"][transport] = device
        return device

    monkeypatch.setattr(flipper_cli, "_make_device", make)
    return state


# --- argumentvalidatie -------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["ble-pair", "geen-mac"],
        ["ble-pair", "AA:BB:CC:DD:EE"],
        ["ble-unpair", "ZZ:BB:CC:DD:EE:FF"],
        ["ble-test", "--address", "nope"],
        ["ble-test", "--file", "/etc/passwd"],
        ["ble-test", "--file", "/ext/../int/x"],
        ["ble-test", "--rounds", "0"],
        ["ble-test", "--rounds", "abc"],
        ["ble-scan", "--timeout", "-1"],
        ["ble-pair", ADDR, "--timeout", "0"],
        [],
        ["onbekend"],
    ],
)
def test_invalid_arguments_exit_with_usage_error(
    argv: list[str], settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        flipper_cli.main(argv)
    assert exc_info.value.code == 2
    assert capsys.readouterr().err


def test_mac_address_is_normalized(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    seen: list[str] = []
    monkeypatch.setattr(bp, "unpair", lambda address, **kw: seen.append(address) or True)
    assert flipper_cli.main(["ble-unpair", "aa-bb-cc-dd-ee-ff"]) == 0
    assert seen == [ADDR]


# --- ble-pair ---------------------------------------------------------------------------


def feed_input(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    prompts: list[str] = []
    iterator = iter(answers)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(iterator)

    monkeypatch.setattr(flipper_cli, "_input", fake_input)
    return prompts


def test_prompt_passkey_retries_on_bad_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = feed_input(monkeypatch, ["abc", "12345", "1234567", "  042731 "])
    # vierde poging mag niet meer: eerst de limiet van drie pogingen
    with pytest.raises(ValueError, match="te veel"):
        flipper_cli.prompt_passkey()
    assert len(prompts) == 3
    assert "6 cijfers" in capsys.readouterr().err


def test_prompt_passkey_accepts_leading_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    feed_input(monkeypatch, ["x1", "042731"])
    assert flipper_cli.prompt_passkey() == 42731


def test_ble_pair_prints_env_line(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    feed_input(monkeypatch, ["nope", "123456"])
    entered: list[int] = []

    def fake_pair(address: str, provider: Callable[[], int], timeout: float, **kw: Any) -> Any:
        entered.append(provider())
        assert timeout == 90.0
        return bp.PairResult(address, "Flipper Abc", "/dev", False, True, True, True)

    monkeypatch.setattr(bp, "pair", fake_pair)
    assert flipper_cli.main(["ble-pair", ADDR]) == 0
    out = capsys.readouterr().out
    assert entered == [123456]
    assert "Zet Bluetooth aan op de Flipper" in out
    assert f"PITOOLBOX_FLIPPER_BLE_ADDRESS={ADDR}" in out
    assert "ble-test" in out


def test_ble_pair_force_unpairs_first(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    order: list[str] = []
    monkeypatch.setattr(bp, "unpair", lambda address, **kw: order.append("unpair") or True)

    def fake_pair(address: str, provider: Callable[[], int], timeout: float, **kw: Any) -> Any:
        order.append("pair")
        return bp.PairResult(address, None, "/dev", False, True, True, True)

    monkeypatch.setattr(bp, "pair", fake_pair)
    assert flipper_cli.main(["ble-pair", ADDR, "--force"]) == 0
    assert order == ["unpair", "pair"]


def test_ble_pair_error_gives_exit_1(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_pair(*args: Any, **kw: Any) -> Any:
        raise bp.BlePairingError("Koppelen mislukt: authenticatie mislukt.")

    monkeypatch.setattr(bp, "pair", fake_pair)
    assert flipper_cli.main(["ble-pair", ADDR]) == 1
    assert "authenticatie mislukt" in capsys.readouterr().err


def test_ble_pair_already_paired_mentions_force(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        bp, "pair", lambda *a, **k: bp.PairResult(ADDR, None, "/dev", True, True, True, False)
    )
    assert flipper_cli.main(["ble-pair", ADDR]) == 0
    captured = capsys.readouterr()
    assert "al gekoppeld" in captured.out
    assert "--force" in captured.out
    assert "Trusted" in captured.err


# --- ble-scan / ble-list / ble-unpair -----------------------------------------------------


def paired_device(address: str = ADDR) -> bp.PairedDevice:
    return bp.PairedDevice(address, "Flipper Abc", True, True, True, False)


def test_ble_scan_shows_paired_state(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        flipper_cli,
        "_scan",
        lambda prefix, timeout: [
            flipper_cli._Found("11:11:11:11:11:11", "Flipper Zwak", -90),
            flipper_cli._Found(ADDR, "Flipper Abc", -50),
            flipper_cli._Found("22:22:22:22:22:22", None, None),
        ],
    )
    monkeypatch.setattr(bp, "list_devices", lambda **kw: [paired_device()])
    assert flipper_cli.main(["ble-scan", "--timeout", "1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    rows = [line for line in lines if line.startswith(("AA:", "11:", "22:"))]
    assert rows[0].startswith(ADDR) and "-50 dBm" in rows[0] and rows[0].endswith("ja")
    assert rows[1].startswith("11:11") and rows[1].endswith("nee")
    assert "-" in rows[2]


def test_ble_scan_without_dbus_shows_unknown(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(flipper_cli, "_scan", lambda p, t: [flipper_cli._Found(ADDR, "F", -40)])

    def no_dbus(**kw: Any) -> Any:
        raise bp.BlePairingError("alleen op Linux")

    monkeypatch.setattr(bp, "list_devices", no_dbus)
    assert flipper_cli.main(["ble-scan"]) == 0
    captured = capsys.readouterr()
    assert "?" in captured.out
    assert "Koppelstatus onbekend" in captured.err


def test_ble_scan_nothing_found(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(flipper_cli, "_scan", lambda p, t: [])
    assert flipper_cli.main(["ble-scan"]) == 1
    assert "Geen Flipper gevonden" in capsys.readouterr().out


def test_scan_uses_ble_transport_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, float]] = []

    def scan_flippers(prefix: str, timeout: float) -> list[Any]:
        calls.append((prefix, timeout))
        return [types.SimpleNamespace(address="aa:bb:cc:dd:ee:ff", name="Flipper X", rssi=-60)]

    module = types.ModuleType("pitoolbox.core.devices._flipper_ble")
    module.scan_flippers = scan_flippers  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pitoolbox.core.devices._flipper_ble", module)
    found = flipper_cli._scan("Flipper", 3.0)
    assert calls == [("Flipper", 3.0)]
    assert found == [flipper_cli._Found(ADDR, "Flipper X", -60)]


def test_scan_supports_async_scan_function(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scan_flippers(prefix: str, timeout: float) -> list[Any]:
        return [types.SimpleNamespace(address=ADDR, name=None, rssi=None)]

    module = types.ModuleType("pitoolbox.core.devices._flipper_ble")
    module.scan_flippers = scan_flippers  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pitoolbox.core.devices._flipper_ble", module)
    assert flipper_cli._scan("Flipper", 1.0) == [flipper_cli._Found(ADDR, None, None)]


def test_scan_without_ble_module_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "pitoolbox.core.devices._flipper_ble", None)
    assert flipper_cli.main(["ble-scan"]) == 1
    assert "BLE-transportlaag niet beschikbaar" in capsys.readouterr().err


def test_ble_list_and_unpair(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    prefixes: list[str] = []

    def fake_list(prefix: str = "", **kw: Any) -> list[bp.PairedDevice]:
        prefixes.append(prefix)
        return [paired_device()]

    monkeypatch.setattr(bp, "list_devices", fake_list)
    assert flipper_cli.main(["ble-list"]) == 0
    assert ADDR in capsys.readouterr().out
    assert prefixes == ["Flipper"]
    assert flipper_cli.main(["ble-list", "--prefix", ""]) == 0
    assert prefixes[-1] == ""

    monkeypatch.setattr(bp, "unpair", lambda address, **kw: False)
    assert flipper_cli.main(["ble-unpair", ADDR]) == 0
    assert "niet bekend" in capsys.readouterr().out


def test_ble_list_empty(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bp, "list_devices", lambda **kw: [])
    assert flipper_cli.main(["ble-list"]) == 0
    assert "Geen gekoppelde Flippers" in capsys.readouterr().out


# --- ble-test ---------------------------------------------------------------------------


def test_ble_test_measures_throughput_and_latency(
    devices: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert flipper_cli.main(["ble-test", "--rounds", "4", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload["reports"]["ble"]

    # Transport geforceerd naar BLE; adres uit de instellingen.
    used = devices["settings"]["ble"]
    assert used.flipper_transport == "ble"
    assert used.flipper_ble_address == "11:22:33:44:55:66"
    assert payload["address"] == "11:22:33:44:55:66"

    assert report["connect_s"] == pytest.approx(2.0)
    assert report["ping"]["method"] == "storage_list /ext"
    assert report["ping"]["n"] == 4
    assert report["ping"]["median_ms"] == pytest.approx(100.0)
    # Grootste bestand <= 256 KiB: x.sub (250000 B), niet huge.ir (300000 B).
    assert report["download"]["file"] == "/ext/subghz/x.sub"
    assert report["download"]["size"] == 250_000
    assert devices["device"]["ble"].reads == [("/ext/subghz/x.sub", 256 * 1024)]
    # 250000 B in 250000/10240 s = 10 KiB/s.
    assert report["download"]["bytes_per_s"] == pytest.approx(10 * 1024)
    assert report["download"]["kib_per_s"] == pytest.approx(10.0)
    assert devices["device"]["ble"].closed
    assert "usb" not in payload["reports"]


def test_ble_test_address_option_overrides_settings(
    devices: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert flipper_cli.main(["ble-test", "--address", "aa:bb:cc:dd:ee:ff", "--json"]) == 0
    assert devices["settings"]["ble"].flipper_ble_address == ADDR
    assert json.loads(capsys.readouterr().out)["address"] == ADDR


def test_ble_test_uses_public_ping_when_available(
    devices: dict[str, Any], clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    devices["factory"]["ble"] = lambda: FakeFlipper(clock, with_ping=True)
    assert flipper_cli.main(["ble-test", "--rounds", "3", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)["reports"]["ble"]
    assert report["ping"]["method"] == "rpc_ping"
    assert report["ping"]["median_ms"] == pytest.approx(20.0)
    assert devices["device"]["ble"].pings == 3


def test_ble_test_explicit_file_and_repeated_reads(
    devices: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["ble-test", "--file", "/ext/infrared/a.ir", "--read-rounds", "3", "--json"]
    assert flipper_cli.main(argv) == 0
    report = json.loads(capsys.readouterr().out)["reports"]["ble"]
    assert report["download"]["file"] == "/ext/infrared/a.ir"
    assert len(report["download"]["rounds"]) == 3
    assert report["download"]["bytes_per_s"] == pytest.approx(10 * 1024)
    assert devices["device"]["ble"].reads[0][1] == 1024 * 1024


def test_ble_test_missing_file_is_a_note_not_a_crash(
    devices: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert flipper_cli.main(["ble-test", "--file", "/ext/infrared/weg.ir", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)["reports"]["ble"]
    assert any("bestaat niet" in note for note in report["notes"])
    assert report["download"]["rounds"] == []


def test_ble_test_without_testfile(
    devices: dict[str, Any], clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    def empty() -> FakeFlipper:
        device = FakeFlipper(clock)
        device.files = {"/ext": []}
        return device

    devices["factory"]["ble"] = empty
    assert flipper_cli.main(["ble-test"]) == 0
    out = capsys.readouterr().out
    assert "Geen testbestand" in out
    assert "hardware_uid" not in out


def test_ble_test_table_output(devices: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert flipper_cli.main(["ble-test", "--rounds", "2"]) == 0
    out = capsys.readouterr().out
    assert "verbinden + system_info" in out and "2.00 s" in out
    assert "10.0 KiB/s" in out
    assert "/ext/subghz/x.sub" in out
    assert "firmware_version = 1.4.3" in out
    assert "hardware_uid" not in out  # uniek serienummer tonen we niet


def test_ble_test_compare_usb(devices: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert flipper_cli.main(["ble-test", "--compare-usb", "--json"]) == 0
    reports = json.loads(capsys.readouterr().out)["reports"]
    assert devices["settings"]["usb"].flipper_transport == "usb"
    assert reports["usb"]["download"]["kib_per_s"] == pytest.approx(100.0)
    assert reports["usb"]["download"]["file"] == reports["ble"]["download"]["file"]
    assert devices["device"]["usb"].closed

    assert flipper_cli.main(["ble-test", "--compare-usb"]) == 0
    out = capsys.readouterr().out
    assert "BLE" in out and "USB" in out
    assert "100.0 KiB/s" in out


def test_ble_test_compare_usb_without_usb_flipper(
    devices: dict[str, Any], clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    devices["factory"]["usb"] = lambda: FakeFlipper(
        clock, fail_info=DeviceError("geen Flipper op USB gevonden")
    )
    assert flipper_cli.main(["ble-test", "--compare-usb", "--json"]) == 0
    captured = capsys.readouterr()
    assert list(json.loads(captured.out)["reports"]) == ["ble"]
    assert "USB-vergelijking overgeslagen" in captured.err
    assert devices["device"]["usb"].closed


def test_ble_test_connect_failure(
    devices: dict[str, Any], clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    devices["factory"]["ble"] = lambda: FakeFlipper(
        clock, fail_info=DeviceError("BLE-verbinding mislukt")
    )
    assert flipper_cli.main(["ble-test"]) == 1
    assert "BLE-test mislukt: BLE-verbinding mislukt" in capsys.readouterr().err
    assert devices["device"]["ble"].closed


def test_ble_test_without_address_scans_by_prefix(
    monkeypatch: pytest.MonkeyPatch, devices: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(flipper_cli, "_load_settings", lambda: Settings())
    assert flipper_cli.main(["ble-test"]) == 0
    assert devices["settings"]["ble"].flipper_ble_address is None
    assert "naamprefix" in capsys.readouterr().err


def test_measure_transport_throughput_math() -> None:
    clock = FakeClock()
    device = FakeFlipper(clock, bytes_per_s=50_000)
    report = flipper_cli.measure_transport(
        device, "ble", file="/ext/subghz/x.sub", rounds=1, read_rounds=2, clock=clock
    )
    stats = report.download_stats()
    assert stats is not None
    assert stats["bytes_per_s"] == pytest.approx(50_000)
    assert stats["kib_per_s"] == pytest.approx(50_000 / 1024)
    assert stats["seconds"] == pytest.approx(10.0)  # 2 x 250000 / 50000
