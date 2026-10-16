"""SQLite-opslag voor de autorisatielaag (enkele gebruiker).

Ontwerp:
- Per aanroep een eigen verbinding (kort, WAL) plus een proces-brede lock; zo is de store
  veilig vanuit async handlers en threads (to_thread) zonder gedeelde cursor.
- Bestand krijgt modus 0600, de map 0700 (alleen als wij hem aanmaken).
- Tokens, autorisatiecodes en request-id's worden uitsluitend als sha256-hash opgeslagen.
- Schema-versie via PRAGMA user_version.
- Alle tijden komen uit een injecteerbare klok (unix-seconden) zodat tests kunnen vooruitspoelen.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crypto import hash_token

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_V1 = (
    """
CREATE TABLE user (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    totp_secret TEXT NOT NULL,
    last_totp_counter INTEGER NOT NULL DEFAULT -1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL
)""",
    """
CREATE TABLE clients (
    client_id TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    created REAL NOT NULL,
    last_used REAL NOT NULL
)""",
    """
CREATE TABLE auth_requests (
    id_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL
)""",
    """
CREATE TABLE auth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    redirect_uri_explicit INTEGER NOT NULL,
    resource TEXT,
    subject TEXT,
    expires REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    family_id TEXT
)""",
    """
CREATE TABLE access_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    subject TEXT,
    expires REAL NOT NULL
)""",
    "CREATE INDEX access_tokens_family ON access_tokens (family_id)",
    """
CREATE TABLE refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    subject TEXT,
    expires REAL NOT NULL,
    rotated INTEGER NOT NULL DEFAULT 0
)""",
    "CREATE INDEX refresh_tokens_family ON refresh_tokens (family_id)",
)

# Tabellen die aan een client hangen (voor opruimen van wezen).
_CLIENT_TABLES = ("auth_requests", "auth_codes", "access_tokens", "refresh_tokens")


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


class AuthStore:
    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._prepare_files()
        self._migrate()

    # --- infrastructuur ------------------------------------------------------------------

    def _prepare_files(self) -> None:
        parent = self._path.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
            # mkdir past de modus aan op umask; forceer 0700 op de nieuwe map zelf.
            with contextlib.suppress(OSError):
                parent.chmod(0o700)
        if not self._path.exists():
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        with contextlib.suppress(OSError):
            self._path.chmod(0o600)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                conn.close()

    def _migrate(self) -> None:
        with self._tx() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"auth-database heeft schema {version}, nieuwer dan bekend ({SCHEMA_VERSION})"
                )
            if version < 1:
                for statement in _SCHEMA_V1:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        with self._tx() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    # --- gebruiker -------------------------------------------------------------------------

    def get_user(self) -> UserRow | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM user WHERE id = 1").fetchone()
        if row is None:
            return None
        return UserRow(
            password_hash=row["password_hash"],
            totp_secret=row["totp_secret"],
            last_totp_counter=row["last_totp_counter"],
            failed_attempts=row["failed_attempts"],
            locked_until=row["locked_until"],
        )

    def set_user(self, password_hash: str, totp_secret: str) -> None:
        """Maak of vervang de (enige) gebruiker; wist ook blokkade en TOTP-teller."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO user (id, password_hash, totp_secret, created) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash, "
                "totp_secret = excluded.totp_secret, last_totp_counter = -1, "
                "failed_attempts = 0, locked_until = 0",
                (password_hash, totp_secret, self._clock()),
            )

    def set_totp_secret(self, totp_secret: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE user SET totp_secret = ?, last_totp_counter = -1 WHERE id = 1",
                (totp_secret,),
            )
            return cur.rowcount == 1

    def register_failed_attempt(self, max_attempts: int, lock_s: float) -> UserRow | None:
        """Tel een mislukte poging; bij ``max_attempts`` volgt een blokkade van ``lock_s``."""
        now = self._clock()
        with self._tx() as conn:
            row = conn.execute("SELECT failed_attempts FROM user WHERE id = 1").fetchone()
            if row is None:
                return None
            failed = int(row["failed_attempts"]) + 1
            if failed >= max_attempts:
                conn.execute(
                    "UPDATE user SET failed_attempts = 0, locked_until = ? WHERE id = 1",
                    (now + lock_s,),
                )
            else:
                conn.execute("UPDATE user SET failed_attempts = ? WHERE id = 1", (failed,))
        return self.get_user()

    def clear_failures(self) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE user SET failed_attempts = 0, locked_until = 0 WHERE id = 1")

    def accept_totp_counter(self, counter: int) -> bool:
        """Leg de teller vast; False als hij niet groter is dan de vorige (replay)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE user SET last_totp_counter = ? WHERE id = 1 AND last_totp_counter < ?",
                (counter, counter),
            )
            return cur.rowcount == 1

    # --- clients ---------------------------------------------------------------------------

    def put_client(
        self, client_id: str, client_json: str, *, max_clients: int, max_idle_s: float
    ) -> None:
        """Sla een client op; ruim lang ongebruikte clients op en begrens het aantal."""
        now = self._clock()
        with self._tx() as conn:
            conn.execute("DELETE FROM clients WHERE last_used < ?", (now - max_idle_s,))
            conn.execute(
                "INSERT OR REPLACE INTO clients (client_id, json, created, last_used) "
                "VALUES (?, ?, ?, ?)",
                (client_id, client_json, now, now),
            )
            # Houd de ``max_clients`` meest recent gebruikte; de rest (met tokens) verdwijnt.
            conn.execute(
                "DELETE FROM clients WHERE client_id IN ("
                "SELECT client_id FROM clients ORDER BY last_used DESC, created DESC "
                "LIMIT -1 OFFSET ?)",
                (max_clients,),
            )
            self._delete_orphans(conn)

    @staticmethod
    def _delete_orphans(conn: sqlite3.Connection) -> None:
        """Verwijder alles wat bij een niet meer bestaande client hoort."""
        for table in _CLIENT_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE client_id NOT IN (SELECT client_id FROM clients)"  # noqa: S608
            )

    def get_client_json(self, client_id: str) -> str | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT json FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        return None if row is None else str(row["json"])

    def touch_client(self, client_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE clients SET last_used = ? WHERE client_id = ?", (self._clock(), client_id)
            )

    def count_clients(self) -> int:
        with self._tx() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0])

    # --- lopende autorisatieverzoeken -----------------------------------------------------------

    def add_auth_request(
        self, request_id: str, client_id: str, params: dict[str, Any], csrf: str, ttl_s: float
    ) -> None:
        now = self._clock()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO auth_requests (id_hash, client_id, params, csrf, created, expires) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (hash_token(request_id), client_id, json.dumps(params), csrf, now, now + ttl_s),
            )

    def get_auth_request(self, request_id: str) -> AuthRequestRow | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM auth_requests WHERE id_hash = ?", (hash_token(request_id),)
            ).fetchone()
        if row is None or row["expires"] <= self._clock():
            return None
        return AuthRequestRow(
            client_id=row["client_id"],
            params=json.loads(row["params"]),
            csrf=row["csrf"],
            expires=row["expires"],
        )

    def delete_auth_request(self, request_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM auth_requests WHERE id_hash = ?", (hash_token(request_id),)
            )
            return cur.rowcount == 1

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

    # --- onderhoud ---------------------------------------------------------------------------

    def purge_expired(self, *, client_max_idle_s: float) -> None:
        now = self._clock()
        with self._tx() as conn:
            conn.execute("DELETE FROM access_tokens WHERE expires <= ?", (now,))
            conn.execute("DELETE FROM refresh_tokens WHERE expires <= ?", (now,))
            conn.execute("DELETE FROM auth_codes WHERE expires <= ?", (now,))
            conn.execute("DELETE FROM auth_requests WHERE expires <= ?", (now,))
            conn.execute("DELETE FROM clients WHERE last_used < ?", (now - client_max_idle_s,))
            self._delete_orphans(conn)

    def revoke_all(self) -> dict[str, int]:
        """Verwijder alle tokens, codes, verzoeken en clients (de gebruiker blijft)."""
        counts: dict[str, int] = {}
        with self._tx() as conn:
            for table in (*_CLIENT_TABLES, "clients"):
                counts[table] = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608
        return counts

    def stats(self) -> StoreStats:
        now = self._clock()
        with self._tx() as conn:
            clients = int(conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0])
            access = int(
                conn.execute(
                    "SELECT COUNT(*) FROM access_tokens WHERE expires > ?", (now,)
                ).fetchone()[0]
            )
            refresh = int(
                conn.execute(
                    "SELECT COUNT(*) FROM refresh_tokens WHERE expires > ? AND rotated = 0",
                    (now,),
                ).fetchone()[0]
            )
            pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM auth_requests WHERE expires > ?", (now,)
                ).fetchone()[0]
            )
        return StoreStats(
            clients=clients,
            active_access_tokens=access,
            active_refresh_tokens=refresh,
            pending_requests=pending,
        )
