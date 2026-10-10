"""Place, release and list legal holds (docs/privacy.md#legal-holds; core.models.LegalHold).

    manage.py legal_hold place   --lead <id> --reference CASE-123 --by admin@example.com
    manage.py legal_hold place   --user <id> --reference CASE-123 --by admin@example.com
    manage.py legal_hold release --lead <id> --by admin@example.com
    manage.py legal_hold list

While a hold is active nothing about that lead or user is erased, pseudonymised, purged or
expired. The reference names the matter (a ticket or case number), never describes it.
Placing and releasing are audited (privacy.legal_hold_placed / _released).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import IntegrityError, transaction
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability
from arkray.leads.models import Lead

REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{0,63}")
REFERENCE_RULE = (
    "--reference must be a case or ticket reference (letters, digits and . _ / # -, at most"
    " 64), never a description."
)


class Command(BaseCommand):
    help = "Place, release or list legal holds."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", choices=["place", "release", "list"])
        parser.add_argument("--lead", type=UUID)
        parser.add_argument("--user", type=UUID)
        parser.add_argument("--reference", default="")
        parser.add_argument("--by", default="", help="the administrator's email")

    def handle(self, *args: Any, action: str, **options: Any) -> None:
        if action == "list":
            for hold in LegalHold.objects.filter(released_at__isnull=True).order_by("placed_at"):
                self.stdout.write(
                    f"{hold.subject_type} {hold.subject_id} {hold.reference}"
                    f" (since {hold.placed_at:%Y-%m-%d})"
                )
            return
        operator = User.objects.filter(email=normalize_email(options["by"])).first()
        if operator is None or not has_capability(operator, Capability.CRM_MANAGE_ANY):
            raise CommandError("--by must name an active administrator.")
        lead_id, user_id = options["lead"], options["user"]
        if bool(lead_id) == bool(user_id):
            raise CommandError("Name exactly one --lead or --user.")
        subject_type = HoldSubject.LEAD if lead_id else HoldSubject.USER
        subject_id = lead_id or user_id
        model = Lead if lead_id else User
        if not model.objects.filter(pk=subject_id).exists():
            raise CommandError(f"No {subject_type} {subject_id}.")
        reference = options["reference"].strip()
        with transaction.atomic():
            if action == "place":
                if not REFERENCE.fullmatch(reference):
                    raise CommandError(REFERENCE_RULE)
                try:
                    with transaction.atomic():
                        LegalHold.objects.create(
                            subject_type=subject_type,
                            subject_id=subject_id,
                            reference=reference,
                            placed_by=operator.pk,
                        )
                except IntegrityError:
                    raise CommandError("There is already an active hold on it.") from None
                verb = "placed"
            else:
                released = LegalHold.objects.filter(
                    subject_type=subject_type, subject_id=subject_id, released_at__isnull=True
                ).update(released_at=timezone.now(), released_by=operator.pk)
                if not released:
                    raise CommandError("There is no active hold on it.")
                verb = "released"
            audit.record(
                f"privacy.legal_hold_{verb}",
                actor_id=operator.pk,
                target_type=str(subject_type),
                target_id=subject_id,
                metadata={"reference": reference} if verb == "placed" else {},
            )
        self.stdout.write(f"Legal hold {verb}: {subject_type} {subject_id}.")
