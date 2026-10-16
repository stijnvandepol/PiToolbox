"""Flipper Zero via USB-CDC: wisselt automatisch tussen CLI-modus en RPC-sessie.

Bronnen (zie de verwijzingen bij de afzonderlijke onderdelen):

* USB VID:PID 0483:5740: flipperzero-firmware targets/f7/furi_hal/furi_hal_usb_cdc.c.
* De poort start in CLI-modus (prompt "\\r\\n>: ", cli_shell_line.c). `start_rpc_session`
  (CR, niet CRLF) schakelt dezelfde poort naar nanopb-delimited RPC (rpc_cli.c); een
  `stop_session`-bericht (rpc.c: PB_Main_stop_session_tag) brengt de CLI terug.
* DTR laag/hoog = verbinding verbreken/maken (applications/services/cli/cli_vcp.c): na het
  heropenen van de poort begint er dus altijd een verse CLI-shell.
* Sessiehandshake en CR-only-detail: afgeleid van busse/flipperzero-mcp (MIT).

Naast USB bestaat BLE (`_flipper_ble.BleTransport`): daar is er geen CLI. De Flipper opent bij
een BLE-verbinding meteen een RPC-sessie (applications/services/bt/bt_service/bt.c,
`RpcOwnerBle`); een transport met `starts_in_rpc` krijgt dus nooit CLI-bytes,
`start_rpc_session` of Ctrl+C. Acties die de CLI nodig hebben (Sub-GHz/NFC/RFID) werken alleen
via USB; in "auto"-modus schakelt het device daarvoor over naar USB als er een Flipper op USB zit.

Eén lock rond alle device-I/O. `status()` en `abort()` nemen die lock nooit blokkerend.
`abort()` verhoogt een generatieteller; lopende CLI-lussen en RPC-wachttijden controleren die
elke ~50 ms, sturen Ctrl+C (0x03, `cli_is_pipe_broken_or_is_etx_next_char`) en geven `Aborted`.

**RPC-gekoppelde app (fase 4, de PiToolbox-FAP).** `start_rpc_app` start een FAP met
`args:"RPC"` (nooit als de loader bezet is: een app van de gebruiker wordt nooit gesloten).
Daarna leest elke RPC-lezer (een tool-request, `wait_event` of `poll_app_events`) ongevraagde
frames via een event-hook: DataExchange-frames van die app en zijn `APP_CLOSED` gaan meteen
naar de `AppEvent`-sink (de bridge), niet naar de begrensde eventqueue. `app_send` stuurt één
DataExchange en wacht op de bevestiging (stop-and-wait). `poll_app_events` leest alleen als
niemand anders op de lock wacht, zodat tool-calls voorgaan. `ir_send` heeft de loader nodig:
staat de app open, dan sluit het device hem eerst en start hem daarna opnieuw (handoff; de app
groet dan met een nieuwe instance). CLI-acties weigeren zolang de app open is (`loader_lock`
en het verlaten van de RPC-sessie zouden hem sluiten).

**De FAP installeren** (`install_app_file`, alleen voor `pitoolbox-flipper ui-install`, geen
MCP-tool): alleen naar een pad onder /ext/apps (zelfde regel als `start_rpc_app`), alleen een
geldige PiToolbox-FAP (`_fap.inspect_fap`), en alleen als de loader vrij is. Schrijft eerst
naar `<pad>.part`, controleert de MD5 op de Flipper en hernoemt dan; een mislukte upload laat
de vorige app dus heel.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pitoolbox.core.devices._fap import FAP_APP_NAME, FAP_MAX_BYTES, inspect_fap
from pitoolbox.core.devices._flipper_cli import CliProfile, select_profile
from pitoolbox.core.devices._flipper_rpc.client import Main, RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.framing import FrameError, encode_frame
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2, storage_pb2
from pitoolbox.core.devices._flipper_transport import (
    FLIPPER_PID,
    FLIPPER_VID,
    ByteTransport,
    TransportError,
    UsbSerialTransport,
)
from pitoolbox.core.devices._flipper_types import (
    AppEvent,
    AppEventSink,
    AppLocked,
    AppNotRunning,
    AppRejected,
    DeviceBusy,
    FlipperTiming,
    _Attempt,
)
from pitoolbox.core.errors import (
    Aborted,
    DeviceError,
    DeviceNotConnected,
    DeviceTimeout,
    UnsupportedTransport,
)

if TYPE_CHECKING:
    from pitoolbox.config import Settings

__all__ = [
    "FLIPPER_PID",
    "FLIPPER_VID",
    "STORAGE_CHUNK",
    "AppEvent",
    "AppEventSink",
    "AppInstall",
    "AppLocked",
    "AppNotRunning",
    "AppRejected",
    "ByteTransport",
    "DeviceBusy",
    "DeviceError",
    "FlipperDevice",
    "FlipperTiming",
    "ProgressFn",
    "TransportError",
    "UsbSerialTransport",
    "make_transport",
]

log = logging.getLogger(__name__)

_ETX = b"\x03"
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_GENERIC_PROMPT_SUFFIX = ">: "
_MAX_CLI_BYTES = 1_000_000
_MAX_RAW_CHARS = 16_000

T = TypeVar("T")


# Alleen een FAP op de SD-kaart onder /ext/apps, zonder "..": nooit een willekeurige app.
_APP_PATH_RE = re.compile(r"^/ext/apps/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+\.fap$")
_CLI_BLOCKED_BY_APP = (
    "De PiToolbox-app is open op de Flipper; Sub-GHz/NFC/RFID via de CLI kunnen niet tegelijk "
    "(de Flipper laat geen CLI-actie toe naast een open app). Sluit de app op de Flipper "
    "(Back in het hoofdmenu) en probeer het opnieuw."
)


# Grootte van één blok bij storage_write_request: flipperzero-protobuf `storage.options`
# ("PB_Storage.File.data max_size:512", "not affected by nanopb, so server & client should keep in
# mind these max values"); de officiële client gebruikt hetzelfde (flipper_storage.py rpc_write).
# Eén frame (blok + pad + kop) blijft zo ruim onder RPC_BUFFER_SIZE = 1024 (rpc.h, mntm-012).
STORAGE_CHUNK = 512
# Fouten van storage_mkdir_request die "bestaat al" betekenen (rpc_storage.c: FSE_EXIST).
_MKDIR_EXISTS = "ERROR_STORAGE_EXIST"


@dataclass(frozen=True)
class AppInstall:
    """Resultaat van `install_app_file`."""

    path: str
    size: int
    md5: str
    skipped: bool  # stond er al identiek (zelfde MD5); er is niets geschreven
    seconds: float


ProgressFn = Callable[[int, int], None]


_CLI_NEEDS_USB = (
    "Sub-GHz/NFC/RFID gaan via de Flipper-CLI en die bestaat alleen via USB; "
    "sluit de Flipper aan met USB"
)


def _auto_prefers_usb(settings: Settings) -> bool:
    """'auto': USB als er een Flipper op zit. Een expliciet ingestelde poort wint tenzij er ook
    een BLE-adres is ingesteld (dan is BLE het vangnet als de poort er niet is)."""
    if UsbSerialTransport.is_present(settings.flipper_port):
        return True
    return settings.flipper_port is not None and settings.flipper_ble_address is None


def make_transport(settings: Settings) -> ByteTransport:
    """Kies USB of BLE volgens `flipper_transport` (bij 'auto' op dit moment: USB eerst)."""
    mode = settings.flipper_transport
    if mode == "usb" or (mode == "auto" and _auto_prefers_usb(settings)):
        return UsbSerialTransport(settings.flipper_port)
    # Pas hier importeren: bleak is zwaar en voor USB-gebruik niet nodig.
    from pitoolbox.core.devices._flipper_ble import BleTransport

    return BleTransport(
        settings.flipper_ble_address,
        settings.flipper_ble_name_prefix,
        adapter=settings.flipper_ble_adapter,
    )


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
        keepalive_s: float = 0.0,
        usb_probe: Callable[[], bool] | None = None,
    ) -> None:
        """`keepalive_s` > 0: RPC-ping na zoveel seconden idle. `usb_probe` (alleen in 'auto'):
        zit er nu een Flipper op USB? Dan schakelt een CLI-actie over BLE naar USB over."""
        self._factory = transport_factory
        self._profile_setting = cli_profile
        self._timing = timing or FlipperTiming()
        self._keepalive_s = keepalive_s
        self._usb_probe = usb_probe
        self._last_io = time.monotonic()
        self._ka_thread: threading.Thread | None = None
        self._ka_stop = threading.Event()
        self._lock = threading.Lock()  # alle device-I/O
        self._gen_lock = threading.Lock()
        self._abort_gen = 0
        self._waiters = 0  # threads die op `_lock` wachten (onder `_gen_lock` geteld)
        # RPC-gekoppelde app (PiToolbox-FAP): "off" | "active" | "suspended" (handoff in ir_send).
        self._rpc_app_state: Literal["off", "active", "suspended"] = "off"
        self._rpc_app_path: str | None = None
        self._rpc_app_closing = False  # wij sluiten hem zelf: APP_CLOSED is voor wait_event
        self._app_sink: AppEventSink | None = None
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
        """Lui: verbindt pas bij het eerste gebruik. De transportkeuze (USB/BLE) wordt bij elke
        (her)verbinding opnieuw gemaakt, zodat 'auto' een net aangesloten kabel oppakt."""
        port = settings.flipper_port

        def probe() -> bool:
            return UsbSerialTransport.is_present(port)

        usb_probe = probe if settings.flipper_transport == "auto" else None

        return cls(
            lambda: make_transport(settings),
            settings.flipper_cli_profile,
            keepalive_s=settings.flipper_keepalive_s,
            usb_probe=usb_probe,
        )

    # --- publieke levenscyclus --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Momentopname zonder I/O, zonder lock en zonder te verbinden."""
        transport = self._transport
        profile = self._profile
        return {
            "connected": transport is not None and transport.is_open,
            "transport": transport.describe() if transport is not None else "nog niet geopend",
            "transport_kind": transport.kind if transport is not None else None,
            "mode": self._mode,
            "busy": self._busy_op is not None,
            "operation": self._busy_op,
            "profile": profile.name if profile is not None else None,
            "ui_app": self._rpc_app_state,
        }

    def connect(self) -> None:
        """Open de poort en breng de Flipper in een bekende CLI-toestand."""

        def op(gen: int, attempt: _Attempt) -> None:
            if self._tx().starts_in_rpc:
                self._ensure_rpc(gen)  # BLE: geen CLI, de sessie loopt al
                return
            self._ensure_cli(gen)
            if self._profile_setting.strip().lower() not in ("", "auto"):
                self._get_profile(gen)

        self._run("connect", op)

    def close(self) -> None:
        """Sluit de verbinding; een lopende operatie wordt eerst afgebroken."""
        self._bump_generation()
        self._stop_keepalive()
        with self._lock:
            self._drop_connection()

    def abort(self) -> None:
        """Breek lopende en wachtende operaties af. Blokkeert niet op de I/O-lock."""
        self._bump_generation()
        # Een schrijfactie die op BLE-flow-control wacht kent de generatieteller niet.
        interrupt = getattr(self._transport, "interrupt", None)
        if callable(interrupt):
            interrupt()
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

    def ping(self, payload: bytes = b"pitoolbox") -> float:
        """Eén RPC-ping (system_ping_request); geeft de round-trip in seconden terug."""

        def op(gen: int, attempt: _Attempt) -> float:
            client = self._ensure_rpc(gen)
            main = flipper_pb2.Main()
            main.system_ping_request.data = payload
            t0 = time.monotonic()
            frames = client.request(main, self._timing.rpc_timeout_s, cancel=self._cancel(gen))
            elapsed = time.monotonic() - t0
            if frames[0].system_ping_response.data != payload:
                raise DeviceError("ping-antwoord komt niet overeen met de verzonden data")
            return elapsed

        return self._run("ping", op)

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
            self._require_cli_transport()
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
            self._require_cli_transport()
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
            self._require_cli_transport()
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
            # De Infrared-app heeft de loader nodig: de PiToolbox-app eerst sluiten (handoff).
            handoff = self._suspend_rpc_app(client)
            try:
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
            finally:
                if handoff is not None:
                    self._resume_rpc_app(client, handoff)
            return {"file": file, "button": button, "sent": True}

        return self._run("ir_send", op)

    # --- RPC-gekoppelde app (PiToolbox-FAP, fase 4) -----------------------------------

    def set_app_sink(self, sink: AppEventSink | None) -> None:
        """Ontvanger van `AppEvent`s (de bridge). Wordt aangeroepen in de thread die het device
        leest, met de device-lock vast: alleen iets in een eigen wachtrij zetten."""
        self._app_sink = sink

    @property
    def rpc_app_state(self) -> str:
        """'off', 'active' of 'suspended' (tijdelijk dicht voor ir_send). Zonder lock."""
        return self._rpc_app_state

    def app_lock_status(self, *, idle_only: bool = False) -> bool:
        """Is de loader bezet (er is een app open)? `idle_only`: `DeviceBusy` als het device
        bezet is, in plaats van te wachten. Verbindt zo nodig."""

        def op(gen: int, attempt: _Attempt) -> bool:
            return self._loader_locked(self._ensure_rpc(None))

        return self._run_maybe_idle("app_lock_status", op, idle_only)

    def start_rpc_app(self, path: str, *, lock_wait_s: float | None = None) -> None:
        """Start de FAP op `path` met `args:"RPC"` en wacht op `APP_STARTED`.

        Alleen als de loader vrij is (hoogstens `lock_wait_s`, standaard `loader_free_wait_s`,
        wachten op een app die net sluit). Een open app van de gebruiker wordt nooit gesloten:
        dan `AppLocked`.
        """
        if not _APP_PATH_RE.fullmatch(path) or ".." in path:
            raise ValueError(f"ongeldig app-pad: {path!r}")

        def op(gen: int, attempt: _Attempt) -> None:
            if self._rpc_app_state != "off":
                return
            client = self._ensure_rpc(None)
            if not self._wait_loader_free(client, lock_wait_s):
                raise AppLocked("er is een andere app open op de Flipper; die sluit de Pi niet")
            self._start_rpc_app(client, path)

        self._run("start_rpc_app", op)

    def exit_rpc_app(self) -> None:
        """Sluit de gekoppelde app (bij afsluiten van de bridge). Best effort, nooit een fout."""
        if self._rpc_app_state == "off":
            return
        try:
            self._run_linked("exit_rpc_app", self._exit_rpc_app_locked)
        except DeviceError as exc:
            log.debug("app afsluiten: %s", exc)
        finally:
            with self._lock:
                # "closed", niet "lost": bewust gesloten, dus niet opnieuw starten.
                self._rpc_app_lost("afgesloten door de Pi", kind="closed")

    def app_send(self, data: bytes, timeout_s: float = 3.0) -> None:
        """Eén DataExchange naar de gekoppelde app; terug zodra de app hem bevestigt.

        Raises:
            AppRejected: de app weigerde (ongeldig of wachtrij vol); later opnieuw proberen.
            AppNotRunning: de app draait niet (meer).
            DeviceTimeout: geen bevestiging binnen `timeout_s`: de sessie geldt als dood.
            DeviceError: verbinding weg.
        """
        if not 1 <= len(data) <= 512:
            raise ValueError("DataExchange van 1..512 bytes")

        def op() -> None:
            client = self._client
            if self._rpc_app_state != "active" or self._mode != "rpc" or client is None:
                raise AppNotRunning("de PiToolbox-app draait niet")
            main = flipper_pb2.Main()
            main.app_data_exchange_request.data = data
            try:
                client.request(main, timeout_s)
            except RpcError as exc:
                if exc.status_name == "ERROR_APP_CMD_ERROR":
                    raise AppRejected("de PiToolbox-app weigerde het bericht") from exc
                if exc.status_name == "ERROR_APP_NOT_RUNNING":
                    self._rpc_app_lost("app draait niet meer")
                    raise AppNotRunning("de PiToolbox-app draait niet meer") from exc
                raise

        self._run_linked("app_send", op)

    def poll_app_events(self, max_wait_s: float) -> bool:
        """Lees ongevraagde frames (bridge-lezer). Alleen als niemand anders het device nodig
        heeft: wacht er iemand op de lock, dan stopt het lezen binnen één leesslice.

        Geeft False als er niets te lezen viel (geen RPC-sessie, of bezet): de aanroeper
        wacht dan even voordat hij het opnieuw probeert.
        """

        def op() -> bool:
            client = self._client
            if self._mode != "rpc" or client is None:
                return False
            client.pump(max_wait_s, stop=lambda: self._waiters > 0)
            return True

        try:
            return self._run_linked("poll_app_events", op, blocking=False, touch_io=False)
        except DeviceBusy:
            return False
        except DeviceError as exc:
            log.debug("poll_app_events: %s", exc)
            return False

    def take_small_file(
        self, path: str, max_bytes: int, *, idle_only: bool = False
    ) -> bytes | None:
        """Lees een klein bestand en wis het meteen (verzoekbestand van de FAP, §8).

        None als het niet bestaat of een map is. Groter dan `max_bytes`: gewist en None.
        """

        def op(gen: int, attempt: _Attempt) -> bytes | None:
            client = self._ensure_rpc(None)
            timeout = self._timing.rpc_timeout_s
            stat = flipper_pb2.Main()
            stat.storage_stat_request.path = path
            try:
                frames = client.request(stat, timeout)
            except RpcError as exc:
                if exc.status_name == "ERROR_STORAGE_NOT_EXIST":
                    return None
                raise
            info = frames[0].storage_stat_response.file
            if info.type == storage_pb2.File.DIR:
                return None
            data: bytes | None = None
            if info.size <= max_bytes:
                read = flipper_pb2.Main()
                read.storage_read_request.path = path
                chunks = client.request(read, timeout)
                data = b"".join(
                    bytes(c.storage_read_response.file.data)
                    for c in chunks
                    if c.WhichOneof("content") == "storage_read_response"
                )[: max_bytes + 1]
            delete = flipper_pb2.Main()
            delete.storage_delete_request.path = path
            client.request(delete, timeout)
            return data if data is not None and len(data) <= max_bytes else None

        return self._run_maybe_idle("take_small_file", op, idle_only)

    def rtc_timestamp(self) -> int:
        """Klok van de Flipper als Unix-tijd, zoals `datetime_datetime_to_timestamp` rekent
        (de RTC is "UTC" zonder zones; calendar.timegm doet hetzelfde)."""

        def op(gen: int, attempt: _Attempt) -> int:
            client = self._ensure_rpc(None)
            main = flipper_pb2.Main()
            main.system_get_datetime_request.SetInParent()
            frames = client.request(main, self._timing.rpc_timeout_s)
            dt = frames[0].system_get_datetime_response.datetime
            return calendar.timegm(
                (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, 0, 0, 0)
            )

        return self._run("rtc_timestamp", op)

    def install_app_file(
        self,
        path: str,
        data: bytes,
        *,
        progress: ProgressFn | None = None,
        skip_if_same: bool = True,
    ) -> AppInstall:
        """Zet de PiToolbox-FAP op `path` (onder /ext/apps, eindigt op .fap).

        Weigert (`AppLocked`) als de gekoppelde app draait of de loader bezet is: een draaiende
        FAP overschrijven is niet veilig, en welke app open is ziet de Pi niet. Volgorde:
        bestaande MD5 vergelijken (`skip_if_same`), mappen maken, schrijven naar `<path>.part`
        in blokken van `STORAGE_CHUNK`, MD5 van de Flipper vergelijken, hernoemen naar `path`
        (storage_common_rename verwijdert een bestaand doel eerst, storage_external_api.c), en
        de MD5 op `path` nog één keer controleren. `progress(bytes_verstuurd, totaal)`.

        Raises:
            ValueError: ongeldig pad, of `data` is geen geldige PiToolbox-FAP.
            AppLocked: er is een app open.
            DeviceError: schrijven of controleren mislukt (de oude app blijft dan staan).
        """
        if not _APP_PATH_RE.fullmatch(path) or ".." in path:
            raise ValueError(f"ongeldig app-pad: {path!r}")
        if not 0 < len(data) <= FAP_MAX_BYTES:
            raise ValueError(f"FAP van {len(data)} bytes; toegestaan 1..{FAP_MAX_BYTES}")
        info = inspect_fap(data)  # FapError is een ValueError
        if info.name != FAP_APP_NAME:
            raise ValueError(f"dit is niet de PiToolbox-app maar {info.name!r}")
        expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
        chunks = [data[i : i + STORAGE_CHUNK] for i in range(0, len(data), STORAGE_CHUNK)]
        part = f"{path}.part"

        def op(gen: int, attempt: _Attempt) -> AppInstall:
            started = time.monotonic()
            if self._rpc_app_state != "off":
                raise AppLocked("de PiToolbox-app draait; sluit hem eerst (Back op de Flipper)")
            client = self._ensure_rpc(gen)
            cancel = self._cancel(gen)
            if self._loader_locked(client):
                raise AppLocked(
                    "er is een app open op de Flipper; sluit die eerst (de Pi overschrijft geen "
                    "app die mogelijk draait, en sluit nooit een app van jou)"
                )
            if skip_if_same and self._md5sum(client, path, cancel) == expected:
                return AppInstall(path, len(data), expected, True, time.monotonic() - started)
            self._mkdir_parents(client, path, cancel)
            attempt.committed = True  # vanaf hier geen stille herhaling na een reconnect
            try:
                self._write_chunks(client, part, chunks, cancel, progress, len(data))
                written = self._md5sum(client, part, cancel)
                if written != expected:
                    raise DeviceError(
                        f"MD5 op de Flipper ({written or 'geen'}) wijkt af van het bestand "
                        f"({expected}); de oude app blijft staan"
                    )
                rename = flipper_pb2.Main()
                rename.storage_rename_request.old_path = part
                rename.storage_rename_request.new_path = path
                client.request(rename, self._timing.rpc_timeout_s, cancel=cancel)
            except BaseException:
                self._delete_quietly(client, part)
                raise
            final = self._md5sum(client, path, cancel)
            if final != expected:
                raise DeviceError(f"MD5 na hernoemen klopt niet ({final or 'geen'})")
            return AppInstall(path, len(data), expected, False, time.monotonic() - started)

        return self._run("install_app_file", op)

    def _md5sum(self, client: RpcClient, path: str, cancel: Callable[[], bool]) -> str | None:
        """MD5 (kleine letters) van een bestand op de Flipper; None als het niet bestaat.
        storage_md5sum_request/-response (storage.proto; md5sum max 32 tekens, storage.options)."""
        main = flipper_pb2.Main()
        main.storage_md5sum_request.path = path
        try:
            frames = client.request(main, self._timing.rpc_timeout_s, cancel=cancel)
        except RpcError as exc:
            if exc.status_name == "ERROR_STORAGE_NOT_EXIST":
                return None
            raise
        return str(frames[0].storage_md5sum_response.md5sum).strip().lower()

    def _mkdir_parents(self, client: RpcClient, path: str, cancel: Callable[[], bool]) -> None:
        """Maak de mappen tussen /ext en het bestand (mkdir maakt één niveau; FSE_EXIST is ok)."""
        parts = path.split("/")[2:-1]  # ["", "ext", "apps", "Tools", "x.fap"] -> apps, Tools
        current = "/ext"
        for name in parts:
            current = f"{current}/{name}"
            main = flipper_pb2.Main()
            main.storage_mkdir_request.path = current
            try:
                client.request(main, self._timing.rpc_timeout_s, cancel=cancel)
            except RpcError as exc:
                if exc.status_name != _MKDIR_EXISTS:
                    raise

    def _write_chunks(
        self,
        client: RpcClient,
        path: str,
        chunks: list[bytes],
        cancel: Callable[[], bool],
        progress: ProgressFn | None,
        total: int,
    ) -> None:
        mains = []
        for chunk in chunks:
            main = flipper_pb2.Main()
            main.storage_write_request.path = path
            main.storage_write_request.file.data = chunk
            mains.append(main)

        def on_chunk(done: int, _count: int) -> None:
            if progress is not None:
                progress(min(done * STORAGE_CHUNK, total), total)

        client.request_chunked(mains, self._timing.rpc_timeout_s, cancel=cancel, on_chunk=on_chunk)

    def _delete_quietly(self, client: RpcClient, path: str) -> None:
        """Opruimen na een mislukte upload; nooit een fout (de link kan al weg zijn)."""
        transport = self._transport
        if transport is None or not transport.is_open or self._client is not client:
            return
        try:
            main = flipper_pb2.Main()
            main.storage_delete_request.path = path
            client.request(main, self._timing.rpc_timeout_s)
        except Exception as exc:  # noqa: BLE001 - opruimen mag de echte fout niet verhullen
            log.debug("%s opruimen: %s", path, exc)

    def _run_maybe_idle(self, name: str, op: Callable[[int, _Attempt], T], idle_only: bool) -> T:
        if not idle_only:
            return self._run(name, op)
        # Eerst kijken zonder te wachten; daarna gewoon via _run (de lock is dan net vrij).
        if self._waiters or self._busy_op is not None:
            raise DeviceBusy(f"Flipper is bezet; {name} overgeslagen")
        return self._run(name, op)

    # --- intern: gekoppelde app ---------------------------------------------------------

    def _emit_app(self, event: AppEvent) -> None:
        sink = self._app_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            log.exception("app-sink faalde")

    def _rpc_app_lost(self, reason: str, kind: Literal["closed", "lost"] = "lost") -> None:
        """De gekoppelde app is weg (ook als hij voor een handoff dicht was)."""
        if self._rpc_app_state == "off":
            return
        self._rpc_app_state = "off"
        self._rpc_app_closing = False
        log.info("PiToolbox-app %s: %s", "gesloten" if kind == "closed" else "kwijt", reason)
        self._emit_app(AppEvent(kind, reason=reason))

    def _on_unsolicited(self, frame: Main) -> bool:
        """Event-hook van de RpcClient (draait in de lezende thread, met de lock)."""
        content = frame.WhichOneof("content")
        if content == "app_data_exchange_request" and frame.command_id == 0:
            if self._rpc_app_state == "active":
                self._emit_app(AppEvent("data", data=bytes(frame.app_data_exchange_request.data)))
            else:
                log.debug("DataExchange zonder gekoppelde app genegeerd")
            return True
        if (
            content == "app_state_response"
            and frame.app_state_response.state == application_pb2.APP_CLOSED
            and self._rpc_app_state == "active"
            and not self._rpc_app_closing
            and not self._app_active
        ):
            self._rpc_app_lost("de app sloot zichzelf", kind="closed")
            return True
        return False

    def _loader_locked(self, client: RpcClient) -> bool:
        main = flipper_pb2.Main()
        main.app_lock_status_request.SetInParent()
        frames = client.request(main, self._timing.rpc_timeout_s)
        return bool(frames[0].app_lock_status_response.locked)

    def _wait_loader_free(self, client: RpcClient, wait_s: float | None = None) -> bool:
        wait = self._timing.loader_free_wait_s if wait_s is None else wait_s
        deadline = time.monotonic() + wait
        while self._loader_locked(client):
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._timing.loader_poll_s)
        return True

    def _start_rpc_app(self, client: RpcClient, path: str) -> None:
        """Onder de lock, loader vrij. Zet de toestand vóór de start op "active", zodat een
        HELLO die direct na APP_STARTED binnenkomt al bij de sink terechtkomt."""
        self._rpc_app_path = path
        self._rpc_app_state = "active"
        self._rpc_app_closing = False
        try:
            start = flipper_pb2.Main()
            start.app_start_request.name = path
            start.app_start_request.args = "RPC"  # rpc_app.c vervangt dit door "RPC %08lX"
            try:
                client.request(start, self._timing.rpc_timeout_s)
            except RpcError as exc:
                if exc.status_name == "ERROR_APP_SYSTEM_LOCKED":
                    raise AppLocked("de gebruiker opende net een andere app") from exc
                raise
            started = client.wait_event(
                lambda m: _is_app_state(m, application_pb2.APP_STARTED),
                self._timing.app_event_timeout_s,
            )
            if started is None:
                # Wij startten hem met "RPC", dus mogen we hem ook weer sluiten (anders blijft de
                # loader bezet door een app die niemand bedient). Geen DeviceTimeout: dat zou bij
                # BLE de hele link sluiten.
                self._rpc_app_state = "off"
                self._exit_app(client, timeout=self._timing.app_exit_timeout_s)
                raise DeviceError("PiToolbox-app meldde niet dat ze gestart is")
        except BaseException:
            self._rpc_app_state = "off"
            raise

    def _exit_rpc_app_locked(self) -> None:
        client = self._client
        if self._rpc_app_state != "active" or client is None:
            return
        self._rpc_app_closing = True
        try:
            main = flipper_pb2.Main()
            main.app_exit_request.SetInParent()
            client.request(main, self._timing.app_exit_timeout_s)
            client.wait_event(
                lambda m: _is_app_state(m, application_pb2.APP_CLOSED),
                self._timing.app_exit_timeout_s,
            )
        finally:
            self._rpc_app_closing = False

    def _suspend_rpc_app(self, client: RpcClient) -> str | None:
        """Handoff vóór ir_send: sluit de gekoppelde app en wacht tot de loader vrij is.

        Geeft het pad terug om hem daarna opnieuw te starten (of None als er geen app open
        was). De sink hoort hier niets van; mislukt de herstart, dan volgt `lost`.
        """
        path = self._rpc_app_path
        if self._rpc_app_state != "active" or path is None:
            return None
        log.info("PiToolbox-app tijdelijk sluiten voor ir_send (handoff)")
        try:
            self._exit_rpc_app_locked()
        except RpcError as exc:
            if exc.status_name != "ERROR_APP_NOT_RUNNING":
                self._rpc_app_lost("sluiten voor ir_send mislukte")
                raise DeviceError(
                    f"PiToolbox-app sluit niet; ir_send niet uitgevoerd: {exc}"
                ) from exc
        self._rpc_app_state = "suspended"
        if not self._wait_loader_free(client):
            self._rpc_app_lost("loader bleef bezet na sluiten")
            raise DeviceError("de Flipper-loader bleef bezet; ir_send niet uitgevoerd")
        return path

    def _resume_rpc_app(self, client: RpcClient, path: str) -> None:
        """Na ir_send: de app opnieuw starten. Nooit een fout; mislukt het, dan `lost`."""
        if self._rpc_app_state != "suspended" or self._client is not client:
            self._rpc_app_lost("verbinding wisselde tijdens ir_send")
            return
        try:
            if not self._wait_loader_free(client):
                self._rpc_app_lost("loader bezet na ir_send; app niet opnieuw gestart")
                return
            self._start_rpc_app(client, path)
        except DeviceError as exc:
            self._rpc_app_state = "suspended"  # zodat _rpc_app_lost hem echt meldt
            self._rpc_app_lost(f"opnieuw starten na ir_send mislukte: {exc}")

    # --- operatiekader ----------------------------------------------------------------

    def _bump_generation(self) -> None:
        with self._gen_lock:
            self._abort_gen += 1

    def _cancel(self, gen: int) -> Callable[[], bool]:
        return lambda: self._abort_gen != gen

    @contextmanager
    def _operation(
        self,
        name: str,
        *,
        abortable: bool = True,
        blocking: bool = True,
        touch_io: bool = True,
    ) -> Iterator[int]:
        """`abortable=False`: een abort() (noodstop) breekt deze operatie niet af (berichten
        van de bridge). `blocking=False`: alleen als niemand de lock heeft of erop wacht, anders
        `DeviceBusy`. `touch_io=False`: telt niet als verkeer voor de keepalive (alleen lezen)."""
        # De generatie wordt vóór het wachten op de lock vastgelegd: een abort() terwijl deze
        # call nog in de rij staat, breekt hem dus ook af.
        gen = self._abort_gen
        if blocking:
            with self._gen_lock:
                self._waiters += 1
            try:
                self._lock.acquire()
            finally:
                with self._gen_lock:
                    self._waiters -= 1
        elif self._waiters or not self._lock.acquire(blocking=False):
            raise DeviceBusy(f"Flipper is bezet; {name} overgeslagen")
        try:
            self._busy_op = name
            try:
                if abortable and self._abort_gen != gen:
                    raise Aborted("afgebroken voordat de Flipper-actie begon")
                yield gen
            finally:
                self._busy_op = None
                if touch_io:
                    self._last_io = time.monotonic()
        finally:
            self._lock.release()

    def _run_linked(
        self,
        name: str,
        op: Callable[[], T],
        *,
        blocking: bool = True,
        touch_io: bool = True,
    ) -> T:
        """Voor de RPC-gekoppelde app: niet afbreekbaar, geen (her)verbinding. Valt de link weg,
        dan is de app-sessie toch voorbij (de Flipper sluit de app)."""
        with self._operation(name, abortable=False, blocking=blocking, touch_io=touch_io):
            transport = self._transport
            if transport is None or not transport.is_open:
                self._rpc_app_lost("verbinding met de Flipper weg")
                raise DeviceNotConnected("Flipper is niet verbonden")
            try:
                return op()
            except TransportError as exc:
                self._drop_connection()
                raise DeviceError(
                    f"verbinding met de Flipper viel weg tijdens {name}: {exc}"
                ) from exc
            except (FrameError, DeviceTimeout):
                self._protocol_failure()
                raise

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
                self._protocol_failure()
                raise
            try:
                self._ensure_open()
                return op(gen, _Attempt())
            except TransportError as exc:
                self._drop_connection()
                raise DeviceError(f"Flipper-verbinding mislukt na reconnect: {exc}") from exc
            except (FrameError, DeviceTimeout):
                self._protocol_failure()
                raise

    def _protocol_failure(self) -> None:
        """Desync of time-out. USB: modus opnieuw bepalen via de CLI. Een transport zonder CLI
        (BLE) kan niet hersynchroniseren; daar sluiten we de link en krijgt de volgende actie
        een verse RPC-sessie."""
        transport = self._transport
        if transport is not None and transport.starts_in_rpc:
            self._drop_connection()
        else:
            self._forget_mode()

    def _forget_mode(self, *, fresh_shell: bool = False) -> None:
        """Modus kwijt. `fresh_shell`: poort is net (her)opend, dus zeker geen RPC-sessie."""
        self._maybe_rpc = False if fresh_shell else (self._maybe_rpc or self._mode == "rpc")
        self._mode = "unknown"
        self._client = None
        self._app_active = False
        # Zonder RPC-sessie is de gekoppelde app ook weg (de firmware stuurt hem SessionClose).
        self._rpc_app_lost("RPC-sessie met de Flipper weg")

    def _tx(self) -> ByteTransport:
        if self._transport is None:
            raise DeviceNotConnected("Flipper-transport is niet geopend")
        return self._transport

    def _ensure_open(self) -> None:
        transport = self._transport
        if transport is not None and transport.is_open:
            return
        if transport is not None:
            # Weggevallen verbinding: eerst netjes opruimen (BLE: loop-thread en link).
            try:
                transport.close()
            except Exception as exc:  # noqa: BLE001 - opruimen mag een reconnect niet blokkeren
                log.debug("fout bij opruimen oude transport: %s", exc)
        # Bij elke (her)verbinding opnieuw kiezen: 'auto' pakt zo een net aangesloten kabel op.
        transport = self._factory()
        self._transport = transport
        transport.open()
        # Na (her)openen begint DTR een verse CLI-shell (cli_vcp.c): toestand opnieuw bepalen.
        self._forget_mode(fresh_shell=True)
        self._device_info = None
        if self._profile_setting.strip().lower() in ("", "auto"):
            self._profile = None
        if transport.starts_in_rpc:
            self._enter_native_rpc()
        self._last_io = time.monotonic()
        self._start_keepalive()

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

    # --- keepalive --------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        if self._keepalive_s <= 0:
            return
        thread = self._ka_thread
        if thread is not None and thread.is_alive():
            return
        stop = threading.Event()
        self._ka_stop = stop
        thread = threading.Thread(
            target=_keepalive_main,
            args=(weakref.ref(self), stop, self._keepalive_s),
            name="flipper-keepalive",
            daemon=True,
        )
        self._ka_thread = thread
        thread.start()

    def _stop_keepalive(self) -> None:
        self._ka_stop.set()
        thread, self._ka_thread = self._ka_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _keepalive_tick(self) -> None:
        """Eén RPC-ping als de link idle is. Slaat over als het device bezet is."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            transport, client = self._transport, self._client
            # Alleen voor transports die altijd RPC zijn (BLE): USB heeft geen keepalive nodig
            # en blijft zo exact zoals in fase 1.
            if (
                transport is None
                or not transport.is_open
                or not getattr(transport, "starts_in_rpc", False)
                or self._mode != "rpc"
                or client is None
                or self._app_active
                or time.monotonic() - self._last_io < self._keepalive_s
            ):
                return
            ping = flipper_pb2.Main()
            ping.system_ping_request.data = b"keepalive"
            try:
                client.request(ping, self._timing.probe_timeout_s)
            except DeviceError as exc:
                log.warning("keepalive-ping mislukte (%s); verbinding wordt gesloten", exc)
                self._drop_connection()  # de volgende actie verbindt opnieuw
                return
            self._last_io = time.monotonic()
        finally:
            self._lock.release()

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

    def _require_cli_transport(self) -> None:
        """CLI-acties: op een transport zonder CLI (BLE) eerst naar USB wisselen of weigeren."""
        if self._rpc_app_state != "off":
            raise AppLocked(_CLI_BLOCKED_BY_APP)
        if not self._tx().starts_in_rpc:
            return
        if self._usb_probe is not None and self._usb_probe():
            log.info("CLI-actie gevraagd over BLE; overschakelen naar USB")
            self._drop_connection()
            self._ensure_open()  # de factory kiest nu USB
            if not self._tx().starts_in_rpc:
                return
        raise UnsupportedTransport(_CLI_NEEDS_USB)

    def _new_client(self, transport: ByteTransport) -> RpcClient:
        return RpcClient(
            transport, read_slice_s=self._timing.read_slice_s, event_hook=self._on_unsolicited
        )

    def _enter_native_rpc(self) -> RpcClient:
        """Transport zonder CLI: de RPC-sessie loopt al, er is alleen een client nodig."""
        transport = self._tx()
        client = self._new_client(transport)
        self._client = client
        self._mode = "rpc"
        self._maybe_rpc = False
        self._app_active = False
        return client

    def _ensure_cli(self, gen: int | None) -> None:
        if self._tx().starts_in_rpc:
            raise UnsupportedTransport(_CLI_NEEDS_USB)
        if self._rpc_app_state != "off":
            raise AppLocked(_CLI_BLOCKED_BY_APP)
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
        if self._tx().starts_in_rpc:
            return self._enter_native_rpc()
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
            client = self._new_client(transport)
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


def _keepalive_main(
    ref: weakref.ReferenceType[FlipperDevice], stop: threading.Event, interval: float
) -> None:
    """Threadlus; houdt alleen een zwakke verwijzing zodat een vergeten device opgeruimd wordt."""
    tick = max(0.02, min(interval / 4, 1.0))
    while not stop.wait(tick):
        device = ref()
        if device is None:
            return
        try:
            device._keepalive_tick()
        except Exception:
            log.exception("keepalive-tick faalde")
        del device


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
