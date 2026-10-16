"""Staat en identiteit van de Alfa-interface: het huidige type (iw) en de driverguard (sysfs).

Gedeeld door `wifi.AlfaAdapter` en `_wifi_capture.CaptureManager`.
"""

from __future__ import annotations

from pathlib import Path

from pitoolbox.core.devices._wifi_parse import parse_iw_dev_info
from pitoolbox.core.errors import CommandFailed, InvalidState
from pitoolbox.core.proc import CommandRunner, validate_iface


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
