"""One-time secrets for emailed account links (invitations, password resets).

A secret is 32 random bytes from the OS CSPRNG (256 bits), URL-safe base64 encoded to
43 characters. Only its SHA-256 digest is stored. A slow password hash is unnecessary:
with 256 bits of entropy there is nothing to brute-force, so a leaked digest is useless.
"""

from __future__ import annotations

import hashlib
import re
import secrets

TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def generate_secret() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def is_well_formed(secret: str) -> bool:
    """Cheap syntactic check before touching the database."""
    return bool(_TOKEN_PATTERN.fullmatch(secret))
