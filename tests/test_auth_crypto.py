"""Tests voor wachtwoord-hashing, TOTP (RFC 6238 bijlage B) en tokenhulpen."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from pitoolbox.auth import crypto

# RFC 6238 bijlage B (SHA1): seed is de ASCII-string "12345678901234567890", 8 cijfers, X = 30.
# https://www.rfc-editor.org/rfc/rfc6238
RFC_SEED_ASCII = b"12345678901234567890"
RFC_SECRET_B32 = base64.b32encode(RFC_SEED_ASCII).decode()
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize(("t", "expected"), RFC_VECTORS)
def test_totp_rfc6238_vectors_8_digits(t: int, expected: str) -> None:
    assert crypto.totp(RFC_SECRET_B32, t, digits=8) == expected


@pytest.mark.parametrize(("t", "expected"), RFC_VECTORS)
def test_totp_six_digits_is_truncation_of_rfc_vectors(t: int, expected: str) -> None:
    # Dezelfde 31-bit waarde modulo 10**6 in plaats van 10**8.
    assert crypto.totp(RFC_SECRET_B32, t) == expected[-6:]


def test_totp_counter_matches_rfc_hex_values() -> None:
    assert crypto.totp_counter(59) == 0x1
    assert crypto.totp_counter(1111111109) == 0x23523EC
    assert crypto.totp_counter(20000000000) == 0x27BC86AA


def test_verify_totp_returns_matched_counter() -> None:
    t = 1111111111
    code = crypto.totp(RFC_SECRET_B32, t)
    assert crypto.verify_totp(RFC_SECRET_B32, code, t) == crypto.totp_counter(t)


def test_verify_totp_window_is_plus_minus_one_step() -> None:
    t = 1111111111
    counter = crypto.totp_counter(t)
    for offset in (-1, 0, 1):
        code = crypto.totp(RFC_SECRET_B32, t + offset * 30)
        assert crypto.verify_totp(RFC_SECRET_B32, code, t) == counter + offset
    for offset in (-2, 2):
        code = crypto.totp(RFC_SECRET_B32, t + offset * 30)
        assert crypto.verify_totp(RFC_SECRET_B32, code, t) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 456", "١٢٣٤٥٦"])
def test_verify_totp_rejects_malformed_codes(bad: str) -> None:
    assert crypto.verify_totp(RFC_SECRET_B32, bad, 59) is None


def test_verify_totp_strips_whitespace_and_accepts_lowercase_secret() -> None:
    code = crypto.totp(RFC_SECRET_B32.lower(), 59)
    assert crypto.verify_totp(RFC_SECRET_B32, f" {code} ", 59) == 1


def test_generate_totp_secret_is_160_bit_base32() -> None:
    secret = crypto.generate_totp_secret()
    assert len(secret) == 32
    assert "=" not in secret
    assert len(base64.b32decode(secret)) == 20
    assert secret != crypto.generate_totp_secret()


def test_otpauth_uri() -> None:
    uri = crypto.otpauth_uri("ABCDEFGH", account="stijn@pi")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth"
    assert parts.netloc == "totp"
    assert parts.path == "/pitoolbox:stijn%40pi"
    query = parse_qs(parts.query)
    assert query["secret"] == ["ABCDEFGH"]
    assert query["issuer"] == ["pitoolbox"]
    assert query["algorithm"] == ["SHA1"]
    assert query["digits"] == ["6"]
    assert query["period"] == ["30"]


def test_password_round_trip_and_format() -> None:
    encoded = crypto.hash_password("een lang genoeg wachtwoord")
    scheme, n, r, p, salt, digest = encoded.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2**15), "8", "1")
    assert len(base64.b64decode(salt)) == 16
    assert len(base64.b64decode(digest)) == 32
    assert crypto.verify_password("een lang genoeg wachtwoord", encoded)


def test_password_hash_is_salted() -> None:
    assert crypto.hash_password("zelfde") != crypto.hash_password("zelfde")


def test_wrong_password_is_rejected() -> None:
    encoded = crypto.hash_password("goed wachtwoord 123")
    assert not crypto.verify_password("fout wachtwoord 123", encoded)
    assert not crypto.verify_password("", encoded)


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "plain",
        "scrypt$x$8$1$AAAA$AAAA",
        "bcrypt$32768$8$1$AAAA$AAAA",
        "scrypt$32768$8$1$!!!$AAAA",
        f"scrypt${2**30}$8$1$AAAA$AAAA",  # onredelijk grote n
        "scrypt$3$8$1$AAAA$AAAA",  # geen macht van twee
    ],
)
def test_malformed_hash_never_verifies(garbage: str) -> None:
    assert not crypto.verify_password("wachtwoord", garbage)


def test_token_helpers() -> None:
    a, b = crypto.new_token(), crypto.new_token()
    assert a != b
    assert len(a) >= 43  # 32 bytes urlsafe
    digest = crypto.hash_token(a)
    assert len(digest) == 64
    assert digest != a
    assert crypto.hash_token(a) == digest
