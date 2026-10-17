"""Tekst voor het scherm van de Flipper: antwoorden op snelle acties, de bevestigingsvraag,
de noodstop, foutmeldingen en het STATUS-bericht. Puur: alleen uit de resultaten van de tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pitoolbox.agent.session import TurnStop
from pitoolbox.frontends.flipper_ui.protocol import (
    MAX_ERROR_TEXT,
    MAX_SUMMARY,
    MAX_TOOL_NAME,
    MAX_UPLINK,
    ErrorCode,
    LastResult,
    Link,
    Status,
    text_width,
    to_display_text,
    to_text_box,
)

# --- tekst voor het scherm --------------------------------------------------------------------

_TURN_NOTICES = {
    TurnStop.MAX_TOKENS: "(antwoord afgekapt)",
    TurnStop.REFUSAL: "(het model weigerde)",
    TurnStop.MAX_STEPS: "(gestopt: te veel stappen)",
    TurnStop.TIMEOUT: "(gestopt: tijdslimiet)",
    TurnStop.INTERRUPTED: "(afgebroken)",
}


def _prompt_text(summary: str, tool: str, frontend: str) -> str:
    origin = frontend.split(":", 1)[0]
    text = to_display_text(f"{summary}\n(van {origin})", max_len=MAX_SUMMARY, multiline=True)
    return text if text.strip() else to_display_text(tool, max_len=MAX_SUMMARY) or "transmit"


# Dezelfde woorden als `ptb_error_title` in pitoolbox_ui.c. De FAP toont een ERROR in zijn
# TextBox als "<titel>: <tekst>" (onbekende code: "Fout <n>: "); de Pi moet weten hoe breed
# dat begin is om de tekst erachter op woordgrenzen af te breken.
_ERROR_TITLES = {
    ErrorCode.BUSY: "Bezig",
    ErrorCode.RATE_LIMITED: "Te snel",
    ErrorCode.UNKNOWN_ACTION: "Onbekende actie",
    ErrorCode.INVALID_MESSAGE: "Ongeldig bericht",
    ErrorCode.UNSUPPORTED_VERSION: "Andere versie",
    ErrorCode.TOOL_FAILED: "Mislukt",
    ErrorCode.AGENT_UNAVAILABLE: "Agent niet beschikbaar",
    ErrorCode.CANCELLED: "Afgebroken",
    ErrorCode.TIMEOUT: "Time-out",
    ErrorCode.CONFIRM_INVALID: "Bevestiging ongeldig",
    ErrorCode.NOT_READY: "Pi kent sessie niet",
}


def _error_screen_text(code: ErrorCode, text: str) -> str:
    """De tekst van een ERROR, afgebroken voor de TextBox achter "<titel>: " van de FAP."""
    title = _ERROR_TITLES.get(code)
    prefix = f"{title}: " if title else f"Fout {int(code)}: "
    return to_text_box(text, max_len=MAX_ERROR_TEXT, prefix_px=text_width(prefix))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _result(raw: Mapping[str, Any]) -> dict[str, Any]:
    return _dict(raw.get("result"))


def _error_text(raw: Mapping[str, Any]) -> str:
    return f"{raw.get('error', 'fout')}: {raw.get('message', '')}".rstrip(": ")


# Wat de gebruiker op de Flipper ziet bij een LLM-fout (codes uit pitoolbox.llm.errors); de
# code zelf blijft tussen haakjes staan voor diagnose. Op hardware gezien (2026-10-17): alleen
# "Fout van het model (llm_auth)" zegt een gebruiker te weinig.
_LLM_ERROR_TEXT = {
    "llm_config": "LLM niet (goed) ingesteld",
    "llm_auth": "API-sleutel geweigerd",
    "llm_bad_request": "verzoek geweigerd door de provider",
    "llm_rate_limited": "te veel verzoeken; probeer het later",
    "llm_server_error": "provider tijdelijk niet beschikbaar",
    "llm_timeout": "provider reageert niet (time-out)",
    "llm_connection": "geen verbinding met de provider",
    "llm_bad_response": "onverwacht antwoord van de provider",
}


def llm_error_text(code: str) -> str:
    """Korte uitleg bij een LLM-foutcode, voor het scherm van de Flipper."""
    return f"Model: {_LLM_ERROR_TEXT.get(code, 'fout')} ({code})"


def _status_text(status: Mapping[str, Any], *, pending: int) -> str:
    lines = [_uplink_line(status.get("uplink")), _power_line(status.get("power"))]
    flipper = status.get("flipper")
    if isinstance(flipper, dict):
        lines.append(f"Flipper-link: {str(flipper.get('transport_kind') or '?').upper()}")
    wifi = status.get("wifi")
    if isinstance(wifi, dict):
        lines.append(f"Wifi: {wifi.get('iface', '?')} {wifi.get('mode', '?')}")
    captures = status.get("captures")
    lines.append(f"Captures: {len(captures) if isinstance(captures, list) else '?'} actief")
    scope = status.get("scope")
    if isinstance(scope, dict):
        lines.append(f"Scope: {scope.get('networks', '?')} wifi, {scope.get('ir_devices', '?')} IR")
        if scope.get("error"):
            lines.append("Scope-fout!")
    lines.append(f"Open bevestigingen: {pending}")
    return "\n".join(lines)


# Dezelfde woorden als het statusscherm van de app (`ptb_scene_status_show` in ptb_scenes.c),
# zodat de snelle actie Status en het statusscherm hetzelfde zeggen.
def _uplink_line(uplink: Any) -> str:
    if isinstance(uplink, dict) and "iface" in uplink:
        ipv4 = _list(uplink.get("ipv4"))
        up = str(uplink.get("operstate") or "").lower() == "up"
        # Adres op een eigen regel, zoals het statusscherm (past zo binnen 120 px).
        return (
            f"Uplink: {'aan' if up else 'UIT'}\n{uplink['iface']} {ipv4[0] if ipv4 else 'geen IP'}"
        )
    return "Uplink: onbekend"


def _power_line(power: Any) -> str:
    if not (isinstance(power, dict) and power.get("available")):
        return "Voeding: onbekend"
    if power.get("undervoltage_now"):
        return "Voeding: ONDERSPANNING!"
    if "undervoltage" in _list(power.get("occurred")):
        return "Voeding: OK\n(eerder onderspanning)"
    return "Voeding: OK"


def _wifi_text(scan: Mapping[str, Any], top: int = 5) -> str:
    networks = _list(scan.get("networks"))
    lines = [
        f"{scan.get('count', len(networks))} netwerken, {scan.get('in_scope_count', 0)} in scope"
    ]
    shown = [n for n in networks if isinstance(n, dict)][:top]
    if shown:
        lines.append("Sterkst:")
    for net in shown:
        ssid = str(net.get("ssid") or "(verborgen)")[:20]
        signal = net.get("signal_dbm")
        mark = " [scope]" if net.get("in_scope") else ""
        lines.append(f" {ssid} {signal if signal is not None else '?'}dBm{mark}")
    return "\n".join(lines)


def _estop_text(raw: Mapping[str, Any]) -> str:
    if not raw.get("ok"):
        return f"Noodstop gaf een fout: {_error_text(raw)}"
    result = _result(raw)
    stopped = result.get("captures_stopped")
    count = len(stopped) if isinstance(stopped, list) else 0
    errors = result.get("errors") or {}
    text = (
        f"Noodstop uitgevoerd.\n{count} capture(s) gestopt,\n"
        f"{result.get('pending_cleared', 0)} bevestiging(en) gewist."
    )
    if errors:
        text += f"\nFouten: {', '.join(sorted(str(k) for k in errors))}"
    return text


def _status_message(
    status: Mapping[str, Any], pending: int, *, tool: str, ok: bool | None, age: int | None
) -> Callable[[int], Status]:
    """STATUS uit het resultaat van de status-tool en de laatste toolcall (`age` in s)."""
    power = _dict(status.get("power"))
    uplink = _dict(status.get("uplink"))
    flipper = _dict(status.get("flipper"))
    captures = status.get("captures")
    available = power.get("available") is True
    occurred = _list(power.get("occurred"))
    ipv4 = _list(uplink.get("ipv4"))
    iface = str(uplink.get("iface") or "?")
    up = uplink.get("operstate") == "UP" and bool(ipv4)
    uplink_text = to_display_text(
        f"{iface} {ipv4[0]}" if ipv4 else f"{iface} geen IP", max_len=MAX_UPLINK
    )
    kind = flipper.get("transport_kind")
    link = Link.USB if kind == "usb" else Link.BLE if kind == "ble" else Link.UNKNOWN
    last = LastResult.NONE if ok is None else LastResult.OK if ok else LastResult.FAILED
    return lambda seq: Status(
        seq=seq,
        uplink_up=up,
        undervoltage_now=available and power.get("undervoltage_now") is True,
        undervoltage_occurred=available and "undervoltage" in occurred,
        power_unknown=not available,
        link=link,
        captures=min(len(captures), 255) if isinstance(captures, list) else 0,
        pending=min(pending, 255),
        last_result=last,
        last_age_s=max(age, 0) if age is not None else None,
        uplink=uplink_text,
        last_tool=to_display_text(tool, max_len=MAX_TOOL_NAME),
    )
