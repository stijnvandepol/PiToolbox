"""Gedeelde hulpmiddelen voor de auth-tests (geen tests hierin)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from typing import Any

from mcp.shared.auth import OAuthClientInformationFull

from pitoolbox.config import Settings

PUBLIC_URL = "https://toolbox.example.com"
RESOURCE = f"{PUBLIC_URL}/mcp"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_COM_CB = "https://claude.com/api/mcp/auth_callback"
PASSWORD = "correct horse battery staple"


class FakeClock:
    """Injecteerbare klok. Start op de echte tijd: de SDK-handlers vergelijken met time.time()."""

    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "public_url": PUBLIC_URL,
        "auth_db": tmp_path / "state" / "auth.sqlite",
    }
    values.update(overrides)
    return Settings(**values)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def client_info(
    client_id: str = "client-1", redirect_uris: list[str] | None = None, **extra: Any
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(
        {
            "client_id": client_id,
            "redirect_uris": redirect_uris if redirect_uris is not None else [CLAUDE_CB],
            "client_name": extra.pop("client_name", "Claude"),
            "token_endpoint_auth_method": extra.pop("token_endpoint_auth_method", "none"),
            **extra,
        }
    )
