"""Two-step verification primitives (ADR-0033): TOTP codes, the secret's encryption at rest,
recovery codes and the setup QR code. Stateless: the use cases are in identity.mfa_services.

- **TOTP** is RFC 6238 over RFC 4226 (HMAC-SHA1, 6 digits, 30-second steps), implemented
  here with hmac/hashlib against the RFC's test vectors, accepting one step of clock drift
  either way. `matching_step` returns the step a code belongs to, so the caller can refuse
  that step and every earlier one afterwards (replay prevention, under the device's row lock).
- **The secret at rest** is a Fernet token under a key derived (HKDF-SHA256, a context of its
  own) from SECRET_KEY, then each of SECRET_KEY_FALLBACKS: a key rotation keeps every
  device working while the old key is a fallback, `rotate` re-encrypts under the current
  key (done on every successful verification, and for all devices by `manage.py
  mfa_reencrypt` before a fallback is retired).
- **Recovery codes**: 80 random bits each, shown as XXXX-XXXX-XXXX-XXXX in Crockford's
  base32 (no I, L, O or U; typed I/L read as 1 and O as 0), case, space and dash
  insensitive; only their SHA-256 is stored, and they are compared in constant time.
- **The QR code** is returned as a module matrix (rows of "0"/"1", no quiet zone), which the
  web app draws as SVG: no image, no data URI, no HTML.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import TypedDict
from urllib.parse import quote, urlencode

import segno
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.utils.crypto import constant_time_compare

DIGITS = 6
STEP_S = 30
DRIFT_STEPS = 1  # one step either way
SECRET_BYTES = 20  # 160 bits, RFC 4226's recommendation for HMAC-SHA1
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_BYTES = 10  # 80 bits
RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's base32
_RECOVERY_READS_AS = str.maketrans({"I": "1", "L": "1", "O": "0"})
_KEY_CONTEXT = b"arkray.identity.mfa.totp-secret.v1"


# --- TOTP (RFC 6238) -----------------------------------------------------------------------
def hotp(key: bytes, counter: int, *, digits: int = DIGITS, digest: str = "sha1") -> str:
    """RFC 4226: dynamic truncation of HMAC(key, counter) to `digits` decimal digits."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % 10**digits).zfill(digits)


def step_at(at: float, step_s: int = STEP_S) -> int:
    return int(at // step_s)


def totp(
    key: bytes, at: float, *, digits: int = DIGITS, step_s: int = STEP_S, digest: str = "sha1"
) -> str:
    return hotp(key, step_at(at, step_s), digits=digits, digest=digest)


def generate_secret() -> str:
    """A new shared secret, base32 without padding (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def secret_bytes(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def normalise_code(raw: str) -> str | None:
    """A typed code: spaces and dashes ignored; None unless exactly six digits."""
    code = "".join(raw.split()).replace("-", "")
    return code if len(code) == DIGITS and code.isascii() and code.isdigit() else None


def matching_step(
    secret: str, code: str, *, at: float | None = None, after_step: int | None = None
) -> int | None:
    """The time step `code` is valid for (now, or one step either side), or None. Steps at
    or before `after_step` (the last one accepted) never match: replay prevention. Every
    candidate is compared, in constant time, whatever matched first."""
    typed = normalise_code(code)
    if typed is None:
        return None
    key = secret_bytes(secret)
    current = step_at(time.time() if at is None else at)
    found: int | None = None
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if constant_time_compare(hotp(key, step), typed) and found is None:
            found = step
    if found is None or (after_step is not None and found <= after_step):
        return None
    return found


def otpauth_uri(secret: str, account: str, issuer: str | None = None) -> str:
    """The Key URI authenticator apps scan (Google Authenticator's format)."""
    issuer = issuer or settings.MFA_ISSUER
    label = f"{quote(issuer, safe='')}:{quote(account, safe='@')}"
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_S,
        },
        quote_via=quote,
    )
    return f"otpauth://totp/{label}?{query}"


class QrMatrix(TypedDict):
    size: int
    rows: list[str]


def qr_matrix(content: str) -> QrMatrix:
    """The QR code of `content` as rows of "1" (dark) and "0" (light) modules, without the
    quiet zone (the client draws a 4-module margin)."""
    code = segno.make_qr(content, error="m")
    rows = ["".join("1" if module else "0" for module in row) for row in code.matrix]
    return {"size": len(rows), "rows": rows}


# --- the secret at rest ---------------------------------------------------------------------
def _derive(key: str) -> Fernet:
    material = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_CONTEXT).derive(
        key.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(material))


@lru_cache(maxsize=4)
def _cipher(keys: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([_derive(key) for key in keys])


def _keys() -> tuple[str, ...]:
    return (settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS)


def encrypt(secret: str) -> str:
    return _cipher(_keys()).encrypt(secret.encode("ascii")).decode("ascii")


def decrypt(token: str) -> str:
    """The secret, under the current key or any fallback; InvalidToken if under none."""
    return _cipher(_keys()).decrypt(token.encode("ascii")).decode("ascii")


def rotate(token: str) -> str:
    """The same secret encrypted under the current key (InvalidToken if under none)."""
    return _cipher(_keys()).rotate(token.encode("ascii")).decode("ascii")


def under_current_key(token: str) -> bool:
    try:
        _derive(settings.SECRET_KEY).decrypt(token.encode("ascii"))
    except InvalidToken:
        return False
    return True


# --- recovery codes -------------------------------------------------------------------------
def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        value = int.from_bytes(secrets.token_bytes(RECOVERY_CODE_BYTES), "big")
        chars = "".join(RECOVERY_ALPHABET[(value >> (5 * i)) & 0x1F] for i in range(15, -1, -1))
        codes.add("-".join(chars[i : i + 4] for i in range(0, 16, 4)))
    return sorted(codes)


def normalise_recovery_code(raw: str) -> str | None:
    """The 16 characters of a typed recovery code (case, spaces and dashes ignored; I/L read
    as 1, O as 0), or None if it can't be one."""
    text = "".join(raw.split()).replace("-", "").upper().translate(_RECOVERY_READS_AS)
    if len(text) != 16 or any(char not in RECOVERY_ALPHABET for char in text):
        return None
    return text


def recovery_code_hash(code: str) -> str:
    """SHA-256 of the normalised code (callers normalise first)."""
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def hash_recovery_codes(codes: Iterable[str]) -> list[str]:
    hashed = []
    for code in codes:
        normalised = normalise_recovery_code(code)
        if normalised is None:  # pragma: no cover — generated codes always normalise
            raise ValueError("Not a recovery code.")
        hashed.append(recovery_code_hash(normalised))
    return hashed


def matching_hash(raw: str, stored: Sequence[str]) -> str | None:
    """Which of `stored` the typed code hashes to, compared in constant time against all of
    them (no early exit), or None."""
    normalised = normalise_recovery_code(raw)
    if normalised is None:
        return None
    typed = recovery_code_hash(normalised)
    found: str | None = None
    for candidate in stored:
        if constant_time_compare(candidate, typed) and found is None:
            found = candidate
    return found
