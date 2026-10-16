"""Toolcontract: elke actie = handler + pydantic-argumentmodel (JSON-schema) + risicoklasse.

Er is bewust geen generieke shell- of 'run command'-tool. Elke tool heeft een vast
argumentmodel met `extra="forbid"`; de runner valideert vóór policy en uitvoering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from pitoolbox.config import Settings
    from pitoolbox.core.devices.flipper import FlipperDevice
    from pitoolbox.core.devices.system import SystemProbe
    from pitoolbox.core.devices.wifi import AlfaAdapter, CaptureManager
    from pitoolbox.core.policy import PendingStore
    from pitoolbox.core.scope import Scope, ScopeStore


class Risk(StrEnum):
    READ = "read"  # scannen/lezen
    CAPTURE = "capture"  # opslaan / adapter-state wijzigen
    TRANSMIT = "transmit"  # alles wat zendt
    CONTROL = "control"  # emergency_stop, confirm_action: altijd toegestaan


class ToolArgs(BaseModel):
    """Basisklasse voor argumentmodellen: onbekende velden zijn een fout."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ScopeCheck:
    in_scope: bool
    reason: str = ""


@dataclass
class ToolContext:
    """Alles wat een handler mag gebruiken. Devices worden lui verbonden.

    Afbreken gaat per device (`flipper.abort()`, `captures.stop_all()`), niet via een
    gedeelde vlag: devices gebruiken een generatieteller zodat een nieuwe call een
    lopende abort niet per ongeluk 'wist'.
    """

    settings: Settings
    scope_store: ScopeStore
    flipper: FlipperDevice
    wifi: AlfaAdapter
    captures: CaptureManager
    system: SystemProbe
    pending: PendingStore

    @property
    def scope(self) -> Scope:
        return self.scope_store.get()


Handler = Callable[[ToolContext, Any], dict[str, Any]]
ScopeFn = Callable[[Any, "Scope"], ScopeCheck]
SummaryFn = Callable[[Any, "Scope"], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: Risk
    params: type[ToolArgs]
    handler: Handler
    # Verplicht voor CAPTURE en TRANSMIT; optioneel voor READ (bijv. frequentiegrenzen).
    scope_check: ScopeFn | None = None
    # Mensleesbare samenvatting voor bevestigingsvragen (transmit); krijgt ook de scope.
    summary: SummaryFn | None = None
    # Grove bovengrens voor de duur, zodat frontends een timeout kunnen kiezen.
    max_duration_s: float = 30.0

    def json_schema(self) -> dict[str, Any]:
        return self.params.model_json_schema()

    def full_description(self) -> str:
        note = {
            Risk.READ: "Risicoklasse: read (alleen lezen/scannen).",
            Risk.CAPTURE: "Risicoklasse: capture (slaat data op; alleen doelen uit scope.yaml).",
            Risk.TRANSMIT: (
                "Risicoklasse: transmit. Zendt alleen naar apparaten in scope.yaml; buiten scope "
                "wordt altijd geweigerd. Binnen scope krijg je eerst een pending_id terug die de "
                "gebruiker expliciet moet bevestigen via confirm_action."
            ),
            Risk.CONTROL: "Risicoklasse: control (altijd toegestaan).",
        }[self.risk]
        return f"{self.description}\n\n{note}"

    def __post_init__(self) -> None:
        if self.risk in (Risk.CAPTURE, Risk.TRANSMIT) and self.scope_check is None:
            raise ValueError(f"tool {self.name}: {self.risk} vereist een scope_check")


class Registry:
    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for s in specs or []:
            self.add(s)

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"dubbele toolnaam: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def __contains__(self, name: object) -> bool:
        return name in self._specs
