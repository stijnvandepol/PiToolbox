"""Tests voor fouten in een HTTP 200-antwoord (OpenAI-compatibele adapter, geen netwerk).

Vormen uit de officiële docs; bronnen in src/pitoolbox/llm/_body_errors.py.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from pitoolbox.llm import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfig,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    ProviderKind,
    StopReason,
    UserMessage,
    build_provider,
)
from pitoolbox.llm._base import HttpProvider
from pitoolbox.llm.presets import PRESETS, Quirk

KEY = "sk-test-geheim-1234567890"
HISTORY = [UserMessage("hallo")]
OK_BODY = {
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hoi"}, "finish_reason": "stop"}
    ]
}


class Script:
    def __init__(self, *bodies: dict[str, Any]) -> None:
        self.bodies = list(bodies)
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        return httpx.Response(200, json=self.bodies.pop(0))


def _provider(
    script: Script,
    *,
    max_retries: int = 2,
    quirks: frozenset[Quirk] = frozenset(),
    sleeps: list[float] | None = None,
) -> HttpProvider:
    cfg = LLMConfig(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="voorbeeld-model",
        base_url="https://api.example.com/v1",
        api_key=SecretStr(KEY),
        max_retries=max_retries,
        quirks=quirks,
    )
    record = sleeps if sleeps is not None else []
    return build_provider(cfg, transport=httpx.MockTransport(script), sleep=record.append)


def _fail(script: Script, **kw: Any) -> LLMError:
    with _provider(script, **kw) as p, pytest.raises(LLMError) as exc:
        p.complete(HISTORY)
    return exc.value


def _openrouter_error(code: Any, error_type: str | None = None, msg: str = "x") -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": msg}
    if error_type is not None:
        err["metadata"] = {"error_type": error_type, "provider_code": "upstream"}
    return {"id": "gen-1", "error": err}


# --- gewone antwoorden blijven gewoon ---


@pytest.mark.parametrize("extra", [{}, {"error": None}])
def test_normal_200_is_unaffected(extra: dict[str, Any]) -> None:
    script = Script({**OK_BODY, **extra})
    with _provider(script) as p:
        result = p.complete(HISTORY)
    assert result.text == "hoi" and result.stop_reason is StopReason.END_TURN
    assert script.count == 1


# --- OpenRouter: top-level error-object ---


@pytest.mark.parametrize(
    ("code", "error_type", "expected"),
    [
        (429, "rate_limit_exceeded", LLMRateLimitError),
        (502, "provider_unavailable", LLMServerError),
        (503, "provider_overloaded", LLMServerError),
        (504, "timeout", LLMServerError),
        (500, "server", LLMServerError),
    ],
)
def test_openrouter_retryable_error_is_retried_then_succeeds(
    code: int, error_type: str, expected: type[LLMError]
) -> None:
    script = Script(_openrouter_error(code, error_type), OK_BODY)
    sleeps: list[float] = []
    with _provider(script, sleeps=sleeps) as p:
        assert p.complete(HISTORY).text == "hoi"
    assert script.count == 2 and len(sleeps) == 1
    assert expected.retryable


def test_openrouter_retry_stops_at_max_retries() -> None:
    script = Script(*(_openrouter_error(429, "rate_limit_exceeded") for _ in range(4)))
    err = _fail(script, max_retries=3)
    assert type(err) is LLMRateLimitError
    assert script.count == 4
    assert err.status == 429 and err.provider_code == "rate_limit_exceeded"
    assert "fout 429 in een HTTP 200-antwoord" in str(err)


@pytest.mark.parametrize(
    ("code", "error_type", "expected"),
    [
        (401, "authentication", LLMAuthError),
        (403, "permission_denied", LLMAuthError),
        (402, "payment_required", LLMBadRequestError),
        (400, "context_length_exceeded", LLMBadRequestError),
        (404, "not_found", LLMBadRequestError),
        (403, "content_policy_violation", LLMBadRequestError),
        (403, "refusal", LLMBadRequestError),
    ],
)
def test_openrouter_non_retryable_error_fails_immediately(
    code: int, error_type: str, expected: type[LLMError]
) -> None:
    script = Script(_openrouter_error(code, error_type), OK_BODY)
    err = _fail(script)
    assert type(err) is expected and not err.retryable
    assert script.count == 1


def test_openrouter_policy_error_is_not_reported_as_auth() -> None:
    err = _fail(Script(_openrouter_error(403, "content_policy_violation", "flagged")))
    assert "inhoudsfilter" in str(err) and "authenticatie" not in str(err)


def test_error_without_metadata_uses_code() -> None:
    err = _fail(Script(_openrouter_error(503)), max_retries=0)
    assert type(err) is LLMServerError and err.provider_code == "503"


@pytest.mark.parametrize("code", ["server_error", None, True, 200, 99])
def test_error_without_http_status_code_is_bad_response(code: Any) -> None:
    script = Script(_openrouter_error(code))
    err = _fail(script)
    assert type(err) is LLMResponseError and script.count == 1


# --- OpenRouter Chat Completions-skin: finish_reason "error" in choices[0] ---


def _choice_error(error: dict[str, Any] | None, finish: str = "error") -> dict[str, Any]:
    choice: dict[str, Any] = {
        "index": 0,
        "message": {"role": "assistant", "content": "gedeeltelijk..."},
        "finish_reason": finish,
    }
    if error is not None:
        choice["error"] = error
    return {"choices": [choice]}


def test_choice_error_is_retried() -> None:
    error = {"code": 502, "message": "disconnected", "metadata": {"error_type": "x"}}
    script = Script(_choice_error(error), OK_BODY)
    with _provider(script) as p:
        assert p.complete(HISTORY).text == "hoi"
    assert script.count == 2


def test_choice_error_bad_request_fails_immediately() -> None:
    script = Script(_choice_error({"code": 400, "message": "te lang"}))
    err = _fail(script)
    assert type(err) is LLMBadRequestError and script.count == 1


@pytest.mark.parametrize("finish", ["error", "network_error"])
def test_error_finish_reason_without_object_is_server_error(finish: str) -> None:
    script = Script(*(_choice_error(None, finish) for _ in range(3)))
    err = _fail(script)
    assert type(err) is LLMServerError and err.provider_code == finish
    assert script.count == 3  # 1 + max_retries


# --- geen sleutel in foutberichten ---


def test_key_never_in_message() -> None:
    body = _openrouter_error(401, "authentication", f"bad key {KEY}\nregel 2")
    err = _fail(Script(body))
    assert KEY not in str(err) and "[geredigeerd]" in str(err)
    assert "\n" not in str(err)


# --- MiniMax: base_resp (Quirk.BASE_RESP) ---

MINIMAX = frozenset({Quirk.BASE_RESP})


def _base_resp(code: int, msg: str = "melding") -> dict[str, Any]:
    # Bij een fout zijn choices leeg of afwezig; base_resp staat op het hoogste niveau.
    return {"id": "1", "choices": None, "base_resp": {"status_code": code, "status_msg": msg}}


def test_minimax_preset_has_base_resp_quirk() -> None:
    assert Quirk.BASE_RESP in PRESETS["minimax"].quirks


def test_minimax_status_zero_is_normal() -> None:
    body = {**OK_BODY, "base_resp": {"status_code": 0, "status_msg": ""}}
    with _provider(Script(body), quirks=MINIMAX) as p:
        assert p.complete(HISTORY).text == "hoi"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1000, LLMServerError),
        (1001, LLMTimeoutError),
        (1002, LLMRateLimitError),
        (1013, LLMServerError),
        (1024, LLMServerError),
        (1033, LLMServerError),
        (1039, LLMRateLimitError),
        (1041, LLMRateLimitError),
        (2045, LLMRateLimitError),
    ],
)
def test_minimax_retryable_code_is_retried(code: int, expected: type[LLMError]) -> None:
    script = Script(_base_resp(code), OK_BODY)
    with _provider(script, quirks=MINIMAX) as p:
        assert p.complete(HISTORY).text == "hoi"
    assert script.count == 2
    err = _fail(Script(*(_base_resp(code) for _ in range(3))), quirks=MINIMAX)
    assert type(err) is expected and err.provider_code == str(code)


def test_minimax_retry_stops_at_max_retries() -> None:
    script = Script(*(_base_resp(1002) for _ in range(5)))
    err = _fail(script, quirks=MINIMAX, max_retries=4)
    assert type(err) is LLMRateLimitError and script.count == 5


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1004, LLMAuthError),
        (2049, LLMAuthError),
        (1008, LLMBadRequestError),
        (1026, LLMBadRequestError),
        (1027, LLMBadRequestError),
        (1042, LLMBadRequestError),
        (2013, LLMBadRequestError),
        (2056, LLMBadRequestError),
        (9999, LLMResponseError),  # onbekende code
    ],
)
def test_minimax_non_retryable_code_fails_immediately(code: int, expected: type[LLMError]) -> None:
    script = Script(_base_resp(code), OK_BODY)
    err = _fail(script, quirks=MINIMAX)
    assert type(err) is expected and not err.retryable
    assert script.count == 1
    assert f"base_resp {code}" in str(err)


def test_minimax_message_redacts_key() -> None:
    err = _fail(Script(_base_resp(1004, f"token not match {KEY}")), quirks=MINIMAX)
    assert KEY not in str(err)


def test_base_resp_ignored_without_quirk() -> None:
    body = {**OK_BODY, "base_resp": {"status_code": 1002, "status_msg": "rate limit"}}
    with _provider(Script(body)) as p:
        assert p.complete(HISTORY).text == "hoi"
