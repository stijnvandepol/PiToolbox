"""Audit-log: één JSON-regel per toolcall (JSONL), append-only.

Velden: ts, frontend, tool, risk, args, decision, ok, error, result, duration_ms
(+ pending_id bij bevestigingen). Lange strings worden ingekort zodat bijv.
storage_read de log niet opblaast; bestandsinhoud hoort in captures, niet hier.

De agent-loop (fase 4) schrijft daarnaast per beurt een regel met `event`
("agent_turn_start" / "agent_turn_end": model, stopreden, aantallen, tokens; nooit
berichtinhoud) en regels met `source: "agent"` voor wat de agent zelf weigert of de
gebruiker afwijst. Toolcall-regels hebben geen `event`-veld.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_STR = 500
MAX_ITEMS = 50


def _shrink(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else value[:MAX_STR] + f"…(+{len(value) - MAX_STR})"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, list | tuple):
        items = [_shrink(v, depth + 1) for v in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"…(+{len(value) - MAX_ITEMS})")
        return items
    if isinstance(value, int | float | bool) or value is None:
        return value
    return str(value)


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, **fields: Any) -> dict[str, Any]:
        raw = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **fields}
        record: dict[str, Any] = _shrink(raw)
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
        return record
