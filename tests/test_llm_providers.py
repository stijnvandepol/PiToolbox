"""Tests voor de drie adapters: verzoekvorm, toolschema's, normalisatie, quirks, replay.

Geen echte netwerkverkeer: alles via httpx.MockTransport.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from pitoolbox.core.tools import build_registry
from pitoolbox.llm import (
    AssistantMessage,
    Completion,
    LLMAuthError,
    LLMConfig,
    LLMRateLimitError,
    LLMResponseError,
    ProviderKind,
    ProviderState,
    StopReason,
    ToolCall,
    ToolCallError,
    ToolDef,
    ToolResult,
    Usage,
    UserMessage,
    build_provider,
    tool_defs_from_specs,
)
from pitoolbox.llm._base import HttpProvider
from pitoolbox.llm._common import SYNTHETIC_ID_PREFIX

KEY = "sk-test-geheim-1234567890"
# Alleen als voorbeeld in tests; nergens een standaard.
ANTHROPIC_MODEL = "claude-opus-5-5"
TOOL = ToolDef(
    name="wifi_scan",
    description="Scan passief.",
    parameters={
        "type": "object",
        "properties": {"seconds": {"type": "integer"}},
        "additionalProperties": False,
    },
)


class Recorder:
    def __init__(self, *bodies: dict[str, Any], status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.bodies.pop(0))

    @property
    def sent(self) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.requests[-1].content)
        return body


def _provider(
    kind: ProviderKind, rec: Recorder, *, key: str | None = KEY, base_url: str | None = None
) -> HttpProvider:
    urls = {
        ProviderKind.OPENAI_COMPATIBLE: "http://127.0.0.1:11434/v1",
        ProviderKind.ANTHROPIC: "https://api.anthropic.com",
        ProviderKind.OPENAI: "https://api.openai.com/v1",
    }
    model = ANTHROPIC_MODEL if kind is ProviderKind.ANTHROPIC else "test-model"
    cfg = LLMConfig(
        provider=kind,
        model=model,
        base_url=base_url or urls[kind],
        api_key=SecretStr(key) if key else None,
        max_retries=1,
        max_tokens=321,
    )
    return build_provider(cfg, transport=httpx.MockTransport(rec), sleep=lambda _s: None)


def _chat_response(message: dict[str, Any], finish: str | None = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


HISTORY = [UserMessage("scan de wifi")]


# ============================ toolschema's ============================


def test_tool_defs_reuse_toolspec_schema_and_description() -> None:
    specs = build_registry().all()
    defs = tool_defs_from_specs(specs)
    assert [d.name for d in defs] == [s.name for s in specs]
    for d, s in zip(defs, specs, strict=True):
        assert d.parameters == s.json_schema()
        assert d.description == s.full_description()
        assert d.parameters["additionalProperties"] is False  # extra="forbid" blijft


def test_tool_defs_are_copies() -> None:
    spec = build_registry().all()[0]
    d = tool_defs_from_specs([spec])[0]
    d.parameters["properties"]["hack"] = {}  # type: ignore[index]
    assert "hack" not in spec.json_schema().get("properties", {})


@pytest.mark.parametrize("kind", list(ProviderKind))
def test_all_registry_tools_are_sent_unchanged(kind: ProviderKind) -> None:
    defs = tool_defs_from_specs(build_registry().all())
    responses = {
        ProviderKind.OPENAI_COMPATIBLE: _chat_response({"role": "assistant", "content": "x"}),
        ProviderKind.ANTHROPIC: {
            "content": [{"type": "text", "text": "x"}],
            "stop_reason": "end_turn",
        },
        ProviderKind.OPENAI: {"status": "completed", "output": []},
    }
    rec = Recorder(responses[kind])
    with _provider(kind, rec) as p:
        p.complete(HISTORY, tools=defs)
    sent = rec.sent["tools"]
    if kind is ProviderKind.OPENAI_COMPATIBLE:
        schemas = [(t["function"]["name"], t["function"]["parameters"]) for t in sent]
    elif kind is ProviderKind.ANTHROPIC:
        schemas = [(t["name"], t["input_schema"]) for t in sent]
    else:
        schemas = [(t["name"], t["parameters"]) for t in sent]
    assert schemas == [(d.name, dict(d.parameters)) for d in defs]


# ============================ OpenAI-compatible ============================


def test_compat_request_shape() -> None:
    rec = Recorder(_chat_response({"role": "assistant", "content": "hoi"}))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY, system="Je bent PiToolbox.", tools=[TOOL])
    req = rec.requests[0]
    assert str(req.url) == "http://127.0.0.1:11434/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert rec.sent == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Je bent PiToolbox."},
            {"role": "user", "content": "scan de wifi"},
        ],
        "max_tokens": 321,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "wifi_scan",
                    "description": "Scan passief.",
                    "parameters": dict(TOOL.parameters),
                },
            }
        ],
    }
    assert "tool_choice" not in rec.sent  # Ollama ondersteunt het niet
    assert out.text == "hoi"
    assert out.stop_reason is StopReason.END_TURN
    assert (out.usage.input_tokens, out.usage.output_tokens) == (11, 7)
    assert out.model == "test-model"
    assert out.tool_calls == ()


def test_compat_without_key_sends_no_authorization() -> None:
    rec = Recorder(_chat_response({"role": "assistant", "content": "hoi"}))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec, key=None) as p:
        p.complete(HISTORY)
    assert "authorization" not in rec.requests[0].headers
    assert "tools" not in rec.sent


def test_compat_max_tokens_override() -> None:
    rec = Recorder(_chat_response({"role": "assistant", "content": "x"}))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        p.complete(HISTORY, max_tokens=50)
    assert rec.sent["max_tokens"] == 50


def test_compat_multiple_tool_calls_with_text() -> None:
    msg = {
        "role": "assistant",
        "content": "Ik scan eerst.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "wifi_scan", "arguments": '{"seconds": 5}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "status", "arguments": "{}"},
            },
        ],
    }
    rec = Recorder(_chat_response(msg, "tool_calls"))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY, tools=[TOOL])
    assert out.text == "Ik scan eerst."
    assert out.stop_reason is StopReason.TOOL_USE
    assert out.tool_calls == (
        ToolCall(
            id="call_1", name="wifi_scan", arguments={"seconds": 5}, raw_arguments='{"seconds": 5}'
        ),
        ToolCall(id="call_2", name="status", arguments={}, raw_arguments="{}"),
    )


def test_compat_ollama_quirks_args_object_and_missing_id() -> None:
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "wifi_scan", "arguments": {"seconds": 3}}},
            {"function": {"name": "status", "arguments": {}}},
        ],
    }
    rec = Recorder(_chat_response(msg, "stop"))  # sommige servers zeggen "stop"
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY, tools=[TOOL])
    assert out.stop_reason is StopReason.TOOL_USE
    first, second = out.tool_calls
    assert first.ok and first.arguments == {"seconds": 3}
    assert json.loads(first.raw_arguments) == {"seconds": 3}
    assert first.id.startswith(SYNTHETIC_ID_PREFIX)
    assert second.id.startswith(SYNTHETIC_ID_PREFIX) and second.id != first.id


def test_compat_llamacpp_flat_tool_call_and_finish_tool() -> None:
    # Vorm uit llama.cpp docs/function-calling.md: geen id, geen "function"-omhulsel.
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"name": "wifi_scan", "arguments": '{"seconds":1}'}],
    }
    rec = Recorder(_chat_response(msg, "tool"))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY)
    assert out.text == ""
    assert out.stop_reason is StopReason.TOOL_USE
    assert out.raw_stop_reason == "tool"
    assert out.tool_calls[0].name == "wifi_scan"
    assert out.tool_calls[0].arguments == {"seconds": 1}


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ('{"seconds": 5', ToolCallError.INVALID_JSON),
        ("[1, 2]", ToolCallError.NOT_AN_OBJECT),
        ('"tekst"', ToolCallError.NOT_AN_OBJECT),
        (42, ToolCallError.NOT_AN_OBJECT),
        ([1], ToolCallError.NOT_AN_OBJECT),
    ],
)
def test_compat_malformed_arguments_become_error(raw: Any, error: ToolCallError) -> None:
    msg = {
        "role": "assistant",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "x", "arguments": raw}}
        ],
    }
    rec = Recorder(_chat_response(msg, "tool_calls"))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY)
    call = out.tool_calls[0]
    assert not call.ok
    assert call.error is error
    assert call.arguments is None


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_compat_empty_arguments_mean_no_arguments(raw: Any) -> None:
    msg = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "function": {"name": "status", "arguments": raw}}],
    }
    rec = Recorder(_chat_response(msg, "tool_calls"))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY)
    assert out.tool_calls[0].arguments == {}


def test_compat_missing_tool_name() -> None:
    msg = {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"arguments": "{}"}}]}
    rec = Recorder(_chat_response(msg, "tool_calls"))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY)
    assert out.tool_calls[0].error is ToolCallError.MISSING_NAME


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("stop", StopReason.END_TURN),
        ("length", StopReason.MAX_TOKENS),
        ("tool_calls", StopReason.TOOL_USE),
        ("function_call", StopReason.TOOL_USE),
        ("content_filter", StopReason.REFUSAL),
        ("iets_nieuws", StopReason.OTHER),
        (None, StopReason.OTHER),
    ],
)
def test_compat_finish_reasons(finish: str | None, expected: StopReason) -> None:
    rec = Recorder(_chat_response({"role": "assistant", "content": "x"}, finish))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        assert p.complete(HISTORY).stop_reason is expected


def test_compat_content_parts_are_joined() -> None:
    msg = {
        "role": "assistant",
        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
    }
    rec = Recorder(_chat_response(msg))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        assert p.complete(HISTORY).text == "ab"


@pytest.mark.parametrize(
    "body",
    [{}, {"choices": []}, {"choices": [{"message": None}]}, {"choices": "x"}],
)
def test_compat_malformed_response(body: dict[str, Any]) -> None:
    rec = Recorder(body)
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p, pytest.raises(LLMResponseError):
        p.complete(HISTORY)


def test_compat_history_replay_with_reasoning_content() -> None:
    """DeepSeek: reasoning_content moet terug; tool-resultaten als role=tool met name (Kimi)."""
    first = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "denk denk",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "wifi_scan", "arguments": "{}"},
            },
            {"id": "call_2", "type": "function", "function": {"name": "kapot", "arguments": "{"}},
        ],
    }
    rec = Recorder(
        _chat_response(first, "tool_calls"),
        _chat_response({"role": "assistant", "content": "klaar"}),
    )
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        out = p.complete(HISTORY, tools=[TOOL])
        assert out.state == ProviderState("openai-compatible", {"reasoning_content": "denk denk"})
        history = [
            *HISTORY,
            out.message,
            ToolResult(call_id="call_1", name="wifi_scan", content='{"ok": true}'),
            ToolResult(call_id="call_2", name="kapot", content='{"ok": false}', is_error=True),
        ]
        final = p.complete(history, tools=[TOOL])
    assert final.text == "klaar"
    msgs = rec.sent["messages"]
    assert msgs[1] == {
        "role": "assistant",
        "content": "",
        "reasoning_content": "denk denk",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "wifi_scan", "arguments": "{}"},
            },
            # ongeldige JSON wordt niet teruggestuurd
            {"id": "call_2", "type": "function", "function": {"name": "kapot", "arguments": "{}"}},
        ],
    }
    assert msgs[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "wifi_scan",
        "content": '{"ok": true}',
    }
    assert msgs[3]["tool_call_id"] == "call_2"


def test_compat_ignores_foreign_state() -> None:
    rec = Recorder(_chat_response({"role": "assistant", "content": "x"}))
    foreign = AssistantMessage(text="eerder", state=ProviderState("anthropic", {"content": []}))
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p:
        p.complete([*HISTORY, foreign, UserMessage("verder")])
    assert rec.sent["messages"][1] == {"role": "assistant", "content": "eerder"}


def test_compat_rate_limit_retry_after() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not sleeps:
            return httpx.Response(
                429, headers={"retry-after": "2"}, json={"error": {"message": "slow down"}}
            )
        return httpx.Response(200, json=_chat_response({"role": "assistant", "content": "ok"}))

    cfg = LLMConfig(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="m",
        base_url="https://api.deepseek.com",
        max_retries=1,
    )
    with build_provider(cfg, transport=httpx.MockTransport(handler), sleep=sleeps.append) as p:
        assert p.complete(HISTORY).text == "ok"
    assert sleeps == [2.0]


def test_compat_rate_limit_exhausted() -> None:
    rec = Recorder({"error": {"message": "slow"}}, {"error": {"message": "slow"}}, status=429)
    with _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p, pytest.raises(LLMRateLimitError):
        p.complete(HISTORY)
    assert len(rec.requests) == 2  # max_retries=1


def test_complete_requires_messages() -> None:
    rec = Recorder()
    with (
        _provider(ProviderKind.OPENAI_COMPATIBLE, rec) as p,
        pytest.raises(ValueError, match="bericht"),
    ):
        p.complete([])


# ============================ Anthropic ============================


def _anthropic_response(content: list[dict[str, Any]], stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": ANTHROPIC_MODEL,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def test_anthropic_request_shape() -> None:
    rec = Recorder(_anthropic_response([{"type": "text", "text": "Hallo"}]))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        out = p.complete(HISTORY, system="Je bent PiToolbox.", tools=[TOOL])
    req = rec.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["content-type"] == "application/json"
    assert "authorization" not in req.headers
    assert rec.sent == {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 321,
        "system": "Je bent PiToolbox.",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "scan de wifi"}]}],
        "tools": [
            {
                "name": "wifi_scan",
                "description": "Scan passief.",
                "input_schema": dict(TOOL.parameters),
            }
        ],
    }
    assert "tool_choice" not in rec.sent
    assert out.text == "Hallo"
    assert out.stop_reason is StopReason.END_TURN
    assert (out.usage.input_tokens, out.usage.output_tokens) == (100, 50)


def test_anthropic_text_and_multiple_tool_uses() -> None:
    content = [
        {"type": "thinking", "thinking": "geheim", "signature": "sig"},
        {"type": "text", "text": "Ik kijk even."},
        {"type": "tool_use", "id": "toolu_1", "name": "wifi_scan", "input": {"seconds": 5}},
        {"type": "tool_use", "id": "toolu_2", "name": "status", "input": {}},
    ]
    rec = Recorder(_anthropic_response(content, "tool_use"))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        out = p.complete(HISTORY, tools=[TOOL])
    assert out.text == "Ik kijk even."  # thinking zit niet in de tekst
    assert out.stop_reason is StopReason.TOOL_USE
    assert [(c.id, c.name, c.arguments) for c in out.tool_calls] == [
        ("toolu_1", "wifi_scan", {"seconds": 5}),
        ("toolu_2", "status", {}),
    ]
    assert out.state is not None and out.state.data["content"] == content


def test_anthropic_replay_and_tool_result_grouping() -> None:
    content = [
        {"type": "thinking", "thinking": "geheim", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "wifi_scan", "input": {}},
        {"type": "tool_use", "id": "toolu_2", "name": "status", "input": {}},
    ]
    rec = Recorder(
        _anthropic_response(content, "tool_use"),
        _anthropic_response([{"type": "text", "text": "Klaar."}]),
    )
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        out = p.complete(HISTORY, tools=[TOOL])
        history = [
            *HISTORY,
            out.message,
            ToolResult(call_id="toolu_1", name="wifi_scan", content='{"ok": true}'),
            ToolResult(call_id="toolu_2", name="status", content='{"ok": false}', is_error=True),
            UserMessage("en nu?"),
        ]
        final = p.complete(history, tools=[TOOL])
    assert final.text == "Klaar."
    msgs = rec.sent["messages"]
    assert msgs[1] == {"role": "assistant", "content": content}  # ongewijzigd, incl. thinking
    assert msgs[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok": true}'},
            {
                "type": "tool_result",
                "tool_use_id": "toolu_2",
                "content": '{"ok": false}',
                "is_error": True,
            },
            {"type": "text", "text": "en nu?"},
        ],
    }
    assert len(msgs) == 3


def test_anthropic_text_before_tool_result_is_moved_after() -> None:
    msg = AssistantMessage(
        tool_calls=(ToolCall(id="toolu_1", name="status", arguments={}),),
    )
    rec = Recorder(_anthropic_response([{"type": "text", "text": "x"}]))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        p.complete(
            [UserMessage("a"), msg, UserMessage("tussendoor"), ToolResult("toolu_1", "status", "")]
        )
    user = rec.sent["messages"][2]["content"]
    assert [b["type"] for b in user] == ["tool_result", "text"]
    assert "content" not in user[0]  # lege inhoud wordt weggelaten


def test_anthropic_builds_blocks_without_own_state() -> None:
    msg = AssistantMessage(
        text="Ik scan.",
        tool_calls=(
            ToolCall(
                id="c1", name="wifi_scan", arguments={"seconds": 1}, raw_arguments='{"seconds":1}'
            ),
            ToolCall(id="c2", name="", arguments=None, error=ToolCallError.MISSING_NAME),
        ),
        state=ProviderState("openai", {"output": []}),
    )
    rec = Recorder(_anthropic_response([{"type": "text", "text": "x"}]))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        p.complete(
            [
                UserMessage("a"),
                msg,
                ToolResult("c1", "wifi_scan", "{}"),
                ToolResult("c2", "", "fout", True),
            ]
        )
    assert rec.sent["messages"][1]["content"] == [
        {"type": "text", "text": "Ik scan."},
        {"type": "tool_use", "id": "c1", "name": "wifi_scan", "input": {"seconds": 1}},
        {"type": "tool_use", "id": "c2", "name": "unknown_tool", "input": {}},
    ]


def test_anthropic_skips_empty_user_text() -> None:
    rec = Recorder(_anthropic_response([{"type": "text", "text": "x"}]))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        p.complete([UserMessage(""), UserMessage("hoi")])
    assert rec.sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hoi"}]}]


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        ("end_turn", StopReason.END_TURN),
        ("stop_sequence", StopReason.END_TURN),
        ("tool_use", StopReason.TOOL_USE),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("model_context_window_exceeded", StopReason.MAX_TOKENS),
        ("refusal", StopReason.REFUSAL),
        ("pause_turn", StopReason.OTHER),
    ],
)
def test_anthropic_stop_reasons(stop: str, expected: StopReason) -> None:
    rec = Recorder(_anthropic_response([{"type": "text", "text": "x"}], stop))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        out = p.complete(HISTORY)
    assert out.stop_reason is expected
    assert out.raw_stop_reason == stop


def test_anthropic_non_object_input_is_error() -> None:
    content = [{"type": "tool_use", "id": "t", "name": "status", "input": [1]}]
    rec = Recorder(_anthropic_response(content, "tool_use"))
    with _provider(ProviderKind.ANTHROPIC, rec) as p:
        out = p.complete(HISTORY)
    assert out.tool_calls[0].error is ToolCallError.NOT_AN_OBJECT


def test_anthropic_malformed_response() -> None:
    rec = Recorder({"type": "message"})
    with _provider(ProviderKind.ANTHROPIC, rec) as p, pytest.raises(LLMResponseError):
        p.complete(HISTORY)


def test_anthropic_overloaded_529_is_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                529,
                json={
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "Overloaded"},
                },
            )
        return httpx.Response(200, json=_anthropic_response([{"type": "text", "text": "ok"}]))

    cfg = LLMConfig(
        provider=ProviderKind.ANTHROPIC,
        model=ANTHROPIC_MODEL,
        base_url="https://api.anthropic.com",
        api_key=SecretStr(KEY),
        max_retries=1,
    )
    with build_provider(cfg, transport=httpx.MockTransport(handler), sleep=lambda _s: None) as p:
        assert p.complete(HISTORY).text == "ok"
    assert len(calls) == 2


# ============================ OpenAI Responses ============================


def _responses(output: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": "test-model",
        "error": None,
        "incomplete_details": None,
        "output": output,
        "usage": {"input_tokens": 291, "output_tokens": 23, "total_tokens": 314},
    }
    body.update(extra)
    return body


def _text_item(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def test_responses_request_shape() -> None:
    rec = Recorder(_responses([_text_item("Hallo")]))
    with _provider(ProviderKind.OPENAI, rec) as p:
        out = p.complete(HISTORY, system="Je bent PiToolbox.", tools=[TOOL])
    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/responses"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert rec.sent == {
        "model": "test-model",
        "input": [{"role": "user", "content": "scan de wifi"}],
        "max_output_tokens": 321,
        "store": False,
        "instructions": "Je bent PiToolbox.",
        "tools": [
            {
                "type": "function",
                "name": "wifi_scan",
                "description": "Scan passief.",
                "parameters": dict(TOOL.parameters),
                "strict": False,
            }
        ],
    }
    assert out.text == "Hallo"
    assert out.stop_reason is StopReason.END_TURN
    assert (out.usage.input_tokens, out.usage.output_tokens) == (291, 23)


def test_responses_function_calls_and_replay() -> None:
    output = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAA"},
        _text_item("Even kijken."),
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "wifi_scan",
            "arguments": '{"seconds": 2}',
            "status": "completed",
        },
        {
            "type": "function_call",
            "id": "fc_2",
            "call_id": "call_2",
            "name": "status",
            "arguments": "niet-json",
            "status": "completed",
        },
    ]
    rec = Recorder(_responses(output), _responses([_text_item("Klaar.")]))
    with _provider(ProviderKind.OPENAI, rec) as p:
        out = p.complete(HISTORY, tools=[TOOL])
        assert out.text == "Even kijken."
        assert out.stop_reason is StopReason.TOOL_USE
        assert [(c.id, c.name, c.arguments, c.error) for c in out.tool_calls] == [
            ("call_1", "wifi_scan", {"seconds": 2}, None),
            ("call_2", "status", None, ToolCallError.INVALID_JSON),
        ]
        history = [
            *HISTORY,
            out.message,
            ToolResult("call_1", "wifi_scan", '{"ok": true}'),
            ToolResult("call_2", "status", '{"ok": false}', is_error=True),
        ]
        p.complete(history, tools=[TOOL])
    items = rec.sent["input"]
    assert items[1:5] == output  # ongewijzigd terug, incl. reasoning-item
    assert items[5:] == [
        {"type": "function_call_output", "call_id": "call_1", "output": '{"ok": true}'},
        {"type": "function_call_output", "call_id": "call_2", "output": '{"ok": false}'},
    ]


def test_responses_builds_items_without_own_state() -> None:
    msg = AssistantMessage(
        text="Ik scan.",
        tool_calls=(ToolCall(id="c1", name="wifi_scan", arguments={}, raw_arguments="{}"),),
    )
    rec = Recorder(_responses([_text_item("x")]))
    with _provider(ProviderKind.OPENAI, rec) as p:
        p.complete([UserMessage("a"), msg, ToolResult("c1", "wifi_scan", "{}")])
    assert rec.sent["input"][1:] == [
        {"role": "assistant", "content": "Ik scan."},
        {"type": "function_call", "call_id": "c1", "name": "wifi_scan", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
    ]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("max_output_tokens", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.REFUSAL),
        ("x", StopReason.OTHER),
    ],
)
def test_responses_incomplete(reason: str, expected: StopReason) -> None:
    rec = Recorder(
        _responses([_text_item("half")], status="incomplete", incomplete_details={"reason": reason})
    )
    with _provider(ProviderKind.OPENAI, rec) as p:
        out = p.complete(HISTORY)
    assert out.stop_reason is expected
    assert out.raw_stop_reason == reason
    assert out.text == "half"


def test_responses_refusal_part() -> None:
    item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": "Nee."}],
    }
    rec = Recorder(_responses([item]))
    with _provider(ProviderKind.OPENAI, rec) as p:
        out = p.complete(HISTORY)
    assert out.stop_reason is StopReason.REFUSAL
    assert out.text == "Nee."


def test_responses_failed_status() -> None:
    rec = Recorder(_responses([], status="failed", error={"code": "server_error", "message": "x"}))
    with _provider(ProviderKind.OPENAI, rec) as p, pytest.raises(LLMResponseError) as exc:
        p.complete(HISTORY)
    assert exc.value.provider_code == "server_error"


def test_responses_missing_output() -> None:
    rec = Recorder({"status": "completed"})
    with _provider(ProviderKind.OPENAI, rec) as p, pytest.raises(LLMResponseError):
        p.complete(HISTORY)


def test_responses_works_against_local_server_base_url() -> None:
    """Ollama ondersteunt /v1/responses (stateless); http naar loopback mag."""
    rec = Recorder(_responses([_text_item("lokaal")]))
    with _provider(ProviderKind.OPENAI, rec, base_url="http://127.0.0.1:11434/v1") as p:
        assert p.complete(HISTORY).text == "lokaal"
    assert str(rec.requests[0].url) == "http://127.0.0.1:11434/v1/responses"


# ============================ algemeen ============================


@pytest.mark.parametrize("kind", list(ProviderKind))
def test_auth_error_never_contains_key(kind: ProviderKind) -> None:
    body = {
        "error": {"message": f"Incorrect API key provided: {KEY}", "type": "invalid_request_error"}
    }
    rec = Recorder(body, status=401)
    with _provider(kind, rec) as p, pytest.raises(LLMAuthError) as exc:
        p.complete(HISTORY)
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)
    assert KEY not in repr(p)
    assert len(rec.requests) == 1  # geen retry op 401


def test_completion_message_round_trip() -> None:
    c = Completion(
        text="t",
        tool_calls=(ToolCall(id="1", name="n", arguments={}),),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(),
        state=ProviderState("openai", {"output": []}),
    )
    assert c.message == AssistantMessage(text="t", tool_calls=c.tool_calls, state=c.state)


def test_adapter_rejects_wrong_config_kind() -> None:
    from pitoolbox.llm._anthropic import AnthropicProvider

    cfg = LLMConfig(
        provider=ProviderKind.OPENAI,
        model="m",
        base_url="https://api.openai.com/v1",
        api_key=SecretStr(KEY),
    )
    with pytest.raises(ValueError, match="hoort bij"):
        AnthropicProvider(cfg)
