"""Domain errors.

Services raise these; the HTTP layer maps them to the standard error envelope
(arkray.core.exceptions). Domain code never imports DRF exceptions.
"""

from __future__ import annotations

from typing import Any

from .context import current_correlation_id


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """The one error envelope every API error uses."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": current_correlation_id(),
        }
    }


class DomainError(Exception):
    code = "error"
    http_status = 400
    default_message = "The request could not be completed."

    def __init__(self, message: str | None = None, *, details: Any = None) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class InvalidInputError(DomainError):
    """Input that is well-formed JSON but invalid; `details` maps fields to messages."""

    code = "validation_error"
    http_status = 400
    default_message = "Some fields are invalid."


class NotFoundError(DomainError):
    """Also used for records outside the caller's scope, so existence is never revealed."""

    code = "not_found"
    http_status = 404
    default_message = "Not found."


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    http_status = 403
    default_message = "You do not have permission to perform this action."


class ConflictError(DomainError):
    """Optimistic-concurrency or uniqueness conflict; the client should reload and retry."""

    code = "conflict"
    http_status = 409
    default_message = "The record was changed by someone else. Reload and try again."


class BusinessRuleViolation(DomainError):
    code = "business_rule_violation"
    http_status = 422
    default_message = "The request violates a business rule."


class RateLimitedError(DomainError):
    """Too many attempts. `retry_after` (seconds) becomes the Retry-After header."""

    code = "rate_limited"
    http_status = 429
    default_message = "Too many attempts. Please try again later."

    def __init__(
        self, message: str | None = None, *, retry_after: int, details: Any = None
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = max(1, int(retry_after))


class ServiceUnavailableError(DomainError):
    """A non-critical dependency (e.g. the AI provider) is temporarily unavailable."""

    code = "service_unavailable"
    http_status = 503
    default_message = "This feature is temporarily unavailable. Please try again shortly."


class AppendOnlyViolation(RuntimeError):
    """Raised when code attempts to modify or delete an append-only record."""
