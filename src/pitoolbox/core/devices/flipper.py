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
Een BLE-write die op flow control wacht, wordt alleen voor een afbreekbare operatie gewekt; wat de
bridge met de app uitwisselt (`app_send`, `poll_app_events`) maakt een noodstop gewoon af.

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

**Op afstand** (`gui_press`, `gui_screen`, alleen voor de ontwikkelaarsmodus
`pitoolbox-flipper remote` en `ui-test --confirm-check --auto`, nooit een tool): knoppen
indrukken via de officiële GUI-RPC, alleen als onze gekoppelde app of geen app open is, en één
schermbeeld lezen. Regels en bronnen: `_flipper_remote`.

**Opbouw.** `FlipperDevice` erft van lagen in eigen modules die dezelfde toestand en dezelfde
lock delen: `_flipper_link_core` (toestand, haken voor de gekoppelde app), `_flipper_link`
(operatiekader, keepalive), `_flipper_modes` (CLI/RPC-wissel), `_flipper_cli_actions`
(Sub-GHz/NFC/RFID), `_flipper_app_channel` (gekoppelde app), `_flipper_install` (FAP
installeren), `_flipper_ir` (ir_send met handoff) en `_flipper_remote` (op afstand). Alles wat
andere code nodig heeft, importeer je uit deze module.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from pitoolbox.core.devices._flipper_cli_actions import FlipperCliActions
from pitoolbox.core.devices._flipper_install import (
    STORAGE_CHUNK,
    AppInstall,
    FlipperInstaller,
    ProgressFn,
)
from pitoolbox.core.devices._flipper_ir import FlipperIr
from pitoolbox.core.devices._flipper_remote import REMOTE_KEYS, FlipperRemote
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2, storage_pb2
from pitoolbox.core.devices._flipper_screen import ScreenFrame
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
    RemoteInputRefused,
    _Attempt,
)
from pitoolbox.core.errors import DeviceError

if TYPE_CHECKING:
    from pitoolbox.config import Settings

__all__ = [
    "FLIPPER_PID",
    "FLIPPER_VID",
    "REMOTE_KEYS",
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
    "RemoteInputRefused",
    "ScreenFrame",
    "TransportError",
    "UsbSerialTransport",
    "make_transport",
]


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


class FlipperDevice(FlipperCliActions, FlipperIr, FlipperInstaller, FlipperRemote):
    """Thread-safe Flipper-toegang met automatische CLI/RPC-modewissel."""

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
        self._interrupt_abortable_write()
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
