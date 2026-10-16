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
        )
