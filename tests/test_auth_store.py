"""Tests voor de SQLite-store van de autorisatielaag."""

from __future__ import annotations

import sqlite3
import stat
import sys
from pathlib import Path

import pytest
from test_auth_support import FakeClock

from pitoolbox.auth.store import AuthStore, CodeRow, TokenRow


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1_000_000.0)


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> AuthStore:
    return AuthStore(tmp_path / "state" / "auth.sqlite", clock)


def _token_row(
    client_id: str = "c1", family: str = "fam", expires: float = 2_000_000.0
) -> TokenRow:
    return TokenRow(client_id, family, ["pitoolbox"], "https://x/mcp", "owner", expires)


def _code_row(client_id: str = "c1", expires: float = 1_000_300.0) -> CodeRow:
    return CodeRow(
        client_id, ["pitoolbox"], "chal", "https://claude.ai/cb", True, "r", "owner", expires
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-modi bestaan niet op Windows")
def test_file_and_dir_permissions(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "newdir" / "auth.sqlite"
    AuthStore(path, clock)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_schema_version_and_wal(store: AuthStore, tmp_path: Path) -> None:
    assert store.schema_version() == 1
    conn = sqlite3.connect(tmp_path / "state" / "auth.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_reopen_keeps_data_and_newer_schema_is_refused(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "auth.sqlite"
    first = AuthStore(path, clock)
    first.set_user("hash", "SECRET")
    assert AuthStore(path, clock).get_user() is not None
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema 99"):
        AuthStore(path, clock)


def test_user_lifecycle_and_lockout(store: AuthStore, clock: FakeClock) -> None:
    assert store.get_user() is None
    assert store.register_failed_attempt(5, 900) is None
    store.set_user("hash", "SECRET")
    for expected in (1, 2, 3, 4):
        user = store.register_failed_attempt(5, 900)
        assert user is not None
        assert user.failed_attempts == expected
        assert user.locked_until == 0
    user = store.register_failed_attempt(5, 900)
    assert user is not None
    assert user.locked_until == clock() + 900
    assert user.failed_attempts == 0  # teller begint na de blokkade opnieuw
    store.clear_failures()
    user = store.get_user()
    assert user is not None
    assert user.locked_until == 0


def test_totp_counter_replay_protection(store: AuthStore) -> None:
    store.set_user("hash", "SECRET")
    assert store.accept_totp_counter(100)
    assert not store.accept_totp_counter(100)
    assert not store.accept_totp_counter(99)
    assert store.accept_totp_counter(101)
    store.set_totp_secret("NEW")  # nieuw geheim wist de teller
    assert store.accept_totp_counter(5)


def test_set_user_replaces_and_resets(store: AuthStore) -> None:
    store.set_user("h1", "S1")
    store.register_failed_attempt(5, 900)
    store.set_user("h2", "S2")
    user = store.get_user()
    assert user is not None
    assert (user.password_hash, user.totp_secret, user.failed_attempts) == ("h2", "S2", 0)


def test_client_cap_keeps_most_recent(store: AuthStore, clock: FakeClock) -> None:
    for i in range(7):
        clock.advance(1)
        store.put_client(f"c{i}", "{}", max_clients=5, max_idle_s=10**9)
    assert store.count_clients() == 5
    assert store.get_client_json("c0") is None
    assert store.get_client_json("c1") is None
    assert store.get_client_json("c6") == "{}"


def test_client_cap_uses_last_used_not_created(store: AuthStore, clock: FakeClock) -> None:
    store.put_client("old", "{}", max_clients=2, max_idle_s=10**9)
    clock.advance(1)
    store.put_client("mid", "{}", max_clients=2, max_idle_s=10**9)
    clock.advance(1)
    store.touch_client("old")  # 'old' is nu het recentst gebruikt
    clock.advance(1)
    store.put_client("new", "{}", max_clients=2, max_idle_s=10**9)
    assert store.get_client_json("mid") is None
    assert store.get_client_json("old") is not None


def test_idle_clients_are_purged_and_take_tokens_with_them(
    store: AuthStore, clock: FakeClock
) -> None:
    store.put_client("stale", "{}", max_clients=50, max_idle_s=30 * 86400)
    store.add_access_token("tok", _token_row("stale"))
    store.add_refresh_token("ref", _token_row("stale"))
    clock.advance(31 * 86400)
    store.put_client("fresh", "{}", max_clients=50, max_idle_s=30 * 86400)
    assert store.get_client_json("stale") is None
    assert store.get_access_token("tok") is None
    assert store.get_refresh_token("ref") is None
    assert store.count_clients() == 1


def test_auth_request_roundtrip_expiry_and_delete(store: AuthStore, clock: FakeClock) -> None:
    store.add_auth_request("rid", "c1", {"a": 1}, "csrf", ttl_s=600)
    req = store.get_auth_request("rid")
    assert req is not None
    assert (req.client_id, req.params, req.csrf) == ("c1", {"a": 1}, "csrf")
    assert store.get_auth_request("other") is None
    assert store.delete_auth_request("rid")
    assert not store.delete_auth_request("rid")
    store.add_auth_request("rid2", "c1", {}, "csrf", ttl_s=600)
    clock.advance(601)
    assert store.get_auth_request("rid2") is None


def test_code_is_single_use_and_expires(store: AuthStore, clock: FakeClock) -> None:
    store.add_code("code", _code_row())
    assert store.consume_code("code", "fam")
    assert not store.consume_code("code", "fam2")
    row = store.get_code("code")
    assert row is not None
    assert row.used
    assert row.family_id == "fam"
    store.add_code("code2", _code_row(expires=clock() + 10))
    clock.advance(11)
    assert not store.consume_code("code2", "fam")
    assert store.get_code("missing") is None


def test_only_hashes_are_stored(store: AuthStore, tmp_path: Path) -> None:
    store.add_auth_request("REQUEST-ID-SECRET", "c1", {}, "csrf", 600)
    store.add_code("CODE-SECRET", _code_row())
    store.add_access_token("ACCESS-SECRET", _token_row())
    store.add_refresh_token("REFRESH-SECRET", _token_row())
    conn = sqlite3.connect(tmp_path / "state" / "auth.sqlite")
    try:
        dump = "\n".join(conn.iterdump())
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    raw = (tmp_path / "state" / "auth.sqlite").read_bytes()
    for secret in ("REQUEST-ID-SECRET", "CODE-SECRET", "ACCESS-SECRET", "REFRESH-SECRET"):
        assert secret not in dump
        assert secret.encode() not in raw


def test_token_rotation_flag_and_family_revocation(store: AuthStore) -> None:
    store.add_access_token("a1", _token_row(family="f1"))
    store.add_refresh_token("r1", _token_row(family="f1"))
    store.add_access_token("a2", _token_row(family="f2"))
    row = store.get_refresh_token("r1")
    assert row is not None
    assert not row.rotated
    assert store.rotate_refresh_token("r1")
    assert not store.rotate_refresh_token("r1")
    row = store.get_refresh_token("r1")
    assert row is not None
    assert row.rotated
    assert store.revoke_family("f1") == 2
    assert store.get_access_token("a1") is None
    assert store.get_refresh_token("r1") is None
    assert store.get_access_token("a2") is not None


def test_rotate_refuses_expired_refresh_token(store: AuthStore, clock: FakeClock) -> None:
    store.add_refresh_token("r", _token_row(expires=clock() + 5))
    clock.advance(6)
    assert not store.rotate_refresh_token("r")


def test_purge_expired_and_stats(store: AuthStore, clock: FakeClock) -> None:
    store.put_client("c1", "{}", max_clients=50, max_idle_s=10**9)
    store.add_access_token("a_short", _token_row(expires=clock() + 10))
    store.add_access_token("a_long", _token_row(expires=clock() + 1000))
    store.add_refresh_token("r_long", _token_row(expires=clock() + 1000))
    store.add_auth_request("rid", "c1", {}, "x", ttl_s=10)
    stats = store.stats()
    assert (stats.clients, stats.active_access_tokens, stats.active_refresh_tokens) == (1, 2, 1)
    assert stats.pending_requests == 1
    clock.advance(11)
    stats = store.stats()
    assert (stats.active_access_tokens, stats.pending_requests) == (1, 0)
    store.purge_expired(client_max_idle_s=10**9)
    assert store.get_access_token("a_short") is None
    assert store.get_access_token("a_long") is not None


def test_revoke_all_keeps_user(store: AuthStore) -> None:
    store.set_user("h", "S")
    store.put_client("c1", "{}", max_clients=50, max_idle_s=10**9)
    store.add_access_token("a", _token_row())
    store.add_refresh_token("r", _token_row())
    counts = store.revoke_all()
    assert counts["clients"] == 1
    assert counts["access_tokens"] == 1
    assert counts["refresh_tokens"] == 1
    assert store.count_clients() == 0
    assert store.get_access_token("a") is None
    assert store.get_user() is not None
