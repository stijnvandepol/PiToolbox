"""Inlogpagina voor de OAuth-flow: wachtwoord + TOTP, volledig zelfstandige HTML.

Flow: ``/authorize`` (SDK) -> provider.authorize() bewaart het verzoek en stuurt naar
``/login?request=<id>``. Hier tonen we wie toegang vraagt, naar welk domein we daarna
terugsturen, welke scopes er gevraagd worden en vragen wachtwoord + 6-cijferige code.
Bij succes volgt een 302 naar de redirect-URI met ``code`` en ``state``.

Bronnen:
- MCP-autorisatiespecificatie 2025-11-25, "Localhost Redirect URI Risks": de
  autorisatieserver MOET de hostnaam van de redirect-URI duidelijk tonen.
  https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- RFC 6238 sectie 5.2: een geaccepteerde TOTP-code mag niet opnieuw worden geaccepteerd.
- Content-Security-Policy ``form-action`` wordt door Chromium ook op de redirect NA een
  formulier-POST toegepast; daarom staat de oorsprong van de redirect-URI van dit ene verzoek
  in de ``form-action`` van de pagina (en niets anders buiten 'self').
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from .crypto import verify_password, verify_totp
from .provider import SCOPE_OFFLINE, SCOPE_TOOLBOX, PitoolboxOAuthProvider
from .store import AuthRequestRow, AuthStore

log = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
LOCK_DURATION_S = 15 * 60
FAIL_DELAY_S = 1.0
MAX_PASSWORD_LEN = 1024

_SCOPE_TEXT = {
    SCOPE_TOOLBOX: "Toegang tot de pitoolbox-tools (Flipper Zero, wifi-adapter, systeeminfo)",
    SCOPE_OFFLINE: "Ingelogd blijven zonder opnieuw inloggen (refresh-token)",
}

_CSS = """
:root{color-scheme:light dark}
body{font:16px/1.5 system-ui,sans-serif;max-width:30rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.4rem}
.host{font-size:1.6rem;font-weight:700;word-break:break-all;margin:.25rem 0}
.box{border:1px solid #888;border-radius:.5rem;padding:1rem;margin:1rem 0}
.err{border-color:#c00;color:#c00}
label{display:block;margin:.75rem 0 .25rem}
input[type=password],input[type=text]{width:100%;box-sizing:border-box;font:inherit;padding:.5rem}
button{font:inherit;padding:.6rem 1.2rem;margin:1rem .5rem 0 0;cursor:pointer}
small{opacity:.75;word-break:break-all}
ul{padding-left:1.2rem}
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="nl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _origin(uri: str) -> str | None:
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


class LoginHandlers:
    def __init__(
        self,
        provider: PitoolboxOAuthProvider,
        store: AuthStore,
        clock: Callable[[], float],
        *,
        fail_delay_s: float = FAIL_DELAY_S,
    ) -> None:
        self._provider = provider
        self._store = store
        self._clock = clock
        self._fail_delay_s = fail_delay_s
        # Serialiseer pogingen: de blokkade telt dan exact, ook bij parallelle verzoeken.
        self._attempt_lock = asyncio.Lock()

    def routes(self) -> list[Route]:
        return [Route("/login", endpoint=self.login, methods=["GET", "POST"])]

    # --- hulpfuncties -----------------------------------------------------------------------

    @staticmethod
    def _headers(form_origin: str | None = None) -> dict[str, str]:
        form_action = "'self'" + (f" {form_origin}" if form_origin else "")
        return {
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"form-action {form_action}; frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }

    def _message(self, status: int, title: str, text: str) -> Response:
        body = f"<h1>{html.escape(title)}</h1><div class='box err'>{html.escape(text)}</div>"
        return HTMLResponse(_page(title, body), status_code=status, headers=self._headers())

    def _form(
        self,
        request_id: str,
        req: AuthRequestRow,
        client: OAuthClientInformationFull | None,
        *,
        error: str | None = None,
        status: int = 200,
    ) -> Response:
        redirect_uri = str(req.params["redirect_uri"])
        host = urlsplit(redirect_uri).hostname or "onbekend"
        name = (client.client_name if client and client.client_name else "Onbekende applicatie")[
            :100
        ]
        scopes = "".join(
            f"<li>{html.escape(_SCOPE_TEXT.get(s, s))}</li>" for s in req.params.get("scopes", [])
        )
        err = f"<div class='box err'>{html.escape(error)}</div>" if error else ""
        e = html.escape
        body = f"""
<h1>pitoolbox: toegang verlenen?</h1>
<p>De applicatie <strong>{e(name)}</strong> vraagt toegang tot jouw pitoolbox.</p>
<div class="box">
  <div>Na goedkeuring ga je terug naar:</div>
  <div class="host">{e(host)}</div>
  <small>{e(redirect_uri)}</small>
</div>
<p>Gevraagde rechten:</p>
<ul>{scopes}</ul>
{err}
<form method="post" action="/login" autocomplete="off">
  <input type="hidden" name="request" value="{e(request_id)}">
  <input type="hidden" name="csrf" value="{e(req.csrf)}">
  <label for="password">Wachtwoord</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <label for="code">Code uit je authenticator-app (6 cijfers)</label>
  <input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code"
         pattern="[0-9 ]*" maxlength="7" required>
  <button type="submit" name="action" value="approve">Toestaan</button>
  <button type="submit" name="action" value="deny" formnovalidate>Weigeren</button>
</form>
<p><small>Alleen goedkeuren als jij dit zojuist zelf hebt gestart.</small></p>
"""
        return HTMLResponse(
            _page("pitoolbox: inloggen", body),
            status_code=status,
            headers=self._headers(_origin(redirect_uri)),
        )

    def _redirect(self, url: str) -> Response:
        return RedirectResponse(url, status_code=302, headers=self._headers())

    # --- handler -----------------------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._post(request)
        return await self._get(request)

    async def _get(self, request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        req = self._store.get_auth_request(request_id) if request_id else None
        if req is None:
            return self._message(
                400,
                "Verzoek ongeldig",
                "Dit inlogverzoek is ongeldig of verlopen. Begin opnieuw vanuit Claude.",
            )
        client = await self._provider.get_client(req.client_id)
        return self._form(request_id, req, client)

    async def _post(self, request: Request) -> Response:
        form = await request.form()

        def field(name: str) -> str:
            value = form.get(name)
            return value if isinstance(value, str) else ""

        request_id = field("request")
        req = self._store.get_auth_request(request_id) if request_id else None
        if req is None:
            return self._message(
                400,
                "Verzoek ongeldig",
                "Dit inlogverzoek is ongeldig of verlopen. Begin opnieuw vanuit Claude.",
            )
        if not hmac.compare_digest(field("csrf").encode(), req.csrf.encode()):
            return self._message(
                403, "Geweigerd", "Ongeldig formulier (CSRF). Laad de pagina opnieuw."
            )

        redirect_uri = str(req.params["redirect_uri"])
        state = req.params.get("state")

        if field("action") == "deny":
            self._store.delete_auth_request(request_id)
            return self._redirect(
                construct_redirect_uri(redirect_uri, error="access_denied", state=state)
            )

        client = await self._provider.get_client(req.client_id)
        async with self._attempt_lock:
            user = self._store.get_user()
            if user is None:
                return self._message(
                    503,
                    "Niet ingesteld",
                    "Er is nog geen gebruiker. Voer 'pitoolbox-auth init' uit op de Pi.",
                )
            now = self._clock()
            if user.locked_until > now:
                return self._locked(user.locked_until - now)

            password = field("password")
            password_ok = len(password) <= MAX_PASSWORD_LEN and await asyncio.to_thread(
                verify_password, password, user.password_hash
            )
            counter = verify_totp(user.totp_secret, field("code").replace(" ", ""), now)
            # Beide controles altijd uitvoeren; pas daarna beslissen (geen aanwijzing welke faalde).
            ok = password_ok and counter is not None
            # Replay-bescherming: dezelfde (of oudere) teller wordt nooit twee keer geaccepteerd.
            if ok and counter is not None and not self._store.accept_totp_counter(counter):
                ok = False
                log.warning("TOTP-code hergebruikt of te oud; login geweigerd")

            if not ok:
                after = self._store.register_failed_attempt(MAX_FAILED_ATTEMPTS, LOCK_DURATION_S)
                log.warning("mislukte login-poging")
                if self._fail_delay_s > 0:
                    await asyncio.sleep(self._fail_delay_s)
                if after is not None and after.locked_until > self._clock():
                    return self._locked(after.locked_until - self._clock())
                return self._form(
                    request_id,
                    req,
                    client,
                    error="Onjuist wachtwoord of onjuiste code.",
                    status=401,
                )

            self._store.clear_failures()

        issued = self._provider.create_authorization_code(request_id)
        if issued is None:
            return self._message(400, "Verzoek ongeldig", "Dit inlogverzoek is inmiddels verlopen.")
        code, state, target = issued
        log.info("login geslaagd voor client %s", req.client_id)
        return self._redirect(construct_redirect_uri(target, code=code, state=state))

    def _locked(self, remaining_s: float) -> Response:
        minutes = max(1, int(remaining_s // 60) + 1)
        resp = self._message(
            429,
            "Tijdelijk geblokkeerd",
            f"Te veel mislukte pogingen. Probeer het over {minutes} min opnieuw.",
        )
        resp.headers["Retry-After"] = str(int(remaining_s) + 1)
        return resp
