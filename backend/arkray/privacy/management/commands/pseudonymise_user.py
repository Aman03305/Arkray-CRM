"""Pseudonymise a former staff member (docs/privacy.md#staff; arkray.privacy.staff).

    manage.py pseudonymise_user <user id> --by admin@example.com           what would change
    manage.py pseudonymise_user <user id> --by admin@example.com --yes     do it (irreversible)

Only a deactivated account that owns no current work, not under a legal hold. Audited as
user.pseudonymised and recorded in the erasure ledger.

An administrator's account (deactivated, or a former administrator whose role is still
administrator) is refused unless `--allow-administrator` is given as well: the API never
pseudonymises one (ADR-0030, SEC-2), so this command, run by an operator with shell and
database access, is the one higher-trust path. Its audit event says `administrator_target`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.core.errors import DomainError
from arkray.core.holds import UnderLegalHold
from arkray.core.ledger import LedgerError
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability
from arkray.privacy import staff


class Command(BaseCommand):
    help = "Pseudonymise a deactivated user (a dry run unless --yes)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("user_id", type=UUID)
        parser.add_argument("--by", required=True, help="the administrator's email")
        parser.add_argument("--yes", action="store_true", help="do it (otherwise a dry run)")
        parser.add_argument(
            "--allow-administrator",
            action="store_true",
            help="the account is an administrator's (refused without it; ADR-0030)",
        )

    def handle(
        self,
        *args: Any,
        user_id: UUID,
        by: str,
        yes: bool,
        allow_administrator: bool = False,
        **options: Any,
    ) -> None:
        operator = User.objects.filter(email=normalize_email(by)).first()
        if operator is None or not has_capability(operator, Capability.USERS_MANAGE):
            raise CommandError(f"--by must name an active administrator ({by!r} isn't one).")
        try:
            found = staff.preview(
                user_id, operator_id=operator.pk, allow_administrator=allow_administrator
            )
            counts = {k: v for k, v in asdict(found).items() if k != "user_id"}
            self.stdout.write(f"User {user_id}: {counts}.")
            if not yes:
                self.stdout.write("Dry run: add --yes to pseudonymise (irreversible).")
                return
            staff.pseudonymise(
                user_id, operator_id=operator.pk, allow_administrator=allow_administrator
            )
        except (staff.AlreadyPseudonymised, UnderLegalHold, DomainError) as exc:
            raise CommandError(str(exc) or type(exc).__name__) from None
        except LedgerError as exc:
            raise CommandError(f"Nothing changed: the erasure ledger failed ({exc}).") from None
        self.stdout.write(f"Pseudonymised user {user_id}. Audited as user.pseudonymised.")
