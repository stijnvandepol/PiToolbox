"""Bridge FAP <-> PiToolbox end-to-end, zonder hardware.

Echte `FlipperDevice` op een `FakeFlipperTransport` (BLE-achtig: altijd RPC) met een
nagebootste FAP (`FakeFap`), echte runner/policy/PendingStore/audit en nep-wifi/systeem.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport, wait_until
from llm_fakes import ScriptedProvider, reply, tool_call, use

from pitoolbox.config import Settings
from pitoolbox.core.audit import AuditLog
from pitoolbox.core.devices.flipper import FlipperDevice
from pitoolbox.core.errors import DeviceError
from pitoolbox.core.policy import PendingStore, Policy
from pitoolbox.core.scope import InfraredDevice, Scope, ScopeStore, WifiNetwork
from pitoolbox.core.tools import build_registry
from pitoolbox.core.tools.base import ToolContext
from pitoolbox.core.tools.runner import CONFIRM_ON_FLIPPER, CONFIRM_TOOL, ToolRunner
from pitoolbox.frontends.flipper_ui import protocol as proto
from pitoolbox.frontends.flipper_ui.bridge import (
    FRONTEND,
    START_REQUEST_PATH,
    BridgeEvent,
    BridgeState,
    BridgeTiming,
    FlipperUiBridge,
    start_from_settings,
)
from pitoolbox.frontends.flipper_ui.presets import PresetConfig

FAP_PATH = "/ext/apps/Tools/pitoolbox_ui.fap"
IR_POWER = {"device": "tv", "button": "Power"}
Q = proto.QuickAction

FAST_BRIDGE = BridgeTiming(
    tick_s=0.01,
    poll_slice_s=0.02,
    poll_idle_s=0.005,
    send_gap_s=0.0,
    send_timeout_s=0.5,
    send_retry_s=0.01,
    progress_gap_s=0.05,
    hello_timeout_s=1.0,
    expire_s=0.05,
    start_poll_s=0.05,
    manual_lock_wait_s=0.3,
    restart_backoff_s=(0.05, 0.1),
    restart_window_s=3.0,
    locked_retry_s=1.0,
    link_backoff_s=(0.05,),
    stop_join_s=2.0,
)
DEVICE_TIMING = replace(FAST_TIMING, loader_free_wait_s=0.3, loader_poll_s=0.02)

SCOPE = Scope(
    wifi=[WifiNetwork(name="lab-ap", ssid="MijnLab", bssid="AA:BB:CC:DD:EE:01", channel=6)],
    infrared=[InfraredDevice(name="tv", file="/ext/infrared/tv.ir", buttons=["Power", "Vol_up"])],
)


class Lab:
    def __init__(
        self,
        tmp_path: Path,
        *,
        provider: ScriptedProvider | None = None,
        presets: Sequence[PresetConfig] = (),
        ttl_s: float = 60.0,
        timing: BridgeTiming = FAST_BRIDGE,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.fake = FakeFlipperTransport(start_in_rpc=True, kind="ble", files=files)
        self.fap = self.fake.attach_fap(FakeFap())
        self.device = FlipperDevice(lambda: self.fake, timing=DEVICE_TIMING)
        self.audit_path = tmp_path / "audit.jsonl"
        settings = Settings(
            scope_file=tmp_path / "scope.yaml",
            audit_log=self.audit_path,
            captures_dir=tmp_path / "captures",
        )
        scope_store = ScopeStore.fixed(SCOPE)
        self.wifi = MagicMock()
        self.wifi.iface = "wlan1"
        self.wifi.get_mode.return_value = "managed"
        self.wifi.scan.return_value = {
            "method": "iw",
            "band": "abg",
            "aps": [
                {"bssid": "AA:BB:CC:DD:EE:01", "ssid": "MijnLab", "signal_dbm": -40},
                {"bssid": "11:22:33:44:55:66", "ssid": "Buren", "signal_dbm": -70},
            ],
        }
        self.captures = MagicMock()
        self.captures.list_active.return_value = []
        self.captures.stop_all.return_value = []
        self.system = MagicMock()
        self.system.throttled.return_value = {
            "available": True,
            "current": [],
            "occurred": ["undervoltage"],
            "undervoltage_now": False,
        }
        self.system.uplink.return_value = {
            "iface": "wlan0",
            "operstate": "UP",
            "ipv4": ["10.0.0.2/24"],
            "is_default_route": True,
        }
        self.system.disk.return_value = {"percent": 10.0}
        self.store = PendingStore(ttl_s=ttl_s)
        ctx = ToolContext(
            settings=settings,
            scope_store=scope_store,
            flipper=self.device,
            wifi=self.wifi,
            captures=self.captures,
            system=self.system,
            pending=self.store,
        )
        self.runner = ToolRunner(
            build_registry(), ctx, Policy(scope_store), AuditLog(self.audit_path)
        )
        self.provider = provider
        self.bridge = FlipperUiBridge(
            self.runner,
            app_path=FAP_PATH,
            presets=presets,
            provider=provider,
            timing=timing,
        )

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]

    def connect(self) -> None:
        self.bridge.start()
        self.bridge.request_start()
        assert wait_until(lambda: self.bridge.state is BridgeState.CONNECTED), self.bridge.state
        assert wait_until(lambda: self.store.physical_confirmer == FRONTEND)

    def wait_welcomed(self, starts: int) -> None:
        """Wacht tot de app `starts` keer gestart is en de laatste instance verbonden is."""
        assert wait_until(lambda: self.fap.starts == starts)
        self.fap.wait_for(proto.Welcome, lambda m: m.instance == self.fap.instance)
        assert wait_until(lambda: self.bridge.state is BridgeState.CONNECTED)

    def prompt(self, predicate: Any = None) -> proto.ConfirmPrompt:
        msg: proto.ConfirmPrompt = self.fap.wait_for(proto.ConfirmPrompt, predicate)
        return msg

    def closed(self, handle: int) -> proto.ConfirmClosed:
        msg: proto.ConfirmClosed = self.fap.wait_for(
            proto.ConfirmClosed, lambda m: m.handle == handle
        )
        return msg

    def error(self, ref: int) -> proto.Error:
        msg: proto.Error = self.fap.wait_for(proto.Error, lambda m: m.ref == ref)
        return msg


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[Lab]:
    instance = Lab(tmp_path)
    yield instance
    instance.bridge.stop()


def _make(tmp_path: Path, **kwargs: Any) -> Lab:
    return Lab(tmp_path, **kwargs)


# --- handdruk -------------------------------------------------------------------------------


def test_handshake_sends_welcome_presets_status_and_attaches_confirmer(tmp_path: Path) -> None:
    presets = [PresetConfig(id=3, label="Wifi kort", prompt="geheime prompt")]
    lab = _make(tmp_path, presets=presets)
    try:
        lab.connect()
        welcome = lab.fap.wait_for(proto.Welcome)
        assert welcome.instance == lab.fap.instance
        assert welcome.proto == proto.PROTO_VERSION
        assert welcome.confirm_on_flipper is True
        assert welcome.agent_available is False  # geen provider
        sent = lab.fap.wait_for(proto.Presets)
        assert [(p.id, p.label) for p in sent.items] == [(3, "Wifi kort")]
        status = lab.fap.wait_for(proto.Status)
        assert status.uplink_up is True and status.uplink == "wlan0 10.0.0.2/24"
        assert status.undervoltage_occurred is True and status.undervoltage_now is False
        assert status.link is proto.Link.BLE
        assert status.pending == 0
        # Volgorde: eerst WELCOME, dan de rest.
        assert isinstance(lab.fap.received[0], proto.Welcome)
        assert lab.fake.app_starts == [FAP_PATH]
        events = [r for r in lab.audit() if r.get("event") == "physical_confirmer"]
        assert events[-1]["attached"] is True and events[-1]["frontend"] == FRONTEND
        # De prompttekst van de preset gaat nooit naar de FAP.
        assert all("geheime" not in repr(m) for m in lab.fap.received)
    finally:
        lab.bridge.stop()
    assert lab.store.physical_confirmer is None
    assert not lab.fake.fap_running  # bij stoppen sluit de bridge zijn app


def test_observer_sees_start_handshake_deliveries_and_detach(tmp_path: Path) -> None:
    lab = _make(tmp_path)
    seen: list[BridgeEvent] = []
    lab.bridge = FlipperUiBridge(
        lab.runner, app_path=FAP_PATH, timing=FAST_BRIDGE, observer=seen.append
    )
    try:
        lab.connect()
        assert wait_until(lambda: any(e.detail == "Status" for e in seen))
        kinds = [e.kind for e in seen]
        assert {"started", "hello", "connected", "delivered"} <= set(kinds)
        assert kinds.index("hello") < kinds.index("connected")
        delivered = [e.detail for e in seen if e.kind == "delivered"]
        assert delivered[0] == "Welcome" and "Presets" in delivered
        hello = next(e for e in seen if e.kind == "hello")
        assert hello.instance == lab.fap.instance and hello.detail == "v1.0"
        # send_status: een verse STATUS (via de status-tool) wordt afgeleverd.
        before = delivered.count("Status")
        lab.bridge.send_status()
        assert wait_until(
            lambda: [e.detail for e in seen if e.kind == "delivered"].count("Status") > before
        )
        lab.fake.fap_exits()
        assert wait_until(lambda: any(e.kind == "detached" for e in seen))
    finally:
        lab.bridge.stop()


def test_lease_is_attached_before_the_bridge_reports_connected(tmp_path: Path) -> None:
    lab = _make(tmp_path)
    lease_at_connected: list[str | None] = []

    def observe(event: BridgeEvent) -> None:
        if event.kind == "connected":
            lease_at_connected.append(lab.store.physical_confirmer)

    lab.bridge = FlipperUiBridge(
        lab.runner, app_path=FAP_PATH, timing=FAST_BRIDGE, observer=observe
    )
    try:
        lab.connect()
        assert lease_at_connected == [FRONTEND]
    finally:
        lab.bridge.stop()


def test_no_session_without_the_lease(lab: Lab) -> None:
    # Al een fysieke bevestiger (kan alleen bij een tweede bridge in hetzelfde proces): dan geen
    # CONNECTED, want vragen tonen zonder "Flipper verplicht" zou de mens misleiden.
    other = lab.runner.attach_physical_confirmer("andere-bridge")
    try:
        lab.bridge.start()
        lab.bridge.request_start()
        assert wait_until(lambda: lab.fake.app_starts == [FAP_PATH])
        assert wait_until(lambda: not lab.fake.fap_running)  # eigen app weer gesloten
        assert lab.bridge.state is BridgeState.IDLE
        assert lab.store.physical_confirmer == "andere-bridge"
        assert not lab.fap.of_type(proto.Presets)
    finally:
        lab.runner.detach_physical_confirmer(other, "test")


def test_observer_reports_locked_loader(tmp_path: Path) -> None:
    lab = _make(tmp_path)
    seen: list[BridgeEvent] = []
    lab.bridge = FlipperUiBridge(
        lab.runner, app_path=FAP_PATH, timing=FAST_BRIDGE, observer=seen.append
    )
    lab.fake.user_app_open = True
    try:
        lab.bridge.start()
        lab.bridge.request_start()
        assert wait_until(lambda: any(e.kind == "start_failed" for e in seen))
        assert next(e for e in seen if e.kind == "start_failed").detail == "locked"
    finally:
        lab.bridge.stop()


def test_repeated_hello_same_instance_gets_welcome_without_reset(lab: Lab) -> None:
    lab.connect()
    lab.fap.wait_for(proto.Status)
    lab.fap.send(lab.fap.hello())
    assert wait_until(lambda: len(lab.fap.of_type(proto.Welcome)) == 2)
    assert lab.bridge.state is BridgeState.CONNECTED
    assert lab.store.physical_confirmer == FRONTEND


def test_message_before_hello_is_not_ready(tmp_path: Path) -> None:
    lab = _make(tmp_path)
    lab.fap.auto_hello = False
    try:
        lab.bridge.start()
        lab.bridge.request_start()
        assert wait_until(lambda: lab.fake.fap_running)
        seq = lab.fap.next_seq()
        lab.fap.send(proto.StatusRequest(seq=seq))
        assert lab.error(seq).code is proto.ErrorCode.NOT_READY
        assert lab.store.physical_confirmer is None
    finally:
        lab.bridge.stop()


def test_unsupported_version_and_garbage_get_errors(lab: Lab) -> None:
    lab.connect()
    raw = bytearray(proto.encode(proto.StatusRequest(seq=40)))
    raw[1] = 9  # onbekende versie
    lab.fap.send_raw(bytes(raw))
    assert lab.error(40).code is proto.ErrorCode.UNSUPPORTED_VERSION
    lab.fap.send_raw(bytes([proto.MAGIC, 1, 0x04, 41, 0, 9, 9]))  # ACTION met kind 9
    assert lab.error(41).code is proto.ErrorCode.INVALID_MESSAGE


def test_hello_timeout_closes_silent_app(tmp_path: Path) -> None:
    lab = _make(tmp_path)
    lab.fap.auto_hello = False
    try:
        lab.bridge.start()
        lab.bridge.request_start()
        assert wait_until(lambda: lab.fake.fap_running)
        assert wait_until(lambda: not lab.fake.fap_running, timeout=3.0)
        assert lab.bridge.state is BridgeState.IDLE
        assert lab.store.physical_confirmer is None
        time.sleep(0.3)  # langer dan de herstart-backoff: een stille app wordt niet herstart
        assert lab.fap.starts == 1
    finally:
        lab.bridge.stop()


# --- snelle acties ----------------------------------------------------------------------------


def test_quick_status_goes_through_runner(lab: Lab) -> None:
    lab.connect()
    ref = lab.fap.quick(Q.STATUS)
    text = lab.fap.result_text(ref)
    assert "Uplink: wlan0 10.0.0.2/24 (up)" in text
    assert "Voeding: eerder onderspanning" in text
    assert "Open bevestigingen: 0" in text
    calls = [r for r in lab.audit() if r.get("tool") == "status"]
    assert calls and all(r["frontend"] == FRONTEND for r in calls)


def test_quick_wifi_scan_summary(lab: Lab) -> None:
    lab.connect()
    ref = lab.fap.quick(Q.WIFI_SCAN)
    text = lab.fap.result_text(ref)
    assert text.startswith("2 netwerken, 1 in scope")
    assert "MijnLab -40dBm [scope]" in text
    lab.wifi.scan.assert_called_once_with("iw", 10, "abg")


def test_quick_stop_captures_uses_ids_from_status(lab: Lab) -> None:
    lab.captures.list_active.return_value = [{"capture_id": "a1b2c3"}, {"capture_id": "d4e5f6"}]
    lab.captures.stop.return_value = {"stopped": True}
    lab.connect()
    ref = lab.fap.quick(Q.STOP_CAPTURES)
    assert lab.fap.result_text(ref) == "2 van 2 capture(s) gestopt."
    assert [c.args[0] for c in lab.captures.stop.call_args_list] == ["a1b2c3", "d4e5f6"]


def test_unknown_quick_action(lab: Lab) -> None:
    lab.connect()
    ref = lab.fap.action(proto.ActionKind.QUICK, 99)
    assert lab.error(ref).code is proto.ErrorCode.UNKNOWN_ACTION


def test_busy_and_emergency_stop_always_allowed(lab: Lab) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_scan(*args: Any) -> dict[str, Any]:
        started.set()
        release.wait(5)
        return {"method": "iw", "band": "abg", "aps": []}

    lab.wifi.scan.side_effect = slow_scan
    lab.connect()
    scan_ref = lab.fap.quick(Q.WIFI_SCAN)
    assert started.wait(2)
    busy_ref = lab.fap.quick(Q.STATUS)
    assert lab.error(busy_ref).code is proto.ErrorCode.BUSY
    stop_ref = lab.fap.quick(Q.EMERGENCY_STOP)
    text = lab.fap.result_text(stop_ref)
    assert text.startswith("Noodstop uitgevoerd.")
    lab.captures.stop_all.assert_called_once()
    release.set()
    time.sleep(0.2)
    # Het afgebroken antwoord van de scan komt niet meer.
    assert not [m for m in lab.fap.of_type(proto.ResultChunk) if m.ref == scan_ref]


def test_cancel_running_action(lab: Lab) -> None:
    release = threading.Event()
    lab.wifi.scan.side_effect = lambda *a: (release.wait(5), {"aps": []})[1]
    lab.connect()
    ref = lab.fap.quick(Q.WIFI_SCAN)
    time.sleep(0.05)
    lab.fap.send(proto.Cancel(seq=lab.fap.next_seq(), ref=ref))
    assert lab.error(ref).code is proto.ErrorCode.CANCELLED
    release.set()
    # Na het afbreken is de bridge weer vrij.
    ref2 = lab.fap.quick(Q.STATUS)
    assert "Uplink" in lab.fap.result_text(ref2)


# --- presets ----------------------------------------------------------------------------------


def test_preset_without_llm_is_agent_unavailable(tmp_path: Path) -> None:
    lab = _make(tmp_path, presets=[PresetConfig(id=1, label="Kort", prompt="hoi")])
    try:
        lab.connect()
        ref = lab.fap.action(proto.ActionKind.PRESET, 1)
        assert lab.error(ref).code is proto.ErrorCode.AGENT_UNAVAILABLE
        ref = lab.fap.action(proto.ActionKind.PRESET, 2)
        assert lab.error(ref).code is proto.ErrorCode.UNKNOWN_ACTION
    finally:
        lab.bridge.stop()


def test_preset_runs_agent_turn_with_progress(tmp_path: Path) -> None:
    def slow_reply(_messages: Sequence[Any]) -> Any:
        time.sleep(0.3)  # het model "denkt": tijd genoeg om PROGRESS te versturen
        return reply("Alles in orde, uplink is up.")

    provider = ScriptedProvider([use(tool_call("status")), slow_reply])
    presets = [PresetConfig(id=1, label="Status", prompt="Hoe staat het ervoor?")]
    lab = _make(tmp_path, provider=provider, presets=presets)
    try:
        lab.connect()
        assert lab.fap.wait_for(proto.Welcome).agent_available is True
        ref = lab.fap.action(proto.ActionKind.PRESET, 1)
        assert lab.fap.result_text(ref) == "Alles in orde, uplink is up."
        progress = lab.fap.of_type(proto.Progress)
        assert [p.text for p in progress if p.ref == ref] == ["tool: status"]
        first = provider.requests[0].messages[0]
        assert getattr(first, "text", "") == "Hoe staat het ervoor?"
        turns = [r for r in lab.audit() if r.get("event") == "agent_turn_end"]
        assert turns and turns[-1]["frontend"] == FRONTEND
    finally:
        lab.bridge.stop()


def test_preset_transmit_is_confirmed_on_flipper_and_agent_hears_outcome(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            use(tool_call("ir_send", IR_POWER)),
            reply("Ik heb gevraagd om te bevestigen."),
            reply("Gedaan."),
        ]
    )
    presets = [PresetConfig(id=1, label="TV aan", prompt="Zet de tv aan")]
    lab = _make(tmp_path, provider=provider, presets=presets)
    try:
        lab.connect()
        ref = lab.fap.action(proto.ActionKind.PRESET, 1)
        assert "Bevestig op de Flipper." in lab.fap.result_text(ref)
        prompt = lab.prompt()
        assert "IR 'Power' naar tv" in prompt.summary and "(van flipper-ui)" in prompt.summary
        lab.fap.answer(prompt.handle, approve=True)
        # Handoff: de app gaat dicht voor de Infrared-app en komt terug met een nieuwe instance.
        assert wait_until(lambda: lab.fake.sent_buttons == ["Power"])
        lab.wait_welcomed(2)
        status = lab.fap.wait_for(
            proto.Status,
            lambda m: m.last_tool == "ir_send" and m.last_result is proto.LastResult.OK,
        )
        assert status.last_age_s is not None
        # Volgende beurt: het model hoort de uitkomst.
        ref2 = lab.fap.action(proto.ActionKind.PRESET, 1)
        assert lab.fap.result_text(ref2) == "Gedaan."
        note = provider.requests[-1].messages[-1]
        assert "bevestigd en het is uitgevoerd" in getattr(note, "text", "")
    finally:
        lab.bridge.stop()


# --- bevestigen ("Flipper verplicht") -----------------------------------------------------------


def test_chat_confirm_refused_while_connected_ok_on_flipper_executes_once(lab: Lab) -> None:
    lab.connect()
    raw = lab.runner.call("mcp", "ir_send", IR_POWER)
    assert raw["error"] == "confirmation_required"
    pending_id = raw["pending_id"]
    prompt = lab.prompt()
    assert prompt.ttl_s <= 60 and "(van mcp)" in prompt.summary
    assert pending_id not in repr(lab.fap.received)  # de pending_id gaat nooit naar de FAP

    refused = lab.runner.call("mcp", CONFIRM_TOOL, {"pending_id": pending_id})
    assert refused["error"] == CONFIRM_ON_FLIPPER
    assert [p.id for p in lab.store.list()] == [pending_id]  # niet verbruikt

    lab.fap.answer(prompt.handle, approve=True)
    assert wait_until(lambda: lab.fake.sent_buttons == ["Power"])
    assert wait_until(lambda: lab.store.list() == [])
    lab.wait_welcomed(2)
    # De auditregel volgt pas als de runner terug is; de nieuwe app kan al eerder groeten.
    assert wait_until(lambda: any(r.get("decision") == "confirmed" for r in lab.audit()))
    records = lab.audit()
    confirmed = [r for r in records if r.get("decision") == "confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0]["frontend"] == FRONTEND
    assert confirmed[0]["confirmed_on"] == "flipper"
    assert confirmed[0]["created_by"] == "mcp"
    deny = [r for r in records if r.get("error") == CONFIRM_ON_FLIPPER]
    assert len(deny) == 1 and deny[0]["frontend"] == "mcp"
    # Dezelfde handle nog eens (replay): ongeldig, en er wordt niets meer verzonden.
    seq = lab.fap.answer(prompt.handle, approve=True)
    assert lab.error(seq).code is proto.ErrorCode.CONFIRM_INVALID
    time.sleep(0.1)
    assert lab.fake.sent_buttons == ["Power"]


def test_back_rejects_pending_and_replay_is_invalid(lab: Lab) -> None:
    lab.connect()
    raw = lab.runner.call("mcp-http:abc", "ir_send", IR_POWER)
    prompt = lab.prompt()
    assert "(van mcp-http)" in prompt.summary
    lab.fap.answer(prompt.handle, approve=False)
    assert lab.closed(prompt.handle).reason is proto.CloseReason.REJECTED
    assert lab.store.list() == []
    assert lab.fake.sent_buttons == []
    rejected = [r for r in lab.audit() if r.get("decision") == "rejected"]
    assert rejected[0]["pending_id"] == raw["pending_id"]
    assert rejected[0]["created_by"] == "mcp-http:abc"
    seq = lab.fap.answer(prompt.handle, approve=True)
    assert lab.error(seq).code is proto.ErrorCode.CONFIRM_INVALID
    refused = [r for r in lab.audit() if r.get("event") == "flipper_confirm_refused"]
    assert len(refused) == 1


def test_chat_confirm_allowed_again_after_app_closes(lab: Lab) -> None:
    lab.connect()
    raw = lab.runner.call("mcp", "ir_send", IR_POWER)
    prompt = lab.prompt()
    assert lab.runner.call("mcp", CONFIRM_TOOL, {"pending_id": raw["pending_id"]})["error"] == (
        CONFIRM_ON_FLIPPER
    )
    lab.fake.fap_exits()  # de gebruiker sluit de app
    assert wait_until(lambda: lab.store.physical_confirmer is None)
    # Eerst gaat de lease los, daarna pas de toestand naar IDLE.
    assert wait_until(lambda: lab.bridge.state is BridgeState.IDLE)
    # Het OK dat nog "onderweg" was, telt niet meer: de app is weg.
    ok = lab.runner.call("mcp", CONFIRM_TOOL, {"pending_id": raw["pending_id"]})
    assert ok["ok"] is True, ok
    assert lab.fake.sent_buttons == ["Power"]
    detached = [r for r in lab.audit() if r.get("event") == "physical_confirmer"]
    assert [r["attached"] for r in detached] == [True, False]
    assert prompt.handle  # gebruikt


def test_pending_from_other_frontend_autostarts_app(lab: Lab) -> None:
    lab.bridge.start()
    assert lab.bridge.state is BridgeState.IDLE
    lab.runner.call("agent-cli", "ir_send", IR_POWER)
    assert wait_until(lambda: lab.bridge.state is BridgeState.CONNECTED)
    prompt = lab.prompt()
    assert "(van agent-cli)" in prompt.summary


def test_pending_from_before_connect_is_shown_on_connect(lab: Lab) -> None:
    lab.fake.user_app_open = True  # autostart lukt niet: loader bezet
    lab.bridge.start()
    raw = lab.runner.call("mcp", "ir_send", IR_POWER)
    assert wait_until(
        lambda: (
            FAP_PATH in lab.fake.app_starts
            or lab.fake.rpc_log.count("app_lock_status_request") >= 3
        )
    )
    assert wait_until(lambda: lab.bridge.state is BridgeState.IDLE)
    assert not lab.fake.fap_running and lab.fake.app_starts == []  # nooit een app weggedrukt
    lab.fake.user_app_open = False
    lab.bridge.request_start()
    assert wait_until(lambda: lab.bridge.state is BridgeState.CONNECTED)
    prompt = lab.prompt()
    assert "(van mcp)" in prompt.summary
    lab.fap.answer(prompt.handle, approve=False)
    assert lab.closed(prompt.handle).reason is proto.CloseReason.REJECTED
    assert lab.store.get(raw["pending_id"]) is None


def test_one_prompt_at_a_time(lab: Lab) -> None:
    lab.connect()
    lab.runner.call("mcp", "ir_send", IR_POWER)
    lab.runner.call("mcp", "ir_send", {"device": "tv", "button": "Vol_up"})
    first = lab.prompt()
    time.sleep(0.1)
    assert len(lab.fap.of_type(proto.ConfirmPrompt)) == 1
    lab.fap.answer(first.handle, approve=False)
    second = lab.prompt(lambda m: m.handle != first.handle)
    assert "Vol_up" in second.summary


def test_expired_handle_is_closed_and_late_answer_refused(tmp_path: Path) -> None:
    lab = _make(tmp_path, ttl_s=1.2)
    try:
        lab.connect()
        lab.runner.call("mcp", "ir_send", IR_POWER)
        prompt = lab.prompt()
        assert prompt.ttl_s == 1
        assert lab.closed(prompt.handle).reason is proto.CloseReason.EXPIRED
        seq = lab.fap.answer(prompt.handle, approve=True)
        assert lab.error(seq).code is proto.ErrorCode.CONFIRM_INVALID
        assert lab.fake.sent_buttons == []
    finally:
        lab.bridge.stop()


def test_handle_from_previous_instance_is_refused(lab: Lab) -> None:
    lab.connect()
    raw = lab.runner.call("mcp", "ir_send", IR_POWER)
    old = lab.prompt()
    # Een nieuwe instance groet (bijv. de app is opnieuw gestart): oude handles vervallen.
    lab.fap.instance = 0x0BADCAFE
    lab.fap.send(lab.fap.hello())
    new = lab.prompt(lambda m: m.handle != old.handle)
    seq = lab.fap.answer(old.handle, approve=True)
    assert lab.error(seq).code is proto.ErrorCode.CONFIRM_INVALID
    assert lab.store.get(raw["pending_id"]) is not None  # niet verbruikt
    lab.fap.answer(new.handle, approve=False)
    assert lab.closed(new.handle).reason is proto.CloseReason.REJECTED


def test_emergency_stop_elsewhere_revokes_prompt(lab: Lab) -> None:
    lab.connect()
    lab.runner.call("mcp", "ir_send", IR_POWER)
    prompt = lab.prompt()
    lab.runner.call("mcp", "emergency_stop", {})
    assert lab.closed(prompt.handle).reason is proto.CloseReason.REVOKED


# --- robuustheid ------------------------------------------------------------------------------


def test_link_loss_mid_message_detaches_and_restarts(lab: Lab) -> None:
    lab.connect()
    first_instance = lab.fap.instance
    lab.fake.fail_on_rpc_kind = "app_data_exchange_request"  # kabel eruit tijdens versturen
    lab.fap.quick(Q.STATUS)
    assert wait_until(lambda: lab.store.physical_confirmer is None)
    # Opnieuw verbinden met backoff: de app start opnieuw en groet met een nieuwe instance.
    # Wachten op die nieuwe instance, niet alleen op CONNECTED: de bridge koppelt de lease
    # los vóór hij de toestand verlaat, dus direct na het loskoppelen staat er nog even de
    # oude CONNECTED.
    assert wait_until(
        lambda: (
            lab.fap.instance != first_instance
            and lab.bridge.state is BridgeState.CONNECTED
            and lab.store.physical_confirmer == FRONTEND
        ),
        timeout=5.0,
    )
    assert lab.fake.opens >= 2


def _lose_link_with_loader_busy(lab: Lab, seen: list[BridgeEvent]) -> None:
    """Link weg terwijl de loader bezet blijft (de oude app sluit nog, of een app van de
    gebruiker): de eerste herstart vindt de loader bezet."""
    lab.connect()
    lab.fake.user_app_open = True
    lab.fake.fail_on_rpc_kind = "app_data_exchange_request"  # kabel eruit tijdens versturen
    lab.fap.quick(Q.STATUS)
    assert wait_until(lambda: _locked_starts(seen) >= 1, timeout=3.0)


def _locked_starts(seen: list[BridgeEvent]) -> int:
    return sum(1 for e in list(seen) if e.kind == "start_failed" and e.detail == "locked")


def test_restart_after_lost_link_waits_for_our_closing_app(tmp_path: Path) -> None:
    """Op de Flipper toont de oude app nog 2 s "Verbinding met Pi verbroken": een snelle
    herstart vindt de loader bezet. Dan even opnieuw proberen, niet meteen opgeven."""
    seen: list[BridgeEvent] = []
    lab = _make(tmp_path)
    lab.bridge = FlipperUiBridge(
        lab.runner, app_path=FAP_PATH, timing=FAST_BRIDGE, observer=seen.append
    )
    try:
        _lose_link_with_loader_busy(lab, seen)
        first = lab.fap.instance
        lab.fake.user_app_open = False  # de oude app is nu dicht
        assert wait_until(
            lambda: (
                lab.fap.instance != first
                and lab.bridge.state is BridgeState.CONNECTED
                and lab.store.physical_confirmer == FRONTEND
            ),
            timeout=5.0,
        )
        assert "app_exit_request" not in lab.fake.rpc_log  # niets gesloten om plaats te maken
    finally:
        lab.bridge.stop()


def test_restart_gives_up_when_the_loader_stays_busy(tmp_path: Path) -> None:
    """Blijft de loader bezet (een app van de gebruiker), dan stopt het opnieuw proberen na
    `locked_retry_s`; die app wordt nooit gesloten."""
    seen: list[BridgeEvent] = []
    lab = _make(tmp_path)
    lab.bridge = FlipperUiBridge(
        lab.runner, app_path=FAP_PATH, timing=FAST_BRIDGE, observer=seen.append
    )
    try:
        _lose_link_with_loader_busy(lab, seen)
        assert wait_until(lambda: _locked_starts(seen) >= 2, timeout=3.0)  # opnieuw geprobeerd
        time.sleep(FAST_BRIDGE.locked_retry_s + 0.5)
        tries = _locked_starts(seen)
        time.sleep(0.5)
        assert _locked_starts(seen) == tries  # opgegeven
        assert lab.bridge.state is BridgeState.IDLE and lab.store.physical_confirmer is None
        assert lab.fake.user_app_open and "app_exit_request" not in lab.fake.rpc_log
        # Opgeven zet de herstart-administratie terug: een volgend verlies telt zijn vensters
        # (restart_window_s, locked_retry_s) vanaf dat nieuwe verlies, niet vanaf het oude.
        assert lab.bridge._restart_step == 0
    finally:
        lab.bridge.stop()


def test_rejected_message_is_retried(lab: Lab) -> None:
    lab.connect()
    lab.fap.wait_for(proto.Status)
    lab.fap.reject_next = 2  # wachtrij van de FAP even vol
    ref = lab.fap.quick(Q.STATUS)
    assert "Uplink" in lab.fap.result_text(ref)
    assert lab.bridge.state is BridgeState.CONNECTED


def test_hung_app_detaches(lab: Lab) -> None:
    lab.connect()
    lab.fap.wait_for(proto.Status)
    lab.fap.hang = True
    lab.fap.quick(Q.STATUS)
    assert wait_until(lambda: lab.store.physical_confirmer is None, timeout=3.0)


def test_manual_start_request_starts_app_and_is_deleted(tmp_path: Path) -> None:
    now = 1_760_000_000
    files = {START_REQUEST_PATH: f"PTBUI 1 {now - 5}\n".encode()}
    lab = _make(tmp_path, files=files)
    lab.fake.rtc_epoch = now
    try:
        lab.device.connect()  # de bridge pollt alleen over een bestaande RPC-sessie
        lab.bridge.start()
        assert wait_until(lambda: lab.bridge.state is BridgeState.CONNECTED)
        assert START_REQUEST_PATH not in lab.fake.files
    finally:
        lab.bridge.stop()


def test_bridge_opens_the_link_itself_for_a_manual_start(tmp_path: Path) -> None:
    # Een vers gestarte service heeft nog geen link; zonder RPC-sessie van de Pi kan de FAP
    # geen verzoek achterlaten. De bridge moet dus zelf verbinden (hardwarevondst 2026-10-17).
    now = 1_760_000_000
    files = {START_REQUEST_PATH: f"PTBUI 1 {now - 5}\n".encode()}
    lab = _make(tmp_path, files=files)
    lab.fake.rtc_epoch = now
    try:
        assert lab.device.status()["connected"] is False
        lab.bridge.start()
        assert wait_until(lambda: lab.bridge.state is BridgeState.CONNECTED)
        assert START_REQUEST_PATH not in lab.fake.files
    finally:
        lab.bridge.stop()


def test_failing_link_backs_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lab = _make(tmp_path, timing=replace(FAST_BRIDGE, link_backoff_s=(0.2, 0.4)))
    attempts: list[float] = []

    def no_flipper() -> float:
        attempts.append(time.monotonic())
        raise DeviceError("geen Flipper")

    monkeypatch.setattr(lab.device, "ping", no_flipper)
    try:
        lab.bridge.start()
        time.sleep(1.0)
    finally:
        lab.bridge.stop()
    # Zonder backoff zou elke start_poll_s (0,05 s) een poging zijn: ~20 in 1 s.
    assert 2 <= len(attempts) <= 4, attempts
    gaps = [b - a for a, b in itertools.pairwise(attempts)]
    assert min(gaps) >= 0.18, gaps


def test_manual_start_request_too_old_is_ignored(tmp_path: Path) -> None:
    now = 1_760_000_000
    files = {START_REQUEST_PATH: f"PTBUI 1 {now - 120}\n".encode()}
    lab = _make(tmp_path, files=files)
    lab.fake.rtc_epoch = now
    try:
        lab.device.connect()
        lab.bridge.start()
        assert wait_until(lambda: START_REQUEST_PATH not in lab.fake.files)
        time.sleep(0.2)
        assert lab.bridge.state is BridgeState.IDLE
        assert lab.fake.app_starts == []
    finally:
        lab.bridge.stop()


def test_manual_start_not_polled_while_user_app_open(tmp_path: Path) -> None:
    now = 1_760_000_000
    files = {START_REQUEST_PATH: f"PTBUI 1 {now}\n".encode()}
    lab = _make(tmp_path, files=files)
    lab.fake.rtc_epoch = now
    lab.fake.user_app_open = True
    try:
        lab.device.connect()
        lab.bridge.start()
        assert wait_until(lambda: "app_lock_status_request" in lab.fake.rpc_log)
        time.sleep(0.2)
        assert START_REQUEST_PATH in lab.fake.files  # niet gelezen, niet gewist
        assert "storage_stat_request" not in lab.fake.rpc_log
        assert lab.fake.app_starts == []
    finally:
        lab.bridge.stop()


def test_inbound_rate_limit(lab: Lab) -> None:
    lab.connect()
    lab.fap.wait_for(proto.Status)
    time.sleep(1.1)  # bucket weer vol
    lab.fap.clear()
    # Twaalf berichten in één keer (zoals een FAP die zich niet aan de 200 ms houdt).
    lab.fap.send_burst([proto.PresetsRequest(seq=lab.fap.next_seq()) for _ in range(12)])
    time.sleep(0.3)
    answered = len(lab.fap.of_type(proto.Presets))
    assert 5 <= answered <= 7, answered


def test_outbound_pacing(tmp_path: Path) -> None:
    lab = _make(tmp_path, timing=replace(FAST_BRIDGE, send_gap_s=0.05))
    try:
        lab.connect()
        ref = lab.fap.quick(Q.STATUS)
        lab.fap.result_text(ref)
        stamps = lab.fap.received_at
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        assert len(gaps) >= 3
        assert min(gaps) >= 0.045, gaps
    finally:
        lab.bridge.stop()


def test_interleaves_with_concurrent_tool_call(tmp_path: Path) -> None:
    data = bytes(range(256)) * 8
    lab = _make(tmp_path, files={"/ext/big.bin": data})
    lab.fake.read_chunk = 16  # 128 antwoordframes
    try:
        lab.connect()
        lab.fap.wait_for(proto.Status)
        result: dict[str, Any] = {}

        def read() -> None:
            result.update(
                lab.runner.call("mcp", "storage_read", {"path": "/ext/big.bin", "max_bytes": 4096})
            )

        reader = threading.Thread(target=read)
        reader.start()
        refs = [lab.fap.quick(Q.STATUS) for _ in range(1)]
        lab.fap.send(proto.PresetsRequest(seq=lab.fap.next_seq()))
        reader.join(5)
        assert result.get("ok") is True, result
        assert result["result"]["size"] == len(data)
        for ref in refs:
            assert "Uplink" in lab.fap.result_text(ref)
        assert wait_until(lambda: len(lab.fap.of_type(proto.Presets)) >= 2)
        assert lab.device._client is not None
        assert lab.device._client.dropped_events == 0
    finally:
        lab.bridge.stop()


def test_cli_tool_refused_while_app_open(lab: Lab) -> None:
    lab.connect()
    raw = lab.runner.call("mcp", "subghz_rx", {"frequency_hz": 433_920_000, "duration_s": 1})
    assert raw["ok"] is False and raw["error"] == "app_locked"
    assert lab.bridge.state is BridgeState.CONNECTED


def test_start_from_settings_off_and_on(tmp_path: Path) -> None:
    lab = _make(tmp_path)
    settings = Settings(audit_log=tmp_path / "a.jsonl", scope_file=tmp_path / "s.yaml")
    assert start_from_settings(lab.runner, settings) is None
    presets = tmp_path / "presets.yaml"
    presets.write_text("presets:\n  - {id: 1, label: Kort, prompt: hoi}\n", encoding="utf-8")
    on = settings.model_copy(update={"flipper_ui": "on", "flipper_ui_presets": presets})
    bridge = start_from_settings(lab.runner, on)
    assert bridge is not None
    try:
        assert list(bridge.presets) == [1]
        assert bridge.agent_available is False
    finally:
        bridge.stop()
