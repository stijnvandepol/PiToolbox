"""Keuze tussen USB en BLE (auto/usb/ble), wisselen voor CLI-acties en keepalive per transport."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from flipper_fakes import (
    FakeBleFlipper,
    FakeBleWorld,
    FakeFlipperTransport,
    install_fake_bleak,
    wait_until,
)

from pitoolbox.config import Settings
from pitoolbox.core.devices import _ble_pairing as bp
from pitoolbox.core.devices import _flipper_ble as ble
from pitoolbox.core.devices import flipper as flipper_mod
from pitoolbox.core.devices.flipper import FlipperDevice, make_transport
from pitoolbox.core.errors import DeviceNotConnected, UnsupportedTransport

BLE_ADDRESS = "80:E1:26:00:00:01"


@pytest.fixture(autouse=True)
def no_real_dbus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Geen echte BlueZ-opzoekingen vanuit de BLE-transport; alleen de fake bleak."""

    async def unavailable() -> bp.BluezBus:
        raise bp.BlePairingError("geen D-Bus in deze test")

    monkeypatch.setattr(ble, "_bluez_bus_factory", unavailable)


@pytest.fixture
def usb(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Vervangt UsbSerialTransport door een CLI-simulator; `state.present` schakelt de kabel."""
    state = SimpleNamespace(present=False, created=[])

    class FakeUsb(FakeFlipperTransport):
        def __init__(self, port: str | None = None) -> None:
            super().__init__()
            self.port = port
            state.created.append(self)

        @staticmethod
        def is_present(port: str | None = None) -> bool:
            return bool(state.present)

        def describe(self) -> str:
            return f"fake-usb {self.port}"

    monkeypatch.setattr(flipper_mod, "UsbSerialTransport", FakeUsb)
    return state


@pytest.fixture
def flipper() -> FakeBleFlipper:
    return FakeBleFlipper(address=BLE_ADDRESS)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, flipper: FakeBleFlipper) -> FakeBleWorld:
    world = FakeBleWorld(flipper)
    install_fake_bleak(monkeypatch, world)
    return world


def settings(**kwargs: Any) -> Settings:
    kwargs.setdefault("flipper_cli_profile", "official")
    kwargs.setdefault("flipper_keepalive_s", 0.0)
    return Settings(**kwargs)


# --- selectiematrix -----------------------------------------------------------------------

# (modus, USB aanwezig, flipper_port, flipper_ble_address) -> verwachte transport
MATRIX = [
    ("auto", True, None, None, "usb"),
    ("auto", False, None, None, "ble"),
    ("auto", True, None, BLE_ADDRESS, "usb"),
    ("auto", False, None, BLE_ADDRESS, "ble"),
    # Een expliciet ingestelde poort telt als "USB gewenst", tenzij er ook een BLE-adres is.
    ("auto", False, "/dev/ttyACM0", None, "usb"),
    ("auto", False, "/dev/ttyACM0", BLE_ADDRESS, "ble"),
    ("auto", True, "/dev/ttyACM0", BLE_ADDRESS, "usb"),
    ("usb", True, None, None, "usb"),
    ("usb", False, None, None, "usb"),
    ("usb", False, "/dev/ttyACM0", BLE_ADDRESS, "usb"),
    ("ble", True, None, None, "ble"),
    ("ble", False, None, None, "ble"),
    ("ble", True, "/dev/ttyACM0", BLE_ADDRESS, "ble"),
]


@pytest.mark.parametrize(("mode", "present", "port", "address", "expected"), MATRIX)
def test_transport_selection_matrix(
    usb: SimpleNamespace,
    mode: str,
    present: bool,
    port: str | None,
    address: str | None,
    expected: str,
) -> None:
    usb.present = present
    transport = make_transport(
        settings(flipper_transport=mode, flipper_port=port, flipper_ble_address=address)
    )
    assert transport.kind == expected
    assert transport.starts_in_rpc is (expected == "ble")
    if expected == "ble":
        assert isinstance(transport, ble.BleTransport)
    else:
        assert transport.describe() == f"fake-usb {port}"


def test_ble_transport_gets_address_and_prefix_from_settings(usb: SimpleNamespace) -> None:
    transport = make_transport(
        settings(
            flipper_transport="ble",
            flipper_ble_address=BLE_ADDRESS,
            flipper_ble_name_prefix="Flipper",
        )
    )
    assert transport.describe() == f"ble:{BLE_ADDRESS} (Flipper*)"
    transport = make_transport(
        settings(flipper_transport="ble", flipper_ble_name_prefix="Mijn Flipper")
    )
    assert transport.describe() == "ble:(auto) (Mijn Flipper*)"


def test_usb_mode_never_touches_ble(usb: SimpleNamespace, world: FakeBleWorld) -> None:
    device = FlipperDevice.from_settings(settings(flipper_transport="usb"))
    try:
        assert device.system_info()
        assert device.status()["transport_kind"] == "usb"
    finally:
        device.close()
    assert world.flippers[0].connects == 0


def test_ble_mode_never_touches_usb(usb: SimpleNamespace, world: FakeBleWorld) -> None:
    usb.present = True
    device = FlipperDevice.from_settings(
        settings(flipper_transport="ble", flipper_ble_address=BLE_ADDRESS)
    )
    try:
        assert device.system_info()
        assert device.status()["transport_kind"] == "ble"
    finally:
        device.close()
    assert usb.created == []


def test_status_shows_no_transport_kind_before_first_use(usb: SimpleNamespace) -> None:
    device = FlipperDevice.from_settings(settings(flipper_transport="usb"))
    status = device.status()
    assert status["transport_kind"] is None
    assert status["connected"] is False


def test_ble_mode_without_flipper_is_device_not_connected(
    usb: SimpleNamespace, world: FakeBleWorld
) -> None:
    world.flippers.clear()
    device = FlipperDevice.from_settings(
        settings(flipper_transport="ble", flipper_ble_name_prefix="Flipper")
    )
    with pytest.raises(DeviceNotConnected):
        device.system_info()


# --- auto: kiezen bij elke (her)verbinding ---------------------------------------------------


def test_auto_picks_up_usb_cable_at_next_connect(
    usb: SimpleNamespace, world: FakeBleWorld, flipper: FakeBleFlipper
) -> None:
    device = FlipperDevice.from_settings(
        settings(flipper_transport="auto", flipper_ble_address=BLE_ADDRESS)
    )
    try:
        device.system_info()
        assert device.status()["transport_kind"] == "ble"
        device.close()
        usb.present = True  # kabel erin
        device.system_info()
        assert device.status()["transport_kind"] == "usb"
        assert len(usb.created) == 1
        device.close()
        usb.present = False  # kabel eruit: terug naar BLE
        device.system_info()
        assert device.status()["transport_kind"] == "ble"
        assert flipper.connects == 2
    finally:
        device.close()


def test_auto_falls_back_to_ble_when_usb_link_drops_and_cable_is_gone(
    usb: SimpleNamespace, world: FakeBleWorld
) -> None:
    usb.present = True
    device = FlipperDevice.from_settings(
        settings(flipper_transport="auto", flipper_ble_address=BLE_ADDRESS)
    )
    try:
        device.system_info()
        assert device.status()["transport_kind"] == "usb"
        usb.created[0].fail_next_write = True  # kabel eruit midden in het gebruik
        usb.present = False
        device.storage_list("/ext")  # transportfout -> één reconnect, nu via BLE
        assert device.status()["transport_kind"] == "ble"
    finally:
        device.close()


# --- CLI-acties over BLE ------------------------------------------------------------------------


def test_cli_action_over_ble_switches_to_usb_in_auto_mode(
    usb: SimpleNamespace, world: FakeBleWorld, flipper: FakeBleFlipper
) -> None:
    device = FlipperDevice.from_settings(
        settings(flipper_transport="auto", flipper_ble_address=BLE_ADDRESS)
    )
    try:
        device.system_info()
        assert device.status()["transport_kind"] == "ble"
        usb.present = True  # kabel aangesloten terwijl de BLE-verbinding open staat
        result = device.rfid_read(0.2)
        assert result["found"] is True
        assert device.status()["transport_kind"] == "usb"
        assert wait_until(lambda: flipper._client is None)  # BLE-link is netjes gesloten
        assert usb.created[-1].cli_log == ["rfid read"]
    finally:
        device.close()


def test_cli_action_over_ble_without_usb_is_unsupported_in_auto_mode(
    usb: SimpleNamespace, world: FakeBleWorld, flipper: FakeBleFlipper
) -> None:
    device = FlipperDevice.from_settings(
        settings(flipper_transport="auto", flipper_ble_address=BLE_ADDRESS)
    )
    try:
        with pytest.raises(UnsupportedTransport, match="alleen via USB"):
            device.subghz_rx(433_920_000, 0.1)
        with pytest.raises(UnsupportedTransport):
            device.nfc_scan(0.1)
        assert device.status()["transport_kind"] == "ble"  # verbinding blijft bruikbaar
        assert device.system_info()
        assert usb.created == []
    finally:
        device.close()


def test_cli_action_in_ble_mode_never_switches_to_usb(
    usb: SimpleNamespace, world: FakeBleWorld
) -> None:
    usb.present = True
    device = FlipperDevice.from_settings(
        settings(flipper_transport="ble", flipper_ble_address=BLE_ADDRESS)
    )
    try:
        with pytest.raises(UnsupportedTransport, match="sluit de Flipper aan met USB"):
            device.rfid_read(0.2)
        assert usb.created == []
    finally:
        device.close()


def test_cli_action_over_usb_is_unchanged(usb: SimpleNamespace) -> None:
    usb.present = True
    device = FlipperDevice.from_settings(settings(flipper_transport="auto"))
    try:
        assert device.rfid_read(0.2)["found"] is True
        assert device.status()["transport_kind"] == "usb"
    finally:
        device.close()


# --- keepalive per transport ----------------------------------------------------------------------


@pytest.fixture
def usb_only(usb: SimpleNamespace) -> Iterator[SimpleNamespace]:
    usb.present = True
    yield usb


def test_keepalive_does_not_run_on_usb_in_cli_mode(usb_only: SimpleNamespace) -> None:
    device = FlipperDevice.from_settings(
        settings(flipper_transport="usb", flipper_keepalive_s=0.05)
    )
    try:
        device.connect()
        assert device.status()["mode"] == "cli"
        fake = usb_only.created[-1]
        etx_before, cli_before = fake.idle_etx_count, len(fake.cli_log)
        time.sleep(0.5)
        assert fake.rpc_log == []
        assert fake.idle_etx_count == etx_before  # geen Ctrl+C of andere bytes door de keepalive
        assert len(fake.cli_log) == cli_before
    finally:
        device.close()


def test_no_keepalive_on_usb_even_in_rpc_mode(usb_only: SimpleNamespace) -> None:
    # Keepalive is alleen voor BLE; USB-gedrag blijft zoals in fase 1.
    device = FlipperDevice.from_settings(
        settings(flipper_transport="usb", flipper_keepalive_s=0.05)
    )
    try:
        device.system_info()
        fake = usb_only.created[-1]
        assert device.status()["mode"] == "rpc"
        pings_after_probe = fake.rpc_log.count("system_ping_request")
        time.sleep(0.3)
        assert fake.rpc_log.count("system_ping_request") == pings_after_probe
    finally:
        device.close()
    assert not any(t.name == "flipper-keepalive" and t.is_alive() for t in threading.enumerate())


def test_keepalive_disabled_with_zero_interval(usb_only: SimpleNamespace) -> None:
    device = FlipperDevice.from_settings(settings(flipper_transport="usb", flipper_keepalive_s=0.0))
    try:
        device.system_info()
        fake = usb_only.created[-1]
        pings_after_probe = fake.rpc_log.count("system_ping_request")
        time.sleep(0.3)
        assert fake.rpc_log.count("system_ping_request") == pings_after_probe
    finally:
        device.close()


def test_ble_adapter_setting_reaches_the_transport(usb: SimpleNamespace) -> None:
    transport = make_transport(
        settings(
            flipper_transport="ble", flipper_ble_address=BLE_ADDRESS, flipper_ble_adapter="hci1"
        )
    )
    assert isinstance(transport, ble.BleTransport)
    assert transport._adapter == "hci1"
    default = make_transport(settings(flipper_transport="ble", flipper_ble_address=BLE_ADDRESS))
    assert isinstance(default, ble.BleTransport)
    assert default._adapter is None


def test_ble_adapter_env_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PITOOLBOX_FLIPPER_BLE_ADAPTER", "hci1")
    assert Settings.from_env().flipper_ble_adapter == "hci1"
    monkeypatch.setenv("PITOOLBOX_FLIPPER_BLE_ADAPTER", "")
    assert Settings.from_env().flipper_ble_adapter is None
    monkeypatch.setenv("PITOOLBOX_FLIPPER_BLE_ADAPTER", "/org/bluez/hci1")
    with pytest.raises(ValueError, match="verwacht hciN"):
        Settings.from_env()
