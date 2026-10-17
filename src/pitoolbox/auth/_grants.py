"""Autorisatiecodes en tokens in de auth-database; alleen als sha256-hash opgeslagen.

Zie `_store_core` voor hoe de delen van `AuthStore` op elkaar bouwen.
"""

from __future__ import annotations

import json
import sqlite3

from ._rows import CodeRow, TokenRow
from ._store_core import _StoreCore
from .crypto import hash_token


class _GrantStore(_StoreCore):
    """Zie `_store_core` voor hoe de delen van `AuthStore` op elkaar bouwen."""

    # --- autorisatiecodes --------------------------------------------------------------------

    def add_code(self, code: str, row: CodeRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO auth_codes (code_hash, client_id, scopes, code_challenge, "
                "redirect_uri, redirect_uri_explicit, resource, subject, expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hash_token(code),
                    row.client_id,
                    json.dumps(row.scopes),
                    row.code_challenge,
                    row.redirect_uri,
                    int(row.redirect_uri_explicit),
                    row.resource,
                    row.subject,
                    row.expires,
                ),
            )

    def get_code(self, code: str) -> CodeRow | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM auth_codes WHERE code_hash = ?", (hash_token(code),)
            ).fetchone()
        if row is None:
            return None
        return CodeRow(
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_explicit=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            subject=row["subject"],
            expires=row["expires"],
            used=bool(row["used"]),
            family_id=row["family_id"],
        )

    def consume_code(self, code: str, family_id: str) -> bool:
        """Markeer een code als gebruikt (atomisch); False als hij al gebruikt/verlopen was."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE auth_codes SET used = 1, family_id = ? "
                "WHERE code_hash = ? AND used = 0 AND expires > ?",
                (family_id, hash_token(code), self._clock()),
            )
            return cur.rowcount == 1

    # --- tokens ------------------------------------------------------------------------------

    def add_access_token(self, token: str, row: TokenRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO access_tokens (token_hash, client_id, family_id, scopes, resource, "
                "subject, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hash_token(token),
                    row.client_id,
                    row.family_id,
                    json.dumps(row.scopes),
                    row.resource,
                    row.subject,
                    row.expires,
                ),
            )

    def add_refresh_token(self, token: str, row: TokenRow) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO refresh_tokens (token_hash, client_id, family_id, scopes, resource, "
                "subject, expires, rotated) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    hash_token(token),
                    row.client_id,
                    row.family_id,
                    json.dumps(row.scopes),
                    row.resource,
                    row.subject,
                    row.expires,
                ),
            )

    @staticmethod
    def _token_row(row: sqlite3.Row, *, refresh: bool) -> TokenRow:
        return TokenRow(
            client_id=row["client_id"],
            family_id=row["family_id"],
            scopes=json.loads(row["scopes"]),
            resource=row["resource"],
            subject=row["subject"],
            expires=row["expires"],
            rotated=bool(row["rotated"]) if refresh else False,
        )

    def get_access_token(self, token: str) -> TokenRow | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM access_tokens WHERE token_hash = ?", (hash_token(token),)
            ).fetchone()
        return None if row is None else self._token_row(row, refresh=False)

    def get_refresh_token(self, token: str) -> TokenRow | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ?", (hash_token(token),)
            ).fetchone()
        return None if row is None else self._token_row(row, refresh=True)

    def rotate_refresh_token(self, token: str) -> bool:
        """Markeer een refresh-token als geroteerd (atomisch); False bij hergebruik."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE refresh_tokens SET rotated = 1 "
                "WHERE token_hash = ? AND rotated = 0 AND expires > ?",
                (hash_token(token), self._clock()),
            )
            return cur.rowcount == 1

    def revoke_family(self, family_id: str) -> int:
        """Verwijder alle access- en refresh-tokens van een familie; geeft het aantal rijen."""
        with self._tx() as conn:
            total = conn.execute(
                "DELETE FROM access_tokens WHERE family_id = ?", (family_id,)
            ).rowcount
            total += conn.execute(
                "DELETE FROM refresh_tokens WHERE family_id = ?", (family_id,)
            ).rowcount
            return total
