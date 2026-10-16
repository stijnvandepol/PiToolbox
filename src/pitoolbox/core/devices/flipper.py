"""Flipper Zero via USB-CDC: wisselt automatisch tussen CLI-modus en RPC-sessie.

Bronnen (zie de verwijzingen bij de afzonderlijke onderdelen):

* USB VID:PID 0483:5740: flipperzero-firmware targets/f7/furi_hal/furi_hal_usb_cdc.c.
* De poort start in CLI-modus (prompt "\\r\\n>: ", cli_shell_line.c). `start_rpc_session`
  (CR, niet CRLF) schakelt dezelfde poort naar nanopb-delimited RPC (rpc_cli.c); een
  `stop_session`-bericht (rpc.c: PB_Main_stop_session_tag) brengt de CLI terug.
* DTR laag/hoog = verbinding verbreken/maken (applications/services/cli/cli_vcp.c): na het
  heropenen van de poort begint er dus altijd een verse CLI-shell.
* Sessiehandshake en CR-only-detail: afgeleid van busse/flipperzero-mcp (MIT).

Eén lock rond alle device-I/O. `status()` en `abort()` nemen die lock nooit blokkerend.
`abort()` verhoogt een generatieteller; lopende CLI-lussen en RPC-wachttijden controleren die
elke ~50 ms, sturen Ctrl+C (0x03, `cli_is_pipe_broken_or_is_etx_next_char`) en geven `Aborted`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import serial
from serial.tools import list_ports

from pitoolbox.core.devices._flipper_cli import CliProfile, select_profile
from pitoolbox.core.devices._flipper_rpc.client import Main, RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.framing import FrameError, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2, storage_pb2
from pitoolbox.core.errors import (
    Aborted,
    DeviceError,
    DeviceNotConnected,
    DeviceTimeout,
)

if TYPE_CHECKING:
    from pitoolbox.config import Settings

log = logging.getLogger(__name__)

FLIPPER_VID = 0x0483
FLIPPER_PID = 0x5740

_ETX = b"\x03"
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_GENERIC_PROMPT_SUFFIX = ">: "
_MAX_CLI_BYTES = 1_000_000
_MAX_RAW_CHARS = 16_000

T = TypeVar("T")


class TransportError(DeviceError):
    """I/O-fout op de transportlaag (kabel eruit, poort weg). Reden voor één reconnect."""

    code = "transport_error"


class ByteTransport(Protocol):
    """Minimale bytestroom naar de Flipper; in tests vervangen door een simulator."""

    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, data: bytes) -> None: ...

    def read(self, max_bytes: int, timeout: float) -> bytes:
        """Lees 1..max_bytes bytes; leeg resultaat betekent: niets binnen `timeout` s."""
        ...

    def reset_input(self) -> None: ...

    def describe(self) -> str: ...


class UsbSerialTransport:
    """pyserial-transport. Zonder expliciete poort wordt op VID:PID 0483:5740 gezocht."""

    def __init__(self, port: str | None = None, baudrate: int = 115200) -> None:
        self._port = port
        self._baudrate = baudrate  # CDC negeert de baudrate; pyserial wil er wel een
        self._ser: Any = None
        self._opened_port: str | None = None

    @staticmethod
    def find_port() -> str:
        found = sorted(
            (p for p in list_ports.comports() if p.vid == FLIPPER_VID and p.pid == FLIPPER_PID),
            key=lambda p: str(p.device),
        )
        if not found:
            raise DeviceNotConnected(
                "Geen Flipper gevonden op USB (VID:PID 0483:5740). Is hij aangesloten en "
                "niet in gebruik door qFlipper of een andere terminal?"
            )
        if len(found) > 1:
            log.warning("meerdere Flippers gevonden, eerste gekozen: %s", found[0].device)
        return str(found[0].device)

    @property
    def is_open(self) -> bool:
        return self._ser is not None and bool(self._ser.is_open)

    def open(self) -> None:
        if self.is_open:
            return
        port = self._port or self.find_port()
        try:
            # DTR hoog bij openen = "verbonden" voor de firmware (cli_vcp.c).
            self._ser = serial.Serial(port, self._baudrate, timeout=0.1, write_timeout=2.0)
        except (serial.SerialException, OSError) as exc:
            self._ser = None
            raise DeviceNotConnected(
                f"kan seriële poort {port} niet openen: {exc} (rechten? zit je in 'dialout'?)"
            ) from exc
        self._opened_port = port

    def close(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except (serial.SerialException, OSError) as exc:
                log.debug("fout bij sluiten van de poort: %s", exc)

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise TransportError("seriële poort is niet open")
        try:
            self._ser.write(data)
            self._ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"schrijffout op seriële poort: {exc}") from exc

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if not self.is_open:
            raise TransportError("seriële poort is niet open")
        try:
            self._ser.timeout = timeout
            data: bytes = self._ser.read(1)
            if data and max_bytes > 1:
                waiting = int(self._ser.in_waiting)
                if waiting:
                    data += self._ser.read(min(waiting, max_bytes - 1))
            return data
        except (serial.SerialException, OSError) as exc:
            raise TransportError(f"leesfout op seriële poort: {exc}") from exc

    def reset_input(self) -> None:
        if self.is_open:
            try:
                self._ser.reset_input_buffer()
            except (serial.SerialException, OSError) as exc:
                raise TransportError(f"kan invoerbuffer niet leegmaken: {exc}") from exc

    def describe(self) -> str:
        return f"usb-serieel {self._opened_port or self._port or '(auto)'}"


@dataclass(frozen=True)
class FlipperTiming:
    """Wachttijden in seconden; tests verkleinen deze."""

    prompt_timeout_s: float = 3.0
    rpc_timeout_s: float = 5.0
    probe_timeout_s: float = 1.5
    settle_quiet_s: float = 0.15
    settle_max_s: float = 1.0
    read_slice_s: float = 0.05
    stop_grace_s: float = 5.0  # extra tijd na Ctrl+C voordat de prompt terug moet zijn
    command_timeout_s: float = 10.0
    app_event_timeout_s: float = 3.0
    app_exit_timeout_s: float = 3.0
    probe_attempts: int = 3


@dataclass
class _Attempt:
    """Wordt waar zodra een actie met bijwerking is gestart; daarna geen automatische retry."""

    committed: bool = False


def _clean(raw: bytes | bytearray) -> str:
    """Bytes naar tekst: ANSI weg, CRLF/CR naar LF."""
    text = _ANSI_RE.sub("", bytes(raw).decode("utf-8", errors="replace"))
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _last_line(text: str) -> str:
    return text.rsplit("\n", 1)[-1]


def _strip_echo(text: str, command: str) -> str:
    """Haal de door de CLI teruggegeven commandoregel (echo) uit de tekst."""
    idx = text.find(command)
    if idx < 0:
        return text
    newline = text.find("\n", idx)
    return text[newline + 1 :] if newline >= 0 else ""


def _strip_prompt(text: str, prompts: tuple[str, ...]) -> str:
    last = _last_line(text)
    return text[: len(text) - len(last)] if last in prompts else text


class FlipperDevice:
    """Thread-safe Flipper-toegang met automatische CLI/RPC-modewissel."""

    def __init__(
        self,
        transport_factory: Callable[[], ByteTransport],
        cli_profile: str = "auto",
        *,
        timing: FlipperTiming | None = None,
    ) -> None:
        self._factory = transport_factory
        self._profile_setting = cli_profile
        self._timing = timing or FlipperTiming()
        self._lock = threading.Lock()  # alle device-I/O
        self._gen_lock = threading.Lock()
        self._abort_gen = 0
        # Onderstaande toestand wordt alleen onder `_lock` gewijzigd; status() leest ze zonder.
        self._transport: ByteTransport | None = None
        self._mode = "unknown"  # "cli" | "rpc" | "unknown"
        self._client: RpcClient | None = None
        self._profile: CliProfile | None = None
        self._device_info: dict[str, str] | None = None
        self._app_active = False
        self._maybe_rpc = False  # poort kan nog in een RPC-sessie staan die wij kwijt zijn
        self._busy_op: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> FlipperDevice:
        """Lui: opent de poort pas bij het eerste gebruik."""
        port = settings.flipper_port

        def factory() -> ByteTransport:
            return UsbSerialTransport(port)

        return cls(factory, settings.flipper_cli_profile)

    # --- publieke levenscyclus --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Momentopname zonder I/O, zonder lock en zonder te verbinden."""
        transport = self._transport
        profile = self._profile
        return {
            "connected": transport is not None and transport.is_open,
            "transport": transport.describe() if transport is not None else "nog niet geopend",
            "mode": self._mode,
            "busy": self._busy_op is not None,
            "operation": self._busy_op,
            "profile": profile.name if profile is not None else None,
        }

    def connect(self) -> None:
        """Open de poort en breng de Flipper in een bekende CLI-toestand."""

        def op(gen: int, attempt: _Attempt) -> None:
            self._ensure_cli(gen)
            if self._profile_setting.strip().lower() not in ("", "auto"):
                self._get_profile(gen)

        self._run("connect", op)

    def close(self) -> None:
        """Sluit de verbinding; een lopende operatie wordt eerst afgebroken."""
        self._bump_generation()
        with self._lock:
            self._drop_connection()

    def abort(self) -> None:
        """Breek lopende en wachtende operaties af. Blokkeert niet op de I/O-lock."""
        self._bump_generation()
        # Een draaiende operatie ruimt zelf op (finally). Alleen als het apparaat vrij is maar
        # er toch een app open staat (eerdere mislukte opruiming) sluiten we die hier.
        if self._app_active and self._mode == "rpc" and self._lock.acquire(blocking=False):
            try:
                client = self._client
                if client is not None and self._app_active:
                    self._exit_app(client, timeout=1.0)
            finally:
                self._lock.release()

    # --- publieke acties --------------------------------------------------------------

    def system_info(self) -> dict[str, str]:
        """DeviceInfo van de Flipper (sleutels met '_', bijv. firmware_origin_fork)."""

        def op(gen: int, attempt: _Attempt) -> dict[str, str]:
            return dict(self._fetch_device_info(gen))

        return self._run("system_info", op)

    def storage_list(self, path: str) -> list[dict[str, Any]]:
        """Lijst een map: [{name, type: 'file'|'dir', size}]."""

        def op(gen: int, attempt: _Attempt) -> list[dict[str, Any]]:
            client = self._ensure_rpc(gen)
            main = flipper_pb2.Main()
            main.storage_list_request.path = path
            frames = client.request(main, self._timing.rpc_timeout_s, cancel=self._cancel(gen))
            entries: list[dict[str, Any]] = []
            for frame in frames:
                if frame.WhichOneof("content") != "storage_list_response":
                    continue
                for f in frame.storage_list_response.file:
                    entries.append(
                        {
                            "name": f.name,
                            "type": "dir" if f.type == storage_pb2.File.DIR else "file",
                            "size": int(f.size),
                        }
                    )
            return entries

        return self._run("storage_list", op)

    def storage_read(self, path: str, max_bytes: int) -> bytes:
        """Lees een bestand; weigert mappen en bestanden groter dan `max_bytes`."""

        def op(gen: int, attempt: _Attempt) -> bytes:
            client = self._ensure_rpc(gen)
            cancel = self._cancel(gen)
            stat = flipper_pb2.Main()
            stat.storage_stat_request.path = path
            frames = client.request(stat, self._timing.rpc_timeout_s, cancel=cancel)
            info = frames[0].storage_stat_response.file
            if info.type == storage_pb2.File.DIR:
                raise DeviceError(f"{path} is een map, geen bestand")
            if info.size > max_bytes:
                raise DeviceError(
                    f"{path} is {info.size} bytes; groter dan de limiet van {max_bytes} bytes"
                )
            read = flipper_pb2.Main()
            read.storage_read_request.path = path
            chunks = client.request(read, self._timing.rpc_timeout_s, cancel=cancel)
            data = b"".join(
                bytes(c.storage_read_response.file.data)
                for c in chunks
                if c.WhichOneof("content") == "storage_read_response"
            )
            if len(data) > max_bytes:
                raise DeviceError(f"{path}: meer data ontvangen dan de limiet van {max_bytes}")
            return data

        return self._run("storage_read", op)

    def subghz_rx(self, frequency_hz: int, duration_s: float) -> dict[str, Any]:
        """Luister `duration_s` seconden op `frequency_hz` met de interne CC1101."""

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            profile = self._get_profile(gen)
            command = profile.subghz_rx_command(frequency_hz)  # valideert de frequentie
            self._ensure_cli(gen)
            body = self._cli_exec(
                gen,
                command,
                prompts=(profile.main_prompt,),
                stop_after_s=duration_s,
            )
            parsed = profile.parse_subghz_rx(body)
            return {
                "frequency_hz": frequency_hz,
                "tuned_frequency_hz": parsed.tuned_frequency_hz,
                "duration_s": duration_s,
                "packets": parsed.packets,
                "packet_count": parsed.packet_count,
                "raw": body[:_MAX_RAW_CHARS],
            }

        return self._run("subghz_rx", op)

    def rfid_read(self, timeout_s: float) -> dict[str, Any]:
        """Lees een 125 kHz-tag (stopt zodra er een tag is of na `timeout_s`)."""

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            profile = self._get_profile(gen)
            self._ensure_cli(gen)
            body = self._cli_exec(
                gen,
                profile.rfid_read_command,
                prompts=(profile.main_prompt,),
                stop_after_s=timeout_s,
            )
            parsed = profile.parse_rfid_read(body)
            return {
                "found": parsed.found,
                "protocol": parsed.protocol,
                "data_hex": parsed.data_hex,
                "info": parsed.info,
                "raw": body[:_MAX_RAW_CHARS],
            }

        return self._run("rfid_read", op)

    def nfc_scan(self, timeout_s: float) -> dict[str, Any]:
        """Detecteer een NFC-tag via de NFC-subshell (`nfc` -> `scanner -t` -> `exit`)."""

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            profile = self._get_profile(gen)
            self._ensure_cli(gen)
            entered = self._cli_exec(
                gen,
                profile.nfc_enter_command,
                prompts=(profile.nfc_prompt, profile.main_prompt),
                keep_prompt=True,
            )
            if _last_line(entered) != profile.nfc_prompt:
                if profile.nfc_busy_marker in entered:
                    raise DeviceError(
                        "De NFC-app draait op de Flipper; sluit die eerst (de NFC-CLI start "
                        "niet naast de NFC-app)."
                    )
                raise DeviceError(
                    "De NFC-subshell startte niet; ondersteunt deze firmware 'nfc' in de CLI? "
                    f"({_first_nonblank(entered)[:100]!r})"
                )
            try:
                body = self._cli_exec(
                    gen,
                    profile.nfc_scan_command,
                    prompts=(profile.nfc_prompt,),
                    stop_after_s=timeout_s,
                )
            finally:
                self._leave_nfc_shell(profile)
            parsed = profile.parse_nfc_scanner(body)
            return {
                "found": parsed.found,
                "protocols": parsed.protocols,
                "raw": body[:_MAX_RAW_CHARS],
            }

        return self._run("nfc_scan", op)

    def ir_send(self, file: str, button: str) -> dict[str, Any]:
        """Verstuur één knop uit een .ir-bestand via de Infrared-app in RPC-modus.

        Volgorde (rpc_app.c, infrared_scene_rpc.c): app_start(Infrared, "RPC") -> event
        APP_STARTED -> app_load_file -> app_button_press_release(naam) -> app_exit.
        """

        def op(gen: int, attempt: _Attempt) -> dict[str, Any]:
            client = self._ensure_rpc(gen)
            cancel = self._cancel(gen)
            timeout = self._timing.rpc_timeout_s
            try:
                start = flipper_pb2.Main()
                start.app_start_request.name = "Infrared"
                start.app_start_request.args = "RPC"  # rpc_app.c vervangt dit door de context
                client.request(start, timeout, cancel=cancel)
                self._app_active = True
                started = client.wait_event(
                    lambda m: _is_app_state(m, application_pb2.APP_STARTED),
                    self._timing.app_event_timeout_s,
                    cancel=cancel,
                )
                if started is None:
                    raise DeviceTimeout("Infrared-app meldde niet dat ze gestart is")
                load = flipper_pb2.Main()
                load.app_load_file_request.path = file
                self._request_app_cmd(client, load, timeout, cancel, f"bestand {file} laden")
                attempt.committed = True
                self._press_button(client, button, timeout, cancel)
            finally:
                if self._app_active:
                    self._exit_app(client, timeout=self._timing.app_exit_timeout_s)
            return {"file": file, "button": button, "sent": True}

        return self._run("ir_send", op)

    # --- operatiekader ----------------------------------------------------------------

    def _bump_generation(self) -> None:
        with self._gen_lock:
            self._abort_gen += 1

    def _cancel(self, gen: int) -> Callable[[], bool]:
        return lambda: self._abort_gen != gen

    @contextmanager
    def _operation(self, name: str) -> Iterator[int]:
        # De generatie wordt vóór het wachten op de lock vastgelegd: een abort() terwijl deze
        # call nog in de rij staat, breekt hem dus ook af.
        gen = self._abort_gen
        with self._lock:
            self._busy_op = name
            try:
                if self._abort_gen != gen:
                    raise Aborted("afgebroken voordat de Flipper-actie begon")
                yield gen
            finally:
                self._busy_op = None

    def _run(self, name: str, op: Callable[[int, _Attempt], T]) -> T:
        """Voer `op` uit onder de lock, met één reconnect bij een transportfout."""
        with self._operation(name) as gen:
            attempt = _Attempt()
            try:
                self._ensure_open()
                return op(gen, attempt)
            except TransportError as exc:
                self._drop_connection()
                if attempt.committed:
                    raise DeviceError(
                        f"verbinding met de Flipper viel weg tijdens {name}: {exc}"
                    ) from exc
                log.warning("transportfout bij %s (%s); één keer opnieuw verbinden", name, exc)
            except (FrameError, DeviceTimeout):
                # Na een desync of time-out weten we niet meer in welke modus de poort staat.
                self._forget_mode()
                raise
            try:
                self._ensure_open()
                return op(gen, _Attempt())
            except TransportError as exc:
                self._drop_connection()
                raise DeviceError(f"Flipper-verbinding mislukt na reconnect: {exc}") from exc
            except (FrameError, DeviceTimeout):
                self._forget_mode()
                raise

    def _forget_mode(self, *, fresh_shell: bool = False) -> None:
        """Modus kwijt. `fresh_shell`: poort is net (her)opend, dus zeker geen RPC-sessie."""
        self._maybe_rpc = False if fresh_shell else (self._maybe_rpc or self._mode == "rpc")
        self._mode = "unknown"
        self._client = None
        self._app_active = False

    def _tx(self) -> ByteTransport:
        if self._transport is None:
            raise DeviceNotConnected("Flipper-transport is niet geopend")
        return self._transport

    def _ensure_open(self) -> None:
        transport = self._transport
        if transport is not None and transport.is_open:
            return
        if transport is None:
            transport = self._factory()
            self._transport = transport
        transport.open()
        # Na (her)openen begint DTR een verse CLI-shell (cli_vcp.c): toestand opnieuw bepalen.
        self._forget_mode(fresh_shell=True)
        self._device_info = None
        if self._profile_setting.strip().lower() in ("", "auto"):
            self._profile = None

    def _drop_connection(self) -> None:
        transport = self._transport
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:  # noqa: BLE001 - opruimen mag nooit de oorspronkelijke fout verhullen
                log.debug("fout bij sluiten transport: %s", exc)
        self._forget_mode()
        self._device_info = None
        if self._profile_setting.strip().lower() in ("", "auto"):
            self._profile = None

    # --- ruwe leesprimitieven ---------------------------------------------------------

    def _read_until_prompt(
        self, suffix_ok: Callable[[str], bool], timeout: float, gen: int | None
    ) -> str | None:
        """Lees tot de laatste regel aan `suffix_ok` voldoet; None bij time-out."""
        transport = self._tx()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            if gen is not None and self._abort_gen != gen:
                raise Aborted("afgebroken tijdens het wachten op de Flipper-prompt")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            chunk = transport.read(4096, min(self._timing.read_slice_s, remaining))
            if chunk:
                buf.extend(chunk)
                text = _clean(buf)
                if suffix_ok(_last_line(text)):
                    return text

    def _settle(self) -> None:
        """Wacht tot de poort even stil is (laatste echo's/prompts na Ctrl+C weglezen)."""
        transport = self._tx()
        end = time.monotonic() + self._timing.settle_max_s
        while time.monotonic() < end:
            if not transport.read(4096, self._timing.settle_quiet_s):
                return

    # --- moduswissel ------------------------------------------------------------------

    def _sync_cli(self, gen: int | None) -> None:
        """Breng de poort naar de hoofd-CLI-prompt, vanuit elke bekende of onbekende toestand."""
        transport = self._tx()
        timing = self._timing
        if self._maybe_rpc:
            # Een geldig stop_session-frame op een frame-grens sluit de sessie schoon; Ctrl+C
            # zou in de RPC-stroom als lengtebyte gelezen worden.
            self._blind_stop_session(gen)
            self._maybe_rpc = False
        for attempt in range(3):
            transport.reset_input()
            transport.write(_ETX)  # leegt een half getypte regel en toont de prompt opnieuw
            text = self._read_until_prompt(
                lambda line: line.endswith(_GENERIC_PROMPT_SUFFIX), timing.prompt_timeout_s, gen
            )
            if text is None:
                if attempt == 1:
                    self._blind_stop_session(gen)
                continue
            line = _last_line(text)
            if line != _GENERIC_PROMPT_SUFFIX:
                # Subshell (bijv. "[nfc]>: ") nog open: netjes verlaten.
                transport.write(b"exit\r")
                text = self._read_until_prompt(
                    lambda ln: ln == _GENERIC_PROMPT_SUFFIX, timing.prompt_timeout_s, gen
                )
                if text is None:
                    continue
            self._settle()
            self._mode = "cli"
            self._client = None
            self._app_active = False
            return
        self._mode = "unknown"
        raise DeviceTimeout("Flipper-CLI reageert niet (geen prompt na Ctrl+C)")

    def _blind_stop_session(self, gen: int | None) -> None:
        """Poort reageert niet op CLI-tekst: mogelijk staat er nog een RPC-sessie open."""
        transport = self._tx()
        main = flipper_pb2.Main()
        main.command_id = 1
        main.stop_session.SetInParent()
        transport.reset_input()
        transport.write(encode_frame(main.SerializeToString()))
        self._read_until_prompt(
            lambda line: line == _GENERIC_PROMPT_SUFFIX, self._timing.prompt_timeout_s, gen
        )

    def _ensure_cli(self, gen: int | None) -> None:
        if self._mode == "cli":
            return
        if self._mode == "rpc" and self._client is not None and self._leave_rpc(gen):
            return
        self._sync_cli(gen)

    def _leave_rpc(self, gen: int | None) -> bool:
        """RPC -> CLI via stop_session (rpc.c); True als de CLI-prompt terug is."""
        client = self._client
        if client is None:
            return False
        self._mode = "unknown"  # pas "cli" als de prompt echt terug is
        text: str | None = None
        try:
            if self._app_active:
                self._exit_app(client, timeout=self._timing.app_exit_timeout_s)
            main = flipper_pb2.Main()
            main.stop_session.SetInParent()
            client.send(main)
            # Het OK-antwoord en de nieuwe prompt komen achter elkaar binnen; daarom lezen we
            # ruw door tot de prompt in plaats van het antwoord via de client te consumeren.
            text = self._read_until_prompt(
                lambda line: line == _GENERIC_PROMPT_SUFFIX, self._timing.prompt_timeout_s, gen
            )
        except TransportError:
            raise
        except DeviceError as exc:
            log.debug("stop_session mislukte: %s", exc)
        self._client = None
        if text is None:
            return False
        self._settle()
        self._mode = "cli"
        return True

    def _ensure_rpc(self, gen: int | None) -> RpcClient:
        if self._mode == "rpc" and self._client is not None:
            return self._client
        self._sync_cli(gen)
        transport = self._tx()
        transport.reset_input()
        self._mode = "unknown"  # vanaf hier kan de poort in RPC staan; pas na de probe "rpc"
        self._maybe_rpc = True
        # CR alleen, geen CRLF: de '\n' zou als eerste varint-byte worden gelezen (busse/…).
        transport.write(b"start_rpc_session\r")
        self._wait_echo(b"start_rpc_session", gen)
        transport.reset_input()
        timing = self._timing
        for _ in range(timing.probe_attempts):
            client = RpcClient(transport, read_slice_s=timing.read_slice_s)
            ping = flipper_pb2.Main()
            ping.system_ping_request.data = b"pitoolbox"
            try:
                reply = client.request(
                    ping, timing.probe_timeout_s, cancel=self._cancel_or_none(gen)
                )
            except (DeviceTimeout, FrameError, RpcError) as exc:
                log.debug("RPC-probe mislukt: %s", exc)
                transport.reset_input()
                continue
            if reply[0].system_ping_response.data == b"pitoolbox":
                self._client = client
                self._mode = "rpc"
                return client
        self._mode = "unknown"
        raise DeviceError("De RPC-sessie op de Flipper startte niet (geen ping-antwoord)")

    def _cancel_or_none(self, gen: int | None) -> Callable[[], bool] | None:
        return None if gen is None else self._cancel(gen)

    def _wait_echo(self, needle: bytes, gen: int | None) -> None:
        """Wacht (best effort) tot de CLI de getypte regel heeft teruggegeven."""
        transport = self._tx()
        buf = bytearray()
        deadline = time.monotonic() + self._timing.prompt_timeout_s
        while time.monotonic() < deadline:
            if gen is not None and self._abort_gen != gen:
                raise Aborted("afgebroken tijdens het starten van de RPC-sessie")
            chunk = transport.read(4096, self._timing.read_slice_s)
            if chunk:
                buf.extend(chunk)
                if needle + b"\r\n" in bytes(buf) or needle + b"\n" in bytes(buf):
                    return

    # --- CLI-uitvoering ---------------------------------------------------------------

    def _cli_exec(
        self,
        gen: int | None,
        command: str,
        *,
        prompts: tuple[str, ...],
        stop_after_s: float | None = None,
        keep_prompt: bool = False,
    ) -> str:
        """Stuur één vaste CLI-regel en lees tot een van `prompts` terug is.

        Met `stop_after_s` wordt na die tijd Ctrl+C gestuurd (voor commando's die tot Ctrl+C
        doorlopen, zoals `subghz rx`); daarna krijgt het commando `stop_grace_s` om af te
        ronden. `keep_prompt` laat de slotprompt in de tekst staan.
        """
        transport = self._tx()
        timing = self._timing
        transport.write(command.encode("ascii") + b"\r")
        buf = bytearray()
        start = time.monotonic()
        etx_sent = False
        limit = (
            stop_after_s + timing.stop_grace_s
            if stop_after_s is not None
            else timing.command_timeout_s
        )
        while True:
            if gen is not None and self._abort_gen != gen:
                self._interrupt(prompts)
                raise Aborted(f"'{command}' afgebroken")
            elapsed = time.monotonic() - start
            if stop_after_s is not None and not etx_sent and elapsed >= stop_after_s:
                transport.write(_ETX)
                etx_sent = True
            if elapsed >= limit:
                if not etx_sent:
                    transport.write(_ETX)
                self._mode = "unknown"
                raise DeviceTimeout(f"Flipper-commando '{command}' kwam niet terug op de prompt")
            chunk = transport.read(4096, timing.read_slice_s)
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _MAX_CLI_BYTES:
                transport.write(_ETX)
                self._mode = "unknown"
                raise DeviceError(f"te veel uitvoer van '{command}'; afgebroken")
            text = _clean(buf)
            if _last_line(text) in prompts:
                if etx_sent:
                    self._settle()
                body = _strip_echo(text, command)
                return body if keep_prompt else _strip_prompt(body, prompts)

    def _interrupt(self, prompts: tuple[str, ...]) -> None:
        """Ctrl+C sturen en op de prompt wachten (best effort, na een abort)."""
        transport = self._tx()
        transport.write(_ETX)
        text = self._read_until_prompt(
            lambda line: line in prompts, self._timing.prompt_timeout_s, None
        )
        if text is None:
            self._mode = "unknown"
        else:
            self._settle()

    def _leave_nfc_shell(self, profile: CliProfile) -> None:
        """Verlaat de NFC-subshell; zonder abort-controle zodat opruimen altijd doorgaat."""
        try:
            self._cli_exec(
                None,
                profile.nfc_exit_command,
                prompts=(profile.main_prompt,),
            )
        except DeviceError as exc:
            log.warning("NFC-subshell verlaten mislukte: %s", exc)
            self._mode = "unknown"

    # --- profiel en device-info -------------------------------------------------------

    def _fetch_device_info(self, gen: int | None) -> dict[str, str]:
        client = self._ensure_rpc(gen)
        main = flipper_pb2.Main()
        main.system_device_info_request.SetInParent()
        frames = client.request(main, self._timing.rpc_timeout_s, cancel=self._cancel_or_none(gen))
        info: dict[str, str] = {}
        for frame in frames:
            if frame.WhichOneof("content") == "system_device_info_response":
                resp = frame.system_device_info_response
                if resp.key:
                    info[str(resp.key)] = str(resp.value)
        self._device_info = info
        return info

    def _get_profile(self, gen: int | None) -> CliProfile:
        if self._profile is not None:
            return self._profile
        if self._profile_setting.strip().lower() in ("", "auto"):
            info = (
                self._device_info if self._device_info is not None else self._fetch_device_info(gen)
            )
            self._profile = select_profile(self._profile_setting, info)
        else:
            self._profile = select_profile(self._profile_setting, None)
        return self._profile

    # --- RPC-hulpjes voor de Infrared-app ---------------------------------------------

    def _request_app_cmd(
        self,
        client: RpcClient,
        main: Main,
        timeout: float,
        cancel: Callable[[], bool] | None,
        what: str,
    ) -> None:
        try:
            client.request(main, timeout, cancel=cancel)
        except RpcError as exc:
            if exc.status_name != "ERROR_APP_CMD_ERROR":
                raise
            raise DeviceError(f"Flipper-app: {what} mislukt{self._app_error_text(client)}") from exc

    def _app_error_text(self, client: RpcClient) -> str:
        """Foutmelding van de app (app_get_error_request), best effort."""
        try:
            main = flipper_pb2.Main()
            main.app_get_error_request.SetInParent()
            frames = client.request(main, 2.0)
            text = str(frames[0].app_get_error_response.text).replace("\n", " ").strip()
            return f": {text}" if text else ""
        except DeviceError:
            return ""

    def _press_button(
        self,
        client: RpcClient,
        button: str,
        timeout: float,
        cancel: Callable[[], bool] | None,
    ) -> None:
        main = flipper_pb2.Main()
        main.app_button_press_release_request.args = button
        try:
            self._request_app_cmd(client, main, timeout, cancel, f"knop {button!r} versturen")
            return
        except RpcError as exc:
            if exc.status_name != "ERROR_NOT_IMPLEMENTED":
                raise
        # Oudere firmware kent app_button_press_release_request niet: los indrukken + loslaten.
        press = flipper_pb2.Main()
        press.app_button_press_request.args = button
        self._request_app_cmd(client, press, timeout, cancel, f"knop {button!r} indrukken")
        release = flipper_pb2.Main()
        release.app_button_release_request.SetInParent()
        client.request(release, timeout)  # loslaten altijd, ook na een abort

    def _exit_app(self, client: RpcClient, timeout: float) -> None:
        """app_exit_request + wachten op APP_CLOSED; nooit een uitzondering (opruimcode)."""
        try:
            main = flipper_pb2.Main()
            main.app_exit_request.SetInParent()
            client.request(main, timeout)
            client.wait_event(lambda m: _is_app_state(m, application_pb2.APP_CLOSED), timeout)
        except DeviceError as exc:
            log.warning("app afsluiten mislukte: %s", exc)
        finally:
            self._app_active = False


def _is_app_state(main: Main, state: int) -> bool:
    return bool(
        main.WhichOneof("content") == "app_state_response"
        and main.app_state_response.state == state
    )


def _first_nonblank(text: str) -> str:
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return ""
