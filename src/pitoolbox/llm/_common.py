"""Gedeelde hulpfuncties voor de adapters: tool-call-argumenten, id's, kleine typechecks."""

from __future__ import annotations

import json
import secrets
from typing import Any

from pitoolbox.llm.errors import LLMResponseError
from pitoolbox.llm.types import ToolCall, ToolCallError

SYNTHETIC_ID_PREFIX = "pitb_call_"


def synthetic_call_id() -> str:
    """Id voor een tool-call zonder id (sommige lokale servers laten hem weg).

    Alleen tekens uit `^[a-zA-Z0-9_-]+$`, het patroon dat Anthropic voor `tool_use.id` eist
    (https://platform.claude.com/docs/en/api/messages), zodat hij overal bruikbaar blijft.
    """
    return SYNTHETIC_ID_PREFIX + secrets.token_hex(8)


def make_tool_call(call_id: Any, name: Any, raw_args: Any) -> ToolCall:
    """Normaliseert één tool-call. Kapotte of vreemde argumenten worden `error`, geen exceptie.

    `raw_args` mag een JSON-tekst zijn (OpenAI-specificatie), een object (Ollama /api/chat:
    "arguments: type: object", https://docs.ollama.com/api/chat) of leeg/None (geen argumenten).
    """
    cid = call_id if isinstance(call_id, str) and call_id else synthetic_call_id()
    if not isinstance(name, str) or not name:
        return ToolCall(
            id=cid,
            name="",
            arguments=None,
            raw_arguments=_as_text(raw_args),
            error=ToolCallError.MISSING_NAME,
        )
    if raw_args is None or (isinstance(raw_args, str) and not raw_args.strip()):
        return ToolCall(id=cid, name=name, arguments={}, raw_arguments="{}")
    if isinstance(raw_args, dict):
        return ToolCall(
            id=cid, name=name, arguments=dict(raw_args), raw_arguments=json.dumps(raw_args)
        )
    if not isinstance(raw_args, str):
        return ToolCall(
            id=cid,
            name=name,
            arguments=None,
            raw_arguments=_as_text(raw_args),
            error=ToolCallError.NOT_AN_OBJECT,
        )
    try:
        parsed = json.loads(raw_args)
    except ValueError:
        return ToolCall(
            id=cid,
            name=name,
            arguments=None,
            raw_arguments=raw_args,
            error=ToolCallError.INVALID_JSON,
        )
    if not isinstance(parsed, dict):
        return ToolCall(
            id=cid,
            name=name,
            arguments=None,
            raw_arguments=raw_args,
            error=ToolCallError.NOT_AN_OBJECT,
        )
    return ToolCall(id=cid, name=name, arguments=parsed, raw_arguments=raw_args)


def replay_arguments(call: ToolCall) -> str:
    """Argumenten om terug te sturen in de geschiedenis: altijd geldige JSON.

    Ongeldige JSON van het model sturen we niet terug (servers die de chat-template
    renderen, parsen die en weigeren dan het hele verzoek); de agent-loop meldt de fout
    via het tool-resultaat.
    """
    return call.raw_arguments if call.ok else "{}"


MISSING_NAME_PLACEHOLDER = "unknown_tool"


def replay_name(call: ToolCall) -> str:
    """Toolnaam om terug te sturen; providers weigeren een lege naam in de geschiedenis."""
    return call.name or MISSING_NAME_PLACEHOLDER


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def require_list(data: dict[str, Any], key: str, what: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise LLMResponseError(f"onverwacht antwoord: {what} ontbreekt")
    return value
