"""Beheer-CLI voor de autorisatielaag (entry point ``pitoolbox-auth``).

Subcommando's:
- ``init``: wachtwoord (twee keer, min. 12 tekens) en TOTP-geheim instellen;
- ``reset-totp``: nieuw TOTP-geheim genereren;
- ``revoke-all``: alle tokens en clients verwijderen;
- ``status``: aantallen en blokkade tonen.

Wachtwoord en codes worden nooit teruggegeven op het scherm of gelogd.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections.abc import Sequence

from ..config import Settings
from .crypto import generate_totp_secret, hash_password, otpauth_uri, verify_totp
from .provider import CLIENT_MAX_IDLE_S
from .store import AuthStore

MIN_PASSWORD_LEN = 12
CONFIRM_ATTEMPTS = 3

# Vervangbaar in tests.
_getpass = getpass.getpass
_input = input


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _prompt_password() -> str | None:
    password = _getpass("Nieuw wachtwoord (min. 12 tekens): ")
    if len(password) < MIN_PASSWORD_LEN:
        _err(f"Wachtwoord te kort (minimaal {MIN_PASSWORD_LEN} tekens).")
        return None
    if _getpass("Herhaal wachtwoord: ") != password:
        _err("Wachtwoorden komen niet overeen.")
        return None
    return password


def _confirm_totp(secret: str) -> bool:
    """Toon het geheim en vraag een geldige code ter bevestiging."""
    _out()
    _out("Voeg dit toe aan je authenticator-app (SHA1, 6 cijfers, 30 s):")
    _out(f"  otpauth-URI : {otpauth_uri(secret)}")
    _out(f"  Geheim      : {secret}")
    _out()
    for _ in range(CONFIRM_ATTEMPTS):
        code = _input("Huidige 6-cijferige code ter bevestiging: ")
        if verify_totp(secret, code.replace(" ", ""), time.time()) is not None:
            return True
        _err("Code onjuist, probeer het opnieuw.")
    return False


def _cmd_init(store: AuthStore, args: argparse.Namespace) -> int:
    if store.get_user() is not None and not args.force:
        _err("Er is al een gebruiker. Gebruik --force om wachtwoord en TOTP te vervangen.")
        return 1
    password = _prompt_password()
    if password is None:
        return 1
    secret = generate_totp_secret()
    if not _confirm_totp(secret):
        _err("Bevestiging mislukt; niets opgeslagen.")
        return 1
    store.set_user(hash_password(password), secret)
    if args.force:
        # Een nieuw wachtwoord/geheim hoort oude sessies ongeldig te maken.
        store.revoke_all()
    _out("Gebruiker opgeslagen.")
    return 0


def _cmd_reset_totp(store: AuthStore, _args: argparse.Namespace) -> int:
    if store.get_user() is None:
        _err("Nog geen gebruiker; voer eerst 'init' uit.")
        return 1
    secret = generate_totp_secret()
    if not _confirm_totp(secret):
        _err("Bevestiging mislukt; oud TOTP-geheim blijft geldig.")
        return 1
    store.set_totp_secret(secret)
    _out("Nieuw TOTP-geheim opgeslagen.")
    return 0


def _cmd_revoke_all(store: AuthStore, _args: argparse.Namespace) -> int:
    counts = store.revoke_all()
    _out(
        "Ingetrokken: "
        f"{counts['access_tokens']} access-tokens, {counts['refresh_tokens']} refresh-tokens, "
        f"{counts['clients']} clients."
    )
    return 0


def _cmd_status(store: AuthStore, _args: argparse.Namespace) -> int:
    store.purge_expired(client_max_idle_s=CLIENT_MAX_IDLE_S)
    stats = store.stats()
    user = store.get_user()
    now = time.time()
    _out(f"Schema-versie        : {store.schema_version()}")
    _out(f"Gebruiker ingesteld  : {'ja' if user else 'nee'}")
    _out(f"Clients              : {stats.clients}")
    _out(f"Actieve access-tokens: {stats.active_access_tokens}")
    _out(f"Actieve refresh-tok. : {stats.active_refresh_tokens}")
    _out(f"Lopende verzoeken    : {stats.pending_requests}")
    if user is not None:
        if user.locked_until > now:
            _out(f"Blokkade             : nog {int(user.locked_until - now)} s")
        else:
            _out(f"Blokkade             : geen (mislukte pogingen: {user.failed_attempts})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pitoolbox-auth", description="Beheer de pitoolbox-login")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="wachtwoord en TOTP instellen")
    p_init.add_argument("--force", action="store_true", help="bestaande gebruiker vervangen")
    sub.add_parser("reset-totp", help="nieuw TOTP-geheim genereren")
    sub.add_parser("revoke-all", help="alle tokens en clients intrekken")
    sub.add_parser("status", help="status tonen")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    store = AuthStore(settings.auth_db, time.time)
    handlers = {
        "init": _cmd_init,
        "reset-totp": _cmd_reset_totp,
        "revoke-all": _cmd_revoke_all,
        "status": _cmd_status,
    }
    return handlers[args.command](store, args)


if __name__ == "__main__":
    sys.exit(main())
