"""Beheer-CLI voor de Flipper over Bluetooth LE (entry point ``pitoolbox-flipper``).

Subcommando's:
- ``ble-scan``: Flippers in de buurt met adres, naam, signaalsterkte en koppelstatus;
- ``ble-pair <adres>``: koppelen; de Flipper toont een 6-cijferige code die je hier invoert;
- ``ble-unpair <adres>``: koppeling (bonding) uit BlueZ verwijderen;
- ``ble-list``: gekoppelde Flippers volgens BlueZ;
- ``ble-test``: de fase-3-meetscript: verbinden, system_info, opslaglijst, ping-latency en
  downloadsnelheid, optioneel naast USB;
- ``ui-install``, ``ui-test``, ``ui-droptest``: de PiToolbox-app op de Flipper installeren, een
  rooktest en de valtest (fase 4, zie flipper_ui_cli.py).

Elk subcommando kent ``--adapter hciN`` (standaard PITOOLBOX_FLIPPER_BLE_ADAPTER, anders de
standaardadapter van BlueZ), handig als er meerdere Bluetooth-adapters zijn.

Het koppelen praat rechtstreeks met BlueZ over D-Bus (zie core/devices/_ble_pairing.py); er wordt
nergens een shell of ``bluetoothctl`` gestart. De Flipper-BLE-transportlaag zelf staat in
core/devices/_flipper_ble.py en wordt pas bij gebruik geimporteerd.

Hier staan de parser en de dispatch; de subcommando's staan per groep in flipper_ble_cli.py
(koppelen), flipper_ble_measure.py en flipper_ble_report.py (ble-test) en flipper_ui_cli.py
(de PiToolbox-app).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import flipper_ble_cli, flipper_ble_measure, flipper_ui_cli
from .config import Settings
from .core.devices import _ble_pairing as ble_pairing
from .core.errors import ToolboxError
from .flipper_ble_cli import prompt_passkey
from .flipper_ble_measure import FlipperLike, measure_transport
from .flipper_ble_report import TransportReport
from .flipper_cli_common import CliError, _err

__all__ = [
    "CliError",
    "FlipperLike",
    "TransportReport",
    "main",
    "measure_transport",
    "prompt_passkey",
]


def _adapter_arg(value: str) -> str | None:
    try:
        return ble_pairing.normalize_adapter(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pitoolbox-flipper",
        description="Flipper Zero over Bluetooth LE koppelen en testen; PiToolbox-app installeren "
        "en testen.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="commando")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--adapter",
        type=_adapter_arg,
        default=settings.flipper_ble_adapter,
        help="Bluetooth-adapter, bijv. hci1 (standaard PITOOLBOX_FLIPPER_BLE_ADAPTER, anders "
        "de standaardadapter van BlueZ)",
    )
    flipper_ble_cli.add_parsers(sub, common, settings)
    flipper_ble_measure.add_parsers(sub, common, settings)
    flipper_ui_cli.add_parsers(sub, common, settings)
    return parser


# --- main --------------------------------------------------------------------------------


def _load_settings() -> Settings:
    return Settings.from_env()


def main(argv: Sequence[str] | None = None) -> int:
    settings = _load_settings()
    args = _build_parser(settings).parse_args(argv)
    try:
        for run in (flipper_ble_cli.run, flipper_ble_measure.run, flipper_ui_cli.run):
            result = run(args, settings)
            if result is not None:
                return result
    except ToolboxError as exc:
        _err(f"Fout: {exc}")
        return 1
    except KeyboardInterrupt:
        _err("Afgebroken.")
        return 130
    _err(f"Onbekend commando: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
