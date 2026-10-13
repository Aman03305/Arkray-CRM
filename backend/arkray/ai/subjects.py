"""Which Ask Arkray conversations concern a customer: shared by erasure and subject-access
exports (arkray.privacy) and by the legal-hold rule in conversation housekeeping
(ai.service.housekeeping). It lives in `ai` because `privacy` sits above it in the module
layering, and housekeeping must match a held lead's conversations exactly as an erasure
would (deep security audit M-5). `held_conversations` is the one legal-hold rule for Ask
Arkray history: housekeeping, erasure and staff pseudonymisation (privacy.staff) all keep
what it returns, and deleting a conversation (ai.service.forget_conversation) asks the same
of one conversation with `hold_keeps`.

These read every hold and every conversation, whoever's: what they find is never shown to a
user (adversarial review M1: deleting a conversation answered 409 `legal_hold` when its text
named anyone's held customer, an oracle; it now always answers the same way and keeps a held
one as a tombstone). Tombstones (`Conversation.forgotten_at`) are found like any other
conversation here: they are the holds' evidence, for exports and erasures as before."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import connection
from django.db.models import Q

from arkray.activities.models import Activity
from arkray.core import holds
from arkray.core.models import HoldSubject, LegalHold
from arkray.leads.models import PHONE_FIELDS, Lead
from arkray.pipeline.models import Opportunity

from .models import Conversation

ERASED = "[erased]"  # the placeholder an erasure writes (privacy.services)
UUID_IN_TEXT = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
MIN_TERM_LENGTH = 4  # shorter names would match unrelated conversations


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


def like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def touching_conversations(
    record_ids: set[UUID], terms: list[str], *, among: Collection[UUID] | None = None
) -> list[UUID]:
    """Conversations with a question or answer that cites any of the records (one scan of
    the ids written in each answer, however many records) or names the person. `among`:
    only these conversations are read (through the questions' conversation index), for a
    caller that needs the answer for a few of them."""
    if among is not None and not among:
        return []
    patterns = [like(term) for term in terms]
    matches = ["q.text ILIKE ANY(%s::text[])", "q.answer::text ILIKE ANY(%s::text[])"]
    params: list[Any] = [patterns, patterns]
    if record_ids:  # no ids (a staff member's name only): no scan of the answers' ids
        matches.insert(
            0,
            "EXISTS (SELECT 1 FROM regexp_matches(q.answer::text, %s, 'g') AS m(found)"
            " WHERE m.found[1] = ANY(%s::text[]))",
        )
        params[:0] = [UUID_IN_TEXT, [str(record_id) for record_id in record_ids]]
    # Only fixed clauses are joined: every value is a bound parameter.
    sql = f"SELECT DISTINCT q.conversation_id FROM ai_question q WHERE ({' OR '.join(matches)})"  # noqa: S608
    if among is not None:
        sql += " AND q.conversation_id = ANY(%s::uuid[])"
        params.append([str(conversation_id) for conversation_id in among])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [row[0] for row in cursor.fetchall()]


def _escaped(match: re.Match[str]) -> str:
    return "\\" + match.group(1)


def word_pattern(term: str) -> str:
    """A PostgreSQL regular expression matching `term` as whole words, case ignored with
    `~*`, any run of spaces between its words: "Anna Lee" matches "anna  lee," but never
    "Hanna Leeds" (review P3-3). Every character that isn't a letter, digit or space is
    escaped (in PostgreSQL's ARE, a backslash before one makes it literal)."""
    words = [re.sub(r"([^\w\s])", _escaped, word) for word in term.split()]
    return r"(^|[^[:alnum:]_])" + r"\s+".join(words) + r"([^[:alnum:]_]|$)"


def naming_conversations(terms: list[str], *, among: Collection[UUID] | None = None) -> list[UUID]:
    """Conversations with a question or answer that names one of `terms` as whole words (a
    staff member's name or email: privacy.staff)."""
    if not terms or (among is not None and not among):
        return []
    patterns = [word_pattern(term) for term in terms]
    sql = (
        "SELECT DISTINCT q.conversation_id FROM ai_question q"
        " WHERE (q.text ~* ANY(%s::text[]) OR q.answer::text ~* ANY(%s::text[]))"
    )
    params: list[Any] = [patterns, patterns]
    if among is not None:
        sql += " AND q.conversation_id = ANY(%s::uuid[])"
        params.append([str(conversation_id) for conversation_id in among])
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [row[0] for row in cursor.fetchall()]


# --- legal holds (docs/privacy.md#legal-holds) -----------------------------------------------
def lead_records(lead_id: UUID) -> set[UUID]:
    """The lead and every deal and activity of it: what an answer citing the customer cites
    (as an erasure collects them, privacy.services)."""
    opportunity_ids = list(Opportunity.objects.filter(lead_id=lead_id).values_list("pk", flat=True))
    activity_ids = Activity.objects.filter(
        Q(lead_id=lead_id) | Q(opportunity_id__in=opportunity_ids)
    ).values_list("pk", flat=True)
    return {lead_id, *opportunity_ids, *activity_ids}


@dataclass(frozen=True, slots=True)
class HeldCustomers:
    """The customers under a legal hold now, ready to match conversations against: their
    records and identifying terms are read once, however many batches are matched."""

    hold_ids: frozenset[UUID]  # the active holds' own ids: a hold placed again is new
    records: frozenset[UUID]
    terms: tuple[str, ...]

    @classmethod
    def now(cls) -> HeldCustomers:
        active = dict(
            LegalHold.objects.filter(
                subject_type=HoldSubject.LEAD, released_at__isnull=True
            ).values_list("pk", "subject_id")
        )
        records: set[UUID] = set()
        terms: set[str] = set()
        for lead in Lead.objects.filter(pk__in=set(active.values())):
            records |= lead_records(lead.pk)
            terms.update(identifying_terms(lead))
        return cls(frozenset(active), frozenset(records), tuple(sorted(terms)))

    def __bool__(self) -> bool:
        return bool(self.hold_ids)

    def touching(self, among: Collection[UUID]) -> set[UUID]:
        """Which of `among` touch a held customer (one scan of their questions)."""
        if not self or not among:
            return set()
        return set(touching_conversations(set(self.records), list(self.terms), among=among))


def user_held(conversations: Any, users: Collection[UUID]) -> Any:
    """`conversations` (a Conversation queryset) without those a user hold keeps: asked by a
    held user or in their workspace. Plain SQL on the conversation's own columns, no scan."""
    if not users:
        return conversations
    users = list(users)
    return conversations.exclude(actor_id__in=users).exclude(subject_id__in=users)


def held_conversations(among: Collection[UUID] | None = None) -> set[UUID]:
    """The conversations a legal hold keeps (deep security audit M-5): every conversation
    asked by a held user or in their workspace, and every conversation that touches a held
    customer, matched exactly as an erasure of that customer would match it (its records
    cited, or the person named). `among`: only these conversations are considered.

    Bounded: holds are rare; the user holds are a lookup on the conversations' own columns,
    and only the conversations they don't keep are matched against the held customers, all
    of them in one scan of those conversations' questions (review P2-3: a held user's whole
    history was matched again on every call).

    A caller that deletes what this doesn't return calls it in the deleting transaction,
    after core.holds.barrier(): a hold being placed meanwhile has then either committed
    before this reads the holds, or waits for the deletion to commit (R134)."""
    users = holds.held_users()
    candidates = Conversation.objects.all()
    if among is not None:
        candidates = candidates.filter(pk__in=list(among))
    kept: set[UUID] = set()
    if users:
        kept = set(candidates.values_list("pk", flat=True)) - set(
            user_held(candidates, users).values_list("pk", flat=True)
        )
    customers = HeldCustomers.now()
    if not customers:
        return kept
    if among is not None:
        return kept | customers.touching(set(among) - kept)
    if users:
        return kept | customers.touching(
            set(user_held(candidates, users).values_list("pk", flat=True))
        )
    return set(touching_conversations(set(customers.records), list(customers.terms)))


def hold_keeps(conversation: Conversation) -> bool:
    """Whether a legal hold keeps this conversation: `held_conversations(among=[it])`, the
    same rule (its asker or its workspace's user held, or a held customer it touches), for
    deleting it (ai.service.forget_conversation).

    With the same work whatever the answer (adversarial review M1): both halves are always
    evaluated, so a deletion takes no shorter path when its asker is held (held_conversations
    skips matching what a user hold already keeps) and no longer one when they aren't. What
    varies is only what every caller shares: how many holds there are.

    Called in the deleting transaction after core.holds.barrier() (R134), like
    held_conversations."""
    users = holds.held_users()
    by_user = bool(users & {conversation.actor_id, conversation.subject_id})
    by_customer = bool(HeldCustomers.now().touching([conversation.pk]))
    return by_user or by_customer
