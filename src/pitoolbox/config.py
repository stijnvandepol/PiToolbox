"""Runtime-instellingen uit omgevingsvariabelen (PITOOLBOX_*)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, SecretStr, field_validator

from pitoolbox.core.devices._ble_pairing import normalize_adapter

_FAP_PATH_RE = re.compile(r"^/ext/apps/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+\.fap$")


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
    # Fase 3: "auto" = eerst USB, dan BLE; of vast "usb" / "ble".
    flipper_transport: Literal["auto", "usb", "ble"] = "auto"
    # BLE: vast adres van de gepairde Flipper (aanbevolen), anders zoeken op naamprefix.
    flipper_ble_address: str | None = None
    flipper_ble_name_prefix: str = "Flipper"
    # BLE: Bluetooth-adapter (hciN) voor scannen, verbinden en koppelen; None = de standaard-
    # adapter van BlueZ/bleak (de eerste die aan staat).
    flipper_ble_adapter: str | None = None
    # Interval (s) voor een RPC-ping als de verbinding idle is; 0 = uit.
    flipper_keepalive_s: float = 20.0
    alfa_iface: str = "wlan1"
    # Verwachte kerneldriver van de Alfa (AWUS036AXML = MT7921AUN, in-kernel mt7921u). Tweede
    # guard naast alfa_iface != uplink_iface: wifi-acties weigeren als de driver niet klopt, ook
    # als interfacenamen verwisseld zijn. Leeg = controle uit (alleen voor andere adapters).
    alfa_driver: str = "mt7921u"
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

    # --- fase 4: LLM-provider voor de agent-loop ---
    # Bewust GEEN standaardprovider of -model: zonder deze twee weigert de agent te starten.
    # Validatie gebeurt in `pitoolbox.llm.config.LLMConfig.from_settings`, niet hier, zodat
    # een fout in de LLM-instellingen de MCP-server niet raakt.
    llm_provider: str | None = None  # "openai-compatible" | "anthropic" | "openai"
    llm_model: str | None = None
    # Verplicht voor openai-compatible (bijv. http://127.0.0.1:11434/v1); voor anthropic en
    # openai standaard het officiële endpoint van die provider.
    llm_base_url: str | None = None
    # SecretStr: verschijnt als '**********' in repr/logs; alleen de HTTP-laag leest hem uit.
    llm_api_key: SecretStr | None = None
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2  # alleen bij 429/5xx/time-out/verbindingsfout
    llm_max_tokens: int = 4096

    # --- fase 4: grenzen van de agent-loop (gecontroleerd in `agent.session.AgentLimits`) ---
    agent_max_steps: int = 8  # tool-rondes per gebruikersbeurt
    agent_max_calls_per_step: int = 8  # tool-calls per modelantwoord
    agent_turn_timeout_s: float = 300.0  # wandkloktijd per beurt (tussen stappen gecontroleerd)
    agent_max_result_chars: int = 8000  # tool-resultaat naar het model, daarna ingekort
    agent_max_history_chars: int = 120_000  # daarboven vallen de oudste hele beurten weg

    # --- fase 4: bediening via de Flipper-app (bridge, `frontends/flipper_ui/bridge.py`) ---
    # "on": het proces dat de Flipper bezit (pitoolbox-mcp of pitoolbox-agent) draait de bridge.
    # Nooit in twee processen tegelijk: beide zouden de Flipper willen bezitten.
    flipper_ui: Literal["off", "on"] = "off"
    # Waar de FAP op de SD-kaart staat; de Pi start alleen deze app (met args "RPC").
    flipper_ui_app: str = "/ext/apps/Tools/pitoolbox_ui.fap"
    # YAML met presets ({id, label, prompt}); ontbreekt het bestand, dan zijn er geen presets.
    flipper_ui_presets: Path = Field(
        default_factory=lambda: Path("~/.config/pitoolbox/flipper_presets.yaml").expanduser()
    )

    @field_validator("flipper_ui_app")
    @classmethod
    def _check_ui_app(cls, value: str) -> str:
        if not _FAP_PATH_RE.fullmatch(value) or ".." in value:
            raise ValueError(
                f"PITOOLBOX_FLIPPER_UI_APP moet een .fap onder /ext/apps/ zijn, niet {value!r}"
            )
        return value

    @field_validator("flipper_ble_adapter")
    @classmethod
    def _check_ble_adapter(cls, value: str | None) -> str | None:
        return normalize_adapter(value)

    @classmethod
    def from_env(cls) -> Settings:
        allow = os.environ.get("PITOOLBOX_TOOL_ALLOWLIST", "")
        return cls(
            scope_file=_env_path("PITOOLBOX_SCOPE_FILE", "~/.config/pitoolbox/scope.yaml"),
            audit_log=_env_path("PITOOLBOX_AUDIT_LOG", "~/.local/state/pitoolbox/audit.jsonl"),
            captures_dir=_env_path("PITOOLBOX_CAPTURES_DIR", "~/captures"),
            flipper_port=os.environ.get("PITOOLBOX_FLIPPER_PORT") or None,
            flipper_cli_profile=os.environ.get("PITOOLBOX_FLIPPER_CLI_PROFILE", "auto"),
            flipper_transport=cast(
                Literal["auto", "usb", "ble"],
                os.environ.get("PITOOLBOX_FLIPPER_TRANSPORT", "auto"),
            ),
            flipper_ble_address=os.environ.get("PITOOLBOX_FLIPPER_BLE_ADDRESS") or None,
            flipper_ble_name_prefix=os.environ.get("PITOOLBOX_FLIPPER_BLE_NAME_PREFIX", "Flipper"),
            flipper_ble_adapter=os.environ.get("PITOOLBOX_FLIPPER_BLE_ADAPTER") or None,
            flipper_keepalive_s=float(os.environ.get("PITOOLBOX_FLIPPER_KEEPALIVE_S", "20")),
            alfa_iface=os.environ.get("PITOOLBOX_ALFA_IFACE", "wlan1"),
            alfa_driver=os.environ.get("PITOOLBOX_ALFA_DRIVER", "mt7921u").strip(),
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
            llm_provider=os.environ.get("PITOOLBOX_LLM_PROVIDER") or None,
            llm_model=os.environ.get("PITOOLBOX_LLM_MODEL") or None,
            llm_base_url=os.environ.get("PITOOLBOX_LLM_BASE_URL") or None,
            llm_api_key=(
                SecretStr(key) if (key := os.environ.get("PITOOLBOX_LLM_API_KEY")) else None
            ),
            llm_timeout_s=float(os.environ.get("PITOOLBOX_LLM_TIMEOUT_S", "60")),
            llm_max_retries=int(os.environ.get("PITOOLBOX_LLM_MAX_RETRIES", "2")),
            llm_max_tokens=int(os.environ.get("PITOOLBOX_LLM_MAX_TOKENS", "4096")),
            agent_max_steps=int(os.environ.get("PITOOLBOX_AGENT_MAX_STEPS", "8")),
            agent_max_calls_per_step=int(os.environ.get("PITOOLBOX_AGENT_MAX_CALLS_PER_STEP", "8")),
            agent_turn_timeout_s=float(os.environ.get("PITOOLBOX_AGENT_TURN_TIMEOUT_S", "300")),
            agent_max_result_chars=int(os.environ.get("PITOOLBOX_AGENT_MAX_RESULT_CHARS", "8000")),
            agent_max_history_chars=int(
                os.environ.get("PITOOLBOX_AGENT_MAX_HISTORY_CHARS", "120000")
            ),
            flipper_ui=cast(
                Literal["off", "on"],
                os.environ.get("PITOOLBOX_FLIPPER_UI", "off").strip().lower() or "off",
            ),
            flipper_ui_app=os.environ.get(
                "PITOOLBOX_FLIPPER_UI_APP", "/ext/apps/Tools/pitoolbox_ui.fap"
            ),
            flipper_ui_presets=_env_path(
                "PITOOLBOX_FLIPPER_UI_PRESETS", "~/.config/pitoolbox/flipper_presets.yaml"
            ),
        )
