"""Tests voor scope.yaml: laden, normalisatie, padvalidatie en herladen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from pitoolbox.core.scope import (
    InfraredDevice,
    Scope,
    ScopeStore,
    load_scope,
    normalize_mac,
    validate_flipper_path,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "scope.example.yaml"

VALID_YAML = """
version: 1
wifi:
  - name: ap
    bssid: "aa-bb-cc-dd-ee-01"
    channel: 6
"""


def _bump_mtime(path: Path, delta: float = 10.0) -> None:
    """Zet de mtime expliciet vooruit, onafhankelijk van de resolutie van het filesystem."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + delta))


def test_example_scope_loads() -> None:
    scope = load_scope(EXAMPLE)
    assert scope.bssids() == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"}
    assert scope.ssids() == {"MijnLab", "MijnLab-5G"}
    tv = scope.ir_device("tv")
    assert tv is not None
    assert tv.file == "/ext/infrared/tv_woonkamer.ir"
    assert tv.buttons == ["Power", "Vol_up", "Vol_dn", "Mute"]
    assert scope.subghz[0].frequency_hz == 433_920_000
    assert scope.ir_device("onbekend") is None


@pytest.mark.parametrize(
    "raw",
    ["aa:bb:cc:dd:ee:01", "AA-BB-CC-DD-EE-01", "  aa-bb-cc-dd-ee-01 ", "AA:BB:CC:DD:EE:01"],
)
def test_normalize_mac_accepts_variants(raw: str) -> None:
    assert normalize_mac(raw) == "AA:BB:CC:DD:EE:01"


@pytest.mark.parametrize(
    "raw", ["", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:0G", "AABBCCDDEE01", "AA:BB:CC:DD:EE:01:02"]
)
def test_normalize_mac_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_mac(raw)


def test_network_lookup_normalizes_and_tolerates_garbage() -> None:
    scope = Scope.model_validate(
        {"wifi": [{"bssid": "aa-bb-cc-dd-ee-01", "channel": 11, "ssid": "x"}]}
    )
    assert scope.wifi[0].bssid == "AA:BB:CC:DD:EE:01"
    assert scope.network_by_bssid("aa:bb:cc:dd:ee:01") is not None
    assert scope.network_by_bssid("11:22:33:44:55:66") is None
    assert scope.network_by_bssid("geen-mac") is None


@pytest.mark.parametrize("channel", [0, 197])
def test_wifi_channel_bounds(channel: int) -> None:
    with pytest.raises(ValidationError):
        Scope.model_validate({"wifi": [{"bssid": "AA:BB:CC:DD:EE:01", "channel": channel}]})


@pytest.mark.parametrize(
    "path",
    [
        "/ext/infrared/tv.ir",
        "/ext",
        "/ext/",
        "/int/app data/x (1).sub",
        "/ext/subghz/433_92-[a].sub",
    ],
)
def test_flipper_path_accepts(path: str) -> None:
    assert validate_flipper_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/ext/../int/x",
        "/ext/infrared/..",
        "/ext/a/../b",
        "/etc/passwd",
        "/tmp/x",
        "ext/infrared/tv.ir",
        "/extra/x",
        "/ext//x",
        "/ext/a;rm -rf",
        "/ext/a`b`",
        "/ext/a$b",
        "/ext/a\nb",
        "/ext/a|b",
        "/ext/é",
        "",
        "/ext/" + "a" * 300,
    ],
)
def test_flipper_path_rejects(path: str) -> None:
    with pytest.raises(ValueError):
        validate_flipper_path(path)


def test_flipper_path_rejects_trailing_newline() -> None:
    with pytest.raises(ValueError):
        validate_flipper_path("/ext/infrared/tv.ir\n")


def test_ir_device_validates_path_and_name() -> None:
    assert InfraredDevice(name="tv", file="/ext/infrared/tv.ir").buttons is None
    with pytest.raises(ValidationError):
        InfraredDevice(name="tv", file="/ext/../x")
    with pytest.raises(ValidationError):
        InfraredDevice(name="TV Kamer", file="/ext/infrared/tv.ir")


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Scope.model_validate({"wifi": [], "onbekend": 1})


def test_missing_file_gives_empty_scope(tmp_path: Path) -> None:
    store = ScopeStore(tmp_path / "bestaat_niet.yaml")
    scope = store.get()
    assert scope == Scope()
    assert scope.wifi == [] and scope.infrared == []
    assert store.error is None


def test_missing_file_is_picked_up_once_created(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    store = ScopeStore(path)
    assert store.get().wifi == []
    path.write_text(VALID_YAML, encoding="utf-8")
    assert store.get().bssids() == {"AA:BB:CC:DD:EE:01"}


def test_reload_on_mtime_change(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    store = ScopeStore(path)
    first = store.get()
    assert first.bssids() == {"AA:BB:CC:DD:EE:01"}
    # Onveranderd bestand: dezelfde instantie uit de cache.
    assert store.get() is first

    path.write_text(VALID_YAML.replace("01", "02"), encoding="utf-8")
    _bump_mtime(path)
    second = store.get()
    assert second is not first
    assert second.bssids() == {"AA:BB:CC:DD:EE:02"}


def test_invalid_yaml_fails_closed_and_recovers(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(VALID_YAML, encoding="utf-8")
    store = ScopeStore(path)
    assert store.get().wifi
    assert store.error is None

    path.write_text("wifi: [onvolledig", encoding="utf-8")
    _bump_mtime(path)
    scope = store.get()
    assert scope.wifi == []  # niet de oude scope
    assert store.error

    path.write_text(VALID_YAML, encoding="utf-8")
    _bump_mtime(path, 20.0)
    assert store.get().wifi
    assert store.error is None


def test_schema_invalid_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text("wifi:\n  - bssid: niet-een-mac\n    channel: 6\n", encoding="utf-8")
    store = ScopeStore(path)
    assert store.get() == Scope()
    assert store.error


def test_invalid_on_first_load_is_empty_with_error(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text("version: 2\n", encoding="utf-8")
    store = ScopeStore(path)
    assert store.get() == Scope()
    assert store.error is not None


def test_fixed_store_returns_given_scope() -> None:
    scope = Scope.model_validate({"wifi": [{"bssid": "AA:BB:CC:DD:EE:01", "channel": 1}]})
    store = ScopeStore.fixed(scope)
    assert store.get() is scope
    assert store.error is None
