"""Snelle acties en presets van de Flipper (protocol §7), en de noodstop.

Eén actie tegelijk (`_action`, alleen in de hoofdlus gezet); het werk loopt in de action-lane
via de runner (frontend `FRONTEND`) of een eigen `AgentSession` (presets, met de prompt van de
Pi). De noodstop gaat altijd door, ook als er al een actie loopt, in een eigen thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from pitoolbox.agent.session import AgentEvent, AgentSession, ToolCallStarted, TurnStop
from pitoolbox.frontends.flipper_ui.bridge_core import _Fap, _Job
from pitoolbox.frontends.flipper_ui.bridge_output import _FapOutput
from pitoolbox.frontends.flipper_ui.bridge_text import (
    _TURN_NOTICES,
    _error_text,
    _estop_text,
    _list,
    _result,
    _status_text,
    _wifi_text,
)
from pitoolbox.frontends.flipper_ui.bridge_types import FRONTEND, LOG_NAME
from pitoolbox.frontends.flipper_ui.presets import PresetConfig
from pitoolbox.frontends.flipper_ui.protocol import (
    MAX_PROGRESS,
    Action,
    ActionKind,
    Cancel,
    ErrorCode,
    ProtocolError,
    QuickAction,
    QuickActionPlan,
    quick_action_plan,
    to_display_text,
)

log = logging.getLogger(LOG_NAME)


class _ActionFailed(Exception):
    def __init__(self, code: ErrorCode, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


class _ActionFlow(_FapOutput):
    """Zie `bridge_core.py` voor hoe de delen van de bridge op elkaar bouwen."""

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
