"""Remove the names and content hashes of files deleted before they went with their objects
(docs/privacy.md#attachments, docs/runbooks.md#forget-names-of-deleted-files).

    manage.py forget_attachment_names --by admin@example.com           how many
    manage.py forget_attachment_names --by admin@example.com --yes     remove them

Only files already removed from storage (deleted, blocked or never finished), never one
under a legal hold. Audited as attachment.metadata_removed (a count). Idempotent.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.activities import attachments
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability


class Command(BaseCommand):
    help = "Forget the names and hashes of files already removed (a dry run unless --yes)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--by", required=True, help="the administrator's email")
        parser.add_argument("--yes", action="store_true", help="remove them (otherwise a dry run)")

    def handle(self, *args: Any, by: str, yes: bool, **options: Any) -> None:
        operator = User.objects.filter(email=normalize_email(by)).first()
        if operator is None or not has_capability(operator, Capability.CRM_MANAGE_ANY):
            raise CommandError(f"--by must name an active administrator ({by!r} isn't one).")
        if not yes:
            count = attachments.forget_purged(actor_id=operator.pk, dry_run=True)
            self.stdout.write(f"{count} removed files still have a name and hash. Add --yes.")
            return
        count = attachments.forget_purged(actor_id=operator.pk, dry_run=False)
        self.stdout.write(f"Forgot the names and hashes of {count} removed files.")
