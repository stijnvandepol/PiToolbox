"""Gedeelde stukken van ``pitoolbox-flipper`` (flipper_cli.py): fout, uitvoer en argumenten."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .core.errors import ToolboxError
from .core.scope import normalize_mac


class CliError(ToolboxError):
    """Fout met een bruikbare melding voor de gebruiker (exitcode 1)."""

    code = "cli_error"


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _mac_arg(value: str) -> str:
    try:
        return normalize_mac(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"ongeldig MAC-adres {value!r} (verwacht AA:BB:CC:DD:EE:FF)"
        ) from exc


def _format_table(rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows
    ]
    return "\n".join(lines)
