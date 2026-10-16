"""Gedeelde HTTP-laag voor alle providers: één POST met JSON, robuust en zuinig.

Waarom httpx en geen vendor-SDK's (openai, anthropic):
- Eén kleine, bekende HTTP-client voor alle providers houdt de laag provider-neutraal;
  geen drie SDK's met eigen retry-, timeout- en loggedrag en eigen dependency-bomen op een Pi.
- httpx stond al in de lock (dev) en is nu een runtime-dependency; `httpx.MockTransport`
  maakt tests zonder netwerk mogelijk.
- Synchroon (`httpx.Client`) omdat de rest synchroon is: `ToolRunner.call`, de devices en
  straks de agent-loop doen alles na elkaar. Een async frontend kan `complete()` net als
  `runner.call` via `anyio.to_thread.run_sync` aanroepen (zie frontends/mcp_server.py).

Gedrag:
- Time-out per verzoek (connect max 10 s), geen redirects volgen (een redirect zou de
  sleutel naar een andere host kunnen sturen), `trust_env=False` (geen proxy-/netrc-
  instellingen uit de omgeving die headers of verkeer omleiden).
- Begrensde retries met exponentiële backoff + jitter, alleen bij 429, 5xx, time-outs en
  verbindingsfouten. `Retry-After` (seconden of HTTP-datum) wordt gerespecteerd; vraagt de
  server langer dan `MAX_RETRY_AFTER_S`, dan geven we direct op. Nooit retries bij andere 4xx.
  Bronnen: https://platform.claude.com/docs/en/api/errors ("Retry the request with
  exponential backoff", "honoring the retry-after header") en
  https://developers.openai.com/api/docs/guides/error-codes ("follow the Retry-After header
  when it's present").
- Antwoordgrootte begrensd (stream lezen en afbreken), ook voor foutantwoorden.
- Foutberichten van de server worden ingekort en de sleutel wordt eruit geredigeerd.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from pitoolbox.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_ERROR_MESSAGE_CHARS = 300
MAX_RETRY_AFTER_S = 30.0
BACKOFF_BASE_S = 0.5
BACKOFF_MAX_S = 8.0
CONNECT_TIMEOUT_S = 10.0

_REDACTED = "[geredigeerd]"


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    return text


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """`Retry-After` als seconden of HTTP-datum (RFC 9110 §10.2.3); None als onbruikbaar."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    if seconds != seconds:  # NaN
        return None
    return max(0.0, seconds)


def _error_details(body: bytes) -> tuple[str, str | None]:
    """(bericht, providercode) uit een foutantwoord.

    Vormen: OpenAI `{"error": {"message", "type", "code"}}`
    (https://developers.openai.com/api/docs/guides/error-codes), Anthropic
    `{"type": "error", "error": {"type", "message"}}`
    (https://platform.claude.com/docs/en/api/errors), en `{"error": "tekst"}` bij sommige
    lokale servers.
    """
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body.decode("utf-8", "replace").strip(), None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            code = err.get("code") or err.get("type")
            return (str(msg) if msg is not None else ""), (str(code) if code else None)
        if isinstance(err, str):
            return err, None
        if isinstance(data.get("message"), str):
            return str(data["message"]), None
    return "", None


def _error_for_status(status: int, message: str, provider_code: str | None) -> LLMError:
    text = f"HTTP {status}" + (f": {message}" if message else "")
    kwargs: dict[str, Any] = {"status": status, "provider_code": provider_code}
    if status in (401, 403):
        return LLMAuthError(f"authenticatie/rechten geweigerd ({text})", **kwargs)
    if status == 429:
        return LLMRateLimitError(f"rate limit of quotum ({text})", **kwargs)
    if status >= 500:
        return LLMServerError(f"serverfout bij de provider ({text})", **kwargs)
    return LLMBadRequestError(f"verzoek geweigerd ({text})", **kwargs)


class JsonHttpClient:
    """POST JSON naar `base_url + path` met retries, limieten en nette fouten."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float,
        max_retries: int,
        secrets: Iterable[str] = (),
        transport: httpx.BaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._secrets = tuple(s for s in secrets if s)
        self._max_response_bytes = max_response_bytes
        self._sleep = sleep
        self._rng = rng
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(CONNECT_TIMEOUT_S, timeout_s)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def post_json(
        self, path: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        url = self.base_url + path
        attempt = 0
        while True:
            try:
                return self._post_once(url, body, headers)
            except LLMError as exc:
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                log.warning(
                    "LLM-verzoek mislukt (%s, poging %d/%d), opnieuw over %.1f s",
                    exc.code,
                    attempt + 1,
                    self._max_retries + 1,
                    delay,
                )
                self._sleep(delay)
                attempt += 1

    def _retry_delay(self, exc: LLMError, attempt: int) -> float | None:
        if not exc.retryable or attempt >= self._max_retries:
            return None
        if exc.retry_after_s is not None:
            if exc.retry_after_s > MAX_RETRY_AFTER_S:
                return None  # langer wachten dan zinvol: direct melden
            return exc.retry_after_s
        backoff = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2.0**attempt))
        return backoff * (0.5 + 0.5 * self._rng())

    def _post_once(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        try:
            with self._client.stream("POST", url, json=body, headers=dict(headers)) as resp:
                if resp.status_code >= 300:
                    raise self._http_error(resp)
                raw = self._read_limited(resp, self._max_response_bytes)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"time-out bij de LLM-provider ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            msg = redact(str(exc), self._secrets)[:MAX_ERROR_MESSAGE_CHARS]
            raise LLMConnectionError(
                f"geen verbinding met de LLM-provider ({type(exc).__name__}: {msg})"
            ) from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise LLMResponseError("antwoord van de provider is geen geldige JSON") from None
        if not isinstance(data, dict):
            raise LLMResponseError("antwoord van de provider is geen JSON-object")
        return data

    def _read_limited(self, resp: httpx.Response, limit: int) -> bytes:
        declared = resp.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise LLMResponseError(f"antwoord te groot ({declared} bytes, max {limit})")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > limit:
                raise LLMResponseError(f"antwoord te groot (meer dan {limit} bytes)")
        return bytes(buf)

    def _http_error(self, resp: httpx.Response) -> LLMError:
        if 300 <= resp.status_code < 400:
            # Redirects volgen we bewust niet (sleutel zou mee kunnen gaan).
            return LLMBadRequestError(
                f"onverwachte redirect (HTTP {resp.status_code}); controleer "
                "PITOOLBOX_LLM_BASE_URL",
                status=resp.status_code,
            )
        try:
            body = self._read_limited(resp, MAX_ERROR_BODY_BYTES)
        except LLMResponseError:
            body = b""
        message, provider_code = _error_details(body)
        message = redact(" ".join(message.split()), self._secrets)[:MAX_ERROR_MESSAGE_CHARS]
        err = _error_for_status(resp.status_code, message, provider_code)
        err.retry_after_s = parse_retry_after(resp.headers.get("retry-after"))
        return err
