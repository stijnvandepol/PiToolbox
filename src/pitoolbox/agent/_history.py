"""Grootte van de gespreksgeschiedenis en waar de oudste hele beurten mogen wegvallen."""

from __future__ import annotations

import json

from pitoolbox.llm.types import Message, ToolResult, UserMessage


def message_chars(message: Message) -> int:
    """Ruwe grootte van een bericht in tekens, inclusief tool calls en providerstate."""
    if isinstance(message, UserMessage):
        return len(message.text)
    if isinstance(message, ToolResult):
        return len(message.content) + len(message.name)
    size = len(message.text) + sum(len(c.name) + len(c.raw_arguments) for c in message.tool_calls)
    if message.state is not None:
        size += len(json.dumps(message.state.data, default=str, ensure_ascii=False))
    return size


def history_cut(history: list[Message], budget: int) -> int:
    """Aantal berichten dat vooraan weg moet om onder `budget` tekens te komen; 0 = niets.

    Knipt alleen vlak vóór een `UserMessage` (of na het laatste bericht), zodat er hele beurten
    wegvallen: nooit een resultaat zonder zijn tool call.
    """
    sizes = [message_chars(m) for m in history]
    total = sum(sizes)
    if total <= budget:
        return 0
    starts = [i for i, m in enumerate(history) if isinstance(m, UserMessage)]
    cut = 0
    for start in [*starts[1:], len(history)]:
        cut = start
        if total - sum(sizes[:cut]) <= budget:
            break
    return cut
