"""Eigen ``/revoke`` (RFC 7009) die de route van de MCP-SDK vervangt.

Waarom: in mcp 2.3.0 heeft ``RevocationRequest.client_secret`` (``mcp/server/auth/handlers/
revoke.py``) geen standaardwaarde, dus een publieke client (``token_endpoint_auth_method =
none``, en dat zijn al onze DCR-clients) krijgt zonder ``client_secret`` in het formulier een
400 "client_secret: Field required". Claude kan zo geen tokens intrekken. Nog niet opgelost in
een release (python-sdk issue #3508).

Zelfde gedrag als de SDK-route, behalve dat ``client_secret`` optioneel is:
- clientauthenticatie met de ``ClientAuthenticator`` van de SDK (publiek: alleen client_id;
  met secret: client_secret_post/basic zoals bij ``/token``);
- onbekend token is geen fout: 200 (RFC 7009 sectie 2.2);
- alleen tokens van de geauthenticeerde client worden ingetrokken (sectie 2.1);
- antwoord met ``Cache-Control: no-store`` en ``Pragma: no-cache``;
- dezelfde body-limiet en CORS als ``create_auth_routes`` in ``mcp/server/auth/routes.py``.

Afwijking: een mislukte clientauthenticatie geeft ``invalid_client`` (RFC 6749 sectie 5.2, waar
RFC 7009 sectie 2.2.1 naar verwijst) in plaats van het ``unauthorized_client`` van de SDK. Een
onbekende ``token_type_hint`` negeren we (sectie 2.1: de server MAG de hint negeren).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial

from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.server.auth.routes import REVOCATION_PATH
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, request_response

from .provider import PitoolboxOAuthProvider

__all__ = ["RevocationEndpoint"]

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_METHODS = ["POST", "OPTIONS"]


def _error(status: int, error: str, description: str) -> Response:
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status, headers=_NO_STORE
    )


class RevocationEndpoint:
    """Handler en route voor ``POST /revoke``; zie de moduledocstring."""

    def __init__(self, provider: PitoolboxOAuthProvider) -> None:
        self._provider = provider
        self._authenticator = ClientAuthenticator(provider)

    def route(self) -> Route:
        """Route met dezelfde middleware-volgorde als de SDK: CORS buitenom, dan de body-limiet
        (zo krijgt ook een 413 CORS-headers)."""
        app = CORSMiddleware(
            app=RequestBodyLimitMiddleware(
                request_response(self.handle), DEFAULT_MAX_REQUEST_BODY_SIZE
            ),
            allow_origins="*",
            allow_methods=_METHODS,
            allow_headers=[MCP_PROTOCOL_VERSION_HEADER],
        )
        return Route(REVOCATION_PATH, endpoint=app, methods=_METHODS)

    async def handle(self, request: Request) -> Response:
        try:
            client = await self._authenticator.authenticate_request(request)
        except AuthenticationError as exc:
            return _error(401, "invalid_client", exc.message)

        form = await request.form()  # Starlette geeft het al gelezen formulier terug
        token_value = form.get("token")
        if not isinstance(token_value, str) or not token_value:
            return _error(400, "invalid_request", "token ontbreekt")

        loaders: list[Callable[[str], Awaitable[AccessToken | RefreshToken | None]]] = [
            self._provider.load_access_token,
            partial(self._provider.load_refresh_token, client),
        ]
        if form.get("token_type_hint") == "refresh_token":
            loaders.reverse()

        token: AccessToken | RefreshToken | None = None
        for loader in loaders:
            token = await loader(token_value)
            if token is not None:
                break

        # RFC 7009 sectie 2.1: een token van een andere client niet intrekken, maar ook niet
        # verraden dat het bestaat (zelfde 200 als bij een onbekend token).
        if token is not None and token.client_id == client.client_id:
            await self._provider.revoke_token(token)
        return Response(status_code=200, headers=_NO_STORE)
