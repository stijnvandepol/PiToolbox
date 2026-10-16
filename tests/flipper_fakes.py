"""Simulator van een Flipper Zero achter een `ByteTransport` voor de Flipper-tests.

Gedrag nagebootst uit de firmwarebronnen (flipperzero-firmware 3fb1d517):
* CLI met echo en prompt "\\r\\n>: " (cli_shell_line.c), Ctrl+C-afhandeling, NFC-subshell met
  prompt "[<groen>nfc<reset>]>: " en `exit` (nfc_cli.c, cli_shell.c).
* `start_rpc_session` schakelt naar nanopb-delimited RPC (rpc_cli.c); `stop_session` geeft een
  OK-frame en daarna weer de CLI-prompt (rpc.c).
* app_start/app_exit sturen een ongevraagde `app_state_response` met command_id 0
  (rpc_app.c), de Infrared-app bevestigt load/press met OK of ERROR_APP_CMD_ERROR
  (infrared_scene_rpc.c).
* Opslag schrijven zoals Momentum mntm-012 `rpc_storage.c`: storage_write_request in frames met
  één command_id en has_next; het eerste frame (of een ander command_id) opent het bestand met
  CREATE_ALWAYS, alleen het laatste frame (of een fout) krijgt een antwoord, en na een fout
  begint een volgend frame dus een nieuw, leeg bestand. mkdir maakt één niveau
  (ERROR_STORAGE_EXIST als het er al is), md5sum geeft hex in kleine letters, rename verwijdert
  een bestaand doel eerst (storage_external_api.c `storage_common_rename`).

De uitvoer van subghz/rfid/nfc komt uit tests/fixtures/flipper/*.txt (zelf geschreven op basis
van de printf-formaten; de decoder-tekst van Sub-GHz is synthetisch). De `*_momentum_*`-fixtures
volgen Momentum mntm-012 (e1784e74): kop en slot van `subghz rx` zoals op de echte Flipper
opgenomen, pakketten in de formaten van lib/subghz/protocols (princeton.c, came_atomo.c,
ws_generic.c) met verzonnen waarden. ANSI-kleuren (bijv. rond "OK" bij Load_keystore) zijn
weggelaten; die haalt FlipperDevice er toch uit.

Momentum gebruikt dezelfde CLI als official; alleen het geldige Sub-GHz-bereik verschilt
(`subghz_ranges`, zie `_flipper_cli.MOMENTUM_SUBGHZ_RANGES_HZ`). De foutmelding noemt in beide
firmwares SUBGHZ_FREQUENCY_RANGE_STR (op hardware bevestigd voor Momentum).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from pitoolbox.core.devices._flipper_rpc.framing import FrameDecoder, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2, storage_pb2
from pitoolbox.core.devices.flipper import FlipperTiming, TransportError
from pitoolbox.frontends.flipper_ui import protocol as proto

FIXTURES = Path(__file__).parent / "fixtures" / "flipper"
ETX_MARKER = "---ETX---"
MAIN_PROMPT = "\r\n>: "
NFC_PROMPT = "\r\n[\x1b[32mnfc\x1b[0m]>: "
FREQ_RANGE_STR = "299999755...348000000 or 386999938...464000000 or 778999847...928000000"
OFFICIAL_RANGES = (
    (299_999_755, 348_000_000),
    (386_999_938, 464_000_000),
    (778_999_847, 928_000_000),
)

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
        subghz_ranges: tuple[tuple[int, int], ...] = OFFICIAL_RANGES,
        start_in_rpc: bool = False,
        kind: str = "usb",
    ) -> None:
        self.kind = kind  # "ble": gedraagt zich als BLE (geen CLI) zodra start_in_rpc ook waar is
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
        self.subghz_ranges = subghz_ranges
        self.start_in_rpc = start_in_rpc
        # --- PiToolbox-FAP (fase 4) ---
        self.fap_path = "/ext/apps/Tools/pitoolbox_ui.fap"
        self.fap: FakeFap | None = None  # geïnstalleerde app (attach_fap)
        self.user_app_open = False  # een app van de gebruiker houdt de loader bezet
        self.rtc_epoch: int | None = None  # None = time.time()
        self.app_starts: list[str] = []
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
        self.drop_app_started = False  # app start wel, maar meldt APP_STARTED niet
        # --- opslag schrijven (ui-install) ---
        self.dirs: set[str] = set()  # lege mappen (naast de mappen die uit `files` volgen)
        self.write_chunks: list[int] = []  # grootte van elk storage_write-blok
        self.fail_write_at: int | None = None  # dit blok (1-based, telt over alle writes) faalt
        self.corrupt_writes = False  # het geschreven bestand wijkt 1 byte af
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
        self._running_app: str | None = None  # "Infrared" of het FAP-pad
        self._loaded_file: str | None = None
        self._error_text = ""
        self._write: tuple[int, str, bytearray] | None = None  # (command_id, pad, inhoud)

    # --- ByteTransport ----------------------------------------------------------------

    @property
    def starts_in_rpc(self) -> bool:
        return self.start_in_rpc

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
            self._session_closed()

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

    # --- PiToolbox-FAP ----------------------------------------------------------------

    def attach_fap(self, fap: FakeFap) -> FakeFap:
        """Installeer de nagebootste FAP op `fap_path`."""
        fap.transport = self
        self.fap = fap
        return fap

    @property
    def fap_running(self) -> bool:
        return self._running_app == self.fap_path and self._app_running

    def inject_app_data(self, data: bytes) -> None:
        """De FAP stuurt een DataExchange naar de Pi (ongevraagd frame, command_id 0)."""
        with self._cond:
            if not self._open or not self.fap_running:
                return
            frame = flipper_pb2.Main()
            frame.command_id = 0
            frame.app_data_exchange_request.data = data
            self._emit(self._frame(frame))

    def fap_exits(self) -> None:
        """De gebruiker sluit de FAP zelf (Back in het hoofdmenu): APP_CLOSED, loader vrij."""
        with self._cond:
            if not self.fap_running:
                return
            self._stop_app()
            if self._open:
                self._emit(self._frame(self._state_event(application_pb2.APP_CLOSED)))

    def _stop_app(self) -> None:
        was_fap = self.fap_running
        self._app_running = False
        self._running_app = None
        self._loaded_file = None
        if was_fap and self.fap is not None:
            self.fap.on_stopped()

    def _session_closed(self) -> None:
        # SessionClose: een RPC-gekoppelde app sluit zichzelf (pitoolbox_ui.c).
        if self._running_app is not None:
            self._stop_app()

    # --- intern -----------------------------------------------------------------------

    def _reset_session(self) -> None:
        self._out.clear()
        self._mode = "rpc" if self.start_in_rpc else "cli"
        self._shell = "main"
        self._line.clear()
        self._running = None
        self._decoder = FrameDecoder()
        self._session_closed()
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
            if not any(lo <= freq <= hi for lo, hi in self.subghz_ranges):
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
        found = path in ("/ext", "/int") or path in self.dirs
        for directory in self.dirs:
            if directory.startswith(prefix):
                head = directory[len(prefix) :].split("/", 1)[0]
                entries.setdefault(head, (head, "dir", 0))
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
        self.app_starts.append(name)
        is_fap = self.fap is not None and name == self.fap_path
        if self._app_running or self.user_app_open:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_SYSTEM_LOCKED))
        elif (name != "Infrared" and not is_fap) or request.app_start_request.args != "RPC":
            self._send(request, self._error(request, flipper_pb2.ERROR_INVALID_PARAMETERS))
        else:
            self._app_running = True
            self._running_app = name
            self._loaded_file = None
            event = self._state_event(application_pb2.APP_STARTED)
            if self.drop_app_started:
                self._send(request, self._ok(request))
            elif self.event_before_response:
                self._send(request, event, self._ok(request))
            else:
                self._send(request, self._ok(request), event)
            if is_fap and self.fap is not None:
                self.fap.on_started()  # stuurt HELLO (pitoolbox_ui.c: na send_started)

    def _rpc_app_lock_status_request(self, request: Any) -> None:
        reply = self._reply(request)
        reply.app_lock_status_response.locked = self._app_running or self.user_app_open
        self._send(request, reply)

    def _rpc_app_data_exchange_request(self, request: Any) -> None:
        if not self.fap_running or self.fap is None:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_NOT_RUNNING))
            return
        verdict = self.fap.on_data(bytes(request.app_data_exchange_request.data))
        if verdict == "hang":
            return  # nooit een confirm: de Pi krijgt een time-out
        if verdict == "ok":
            self._send(request, self._ok(request))
        else:
            self._send(request, self._error(request, flipper_pb2.ERROR_APP_CMD_ERROR))

    def _rpc_storage_delete_request(self, request: Any) -> None:
        path = request.storage_delete_request.path
        if self.files.pop(path, None) is None:
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
        else:
            self._send(request, self._ok(request))

    def _is_dir(self, path: str) -> bool:
        return path not in self.files and self._dir_entries(path) is not None

    def _rpc_storage_write_request(self, request: Any) -> None:
        req = request.storage_write_request
        self.write_chunks.append(len(req.file.data))
        if self._write is not None and self._write[0] != request.command_id:
            self._write = None  # firmware: reset (ERROR_CONTINUOUS_COMMAND_INTERRUPTED)
        if self._write is None:
            parent = req.path.rsplit("/", 1)[0]
            if not self._is_dir(parent):
                self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
                return
            self._write = (request.command_id, req.path, bytearray())
            self.files[req.path] = b""  # FSOM_CREATE_ALWAYS
        _cid, path, buf = self._write
        if self.fail_write_at is not None and len(self.write_chunks) == self.fail_write_at:
            self._write = None
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_INTERNAL))
            return
        buf.extend(req.file.data)
        self.files[path] = bytes(buf)
        if not request.has_next:
            self._write = None
            if self.corrupt_writes and buf:
                buf[0] ^= 0xFF
                self.files[path] = bytes(buf)
            self._send(request, self._ok(request))

    def _rpc_storage_md5sum_request(self, request: Any) -> None:
        path = request.storage_md5sum_request.path
        if path not in self.files:
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
            return
        reply = self._reply(request)
        reply.storage_md5sum_response.md5sum = hashlib.md5(
            self.files[path], usedforsecurity=False
        ).hexdigest()
        self._send(request, reply)

    def _rpc_storage_mkdir_request(self, request: Any) -> None:
        path = request.storage_mkdir_request.path
        if path in self.files or self._is_dir(path):
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_EXIST))
        elif not self._is_dir(path.rsplit("/", 1)[0]):
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
        else:
            self.dirs.add(path)
            self._send(request, self._ok(request))

    def _rpc_storage_rename_request(self, request: Any) -> None:
        old = request.storage_rename_request.old_path
        new = request.storage_rename_request.new_path
        if old not in self.files:
            self._send(request, self._error(request, flipper_pb2.ERROR_STORAGE_NOT_EXIST))
            return
        self.files[new] = self.files.pop(old)
        self._send(request, self._ok(request))

    def _rpc_system_get_datetime_request(self, request: Any) -> None:
        epoch = self.rtc_epoch if self.rtc_epoch is not None else int(time.time())
        moment = time.gmtime(epoch)
        reply = self._reply(request)
        dt = reply.system_get_datetime_response.datetime
        dt.year, dt.month, dt.day = moment.tm_year, moment.tm_mon, moment.tm_mday
        dt.hour, dt.minute, dt.second = moment.tm_hour, moment.tm_min, moment.tm_sec
        dt.weekday = moment.tm_wday + 1
        self._send(request, reply)

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
        self._stop_app()
        self._send(request, self._ok(request), self._state_event(application_pb2.APP_CLOSED))

    def _rpc_stop_session(self, request: Any) -> None:
        self._send(request, self._ok(request))
        self._mode = "cli"
        self._shell = "main"
        self._line.clear()
        self._session_closed()
        self._app_running = False
        self._emit(MAIN_PROMPT)


class FakeFap:
    """Nagebootste `pitoolbox_ui` (FAP-kant van docs/fase4-protocol.md) voor de bridge-tests.

    Gedraagt zich als `pitoolbox_ui.c`: groet na de start met HELLO (nieuwe willekeurige
    instance per start), bevestigt elke DataExchange direct (OK, of ERROR_APP_CMD_ERROR bij
    `reject_next`), en neemt Pi-berichten op met hun aankomsttijd. `hang` = nooit bevestigen.
    """

    def __init__(self, *, auto_hello: bool = True, max_rx: int = proto.MAX_MSG) -> None:
        self.transport: FakeFlipperTransport | None = None
        self.auto_hello = auto_hello
        self.max_rx = max_rx
        self.instances: list[int] = []  # vaste instances voor de volgende starts (anders random)
        self.instance = 0
        self.running = False
        self.starts = 0
        self.stops = 0
        self.reject_next = 0
        self.hang = False
        self.received: list[proto.PiMessage] = []
        self.received_at: list[float] = []
        self.invalid = 0
        self._seq = proto.SeqCounter()
        self._cond = threading.Condition()

    # --- door de transport aangeroepen (met de transport-lock) ---

    def on_started(self) -> None:
        import secrets

        self.running = True
        self.starts += 1
        if self.instances:
            self.instance = self.instances.pop(0)
        else:
            self.instance = secrets.randbelow(2**32 - 1) + 1
        if self.auto_hello:
            self.send(self.hello())

    def on_stopped(self) -> None:
        self.running = False
        self.stops += 1
        with self._cond:
            self._cond.notify_all()

    def on_data(self, data: bytes) -> str:
        if self.hang:
            return "hang"
        if self.reject_next:
            self.reject_next -= 1
            return "reject"
        try:
            msg = proto.decode_from_pi(data)
        except proto.ProtocolError:
            self.invalid += 1
            return "reject"
        with self._cond:
            self.received.append(msg)
            self.received_at.append(time.monotonic())
            self._cond.notify_all()
        return "ok"

    # --- testkant ---

    def next_seq(self) -> int:
        return self._seq.next()

    def hello(self, instance: int | None = None) -> proto.Hello:
        return proto.Hello(
            seq=self.next_seq(),
            app_major=1,
            app_minor=0,
            proto_max=1,
            instance=instance or self.instance,
            max_rx=self.max_rx,
        )

    def send(self, msg: proto.Message) -> None:
        self.send_raw(proto.encode(msg))

    def send_raw(self, data: bytes) -> None:
        assert self.transport is not None
        self.transport.inject_app_data(data)

    def send_burst(self, msgs: list[proto.Message]) -> None:
        """Alle berichten in één keer in de stroom (de Pi leest ze in één slag)."""
        assert self.transport is not None
        with self.transport._cond:
            for msg in msgs:
                self.transport.inject_app_data(proto.encode(msg))

    def action(self, kind: proto.ActionKind, action_id: int) -> int:
        seq = self.next_seq()
        self.send(proto.Action(seq=seq, kind=kind, action_id=action_id))
        return seq

    def quick(self, action: proto.QuickAction) -> int:
        return self.action(proto.ActionKind.QUICK, int(action))

    def answer(self, handle: int, approve: bool) -> int:
        seq = self.next_seq()
        self.send(proto.ConfirmAnswer(seq=seq, handle=handle, approve=approve))
        return seq

    def of_type(self, cls: type[Any]) -> list[Any]:
        with self._cond:
            return [m for m in self.received if isinstance(m, cls)]

    def wait_for(self, cls: type[Any], predicate: Any = None, timeout: float = 3.0) -> Any:
        """Eerste ontvangen bericht van type `cls` (dat aan `predicate` voldoet)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for msg in self.received:
                    if isinstance(msg, cls) and (predicate is None or predicate(msg)):
                        return msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"geen {cls.__name__} ontvangen; wel: "
                        f"{[type(m).__name__ for m in self.received]}"
                    )
                self._cond.wait(remaining)

    def result_text(self, ref: int, timeout: float = 3.0) -> str:
        """Wacht op een volledig RESULT voor `ref` en zet de delen weer in elkaar."""
        reassembler = proto.Reassembler()
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            chunks = [m for m in self.of_type(proto.ResultChunk) if m.ref == ref]
            for chunk in chunks[seen:]:
                text = reassembler.feed(chunk)
                if text is not None:
                    return text
            seen = len(chunks)
            if time.monotonic() > deadline:
                raise AssertionError(f"geen volledig RESULT voor ref {ref}")
            time.sleep(0.005)

    def clear(self) -> None:
        with self._cond:
            self.received.clear()
            self.received_at.clear()


# --- BLE: GATT-kant van een Flipper + fake bleak --------------------------------------------
#
# Nagebootst uit serial_service.c: TX = indicaties, flow control = uint32 big-endian met de
# vrije RPC-buffer (alleen een notify als het krediet op 0 stond en de buffer leeg is), RX = write
# (without response). De RPC-engine erachter is de FakeFlipperTransport in RPC-modus.

BLE_ADV_UUID = "00003080-0000-1000-8000-00805f9b34fb"

# Letterlijke kopieën, los van de productiecode (zodat een typefout daar opvalt).
RX_UUID_FAKE = "19ed82ae-ed21-4c9d-4145-228e62fe0000"
TX_UUID_FAKE = "19ed82ae-ed21-4c9d-4145-228e61fe0000"
FLOW_UUID_FAKE = "19ed82ae-ed21-4c9d-4145-228e63fe0000"


def wait_until(condition: Any, timeout: float = 3.0) -> bool:
    """Poll tot `condition()` waar is (voor gebeurtenissen in de BLE-loopthread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


class FakeBleFlipper:
    """De GATT-server van één Flipper. Thread-safe: tests en de bleak-loop praten tegelijk."""

    def __init__(
        self,
        *,
        address: str = "80:E1:26:00:00:01",
        name: str = "Flipper Testy",
        rssi: int = -60,
        buff_size: int = 1024,
        mtu: int = 247,
        indication_size: int = 100,
        paired: bool = True,
        swallow: bool = False,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.address = address
        self.name = name
        self.rssi = rssi
        self.buff_size = buff_size
        self.mtu = mtu
        self.indication_size = indication_size
        self.paired = paired
        self.swallow = swallow  # RX-bytes alleen tellen, niet door de RPC-engine halen
        self.rpc = FakeFlipperTransport(start_in_rpc=True, kind="ble", files=files)
        # --- schakelaars ---
        self.advertising = True
        self.auto_refill = True
        self.refill_delay_s = 0.0
        self.connect_error: BaseException | None = None
        self.missing_chars = False
        self.initial_credit: int | None = None  # None = buff_size
        # --- waarnemingen ---
        self.connects = 0
        self.writes: list[int] = []  # lengte van elke RX-write
        self.credits_before: list[int] = []  # krediet (Flipper-kant) vlak voor elke write
        self.overflows = 0  # writes groter dan het krediet
        self.received = bytearray()
        self.write_responses: list[bool] = []
        self.indications = 0
        self.refills = 0
        # --- intern ---
        self.credit = buff_size
        self._lock = threading.Lock()
        self._client: FakeBleakClient | None = None
        self._loop: Any = None
        self._subs: dict[str, Any] = {}

    # --- verbinding -------------------------------------------------------------------

    def attach(self, client: FakeBleakClient, loop: Any) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        with self._lock:
            self._client = client
            self._loop = loop
            self._subs.clear()
            self.connects += 1
            self.credit = self.buff_size if self.initial_credit is None else self.initial_credit
        self.rpc.close()
        self.rpc.open()  # nieuwe RPC-sessie (bt.c opent die bij elke verbinding)

    def detach(self, client: FakeBleakClient) -> None:
        with self._lock:
            if self._client is client:
                self._client = None

    def drop_link(self) -> None:
        """De link valt weg (Flipper uit, buiten bereik): disconnected_callback volgt."""
        with self._lock:
            client, loop = self._client, self._loop
            self._client = None
        if client is not None and loop is not None:
            client.mark_lost()
            loop.call_soon_threadsafe(client.fire_disconnected)

    # --- GATT-kant ---------------------------------------------------------------------

    def _auth_error(self) -> BaseException:
        from bleak.exc import BleakGATTProtocolError, BleakGATTProtocolErrorCode

        return BleakGATTProtocolError(BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHENTICATION)

    def read_flow(self) -> bytes:
        if not self.paired:
            raise self._auth_error()
        with self._lock:
            value = self.credit
        return int(value).to_bytes(4, "big")

    def subscribe(self, uuid: str, callback: Any, characteristic: Any) -> None:
        if not self.paired:
            raise self._auth_error()
        with self._lock:
            self._subs[uuid] = (callback, characteristic)

    def write_rx(self, data: bytes, response: bool) -> None:
        if not self.paired:
            if response:
                raise self._auth_error()
            return  # write without response naar een geweigerd attribuut verdwijnt stil
        with self._lock:
            self.writes.append(len(data))
            self.credits_before.append(self.credit)
            self.write_responses.append(response)
            if len(data) > self.credit:
                self.overflows += 1
            self.credit = max(0, self.credit - len(data))
            self.received.extend(data)
            drained = self.credit == 0
        if not self.swallow:
            try:
                self.rpc.write(bytes(data))
            except TransportError:
                self.drop_link()  # de simulator liet de verbinding vallen (fail_on_rpc_kind)
                return
            self._pump()
        if drained and self.auto_refill and self._loop is not None:
            self._loop.call_later(self.refill_delay_s, self.refill)

    def _pump(self) -> None:
        """Antwoordbytes van de RPC-engine als indicaties van `indication_size` bytes."""
        while True:
            data = self.rpc.read(4096, 0.0)
            if not data:
                return
            for i in range(0, len(data), self.indication_size):
                self._deliver(TX_UUID_FAKE, data[i : i + self.indication_size])
                self.indications += 1

    def refill(self) -> None:
        """RPC-buffer leeg: de Flipper meldt de volle bufferruimte (notify_buffer_is_empty)."""
        with self._lock:
            self.credit = self.buff_size
            self.refills += 1
        self._deliver(FLOW_UUID_FAKE, self.buff_size.to_bytes(4, "big"))

    def _deliver(self, uuid: str, data: bytes) -> None:
        with self._lock:
            sub = self._subs.get(uuid)
            loop = self._loop
        if sub is not None and loop is not None:
            callback, characteristic = sub
            loop.call_soon_threadsafe(callback, characteristic, bytearray(data))


class FakeBleChar:
    def __init__(self, flipper: FakeBleFlipper, uuid: str, properties: list[str]) -> None:
        self._flipper = flipper
        self.uuid = uuid
        self.properties = properties

    @property
    def max_write_without_response_size(self) -> int:
        return self._flipper.mtu - 3


class FakeBleServices:
    def __init__(self, flipper: FakeBleFlipper) -> None:
        self._chars = {
            RX_UUID_FAKE: FakeBleChar(
                flipper, RX_UUID_FAKE, ["write-without-response", "write", "read"]
            ),
            TX_UUID_FAKE: FakeBleChar(flipper, TX_UUID_FAKE, ["read", "indicate"]),
            FLOW_UUID_FAKE: FakeBleChar(flipper, FLOW_UUID_FAKE, ["read", "notify"]),
        }
        if flipper.missing_chars:
            del self._chars[FLOW_UUID_FAKE]

    def get_characteristic(self, uuid: str) -> FakeBleChar | None:
        return self._chars.get(str(uuid).lower())


class FakeBleakClient:
    """Vervanger voor `bleak.BleakClient`; `world` wordt per test gebonden (install_fake_bleak)."""

    world: FakeBleWorld

    def __init__(
        self,
        address_or_ble_device: Any,
        disconnected_callback: Any = None,
        services: Any = None,
        *,
        timeout: float = 30,
        pair: bool = False,
        **kwargs: Any,
    ) -> None:
        self.address = str(getattr(address_or_ble_device, "address", address_or_ble_device))
        self._flipper = self.world.find(self.address)
        self._disconnected_callback = disconnected_callback
        self._connected = False
        self.pair_requested = pair

    async def connect(self, **kwargs: Any) -> None:
        import asyncio

        from bleak.exc import BleakDeviceNotFoundError

        if self._flipper is None or not self._flipper.advertising:
            raise BleakDeviceNotFoundError(self.address, f"{self.address} niet gevonden")
        self._flipper.attach(self, asyncio.get_running_loop())
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected and self._flipper is not None:
            self._connected = False
            self._flipper.detach(self)

    # --- door FakeBleFlipper aangeroepen ---

    def mark_lost(self) -> None:
        self._connected = False

    def fire_disconnected(self) -> None:
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)

    # --- bleak-API ---

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def name(self) -> str:
        return self._flipper.name if self._flipper is not None else self.address

    @property
    def mtu_size(self) -> int:
        return 23  # zoals BlueZ

    @property
    def services(self) -> FakeBleServices:
        assert self._flipper is not None
        return FakeBleServices(self._flipper)

    def _require_connected(self) -> FakeBleFlipper:
        from bleak.exc import BleakError

        if not self._connected or self._flipper is None:
            raise BleakError("Not connected")
        return self._flipper

    async def read_gatt_char(self, char: Any, *, use_cached: bool = False) -> bytearray:
        flipper = self._require_connected()
        assert str(char).lower() == FLOW_UUID_FAKE
        return bytearray(flipper.read_flow())

    async def start_notify(self, char: Any, callback: Any, **kwargs: Any) -> None:
        flipper = self._require_connected()
        uuid = str(char).lower()
        characteristic = FakeBleServices(flipper).get_characteristic(uuid)
        assert characteristic is not None
        flipper.subscribe(uuid, callback, characteristic)

    async def write_gatt_char(self, char: Any, data: Any, response: bool | None = None) -> None:
        from bleak.exc import BleakError

        flipper = self._require_connected()
        assert str(char).lower() == RX_UUID_FAKE
        if not response and len(data) > flipper.mtu - 3:
            raise BleakError("data langer dan max_write_without_response_size")
        flipper.write_rx(bytes(data), bool(response))


class FakeBleWorld:
    """Alle BLE-apparaten in de buurt: Flippers en ruis."""

    def __init__(self, *flippers: FakeBleFlipper) -> None:
        self.flippers: list[FakeBleFlipper] = list(flippers)
        self.noise: list[tuple[str, str, int, list[str]]] = []  # (adres, naam, rssi, uuids)
        self.scan_error: BaseException | None = None

    def add_noise(
        self, address: str, name: str, rssi: int = -80, service_uuids: list[str] | None = None
    ) -> None:
        self.noise.append((address, name, rssi, service_uuids or []))

    def find(self, address: str) -> FakeBleFlipper | None:
        for flipper in self.flippers:
            if flipper.address.lower() == address.lower():
                return flipper
        return None

    def seen(self) -> dict[str, tuple[Any, Any]]:
        from bleak.backends.device import BLEDevice
        from bleak.backends.scanner import AdvertisementData

        result: dict[str, tuple[Any, Any]] = {}
        entries: list[tuple[str, str, int, list[str]]] = [
            (f.address, f.name, f.rssi, [BLE_ADV_UUID]) for f in self.flippers if f.advertising
        ]
        entries.extend(self.noise)
        for address, name, rssi, uuids in entries:
            device = BLEDevice(address, name, None)
            adv = AdvertisementData(name or None, {}, {}, uuids, None, rssi, ())
            result[address] = (device, adv)
        return result


class FakeBleakScanner:
    world: FakeBleWorld

    @classmethod
    async def discover(
        cls, timeout: float = 5.0, *, return_adv: bool = False, **kwargs: Any
    ) -> Any:
        if cls.world.scan_error is not None:
            raise cls.world.scan_error
        seen = cls.world.seen()
        return seen if return_adv else [d for d, _ in seen.values()]

    @classmethod
    async def find_device_by_filter(
        cls, filterfunc: Any, timeout: float = 10.0, **kwargs: Any
    ) -> Any:
        if cls.world.scan_error is not None:
            raise cls.world.scan_error
        for device, adv in cls.world.seen().values():
            if filterfunc(device, adv):
                return device
        return None

    @classmethod
    async def find_device_by_address(
        cls, device_identifier: str, timeout: float = 10.0, **kwargs: Any
    ) -> Any:
        return await cls.find_device_by_filter(
            lambda d, ad: d.address.lower() == device_identifier.lower(), timeout
        )


def install_fake_bleak(monkeypatch: Any, world: FakeBleWorld) -> None:
    """Laat `_flipper_ble` de fake client en scanner gebruiken (niets raakt echte Bluetooth)."""
    from pitoolbox.core.devices import _flipper_ble

    monkeypatch.setattr(
        _flipper_ble, "BleakClient", type("BoundFakeClient", (FakeBleakClient,), {"world": world})
    )
    monkeypatch.setattr(
        _flipper_ble,
        "BleakScanner",
        type("BoundFakeScanner", (FakeBleakScanner,), {"world": world}),
    )


# --- FAP-bestanden voor ui-install ----------------------------------------------------------


def make_fap(
    *,
    name: str = "PiToolbox",
    api: tuple[int, int] = (87, 1),
    target: int = 7,
    body: int = 3000,
    machine: int = 40,
    with_meta: bool = True,
) -> bytes:
    """Minimale ELF32-ARM met een `.fapmeta`-sectie zoals ufbt die maakt (zie core/devices/_fap.py
    voor de bronnen van de layout). `body` bytes vulling maken het bestand groter."""
    import struct

    meta = struct.pack("<IIHHHHI", 0x52474448, 1, api[1], api[0], target, 4096, 0x00010000)
    meta += name.encode("ascii").ljust(32, b"\0") + b"\0" + b"\0" * 32 + struct.pack("<I", 0)
    names = b"\0.shstrtab\0" + (b".fapmeta\0" if with_meta else b".text\0")
    filler = bytes((i * 7) & 0xFF for i in range(body))
    data_off = 52
    meta_off = data_off + len(filler)
    names_off = meta_off + len(meta)
    shoff = names_off + len(names)
    sections = [
        (0, 0, 0),  # SHN_UNDEF
        (11, meta_off, len(meta)),  # .fapmeta (of .text)
        (1, names_off, len(names)),  # .shstrtab
    ]
    header = bytearray(52)
    header[:6] = b"\x7fELF\x01\x01"
    header[6] = 1
    struct.pack_into("<HHI", header, 16, 1, machine, 1)  # ET_REL, e_machine, EV_CURRENT
    struct.pack_into("<I", header, 32, shoff)
    struct.pack_into("<HHHHHH", header, 40, 52, 0, 0, 40, len(sections), 2)
    table = b"".join(
        struct.pack("<IIIIIIIIII", n, 1, 0, 0, off, size, 0, 0, 1, 0) for n, off, size in sections
    )
    return bytes(header) + filler + meta + names + table
