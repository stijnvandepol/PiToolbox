"""Runtime-instellingen uit omgevingsvariabelen (PITOOLBOX_*)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


class Settings(BaseModel):
    scope_file: Path = Field(
        default_factory=lambda: Path("~/.config/pitoolbox/scope.yaml").expanduser()
    )
    audit_log: Path = Field(
        default_factory=lambda: Path("~/.local/state/pitoolbox/audit.jsonl").expanduser()
    )
    captures_dir: Path = Field(default_factory=lambda: Path("~/captures").expanduser())
    # None = automatisch zoeken op USB VID:PID 0483:5740
    # (bron: flipperzero-firmware targets/f7/furi_hal/furi_hal_usb_cdc.c)
    flipper_port: str | None = None
    # "auto" = kiezen op basis van firmware_origin_fork uit DeviceInfo
    flipper_cli_profile: str = "auto"
    alfa_iface: str = "wlan1"
    uplink_iface: str = "wlan0"
    confirm_ttl_s: int = 60
    # Lege lijst = alle geregistreerde tools toegestaan
    tool_allowlist: list[str] = Field(default_factory=list)

    # --- fase 2: remote MCP over HTTP + OAuth ---
    # Publieke basis-URL achter de tunnel, zonder pad en zonder slash aan het eind,
    # bijv. "https://toolbox.mijndomein.nl". De MCP-resource is dan <public_url>/mcp.
    public_url: str | None = None
    # Alleen loopback: de tunnel (cloudflared) is de enige weg naar buiten.
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    auth_db: Path = Field(
        default_factory=lambda: Path("~/.local/state/pitoolbox/auth.sqlite").expanduser()
    )
    access_token_ttl_s: int = 900  # 15 min
    refresh_token_ttl_s: int = 7 * 24 * 3600  # 7 dagen, met rotatie
    auth_code_ttl_s: int = 300
    # Standaard alleen de Claude-callbacks. Alleen tijdelijk voor testen (bijv. MCP Inspector:
    # http://127.0.0.1:6274/oauth/callback) extra exacte redirect-URI's toestaan.
    extra_redirect_uris: list[str] = Field(default_factory=list)

    @classmethod
    def from_env(cls) -> Settings:
        allow = os.environ.get("PITOOLBOX_TOOL_ALLOWLIST", "")
        return cls(
            scope_file=_env_path("PITOOLBOX_SCOPE_FILE", "~/.config/pitoolbox/scope.yaml"),
            audit_log=_env_path("PITOOLBOX_AUDIT_LOG", "~/.local/state/pitoolbox/audit.jsonl"),
            captures_dir=_env_path("PITOOLBOX_CAPTURES_DIR", "~/captures"),
            flipper_port=os.environ.get("PITOOLBOX_FLIPPER_PORT") or None,
            flipper_cli_profile=os.environ.get("PITOOLBOX_FLIPPER_CLI_PROFILE", "auto"),
            alfa_iface=os.environ.get("PITOOLBOX_ALFA_IFACE", "wlan1"),
            uplink_iface=os.environ.get("PITOOLBOX_UPLINK_IFACE", "wlan0"),
            confirm_ttl_s=int(os.environ.get("PITOOLBOX_CONFIRM_TTL_S", "60")),
            tool_allowlist=[t.strip() for t in allow.split(",") if t.strip()],
            public_url=(os.environ.get("PITOOLBOX_PUBLIC_URL") or "").rstrip("/") or None,
            http_host=os.environ.get("PITOOLBOX_HTTP_HOST", "127.0.0.1"),
            http_port=int(os.environ.get("PITOOLBOX_HTTP_PORT", "8765")),
            auth_db=_env_path("PITOOLBOX_AUTH_DB", "~/.local/state/pitoolbox/auth.sqlite"),
            access_token_ttl_s=int(os.environ.get("PITOOLBOX_ACCESS_TOKEN_TTL_S", "900")),
            refresh_token_ttl_s=int(os.environ.get("PITOOLBOX_REFRESH_TOKEN_TTL_S", "604800")),
            extra_redirect_uris=[
                u.strip()
                for u in os.environ.get("PITOOLBOX_EXTRA_REDIRECT_URIS", "").split(",")
                if u.strip()
            ],
        )
