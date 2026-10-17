"""``ble-test``, subcommando van ``pitoolbox-flipper`` (flipper_cli.py): het fase-3-meetscript.

Verbinden, system_info, opslaglijst, ping-latency en downloadsnelheid over BLE, optioneel
naast USB; het rapport staat in flipper_ble_report.py. De Flipper-BLE-transportlaag
(core/devices/_flipper_ble.py) wordt pas bij gebruik geimporteerd, via ``FlipperDevice``.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol

from .config import Settings
from .core.errors import ToolboxError
from .core.scope import validate_flipper_path
from .flipper_ble_report import TransportReport, _render_reports
from .flipper_cli_common import CliError, _err, _mac_arg, _out

# Vervangbaar in tests.
_clock = time.perf_counter

# Grens voor het automatisch gekozen testbestand; een expliciete --file mag tot READ_LIMIT.
AUTO_FILE_MAX_BYTES = 256 * 1024
READ_LIMIT = 1024 * 1024
TEST_DIRS = ("/ext/infrared", "/ext/subghz")


class FlipperLike(Protocol):
    """Wat het testscript van een Flipper nodig heeft (FlipperDevice voldoet hieraan)."""

    def system_info(self) -> dict[str, str]: ...

    def storage_list(self, path: str) -> list[dict[str, Any]]: ...

    def storage_read(self, path: str, max_bytes: int) -> bytes: ...

    def status(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


# --- argumenten --------------------------------------------------------------------------


def _path_arg(value: str) -> str:
    try:
        return validate_flipper_path(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ongeldig Flipper-pad {value!r} (alleen /ext/... of /int/...)"
        ) from exc


def _rounds_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen geheel getal: {value!r}") from exc
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("moet tussen 1 en 1000 liggen")
    return number


def add_parsers(sub: Any, common: argparse.ArgumentParser, settings: Settings) -> None:
    """Voeg ble-test toe aan de subparsers van pitoolbox-flipper."""
    p_test = sub.add_parser(
        "ble-test", parents=[common], help="verbinding en doorvoer meten over BLE"
    )
    p_test.add_argument(
        "--address",
        type=_mac_arg,
        default=None,
        help="BLE-adres (standaard PITOOLBOX_FLIPPER_BLE_ADDRESS)",
    )
    p_test.add_argument(
        "--file",
        type=_path_arg,
        default=None,
        help="bestand voor de downloadmeting (standaard: grootste <= 256 KiB in "
        "/ext/infrared of /ext/subghz)",
    )
    p_test.add_argument(
        "--rounds", type=_rounds_arg, default=10, help="aantal pings voor de latency (standaard 10)"
    )
    p_test.add_argument(
        "--read-rounds",
        type=_rounds_arg,
        default=1,
        help="aantal keer downloaden van het testbestand (standaard 1)",
    )
    p_test.add_argument(
        "--compare-usb", action="store_true", help="dezelfde metingen over USB ernaast zetten"
    )
    p_test.add_argument("--json", action="store_true", help="machineleesbare uitvoer")


def run(args: argparse.Namespace, settings: Settings) -> int | None:
    """Voer ble-test uit; None als `args.command` iets anders is."""
    if args.command == "ble-test":
        return cmd_test(args, settings)
    return None


# --- meten -------------------------------------------------------------------------------


def _make_device(settings: Settings) -> FlipperLike:
    from .core.devices.flipper import FlipperDevice

    return FlipperDevice.from_settings(settings)


def _pick_test_file(device: FlipperLike) -> tuple[str, int] | None:
    """Grootste bestand <= 256 KiB in /ext/infrared of /ext/subghz (niet recursief)."""
    best: tuple[str, int] | None = None
    for directory in TEST_DIRS:
        try:
            entries = device.storage_list(directory)
        except ToolboxError:
            continue  # map bestaat niet (nog geen SD-inhoud)
        for entry in entries:
            size = int(entry.get("size", 0))
            if entry.get("type") != "file" or not 0 < size <= AUTO_FILE_MAX_BYTES:
                continue
            path = f"{directory}/{entry.get('name', '')}"
            try:
                validate_flipper_path(path)
            except ValueError:
                continue
            if best is None or size > best[1]:
                best = (path, size)
    return best


def _size_of(device: FlipperLike, path: str) -> int:
    parent, _, name = path.rpartition("/")
    for entry in device.storage_list(parent or "/"):
        if entry.get("name") == name:
            if entry.get("type") != "file":
                raise CliError(f"{path} is geen bestand.")
            return int(entry.get("size", 0))
    raise CliError(f"{path} bestaat niet op de Flipper.")


def measure_transport(
    device: FlipperLike,
    label: str,
    *,
    file: str | None,
    rounds: int,
    read_rounds: int,
    clock: Callable[[], float] | None = None,
) -> TransportReport:
    """Meet verbinden, ping-latency en downloadsnelheid van een (al geconfigureerd) device.

    Fouten bij verbinden/system_info gaan door naar de aanroeper; fouten bij de downloadmeting
    komen als notitie in het rapport.
    """
    now = clock or _clock
    report = TransportReport(transport=label)

    # FlipperDevice is lui: de eerste aanroep opent de verbinding en start de RPC-sessie. Die
    # tijd (verbinden + eerste system_info) is wat we "connect" noemen.
    started = now()
    report.info = dict(device.system_info())
    report.connect_s = now() - started

    entries = device.storage_list("/ext")
    report.ext_entries = len(entries)

    _measure_latency(device, report, rounds, now)
    try:
        _measure_download(device, report, file, read_rounds, now)
    except ToolboxError as exc:
        report.notes.append(f"Downloadmeting mislukt: {exc}")
    return report


def _measure_latency(
    device: FlipperLike, report: TransportReport, rounds: int, now: Callable[[], float]
) -> None:
    # Latency: een publieke ping van de device als die er is, anders een kleine RPC-aanroep.
    ping = getattr(device, "ping", None)
    op: Callable[[], object]
    if callable(ping):
        report.ping_method = "rpc_ping"
        op = ping
    else:
        report.ping_method = "storage_list /ext"
        op = partial(device.storage_list, "/ext")

    for _ in range(rounds):
        t0 = now()
        op()
        report.ping_ms.append((now() - t0) * 1000.0)


def _measure_download(
    device: FlipperLike,
    report: TransportReport,
    file: str | None,
    read_rounds: int,
    now: Callable[[], float],
) -> None:
    if file is not None:
        size = _size_of(device, file)
        if size > READ_LIMIT:
            raise CliError(f"{file} is {size} bytes; de test leest hoogstens {READ_LIMIT}.")
        chosen: tuple[str, int] | None = (file, size)
    else:
        chosen = _pick_test_file(device)
    if chosen is None:
        report.notes.append(
            "Geen testbestand (<= 256 KiB) in /ext/infrared of /ext/subghz; geef er een met --file."
        )
        return
    report.file, report.file_size = chosen
    limit = READ_LIMIT if file is not None else AUTO_FILE_MAX_BYTES
    for _ in range(read_rounds):
        t0 = now()
        data = device.storage_read(chosen[0], limit)
        elapsed = now() - t0
        report.read_rounds.append({"bytes": float(len(data)), "seconds": elapsed})


# --- ble-test ----------------------------------------------------------------------------


def cmd_test(args: argparse.Namespace, settings: Settings) -> int:
    address: str | None = args.address or settings.flipper_ble_address
    ble_settings = settings.model_copy(
        update={
            "flipper_transport": "ble",
            "flipper_ble_address": address,
            "flipper_ble_adapter": args.adapter,
        }
    )
    if address is None and not args.json:
        _err(
            "Geen adres opgegeven (--address of PITOOLBOX_FLIPPER_BLE_ADDRESS): "
            f"zoeken op naamprefix {ble_settings.flipper_ble_name_prefix!r}."
        )
    reports: list[TransportReport] = []
    device = _make_device(ble_settings)
    try:
        try:
            ble_report = measure_transport(
                device, "ble", file=args.file, rounds=args.rounds, read_rounds=args.read_rounds
            )
        except ToolboxError as exc:
            _err(f"BLE-test mislukt: {exc}")
            return 1
        reports.append(ble_report)
    finally:
        device.close()

    if args.compare_usb:
        usb_report = _measure_usb(args, settings, args.file or ble_report.file)
        if usb_report is not None:
            reports.append(usb_report)

    if args.json:
        payload = {"address": address, "reports": {r.transport: r.to_dict() for r in reports}}
        _out(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _out(_render_reports(address, reports))
    return 0


def _measure_usb(
    args: argparse.Namespace, settings: Settings, file: str | None
) -> TransportReport | None:
    """Dezelfde metingen over USB (--compare-usb); None als dat niet lukt (gemeld)."""
    usb_settings = settings.model_copy(update={"flipper_transport": "usb"})
    usb_device = _make_device(usb_settings)
    try:
        return measure_transport(
            usb_device,
            "usb",
            file=file,
            rounds=args.rounds,
            read_rounds=args.read_rounds,
        )
    except ToolboxError as exc:
        _err(f"USB-vergelijking overgeslagen: {exc}")
        return None
    finally:
        usb_device.close()
