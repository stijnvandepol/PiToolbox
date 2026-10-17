"""Fouten die een OpenAI-compatibele provider in een HTTP 200-antwoord meldt.

Zonder deze check wordt zo'n antwoord een algemene "onverwacht antwoord"-fout die nooit
opnieuw wordt geprobeerd. Hier wordt het dezelfde `LLMError` als bij de bijbehorende
HTTP-status, zodat `_http.JsonHttpClient` dezelfde retries doet (zie `_http.BodyCheck`).

Vormen (gecontroleerd op 2026-10-17):
- OpenRouter, https://openrouter.ai/docs/api_reference/errors-and-debugging:
  `{"error": {"code": number, "message": string, "metadata"?: {...}}}`; "Otherwise, the
  returned HTTP response status will be 200 and any error occurred while the LLM is
  producing the output will be emitted in the response body". Niet-streamend: "a 200 OK
  whose JSON body holds only an error object and no choices", of (Chat Completions-skin)
  `choices[0]` met `finish_reason: "error"` en een `error`-object naast gedeeltelijke
  inhoud. `code` is de HTTP-status; `metadata.error_type` is de stabiele soort.
- GLM (Z.ai), https://docs.z.ai/api-reference/llm/chat-completion: `finish_reason` van het
  200-antwoord kan `network_error` zijn ("Reason for model inference termination"), zonder
  error-object. De docs zeggen niets over opnieuw proberen; wij behandelen het als een
  tijdelijke serverfout (begrensd door PITOOLBOX_LLM_MAX_RETRIES).
- MiniMax (alleen met `Quirk.BASE_RESP`), https://platform.minimax.io/docs/api-reference/
  text-chat-openai: het 200-antwoord heeft `base_resp: {status_code, status_msg}` ("Error
  status code and details"; 0 is goed). Codes en advies per code:
  https://platform.minimax.io/docs/api-reference/errorcode (zie `_MINIMAX_CODES`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeGuard

from pitoolbox.llm._http import error_for_status
from pitoolbox.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)

Clean = Callable[[str], str]

# OpenRouter `error_type` van een inhoudsfilter of weigering: status 403, maar geen
# rechtenprobleem ("the request was understood and declined"; zelfde bron als hierboven).
_POLICY_ERROR_TYPES = frozenset({"content_policy_violation", "refusal"})
# `finish_reason` die een afgebroken generatie meldt: "error" (OpenRouter), "network_error"
# (GLM). Bronnen hierboven.
_ERROR_FINISH_REASONS = frozenset({"error", "network_error"})

# MiniMax `base_resp.status_code` -> (foutklasse, omschrijving). Alleen de codes die bij
# tekstgeneratie horen (stem-/kloon-codes laten we weg). Retrybaar alleen waar de
# errorcode-pagina "Please retry your requests later" zegt (1000, 1001, 1002, 1024, 1033,
# 1039) of het om een tijdelijke limiet gaat (1041 "conn limit", 2045 "rate growth
# limit"); 1013 ("Internal server error") staat alleen in de text-chat-openai-pagina.
# 1008 (saldo) en 2056 ("wait for the resource release in the next 5-hour window") zijn
# niet binnen seconden op te lossen en falen dus direct.
_MINIMAX_CODES: dict[int, tuple[type[LLMError], str]] = {
    1000: (LLMServerError, "onbekende fout bij de provider"),
    1001: (LLMTimeoutError, "time-out bij de provider"),
    1002: (LLMRateLimitError, "rate limit"),
    1004: (LLMAuthError, "authenticatie geweigerd; controleer PITOOLBOX_LLM_API_KEY"),
    1008: (LLMBadRequestError, "saldo op bij de provider"),
    1013: (LLMServerError, "serverfout bij de provider"),
    1024: (LLMServerError, "serverfout bij de provider"),
    1026: (LLMBadRequestError, "invoer geweigerd door het inhoudsfilter"),
    1027: (LLMBadRequestError, "uitvoer geweigerd door het inhoudsfilter"),
    1033: (LLMServerError, "systeemfout bij de provider"),
    1039: (LLMRateLimitError, "tokenlimiet"),
    1041: (LLMRateLimitError, "limiet op gelijktijdige verbindingen"),
    1042: (LLMBadRequestError, "te veel onzichtbare tekens in de invoer"),
    2013: (LLMBadRequestError, "ongeldige parameters"),
    2045: (LLMRateLimitError, "verzoeken stijgen te snel"),
    2049: (LLMAuthError, "ongeldige API-sleutel; controleer PITOOLBOX_LLM_API_KEY"),
    2056: (LLMBadRequestError, "gebruikslimiet bereikt; wacht op het volgende venster"),
}


def minimax_base_resp_error(data: dict[str, Any], clean: Clean) -> LLMError | None:
    """Fout uit MiniMax' `base_resp`, of None als die ontbreekt of status_code 0 is."""
    base = data.get("base_resp")
    if not isinstance(base, dict):
        return None
    code = base.get("status_code")
    if not isinstance(code, int) or isinstance(code, bool) or code == 0:
        return None
    message = clean(str(base.get("status_msg") or ""))
    where = f"base_resp {code}" + (f": {message}" if message else "")
    cls, what = _MINIMAX_CODES.get(code, (LLMResponseError, "fout van de provider"))
    return cls(f"{what} ({where})", provider_code=str(code))


def openai_compat_body_error(data: dict[str, Any], clean: Clean) -> LLMError | None:
    """Fout uit een 200-antwoord van Chat Completions, of None als het een gewoon antwoord is."""
    top = data.get("error")
    if isinstance(top, dict):
        return _from_error_object(top, clean)
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(first, dict) or first.get("finish_reason") not in _ERROR_FINISH_REASONS:
        return None
    inner = first.get("error")
    if isinstance(inner, dict):
        return _from_error_object(inner, clean)
    reason = str(first["finish_reason"])
    return LLMServerError(
        f"generatie afgebroken door de provider (finish_reason {reason})", provider_code=reason
    )


def _from_error_object(err: dict[str, Any], clean: Clean) -> LLMError:
    message = clean(str(err.get("message") or ""))
    metadata = err.get("metadata")
    error_type = metadata.get("error_type") if isinstance(metadata, dict) else None
    code = err.get("code")
    provider_code = error_type if isinstance(error_type, str) and error_type else _code_text(code)
    detail = f": {message}" if message else ""
    if error_type in _POLICY_ERROR_TYPES:
        return LLMBadRequestError(
            f"geweigerd door een inhoudsfilter of het model ({error_type}{detail})",
            status=code if _is_status(code) else None,
            provider_code=provider_code,
        )
    if _is_status(code):
        return error_for_status(code, message, provider_code, in_body=True)
    return LLMResponseError(
        f"fout van de provider in een HTTP 200-antwoord{detail}", provider_code=provider_code
    )


def _is_status(code: Any) -> TypeGuard[int]:
    return isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599


def _code_text(code: Any) -> str | None:
    if isinstance(code, bool) or not isinstance(code, int | str) or code == "":
        return None
    return str(code)
