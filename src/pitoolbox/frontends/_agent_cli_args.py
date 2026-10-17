"""Argumenten van `pitoolbox-agent`, en ``--list-providers`` (heeft geen configuratie nodig)."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pitoolbox.frontends._agent_cli_view import _out
from pitoolbox.llm.presets import ALIASES, PRESETS


def list_providers() -> None:
    """Tabel van alle waarden voor PITOOLBOX_LLM_PROVIDER, zonder iets te verbinden."""
    rows = [("naam", "formaat", "base-URL", "sleutel")]
    for preset in PRESETS.values():
        rows.append(
            (
                preset.name,
                preset.kind.value,
                preset.base_url or "(zelf zetten: PITOOLBOX_LLM_BASE_URL)",
                "verplicht" if preset.requires_key else "optioneel",
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        padded = [cell.ljust(width) for cell, width in zip(row[:3], widths, strict=True)]
        _out("  ".join([*padded, row[3]]))
    _out()
    _out("Aliassen: " + ", ".join(f"{alias} = {name}" for alias, name in ALIASES.items()))
    _out(
        "PITOOLBOX_LLM_MODEL is altijd verplicht (er is geen standaardmodel). "
        "PITOOLBOX_LLM_BASE_URL overschrijft de base-URL van een preset."
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pitoolbox-agent",
        description="PiToolbox-agent in de terminal (LLM uit PITOOLBOX_LLM_*)",
    )
    parser.add_argument(
        "--scope", type=Path, help="pad naar scope.yaml (overschrijft PITOOLBOX_SCOPE_FILE)"
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        help="pad naar het audit-logbestand (overschrijft PITOOLBOX_AUDIT_LOG)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="logging op INFO (stderr)")
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="toon de geldige waarden van PITOOLBOX_LLM_PROVIDER en stop",
    )
    return parser.parse_args(argv)
