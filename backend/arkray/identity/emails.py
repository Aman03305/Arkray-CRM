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


_MFA_NOTICES = {
    "enabled": (
        "Two-step verification was turned on",
        "Two-step verification was turned on for your account, with a new authenticator.",
    ),
    "disabled": (
        "Two-step verification was turned off",
        "Two-step verification was turned off for your account.",
    ),
    "reset": (
        "Two-step verification was reset",
        "An administrator or an operator removed the two-step verification of your account."
        " You were signed out everywhere.",
    ),
    "recovery_codes_replaced": (
        "New recovery codes",
        "New two-step verification recovery codes were created for your account; the old"
        " ones no longer work.",
    ),
    "recovery_code_used": (
        "A recovery code was used",
        "One of your two-step verification recovery codes was used on your account.",
    ),
    # Wrong codes (deep security audit M-4): a code is asked for only after the right
    # password, or in a signed-in session.
    "wrong_code": (
        "A wrong verification code was entered",
        "A wrong two-step verification code was just entered for your account. A code is"
        " asked for only after the right password, or in a signed-in session.",
    ),
    "locked": (
        "Two-step verification is locked",
        "Too many wrong two-step verification codes were entered for your account, so it is"
        " locked: no code is accepted, not even the right one. To unlock it, reset your"
        " password (Forgot password on the sign-in page) or ask your administrator.",
    ),
    # Past the cap a new password doesn't clear (R133): it took the password, and new ones
    # chosen through the emailed link, to get there.
    "locked_until_reset": (
        "Two-step verification is locked until it is reset",
        "Too many wrong two-step verification codes were entered for your account, across"
        " several password resets, so it is locked: no code is accepted, not even the right"
        " one, and resetting your password no longer unlocks it. Ask your administrator to"
        " reset your two-step verification.",
    ),
}
_MFA_ADVICE = (
    "If this was you, there is nothing to do. If it wasn't, change your password now "
    "and tell your administrator.\n"
)
# Someone else probably knows the password: it is what to replace.
_MFA_FAILURE_ADVICE = (
    "If this wasn't you, someone else probably knows your password: reset it now (Forgot "
    "password on the sign-in page) and tell your administrator.\n"
)
# Someone else probably has the password and could use the emailed reset links too: the
# mailbox comes first.
_MFA_RESET_LOCK_ADVICE = (
    "If this wasn't you, someone else probably knows your password and can read your email: "
    "secure your email account first (change its password), then tell your administrator.\n"
)
_ADVICE_BY_CHANGE = {
    "wrong_code": _MFA_FAILURE_ADVICE,
    "locked": _MFA_FAILURE_ADVICE,
    "locked_until_reset": _MFA_RESET_LOCK_ADVICE,
}


def send_mfa_notice(*, to: str, first_name: str, change: str) -> None:
    """Tell the owner their second factor changed, or that wrong codes were entered for it
    (ADR-0033). Never a code or a secret."""
    subject, what = _MFA_NOTICES[change]
    advice = _ADVICE_BY_CHANGE.get(change, _MFA_ADVICE)
    _send(to, f"{PRODUCT_NAME}: {subject}", f"Hello {first_name},\n\n{what}\n\n{advice}")


def send_sign_in_locked(*, to: str, first_name: str, link: str, expires_at: datetime) -> None:
    """The daily failure cap locked the account's sign-in from new browsers (adversarial
    review of 73ff1e3, L1): the owner is told, with the way back. Never a password."""
    _send(
        to,
        f"{PRODUCT_NAME}: too many failed sign-ins to your account",
        f"Hello {first_name},\n\n"
        f"There were many failed sign-in attempts to your {PRODUCT_NAME} account, so signing "
        "in to it from a browser it hasn't been used in before is locked for now. Browsers "
        "you've signed in from before still work.\n\n"
        f"To lift the lock (and trust the browser you open it in), use this link:\n{link}\n\n"
        f"This link can be used once and expires on {_business_time(expires_at)}. Resetting "
        "your password (Forgot password on the sign-in page) lifts the lock too.\n\n"
        "If these attempts weren't you, someone may be guessing your password: choose a "
        "strong one you don't use anywhere else, and tell your administrator.\n",
    )


_ACCOUNT_NOTICES = {
    # Adversarial review of 73ff1e3, L3: an administrator's change was silent.
    "password_set_by_admin": (
        "An administrator set a new password for your account",
        "An administrator set a new, temporary password for your account; you were signed out"
        " everywhere, and you'll choose your own the next time you sign in.",
    ),
    "role_changed": (
        "Your role was changed",
        "An administrator changed your role to {role}. You were signed out everywhere.",
    ),
}


def send_account_notice(
    *, to: str, first_name: str, change: str, role_label: str | None = None
) -> None:
    """Tell the account's own address about a change an administrator made to it. Never a
    password."""
    subject, what = _ACCOUNT_NOTICES[change]
    _send(
        to,
        f"{PRODUCT_NAME}: {subject}",
        f"Hello {first_name},\n\n{what.format(role=role_label or 'another role')}\n\n"
        "If you didn't expect this, tell your administrator, or another one, now.\n",
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
