"""Password policy (docs/authorization.md#password-policy).

Length and known-bad-password checks rather than composition rules ("one digit, one
symbol"), following NIST SP 800-63B: at least 12 characters, at most 128, not a common
password, not all digits, not similar to the user's name or email, and not built around
the product's own name. Configured in settings.AUTH_PASSWORD_VALIDATORS.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from arkray.core.errors import InvalidInputError

# Words an attacker would try first against this particular system.
CONTEXT_WORDS = ("arkray",)


class MaximumLengthValidator:
    """Bounds hashing work per request; 128 characters comfortably fits any passphrase."""

    def __init__(self, max_length: int = 128) -> None:
        self.max_length = max_length

    def validate(self, password: str, user: Any = None) -> None:
        if len(password) > self.max_length:
            raise ValidationError(
                f"This password is too long. It must contain at most {self.max_length} characters.",
                code="password_too_long",
            )

    def get_help_text(self) -> str:
        return f"Your password must contain at most {self.max_length} characters."


class ContextSpecificWordsValidator:
    def validate(self, password: str, user: Any = None) -> None:
        lowered = password.lower()
        if any(word in lowered for word in CONTEXT_WORDS):
            raise ValidationError(
                "This password contains the name of this application.",
                code="password_context_word",
            )

    def get_help_text(self) -> str:
        return "Your password can't contain the name of this application."


def check_new_password(password: str, user: Any, *, field: str) -> None:
    """Run the configured validators; failures become a 400 with messages for `field`."""
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise InvalidInputError(
            "Choose a stronger password.", details={field: list(exc.messages)}
        ) from None
