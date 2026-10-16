"""flipper_tools: argumentvalidatie, scope_checks, handlers en de weg via ToolRunner + Policy."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from conftest import MakeCtx
from flipper_fakes import FAST_TIMING, FakeFlipperTransport
from pydantic import ValidationError

from pitoolbox.core.audit import AuditLog
from pitoolbox.core.devices.flipper import FlipperDevice
from pitoolbox.core.errors import CommandFailed
from pitoolbox.core.policy import Policy
from pitoolbox.core.scope import InfraredDevice, Scope
from pitoolbox.core.tools import flipper_tools as ft
from pitoolbox.core.tools.base import Registry, Risk, ToolSpec
from pitoolbox.core.tools.runner import ToolRunner


def spec(name: str) -> ToolSpec:
    return next(s for s in ft.TOOLS if s.name == name)


def runner_for(ctx: Any, tmp_path: Path) -> ToolRunner:
    return ToolRunner(
        Registry(ft.TOOLS), ctx, Policy(ctx.scope_store, []), AuditLog(tmp_path / "a.jsonl")
    )


def test_tool_names_and_risks() -> None:
    by = {s.name: s.risk for s in ft.TOOLS}
    assert by == {
        "system_info": Risk.READ,
        "storage_list": Risk.READ,
        "storage_read": Risk.READ,
        "subghz_rx": Risk.READ,
        "nfc_read": Risk.READ,
        "rfid_read": Risk.READ,
        "ir_send": Risk.TRANSMIT,
    }
    for s in ft.TOOLS:
        assert s.description
        assert s.json_schema()["additionalProperties"] is False


# --- argumentvalidatie -------------------------------------------------------------------


def test_storage_list_defaults_and_path_validation() -> None:
    assert ft.StorageListArgs().path == "/ext"
    assert ft.StorageListArgs(path="/ext/infrared").path == "/ext/infrared"
    for bad in ("/etc/passwd", "/ext/../int", "ext/infrared", "/ext/a;b", "/ext/$(x)"):
        with pytest.raises(ValidationError):
            ft.StorageListArgs(path=bad)


def test_storage_read_limits() -> None:
    assert ft.StorageReadArgs(path="/ext/a.txt").max_bytes == 65536
    ft.StorageReadArgs(path="/ext/a.txt", max_bytes=1)
    ft.StorageReadArgs(path="/ext/a.txt", max_bytes=262144)
    for bad in (0, 262145, -5):
        with pytest.raises(ValidationError):
            ft.StorageReadArgs(path="/ext/a.txt", max_bytes=bad)
    with pytest.raises(ValidationError):
        ft.StorageReadArgs()  # path is verplicht


def test_subghz_args_bounds() -> None:
    assert ft.SubGhzRxArgs(frequency_hz=433_920_000).duration_s == 10
    for bad in (0, 121):
        with pytest.raises(ValidationError):
            ft.SubGhzRxArgs(frequency_hz=433_920_000, duration_s=bad)
    for bad in (299_999_754, 928_000_001, -1):
        with pytest.raises(ValidationError):
            ft.SubGhzRxArgs(frequency_hz=bad)


@pytest.mark.parametrize("model", [ft.NfcReadArgs, ft.RfidReadArgs])
def test_tag_read_timeout_bounds(model: Any) -> None:
    assert model().timeout_s == 15
    model(timeout_s=1)
    model(timeout_s=60)
    for bad in (0, 61):
        with pytest.raises(ValidationError):
            model(timeout_s=bad)


def test_extra_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ft.NfcReadArgs(timeout_s=5, commando="id")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ft.IrSendArgs(device="tv", button="Power", file="/ext/x.ir")  # type: ignore[call-arg]


@pytest.mark.parametrize("button", ["Power", "Vol_up", "Ch +", "OK.1", "a-b", "x" * 32])
def test_ir_button_pattern_accepts(button: str) -> None:
    assert ft.IrSendArgs(device="tv", button=button).button == button


@pytest.mark.parametrize("button", ["", "x" * 33, "Power;ls", "a\nb", "Pow/er", "ü", "$(x)", "a\r"])
def test_ir_button_pattern_rejects(button: str) -> None:
    with pytest.raises(ValidationError):
        ft.IrSendArgs(device="tv", button=button)


@pytest.mark.parametrize("device", ["", "TV", "tv room", "-tv", "../x", "a" * 33])
def test_ir_device_pattern_rejects(device: str) -> None:
    with pytest.raises(ValidationError):
        ft.IrSendArgs(device=device, button="Power")


# --- scope_check -------------------------------------------------------------------------


def test_subghz_scope_check_frequency_ranges() -> None:
    check = spec("subghz_rx").scope_check
    assert check is not None
    scope = Scope()
    for ok in (299_999_755, 315_000_000, 348_000_000, 386_999_938, 433_920_000, 868_350_000):
        assert check(ft.SubGhzRxArgs(frequency_hz=ok), scope).in_scope, ok
    for gap in (348_000_001, 360_000_000, 386_999_937, 464_000_001, 500_000_000, 778_999_846):
        result = check(ft.SubGhzRxArgs(frequency_hz=gap), scope)
        assert not result.in_scope, gap
        assert "buiten de bereiken" in result.reason
        assert "299999755...348000000" in result.reason


def test_ir_scope_check_device_and_buttons(fixed_scope: Scope) -> None:
    check = spec("ir_send").scope_check
    assert check is not None
    assert check(ft.IrSendArgs(device="tv", button="Power"), fixed_scope).in_scope
    unknown_device = check(ft.IrSendArgs(device="beamer", button="Power"), fixed_scope)
    assert not unknown_device.in_scope
    assert "beamer" in unknown_device.reason
    bad_button = check(ft.IrSendArgs(device="tv", button="Vol_down"), fixed_scope)
    assert not bad_button.in_scope
    assert "Power" in bad_button.reason  # toegestane knoppen worden genoemd
    # Knoppennamen zijn hoofdlettergevoelig, net als op de Flipper
    assert not check(ft.IrSendArgs(device="tv", button="power"), fixed_scope).in_scope


def test_ir_scope_check_without_button_list_allows_any_button() -> None:
    scope = Scope(infrared=[InfraredDevice(name="amp", file="/ext/infrared/amp.ir")])
    check = spec("ir_send").scope_check
    assert check is not None
    assert check(ft.IrSendArgs(device="amp", button="Whatever"), scope).in_scope


def test_ir_scope_check_empty_scope_denies_everything() -> None:
    check = spec("ir_send").scope_check
    assert check is not None
    assert not check(ft.IrSendArgs(device="tv", button="Power"), Scope()).in_scope


def test_ir_summary_names_button_and_device() -> None:
    summary = spec("ir_send").summary
    assert summary is not None
    scope = Scope.model_validate(
        {"infrared": [{"name": "tv", "file": "/ext/infrared/tv.ir", "buttons": ["Power"]}]}
    )
    text = summary(ft.IrSendArgs(device="tv", button="Power"), scope)
    assert text == "IR 'Power' naar tv (/ext/infrared/tv.ir)"


def test_max_duration_covers_arg_maximum() -> None:
    assert spec("subghz_rx").max_duration_s >= 120
    assert spec("nfc_read").max_duration_s >= 60
    assert spec("rfid_read").max_duration_s >= 60


# --- handlers (Flipper = MagicMock) ------------------------------------------------------


def test_handlers_delegate_to_flipper(make_ctx: MakeCtx, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.flipper.system_info.return_value = {"hardware_name": "Flippy"}
    assert spec("system_info").handler(ctx, ft.SystemInfoArgs()) == {
        "info": {"hardware_name": "Flippy"}
    }
    ctx.flipper.storage_list.return_value = [{"name": "a.ir", "type": "file", "size": 3}]
    listing = spec("storage_list").handler(ctx, ft.StorageListArgs())
    ctx.flipper.storage_list.assert_called_once_with("/ext")
    assert listing["count"] == 1
    ctx.flipper.subghz_rx.return_value = {"packets": []}
    spec("subghz_rx").handler(ctx, ft.SubGhzRxArgs(frequency_hz=433_920_000))
    ctx.flipper.subghz_rx.assert_called_once_with(433_920_000, 10)
    ctx.flipper.nfc_scan.return_value = {"found": False}
    spec("nfc_read").handler(ctx, ft.NfcReadArgs(timeout_s=7))
    ctx.flipper.nfc_scan.assert_called_once_with(7)
    ctx.flipper.rfid_read.return_value = {"found": False}
    spec("rfid_read").handler(ctx, ft.RfidReadArgs(timeout_s=9))
    ctx.flipper.rfid_read.assert_called_once_with(9)


def test_storage_read_text_and_binary_encoding(make_ctx: MakeCtx, tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    handler = spec("storage_read").handler
    args = ft.StorageReadArgs(path="/ext/infrared/tv.ir", max_bytes=100)

    ctx.flipper.storage_read.return_value = "Zwölf\n".encode()
    assert handler(ctx, args) == {
        "path": "/ext/infrared/tv.ir",
        "size": len("Zwölf\n".encode()),
        "encoding": "utf-8",
        "content": "Zwölf\n",
    }
    ctx.flipper.storage_read.assert_called_with("/ext/infrared/tv.ir", 100)

    binary = b"\xff\xfe\x00\x01"
    ctx.flipper.storage_read.return_value = binary
    result = handler(ctx, args)
    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == binary
    assert result["size"] == 4

    nul = b"abc\x00def"  # geldig utf-8, maar met NUL: behandelen als binair
    ctx.flipper.storage_read.return_value = nul
    assert handler(ctx, args)["encoding"] == "base64"


def test_ir_send_handler_resolves_file_from_scope(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.flipper.ir_send.return_value = {
        "file": "/ext/infrared/tv.ir",
        "button": "Power",
        "sent": True,
    }
    result = spec("ir_send").handler(ctx, ft.IrSendArgs(device="tv", button="Power"))
    ctx.flipper.ir_send.assert_called_once_with("/ext/infrared/tv.ir", "Power")
    assert result["device"] == "tv"
    assert result["sent"] is True


def test_ir_send_handler_without_scope_entry_is_a_safety_net(
    make_ctx: MakeCtx, tmp_path: Path
) -> None:
    ctx = make_ctx(tmp_path, Scope())
    with pytest.raises(CommandFailed):
        spec("ir_send").handler(ctx, ft.IrSendArgs(device="tv", button="Power"))
    ctx.flipper.ir_send.assert_not_called()


# --- via ToolRunner + Policy -------------------------------------------------------------


def test_runner_ir_send_requires_confirmation_then_sends(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.flipper.ir_send.return_value = {
        "file": "/ext/infrared/tv.ir",
        "button": "Power",
        "sent": True,
    }
    runner = runner_for(ctx, tmp_path)
    first = runner.call("test", "ir_send", {"device": "tv", "button": "Power"})
    assert first["error"] == "confirmation_required"
    assert "'Power'" in first["summary"]
    ctx.flipper.ir_send.assert_not_called()
    # confirm_action zit niet in deze testregistry; de pending bevestigen via de store
    pending = ctx.pending.take(first["pending_id"], "test")
    assert pending is not None
    assert spec("ir_send").handler(ctx, pending.args)["sent"] is True


def test_runner_ir_send_denied_out_of_scope(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    ctx = make_ctx(tmp_path, fixed_scope)
    runner = runner_for(ctx, tmp_path)
    wrong_button = runner.call("test", "ir_send", {"device": "tv", "button": "Menu"})
    assert wrong_button["error"] == "denied"
    wrong_device = runner.call("test", "ir_send", {"device": "beamer", "button": "Power"})
    assert wrong_device["error"] == "denied"
    injection = runner.call("test", "ir_send", {"device": "tv", "button": "Power; reboot"})
    assert injection["error"] == "invalid_arguments"
    ctx.flipper.ir_send.assert_not_called()


def test_runner_subghz_out_of_range_denied_and_in_range_allowed(
    make_ctx: MakeCtx, tmp_path: Path
) -> None:
    ctx = make_ctx(tmp_path)
    ctx.flipper.subghz_rx.return_value = {"packets": [], "packet_count": 0}
    runner = runner_for(ctx, tmp_path)
    denied = runner.call("test", "subghz_rx", {"frequency_hz": 360_000_000})
    assert denied["error"] == "denied"
    assert "buiten de bereiken" in denied["message"]
    ok = runner.call("test", "subghz_rx", {"frequency_hz": 433_920_000, "duration_s": 5})
    assert ok["ok"] is True
    ctx.flipper.subghz_rx.assert_called_once_with(433_920_000, 5)


def test_runner_read_tools_are_allowed_without_confirmation(
    make_ctx: MakeCtx, tmp_path: Path
) -> None:
    ctx = make_ctx(tmp_path)
    ctx.flipper.storage_list.return_value = []
    result = runner_for(ctx, tmp_path).call("test", "storage_list", {})
    assert result == {"ok": True, "result": {"path": "/ext", "count": 0, "entries": []}}


def test_runner_maps_flipper_errors_to_structured_errors(make_ctx: MakeCtx, tmp_path: Path) -> None:
    from pitoolbox.core.errors import DeviceNotConnected

    ctx = make_ctx(tmp_path)
    ctx.flipper.system_info.side_effect = DeviceNotConnected("geen Flipper")
    result = runner_for(ctx, tmp_path).call("test", "system_info", {})
    assert result == {"ok": False, "error": "device_not_connected", "message": "geen Flipper"}


# --- integratie: tools -> echte FlipperDevice -> fake transport --------------------------


def test_integration_tools_against_fake_flipper(
    make_ctx: MakeCtx, tmp_path: Path, fixed_scope: Scope
) -> None:
    fake = FakeFlipperTransport(files={"/ext/infrared/tv.ir": b"name: Power\n"})
    ctx = make_ctx(tmp_path, fixed_scope)
    ctx.flipper = FlipperDevice(lambda: fake, "auto", timing=FAST_TIMING)
    runner = runner_for(ctx, tmp_path)

    info = runner.call("test", "system_info", {})
    assert info["result"]["info"]["firmware_origin_fork"] == "Official"

    listing = runner.call("test", "storage_list", {"path": "/ext/infrared"})
    assert listing["result"]["entries"] == [{"name": "tv.ir", "type": "file", "size": 12}]

    read = runner.call("test", "storage_read", {"path": "/ext/infrared/tv.ir"})
    assert read["result"]["content"] == "name: Power\n"

    too_big = runner.call("test", "storage_read", {"path": "/ext/infrared/tv.ir", "max_bytes": 5})
    assert too_big["error"] == "device_error"

    rfid = runner.call("test", "rfid_read", {"timeout_s": 2})
    assert rfid["result"]["protocol"] == "EM4100"

    pending = runner.call("test", "ir_send", {"device": "tv", "button": "Power"})
    assert pending["error"] == "confirmation_required"
    taken = ctx.pending.take(pending["pending_id"], "test")
    assert taken is not None
    sent = spec("ir_send").handler(ctx, taken.args)
    assert sent == {"device": "tv", "file": "/ext/infrared/tv.ir", "button": "Power", "sent": True}
    assert fake.sent_buttons == ["Power"]
