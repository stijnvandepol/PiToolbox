"""Generieke OpenAI-compatibele adapter (Chat Completions met tools).

Werkt met elke server die `POST {base_url}/chat/completions` spreekt: Ollama, llama.cpp
(`llama-server --jinja`), LM Studio, vLLM (`--enable-auto-tool-choice --tool-call-parser
...`), DeepSeek, Qwen/DashScope, Kimi/Moonshot, GLM/Zhipu, MiniMax.

Bronnen:
- Protocol: https://platform.openai.com/docs/api-reference/chat/create en
  https://platform.openai.com/docs/api-reference/chat/object — `tools[]` =
  `{"type": "function", "function": {name, description, parameters}}`; antwoord
  `choices[0].message.{content, tool_calls[{id, type, function{name, arguments}}]}`,
  `arguments` is een JSON-tekst ("the model does not always generate valid JSON ...
  Validate the arguments in your code"); `finish_reason` in stop/length/tool_calls/
  content_filter/function_call; `usage.{prompt_tokens, completion_tokens}`; tool-resultaat
  als `{"role": "tool", "tool_call_id", "content"}`.
- Ollama: https://docs.ollama.com/api/openai-compatibility — ondersteunt `max_tokens` en
  `tools`, maar niet `tool_choice` en (in de lijst) niet `max_completion_tokens`. Daarom
  sturen we `max_tokens` en laten we `tool_choice` weg. De native `/api/chat` geeft
  `arguments` als object en zonder `id` (https://docs.ollama.com/api/chat).
- llama.cpp: https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md —
  het voorbeeldantwoord heeft `finish_reason: "tool"` en tool-calls zonder `id` en zonder
  `function`-omhulsel (`{"name", "arguments"}` direct). Beide vormen worden geaccepteerd.
- Kimi/Moonshot: https://platform.kimi.ai/docs/guide/use-kimi-api-to-complete-tool-calls —
  "we need to provide the tool_call_id and name fields in the message" (rol `tool`).
  Daarom sturen we `name` mee; andere servers negeren het.
- DeepSeek: https://api-docs.deepseek.com/guides/thinking_mode — met tools moet
  `reasoning_content` in alle volgende verzoeken terug ("the API will return a 400 error").
- MiniMax: https://platform.minimax.io/docs/guides/text-m2-function-call — het volledige
  assistant-bericht incl. `reasoning_details` bewaren, en `content` niet wijzigen.
  Daarom bewaren we die velden in `ProviderState` en laten we `content` ongemoeid.

Fouten in een HTTP 200-antwoord (OpenRouter `error`, `finish_reason` "error" of
"network_error") worden een gewone `LLMError` met retries; zie `_body_errors.py`.

Afwijkingen per preset (`Quirk`, gezet via `LLMConfig.quirks`; bronnen in `presets.py`):
- `TOOL_CALL_EXTRA_CONTENT` (Gemini): `tool_calls[].extra_content` (de thought signature)
  per call bewaren en ongewijzigd terugsturen; zonder geeft Gemini 3 een 400.
- `CONTENT_PARTS` (Mistral): is `content` een lijst (thinking- en tekstblokken), dan die
  lijst ongewijzigd terugsturen in plaats van alleen de tekst.
- `BASE_RESP` (MiniMax): een niet-nul `base_resp.status_code` in een 200-antwoord is een
  fout (zie `_body_errors.py`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pitoolbox.llm._base import HttpProvider
from pitoolbox.llm._body_errors import minimax_base_resp_error, openai_compat_body_error
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
from pitoolbox.llm.errors import LLMError, LLMResponseError
from pitoolbox.llm.presets import Quirk
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

# Velden van het assistant-bericht die ongewijzigd terug moeten (zie bronnen hierboven).
_REPLAY_FIELDS = ("reasoning_content", "reasoning_details")
# Sleutels in `ProviderState.data` voor de quirks (naast `_REPLAY_FIELDS`).
_STATE_CALL_EXTRA = "tool_call_extra_content"  # {call_id: extra_content}
_STATE_CONTENT = "content"

_FINISH_REASONS = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,  # verouderd, nog steeds gedocumenteerd
    "tool": StopReason.TOOL_USE,  # llama.cpp
    "content_filter": StopReason.REFUSAL,
}


def stop_reason_with_calls(reason: StopReason, calls: Sequence[ToolCall]) -> StopReason:
    """Lokale servers melden soms "stop" terwijl er tool-calls zijn: de calls winnen."""
    if calls and reason in (StopReason.END_TURN, StopReason.OTHER):
        return StopReason.TOOL_USE
    return reason


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content parts: [{"type": "text", "text": ...}]
        return "".join(
            p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return ""


class OpenAICompatibleProvider(HttpProvider):
    kind = ProviderKind.OPENAI_COMPATIBLE
    path = "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        secret = self._secret()
        if secret:  # lokale servers hebben meestal geen sleutel nodig
            headers["Authorization"] = f"Bearer {secret}"
        return headers

    def _build_body(
        self,
        messages: Sequence[Message],
        *,
        system: str | None,
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.text})
            elif isinstance(m, AssistantMessage):
                out.append(self._assistant(m))
            elif isinstance(m, ToolResult):
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.call_id,
                        "name": m.name,
                        "content": m.content,
                    }
                )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": out,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": dict(t.parameters),
                    },
                }
                for t in tools
            ]
        return body

    def _assistant(self, m: AssistantMessage) -> dict[str, Any]:
        state = self._own_state(m.state)
        data = state.data if state is not None else {}
        content = data.get(_STATE_CONTENT, m.text)
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if m.tool_calls:
            extra = data.get(_STATE_CALL_EXTRA, {})
            msg["tool_calls"] = [self._replay_call(c, extra.get(c.id)) for c in m.tool_calls]
        for key in _REPLAY_FIELDS:
            if key in data:
                msg[key] = data[key]
        return msg

    @staticmethod
    def _replay_call(call: ToolCall, extra_content: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": call.id,
            "type": "function",
            "function": {"name": replay_name(call), "arguments": replay_arguments(call)},
        }
        if extra_content is not None:
            out["extra_content"] = extra_content
        return out

    def _body_error(self, data: dict[str, Any], clean: Callable[[str], str]) -> LLMError | None:
        if Quirk.BASE_RESP in self.config.quirks:
            err = minimax_base_resp_error(data, clean)
            if err is not None:
                return err
        return openai_compat_body_error(data, clean)

    def _parse(self, data: dict[str, Any]) -> Completion:
        choices = require_list(data, "choices", "choices")
        if not choices or not isinstance(choices[0], dict):
            raise LLMResponseError("onverwacht antwoord: geen keuze in choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LLMResponseError("onverwacht antwoord: choices[0].message ontbreekt")

        calls, call_extra = self._parse_calls(message.get("tool_calls"))
        raw_reason = as_str(choice.get("finish_reason"))
        reason = _FINISH_REASONS.get(raw_reason or "", StopReason.OTHER)
        usage = as_dict(data.get("usage"))
        replay = {k: message[k] for k in _REPLAY_FIELDS if message.get(k) is not None}
        if call_extra:
            replay[_STATE_CALL_EXTRA] = call_extra
        if Quirk.CONTENT_PARTS in self.config.quirks and isinstance(message.get("content"), list):
            replay[_STATE_CONTENT] = message["content"]
        return Completion(
            text=_content_text(message.get("content")),
            tool_calls=tuple(calls),
            stop_reason=stop_reason_with_calls(reason, calls),
            usage=Usage(
                input_tokens=as_int(usage.get("prompt_tokens")),
                output_tokens=as_int(usage.get("completion_tokens")),
            ),
            raw_stop_reason=raw_reason,
            model=as_str(data.get("model")),
            state=ProviderState(self.kind.value, replay) if replay else None,
        )

    def _parse_calls(self, raw_calls: Any) -> tuple[list[ToolCall], dict[str, Any]]:
        """Tool-calls plus (bij `TOOL_CALL_EXTRA_CONTENT`) hun `extra_content` per call-id."""
        keep_extra = Quirk.TOOL_CALL_EXTRA_CONTENT in self.config.quirks
        calls: list[ToolCall] = []
        extra: dict[str, Any] = {}
        for tc in raw_calls if isinstance(raw_calls, list) else []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            fn = fn if isinstance(fn, dict) else tc  # llama.cpp: name/arguments direct
            call = make_tool_call(tc.get("id"), fn.get("name"), fn.get("arguments"))
            calls.append(call)
            if keep_extra and tc.get("extra_content") is not None:
                extra[call.id] = tc["extra_content"]
        return calls, extra
