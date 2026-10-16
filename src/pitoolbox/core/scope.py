"""scope.yaml: wat van mij is en dus aangeraakt mag worden.

Wordt bij elke beslissing opnieuw gelezen als het bestand gewijzigd is (mtime),
zodat je de scope kunt aanpassen zonder de server te herstarten.
Ontbreekt het bestand, dan is de scope leeg: alle transmit en capture wordt geweigerd.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
# Flipper-paden: alleen SD (/ext) of intern (/int), geen '..', alleen veilige tekens.
FLIPPER_PATH_RE = re.compile(r"^/(ext|int)(/[A-Za-z0-9 _.\-()\[\]]+)*/?$")


def normalize_mac(value: str) -> str:
    v = value.strip().upper().replace("-", ":")
    if not _MAC_RE.fullmatch(v):
        raise ValueError(f"ongeldig MAC-adres: {value!r}")
    return v


def validate_flipper_path(value: str) -> str:
    if ".." in value.split("/") or not FLIPPER_PATH_RE.fullmatch(value) or len(value) > 255:
        raise ValueError(f"ongeldig Flipper-pad: {value!r}")
    return value


Mac = Annotated[str, AfterValidator(normalize_mac)]
FlipperPath = Annotated[str, AfterValidator(validate_flipper_path)]


class WifiNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    ssid: str | None = None
    bssid: Mac
    channel: int = Field(ge=1, le=196)


class InfraredDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,31}$")
    file: FlipperPath
    # None = alle knoppen uit het .ir-bestand toegestaan
    buttons: list[str] | None = None


class SubGhzDevice(BaseModel):
    """Voor latere transmit-tools; in fase 1 alleen informatief."""

    model_config = ConfigDict(extra="forbid")
    name: str
    frequency_hz: int = Field(ge=300_000_000, le=928_000_000)
    file: FlipperPath | None = None


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    wifi: list[WifiNetwork] = Field(default_factory=list)
    infrared: list[InfraredDevice] = Field(default_factory=list)
    subghz: list[SubGhzDevice] = Field(default_factory=list)

    def network_by_bssid(self, bssid: str) -> WifiNetwork | None:
        try:
            mac = normalize_mac(bssid)
        except ValueError:
            return None
        return next((n for n in self.wifi if n.bssid == mac), None)

    def bssids(self) -> set[str]:
        return {n.bssid for n in self.wifi}

    def ssids(self) -> set[str]:
        return {n.ssid for n in self.wifi if n.ssid}

    def ir_device(self, name: str) -> InfraredDevice | None:
        return next((d for d in self.infrared if d.name == name), None)


def load_scope(path: Path) -> Scope:
    if not path.exists():
        log.warning("scope-bestand %s ontbreekt: lege scope (alles buiten scope)", path)
        return Scope()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Scope.model_validate(data)


class ScopeStore:
    """Thread-safe cache van scope.yaml die herlaadt bij een gewijzigde mtime.

    Een ongeldig bestand na wijziging geeft een lege scope (fail closed), niet de oude.
    """

    def __init__(self, path: Path | None, scope: Scope | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._loaded = False
        self._scope = scope or Scope()
        self._error: str | None = None

    @classmethod
    def fixed(cls, scope: Scope) -> ScopeStore:
        """Voor tests: vaste scope zonder bestand."""
        return cls(None, scope)

    @property
    def error(self) -> str | None:
        return self._error

    def get(self) -> Scope:
        with self._lock:
            if self._path is None:
                return self._scope
            try:
                mtime: float | None = self._path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if not self._loaded or mtime != self._mtime:
                try:
                    self._scope = load_scope(self._path)
                    self._error = None
                except Exception as exc:  # noqa: BLE001 - fail closed bij elke parsefout
                    log.error("scope.yaml ongeldig, lege scope actief: %s", exc)
                    self._scope = Scope()
                    self._error = str(exc)
                self._mtime = mtime
                self._loaded = True
            return self._scope
