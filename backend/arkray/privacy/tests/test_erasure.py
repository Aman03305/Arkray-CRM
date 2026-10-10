"""Erasure of a lead's personal data on request (docs/privacy.md#erasure)."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from arkray.activities.models import Activity
from arkray.ai.models import Conversation, KnowledgeChunk, Question, QuestionStatus, WorkspaceKind
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.leads.models import Lead
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Opportunity, Stage, StageHistory
from arkray.privacy import services
from tests.ai_fixtures import index, note
from tests.factories import (
    AdminFactory,
    LeadFactory,
    MeetingFactory,
    OpportunityFactory,
    TaskFactory,
    default_stage,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]

SECRETS = (
    "Rahul",
    "Sharma",
    "rahul.sharma@clinic.example",
    "9800000001",
    "Sharma Diagnostics",
    "Koregaon",
    "after 5pm",
    "periwinkle",
    "competitor Zeta",
)


def asked(
    actor: Any,
    text: str,
    answer: dict[str, Any],
    conversation: Conversation | None = None,
    status: str = QuestionStatus.ANSWERED,
) -> Question:
    conversation = conversation or Conversation.objects.create(
        actor=actor, workspace_kind=WorkspaceKind.SELF, subject=actor
    )
    now = timezone.now()
    done = status != QuestionStatus.PENDING
    return Question.objects.create(
        conversation=conversation,
        actor=actor,
        text=text,
        status=status,
        mode="retrieval" if done else "",
        answer=answer,
        finished_at=now if done else None,
        expires_at=now + timedelta(minutes=5),
    )


def answer_citing(actor: Any, record_id: Any, snippet: str = "about the periwinkle") -> Question:
    return asked(
        actor,
        "What did they ask?",
        {"citations": [{"ref": f"note:{record_id}", "snippet": snippet}]},
    )


@pytest.fixture
def person(user_a: Any) -> dict[str, Any]:
    lead = LeadFactory(
        owner=user_a,
        first_name="Rahul",
        last_name="Sharma",
        organization_name="Sharma Diagnostics",
        job_title="Lab head",
        email="rahul.sharma@clinic.example",
        phone="+91 98000 00001",
        address_line_1="12 Koregaon Park",
        city="Pune",
        description="Prefers calls after 5pm.",
    )
    written = note(user_a, lead, "Rahul asked about the periwinkle analyser's price.")
    index()
    task = TaskFactory(lead=lead, owner=user_a, title="Call Rahul", description="Koregaon lab")
    meeting = MeetingFactory(
        lead=lead,
        owner=user_a,
        title="Demo for Rahul",
        location="Koregaon office",
        meeting_url="https://meet.example/rahul",
    )
    OpportunityFactory(
        lead=lead, owner=user_a, title="Rahul's analyser", description="Sharma budget"
    )
    opportunity = Opportunity.objects.get(title="Rahul's analyser")
    stages = list(Stage.objects.filter(pipeline=opportunity.pipeline).order_by("position"))
    StageHistory.objects.create(
        opportunity=opportunity,
        from_stage=stages[0],
        to_stage=stages[-1],
        from_stage_name=stages[0].name,
        to_stage_name=stages[-1].name,
        from_status="open",
        to_status="lost",
        value=Decimal("100.00"),
        probability=Decimal("0"),
        lost_reason="Rahul chose competitor Zeta",
        actor=user_a,
    )
    citing = answer_citing(user_a, written.pk)
    return {
        "lead": lead,
        "note": written,
        "task": task,
        "meeting": meeting,
        "opportunity": opportunity,
        "answer": citing,
        # Phase 11 review, P0: a follow-up written from the earlier turns cites no record,
        # and a question can name the person without any answer citing them.
        "follow_up": asked(
            user_a,
            "And when are they free?",
            {"blocks": [{"parts": [{"text": "He prefers calls after 5pm."}]}]},
            conversation=citing.conversation,
        ),
        "named": asked(
            user_a,
            "And what is Rahul Sharma's email?",
            {"blocks": [{"parts": [{"text": "It is rahul.sharma@clinic.example."}]}]},
        ),
        "failed": asked(user_a, "Call notes for Rahul Sharma, Koregaon?", {}, status="failed"),
    }


@pytest.fixture
def bystander(user_a: Any) -> dict[str, Any]:
    lead = LeadFactory(owner=user_a, first_name="Priya", email="priya@lab.example")
    written = note(user_a, lead, "Priya wants a quote.")
    index()
    return {"lead": lead, "note": written, "answer": answer_citing(user_a, written.pk, "a quote")}


def erase(lead: Lead, admin: Any, *extra: str) -> str:
    out = StringIO()
    call_command("erase_lead", str(lead.pk), "--by", admin.email, *extra, stdout=out)
    return out.getvalue()


def dump(*rows: Any) -> str:
    return json.dumps([{k: str(v) for k, v in vars(r).items()} for r in rows])


def test_every_trace_of_the_person_is_erased_and_nothing_else(person, bystander):
    admin = AdminFactory()
    lead = person["lead"]
    assert KnowledgeChunk.objects.filter(lead_id=lead.pk).exists()
    out = erase(lead, admin, "--yes")
    assert "Erased lead" in out

    lead.refresh_from_db()
    activities = list(Activity.objects.filter(lead=lead))
    opportunity = Opportunity.objects.get(pk=person["opportunity"].pk)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT lost_reason FROM pipeline_stage_history WHERE opportunity_id = %s",
            [opportunity.pk],
        )
        history = [row[0] for row in cursor.fetchall()]
    erased = dump(lead, opportunity, *activities) + json.dumps(history)
    for secret in SECRETS:
        assert secret.lower() not in erased.lower(), secret
    assert (lead.display_name, lead.phone_keys) == ("[erased]", [])
    assert lead.archived_at is not None
    assert history == [""]
    assert not KnowledgeChunk.objects.filter(lead_id=lead.pk).exists()
    for key in ("answer", "follow_up", "named", "failed"):
        assert not Question.objects.filter(pk=person[key].pk).exists(), key
    stored = json.dumps([[q.text, q.answer] for q in Question.objects.all()])
    for secret in SECRETS:
        assert secret.lower() not in stored.lower(), secret
    assert not Lead.objects.filter(search_text__icontains="RAHUL").exists()
    # The bystander is untouched.
    other = bystander["lead"]
    other.refresh_from_db()
    assert (other.first_name, other.email) == ("Priya", "priya@lab.example")
    assert KnowledgeChunk.objects.filter(lead_id=other.pk).exists()
    assert Question.objects.filter(pk=bystander["answer"].pk).exists()
    # Audited with counts only.
    event = AuditEvent.objects.get(action="lead.erased")
    assert (event.actor_id, event.target_id) == (admin.pk, str(lead.pk))
    assert event.metadata["activities"] == 3
    assert event.metadata["history_rows"] == 1
    assert event.metadata["conversations"] == 3
    for secret in SECRETS:
        assert secret.lower() not in json.dumps(event.metadata).lower()


def test_the_history_stays_append_only_afterwards(person):
    erase(person["lead"], AdminFactory(), "--yes")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE pipeline_stage_history SET lost_reason = 'x'")


def test_edits_in_flight_meet_a_new_version(person):
    """Versions move, so a form opened before the erasure can't write the old text back."""
    before = {r.pk: r.version for r in Activity.objects.filter(lead=person["lead"])}
    erase(person["lead"], AdminFactory(), "--yes")
    after = {r.pk: r.version for r in Activity.objects.filter(lead=person["lead"])}
    assert all(after[pk] == version + 1 for pk, version in before.items())


def test_a_dry_run_counts_and_changes_nothing(person):
    out = erase(person["lead"], AdminFactory())
    assert "3 activities, 1 opportunities, 1 history rows with free text" in out
    assert "3 Ask Arkray conversations" in out
    assert "Dry run" in out
    person["lead"].refresh_from_db()
    assert person["lead"].email == "rahul.sharma@clinic.example"
    assert not AuditEvent.objects.filter(action="lead.erased").exists()


def test_without_the_owners_credentials_nothing_is_erased(person, monkeypatch):
    monkeypatch.setattr(services, "_owns_history", lambda: False)
    with pytest.raises(CommandError, match="owner's database credentials"):
        erase(person["lead"], AdminFactory(), "--yes")
    person["lead"].refresh_from_db()
    assert person["lead"].email == "rahul.sharma@clinic.example"
    assert Activity.objects.filter(lead=person["lead"], title="Call Rahul").exists()


def test_a_lead_without_lost_reasons_needs_no_owner(user_a, monkeypatch):
    monkeypatch.setattr(services, "_owns_history", lambda: False)
    lead = LeadFactory(owner=user_a, first_name="Asha", email="asha@x.example")
    erase(lead, AdminFactory(), "--yes")
    lead.refresh_from_db()
    assert (lead.first_name, lead.email) == ("[erased]", "")


def test_only_an_active_administrator_may_erase(person, user_b):
    with pytest.raises(CommandError, match="active administrator"):
        erase(person["lead"], user_b, "--yes")
    gone = AdminFactory(is_active=False)
    with pytest.raises(CommandError, match="active administrator"):
        erase(person["lead"], gone, "--yes")


def test_an_answer_read_before_the_erasure_is_discarded_not_stored(person, monkeypatch):
    """Phase 11 review, then the whole-software audit: a worker that read the note before
    the erasure committed stored its answer after the erasure's sweep. The answer is now
    discarded at storing: the question fails `ai_unavailable` (ask again)."""
    from arkray.ai import service

    pending = asked(
        person["lead"].owner, "Who asked about prices?", {}, status=QuestionStatus.PENDING
    )
    composed = service.answer_from_retrieval

    def erasure_meanwhile(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result: dict[str, Any] = composed(*args, **kwargs)  # read before the erasure
        assert "periwinkle" in json.dumps(result).lower()
        erase(person["lead"], AdminFactory(), "--yes")
        return result

    monkeypatch.setattr(service, "answer_from_retrieval", erasure_meanwhile)
    service.answer(pending.pk)
    pending.refresh_from_db()
    assert (pending.status, pending.error_code) == (QuestionStatus.FAILED, "ai_unavailable")
    assert pending.answer == {}


def test_a_question_waiting_during_the_erasure_is_answered_from_the_redacted_text(person):
    """Not started when the erasure ran: answered afterwards, from what is left (before the
    audit every pending question in the CRM failed, the whole organisation's)."""
    from arkray.ai import service

    waiting = asked(
        person["lead"].owner, "Who asked about prices?", {}, status=QuestionStatus.PENDING
    )
    erase(person["lead"], AdminFactory(), "--yes")
    index()
    service.answer(waiting.pk)
    waiting.refresh_from_db()
    assert waiting.status == QuestionStatus.ANSWERED
    stored = json.dumps(waiting.answer).lower()
    for secret in SECRETS:
        assert secret.lower() not in stored, secret


def test_an_erased_lead_is_not_erased_again(person, bystander):
    """Whole-software audit: the placeholder name became the search term and deleted every
    other erased lead's conversations (here, any answer quoting "[erased]")."""
    admin = AdminFactory()
    erase(person["lead"], admin, "--yes")
    quoting = asked(
        bystander["lead"].owner,
        "What are my open tasks?",
        {"blocks": [{"parts": [{"text": "[erased] — due 3 Oct"}]}]},
    )
    with pytest.raises(CommandError, match="already been erased"):
        erase(person["lead"], admin)
    with pytest.raises(CommandError, match="already been erased"):
        erase(person["lead"], admin, "--yes")
    assert Question.objects.filter(pk=quoting.pk).exists()
    assert AuditEvent.objects.filter(action="lead.erased").count() == 1


def test_a_persons_organisation_does_not_erase_their_colleagues_conversations(user_a):
    """Whole-software audit: the organisation's name was a search term, so erasing one
    contact deleted every user's conversations about the company. It is one only for a lead
    that is just an organisation."""
    contact = LeadFactory(
        owner=user_a, first_name="Asha", last_name="Verma", organization_name="Apollo Diagnostics"
    )
    about_company = asked(
        user_a,
        "What did Apollo Diagnostics order?",
        {"blocks": [{"parts": [{"text": "Two analysers."}]}]},
    )
    assert "Apollo Diagnostics" not in services.identifying_terms(contact)
    erase(contact, AdminFactory(), "--yes")
    assert Question.objects.filter(pk=about_company.pk).exists()

    company = LeadFactory(
        owner=user_a, first_name="", last_name="", organization_name="Zenith Labs"
    )
    assert "Zenith Labs" in services.identifying_terms(company)


def test_the_erasure_archives_the_lead_on_its_timeline(person):
    """Whole-software audit: the lead became archived with no timeline entry, so a later
    restore showed `lead.restored` without an archive."""
    from arkray.activities.models import TimelineEntry, TimelineKind

    lead = person["lead"]
    erase(lead, AdminFactory(), "--yes")
    kinds = list(TimelineEntry.objects.filter(lead=lead).values_list("kind", flat=True))
    assert TimelineKind.LEAD_ARCHIVED in kinds


def test_the_operator_is_found_like_at_sign_in(person):
    """Whole-software audit: `--by` matched the email case-insensitively and the role by
    name, not as sign-in and the capability policy do."""
    AdminFactory(email="erasure.admin@arkray.example")
    out = StringIO()
    call_command(
        "erase_lead",
        str(person["lead"].pk),
        "--by",
        "  Erasure.Admin@Arkray.Example ",
        "--yes",
        stdout=out,
    )
    assert "Erased lead" in out.getvalue()


def test_the_erased_records_are_indexed_again_from_their_redacted_text(person):
    """An indexing job that read the old text concurrently is overwritten afterwards."""
    from arkray.core.models import OutboxEvent

    erase(person["lead"], AdminFactory(), "--yes")
    assert OutboxEvent.objects.filter(dedupe_key=f"lead-tree:{person['lead'].pk}").exists()
    index()
    chunk_text = json.dumps(
        list(KnowledgeChunk.objects.filter(lead_id=person["lead"].pk).values("source_hash"))
    )
    assert "periwinkle" not in chunk_text


def test_the_application_role_erases_a_lead_whose_opportunities_have_no_lost_reason(
    user_a, monkeypatch
):
    """Phase 11 review: the history redaction ran (and needed the owner) whenever the lead
    had an opportunity, even with nothing to redact."""
    monkeypatch.setattr(services, "_owns_history", lambda: False)
    lead = LeadFactory(owner=user_a, first_name="Kavya", email="kavya@lab.example")
    OpportunityFactory(lead=lead, owner=user_a, title="Kavya's order")
    erase(lead, AdminFactory(), "--yes")
    lead.refresh_from_db()
    assert lead.first_name == "[erased]"
    assert Opportunity.objects.get(title="[erased]").lead_id == lead.pk


def test_names_in_patterns_are_matched_literally():
    """An underscore or a percent sign in an email must not widen the match."""
    assert services._like("a_b%c") == "%a\\_b\\%c%"


def test_free_text_columns_of_the_deal_and_its_agreed_cpt_are_erased_too(user_a):
    """Final audit DOMAIN-2 / ARCH-4: work load and Expected CPT are free text a name is typed
    into, and so is the append-only agreed CPT of every negotiated price."""
    lead = LeadFactory(owner=user_a, first_name="Meera", last_name="Rao", email="meera@lab.example")
    opportunity = OpportunityFactory(
        lead=lead,
        owner=user_a,
        stage=default_stage("proposal"),
        work_load="Meera Rao lab 300/day",
        expected_cpt="Meera Rao special rate 4.5",
    )
    negotiation = Stage.objects.get(pipeline=opportunity.pipeline, is_negotiation=True)
    scope = AccessScope.own(user_a.pk)
    pipeline_services.move_opportunity(
        actor=user_a,
        scope=scope,
        opportunity_id=opportunity.pk,
        version=opportunity.version,
        stage_id=negotiation.pk,
        negotiated_price=Decimal("1100000"),
        agreed_cpt="Meera Rao agreed 3.9",
    )

    out = erase(lead, AdminFactory(), "--yes")

    opportunity.refresh_from_db()
    assert (opportunity.work_load, opportunity.expected_cpt) == ("", "")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT agreed_cpt, price FROM pipeline_negotiation_price WHERE opportunity_id = %s",
            [opportunity.pk],
        )
        rows = cursor.fetchall()
    assert rows == [("", Decimal("1100000.00"))]  # the figure stays, the words are gone
    assert "1 history rows" in out
    assert "Meera" not in dump(opportunity) + json.dumps([str(r) for r in rows])
    # The table is append-only again afterwards.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE pipeline_negotiation_price SET agreed_cpt = 'x'")


def test_a_free_text_instrument_is_blanked_a_listed_one_kept(user_a, admin):
    """Privacy remediation: an instrument typed before the list (ADR-0028) may name anyone."""
    from arkray.pipeline import instruments

    lead = LeadFactory(owner=user_a, first_name="Typed", last_name="Instrument")
    typed = OpportunityFactory(lead=lead, instrument_name="Analyser for Dr Mehta's lab")
    listed = OpportunityFactory(lead=lead, instrument_name=instruments.INSTRUMENTS[0])
    services.erase(lead.pk, operator_id=admin.pk)
    assert Opportunity.objects.get(pk=typed.pk).instrument_name == ""
    assert Opportunity.objects.get(pk=listed.pk).instrument_name == instruments.INSTRUMENTS[0]


def test_a_legal_hold_refuses_the_erasure(user_a, admin):
    from arkray.core.holds import UnderLegalHold
    from arkray.core.models import HoldSubject, LegalHold

    lead = LeadFactory(owner=user_a)
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD, subject_id=lead.pk, reference="CASE-3", placed_by=admin.pk
    )
    with pytest.raises(UnderLegalHold):
        services.erase(lead.pk, operator_id=admin.pk)
    assert Lead.objects.get(pk=lead.pk).first_name == lead.first_name
