"""Email addresses as login identities, and the transactional emails Arkray sends.

Canonicalisation policy (docs/authorization.md#email-canonicalisation): surrounding
whitespace is removed and the whole address is lower-cased (identity.models
.normalize_email); only ASCII addresses are accepted, so visually identical Unicode
look-alikes (an "admin@..." spelled with a Cyrillic "a") can never become separate
identities; nothing provider-specific (Gmail dots, +tags) is stripped.

Emails are plain text: nothing to escape, nothing to render remotely, and links are the
only active content.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage

PRODUCT_NAME = "Arkray CRM"


def validate_ascii_email(value: str) -> None:
    if not value.isascii():
        raise ValidationError(
            "Use an email address with ASCII characters only.", code="non_ascii_email"
        )


def mask_email(email: str) -> str:
    """r***@example.com: enough to recognise an address without disclosing it."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _business_time(moment: datetime) -> str:
    local = moment.astimezone(ZoneInfo(settings.CRM_TIME_ZONE))
    return local.strftime("%d %b %Y, %H:%M %Z")


def account_link(path: str, secret: str) -> str:
    return f"{settings.APP_BASE_URL}/{path}/{secret}"


def _send(to: str, subject: str, body: str) -> None:
    # Raises on failure: the outbox retries with backoff (the single retry layer).
    EmailMessage(subject=subject, body=body, to=[to]).send(fail_silently=False)


def send_invitation(
    *, to: str, first_name: str, inviter_name: str | None, link: str, expires_at: datetime
) -> None:
    inviter = f"{inviter_name} has" if inviter_name else "You have been"
    invited = "invited you" if inviter_name else "invited"
    _send(
        to,
        f"You're invited to {PRODUCT_NAME}",
        f"Hello {first_name},\n\n"
        f"{inviter} {invited} to {PRODUCT_NAME}.\n\n"
        f"Set your password to activate your account:\n{link}\n\n"
        f"This link can be used once and expires on {_business_time(expires_at)}.\n"
        "If you weren't expecting this invitation, you can ignore this email.\n",
    )


def send_password_reset(*, to: str, first_name: str, link: str, expires_at: datetime) -> None:
    _send(
        to,
        f"Reset your {PRODUCT_NAME} password",
        f"Hello {first_name},\n\n"
        f"We received a request to reset the password for your {PRODUCT_NAME} account.\n\n"
        f"Choose a new password here:\n{link}\n\n"
        f"This link can be used once and expires on {_business_time(expires_at)}.\n"
        "If you didn't ask to reset your password, you can ignore this email: your "
        "password has not been changed.\n",
    )


def send_email_changed_notice(*, to: str, first_name: str, new_email: str) -> None:
    _send(
        to,
        f"Your {PRODUCT_NAME} sign-in email was changed",
        f"Hello {first_name},\n\n"
        f"An administrator changed the email address you use to sign in to {PRODUCT_NAME} "
        f"to {mask_email(new_email)}. You have been signed out everywhere.\n\n"
        "If you did not expect this change, contact your administrator.\n",
    )
