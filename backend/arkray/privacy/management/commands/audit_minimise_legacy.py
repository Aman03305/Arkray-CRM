"""Move the personal details of audit events written before AuditDetail existed into
expiring details (docs/privacy.md#audit-trail, docs/runbooks.md#minimise-legacy-audit-details).

    manage.py audit_minimise_legacy --by admin@example.com           what would move
    manage.py audit_minimise_legacy --by admin@example.com --yes     move it (once)

Run with the schema owner's database credentials (the migrate job's): it updates the
append-only audit trail with its trigger disabled for each statement, in one transaction,
and records `audit.legacy_minimised` (counts and a digest of every value moved). Idempotent.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.audit import retention
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability


class Command(BaseCommand):
    help = "Move legacy audit details into expiring details (a dry run unless --yes)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--by", required=True, help="the administrator's email")
        parser.add_argument("--yes", action="store_true", help="move them (otherwise a dry run)")

    def handle(self, *args: Any, by: str, yes: bool, **options: Any) -> None:
        operator = User.objects.filter(email=normalize_email(by)).first()
        if operator is None or not has_capability(operator, Capability.AUDIT_VIEW):
            raise CommandError(f"--by must name an active administrator ({by!r} isn't one).")
        found = retention.preview_legacy()
        self.stdout.write(
            f"{found.events} legacy events: {found.addresses} client addresses and"
            f" {found.values} personal values to move into expiring details."
        )
        if not yes:
            self.stdout.write("Dry run: add --yes to move them.")
            return
        try:
            done = retention.minimise_legacy(operator_id=operator.pk)
        except retention.OwnerRequired as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(
            f"Moved {done.addresses} addresses and {done.values} values from {done.events}"
            f" events (digest {done.digest[:16]}...). Audited as audit.legacy_minimised."
        )
