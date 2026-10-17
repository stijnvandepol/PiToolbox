"""Kanaal naar de RPC-gekoppelde PiToolbox-app (fase 4): starten, DataExchange, events, handoff.

Gedrag en regels: zie "RPC-gekoppelde app" in flipper.py. De Flipper-bridge is de gebruiker.
"""

from __future__ import annotations

import calendar
import logging
import re
import time

from pitoolbox.core.devices._flipper_link import _is_app_state
from pitoolbox.core.devices._flipper_modes import FlipperModes
from pitoolbox.core.devices._flipper_rpc.client import RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2, storage_pb2
from pitoolbox.core.devices._flipper_types import (
    AppEventSink,
    AppLocked,
    AppNotRunning,
    AppRejected,
    DeviceBusy,
    _Attempt,
)
from pitoolbox.core.errors import DeviceError, DeviceTimeout

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

# Alleen een FAP op de SD-kaart onder /ext/apps, zonder "..": nooit een willekeurige app.
_APP_PATH_RE = re.compile(r"^/ext/apps/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+\.fap$")


class FlipperAppChannel(FlipperModes):
    """De gekoppelde app: publieke acties voor de bridge en de interne starten/sluiten-logica."""

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
            self._run_linked("exit_rpc_app", self._exit_rpc_app_quietly)
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
            AppNotRunning: de app draait niet (meer), of bevestigde niet binnen `timeout_s`
                (dan hangt ze of is ze weg; het device heeft haar gesloten).
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
            except DeviceTimeout as exc:
                self._close_unresponsive_app()
                raise AppNotRunning(
                    f"de PiToolbox-app bevestigde een bericht niet binnen {timeout_s:g} s; "
                    "de Pi heeft haar gesloten"
                ) from exc
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

        Leest de bridge vrijwel continu, dan krijgt de keepalive-thread de lock nooit (die
        wacht niet). Daarom pingt deze lezer zelf als de link `keepalive_s` idle is. Wat de
        app ongevraagd stuurt, telt niet als verkeer: de keepalive bewijst dat de RPC-sessie
        in beide richtingen werkt (onze write met flow control én een antwoord), en ruimt zo
        een link op die nog open lijkt maar dood is. Een ongevraagd frame bewijst alleen de
        richting Flipper -> Pi. (De BLE-supervision zelf heeft geen verkeer nodig; de
        controllers houden de link met lege pakketten in stand.)
        """

        def op() -> bool:
            self._ping_if_idle()
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

    # --- intern: starten, sluiten en handoff -----------------------------------------

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

    def _close_unresponsive_app(self) -> None:
        """Onder de lock: onze app bevestigde een DataExchange niet op tijd, dus ze hangt of is
        weg. Zelf sluiten (wij startten haar met "RPC"; nooit een app van de gebruiker) en pas
        daarna `lost` melden, zodat een nieuwe start niet op onze eigen app stuit.

        De link blijft open: een app die niet antwoordt is geen kapotte verbinding (over BLE
        zou sluiten de hele sessie kosten). Is de link toch dood, dan merkt de keepalive dat.
        Een late bevestiging van het verlopen verzoek heeft een eigen command_id; de RpcClient
        zet die in zijn eventqueue, waar geen later verzoek of `wait_event` hem voor zich houdt.
        """
        self._exit_rpc_app_quietly()
        self._rpc_app_lost("de app bevestigde een bericht niet op tijd")

    def _exit_rpc_app_quietly(self) -> None:
        """Onder de lock: onze app sluiten. Antwoordt ze niet (ze hangt), dan is dat geen kapotte
        verbinding: de time-out mag niet bij `_run_linked` komen, want die sluit over BLE de hele
        link en dan wordt het `lost` (opnieuw starten) in plaats van `closed`."""
        try:
            self._exit_rpc_app_locked()
        except (RpcError, DeviceTimeout) as exc:
            log.warning("PiToolbox-app sluiten mislukte (de app antwoordt niet): %s", exc)

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
