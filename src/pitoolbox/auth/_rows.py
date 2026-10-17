"""Rijen uit de auth-database zoals de rest van de autorisatielaag ze ziet."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UserRow:
    password_hash: str
    totp_secret: str
    last_totp_counter: int
    failed_attempts: int
    locked_until: float


@dataclass(frozen=True)
class AuthRequestRow:
    client_id: str
    params: dict[str, Any]
    csrf: str
    expires: float


@dataclass(frozen=True)
class CodeRow:
    client_id: str
    scopes: list[str]
    code_challenge: str
    redirect_uri: str
    redirect_uri_explicit: bool
    resource: str | None
    subject: str | None
    expires: float
    used: bool = False
    family_id: str | None = None


@dataclass(frozen=True)
class TokenRow:
    """Gedeelde vorm voor access- en refresh-tokens."""

    client_id: str
    family_id: str
    scopes: list[str]
    resource: str | None
    subject: str | None
    expires: float
    rotated: bool = False


@dataclass(frozen=True)
class StoreStats:
    clients: int
    active_access_tokens: int
    active_refresh_tokens: int
    pending_requests: int
