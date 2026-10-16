"""Presets voor de Flipper-app: vaste prompts op de Pi, alleen id en label gaan naar de FAP.

Bestand (YAML, `PITOOLBOX_FLIPPER_UI_PRESETS`, standaard
`~/.config/pitoolbox/flipper_presets.yaml`):

    presets:
      - id: 1
        label: "Wifi-overzicht"
        prompt: "Doe een wifi-scan en vat samen welke netwerken in scope zijn."

Regels (zoals het PRESETS-bericht, docs/fase4-protocol.md §3 en §7): hoogstens 12 presets,
ids 1..255 en uniek, label 1..24 tekens printbare ASCII. De prompt blijft op de Pi en is
1..2000 tekens. Ontbreekt het bestand, dan zijn er geen presets; een ongeldig bestand geeft
`PresetsError` (de bridge logt dat en draait dan zonder presets).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pitoolbox.frontends.flipper_ui.protocol import MAX_PRESETS, Label

MAX_PROMPT = 2000


class PresetsError(ValueError):
    """Presetbestand ongeldig (de melding noemt het veld, nooit de prompttekst)."""


class PresetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[int, Field(ge=1, le=0xFF)]
    label: Label
    prompt: Annotated[str, Field(min_length=1, max_length=MAX_PROMPT)]


class _PresetFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    presets: tuple[PresetConfig, ...] = Field(default=(), max_length=MAX_PRESETS)

    @model_validator(mode="after")
    def _unique(self) -> _PresetFile:
        ids = [p.id for p in self.presets]
        if len(ids) != len(set(ids)):
            raise ValueError("preset-ids moeten uniek zijn")
        return self


def parse_presets(data: object) -> tuple[PresetConfig, ...]:
    if data is None:
        return ()
    if not isinstance(data, Mapping):
        raise PresetsError("presetbestand: verwacht een mapping met 'presets'")
    try:
        return _PresetFile.model_validate(dict(data)).presets
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        where = ".".join(str(p) for p in first["loc"]) or "presets"
        raise PresetsError(f"presetbestand: {where}: {first['msg']}") from None


def load_presets(path: Path) -> tuple[PresetConfig, ...]:
    """Lees en valideer het presetbestand; () als het niet bestaat."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise PresetsError(f"presetbestand {path} niet leesbaar: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PresetsError(f"presetbestand {path}: geen geldige YAML") from exc
    return parse_presets(data)
