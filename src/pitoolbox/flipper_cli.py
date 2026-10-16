"""Beheer-CLI voor de Flipper over Bluetooth LE (entry point ``pitoolbox-flipper``).

Subcommando's:
- ``ble-scan``: Flippers in de buurt met adres, naam, signaalsterkte en koppelstatus;
- ``ble-pair <adres>``: koppelen; de Flipper toont een 6-cijferige code die je hier invoert;
- ``ble-unpair <adres>``: koppeling (bonding) uit BlueZ verwijderen;
- ``ble-list``: gekoppelde Flippers volgens BlueZ;
- ``ble-test``: de fase-3-meetscript: verbinden, system_info, opslaglijst, ping-latency en
  downloadsnelheid, optioneel naast USB.

Elk subcommando kent ``--adapter hciN`` (standaard PITOOLBOX_FLIPPER_BLE_ADAPTER, anders de
standaardadapter van BlueZ), handig als er meerdere Bluetooth-adapters zijn.

Het koppelen praat rechtstreeks met BlueZ over D-Bus (zie core/devices/_ble_pairing.py); er wordt
nergens een shell of ``bluetoothctl`` gestart. De Flipper-BLE-transportlaag zelf staat in
core/devices/_flipper_ble.py en wordt pas bij gebruik geimporteerd.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import re
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from .config import Settings
from .core.devices import _ble_pairing as ble_pairing
from .core.errors import ToolboxError
from .core.scope import normalize_mac, validate_flipper_path

# Vervangbaar in tests.
_input = input
_clock = time.perf_counter

PASSKEY_ATTEMPTS = 3
PASSKEY_RE = re.compile(r"\d{6}")  # de Flipper toont "%06lu" (bt.c, "Pairing code")
# Grens voor het automatisch gekozen testbestand; een expliciete --file mag tot READ_LIMIT.
AUTO_FILE_MAX_BYTES = 256 * 1024
READ_LIMIT = 1024 * 1024
TEST_DIRS = ("/ext/infrared", "/ext/subghz")
# Sleutels uit DeviceInfo die we tonen (geen hardware_uid: dat is een uniek serienummer).
INFO_KEYS = (
    "hardware_model",
    "hardware_name",
    "firmware_version",
    "firmware_origin_fork",
    "firmware_branch",
    "firmware_commit",
    "protobuf_version_major",
    "protobuf_version_minor",
)


class CliError(ToolboxError):
    """Fout met een bruikbare melding voor de gebruiker (exitcode 1)."""

    code = "cli_error"


class FlipperLike(Protocol):
    """Wat het testscript van een Flipper nodig heeft (FlipperDevice voldoet hieraan)."""

    def system_info(self) -> dict[str, str]: ...

    def storage_list(self, path: str) -> list[dict[str, Any]]: ...

    def storage_read(self, path: str, max_bytes: int) -> bytes: ...

    def status(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


# --- argumentvalidatie -------------------------------------------------------------------


def _mac_arg(value: str) -> str:
    try:
        return normalize_mac(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ongeldig MAC-adres {value!r} (verwacht AA:BB:CC:DD:EE:FF)"
        ) from exc


def _path_arg(value: str) -> str:
    try:
        return validate_flipper_path(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ongeldig Flipper-pad {value!r} (alleen /ext/... of /int/...)"
        ) from exc


def _adapter_arg(value: str) -> str | None:
    try:
        return ble_pairing.normalize_adapter(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen getal: {value!r}") from exc
    if not 0 < number <= 3600:
        raise argparse.ArgumentTypeError("moet groter dan 0 en hoogstens 3600 seconden zijn")
    return number


def _rounds_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"geen geheel getal: {value!r}") from exc
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("moet tussen 1 en 1000 liggen")
    return number


def _build_parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pitoolbox-flipper",
        description="Flipper Zero over Bluetooth LE koppelen en testen.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="commando")
    prefix_default = settings.flipper_ble_name_prefix
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--adapter",
        type=_adapter_arg,
        default=settings.flipper_ble_adapter,
        help="Bluetooth-adapter, bijv. hci1 (standaard PITOOLBOX_FLIPPER_BLE_ADAPTER, anders "
        "de standaardadapter van BlueZ)",
    )

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
    return parser


# --- hulpfuncties voor uitvoer -----------------------------------------------------------


def _format_table(rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    return "\n".join(lines)


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


# --- ble-test ----------------------------------------------------------------------------


@dataclass
class TransportReport:
    transport: str
    connect_s: float | None = None
    info: dict[str, str] = field(default_factory=dict)
    ext_entries: int | None = None
    ping_method: str | None = None
    ping_ms: list[float] = field(default_factory=list)
    file: str | None = None
    file_size: int | None = None
    read_rounds: list[dict[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def ping_stats(self) -> dict[str, float] | None:
        if not self.ping_ms:
            return None
        return {
            "n": float(len(self.ping_ms)),
            "min_ms": min(self.ping_ms),
            "median_ms": statistics.median(self.ping_ms),
            "mean_ms": statistics.fmean(self.ping_ms),
            "max_ms": max(self.ping_ms),
        }

    def download_stats(self) -> dict[str, float] | None:
        if not self.read_rounds:
            return None
        total_bytes = sum(r["bytes"] for r in self.read_rounds)
        total_s = max(sum(r["seconds"] for r in self.read_rounds), 1e-9)
        return {
            "bytes_per_round": total_bytes / len(self.read_rounds),
            "seconds": total_s,
            "bytes_per_s": total_bytes / total_s,
            "kib_per_s": total_bytes / total_s / 1024,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "connect_s": self.connect_s,
            "info": self.info,
            "ext_entries": self.ext_entries,
            "ping": {
                "method": self.ping_method,
                "samples_ms": self.ping_ms,
                **(self.ping_stats() or {}),
            },
            "download": {
                "file": self.file,
                "size": self.file_size,
                "rounds": self.read_rounds,
                **(self.download_stats() or {}),
            },
            "notes": self.notes,
        }


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

    try:
        if file is not None:
            size = _size_of(device, file)
            if size > READ_LIMIT:
                raise CliError(f"{file} is {size} bytes; de test leest hoogstens {READ_LIMIT}.")
            chosen: tuple[str, int] | None = (file, size)
        else:
            chosen = _pick_test_file(device)
        if chosen is None:
            report.notes.append(
                "Geen testbestand (<= 256 KiB) in /ext/infrared of /ext/subghz; "
                "geef er een met --file."
            )
            return report
        report.file, report.file_size = chosen
        limit = READ_LIMIT if file is not None else AUTO_FILE_MAX_BYTES
        for _ in range(read_rounds):
            t0 = now()
            data = device.storage_read(chosen[0], limit)
            elapsed = now() - t0
            report.read_rounds.append({"bytes": float(len(data)), "seconds": elapsed})
    except ToolboxError as exc:
        report.notes.append(f"Downloadmeting mislukt: {exc}")
    return report


def _fmt_ms(stats: dict[str, float] | None) -> str:
    if stats is None:
        return "-"
    return f"{stats['min_ms']:.0f} / {stats['median_ms']:.0f} / {stats['max_ms']:.0f} ms"


def _render_reports(address: str | None, reports: list[TransportReport]) -> str:
    labels = [r.transport.upper() for r in reports]
    rows: list[list[str]] = [["Meting", *labels]]
    rows.append(
        [
            "verbinden + system_info",
            *[f"{r.connect_s:.2f} s" if r.connect_s is not None else "-" for r in reports],
        ]
    )
    rows.append(
        [
            "items in /ext",
            *[str(r.ext_entries) if r.ext_entries is not None else "-" for r in reports],
        ]
    )
    method = next((r.ping_method for r in reports if r.ping_method), "ping")
    rounds = max((len(r.ping_ms) for r in reports), default=0)
    rows.append(
        [f"latency min/med/max ({method}, n={rounds})", *[_fmt_ms(r.ping_stats()) for r in reports]]
    )
    file_label = next((r.file for r in reports if r.file), None)
    size = next((r.file_size for r in reports if r.file_size is not None), None)
    cells = []
    for r in reports:
        stats = r.download_stats()
        cells.append(
            f"{stats['kib_per_s']:.1f} KiB/s ({stats['bytes_per_s']:.0f} B/s)" if stats else "-"
        )
    rows.append([f"download {file_label} ({size} B)" if file_label else "download", *cells])

    lines = [f"pitoolbox BLE-test{f' ({address})' if address else ''}", ""]
    lines.append(_format_table(rows))
    for r in reports:
        interesting = {k: v for k, v in r.info.items() if k in INFO_KEYS}
        if interesting:
            lines.append("")
            lines.append(f"Firmware/hardware ({r.transport.upper()}):")
            lines.extend(f"  {k} = {v}" for k, v in interesting.items())
        for note in r.notes:
            lines.append(f"Let op ({r.transport.upper()}): {note}")
    return "\n".join(lines)


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
        usb_settings = settings.model_copy(update={"flipper_transport": "usb"})
        usb_device = _make_device(usb_settings)
        try:
            try:
                reports.append(
                    measure_transport(
                        usb_device,
                        "usb",
                        file=args.file or ble_report.file,
                        rounds=args.rounds,
                        read_rounds=args.read_rounds,
                    )
                )
            except ToolboxError as exc:
                _err(f"USB-vergelijking overgeslagen: {exc}")
        finally:
            usb_device.close()

    if args.json:
        payload = {"address": address, "reports": {r.transport: r.to_dict() for r in reports}}
        _out(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _out(_render_reports(address, reports))
    return 0


# --- main --------------------------------------------------------------------------------


def _load_settings() -> Settings:
    return Settings.from_env()


def main(argv: Sequence[str] | None = None) -> int:
    settings = _load_settings()
    args = _build_parser(settings).parse_args(argv)
    try:
        if args.command == "ble-scan":
            return cmd_scan(args)
        if args.command == "ble-pair":
            return cmd_pair(args)
        if args.command == "ble-unpair":
            return cmd_unpair(args)
        if args.command == "ble-list":
            return cmd_list(args)
        if args.command == "ble-test":
            return cmd_test(args, settings)
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
