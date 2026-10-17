"""Basis van `FlipperDevice`: toestand, operatiekader (lock, abort, reconnect) en keepalive.

Eén lock (`_lock`) rond alle device-I/O; `_gen_lock` beschermt de abort-generatie en de teller
van wachtende threads. Alle toestand wordt hier aangemaakt; de andere `_flipper_*`-lagen bouwen
hierop voort. Overzicht en bronnen: zie flipper.py.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, TypeVar

from pitoolbox.core.devices._flipper_cli import CliProfile
from pitoolbox.core.devices._flipper_rpc.client import Main, RpcClient
from pitoolbox.core.devices._flipper_rpc.framing import FrameError
from pitoolbox.core.devices._flipper_rpc.pb import application_pb2, flipper_pb2
from pitoolbox.core.devices._flipper_transport import ByteTransport, TransportError
from pitoolbox.core.devices._flipper_types import (
    AppEvent,
    AppEventSink,
    DeviceBusy,
    FlipperTiming,
    _Attempt,
)
from pitoolbox.core.errors import Aborted, DeviceError, DeviceNotConnected, DeviceTimeout

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

T = TypeVar("T")


class FlipperLink:
    """Toestand en operatiekader die alle Flipper-acties delen (zie `FlipperDevice`)."""

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
        self._op_abortable = False  # de lopende operatie is afbreekbaar (onder `_gen_lock`)
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

    # --- operatiekader ----------------------------------------------------------------

    def _bump_generation(self) -> None:
        with self._gen_lock:
            self._abort_gen += 1

    def _interrupt_abortable_write(self) -> None:
        """Maak een schrijfactie wakker die op BLE-flow-control wacht (die kent de
        generatieteller niet), maar alleen als de lopende operatie afbreekbaar is.

        Een niet-afbreekbare operatie (een bericht van de bridge) midden in een bericht
        onderbreken zou over BLE de hele link sluiten: de app is dan weg, de lease los, en de
        uitkomst van de noodstop komt niet meer op de Flipper. Onder `_gen_lock`, zodat er
        tussen de controle en de interrupt geen andere operatie kan beginnen.
        """
        with self._gen_lock:
            if not self._op_abortable:
                return
            interrupt = getattr(self._transport, "interrupt", None)
            if callable(interrupt):
                interrupt()

    def _cancel(self, gen: int) -> Callable[[], bool]:
        return lambda: self._abort_gen != gen

    def _cancel_or_none(self, gen: int | None) -> Callable[[], bool] | None:
        return None if gen is None else self._cancel(gen)

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
            with self._gen_lock:
                self._op_abortable = abortable
            try:
                if abortable and self._abort_gen != gen:
                    raise Aborted("afgebroken voordat de Flipper-actie begon")
                yield gen
            finally:
                with self._gen_lock:
                    self._op_abortable = False
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

    def _run_maybe_idle(self, name: str, op: Callable[[int, _Attempt], T], idle_only: bool) -> T:
        if not idle_only:
            return self._run(name, op)
        # Eerst kijken zonder te wachten; daarna gewoon via _run (de lock is dan net vrij).
        if self._waiters or self._busy_op is not None:
            raise DeviceBusy(f"Flipper is bezet; {name} overgeslagen")
        return self._run(name, op)

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
            self._ping_if_idle()
        finally:
            self._lock.release()

    def _ping_if_idle(self) -> None:
        """Onder de lock: één keepalive-ping als de link `keepalive_s` idle is. Mislukt hij,
        dan wordt de verbinding gesloten (de volgende actie verbindt opnieuw)."""
        transport, client = self._transport, self._client
        # Alleen voor transports die altijd RPC zijn (BLE): USB heeft geen keepalive nodig
        # en blijft zo exact zoals in fase 1.
        if (
            self._keepalive_s <= 0
            or transport is None
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
            self._drop_connection()
            return
        self._last_io = time.monotonic()


def _keepalive_main(
    ref: weakref.ReferenceType[FlipperLink], stop: threading.Event, interval: float
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
