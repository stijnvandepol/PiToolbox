"""Tests voor `pitoolbox-flipper ui-install`, `ui-test` en `ui-droptest` (flipper_ui_cli.py).

Echte `FlipperDevice`, runner, policy en bridge op een `FakeFlipperTransport` met een
nagebootste FAP; het verbreken van BLE (D-Bus) is vervangen door het sluiten van de fake-link.
"""

from __future__ import annotations

import argparse
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from flipper_fakes import FAST_TIMING, FakeFap, FakeFlipperTransport, make_fap, wait_until

from pitoolbox import (
    flipper_cli,
    flipper_ui_cli,
    flipper_ui_droptest,
    flipper_ui_harness,
    flipper_ui_install,
)
from pitoolbox.config import Settings
from pitoolbox.core.audit import AuditLog
from pitoolbox.core.devices.flipper import FlipperDevice
from pitoolbox.core.policy import PendingStore, Policy
from pitoolbox.core.scope import ScopeStore
from pitoolbox.core.tools import build_registry
from pitoolbox.core.tools.base import ToolContext
from pitoolbox.core.tools.flipper_tools import IrSendArgs
from pitoolbox.core.tools.runner import CONFIRM_ON_FLIPPER, CONFIRM_TOOL, ToolRunner
from pitoolbox.flipper_ui_smoke import CHECK_BUTTON, CHECK_DEVICE, CHECK_FRONTEND, CHECK_TOOL
from pitoolbox.frontends.flipper_ui import protocol as proto
from pitoolbox.frontends.flipper_ui.bridge import BridgeTiming

ADDR = "80:E1:26:20:F6:6B"
APP = "/ext/apps/Tools/pitoolbox_ui.fap"
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
    link_backoff_s=(0.05,),
    stop_join_s=2.0,
)
FAST_UI = flipper_ui_cli.UiTiming(
    connect_s=5.0,
    hello_s=3.0,
    step_s=3.0,
    exit_s=2.0,
    detach_s=3.0,
    reconnect_s=5.0,
    locked_wait_s=2.0,
    poll_s=0.02,
    traffic_gap_s=0.02,
    traffic_s=0.05,
    traffic_step_s=0.01,
)
DEVICE_TIMING = replace(FAST_TIMING, loader_free_wait_s=0.3, loader_poll_s=0.02)


class UiLab:
    """Eén nep-Flipper met FAP; vervangt de opbouw-hooks van de ui-commando's."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fake = FakeFlipperTransport(start_in_rpc=True, kind="ble")
        self.fap = self.fake.attach_fap(FakeFap())
        self.audit_path = tmp_path / "audit.jsonl"
        self.settings = Settings(
            scope_file=tmp_path / "scope.yaml",
            audit_log=self.audit_path,
            captures_dir=tmp_path / "captures",
            flipper_ble_address=ADDR,
            flipper_ble_adapter="hci1",
        )
        self.runner_settings: list[Settings] = []
        self.runners: list[ToolRunner] = []
        self.device_settings: list[Settings] = []
        self.disconnects: list[tuple[str, str | None]] = []
        self.on_disconnect: Any = None
        self.wifi = MagicMock()
        self.wifi.get_mode.return_value = "managed"
        monkeypatch.setattr(flipper_cli, "_load_settings", lambda: self.settings)
        monkeypatch.setattr(flipper_ui_harness, "_make_runner", self.make_runner)
        monkeypatch.setattr(flipper_ui_install, "_make_device", self.make_device)
        monkeypatch.setattr(flipper_ui_droptest, "_disconnect", self.disconnect)
        monkeypatch.setattr(flipper_ui_harness, "BRIDGE_TIMING", FAST_BRIDGE)
        monkeypatch.setattr(flipper_ui_harness, "TIMING", FAST_UI)

    def make_device(self, settings: Settings) -> FlipperDevice:
        self.device_settings.append(settings)
        return FlipperDevice(lambda: self.fake, timing=DEVICE_TIMING)

    def make_runner(self, settings: Settings) -> ToolRunner:
        self.runner_settings.append(settings)
        scope_store = ScopeStore(settings.scope_file)  # zoals app.build_runner
        captures = MagicMock()
        captures.list_active.return_value = []
        system = MagicMock()
        system.throttled.return_value = {"available": False}
        system.uplink.return_value = {"iface": "wlan0", "operstate": "UP", "ipv4": ["10.0.0.2"]}
        system.disk.return_value = {"percent": 1.0}
        ctx = ToolContext(
            settings=settings,
            scope_store=scope_store,
            flipper=self.make_device(settings),
            wifi=self.wifi,
            captures=captures,
            system=system,
            pending=PendingStore(ttl_s=settings.confirm_ttl_s),
        )
        policy = Policy(scope_store, settings.tool_allowlist)  # zoals app.build_runner
        runner = ToolRunner(build_registry(), ctx, policy, AuditLog(settings.audit_log))
        self.runners.append(runner)
        return runner

    def disconnect(self, address: str, adapter: str | None) -> bool:
        self.disconnects.append((address, adapter))
        if self.on_disconnect is not None:
            self.on_disconnect()
        was_open = self.fake.is_open
        self.fake.close()  # de link valt weg: de FAP krijgt SessionClose en sluit
        return was_open

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text().splitlines()]


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[UiLab]:
    yield UiLab(tmp_path, monkeypatch)


def _run(*argv: str) -> int:
    return flipper_cli.main(list(argv))


# --- ui-install -----------------------------------------------------------------------------


def test_ui_install_uploads_with_progress_and_md5(
    lab: UiLab, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fap = tmp_path / "pitoolbox_ui.fap"
    data = make_fap(body=8000)
    fap.write_bytes(data)
    assert _run("ui-install", "--fap", str(fap), "--transport", "ble") == 0
    out = capsys.readouterr().out
    assert lab.fake.files[APP] == data
    assert "API 87.1" in out and "100%" in out and "Geïnstalleerd" in out and "MD5" in out
    assert "ui-test" in out
    used = lab.device_settings[0]
    assert used.flipper_transport == "ble" and used.flipper_ble_adapter == "hci1"
    # Tweede keer: zelfde MD5, niets geschreven; --force schrijft toch.
    chunks = len(lab.fake.write_chunks)
    assert _run("ui-install", "--fap", str(fap)) == 0
    assert "Staat er al" in capsys.readouterr().out
    assert len(lab.fake.write_chunks) == chunks
    assert _run("ui-install", "--fap", str(fap), "--force") == 0
    assert len(lab.fake.write_chunks) > chunks


def test_ui_install_default_path_and_errors(
    lab: UiLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "dist" / "pitoolbox_ui.fap"
    monkeypatch.setattr(flipper_ui_install, "DEFAULT_FAP", missing)
    assert _run("ui-install") == 1
    assert "bestaat niet" in capsys.readouterr().err
    missing.parent.mkdir()
    missing.write_bytes(b"geen elf")
    assert _run("ui-install") == 1
    assert "geen geldige FAP" in capsys.readouterr().err
    missing.write_bytes(make_fap(name="Snake"))
    assert _run("ui-install") == 1
    assert "niet de PiToolbox-app" in capsys.readouterr().err
    assert lab.device_settings == []  # niets verbonden


def test_ui_install_refuses_api_mismatch(
    lab: UiLab, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fap = tmp_path / "x.fap"
    fap.write_bytes(make_fap(api=(86, 0)))
    assert _run("ui-install", "--fap", str(fap)) == 1
    err = capsys.readouterr().err
    assert "Past niet" in err and "App Too Old/New" in err
    assert lab.fake.write_chunks == []


def test_ui_install_refuses_when_app_open(
    lab: UiLab, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fap = tmp_path / "x.fap"
    fap.write_bytes(make_fap())
    lab.fake.user_app_open = True
    assert _run("ui-install", "--fap", str(fap)) == 1
    assert "app open" in capsys.readouterr().err
    assert lab.fake.write_chunks == []


# --- ui-test --------------------------------------------------------------------------------


def test_ui_test_handshake_status_and_clean_exit(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("ui-test") == 0
    out = capsys.readouterr().out
    for label in (
        "verbinden + system_info",
        "app gestart",
        "HELLO ontvangen (v1.0)",
        "WELCOME afgeleverd",
        "STATUS afgeleverd",
        "app gesloten, loader vrij",
    ):
        assert label in out
    assert "Rooktest geslaagd" in out
    assert not lab.fake.fap_running
    assert lab.fake.app_starts == [APP]
    assert lab.runner_settings[0].tool_allowlist == ["status"]
    tools = {r.get("tool") for r in lab.audit() if "tool" in r}
    assert tools == {"status"}
    assert all(r["frontend"] == "flipper-ui" for r in lab.audit() if "tool" in r)


def test_ui_test_refuses_when_another_app_is_open(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    lab.fake.user_app_open = True
    assert _run("ui-test") == 1
    assert "Er is een app open" in capsys.readouterr().err
    assert lab.fake.app_starts == []


def test_ui_test_without_hello_fails_and_app_is_closed(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    lab.fap.auto_hello = False
    assert _run("ui-test") == 1
    err = capsys.readouterr().err
    assert "geen HELLO" in err and "app gestart" in err  # wat wel lukte, staat erbij
    assert not lab.fake.fap_running


def _press_after_status(lab: UiLab, action: proto.QuickAction) -> threading.Thread:
    def press() -> None:
        lab.fap.wait_for(proto.Status, timeout=5.0)
        lab.fap.quick(action)

    thread = threading.Thread(target=press, daemon=True)
    thread.start()
    return thread


def test_ui_test_wait_action_status_end_to_end(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    thread = _press_after_status(lab, Q.STATUS)
    assert _run("ui-test", "--wait-action", "3") == 0
    thread.join(1.0)
    out = capsys.readouterr().out
    assert "actie Status: antwoord afgeleverd" in out
    assert "Uplink: wlan0" in "".join(m.text for m in lab.fap.of_type(proto.ResultChunk))


def test_ui_test_other_action_is_refused_by_allowlist(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    thread = _press_after_status(lab, Q.WIFI_SCAN)
    assert _run("ui-test", "--wait-action", "3") == 1
    thread.join(1.0)
    assert "foutmelding" in capsys.readouterr().err
    lab.wifi.scan.assert_not_called()  # de policy liet wifi_scan niet door


# --- ui-test --confirm-check ----------------------------------------------------------------

CHECK_SCOPE = """\
version: 1
infrared:
  - name: pitoolbox_confirm_check
    file: /ext/infrared/tv.ir
"""


@pytest.fixture
def ir_sends(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Elke aanroep van FlipperDevice.ir_send; de tests eisen dat dit leeg blijft."""
    calls: list[tuple[Any, ...]] = []

    def record(self: FlipperDevice, *args: Any) -> dict[str, Any]:
        calls.append(args)
        raise AssertionError("ir_send mag in ui-test nooit aangeroepen worden")

    monkeypatch.setattr(FlipperDevice, "ir_send", record)
    return calls


def _chat_refused(lab: UiLab) -> bool:
    # Tekst, geen JSON: de regel kan net geschreven worden terwijl we lezen.
    return lab.audit_path.exists() and CONFIRM_ON_FLIPPER in lab.audit_path.read_text()


def _answer_check_prompts(lab: UiLab, approvals: list[bool]) -> threading.Thread:
    """Speelt de mens: wacht tot de chat geweigerd is, en beantwoordt dan de test-vragen."""

    def press() -> None:
        assert wait_until(lambda: _chat_refused(lab), timeout=5.0)
        handled: set[int] = set()
        for approve in approvals:
            prompt = lab.fap.wait_for(
                proto.ConfirmPrompt, lambda m: m.handle not in handled, timeout=5.0
            )
            handled.add(prompt.handle)
            lab.fap.answer(prompt.handle, approve)

    thread = threading.Thread(target=press, daemon=True)
    thread.start()
    return thread


def _check_rows(lab: UiLab) -> list[dict[str, Any]]:
    return [r for r in lab.audit() if r.get("tool") == CHECK_TOOL]


def test_ui_test_confirm_check_ok_then_back(
    lab: UiLab, ir_sends: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    thread = _answer_check_prompts(lab, [True, False])
    assert _run("ui-test", "--confirm-check") == 0
    thread.join(1.0)
    out = capsys.readouterr().out
    for label in (
        "STATUS afgeleverd",
        "vraag 1 op de Flipper (CONFIRM_PROMPT)",
        "chat-bevestiging geweigerd (Flipper verplicht)",
        "OK op vraag 1: bevestigd, policy weigert",
        "vraag 1 gesloten (CONFIRM_CLOSED)",
        "vraag 2 op de Flipper (CONFIRM_PROMPT)",
        "Back op vraag 2: afgewezen",
        "vraag 2 gesloten (CONFIRM_CLOSED)",
        "app gesloten, loader vrij",
    ):
        assert label in out
    assert "druk nu OK" in out and "druk nu Back" in out and "Rooktest geslaagd" in out
    # De tijdkolom staat onder elkaar, ook met het langste label.
    timeline = [line for line in out.splitlines() if "(totaal" in line]
    assert len({line.index("(totaal") for line in timeline}) == 1
    # De Flipper hoorde wat er gebeurde: OK = mislukt (geweigerd door de policy), Back = nee.
    reasons = [m.reason for m in lab.fap.of_type(proto.ConfirmClosed)]
    assert reasons == [proto.CloseReason.FAILED, proto.CloseReason.REJECTED]
    prompts = lab.fap.of_type(proto.ConfirmPrompt)
    assert [p.summary.split(":")[0] for p in prompts] == ["Test 1/2", "Test 2/2"]
    # Zonder waarde: 60 s per vraag, de pending leeft 10 s langer.
    used = lab.runner_settings[0]
    assert used.confirm_ttl_s == 70 and used.tool_allowlist == ["status"]
    rows = _check_rows(lab)
    assert [(r["decision"], r.get("confirmed_on")) for r in rows] == [
        ("deny", "flipper"),
        ("rejected", "flipper"),
    ]
    assert "allowlist" in rows[0]["result"]
    assert ir_sends == [] and lab.fake.sent_buttons == [] and lab.fake.app_starts == [APP]
    assert lab.runners[0].ctx.pending.list() == []


def test_ui_test_runner_has_an_empty_scope_of_its_own(lab: UiLab, tmp_path: Path) -> None:
    lab.settings.scope_file.write_text(CHECK_SCOPE)  # de echte scope van de gebruiker
    assert _run("ui-test") == 0
    used = lab.runner_settings[0].scope_file
    assert used != lab.settings.scope_file
    assert used.parent.name.startswith(flipper_ui_harness.SCOPE_DIR_PREFIX)
    assert not used.parent.exists()  # opgeruimd na de test


@pytest.mark.parametrize(
    ("approvals", "expected"),
    [
        ([False], "op vraag 1 kwam Back binnen, verwacht was OK"),
        ([True, True], "op vraag 2 kwam OK binnen, verwacht was Back"),
    ],
)
def test_ui_test_confirm_check_wrong_button(
    lab: UiLab,
    ir_sends: list[tuple[Any, ...]],
    capsys: pytest.CaptureFixture[str],
    approvals: list[bool],
    expected: str,
) -> None:
    thread = _answer_check_prompts(lab, approvals)
    assert _run("ui-test", "--confirm-check", "3") == 1
    thread.join(1.0)
    err = capsys.readouterr().err
    assert "Rooktest MISLUKT" in err and expected in err and "niets uitgezonden" in err
    assert "Wel gelukt" in err and "vraag 1 op de Flipper" in err
    assert ir_sends == [] and lab.fake.sent_buttons == []
    assert not lab.fake.fap_running


def test_ui_test_confirm_check_timeout_says_where_to_look(
    lab: UiLab, ir_sends: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("ui-test", "--confirm-check", "1") == 1
    err = capsys.readouterr().err
    assert "geen antwoord op vraag 1 binnen 1 s" in err
    assert "Kijk op de Flipper" in err and "Test 1/2" in err
    assert "chat-bevestiging geweigerd" in err  # dat lukte wel
    assert lab.runners[0].ctx.pending.list() == []  # de vraag bleef niet staan
    assert ir_sends == [] and not lab.fake.fap_running


def test_ui_test_confirm_check_never_transmits_even_if_in_users_scope(
    lab: UiLab, ir_sends: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    """OK op beide vragen, en de echte scope van de gebruiker bevat het test-apparaat zelfs:
    de bevestigde test-actie komt nooit bij FlipperDevice.ir_send."""
    lab.settings.scope_file.write_text(CHECK_SCOPE)
    thread = _answer_check_prompts(lab, [True, True])
    assert _run("ui-test", "--confirm-check", "3") == 1
    thread.join(1.0)
    capsys.readouterr()
    confirmed = [r for r in _check_rows(lab) if r.get("confirmed")]
    assert [r["decision"] for r in confirmed] == ["deny", "deny"]
    assert ir_sends == [] and lab.fake.sent_buttons == [] and lab.fake.app_starts == [APP]


def test_confirm_check_action_is_denied_by_each_layer_alone(
    lab: UiLab, ir_sends: list[tuple[Any, ...]], tmp_path: Path
) -> None:
    """Dubbele zekerheid: de allowlist alleen weigert, en de lege scope alleen ook."""
    users_scope = tmp_path / "users-scope.yaml"
    users_scope.write_text(CHECK_SCOPE)
    link = argparse.Namespace(adapter=None, address=None)
    with flipper_ui_harness._empty_scope_dir() as scope_dir:
        harness_settings = flipper_ui_harness._runner_settings(lab.settings, link, "ble", scope_dir)
        only_allowlist = harness_settings.model_copy(update={"scope_file": users_scope})
        only_empty_scope = harness_settings.model_copy(update={"tool_allowlist": []})
        for settings, reason in ((only_allowlist, "allowlist"), (only_empty_scope, "scope.yaml")):
            runner = lab.make_runner(settings)
            args = IrSendArgs(device=CHECK_DEVICE, button=CHECK_BUTTON)
            pending = runner.ctx.pending.create(CHECK_TOOL, args, CHECK_FRONTEND, "test")
            raw = runner.call(CHECK_FRONTEND, CONFIRM_TOOL, {"pending_id": pending.id})
            assert raw["error"] == "denied" and reason in raw["message"]
    assert ir_sends == [] and lab.fake.sent_buttons == []


# --- ui-droptest ----------------------------------------------------------------------------


def test_ui_droptest_rounds_reconnect_and_ping(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("ui-droptest", "--rounds", "3", "--pause", "0", "--yes") == 0
    out = capsys.readouterr().out
    assert out.count(": OK  verbreking gezien") == 3
    assert "Valtest geslaagd: 3 van 3" in out
    assert lab.disconnects == [(ADDR, "hci1")] * 3
    assert lab.fap.starts == 4  # eerste start + na elke val opnieuw
    assert not lab.fake.fap_running  # na afloop netjes gesloten
    assert lab.runner_settings[0].flipper_transport == "ble"
    assert lab.runner_settings[0].tool_allowlist == ["status"]
    # De Pi stuurde in elke ronde STATUS-berichten vóór de val.
    assert len(lab.fap.of_type(proto.Status)) >= 3


def test_ui_droptest_waits_for_closing_app_and_restarts(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    def old_app_still_closing() -> None:
        lab.fake.user_app_open = True  # zoals "Verbinding met Pi verbroken" (2 s) op de Flipper
        timer = threading.Timer(0.8, lambda: setattr(lab.fake, "user_app_open", False))
        timer.daemon = True
        timer.start()

    lab.on_disconnect = old_app_still_closing
    assert _run("ui-droptest", "--rounds", "1", "--pause", "0", "--yes") == 0
    out = capsys.readouterr().out
    assert "loader nog bezet bij herstart" in out
    assert lab.fap.starts == 2


def test_ui_droptest_stops_at_first_failure_with_screen_instructions(
    lab: UiLab, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = {"n": 0}

    def second_round_hangs() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            lab.fake.user_app_open = True  # de oude app sluit nooit

    lab.on_disconnect = second_round_hangs
    assert _run("ui-droptest", "--rounds", "5", "--pause", "0", "--yes") == 1
    captured = capsys.readouterr()
    assert "Ronde   1/5: OK" in captured.out
    assert "RONDE 2/5 MISLUKT" in captured.err and "loader bleef bezet" in captured.err
    assert "Kijk NU op het scherm" in captured.err and "Links + Back" in captured.err
    assert "Laatste gebeurtenissen" in captured.err
    assert len(lab.disconnects) == 2


def test_ui_droptest_needs_address_and_confirmation(
    lab: UiLab, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lab.settings = lab.settings.model_copy(update={"flipper_ble_address": None})
    assert _run("ui-droptest", "--yes") == 2
    assert "BLE-adres" in capsys.readouterr().err
    monkeypatch.setattr(flipper_ui_droptest, "_input", lambda prompt: "n")
    assert _run("ui-droptest", "--address", "80:e1:26:20:f6:6b") == 1
    assert "Niet gestart" in capsys.readouterr().out
    assert lab.runner_settings == [] and lab.disconnects == []


def test_ui_droptest_confirmed_interactively(
    lab: UiLab, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(flipper_ui_droptest, "_input", lambda p: prompts.append(p) or "ja")
    assert _run("ui-droptest", "--rounds", "1", "--pause", "0") == 0
    assert len(prompts) == 1
    assert wait_until(lambda: not lab.fake.fap_running)


@pytest.mark.parametrize(
    "argv",
    [
        ["ui-droptest", "--rounds", "0"],
        ["ui-droptest", "--rounds", "201"],
        ["ui-droptest", "--pause", "-1"],
        ["ui-test", "--wait-action", "x"],
        ["ui-test", "--confirm-check", "0"],
        ["ui-test", "--confirm-check", "111"],
        ["ui-test", "--transport", "wifi"],
        ["ui-install", "--address", "geen-mac"],
    ],
)
def test_ui_arguments_are_validated(lab: UiLab, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(*argv)
    assert exc.value.code == 2
