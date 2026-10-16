"""Systeemstatus van de Pi: voeding/throttling, uplink-interface en schijfruimte.

Bronnen:
- `vcgencmd get_throttled` (bits 0-3 = nu, bits 16-19 = sinds boot opgetreden):
  https://www.raspberrypi.com/documentation/computers/os.html#vcgencmd
  Uitvoer is `throttled=0x50005`.
- `ip -j` (JSON-uitvoer, 'Output results in JavaScript Object Notation'):
  https://man7.org/linux/man-pages/man8/ip.8.html
  `ip -j addr show dev X` geeft een lijst met o.a. `ifname`, `operstate` en `addr_info`
  (elk met `family`, `local`, `prefixlen`); `ip -j route show default` een lijst met
  `dst`, `gateway`, `dev`.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pitoolbox.core.errors import CommandFailed
from pitoolbox.core.proc import CommandRunner, validate_iface

if TYPE_CHECKING:
    from pitoolbox.config import Settings

log = logging.getLogger(__name__)

_THROTTLED_RE = re.compile(r"throttled=(0x[0-9a-fA-F]+)")

# Bitnummer -> naam (huidig: bits 0-3; opgetreden sinds boot: bits 16-19).
_FLAG_NAMES: dict[int, str] = {
    0: "undervoltage",
    1: "arm_freq_capped",
    2: "throttled",
    3: "soft_temp_limit",
}


def decode_throttled(value: int) -> dict[str, list[str]]:
    current = [name for bit, name in _FLAG_NAMES.items() if value & (1 << bit)]
    occurred = [name for bit, name in _FLAG_NAMES.items() if value & (1 << (bit + 16))]
    return {"current": current, "occurred": occurred}


class SystemProbe:
    def __init__(self, uplink_iface: str, captures_dir: Path, runner: CommandRunner) -> None:
        self._uplink = uplink_iface
        self._captures_dir = captures_dir
        self._runner = runner

    @classmethod
    def from_settings(cls, settings: Settings, runner: CommandRunner) -> SystemProbe:
        return cls(settings.uplink_iface, settings.captures_dir, runner)

    def throttled(self) -> dict[str, Any]:
        try:
            res = self._runner.run(["vcgencmd", "get_throttled"], timeout=5)
        except CommandFailed as exc:
            return {"available": False, "reason": str(exc)}
        if res.returncode != 0:
            reason = res.stderr.strip() or f"returncode {res.returncode}"
            return {"available": False, "reason": reason}
        m = _THROTTLED_RE.search(res.stdout)
        if m is None:
            return {"available": False, "reason": f"onverwachte uitvoer: {res.stdout.strip()!r}"}
        value = int(m.group(1), 16)
        flags = decode_throttled(value)
        return {
            "available": True,
            "raw": m.group(1).lower(),
            "current": flags["current"],
            "occurred": flags["occurred"],
            "undervoltage_now": bool(value & 0x1),
        }

    def _ip_json(self, argv: list[str]) -> list[dict[str, Any]]:
        res = self._runner.run(argv, timeout=5)
        if res.returncode != 0:
            raise CommandFailed(f"{' '.join(argv)} faalde: {res.stderr.strip() or res.returncode}")
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise CommandFailed(f"{' '.join(argv)}: ongeldige JSON") from exc
        if not isinstance(data, list):
            raise CommandFailed(f"{' '.join(argv)}: onverwachte JSON-vorm")
        return [d for d in data if isinstance(d, dict)]

    def uplink(self) -> dict[str, Any]:
        iface = validate_iface(self._uplink)
        links = self._ip_json(["ip", "-j", "addr", "show", "dev", iface])
        if not links:
            raise CommandFailed(f"interface {iface} niet gevonden")
        link = links[0]
        ipv4 = [
            f"{a['local']}/{a['prefixlen']}"
            for a in link.get("addr_info", [])
            if isinstance(a, dict) and a.get("family") == "inet" and "local" in a
        ]
        try:
            routes = self._ip_json(["ip", "-j", "route", "show", "default"])
            is_default: bool | None = any(r.get("dev") == iface for r in routes)
        except CommandFailed:
            is_default = None
        return {
            "iface": iface,
            "operstate": link.get("operstate"),
            "ipv4": ipv4,
            "is_default_route": is_default,
        }

    def disk(self) -> dict[str, Any]:
        path = self._captures_dir
        while not path.exists() and path != path.parent:
            path = path.parent
        usage = shutil.disk_usage(path)
        percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        return {
            "path": str(path),
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": percent,
        }
