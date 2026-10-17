"""OAuth 2.1 autorisatieserver-provider voor pitoolbox (enkele gebruiker).

Implementeert het protocol ``OAuthAuthorizationServerProvider`` uit de MCP-SDK
(``mcp/server/auth/provider.py``) bovenop :class:`AuthStore`. De SDK levert zelf de routes
(``/authorize``, ``/token``, ``/register``, ``/revoke``, metadata) en controleert PKCE S256,
redirect_uri-gelijkheid en code-verloop in ``handlers/token.py``; deze provider zorgt voor:

- redirect-allowlist (alleen de twee Claude-callbacks, exacte string-match);
- DCR als publieke client (``token_endpoint_auth_method = none``) met begrensd aantal clients;
- codes die maar een keer geldig zijn (hergebruik trekt de uitgegeven tokens in);
- access-tokens gebonden aan ``resource`` (RFC 8707);
- refresh-rotatie met familie-intrekking bij hergebruik van een al geroteerd token
  (OAuth 2.1 sectie 4.3.1 en MCP-autorisatiespecificatie, "Token Theft").

Claude-eisen (https://claude.com/docs/connectors/building/authentication):
- Hosted Claude gebruikt exact ``https://claude.ai/api/mcp/auth_callback`` als redirect-URI.
- PKCE ``S256`` bij elk verzoek; metadata adverteert ``code_challenge_methods_supported``.
- DCR registreert Claude als publieke client en bij elke verse verbinding opnieuw, dus het
  aantal bewaarde clients wordt begrensd.
- Refresh-tokens roteren; een ongeldig refresh-token moet ``invalid_grant`` geven.
- Claude voegt ``offline_access`` toe als dat in ``scopes_supported`` staat.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Iterable
from urllib.parse import quote

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .crypto import new_token
from .store import AuthStore, CodeRow, TokenRow

log = logging.getLogger(__name__)

# Exacte redirect-URI's van de gehoste Claude-apps (claude.ai en claude.com). Geen prefix,
# geen wildcard, geen loopback: zie "Callback URLs" in de Claude-documentatie.
CLAUDE_REDIRECT_URIS: frozenset[str] = frozenset(
    {
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    }
)

SCOPE_TOOLBOX = "pitoolbox"
SCOPE_OFFLINE = "offline_access"
SUPPORTED_SCOPES: tuple[str, ...] = (SCOPE_TOOLBOX, SCOPE_OFFLINE)
DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_TOOLBOX,)

MAX_CLIENTS = 50
CLIENT_MAX_IDLE_S = 30 * 24 * 3600
AUTH_REQUEST_TTL_S = 600
MAX_PENDING_REQUESTS = 200
MAX_CLIENT_NAME_LEN = 200
OWNER_SUBJECT = "owner"

# RFC 7636 sectie 4.2: code_challenge is 43-128 tekens uit [A-Za-z0-9-._~].
_CODE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_\-.~]{43,128}$")


def _normalize_resource(resource: str) -> str:
    return resource.rstrip("/")


class PitoolboxOAuthProvider:
    """SDK-provider met SQLite-opslag; zie moduledocstring."""

    def __init__(  # noqa: PLR0913 keyword-only afhankelijkheden (werkt als een struct)
        self,
        store: AuthStore,
        *,
        public_url: str,
        access_token_ttl_s: int,
        refresh_token_ttl_s: int,
        auth_code_ttl_s: int,
        clock: Callable[[], float],
        extra_redirect_uris: Iterable[str] = (),
    ) -> None:
        self.store = store
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self.access_token_ttl_s = access_token_ttl_s
        self.refresh_token_ttl_s = refresh_token_ttl_s
        self.auth_code_ttl_s = auth_code_ttl_s
        self._clock = clock
        self.redirect_allowlist: frozenset[str] = CLAUDE_REDIRECT_URIS | frozenset(
            extra_redirect_uris
        )

    # --- clients -----------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.get_client_json(client_id)
        if raw is None:
            return None
        return OAuthClientInformationFull.model_validate_json(raw)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris:
            raise RegistrationError(
                error="invalid_redirect_uri", error_description="redirect_uris ontbreekt"
            )
        for uri in uris:
            if uri not in self.redirect_allowlist:
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect_uri niet toegestaan: {uri[:200]}",
                )
        if client_info.client_name and len(client_info.client_name) > MAX_CLIENT_NAME_LEN:
            raise RegistrationError(
                error="invalid_client_metadata", error_description="client_name te lang"
            )
        # Publieke client: de SDK maakt bij een ontbrekende methode een secret aan
        # (handlers/register.py); dat overschrijven we, het antwoord toont de definitieve waarden.
        client_info.token_endpoint_auth_method = "none"  # noqa: S105
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        # Een client mag alleen de scopes van deze server aanvragen; Claude vraagt
        # ``offline_access`` zelf bij /authorize, ook als DCR alleen ``pitoolbox`` noemde.
        client_info.scope = " ".join(SUPPORTED_SCOPES)
        self.store.put_client(
            client_info.client_id,
            client_info.model_dump_json(),
            max_clients=MAX_CLIENTS,
            max_idle_s=CLIENT_MAX_IDLE_S,
        )

    # --- /authorize --------------------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        redirect_uri = str(params.redirect_uri)
        registered = {str(u) for u in client.redirect_uris or []}
        if redirect_uri not in registered or redirect_uri not in self.redirect_allowlist:
            raise AuthorizeError(error="invalid_request", error_description="redirect_uri ongeldig")

        # RFC 8707: de aangevraagde resource moet deze MCP-server zijn; zonder resource binden we
        # het token aan onze eigen resource.
        resource = self.resource_url
        if params.resource is not None and _normalize_resource(params.resource) != resource:
            raise AuthorizeError(error="invalid_target", error_description="onbekende resource")

        scopes = list(dict.fromkeys(params.scopes or DEFAULT_SCOPES))
        if any(s not in SUPPORTED_SCOPES for s in scopes) or SCOPE_TOOLBOX not in scopes:
            raise AuthorizeError(error="invalid_scope", error_description="scope niet toegestaan")

        if not _CODE_CHALLENGE_RE.match(params.code_challenge):
            raise AuthorizeError(
                error="invalid_request", error_description="code_challenge ongeldig"
            )

        self.store.purge_expired(client_max_idle_s=CLIENT_MAX_IDLE_S)
        if self.store.stats().pending_requests >= MAX_PENDING_REQUESTS:
            raise AuthorizeError(
                error="temporarily_unavailable", error_description="te veel lopende verzoeken"
            )
        self.store.touch_client(client.client_id)

        request_id = new_token()
        self.store.add_auth_request(
            request_id,
            client.client_id,
            {
                "state": params.state,
                "scopes": scopes,
                "code_challenge": params.code_challenge,
                "redirect_uri": redirect_uri,
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": resource,
            },
            csrf=new_token(),
            ttl_s=AUTH_REQUEST_TTL_S,
        )
        return f"{self.public_url}/login?request={quote(request_id, safe='')}"

    def create_authorization_code(self, request_id: str) -> tuple[str, str | None, str] | None:
        """Wissel een goedgekeurd verzoek in voor een code (door de loginpagina aangeroepen).

        Geeft ``(code, state, redirect_uri)`` of None als het verzoek niet (meer) bestaat.
        Het verzoek is daarna verbruikt.
        """
        req = self.store.get_auth_request(request_id)
        if req is None or not self.store.delete_auth_request(request_id):
            return None
        p = req.params
        code = new_token()
        self.store.add_code(
            code,
            CodeRow(
                client_id=req.client_id,
                scopes=list(p["scopes"]),
                code_challenge=p["code_challenge"],
                redirect_uri=p["redirect_uri"],
                redirect_uri_explicit=bool(p["redirect_uri_provided_explicitly"]),
                resource=p["resource"],
                subject=OWNER_SUBJECT,
                expires=self._clock() + self.auth_code_ttl_s,
            ),
        )
        return code, p.get("state"), str(p["redirect_uri"])

    # --- authorization_code ----------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.get_code(authorization_code)
        if row is None or row.client_id != client.client_id:
            return None
        if row.used:
            # OAuth 2.1 sectie 4.1.3: bij hergebruik van een code de uitgegeven tokens intrekken.
            if row.family_id:
                self.store.revoke_family(row.family_id)
            log.warning("autorisatiecode hergebruikt; tokenfamilie ingetrokken")
            return None
        if row.expires <= self._clock():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row.scopes,
            expires_at=row.expires,
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_explicit,
            resource=row.resource,
            subject=row.subject,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        family_id = secrets.token_urlsafe(16)
        if not self.store.consume_code(authorization_code.code, family_id):
            row = self.store.get_code(authorization_code.code)
            if row is not None and row.family_id:
                self.store.revoke_family(row.family_id)
            raise TokenError(
                error="invalid_grant", error_description="code is al gebruikt of verlopen"
            )
        self.store.touch_client(client.client_id)
        return self._issue_tokens(
            client_id=client.client_id,
            family_id=family_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )

    # --- refresh_token ---------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self.store.get_refresh_token(refresh_token)
        if row is None or row.client_id != client.client_id:
            return None
        if row.rotated:
            # Een al geroteerd token opnieuw aanbieden duidt op diefstal: hele familie weg.
            self.store.revoke_family(row.family_id)
            log.warning("geroteerd refresh-token hergebruikt; tokenfamilie ingetrokken")
            return None
        if row.expires <= self._clock():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires),
            resource=row.resource,
            subject=row.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        row = self.store.get_refresh_token(refresh_token.token)
        if row is None or row.client_id != client.client_id:
            raise TokenError(error="invalid_grant", error_description="refresh-token ongeldig")
        if SCOPE_TOOLBOX not in scopes or any(s not in row.scopes for s in scopes):
            raise TokenError(error="invalid_scope", error_description="scope niet toegestaan")
        if not self.store.rotate_refresh_token(refresh_token.token):
            # Gelijktijdig of eerder gebruikt (of net verlopen): familie intrekken.
            self.store.revoke_family(row.family_id)
            raise TokenError(error="invalid_grant", error_description="refresh-token ongeldig")
        self.store.touch_client(client.client_id)
        return self._issue_tokens(
            client_id=row.client_id,
            family_id=row.family_id,
            scopes=scopes,
            resource=row.resource,
            subject=row.subject,
        )

    # --- access-tokens ----------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_access_token(token)
        if row is None or row.expires <= self._clock():
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires),
            resource=row.resource,
            subject=row.subject,
            claims={"iss": self.public_url},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Trek het token en de rest van zijn familie in (RFC 7009: onbekend is geen fout)."""
        if isinstance(token, RefreshToken):
            row = self.store.get_refresh_token(token.token)
        else:
            row = self.store.get_access_token(token.token)
        if row is not None:
            self.store.revoke_family(row.family_id)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        """Enterprise-SSO (SEP-990 ID-JAG) wordt bewust niet ondersteund: één gebruiker."""
        raise TokenError(
            error="unsupported_grant_type",
            error_description="jwt-bearer grant wordt niet ondersteund",
        )

    # --- intern -------------------------------------------------------------------------------

    def _issue_tokens(
        self,
        *,
        client_id: str,
        family_id: str,
        scopes: list[str],
        resource: str | None,
        subject: str | None,
    ) -> OAuthToken:
        now = self._clock()
        access = new_token()
        refresh = new_token()
        self.store.add_access_token(
            access,
            TokenRow(
                client_id, family_id, scopes, resource, subject, now + self.access_token_ttl_s
            ),
        )
        # Altijd een refresh-token: de access-token leeft maar kort (standaard 15 min) en
        # Claude vraagt ``offline_access`` toch. Verloop schuift mee (sliding) bij elke rotatie.
        self.store.add_refresh_token(
            refresh,
            TokenRow(
                client_id, family_id, scopes, resource, subject, now + self.refresh_token_ttl_s
            ),
        )
        return OAuthToken(
            access_token=access,
            expires_in=self.access_token_ttl_s,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )
