"""Tests voor de LLM-configuratie: geen standaardprovider, base-URL-regel, geheime sleutel."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from pitoolbox.config import Settings
from pitoolbox.llm import (
    LLMConfig,
    LLMConfigError,
    ProviderKind,
    build_provider,
    validate_base_url,
)

KEY = "sk-test-geheim-1234567890"


def _settings(**kw: object) -> Settings:
    return Settings.model_validate(kw)


# --- geen standaardprovider ---


def test_settings_have_no_llm_defaults() -> None:
    s = Settings()
    assert s.llm_provider is None
    assert s.llm_model is None
    assert s.llm_base_url is None
    assert s.llm_api_key is None


def test_no_provider_configured_names_env_vars() -> None:
    with pytest.raises(LLMConfigError) as exc:
        LLMConfig.from_settings(Settings())
    msg = str(exc.value)
    assert "Geen LLM-provider ingesteld" in msg
    for var in ("PITOOLBOX_LLM_PROVIDER", "PITOOLBOX_LLM_MODEL", "PITOOLBOX_LLM_BASE_URL"):
        assert var in msg


def test_build_provider_without_config_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PITOOLBOX_LLM_PROVIDER", "PITOOLBOX_LLM_MODEL", "PITOOLBOX_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(LLMConfigError, match="PITOOLBOX_LLM_PROVIDER"):
        build_provider()


def test_provider_without_model_fails() -> None:
    with pytest.raises(LLMConfigError, match="PITOOLBOX_LLM_MODEL"):
        LLMConfig.from_settings(
            _settings(llm_provider="openai-compatible", llm_base_url="http://127.0.0.1:11434/v1")
        )


def test_unknown_provider_lists_choices() -> None:
    with pytest.raises(LLMConfigError, match="openai-compatible, anthropic, openai"):
        LLMConfig.from_settings(_settings(llm_provider="gemini", llm_model="x"))


def test_openai_compatible_requires_base_url() -> None:
    with pytest.raises(LLMConfigError, match="PITOOLBOX_LLM_BASE_URL is verplicht"):
        LLMConfig.from_settings(_settings(llm_provider="openai-compatible", llm_model="qwen3:8b"))


def test_openai_compatible_key_optional() -> None:
    cfg = LLMConfig.from_settings(
        _settings(
            llm_provider="OpenAI-Compatible ",  # hoofdletters/spaties worden genormaliseerd
            llm_model="qwen3:8b",
            llm_base_url="http://127.0.0.1:11434/v1/",
        )
    )
    assert cfg.provider is ProviderKind.OPENAI_COMPATIBLE
    assert cfg.base_url == "http://127.0.0.1:11434/v1"
    assert cfg.api_key is None


@pytest.mark.parametrize(
    ("provider", "url"),
    [("anthropic", "https://api.anthropic.com"), ("openai", "https://api.openai.com/v1")],
)
def test_cloud_provider_uses_official_endpoint(provider: str, url: str) -> None:
    cfg = LLMConfig.from_settings(
        _settings(llm_provider=provider, llm_model="m", llm_api_key=SecretStr(KEY))
    )
    assert cfg.base_url == url


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("key", [None, "   "])
def test_cloud_provider_requires_key(provider: str, key: str | None) -> None:
    with pytest.raises(LLMConfigError, match="PITOOLBOX_LLM_API_KEY is verplicht"):
        LLMConfig.from_settings(
            _settings(
                llm_provider=provider,
                llm_model="m",
                llm_api_key=SecretStr(key) if key is not None else None,
            )
        )


def test_invalid_numbers_are_reported_without_values() -> None:
    with pytest.raises(LLMConfigError, match="timeout_s") as exc:
        LLMConfig.from_settings(
            _settings(
                llm_provider="anthropic",
                llm_model="m",
                llm_api_key=SecretStr(KEY),
                llm_timeout_s=-1,
            )
        )
    assert KEY not in str(exc.value)


# --- base-URL-regel ---


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com",
        "https://open.bigmodel.cn/api/paas/v4/",
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080/v1",
        "http://127.8.9.10:1234/v1",
        "http://[::1]:8000/v1",
        "http://10.0.0.5:11434/v1",
        "http://172.16.3.4:8000/v1",
        "http://172.31.255.254:8000/v1",
        "http://192.168.1.20:1234/v1",
        "http://[fd12:3456::1]:11434/v1",
    ],
)
def test_base_url_allowed(url: str) -> None:
    assert validate_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.deepseek.com", "https"),
        ("http://ollama.local:11434/v1", "https"),  # hostnaam: kan publiek zijn
        ("http://8.8.8.8/v1", "https"),
        ("http://172.32.0.1/v1", "https"),  # net buiten 172.16/12
        ("http://169.254.169.254/latest", "https"),  # link-local/metadata: niet toegestaan
        ("http://[::ffff:8.8.8.8]/v1", "https"),
        ("http://100.64.0.1/v1", "https"),  # CGNAT is geen privé-LAN
        ("ftp://127.0.0.1/v1", "https://"),
        ("127.0.0.1:11434", "https://"),
        ("https:///v1", "host"),
        ("https://api.example.com/v1?key=abc", "query"),
        ("https://api.example.com/v1#x", "fragment"),
        ("https://localhost:99999/v1", "poort"),
    ],
)
def test_base_url_rejected(url: str, reason: str) -> None:
    with pytest.raises(LLMConfigError, match=reason):
        validate_base_url(url)


def test_base_url_with_credentials_rejected_without_leaking() -> None:
    with pytest.raises(LLMConfigError) as exc:
        validate_base_url(f"https://user:{KEY}@api.example.com/v1")
    assert KEY not in str(exc.value)


def test_base_url_validated_via_settings() -> None:
    with pytest.raises(LLMConfigError, match="https"):
        LLMConfig.from_settings(
            _settings(
                llm_provider="openai-compatible",
                llm_model="m",
                llm_base_url="http://example.com/v1",
            )
        )


# --- env en geheimhouding ---


def test_from_env_reads_llm_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PITOOLBOX_LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("PITOOLBOX_LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("PITOOLBOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("PITOOLBOX_LLM_API_KEY", KEY)
    monkeypatch.setenv("PITOOLBOX_LLM_TIMEOUT_S", "12.5")
    monkeypatch.setenv("PITOOLBOX_LLM_MAX_RETRIES", "4")
    monkeypatch.setenv("PITOOLBOX_LLM_MAX_TOKENS", "1000")
    s = Settings.from_env()
    assert s.llm_provider == "openai-compatible"
    assert s.llm_model == "qwen3:8b"
    assert s.llm_api_key is not None and s.llm_api_key.get_secret_value() == KEY
    assert (s.llm_timeout_s, s.llm_max_retries, s.llm_max_tokens) == (12.5, 4, 1000)
    assert KEY not in repr(s)
    assert KEY not in str(s.model_dump())
    cfg = LLMConfig.from_settings(s)
    assert KEY not in repr(cfg)
    provider = build_provider(cfg)
    try:
        assert KEY not in repr(provider)
        assert "qwen3:8b" in repr(provider)
    finally:
        provider.close()


def test_from_env_without_llm_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
        monkeypatch.delenv(f"PITOOLBOX_LLM_{var}", raising=False)
    s = Settings.from_env()
    assert s.llm_provider is None and s.llm_api_key is None


def test_bad_llm_provider_does_not_break_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een tikfout in de LLM-config mag de MCP-server (Settings.from_env) niet breken."""
    monkeypatch.setenv("PITOOLBOX_LLM_PROVIDER", "bestaat-niet")
    monkeypatch.setenv("PITOOLBOX_LLM_BASE_URL", "http://example.com")
    s = Settings.from_env()
    with pytest.raises(LLMConfigError):
        LLMConfig.from_settings(s)
