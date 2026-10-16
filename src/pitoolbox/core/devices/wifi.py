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
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pitoolbox.core.errors import CommandFailed, InvalidState
from pitoolbox.core.proc import (
    CommandRunner,
    ProcHandle,
    stop_process,
    validate_iface,
)
from pitoolbox.core.scope import normalize_mac

if TYPE_CHECKING:
    from pitoolbox.config import Settings

log = logging.getLogger(__name__)

MODES = ("managed", "monitor")
BANDS = ("bg", "a", "abg")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_CONCURRENT_CAPTURES = 2


# --------------------------------------------------------------------------------------
# Pure parsers
# --------------------------------------------------------------------------------------


def freq_to_channel(mhz: int) -> int | None:
    """Frequentie (MHz) -> kanaalnummer voor 2,4/5/6 GHz; None als onbekend."""
    if mhz == 2484:
        return 14
    if 2412 <= mhz <= 2472 and (mhz - 2407) % 5 == 0:
        return (mhz - 2407) // 5
    if 5160 <= mhz <= 5885 and (mhz - 5000) % 5 == 0:
        return (mhz - 5000) // 5
    if mhz == 5935:  # 6 GHz kanaal 2 is een uitzondering op de 5950+5n-regel
        return 2
    if 5955 <= mhz <= 7115 and (mhz - 5950) % 5 == 0:
        return (mhz - 5950) // 5
    return None


def _unescape_ssid(raw: str) -> str:
    """iw escapet niet-printbare bytes als `\\xNN` (print_ssid_escaped); terug naar tekst."""
    out = bytearray()
    for part in re.split(r"(\\x[0-9a-fA-F]{2})", raw):
        if re.fullmatch(r"\\x[0-9a-fA-F]{2}", part):
            out += bytes.fromhex(part[2:])
        else:
            out += part.encode("utf-8")
    return out.decode("utf-8", errors="replace")


def parse_iw_dev_info(text: str) -> dict[str, Any]:
    """Parse `iw dev <if> info`.

    Formaat (iw interface.c, print_iface_handler): `Interface wlan1`, `\\ttype monitor`,
    `\\tchannel 6 (2437 MHz), width: 20 MHz, ...`, `\\taddr aa:bb:...`.
    Geeft {"iface", "mode", "channel", "freq_mhz", "addr"}; ontbrekende velden zijn None.
    """
    info: dict[str, Any] = {
        "iface": None,
        "mode": None,
        "channel": None,
        "freq_mhz": None,
        "addr": None,
    }
    seen_iface = False
    for line in text.splitlines():
        s = line.strip()
        if m := re.match(r"^Interface (\S+)$", s):
            if seen_iface:
                break  # alleen het eerste interface-blok
            seen_iface = True
            info["iface"] = m.group(1)
        elif m := re.match(r"^type (\S+)$", s):
            info["mode"] = m.group(1)
        elif m := re.match(r"^addr (\S+)$", s):
            info["addr"] = m.group(1).upper()
        elif m := re.match(r"^channel (\d+) \((\d+)(?:\.\d+)? MHz\)", s):
            info["channel"] = int(m.group(1))
            info["freq_mhz"] = int(m.group(2))
    return info


_BSS_RE = re.compile(r"^BSS ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def _security_from_ie(
    has_rsn: bool, has_wpa: bool, privacy: bool, rsn_suites: str, wpa_suites: str
) -> str:
    if has_rsn:
        if "802.1X" in rsn_suites:
            return "wpa2-enterprise"
        has_sae = "SAE" in rsn_suites
        has_psk = "PSK" in rsn_suites
        if has_sae and has_psk:
            return "wpa2/wpa3"
        if has_sae:
            return "wpa3"
        if "OWE" in rsn_suites:
            return "owe"
        if has_wpa:
            return "wpa/wpa2"
        return "wpa2"
    if has_wpa:
        return "wpa-enterprise" if "802.1X" in wpa_suites else "wpa"
    return "wep" if privacy else "open"


def _parse_bss_block(bssid: str, associated: bool, lines: list[str]) -> dict[str, Any]:
    freq: int | None = None
    signal: float | None = None
    ssid = ""
    ds_channel: int | None = None
    ht_primary: int | None = None
    privacy = False
    has_rsn = has_wpa = False
    rsn_suites = wpa_suites = ""
    section = ""

    for line in lines:
        if not line.strip():
            continue
        top_level = line.startswith("\t") and not line.startswith(("\t\t", "\t "))
        if top_level:
            key, _, rest = line[1:].partition(":")
            key = key.strip()
            rest = rest.strip()
            section = key
            if key == "freq":
                if m := re.match(r"^(\d+)(?:\.\d+)?$", rest):
                    freq = int(m.group(1))
            elif key == "signal":
                if m := re.match(r"^(-?\d+(?:\.\d+)?)\s*dBm$", rest):
                    signal = float(m.group(1))
            elif key == "SSID":
                ssid = _unescape_ssid(rest)
            elif key == "DS Parameter set":
                if m := re.match(r"^channel (\d+)$", rest):
                    ds_channel = int(m.group(1))
            elif key == "capability":
                privacy = "Privacy" in rest.split("(")[0].split()
            elif key == "RSN":
                has_rsn = True
            elif key == "WPA":
                has_wpa = True
            continue
        # Regel van een geneste sectie (begint met twee tabs of tab+spatie).
        s = line.strip().lstrip("*").strip()
        if section == "HT operation" and (m := re.match(r"^primary channel: (\d+)$", s)):
            ht_primary = int(m.group(1))
        elif s.startswith("Authentication suites:"):
            suites = s.partition(":")[2]
            if section == "RSN":
                rsn_suites = suites
            elif section == "WPA":
                wpa_suites = suites

    hidden = not ssid or all(c == "\x00" for c in ssid)
    channel = ds_channel or ht_primary or (freq_to_channel(freq) if freq is not None else None)
    return {
        "bssid": bssid,
        "ssid": "" if hidden else ssid,
        "hidden": hidden,
        "freq_mhz": freq,
        "channel": channel,
        "signal_dbm": signal,
        "security": _security_from_ie(has_rsn, has_wpa, privacy, rsn_suites, wpa_suites),
        "associated": associated,
    }


def parse_iw_scan(text: str) -> list[dict[str, Any]]:
    """Parse `iw dev <if> scan` (scan.c: `BSS <mac>(on <if>)`, `\\tfreq: 2437`, `\\tsignal: ...`).

    De frequentie staat als `2437` (oudere iw) of `2437.0` (nieuwere iw met freq-offset);
    beide worden naar int omgezet.
    """
    result: list[dict[str, Any]] = []
    current: tuple[str, bool, list[str]] | None = None
    for line in text.splitlines():
        if m := _BSS_RE.match(line):
            if current is not None:
                result.append(_parse_bss_block(*current))
            current = (m.group(1).upper(), "-- associated" in line, [])
        elif current is not None:
            current[2].append(line)
    if current is not None:
        result.append(_parse_bss_block(*current))
    return result


def _int_or_none(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


def _norm_privacy(privacy: str) -> str:
    p = privacy.strip().upper()
    if p in ("", "OPN"):
        return "open"
    return "/".join(p.lower().split())


def _signal_or_none(value: str) -> int | None:
    v = _int_or_none(value)
    return None if v is None or v == -1 else v


def parse_airodump_csv(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse de airodump-ng CSV: twee secties gescheiden door een lege regel.

    AP-kop: `BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher,
    Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key`.
    Station-kop: `Station MAC, First time seen, Last time seen, Power, # packets, BSSID,
    Probed ESSIDs`. (https://www.aircrack-ng.org/doku.php?id=airodump-ng)
    Een ESSID kan komas bevatten; daarom gebruiken we het ID-length-veld om hem uit te knippen.
    """
    aps: list[dict[str, Any]] = []
    stations: list[dict[str, Any]] = []
    section = ""
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip("\r\n")
        if not line.strip():
            continue
        if line.startswith("BSSID,"):
            section = "ap"
            continue
        if line.startswith("Station MAC,"):
            section = "station"
            continue
        if section == "ap":
            ap = _parse_airodump_ap(line)
            if ap is not None:
                aps.append(ap)
        elif section == "station":
            st = _parse_airodump_station(line)
            if st is not None:
                stations.append(st)
    return {"aps": aps, "stations": stations}


def _parse_airodump_ap(line: str) -> dict[str, Any] | None:
    f = line.split(",", 13)
    if len(f) < 14:
        return None
    try:
        bssid = normalize_mac(f[0])
    except ValueError:
        return None
    id_len = _int_or_none(f[12]) or 0
    rest = f[13]
    # airodump schrijft ", <essid>, <key>": spatie, ESSID van id_len tekens, dan ", key".
    body = rest[1:] if rest.startswith(" ") else rest
    essid = body[:id_len] if id_len > 0 else ""
    if id_len == 0 or all(c == "\x00" for c in essid):
        essid = ""
    channel = _int_or_none(f[3])
    privacy = f[5].strip()
    return {
        "bssid": bssid,
        "ssid": essid,
        "hidden": essid == "",
        "channel": channel if channel is not None and channel > 0 else None,
        "speed": _int_or_none(f[4]),
        "privacy": privacy,
        "cipher": f[6].strip(),
        "auth": f[7].strip(),
        "security": _norm_privacy(privacy),
        "signal_dbm": _signal_or_none(f[8]),
        "beacons": _int_or_none(f[9]),
        "first_seen": f[1].strip(),
        "last_seen": f[2].strip(),
    }


def _parse_airodump_station(line: str) -> dict[str, Any] | None:
    f = line.split(",", 6)
    if len(f) < 6:
        return None
    try:
        mac = normalize_mac(f[0])
    except ValueError:
        return None
    assoc = f[5].strip()
    bssid: str | None
    try:
        bssid = normalize_mac(assoc)
    except ValueError:
        bssid = None  # "(not associated)"
    probes = [p.strip() for p in f[6].split(",") if p.strip()] if len(f) > 6 else []
    return {
        "mac": mac,
        "first_seen": f[1].strip(),
        "last_seen": f[2].strip(),
        "signal_dbm": _signal_or_none(f[3]),
        "packets": _int_or_none(f[4]),
        "bssid": bssid,
        "probes": probes,
    }


# --------------------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------------------


def read_mode(runner: CommandRunner, iface: str) -> str:
    """Huidige interface-type (`managed`, `monitor`, ...) via `iw dev <if> info`."""
    iface = validate_iface(iface)
    res = runner.run(["iw", "dev", iface, "info"], timeout=10)
    if res.returncode != 0:
        raise CommandFailed(f"iw dev {iface} info faalde: {res.stderr.strip() or res.returncode}")
    mode = parse_iw_dev_info(res.stdout)["mode"]
    if not isinstance(mode, str):
        raise CommandFailed(f"kon het type van {iface} niet uit iw-uitvoer halen")
    return mode


def _filter_band(networks: list[dict[str, Any]], band: str) -> list[dict[str, Any]]:
    if band == "abg":
        return networks

    def keep(n: dict[str, Any]) -> bool:
        f: int | None = n.get("freq_mhz")
        if f is None:
            return True
        return f < 3000 if band == "bg" else 4900 <= f < 5925

    return [n for n in networks if keep(n)]


SYSFS_NET = Path("/sys/class/net")


def iface_driver(iface: str, sysfs_net: Path = SYSFS_NET) -> str | None:
    """Kerneldriver van een netwerkinterface, of None als die niet te bepalen is.

    `/sys/class/net/<iface>/device/driver` is een symlink naar de driver; de naam is de basename
    (Documentation/ABI/stable/sysfs-class-net en sysfs-bus-*, "driver"-link). Dit is dezelfde
    waarde als udev's ID_NET_DRIVER (gemeten op de Pi: wlan1 -> mt7921u, wlan0 -> brcmfmac).
    """
    try:
        target = (sysfs_net / validate_iface(iface) / "device" / "driver").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return target.name


def require_driver(iface: str, expected: str | None, sysfs_net: Path = SYSFS_NET) -> None:
    """Tweede guard naast de naamvergelijking: alleen de adapter met de verwachte driver.

    Een naamvergelijking beschermt de uplink niet als de interfacenamen ooit verwisseld raken
    (bijv. de .link-bestanden uit fase 5 ontbreken). Met de driver weten we zeker dat het de
    Alfa is. `expected` None of leeg: controle uit (PITOOLBOX_ALFA_DRIVER leeg).
    """
    if not expected:
        return
    actual = iface_driver(iface, sysfs_net)
    if actual != expected:
        found = repr(actual) if actual else "onbekend (interface bestaat niet?)"
        raise InvalidState(
            f"{iface} heeft driver {found}, verwacht {expected!r}; weiger hem aan te raken. "
            "Klopt PITOOLBOX_ALFA_IFACE (zie `ls -l /sys/class/net/*/device/driver`)?"
        )


class AlfaAdapter:
    """Wijzigt en leest de staat van de Alfa-interface. Raakt NOOIT de uplink-interface aan."""

    def __init__(
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
            raise CommandFailed(f"{' '.join(argv)} faalde: {res.stderr.strip() or res.returncode}")

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
            hint = " (adapter bezig; probeer opnieuw)" if "busy" in msg.lower() else ""
            raise CommandFailed(f"iw dev {iface} scan faalde: {msg}{hint}")
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


# --------------------------------------------------------------------------------------
# Capture manager
# --------------------------------------------------------------------------------------


class _TimerLike(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], _TimerLike]


def _default_timer_factory(interval: float, fn: Callable[[], None]) -> _TimerLike:
    t = threading.Timer(interval, fn)
    t.daemon = True
    return t


@dataclass
class _Capture:
    capture_id: str
    bssid: str
    channel: int
    directory: Path
    prefix: Path
    handle: ProcHandle
    timer: _TimerLike
    started: datetime
    stops_at: datetime
    started_mono: float

    def info(self, now_mono: float) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "pid": self.handle.pid,
            "bssid": self.bssid,
            "channel": self.channel,
            "dir": str(self.directory),
            "files_prefix": str(self.prefix),
            "started": self.started.isoformat(),
            "stops_at": self.stops_at.isoformat(),
            "elapsed_s": round(now_mono - self.started_mono, 1),
        }


class CaptureManager:
    """Beheert airodump-ng pcap-captures (max. 2 tegelijk) in captures_dir/<sessie>/."""

    def __init__(
        self,
        iface: str,
        uplink_iface: str,
        captures_dir: Path,
        runner: CommandRunner,
        *,
        timer_factory: TimerFactory = _default_timer_factory,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        expected_driver: str | None = None,
        sysfs_net: Path = SYSFS_NET,
    ) -> None:
        self._iface = iface
        self._uplink = uplink_iface
        self._expected_driver = expected_driver
        self._sysfs_net = sysfs_net
        self._captures_dir = captures_dir
        self._runner = runner
        self._timer_factory = timer_factory
        self._clock = clock
        self._now = now
        self._lock = threading.RLock()
        self._active: dict[str, _Capture] = {}
        self._finished: dict[str, dict[str, Any]] = {}
        self._default_dir: Path | None = None

    @classmethod
    def from_settings(cls, settings: Settings, runner: CommandRunner) -> CaptureManager:
        return cls(
            settings.alfa_iface,
            settings.uplink_iface,
            settings.captures_dir,
            runner,
            expected_driver=settings.alfa_driver or None,
        )

    # -- sessiemap ---------------------------------------------------------------------

    def _session_dir(self, session: str | None) -> Path:
        if session is not None:
            if not _SLUG_RE.fullmatch(session):
                raise InvalidState(f"ongeldige sessienaam {session!r} (a-z, 0-9, _ en -; max 32)")
            d = self._captures_dir / session
        else:
            with self._lock:
                if self._default_dir is None:
                    stamp = self._now().strftime("%Y%m%d-%H%M%S")
                    self._default_dir = self._captures_dir / f"{stamp}-{secrets.token_hex(3)}"
                d = self._default_dir
        # 0700: captures kunnen gevoelige data bevatten.
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    # -- start/stop --------------------------------------------------------------------

    def start(
        self, bssid: str, channel: int, duration_s: int, session: str | None = None
    ) -> dict[str, Any]:
        try:
            mac = normalize_mac(bssid)
        except ValueError as exc:
            raise InvalidState(str(exc)) from exc
        if isinstance(channel, bool) or not 1 <= int(channel) <= 196:
            raise InvalidState(f"ongeldig kanaal {channel!r}")
        channel = int(channel)
        if not 10 <= int(duration_s) <= 3600:
            raise InvalidState("duur moet tussen 10 en 3600 seconden liggen")
        duration_s = int(duration_s)

        if self._iface == self._uplink:
            raise InvalidState("alfa_iface is gelijk aan uplink_iface; weiger te capturen")
        iface = validate_iface(self._iface)
        require_driver(iface, self._expected_driver, self._sysfs_net)
        mode = read_mode(self._runner, iface)
        if mode != "monitor":
            raise InvalidState(
                f"capture vereist monitor mode, {iface} staat op {mode!r}; "
                "gebruik eerst alfa_set_mode(monitor)"
            )

        with self._lock:
            self._reap()
            if len(self._active) >= MAX_CONCURRENT_CAPTURES:
                raise InvalidState(f"maximaal {MAX_CONCURRENT_CAPTURES} gelijktijdige captures")
            for cap in self._active.values():
                if cap.bssid == mac:
                    raise InvalidState(f"er loopt al een capture voor {mac} ({cap.capture_id})")
                if cap.channel != channel:
                    # Eén adapter kan maar op één kanaal tegelijk luisteren.
                    raise InvalidState(
                        f"lopende capture {cap.capture_id} staat op kanaal {cap.channel}; "
                        "één adapter kan niet op twee kanalen tegelijk"
                    )
            directory = self._session_dir(session)
            cid = secrets.token_hex(3)
            while cid in self._active or cid in self._finished:
                cid = secrets.token_hex(3)
            prefix = directory / f"cap-{cid}"
            # airodump-ng: --bssid filtert op AP, --channel zet vaste kanaal (geen hoppen),
            # --output-format pcap schrijft <prefix>-01.cap.
            # https://www.aircrack-ng.org/doku.php?id=airodump-ng
            argv = [
                "airodump-ng",
                "--bssid",
                mac,
                "--channel",
                str(channel),
                "--output-format",
                "pcap",
                "--write",
                str(prefix),
                iface,
            ]
            handle = self._runner.spawn(argv, directory, f"cap-{cid}.stderr.log")
            started = self._now()
            stops_at = datetime.fromtimestamp(started.timestamp() + duration_s, UTC)
            timer = self._timer_factory(float(duration_s), lambda: self._auto_stop(cid))
            cap = _Capture(
                cid,
                mac,
                channel,
                directory,
                prefix,
                handle,
                timer,
                started,
                stops_at,
                self._clock(),
            )
            self._active[cid] = cap
            timer.start()
            info = cap.info(self._clock())
        return {
            "capture_id": cid,
            "pid": handle.pid,
            "dir": str(directory),
            "files_prefix": str(prefix),
            "started": started.isoformat(),
            "stops_at": stops_at.isoformat(),
            "bssid": mac,
            "channel": channel,
            "elapsed_s": info["elapsed_s"],
        }

    def _auto_stop(self, capture_id: str) -> None:
        try:
            self.stop(capture_id)
        except InvalidState:
            pass  # al handmatig gestopt
        except Exception:
            log.exception("auto-stop van capture %s faalde", capture_id)

    def stop(self, capture_id: str) -> dict[str, Any]:
        with self._lock:
            cap = self._active.pop(capture_id, None)
            if cap is None:
                prior = self._finished.get(capture_id)
                if prior is not None:
                    return {**prior, "already_stopped": True}
                raise InvalidState(f"onbekende capture_id {capture_id!r}")
        cap.timer.cancel()
        rc = stop_process(cap.handle)  # SIGINT, 5 s wachten, dan kill
        return self._finalize(cap, rc)

    def _finalize(self, cap: _Capture, returncode: int | None) -> dict[str, Any]:
        files = [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted(cap.directory.glob(f"cap-{cap.capture_id}*"))
            if p.is_file() and not p.name.endswith(".stderr.log")
        ]
        result: dict[str, Any] = {
            "capture_id": cap.capture_id,
            "bssid": cap.bssid,
            "dir": str(cap.directory),
            "files": files,
            "duration_s": round(self._clock() - cap.started_mono, 1),
            "returncode": returncode,
        }
        with self._lock:
            self._finished[cap.capture_id] = result
            while len(self._finished) > 20:
                self._finished.pop(next(iter(self._finished)))
        return result

    def _reap(self) -> None:
        """Ruim captures op waarvan het proces zelf gestopt is (bijv. airodump crashte)."""
        with self._lock:
            for cid in [c for c, cap in self._active.items() if cap.handle.poll() is not None]:
                cap = self._active.pop(cid)
                cap.timer.cancel()
                rc = cap.handle.poll()
                res = self._finalize(cap, rc)
                res["ended"] = "process_exited"
                log.warning("capture %s stopte zelf (rc=%s)", cid, rc)

    def stop_all(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._active)
        results: list[dict[str, Any]] = []
        for cid in ids:
            try:
                results.append(self.stop(cid))
            except Exception as exc:
                log.exception("stop_all: capture %s faalde", cid)
                results.append({"capture_id": cid, "error": f"{type(exc).__name__}: {exc}"})
        return results

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reap()
            now = self._clock()
            return [cap.info(now) for cap in self._active.values()]
