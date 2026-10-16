"""Simulator van een Flipper Zero achter een `ByteTransport` voor de Flipper-tests.

Gedrag nagebootst uit de firmwarebronnen (flipperzero-firmware 3fb1d517):
* CLI met echo en prompt "\\r\\n>: " (cli_shell_line.c), Ctrl+C-afhandeling, NFC-subshell met
  prompt "[<groen>nfc<reset>]>: " en `exit` (nfc_cli.c, cli_shell.c).
* `start_rpc_session` schakelt naar nanopb-delimited RPC (rpc_cli.c); `stop_session` geeft een
  OK-frame en daarna weer de CLI-prompt (rpc.c).
* app_start/app_exit sturen een ongevraagde `app_state_response` met command_id 0
  (rpc_app.c), de Infrared-app bevestigt load/press met OK of ERROR_APP_CMD_ERROR
  (infrared_scene_rpc.c).

De uitvoer van subghz/rfid/nfc komt uit tests/fixtures/flipper/*.txt (zelf geschreven op basis
van de printf-formaten; de decoder-tekst van Sub-GHz is synthetisch).
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from pitoolbox.core.devices._flipper_rpc.framing import FrameDecoder, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2, storage_pb2
from pitoolbox.core.devices.flipper import FlipperTiming, TransportError

FIXTURES = Path(__file__).parent / "fixtures" / "flipper"
ETX_MARKER = "---ETX---"
MAIN_PROMPT = "\r\n>: "
NFC_PROMPT = "\r\n[\x1b[32mnfc\x1b[0m]>: "
FREQ_RANGE_STR = "299999755...348000000 or 386999938...464000000 or 778999847...928000000"

# Korte wachttijden zodat de tests snel blijven.
FAST_TIMING = FlipperTiming(
    prompt_timeout_s=0.5,
    rpc_timeout_s=1.0,
    probe_timeout_s=0.4,
    settle_quiet_s=0.02,
    settle_max_s=0.2,
    read_slice_s=0.01,
    stop_grace_s=1.0,
    command_timeout_s=1.0,
    app_event_timeout_s=0.5,
    app_exit_timeout_s=0.5,
    probe_attempts=2,
)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict[str, str]:
    data: dict[str, str] = json.loads(fixture_text(name))
    return data


def _crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def split_script(name: str) -> tuple[str, str]:
    """(uitvoer bij start, uitvoer na Ctrl+C). Zonder marker: alles bij start, direct klaar."""
    text = fixture_text(name)
    if ETX_MARKER not in text:
        return _crlf(text), ""
    pre, post = text.split(ETX_MARKER + "\n", 1)
    return _crlf(pre), _crlf(post)


class FakeFlipperTransport:
    """Eén fysiek apparaat; `open()` na `close()` start een verse CLI-shell (DTR-gedrag)."""

    def __init__(
        self,
        *,
        device_info: dict[str, str] | None = None,
        files: dict[str, bytes] | None = None,
        ir_buttons: dict[str, list[str]] | None = None,
        subghz_script: str = "subghz_rx_session.txt",
        rfid_script: str = "rfid_read_tag.txt",
        nfc_script: str = "nfc_scanner_tag.txt",
        start_in_rpc: bool = False,
    ) -> None:
        self.device_info = (
            device_info if device_info is not None else fixture_json("device_info.json")
        )
        self.files: dict[str, bytes] = files if files is not None else {}
        # .ir-bestand -> knoppen die erin staan
        self.ir_buttons = (
            ir_buttons if ir_buttons is not None else {"/ext/infrared/tv.ir": ["Power", "Vol_up"]}
        )
        self.subghz_script = subghz_script
        self.rfid_script = rfid_script
        self.nfc_script = nfc_script
        self.start_in_rpc = start_in_rpc
        # --- schakelaars voor tests ---
        self.responsive = True
        self.nfc_app_running = False
        self.event_before_response = False
        self.press_release_supported = True
        self.read_chunk = 8
        self.list_chunk = 2
        self.fail_next_write = False
        self.ignore_etx = False  # lopend CLI-commando negeert Ctrl+C
        self.fail_on_rpc_kind: str | None = None  # TransportError bij dit RPC-berichttype (1x)
        self.extra_frames: list[Any] = []  # komen vóór het eerstvolgende antwoord
        # --- waarnemingen ---
        self.opens = 0
        self.closes = 0
        self.cli_log: list[str] = []
        self.rpc_log: list[str] = []
        self.sent_buttons: list[str] = []
        self.etx_count = 0  # Ctrl+C terwijl een commando liep
        self.idle_etx_count = 0  # Ctrl+C op een lege prompt
        self.cli_started = threading.Event()
        # --- interne toestand ---
        self._cond = threading.Condition()
        self._out = bytearray()
        self._open = False
        self._mode = "cli"
        self._shell = "main"
        self._line = bytearray()
        self._running: tuple[str, str] | None = None  # (commando, uitvoer na Ctrl+C)
        self._decoder = FrameDecoder()
        self._app_running = False
        self._loaded_file: str | None = None
        self._error_text = ""

    # --- ByteTransport ----------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        with self._cond:
            if self._open:
                return
            self._open = True
            self.opens += 1
            self._reset_session()
            if not self.start_in_rpc:
                self._emit("Welcome to Flipper Zero Command Line Interface!\r\n" + MAIN_PROMPT)

    def close(self) -> None:
        with self._cond:
            if self._open:
                self.closes += 1
            self._open = False

    def write(self, data: bytes) -> None:
        with self._cond:
            if not self._open:
                raise TransportError("fake: poort niet open")
            if self.fail_next_write:
                self.fail_next_write = False
                raise TransportError("fake: kabel eruit")
            if not self.responsive:
                return
            if self._mode == "cli":
                for byte in data:
                    self._cli_byte(byte)
            else:
                self._rpc_bytes(data)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._out:
                if not self._open:
                    raise TransportError("fake: poort niet open")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cond.wait(remaining)
            chunk = bytes(self._out[:max_bytes])
            del self._out[:max_bytes]
            return chunk

    def reset_input(self) -> None:
        with self._cond:
            self._out.clear()

    def describe(self) -> str:
        return "fake-flipper"

    # --- hulpmethoden voor tests ------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def shell(self) -> str:
        return self._shell

    @property
    def app_running(self) -> bool:
        return self._app_running

    @property
    def idle_at_prompt(self) -> bool:
        return self._mode == "cli" and self._running is None and self._shell == "main"

    def set_app_running(self, running: bool) -> None:
        with self._cond:
            self._app_running = running

    def queue_extra_frame(self, main: Any) -> None:
        self.extra_frames.append(main)

    # --- intern -----------------------------------------------------------------------

    def _reset_session(self) -> None:
        self._out.clear()
        self._mode = "rpc" if self.start_in_rpc else "cli"
        self._shell = "main"
        self._line.clear()
        self._running = None
        self._decoder = FrameDecoder()
        self._app_running = False
        self._loaded_file = None

    def _emit(self, text: str | bytes) -> None:
        raw = text.encode("utf-8") if isinstance(text, str) else text
        self._out.extend(raw)
        self._cond.notify_all()

    def _prompt(self) -> str:
        return MAIN_PROMPT if self._shell == "main" else NFC_PROMPT

    # --- CLI --------------------------------------------------------------------------

    def _cli_byte(self, byte: int) -> None:
        if self._running is not None:
            if byte == 0x03:
                self.etx_count += 1
                if self.ignore_etx:
                    return
                _, post = self._running
                self._running = None
                self._emit(post + self._prompt())
            return
        if byte == 0x03:
            self.idle_etx_count += 1
            self._line.clear()
            self._emit("^C" + self._prompt())
        elif byte == 0x0D:
            line = self._line.decode("ascii", errors="replace").strip()
            self._line.clear()
            self._emit("\r\n")
            self._cli_execute(line)
        else:
            self._line.append(byte)
            self._emit(bytes([byte]))

    def _run_script(self, command: str, script: str) -> None:
        pre, post = split_script(script)
        self._emit(pre)
        if post:
            self._running = (command, post)
            self.cli_started.set()
        else:
            self._emit(self._prompt())

    def _cli_execute(self, line: str) -> None:
        if not line:
            self._emit(self._prompt())
            return
        self.cli_log.append(line)
        if self._shell == "nfc":
            self._nfc_execute(line)
            return
        if line == "start_rpc_session":
            self._mode = "rpc"
            self._decoder = FrameDecoder()
            return
        match = re.fullmatch(r"subghz rx (\d+) (\d+)", line)
        if match:
            freq = int(match.group(1))
            ranges = (
                (299_999_755, 348_000_000),
                (386_999_938, 464_000_000),
                (778_999_847, 928_000_000),
            )
            if not any(lo <= freq <= hi for lo, hi in ranges):
                self._emit(f"Frequency must be in {FREQ_RANGE_STR} range, not {freq}\r\n")
                self._emit(self._prompt())
                return
            self._run_script(line, self.subghz_script)
        elif line == "rfid read":
            self._run_script(line, self.rfid_script)
        elif line == "nfc":
            if self.nfc_app_running:
                self._emit(
                    "\x1b[33mNFC app is running, unable to run NFC CLI at the same time!\r\n\x1b[0m"
                )
            else:
                self._shell = "nfc"
                self._emit("\x1b[94mWelcome to NFC Command Line Interface!\r\n\x1b[0m")
            self._emit(self._prompt())
        else:
            self._emit(f"Command not found: {line}\r\n" + self._prompt())

    def _nfc_execute(self, line: str) -> None:
        if line == "scanner -t":
            self._run_script(line, self.nfc_script)
        elif line == "exit":
            self._shell = "main"
            self._emit(self._prompt())
        else:
            self._emit(f"Command not found: {line}\r\n" + self._prompt())

    # --- RPC --------------------------------------------------------------------------

    def _rpc_bytes(self, data: bytes) -> None:
        try:
            payloads = self._decoder.feed(data)
            requests = []
            for payload in payloads:
                request = flipper_pb2.Main()
                request.ParseFromString(payload)
                requests.append(request)
        except Exception:  # noqa: BLE001 - firmware (rpc.c): ERROR_DECODE en sessie sluiten
            error = flipper_pb2.Main()
            error.command_status = flipper_pb2.ERROR_DECODE
            error.empty.SetInParent()
            self._emit(self._frame(error))
            self._mode = "cli"
            self._decoder = FrameDecoder()
            self._emit(MAIN_PROMPT)
            return
        for request in requests:
            self._rpc_handle(request)

    def _frame(self, main: Any) -> bytes:
        return encode_frame(main.SerializeToString())

    def _reply(self, request: Any, status: int = flipper_pb2.OK, *, has_next: bool = False) -> Any:
        main = flipper_pb2.Main()
        main.command_id = request.command_id
        main.command_status = status
        main.has_next = has_next
        return main

    def _send(self, request: Any, *responses: Any) -> None:
        for extra in self.extra_frames:
            self._emit(self._frame(extra))
        self.extra_frames.clear()
        for response in responses:
            self._emit(self._frame(response))

    def _state_event(self, state: int) -> Any:
        event = flipper_pb2.Main()
        event.command_id = 0
        event.app_state_response.state = state
        return event

    def _ok(self, request: Any) -> Any:
        main = self._reply(request)
        main.empty.SetInParent()
        return main

    def _error(self, request: Any, status: int) -> Any:
        main = self._reply(request, status)
        main.empty.SetInParent()
        return main

    def _rpc_handle(self, request: Any) -> None:
        kind = str(request.WhichOneof("content"))
        self.rpc_log.append(kind)
        if self.fail_on_rpc_kind == kind:
            self.fail_on_rpc_kind = None
            raise TransportError("fake: kabel eruit tijdens RPC")
        handler = getattr(self, f"_rpc_{kind}", None)
        if handler is None:
            self._send(request, self._error(request, flipper_pb2.ERROR_NOT_IMPLEMENTED))
        else:
            handler(request)

    def _rpc_system_ping_request(self, request: Any) -> None:
        reply = self._reply(request)
        reply.system_ping_response.data = request.system_ping_request.data
        self._send(request, reply)

    def _rpc_system_device_info_request(self, request: Any) -> None:
        items = list(self.device_info.items())
        frames = []
        for i, (key, value) in enumerate(items):
            reply = self._reply(request, has_next=i < len(items) - 1)
            reply.system_device_info_response.key = key
            reply.system_device_info_response.value = value
            frames.append(reply)
        self._send(request, *frames)

    def _dir_entries(self, path: str) -> list[tuple[str, str, int]] | None:
        prefix = path.rstrip("/") + "/"
        entries: dict[str, tuple[str, str, int]] = {}
        found = path in ("/ext", "/int")
        for file_path, data in self.files.items():
            if not file_path.startswith(prefix):
                continue
            found = True
            rest = file_path[len(prefix) :]
            head, sep, _ = rest.partition("/")
            if sep:
                entries[head] = (head, "dir", 0)
            else:
                entries[head] = (head, "file", len(data))
        return sorted(entries.values()) if found else None

    def _rpc_storage_list_request(self, request: Any) -> None:
        entries = self._dir_entries(request.storage_list_request.path)
        if entries is None:
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
            return
        chunks = [entries[i : i + self.list_chunk] for i in range(0, len(entries), self.list_chunk)]
        chunks = chunks or [[]]
        frames = []
        for i, chunk in enumerate(chunks):
            reply = self._reply(request, has_next=i < len(chunks) - 1)
            reply.storage_list_response.SetInParent()
            for name, kind, size in chunk:
                f = reply.storage_list_response.file.add()
                f.name = name
                f.size = size
                f.type = storage_pb2.File.DIR if kind == "dir" else storage_pb2.File.FILE
            frames.append(reply)
        self._send(request, *frames)

    def _rpc_storage_stat_request(self, request: Any) -> None:
        path = request.storage_stat_request.path
        reply = self._reply(request)
        reply.storage_stat_response.SetInParent()
        if path in self.files:
            reply.storage_stat_response.file.type = storage_pb2.File.FILE
            reply.storage_stat_response.file.size = len(self.files[path])
        elif self._dir_entries(path) is not None:
            reply.storage_stat_response.file.type = storage_pb2.File.DIR
        else:
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
            return
        self._send(request, reply)

    def _rpc_storage_read_request(self, request: Any) -> None:
        path = request.storage_read_request.path
        if path not in self.files:
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
            return
        data = self.files[path]
        chunks = [data[i : i + self.read_chunk] for i in range(0, len(data), self.read_chunk)]
        chunks = chunks or [b""]
        frames = []
        for i, chunk in enumerate(chunks):
            reply = self._reply(request, has_next=i < len(chunks) - 1)
            reply.storage_read_response.file.data = chunk
            frames.append(reply)
        self._send(request, *frames)

    def _rpc_app_start_request(self, request: Any) -> None:
        name = request.app_start_request.name
        if self._app_running:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_SYSTEM_LOCKED))
        elif name != "Infrared" or request.app_start_request.args != "RPC":
            self._send(request, self._error(request, flipper_pb2.ERROR_INVALID_PARAMETERS))
        else:
            self._app_running = True
            self._loaded_file = None
            event = self._state_event(application_pb2.APP_STARTED)
            if self.event_before_response:
                self._send(request, event, self._ok(request))
            else:
                self._send(request, self._ok(request), event)

    def _rpc_app_load_file_request(self, request: Any) -> None:
        path = request.app_load_file_request.path
        if not self._app_running:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_NOT_RUNNING))
        elif path in self.ir_buttons:
            self._loaded_file = path
            self._error_text = ""
            self._send(request, self._ok(request))
        else:
            self._error_text = f"Failed to load\n{path}"
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_CMD_ERROR))

    def _known_button(self, name: str) -> bool:
        return self._loaded_file is not None and name in self.ir_buttons[self._loaded_file]

    def _rpc_app_button_press_release_request(self, request: Any) -> None:
        name = request.app_button_press_release_request.args
        if not self.press_release_supported:
            self._send(request, self._error(request, flipper_pb2.ERROR_NOT_IMPLEMENTED))
        elif self._known_button(name):
            self.sent_buttons.append(name)
            self._send(request, self._ok(request))
        else:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_CMD_ERROR))

    def _rpc_app_button_press_request(self, request: Any) -> None:
        name = request.app_button_press_request.args
        if self._known_button(name):
            self.sent_buttons.append(f"press:{name}")
            self._send(request, self._ok(request))
        else:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_CMD_ERROR))

    def _rpc_app_button_release_request(self, request: Any) -> None:
        self.sent_buttons.append("release")
        self._send(request, self._ok(request))

    def _rpc_app_get_error_request(self, request: Any) -> None:
        reply = self._reply(request)
        reply.app_get_error_response.code = 1
        reply.app_get_error_response.text = self._error_text
        self._send(request, reply)

    def _rpc_app_exit_request(self, request: Any) -> None:
        if not self._app_running:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_NOT_RUNNING))
            return
        self._app_running = False
        self._loaded_file = None
        self._send(request, self._ok(request), self._state_event(application_pb2.APP_CLOSED))

    def _rpc_stop_session(self, request: Any) -> None:
        self._send(request, self._ok(request))
        self._mode = "cli"
        self._shell = "main"
        self._line.clear()
        self._app_running = False
        self._emit(MAIN_PROMPT)
