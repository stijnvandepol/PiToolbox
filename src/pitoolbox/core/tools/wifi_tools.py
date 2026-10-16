"""Wi-Fi tools: adaptermodus, passieve scan en pcap-captures van doelen uit scope.yaml.

Scope-beslissing: een scan is passief en toont dus ook buurnetwerken (met `in_scope`-vlag);
captures zijn alleen toegestaan voor BSSID's uit scope.yaml, op het kanaal uit de scope.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from pitoolbox.core.errors import InvalidState
from pitoolbox.core.scope import Mac, Scope
from pitoolbox.core.tools.base import Risk, ScopeCheck, ToolArgs, ToolContext, ToolSpec


class AlfaSetModeArgs(ToolArgs):
    mode: Literal["managed", "monitor"]


class WifiScanArgs(ToolArgs):
    method: Literal["iw", "airodump"] = Field(
        default="iw",
        description="iw = managed mode (snel); airodump = monitor mode (duurt duration_s)",
    )
    duration_s: int = Field(default=10, ge=3, le=60, description="Alleen voor airodump")
    band: Literal["bg", "a", "abg"] = "abg"


class CaptureStartArgs(ToolArgs):
    bssid: Mac
    channel: int | None = Field(default=None, ge=1, le=196)
    duration_s: int = Field(default=300, ge=10, le=3600)
    session: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")


class CaptureStopArgs(ToolArgs):
    capture_id: str = Field(pattern=r"^[0-9a-f]{6}$")


# -- alfa_set_mode ---------------------------------------------------------------------


def _set_mode_scope(args: AlfaSetModeArgs, scope: Scope) -> ScopeCheck:
    # Geen netwerkdoel, dus altijd binnen scope. De harde guard (nooit de uplink omzetten)
    # zit in AlfaAdapter.set_mode, die de actuele Settings kent.
    return ScopeCheck(True)


def _alfa_set_mode(ctx: ToolContext, args: AlfaSetModeArgs) -> dict[str, Any]:
    if args.mode == "managed" and ctx.captures.list_active():
        raise InvalidState("er lopen captures; stop die eerst (capture_stop)")
    return ctx.wifi.set_mode(args.mode)


# -- wifi_scan -------------------------------------------------------------------------


def _wifi_scan(ctx: ToolContext, args: WifiScanArgs) -> dict[str, Any]:
    if args.method == "airodump" and ctx.captures.list_active():
        # Kanaalhoppen zou een lopende capture (vast kanaal) verstoren.
        raise InvalidState("er loopt een capture; airodump-scan zou het kanaal verstoren")
    raw = ctx.wifi.scan(args.method, args.duration_s, args.band)
    in_scope = ctx.scope.bssids()
    networks = [{**ap, "in_scope": ap["bssid"] in in_scope} for ap in raw["aps"]]
    networks.sort(key=lambda n: (n["signal_dbm"] is None, -(n["signal_dbm"] or 0)))
    result: dict[str, Any] = {
        "iface": ctx.wifi.iface,
        "method": raw["method"],
        "band": raw["band"],
        "count": len(networks),
        "in_scope_count": sum(1 for n in networks if n["in_scope"]),
        "networks": networks,
    }
    if raw["method"] == "airodump":
        # Alleen een telling: clientadressen van derden worden niet teruggegeven.
        result["client_count"] = len(raw["stations"])
    return result


# -- capture_start / capture_stop ------------------------------------------------------


def _capture_start_scope(args: CaptureStartArgs, scope: Scope) -> ScopeCheck:
    net = scope.network_by_bssid(args.bssid)
    if net is None:
        return ScopeCheck(False, f"BSSID {args.bssid} staat niet in scope.yaml (wifi)")
    if args.channel is not None and args.channel != net.channel:
        return ScopeCheck(
            False,
            f"kanaal {args.channel} wijkt af van scope-kanaal {net.channel} voor {args.bssid}",
        )
    return ScopeCheck(True)


def _capture_start(ctx: ToolContext, args: CaptureStartArgs) -> dict[str, Any]:
    net = ctx.scope.network_by_bssid(args.bssid)
    if net is None:  # scope kan tussen policy en handler gewijzigd zijn
        raise InvalidState(f"BSSID {args.bssid} staat niet (meer) in scope.yaml")
    return ctx.captures.start(net.bssid, net.channel, args.duration_s, args.session)


def _always_in_scope(args: Any, scope: Scope) -> ScopeCheck:
    return ScopeCheck(True)


def _capture_stop(ctx: ToolContext, args: CaptureStopArgs) -> dict[str, Any]:
    return ctx.captures.stop(args.capture_id)


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="alfa_set_mode",
        description=(
            "Zet de Alfa-adapter (nooit de uplink) in managed of monitor mode. "
            "Monitor mode is nodig voor airodump-scan en captures."
        ),
        risk=Risk.CAPTURE,
        params=AlfaSetModeArgs,
        handler=_alfa_set_mode,
        scope_check=_set_mode_scope,
    ),
    ToolSpec(
        name="wifi_scan",
        description=(
            "Passieve Wi-Fi-scan. method 'iw' vereist managed mode, 'airodump' monitor mode. "
            "Resultaten bevatten ook buurnetwerken; 'in_scope' markeert doelen uit scope.yaml."
        ),
        risk=Risk.READ,
        params=WifiScanArgs,
        handler=_wifi_scan,
        max_duration_s=75.0,
    ),
    ToolSpec(
        name="capture_start",
        description=(
            "Start een pcap-capture (airodump-ng) van een netwerk uit scope.yaml, op het "
            "scope-kanaal. Vereist monitor mode. Stopt vanzelf na duration_s. Max. 2 tegelijk."
        ),
        risk=Risk.CAPTURE,
        params=CaptureStartArgs,
        handler=_capture_start,
        scope_check=_capture_start_scope,
    ),
    ToolSpec(
        name="capture_stop",
        description="Stop een lopende capture en geef de bestanden en groottes terug.",
        risk=Risk.CAPTURE,
        params=CaptureStopArgs,
        handler=_capture_stop,
        scope_check=_always_in_scope,
        max_duration_s=15.0,
    ),
]
