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

Het verzoekbestand (§8) wordt gepold als het device in RPC staat (`device.status()`, zonder
lock). Zonder verbinding opent de bridge er zelf één (RPC-ping, daarna keepalive): zonder
RPC-sessie van de Pi schrijft de FAP geen verzoek (`rpc_get_sessions_count`), en een vers
gestarte service kreeg anders nooit een sessie (hardware, 2026-10-17). Mislukt verbinden, dan
`link_backoff_s` (geen reconnect-storm over BLE). Een open link in CLI-modus blijft met rust:
USB gaat niet elke paar seconden van CLI naar RPC.

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

**Eén vraag tegelijk, en pas na CONFIRM_CLOSED.** De rij met vragen is de `PendingStore` zelf
(oudste eerst, zonder de pendings waarvoor een OK/Back loopt): een pending kan dus niet uit
een eigen rij vallen. Een nieuwe CONFIRM_PROMPT gaat pas weg als de CONFIRM_CLOSED van de
vorige echt afgeleverd is; een CLOSED of PROMPT die niet aankomt, maakt de sessie
onbetrouwbaar (loskoppelen, de app opnieuw laten groeten). Verloopt een handle terwijl de
pending nog leeft (handle-TTL ≤ 120 s), dan volgt een nieuwe vraag met een nieuwe handle.

**Opnieuw groeten.** Draait de app nog terwijl de bridge losgekoppeld is (bijv. na een
CLOSED die niet aankwam), dan stuurt de bridge bij het "starten" ERROR `NOT_READY`; de FAP
groet daarop opnieuw (§5). Komt er geen HELLO, dan sluit de bridge zijn eigen app.

**Waarnemen.** `observer` (optioneel) krijgt `BridgeEvent`s: app gestart of niet, HELLO,
handdruk rond, elk afgeleverd bericht, losgekoppeld. Alleen kijken, niet sturen; gebruikt door
`pitoolbox-flipper ui-test` en `ui-droptest`. `send_status()` stuurt een verse STATUS.

**Opbouw.** `FlipperUiBridge` is één object, per verantwoordelijkheid verdeeld over
`bridge_core.py` (toestand, inbox, loskoppelen), `bridge_output.py` (berichten naar de FAP),
`bridge_sender.py` (zendrij en zender), `bridge_start.py` (starten, §8), `bridge_confirm.py`
(bevestigen, §6), `bridge_actions.py` (acties, §7), `bridge_intake.py` (ingangen van de
hoofdlus, de lezer, decoderen en verdelen van FAP-berichten), `bridge_lanes.py` (werkthreads)
en `bridge_text.py` (tekst voor het scherm). Hier: levenscyclus, hoofdlus, timers,
app-gebeurtenissen en de handdruk (§5). Alles hierboven geldt voor alle delen.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from pitoolbox.agent.session import AgentLimits
from pitoolbox.core.devices.flipper import AppEvent
from pitoolbox.frontends.flipper_ui.bridge_core import _Fap
from pitoolbox.frontends.flipper_ui.bridge_intake import _Intake
from pitoolbox.frontends.flipper_ui.bridge_start import (
    START_REQUEST_MAX_AGE_S,
    START_REQUEST_MAX_BYTES,
    START_REQUEST_PATH,
)
from pitoolbox.frontends.flipper_ui.bridge_types import (
    FRONTEND,
    BridgeEvent,
    BridgeEventKind,
    BridgeObserver,
    BridgeState,
    BridgeTiming,
)
from pitoolbox.frontends.flipper_ui.protocol import (
    PROTO_VERSION,
    ConfirmHandles,
    ErrorCode,
    Hello,
    Welcome,
)

if TYPE_CHECKING:
    from pitoolbox.config import Settings
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider

__all__ = [
    "FRONTEND",
    "MAX_QUEUED_PROMPTS",
    "START_REQUEST_MAX_AGE_S",
    "START_REQUEST_MAX_BYTES",
    "START_REQUEST_PATH",
    "BridgeEvent",
    "BridgeEventKind",
    "BridgeObserver",
    "BridgeState",
    "BridgeTiming",
    "FlipperUiBridge",
    "start_from_settings",
]

log = logging.getLogger(__name__)

MAX_QUEUED_PROMPTS = 8  # protocol §6: hoogstens 8 open handles (in de praktijk 1: één vraag)


class FlipperUiBridge(_Intake):
    """Pi-kant van docs/fase4-protocol.md. `start()` en `stop()` vanuit de eigenaar."""

    # --- levenscyclus -----------------------------------------------------------------

    @property
    def state(self) -> BridgeState:
        return self._state

    @property
    def agent_available(self) -> bool:
        return self.provider is not None

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
            ("flipper-ui-sender", self._sender.run),
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
            self._stopping.set()
            self._cond.notify_all()
        self._sender.stop()
        self.device.set_app_sink(None)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(self.timing.stop_join_s)
        for lane in self._lanes.values():
            lane.stop(self.timing.stop_join_s)
        if self.device.rpc_app_state != "off":
            self._exit_app_bounded()
        if self._session is not None:
            self._session.close()
        if self._owns_provider and self.provider is not None:
            self.provider.close()
        log.info("Flipper-bridge gestopt")

    def _exit_app_bounded(self) -> None:
        """App sluiten, maar stop() wacht hoogstens `stop_join_s`: `exit_rpc_app` wacht op de
        device-lock, en een lopende tool (bijv. een lange capture) kan die lang vasthouden. De
        lease is dan al los; de app sluit later alsnog (of bij `device.close()`)."""
        thread = threading.Thread(
            target=self.device.exit_rpc_app, name="flipper-ui-exit", daemon=True
        )
        thread.start()
        thread.join(self.timing.stop_join_s)
        if thread.is_alive():
            log.warning("Flipper-bridge: app sluiten wacht op een lopende Flipper-actie")

    # --- inbox ------------------------------------------------------------------------

    def _on_app_event(self, event: AppEvent) -> None:
        # Device-thread, met de device-lock: alleen doorzetten.
        self._post(lambda: self._handle_app_event(event), external=True)

    def _main(self) -> None:
        while True:
            with self._cond:
                if not self._inbox and not self._stopping.is_set():
                    self._cond.wait(self.timing.tick_s)
                items = list(self._inbox)
                self._inbox.clear()
                self._external = 0
                stopping = self._stopping.is_set()
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
            self._state is BridgeState.STARTING
            and self._start_deadline is not None
            and now >= self._start_deadline
        ):
            # Vangnet: de start-job meldde zich nooit (bijv. de device-lock bleef bezet).
            log.warning("PiToolbox-app starten duurde te lang; opgegeven")
            self._start_deadline = None
            self._state = BridgeState.IDLE
            return
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
        elif now >= self._next_poll and not self._poll_busy:
            # Eén poll tegelijk: een mislukte BLE-verbinding duurt langer dan start_poll_s, en
            # opgestapelde jobs zouden de backoff van `_link_failed` omzeilen.
            self._next_poll = now + self.timing.start_poll_s
            self._poll_busy = self._lanes["device"].submit(self._poll_job)

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

    def _on_data(self, data: bytes) -> None:
        msg = self._decode_limited(data)
        if msg is None:
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
        self._dispatch(fap, msg)

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
        self._sender.bind(msg.instance)
        self._state = BridgeState.HANDSHAKE
        self._hello_deadline = self._clock() + self.timing.hello_timeout_s
        self._restart_at = None
        self._restart_step = 0
        log.info("Flipper-app groet: v%d.%d", msg.app_major, msg.app_minor)
        self._notify("hello", f"v{msg.app_major}.{msg.app_minor}", msg.instance)
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
            # Eerst de lease, dan CONNECTED: wie CONNECTED ziet, mag erop rekenen dat alleen
            # nog OK op de Flipper bevestigt. Lukt koppelen niet (er is al een bevestiger),
            # dan geen sessie: vragen tonen zonder die regel zou de mens misleiden.
            if self._lease is None:
                try:
                    self._lease = self.runner.attach_physical_confirmer(FRONTEND)
                except RuntimeError:
                    log.exception("fysieke bevestiger koppelen mislukte; app wordt gesloten")
                    self._detach("fysieke bevestiger koppelen mislukte", restart=False)
                    self._lanes["device"].submit(self.device.exit_rpc_app)
                    return
            self._state = BridgeState.CONNECTED
            self._hello_deadline = None
            self._notify("connected", instance=instance)
            self._send_presets(fap)
            self._send_status()
        # Alles wat al openstaat, moet nu op de Flipper (de chat kan het niet meer).
        self._show_next()


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
