"""Pure parsers voor de uitvoer van iw en airodump-ng (zonder I/O).

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

import re
from dataclasses import dataclass
from typing import Any

from pitoolbox.core.scope import normalize_mac


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
        else:
            _apply_iface_field(info, s)
    return info


def _apply_iface_field(info: dict[str, Any], s: str) -> None:
    """Eén (gestripte) regel binnen een interface-blok van `iw dev <if> info`."""
    if m := re.match(r"^type (\S+)$", s):
        info["mode"] = m.group(1)
    elif m := re.match(r"^addr (\S+)$", s):
        info["addr"] = m.group(1).upper()
    elif m := re.match(r"^channel (\d+) \((\d+)(?:\.\d+)? MHz\)", s):
        info["channel"] = int(m.group(1))
        info["freq_mhz"] = int(m.group(2))


_BSS_RE = re.compile(r"^BSS ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


@dataclass
class _BssFields:
    """Wat `_parse_bss_block` uit de regels van één BSS-blok verzamelt."""

    freq: int | None = None
    signal: float | None = None
    ssid: str = ""
    ds_channel: int | None = None
    ht_primary: int | None = None
    privacy: bool = False
    has_rsn: bool = False
    has_wpa: bool = False
    rsn_suites: str = ""
    wpa_suites: str = ""


def _security_from_ie(bss: _BssFields) -> str:
    if bss.has_rsn:
        if "802.1X" in bss.rsn_suites:
            return "wpa2-enterprise"
        has_sae = "SAE" in bss.rsn_suites
        has_psk = "PSK" in bss.rsn_suites
        if has_sae and has_psk:
            return "wpa2/wpa3"
        if has_sae:
            return "wpa3"
        if "OWE" in bss.rsn_suites:
            return "owe"
        if bss.has_wpa:
            return "wpa/wpa2"
        return "wpa2"
    if bss.has_wpa:
        return "wpa-enterprise" if "802.1X" in bss.wpa_suites else "wpa"
    return "wep" if bss.privacy else "open"


def _apply_bss_field(bss: _BssFields, key: str, rest: str) -> None:
    """Eén regel op het hoogste niveau van een BSS-blok: `\\t<key>: <rest>`."""
    match key:
        case "freq":
            if m := re.match(r"^(\d+)(?:\.\d+)?$", rest):
                bss.freq = int(m.group(1))
        case "signal":
            if m := re.match(r"^(-?\d+(?:\.\d+)?)\s*dBm$", rest):
                bss.signal = float(m.group(1))
        case "SSID":
            bss.ssid = _unescape_ssid(rest)
        case "DS Parameter set":
            if m := re.match(r"^channel (\d+)$", rest):
                bss.ds_channel = int(m.group(1))
        case "capability":
            bss.privacy = "Privacy" in rest.split("(")[0].split()
        case "RSN":
            bss.has_rsn = True
        case "WPA":
            bss.has_wpa = True


def _apply_bss_section_line(bss: _BssFields, section: str, s: str) -> None:
    """Eén regel binnen de sectie `section` (zonder inspringing en opsommingsteken)."""
    if section == "HT operation" and (m := re.match(r"^primary channel: (\d+)$", s)):
        bss.ht_primary = int(m.group(1))
        return
    if not s.startswith("Authentication suites:"):
        return
    suites = s.partition(":")[2]
    if section == "RSN":
        bss.rsn_suites = suites
    elif section == "WPA":
        bss.wpa_suites = suites


def _parse_bss_block(bssid: str, associated: bool, lines: list[str]) -> dict[str, Any]:
    bss = _BssFields()
    section = ""
    for line in lines:
        if not line.strip():
            continue
        top_level = line.startswith("\t") and not line.startswith(("\t\t", "\t "))
        if top_level:
            key, _, rest = line[1:].partition(":")
            section = key.strip()
            _apply_bss_field(bss, section, rest.strip())
            continue
        # Regel van een geneste sectie (begint met twee tabs of tab+spatie).
        _apply_bss_section_line(bss, section, line.strip().lstrip("*").strip())

    freq = bss.freq
    hidden = not bss.ssid or all(c == "\x00" for c in bss.ssid)
    channel = (
        bss.ds_channel or bss.ht_primary or (freq_to_channel(freq) if freq is not None else None)
    )
    return {
        "bssid": bssid,
        "ssid": "" if hidden else bss.ssid,
        "hidden": hidden,
        "freq_mhz": freq,
        "channel": channel,
        "signal_dbm": bss.signal,
        "security": _security_from_ie(bss),
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
