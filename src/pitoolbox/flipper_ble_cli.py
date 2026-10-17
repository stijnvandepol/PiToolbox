"""Koppelen met een Flipper over BLE; subcommando's van ``pitoolbox-flipper`` (flipper_cli.py).

- ``ble-scan``: Flippers in de buurt met adres, naam, signaalsterkte en koppelstatus;
- ``ble-pair <adres>``: koppelen; de Flipper toont een 6-cijferige code die je hier invoert;
- ``ble-unpair <adres>``: koppeling (bonding) uit BlueZ verwijderen;
- ``ble-list``: gekoppelde Flippers volgens BlueZ.

Het koppelen praat rechtstreeks met BlueZ over D-Bus (zie core/devices/_ble_pairing.py); er wordt
nergens een shell of ``bluetoothctl`` gestart.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .core.devices import _ble_pairing as ble_pairing
from .flipper_cli_common import CliError, _err, _format_table, _mac_arg, _out

# Vervangbaar in tests.
_input = input

PASSKEY_ATTEMPTS = 3
PASSKEY_RE = re.compile(r"\d{6}")  # de Flipper toont "%06lu" (bt.c, "Pairing code")


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 0 < number <= 3600:
        raise argparse.ArgumentTypeError("moet groter dan 0 en hoogstens 3600 seconden zijn")
    return number


def add_parsers(sub: Any, common: argparse.ArgumentParser, settings: Settings) -> None:
    """Voeg ble-scan, ble-pair, ble-unpair en ble-list toe aan de subparsers."""
    prefix_default = settings.flipper_ble_name_prefix

    p_scan = sub.add_parser("ble-scan", parents=[common], help="Flippers in de buurt zoeken")
    p_scan.add_argument("--timeout", type=_positive_float, default=8.0, help="scanduur (s)")
    p_scan.add_argument("--prefix", default=prefix_default, help="naamprefix van de Flipper")

    p_pair = sub.add_parser(
        "ble-pair", parents=[common], help="koppelen met een Flipper (code van zijn scherm)"
    )
    p_pair.add_argument("address", type=_mac_arg, help="BLE-adres (uit ble-scan)")
    p_pair.add_argument(
        "--timeout", type=_positive_float, default=90.0, help="totale tijd voor het koppelen (s)"
    )
    p_pair.add_argument(
        "--force", action="store_true", help="eerst een bestaande koppeling verwijderen"
    )

    p_unpair = sub.add_parser(
        "ble-unpair", parents=[common], help="koppeling met een Flipper verwijderen"
    )
    p_unpair.add_argument("address", type=_mac_arg)

    p_list = sub.add_parser("ble-list", parents=[common], help="gekoppelde Flippers tonen")
    p_list.add_argument(
        "--prefix", default=prefix_default, help="naamprefix; leeg = alle apparaten"
    )


def run(args: argparse.Namespace, settings: Settings) -> int | None:
    """Voer een koppel-commando uit; None als `args.command` er geen is."""
    if args.command == "ble-scan":
        return cmd_scan(args)
    if args.command == "ble-pair":
        return cmd_pair(args)
    if args.command == "ble-unpair":
        return cmd_unpair(args)
    if args.command == "ble-list":
        return cmd_list(args)
    return None


# --- hulpfuncties ------------------------------------------------------------------------


def _call_maybe_async(value: Any) -> Any:
    """Laat een sync of async scan-functie allebei toe (contract van _flipper_ble)."""
    if inspect.isawaitable(value):
        return asyncio.run(_await(value))
    return value


async def _await(value: Any) -> Any:
    return await value


# --- ble-scan / ble-list / ble-unpair / ble-pair -----------------------------------------


@dataclass(frozen=True)
class _Found:
    address: str
    name: str | None
    rssi: int | None


def _scan(prefix: str, timeout: float, adapter: str | None = None) -> list[_Found]:
    """Zoek Flippers met de BLE-transportlaag (lui geimporteerd; die module is niet vast)."""
    try:
        from .core.devices._flipper_ble import scan_flippers
    except ImportError as exc:
        raise CliError(f"BLE-transportlaag niet beschikbaar ({exc}).") from exc
    raw = _call_maybe_async(scan_flippers(prefix, timeout, adapter=adapter))
    found = []
    for cand in raw:
        rssi = getattr(cand, "rssi", None)
        found.append(
            _Found(
                address=str(cand.address).upper(),
                name=getattr(cand, "name", None),
                rssi=int(rssi) if rssi is not None else None,
            )
        )
    return found


def _paired_by_address(
    prefix: str, adapter: str | None = None
) -> dict[str, ble_pairing.PairedDevice] | None:
    """Koppelstatus uit BlueZ; None als D-Bus/BlueZ niet beschikbaar is (bijv. Windows)."""
    try:
        devices = ble_pairing.list_devices(prefix="", only_paired=True, adapter=adapter)
    except ble_pairing.BlePairingError as exc:
        _err(f"Koppelstatus onbekend: {exc}")
        return None
    return {d.address: d for d in devices}


def cmd_scan(args: argparse.Namespace) -> int:
    via = f", adapter {args.adapter}" if args.adapter else ""
    _out(f"Zoeken naar Flippers (naamprefix {args.prefix!r}, {args.timeout:g} s{via})...")
    found = _scan(args.prefix, args.timeout, args.adapter)
    if not found:
        _out(
            "Geen Flipper gevonden. Staat Bluetooth aan (Settings > Bluetooth) en is hij niet al "
            "verbonden met een telefoon?"
        )
        return 1
    paired = _paired_by_address(args.prefix, args.adapter)
    rows = [["ADRES", "NAAM", "RSSI", "GEKOPPELD"]]
    for item in sorted(found, key=lambda f: -(f.rssi if f.rssi is not None else -999)):
        state = "?" if paired is None else ("ja" if item.address in paired else "nee")
        rssi = f"{item.rssi} dBm" if item.rssi is not None else "-"
        rows.append([item.address, item.name or "-", rssi, state])
    _out(_format_table(rows))
    _out("")
    _out("Koppelen: pitoolbox-flipper ble-pair <ADRES>")
    return 0


def _flipper_uuids() -> frozenset[str]:
    """Service-UUID's van een Flipper (ook hernoemd herkenbaar); leeg zonder BLE-module."""
    try:
        from .core.devices._flipper_ble import FLIPPER_UUIDS
    except ImportError:
        return frozenset()
    return FLIPPER_UUIDS


def cmd_list(args: argparse.Namespace) -> int:
    devices = ble_pairing.list_devices(
        prefix=args.prefix,
        only_paired=True,
        adapter=args.adapter,
        service_uuids=_flipper_uuids(),
    )
    if not devices:
        _out("Geen gekoppelde Flippers in BlueZ. Koppel er een met: pitoolbox-flipper ble-pair")
        return 0
    rows = [["ADRES", "NAAM", "GEKOPPELD", "BONDED", "TRUSTED", "VERBONDEN"]]
    for d in devices:
        rows.append(
            [
                d.address,
                d.name or "-",
                "ja" if d.paired else "nee",
                "ja" if d.bonded else "nee",
                "ja" if d.trusted else "nee",
                "ja" if d.connected else "nee",
            ]
        )
    _out(_format_table(rows))
    return 0


def cmd_unpair(args: argparse.Namespace) -> int:
    if ble_pairing.unpair(args.address, adapter=args.adapter):
        _out(f"Koppeling met {args.address} verwijderd uit BlueZ.")
        _out(
            "Verwijder ook de koppeling op de Flipper (Settings > Bluetooth > Unpair All "
            "Devices), anders weigert hij de volgende keer de nieuwe sleutel."
        )
    else:
        _out(f"{args.address} is niet bekend bij BlueZ; er is niets verwijderd.")
    return 0


def prompt_passkey() -> int:
    """Vraag de 6-cijferige code van het Flipper-scherm; alleen cijfers, opnieuw bij fouten."""
    for _attempt in range(PASSKEY_ATTEMPTS):
        text = _input("Code van het Flipper-scherm (6 cijfers): ").strip()
        if PASSKEY_RE.fullmatch(text):
            return int(text)
        _err("Ongeldige invoer: de code bestaat uit precies 6 cijfers (bijv. 042731).")
    raise ValueError("te veel ongeldige pogingen")


def cmd_pair(args: argparse.Namespace) -> int:
    address: str = args.address
    _out(f"Koppelen met {address}.")
    _out("1. Zet Bluetooth aan op de Flipper (Settings > Bluetooth > Bluetooth: ON).")
    _out("2. Zorg dat de Flipper niet verbonden is met een telefoon of de Flipper-app.")
    _out("3. Zodra de Flipper 'Pairing code' met 6 cijfers toont, voer je die hieronder in.")
    _out()
    if args.force and ble_pairing.unpair(address, adapter=args.adapter):
        _out("Bestaande koppeling in BlueZ verwijderd (--force).")
    result = ble_pairing.pair(address, prompt_passkey, args.timeout, adapter=args.adapter)
    if result.already_paired:
        _out(f"{address} was al gekoppeld.")
        _out("Werkt verbinden niet meer? Gebruik 'ble-pair --force' en kies op de Flipper")
        _out("Unpair All Devices.")
    else:
        _out(f"Gekoppeld met {result.name or address} ({address}).")
    if not result.trusted:
        _err("Waarschuwing: kon 'Trusted' niet zetten; automatisch herverbinden kan haperen.")
    _out()
    _out("Zet in /etc/pitoolbox/pitoolbox.env (daarna de service herstarten):")
    _out(f"PITOOLBOX_FLIPPER_BLE_ADDRESS={address}")
    if args.adapter:
        _out(f"PITOOLBOX_FLIPPER_BLE_ADAPTER={args.adapter}")
    _out()
    via = f" --adapter {args.adapter}" if args.adapter else ""
    _out(f"Test de verbinding met: pitoolbox-flipper ble-test --address {address}{via}")
    return 0
