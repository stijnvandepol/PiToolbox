"""Benoemde providers (presets) voor PITOOLBOX_LLM_PROVIDER.

Een preset koppelt een naam aan een wire-formaat (`ProviderKind`), het officiële endpoint,
of een API-sleutel verplicht is, en eventuele afwijkingen (`Quirk`) die de adapter nodig
heeft om tool-calls te laten werken. Er is **geen standaardpreset en geen standaardmodel**:
PITOOLBOX_LLM_MODEL blijft altijd verplicht. PITOOLBOX_LLM_BASE_URL overschrijft het
endpoint van een preset (bijv. een China-regio) en gaat door `validate_base_url`.

Elk endpoint en elke quirk komt uit de officiële documentatie van de provider, gecontroleerd
op 2026-10-17; de bron staat bij de regel. Cloudpresets gebruiken alleen https.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pitoolbox.llm.errors import LLMConfigError


class ProviderKind(StrEnum):
    """Het wire-formaat: welke adapter het verzoek bouwt."""

    OPENAI_COMPATIBLE = "openai-compatible"  # Chat Completions: Ollama, vLLM, DeepSeek, ...
    ANTHROPIC = "anthropic"  # Messages API
    OPENAI = "openai"  # Responses API


class Quirk(StrEnum):
    """Afwijking van een preset die de OpenAI-compatibele adapter moet volgen."""

    # Gemini 3: `tool_calls[].extra_content` (thought signature) ongewijzigd terugsturen.
    TOOL_CALL_EXTRA_CONTENT = "tool_call_extra_content"
    # Mistral: `content` als lijst (thinking + tekst) ongewijzigd terugsturen.
    CONTENT_PARTS = "content_parts"
    # MiniMax: fouten staan als `base_resp.status_code` in een HTTP 200-antwoord.
    BASE_RESP = "base_resp"


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    kind: ProviderKind
    base_url: str | None  # None: PITOOLBOX_LLM_BASE_URL is verplicht
    requires_key: bool
    quirks: frozenset[Quirk] = frozenset()


_COMPAT = ProviderKind.OPENAI_COMPATIBLE

_PRESET_LIST = (
    # --- wire-formaten (zoals vóór de presets) ---
    ProviderPreset("openai-compatible", _COMPAT, None, requires_key=False),
    # https://platform.claude.com/docs/en/api/errors (curl naar https://api.anthropic.com/v1/...)
    ProviderPreset("anthropic", ProviderKind.ANTHROPIC, "https://api.anthropic.com", True),
    # https://developers.openai.com/api/reference/overview (curl naar https://api.openai.com/v1)
    ProviderPreset("openai", ProviderKind.OPENAI, "https://api.openai.com/v1", True),
    # --- cloud, Chat Completions ---
    # https://api-docs.deepseek.com/ ("base_url (OpenAI) | https://api.deepseek.com"). Tools en
    # thinking: https://api-docs.deepseek.com/guides/thinking_mode (reasoning_content moet terug
    # bij tools; dat doet de adapter altijd).
    ProviderPreset("deepseek", _COMPAT, "https://api.deepseek.com", True),
    # https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions:
    # "Singapore: from https://dashscope-intl.aliyuncs.com to https://{WorkspaceId}.ap-southeast-1
    # .maas.aliyuncs.com ... The existing domain remains fully functional." Pad /compatible-mode/v1.
    # China (Beijing): https://dashscope.aliyuncs.com/compatible-mode/v1 via BASE_URL.
    ProviderPreset("qwen", _COMPAT, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True),
    # https://platform.kimi.ai/docs/api/chat.md (base_url="https://api.moonshot.ai/v1");
    # China: https://platform.moonshot.cn/docs/overview.md (https://api.moonshot.cn/v1).
    ProviderPreset("kimi", _COMPAT, "https://api.moonshot.ai/v1", True),
    # https://docs.z.ai/api-reference/introduction.md ("https://api.z.ai/api/paas/v4");
    # China (Zhipu): https://docs.bigmodel.cn/cn/api/introduction.md (open.bigmodel.cn/api/paas/v4).
    ProviderPreset("glm", _COMPAT, "https://api.z.ai/api/paas/v4", True),
    # https://platform.minimax.io/docs/guides/text-m3-function-call.md ("For international
    # users, use https://api.minimax.io/v1; for users in China, use https://api.minimax.cn/v1").
    # Quirk: https://platform.minimax.io/docs/api-reference/text-chat-openai (alleen een
    # 200-antwoord gedocumenteerd, met `base_resp` "Error status code and details").
    ProviderPreset(
        "minimax", _COMPAT, "https://api.minimax.io/v1", True, frozenset({Quirk.BASE_RESP})
    ),
    # https://docs.mistral.ai/openapi.yaml (server https://api.mistral.ai, pad
    # /v1/chat/completions).
    # Quirk: https://docs.mistral.ai/studio-api/conversations/reasoning ("message.content is a
    # list of chunks", "Do not strip ThinkChunk from assistant messages before replaying them").
    ProviderPreset(
        "mistral", _COMPAT, "https://api.mistral.ai/v1", True, frozenset({Quirk.CONTENT_PARTS})
    ),
    # https://console.groq.com/docs/openai ("https://api.groq.com/openai/v1").
    ProviderPreset("groq", _COMPAT, "https://api.groq.com/openai/v1", True),
    # https://openrouter.ai/docs/quickstart (baseURL 'https://openrouter.ai/api/v1'). De
    # attributieheaders (HTTP-Referer, X-OpenRouter-Title) zijn optioneel en sturen we niet.
    ProviderPreset("openrouter", _COMPAT, "https://openrouter.ai/api/v1", True),
    # https://docs.x.ai/developers/rest-api-reference/inference (https://api.x.ai, Bearer-key);
    # Chat Completions: https://docs.x.ai/developers/model-capabilities/legacy/chat-completions.
    ProviderPreset("xai", _COMPAT, "https://api.x.ai/v1", True),
    # https://ai.google.dev/gemini-api/docs/openai
    # (base_url="https://generativelanguage.googleapis.com/v1beta/openai/"). Quirk: "you must
    # pass back thought signatures during function calling, otherwise you will get a validation
    # error (4xx)", in `extra_content.google.thought_signature` per tool-call: Googles
    # thought-signatures-pagina (archief: https://web.archive.org/web/20260608134601/
    # https://ai.google.dev/gemini-api/docs/thought-signatures); de huidige openai-pagina
    # verwijst ernaar ("Gemini 3 supports OpenAI compatibility for thought signatures").
    ProviderPreset(
        "gemini",
        _COMPAT,
        "https://generativelanguage.googleapis.com/v1beta/openai",
        True,
        frozenset({Quirk.TOOL_CALL_EXTRA_CONTENT}),
    ),
    # --- lokaal (geen sleutel nodig; http alleen naar loopback, zie validate_base_url) ---
    # https://docs.ollama.com/api/openai-compatibility (base_url='http://localhost:11434/v1/';
    # "Ollama binds 127.0.0.1 port 11434 by default").
    ProviderPreset("ollama", _COMPAT, "http://127.0.0.1:11434/v1", False),
    # https://lmstudio.ai/docs/developer/rest/quickstart ("By default, the server is available
    # at http://localhost:1234"); tools: https://lmstudio.ai/docs/developer/openai-compat/tools.
    ProviderPreset("lmstudio", _COMPAT, "http://localhost:1234/v1", False),
    # https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md ("--port PORT |
    # port to listen (default: 9931)", "--host HOST ... (default: 127.0.0.1)").
    ProviderPreset("llamacpp", _COMPAT, "http://127.0.0.1:9931/v1", False),
    # https://github.com/vllm-project/vllm/blob/main/docs/getting_started/quickstart.md ("By
    # default, it starts the server at http://localhost:8000"). Tools alleen met
    # `--enable-auto-tool-choice --tool-call-parser ...`, anders komt er stil gewone tekst
    # (https://docs.vllm.ai/en/latest/features/tool_calling/).
    ProviderPreset("vllm", _COMPAT, "http://localhost:8000/v1", False),
)

PRESETS: dict[str, ProviderPreset] = {p.name: p for p in _PRESET_LIST}

# Bewust weinig: alleen de andere bekende naam van dezelfde provider.
ALIASES: dict[str, str] = {"moonshot": "kimi", "zhipu": "glm"}


def resolve_preset(name: str) -> ProviderPreset:
    """Preset bij een (al genormaliseerde) naam of alias; `LLMConfigError` met alle namen."""
    preset = PRESETS.get(ALIASES.get(name, name))
    if preset is None:
        choices = ", ".join([*PRESETS, *ALIASES])
        raise LLMConfigError(
            f"Onbekende PITOOLBOX_LLM_PROVIDER {name!r}; kies uit: {choices} "
            "(details: pitoolbox-agent --list-providers)"
        )
    return preset
