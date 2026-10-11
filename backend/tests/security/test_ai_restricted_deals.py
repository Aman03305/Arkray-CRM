"""Ask Arkray and a closed deal whose customer the asker no longer sees (privacy audit PRIV-1;
pipeline.customer; docs/authorization.md#historical-deals).

`get_negotiation_history` cited such a deal by its full title, which names the customer.
Masking must hold everywhere the deal can surface: every tool's content *and* the labels
its citations carry, retrieval, the stored answer's sources, the conversation replayed to
the model with a follow-up, everything a provider would be sent, and the API's responses.
Authorisation is applied when an answer is *used*, not only when it was given: an answer
that named the customer while the asker owned the lead is withheld once the lead has been
reassigned (stale access), on re-reading and in the replayed history alike; one that never
named them stays. An administrator's support session sees what its user sees; organisation-
wide, an administrator sees everything.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from arkray.ai import indexing, service, tools
from arkray.ai.models import Question
from arkray.ai.sources import SourceModule
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from arkray.pipeline.models import NegotiationPrice, Opportunity
from tests.ai_fixtures import own
from tests.factories import LeadFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("ai_on")]

PERSON = "Ravi Kumar"
PHONE = "+91 98111 55555"
EMAIL = "ravi.kumar@home.example"
PII = (PERSON, "Ravi", "Kumar", PHONE, EMAIL)
NEUTRAL = "Apollo Labs — Adams 8380 V-lite"


def leaked(payload: Any) -> list[str]:
    text = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False, default=str)
    )
    return [value for value in PII if value in text]


def _deal(user_a, stage):
    lead = LeadFactory(owner=user_a, first_name="Ravi", last_name="Kumar", organization_name="")
    opportunity = OpportunityFactory(
        lead=lead,
        stage_key=stage,
        title=f"{PERSON} — Adams 8380 V-lite",
        account_name="Apollo Labs",
        customer_name=PERSON,
        contact_phone=PHONE,
        contact_email=EMAIL,
        instrument_name="Adams 8380 V-lite",
        value=Decimal("950000"),
        description=f"{PERSON} wants the analyser installed before March.",
        lost_reason=f"{PERSON} chose a cheaper analyser." if stage == "lost" else "",
    )
    NegotiationPrice.objects.create(
        opportunity=opportunity,
        price=Decimal("900000"),
        agreed_cpt="Rs 18 per test",
        currency="INR",
        stage_id=opportunity.stage_id,
        stage_name="Negotiation",
        source="revision",
        opportunity_version=opportunity.version,
        actor_id=user_a.pk,
        occurred_at=timezone.now(),
    )
    task = TaskFactory(
        lead=lead, opportunity=opportunity, owner=user_a, status="completed", title="Install done"
    )
    return {"lead": lead, "opportunity": opportunity, "task": task}


@pytest.fixture(params=["won", "lost"])
def deal(request: Any, user_a: Any) -> Any:
    """A closed deal (won, or lost with a lost reason) worked by user A."""
    return _deal(user_a, request.param)


@pytest.fixture
def won(user_a: Any) -> Any:
    return _deal(user_a, "won")


def reassign(admin: Any, deal: dict[str, Any], to: Any) -> None:
    lead = deal["lead"]
    lead.refresh_from_db()
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=to.pk,
    )


def ref(deal: dict[str, Any]) -> str:
    return f"opportunity:{deal['opportunity'].pk}"


# --- every tool, content and citation labels ---------------------------------------------------
TOOL_CALLS = {
    "get_negotiation_history": lambda d: {"ref": ref(d)},
    "get_record": lambda d: {"ref": ref(d)},
    "find_records": lambda d: {"query": "Apollo"},
    "list_opportunities": lambda d: {"status": d["opportunity"].status},
    "search_notes": lambda d: {"query": "analyser installed before March"},
    "find_records:task": lambda d: {"query": "Install"},  # the deal's completed task
}


def _run(scope: AccessScope, name: str, args: dict[str, Any]) -> tuple[Any, tools.ToolContext]:
    ctx = tools.ToolContext(scope=scope, now=timezone.now())
    outcome = tools.execute(ctx, name, args)
    assert not outcome.is_error, outcome.content
    return json.loads(outcome.content), ctx


@pytest.mark.parametrize("name", TOOL_CALLS)
def test_no_tool_names_the_customer_after_reassignment(admin, user_a, user_b, deal, name):
    indexing.index_source(SourceModule.OPPORTUNITY, deal["opportunity"].pk)  # its text indexed
    reassign(admin, deal, user_b)
    content, ctx = _run(own(user_a), name.partition(":")[0], TOOL_CALLS[name](deal))
    if name.startswith("find_records"):
        del content["query"]  # the asker's own words, echoed
    assert not leaked(content), content
    labels = [(record.label, record.detail) for record in ctx.records.values()]
    assert not leaked(labels), labels
    assert not leaked([(c.label, c.snippet) for c in ctx.citations])
    cited = ctx.records.get(ref(deal))
    if cited is not None:  # still the asker's deal, named without its customer
        assert cited.label == NEUTRAL
        assert cited.restricted
    if name in {"get_record", "get_negotiation_history"}:
        assert content["customer_restricted"] is True
        assert cited is not None


def test_the_deals_own_text_is_withheld_but_its_figures_stay(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    content, _ = _run(own(user_a), "get_record", {"ref": ref(deal)})
    assert content["untrusted_text"] is None
    assert "lost_reason" not in content
    assert content["value"]["amount"] == "950000.00"
    assert content["negotiation_history"][0]["agreed_cpt"] == "Rs 18 per test"
    history, _ = _run(own(user_a), "get_negotiation_history", {"ref": ref(deal)})
    assert history["title"] == NEUTRAL
    assert history["history"][0]["price"]["amount"] == "900000.00"


def test_retrieval_never_quotes_the_deal_after_reassignment(admin, user_a, user_b, won):
    indexing.index_source(SourceModule.OPPORTUNITY, won["opportunity"].pk)
    before, _ = _run(own(user_a), "search_notes", {"query": "analyser installed before March"})
    assert [p["ref"] for p in before["passages"]] == [ref(won)]  # control: found while owned
    reassign(admin, won, user_b)
    after, ctx = _run(own(user_a), "search_notes", {"query": "analyser installed before March"})
    assert after["passages"] == []
    assert ctx.citations == []


def test_the_owner_and_the_organisation_see_the_customer(admin, user_a, user_b, won):
    _, ctx = _run(own(user_a), "get_negotiation_history", {"ref": ref(won)})
    assert ctx.records[ref(won)].label == won["opportunity"].title  # before: named in full
    assert not ctx.records[ref(won)].restricted
    reassign(admin, won, user_b)
    content, ctx = _run(
        AccessScope.organization(admin.pk), "get_negotiation_history", {"ref": ref(won)}
    )
    assert ctx.records[ref(won)].label == won["opportunity"].title
    assert "customer_restricted" not in content


# --- the model, the stored answer and the conversation -------------------------------------
def ask(actor: Any, scope: AccessScope, text: str, conversation: Any = None) -> Question:
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        question = service.submit(actor, scope, text, conversation)
        return service.dispatch(question)


def answered(question: Question) -> Question:
    service.answer(question.pk)
    question.refresh_from_db()
    return question


def sent(scripted: Any) -> str:
    return json.dumps(scripted.requests[-1]["messages"], ensure_ascii=False)


def _ask_about_the_deal(user_a: Any, won: dict[str, Any], scripted: Any) -> Question:
    scripted.steps += [
        [("get_negotiation_history", {"ref": ref(won)})],
        f"Your deal with {PERSON}: [[{ref(won)}]].",
    ]
    return answered(ask(user_a, own(user_a), "What did we agree on that deal?"))


def test_an_answer_that_named_the_customer_is_withheld_after_reassignment(
    admin, user_a, user_b, won, scripted
):
    """Stale access: asked while user A owned the lead, re-read after it was reassigned."""
    first = _ask_about_the_deal(user_a, won, scripted)
    assert first.status == "answered"
    assert leaked(first.answer)  # stored as given: it named the customer then
    client = signed_in(user_a)
    before = client.get(f"/api/v1/workspaces/me/ask/questions/{first.pk}").json()
    assert PERSON in json.dumps(before, ensure_ascii=False)  # control: shown while owned

    reassign(admin, won, user_b)
    polled = client.get(f"/api/v1/workspaces/me/ask/questions/{first.pk}").json()
    assert not leaked(polled), polled
    assert "records_hidden" in polled["answer"]["notices"]
    conversation = client.get(
        f"/api/v1/workspaces/me/ask/conversations/{first.conversation_id}"
    ).json()
    assert not leaked(conversation), conversation
    listed = client.get("/api/v1/workspaces/me/ask/conversations").json()
    assert not leaked(listed)


def test_a_follow_up_never_replays_the_customer_after_reassignment(
    admin, user_a, user_b, won, scripted
):
    first = _ask_about_the_deal(user_a, won, scripted)
    reassign(admin, won, user_b)
    scripted.steps += ["Nothing more."]
    answered(ask(user_a, own(user_a), "Anything else?", first.conversation_id))
    payload = sent(scripted)
    assert "<earlier_turns>" not in payload
    assert not leaked(payload), leaked(payload)


def test_an_answer_that_never_named_the_customer_stays_readable(
    admin, user_a, user_b, won, scripted
):
    reassign(admin, won, user_b)
    scripted.steps += [
        [("get_negotiation_history", {"ref": ref(won)}), ("get_record", {"ref": ref(won)})],
        f"That deal is [[{ref(won)}]].",
    ]
    first = answered(ask(user_a, own(user_a), "What did we agree on that deal?"))
    assert first.answer["masked"] == [ref(won)]
    assert not leaked(first.answer)
    shown = service.visible_answer(first.answer, own(user_a))
    assert "records_hidden" not in shown["notices"]
    assert [s["label"] for s in shown["sources"]] == [NEUTRAL]

    scripted.steps += ["Nothing more."]
    answered(ask(user_a, own(user_a), "Anything else?", first.conversation_id))
    payload = json.dumps(scripted.requests, ensure_ascii=False)  # every request, tools too
    assert "<earlier_turns>" in sent(scripted)  # replayed: it never named the customer
    assert NEUTRAL in sent(scripted)
    assert not leaked(payload), leaked(payload)


def test_nothing_sent_to_the_provider_names_the_customer(admin, user_a, user_b, won, scripted):
    indexing.index_source(SourceModule.OPPORTUNITY, won["opportunity"].pk)
    reassign(admin, won, user_b)
    scripted.steps += [
        [(name.partition(":")[0], call(won)) for name, call in TOOL_CALLS.items()],
        "Done.",
    ]
    question = answered(ask(user_a, own(user_a), "Tell me about the Apollo deal"))
    assert question.status == "answered"
    everything = json.dumps(scripted.requests, ensure_ascii=False)
    assert not leaked(everything), leaked(everything)
    assert not leaked(question.answer)


def test_a_legacy_answer_without_masked_is_withheld_once_restricted(admin, user_a, user_b, won):
    """An answer stored before `masked` existed may have named the customer: withheld."""
    legacy = {
        "basis": [ref(won)],
        "blocks": [{"type": "paragraph", "parts": [{"text": f"{PERSON} agreed."}]}],
        "facts": [],
        "sources": [{"ref": ref(won), "kind": "opportunity", "label": won["opportunity"].title}],
        "citations": [],
        "notices": [],
    }
    assert service.visible_answer(legacy, own(user_a)) is legacy  # owned: shown as stored
    reassign(admin, won, user_b)
    shown = service.visible_answer(legacy, own(user_a))
    assert not leaked(shown)
    assert "records_hidden" in shown["notices"]


# --- administrators ----------------------------------------------------------------------------
def test_a_support_session_asks_as_its_user_sees(admin, user_a, user_b, won, scripted):
    reassign(admin, won, user_b)
    client = signed_in(admin)
    started = client.post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "Ticket 7"},
        format="json",
    )
    assert started.status_code in (200, 201), started.content
    scripted.steps += [
        [("get_negotiation_history", {"ref": ref(won)}), ("find_records", {"query": "Apollo"})],
        f"See [[{ref(won)}]].",
    ]
    with mock.patch("arkray.ai.tasks.answer_question.apply_async"):
        asked = client.post(
            f"/api/v1/workspaces/{user_a.pk}/ask",
            {"question": "What did we agree on the Apollo deal?"},
            format="json",
        )
    assert asked.status_code == 201, asked.content
    question_id = asked.json()["id"]
    service.answer(question_id)
    assert not leaked(json.dumps(scripted.requests, ensure_ascii=False))
    polled = client.get(f"/api/v1/workspaces/{user_a.pk}/ask/questions/{question_id}").json()
    assert polled["status"] == "answered", polled
    assert not leaked(polled), polled
    assert [s["label"] for s in polled["answer"]["sources"]] == [NEUTRAL]


def test_organisation_wide_an_administrators_answer_keeps_the_customer(
    admin, user_a, user_b, won, scripted
):
    reassign(admin, won, user_b)
    scripted.steps += [
        [("get_negotiation_history", {"ref": ref(won)})],
        f"Agreed with {PERSON}: [[{ref(won)}]].",
    ]
    organisation = AccessScope.organization(admin.pk)
    first = answered(ask(admin, organisation, "What did we agree on that deal?"))
    assert first.answer["masked"] == []
    shown = service.visible_answer(first.answer, organisation)
    assert "records_hidden" not in shown["notices"]
    assert [s["label"] for s in shown["sources"]] == [won["opportunity"].title]
    assert Opportunity.objects.get(pk=won["opportunity"].pk).owner_id == user_a.pk
