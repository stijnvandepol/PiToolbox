"""Gevalideerde LLM-configuratie, opgebouwd uit `Settings` (PITOOLBOX_LLM_*).

Er is bewust **geen standaardprovider en geen standaardmodel**: zonder
PITOOLBOX_LLM_PROVIDER en PITOOLBOX_LLM_MODEL faalt de agent met een duidelijke fout.
`Settings` zelf valideert de LLM-velden niet, zodat een tikfout hier de MCP-server niet
laat crashen; de controle gebeurt pas als de agent een provider bouwt.

PITOOLBOX_LLM_PROVIDER is een preset uit `presets.py` (deepseek, mistral, ollama, ...) of een
van de drie wire-formaten. Een preset levert het officiële endpoint en of een sleutel
verplicht is; PITOOLBOX_LLM_BASE_URL overschrijft dat endpoint (bijv. een China-regio).

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

from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from pitoolbox.llm.errors import LLMConfigError

# ProviderKind blijft ook hier importeerbaar (de adapters en tests halen hem uit config).
from pitoolbox.llm.presets import ProviderKind as ProviderKind
from pitoolbox.llm.presets import ProviderPreset, Quirk, resolve_preset

if TYPE_CHECKING:
    from pitoolbox.config import Settings


_LOCAL_HTTP_NETWORKS: tuple[IPv4Network | IPv6Network, ...] = (
    IPv4Network("127.0.0.0/8"),
    IPv6Network("::1/128"),
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
    IPv6Network("fc00::/7"),
)

ENV_HELP = (
    "Zet PITOOLBOX_LLM_PROVIDER (een preset zoals deepseek, mistral of ollama, of een "
    "wire-formaat: openai-compatible, anthropic, openai; alle namen: pitoolbox-agent "
    "--list-providers) en PITOOLBOX_LLM_MODEL; voor openai-compatible ook "
    "PITOOLBOX_LLM_BASE_URL, en voor cloudproviders PITOOLBOX_LLM_API_KEY."
)


def _http_host_allowed(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in _LOCAL_HTTP_NETWORKS)


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return host is not None and ip_address(host).is_loopback
    except ValueError:
        return False


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
    # Naam van de gekozen preset (alleen voor weergave) en zijn afwijkingen (zie `Quirk`).
    preset: str | None = None
    quirks: frozenset[Quirk] = frozenset()

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMConfig:
        preset = _preset_from(settings.llm_provider)
        model = (settings.llm_model or "").strip()
        if not model:
            raise LLMConfigError(f"Geen LLM-model ingesteld (PITOOLBOX_LLM_MODEL). {ENV_HELP}")
        base_url = _base_url_for(preset, settings.llm_base_url)
        key = settings.llm_api_key
        if key is not None and not key.get_secret_value().strip():
            key = None
        if key is None and preset.requires_key:
            raise LLMConfigError(f"PITOOLBOX_LLM_API_KEY is verplicht voor {preset.name}")
        try:
            return cls(
                provider=preset.kind,
                model=model,
                base_url=base_url,
                api_key=key,
                timeout_s=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
                max_tokens=settings.llm_max_tokens,
                preset=preset.name,
                quirks=preset.quirks,
            )
        except ValidationError as exc:
            # Alleen veldnamen en regels, geen waarden (die kunnen een sleutel bevatten).
            fields = ", ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise LLMConfigError(f"ongeldige LLM-instelling: {fields}") from None


def _preset_from(raw: str | None) -> ProviderPreset:
    name = (raw or "").strip().lower()
    if not name:
        raise LLMConfigError(f"Geen LLM-provider ingesteld. {ENV_HELP}")
    return resolve_preset(name)


def _base_url_for(preset: ProviderPreset, raw: str | None) -> str:
    """Eigen PITOOLBOX_LLM_BASE_URL wint (bijv. een regio-endpoint), anders die van de preset.

    Bij een preset met verplichte sleutel (cloud) moet een eigen URL https zijn: de sleutel
    mag nooit onversleuteld over het netwerk, ook niet in het LAN. Alleen loopback mag wel
    http (de sleutel verlaat de Pi dan niet), bijv. `openai` tegen de Responses-API van een
    lokale Ollama of een lokale proxy.
    """
    override = (raw or "").strip()
    if not override:
        if preset.base_url is None:
            raise LLMConfigError(
                f"PITOOLBOX_LLM_BASE_URL is verplicht voor {preset.name}, bijv. "
                "http://127.0.0.1:11434/v1; of kies een preset met een vast endpoint "
                "(pitoolbox-agent --list-providers)"
            )
        return validate_base_url(preset.base_url)
    url = validate_base_url(override)
    parts = urlsplit(url)
    if preset.requires_key and parts.scheme != "https" and not _is_loopback(parts.hostname):
        raise LLMConfigError(
            f"PITOOLBOX_LLM_BASE_URL moet https:// zijn voor {preset.name}: "
            "de API-sleutel gaat anders onversleuteld over het netwerk (http alleen naar "
            "loopback)"
        )
    return url
