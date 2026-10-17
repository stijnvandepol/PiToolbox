"""Alfa AWUS036AXML (MediaTek MT7921AUN) aansturen via iw / ip / airodump-ng.

Hardware: de AWUS036AXML is een MT7921AUN en draait op de in-kernel `mt7921u`-driver
(kernel >= 5.18). Monitor mode werkt met `iw dev <if> set type monitor`. We gebruiken bewust
NIET de monitor-flag `active` (`iw dev <if> set monitor active`): mt76-issue #839 meldt dat de
driver daarmee vastloopt. Ook zetten we geen andere monitor-flags.

Bronnen voor de uitvoerformaten:
- iw `scan.c` (BSS-header, freq/signal/SSID/DS Parameter set/primary channel/RSN/WPA):
  https://git.kernel.org/pub/scm/linux/kernel/git/jberg/iw.git/tree/scan.c
  (gecontroleerd via de gelijke Android-kopie:
  https://android.googlesource.com/platform/external/iw/+/refs/heads/main/scan.c)
- iw `interface.c` (`iw dev <if> info`, `set type`):
  https://android.googlesource.com/platform/external/iw/+/refs/heads/main/interface.c
- airodump-ng opties en CSV-indeling:
  https://www.aircrack-ng.org/doku.php?id=airodump-ng
- scan-frequentie -> kanaal volgens IEEE 802.11 Annex E (2,4 GHz: 2407 + 5n; 5 GHz: 5000 + 5n;
  6 GHz: 5950 + 5n, kanaal 2 = 5935 MHz).

Indeling: de pure parsers staan in `_wifi_parse`, het interfacetype en de driverguard in
`_wifi_iface` en de captures in `_wifi_capture`; deze module heeft de `AlfaAdapter` en is het
publieke importpad voor alles.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pitoolbox.core.devices._wifi_capture import CaptureManager
from pitoolbox.core.devices._wifi_iface import SYSFS_NET, iface_driver, read_mode, require_driver
from pitoolbox.core.devices._wifi_parse import (
    freq_to_channel,
    parse_airodump_csv,
    parse_iw_dev_info,
    parse_iw_scan,
)
from pitoolbox.core.errors import CommandFailed, InvalidState
from pitoolbox.core.proc import (
    CommandRunner,
    ProcHandle,
    stop_process,
    validate_iface,
)

if TYPE_CHECKING:
    from pitoolbox.config import Settings

__all__ = [
    "AlfaAdapter",
    "CaptureManager",
    "freq_to_channel",
    "iface_driver",
    "parse_airodump_csv",
    "parse_iw_dev_info",
    "parse_iw_scan",
    "read_mode",
    "require_driver",
]

MODES = ("managed", "monitor")
BANDS = ("bg", "a", "abg")


def _filter_band(networks: list[dict[str, Any]], band: str) -> list[dict[str, Any]]:
    if band == "abg":
        return networks

    def keep(n: dict[str, Any]) -> bool:
        f: int | None = n.get("freq_mhz")
        if f is None:
            return True
        return f < 3000 if band == "bg" else 4900 <= f < 5925

    return [n for n in networks if keep(n)]


def _hint(stderr: str) -> str:
    """Korte uitleg bij bekende fouten van iw/ip, voor het scherm van de Flipper en de chat.

    "Operation not permitted" is EPERM (-1) van nl80211: het proces mist CAP_NET_ADMIN. Onder
    systemd geeft pitoolbox-mcp.service die als ambient capability (deploy/install.sh); bij
    `uv run` vanuit je eigen shell heb je hem niet.
    """
    low = stderr.lower()
    if "busy" in low:
        return " (adapter bezig; probeer opnieuw)"
    if "not permitted" in low:
        return " (geen rechten: CAP_NET_ADMIN nodig, die krijgt de service via install.sh)"
    return ""


class AlfaAdapter:
    """Wijzigt en leest de staat van de Alfa-interface. Raakt NOOIT de uplink-interface aan."""

    def __init__(  # noqa: PLR0913 keyword-only afhankelijkheden (werkt als een struct)
        self,
        iface: str,
        uplink_iface: str,
        runner: CommandRunner,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        tmp_root: Path | None = None,
        expected_driver: str | None = None,
        sysfs_net: Path = SYSFS_NET,
    ) -> None:
        self._iface = iface
        self._uplink = uplink_iface
        self._expected_driver = expected_driver
        self._sysfs_net = sysfs_net
        self._runner = runner
        self._clock = clock
        self._sleep = sleep
        self._tmp_root = tmp_root
        self._lock = threading.Lock()  # serialiseert set_mode/scan op de ene adapter

    @classmethod
    def from_settings(cls, settings: Settings, runner: CommandRunner) -> AlfaAdapter:
        return cls(
            settings.alfa_iface,
            settings.uplink_iface,
            runner,
            expected_driver=settings.alfa_driver or None,
        )

    @property
    def iface(self) -> str:
        return self._iface

    def _guarded_iface(self) -> str:
        """Harde guard: de uplink (SSH/internet) mag nooit omgezet of gescand worden."""
        if self._iface == self._uplink:
            raise InvalidState(
                f"alfa_iface {self._iface!r} is gelijk aan uplink_iface; weiger de uplink aan te "
                "raken (stel PITOOLBOX_ALFA_IFACE en PITOOLBOX_UPLINK_IFACE verschillend in)"
            )
        iface = validate_iface(self._iface)
        require_driver(iface, self._expected_driver, self._sysfs_net)
        return iface

    def get_mode(self) -> str:
        return read_mode(self._runner, self._iface)

    def set_mode(self, mode: str) -> dict[str, Any]:
        """managed/monitor: down, `iw dev X set type`, up, en controleer via `iw dev X info`."""
        if mode not in MODES:
            raise InvalidState(f"ongeldige mode {mode!r}; kies uit {MODES}")
        iface = self._guarded_iface()
        with self._lock:
            self._must_ok(["ip", "link", "set", "dev", iface, "down"])
            try:
                self._must_ok(["iw", "dev", iface, "set", "type", mode])
            except CommandFailed:
                # Interface weer omhoog zodat we hem niet 'down' achterlaten.
                self._runner.run(["ip", "link", "set", "dev", iface, "up"], timeout=10)
                raise
            self._must_ok(["ip", "link", "set", "dev", iface, "up"])
            actual = read_mode(self._runner, iface)
        if actual != mode:
            raise CommandFailed(f"{iface} staat na set type op {actual!r}, verwacht {mode!r}")
        return {"iface": iface, "mode": actual}

    def _must_ok(self, argv: list[str]) -> None:
        res = self._runner.run(argv, timeout=15)
        if res.returncode != 0:
            msg = res.stderr.strip() or str(res.returncode)
            raise CommandFailed(f"{' '.join(argv)} faalde: {msg}{_hint(msg)}")

    def scan(self, method: str = "iw", duration_s: int = 10, band: str = "abg") -> dict[str, Any]:
        """Passieve scan. iw: managed mode; airodump: monitor mode (duurt `duration_s`)."""
        if method not in ("iw", "airodump"):
            raise InvalidState(f"onbekende scanmethode {method!r}")
        if band not in BANDS:
            raise InvalidState(f"onbekende band {band!r}; kies uit {BANDS}")
        iface = self._guarded_iface()
        mode = read_mode(self._runner, iface)
        if method == "iw":
            if mode != "managed":
                raise InvalidState(
                    f"scan met 'iw' vereist managed mode, {iface} staat op {mode!r}; "
                    "gebruik alfa_set_mode(managed) of kies method 'airodump'"
                )
            with self._lock:
                return self._scan_iw(iface, band)
        if mode != "monitor":
            raise InvalidState(
                f"scan met 'airodump' vereist monitor mode, {iface} staat op {mode!r}; "
                "gebruik eerst alfa_set_mode(monitor) of kies method 'iw'"
            )
        with self._lock:
            return self._scan_airodump(iface, duration_s, band)

    def _scan_iw(self, iface: str, band: str) -> dict[str, Any]:
        res = self._runner.run(["iw", "dev", iface, "scan"], timeout=30)
        if res.returncode != 0:
            msg = res.stderr.strip() or str(res.returncode)
            raise CommandFailed(f"iw dev {iface} scan faalde: {msg}{_hint(msg)}")
        nets = _filter_band(parse_iw_scan(res.stdout), band)
        return {"method": "iw", "band": band, "aps": nets, "stations": []}

    def _scan_airodump(self, iface: str, duration_s: int, band: str) -> dict[str, Any]:
        tmp = Path(tempfile.mkdtemp(prefix="pitoolbox-scan-", dir=self._tmp_root))
        try:
            argv = [
                "airodump-ng",
                "--output-format",
                "csv",
                "--write-interval",
                "1",
                "-w",
                str(tmp / "scan"),
                "--band",
                band,
                iface,
            ]
            handle = self._runner.spawn(argv, tmp)
            self._wait_for(handle, float(duration_s))
            stop_process(handle)
            csv_files = sorted(tmp.glob("scan-??.csv"))
            if not csv_files:
                raise CommandFailed("airodump-ng schreef geen CSV-bestand")
            text = csv_files[0].read_text(encoding="utf-8", errors="replace")
            parsed = parse_airodump_csv(text)
            return {
                "method": "airodump",
                "band": band,
                "aps": parsed["aps"],
                "stations": parsed["stations"],
            }
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _wait_for(self, handle: ProcHandle, duration_s: float) -> None:
        deadline = self._clock() + duration_s
        while True:
            rc = handle.poll()
            if rc is not None:
                tail = _read_tail(handle.stderr_path)
                raise CommandFailed(f"airodump-ng stopte voortijdig (rc={rc}): {tail}")
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(0.5, remaining))


def _read_tail(path: Path | None, limit: int = 400) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-limit:]
    except OSError:
        return ""
