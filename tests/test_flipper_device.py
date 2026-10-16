"""FlipperDevice, RpcClient en CLI-profiel tegen de FakeFlipperTransport."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from flipper_fakes import (
    FAST_TIMING,
    FakeFlipperTransport,
    fixture_json,
    fixture_text,
)

from pitoolbox.config import Settings
from pitoolbox.core.devices import _flipper_cli as cli
from pitoolbox.core.devices._flipper_rpc.client import RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.framing import FrameDecoder, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2
from pitoolbox.core.devices.flipper import (
    ByteTransport,
    FlipperDevice,
    FlipperTiming,
    UsbSerialTransport,
)
from pitoolbox.core.errors import (
    Aborted,
    DeviceError,
    DeviceNotConnected,
    DeviceTimeout,
    UnsupportedFirmware,
)

IR_FILE = "/ext/infrared/tv.ir"


def make_device(
    fake: FakeFlipperTransport | None = None,
    profile: str = "auto",
    timing: FlipperTiming = FAST_TIMING,
) -> tuple[FlipperDevice, FakeFlipperTransport]:
    fake = fake or FakeFlipperTransport()
    return FlipperDevice(lambda: fake, profile, timing=timing), fake


# --- status, lui verbinden ---------------------------------------------------------------


def test_status_never_connects_and_is_lazy() -> None:
    calls: list[int] = []

    def factory() -> ByteTransport:
        calls.append(1)
        return FakeFlipperTransport()

    device = FlipperDevice(factory)
    status = device.status()
    assert status["connected"] is False
    assert status["mode"] == "unknown"
    assert status["busy"] is False
    assert status["profile"] is None
    assert calls == []


def test_from_settings_is_lazy_and_missing_port_is_not_connected() -> None:
    device = FlipperDevice.from_settings(Settings(flipper_port="COM99", flipper_cli_profile="auto"))
    assert device.status()["connected"] is False
    with pytest.raises(DeviceNotConnected):
        device.connect()
    assert device.status()["connected"] is False


def test_find_port_by_vid_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = [
        SimpleNamespace(vid=0x1234, pid=0x5678, device="COM1"),
        SimpleNamespace(vid=0x0483, pid=0x5740, device="COM7"),
    ]
    monkeypatch.setattr(
        "pitoolbox.core.devices._flipper_transport.list_ports.comports", lambda: ports
    )
    assert UsbSerialTransport.find_port() == "COM7"
    monkeypatch.setattr(
        "pitoolbox.core.devices._flipper_transport.list_ports.comports", lambda: ports[:1]
    )
    with pytest.raises(DeviceNotConnected, match="0483:5740"):
        UsbSerialTransport.find_port()


def test_connect_brings_device_to_cli() -> None:
    device, fake = make_device()
    device.connect()
    status = device.status()
    assert status["connected"] is True
    assert status["mode"] == "cli"
    assert status["transport"] == "fake-flipper"
    assert fake.idle_at_prompt


# --- moduswissel -------------------------------------------------------------------------


def test_system_info_collects_multi_frame_device_info() -> None:
    device, fake = make_device()
    info = device.system_info()
    assert info == fixture_json("device_info.json")
    assert info["firmware_origin_fork"] == "Official"
    assert fake.rpc_log == ["system_ping_request", "system_device_info_request"]
    assert fake.cli_log == ["start_rpc_session"]  # CR-only, geen extra regels
    assert device.status()["mode"] == "rpc"


def test_mode_switches_both_ways_automatically() -> None:
    device, fake = make_device()
    device.system_info()  # CLI -> RPC
    assert fake.mode == "rpc"
    result = device.rfid_read(0.2)  # RPC -> CLI via stop_session
    assert result["found"] is True
    assert "stop_session" in fake.rpc_log
    assert fake.mode == "cli"
    assert device.status()["mode"] == "cli"
    device.storage_list("/ext")  # CLI -> RPC opnieuw
    assert fake.mode == "rpc"
    assert fake.cli_log.count("start_rpc_session") == 2


def test_auto_profile_selected_from_firmware_fork() -> None:
    device, fake = make_device()
    device.rfid_read(0.2)
    assert device.status()["profile"] == "official"
    assert "system_device_info_request" in fake.rpc_log
    assert fake.mode == "cli"


def test_forced_profile_needs_no_rpc() -> None:
    device, fake = make_device(profile="official")
    device.rfid_read(0.2)
    assert fake.rpc_log == []
    assert device.status()["profile"] == "official"


def test_unknown_firmware_fork_is_unsupported_and_can_be_forced() -> None:
    info = fixture_json("device_info.json") | {"firmware_origin_fork": "Unleashed"}
    device, _ = make_device(FakeFlipperTransport(device_info=info))
    with pytest.raises(UnsupportedFirmware, match="PITOOLBOX_FLIPPER_CLI_PROFILE"):
        device.rfid_read(0.2)
    forced, _ = make_device(FakeFlipperTransport(device_info=info), profile="official")
    assert forced.rfid_read(0.2)["found"] is True


def test_unknown_profile_name_is_unsupported() -> None:
    device, _ = make_device(profile="unleashed")
    with pytest.raises(UnsupportedFirmware, match="beschikbaar"):
        device.rfid_read(0.2)


def test_sync_leaves_nfc_subshell_left_open() -> None:
    fake = FakeFlipperTransport()
    device, _ = make_device(fake, profile="official")
    device.connect()
    # Iemand liet de NFC-subshell open staan; de volgende actie moet dat herstellen.
    fake.write(b"nfc\r")
    assert fake.shell == "nfc"
    device._mode = "unknown"
    device.rfid_read(0.2)
    assert fake.shell == "main"


def test_recovers_from_lost_rpc_session_via_stop_session() -> None:
    device, fake = make_device(profile="official")
    device.system_info()
    assert fake.mode == "rpc"
    device._forget_mode()
    result = device.rfid_read(0.2)
    assert result["found"] is True
    assert "stop_session" in fake.rpc_log
    assert fake.mode == "cli"


# --- has_next, storage -------------------------------------------------------------------


def test_storage_list_collects_has_next_frames() -> None:
    files = {f"/ext/infrared/{n}.ir": b"x" * (i + 1) for i, n in enumerate("abcde")}
    files["/ext/infrared/sub/x.ir"] = b""
    device, fake = make_device(FakeFlipperTransport(files=files))
    entries = device.storage_list("/ext/infrared")
    assert [e["name"] for e in entries] == ["a.ir", "b.ir", "c.ir", "d.ir", "e.ir", "sub"]
    assert entries[0] == {"name": "a.ir", "type": "file", "size": 1}
    assert entries[-1]["type"] == "dir"
    assert fake.rpc_log.count("storage_list_request") == 1


def test_storage_list_missing_path_is_rpc_error() -> None:
    device, _ = make_device()
    with pytest.raises(RpcError) as exc_info:
        device.storage_list("/ext/nope")
    assert exc_info.value.status_name == "ERROR_STORAGE_NOT_EXIST"
    assert "bestaat niet" in str(exc_info.value)


def test_storage_read_concatenates_chunks() -> None:
    data = bytes(range(25))
    device, fake = make_device(FakeFlipperTransport(files={"/ext/a.bin": data}))
    assert device.storage_read("/ext/a.bin", 1000) == data
    assert fake.rpc_log[-2:] == ["storage_stat_request", "storage_read_request"]


def test_storage_read_empty_file() -> None:
    device, _ = make_device(FakeFlipperTransport(files={"/ext/empty.txt": b""}))
    assert device.storage_read("/ext/empty.txt", 10) == b""


def test_storage_read_refuses_big_file_before_reading() -> None:
    device, fake = make_device(FakeFlipperTransport(files={"/ext/big.bin": b"z" * 100}))
    with pytest.raises(DeviceError, match="groter"):
        device.storage_read("/ext/big.bin", 99)
    assert "storage_read_request" not in fake.rpc_log
    assert device.storage_read("/ext/big.bin", 100) == b"z" * 100


def test_storage_read_refuses_directory() -> None:
    device, fake = make_device(FakeFlipperTransport(files={"/ext/d/a.txt": b"a"}))
    with pytest.raises(DeviceError, match="map"):
        device.storage_read("/ext/d", 100)
    assert "storage_read_request" not in fake.rpc_log


# --- RpcClient direct --------------------------------------------------------------------


def rpc_client(fake: FakeFlipperTransport) -> RpcClient:
    fake.open()
    return RpcClient(fake, read_slice_s=0.01)


def ping(data: bytes = b"p") -> Any:
    main = flipper_pb2.Main()
    main.system_ping_request.data = data
    return main


def test_client_collects_frames_until_has_next_false() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    main = flipper_pb2.Main()
    main.system_device_info_request.SetInParent()
    frames = client.request(main, 1.0)
    assert len(frames) == len(fake.device_info) > 3
    assert [f.has_next for f in frames] == [True] * (len(frames) - 1) + [False]
    assert len({f.command_id for f in frames}) == 1


def test_client_matches_command_id_and_queues_interleaved_event() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    event = flipper_pb2.Main()
    event.command_id = 0
    event.app_state_response.state = application_pb2.APP_STARTED
    stale = flipper_pb2.Main()
    stale.command_id = 4242  # antwoord op iets wat we niet (meer) wachten
    stale.system_ping_response.data = b"oud"
    fake.queue_extra_frame(event)
    fake.queue_extra_frame(stale)
    [reply] = client.request(ping(b"nieuw"), 1.0)
    assert reply.system_ping_response.data == b"nieuw"
    assert len(client.events) == 2
    found = client.wait_event(
        lambda m: (
            m.WhichOneof("content") == "app_state_response"
            and m.app_state_response.state == application_pb2.APP_STARTED
        ),
        0.1,
    )
    assert found is not None
    assert len(client.events) == 1  # alleen het verouderde antwoord blijft over


def test_client_wait_event_reads_from_wire_and_times_out() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    fake.set_app_running(False)
    assert client.wait_event(lambda m: False, 0.05) is None


def test_client_raises_rpc_error_with_status_name() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    main = flipper_pb2.Main()
    main.storage_stat_request.path = "/ext/weg"
    with pytest.raises(RpcError) as exc_info:
        client.request(main, 1.0)
    assert exc_info.value.status_name == "ERROR_STORAGE_NOT_EXIST"
    assert exc_info.value.request == "storage_stat_request"


def test_client_session_error_with_command_id_zero_raises() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    decode_error = flipper_pb2.Main()
    decode_error.command_id = 0
    decode_error.command_status = flipper_pb2.ERROR_DECODE
    fake.queue_extra_frame(decode_error)
    with pytest.raises(RpcError, match="ERROR_DECODE"):
        client.request(ping(), 1.0)


def test_client_timeout_and_cancel() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    fake.responsive = False
    with pytest.raises(DeviceTimeout, match="system_ping_request"):
        client.request(ping(), 0.05)
    with pytest.raises(Aborted):
        client.request(ping(), 5.0, cancel=lambda: True)


class _SlowFrames:
    """Levert de antwoordframes één voor één met een pauze ertussen (zoals BLE-indicaties)."""

    def __init__(self, count: int, gap_s: float) -> None:
        self._count = count
        self._gap_s = gap_s
        self._pending: list[bytes] = []

    def write(self, data: bytes) -> None:
        [payload] = FrameDecoder().feed(data)
        request = flipper_pb2.Main()
        request.ParseFromString(payload)
        for i in range(self._count):
            frame = flipper_pb2.Main()
            frame.command_id = request.command_id
            frame.has_next = i < self._count - 1
            frame.system_ping_response.data = b"x"
            self._pending.append(encode_frame(frame.SerializeToString()))

    def read(self, max_bytes: int, timeout: float) -> bytes:
        time.sleep(min(self._gap_s, timeout))
        return self._pending.pop(0) if self._pending else b""


def test_client_timeout_is_per_frame_not_for_the_whole_answer() -> None:
    client = RpcClient(cast(ByteTransport, _SlowFrames(count=8, gap_s=0.03)), read_slice_s=0.05)
    frames = client.request(ping(), 0.1)  # totaal ~0,24 s, maar nooit 0,1 s stil
    assert len(frames) == 8


def test_client_command_ids_are_unique_and_nonzero() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    client = rpc_client(fake)
    ids = {client.request(ping(), 1.0)[0].command_id for _ in range(5)}
    assert len(ids) == 5
    assert 0 not in ids


# --- Infrared ----------------------------------------------------------------------------


def test_ir_send_full_sequence() -> None:
    device, fake = make_device()
    result = device.ir_send(IR_FILE, "Power")
    assert result == {"file": IR_FILE, "button": "Power", "sent": True}
    assert fake.rpc_log == [
        "system_ping_request",
        "app_start_request",
        "app_load_file_request",
        "app_button_press_release_request",
        "app_exit_request",
    ]
    assert fake.sent_buttons == ["Power"]
    assert not fake.app_running


def test_ir_send_handles_event_before_response() -> None:
    fake = FakeFlipperTransport()
    fake.event_before_response = True
    device, _ = make_device(fake)
    assert device.ir_send(IR_FILE, "Vol_up")["sent"] is True
    assert fake.sent_buttons == ["Vol_up"]
    assert not fake.app_running


def test_ir_send_falls_back_to_press_and_release() -> None:
    fake = FakeFlipperTransport()
    fake.press_release_supported = False
    device, _ = make_device(fake)
    device.ir_send(IR_FILE, "Power")
    assert fake.sent_buttons == ["press:Power", "release"]
    assert fake.rpc_log[-2:] == ["app_button_release_request", "app_exit_request"]


def test_ir_send_unknown_button_closes_app() -> None:
    device, fake = make_device()
    with pytest.raises(DeviceError, match="Zap"):
        device.ir_send(IR_FILE, "Zap")
    assert fake.rpc_log[-1] == "app_exit_request"
    assert not fake.app_running
    assert fake.sent_buttons == []


def test_ir_send_load_failure_reports_app_error_text_and_closes_app() -> None:
    device, fake = make_device()
    with pytest.raises(DeviceError, match="Failed to load"):
        device.ir_send("/ext/infrared/weg.ir", "Power")
    assert not fake.app_running


def test_ir_send_app_locked_does_not_try_to_exit() -> None:
    fake = FakeFlipperTransport()
    device, _ = make_device(fake)
    device.system_info()
    fake.set_app_running(True)  # een andere app draait al
    with pytest.raises(RpcError) as exc_info:
        device.ir_send(IR_FILE, "Power")
    assert exc_info.value.status_name == "ERROR_APP_SYSTEM_LOCKED"
    assert "app_exit_request" not in fake.rpc_log
    assert fake.app_running  # niet van ons: laten staan


def test_ir_send_then_cli_command_switches_back() -> None:
    device, fake = make_device()
    device.ir_send(IR_FILE, "Power")
    assert device.rfid_read(0.2)["found"] is True
    assert fake.mode == "cli"


# --- CLI-acties --------------------------------------------------------------------------


def test_subghz_rx_parses_packets_and_stops_with_ctrl_c() -> None:
    device, fake = make_device()
    result = device.subghz_rx(433_920_000, 0.15)
    assert result["frequency_hz"] == 433_920_000
    assert result["tuned_frequency_hz"] == 433_920_000
    assert result["packet_count"] == 2
    assert result["packets"] == [
        "Princeton 24bit\nKey:0x00ABCDEF\nYek:0x00FEDCBA",
        "Nice FLO 12bit\nKey:0x0000012A",
    ]
    assert "Packets received 2" in result["raw"]
    assert fake.etx_count >= 1
    assert fake.idle_at_prompt
    assert "subghz rx 433920000 0" in fake.cli_log


def test_subghz_rx_without_packets() -> None:
    device, _ = make_device(FakeFlipperTransport(subghz_script="subghz_rx_silent.txt"))
    result = device.subghz_rx(315_000_000, 0.1)
    assert result["packets"] == []
    assert result["packet_count"] == 0


def test_subghz_rx_out_of_range_never_reaches_the_cli() -> None:
    device, fake = make_device()
    with pytest.raises(DeviceError, match="buiten de bereiken"):
        device.subghz_rx(350_000_000, 0.1)
    assert not any(c.startswith("subghz") for c in fake.cli_log)


def test_rfid_read_tag_completes_without_ctrl_c() -> None:
    device, fake = make_device()
    result = device.rfid_read(5.0)
    assert result["found"] is True
    assert result["protocol"] == "EM4100"
    assert result["data_hex"] == "1A2B3C4D5E"
    assert "FC: 1A" in result["info"]
    assert fake.etx_count == 0  # commando eindigde zelf; geen Ctrl+C nodig
    assert fake.idle_at_prompt


def test_rfid_read_timeout_without_tag() -> None:
    device, fake = make_device(FakeFlipperTransport(rfid_script="rfid_read_none.txt"))
    result = device.rfid_read(0.15)
    assert result["found"] is False
    assert result["protocol"] is None
    assert fake.etx_count >= 1
    assert fake.idle_at_prompt


def test_nfc_scan_enters_and_leaves_subshell() -> None:
    device, fake = make_device()
    result = device.nfc_scan(5.0)
    assert result["found"] is True
    assert result["protocols"] == [
        ["ISO14443-3A", "ISO14443-4A", "Mifare Plus"],
        ["ISO14443-3A", "Mifare Classic"],
    ]
    assert fake.cli_log[-3:] == ["nfc", "scanner -t", "exit"]
    assert fake.shell == "main"


def test_nfc_scan_timeout_without_tag_still_exits_subshell() -> None:
    device, fake = make_device(FakeFlipperTransport(nfc_script="nfc_scanner_none.txt"))
    result = device.nfc_scan(0.15)
    assert result == {"found": False, "protocols": [], "raw": result["raw"]}
    assert fake.shell == "main"
    assert fake.cli_log[-1] == "exit"


def test_nfc_scan_refused_when_nfc_app_runs() -> None:
    fake = FakeFlipperTransport()
    fake.nfc_app_running = True
    device, _ = make_device(fake)
    with pytest.raises(DeviceError, match="NFC-app draait"):
        device.nfc_scan(1.0)
    assert fake.shell == "main"
    assert "scanner -t" not in fake.cli_log


# --- afbreken ----------------------------------------------------------------------------


def run_in_thread(fn: Any, *args: Any) -> tuple[threading.Thread, list[Any]]:
    outcome: list[Any] = []

    def target() -> None:
        try:
            outcome.append(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - uitkomst voor de test bewaren
            outcome.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def test_abort_during_subghz_rx_sends_ctrl_c_and_raises_aborted() -> None:
    device, fake = make_device()
    thread, outcome = run_in_thread(device.subghz_rx, 433_920_000, 30.0)
    assert fake.cli_started.wait(2.0)
    t0 = time.monotonic()
    device.abort()
    assert time.monotonic() - t0 < 0.2  # abort() blokkeert niet
    thread.join(2.0)
    assert not thread.is_alive()
    assert isinstance(outcome[0], Aborted)
    assert time.monotonic() - t0 < 1.0
    assert fake.etx_count >= 1
    assert fake.idle_at_prompt
    # de volgende call is niet 'besmet' door de oude abort
    assert device.rfid_read(0.2)["found"] is True


def test_abort_also_cancels_queued_operations() -> None:
    device, fake = make_device()
    first, out1 = run_in_thread(device.subghz_rx, 433_920_000, 30.0)
    assert fake.cli_started.wait(2.0)
    second, out2 = run_in_thread(device.rfid_read, 5.0)  # staat in de rij achter de lock
    time.sleep(0.1)
    device.abort()
    first.join(2.0)
    second.join(2.0)
    assert isinstance(out1[0], Aborted)
    assert isinstance(out2[0], Aborted)
    assert "rfid read" not in fake.cli_log


def test_status_is_nonblocking_while_busy() -> None:
    device, fake = make_device()
    thread, outcome = run_in_thread(device.subghz_rx, 433_920_000, 30.0)
    assert fake.cli_started.wait(2.0)
    t0 = time.monotonic()
    status = device.status()
    assert time.monotonic() - t0 < 0.1
    assert status["busy"] is True
    assert status["operation"] == "subghz_rx"
    assert status["mode"] == "cli"
    device.abort()
    thread.join(2.0)
    assert isinstance(outcome[0], Aborted)
    assert device.status()["busy"] is False


def test_abort_during_nfc_scan_leaves_subshell() -> None:
    device, fake = make_device(FakeFlipperTransport(nfc_script="nfc_scanner_none.txt"))
    thread, outcome = run_in_thread(device.nfc_scan, 30.0)
    assert fake.cli_started.wait(2.0)
    device.abort()
    thread.join(2.0)
    assert isinstance(outcome[0], Aborted)
    assert fake.shell == "main"


def test_abort_during_ir_send_still_closes_app() -> None:
    device, fake = make_device()
    started = threading.Event()
    original = fake._rpc_app_load_file_request

    def slow_load(request: Any) -> None:
        started.set()
        time.sleep(0.2)  # abort() valt hier tussen
        original(request)

    fake._rpc_app_load_file_request = slow_load  # type: ignore[method-assign]
    thread, outcome = run_in_thread(device.ir_send, IR_FILE, "Power")
    assert started.wait(2.0)
    # slow_load draait binnen fake.write() in de device-thread; abort() blokkeert niet
    device.abort()
    thread.join(3.0)
    assert isinstance(outcome[0], Aborted)
    assert not fake.app_running
    assert "app_exit_request" in fake.rpc_log
    assert fake.sent_buttons == []


# --- time-outs en fouten -----------------------------------------------------------------


def test_unresponsive_device_gives_device_timeout() -> None:
    fake = FakeFlipperTransport()
    fake.open()
    fake.responsive = False
    device, _ = make_device(fake, timing=replace(FAST_TIMING, prompt_timeout_s=0.1))
    with pytest.raises(DeviceTimeout, match="CLI"):
        device.connect()
    assert device.status()["mode"] == "unknown"


def test_cli_command_that_ignores_ctrl_c_times_out() -> None:
    fake = FakeFlipperTransport()
    fake.ignore_etx = True
    device, _ = make_device(fake, timing=replace(FAST_TIMING, stop_grace_s=0.2))
    with pytest.raises(DeviceTimeout, match="subghz rx"):
        device.subghz_rx(433_920_000, 0.1)
    assert device.status()["mode"] == "unknown"


def test_rpc_timeout_resets_mode() -> None:
    device, fake = make_device()
    device.system_info()
    fake.responsive = False
    with pytest.raises(DeviceTimeout):
        device.storage_list("/ext")
    assert device.status()["mode"] == "unknown"
    fake.responsive = True
    assert device.storage_list("/ext") == []  # herstelt via CLI-sync + nieuwe sessie


def test_open_failure_is_device_not_connected() -> None:
    class Unplugged(FakeFlipperTransport):
        def open(self) -> None:
            raise DeviceNotConnected("niet aangesloten")

    device = FlipperDevice(Unplugged)
    with pytest.raises(DeviceNotConnected):
        device.system_info()
    assert device.status()["connected"] is False


def test_reconnects_once_on_transport_error() -> None:
    device, fake = make_device(profile="official")
    device.connect()
    fake.fail_next_write = True
    result = device.rfid_read(0.2)
    assert result["found"] is True
    assert fake.opens == 2
    assert fake.closes == 1


def test_no_retry_after_transmit_started() -> None:
    device, fake = make_device()
    device.system_info()
    fake.fail_on_rpc_kind = "app_button_press_release_request"
    with pytest.raises(DeviceError, match="viel weg"):
        device.ir_send(IR_FILE, "Power")
    assert fake.sent_buttons == []  # niet automatisch opnieuw verstuurd
    assert fake.opens == 1


def test_close_drops_connection_and_allows_reconnect() -> None:
    device, fake = make_device()
    device.system_info()
    device.close()
    status = device.status()
    assert status["connected"] is False
    assert status["mode"] == "unknown"
    assert device.system_info()["hardware_name"] == "Flippy"
    assert fake.opens == 2


# --- CLI-profiel en parsers --------------------------------------------------------------


def crlf(name: str) -> str:
    return fixture_text(name).replace("\n", "\r\n")


def test_select_profile_rules() -> None:
    official = fixture_json("device_info.json")
    assert cli.select_profile("auto", official).name == "official"
    assert cli.select_profile("OFFICIAL", None).name == "official"
    with pytest.raises(UnsupportedFirmware, match="Unleashed"):
        cli.select_profile("auto", {"firmware_origin_fork": "Unleashed"})
    with pytest.raises(UnsupportedFirmware, match="onbekend"):
        cli.select_profile("auto", {})


def test_subghz_command_is_built_from_validated_ints_only() -> None:
    profile = cli.PROFILES["official"]
    assert profile.subghz_rx_command(433_920_000) == "subghz rx 433920000 0"
    for bad in (True, "433920000; reboot", 433.92e6):
        with pytest.raises(DeviceError):
            profile.subghz_rx_command(bad)  # type: ignore[arg-type]
    for freq in (0, 299_999_754, 348_000_001, 386_999_937, 464_000_001, 928_000_001):
        with pytest.raises(DeviceError, match="buiten"):
            profile.subghz_rx_command(freq)
    for freq in (299_999_755, 348_000_000, 386_999_938, 464_000_000, 778_999_847, 928_000_000):
        assert profile.subghz_rx_command(freq).startswith("subghz rx ")


def test_parse_subghz_rx_fixture() -> None:
    text = crlf("subghz_rx_session.txt").replace("---ETX---\r\n", "")
    parsed = cli.parse_subghz_rx(text)
    assert parsed.packet_count == 2
    assert len(parsed.packets) == 2
    assert "." not in parsed.packets[0].split("\n")[0]  # reset-punten verwijderd


def test_parse_subghz_rx_firmware_error_and_garbage() -> None:
    err = (
        f"Frequency must be in {cli.format_ranges(cli.OFFICIAL_SUBGHZ_RANGES_HZ)} range, not 1\r\n"
    )
    with pytest.raises(DeviceError, match="Frequency must be in"):
        cli.parse_subghz_rx(err)
    with pytest.raises(DeviceError, match="onverwachte"):
        cli.parse_subghz_rx("iets heel anders\r\n")


def test_parse_rfid_variants() -> None:
    tag = cli.parse_rfid_read(crlf("rfid_read_tag.txt"))
    assert (tag.found, tag.protocol, tag.data_hex) == (True, "EM4100", "1A2B3C4D5E")
    none = cli.parse_rfid_read(crlf("rfid_read_none.txt").replace("---ETX---\r\n", ""))
    assert none.found is False
    spaced = cli.parse_rfid_read("Press Ctrl+C to abort\r\nIO Prox 0102\r\nReading stopped\r\n")
    assert spaced.protocol == "IO Prox"
    with pytest.raises(DeviceError, match="onverwachte"):
        cli.parse_rfid_read("Usage: rfid read\r\n")


def test_parse_nfc_variants() -> None:
    tag = cli.parse_nfc_scanner(crlf("nfc_scanner_tag.txt"))
    assert tag.protocols[1] == ["ISO14443-3A", "Mifare Classic"]
    none = cli.parse_nfc_scanner(crlf("nfc_scanner_none.txt").replace("---ETX---\r\n", ""))
    assert none.found is False
    with pytest.raises(DeviceError):
        cli.parse_nfc_scanner("niks\r\n")


def test_client_event_queue_overflow_is_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeFlipperTransport(start_in_rpc=True)
    fake.open()
    client = RpcClient(fake, read_slice_s=0.01, max_events=2)
    for i in range(3):
        event = flipper_pb2.Main()
        event.command_id = 0
        event.system_ping_response.data = bytes([i])
        fake.queue_extra_frame(event)
    with caplog.at_level("WARNING"):
        client.request(ping(), 1.0)
    assert client.dropped_events == 1
    assert [e.system_ping_response.data for e in client.events] == [b"\x01", b"\x02"]
    assert "eventqueue vol" in caplog.text
