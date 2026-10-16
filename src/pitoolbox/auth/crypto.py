"""Cryptografische hulpfuncties voor de autorisatielaag.

- Wachtwoord-hashing met scrypt (stdlib, geen extra dependency).
- TOTP volgens RFC 6238 (SHA1, 6 cijfers, 30 s) met replay-bescherming via de
  teruggegeven teller.
- Token-generatie en sha256-hashing voor opslag (tokens worden nooit leesbaar bewaard).

Bronnen:
- RFC 6238 (TOTP), bijlage B voor de testvectoren; sectie 5.2: hooguit een tijdstap
  netwerkvertraging toestaan en "The verifier MUST NOT accept the second attempt of the OTP".
  https://www.rfc-editor.org/rfc/rfc6238
- RFC 4226 (HOTP) voor de dynamic truncation.
- Key URI Format (otpauth://) van Google Authenticator.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote, urlencode

# --- wachtwoord-hashing -------------------------------------------------------------

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DKLEN = 32

# Bovengrenzen voor parameters uit een opgeslagen hash: een gemanipuleerde database
# mag geen enorme geheugenallocatie kunnen afdwingen.
_SCRYPT_MAX_N = 2**20
_SCRYPT_MAX_R = 16
_SCRYPT_MAX_P = 4


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    # hashlib.scrypt gebruikt standaard maxmem=32 MiB, precies de behoefte van n=2**15, r=8;
    # dat faalt dus zonder marge. Reken 2x de behoefte als limiet.
    maxmem = 2 * 128 * n * r * p + 1024 * 1024
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=maxmem, dklen=dklen
    )


def hash_password(password: str) -> str:
    """Geef een gecodeerde scrypt-hash: ``scrypt$n$r$p$salt_b64$hash_b64``."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return "$".join(
        [
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """Controleer een wachtwoord tegen een gecodeerde hash (constante tijd)."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    if not (2 <= n <= _SCRYPT_MAX_N and n & (n - 1) == 0):
        return False
    if not (1 <= r <= _SCRYPT_MAX_R and 1 <= p <= _SCRYPT_MAX_P) or not expected:
        return False
    actual = _scrypt(password, salt, n, r, p, len(expected))
    return hmac.compare_digest(actual, expected)


# --- TOTP (RFC 6238) ------------------------------------------------------------------

TOTP_DIGITS = 6
TOTP_PERIOD_S = 30
TOTP_ISSUER = "pitoolbox"


def generate_totp_secret() -> str:
    """Nieuw TOTP-geheim van 160 bit, base32 zonder padding (32 tekens)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    cleaned = secret_b32.replace(" ", "").replace("-", "").upper()
    cleaned += "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned)


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    """HOTP (RFC 4226) met HMAC-SHA1 en dynamic truncation."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % 10**digits).zfill(digits)


def totp_counter(t: float, period: int = TOTP_PERIOD_S) -> int:
    """Tijdstapteller T = floor((t - T0) / X) met T0 = 0 (RFC 6238 sectie 4.2)."""
    return int(t // period)


def totp(
    secret_b32: str, t: float, *, digits: int = TOTP_DIGITS, period: int = TOTP_PERIOD_S
) -> str:
    """TOTP-code voor tijdstip ``t`` (unix-seconden)."""
    return hotp(_decode_secret(secret_b32), totp_counter(t, period), digits)


def verify_totp(
    secret_b32: str,
    code: str,
    t: float,
    *,
    window: int = 1,
    digits: int = TOTP_DIGITS,
    period: int = TOTP_PERIOD_S,
) -> int | None:
    """Controleer een code met een venster van +-``window`` tijdstappen.

    Geeft de overeenkomende teller terug (of None). De aanroeper bewaart die teller en
    weigert een teller die niet groter is dan de laatst geaccepteerde: zo kan dezelfde
    code niet twee keer worden gebruikt (RFC 6238 sectie 5.2).
    """
    code = code.strip()
    if len(code) != digits or not code.isascii() or not code.isdigit():
        return None
    key = _decode_secret(secret_b32)
    current = totp_counter(t, period)
    matched: int | None = None
    # Altijd het hele venster doorlopen en constant-time vergelijken.
    for counter in range(current - window, current + window + 1):
        if counter >= 0 and hmac.compare_digest(hotp(key, counter, digits), code):
            matched = counter
    return matched


def otpauth_uri(secret_b32: str, account: str = "owner", issuer: str = TOTP_ISSUER) -> str:
    """otpauth://-URI voor een authenticator-app (QR-code of handmatig)."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    query = urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": TOTP_DIGITS,
            "period": TOTP_PERIOD_S,
        }
    )
    return f"otpauth://totp/{label}?{query}"


# --- tokens ---------------------------------------------------------------------------


def new_token() -> str:
    """Nieuw geheim token (256 bit, URL-veilig)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """sha256 (hex) van een token; alleen dit wordt opgeslagen."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
