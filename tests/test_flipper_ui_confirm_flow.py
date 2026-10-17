"""Bridge: bevestigen, opnieuw groeten en levenscyclus (regressies uit de review van fase 4).

Zelfde opzet als `test_flipper_ui_bridge.py` (echte `FlipperDevice`, nagebootste FAP, echte
runner/policy/PendingStore/audit). `FakeFap` doet wat `pitoolbox_ui.c` doet bij ERROR
`NOT_READY`: opnieuw HELLO met dezelfde instance (docs/fase4-protocol.md §5).
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport, wait_until
from llm_fakes import ScriptedProvider, reply, tool_call, use
from test_flipper_ui_bridge import FAP_PATH, FAST_BRIDGE, IR_POWER, Lab

from pitoolbox.agent.session import AgentSession, ConfirmStatus
from pitoolbox.core.devices.flipper import AppEvent, FlipperDevice
from pitoolbox.core.tools.runner import CONFIRM_ON_FLIPPER, CONFIRM_TOOL
from pitoolbox.frontends.flipper_ui import bridge_confirm
from pitoolbox.frontends.flipper_ui import protocol as proto
from pitoolbox.frontends.flipper_ui.bridge import FRONTEND, BridgeState

IR_VOL = {"device": "tv", "button": "Vol_up"}


def _lab(tmp_path: Path, *, rehello: bool = True, **kwargs: Any) -> Lab:
    lab = Lab(tmp_path, **kwargs)
    if not rehello:
        lab.fap = lab.fake.attach_fap(FakeFap(rehello=False))
    return lab


def _physical_events(lab: Lab) -> list[bool]:
    return [r["attached"] for r in lab.audit() if r.get("event") == "physical_confirmer"]


def _prompts(lab: Lab) -> list[proto.ConfirmPrompt]:
    return lab.fap.of_type(proto.ConfirmPrompt)


# --- #1: een nieuwe vraag pas na een afgeleverde CONFIRM_CLOSED ---------------------------------


def test_undelivered_closed_detaches_and_app_greets_again(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    try:
        lab.connect()
        lab.fap.wait_for(proto.Status)
        time.sleep(0.2)  # niets meer onderweg
        first = lab.runner.call("mcp", "ir_send", IR_POWER)
        lab.runner.call("mcp", "ir_send", IR_VOL)
        p1 = lab.prompt()
        time.sleep(0.2)
        lab.fap.reject_next = 4  # FAP-inbox vol: CLOSED(h1) wordt 4x geweigerd
        assert lab.store.discard(first["pending_id"], "mcp") is not None  # nee in de chat
        # Wat er op het scherm staat, weet de Pi niet meer: eerst de lease los ...
        assert wait_until(lambda: lab.store.physical_confirmer is None)
        # ... en de tweede vraag ging NIET weg terwijl h1 misschien nog op het scherm stond.
        assert [p.handle for p in _prompts(lab)] == [p1.handle]
        # De app draait nog: de bridge vraagt hem opnieuw te groeten (geen nieuwe start).
        p2 = lab.prompt(lambda m: m.handle != p1.handle)
        assert "Vol_up" in p2.summary
        assert lab.fap.rehellos >= 1 and lab.fap.starts == 1
        assert lab.store.physical_confirmer == FRONTEND
        nudges = [e for e in lab.fap.of_type(proto.Error) if e.code is proto.ErrorCode.NOT_READY]
        assert nudges and nudges[0].ref == 0
        # De oude handle hoort bij de vorige sessie: weigeren, niets verbruiken.
        seq = lab.fap.answer(p1.handle, approve=True)
        assert lab.error(seq).code is proto.ErrorCode.CONFIRM_INVALID
        lab.fap.answer(p2.handle, approve=False)
        assert lab.closed(p2.handle).reason is proto.CloseReason.REJECTED
        assert lab.fake.sent_buttons == []
        assert _physical_events(lab) == [True, False, True]
    finally:
        lab.bridge.stop()


def test_undelivered_prompt_detaches(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    try:
        lab.connect()
        lab.fap.wait_for(proto.Status)
        time.sleep(0.2)
        lab.fap.reject_next = 4  # de vraag zelf komt niet aan
        lab.runner.call("mcp", "ir_send", IR_POWER)
        assert wait_until(lambda: lab.store.physical_confirmer is None)
        prompt = lab.prompt()  # na opnieuw groeten alsnog, met een verse handle
        assert lab.store.physical_confirmer == FRONTEND
        lab.fap.answer(prompt.handle, approve=False)
        assert lab.closed(prompt.handle).reason is proto.CloseReason.REJECTED
    finally:
        lab.bridge.stop()


def test_app_that_does_not_greet_again_is_closed(tmp_path: Path) -> None:
    """Een FAP zonder opnieuw-groeten (oude build): na de HELLO-time-out sluit de bridge zijn
    eigen app; nooit een app van de gebruiker."""
    lab = _lab(tmp_path, rehello=False)
    try:
        lab.connect()
        lab.fap.wait_for(proto.Status)
        time.sleep(0.2)
        lab.fap.reject_next = 4
        lab.runner.call("mcp", "ir_send", IR_POWER)
        assert wait_until(lambda: lab.store.physical_confirmer is None)
        assert wait_until(lambda: not lab.fake.fap_running, timeout=4.0)
        assert lab.bridge.state is BridgeState.IDLE
        assert lab.fap.starts == 1
    finally:
        lab.bridge.stop()


def test_fake_fap_greets_again_like_the_real_app() -> None:
    """Bewaakt de nabootsing waar de tests hierboven op leunen (`ptb_on_not_ready` in
    pitoolbox_ui.c): genegeerd tijdens de handdruk, dan zelfde instance, hoogstens één HELLO
    per `hello_retry_s`."""
    fake = FakeFlipperTransport(start_in_rpc=True, kind="ble")
    fap = fake.attach_fap(FakeFap(hello_retry_s=0.3))
    device = FlipperDevice(lambda: fake, timing=FAST_TIMING)
    hellos: list[tuple[float, int]] = []

    def sink(event: AppEvent) -> None:
        msg = proto.decode_from_fap(event.data) if event.kind == "data" else None
        if isinstance(msg, proto.Hello):
            hellos.append((time.monotonic(), msg.instance))

    def pi_says(msg: proto.PiMessage) -> None:
        device.app_send(proto.encode(msg))

    def not_ready() -> proto.Error:
        return proto.Error(seq=1, ref=0, code=proto.ErrorCode.NOT_READY, text="x")

    device.set_app_sink(sink)
    try:
        device.start_rpc_app(FAP_PATH)
        pi_says(not_ready())  # handdruk loopt nog: genegeerd
        pi_says(
            proto.Welcome(
                seq=2,
                proto=proto.PROTO_VERSION,
                instance=fap.instance,
                confirm_on_flipper=True,
                agent_available=False,
            )
        )
        assert fap.connected
        pi_says(not_ready())
        pi_says(not_ready())  # de nieuwe handdruk loopt al: genegeerd
        assert wait_until(lambda: device.poll_app_events(0.02) and len(hellos) >= 2)
        time.sleep(0.4)
        device.poll_app_events(0.02)
        assert len(hellos) == 2 and fap.rehellos == 1
        (first_at, first), (again_at, again) = hellos
        assert first == again == fap.instance
        assert again_at - first_at >= 0.25  # gemeten bij het lezen; zonder grens: 0,01 s
    finally:
        device.close()


# --- #7: OK op een net verlopen handle ---------------------------------------------------------


def test_answer_on_just_expired_handle_gets_closed_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_confirm, "MAX_TTL_S", 2)  # handle korter dan de pending
    # Timer voor handles praktisch uit: het antwoord komt vóór de volgende controle.
    lab = _lab(tmp_path, ttl_s=10.0, timing=replace(FAST_BRIDGE, expire_s=30.0))
    try:
        lab.connect()
        lab.runner.call("mcp", "ir_send", IR_POWER)
        prompt = lab.prompt()
        assert prompt.ttl_s == 1  # aftellen <= de 2 s van de handle
        time.sleep(2.1)  # handle verlopen volgens de Pi, de timer nog niet langs
        seq = lab.fap.answer(prompt.handle, approve=True)
        assert lab.error(seq).code is proto.ErrorCode.CONFIRM_INVALID
        assert lab.closed(prompt.handle).reason is proto.CloseReason.EXPIRED
        assert lab.fake.sent_buttons == []
        # De pending leeft nog: dezelfde vraag komt terug, met een nieuwe handle.
        again = lab.prompt(lambda m: m.handle != prompt.handle)
        assert again.summary == prompt.summary
    finally:
        lab.bridge.stop()


def test_prompt_ttl_is_computed_when_sent(tmp_path: Path) -> None:
    """De vraag wacht in de zendrij (PRESETS gaat voor): het aftellen mag niet langer zijn dan
    wat de Pi bij aankomst nog over heeft."""
    lab = _lab(tmp_path, ttl_s=3.3, timing=replace(FAST_BRIDGE, send_gap_s=0.4))
    try:
        lab.connect()
        raw = lab.runner.call("mcp", "ir_send", IR_POWER)
        prompt = lab.prompt()
        action = lab.store.get(raw["pending_id"])
        assert action is not None
        assert prompt.ttl_s <= lab.store.remaining_s(action)
    finally:
        lab.bridge.stop()


def test_build_prompt_uses_clock_at_send_time(tmp_path: Path) -> None:
    now = [100.0]
    lab = Lab(tmp_path)
    lab.bridge._clock = lambda: now[0]
    prompt = lab.bridge._build_prompt(7, handle=5, deadline=103.7, summary="x")
    assert prompt is not None and prompt.ttl_s == 3 and prompt.seq == 7
    now[0] = 103.2
    assert lab.bridge._build_prompt(8, handle=5, deadline=103.7, summary="x") is None
    now[0] = 0.0
    capped = lab.bridge._build_prompt(9, handle=5, deadline=500.0, summary="x")
    assert capped is not None and capped.ttl_s == proto.MAX_TTL_S


# --- #8: geen gestrande pendings ---------------------------------------------------------------


def test_pending_longer_than_handle_ttl_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_confirm, "MAX_TTL_S", 2)  # zoals PITOOLBOX_CONFIRM_TTL_S > 120
    lab = _lab(tmp_path, ttl_s=10.0)
    try:
        lab.connect()
        raw = lab.runner.call("mcp", "ir_send", IR_POWER)
        first = lab.prompt()
        assert lab.closed(first.handle).reason is proto.CloseReason.EXPIRED
        again = lab.prompt(lambda m: m.handle != first.handle)
        assert again.summary == first.summary
        refused = lab.runner.call("mcp", CONFIRM_TOOL, {"pending_id": raw["pending_id"]})
        assert refused["error"] == CONFIRM_ON_FLIPPER
        lab.fap.answer(again.handle, approve=False)
        assert lab.closed(again.handle).reason is proto.CloseReason.REJECTED
        assert lab.store.list() == []
    finally:
        lab.bridge.stop()


def test_more_than_eight_pendings_are_all_asked(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    try:
        lab.connect()
        ids = [lab.runner.call("mcp", "ir_send", IR_POWER)["pending_id"] for _ in range(10)]
        handled: list[int] = []
        for _ in ids:
            prompt = lab.prompt(lambda m: m.handle not in handled)
            handled.append(prompt.handle)
            time.sleep(0.25)  # een echte FAP stuurt hoogstens 5 berichten/s (§4)
            lab.fap.answer(prompt.handle, approve=False)
            assert lab.closed(prompt.handle).reason is proto.CloseReason.REJECTED
        assert lab.store.list() == []
        assert len(handled) == 10
    finally:
        lab.bridge.stop()


def test_busy_confirm_lane_asks_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = _lab(tmp_path)
    try:
        lab.connect()
        lab.runner.call("mcp", "ir_send", IR_POWER)
        first = lab.prompt()
        lane = lab.bridge._lanes["confirm"]
        real_submit = lane.submit
        refused = [0]

        def full_once(fn: Any) -> bool:
            if not refused[0]:
                refused[0] = 1
                return False
            return real_submit(fn)

        monkeypatch.setattr(lane, "submit", full_once)
        seq = lab.fap.answer(first.handle, approve=False)
        assert lab.error(seq).code is proto.ErrorCode.BUSY
        assert lab.closed(first.handle).reason is proto.CloseReason.REVOKED
        again = lab.prompt(lambda m: m.handle != first.handle)
        assert again.summary == first.summary
        lab.fap.answer(again.handle, approve=False)
        assert lab.closed(again.handle).reason is proto.CloseReason.REJECTED
        assert lab.store.list() == []
    finally:
        lab.bridge.stop()


# --- #3: pitoolbox-agent hoort de uitkomst van een OK op de Flipper -----------------------------


def test_agent_cli_session_hears_flipper_outcome(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    try:
        lab.connect()
        provider = ScriptedProvider(
            [use(tool_call("ir_send", IR_POWER)), reply("Bevestig op de Flipper."), reply("Ok.")]
        )
        session = AgentSession(provider, lab.runner, frontend="agent-cli")
        result = session.run_turn("zet de tv aan")
        assert result.pending
        prompt = lab.prompt()
        assert "(van agent-cli)" in prompt.summary
        lab.fap.answer(prompt.handle, approve=True)
        assert wait_until(lambda: lab.fake.sent_buttons == ["Power"])
        outcomes: list[Any] = []
        assert wait_until(lambda: bool(outcomes.extend(session.take_external()) or outcomes))
        assert [o.status for o in outcomes] == [ConfirmStatus.EXECUTED]
        assert session.open_confirmations == ()
        session.run_turn("en?")
        text = getattr(provider.requests[-1].messages[-1], "text", "")
        assert "bevestigd en het is uitgevoerd" in text
        session.close()
    finally:
        lab.bridge.stop()


# --- #2: noodstop tijdens het starten -----------------------------------------------------------


def test_abort_during_start_does_not_wedge_starting(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    try:
        lab.bridge.start()
        lab.device._lock.acquire()  # een andere Flipper-operatie loopt
        try:
            lab.bridge.request_start()
            assert wait_until(lambda: lab.bridge.state is BridgeState.STARTING)
            time.sleep(0.1)  # de start-job wacht op de lock
            lab.device.abort()  # noodstop
        finally:
            lab.device._lock.release()
        assert wait_until(lambda: lab.bridge.state is BridgeState.IDLE)
        assert lab.fake.app_starts == []
        lab.bridge.request_start()
        assert wait_until(lambda: lab.bridge.state is BridgeState.CONNECTED)
    finally:
        lab.bridge.stop()


def test_starting_has_a_deadline(tmp_path: Path) -> None:
    lab = _lab(tmp_path, timing=replace(FAST_BRIDGE, start_timeout_s=0.3))
    try:
        lab.bridge.start()
        lab.device._lock.acquire()
        try:
            lab.bridge.request_start()
            assert wait_until(lambda: lab.bridge.state is BridgeState.STARTING)
            assert wait_until(lambda: lab.bridge.state is BridgeState.IDLE, timeout=2.0)
        finally:
            lab.device._lock.release()
        # De job loopt daarna alsnog; de HELLO van de app wordt gewoon beantwoord.
        assert wait_until(lambda: lab.bridge.state is BridgeState.CONNECTED)
    finally:
        lab.bridge.stop()


# --- #9: verzoekbestand alleen pollen over een bestaande RPC-sessie -----------------------------


def test_start_request_poll_never_connects(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    try:
        lab.bridge.start()
        time.sleep(0.4)  # vele poll-intervallen
        assert lab.fake.opens == 0 and lab.fake.rpc_log == []
    finally:
        lab.bridge.stop()


def test_start_request_poll_leaves_usb_cli_alone(tmp_path: Path) -> None:
    lab = _lab(tmp_path)
    lab.fake = FakeFlipperTransport(start_in_rpc=False, kind="usb")
    lab.fap = lab.fake.attach_fap(FakeFap())
    try:
        lab.device.connect()  # USB: CLI
        assert lab.device.status()["mode"] == "cli"
        lab.bridge.start()
        time.sleep(0.4)
        assert lab.device.status()["mode"] == "cli"
        assert lab.fake.rpc_log == []
    finally:
        lab.bridge.stop()


# --- #10: stop() wacht niet op een lange tool ----------------------------------------------------


def test_stop_is_bounded_while_device_is_busy(tmp_path: Path) -> None:
    lab = _lab(tmp_path, timing=replace(FAST_BRIDGE, stop_join_s=0.3))
    lab.connect()
    release = threading.Event()

    def long_tool() -> None:
        with lab.device._operation("lange_tool"):
            release.wait(10)

    tool = threading.Thread(target=long_tool, daemon=True)
    tool.start()
    try:
        assert wait_until(lambda: lab.device.status()["operation"] == "lange_tool")
        t0 = time.monotonic()
        lab.bridge.stop()
        assert time.monotonic() - t0 < 2.5
        assert lab.store.physical_confirmer is None
    finally:
        release.set()
        tool.join(2)
    # Na de tool sluit de app alsnog (de exit-thread kreeg de lock).
    assert wait_until(lambda: not lab.fake.fap_running)
