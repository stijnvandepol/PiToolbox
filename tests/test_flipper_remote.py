"""Ontwikkelaarsmodus op afstand: `gui_press`/`gui_screen` op FlipperDevice, de wachter, het
schermbeeld als tekst en PNG, en dat niets in het product deze methoden kan bereiken."""

from __future__ import annotations

import inspect
import logging
import re
import struct
import threading
import time
import zlib
from dataclasses import replace
from pathlib import Path

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport

from pitoolbox.core.devices._flipper_rpc.pb import gui_pb2
from pitoolbox.core.devices._flipper_screen import (
    FRAME_BYTES,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
    decode_frame,
    render_png,
    render_text,
)
from pitoolbox.core.devices.flipper import (
    REMOTE_KEYS,
    DeviceError,
    FlipperDevice,
    RemoteInputRefused,
)
from pitoolbox.core.errors import Aborted
from pitoolbox.core.tools import build_registry

FAP = "/ext/apps/Tools/pitoolbox_ui.fap"
TIMING = replace(FAST_TIMING, loader_free_wait_s=0.2, loader_poll_s=0.02)
SRC = Path(__file__).resolve().parents[1] / "src" / "pitoolbox"
CLICK_OK = [("OK", "PRESS"), ("OK", "SHORT"), ("OK", "RELEASE")]


def make(*, ble: bool = True) -> tuple[FlipperDevice, FakeFlipperTransport, FakeFap]:
    fake = FakeFlipperTransport(start_in_rpc=ble, kind="ble" if ble else "usb")
    fap = fake.attach_fap(FakeFap())
    return FlipperDevice(lambda: fake, timing=TIMING), fake, fap


def frame_with(pixels: set[tuple[int, int]]) -> bytes:
    """Framebuffer met precies deze (x, y) donker: byte x + (y // 8) * 128, bit y % 8."""
    data = bytearray(FRAME_BYTES)
    for x, y in pixels:
        data[x + (y // 8) * SCREEN_WIDTH] |= 1 << (y % 8)
    return bytes(data)


# --- gui_press: volgorde en wachter ---------------------------------------------------------


@pytest.mark.parametrize("ble", [True, False])
def test_click_is_press_short_release_on_a_free_loader(
    ble: bool, caplog: pytest.LogCaptureFixture
) -> None:
    device, fake, _ = make(ble=ble)
    with caplog.at_level(logging.WARNING, logger="pitoolbox.core.devices.flipper"):
        device.gui_press("ok")
        device.gui_press("back", long=True)
    assert fake.gui_inputs == [*CLICK_OK, ("BACK", "PRESS"), ("BACK", "LONG"), ("BACK", "RELEASE")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "invoer op afstand: OK (geen app open)",
        "invoer op afstand: BACK (lang) (geen app open)",
    ]
    # Vóór elke klik eerst de loader gevraagd.
    assert fake.rpc_log.count("app_lock_status_request") == 2


def test_press_allowed_in_our_own_app() -> None:
    device, fake, fap = make()
    device.start_rpc_app(FAP)
    device.gui_press("down")
    assert fake.gui_inputs == [("DOWN", "PRESS"), ("DOWN", "SHORT"), ("DOWN", "RELEASE")]
    assert fap.keys == ["DOWN"]


def test_press_refused_when_another_app_is_open(caplog: pytest.LogCaptureFixture) -> None:
    device, fake, _ = make()
    fake.user_app_open = True
    with caplog.at_level(logging.WARNING), pytest.raises(RemoteInputRefused, match="Sub-GHz"):
        device.gui_press("ok")
    assert fake.gui_inputs == []
    assert "invoer op afstand" not in caplog.text


def test_guard_is_checked_before_every_press() -> None:
    device, fake, _ = make()
    device.gui_press("ok")
    fake.user_app_open = True  # de gebruiker opent intussen een app
    with pytest.raises(RemoteInputRefused):
        device.gui_press("ok")
    assert fake.gui_inputs == CLICK_OK


def test_our_app_closed_then_other_app_opened_is_refused() -> None:
    """Onze app sluit (APP_CLOSED staat nog ongelezen in de stroom) en de gebruiker opent iets
    anders: de wachter verwerkt eerst wat er binnen is en ziet dan een vreemde app."""
    device, fake, _ = make()
    device.start_rpc_app(FAP)
    fake.fap_exits()
    fake.user_app_open = True
    assert device.rpc_app_state == "active"  # nog niets gelezen
    with pytest.raises(RemoteInputRefused):
        device.gui_press("ok")
    assert device.rpc_app_state == "off"
    assert fake.gui_inputs == []


def test_unknown_key_is_refused_without_io() -> None:
    device, fake, _ = make()
    with pytest.raises(ValueError, match="onbekende knop"):
        device.gui_press("select")
    assert fake.opens == 0


def test_remote_keys_are_the_six_buttons() -> None:
    names = {gui_pb2.InputKey.Name(v) for v in REMOTE_KEYS.values()}
    assert names == {"UP", "DOWN", "LEFT", "RIGHT", "OK", "BACK"}


# --- gui_screen ---------------------------------------------------------------------------


def test_screen_returns_one_frame_and_stops_the_stream() -> None:
    device, fake, _ = make()
    fake.set_screen(frame_with({(0, 0), (127, 63)}))
    frame = device.gui_screen()
    assert frame.orientation == "HORIZONTAL"
    grid = frame.pixels()
    assert grid[0][0] and grid[63][127] and not grid[0][1]
    assert fake.stream_requests == ["start", "stop"] and not fake.screen_streaming


def test_screen_timeout_still_stops_the_stream() -> None:
    device, fake, _ = make()
    fake.redraw_on_stream_start = False
    with pytest.raises(DeviceError, match="geen schermbeeld"):
        device.gui_screen(timeout_s=0.2)
    assert fake.stream_requests == ["start", "stop"] and not fake.screen_streaming
    assert fake.is_open  # geen DeviceTimeout: de BLE-link blijft


def test_screen_abort_still_stops_the_stream() -> None:
    device, fake, _ = make()
    fake.redraw_on_stream_start = False
    timer = threading.Timer(0.1, device.abort)
    timer.start()
    try:
        with pytest.raises(Aborted):
            device.gui_screen(timeout_s=5.0)
    finally:
        timer.cancel()
    assert fake.stream_requests == ["start", "stop"] and not fake.screen_streaming


def test_screen_wrong_size_is_an_error_after_stopping() -> None:
    device, fake, _ = make()
    fake.screen = b"\x00" * 10
    with pytest.raises(DeviceError, match="10 bytes"):
        device.gui_screen()
    assert not fake.screen_streaming


def test_screen_restarts_a_stream_that_was_still_running() -> None:
    device, fake, _ = make()
    device.ping()
    fake.screen_streaming = True  # een eerdere stop kwam niet aan
    fake.set_screen(frame_with({(5, 9)}))
    assert device.gui_screen().pixels()[9][5]
    assert fake.stream_requests == ["start", "stop", "start", "stop"]


def test_late_frames_never_reach_the_event_queue_or_the_app_sink() -> None:
    device, fake, _ = make()
    events: list[object] = []
    device.set_app_sink(events.append)
    device.start_rpc_app(FAP)
    device.gui_screen()
    fake.screen_streaming = True  # de stream liep toch door
    fake.set_screen(frame_with({(1, 1)}))
    assert device.poll_app_events(0.1)
    client = device._client
    assert client is not None and client.events == []
    assert all(getattr(e, "kind", "") == "data" for e in events)  # alleen HELLO van de app


def test_screen_while_app_is_linked_keeps_app_messages_flowing() -> None:
    device, fake, fap = make()
    received: list[object] = []
    device.set_app_sink(received.append)
    fap.auto_hello = False  # alleen de HELLO hieronder
    device.start_rpc_app(FAP)
    fake.redraw_on_stream_start = False

    def app_talks_then_screen_redraws() -> None:
        time.sleep(0.05)
        fap.send(fap.hello())
        fake.set_screen(frame_with({(2, 3)}))

    thread = threading.Thread(target=app_talks_then_screen_redraws)
    thread.start()
    frame = device.gui_screen(timeout_s=2.0)
    thread.join()
    assert frame.pixels()[3][2]
    assert len(received) == 1  # de HELLO kwam tijdens het wachten bij de sink


# --- schermbeeld: decoderen, tekst, PNG ---------------------------------------------------


def test_decode_follows_the_u8g2_page_layout() -> None:
    data = bytearray(FRAME_BYTES)
    data[0] = 0b0000_0001  # (0, 0)
    data[0 + 1 * 128] = 0b1000_0000  # x 0, pagina 1, bit 7 -> (0, 15)
    data[127 + 7 * 128] = 0b1000_0000  # (127, 63)
    data[10] = 0b0000_0100  # (10, 2)
    grid = decode_frame(bytes(data))
    assert len(grid) == SCREEN_HEIGHT and all(len(row) == SCREEN_WIDTH for row in grid)
    on = {(x, y) for y, row in enumerate(grid) for x, value in enumerate(row) if value}
    assert on == {(0, 0), (0, 15), (127, 63), (10, 2)}


def test_decode_refuses_wrong_size() -> None:
    with pytest.raises(ValueError, match="1023 bytes"):
        decode_frame(bytes(FRAME_BYTES - 1))


def test_text_uses_half_blocks_two_rows_per_line() -> None:
    grid = decode_frame(frame_with({(0, 0), (1, 1), (2, 0), (2, 1)}))
    lines = render_text(grid).splitlines()
    assert len(lines) == SCREEN_HEIGHT // 2 + 2
    assert all(len(line) == SCREEN_WIDTH + 2 for line in lines)
    assert lines[1].startswith("|▀▄█ ")


def _png_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, pos = [], 8
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, body = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks.append((kind, body))
        pos += 12 + length
    return chunks


@pytest.mark.parametrize("scale", [1, 3])
def test_png_is_valid_and_has_the_pixels(scale: int) -> None:
    grid = decode_frame(frame_with({(0, 0), (127, 63)}))
    chunks = _png_chunks(render_png(grid, scale))
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, color, *_ = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (width, height, depth, color) == (128 * scale, 64 * scale, 8, 0)
    raw = zlib.decompress(chunks[1][1])
    stride = width + 1
    assert len(raw) == stride * height
    rows = [raw[i * stride : (i + 1) * stride] for i in range(height)]
    assert all(row[0] == 0 for row in rows)  # filter 0
    assert rows[0][1] == 0x00 and rows[0][1 + scale] == 0xFF  # (0, 0) donker, (1, 0) licht
    assert rows[-1][-1] == 0x00 and rows[-1][-1 - scale] == 0xFF


def test_png_scale_is_bounded() -> None:
    grid = decode_frame(bytes(FRAME_BYTES))
    with pytest.raises(ValueError, match="schaal"):
        render_png(grid, 0)


# --- nooit bereikbaar vanuit het product ----------------------------------------------------
#
# Met invoer op afstand kan de Pi zelf OK drukken op een "Flipper verplicht"-vraag. Dat mag
# alleen in de testopzet van ui-test (allowlist `status`, lege scope: niets kan uitzenden), nooit
# via een tool, de runner, MCP, de agent of de bridge.

_REMOTE_RE = re.compile(r"\b(gui_press|gui_screen)\b")
# De enige plekken die ze mogen noemen: de devicelaag zelf en de ontwikkelaars-CLI.
_ALLOWED = {
    "core/devices/_flipper_remote.py",
    "core/devices/flipper.py",
    "flipper_remote_cli.py",
    "flipper_ui_smoke.py",
}
_FORBIDDEN = ("core/tools/", "frontends/", "agent/", "llm/", "auth/", "app.py")


def test_remote_methods_are_only_named_in_the_dev_harness() -> None:
    users = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if _REMOTE_RE.search(path.read_text(encoding="utf-8"))
    }
    assert users <= _ALLOWED
    assert not [u for u in users if u.startswith(_FORBIDDEN)]
    assert "core/devices/_flipper_remote.py" in users  # de scan vindt ze wel


def test_no_tool_spec_or_handler_reaches_remote_input() -> None:
    for spec in build_registry().all():
        assert not _REMOTE_RE.search(spec.name)
        assert not _REMOTE_RE.search(inspect.getsource(spec.handler)), spec.name
