"""Native Anthropic-adapter (Messages API met tool use).

Bronnen:
- Endpoint, body en contentblokken: https://platform.claude.com/docs/en/api/messages —
  `POST /v1/messages`; `model`, `max_tokens`, `messages`, top-level `system`; tools als
  `{name, description, input_schema}`; `tool_use` = `{type, id, name, input}`;
  `tool_result` = `{type, tool_use_id, content?, is_error?}`; `text` heeft minLength 1.
- Headers: https://platform.claude.com/docs/en/api/versioning ("you must send an
  `anthropic-version` request header. For example, `anthropic-version: 2023-06-01`") en
  het curl-voorbeeld in https://platform.claude.com/docs/en/api/errors (`x-api-key`,
  `anthropic-version: 2023-06-01`, `content-type: application/json`).
- Tool-resultaten: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
  — resultaten in één `user`-bericht direct na de tool-use-beurt, "the tool_result blocks
  must come FIRST in the content array".
- Stopredenen en usage: https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
  (end_turn, max_tokens, stop_sequence, tool_use, pause_turn, refusal,
  model_context_window_exceeded; `usage.{input_tokens, output_tokens}`).
- Thinking-blokken: https://platform.claude.com/docs/en/api/errors ("With tool use, every
  `thinking` and `redacted_thinking` block from the assistant turn must be passed back
  exactly as received"). Daarom bewaren we de ruwe `content` in `ProviderState` en sturen
  die ongewijzigd terug.
- `tool_choice` laten we weg: `auto` is de standaard als er tools zijn, en sommige modellen
  weigeren `any`/`tool` (https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools).

Modelnamen staan nergens als standaard; die komen uit PITOOLBOX_LLM_MODEL.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from pitoolbox.llm._base import HttpProvider
from pitoolbox.llm._common import (
    as_dict,
    as_int,
    as_str,
    make_tool_call,
    replay_name,
    require_list,
)
from pitoolbox.llm.config import ProviderKind
from pitoolbox.llm.types import (
    AssistantMessage,
    Completion,
    Message,
    ProviderState,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResult,
    Usage,
    UserMessage,
)

ANTHROPIC_VERSION = "2023-06-01"

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "stop_sequence": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "model_context_window_exceeded": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
    # "pause_turn" hoort bij server-tools, die we niet gebruiken -> OTHER
}


class AnthropicProvider(HttpProvider):
    kind = ProviderKind.ANTHROPIC
    path = "/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._secret() or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "accept": "application/json",
        }

    def _build_body(
        self,
        messages: Sequence[Message],
        *,
        system: str | None,
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> dict[str, Any]:
        turns: list[dict[str, Any]] = []

        def add(role: str, blocks: list[dict[str, Any]]) -> None:
            if not blocks:
                return
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"].extend(blocks)
            else:
                turns.append({"role": role, "content": blocks})

        for m in messages:
            if isinstance(m, UserMessage):
                add("user", [{"type": "text", "text": m.text}] if m.text else [])
            elif isinstance(m, AssistantMessage):
                add("assistant", self._assistant_blocks(m))
            elif isinstance(m, ToolResult):
                block: dict[str, Any] = {"type": "tool_result", "tool_use_id": m.call_id}
                if m.content:
                    block["content"] = m.content
                if m.is_error:
                    block["is_error"] = True
                add("user", [block])

        for turn in turns:
            if turn["role"] == "user":
                # tool_result-blokken eerst, tekst daarna (stabiel gesorteerd).
                turn["content"].sort(key=lambda b: b.get("type") != "tool_result")

        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": turns}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": dict(t.parameters)}
                for t in tools
            ]
        return body

    def _assistant_blocks(self, m: AssistantMessage) -> list[dict[str, Any]]:
        state = self._own_state(m.state)
        raw = state.data.get("content") if state is not None else None
        if isinstance(raw, list):
            return copy.deepcopy(raw)  # ongewijzigd terug (thinking-blokken!)
        blocks: list[dict[str, Any]] = []
        if m.text:
            blocks.append({"type": "text", "text": m.text})
        blocks.extend(
            {
                "type": "tool_use",
                "id": c.id,
                "name": replay_name(c),
                "input": c.arguments if c.arguments is not None else {},
            }
            for c in m.tool_calls
        )
        return blocks

    def _parse(self, data: dict[str, Any]) -> Completion:
        content = require_list(data, "content", "content")
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind == "tool_use":
                calls.append(make_tool_call(block.get("id"), block.get("name"), block.get("input")))
            # thinking/redacted_thinking en onbekende blokken: alleen in de ruwe state
        raw_reason = as_str(data.get("stop_reason"))
        reason = _STOP_REASONS.get(raw_reason or "", StopReason.OTHER)
        if calls and reason in (StopReason.END_TURN, StopReason.OTHER):
            reason = StopReason.TOOL_USE
        usage = as_dict(data.get("usage"))
        return Completion(
            text="\n\n".join(t for t in texts if t),
            tool_calls=tuple(calls),
            stop_reason=reason,
            usage=Usage(
                input_tokens=as_int(usage.get("input_tokens")),
                output_tokens=as_int(usage.get("output_tokens")),
            ),
            raw_stop_reason=raw_reason,
            model=as_str(data.get("model")),
            state=ProviderState(self.kind.value, {"content": content}),
        )
