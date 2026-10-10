"""Legal holds (core.models.LegalHold): what every retention, erasure and purge job asks
before it removes anything about a person (docs/privacy.md#legal-holds)."""

from __future__ import annotations

from uuid import UUID

from .errors import ConflictError
from .models import HoldSubject, LegalHold


def active() -> dict[str, set[UUID]]:
    """The held subjects by type, in one query: {"lead": {...}, "user": {...}}."""
    held: dict[str, set[UUID]] = {subject: set() for subject in HoldSubject.values}
    for subject_type, subject_id in LegalHold.objects.filter(released_at__isnull=True).values_list(
        "subject_type", "subject_id"
    ):
        held[subject_type].add(subject_id)
    return held


def held_users() -> set[UUID]:
    return active()[HoldSubject.USER]


def held_leads() -> set[UUID]:
    return active()[HoldSubject.LEAD]


def is_held(subject_type: HoldSubject, subject_id: UUID) -> bool:
    return LegalHold.objects.filter(
        subject_type=subject_type, subject_id=subject_id, released_at__isnull=True
    ).exists()


class UnderLegalHold(ConflictError):
    """The operation would remove or change data under an active legal hold (409 through
    the API)."""

    code = "legal_hold"

    def __init__(self, subject_type: str, subject_id: UUID) -> None:
        super().__init__(
            f"The {subject_type} {subject_id} is under a legal hold: release it first (an"
            " administrator, with the matter's reference)."
        )
