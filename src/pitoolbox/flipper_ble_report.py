"""Het rapport van ``ble-test`` (flipper_ble_measure.py): meetwaarden per transport,
statistiek en de tabel voor de terminal."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .flipper_cli_common import _format_table

# Sleutels uit DeviceInfo die we tonen (geen hardware_uid: dat is een uniek serienummer).
INFO_KEYS = (
    "hardware_model",
    "hardware_name",
    "firmware_version",
    "firmware_origin_fork",
    "firmware_branch",
    "firmware_commit",
    "protobuf_version_major",
    "protobuf_version_minor",
)


@dataclass
class TransportReport:
    transport: str
    connect_s: float | None = None
    info: dict[str, str] = field(default_factory=dict)
    ext_entries: int | None = None
    ping_method: str | None = None
    ping_ms: list[float] = field(default_factory=list)
    file: str | None = None
    file_size: int | None = None
    read_rounds: list[dict[str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def ping_stats(self) -> dict[str, float] | None:
        if not self.ping_ms:
            return None
        return {
            "n": float(len(self.ping_ms)),
            "min_ms": min(self.ping_ms),
            "median_ms": statistics.median(self.ping_ms),
            "mean_ms": statistics.fmean(self.ping_ms),
            "max_ms": max(self.ping_ms),
        }

    def download_stats(self) -> dict[str, float] | None:
        if not self.read_rounds:
            return None
        total_bytes = sum(r["bytes"] for r in self.read_rounds)
        total_s = max(sum(r["seconds"] for r in self.read_rounds), 1e-9)
        return {
            "bytes_per_round": total_bytes / len(self.read_rounds),
            "seconds": total_s,
            "bytes_per_s": total_bytes / total_s,
            "kib_per_s": total_bytes / total_s / 1024,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "connect_s": self.connect_s,
            "info": self.info,
            "ext_entries": self.ext_entries,
            "ping": {
                "method": self.ping_method,
                "samples_ms": self.ping_ms,
                **(self.ping_stats() or {}),
            },
            "download": {
                "file": self.file,
                "size": self.file_size,
                "rounds": self.read_rounds,
                **(self.download_stats() or {}),
            },
            "notes": self.notes,
        }


# --- uitvoer -----------------------------------------------------------------------------


def _fmt_ms(stats: dict[str, float] | None) -> str:
    if stats is None:
        return "-"
    return f"{stats['min_ms']:.0f} / {stats['median_ms']:.0f} / {stats['max_ms']:.0f} ms"


def _render_reports(address: str | None, reports: list[TransportReport]) -> str:
    lines = [f"pitoolbox BLE-test{f' ({address})' if address else ''}", ""]
    lines.append(_format_table(_report_rows(reports)))
    for r in reports:
        lines.extend(_report_details(r))
    return "\n".join(lines)


def _report_rows(reports: list[TransportReport]) -> list[list[str]]:
    labels = [r.transport.upper() for r in reports]
    rows: list[list[str]] = [["Meting", *labels]]
    rows.append(
        [
            "verbinden + system_info",
            *[f"{r.connect_s:.2f} s" if r.connect_s is not None else "-" for r in reports],
        ]
    )
    rows.append(
        [
            "items in /ext",
            *[str(r.ext_entries) if r.ext_entries is not None else "-" for r in reports],
        ]
    )
    method = next((r.ping_method for r in reports if r.ping_method), "ping")
    rounds = max((len(r.ping_ms) for r in reports), default=0)
    rows.append(
        [f"latency min/med/max ({method}, n={rounds})", *[_fmt_ms(r.ping_stats()) for r in reports]]
    )
    rows.append(_download_row(reports))
    return rows


def _download_row(reports: list[TransportReport]) -> list[str]:
    file_label = next((r.file for r in reports if r.file), None)
    size = next((r.file_size for r in reports if r.file_size is not None), None)
    cells = []
    for r in reports:
        stats = r.download_stats()
        cells.append(
            f"{stats['kib_per_s']:.1f} KiB/s ({stats['bytes_per_s']:.0f} B/s)" if stats else "-"
        )
    return [f"download {file_label} ({size} B)" if file_label else "download", *cells]


def _report_details(r: TransportReport) -> list[str]:
    lines: list[str] = []
    interesting = {k: v for k, v in r.info.items() if k in INFO_KEYS}
    if interesting:
        lines.append("")
        lines.append(f"Firmware/hardware ({r.transport.upper()}):")
        lines.extend(f"  {k} = {v}" for k, v in interesting.items())
    for note in r.notes:
        lines.append(f"Let op ({r.transport.upper()}): {note}")
    return lines
