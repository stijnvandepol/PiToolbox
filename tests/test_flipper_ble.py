"""BLE-transport en FlipperDevice over BLE, tegen een gesimuleerde GATT-server (geen Bluetooth)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from bleak.exc import BleakDBusError, BleakError, BleakGATTProtocolError
from flipper_fakes import (
    BLE_ADV_UUID,
    FAST_TIMING,
    FakeBleFlipper,
    FakeBleWorld,
    fixture_json,
    install_fake_bleak,
    wait_until,
)

from pitoolbox.core.devices import _flipper_ble as ble
from pitoolbox.core.devices._flipper_rpc.framing import FrameDecoder, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2
from pitoolbox.core.devices.flipper import FlipperDevice, TransportError
from pitoolbox.core.errors import (
    Aborted,
    DeviceError,
    DeviceNotConnected,
    UnsupportedTransport,
)

# --- hulpjes -----------------------------------------------------------------------------


def ble_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "pitoolbox-ble" and t.is_alive()]


def ping_frame(data: bytes = b"hallo", command_id: int = 1) -> bytes:
    main = flipper_pb2.Main()
    main.command_id = command_id
    main.system_ping_request.data = data
    return encode_frame(main.SerializeToString())


def read_frame(transport: ble.BleTransport, timeout: float = 2.0) -> Any:
    decoder = FrameDecoder()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for payload in decoder.feed(transport.read(4096, 0.05)):
            main = flipper_pb2.Main()
            main.ParseFromString(payload)
            return main
    raise AssertionError("geen frame ontvangen")


@pytest.fixture
def flipper() -> FakeBleFlipper:
    return FakeBleFlipper()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, flipper: FakeBleFlipper) -> FakeBleWorld:
    world = FakeBleWorld(flipper)
    install_fake_bleak(monkeypatch, world)
    return world


@pytest.fixture
def make_transport(world: FakeBleWorld) -> Iterator[Callable[..., ble.BleTransport]]:
    created: list[ble.BleTransport] = []

    def make(flipper: FakeBleFlipper, **kwargs: Any) -> ble.BleTransport:
        options: dict[str, Any] = {
            "scan_timeout_s": 1.0,
            "connect_timeout_s": 2.0,
            "credit_timeout_s": 2.0,
            "write_timeout_s": 2.0,
        }
        options.update(kwargs)
        transport = ble.BleTransport(flipper.address, **options)
        created.append(transport)
        return transport

    yield make
    for transport in created:
        transport.close()


def make_device(
    flipper: FakeBleFlipper, *, keepalive_s: float = 0.0, **kwargs: Any
) -> FlipperDevice:
    def factory() -> ble.BleTransport:
        return ble.BleTransport(
            flipper.address,
            scan_timeout_s=1.0,
            connect_timeout_s=2.0,
            credit_timeout_s=2.0,
            write_timeout_s=2.0,
            **kwargs,
        )

    return FlipperDevice(factory, "official", timing=FAST_TIMING, keepalive_s=keepalive_s)


# --- UUID's en kleine helpers ---------------------------------------------------------------


def test_uuids_derived_from_firmware_byte_arrays() -> None:
    # serial_service_uuid.inc staat LSB-eerst; de string is de omgekeerde bytevolgorde.
    assert ble.SERVICE_UUID == "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
    assert ble.TX_CHAR_UUID == "19ed82ae-ed21-4c9d-4145-228e61fe0000"
    assert ble.RX_CHAR_UUID == "19ed82ae-ed21-4c9d-4145-228e62fe0000"
    assert ble.FLOW_CONTROL_UUID == "19ed82ae-ed21-4c9d-4145-228e63fe0000"
    assert ble.RPC_STATUS_UUID == "19ed82ae-ed21-4c9d-4145-228e64fe0000"


def test_uuid_conversion_reverses_bytes_and_validates_length() -> None:
    raw = tuple(range(16))
    assert ble.uuid_from_firmware_bytes(raw) == "0f0e0d0c-0b0a-0908-0706-050403020100"
    assert ble.uuid_from_firmware_bytes(bytes(raw)) == "0f0e0d0c-0b0a-0908-0706-050403020100"
    with pytest.raises(ValueError, match="16 bytes"):
        ble.uuid_from_firmware_bytes((1, 2, 3))


def test_flow_control_value_is_big_endian_uint32() -> None:
    assert ble._parse_flow_credit(b"\x00\x00\x04\x00") == 1024
    assert ble._parse_flow_credit(bytearray(b"\x00\x00\x00\x2a")) == 42
    with pytest.raises(DeviceError, match="4"):
        ble._parse_flow_credit(b"\x00\x04")


def test_is_auth_error_classification() -> None:
    assert ble.is_auth_error(BleakGATTProtocolError(0x05))  # insufficient authentication
    assert ble.is_auth_error(BleakGATTProtocolError(0x0F))  # insufficient encryption
    assert not ble.is_auth_error(BleakGATTProtocolError(0x0A))  # attribute not found
    assert ble.is_auth_error(BleakDBusError("org.bluez.Error.AuthenticationFailed", []))
    assert ble.is_auth_error(BleakDBusError("org.bluez.Error.NotPermitted", ["Not paired"]))
    assert ble.is_auth_error(BleakError("Insufficient Encryption"))
    assert not ble.is_auth_error(BleakError("Not connected"))
    assert not ble.is_auth_error(OSError("authentication"))


# --- scannen ---------------------------------------------------------------------------------


def test_scan_finds_flippers_by_prefix_and_adv_uuid_sorted_by_rssi(world: FakeBleWorld) -> None:
    world.flippers.append(
        FakeBleFlipper(address="80:E1:26:00:00:02", name="Flipper Beta", rssi=-40)
    )
    world.add_noise("11:11:11:11:11:11", "Telefoon", rssi=-30)
    world.add_noise("22:22:22:22:22:22", "", rssi=-70, service_uuids=[BLE_ADV_UUID])
    world.add_noise("33:33:33:33:33:33", "Ander", rssi=-71, service_uuids=[ble.SERVICE_UUID])
    world.add_noise(
        "44:44:44:44:44:44", "flipper klein", rssi=-90
    )  # prefix is niet hoofdlettergevoelig

    found = ble.scan_flippers("Flipper", timeout=0.1)

    assert [c.address for c in found] == [
        "80:E1:26:00:00:02",
        "80:E1:26:00:00:01",
        "22:22:22:22:22:22",
        "33:33:33:33:33:33",
        "44:44:44:44:44:44",
    ]
    assert found[0] == ble.BleCandidate("80:E1:26:00:00:02", "Flipper Beta", -40)


def test_scan_empty_prefix_only_matches_on_service_uuid(world: FakeBleWorld) -> None:
    world.add_noise("11:11:11:11:11:11", "Telefoon", rssi=-30)
    found = ble.scan_flippers("", timeout=0.1)
    assert [c.address for c in found] == ["80:E1:26:00:00:01"]


async def test_scan_works_inside_a_running_event_loop(world: FakeBleWorld) -> None:
    assert len(ble.scan_flippers("Flipper", timeout=0.1)) == 1


def test_scan_failure_becomes_device_error(world: FakeBleWorld) -> None:
    world.scan_error = BleakError("Bluetooth uit")
    with pytest.raises(DeviceError, match="BLE-scan mislukt"):
        ble.scan_flippers("Flipper", timeout=0.1)


# --- open / sluiten --------------------------------------------------------------------------


def test_open_by_address_reads_credit_and_describes(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    transport = make_transport(flipper)
    assert transport.kind == "ble"
    assert transport.starts_in_rpc is True
    assert not transport.is_open
    transport.open()
    assert transport.is_open
    assert transport.describe() == "ble:80:E1:26:00:00:01 (Flipper Testy)"
    assert flipper.connects == 1
    # De Flipper-kant begint met het volle krediet; er is nog niets geschreven.
    assert flipper.writes == []


def test_open_by_name_prefix_without_address(world: FakeBleWorld, flipper: FakeBleFlipper) -> None:
    world.add_noise("11:11:11:11:11:11", "Telefoon", rssi=-30)
    transport = ble.BleTransport(None, "Flipper", scan_timeout_s=1.0, connect_timeout_s=2.0)
    try:
        transport.open()
        assert transport.is_open
        assert transport.describe() == "ble:80:E1:26:00:00:01 (Flipper Testy)"
    finally:
        transport.close()


def test_open_without_any_flipper_is_device_not_connected(world: FakeBleWorld) -> None:
    world.flippers.clear()
    transport = ble.BleTransport(None, "Flipper", scan_timeout_s=0.1)
    with pytest.raises(DeviceNotConnected, match="Geen Flipper gevonden"):
        transport.open()
    assert not transport.is_open
    assert ble_threads() == []


def test_open_unknown_address_is_device_not_connected(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.advertising = False  # bijv. al verbonden met een telefoon
    with pytest.raises(DeviceNotConnected, match="niet gevonden"):
        make_transport(flipper).open()


def test_open_unpaired_gives_clear_pairing_error_and_leaks_nothing(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.paired = False
    transport = make_transport(flipper)
    with pytest.raises(ble.BleNotPaired) as exc_info:
        transport.open()
    assert "niet gepaird: draai `pitoolbox-flipper ble-pair`" in str(exc_info.value)
    assert exc_info.value.code == "ble_not_paired"
    assert isinstance(exc_info.value, DeviceError)
    assert not transport.is_open
    assert ble_threads() == []


def test_open_connect_failure_is_device_not_connected(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.connect_error = BleakError("le-connection-abort-by-local")
    with pytest.raises(DeviceNotConnected, match="mislukt"):
        make_transport(flipper).open()
    assert ble_threads() == []


def test_open_without_serial_service_is_device_error(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.missing_chars = True
    with pytest.raises(DeviceError, match="seriële Flipper-service"):
        make_transport(flipper).open()


def test_open_with_zero_credit_means_no_ble_rpc_session(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.initial_credit = 0  # bt.c: "RPC is busy, failed to open new session"
    with pytest.raises(DeviceError, match="geen RPC-sessie"):
        make_transport(flipper).open()


def test_close_stops_loop_thread_and_is_idempotent(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    transport = make_transport(flipper)
    transport.open()
    assert len(ble_threads()) == 1
    transport.close()
    transport.close()
    assert not transport.is_open
    assert ble_threads() == []
    with pytest.raises(TransportError):
        transport.write(b"x")
    transport.open()  # opnieuw openen na sluiten
    assert transport.is_open
    assert flipper.connects == 2


# --- lezen: gesplitste indicaties ----------------------------------------------------------


def test_request_and_response_over_split_indications(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.indication_size = 5
    transport = make_transport(flipper)
    transport.open()
    transport.write(ping_frame(b"hallo-wereld", command_id=7))
    reply = read_frame(transport)
    assert reply.command_id == 7
    assert reply.system_ping_response.data == b"hallo-wereld"
    assert flipper.indications >= 3  # antwoord kwam over meerdere indicaties binnen


def test_read_respects_max_bytes_and_times_out_empty(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    transport = make_transport(flipper)
    transport.open()
    assert transport.read(10, 0.05) == b""
    transport.write(ping_frame(b"x" * 40))
    first = transport.read(3, 1.0)
    assert 1 <= len(first) <= 3
    transport.reset_input()
    assert transport.read(10, 0.05) == b""


# --- schrijven met flow control ---------------------------------------------------------------


def test_chunks_never_exceed_credit_mtu_or_243(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:01", mtu=247, swallow=True)
    world.flippers.append(flipper)
    transport = ble.BleTransport(flipper.address, scan_timeout_s=1.0, write_timeout_s=2.0)
    try:
        transport.open()
        data = bytes(range(256)) * 20  # 5120 bytes = 5 volle bufferlengtes
        transport.write(data)
        assert bytes(flipper.received) == data
        # MTU 247 -> 244 bytes per write-without-response, maar max 243 (serial_service.h).
        assert max(flipper.writes) == 243
        assert flipper.overflows == 0
        assert all(
            w <= before for w, before in zip(flipper.writes, flipper.credits_before, strict=True)
        )
        # Het krediet raakte exact 5x op 0 (5120 = 5 x 1024); elke keer volgde een notify.
        assert wait_until(lambda: flipper.refills == 5)
    finally:
        transport.close()


def test_chunk_size_follows_a_small_mtu(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:02", mtu=50, swallow=True)
    world.flippers.append(flipper)
    transport = ble.BleTransport(flipper.address, scan_timeout_s=1.0)
    try:
        transport.open()
        transport.write(b"x" * 500)
        assert max(flipper.writes) == 47  # MTU 50 - 3
        assert sum(flipper.writes) == 500
    finally:
        transport.close()


def test_chunks_respect_a_small_remaining_credit(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:03", swallow=True)
    flipper.initial_credit = 100  # minder dan de bufferlengte
    world.flippers.append(flipper)
    transport = ble.BleTransport(flipper.address, scan_timeout_s=1.0)
    try:
        transport.open()
        transport.write(b"y" * 300)
        assert flipper.writes[0] == 100  # precies tot krediet 0, anders komt er nooit een notify
        assert flipper.overflows == 0
        assert sum(flipper.writes) == 300
        assert flipper.refills == 1
    finally:
        transport.close()


def test_small_writes_do_not_need_a_refill(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    flipper.auto_refill = False
    transport = make_transport(flipper)
    transport.open()
    for _ in range(10):
        transport.write(b"z" * 100)  # 1000 < 1024: geen wachttijd
    assert sum(flipper.writes) == 1000
    assert flipper.refills == 0


def test_write_blocks_until_refill_notification(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    flipper.auto_refill = False
    transport = make_transport(flipper)
    transport.open()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            transport.write(b"q" * 2000)
        except BaseException as exc:  # noqa: BLE001 - doorgeven aan de test
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert wait_until(lambda: sum(flipper.writes) == 1024)
    time.sleep(0.3)
    assert thread.is_alive()  # wacht op het krediet
    assert sum(flipper.writes) == 1024
    flipper.refill()  # de Flipper meldt weer 1024 bytes vrij
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert errors == []
    assert sum(flipper.writes) == 2000
    assert flipper.overflows == 0


def test_write_times_out_without_refill_and_marks_link_closed(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    flipper.auto_refill = False
    transport = make_transport(flipper, credit_timeout_s=0.3)
    transport.open()
    started = time.monotonic()
    with pytest.raises(TransportError, match="flow control"):
        transport.write(b"q" * 2000)
    assert 0.25 < time.monotonic() - started < 2.0
    assert not transport.is_open  # midden in een bericht: RPC-stroom niet meer in de pas
    with pytest.raises(TransportError):
        transport.read(10, 0.05)


def test_interrupt_wakes_a_blocked_write_with_aborted(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    flipper.auto_refill = False
    transport = make_transport(flipper)
    transport.open()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            transport.write(b"q" * 2000)
        except BaseException as exc:  # noqa: BLE001 - doorgeven aan de test
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert wait_until(lambda: sum(flipper.writes) == 1024)
    transport.interrupt()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], Aborted)
    assert not transport.is_open
    # Een volgende schrijfactie na de interrupt is weer gewoon mogelijk (nieuwe sessie).
    transport.open()
    flipper.auto_refill = True
    transport.write(b"ok")


def test_link_loss_wakes_a_blocked_write(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    flipper.auto_refill = False
    transport = make_transport(flipper)
    transport.open()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            transport.write(b"q" * 2000)
        except BaseException as exc:  # noqa: BLE001 - doorgeven aan de test
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    assert wait_until(lambda: sum(flipper.writes) == 1024)
    flipper.drop_link()
    thread.join(timeout=2.0)
    assert len(errors) == 1 and isinstance(errors[0], TransportError)
    assert wait_until(lambda: not transport.is_open)


def test_write_with_response_option(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    transport = make_transport(flipper, write_with_response=True)
    transport.open()
    transport.write(b"abc")
    assert flipper.write_responses == [True]


def test_default_writes_are_without_response(
    flipper: FakeBleFlipper, make_transport: Callable[..., ble.BleTransport]
) -> None:
    flipper.swallow = True
    transport = make_transport(flipper)
    transport.open()
    transport.write(b"abc")
    assert flipper.write_responses == [False]


def test_payload_bytes_are_never_logged(
    flipper: FakeBleFlipper,
    make_transport: Callable[..., ble.BleTransport],
    caplog: pytest.LogCaptureFixture,
) -> None:
    flipper.swallow = True
    caplog.set_level(logging.DEBUG, logger="pitoolbox.core.devices._flipper_ble")
    transport = make_transport(flipper)
    transport.open()
    transport.write(b"GEHEIM-PAYLOAD")
    assert "GEHEIM" not in caplog.text
    assert "47454845494d" not in caplog.text.lower()


# --- FlipperDevice over BLE -----------------------------------------------------------------


def test_device_system_info_multi_frame_over_split_indications(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:10", indication_size=7)
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        info = device.system_info()
        assert info == fixture_json("device_info.json")
        assert flipper.indications > 10  # het antwoord kwam in veel kleine indicaties
        status = device.status()
        assert status["mode"] == "rpc"
        assert status["transport_kind"] == "ble"
        assert status["connected"] is True
        assert status["transport"].startswith("ble:AA:AA:AA:AA:AA:10")
        # Geen CLI over BLE: geen start_rpc_session en geen CLI-regels.
        assert b"start_rpc_session" not in bytes(flipper.received)
        assert flipper.rpc.cli_log == []
        assert flipper.rpc.idle_etx_count == 0
    finally:
        device.close()


def test_device_storage_list_and_read_over_ble(world: FakeBleWorld) -> None:
    files = {"/ext/infrared/tv.ir": b"Filetype: IR\n" * 20, "/ext/readme.txt": b"hoi"}
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:11", files=files, indication_size=13)
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        listing = device.storage_list("/ext")
        assert {e["name"]: e["type"] for e in listing} == {"infrared": "dir", "readme.txt": "file"}
        assert device.storage_read("/ext/infrared/tv.ir", 10_000) == files["/ext/infrared/tv.ir"]
    finally:
        device.close()


def test_device_ir_send_over_ble(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:12")
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        assert device.ir_send("/ext/infrared/tv.ir", "Power")["sent"] is True
        assert flipper.rpc.sent_buttons == ["Power"]
    finally:
        device.close()


def test_cli_operations_raise_unsupported_transport_over_ble(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:13")
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        for call in (
            lambda: device.subghz_rx(433_920_000, 0.1),
            lambda: device.nfc_scan(0.1),
            lambda: device.rfid_read(0.1),
        ):
            with pytest.raises(UnsupportedTransport, match="alleen via USB") as exc_info:
                call()
            assert exc_info.value.code == "unsupported_transport"
            assert "sluit de Flipper aan met USB" in str(exc_info.value)
        assert flipper.writes == []  # er is niets (ook geen Ctrl+C) naar de Flipper gestuurd
        assert flipper.rpc.cli_log == []
        assert device.status()["mode"] == "rpc"
    finally:
        device.close()


def test_connect_over_ble_enters_rpc_without_cli_bytes(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:14")
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        device.connect()
        assert device.status()["mode"] == "rpc"
        assert flipper.writes == []  # geen probe nodig: de sessie loopt al
    finally:
        device.close()


def test_device_reports_unpaired_link_clearly(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:15", paired=False)
    world.flippers.append(flipper)
    device = make_device(flipper)
    with pytest.raises(ble.BleNotPaired, match="ble-pair"):
        device.system_info()
    assert device.status()["connected"] is False
    assert ble_threads() == []


def test_reconnects_after_link_loss_between_calls(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:16", files={"/ext/a.txt": b"a"})
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        assert device.storage_list("/ext")[0]["name"] == "a.txt"
        flipper.drop_link()
        assert wait_until(lambda: device.status()["connected"] is False)
        assert device.storage_list("/ext")[0]["name"] == "a.txt"
        assert flipper.connects == 2
        assert device.status()["connected"] is True
    finally:
        device.close()


def test_reconnects_once_when_link_drops_mid_request(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:17")
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        device.system_info()
        flipper.rpc.fail_on_rpc_kind = "system_device_info_request"  # link valt tijdens het verzoek
        info = device.system_info()
        assert info == fixture_json("device_info.json")
        assert flipper.connects == 2
    finally:
        device.close()


def test_no_retry_after_ir_button_was_sent_when_link_drops(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:18")
    world.flippers.append(flipper)
    device = make_device(flipper)
    try:
        device.system_info()
        flipper.rpc.fail_on_rpc_kind = "app_button_press_release_request"
        with pytest.raises(DeviceError, match="viel weg"):
            device.ir_send("/ext/infrared/tv.ir", "Power")
        assert flipper.rpc.sent_buttons == []  # niet automatisch opnieuw verstuurd
        assert flipper.connects == 1
        assert wait_until(lambda: device.status()["connected"] is False)
    finally:
        device.close()


def test_abort_wakes_a_request_blocked_on_flow_control(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:19", buff_size=64, swallow=True)
    flipper.auto_refill = False
    world.flippers.append(flipper)
    device = make_device(flipper)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            device.storage_list("/ext/" + "a" * 150)  # verzoek > 64 bytes: wacht op krediet
        except BaseException as exc:  # noqa: BLE001 - doorgeven aan de test
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert wait_until(lambda: sum(flipper.writes) == 64)
        device.abort()
        thread.join(timeout=3.0)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], Aborted)
        assert device.status()["connected"] is False  # volgende actie krijgt een verse sessie
    finally:
        device.close()


# --- keepalive over BLE -----------------------------------------------------------------------


def pings(flipper: FakeBleFlipper) -> int:
    return flipper.rpc.rpc_log.count("system_ping_request")


def test_keepalive_pings_when_idle_and_stops_on_close(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:20")
    world.flippers.append(flipper)
    device = make_device(flipper, keepalive_s=0.1)
    device.connect()
    assert wait_until(lambda: pings(flipper) >= 3, timeout=5.0)
    device.close()
    after_close = pings(flipper)
    time.sleep(0.4)
    assert pings(flipper) == after_close
    assert ble_threads() == []
    assert not any(t.name == "flipper-keepalive" and t.is_alive() for t in threading.enumerate())


def test_keepalive_skips_while_device_is_busy(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:21")
    world.flippers.append(flipper)
    device = make_device(flipper, keepalive_s=0.1)
    try:
        device.connect()
        assert wait_until(lambda: pings(flipper) >= 1, timeout=5.0)
        with device._lock:  # een actie bezet de I/O-lock
            before = pings(flipper)
            time.sleep(0.5)
            assert pings(flipper) == before
        assert wait_until(lambda: pings(flipper) > before, timeout=5.0)
    finally:
        device.close()


def test_keepalive_failure_marks_link_disconnected_and_next_call_reconnects(
    world: FakeBleWorld,
) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:22", files={"/ext/a.txt": b"a"})
    world.flippers.append(flipper)
    device = make_device(flipper, keepalive_s=0.1)
    try:
        device.connect()
        flipper.rpc.fail_on_rpc_kind = "system_ping_request"
        assert wait_until(lambda: device.status()["connected"] is False, timeout=5.0)
        assert device.storage_list("/ext")[0]["name"] == "a.txt"
        assert flipper.connects >= 2
    finally:
        device.close()


def test_keepalive_is_off_by_default(world: FakeBleWorld) -> None:
    flipper = FakeBleFlipper(address="AA:AA:AA:AA:AA:23")
    world.flippers.append(flipper)
    device = make_device(flipper)  # keepalive_s=0
    try:
        device.connect()
        time.sleep(0.3)
        assert pings(flipper) == 0
    finally:
        device.close()
