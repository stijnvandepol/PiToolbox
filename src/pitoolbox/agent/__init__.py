"""Agent-loop (fase 4): LLM + ToolRunner, met bevestigen door alleen een mens.

Los van `pitoolbox.llm` (alleen transport naar providers, zonder kennis van tools of
policy) en van de frontends (terminal nu, later de Flipper-FAP), die allemaal dezelfde
`AgentSession` gebruiken.
"""

from __future__ import annotations

from pitoolbox.agent.prompt import NOTE_PREFIX, SYSTEM_PROMPT
from pitoolbox.agent.session import (
    MODEL_HIDDEN_TOOLS,
    AgentEvent,
    AgentLimits,
    AgentSession,
    AssistantText,
    ConfirmOutcome,
    ConfirmStatus,
    PendingConfirmation,
    ToolCallFinished,
    ToolCallStarted,
    TurnResult,
    TurnStop,
    model_tool_defs,
)
from pitoolbox.agent.text import for_terminal, strip_think

__all__ = [
    "MODEL_HIDDEN_TOOLS",
    "NOTE_PREFIX",
    "SYSTEM_PROMPT",
    "AgentEvent",
    "AgentLimits",
    "AgentSession",
    "AssistantText",
    "ConfirmOutcome",
    "ConfirmStatus",
    "PendingConfirmation",
    "ToolCallFinished",
    "ToolCallStarted",
    "TurnResult",
    "TurnStop",
    "for_terminal",
    "model_tool_defs",
    "strip_think",
]
