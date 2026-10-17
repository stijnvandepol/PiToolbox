"""``remote screen`` en ``remote press``, subcommando's van ``pitoolbox-flipper``: de Flipper op
afstand bedienen tijdens de ontwikkeling (registratie in flipper_ui_cli.py).

- ``remote screen [--png PAD] [--scale N]``: één schermbeeld als tekst (halve blokken), met
  ``--png`` ook als PNG-bestand.
- ``remote press KNOP [--long] [--times N]``: KNOP is up, down, left, right, ok of back.

**Alleen voor ontwikkeling**, nooit een tool (zie core/devices/_flipper_remote.py). Het device
controleert vóór elke klik dat er geen app open is, of alleen de PiToolbox-app die dít proces
startte; anders weigert het. Een app die een ander proces startte (de service), herkent dit
proces niet als de zijne. Drukken binnen de PiToolbox-app kan dus alleen in het proces dat hem
opende: ``ui-test --confirm-check --auto``. Hier: navigeren vanaf het bureaublad, bijv. naar
Apps > Tools > PiToolbox voor de handmatige start, en kijken wat er op het scherm staat.
**Eén eigenaar:** stop eerst ``pitoolbox-mcp``/``pitoolbox-agent`` als die de Flipper hebben.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import flipper_ui_harness as harness
from . import flipper_ui_install
from .config import Settings
from .core.devices._flipper_screen import PNG_SCALE_MAX, render_png, render_text
from .flipper_cli_common import CliError
from .flipper_ui_harness import _device_settings, _err, _out

if TYPE_CHECKING:
    from .core.devices.flipper import FlipperDevice

# Gelijk aan flipper.REMOTE_KEYS (een test bewaakt dat); hier letterlijk, zodat het parsen van
# de argumenten de devicelaag niet hoeft te laden.
KEYS = ("up", "down", "left", "right", "ok", "back")
TIMES_MAX = 20
# Tussen twee klikken: de app moet het vorige scherm eerst tekenen (menu's animeren kort).
PRESS_GAP_S = 0.3


def _bounded_int(low: int, high: int) -> Any:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"geen geheel getal: {value!r}") from exc
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"moet tussen {low} en {high} liggen")
        return number

    return parse


def add_parsers(sub: Any, parents: list[argparse.ArgumentParser], settings: Settings) -> None:
    """Voeg ``remote screen`` en ``remote press`` toe (parents: --adapter en --address)."""
    transport = argparse.ArgumentParser(add_help=False)
    transport.add_argument(
        "--transport",
        choices=("auto", "usb", "ble"),
        default=settings.flipper_transport,
        help="standaard PITOOLBOX_FLIPPER_TRANSPORT",
    )
    remote = sub.add_parser(
        "remote", help="ontwikkelaarsmodus: scherm lezen en knoppen drukken op afstand"
    )
    remote_sub = remote.add_subparsers(dest="remote_command", required=True, metavar="actie")
    p_screen = remote_sub.add_parser(
        "screen", parents=[*parents, transport], help="één schermbeeld als tekst (of PNG)"
    )
    p_screen.add_argument("--png", type=Path, default=None, metavar="PAD", help="ook als PNG")
    p_screen.add_argument(
        "--scale",
        type=_bounded_int(1, PNG_SCALE_MAX),
        default=4,
        help=f"PNG-pixels per schermpixel (1..{PNG_SCALE_MAX}, standaard 4)",
    )
    p_press = remote_sub.add_parser(
        "press",
        parents=[*parents, transport],
        help="knop indrukken (alleen zonder open app of in de eigen PiToolbox-app)",
    )
    p_press.add_argument("key", choices=KEYS, metavar="KNOP", help=", ".join(KEYS))
    p_press.add_argument("--long", action="store_true", help="lang indrukken (LONG)")
    p_press.add_argument(
        "--times",
        type=_bounded_int(1, TIMES_MAX),
        default=1,
        help=f"zo vaak achter elkaar (1..{TIMES_MAX}, standaard 1)",
    )


def run(args: argparse.Namespace, settings: Settings) -> int | None:
    """Voer een remote-commando uit; None als `args.command` geen remote-commando is."""
    if args.command != "remote":
        return None
    device = flipper_ui_install._make_device(_device_settings(settings, args, args.transport))
    try:
        if args.remote_command == "screen":
            return _screen(device, args.png, args.scale)
        return _press(device, args.key, long=args.long, times=args.times)
    finally:
        device.close()


def _screen(device: FlipperDevice, png: Path | None, scale: int) -> int:
    frame = device.gui_screen()
    pixels = frame.pixels()
    _out(render_text(pixels))
    if frame.orientation != "HORIZONTAL":
        _out(f"Stand: {frame.orientation} (getoond zoals het framebuffer, niet gedraaid).")
    if png is not None:
        try:
            png.write_bytes(render_png(pixels, scale))
        except OSError as exc:
            raise CliError(f"PNG niet geschreven naar {png}: {exc.strerror or exc}") from exc
        _out(f"PNG opgeslagen: {png}")
    return 0


def _press(device: FlipperDevice, key: str, *, long: bool, times: int) -> int:
    from .core.devices.flipper import RemoteInputRefused

    label = key.upper() + (" (lang)" if long else "")
    _out("Invoer op afstand (alleen voor ontwikkeling); elke klik staat als WARNING in de log.")
    for number in range(1, times + 1):
        if number > 1:
            harness._sleep(PRESS_GAP_S)
        try:
            device.gui_press(key, long=long)
        except RemoteInputRefused as exc:
            _err(f"Geweigerd bij klik {number}/{times}: {exc}")
            return 1
        _out(f"{label} gedrukt ({number}/{times})")
    return 0
