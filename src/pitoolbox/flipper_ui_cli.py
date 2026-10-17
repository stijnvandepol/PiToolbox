"""Hardwaretests voor de PiToolbox-app op de Flipper (fase 4); subcommando's van pitoolbox-flipper.

- ``ui-install [--fap PAD] [--force]``: de FAP controleren (ELF, manifest, API van de firmware)
  en via de storage-RPC op ``PITOOLBOX_FLIPPER_UI_APP`` zetten, met voortgang en MD5-controle.
- ``ui-test``: de app starten via de echte bridge, HELLO/WELCOME, een STATUS van de
  ``status``-tool end-to-end afleveren, de app netjes sluiten; met tijden per stap. Met
  ``--confirm-check`` daarna OK en Back op twee test-vragen ("Flipper verplicht").
- ``ui-droptest [--rounds N]``: valtest over BLE. Terwijl de Pi STATUS-berichten stuurt, wordt de
  link via BlueZ verbroken (``org.bluez.Device1.Disconnect`` over D-Bus, geen root). Daarna moet
  de Flipper antwoorden en de app opnieuw starten. Stopt bij de eerste fout.

**Niets hiervan zendt radio uit.** ui-test en ui-droptest draaien de bridge met een runner die
alleen ``status`` toelaat (``tool_allowlist``) en een lege scope heeft: kiest iemand op de
Flipper toch iets anders (wifi-scan, noodstop, preset), of drukt hij OK op een test-vraag, dan
weigert de policy dat. **Eén eigenaar:** draait
``pitoolbox-mcp`` of ``pitoolbox-agent`` met de Flipper, stop die dan eerst; twee processen
drukken elkaars BLE-link weg.

Hier staan de argumenten en de dispatch; de commando's staan in flipper_ui_install.py,
flipper_ui_smoke.py (ui-test) en flipper_ui_droptest.py, met het gedeelde gereedschap in
flipper_ui_harness.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import flipper_ui_install
from .config import Settings
from .flipper_cli_common import _mac_arg
from .flipper_ui_droptest import cmd_droptest
from .flipper_ui_harness import UiTiming
from .flipper_ui_install import cmd_install
from .flipper_ui_smoke import CHECK_TTL_MARGIN_S, cmd_ui_test
from .frontends.flipper_ui.protocol import MAX_TTL_S

__all__ = ["UiTiming", "add_parsers", "run"]

DROP_ROUNDS_MAX = 200
CONFIRM_WAIT_DEFAULT_S = 60.0
# Zo blijft de TTL van een test-vraag (wachttijd + marge) binnen de grens van het protocol.
CONFIRM_WAIT_MAX_S = float(MAX_TTL_S - CHECK_TTL_MARGIN_S)


# --- argumenten ------------------------------------------------------------------------------


def _drop_rounds_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen geheel getal: {value!r}") from exc
    if not 1 <= number <= DROP_ROUNDS_MAX:
        raise argparse.ArgumentTypeError(f"moet tussen 1 en {DROP_ROUNDS_MAX} liggen")
    return number


def _pause_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 0 <= number <= 60:
        raise argparse.ArgumentTypeError("moet tussen 0 en 60 seconden liggen")
    return number


def _wait_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 0 <= number <= 600:
        raise argparse.ArgumentTypeError("moet tussen 0 en 600 seconden liggen")
    return number


def _confirm_wait_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 1 <= number <= CONFIRM_WAIT_MAX_S:
        raise argparse.ArgumentTypeError(f"moet tussen 1 en {CONFIRM_WAIT_MAX_S:g} seconden liggen")
    return number


def add_parsers(sub: Any, common: argparse.ArgumentParser, settings: Settings) -> None:
    """Voeg ui-install, ui-test en ui-droptest toe aan de subparsers van pitoolbox-flipper."""
    link = argparse.ArgumentParser(add_help=False)
    link.add_argument(
        "--address",
        type=_mac_arg,
        default=None,
        help="BLE-adres (standaard PITOOLBOX_FLIPPER_BLE_ADDRESS)",
    )
    parents = [common, link]
    _add_install_parser(sub, parents, settings)
    _add_test_parser(sub, parents, settings)
    _add_droptest_parser(sub, parents)


def _add_install_parser(
    sub: Any, parents: list[argparse.ArgumentParser], settings: Settings
) -> None:
    default_fap = flipper_ui_install.DEFAULT_FAP
    p_install = sub.add_parser(
        "ui-install", parents=parents, help="de PiToolbox-app (FAP) op de Flipper zetten"
    )
    p_install.add_argument(
        "--fap",
        type=Path,
        default=None,
        help=f"het .fap-bestand (standaard {default_fap.relative_to(default_fap.parents[3])})",
    )
    p_install.add_argument(
        "--force", action="store_true", help="ook schrijven als dezelfde versie er al staat"
    )
    p_install.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default=settings.flipper_transport,
        help="standaard PITOOLBOX_FLIPPER_TRANSPORT (USB is sneller dan BLE)",
    )


def _add_test_parser(sub: Any, parents: list[argparse.ArgumentParser], settings: Settings) -> None:
    p_test = sub.add_parser(
        "ui-test", parents=parents, help="rooktest: app starten, handdruk, STATUS, sluiten"
    )
    p_test.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default=settings.flipper_transport,
        help="standaard PITOOLBOX_FLIPPER_TRANSPORT",
    )
    p_test.add_argument(
        "--wait-action",
        type=_wait_arg,
        default=0.0,
        metavar="S",
        help="daarna S seconden wachten tot je op de Flipper 'Status' kiest (standaard 0: niet)",
    )
    p_test.add_argument(
        "--confirm-check",
        type=_confirm_wait_arg,
        nargs="?",
        const=CONFIRM_WAIT_DEFAULT_S,
        default=0.0,
        metavar="S",
        help=(
            "daarna twee test-vragen: OK en dan Back op de Flipper, elk binnen S seconden "
            f"(standaard {CONFIRM_WAIT_DEFAULT_S:g}); zendt nooit iets uit"
        ),
    )


def _add_droptest_parser(sub: Any, parents: list[argparse.ArgumentParser]) -> None:
    p_drop = sub.add_parser(
        "ui-droptest",
        parents=parents,
        help="valtest: BLE-link N keer verbreken terwijl de app berichten krijgt",
    )
    p_drop.add_argument(
        "--rounds", type=_drop_rounds_arg, default=20, help="aantal keer verbreken (standaard 20)"
    )
    p_drop.add_argument(
        "--pause",
        type=_pause_arg,
        default=3.0,
        help="seconden tussen de rondes (standaard 3)",
    )
    p_drop.add_argument("--yes", action="store_true", help="niet om bevestiging vragen")


def run(args: argparse.Namespace, settings: Settings) -> int | None:
    """Voer een ui-commando uit; None als `args.command` geen ui-commando is."""
    if args.command == "ui-install":
        return cmd_install(args, settings)
    if args.command == "ui-test":
        return cmd_ui_test(args, settings)
    if args.command == "ui-droptest":
        return cmd_droptest(args, settings)
    return None
