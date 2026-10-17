"""Tests voor `pitoolbox-flipper remote screen` en `remote press` (flipper_remote_cli.py), met een
echte `FlipperDevice` op een `FakeFlipperTransport`."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport

from pitoolbox import flipper_cli, flipper_ui_harness, flipper_ui_install
from pitoolbox.config import Settings
from pitoolbox.core.devices._flipper_rpc.pb import gui_pb2
from pitoolbox.core.devices._flipper_screen import FRAME_BYTES
from pitoolbox.core.devices.flipper import REMOTE_KEYS, FlipperDevice
from pitoolbox.flipper_remote_cli import KEYS, PRESS_GAP_S

ADDR = "80:E1:26:20:F6:6B"
TIMING = replace(FAST_TIMING, loader_free_wait_s=0.2, loader_poll_s=0.02)


class Lab:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fake = FakeFlipperTransport(start_in_rpc=True, kind="ble")
        self.fap = self.fake.attach_fap(FakeFap())
        self.settings = Settings(
            scope_file=tmp_path / "scope.yaml",
            audit_log=tmp_path / "audit.jsonl",
            captures_dir=tmp_path / "captures",
        )
        self.device_settings: list[Settings] = []
        self.devices: list[FlipperDevice] = []
        self.sleeps: list[float] = []
        monkeypatch.setattr(flipper_cli, "_load_settings", lambda: self.settings)
        monkeypatch.setattr(flipper_ui_install, "_make_device", self.make_device)
        monkeypatch.setattr(flipper_ui_harness, "_sleep", self.sleeps.append)

    def make_device(self, settings: Settings) -> FlipperDevice:
        self.device_settings.append(settings)
        device = FlipperDevice(lambda: self.fake, timing=TIMING)
        self.devices.append(device)
        return device


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Lab:
    return Lab(tmp_path, monkeypatch)


def _run(*argv: str) -> int:
    return flipper_cli.main(list(argv))


def test_cli_keys_match_the_device_keys() -> None:
    assert tuple(REMOTE_KEYS) == KEYS


def test_screen_prints_text(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    data = bytearray(FRAME_BYTES)
    data[0] = 0b11  # (0, 0) en (0, 1): één vol blok linksboven
    lab.fake.set_screen(bytes(data))
    assert _run("remote", "screen", "--transport", "ble", "--address", ADDR) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("|█ ") and len(lines) == 34
    assert lab.fake.stream_requests == ["start", "stop"]
    used = lab.device_settings[0]
    assert used.flipper_transport == "ble" and used.flipper_ble_address == ADDR
    assert not lab.fake.is_open  # device netjes gesloten


def test_screen_saves_png_and_reports_orientation(
    lab: Lab, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lab.fake.screen_orientation = gui_pb2.HORIZONTAL_FLIP
    out_png = tmp_path / "scherm.png"
    assert _run("remote", "screen", "--png", str(out_png), "--scale", "2") == 0
    png = out_png.read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png[16:24] == (256).to_bytes(4, "big") + (
        128
    ).to_bytes(4, "big")
    out = capsys.readouterr().out
    assert "PNG opgeslagen" in out and "HORIZONTAL_FLIP" in out


def test_screen_png_write_error_is_reported(
    lab: Lab, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("remote", "screen", "--png", str(tmp_path / "geen-map" / "x.png")) == 1
    assert "PNG niet geschreven" in capsys.readouterr().err


def test_press_times_with_gap(lab: Lab, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("remote", "press", "down", "--times", "3") == 0
    assert [k for k, t in lab.fake.gui_inputs if t == "SHORT"] == ["DOWN"] * 3
    assert lab.sleeps == [PRESS_GAP_S] * 2
    out = capsys.readouterr().out
    assert "DOWN gedrukt (3/3)" in out and "alleen voor ontwikkeling" in out


def test_press_long(lab: Lab) -> None:
    assert _run("remote", "press", "back", "--long") == 0
    assert lab.fake.gui_inputs == [("BACK", "PRESS"), ("BACK", "LONG"), ("BACK", "RELEASE")]


def test_press_refused_when_another_app_is_open(
    lab: Lab, capsys: pytest.CaptureFixture[str]
) -> None:
    lab.fake.user_app_open = True
    assert _run("remote", "press", "ok") == 1
    assert "Geweigerd bij klik 1/1" in capsys.readouterr().err
    assert lab.fake.gui_inputs == []


def test_press_stops_at_the_first_refusal(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """De eerste OK opent een app (zoals OK op een menu-item); de tweede klik weigert."""

    def opens_app(seconds: float) -> None:
        lab.fake.user_app_open = True

    monkeypatch.setattr(flipper_ui_harness, "_sleep", opens_app)
    assert _run("remote", "press", "ok", "--times", "2") == 1
    assert "Geweigerd bij klik 2/2" in capsys.readouterr().err
    assert [t for _, t in lab.fake.gui_inputs] == ["PRESS", "SHORT", "RELEASE"]


@pytest.mark.parametrize(
    "argv",
    [
        ["remote"],
        ["remote", "press"],
        ["remote", "press", "select"],
        ["remote", "press", "ok", "--times", "0"],
        ["remote", "press", "ok", "--times", "21"],
        ["remote", "screen", "--scale", "17"],
    ],
)
def test_remote_arguments_are_validated(lab: Lab, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(*argv)
    assert exc.value.code == 2
    assert lab.devices == []
