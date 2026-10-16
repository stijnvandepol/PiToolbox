"""Lokale controle van een FAP-bestand (Flipper Application Package) vóór het installeren.

Een FAP is een ELF32-bestand voor ARM met een eigen sectie `.fapmeta` (het manifest). Bronnen:

* ELF-kop en sectiekoppen: System V ABI, gABI hoofdstuk 4 (`Elf32_Ehdr`: e_ident met
  EI_CLASS=1 voor 32-bit en EI_DATA=1 voor little-endian, e_machine op offset 18, e_shoff op 32,
  e_shentsize/e_shnum/e_shstrndx op 46/48/50; `Elf32_Shdr` van 40 bytes met sh_name,
  sh_offset en sh_size op 0, 16 en 20). EM_ARM = 40 (ARM ELF ABI).
* Manifest: Momentum mntm-012 `lib/flipper_application/application_manifest.h`
  (`FlipperApplicationManifestBase`, gepackt: manifest_magic u32 = 0x52474448,
  manifest_version u32 = 1, api_version {minor u16, major u16}, hardware_target_id u16; daarna
  in V1 stack_size u16, app_version u32 en name[32]).
* Wanneer de firmware een app weigert: `lib/flipper_application/flipper_application.c`
  (`flipper_application_validate_manifest`): manifest ongeldig, hardware_target_id anders dan
  het target van de firmware, of API-major anders dan die van de firmware (ApiTooOld/ApiTooNew;
  `application_manifest.c` vergelijkt alleen de major). Bij een API-mismatch toont de Flipper
  "App Too Old/New"; Cancel op dat scherm terwijl de app via RPC gestart is gaf in de spike een
  `furi_crash()` (docs/fase4-spike.md §2). Daarom weigeren we hier al vóór het installeren.

Puur Python, geen I/O: de aanroeper leest het bestand.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Grens voor een PiToolbox-FAP: de huidige build is ~24 KiB (README van de FAP).
FAP_MAX_BYTES = 256 * 1024
FAP_APP_NAME = "PiToolbox"  # `name` in application.fam

_ELF_MAGIC = b"\x7fELF"
_ELFCLASS32 = 1
_ELFDATA2LSB = 1
_EM_ARM = 40
_SHDR_SIZE = 40
_MANIFEST_MAGIC = 0x52474448
_MANIFEST_VERSION = 1
_MANIFEST_V1_MIN = 52  # base (14) + stack_size (2) + app_version (4) + name[32]


@dataclass(frozen=True)
class FapInfo:
    """Wat uit `.fapmeta` komt."""

    name: str
    api_major: int
    api_minor: int
    target: int
    app_version: int  # (major << 16) | minor, zoals fap_version in application.fam

    @property
    def api(self) -> str:
        return f"{self.api_major}.{self.api_minor}"

    @property
    def version(self) -> str:
        return f"{self.app_version >> 16}.{self.app_version & 0xFFFF}"


class FapError(ValueError):
    """Het bestand is geen (geldige) FAP; de melding zegt waarom."""


def inspect_fap(data: bytes) -> FapInfo:
    """Controleer de ELF-kop en lees het manifest. Gooit `FapError` bij elke afwijking."""
    if not 0 < len(data) <= FAP_MAX_BYTES:
        raise FapError(f"{len(data)} bytes; een FAP moet 1..{FAP_MAX_BYTES} bytes zijn")
    if len(data) < 52 or data[:4] != _ELF_MAGIC:
        raise FapError("geen ELF-bestand (verkeerde magic)")
    if data[4] != _ELFCLASS32 or data[5] != _ELFDATA2LSB:
        raise FapError("geen 32-bit little-endian ELF")
    (machine,) = struct.unpack_from("<H", data, 18)
    if machine != _EM_ARM:
        raise FapError(f"ELF voor machine {machine}, niet ARM ({_EM_ARM})")
    manifest = _section(data, ".fapmeta")
    if manifest is None:
        raise FapError("geen .fapmeta-sectie: dit is geen Flipper-app")
    if len(manifest) < _MANIFEST_V1_MIN:
        raise FapError(".fapmeta is te kort")
    magic, version, minor, major, target = struct.unpack_from("<IIHHH", manifest, 0)
    if magic != _MANIFEST_MAGIC or version != _MANIFEST_VERSION:
        raise FapError("onbekend FAP-manifest (magic of versie)")
    (app_version,) = struct.unpack_from("<I", manifest, 16)
    raw_name = manifest[20:52].split(b"\0", 1)[0]
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FapError("app-naam in het manifest is geen ASCII") from exc
    return FapInfo(
        name=name, api_major=major, api_minor=minor, target=target, app_version=app_version
    )


def check_compatible(info: FapInfo, device_info: dict[str, str]) -> list[str]:
    """Problemen tussen app en firmware (leeg = in orde). Ontbrekende sleutels in DeviceInfo
    (`firmware_api_major/minor`, `firmware_target`; furi_hal_info.c) worden overgeslagen."""
    problems: list[str] = []
    fw_major = _int(device_info.get("firmware_api_major"))
    fw_minor = _int(device_info.get("firmware_api_minor"))
    fw_target = _int(device_info.get("firmware_target"))
    if fw_major is not None and info.api_major != fw_major:
        problems.append(
            f"app gebouwd voor API {info.api}, firmware heeft API {fw_major}.{fw_minor}: de "
            "Flipper zou 'App Too Old/New' tonen"
        )
    elif fw_minor is not None and info.api_minor > fw_minor:
        problems.append(
            f"app gebouwd voor API {info.api}, nieuwer dan de firmware ({fw_major}.{fw_minor}): "
            "symbolen kunnen ontbreken"
        )
    if fw_target is not None and info.target != fw_target:
        problems.append(f"app voor hardware-target {info.target}, Flipper heeft {fw_target}")
    return problems


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _section(data: bytes, wanted: str) -> bytes | None:
    """Inhoud van sectie `wanted`, met grenscontrole op elke offset (het bestand is invoer)."""
    (shoff,) = struct.unpack_from("<I", data, 32)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 46)
    if shentsize != _SHDR_SIZE or shnum == 0 or shstrndx >= shnum:
        raise FapError("ongeldige sectietabel in de ELF-kop")
    if shoff + shnum * _SHDR_SIZE > len(data):
        raise FapError("sectietabel valt buiten het bestand")

    def header(index: int) -> tuple[int, int, int]:
        base = shoff + index * _SHDR_SIZE
        (name,) = struct.unpack_from("<I", data, base)
        offset, size = struct.unpack_from("<II", data, base + 16)
        return name, offset, size

    _, str_off, str_size = header(shstrndx)
    if str_off + str_size > len(data):
        raise FapError("sectienamen vallen buiten het bestand")
    names = data[str_off : str_off + str_size]
    target = wanted.encode("ascii")
    for index in range(shnum):
        name_off, offset, size = header(index)
        if name_off >= len(names):
            continue
        if names[name_off:].split(b"\0", 1)[0] != target:
            continue
        if offset + size > len(data):
            raise FapError(f"sectie {wanted} valt buiten het bestand")
        return data[offset : offset + size]
    return None
