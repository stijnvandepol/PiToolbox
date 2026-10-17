"""FlipperDevice: RPC-gekoppelde app (PiToolbox-FAP), event-hook en handoff voor ir_send."""

from __future__ import annotations

import calendar
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport, wait_until

from pitoolbox.core.devices._flipper_rpc.client import RpcClient
from pitoolbox.core.devices._flipper_rpc.framing import encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2
from pitoolbox.core.devices.flipper import (
    AppEvent,
    AppLocked,
    AppNotRunning,
    AppRejected,
    DeviceError,
    FlipperDevice,
)
from pitoolbox.frontends.flipper_ui import protocol as proto

FAP = "/ext/apps/Tools/pitoolbox_ui.fap"
TIMING = replace(FAST_TIMING, loader_free_wait_s=0.2, loader_poll_s=0.02)


class Sink:
    def __init__(self) -> None:
        self.events: list[AppEvent] = []
        self.lock = threading.Lock()

    def __call__(self, event: AppEvent) -> None:
        with self.lock:
            self.events.append(event)

    def kinds(self) -> list[str]:
        with self.lock:
            return [e.kind for e in self.events]

    def data(self) -> list[proto.FapMessage]:
        with self.lock:
            return [proto.decode_from_fap(e.data) for e in self.events if e.kind == "data"]


def make(
    *, ble: bool = True, auto_hello: bool = True, keepalive_s: float = 0.0
) -> tuple[FlipperDevice, FakeFlipperTransport, FakeFap, Sink]:
    fake = FakeFlipperTransport(start_in_rpc=ble, kind="ble" if ble else "usb")
    fap = fake.attach_fap(FakeFap(auto_hello=auto_hello))
    device = FlipperDevice(lambda: fake, timing=TIMING, keepalive_s=keepalive_s)
    sink = Sink()
    device.set_app_sink(sink)
    return device, fake, fap, sink


def _hello_data(instance: int = 7) -> bytes:
    return proto.encode(
        proto.Hello(seq=1, app_major=1, app_minor=0, proto_max=1, instance=instance, max_rx=512)
    )


# --- RpcClient: event-hook en pump ---------------------------------------------------------


def _frame(main: Any) -> bytes:
    return encode_frame(main.SerializeToString())


def _exchange_frame(data: bytes) -> Any:
    main = flipper_pb2.Main()
    main.command_id = 0
    main.app_data_exchange_request.data = data
    return main


def test_event_hook_takes_frames_before_the_queue() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True, kind="ble")
    fake.open()
    taken: list[Any] = []

    def hook(frame: Any) -> bool:
        if frame.WhichOneof("content") == "app_data_exchange_request":
            taken.append(bytes(frame.app_data_exchange_request.data))
            return True
        if frame.WhichOneof("content") == "app_state_response":
            raise RuntimeError("kapotte hook")
        return False

    client = RpcClient(fake, read_slice_s=0.01, event_hook=hook)
    state = flipper_pb2.Main()
    state.app_state_response.state = application_pb2.APP_CLOSED
    fake.queue_extra_frame(_exchange_frame(b"abc"))
    fake.queue_extra_frame(state)
    ping = flipper_pb2.Main()
    ping.system_ping_request.data = b"x"
    client.request(ping, 1.0)
    assert taken == [b"abc"]
    assert [e.WhichOneof("content") for e in client.events] == ["app_state_response"]


def test_pump_reads_unsolicited_frames_and_stops_early() -> None:
    fake = FakeFlipperTransport(start_in_rpc=True, kind="ble")
    fake.open()
    taken: list[bytes] = []
    client = RpcClient(
        fake,
        read_slice_s=0.01,
        event_hook=lambda f: bool(taken.append(bytes(f.app_data_exchange_request.data)) or True),
    )
    with fake._cond:
        fake._emit(_frame(_exchange_frame(b"1")) + _frame(_exchange_frame(b"2")))
    assert client.pump(0.1) == 2
    assert taken == [b"1", b"2"]
    t0 = time.monotonic()
    client.pump(5.0, stop=lambda: True)
    assert time.monotonic() - t0 < 0.5


# --- starten en berichten -----------------------------------------------------------------------


def test_start_rpc_app_delivers_hello_to_sink() -> None:
    device, fake, fap, sink = make()
    device.start_rpc_app(FAP)
    assert device.rpc_app_state == "active"
    assert device.status()["ui_app"] == "active"
    assert fake.app_starts == [FAP]
    assert wait_until(lambda: device.poll_app_events(0.05) or bool(sink.data()))
    (hello,) = sink.data()
    assert isinstance(hello, proto.Hello) and hello.instance == fap.instance
    assert device._client is not None and device._client.events == []


def test_start_rpc_app_never_kicks_a_user_app() -> None:
    device, fake, _fap, _sink = make()
    fake.user_app_open = True
    with pytest.raises(AppLocked):
        device.start_rpc_app(FAP)
    assert fake.app_starts == []
    assert "app_exit_request" not in fake.rpc_log
    assert "gui_send_input_event_request" not in fake.rpc_log
    assert device.rpc_app_state == "off"


@pytest.mark.parametrize(
    "path", ["/ext/apps/Tools/../x.fap", "/int/x.fap", "Infrared", "/ext/apps/x.fap"]
)
def test_start_rpc_app_rejects_odd_paths(path: str) -> None:
    device, fake, _fap, _sink = make()
    with pytest.raises(ValueError):
        device.start_rpc_app(path)
    assert fake.app_starts == []


def test_app_send_ok_rejected_and_not_running() -> None:
    device, fake, fap, sink = make()
    device.start_rpc_app(FAP)
    data = proto.encode(proto.Error(seq=1, ref=0, code=proto.ErrorCode.BUSY, text="x"))
    device.app_send(data)
    assert isinstance(fap.received[-1], proto.Error)
    fap.reject_next = 1
    with pytest.raises(AppRejected):
        device.app_send(data)
    fake.fap_exits()
    with pytest.raises(AppNotRunning):
        device.app_send(data)
    assert device.rpc_app_state == "off"
    assert "closed" in sink.kinds()


@pytest.mark.parametrize("ble", [True, False])
def test_unacknowledged_message_closes_our_app_but_keeps_the_link(ble: bool) -> None:
    """Geen bevestiging binnen de time-out: de app hangt of is weg. Het device sluit zijn eigen
    app en meldt pas dan `lost`; de link blijft (BLE sloot eerst de hele link, USB liet de app
    open staan zodat de volgende start op onze eigen app AppLocked kreeg)."""
    device, fake, fap, sink = make(ble=ble)
    device.start_rpc_app(FAP)
    opens = fake.opens
    fap.hang = True
    with pytest.raises(AppNotRunning, match="gesloten"):
        device.app_send(proto.encode(proto.Presets(seq=2, items=())), timeout_s=0.2)
    assert "app_exit_request" in fake.rpc_log
    assert not fake.fap_running
    assert sink.kinds()[-1] == "lost"
    assert device.rpc_app_state == "off"
    assert device.status()["connected"] is True and fake.opens == opens
    fap.hang = False
    device.start_rpc_app(FAP)
    assert fap.starts == 2 and fake.opens == opens


def test_late_answer_to_timed_out_message_is_harmless() -> None:
    """De bevestiging komt pas binnen als de Pi al opgegeven heeft (vóór het antwoord op
    app_exit): sluiten en latere verzoeken lopen gewoon door."""
    device, fake, fap, _sink = make()
    fake.files = {"/ext/a.txt": b"abc"}
    device.start_rpc_app(FAP)
    original = fake._rpc_app_data_exchange_request

    def answer_late(request: Any) -> None:
        original(request)  # fap.hang: nu geen antwoord
        fake.queue_extra_frame(fake._ok(request))  # pas vóór het volgende antwoord

    fake._rpc_app_data_exchange_request = answer_late  # type: ignore[method-assign]
    fap.hang = True
    with pytest.raises(AppNotRunning):
        device.app_send(proto.encode(proto.Presets(seq=2, items=())), timeout_s=0.2)
    assert not fake.fap_running and fake.extra_frames == []  # het late antwoord is gelezen
    assert device.storage_read("/ext/a.txt", 10) == b"abc"
    fap.hang = False
    device.start_rpc_app(FAP)
    device.app_send(proto.encode(proto.Presets(seq=3, items=())))
    assert isinstance(fap.received[-1], proto.Presets) and fap.received[-1].seq == 3


def test_abort_does_not_break_app_messages() -> None:
    device, _fake, fap, _sink = make()
    device.start_rpc_app(FAP)
    device.abort()  # noodstop: berichten van de bridge zijn niet afbreekbaar
    device.app_send(proto.encode(proto.Presets(seq=2, items=())))
    assert isinstance(fap.received[-1], proto.Presets)


def test_app_closing_itself_is_reported_once() -> None:
    device, fake, _fap, sink = make()
    device.start_rpc_app(FAP)
    fake.fap_exits()
    assert wait_until(lambda: device.poll_app_events(0.02) and "closed" in sink.kinds())
    assert device.rpc_app_state == "off"
    assert sink.kinds().count("closed") == 1


def test_link_loss_is_reported_as_lost() -> None:
    device, fake, _fap, sink = make()
    device.start_rpc_app(FAP)
    fake.close()  # BLE weg
    assert device.poll_app_events(0.02) is False
    assert sink.kinds()[-1] == "lost"
    assert device.rpc_app_state == "off"


def test_app_data_during_tool_request_goes_to_sink() -> None:
    device, fake, _fap, sink = make(auto_hello=False)
    fake.files = {"/ext/a.txt": b"x" * 40}
    device.start_rpc_app(FAP)
    fake.queue_extra_frame(_exchange_frame(_hello_data()))
    assert device.storage_read("/ext/a.txt", 100) == b"x" * 40
    assert [type(m) for m in sink.data()] == [proto.Hello]
    assert device._client is not None and device._client.events == []


def test_poll_yields_to_waiting_tool_call() -> None:
    device, fake, _fap, _sink = make()
    fake.files = {"/ext/a.txt": b"abc"}
    device.start_rpc_app(FAP)
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            if not device.poll_app_events(0.5):
                time.sleep(0.001)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        time.sleep(0.05)
        t0 = time.monotonic()
        assert device.storage_read("/ext/a.txt", 10) == b"abc"
        assert time.monotonic() - t0 < 0.3  # niet een hele poll-slice (0,5 s) wachten
    finally:
        stop.set()
        thread.join()


@contextmanager
def bridge_reader(device: FlipperDevice, slice_s: float) -> Iterator[None]:
    """Leest zoals de lezer van de bridge: direct opnieuw pollen zolang er iets te lezen viel."""
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            if not device.poll_app_events(slice_s):
                time.sleep(0.001)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def test_keepalive_keeps_pinging_while_the_reader_polls() -> None:
    """De keepalive-thread wacht nooit op de lock; met een continu lezende bridge kwam hij
    er dus (bijna) nooit aan (gemeten: 0 pings in 3 s). Nu pingt de lezer zelf."""
    device, fake, fap, _sink = make(keepalive_s=0.1)
    device.start_rpc_app(FAP)

    def pings() -> int:
        return fake.rpc_log.count("system_ping_request")

    try:
        with bridge_reader(device, 0.2):
            before = pings()
            # Verkeer van de app telt niet als verkeer: de ping gaat ook tijdens HELLO's door.
            for _ in range(5):
                fap.send(fap.hello())
                time.sleep(0.1)
            assert wait_until(lambda: pings() >= before + 3, timeout=2.0)
        assert device.rpc_app_state == "active"
    finally:
        device.close()


def test_cli_actions_refused_while_app_open() -> None:
    device, fake, _fap, _sink = make(ble=False)  # USB: CLI bestaat, maar de app houdt de loader
    device.start_rpc_app(FAP)
    with pytest.raises(AppLocked):
        device.subghz_rx(433_920_000, 1.0)
    assert fake.fap_running
    assert device.rpc_app_state == "active"


def test_exit_rpc_app() -> None:
    device, fake, _fap, sink = make()
    device.start_rpc_app(FAP)
    device.exit_rpc_app()
    assert not fake.fap_running and device.rpc_app_state == "off"
    # HELLO, dan "closed": bewust gesloten, geen "lost" (dat zou de bridge laten herstarten).
    assert sink.kinds() == ["data", "closed"]
    device.exit_rpc_app()  # nog eens: niets
    assert sink.kinds() == ["data", "closed"]


def test_app_without_started_event_is_closed_again() -> None:
    device, fake, _fap, _sink = make(auto_hello=False)
    fake.drop_app_started = True
    with pytest.raises(DeviceError, match="gestart"):
        device.start_rpc_app(FAP)
    assert device.rpc_app_state == "off"
    assert not fake.fap_running  # loader niet bezet gelaten
    assert "app_exit_request" in fake.rpc_log


# --- handoff voor ir_send ---------------------------------------------------------------------


def test_ir_send_hands_loader_over_and_restarts_app() -> None:
    device, fake, fap, sink = make()
    device.start_rpc_app(FAP)
    first = fap.instance
    result = device.ir_send("/ext/infrared/tv.ir", "Power")
    assert result["sent"] is True
    assert fake.sent_buttons == ["Power"]
    assert fake.app_starts == [FAP, "Infrared", FAP]
    assert fap.starts == 2 and fap.instance != first
    assert device.rpc_app_state == "active" and fake.fap_running
    assert wait_until(lambda: device.poll_app_events(0.02) or len(sink.data()) == 2)
    hellos = [m for m in sink.data() if isinstance(m, proto.Hello)]
    assert [h.instance for h in hellos] == [first, fap.instance]
    assert "closed" not in sink.kinds() and "lost" not in sink.kinds()


def test_ir_send_failed_restart_reports_lost() -> None:
    device, fake, fap, sink = make()
    device.start_rpc_app(FAP)
    original = fake._rpc_app_exit_request

    def exit_then_block(request: Any) -> None:
        original(request)
        if fake._running_app is None and "Infrared" in fake.app_starts:
            fake.user_app_open = True  # de gebruiker opent iets na de Infrared-app

    fake._rpc_app_exit_request = exit_then_block  # type: ignore[method-assign]
    device.ir_send("/ext/infrared/tv.ir", "Power")
    assert fake.sent_buttons == ["Power"]
    assert device.rpc_app_state == "off"
    assert sink.kinds()[-1] == "lost"
    assert fap.starts == 1


def test_ir_send_without_app_is_unchanged() -> None:
    device, fake, _fap, sink = make()
    device.ir_send("/ext/infrared/tv.ir", "Power")
    assert fake.app_starts == ["Infrared"]
    assert sink.kinds() == []


# --- kleine opslag- en klokacties ---------------------------------------------------------------


def test_take_small_file_reads_and_deletes() -> None:
    device, fake, _fap, _sink = make()
    fake.files = {"/ext/r": b"PTBUI 1 5\n", "/ext/big": b"x" * 100}
    assert device.take_small_file("/ext/r", 64) == b"PTBUI 1 5\n"
    assert "/ext/r" not in fake.files
    assert device.take_small_file("/ext/r", 64) is None
    assert device.take_small_file("/ext/big", 64) is None
    assert "/ext/big" not in fake.files  # te groot: toch gewist


def test_rtc_timestamp_matches_flipper_clock() -> None:
    device, fake, _fap, _sink = make()
    fake.rtc_epoch = calendar.timegm((2026, 10, 16, 12, 34, 56, 0, 0, 0))
    assert device.rtc_timestamp() == fake.rtc_epoch


def test_app_lock_status_idle_only_skips_when_busy() -> None:
    from pitoolbox.core.devices.flipper import DeviceBusy

    device, fake, _fap, _sink = make()
    assert device.app_lock_status() is False
    fake.user_app_open = True
    assert device.app_lock_status(idle_only=True) is True
    with device._lock:
        device._busy_op = "iets"
        with pytest.raises(DeviceBusy):
            device.app_lock_status(idle_only=True)
        device._busy_op = None
