"""Berichten naar de FAP, de kant van de hoofdlus: welke instance, en wat er in de zendrij gaat.

Het versturen zelf doet de zender-thread (`bridge_sender.py`). STATUS wordt in de aux-lane
samengesteld (de status-tool via de runner) en daarna in de hoofdlus in de rij gezet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from pitoolbox.frontends.flipper_ui.bridge_core import _BridgeCore, _Fap
from pitoolbox.frontends.flipper_ui.bridge_text import (
    _error_screen_text,
    _result,
    _status_message,
)
from pitoolbox.frontends.flipper_ui.bridge_types import FRONTEND, BridgeState
from pitoolbox.frontends.flipper_ui.protocol import (
    Error,
    ErrorCode,
    Message,
    Preset,
    Presets,
    Progress,
    ResultChunk,
    SeqCounter,
    Status,
    chunk_result,
)


class _FapOutput(_BridgeCore):
    """Zie `bridge_core.py` voor hoe de delen van de bridge op elkaar bouwen."""

    def _send(
        self,
        build: Callable[[int], Message | None],
        *,
        on_sent: Callable[[], None] | None = None,
        on_failed: Callable[[], None] | None = None,
        instance: int | None = -1,
    ) -> None:
        """Zet een bericht in de zendrij. Standaard alleen voor de huidige instance."""
        if instance == -1:
            fap = self._fap
            if fap is None:
                return
            instance = fap.instance
        queued = self._sender.put(build, instance, on_sent=on_sent, on_failed=on_failed)
        if not queued and on_failed is not None:
            self._post(on_failed)

    def _send_error(
        self, ref: int, code: ErrorCode, text: str, *, instance: int | None = -1
    ) -> None:
        clean = _error_screen_text(code, text)
        if instance == -1 and self._fap is None:
            instance = None
        self._send(lambda seq: Error(seq=seq, ref=ref, code=code, text=clean), instance=instance)

    def _send_result(self, fap: _Fap, ref: int, text: str) -> None:
        for chunk in chunk_result(ref, text, SeqCounter(), max_msg=fap.max_rx):
            self._send(partial(_with_seq, chunk))
        # Een PROGRESS die nog wacht, is nu achterhaald.
        self._sender.drop_progress(ref)

    def _send_progress(self, instance: int, ref: int, text: str) -> None:
        fap = self._fap
        job = self._action
        if fap is None or fap.instance != instance or job is None or job.ref != ref:
            return
        self._sender.put_progress(lambda seq: Progress(seq=seq, ref=ref, text=text), instance, ref)

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
        with self._last_lock:
            tool, ok, at = self._last_tool, self._last_ok, self._last_at
        age = None if at is None else min(int(self._clock() - at), 0xFFFE)
        return _status_message(status, pending, tool=tool, ok=ok, age=age)


def _with_seq(chunk: ResultChunk, seq: int) -> ResultChunk:
    return chunk.model_copy(update={"seq": seq})
