"""Bridge tussen de Flipper-app (`flipper_app/pitoolbox_ui`) en PiToolbox (fase 4).

Spec: docs/fase4-protocol.md (§4 volgorde, §5 handdruk, §6 bevestigen, §7 acties, §8 starten,
§10 deze bridge). De FAP is een onbetrouwbaar invoerapparaat: hij stuurt alleen ids. Alles wat
hij vraagt, gaat via de `ToolRunner` met frontend-id `FRONTEND` (validatie, policy, scope,
audit), of via een eigen `AgentSession` (presets).

**Eén eigenaar.** De bridge draait in het proces dat de `FlipperDevice` bezit (pitoolbox-mcp
of pitoolbox-agent, met `PITOOLBOX_FLIPPER_UI=on`). Twee processen mogen de Flipper nooit
tegelijk bezitten: over BLE verbreekt elk proces bij het verbinden een "achtergebleven" link,
dus ze zouden elkaar steeds wegdrukken (en over USB delen ze één seriële poort).

**Threads.** Alle toestand van de bridge hoort bij de hoofdlus (`flipper-ui-bridge`). Andere
threads posten alleen een callable naar die lus:

- `flipper-ui-reader`: `device.poll_app_events()` zolang de app open is. Leest alleen als
  niemand anders op de device-lock wacht; tijdens een tool-call leest die call zelf, en de
  event-hook van het device geeft FAP-frames meteen door (`AppEvent` → hoofdlus). Zo gaat er
  niets via de begrensde eventqueue van de `RpcClient`.
- `flipper-ui-sender`: Pi → FAP, stop-and-wait (`device.app_send` wacht op de bevestiging),
  hoogstens één bericht per `send_gap_s` (5/s), 3x opnieuw na 200 ms bij `AppRejected`.
  PROGRESS hoogstens 1/s; een nieuwere vervangt een oudere die nog niet weg is.
- `flipper-ui-action` (snelle actie of preset, één tegelijk), `flipper-ui-confirm`
  (bevestigingen), `flipper-ui-device` (app starten, verzoekbestand pollen) en `flipper-ui-aux`
  (STATUS samenstellen). De noodstop draait altijd meteen in een eigen thread.

De device-lock serialiseert alles wat de Flipper raakt; de Flipper doet toch maar één
RPC-opdracht tegelijk. De sink van het device en de luisteraar van de `PendingStore` worden
met hún lock vast aangeroepen: ze zetten alleen iets in de begrensde inbox van de hoofdlus.

**Flipper verplicht.** Na een geslaagde handdruk (WELCOME afgeleverd) koppelt de bridge de
fysieke bevestiger (`runner.attach_physical_confirmer`). Vanaf dan weigert `confirm_action` uit
elke frontend, en bevestigt alleen OK op de Flipper (`runner.confirm_physical` met de lease).
Elke nieuwe pending (van elke frontend) verschijnt op de Flipper; is de app dicht maar de
loader vrij, dan start de bridge hem ("Pi kan starten"). Bij app dicht, link weg, een hangende
app of afsluiten koppelt de bridge eerst los (daarna geldt de oude regel weer), en pas dan
gaat de rest. Een OK die nog onderweg was als de link wegviel, vindt een losgekoppelde lease
en voert niets uit (fail closed); de pending houdt zijn TTL en kan daarna in de chat.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any

from pitoolbox.agent.session import AgentEvent, AgentLimits, AgentSession, ToolCallStarted, TurnStop
from pitoolbox.core.devices.flipper import (
    AppEvent,
    AppLocked,
    AppNotRunning,
    AppRejected,
    DeviceBusy,
    FlipperDevice,
)
from pitoolbox.core.errors import DeviceError, ToolboxError
from pitoolbox.core.policy import PendingChange, PendingEvent, PhysicalConfirmer
from pitoolbox.frontends.flipper_ui.presets import PresetConfig
from pitoolbox.frontends.flipper_ui.protocol import (
    MAX_ERROR_TEXT,
    MAX_PROGRESS,
    MAX_SUMMARY,
    MAX_TOOL_NAME,
    MAX_TTL_S,
    MAX_UPLINK,
    PROTO_VERSION,
    Action,
    ActionKind,
    Cancel,
    CloseReason,
    ConfirmAnswer,
    ConfirmClosed,
    ConfirmHandles,
    ConfirmPrompt,
    Error,
    ErrorCode,
    Hello,
    LastResult,
    Link,
    Message,
    Preset,
    Presets,
    PresetsRequest,
    Progress,
    ProtocolError,
    QuickAction,
    QuickActionPlan,
    RateLimiter,
    ResultChunk,
    SeqCounter,
    SeqTracker,
    Status,
    StatusRequest,
    Welcome,
    chunk_result,
    decode_from_fap,
    encode,
    quick_action_plan,
    to_display_text,
)

if TYPE_CHECKING:
    from pitoolbox.config import Settings
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider

log = logging.getLogger(__name__)

FRONTEND = "flipper-ui"
START_REQUEST_PATH = "/ext/apps_data/pitoolbox_ui/start_request"
START_REQUEST_MAX_BYTES = 64
START_REQUEST_MAX_AGE_S = 30
_START_REQUEST_RE = re.compile(rb"^PTBUI 1 ([0-9]{1,10})\n?$")
MAX_QUEUED_PROMPTS = 8  # protocol §6: hoogstens 8 open bevestigingen
_MAX_INBOX = 256  # externe gebeurtenissen (FAP-frames, pendings) in de hoofdlus
_MAX_OUTBOX = 64


@dataclass(frozen=True)
class BridgeTiming:
    """Wachttijden in seconden; tests verkleinen ze."""

    tick_s: float = 0.1  # hoofdlus: timers controleren
    poll_slice_s: float = 0.1  # lezer: hoogstens zo lang de lock per keer
    poll_idle_s: float = 0.05  # lezer: pauze als er niets te lezen viel
    send_gap_s: float = 0.2  # Pi -> FAP hoogstens 5 berichten/s
    send_timeout_s: float = 3.0  # geen bevestiging: de app hangt (protocol §4)
    send_retry_s: float = 0.2  # na ERROR_APP_CMD_ERROR
    send_retries: int = 3
    progress_gap_s: float = 1.0
    hello_timeout_s: float = 10.0  # na APP_STARTED: de FAP probeert HELLO 3x per 3 s
    expire_s: float = 1.0  # handles controleren op TTL
    start_poll_s: float = 3.0  # verzoekbestand (§8)
    manual_lock_wait_s: float = 3.0  # de handmatige instance sluit nog
    restart_backoff_s: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0, 60.0)
    restart_window_s: float = 300.0  # na een verloren link zo lang opnieuw proberen
    link_backoff_s: tuple[float, ...] = (3.0, 6.0, 12.0, 30.0, 60.0)  # pollen zonder link
    stop_join_s: float = 2.0


class BridgeState(StrEnum):
    IDLE = "idle"  # geen app (of nog niet gestart)
    STARTING = "starting"  # StartRequest loopt
    HANDSHAKE = "handshake"  # app draait, wacht op HELLO of tot WELCOME afgeleverd is
    CONNECTED = "connected"  # handdruk rond: bevestigen kan alleen nog op de Flipper
    STOPPED = "stopped"


class _Delivery(Enum):
    SENT = 1
    DROPPED = 2  # na de retries nog geweigerd; link is in orde
    DEAD = 3  # app of link weg


@dataclass
class _Out:
    build: Callable[[int], Message]  # krijgt de seq pas bij versturen (volgorde = seq-volgorde)
    instance: int | None  # None: altijd sturen (fouten vóór HELLO)
    on_sent: Callable[[], None] | None = None  # draait in de hoofdlus
    progress_ref: int | None = None


@dataclass
class _Job:
    ref: int
    instance: int
    cancelled: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Fap:
    """Toestand van één FAP-instance (van HELLO tot een nieuwe HELLO of loskoppelen)."""

    instance: int
    max_rx: int
    handles: ConfirmHandles[str]
    rx_seq: SeqTracker = field(default_factory=SeqTracker)
    shown: tuple[int, str] | None = None  # (handle, pending_id) dat nu op het scherm staat
    queue: deque[str] = field(default_factory=deque)  # pending_ids die nog moeten


class _ActionFailed(Exception):
    def __init__(self, code: ErrorCode, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


class _Lane:
    """Eén werkthread met een begrensde FIFO."""

    def __init__(self, name: str, maxsize: int = 16) -> None:
        self.name = name
        self._q: queue.Queue[Callable[[], None]] = queue.Queue(maxsize)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, fn: Callable[[], None]) -> bool:
        try:
            self._q.put_nowait(fn)
            return True
        except queue.Full:
            log.warning("%s: wachtrij vol, taak overgeslagen", self.name)
            return False

    def stop(self, timeout: float) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _main(self) -> None:
        while not self._stop.is_set():
            try:
                fn = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception:
                log.exception("%s: taak faalde", self.name)


class FlipperUiBridge:
    """Pi-kant van docs/fase4-protocol.md. `start()` en `stop()` vanuit de eigenaar."""

    def __init__(
        self,
        runner: ToolRunner,
        *,
        app_path: str,
        presets: Sequence[PresetConfig] = (),
        provider: LLMProvider | None = None,
        limits: AgentLimits | None = None,
        timing: BridgeTiming | None = None,
        clock: Callable[[], float] = time.monotonic,
        owns_provider: bool = False,
    ) -> None:
        self.runner = runner
        self._owns_provider = owns_provider
        self.device: FlipperDevice = runner.ctx.flipper
        self.store = runner.ctx.pending
        self.app_path = app_path
        self.presets: Mapping[int, PresetConfig] = {p.id: p for p in presets}
        self.provider = provider
        self.limits = limits
        self.timing = timing or BridgeTiming()
        self._clock = clock

        # --- hoofdlus (alleen daar gewijzigd) ---
        self._state = BridgeState.IDLE
        self._fap: _Fap | None = None
        self._lease: PhysicalConfirmer | None = None
        self._action: _Job | None = None
        self._answering: set[str] = set()  # pending_ids waarvoor een OK/Back loopt
        self._hello_deadline: float | None = None
        self._restart_at: float | None = None
        self._restart_until = 0.0
        self._restart_step = 0
        self._next_poll = 0.0
        self._link_step = 0
        self._next_expire = 0.0
        self._rx_rate = RateLimiter(clock=clock)
        self._session: AgentSession | None = None  # alleen in de action-lane gebruikt

        # --- inbox van de hoofdlus ---
        self._cond = threading.Condition()
        self._inbox: deque[tuple[bool, Callable[[], None]]] = deque()
        self._external = 0
        self.dropped_inbox = 0
        self._stopping = False

        # --- zendrij (eigen lock) ---
        self._out_cond = threading.Condition()
        self._outbox: deque[_Out] = deque()
        self._progress: _Out | None = None
        self._send_instance: int | None = None
        self._last_send = 0.0
        self._last_progress = 0.0
        self._tx_seq = SeqCounter()
        self.dropped_out = 0

        # --- laatste toolcall (voor STATUS; ook uit lanes gezet) ---
        self._last_lock = threading.Lock()
        self._last_tool = ""
        self._last_ok: bool | None = None
        self._last_at: float | None = None

        self._estop_lock = threading.Lock()
        self._unsubscribe: Callable[[], None] | None = None
        self._threads: list[threading.Thread] = []
        self._lanes = {
            name: _Lane(f"flipper-ui-{name}") for name in ("action", "confirm", "device", "aux")
        }

    # --- levenscyclus -----------------------------------------------------------------

    @property
    def state(self) -> BridgeState:
        return self._state

    @property
    def instance(self) -> int | None:
        fap = self._fap
        return fap.instance if fap is not None else None

    @property
    def agent_available(self) -> bool:
        return self.provider is not None

    def request_start(self) -> None:
        """Start de app nu (als hij niet al draait en de loader vrij is). Thread-safe."""
        self._post(lambda: self._request_start("op verzoek"))

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("bridge draait al")
        self.device.set_app_sink(self._on_app_event)
        self._unsubscribe = self.store.subscribe(self._on_pending_event)
        for lane in self._lanes.values():
            lane.start()
        for name, target in (
            ("flipper-ui-bridge", self._main),
            ("flipper-ui-reader", self._reader_main),
            ("flipper-ui-sender", self._sender_main),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()
        log.info("Flipper-bridge gestart (app %s, %d presets)", self.app_path, len(self.presets))

    def stop(self) -> None:
        """Eerst loskoppelen (oude bevestigingsregel terug), dan de app sluiten, dan threads."""
        if not self._threads or self._state is BridgeState.STOPPED:
            return
        done = threading.Event()

        def shutdown() -> None:
            self._detach("bridge gestopt", restart=False)
            self._state = BridgeState.STOPPED
            done.set()

        self._post(shutdown)
        if not done.wait(self.timing.stop_join_s):
            log.warning("Flipper-bridge: hoofdlus reageerde niet; lease alsnog loskoppelen")
            lease = self._lease
            if lease is not None:
                self.runner.detach_physical_confirmer(lease, "bridge gestopt")
        if self._unsubscribe is not None:
            self._unsubscribe()
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        with self._out_cond:
            self._outbox.clear()
            self._out_cond.notify_all()
        self.device.set_app_sink(None)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(self.timing.stop_join_s)
        for lane in self._lanes.values():
            lane.stop(self.timing.stop_join_s)
        if self.device.rpc_app_state != "off":
            self.device.exit_rpc_app()
        if self._owns_provider and self.provider is not None:
            self.provider.close()
        log.info("Flipper-bridge gestopt")

    # --- inbox ------------------------------------------------------------------------

    def _post(self, fn: Callable[[], None], *, external: bool = False) -> None:
        """Zet werk voor de hoofdlus klaar. Extern (FAP, pendings) is begrensd."""
        with self._cond:
            if external:
                if self._external >= _MAX_INBOX:
                    self.dropped_inbox += 1
                    if self.dropped_inbox % 100 == 1:
                        log.warning("Flipper-bridge: inbox vol, gebeurtenis weggegooid")
                    return
                self._external += 1
            self._inbox.append((external, fn))
            self._cond.notify_all()

    def _on_app_event(self, event: AppEvent) -> None:
        # Device-thread, met de device-lock: alleen doorzetten.
        self._post(lambda: self._handle_app_event(event), external=True)

    def _on_pending_event(self, event: PendingEvent) -> None:
        # Met de lock van de PendingStore: alleen doorzetten.
        self._post(lambda: self._handle_pending_event(event), external=True)

    def _main(self) -> None:
        while True:
            with self._cond:
                if not self._inbox and not self._stopping:
                    self._cond.wait(self.timing.tick_s)
                items = list(self._inbox)
                self._inbox.clear()
                self._external = 0
                stopping = self._stopping
            for _external, fn in items:
                try:
                    fn()
                except Exception:
                    log.exception("Flipper-bridge: fout in de hoofdlus")
            if stopping:
                return
            try:
                self._tick()
            except Exception:
                log.exception("Flipper-bridge: fout in de timers")

    # --- timers -----------------------------------------------------------------------

    def _tick(self) -> None:
        if self._state is BridgeState.STOPPED:
            return
        now = self._clock()
        if now >= self._next_expire:
            self._next_expire = now + self.timing.expire_s
            self._expire_handles()
        if (
            self._state in (BridgeState.STARTING, BridgeState.HANDSHAKE)
            and self._hello_deadline is not None
            and now >= self._hello_deadline
        ):
            log.warning("PiToolbox-app groette niet op tijd; app wordt gesloten")
            self._detach("geen HELLO van de app", restart=False)
            self._lanes["device"].submit(self.device.exit_rpc_app)
            return
        if self._state is not BridgeState.IDLE:
            return
        if self._restart_at is not None and now >= self._restart_at:
            self._restart_at = None
            self._request_start("herstart na verloren verbinding", retry=True)
        elif now >= self._next_poll:
            self._next_poll = now + self.timing.start_poll_s
            self._lanes["device"].submit(self._poll_start_request)

    def _expire_handles(self) -> None:
        fap = self._fap
        if fap is None:
            return
        for handle, _pid in fap.handles.expire():
            log.info("bevestiging op de Flipper verlopen (handle %08x)", handle)
            self._send(partial(_closed, handle=handle, reason=CloseReason.EXPIRED))
            if fap.shown is not None and fap.shown[0] == handle:
                fap.shown = None
        self._show_next()

    # --- starten (§8) -----------------------------------------------------------------

    def _request_start(
        self, why: str, *, retry: bool = False, lock_wait_s: float | None = None
    ) -> None:
        if self._state is not BridgeState.IDLE:
            return
        self._state = BridgeState.STARTING
        self._hello_deadline = None
        log.info("PiToolbox-app starten (%s)", why)

        def job() -> None:
            try:
                self.device.start_rpc_app(self.app_path, lock_wait_s=lock_wait_s)
            except AppLocked as exc:
                log.info("app niet gestart: %s", exc)
                self._post(lambda: self._on_start_failed(retry=False))
            except (DeviceError, ValueError) as exc:
                log.warning("app starten mislukte: %s", exc)
                self._post(lambda: self._on_start_failed(retry=retry))
            else:
                self._post(self._on_started)

        if not self._lanes["device"].submit(job):
            self._state = BridgeState.IDLE

    def _on_started(self) -> None:
        if self._state is BridgeState.STARTING:
            self._state = BridgeState.HANDSHAKE
        if self._state is BridgeState.HANDSHAKE:
            self._hello_deadline = self._clock() + self.timing.hello_timeout_s

    def _on_start_failed(self, *, retry: bool) -> None:
        if self._state is BridgeState.STARTING:
            self._state = BridgeState.IDLE
        if retry:
            self._schedule_restart()
        else:
            self._restart_at = None

    def _schedule_restart(self) -> None:
        now = self._clock()
        if self._restart_at is None and self._restart_step == 0:
            self._restart_until = now + self.timing.restart_window_s
        if now >= self._restart_until:
            log.info("PiToolbox-app: opnieuw starten opgegeven")
            self._restart_at = None
            self._restart_step = 0
            return
        backoff = self.timing.restart_backoff_s
        delay = backoff[min(self._restart_step, len(backoff) - 1)]
        self._restart_step += 1
        self._restart_at = now + delay

    def _poll_start_request(self) -> None:
        """Device-lane: verzoekbestand van een handmatig gestarte FAP (§8). Alleen als het
        device vrij is en er geen andere app open is; een oud verzoek (> 30 s) telt niet."""
        if self.device.rpc_app_state != "off":
            return
        try:
            if self.device.app_lock_status(idle_only=True):
                return
            raw = self.device.take_small_file(
                START_REQUEST_PATH, START_REQUEST_MAX_BYTES, idle_only=True
            )
            if raw is None:
                self._post(self._link_ok)
                return
            match = _START_REQUEST_RE.fullmatch(raw)
            if match is None:
                log.warning("verzoekbestand van de app ongeldig; genegeerd")
                return
            age = self.device.rtc_timestamp() - int(match.group(1))
        except DeviceBusy:
            return
        except (DeviceError, ToolboxError) as exc:
            log.debug("verzoekbestand pollen: %s", exc)
            self._post(self._link_failed)
            return
        self._post(self._link_ok)
        if not 0 <= age <= START_REQUEST_MAX_AGE_S:
            log.info("verzoek om de app te openen is %d s oud; genegeerd", age)
            return
        self._post(
            lambda: self._request_start(
                "handmatig geopend", lock_wait_s=self.timing.manual_lock_wait_s
            )
        )

    def _link_ok(self) -> None:
        self._link_step = 0

    def _link_failed(self) -> None:
        backoff = self.timing.link_backoff_s
        delay = backoff[min(self._link_step, len(backoff) - 1)]
        self._link_step += 1
        self._next_poll = self._clock() + delay

    # --- app-gebeurtenissen ---------------------------------------------------------------

    def _handle_app_event(self, event: AppEvent) -> None:
        if self._state is BridgeState.STOPPED:
            return
        if event.kind == "data":
            self._on_data(event.data)
        elif event.kind == "closed":
            self._detach(f"app gesloten: {event.reason}", restart=False)
        else:
            self._detach(f"verbinding weg: {event.reason}", restart=True)

    def _detach(self, reason: str, *, restart: bool) -> None:
        """Alles van de FAP-sessie weg. Eerst de lease: daarna geldt weer de oude regel."""
        lease, self._lease = self._lease, None
        if lease is not None:
            self.runner.detach_physical_confirmer(lease, reason)
        fap, self._fap = self._fap, None
        if fap is not None:
            fap.handles.revoke_all()
            log.info("Flipper-app losgekoppeld: %s", reason)
        job, self._action = self._action, None
        if job is not None:
            job.cancelled.set()
        with self._out_cond:
            self._send_instance = None
            self._outbox.clear()
            self._progress = None
        if self._state is not BridgeState.STOPPED:
            self._state = BridgeState.IDLE
        self._hello_deadline = None
        if restart:
            self._schedule_restart()

    def _on_data(self, data: bytes) -> None:
        try:
            msg = decode_from_fap(data)
        except ProtocolError as exc:
            if self._rx_rate.allow():
                log.info("ongeldig bericht van de app: %s", exc)
                self._send_error(exc.seq or 0, exc.code, str(exc), instance=None)
            return
        estop = (
            isinstance(msg, Action)
            and msg.kind is ActionKind.QUICK
            and msg.action_id == QuickAction.EMERGENCY_STOP
        )
        # De noodstop gaat altijd door, ook boven de rate limit.
        if not estop and not self._rx_rate.allow():
            log.info("bericht van de app gedropt (rate limit, totaal %d)", self._rx_rate.dropped)
            if isinstance(msg, Action):
                self._send_error(msg.seq, ErrorCode.RATE_LIMITED, "te snel", instance=None)
            return
        if isinstance(msg, Hello):
            self._on_hello(msg)
            return
        fap = self._fap
        if fap is None or self._state is not BridgeState.CONNECTED:
            self._send_error(msg.seq, ErrorCode.NOT_READY, "eerst HELLO", instance=None)
            return
        gap = fap.rx_seq.accept(msg.seq)
        if gap is None:
            log.debug("duplicaat of oud bericht (seq %d) genegeerd", msg.seq)
            return
        if gap:
            log.info("%d bericht(en) van de app gemist", gap)
        if isinstance(msg, StatusRequest):
            self._send_status()
        elif isinstance(msg, PresetsRequest):
            self._send_presets(fap)
        elif isinstance(msg, Action):
            self._on_action(fap, msg)
        elif isinstance(msg, ConfirmAnswer):
            self._on_confirm_answer(fap, msg)
        elif isinstance(msg, Cancel):
            self._on_cancel(fap, msg)

    # --- handdruk (§5) ----------------------------------------------------------------------

    def _on_hello(self, msg: Hello) -> None:
        fap = self._fap
        if fap is not None and fap.instance == msg.instance:
            self._send_welcome(fap)  # herhaalde HELLO: nog een WELCOME, geen reset
            return
        if fap is not None:
            fap.handles.revoke_all()  # de nieuwe instance kent die handles niet
        fap = _Fap(
            instance=msg.instance,
            max_rx=msg.max_rx,
            handles=ConfirmHandles(clock=self._clock, max_open=MAX_QUEUED_PROMPTS),
        )
        self._fap = fap
        job, self._action = self._action, None
        if job is not None:
            job.cancelled.set()
        with self._out_cond:
            self._send_instance = msg.instance
            self._outbox = deque(o for o in self._outbox if o.instance is None)
            self._progress = None
        self._state = BridgeState.HANDSHAKE
        self._hello_deadline = self._clock() + self.timing.hello_timeout_s
        self._restart_at = None
        self._restart_step = 0
        log.info("Flipper-app groet: v%d.%d", msg.app_major, msg.app_minor)
        self._send_welcome(fap)

    def _send_welcome(self, fap: _Fap) -> None:
        instance = fap.instance
        agent = self.agent_available
        self._send(
            lambda seq: Welcome(
                seq=seq,
                proto=PROTO_VERSION,
                instance=instance,
                confirm_on_flipper=True,
                agent_available=agent,
            ),
            on_sent=lambda: self._on_welcome_sent(instance),
        )

    def _on_welcome_sent(self, instance: int) -> None:
        fap = self._fap
        if fap is None or fap.instance != instance or self._state is BridgeState.STOPPED:
            return
        if self._state is not BridgeState.CONNECTED:
            self._state = BridgeState.CONNECTED
            self._hello_deadline = None
            if self._lease is None:
                try:
                    self._lease = self.runner.attach_physical_confirmer(FRONTEND)
                except RuntimeError:
                    log.exception("fysieke bevestiger koppelen mislukte")
            self._send_presets(fap)
            self._send_status()
        # Alles wat al openstaat, moet nu op de Flipper (de chat kan het niet meer).
        for action in self.store.list():
            self._queue_prompt(fap, action.id)
        self._show_next()

    # --- bevestigen (§6) --------------------------------------------------------------------

    def _handle_pending_event(self, event: PendingEvent) -> None:
        if self._state is BridgeState.STOPPED:
            return
        pid = event.action.id
        if event.change is PendingChange.CREATED:
            fap = self._fap
            if self._state is BridgeState.CONNECTED and fap is not None:
                self._queue_prompt(fap, pid)
                self._show_next()
            elif self._state is BridgeState.IDLE:
                self._request_start("bevestiging nodig")
            return
        if pid in self._answering:
            return  # de uitkomst van ons eigen OK/Back meldt _on_answered
        fap = self._fap
        if fap is None:
            return
        if fap.shown is not None and fap.shown[1] == pid:
            handle = fap.shown[0]
            fap.handles.revoke(handle)
            fap.shown = None
            reason = (
                CloseReason.EXPIRED
                if event.change is PendingChange.EXPIRED
                else CloseReason.REVOKED
            )
            self._send(lambda seq: ConfirmClosed(seq=seq, handle=handle, reason=reason))
            self._show_next()
        elif pid in fap.queue:
            fap.queue.remove(pid)

    def _queue_prompt(self, fap: _Fap, pid: str) -> None:
        if (fap.shown is not None and fap.shown[1] == pid) or pid in fap.queue:
            return
        if pid in self._answering:
            return
        if len(fap.queue) >= MAX_QUEUED_PROMPTS:
            log.warning("te veel open bevestigingen; deze verschijnt niet op de Flipper")
            return
        fap.queue.append(pid)

    def _show_next(self) -> None:
        """Eén vraag tegelijk op het scherm; de rest wacht (protocol §6)."""
        fap = self._fap
        if fap is None or self._state is not BridgeState.CONNECTED or self._lease is None:
            return
        while fap.shown is None and fap.queue:
            pid = fap.queue.popleft()
            action = self.store.get(pid)
            if action is None:
                continue
            ttl = int(self.store.remaining_s(action))
            if ttl < 1:
                continue
            ttl = min(ttl, MAX_TTL_S)
            try:
                handle = fap.handles.issue(pid, instance=fap.instance, ttl_s=ttl)
            except ProtocolError:
                log.warning("te veel open handles; bevestiging niet getoond")
                return
            fap.shown = (handle, pid)
            summary = _prompt_text(action.summary, action.tool, action.frontend)
            self._send(partial(_prompt, handle=handle, ttl_s=ttl, summary=summary))

    def _on_confirm_answer(self, fap: _Fap, msg: ConfirmAnswer) -> None:
        lease = self._lease
        pid = fap.handles.take(msg.handle, instance=fap.instance) if lease is not None else None
        if fap.shown is not None and fap.shown[0] == msg.handle:
            fap.shown = None
        if lease is None or pid is None:
            # Onbekend, al gebruikt, verlopen of van een andere instance: niets doen.
            self._audit_refusal("ongeldige of verlopen handle", approve=msg.approve)
            self._send_error(msg.seq, ErrorCode.CONFIRM_INVALID, "Bevestiging ongeldig of verlopen")
            self._show_next()
            return
        self._answering.add(pid)
        instance = fap.instance
        handle = msg.handle
        approve = msg.approve

        def job() -> None:
            action = self.store.get(pid)
            tool = action.tool if action is not None else "bevestiging"
            try:
                if approve:
                    raw = self.runner.confirm_physical(lease, pid)
                else:
                    raw = self.runner.reject_physical(lease, pid)
            except Exception as exc:
                log.exception("bevestiging uitvoeren faalde")
                raw = {"ok": False, "error": "internal_error", "message": type(exc).__name__}
            self._post(lambda: self._on_answered(instance, handle, pid, tool, approve, raw))

        if not self._lanes["confirm"].submit(job):
            self._answering.discard(pid)
            self._send_error(msg.seq, ErrorCode.BUSY, "Pi is bezet, probeer opnieuw")

    def _on_answered(
        self,
        instance: int,
        handle: int,
        pid: str,
        tool: str,
        approve: bool,
        raw: dict[str, Any],
    ) -> None:
        self._answering.discard(pid)
        error = raw.get("error")
        if approve:
            if raw.get("ok"):
                reason = CloseReason.EXECUTED
            elif error == "unknown_pending":
                reason = CloseReason.EXPIRED
            else:
                reason = CloseReason.FAILED
            self._record(tool, bool(raw.get("ok")))
        else:
            reason = CloseReason.REJECTED if raw.get("ok") else CloseReason.EXPIRED
        log.info("bevestiging op de Flipper: %s", reason.name.lower())
        fap = self._fap
        if fap is not None and fap.instance == instance:
            self._send(lambda seq: ConfirmClosed(seq=seq, handle=handle, reason=reason))
        if self._session is not None:
            session = self._session
            self._lanes["action"].submit(
                partial(_external_outcome, session, pid, approve=approve, result=raw)
            )
        self._send_status()
        self._show_next()

    def _audit_refusal(self, reason: str, *, approve: bool) -> None:
        try:
            self.runner.audit.write(
                event="flipper_confirm_refused", frontend=FRONTEND, reason=reason, approve=approve
            )
        except OSError:
            log.exception("audit-regel flipper_confirm_refused kon niet worden geschreven")
        log.warning("antwoord van de Flipper geweigerd: %s", reason)

    # --- acties (§7) ------------------------------------------------------------------------

    def _on_action(self, fap: _Fap, msg: Action) -> None:
        if msg.kind is ActionKind.QUICK:
            try:
                plan = quick_action_plan(msg)
            except ProtocolError as exc:
                self._send_error(msg.seq, exc.code, "onbekende actie")
                return
            if msg.action_id == QuickAction.EMERGENCY_STOP:
                self._emergency_stop(fap, msg.seq)
                return
            work: Callable[[_Job], str] = partial(self._quick, plan)
        else:
            preset = self.presets.get(msg.action_id)
            if preset is None:
                self._send_error(msg.seq, ErrorCode.UNKNOWN_ACTION, "onbekende preset")
                return
            if self.provider is None:
                self._send_error(
                    msg.seq,
                    ErrorCode.AGENT_UNAVAILABLE,
                    "Geen LLM ingesteld op de Pi (PITOOLBOX_LLM_*)",
                )
                return
            work = partial(self._preset, preset)
        if self._action is not None:
            self._send_error(msg.seq, ErrorCode.BUSY, "er loopt al een actie")
            return
        job = _Job(ref=msg.seq, instance=fap.instance)
        self._action = job

        def run() -> None:
            text: str | None = None
            failed: _ActionFailed | None = None
            try:
                text = work(job)
            except _ActionFailed as exc:
                failed = exc
            except Exception as exc:
                log.exception("actie van de Flipper faalde")
                failed = _ActionFailed(ErrorCode.TOOL_FAILED, type(exc).__name__)
            self._post(partial(self._finish_action, job, text, failed))

        if not self._lanes["action"].submit(run):
            self._action = None
            self._send_error(msg.seq, ErrorCode.BUSY, "Pi is bezet")

    def _finish_action(self, job: _Job, text: str | None, failed: _ActionFailed | None) -> None:
        if self._action is job:
            self._action = None
        fap = self._fap
        if job.cancelled.is_set() or fap is None or fap.instance != job.instance:
            return  # afgebroken of andere app: het antwoord hoort nergens meer bij
        if failed is not None:
            self._send_error(job.ref, failed.code, failed.text)
        else:
            self._send_result(fap, job.ref, text or "(leeg)")
        self._send_status()

    def _on_cancel(self, fap: _Fap, msg: Cancel) -> None:
        job = self._action
        if job is None or job.ref != msg.ref:
            return
        job.cancelled.set()
        self._action = None
        self._send_error(msg.ref, ErrorCode.CANCELLED, "afgebroken")

    def _emergency_stop(self, fap: _Fap, ref: int) -> None:
        """Altijd, ook als er al een actie loopt: direct in een eigen thread."""
        job, self._action = self._action, None
        if job is not None:
            job.cancelled.set()
        instance = fap.instance
        if not self._estop_lock.acquire(blocking=False):
            self._send_result(fap, ref, "Noodstop loopt al.")
            return

        def run() -> None:
            try:
                raw = self.runner.call(FRONTEND, "emergency_stop", {})
            except Exception as exc:
                log.exception("noodstop faalde")
                raw = {"ok": False, "error": "internal_error", "message": type(exc).__name__}
            finally:
                self._estop_lock.release()
            self._record("emergency_stop", bool(raw.get("ok")))
            self._post(lambda: self._on_estop_done(instance, ref, raw))

        threading.Thread(target=run, name="flipper-ui-estop", daemon=True).start()

    def _on_estop_done(self, instance: int, ref: int, raw: dict[str, Any]) -> None:
        fap = self._fap
        if fap is not None and fap.instance == instance:
            self._send_result(fap, ref, _estop_text(raw))
        self._send_status()

    def _call(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        raw = self.runner.call(FRONTEND, tool, dict(args))
        self._record(tool, bool(raw.get("ok")))
        return raw

    def _quick(self, plan: QuickActionPlan, job: _Job) -> str:
        """Action-lane: vaste tool-calls via de runner, met een korte samenvatting."""
        if plan.per_active_capture:
            status = self._call("status", {})
            ids = [
                c["capture_id"]
                for c in _list(_result(status).get("captures"))
                if isinstance(c, dict) and isinstance(c.get("capture_id"), str)
            ]
            if not ids:
                return "Geen actieve captures."
            stopped = 0
            for capture_id in ids:
                if job.cancelled.is_set():
                    break
                raw = self._call(plan.tool, {**plan.args, "capture_id": capture_id})
                stopped += bool(raw.get("ok"))
            return f"{stopped} van {len(ids)} capture(s) gestopt."
        raw = self._call(plan.tool, plan.args)
        if not raw.get("ok"):
            raise _ActionFailed(ErrorCode.TOOL_FAILED, _error_text(raw))
        if plan.tool == "status":
            return _status_text(_result(raw), pending=len(self.store.list()))
        if plan.tool == "wifi_scan":
            return _wifi_text(_result(raw))
        return "OK."

    def _preset(self, preset: PresetConfig, job: _Job) -> str:
        """Action-lane: één agent-beurt met de prompt van de Pi (nooit tekst van de FAP)."""
        provider = self.provider
        if provider is None:  # gecontroleerd vóór de job; puur defensief
            raise _ActionFailed(ErrorCode.AGENT_UNAVAILABLE, "geen LLM")
        if self._session is None:
            self._session = AgentSession(
                provider, self.runner, frontend=FRONTEND, limits=self.limits
            )
        session = self._session
        ref, instance = job.ref, job.instance

        def on_event(event: AgentEvent) -> None:
            if isinstance(event, ToolCallStarted):
                text = to_display_text(f"tool: {event.name}", max_len=MAX_PROGRESS)
                self._post(lambda: self._send_progress(instance, ref, text))

        result = session.run_turn(preset.prompt, on_event, cancel=job.cancelled.is_set)
        if result.stop is TurnStop.LLM_ERROR:
            code = (result.error or "fout").split(":", 1)[0]
            raise _ActionFailed(ErrorCode.TOOL_FAILED, f"Fout van het model ({code})")
        text = result.text or ""
        notice = _TURN_NOTICES.get(result.stop)
        if notice:
            text = f"{text}\n\n{notice}" if text else notice
        if result.pending:
            text += "\n\nBevestig op de Flipper."
        return text or "(geen antwoord)"

    def _record(self, tool: str, ok: bool) -> None:
        with self._last_lock:
            self._last_tool = tool
            self._last_ok = ok
            self._last_at = self._clock()

    # --- berichten naar de FAP ----------------------------------------------------------------

    def _send(
        self,
        build: Callable[[int], Message],
        *,
        on_sent: Callable[[], None] | None = None,
        instance: int | None = -1,
    ) -> None:
        """Zet een bericht in de zendrij. Standaard alleen voor de huidige instance."""
        if instance == -1:
            fap = self._fap
            if fap is None:
                return
            instance = fap.instance
        with self._out_cond:
            if len(self._outbox) >= _MAX_OUTBOX:
                self.dropped_out += 1
                log.warning("zendrij naar de app vol; bericht weggegooid")
                return
            self._outbox.append(_Out(build, instance, on_sent))
            self._out_cond.notify_all()

    def _send_error(
        self, ref: int, code: ErrorCode, text: str, *, instance: int | None = -1
    ) -> None:
        clean = to_display_text(text, max_len=MAX_ERROR_TEXT)
        if instance == -1 and self._fap is None:
            instance = None
        self._send(lambda seq: Error(seq=seq, ref=ref, code=code, text=clean), instance=instance)

    def _send_result(self, fap: _Fap, ref: int, text: str) -> None:
        for chunk in chunk_result(ref, text, SeqCounter(), max_msg=fap.max_rx):
            self._send(partial(_with_seq, chunk))
        # Een PROGRESS die nog wacht, is nu achterhaald.
        with self._out_cond:
            if self._progress is not None and self._progress.progress_ref == ref:
                self._progress = None

    def _send_progress(self, instance: int, ref: int, text: str) -> None:
        fap = self._fap
        job = self._action
        if fap is None or fap.instance != instance or job is None or job.ref != ref:
            return
        with self._out_cond:
            self._progress = _Out(
                lambda seq: Progress(seq=seq, ref=ref, text=text), instance, progress_ref=ref
            )
            self._out_cond.notify_all()

    def _send_presets(self, fap: _Fap) -> None:
        items = tuple(Preset(id=p.id, label=p.label) for p in self.presets.values())
        self._send(lambda seq: Presets(seq=seq, items=items))

    def _send_status(self) -> None:
        fap = self._fap
        if fap is None or self._state is not BridgeState.CONNECTED:
            return
        instance = fap.instance

        def job() -> None:
            raw = self.runner.call(FRONTEND, "status", {})
            build = self._status_builder(_result(raw), len(self.store.list()))
            self._post(lambda: self._send(build) if self.instance == instance else None)

        self._lanes["aux"].submit(job)

    def _status_builder(self, status: Mapping[str, Any], pending: int) -> Callable[[int], Status]:
        power = _dict(status.get("power"))
        uplink = _dict(status.get("uplink"))
        flipper = _dict(status.get("flipper"))
        captures = status.get("captures")
        available = power.get("available") is True
        occurred = _list(power.get("occurred"))
        ipv4 = _list(uplink.get("ipv4"))
        iface = str(uplink.get("iface") or "?")
        up = uplink.get("operstate") == "UP" and bool(ipv4)
        uplink_text = to_display_text(
            f"{iface} {ipv4[0]}" if ipv4 else f"{iface} geen IP", max_len=MAX_UPLINK
        )
        kind = flipper.get("transport_kind")
        link = Link.USB if kind == "usb" else Link.BLE if kind == "ble" else Link.UNKNOWN
        with self._last_lock:
            tool, ok, at = self._last_tool, self._last_ok, self._last_at
        last = LastResult.NONE if ok is None else LastResult.OK if ok else LastResult.FAILED
        age = None if at is None else min(int(self._clock() - at), 0xFFFE)
        return lambda seq: Status(
            seq=seq,
            uplink_up=up,
            undervoltage_now=available and power.get("undervoltage_now") is True,
            undervoltage_occurred=available and "undervoltage" in occurred,
            power_unknown=not available,
            link=link,
            captures=min(len(captures), 255) if isinstance(captures, list) else 0,
            pending=min(pending, 255),
            last_result=last,
            last_age_s=max(age, 0) if age is not None else None,
            uplink=uplink_text,
            last_tool=to_display_text(tool, max_len=MAX_TOOL_NAME),
        )

    # --- lezer ---------------------------------------------------------------------------------

    def _reader_main(self) -> None:
        while not self._stopping:
            if self.device.rpc_app_state != "active" or not self.device.poll_app_events(
                self.timing.poll_slice_s
            ):
                time.sleep(self.timing.poll_idle_s)

    # --- zender ------------------------------------------------------------------------------

    def _next_out(self) -> _Out | None:
        """Onder `_out_cond`: eerst de gewone rij, dan (hoogstens 1/s) de laatste PROGRESS."""
        if self._outbox:
            return self._outbox.popleft()
        if self._progress is not None and (
            time.monotonic() - self._last_progress >= self.timing.progress_gap_s
        ):
            item, self._progress = self._progress, None
            self._last_progress = time.monotonic()
            return item
        return None

    def _sender_main(self) -> None:
        while True:
            with self._out_cond:
                item = None
                while not self._stopping:
                    item = self._next_out()
                    if item is not None:
                        break
                    self._out_cond.wait(self.timing.tick_s)
                if self._stopping:
                    return
                current = self._send_instance
            assert item is not None  # noqa: S101
            if item.instance is not None and item.instance != current:
                continue  # bedoeld voor een app die er niet meer is
            wait = self._last_send + self.timing.send_gap_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                data = encode(item.build(self._tx_seq.next()))
            except (ProtocolError, ValueError):
                log.exception("bericht naar de app kon niet gecodeerd worden")
                continue
            outcome = self._deliver(data)
            self._last_send = time.monotonic()
            if outcome is _Delivery.SENT and item.on_sent is not None:
                self._post(item.on_sent)
            elif outcome is _Delivery.DEAD:
                with self._out_cond:
                    self._outbox.clear()
                    self._progress = None
                self._post(self._on_send_dead)

    def _on_send_dead(self) -> None:
        # Meestal meldde het device dit al (lost/closed); dit is het vangnet.
        if self._fap is not None or self._state in (BridgeState.HANDSHAKE, BridgeState.CONNECTED):
            self._detach("app reageert niet", restart=True)

    def _deliver(self, data: bytes) -> _Delivery:
        for attempt in range(self.timing.send_retries + 1):
            try:
                self.device.app_send(data, self.timing.send_timeout_s)
                return _Delivery.SENT
            except AppRejected:
                if attempt == self.timing.send_retries:
                    log.warning("app weigerde een bericht %d keer; laten vallen", attempt + 1)
                    return _Delivery.DROPPED
                time.sleep(self.timing.send_retry_s)
            except (AppNotRunning, DeviceError) as exc:
                log.info("versturen naar de app mislukte: %s", exc)
                return _Delivery.DEAD
            except ToolboxError as exc:  # bijv. Aborted door een noodstop midden in een write
                log.info("versturen naar de app afgebroken: %s", exc)
                return _Delivery.DROPPED
        return _Delivery.DROPPED  # pragma: no cover


# --- tekst voor het scherm --------------------------------------------------------------------

_TURN_NOTICES = {
    TurnStop.MAX_TOKENS: "(antwoord afgekapt)",
    TurnStop.REFUSAL: "(het model weigerde)",
    TurnStop.MAX_STEPS: "(gestopt: te veel stappen)",
    TurnStop.TIMEOUT: "(gestopt: tijdslimiet)",
    TurnStop.INTERRUPTED: "(afgebroken)",
}


def _closed(seq: int, *, handle: int, reason: CloseReason) -> ConfirmClosed:
    return ConfirmClosed(seq=seq, handle=handle, reason=reason)


def _prompt(seq: int, *, handle: int, ttl_s: int, summary: str) -> ConfirmPrompt:
    return ConfirmPrompt(seq=seq, handle=handle, ttl_s=ttl_s, summary=summary)


def _with_seq(chunk: ResultChunk, seq: int) -> ResultChunk:
    return chunk.model_copy(update={"seq": seq})


def _external_outcome(
    session: AgentSession, pending_id: str, *, approve: bool, result: dict[str, Any]
) -> None:
    session.external_outcome(pending_id, approved=approve, result=result)


def _prompt_text(summary: str, tool: str, frontend: str) -> str:
    origin = frontend.split(":", 1)[0]
    text = to_display_text(f"{summary}\n(van {origin})", max_len=MAX_SUMMARY, multiline=True)
    return text if text.strip() else to_display_text(tool, max_len=MAX_SUMMARY) or "transmit"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _result(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _dict(raw.get("result"))


def _error_text(raw: Mapping[str, Any]) -> str:
    return f"{raw.get('error', 'fout')}: {raw.get('message', '')}".rstrip(": ")


def _status_text(status: Mapping[str, Any], *, pending: int) -> str:
    lines: list[str] = []
    uplink = status.get("uplink")
    if isinstance(uplink, dict) and "iface" in uplink:
        ipv4 = _list(uplink.get("ipv4"))
        state = str(uplink.get("operstate") or "?").lower()
        lines.append(f"Uplink: {uplink['iface']} {ipv4[0] if ipv4 else 'geen IP'} ({state})")
    else:
        lines.append("Uplink: onbekend")
    power = status.get("power")
    if isinstance(power, dict) and power.get("available"):
        if power.get("undervoltage_now"):
            lines.append("Voeding: ONDERSPANNING")
        elif "undervoltage" in _list(power.get("occurred")):
            lines.append("Voeding: eerder onderspanning")
        else:
            lines.append("Voeding: ok")
    else:
        lines.append("Voeding: onbekend")
    flipper = status.get("flipper")
    if isinstance(flipper, dict):
        lines.append(f"Flipper: {flipper.get('transport_kind') or '?'}")
    wifi = status.get("wifi")
    if isinstance(wifi, dict):
        lines.append(f"Wifi: {wifi.get('iface', '?')} {wifi.get('mode', '?')}")
    captures = status.get("captures")
    lines.append(f"Captures: {len(captures) if isinstance(captures, list) else '?'}")
    scope = status.get("scope")
    if isinstance(scope, dict):
        lines.append(f"Scope: {scope.get('networks', '?')} wifi, {scope.get('ir_devices', '?')} IR")
        if scope.get("error"):
            lines.append("Scope-fout!")
    lines.append(f"Open bevestigingen: {pending}")
    return "\n".join(lines)


def _wifi_text(scan: Mapping[str, Any], top: int = 5) -> str:
    networks = _list(scan.get("networks"))
    lines = [
        f"{scan.get('count', len(networks))} netwerken, {scan.get('in_scope_count', 0)} in scope"
    ]
    shown = [n for n in networks if isinstance(n, dict)][:top]
    if shown:
        lines.append("Sterkst:")
    for net in shown:
        ssid = str(net.get("ssid") or "(verborgen)")[:20]
        signal = net.get("signal_dbm")
        mark = " [scope]" if net.get("in_scope") else ""
        lines.append(f" {ssid} {signal if signal is not None else '?'}dBm{mark}")
    return "\n".join(lines)


def _estop_text(raw: Mapping[str, Any]) -> str:
    if not raw.get("ok"):
        return f"Noodstop gaf een fout: {_error_text(raw)}"
    result = _result(raw)
    stopped = result.get("captures_stopped")
    count = len(stopped) if isinstance(stopped, list) else 0
    errors = result.get("errors") or {}
    text = (
        f"Noodstop uitgevoerd.\n{count} capture(s) gestopt,\n"
        f"{result.get('pending_cleared', 0)} bevestiging(en) gewist."
    )
    if errors:
        text += f"\nFouten: {', '.join(sorted(str(k) for k in errors))}"
    return text


# --- opbouw vanuit Settings -------------------------------------------------------------------


def start_from_settings(runner: ToolRunner, settings: Settings) -> FlipperUiBridge | None:
    """Start de bridge als `PITOOLBOX_FLIPPER_UI=on`; anders None.

    Presets uit `PITOOLBOX_FLIPPER_UI_PRESETS` (ongeldig: gelogd, dan zonder presets). Een
    LLM-provider alleen als die ingesteld is; zonder provider meldt WELCOME "geen agent" en
    krijgt een preset ERROR `AGENT_UNAVAILABLE`. De bridge sluit zijn eigen provider bij
    `stop()`.
    """
    if settings.flipper_ui != "on":
        return None
    from pitoolbox.frontends.flipper_ui.presets import PresetsError, load_presets
    from pitoolbox.llm.errors import LLMConfigError
    from pitoolbox.llm.providers import build_provider

    try:
        presets = load_presets(settings.flipper_ui_presets)
    except PresetsError as exc:
        log.error("Flipper-presets niet geladen: %s", exc)
        presets = ()
    provider: LLMProvider | None = None
    limits: AgentLimits | None = None
    if settings.llm_provider:
        try:
            limits = AgentLimits.from_settings(settings)
            provider = build_provider(settings)
        except (LLMConfigError, ValueError) as exc:
            log.error("Flipper-presets zonder agent: %s", exc)
            provider = None
    bridge = FlipperUiBridge(
        runner,
        app_path=settings.flipper_ui_app,
        presets=presets,
        provider=provider,
        limits=limits,
        owns_provider=True,
    )
    bridge.start()
    return bridge
