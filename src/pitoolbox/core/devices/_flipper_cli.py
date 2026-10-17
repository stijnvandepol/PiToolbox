"""CLI-profielen: vaste commando-templates en uitvoerparsers per Flipper-firmware.

Alleen commando's die uit gevalideerde ints/enums zijn opgebouwd; er wordt nooit vrije tekst
in een CLI-regel geplaatst. Een profiel beschrijft één firmwarefamilie. Profiel "official" is
afgeleid van de officiële flipperzero-firmware (commit 3fb1d517):

* prompt: lib/toolbox/cli/shell/cli_shell_line.c (`"%s>: "` met prompt-prefix; de NFC-subshell
  zet prefix "[" ANSI_FG_GREEN "nfc" ANSI_RESET "]" in applications/main/nfc/cli/nfc_cli.c).
* `subghz rx <freq> <device>`: applications/main/subghz/subghz_cli.c (`subghz_cli_command_rx`,
  SUBGHZ_FREQUENCY_RANGE_STR, printf-formaten).
* `rfid read`: applications/main/lfrfid/lfrfid_cli.c (`lfrfid_cli_read`).
* `nfc` + `scanner -t`: applications/main/nfc/cli/nfc_cli.c en
  commands/nfc_cli_command_scanner.c; `exit` verlaat de subshell (cli_shell.c).

Profiel "momentum" is afgeleid van Next-Flip/Momentum-Firmware, tag mntm-012 (commit
e1784e7418d8). Momentum heeft de CLI-herstructurering van de officiële firmware overgenomen;
vergeleken met official 3fb1d517 zijn deze bestanden byte-voor-byte gelijk:
lib/toolbox/cli/shell/cli_shell_line.c (prompt), lib/toolbox/cli/cli_command.c,
applications/main/lfrfid/lfrfid_cli.c, applications/main/nfc/cli/nfc_cli.c,
applications/main/nfc/cli/nfc_cli_command_processor.c,
applications/main/nfc/cli/commands/nfc_cli_command_scanner.c en
commands/helpers/nfc_cli_scanner.c. Prompts, commando's en alle parsers worden daarom gedeeld.
Het enige verschil dat ertoe doet is het geldige Sub-GHz-bereik (zie MOMENTUM_SUBGHZ_RANGES_HZ).
Kleinere verschillen zonder invloed op pitoolbox: andere protocolnamentabel in
commands/helpers/nfc_cli_format.c (geen "Ntag4xx"/"Type 4 Tag"), een "did you mean"-suggestie
bij onbekende commando's (cli_shell.c) en andere teksten bij TX-weigering (subghz_cli.c).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from pitoolbox.core.errors import DeviceError, UnsupportedFirmware

# --- resultaattypes ----------------------------------------------------------------------


@dataclass(frozen=True)
class SubGhzRxResult:
    tuned_frequency_hz: int
    packets: list[str]
    packet_count: int


@dataclass(frozen=True)
class RfidReadResult:
    found: bool
    protocol: str | None = None
    data_hex: str | None = None
    info: str = ""


@dataclass(frozen=True)
class NfcScanResult:
    found: bool
    protocols: list[list[str]] = field(default_factory=list)


# --- hulpfuncties ------------------------------------------------------------------------


def _norm(text: str) -> str:
    """CRLF/CR naar LF; de parsers werken op regels gescheiden door \\n."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _first_line(text: str) -> str:
    for line in _norm(text).split("\n"):
        if line.strip():
            return line.strip()
    return ""


def _after_marker(text: str, marker: str, what: str) -> str:
    idx = text.find(marker)
    if idx < 0:
        raise DeviceError(f"onverwachte uitvoer van '{what}': {_first_line(text)[:120]!r}")
    return text[idx + len(marker) :]


# --- parsers (gedeeld door official en momentum) -----------------------------------------

_LISTENING_RE = re.compile(r"Listening at frequency: (\d+) device: (\d+)")
_PACKETS_RE = re.compile(r"Packets received (\d+)")
# `subghz rx` print per pakket alleen `subghz_protocol_decoder_base_get_string()`, zonder
# scheidingsteken; splitsen is dus een heuristiek op de decoderformaten in lib/subghz/protocols
# (gecontroleerd in Momentum mntm-012; de officiële firmware gebruikt dezelfde vormen):
# * meestal "%s %dbit\r\n" (princeton.c, nice_flo.c) of "%s %db\r\n" (came_atomo.c,
#   somfy_telis.c, marantec.c): de kop zonder dubbele punt eindigt op het aantal bits;
# * soms "%s\r\n%dbit..." (ws_generic.c `ws_block_generic_get_string` voor weerstations,
#   hormann.c, honeywell.c): de naam staat alleen op de regel vóór "<n>bit".
# Niet te splitsen: een decoder die zonder "\r\n" eindigt (came_atomo.c, ws_generic.c) plakt
# de kop van het volgende pakket aan zijn laatste regel. `packet_count` komt daarom altijd uit
# de firmwareregel "Packets received", nooit uit deze splitsing.
# Hoofdlettergevoelig: de formaten zijn kleine letters, hex-velden zijn "%lX" (dus "12B" is data).
_PACKET_HEADER_RE = re.compile(r"^[^:]*\b\d+\s?(?:bit|b)\b[^:]*$")
_BITS_FIRST_RE = re.compile(r"^\d+\s?bit\b")


def _split_packets(body: str) -> list[str]:
    packets: list[list[str]] = []
    for raw in body.split("\n"):
        # '.' wordt geprint bij elke reset van de ontvanger (subghz_cli_command_rx), dus ook
        # direct achter een pakket dat niet met een regeleinde eindigt.
        line = raw.strip(".").rstrip()
        if not line.strip():
            continue
        if not packets:
            packets.append([line])
        elif _BITS_FIRST_RE.match(line) and ":" not in packets[-1][-1]:
            # "<naam>\r\n<n>bit ...": de vorige regel is de kop van dit pakket.
            if len(packets[-1]) > 1:
                packets.append([packets[-1].pop(), line])
            else:
                packets[-1].append(line)
        elif _PACKET_HEADER_RE.match(line):
            packets.append([line])
        else:
            packets[-1].append(line)
    return ["\n".join(p) for p in packets]


def parse_subghz_rx(text: str) -> SubGhzRxResult:
    """Parseer de uitvoer van `subghz rx` (inclusief het afsluitende 'Packets received')."""
    text = _norm(text)
    listening = _LISTENING_RE.search(text)
    if listening is None:
        if "Frequency must be in" in text:
            raise DeviceError(_first_line(text[text.find("Frequency must be in") :]))
        raise DeviceError(f"onverwachte uitvoer van 'subghz rx': {_first_line(text)[:120]!r}")
    counted = _PACKETS_RE.search(text, listening.end())
    end = counted.start() if counted else len(text)
    newline = text.find("\n", listening.end())
    body = text[newline + 1 : end] if 0 <= newline < end else ""
    packets = _split_packets(body)
    return SubGhzRxResult(
        tuned_frequency_hz=int(listening.group(1)),
        packets=packets,
        packet_count=int(counted.group(1)) if counted else len(packets),
    )


_RFID_LINE_RE = re.compile(r"^(?P<protocol>\S.*?) (?P<data>(?:[0-9A-Fa-f]{2})+)$")


def parse_rfid_read(text: str) -> RfidReadResult:
    """Parseer `rfid read`: '<Protocol> <HEX>' + info-regels, altijd 'Reading stopped'."""
    after = _after_marker(_norm(text), "Press Ctrl+C to abort", "rfid read")
    end = after.find("Reading stopped")
    body = after[:end] if end >= 0 else after
    lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
    if not lines:
        return RfidReadResult(found=False)
    match = _RFID_LINE_RE.match(lines[0])
    if match is None:
        raise DeviceError(f"onverwachte uitvoer van 'rfid read': {lines[0][:120]!r}")
    return RfidReadResult(
        found=True,
        protocol=match.group("protocol"),
        data_hex=match.group("data").upper(),
        info="\n".join(lines[1:]),
    )


_NFC_PROTOCOL_RE = re.compile(r"^Protocol \[\d+\]: (?P<tree>.+)$", re.MULTILINE)


def parse_nfc_scanner(text: str) -> NfcScanResult:
    """Parseer `scanner -t`: regels 'Protocol [n]: A -> B' na 'Protocols detected:'."""
    after = _after_marker(_norm(text), "Press Ctrl+C to abort", "nfc scanner")
    protocols = [
        [part.strip() for part in m.group("tree").split("->")]
        for m in _NFC_PROTOCOL_RE.finditer(after)
    ]
    return NfcScanResult(found=bool(protocols), protocols=protocols)


# --- profiel -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CliProfile:
    name: str
    description: str
    # Laatste regel van de uitvoer (zonder ANSI) die aangeeft dat de shell op input wacht.
    main_prompt: str
    nfc_prompt: str
    # Geldige Sub-GHz-bereiken in Hz (inclusief), uit SUBGHZ_FREQUENCY_RANGE_STR.
    subghz_ranges: tuple[tuple[int, int], ...]
    subghz_device: int  # 0 = intern (CC1101_INT)
    rfid_read_command: str
    nfc_enter_command: str
    nfc_scan_command: str
    nfc_exit_command: str
    # Tekst die `nfc` print als de NFC-app op het scherm draait (subshell start dan niet).
    nfc_busy_marker: str
    parse_subghz_rx: Callable[[str], SubGhzRxResult]
    parse_rfid_read: Callable[[str], RfidReadResult]
    parse_nfc_scanner: Callable[[str], NfcScanResult]

    def frequency_valid(self, frequency_hz: int) -> bool:
        return any(lo <= frequency_hz <= hi for lo, hi in self.subghz_ranges)

    def subghz_rx_command(self, frequency_hz: int) -> str:
        """Bouw `subghz rx <freq> <device>` uitsluitend uit gevalideerde ints."""
        if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, int):
            raise DeviceError("frequentie moet een geheel getal (Hz) zijn")
        if not self.frequency_valid(frequency_hz):
            raise DeviceError(
                f"frequentie {frequency_hz} Hz valt buiten de bereiken van de firmware "
                f"({format_ranges(self.subghz_ranges)})"
            )
        return f"subghz rx {frequency_hz} {self.subghz_device}"


def format_ranges(ranges: tuple[tuple[int, int], ...]) -> str:
    return " of ".join(f"{lo}...{hi}" for lo, hi in ranges)


# Officiële bereiken; bron: subghz_cli.c, SUBGHZ_FREQUENCY_RANGE_STR
OFFICIAL_SUBGHZ_RANGES_HZ: tuple[tuple[int, int], ...] = (
    (299_999_755, 348_000_000),
    (386_999_938, 464_000_000),
    (778_999_847, 928_000_000),
)

OFFICIAL = CliProfile(
    name="official",
    description="Officiële Flipper-firmware (flipperzero-firmware, CLI met subshells)",
    main_prompt=">: ",
    nfc_prompt="[nfc]>: ",
    subghz_ranges=OFFICIAL_SUBGHZ_RANGES_HZ,
    subghz_device=0,
    rfid_read_command="rfid read",
    nfc_enter_command="nfc",
    nfc_scan_command="scanner -t",
    nfc_exit_command="exit",
    nfc_busy_marker="NFC app is running",
    parse_subghz_rx=parse_subghz_rx,
    parse_rfid_read=parse_rfid_read,
    parse_nfc_scanner=parse_nfc_scanner,
)

# Momentum mntm-012: `subghz rx` controleert met `subghz_devices_is_frequency_valid()`
# (applications/main/subghz/subghz_cli.c), dat voor de interne CC1101 naar
# `furi_hal_subghz_is_frequency_valid()` gaat (lib/subghz/devices/cc1101_int/
# cc1101_int_interconnect.c). In Momentum is dat het volledige "extended" bereik
# (targets/f7/furi_hal/furi_hal_subghz.c); de instellingen extend_range/bypass_region beperken
# alleen zenden (`furi_hal_subghz_check_tx`), niet ontvangen. De foutmelding print wel nog de
# officiële SUBGHZ_FREQUENCY_RANGE_STR. Op hardware bevestigd: 290 MHz wordt geaccepteerd.
MOMENTUM_SUBGHZ_RANGES_HZ: tuple[tuple[int, int], ...] = (
    (281_000_000, 361_000_000),
    (378_000_000, 481_000_000),
    (749_000_000, 962_000_000),
)

# Alles behalve naam, beschrijving en Sub-GHz-bereik is gelijk aan official (zie de
# moduledocstring voor de bestanden die byte-voor-byte vergeleken zijn).
MOMENTUM = replace(
    OFFICIAL,
    name="momentum",
    description="Momentum-firmware mntm-012 (Next-Flip/Momentum-Firmware, CLI als official)",
    subghz_ranges=MOMENTUM_SUBGHZ_RANGES_HZ,
)

PROFILES: dict[str, CliProfile] = {"official": OFFICIAL, "momentum": MOMENTUM}

# DeviceInfo `firmware_origin_fork` (kleine letters) -> profielnaam. Momentum zet
# FIRMWARE_ORIGIN op "Momentum" in fbt_options.py (lib/toolbox/version.c, furi_hal_info.c);
# op de hardware gelezen: firmware_origin_fork = "Momentum".
_FORK_TO_PROFILE: dict[str, str] = {"official": "official", "momentum": "momentum"}


def select_profile(setting: str, device_info: Mapping[str, str] | None) -> CliProfile:
    """Kies het CLI-profiel: expliciete naam, of bij "auto" op `firmware_origin_fork`.

    Raises:
        UnsupportedFirmware: onbekende profielnaam, of onbekende fork bij "auto".
    """
    name = setting.strip().lower()
    if name and name != "auto":
        profile = PROFILES.get(name)
        if profile is None:
            raise UnsupportedFirmware(
                f"onbekend Flipper-CLI-profiel {setting!r}; beschikbaar: {', '.join(PROFILES)}"
            )
        return profile
    fork = (device_info or {}).get("firmware_origin_fork", "").strip()
    profile_name = _FORK_TO_PROFILE.get(fork.lower())
    if profile_name is None:
        shown = fork or "onbekend"
        version = (device_info or {}).get("firmware_version", "?")
        raise UnsupportedFirmware(
            f"Flipper-firmware '{shown}' (versie {version}) heeft nog geen CLI-profiel. "
            f"Beschikbare profielen: {', '.join(PROFILES)}. Forceer er een met "
            "PITOOLBOX_FLIPPER_CLI_PROFILE=<naam> als je zeker weet dat de CLI gelijk is."
        )
    return PROFILES[profile_name]
