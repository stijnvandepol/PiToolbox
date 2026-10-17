"""Tests voor de gedeelde HTTP-laag: retries, Retry-After, time-outs, limieten, redactie."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from pitoolbox.llm import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)
from pitoolbox.llm._http import MAX_RETRY_AFTER_S, JsonHttpClient, parse_retry_after, redact

KEY = "sk-test-geheim-1234567890"
OK = {"ok": True}

Handler = Callable[[httpx.Request], httpx.Response]


class Script:
    """MockTransport-handler die per verzoek het volgende antwoord (of exceptie) geeft."""

    def __init__(self, *steps: httpx.Response | Exception) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _client(
    handler: Handler, *, max_retries: int = 2, sleeps: list[float] | None = None, **kw: Any
) -> JsonHttpClient:
    record = sleeps if sleeps is not None else []
    return JsonHttpClient(
        base_url="https://llm.example.com/v1/",
        timeout_s=5,
        max_retries=max_retries,
        secrets=(KEY,),
        transport=httpx.MockTransport(handler),
        sleep=record.append,
        rng=lambda: 1.0,
        **kw,
    )


def _post(client: JsonHttpClient) -> dict[str, Any]:
    return client.post_json("/x", {"a": 1}, {"Authorization": f"Bearer {KEY}"})


def test_success_posts_json_to_joined_url() -> None:
    script = Script(httpx.Response(200, json=OK))
    assert _post(_client(script)) == OK
    req = script.requests[0]
    assert str(req.url) == "https://llm.example.com/v1/x"
    assert req.method == "POST"
    assert json.loads(req.content) == {"a": 1}
    assert req.headers["content-type"] == "application/json"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
def test_retries_on_429_and_5xx_then_succeeds(status: int) -> None:
    script = Script(httpx.Response(status, json={}), httpx.Response(200, json=OK))
    sleeps: list[float] = []
    assert _post(_client(script, sleeps=sleeps)) == OK
    assert len(script.requests) == 2
    assert sleeps == [0.5]  # backoff 0,5 s * 2^0, jitter-factor 1


def test_backoff_grows_and_gives_up_after_max_retries() -> None:
    script = Script(*(httpx.Response(503, json={}) for _ in range(4)))
    sleeps: list[float] = []
    with pytest.raises(LLMServerError) as exc:
        _post(_client(script, max_retries=3, sleeps=sleeps))
    assert len(script.requests) == 4
    assert sleeps == [0.5, 1.0, 2.0]
    assert exc.value.status == 503 and exc.value.retryable


def test_zero_retries() -> None:
    script = Script(httpx.Response(500, json={}))
    with pytest.raises(LLMServerError):
        _post(_client(script, max_retries=0))
    assert len(script.requests) == 1


def test_retry_after_seconds_is_respected() -> None:
    script = Script(
        httpx.Response(429, headers={"retry-after": "7"}, json={}),
        httpx.Response(200, json=OK),
    )
    sleeps: list[float] = []
    assert _post(_client(script, sleeps=sleeps)) == OK
    assert sleeps == [7.0]


def test_retry_after_too_long_fails_immediately() -> None:
    too_long = str(int(MAX_RETRY_AFTER_S) + 1)
    script = Script(httpx.Response(429, headers={"retry-after": too_long}, json={}))
    sleeps: list[float] = []
    with pytest.raises(LLMRateLimitError) as exc:
        _post(_client(script, sleeps=sleeps))
    assert sleeps == []
    assert exc.value.retry_after_s == float(too_long)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_no_retry_on_other_4xx(status: int) -> None:
    script = Script(httpx.Response(status, json={}), httpx.Response(200, json=OK))
    sleeps: list[float] = []
    with pytest.raises((LLMAuthError, LLMBadRequestError)) as exc:
        _post(_client(script, sleeps=sleeps))
    assert len(script.requests) == 1 and sleeps == []
    assert not exc.value.retryable
    expected = LLMAuthError if status in (401, 403) else LLMBadRequestError
    assert type(exc.value) is expected


def test_timeout_is_retried_then_reported() -> None:
    script = Script(*(httpx.ReadTimeout("te traag") for _ in range(3)))
    with pytest.raises(LLMTimeoutError):
        _post(_client(script))
    assert len(script.requests) == 3


def test_connection_error_is_retried_then_succeeds() -> None:
    script = Script(httpx.ConnectError("Connection refused"), httpx.Response(200, json=OK))
    assert _post(_client(script)) == OK


def test_connection_error_message() -> None:
    script = Script(*(httpx.ConnectError("Connection refused") for _ in range(3)))
    with pytest.raises(LLMConnectionError, match="Connection refused"):
        _post(_client(script))


def test_openai_error_shape_is_parsed() -> None:
    body = {"error": {"message": "Rate limit reached", "type": "rate_limit_error", "code": "x"}}
    script = Script(httpx.Response(429, json=body))
    with pytest.raises(LLMRateLimitError, match="Rate limit reached") as exc:
        _post(_client(script, max_retries=0))
    assert exc.value.provider_code == "x"


def test_anthropic_error_shape_is_parsed() -> None:
    body = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        "request_id": "req_1",
    }
    script = Script(httpx.Response(401, json=body))
    with pytest.raises(LLMAuthError, match="invalid x-api-key") as exc:
        _post(_client(script))
    assert exc.value.provider_code == "authentication_error"
    assert exc.value.status == 401


def test_plain_string_error_shape() -> None:
    script = Script(httpx.Response(404, json={"error": "model 'x' not found"}))
    with pytest.raises(LLMBadRequestError, match="model 'x' not found"):
        _post(_client(script))


def test_error_message_redacts_key_and_is_truncated() -> None:
    msg = f"Incorrect API key provided: {KEY}. " + "x" * 1000
    script = Script(httpx.Response(401, json={"error": {"message": msg}}))
    with pytest.raises(LLMAuthError) as exc:
        _post(_client(script))
    text = str(exc.value)
    assert KEY not in text
    assert "[geredigeerd]" in text
    assert len(text) < 400


def test_connection_error_redacts_key() -> None:
    script = Script(*(httpx.ConnectError(f"boom {KEY}") for _ in range(3)))
    with pytest.raises(LLMConnectionError) as exc:
        _post(_client(script))
    assert KEY not in str(exc.value)


def test_key_not_in_any_exception_chain() -> None:
    script = Script(*(httpx.ReadTimeout("t") for _ in range(3)))
    with pytest.raises(LLMTimeoutError) as exc:
        _post(_client(script))
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_response_too_large_by_content_length() -> None:
    script = Script(httpx.Response(200, content=b"{}", headers={"content-length": "999999"}))
    with pytest.raises(LLMResponseError, match="te groot"):
        _post(_client(script, max_response_bytes=1000))


def test_response_too_large_streamed() -> None:
    big = json.dumps({"x": "y" * 5000}).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        # Zonder content-length: wordt tijdens het lezen afgebroken.
        return httpx.Response(200, stream=httpx.ByteStream(big))

    with pytest.raises(LLMResponseError, match="te groot"):
        _post(_client(handler, max_response_bytes=1000))


def test_non_json_response() -> None:
    script = Script(httpx.Response(200, content=b"<html>nope</html>"))
    with pytest.raises(LLMResponseError, match="geen geldige JSON"):
        _post(_client(script))


def test_json_but_not_object() -> None:
    script = Script(httpx.Response(200, json=[1, 2]))
    with pytest.raises(LLMResponseError, match="geen JSON-object"):
        _post(_client(script))


def test_redirect_is_not_followed() -> None:
    script = Script(httpx.Response(307, headers={"location": "https://evil.example.com/x"}))
    with pytest.raises(LLMBadRequestError, match="redirect"):
        _post(_client(script))
    assert len(script.requests) == 1


# --- fout in een 200-antwoord (BodyCheck) ---


def _flag_error(data: dict[str, Any], clean: Callable[[str], str]) -> LLMServerError | None:
    return LLMServerError(clean(str(data["fout"]))) if "fout" in data else None


def _post_checked(client: JsonHttpClient) -> dict[str, Any]:
    return client.post_json(
        "/x", {"a": 1}, {"Authorization": f"Bearer {KEY}"}, check_body=_flag_error
    )


def test_body_check_error_is_retried_like_http_error() -> None:
    script = Script(httpx.Response(200, json={"fout": "even niet"}), httpx.Response(200, json=OK))
    sleeps: list[float] = []
    assert _post_checked(_client(script, sleeps=sleeps)) == OK
    assert len(script.requests) == 2 and sleeps == [0.5]


def test_body_check_error_stops_at_max_retries() -> None:
    script = Script(*(httpx.Response(200, json={"fout": "x"}) for _ in range(3)))
    with pytest.raises(LLMServerError):
        _post_checked(_client(script, max_retries=2))
    assert len(script.requests) == 3


def test_body_check_uses_retry_after_header() -> None:
    script = Script(
        httpx.Response(200, headers={"retry-after": "3"}, json={"fout": "x"}),
        httpx.Response(200, json=OK),
    )
    sleeps: list[float] = []
    assert _post_checked(_client(script, sleeps=sleeps)) == OK
    assert sleeps == [3.0]


def test_body_check_message_is_cleaned() -> None:
    script = Script(httpx.Response(200, json={"fout": f"sleutel {KEY}\n" + "x" * 1000}))
    with pytest.raises(LLMServerError) as exc:
        _post_checked(_client(script, max_retries=0))
    assert KEY not in str(exc.value) and "\n" not in str(exc.value)
    assert len(str(exc.value)) <= 300


def test_without_body_check_200_is_returned_as_is() -> None:
    script = Script(httpx.Response(200, json={"fout": "x"}))
    assert _post(_client(script)) == {"fout": "x"}


# --- hulpfuncties ---


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("-4") == 0.0
    assert parse_retry_after("nan") is None
    assert parse_retry_after("onzin") is None
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    later = format_datetime(now + timedelta(seconds=20), usegmt=True)
    assert parse_retry_after(later, now=now.timestamp()) == pytest.approx(20.0)


def test_retry_after_http_date_is_used() -> None:
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    script = Script(
        httpx.Response(503, headers={"retry-after": soon}, json={}),
        httpx.Response(200, json=OK),
    )
    sleeps: list[float] = []
    assert _post(_client(script, sleeps=sleeps)) == OK
    assert 0 <= sleeps[0] <= 5


def test_redact_ignores_short_secrets() -> None:
    assert redact("abc sk-1234 def", ["sk-1234", "ab", ""]) == "abc [geredigeerd] def"
