"""LLM-providerlaag (fase 4): één interface, meerdere adapters, geen standaardprovider.

Interface (`LLMProvider`):

    complete(messages, *, system=None, tools=(), max_tokens=None) -> Completion

- `messages`: `UserMessage` / `AssistantMessage` / `ToolResult` (zie `llm.types`).
- `tools`: `tool_defs_from_specs(runner.specs())`: exact het JSON-schema van de ToolSpecs.
- `Completion`: tekst, tool-calls (id, naam, geparste JSON-argumenten of een fout),
  genormaliseerde stopreden, usage, en `.message` om aan de geschiedenis toe te voegen.

Adapters:
- `openai-compatible`: Chat Completions op een eigen base-URL (Ollama, llama.cpp, LM Studio,
  vLLM, DeepSeek, Qwen/DashScope, Kimi/Moonshot, GLM/Zhipu, MiniMax, ...).
- `anthropic`: Messages API.
- `openai`: Responses API.

Welke provider en welk model: alleen uit PITOOLBOX_LLM_PROVIDER / PITOOLBOX_LLM_MODEL.
Zonder die twee geeft `build_provider` een `LLMConfigError` met de te zetten variabelen.

Tool-argumenten van het model zijn onbetrouwbaar: deze laag parseert alleen JSON. De
agent-loop moet elke call via `ToolRunner.call` laten lopen (valideren, policy, audit) en
calls met `error` niet uitvoeren maar als fout terugmelden.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

from pitoolbox.llm._anthropic import AnthropicProvider
from pitoolbox.llm._base import HttpProvider
from pitoolbox.llm._openai_compat import OpenAICompatibleProvider
from pitoolbox.llm._openai_responses import OpenAIResponsesProvider
from pitoolbox.llm.config import LLMConfig, ProviderKind
from pitoolbox.llm.types import Completion, Message, ToolDef

if TYPE_CHECKING:
    from pitoolbox.config import Settings


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model(self) -> str: ...

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        tools: Sequence[ToolDef] = (),
        max_tokens: int | None = None,
    ) -> Completion: ...

    def close(self) -> None: ...


_ADAPTERS: dict[ProviderKind, type[HttpProvider]] = {
    ProviderKind.OPENAI_COMPATIBLE: OpenAICompatibleProvider,
    ProviderKind.ANTHROPIC: AnthropicProvider,
    ProviderKind.OPENAI: OpenAIResponsesProvider,
}


def build_provider(
    settings: Settings | LLMConfig | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpProvider:
    """Bouwt de geconfigureerde provider. `LLMConfigError` als er niets (geldigs) is ingesteld.

    `transport` en `sleep` zijn er voor tests (httpx.MockTransport, geen echte wachttijd).
    """
    if isinstance(settings, LLMConfig):
        config = settings
    else:
        if settings is None:
            from pitoolbox.config import Settings

            settings = Settings.from_env()
        config = LLMConfig.from_settings(settings)
    return _ADAPTERS[config.provider](config, transport=transport, sleep=sleep)
