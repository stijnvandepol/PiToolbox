"""Gevalideerde LLM-configuratie, opgebouwd uit `Settings` (PITOOLBOX_LLM_*).

Er is bewust **geen standaardprovider en geen standaardmodel**: zonder
PITOOLBOX_LLM_PROVIDER en PITOOLBOX_LLM_MODEL faalt de agent met een duidelijke fout.
`Settings` zelf valideert de LLM-velden niet, zodat een tikfout hier de MCP-server niet
laat crashen; de controle gebeurt pas als de agent een provider bouwt.

Regel voor de base-URL (expliciet, zonder DNS-lookup):
- `https://` mag altijd.
- `http://` mag alleen naar `localhost` of een IP-literal in loopback (127.0.0.0/8, ::1)
  of privé-LAN (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, IPv6 ULA fc00::/7). Dat is voor
  lokale servers zoals Ollama, llama.cpp, LM Studio en vLLM. Een hostnaam (ook `.local`)
  wordt bij http geweigerd: die kan naar een publiek adres wijzen. Gebruik dan het IP.
- Geen gebruikersnaam/wachtwoord, query of fragment in de URL (sleutels horen in
  PITOOLBOX_LLM_API_KEY, nooit in een URL die in logs belandt).
"""

from __future__ import annotations

from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from pitoolbox.llm.errors import LLMConfigError

if TYPE_CHECKING:
    from pitoolbox.config import Settings


class ProviderKind(StrEnum):
    OPENAI_COMPATIBLE = "openai-compatible"  # Chat Completions: Ollama, vLLM, DeepSeek, ...
    ANTHROPIC = "anthropic"  # Messages API
    OPENAI = "openai"  # Responses API


# Officiële endpoints, alleen gebruikt als die provider expliciet gekozen is.
# Bronnen: https://platform.claude.com/docs/en/api/errors (curl-voorbeeld naar
# https://api.anthropic.com/v1/messages) en https://developers.openai.com/api/reference/overview
# (curl-voorbeelden naar https://api.openai.com/v1/...).
_OFFICIAL_BASE_URLS = {
    ProviderKind.ANTHROPIC: "https://api.anthropic.com",
    ProviderKind.OPENAI: "https://api.openai.com/v1",
}

_LOCAL_HTTP_NETWORKS: tuple[IPv4Network | IPv6Network, ...] = (
    IPv4Network("127.0.0.0/8"),
    IPv6Network("::1/128"),
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
    IPv6Network("fc00::/7"),
)

ENV_HELP = (
    "Zet PITOOLBOX_LLM_PROVIDER (openai-compatible, anthropic of openai) en "
    "PITOOLBOX_LLM_MODEL; voor openai-compatible ook PITOOLBOX_LLM_BASE_URL, en voor "
    "cloudproviders PITOOLBOX_LLM_API_KEY."
)


def _http_host_allowed(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in _LOCAL_HTTP_NETWORKS)


def validate_base_url(url: str) -> str:
    """Controleert de base-URL volgens de regel hierboven; geeft hem zonder '/' aan het eind."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise LLMConfigError(f"PITOOLBOX_LLM_BASE_URL moet met https:// beginnen: {url!r}")
    if not parts.hostname:
        raise LLMConfigError(f"PITOOLBOX_LLM_BASE_URL heeft geen host: {url!r}")
    if parts.username is not None or parts.password is not None:
        # Geen URL in de foutmelding: daar staat dan juist een geheim in.
        raise LLMConfigError(
            "PITOOLBOX_LLM_BASE_URL mag geen gebruikersnaam of wachtwoord bevatten; "
            "zet de sleutel in PITOOLBOX_LLM_API_KEY"
        )
    if parts.query or parts.fragment:
        raise LLMConfigError("PITOOLBOX_LLM_BASE_URL mag geen query of fragment bevatten")
    try:
        _ = parts.port  # ongeldige poort -> ValueError
    except ValueError:
        raise LLMConfigError(f"PITOOLBOX_LLM_BASE_URL heeft een ongeldige poort: {url!r}") from None
    if parts.scheme == "http" and not _http_host_allowed(parts.hostname):
        raise LLMConfigError(
            f"http:// is alleen toegestaan naar localhost, loopback of een privé-LAN-IP "
            f"(10/8, 172.16/12, 192.168/16, fc00::/7); gebruik https:// voor {parts.hostname!r}"
        )
    return url.strip().rstrip("/")


class LLMConfig(BaseModel):
    """Volledige, gevalideerde configuratie voor één provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ProviderKind
    model: str = Field(min_length=1, max_length=200)
    base_url: str
    api_key: SecretStr | None = None
    timeout_s: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_tokens: int = Field(default=4096, ge=1, le=1_000_000)

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMConfig:
        raw_provider = (settings.llm_provider or "").strip().lower()
        if not raw_provider:
            raise LLMConfigError(f"Geen LLM-provider ingesteld. {ENV_HELP}")
        try:
            provider = ProviderKind(raw_provider)
        except ValueError:
            choices = ", ".join(k.value for k in ProviderKind)
            raise LLMConfigError(
                f"Onbekende PITOOLBOX_LLM_PROVIDER {raw_provider!r}; kies uit: {choices}"
            ) from None

        model = (settings.llm_model or "").strip()
        if not model:
            raise LLMConfigError(f"Geen LLM-model ingesteld (PITOOLBOX_LLM_MODEL). {ENV_HELP}")

        raw_url = (settings.llm_base_url or "").strip()
        if not raw_url:
            if provider is ProviderKind.OPENAI_COMPATIBLE:
                raise LLMConfigError(
                    "PITOOLBOX_LLM_BASE_URL is verplicht voor openai-compatible, bijv. "
                    "http://127.0.0.1:11434/v1 (Ollama) of https://api.deepseek.com"
                )
            raw_url = _OFFICIAL_BASE_URLS[provider]
        base_url = validate_base_url(raw_url)

        key = settings.llm_api_key
        if key is not None and not key.get_secret_value().strip():
            key = None
        if key is None and provider is not ProviderKind.OPENAI_COMPATIBLE:
            raise LLMConfigError(f"PITOOLBOX_LLM_API_KEY is verplicht voor {provider.value}")

        try:
            return cls(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=key,
                timeout_s=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
                max_tokens=settings.llm_max_tokens,
            )
        except ValidationError as exc:
            # Alleen veldnamen en regels, geen waarden (die kunnen een sleutel bevatten).
            fields = ", ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise LLMConfigError(f"ongeldige LLM-instelling: {fields}") from None
