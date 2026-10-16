"""Provider-neutrale berichten, tooldefinities en het genormaliseerde antwoord.

De agent-loop werkt alleen met deze types; elke adapter vertaalt ze naar het eigen
protocol en terug. Bewust klein gehouden:

- `system` is geen bericht maar een apart argument van `complete()`: Anthropic en de
  Responses-API hebben er een eigen veld voor, Chat Completions een `system`-bericht.
- Tooldefinities komen uit de bestaande `ToolSpec`s: hetzelfde JSON-schema
  (`spec.json_schema()`) en dezelfde beschrijving als de MCP-frontend. Geen tweede model.
- Tool-argumenten van het model zijn onbetrouwbaar. De adapter parseert alleen JSON;
  valideren doet de `ToolRunner` (enige validatiepunt). Kapotte JSON wordt een
  `ToolCall` met `error`, nooit een exceptie.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pitoolbox.core.tools.base import ToolSpec


@dataclass(frozen=True)
class ToolDef:
    """Een tool zoals het model hem ziet."""

    name: str
    description: str
    parameters: Mapping[str, Any]  # JSON-schema, exact `ToolSpec.json_schema()`


def tool_defs_from_specs(specs: Iterable[ToolSpec]) -> tuple[ToolDef, ...]:
    """Tooldefinities voor het model: hetzelfde schema en dezelfde tekst als de MCP-tools."""
    return tuple(
        ToolDef(
            name=s.name,
            description=s.full_description(),
            # Kopie: een adapter mag het schema nooit wijzigen in de registry.
            parameters=copy.deepcopy(s.json_schema()),
        )
        for s in specs
    )


class ToolCallError(StrEnum):
    """Waarom een tool-call niet uitgevoerd kan worden (de agent-loop meldt dit terug)."""

    INVALID_JSON = "invalid_json"  # argumenten zijn geen geldige JSON
    NOT_AN_OBJECT = "arguments_not_object"  # wel JSON, maar geen object
    MISSING_NAME = "missing_name"  # het model gaf geen toolnaam


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # Geparste argumenten; None als `error` gezet is. Nog NIET gevalideerd: dat doet de runner.
    arguments: dict[str, Any] | None
    # De argumenten zoals het model ze stuurde (JSON-tekst), voor terugsturen en audit.
    raw_arguments: str = "{}"
    error: ToolCallError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


class StopReason(StrEnum):
    END_TURN = "end_turn"  # klaar (ook: een stopsequentie geraakt)
    TOOL_USE = "tool_use"  # wil één of meer tools aanroepen
    MAX_TOKENS = "max_tokens"  # afgekapt op max_tokens of het contextvenster
    REFUSAL = "refusal"  # geweigerd of door een contentfilter tegengehouden
    OTHER = "other"  # onbekende of niet-ondersteunde reden; zie `raw_stop_reason`


@dataclass(frozen=True)
class ProviderState:
    """Ondoorzichtige providerdata die terug moet in het volgende verzoek.

    Voorbeelden: thinking-blokken van Anthropic (moeten ongewijzigd terug bij tool use),
    reasoning-items van de Responses-API, `reasoning_content` van DeepSeek. Een adapter
    gebruikt dit alleen als `kind` de eigen soort is; anders bouwt hij het bericht op uit
    tekst en tool-calls.
    """

    kind: str
    data: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    state: ProviderState | None = None


@dataclass(frozen=True)
class ToolResult:
    """Resultaat van één tool-call; meerdere achter elkaar horen bij dezelfde beurt."""

    call_id: str
    name: str
    content: str  # meestal de JSON van `ToolRunner.call`
    is_error: bool = False


Message = UserMessage | AssistantMessage | ToolResult


@dataclass(frozen=True)
class Completion:
    """Genormaliseerd antwoord van `LLMProvider.complete`."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: StopReason
    usage: Usage
    raw_stop_reason: str | None = None
    model: str | None = None
    state: ProviderState | None = None

    @property
    def message(self) -> AssistantMessage:
        """Het assistant-bericht om aan de geschiedenis toe te voegen (incl. providerdata)."""
        return AssistantMessage(text=self.text, tool_calls=self.tool_calls, state=self.state)
