"""Schema van de auth-database (SQLite) en de migratie ernaar.

Schema-versie via PRAGMA user_version.
"""

from __future__ import annotations

import sqlite3

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
CLIENT_TABLES = ("auth_requests", "auth_codes", "access_tokens", "refresh_tokens")


def migrate(conn: sqlite3.Connection) -> None:
    """Zet een lege database op SCHEMA_VERSION; weigert een nieuwer schema."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"auth-database heeft schema {version}, nieuwer dan bekend ({SCHEMA_VERSION})"
        )
    if version < 1:
        for statement in _SCHEMA_V1:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
