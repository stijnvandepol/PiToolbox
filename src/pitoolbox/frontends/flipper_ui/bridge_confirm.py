"""Bevestigen op de Flipper (protocol §6): de vraag tonen, het antwoord afhandelen.

**Eén vraag tegelijk, en pas na CONFIRM_CLOSED** (zie de module-docstring van `bridge.py`): de
rij is de `PendingStore` zelf; een nieuwe CONFIRM_PROMPT gaat pas weg als de CONFIRM_CLOSED van
de vorige afgeleverd is. Uitvoeren gebeurt alleen via `runner.confirm_physical` met de lease
van deze bridge, in de confirm-lane; de uitkomst gaat via `_post` terug naar de hoofdlus.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from pitoolbox.core.policy import PendingChange, PendingEvent
from pitoolbox.frontends.flipper_ui.bridge_core import _Fap
from pitoolbox.frontends.flipper_ui.bridge_start import _AppStarter
from pitoolbox.frontends.flipper_ui.bridge_text import _prompt_text
from pitoolbox.frontends.flipper_ui.bridge_types import FRONTEND, LOG_NAME, BridgeState
from pitoolbox.frontends.flipper_ui.protocol import (
    MAX_TTL_S,
    CloseReason,
    ConfirmAnswer,
    ConfirmClosed,
    ConfirmPrompt,
    ErrorCode,
    ProtocolError,
)

log = logging.getLogger(LOG_NAME)


class _ConfirmFlow(_AppStarter):
    """Zie `bridge_core.py` voor hoe de delen van de bridge op elkaar bouwen."""

    # --- bevestigen (§6) --------------------------------------------------------------------

    def _expire_handles(self) -> None:
        fap = self._fap
        if fap is None:
            return
        for handle, _pid in fap.handles.expire():
            # Leeft de pending nog (TTL > handle-TTL), dan vraagt _show_next hem opnieuw.
            log.info("bevestiging op de Flipper verlopen (handle %08x)", handle)
            if fap.shown is not None and fap.shown[0] == handle:
                fap.shown = None
            self._close_prompt(fap, handle, CloseReason.EXPIRED)
        self._show_next()

    def _handle_pending_event(self, event: PendingEvent) -> None:
        if self._state is BridgeState.STOPPED:
            return
        pid = event.action.id
        if event.change is PendingChange.CREATED:
            if self._state is BridgeState.CONNECTED and self._fap is not None:
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
            self._close_prompt(fap, handle, reason)

    def _close_prompt(self, fap: _Fap, handle: int, reason: CloseReason) -> None:
        """CONFIRM_CLOSED naar de FAP. Tot die afgeleverd is, gaat er geen nieuwe vraag weg:
        anders kan een OK die al onderweg was, op een vraag terechtkomen die de mens niet zag.
        Komt hij niet aan, dan is de sessie onbetrouwbaar (`_on_confirm_undelivered`)."""
        fap.closing.add(handle)
        self._send(
            partial(_closed, handle=handle, reason=reason),
            on_sent=partial(self._on_closed_sent, fap, handle),
            on_failed=partial(self._on_confirm_undelivered, fap, "CONFIRM_CLOSED"),
        )

    # Deze callbacks vergelijken de sessie zelf (`_Fap`), niet het instance-nummer: na opnieuw
    # groeten (§5) heeft de nieuwe sessie hetzelfde nummer.

    def _on_closed_sent(self, fap: _Fap, handle: int) -> None:
        if self._fap is not fap:
            return
        fap.closing.discard(handle)
        self._show_next()

    def _on_confirm_undelivered(self, fap: _Fap, what: str) -> None:
        """Een PROMPT of CLOSED kwam niet aan terwijl de link in orde leek: wat er op het
        scherm staat, weet de Pi dan niet meer. Loskoppelen (eerst de lease) en de app opnieuw
        laten groeten; daarna komen de open pendings opnieuw, met nieuwe handles."""
        if self._fap is not fap or self._state is BridgeState.STOPPED:
            return
        log.warning("%s niet afgeleverd; sessie met de app opnieuw opbouwen", what)
        self._detach(f"{what} niet afgeleverd", restart=True)

    def _show_next(self) -> None:
        """Eén vraag tegelijk op het scherm (protocol §6). De rij is de PendingStore zelf:
        oudste eerst, zonder de pendings waarvoor nog een OK/Back loopt."""
        fap = self._fap
        if (
            fap is None
            or self._state is not BridgeState.CONNECTED
            or self._lease is None
            or fap.shown is not None
            or fap.closing
        ):
            return
        for action in self.store.list():
            pid = action.id
            if pid in self._answering:
                continue
            ttl = min(self.store.remaining_s(action), float(MAX_TTL_S))
            if ttl < 1:
                continue
            # Vóór issue() vastgelegd: het aftellen op de FAP (vanaf `deadline`) eindigt zo
            # nooit ná de handle; die verloopt uiterlijk met de pending (fractie van een ms).
            deadline = self._clock() + ttl
            try:
                handle = fap.handles.issue(pid, instance=fap.instance, ttl_s=ttl)
            except ProtocolError:
                log.warning("te veel open handles; bevestiging niet getoond")
                return
            fap.shown = (handle, pid)
            summary = _prompt_text(action.summary, action.tool, action.frontend)
            self._send(
                partial(self._build_prompt, handle=handle, deadline=deadline, summary=summary),
                on_failed=partial(self._on_confirm_undelivered, fap, "CONFIRM_PROMPT"),
            )
            return

    def _build_prompt(
        self, seq: int, *, handle: int, deadline: float, summary: str
    ) -> ConfirmPrompt | None:
        """Zender-thread: de TTL pas nu berekenen, zodat het aftellen op de FAP niet langer is
        dan de handle-TTL op de Pi (ook niet na een tijd in de zendrij). Is de tijd al op, dan
        niet sturen; de handle verloopt vanzelf (CONFIRM_CLOSED) en de pending komt opnieuw."""
        ttl = min(int(deadline - self._clock()), MAX_TTL_S)
        if ttl < 1:
            return None
        return ConfirmPrompt(seq=seq, handle=handle, ttl_s=ttl, summary=summary)

    def _on_confirm_answer(self, fap: _Fap, msg: ConfirmAnswer) -> None:
        # CONFIRM_ANSWER heeft geen instance-veld: `fap` is de sessie sinds de laatste HELLO,
        # en een handle bestaat alleen in de ConfirmHandles van die sessie (§6).
        lease = self._lease
        on_screen = fap.shown is not None and fap.shown[0] == msg.handle
        pid = fap.handles.take(msg.handle, instance=fap.instance) if lease is not None else None
        if on_screen:
            fap.shown = None
        if lease is None or pid is None:
            # Onbekend, al gebruikt, verlopen of van een vorige sessie: niets doen.
            self._audit_refusal("ongeldige of verlopen handle", approve=msg.approve)
            self._send_error(msg.seq, ErrorCode.CONFIRM_INVALID, "Bevestiging ongeldig of verlopen")
            if on_screen:
                # Onze handle, net verlopen (de timer was nog niet langs): de FAP moet horen
                # dat er niets gebeurde, anders blijft "ja gestuurd" staan.
                fap.handles.revoke(msg.handle)
                reason = CloseReason.EXPIRED if lease is not None else CloseReason.REVOKED
                self._close_prompt(fap, msg.handle, reason)
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
            # De handle is verbruikt, de pending niet: intrekken en opnieuw vragen.
            self._answering.discard(pid)
            self._send_error(msg.seq, ErrorCode.BUSY, "Pi is bezet, probeer opnieuw")
            self._close_prompt(fap, handle, CloseReason.REVOKED)

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
            self._close_prompt(fap, handle, reason)
        # De preset-sessie (en elke andere AgentSession) hoort de uitkomst via de runner
        # (`subscribe_resolutions`), niet via de bridge.
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


def _closed(seq: int, *, handle: int, reason: CloseReason) -> ConfirmClosed:
    return ConfirmClosed(seq=seq, handle=handle, reason=reason)
