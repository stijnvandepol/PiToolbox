"""Flipper-tools: alleen benoemde acties met gevalideerde argumenten, nooit een shell.

Het IR-bestand komt altijd uit scope.yaml (`infrared[].file`), nooit uit de argumenten.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import Field

from pitoolbox.core.devices._flipper_cli import OFFICIAL_SUBGHZ_RANGES_HZ, format_ranges
from pitoolbox.core.errors import CommandFailed
from pitoolbox.core.scope import FlipperPath, Scope
from pitoolbox.core.tools.base import Risk, ScopeCheck, ToolArgs, ToolContext, ToolSpec

# Zelfde grenzen als SUBGHZ_FREQUENCY_RANGE_STR in de firmware (subghz_cli.c).
_SUBGHZ_MIN_HZ = OFFICIAL_SUBGHZ_RANGES_HZ[0][0]
_SUBGHZ_MAX_HZ = OFFICIAL_SUBGHZ_RANGES_HZ[-1][1]

_IR_BUTTON_PATTERN = r"^[A-Za-z0-9_\-+. ]{1,32}$"


# --- argumentmodellen --------------------------------------------------------------------


class SystemInfoArgs(ToolArgs):
    pass


class StorageListArgs(ToolArgs):
    path: FlipperPath = Field(
        default="/ext",
        description="Map op de Flipper, alleen onder /ext (SD) of /int, bijv. /ext/infrared.",
    )


class StorageReadArgs(ToolArgs):
    path: FlipperPath = Field(
        description="Bestand op de Flipper onder /ext of /int, bijv. /ext/infrared/tv.ir."
    )
    max_bytes: int = Field(
        default=65536,
        ge=1,
        le=262144,
        description="Weiger bestanden groter dan dit aantal bytes (1..262144).",
    )


class SubGhzRxArgs(ToolArgs):
    frequency_hz: int = Field(
        ge=_SUBGHZ_MIN_HZ,
        le=_SUBGHZ_MAX_HZ,
        description=(
            "Frequentie in Hz, bijv. 433920000. Geldig: "
            f"{format_ranges(OFFICIAL_SUBGHZ_RANGES_HZ)}."
        ),
    )
    duration_s: int = Field(
        default=10, ge=1, le=120, description="Hoe lang er geluisterd wordt (1..120 s)."
    )


class NfcReadArgs(ToolArgs):
    timeout_s: int = Field(
        default=15, ge=1, le=60, description="Maximale wachttijd op een tag (1..60 s)."
    )


class RfidReadArgs(ToolArgs):
    timeout_s: int = Field(
        default=15, ge=1, le=60, description="Maximale wachttijd op een tag (1..60 s)."
    )


class IrSendArgs(ToolArgs):
    device: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_\-]{0,31}$",
        description="Naam van het IR-apparaat zoals in scope.yaml (infrared[].name), bijv. 'tv'.",
    )
    button: str = Field(
        pattern=_IR_BUTTON_PATTERN,
        description=(
            "Knopnaam zoals in het .ir-bestand, bijv. 'Power' of 'Vol_up' (hoofdlettergevoelig)."
        ),
    )


# --- handlers ----------------------------------------------------------------------------


def _system_info(ctx: ToolContext, args: SystemInfoArgs) -> dict[str, Any]:
    return {"info": ctx.flipper.system_info()}


def _storage_list(ctx: ToolContext, args: StorageListArgs) -> dict[str, Any]:
    entries = ctx.flipper.storage_list(args.path)
    return {"path": args.path, "count": len(entries), "entries": entries}


def _storage_read(ctx: ToolContext, args: StorageReadArgs) -> dict[str, Any]:
    data = ctx.flipper.storage_read(args.path, args.max_bytes)
    result: dict[str, Any] = {"path": args.path, "size": len(data)}
    try:
        # NUL-bytes komen in tekstbestanden niet voor; dan is het binair, ook als het toevallig
        # geldige UTF-8 is.
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL-byte")
        result["content"] = data.decode("utf-8")
        result["encoding"] = "utf-8"
    except UnicodeDecodeError:
        result["content"] = base64.b64encode(data).decode("ascii")
        result["encoding"] = "base64"
    return result


def _subghz_rx(ctx: ToolContext, args: SubGhzRxArgs) -> dict[str, Any]:
    return ctx.flipper.subghz_rx(args.frequency_hz, args.duration_s)


def _nfc_read(ctx: ToolContext, args: NfcReadArgs) -> dict[str, Any]:
    return ctx.flipper.nfc_scan(args.timeout_s)


def _rfid_read(ctx: ToolContext, args: RfidReadArgs) -> dict[str, Any]:
    return ctx.flipper.rfid_read(args.timeout_s)


def _ir_send(ctx: ToolContext, args: IrSendArgs) -> dict[str, Any]:
    device = ctx.scope.ir_device(args.device)
    if device is None:  # beleid heeft dit al geweigerd; dit is een vangnet
        raise CommandFailed(f"IR-apparaat {args.device!r} staat niet in scope.yaml")
    result = ctx.flipper.ir_send(device.file, args.button)
    return {"device": device.name, **result}


# --- scope en samenvatting ---------------------------------------------------------------


def _subghz_rx_scope(args: SubGhzRxArgs, scope: Scope) -> ScopeCheck:
    freq = args.frequency_hz
    if any(lo <= freq <= hi for lo, hi in OFFICIAL_SUBGHZ_RANGES_HZ):
        return ScopeCheck(True)
    return ScopeCheck(
        False,
        f"{freq} Hz valt buiten de bereiken van de Flipper-firmware "
        f"({format_ranges(OFFICIAL_SUBGHZ_RANGES_HZ)})",
    )


def _ir_send_scope(args: IrSendArgs, scope: Scope) -> ScopeCheck:
    device = scope.ir_device(args.device)
    if device is None:
        return ScopeCheck(False, f"IR-apparaat {args.device!r} staat niet in scope.yaml (infrared)")
    if device.buttons is not None and args.button not in device.buttons:
        allowed = ", ".join(device.buttons) or "(geen)"
        return ScopeCheck(
            False,
            f"knop {args.button!r} is niet toegestaan voor {device.name!r}; toegestaan: {allowed}",
        )
    return ScopeCheck(True)


def _ir_send_summary(args: IrSendArgs, scope: Scope) -> str:
    device = scope.ir_device(args.device)
    where = device.file if device else "niet in scope"
    return f"IR '{args.button}' naar {args.device} ({where})"


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="system_info",
        description=(
            "Toont apparaat- en firmware-info van de Flipper Zero (hardware, firmware-fork en "
            "-versie, batterij, enz.) als sleutel/waarde-paren."
        ),
        risk=Risk.READ,
        params=SystemInfoArgs,
        handler=_system_info,
        max_duration_s=20.0,
    ),
    ToolSpec(
        name="storage_list",
        description=(
            "Lijst bestanden en mappen in een map op de Flipper (SD-kaart /ext of intern /int). "
            "Geeft naam, type (file/dir) en grootte."
        ),
        risk=Risk.READ,
        params=StorageListArgs,
        handler=_storage_list,
        max_duration_s=20.0,
    ),
    ToolSpec(
        name="storage_read",
        description=(
            "Leest een bestand van de Flipper (tekst als utf-8, anders base64). Weigert bestanden "
            "groter dan max_bytes. Handig voor .ir-, .sub- en .nfc-bestanden."
        ),
        risk=Risk.READ,
        params=StorageReadArgs,
        handler=_storage_read,
        max_duration_s=30.0,
    ),
    ToolSpec(
        name="subghz_rx",
        description=(
            "Luistert met de Sub-GHz-radio van de Flipper op een frequentie en geeft de "
            "gedecodeerde pakketten terug. Alleen ontvangen, er wordt niets verzonden."
        ),
        risk=Risk.READ,
        params=SubGhzRxArgs,
        handler=_subghz_rx,
        scope_check=_subghz_rx_scope,
        max_duration_s=135.0,
    ),
    ToolSpec(
        name="nfc_read",
        description=(
            "Wacht tot een NFC-tag bij de Flipper wordt gehouden en geeft de herkende "
            "protocollen terug (bijv. ISO14443-3A -> Mifare Classic). Leest geen kaartinhoud."
        ),
        risk=Risk.READ,
        params=NfcReadArgs,
        handler=_nfc_read,
        max_duration_s=75.0,
    ),
    ToolSpec(
        name="rfid_read",
        description=(
            "Wacht tot een 125 kHz-RFID-tag bij de Flipper wordt gehouden en geeft protocol, "
            "data (hex) en leesbare info terug."
        ),
        risk=Risk.READ,
        params=RfidReadArgs,
        handler=_rfid_read,
        max_duration_s=75.0,
    ),
    ToolSpec(
        name="ir_send",
        description=(
            "Stuurt één infraroodknop naar een apparaat uit scope.yaml (bijv. device 'tv', knop "
            "'Power'). Het .ir-bestand komt uit de scope, niet uit de argumenten. Vereist "
            "bevestiging door de gebruiker."
        ),
        risk=Risk.TRANSMIT,
        params=IrSendArgs,
        handler=_ir_send,
        scope_check=_ir_send_scope,
        summary=_ir_send_summary,
        max_duration_s=25.0,
    ),
]
