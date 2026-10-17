"""Tests voor de providerpresets: oplossen, overschrijven, sleutelregels, foutmeldingen."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr

from pitoolbox.config import Settings
from pitoolbox.llm import LLMConfig, LLMConfigError, ProviderKind, build_provider
from pitoolbox.llm.presets import ALIASES, PRESETS, Quirk, resolve_preset

KEY = "sk-test-geheim-1234567890"
CLOUD = sorted(n for n, p in PRESETS.items() if p.requires_key)
LOCAL = ["ollama", "lmstudio", "llamacpp", "vllm"]
NEW_CLOUD = [
    "deepseek",
    "qwen",
    "kimi",
    "glm",
    "minimax",
    "mistral",
    "groq",
    "openrouter",
    "xai",
    "gemini",
]


def _cfg(provider: str, *, key: str | None = KEY, base_url: str | None = None) -> LLMConfig:
    return LLMConfig.from_settings(
        Settings.model_validate(
            {
                "llm_provider": provider,
                "llm_model": "voorbeeld-model",
                "llm_api_key": SecretStr(key) if key is not None else None,
                "llm_base_url": base_url,
            }
        )
    )


def test_all_requested_presets_exist() -> None:
    expected = {"openai-compatible", "anthropic", "openai", *NEW_CLOUD, *LOCAL}
    assert set(PRESETS) == expected
    assert set(CLOUD) == {"anthropic", "openai", *NEW_CLOUD}


@pytest.mark.parametrize("name", CLOUD)
def test_cloud_preset_uses_official_https_endpoint(name: str) -> None:
    preset = PRESETS[name]
    assert preset.base_url is not None
    assert urlsplit(preset.base_url).scheme == "https"
    cfg = _cfg(name)
    assert cfg.provider is preset.kind
    assert cfg.base_url == preset.base_url.rstrip("/")
    assert cfg.preset == name
    assert cfg.quirks == preset.quirks


@pytest.mark.parametrize("name", CLOUD)
@pytest.mark.parametrize("key", [None, "  "])
def test_cloud_preset_requires_key(name: str, key: str | None) -> None:
    with pytest.raises(LLMConfigError, match=f"PITOOLBOX_LLM_API_KEY is verplicht voor {name}"):
        _cfg(name, key=key)


@pytest.mark.parametrize("name", LOCAL)
def test_local_preset_needs_no_key_and_uses_loopback_http(name: str) -> None:
    cfg = _cfg(name, key=None)
    assert cfg.provider is ProviderKind.OPENAI_COMPATIBLE
    assert cfg.api_key is None
    parts = urlsplit(cfg.base_url)
    assert parts.scheme == "http"
    assert parts.hostname in ("127.0.0.1", "localhost")
    assert parts.path == "/v1"


def test_local_preset_defaults() -> None:
    urls = {name: _cfg(name, key=None).base_url for name in LOCAL}
    assert urls == {
        "ollama": "http://127.0.0.1:11434/v1",
        "lmstudio": "http://localhost:1234/v1",
        "llamacpp": "http://127.0.0.1:9931/v1",
        "vllm": "http://localhost:8000/v1",
    }


def test_local_preset_accepts_key_and_lan_override() -> None:
    cfg = _cfg("vllm", base_url="http://192.168.1.20:8000/v1/")
    assert cfg.base_url == "http://192.168.1.20:8000/v1"
    assert cfg.api_key is not None


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"),  # China (Beijing)
        ("kimi", "https://api.moonshot.cn/v1"),
        ("glm", "https://open.bigmodel.cn/api/paas/v4"),
        ("minimax", "https://api.minimax.cn/v1"),
    ],
)
def test_base_url_overrides_preset(name: str, url: str) -> None:
    assert _cfg(name, base_url=url).base_url == url


@pytest.mark.parametrize("name", ["deepseek", "gemini", "anthropic"])
def test_cloud_preset_override_must_be_https(name: str) -> None:
    with pytest.raises(LLMConfigError, match="https") as exc:
        _cfg(name, base_url="http://192.168.1.20:8080/v1")
    assert KEY not in str(exc.value)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:11434/v1", "http://localhost:4000", "http://[::1]:8080/v1"]
)
def test_key_preset_may_use_http_to_loopback(url: str) -> None:
    # De sleutel verlaat de Pi niet: bijv. `openai` tegen de Responses-API van een lokale Ollama.
    assert _cfg("openai", base_url=url).base_url == url


def test_override_still_validated() -> None:
    with pytest.raises(LLMConfigError, match="query"):
        _cfg("deepseek", base_url="https://api.deepseek.com/?key=abc")
    with pytest.raises(LLMConfigError, match="https"):
        _cfg("ollama", key=None, base_url="http://ollama.example.com:11434/v1")


def test_generic_openai_compatible_still_needs_base_url() -> None:
    with pytest.raises(LLMConfigError, match="PITOOLBOX_LLM_BASE_URL is verplicht"):
        _cfg("openai-compatible", key=None)


def test_model_still_required_for_preset() -> None:
    with pytest.raises(LLMConfigError, match="PITOOLBOX_LLM_MODEL"):
        LLMConfig.from_settings(
            Settings.model_validate({"llm_provider": "deepseek", "llm_api_key": SecretStr(KEY)})
        )


@pytest.mark.parametrize(("alias", "name"), sorted(ALIASES.items()))
def test_aliases(alias: str, name: str) -> None:
    assert resolve_preset(alias) is PRESETS[name]
    assert _cfg(alias.upper()).preset == name  # hoofdletters worden genormaliseerd


def test_unknown_name_lists_every_valid_name() -> None:
    with pytest.raises(LLMConfigError) as exc:
        _cfg("deepsek")
    msg = str(exc.value)
    assert "'deepsek'" in msg and "--list-providers" in msg
    for name in [*PRESETS, *ALIASES]:
        assert name in msg
    assert KEY not in msg


def test_quirk_flags_per_preset() -> None:
    with_quirks = {name: p.quirks for name, p in PRESETS.items() if p.quirks}
    assert with_quirks == {
        "gemini": frozenset({Quirk.TOOL_CALL_EXTRA_CONTENT}),
        "mistral": frozenset({Quirk.CONTENT_PARTS}),
    }


@pytest.mark.parametrize("name", ["deepseek", "gemini", "ollama"])
def test_key_never_in_repr(name: str) -> None:
    cfg = _cfg(name)
    assert KEY not in repr(cfg) and KEY not in str(cfg.model_dump())
    provider = build_provider(cfg)
    try:
        assert KEY not in repr(provider)
        assert cfg.base_url in repr(provider)
    finally:
        provider.close()
