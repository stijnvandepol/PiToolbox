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
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeGuard

from pitoolbox.llm._http import error_for_status
from pitoolbox.llm.errors import LLMBadRequestError, LLMError, LLMResponseError, LLMServerError

Clean = Callable[[str], str]

# OpenRouter `error_type` van een inhoudsfilter of weigering: status 403, maar geen
# rechtenprobleem ("the request was understood and declined"; zelfde bron als hierboven).
_POLICY_ERROR_TYPES = frozenset({"content_policy_violation", "refusal"})
# `finish_reason` die een afgebroken generatie meldt: "error" (OpenRouter), "network_error"
# (GLM). Bronnen hierboven.
_ERROR_FINISH_REASONS = frozenset({"error", "network_error"})


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
