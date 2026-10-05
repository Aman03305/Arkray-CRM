"""Erasing a person's data on request (docs/privacy.md#erasure).

A lead is a person (or the person behind an organisation). On a verified erasure request an
administrator runs `manage.py erase_lead`, which in one transaction:

- deletes every Ask Arkray conversation that touches the person: one whose questions or
  answers cite any of the records below, or name the person (their name, email or a phone
  number; the organisation's name only for a lead that is just an organisation, since it
  would match every colleague's conversations), in a question's text or an answer. Whole
  conversations, because a follow-up answer can be written from the earlier turns alone
  (Phase 11 review). Answers are serialised with it (`ai.service.hold_erasure_lock`): one
  being stored finishes before the sweep, and one read before the erasure committed is
  discarded instead of stored (whole-software audit);
- clears every personal field of the lead (names, organisation, job title, email, phones,
  address, description; the derived search text and phone keys follow), names it `[erased]`
  and archives it (a `lead.archived` timeline entry, like any archive), so figures, history
  and the audit trail keep their shape;
- redacts the text of its activities (notes, task and meeting titles and descriptions,
  meeting places and links) and of its opportunities (titles, descriptions, lost reasons,
  account and customer names, contact phone and email, address, custom field values);
- deletes the files attached to its notes (names blanked, objects removed from storage by
  the purge job and, failing that, the hourly housekeeping);
- deletes the Ask Arkray index chunks of those records, and queues their re-indexing from
  the redacted text (an indexing job that read the old text concurrently is overwritten);
- records `lead.erased` in the audit trail: the operator, the lead's id and counts, never
  any of the erased values;
- last, redacts the lost reasons kept in the append-only stage history, the one free-text
  column it holds, when there are any: the trigger is disabled for that single statement,
  inside the transaction, which needs the schema owner's credentials; the lock this takes
  holds stage moves for the moment between it and the commit.

What stays: ids, dates, statuses, stage names, amounts and the audit trail, none of which
identifies the person once the text is gone. What it can't find: the person named in other
leads' notes. Backups keep the old data until they expire (docs/privacy.md#retention).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from arkray.activities import attachments
from arkray.activities.models import Activity, ActivityType, Attachment
from arkray.ai import indexing
from arkray.ai import service as ai_service
from arkray.ai.models import Conversation, KnowledgeChunk
from arkray.audit import services as audit
from arkray.core.domain_events import publish
from arkray.leads.events import LeadArchived
from arkray.leads.models import PHONE_FIELDS, Lead
from arkray.pipeline.models import Opportunity

ERASED = "[erased]"
LEAD_TEXT_FIELDS = (
    "last_name",
    "organization_name",
    "job_title",
    "email",
    *PHONE_FIELDS,
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "postal_code",
    "country",
    "description",
)
HISTORY_TABLE = "pipeline_stage_history"
HISTORY_TRIGGER = f"{HISTORY_TABLE}_append_only"
# An operator's command over every stored answer: not a web request's 10 s.
STATEMENT_TIMEOUT = "120s"
UUID_IN_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MIN_TERM_LENGTH = 4  # shorter names would match unrelated conversations


class OwnerRequired(Exception):
    """The lead has lost reasons in the append-only history: only the schema owner may
    redact them."""


class AlreadyErased(Exception):
    """Nothing of the person is left to erase. (Its placeholder name would otherwise be the
    search term, and match every other erased lead's conversations: audit P3.)"""

    def __init__(self, lead_id: UUID) -> None:
        super().__init__(f"Lead {lead_id} has already been erased.")


def _already_erased(lead: Lead) -> bool:
    return lead.first_name == ERASED and not any(getattr(lead, f) for f in LEAD_TEXT_FIELDS)


@dataclass(frozen=True, slots=True)
class Erasure:
    lead_id: UUID
    activities: int
    opportunities: int
    history_rows: int
    chunks: int
    conversations: int
    attachments: int = 0


def _owns_history() -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_has_role(current_user, relowner, 'MEMBER') FROM pg_class"
            " WHERE oid = %s::regclass",
            [HISTORY_TABLE],
        )
        return bool(cursor.fetchone()[0])


def _history_with_reasons(opportunity_ids: list[UUID]) -> int:
    if not opportunity_ids:
        return 0
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT count(*) FROM {HISTORY_TABLE}"  # noqa: S608 — a code constant
            " WHERE opportunity_id = ANY(%s) AND lost_reason <> ''",
            [opportunity_ids],
        )
        return int(cursor.fetchone()[0])


def _redact_history(opportunity_ids: list[UUID]) -> int:
    with connection.cursor() as cursor:
        # ALTER TABLE refuses a table with deferred constraint checks still queued in this
        # transaction: run them now (they would run at commit anyway).
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        cursor.execute(f"ALTER TABLE {HISTORY_TABLE} DISABLE TRIGGER {HISTORY_TRIGGER}")
        cursor.execute(
            f"UPDATE {HISTORY_TABLE} SET lost_reason = ''"  # noqa: S608 — a code constant
            " WHERE opportunity_id = ANY(%s) AND lost_reason <> ''",
            [opportunity_ids],
        )
        redacted = int(cursor.rowcount)
        cursor.execute(f"ALTER TABLE {HISTORY_TABLE} ENABLE TRIGGER {HISTORY_TRIGGER}")
    return redacted


def identifying_terms(lead: Lead) -> list[str]:
    """What names the person in free text: their name, email and phone numbers as stored
    and as digits (case is ignored when matching). The organisation's name only when the
    lead is just an organisation: for a person, it names their colleagues too, and would
    delete every user's conversations about the company (whole-software audit)."""
    names = [
        f"{lead.first_name} {lead.last_name}".strip(),
        lead.email,
        *(getattr(lead, field) for field in PHONE_FIELDS),
        *lead.phone_keys,
    ]
    if not (lead.first_name or lead.last_name):
        names.append(lead.organization_name)
    if not lead.last_name:  # a one-word name only when it is the whole name
        names.append(lead.first_name)
    terms = {term.strip() for term in names if len(term.strip()) >= MIN_TERM_LENGTH}
    return sorted(terms - {ERASED})


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _touching_conversations(record_ids: set[UUID], terms: list[str]) -> list[UUID]:
    """Conversations with a question or answer that cites any of the records (one scan of
    the ids written in each answer, however many records) or names the person."""
    patterns = [_like(term) for term in terms]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT q.conversation_id FROM ai_question q WHERE EXISTS ("
            "  SELECT 1 FROM regexp_matches(q.answer::text, %s, 'g') AS m(found)"
            "  WHERE m.found[1] = ANY(%s::text[]))"
            " OR q.text ILIKE ANY(%s::text[]) OR q.answer::text ILIKE ANY(%s::text[])",
            [UUID_IN_TEXT, [str(record_id) for record_id in record_ids], patterns, patterns],
        )
        return [row[0] for row in cursor.fetchall()]


def _records(lead_id: UUID) -> tuple[list[UUID], list[UUID]]:
    opportunity_ids = list(Opportunity.objects.filter(lead_id=lead_id).values_list("pk", flat=True))
    activity_ids = list(
        Activity.objects.filter(
            Q(lead_id=lead_id) | Q(opportunity_id__in=opportunity_ids)
        ).values_list("pk", flat=True)
    )
    return opportunity_ids, activity_ids


@transaction.atomic
def preview(lead_id: UUID) -> Erasure:
    """What `erase()` would touch, without changing anything."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
    lead = Lead.objects.get(pk=lead_id)
    if _already_erased(lead):
        raise AlreadyErased(lead_id)
    opportunity_ids, activity_ids = _records(lead_id)
    records = {lead_id, *opportunity_ids, *activity_ids}
    return Erasure(
        lead_id=lead_id,
        activities=len(activity_ids),
        opportunities=len(opportunity_ids),
        history_rows=_history_with_reasons(opportunity_ids),
        chunks=KnowledgeChunk.objects.filter(Q(lead_id=lead_id) | Q(source_id__in=records)).count(),
        conversations=len(_touching_conversations(records, identifying_terms(lead))),
        attachments=Attachment.objects.filter(
            note_id__in=activity_ids, purged_at__isnull=True
        ).count(),
    )


@transaction.atomic
def erase(lead_id: UUID, *, operator_id: UUID) -> Erasure:
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
    # First, before any lock a CRM writer could be waiting behind: answers being stored
    # finish, and none is stored until this commits (see ai.service).
    ai_service.hold_erasure_lock()
    lead = Lead.objects.select_for_update().get(pk=lead_id)
    if _already_erased(lead):
        raise AlreadyErased(lead_id)
    opportunity_ids = list(
        Opportunity.objects.select_for_update().filter(lead_id=lead_id).values_list("pk", flat=True)
    )
    activity_ids = list(
        Activity.objects.select_for_update()
        .filter(Q(lead_id=lead_id) | Q(opportunity_id__in=opportunity_ids))
        .values_list("pk", flat=True)
    )
    history_rows = _history_with_reasons(opportunity_ids)
    if history_rows and not _owns_history():
        raise OwnerRequired(
            "This lead's stage history holds lost reasons, which only the schema owner may"
            " redact: run the command with the owner's database credentials."
        )
    now = timezone.now()
    records = {lead_id, *opportunity_ids, *activity_ids}

    # Ask Arkray first, while the person's identifiers are still known. The conversations
    # are locked before the delete collects their questions: one being added meanwhile (its
    # writer holds the conversation's lock) is then committed and collected too, instead of
    # failing the whole erasure at commit (audit P3).
    conversation_ids = list(
        Conversation.objects.select_for_update()
        .filter(pk__in=_touching_conversations(records, identifying_terms(lead)))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    _, deleted = Conversation.objects.filter(pk__in=conversation_ids).delete()
    conversations = deleted.get(Conversation._meta.label, 0)

    bump = {"version": F("version") + 1, "updated_at": now}
    Activity.objects.filter(pk__in=activity_ids, type=ActivityType.NOTE).update(
        title="", description=ERASED, **bump
    )
    Activity.objects.filter(pk__in=activity_ids).exclude(type=ActivityType.NOTE).update(
        title=ERASED, description="", location="", meeting_url="", **bump
    )
    Opportunity.objects.filter(pk__in=opportunity_ids).update(
        title=ERASED,
        description="",
        lost_reason="",
        account_name=ERASED,
        customer_name=ERASED,
        contact_phone="",
        contact_email="",
        address="",
        custom_fields={},
        **bump,
    )
    note_ids = list(
        Activity.objects.filter(pk__in=activity_ids, type=ActivityType.NOTE).values_list(
            "pk", flat=True
        )
    )
    files = attachments.erase_for_notes(note_ids, now)
    for field in LEAD_TEXT_FIELDS:
        setattr(lead, field, "")
    lead.first_name = ERASED
    archives = lead.archived_at is None
    lead.archived_at = lead.archived_at or now
    lead.version += 1
    lead.save()  # keeps phone_keys in step; display_name and search_text are generated
    if archives:  # the timeline records it like any archive (audit P3)
        publish(
            LeadArchived(
                lead_id=lead.pk, owner_id=lead.owner_id, actor_id=operator_id, occurred_at=now
            )
        )

    chunks, _ = KnowledgeChunk.objects.filter(
        Q(lead_id=lead_id) | Q(source_id__in=records)
    ).delete()
    indexing.enqueue_lead(lead_id)  # after commit, from the redacted text

    result = Erasure(
        lead_id=lead_id,
        activities=len(activity_ids),
        opportunities=len(opportunity_ids),
        history_rows=history_rows,
        chunks=chunks,
        conversations=conversations,
        attachments=files,
    )
    counts = {key: value for key, value in asdict(result).items() if key != "lead_id"}
    audit.record(
        "lead.erased", actor_id=operator_id, target_type="lead", target_id=lead_id, metadata=counts
    )
    if history_rows:  # last: the lock it takes is held only until the commit
        _redact_history(opportunity_ids)
    return result
