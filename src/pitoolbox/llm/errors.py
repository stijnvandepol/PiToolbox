"""Foutklassen van de LLM-laag, los van de providers.

Elke fout heeft een vaste `code` (voor logging/audit door de agent-loop) en `retryable`.
Berichten bevatten nooit een API-sleutel of request-headers: foutteksten van de server
worden ingekort en geredigeerd voordat ze hier terechtkomen (zie `_http.redact`).
"""

from __future__ import annotations


class LLMError(Exception):
    """Basis voor alle fouten van de LLM-laag."""

    code = "llm_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        provider_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s
        # Fouttype/-code van de provider zelf (bijv. "rate_limit_error"), zonder vrije tekst.
        self.provider_code = provider_code


class LLMConfigError(LLMError):
    """Geen of ongeldige LLM-configuratie (provider, model, base-URL, sleutel)."""

    code = "llm_config"


class LLMAuthError(LLMError):
    """401/403: sleutel ontbreekt, is ongeldig of heeft geen rechten."""

    code = "llm_auth"


class LLMBadRequestError(LLMError):
    """Overige 4xx: het verzoek klopt niet (model onbekend, schema geweigerd, te groot)."""

    code = "llm_bad_request"


class LLMRateLimitError(LLMError):
    """429: te veel verzoeken of quotum op."""

    code = "llm_rate_limited"
    retryable = True


class LLMServerError(LLMError):
    """5xx (incl. 529 overloaded bij Anthropic)."""

    code = "llm_server_error"
    retryable = True


class LLMTimeoutError(LLMError):
    code = "llm_timeout"
    retryable = True


class LLMConnectionError(LLMError):
    code = "llm_connection"
    retryable = True


class LLMResponseError(LLMError):
    """Het antwoord is onbruikbaar: geen JSON, onverwachte vorm, te groot of 'failed'."""

    code = "llm_bad_response"
