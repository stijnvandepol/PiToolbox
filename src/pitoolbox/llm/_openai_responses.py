"""Native OpenAI-adapter (Responses API met function tools).

Bronnen:
- Endpoint en vormen: https://platform.openai.com/docs/api-reference/responses/create —
  `POST /v1/responses`; antwoord met `status`, `output[]` (items `message` met
  `content[{type: "output_text", text}]` en `function_call` met `{id, call_id, name,
  arguments}`), `incomplete_details`, `error`, `usage.{input_tokens, output_tokens}`.
- Function calling: https://platform.openai.com/docs/guides/function-calling — tools zijn
  vlak `{type: "function", name, description, parameters, strict}`; resultaat terug als
  `{type: "function_call_output", call_id, output}`. Over `strict`: "If you omit strict ...
  Responses requests will attempt to normalize your schema into strict mode ... To opt out
  ... explicitly set strict: false." Wij zetten `strict: false`: het schema blijft exact
  `ToolSpec.json_schema()` (met optionele velden) en de ToolRunner valideert.
- `incomplete_details.reason`: "max_output_tokens" of "content_filter"
  (https://platform.openai.com/docs/api-reference/responses).
- Stateless: `store: false`, en "preserve every item in the response's output array ...
  Replaying the complete output keeps reasoning items ... intact"
  (https://developers.openai.com/api/docs/guides/conversation-state?api-mode=responses);
  in stateless mode bevatten reasoning-items standaard `encrypted_content`
  (https://developers.openai.com/api/docs/guides/reasoning?api-mode=responses). Daarom
  bewaren we `output` in `ProviderState` en sturen die ongewijzigd terug. Geen
  `previous_response_id` (Ollama ondersteunt geen stateful Responses:
  https://docs.ollama.com/api/openai-compatibility).
- Fouten: https://developers.openai.com/api/docs/guides/error-codes.
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
    replay_arguments,
    replay_name,
    require_list,
)
from pitoolbox.llm.config import ProviderKind
from pitoolbox.llm.errors import LLMResponseError
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

_INCOMPLETE_REASONS = {
    "max_output_tokens": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAIResponsesProvider(HttpProvider):
    kind = ProviderKind.OPENAI
    path = "/responses"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret() or ''}", "Accept": "application/json"}

    def _build_body(
        self,
        messages: Sequence[Message],
        *,
        system: str | None,
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, UserMessage):
                items.append({"role": "user", "content": m.text})
            elif isinstance(m, AssistantMessage):
                items.extend(self._assistant_items(m))
            elif isinstance(m, ToolResult):
                items.append(
                    {"type": "function_call_output", "call_id": m.call_id, "output": m.content}
                )
        body: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if system:
            body["instructions"] = system
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": dict(t.parameters),
                    "strict": False,
                }
                for t in tools
            ]
        return body

    def _assistant_items(self, m: AssistantMessage) -> list[dict[str, Any]]:
        state = self._own_state(m.state)
        raw = state.data.get("output") if state is not None else None
        if isinstance(raw, list):
            return copy.deepcopy(raw)
        items: list[dict[str, Any]] = []
        if m.text:
            items.append({"role": "assistant", "content": m.text})
        items.extend(
            {
                "type": "function_call",
                "call_id": c.id,
                "name": replay_name(c),
                "arguments": replay_arguments(c),
            }
            for c in m.tool_calls
        )
        return items

    def _parse(self, data: dict[str, Any]) -> Completion:
        status = as_str(data.get("status"))
        if status == "failed":
            err = as_dict(data.get("error"))
            code = as_str(err.get("code"))
            raise LLMResponseError(
                f"provider meldt status 'failed' ({code or 'onbekend'})", provider_code=code
            )
        output = require_list(data, "output", "output")
        texts: list[str] = []
        calls: list[ToolCall] = []
        refused = False
        for item in output:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "message":
                parts = item.get("content")
                for part in parts if isinstance(parts, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        texts.append(part["text"])
                    elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                        texts.append(part["refusal"])
                        refused = True
            elif kind == "function_call":
                calls.append(
                    make_tool_call(item.get("call_id"), item.get("name"), item.get("arguments"))
                )

        raw_reason: str | None = status
        if status == "incomplete":
            raw_reason = as_str(as_dict(data.get("incomplete_details")).get("reason"))
            reason = _INCOMPLETE_REASONS.get(raw_reason or "", StopReason.OTHER)
        elif calls:
            reason = StopReason.TOOL_USE
        elif refused:
            reason = StopReason.REFUSAL
        elif status == "completed":
            reason = StopReason.END_TURN
        else:
            reason = StopReason.OTHER

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
            state=ProviderState(self.kind.value, {"output": output}),
        )
