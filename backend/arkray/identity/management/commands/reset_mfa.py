"""Remove an account's second factor, for an operator with shell access (ADR-0033).

    manage.py reset_mfa --email someone@example.com --by admin@example.com --reason "ticket 123"

The one way to reset an administrator's two-step verification: no administrator can reset
another's through the API (ADR-0030). Their sessions end; they sign in with their password
and, if required for their role, set up a new authenticator at once. Audited as `mfa.reset`
(by "operator", with the `--by` administrator as the actor; the reason kept as an expiring
detail), and the account's own address is told. Confirm the person's identity out of
band first (docs/runbooks.md#lost-second-factor).

`--by` names a super administrator (users.manage): a team administrator administers only
their own team's users, and never an administrator's second factor (ADR-0035).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.core.errors import DomainError
from arkray.core.text import TextRejected, clean_line
from arkray.identity import mfa_services
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability

REASON_MAX_LENGTH = 200


class Command(BaseCommand):
    help = "Remove an account's second factor (two-step verification). Audited."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--email", required=True, help="the account's sign-in email")
        parser.add_argument("--reason", required=True, help="why (kept in the audit detail)")
        parser.add_argument(
            "--by", required=True, help="the super admin on whose behalf this is done"
        )

    def handle(self, *args: Any, email: str, reason: str, by: str, **options: Any) -> None:
        operator = User.objects.filter(email=normalize_email(by)).first()
        if operator is None or not has_capability(operator, Capability.USERS_MANAGE):
            raise CommandError(f"--by must name an active super admin ({by!r} isn't one).")
        try:
            text = clean_line(reason)
        except TextRejected as exc:
            raise CommandError(f"--reason: {exc}") from None
        if not text or len(text) > REASON_MAX_LENGTH:
            raise CommandError(f"--reason must say why, in at most {REASON_MAX_LENGTH} characters.")
        try:
            removed = mfa_services.operator_reset(email, text, operator_id=operator.pk)
        except DomainError as exc:
            raise CommandError(str(exc)) from None
        if not removed:
            self.stdout.write("That account has no second factor: nothing changed.")
            return
        self.stdout.write(
            "Second factor removed; the account's sessions ended. Audited as mfa.reset."
        )
