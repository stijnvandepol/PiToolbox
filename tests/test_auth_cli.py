"""Tests voor de beheer-CLI ``pitoolbox-auth``."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from pitoolbox.auth import cli
from pitoolbox.auth.crypto import totp, verify_password, verify_totp
from pitoolbox.auth.store import AuthStore, TokenRow

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
PASSWORD = "een veilig wachtwoord 42"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / "auth.sqlite"
    monkeypatch.setenv("PITOOLBOX_AUTH_DB", str(path))
    monkeypatch.setattr(cli, "generate_totp_secret", lambda: SECRET)
    return path


def _answers(
    monkeypatch: pytest.MonkeyPatch, passwords: list[str], code: Callable[[], str]
) -> None:
    pw = iter(passwords)
    monkeypatch.setattr(cli, "_getpass", lambda _prompt="": next(pw))
    monkeypatch.setattr(cli, "_input", lambda _prompt="": code())


def _store(db: Path) -> AuthStore:
    return AuthStore(db, time.time)


def test_init_saves_user_and_never_prints_password(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, [PASSWORD, PASSWORD], lambda: totp(SECRET, time.time()))
    assert cli.main(["init"]) == 0
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err
    assert "otpauth://totp/pitoolbox:" in out.out
    assert SECRET in out.out
    user = _store(db).get_user()
    assert user is not None
    assert verify_password(PASSWORD, user.password_hash)
    assert user.totp_secret == SECRET
    assert PASSWORD not in user.password_hash


def test_init_rejects_short_or_mismatching_password(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _answers(monkeypatch, ["kort"], lambda: "000000")
    assert cli.main(["init"]) == 1
    _answers(monkeypatch, [PASSWORD, PASSWORD + "x"], lambda: "000000")
    assert cli.main(["init"]) == 1
    assert _store(db).get_user() is None


def test_init_needs_valid_confirmation_code(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _answers(monkeypatch, [PASSWORD, PASSWORD], lambda: "abcdef")
    assert cli.main(["init"]) == 1
    assert _store(db).get_user() is None


def test_init_refuses_existing_user_without_force(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(db).set_user("hash", "OLD")
    _answers(monkeypatch, [PASSWORD, PASSWORD], lambda: totp(SECRET, time.time()))
    assert cli.main(["init"]) == 1
    user = _store(db).get_user()
    assert user is not None
    assert user.totp_secret == "OLD"
    assert cli.main(["init", "--force"]) == 0
    user = _store(db).get_user()
    assert user is not None
    assert user.totp_secret == SECRET


def test_reset_totp(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(db)
    assert cli.main(["reset-totp"]) == 1  # nog geen gebruiker
    store.set_user("hash", "OLD")
    _answers(monkeypatch, [], lambda: totp(SECRET, time.time()))
    assert cli.main(["reset-totp"]) == 0
    user = store.get_user()
    assert user is not None
    assert user.totp_secret == SECRET
    assert user.password_hash == "hash"
    assert verify_totp(SECRET, totp(SECRET, time.time()), time.time()) is not None


def test_reset_totp_failed_confirmation_keeps_old_secret(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(db)
    store.set_user("hash", "OLD")
    _answers(monkeypatch, [], lambda: "000000x")
    assert cli.main(["reset-totp"]) == 1
    user = store.get_user()
    assert user is not None
    assert user.totp_secret == "OLD"


def test_revoke_all_and_status(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(db)
    store.set_user("hash", "S")
    store.put_client("c1", "{}", max_clients=50, max_idle_s=10**9)
    row = TokenRow("c1", "fam", ["pitoolbox"], None, "owner", time.time() + 600)
    store.add_access_token("a", row)
    store.add_refresh_token("r", row)
    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "Clients              : 1" in status
    assert "Actieve access-tokens: 1" in status
    assert "Blokkade             : geen" in status
    assert cli.main(["revoke-all"]) == 0
    assert "1 clients" in capsys.readouterr().out
    assert store.stats().clients == 0
    assert store.get_user() is not None


def test_status_shows_lockout(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = _store(db)
    store.set_user("hash", "S")
    for _ in range(5):
        store.register_failed_attempt(5, 900)
    assert cli.main(["status"]) == 0
    assert "Blokkade             : nog" in capsys.readouterr().out
