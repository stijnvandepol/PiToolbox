"""LLM-providerlaag voor de agent-loop (fase 4). Zie `providers.py` voor het ontwerp."""

from __future__ import annotations

from pitoolbox.llm.config import LLMConfig, ProviderKind, validate_base_url
from pitoolbox.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)
from pitoolbox.llm.providers import LLMProvider, build_provider
from pitoolbox.llm.types import (
    AssistantMessage,
    Completion,
    Message,
    ProviderState,
    StopReason,
    ToolCall,
    ToolCallError,
    ToolDef,
    ToolResult,
    Usage,
    UserMessage,
    tool_defs_from_specs,
)

__all__ = [
    "AssistantMessage",
    "Completion",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMConfig",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
    "Message",
    "ProviderKind",
    "ProviderState",
    "StopReason",
    "ToolCall",
    "ToolCallError",
    "ToolDef",
    "ToolResult",
    "Usage",
    "UserMessage",
    "build_provider",
    "tool_defs_from_specs",
    "validate_base_url",
]
