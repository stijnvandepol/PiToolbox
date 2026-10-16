"""Gescripte nep-`LLMProvider` voor de agent-tests (geen netwerk).

Het script is een lijst stappen; elke `complete()` neemt de volgende stap:
- een `Completion` wordt teruggegeven;
- een `BaseException` (bijv. `LLMServerError`, `KeyboardInterrupt`) wordt gegooid;
- een callable krijgt de berichten en geeft een `Completion` (voor stappen die iets uit de
  geschiedenis of de runner nodig hebben).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pitoolbox.llm.types import (
    AssistantMessage,
    Completion,
    Message,
    ProviderState,
    StopReason,
    ToolCall,
    ToolCallError,
    ToolDef,
    ToolResult,
    Usage,
    UserMessage,
)

Step = Completion | BaseException | Callable[[Sequence[Message]], Completion]

_ids = iter(range(1, 1_000_000))


def tool_call(
    name: str, args: dict[str, Any] | None = None, call_id: str | None = None
) -> ToolCall:
    args = args if args is not None else {}
    return ToolCall(
        id=call_id or f"call_{next(_ids)}",
        name=name,
        arguments=args,
        raw_arguments=json.dumps(args),
    )


def broken_call(raw: str = "{kapot", call_id: str | None = None) -> ToolCall:
    return ToolCall(
        id=call_id or f"call_{next(_ids)}",
        name="status",
        arguments=None,
        raw_arguments=raw,
        error=ToolCallError.INVALID_JSON,
    )


def reply(
    text: str,
    *,
    stop: StopReason = StopReason.END_TURN,
    state: ProviderState | None = None,
) -> Completion:
    return Completion(
        text=text,
        tool_calls=(),
        stop_reason=stop,
        usage=Usage(input_tokens=10, output_tokens=5),
        model="fake-model-2026",
        state=state,
    )


def use(
    *calls: ToolCall,
    text: str = "",
    stop: StopReason = StopReason.TOOL_USE,
    state: ProviderState | None = None,
) -> Completion:
    return Completion(
        text=text,
        tool_calls=tuple(calls),
        stop_reason=stop,
        usage=Usage(input_tokens=20, output_tokens=7),
        model="fake-model-2026",
        state=state,
    )


@dataclass(frozen=True)
class Request:
    messages: tuple[Message, ...]
    system: str | None
    tools: tuple[ToolDef, ...]


class ScriptedProvider:
    def __init__(self, script: Sequence[Step] = (), model: str = "fake-model") -> None:
        self.script: list[Step] = list(script)
        self.requests: list[Request] = []
        self.closed = False
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def add(self, *steps: Step) -> None:
        self.script.extend(steps)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        tools: Sequence[ToolDef] = (),
        max_tokens: int | None = None,
    ) -> Completion:
        # Momentopname: de sessie voegt na deze call nog berichten toe aan dezelfde lijst.
        self.requests.append(Request(tuple(messages), system, tuple(tools)))
        assert_valid_history(messages)
        if not self.script:
            raise AssertionError("script is op: onverwachte extra modelcall")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, Completion):
            return step
        return step(messages)

    def close(self) -> None:
        self.closed = True


def assert_valid_history(messages: Sequence[Message]) -> None:
    """De regels die alle drie de adapters nodig hebben.

    - begint met een gebruikersbericht, geen twee gebruikersberichten achter elkaar;
    - elke tool-call van een assistant-bericht krijgt direct daarna precies één resultaat;
    - geen resultaat zonder bijbehorende call.
    """
    assert messages, "lege geschiedenis"
    assert isinstance(messages[0], UserMessage), "geschiedenis begint niet met de gebruiker"
    i = 0
    prev: Message | None = None
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, UserMessage):
            assert not isinstance(prev, UserMessage), "twee gebruikersberichten achter elkaar"
            i += 1
        elif isinstance(msg, AssistantMessage):
            expected = [c.id for c in msg.tool_calls]
            got: list[str] = []
            j = i + 1
            while j < len(messages) and isinstance(messages[j], ToolResult):
                result = messages[j]
                assert isinstance(result, ToolResult)
                got.append(result.call_id)
                j += 1
            assert got == expected, f"tool-resultaten {got} horen bij calls {expected}"
            i = j
            msg = messages[j - 1]
        else:
            raise AssertionError(f"los tool-resultaat op positie {i}")
        prev = msg
