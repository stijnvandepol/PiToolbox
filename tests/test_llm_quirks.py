"""Tests voor de quirks per preset in de OpenAI-compatibele adapter (geen netwerk)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import SecretStr

from pitoolbox.llm import (
    LLMConfig,
    ProviderKind,
    ToolResult,
    UserMessage,
    build_provider,
)
from pitoolbox.llm._base import HttpProvider
from pitoolbox.llm.presets import PRESETS, Quirk

KEY = "sk-test-geheim-1234567890"
HISTORY = [UserMessage("scan de wifi")]
SIGNATURE = {"google": {"thought_signature": "c2lnLUE="}}


class Recorder:
    def __init__(self, *messages: dict[str, Any]) -> None:
        self.messages = list(messages)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        message = self.messages.pop(0)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return httpx.Response(
            200, json={"choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        )


def _provider(rec: Recorder, quirks: frozenset[Quirk] = frozenset()) -> HttpProvider:
    cfg = LLMConfig(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="voorbeeld-model",
        base_url="https://api.example.com/v1",
        api_key=SecretStr(KEY),
        quirks=quirks,
    )
    return build_provider(cfg, transport=httpx.MockTransport(rec), sleep=lambda _s: None)


def _call(call_id: str, name: str, extra: Any = None) -> dict[str, Any]:
    call: dict[str, Any] = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }
    if extra is not None:
        call["extra_content"] = extra
    return call


def _two_rounds(rec: Recorder, quirks: frozenset[Quirk]) -> dict[str, Any]:
    """Eerste antwoord + tool-resultaten terug; geeft het teruggestuurde assistant-bericht."""
    with _provider(rec, quirks) as p:
        first = p.complete(HISTORY)
        results = [ToolResult(call_id=c.id, name=c.name, content="{}") for c in first.tool_calls]
        p.complete([*HISTORY, first.message, *results])
    replayed: dict[str, Any] = rec.sent[-1]["messages"][1]
    return replayed


# --- Gemini: thought signature in tool_calls[].extra_content ---


def _gemini_first() -> dict[str, Any]:
    # Parallelle calls: alleen de eerste draagt de signature (Googles thought-signatures-docs).
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [_call("fc-1", "wifi_scan", SIGNATURE), _call("fc-2", "status")],
    }


def test_gemini_preset_has_extra_content_quirk() -> None:
    assert Quirk.TOOL_CALL_EXTRA_CONTENT in PRESETS["gemini"].quirks


def test_extra_content_is_replayed_per_call() -> None:
    rec = Recorder(_gemini_first(), {"role": "assistant", "content": "klaar"})
    msg = _two_rounds(rec, frozenset({Quirk.TOOL_CALL_EXTRA_CONTENT}))
    assert msg["tool_calls"] == [_call("fc-1", "wifi_scan", SIGNATURE), _call("fc-2", "status")]
    # Alle calls vóór de tool-resultaten (volgorde die Gemini eist).
    roles = [m["role"] for m in rec.sent[-1]["messages"]]
    assert roles == ["user", "assistant", "tool", "tool"]


def test_extra_content_survives_missing_call_id() -> None:
    first = {"role": "assistant", "tool_calls": [{**_call("", "wifi_scan", SIGNATURE)}]}
    rec = Recorder(first, {"role": "assistant", "content": "klaar"})
    msg = _two_rounds(rec, frozenset({Quirk.TOOL_CALL_EXTRA_CONTENT}))
    assert msg["tool_calls"][0]["extra_content"] == SIGNATURE
    assert msg["tool_calls"][0]["id"]  # synthetische id, dezelfde als in het tool-resultaat
    assert rec.sent[-1]["messages"][2]["tool_call_id"] == msg["tool_calls"][0]["id"]


def test_extra_content_not_replayed_without_quirk() -> None:
    rec = Recorder(_gemini_first(), {"role": "assistant", "content": "klaar"})
    msg = _two_rounds(rec, frozenset())
    assert all("extra_content" not in c for c in msg["tool_calls"])


# --- Mistral: content als lijst van blokken ---

MISTRAL_CONTENT = [
    {"type": "thinking", "thinking": [{"type": "text", "text": "eerst scannen"}]},
    {"type": "text", "text": "Ik scan."},
]


def _mistral_first(content: Any = MISTRAL_CONTENT) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "tool_calls": [_call("abc123XYZ", "status")]}


def test_mistral_preset_has_content_parts_quirk() -> None:
    assert Quirk.CONTENT_PARTS in PRESETS["mistral"].quirks


def test_content_parts_replayed_verbatim() -> None:
    rec = Recorder(_mistral_first(), {"role": "assistant", "content": "klaar"})
    with _provider(rec, frozenset({Quirk.CONTENT_PARTS})) as p:
        first = p.complete(HISTORY)
        assert first.text == "Ik scan."  # alleen tekst voor de gebruiker, geen thinking
        p.complete([*HISTORY, first.message, ToolResult("abc123XYZ", "status", "{}")])
    msg = rec.sent[-1]["messages"][1]
    assert msg["content"] == MISTRAL_CONTENT
    assert set(msg) == {"role", "content", "tool_calls"}  # Mistral weigert onbekende velden


def test_string_content_unchanged_with_quirk() -> None:
    rec = Recorder(_mistral_first("Ik scan."), {"role": "assistant", "content": "klaar"})
    msg = _two_rounds(rec, frozenset({Quirk.CONTENT_PARTS}))
    assert msg["content"] == "Ik scan."


def test_content_parts_flattened_without_quirk() -> None:
    rec = Recorder(_mistral_first(), {"role": "assistant", "content": "klaar"})
    msg = _two_rounds(rec, frozenset())
    assert msg["content"] == "Ik scan."
