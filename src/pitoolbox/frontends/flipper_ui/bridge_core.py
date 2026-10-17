"""Kern van de Flipper-bridge: alle toestand, de inbox van de hoofdlus en loskoppelen.

`FlipperUiBridge` (`bridge.py`) is één object, per verantwoordelijkheid over modules verdeeld:
`_BridgeCore` (hier) -> `_FapOutput` (`bridge_output.py`, berichten naar de FAP) ->
`_AppStarter` (`bridge_start.py`, de app starten, §8) -> `_ConfirmFlow` (`bridge_confirm.py`,
bevestigen, §6); `_ActionFlow` (`bridge_actions.py`, acties, §7) bouwt ook op `_FapOutput`.
`_Intake` (`bridge_intake.py`, wat de hoofdlus binnenkrijgt) bouwt op beide flows, en
`FlipperUiBridge` (`bridge.py`) op `_Intake`. Elk deel gebruikt alleen wat eronder ligt. De
regels voor threads en toestand staan in de module-docstring van `bridge.py` en gelden voor
alle delen: toestand wordt alleen in de hoofdlus gewijzigd, andere threads posten werk
(`_post`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from pitoolbox.agent.session import AgentLimits, AgentSession
from pitoolbox.core.devices.flipper import FlipperDevice
from pitoolbox.core.policy import PhysicalConfirmer
from pitoolbox.frontends.flipper_ui.bridge_lanes import _Lane
from pitoolbox.frontends.flipper_ui.bridge_sender import _Sender
from pitoolbox.frontends.flipper_ui.bridge_types import (
    LOG_NAME,
    BridgeEvent,
    BridgeEventKind,
    BridgeObserver,
    BridgeState,
    BridgeTiming,
)
from pitoolbox.frontends.flipper_ui.presets import PresetConfig
from pitoolbox.frontends.flipper_ui.protocol import ConfirmHandles, RateLimiter, SeqTracker

if TYPE_CHECKING:
    from pitoolbox.core.tools.runner import ToolRunner
    from pitoolbox.llm.providers import LLMProvider

log = logging.getLogger(LOG_NAME)

_MAX_INBOX = 256  # externe gebeurtenissen (FAP-frames, pendings) in de hoofdlus


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
    closing: set[int] = field(default_factory=set)  # handles met een CLOSED die nog onderweg is


class _BridgeCore:
    """Toestand en basis van `FlipperUiBridge`; zie de module-docstring."""

    def __init__(  # noqa: PLR0913 keyword-only afhankelijkheden (werkt als een struct)
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
        observer: BridgeObserver | None = None,
    ) -> None:
        """`observer` wordt in verschillende threads aangeroepen en mag niet blokkeren (bijv.
        alleen `queue.put_nowait`)."""
        self.runner = runner
        self._observer = observer
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
        self._start_deadline: float | None = None
        self._restart_at: float | None = None
        self._restart_until = 0.0
        self._locked_until = 0.0  # tot hier opnieuw proberen als de loader bezet is
        self._restart_step = 0
        self._next_poll = 0.0
        self._poll_busy = False  # er loopt al een poll-job (er mag er maar één tegelijk zijn)
        self._link_step = 0
        self._next_expire = 0.0
        # Per bridge, niet per FAP-sessie (§4): een nieuwe HELLO geeft geen nieuwe burst.
        self._rx_rate = RateLimiter(clock=clock)
        self._session: AgentSession | None = None  # alleen in de action-lane gebruikt

        # --- inbox van de hoofdlus ---
        self._cond = threading.Condition()
        self._inbox: deque[tuple[bool, Callable[[], None]]] = deque()
        self._external = 0
        self.dropped_inbox = 0
        self._stopping = threading.Event()

        # --- zendrij (eigen lock) ---
        self._sender = _Sender(
            self.device,
            self.timing,
            self._stopping,
            self._post,
            on_delivered=partial(self._notify, "delivered"),
            on_dead=self._on_send_dead,
        )

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

    @property
    def instance(self) -> int | None:
        fap = self._fap
        return fap.instance if fap is not None else None

    def _notify(self, kind: BridgeEventKind, detail: str = "", instance: int | None = None) -> None:
        observer = self._observer
        if observer is None:
            return
        try:
            observer(BridgeEvent(kind, detail, instance, time.monotonic()))
        except Exception:
            log.exception("observer van de Flipper-bridge faalde")

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

    # --- loskoppelen ------------------------------------------------------------------

    def _detach(self, reason: str, *, restart: bool) -> None:
        """Alles van de FAP-sessie weg. Eerst de lease: daarna geldt weer de oude regel."""
        lease, self._lease = self._lease, None
        if lease is not None:
            self.runner.detach_physical_confirmer(lease, reason)
        if self._fap is not None or self._state is not BridgeState.IDLE:
            self._notify("detached", reason, self.instance)
        fap, self._fap = self._fap, None
        if fap is not None:
            fap.handles.revoke_all()
            log.info("Flipper-app losgekoppeld: %s", reason)
        job, self._action = self._action, None
        if job is not None:
            job.cancelled.set()
        self._sender.unbind()
        if self._state is not BridgeState.STOPPED:
            self._state = BridgeState.IDLE
        self._hello_deadline = None
        if restart:
            self._schedule_restart()

    def _schedule_restart(self) -> None:
        now = self._clock()
        if self._restart_at is None and self._restart_step == 0:
            self._restart_until = now + self.timing.restart_window_s
            self._locked_until = now + self.timing.locked_retry_s
        if now >= self._restart_until:
            log.info("PiToolbox-app: opnieuw starten opgegeven")
            self._restart_at = None
            self._restart_step = 0
            return
        backoff = self.timing.restart_backoff_s
        delay = backoff[min(self._restart_step, len(backoff) - 1)]
        self._restart_step += 1
        self._restart_at = now + delay

    def _on_send_dead(self) -> None:
        # Meestal meldde het device dit al (lost/closed); dit is het vangnet.
        if self._fap is not None or self._state in (BridgeState.HANDSHAKE, BridgeState.CONNECTED):
            self._detach("app reageert niet", restart=True)

    # --- laatste toolcall -------------------------------------------------------------

    def _record(self, tool: str, ok: bool) -> None:
        with self._last_lock:
            self._last_tool = tool
            self._last_ok = ok
            self._last_at = self._clock()
