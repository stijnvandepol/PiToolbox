"""Gemeenschappelijke basis voor de HTTP-adapters."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, ClassVar

import httpx

from pitoolbox.llm._http import JsonHttpClient
from pitoolbox.llm.config import LLMConfig, ProviderKind
from pitoolbox.llm.errors import LLMError
from pitoolbox.llm.types import Completion, Message, ProviderState, ToolDef


class HttpProvider:
    """Basis: bouwt de body (subklasse), POST via `JsonHttpClient`, normaliseert (subklasse).

    De API-sleutel blijft een `SecretStr` en wordt alleen in `_auth_headers` uitgelezen;
    `repr()` toont provider, model en base-URL, nooit de sleutel.
    """

    kind: ClassVar[ProviderKind]
    path: ClassVar[str]

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.provider is not self.kind:
            raise ValueError(f"{type(self).__name__} hoort bij {self.kind}, niet {config.provider}")
        self.config = config
        secret = config.api_key.get_secret_value() if config.api_key is not None else ""
        self._http = JsonHttpClient(
            base_url=config.base_url,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            secrets=(secret,) if secret else (),
            transport=transport,
            sleep=sleep,
        )

    @property
    def model(self) -> str:
        return self.config.model

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r}, base_url={self.config.base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HttpProvider:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        tools: Sequence[ToolDef] = (),
        max_tokens: int | None = None,
    ) -> Completion:
        if not messages:
            raise ValueError("complete() heeft minstens één bericht nodig")
        body = self._build_body(
            messages, system=system, tools=tools, max_tokens=max_tokens or self.config.max_tokens
        )
        data = self._http.post_json(self.path, body, self._headers(), check_body=self._body_error)
        return self._parse(data)

    # --- per provider ---

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _build_body(
        self,
        messages: Sequence[Message],
        *,
        system: str | None,
        tools: Sequence[ToolDef],
        max_tokens: int,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, data: dict[str, Any]) -> Completion:
        raise NotImplementedError

    def _body_error(self, data: dict[str, Any], clean: Callable[[str], str]) -> LLMError | None:
        """Fout in een HTTP 200-antwoord (zie `_http.BodyCheck`); standaard: geen."""
        return None

    # --- hulp ---

    def _secret(self) -> str | None:
        key = self.config.api_key
        return key.get_secret_value() if key is not None else None

    def _own_state(self, state: ProviderState | None) -> ProviderState | None:
        return state if state is not None and state.kind == self.kind.value else None
