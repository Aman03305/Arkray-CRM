"""Erase a lead's personal data on a verified request (docs/privacy.md#erasure).

    manage.py erase_lead <lead id> --by admin@example.com           what would be erased
    manage.py erase_lead <lead id> --by admin@example.com --yes     erase it

Run with the schema owner's database credentials (the migrate job's) when the lead's
opportunities have lost reasons in the stage history; otherwise the application's are
enough. `--by` names the administrator who verified the request: the audit trail records
them. Irreversible, except from a backup.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability
from arkray.leads.models import Lead
from arkray.privacy import services


class Command(BaseCommand):
    help = "Erase a lead's personal data (a dry run unless --yes)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("lead_id", type=UUID)
        parser.add_argument("--by", required=True, help="the administrator's email")
        parser.add_argument("--yes", action="store_true", help="erase (otherwise a dry run)")

    def handle(self, *args: Any, lead_id: UUID, by: str, yes: bool, **options: Any) -> None:
        # The same email normalisation and capability policy as sign-in and the API.
        operator = User.objects.filter(email=normalize_email(by)).first()
        if operator is None or not has_capability(operator, Capability.CRM_MANAGE_ANY):
            raise CommandError(f"--by must name an active administrator ({by!r} isn't one).")
        if not Lead.objects.filter(pk=lead_id).exists():
            raise CommandError(f"No lead {lead_id}.")
        try:
            found = services.preview(lead_id)
        except services.AlreadyErased as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(
            f"Lead {lead_id}: {found.activities} activities, {found.opportunities} opportunities,"
            f" {found.history_rows} history rows with free text (lost reasons, agreed CPTs),"
            f" {found.chunks} index chunks, {found.conversations} Ask Arkray conversations."
        )
        if not yes:
            self.stdout.write("Dry run: add --yes to erase (irreversible).")
            return
        try:
            done = services.erase(lead_id, operator_id=operator.pk)
        except (services.OwnerRequired, services.AlreadyErased) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(
            f"Erased lead {lead_id}: {done.activities} activities, {done.opportunities}"
            f" opportunities, {done.history_rows} history rows, {done.chunks} chunks deleted,"
            f" {done.conversations} conversations deleted. Audited as lead.erased."
        )
