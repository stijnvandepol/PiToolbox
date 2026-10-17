"""``ui-install [--fap PAD] [--force]``, subcommando van ``pitoolbox-flipper`` (flipper_ui_cli.py).

De FAP controleren (ELF, manifest, API van de firmware) en via de storage-RPC op
``PITOOLBOX_FLIPPER_UI_APP`` zetten, met voortgang en MD5-controle.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from . import flipper_ui_harness as harness
from .config import Settings
from .core.devices._fap import (
    FAP_APP_NAME,
    FAP_MAX_BYTES,
    FapError,
    FapInfo,
    check_compatible,
    inspect_fap,
)
from .core.errors import ToolboxError
from .flipper_ui_harness import _device_settings, _err, _out

if TYPE_CHECKING:
    from .core.devices.flipper import AppInstall, FlipperDevice

# src/pitoolbox/flipper_ui_install.py -> de root van de checkout. Vervangbaar in tests.
DEFAULT_FAP = Path(__file__).resolve().parents[2] / "flipper_app/pitoolbox_ui/dist/pitoolbox_ui.fap"


class UiCliError(ToolboxError):
    code = "cli_error"


# --- opbouw (vervangbaar in tests) -----------------------------------------------------------


def _make_device(settings: Settings) -> FlipperDevice:
    from .core.devices.flipper import FlipperDevice

    return FlipperDevice.from_settings(settings)


# --- ui-install ------------------------------------------------------------------------------


def _read_fap(path: Path) -> bytes:
    if not path.is_file():
        raise UiCliError(
            f"{path} bestaat niet. Bouw de app eerst (flipper_app/pitoolbox_ui/README.md, "
            "'ufbt') of geef het bestand met --fap."
        )
    size = path.stat().st_size
    if not 0 < size <= FAP_MAX_BYTES:
        raise UiCliError(f"{path} is {size} bytes; een FAP mag 1..{FAP_MAX_BYTES} bytes zijn.")
    return path.read_bytes()


class _Progress:
    """Eén regel per 10 %: over BLE duurt ~24 KiB zo'n 20 s."""

    def __init__(self) -> None:
        self._started = harness._clock()
        self._shown = -1

    def __call__(self, done: int, total: int) -> None:
        step = done * 10 // max(total, 1)
        if step <= self._shown:
            return
        self._shown = step
        elapsed = max(harness._clock() - self._started, 1e-6)
        _out(f"  {step * 10:3d}%  {done:>7}/{total} B  {done / elapsed / 1024:5.1f} KiB/s")


def cmd_install(args: argparse.Namespace, settings: Settings) -> int:
    fap_path: Path = args.fap if args.fap is not None else DEFAULT_FAP
    data = _read_fap(fap_path)
    try:
        info = inspect_fap(data)
    except FapError as exc:
        raise UiCliError(f"{fap_path} is geen geldige FAP: {exc}") from exc
    if info.name != FAP_APP_NAME:
        raise UiCliError(f"{fap_path} is niet de PiToolbox-app maar {info.name!r}.")
    dest = settings.flipper_ui_app
    _out(
        f"FAP: {fap_path} ({len(data)} B, {info.name} v{info.version}, API {info.api}, "
        f"target {info.target})"
    )
    _out(f"Doel op de Flipper: {dest} (PITOOLBOX_FLIPPER_UI_APP)")
    device = _make_device(_device_settings(settings, args, args.transport))
    try:
        result = _install(device, info, data, dest, force=args.force)
        if result is None:
            return 1
    finally:
        device.close()
    if result.skipped:
        _out(f"Staat er al (zelfde MD5 {result.md5}); niets geschreven. --force schrijft toch.")
    else:
        rate = result.size / max(result.seconds, 1e-6) / 1024
        _out(
            f"Geïnstalleerd: {result.path} ({result.size} B in {result.seconds:.1f} s, "
            f"{rate:.1f} KiB/s); MD5 {result.md5} op de Flipper gecontroleerd."
        )
    _out("Volgende stap: pitoolbox-flipper ui-test")
    return 0


def _install(
    device: FlipperDevice, info: FapInfo, data: bytes, dest: str, *, force: bool
) -> AppInstall | None:
    """Verbinden, de API controleren en schrijven; None als het niet past (gemeld)."""
    from .core.devices.flipper import AppLocked

    started = harness._clock()
    device_info = device.system_info()
    kind = device.status().get("transport_kind") or "?"
    _out(
        f"Verbonden via {kind} in {harness._clock() - started:.1f} s; firmware "
        f"{device_info.get('firmware_version', '?')} "
        f"(API {device_info.get('firmware_api_major', '?')}."
        f"{device_info.get('firmware_api_minor', '?')})"
    )
    problems = check_compatible(info, device_info)
    if problems:
        for problem in problems:
            _err(f"Past niet: {problem}.")
        _err(
            "Niet geïnstalleerd. Bouw de app tegen de SDK van precies deze firmware "
            "(flipper_app/pitoolbox_ui/README.md). Een app met de verkeerde API kan de "
            "Flipper laten crashen als hij via RPC start (fase4-spike.md §2)."
        )
        return None
    if "firmware_api_major" not in device_info:
        _err("Let op: de firmware meldt geen API-versie; de app is daar niet op gecontroleerd.")
    if kind == "ble":
        _out(f"Over BLE gaat dit met ~1 KiB/s: reken op ~{len(data) // 1024 + 5} s.")
    try:
        return device.install_app_file(dest, data, progress=_Progress(), skip_if_same=not force)
    except AppLocked as exc:
        _err(f"Niet geïnstalleerd: {exc}.")
        return None
