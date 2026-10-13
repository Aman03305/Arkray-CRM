"""A closed deal stays with the salesperson who worked it after its lead is reassigned, but
without the customer's identity and contact details (privacy remediation P2-10;
docs/authorization.md#historical-deals; pipeline.customer).

Checked before and after the reassignment, on every surface that shows a deal: its page,
lists and board, archived deals, global search, activity and timeline links, Ask Arkray's
tools and an administrator's support session (which sees what the user sees). The new owner
and an administrator organisation-wide see everything.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from arkray.ai import tools
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from arkray.pipeline.models import CustomField, FieldType, Opportunity
from tests.factories import LeadFactory, OpportunityFactory, TaskFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

PERSON = "Ravi Kumar"
PHONE = "+91 98111 55555"
EMAIL = "ravi.kumar@home.example"
ADDRESS = "14 Lake Road, Pune"
PII = (PERSON, "Ravi", PHONE, EMAIL, ADDRESS)


def leaked(payload: Any) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return [value for value in PII if value in text]


@pytest.fixture
def deal(user_a, admin):
    lead = LeadFactory(owner=user_a, first_name="Ravi", last_name="Kumar", organization_name="")
    opportunity = OpportunityFactory(
        lead=lead,
        stage_key="won",
        title=f"{PERSON} — Adams 8380 V-lite",
        account_name="Apollo Labs",
        customer_name=PERSON,
        contact_phone=PHONE,
        contact_email=EMAIL,
        address=ADDRESS,
        instrument_name="Adams 8380 V-lite",
        value=Decimal("950000"),
    )
    notes_field = CustomField.objects.create(
        pipeline=opportunity.pipeline, name="Lab contact", field_type=FieldType.TEXT, position=90
    )
    volume_field = CustomField.objects.create(
        pipeline=opportunity.pipeline, name="Tests a day", field_type=FieldType.NUMBER, position=91
    )
    Opportunity.objects.filter(pk=opportunity.pk).update(
        custom_fields={str(notes_field.pk): f"Ask for {PERSON}", str(volume_field.pk): "300"}
    )
    task = TaskFactory(
        lead=lead, opportunity=opportunity, owner=user_a, status="completed", title="Install done"
    )
    return {"lead": lead, "opportunity": opportunity, "task": task, "volume": str(volume_field.pk)}


def reassign(admin, deal, to):
    lead = deal["lead"]
    lead.refresh_from_db()
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=to.pk,
    )


def page(client: Any, deal: dict[str, Any]) -> dict[str, Any]:
    response = client.get(f"/api/v1/workspaces/me/opportunities/{deal['opportunity'].pk}")
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def test_before_reassignment_the_owner_sees_the_customer(user_a, deal):
    body = page(signed_in(user_a), deal)
    assert body["customer_restricted"] is False
    assert (body["customer_name"], body["contact_email"]) == (PERSON, EMAIL)


def test_after_reassignment_the_page_hides_the_customer(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    body = page(signed_in(user_a), deal)
    assert body["customer_restricted"] is True
    assert body["lead"] == {"id": None, "restricted": True}
    for field in ("customer_name", "contact_phone", "contact_email", "address"):
        assert body[field] == ""
    assert body["title"] == "Apollo Labs — Adams 8380 V-lite"
    assert body["account_name"] == "Apollo Labs"
    assert body["value"] == "950000.00"  # the sales history stays
    assert body["custom_fields"] == {deal["volume"]: "300"}  # free text left out
    assert not leaked(body), leaked(body)


def test_a_person_only_account_is_not_shown_either(admin, user_a, user_b, deal):
    Opportunity.objects.filter(pk=deal["opportunity"].pk).update(account_name=PERSON)
    reassign(admin, deal, user_b)
    body = page(signed_in(user_a), deal)
    assert body["account_name"] == ""
    assert body["title"] == "Customer restricted — Adams 8380 V-lite"
    assert not leaked(body)


def test_lists_board_and_archived_lists_hide_it(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(user_a)
    listing = client.get("/api/v1/workspaces/me/opportunities?status=won").json()
    board = client.get("/api/v1/workspaces/me/pipeline-board").json()
    assert listing["results"][0]["customer_restricted"] is True
    assert not leaked(listing)
    assert not leaked(board)
    Opportunity.objects.filter(pk=deal["opportunity"].pk).update(archived_at=timezone.now())
    archived = client.get("/api/v1/workspaces/me/opportunities?archived=true").json()
    assert archived["results"]
    assert not leaked(archived)


def test_search_never_matches_it(admin, user_a, user_b, deal):
    client = signed_in(user_a)
    before = client.get("/api/v1/workspaces/me/search?q=Ravi").json()
    assert [o["id"] for o in before["opportunities"]["results"]] == [str(deal["opportunity"].pk)]
    reassign(admin, deal, user_b)
    for term in ("Ravi", "Kumar"):  # the customer's name finds nothing any more
        after = client.get(f"/api/v1/workspaces/me/search?q={term}").json()
        assert after["opportunities"]["results"] == [], term
        assert not leaked({k: v for k, v in after.items() if k not in ("query", "terms")})
    # Its organisation still finds it, shown without the customer.
    found = client.get("/api/v1/workspaces/me/search?q=Apollo").json()["opportunities"]["results"]
    assert [o["id"] for o in found] == [str(deal["opportunity"].pk)]
    assert found[0]["customer_restricted"] is True
    assert found[0]["title"] == "Apollo Labs — Adams 8380 V-lite"
    assert not leaked(found)


def test_activity_and_timeline_links_hide_it(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(user_a)
    activities = client.get("/api/v1/workspaces/me/activities?status=completed").json()
    rows = [r for r in activities["results"] if r["id"] == str(deal["task"].pk)]
    assert rows
    assert rows[0]["opportunity"]["title"] == "Apollo Labs — Adams 8380 V-lite"
    timeline = client.get(
        f"/api/v1/workspaces/me/opportunities/{deal['opportunity'].pk}/timeline"
    ).json()
    assert not leaked(activities)
    assert not leaked(timeline)


def test_ask_arkray_tools_hide_it(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    outcome = tools.execute(ctx, "get_record", {"ref": f"opportunity:{deal['opportunity'].pk}"})
    assert not outcome.is_error, outcome.content
    assert not leaked(json.loads(outcome.content))
    assert not leaked([record.label for record in ctx.records.values()])


@pytest.mark.parametrize("query", ["Apollo", "Adams 8380", "Ravi Kumar"])
def test_ask_arkray_search_never_names_the_customer(admin, user_a, user_b, deal, query):
    """Backend review P1: find_records cited the deal by its full title."""
    reassign(admin, deal, user_b)
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    outcome = tools.execute(ctx, "find_records", {"query": query})
    assert not outcome.is_error, outcome.content
    content = json.loads(outcome.content)
    del content["query"]  # the user's own words, echoed
    assert not leaked(content)
    assert not leaked([record.label for record in ctx.records.values()])
    if query == "Apollo":  # still found by its organisation, under its neutral name
        assert [o["title"] for o in content["opportunities"]] == ["Apollo Labs — Adams 8380 V-lite"]


def test_ask_arkray_retrieval_never_returns_it(admin, user_a, user_b, deal):
    """Backend review P1: retrieval re-read the deal's title and text for its old owner."""
    from arkray.pipeline import selectors

    Opportunity.objects.filter(pk=deal["opportunity"].pk).update(
        description=f"{PERSON} wants the install before March."
    )
    ids = [deal["opportunity"].pk]
    assert selectors.knowledge_documents(AccessScope.own(user_a.pk), ids)
    reassign(admin, deal, user_b)
    assert selectors.knowledge_documents(AccessScope.own(user_a.pk), ids) == []
    assert selectors.knowledge_documents(AccessScope.own(user_b.pk), ids) == []  # not their deal
    found = selectors.knowledge_documents(AccessScope.organization(admin.pk), ids)
    assert [doc.source_id for doc in found] == ids


def test_a_support_session_sees_what_the_user_sees(admin, user_a, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(admin)
    started = client.post(
        "/api/v1/admin/support-sessions",
        {"user": str(user_a.pk), "reason": "Ticket 1"},
        format="json",
    )
    assert started.status_code in (200, 201)
    body = client.get(
        f"/api/v1/workspaces/{user_a.pk}/opportunities/{deal['opportunity'].pk}"
    ).json()
    assert body["customer_restricted"] is True
    assert not leaked(body)


def test_an_administrator_organisation_wide_sees_everything(admin, user_b, deal):
    reassign(admin, deal, user_b)
    body = (
        signed_in(admin)
        .get(f"/api/v1/workspaces/all/opportunities/{deal['opportunity'].pk}")
        .json()
    )
    assert body["customer_restricted"] is False
    assert body["customer_name"] == PERSON


# --- writes follow reads (deep security audit M-2) ------------------------------------------------
def _text_field(deal: dict[str, Any]) -> str:
    return str(
        CustomField.objects.get(pipeline=deal["opportunity"].pipeline, name="Lab contact").pk
    )


# Each hidden field: the value it holds (a right guess) and another (a wrong one, or an
# overwrite).
HIDDEN = [
    ("customer_name", PERSON, "Someone Else"),
    ("contact_phone", PHONE, "+91 90000 00000"),
    ("contact_email", EMAIL, "attacker@example.com"),
    ("address", ADDRESS, "Nowhere"),
    ("account_name", "Apollo Labs", "Other Labs"),
    ("custom_fields", f"Ask for {PERSON}", "overwritten"),
]


def _state(deal: dict[str, Any]) -> tuple[Any, ...]:
    row = Opportunity.objects.filter(pk=deal["opportunity"].pk).values().get()
    return row, AuditEvent.objects.count()


@pytest.mark.parametrize(("field", "right", "wrong"), HIDDEN, ids=[h[0] for h in HIDDEN])
def test_the_previous_owner_cannot_write_or_probe_a_hidden_customer_field(
    admin, user_a, user_b, deal, field, right, wrong
):
    """Right and wrong guesses get the same 403, and nothing is written, versioned or
    audited: no tampering, and no oracle (a right guess used to answer 200 with the same
    version, a wrong one a new version)."""
    reassign(admin, deal, user_b)
    client = signed_in(user_a)
    url = f"/api/v1/workspaces/me/opportunities/{deal['opportunity'].pk}"
    version = page(client, deal)["version"]
    before = _state(deal)

    def send(value: str) -> Any:
        payload = {_text_field(deal): value} if field == "custom_fields" else value
        return client.patch(url, {"version": version, field: payload}, format="json")

    answers = []
    for value in (right, wrong):
        response = send(value)
        assert response.status_code == 403, response.content
        assert response.json()["error"]["code"] == "customer_restricted"
        answers.append(without_request_id(response))
        assert _state(deal) == before, value
    assert answers[0] == answers[1]
    assert page(client, deal)["version"] == version


def test_the_previous_owner_no_longer_edits_the_deals_value(admin, user_a, user_b, deal):
    """Their value, probability and dates stayed editable here; a closed deal's figures are
    frozen for everyone now (adversarial review of 73ff1e3, M2: 422 `deal_closed`), so such
    a deal is read-only to its previous owner."""
    reassign(admin, deal, user_b)
    client = signed_in(user_a)
    url = f"/api/v1/workspaces/me/opportunities/{deal['opportunity'].pk}"
    body = page(client, deal)
    response = client.patch(
        url,
        {
            "version": body["version"],
            "value": "900000.00",
            # What they see, sent back unchanged (an edit form): not a change.
            "description": body["description"],
            "custom_fields": {deal["volume"]: "300"},
        },
        format="json",
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "deal_closed"
    stored = Opportunity.objects.get(pk=deal["opportunity"].pk)
    assert stored.value == Decimal("950000.00")
    assert stored.contact_email == EMAIL  # the hidden copy is untouched
    assert stored.custom_fields[_text_field(deal)] == f"Ask for {PERSON}"


def test_the_new_owner_and_an_administrator_still_edit_the_customer(admin, user_b, deal):
    reassign(admin, deal, user_b)
    client = signed_in(admin)
    url = f"/api/v1/workspaces/all/opportunities/{deal['opportunity'].pk}"
    version = client.get(url).json()["version"]
    response = client.patch(
        url, {"version": version, "contact_email": "ravi@new.example"}, format="json"
    )
    assert response.status_code == 200, response.content


# Review P3-2: the deal's other text is read-only for the previous owner too, with one answer
# whether the customer they no longer see is untouched, erased or under a legal hold.
FROZEN_CHANGES = [
    {"description": "Installed in March."},
    {"work_load": "300 tests a day"},
    {"expected_cpt": "Rs 18 per test"},
    {"instrument_name": "Adams 8180 V"},
    "volume",
]


@pytest.mark.parametrize(
    "change", FROZEN_CHANGES, ids=lambda c: c if isinstance(c, str) else next(iter(c))
)
def test_a_frozen_field_gets_one_answer_whatever_became_of_the_customer(
    admin, user_a, user_b, change
):
    from arkray.core.models import HoldSubject, LegalHold
    from arkray.privacy import services as privacy

    volume = None
    deals = {}
    for state in ("untouched", "erased", "held"):
        lead = LeadFactory(owner=user_a, first_name="Ravi", last_name="Kumar")
        opportunity = OpportunityFactory(
            lead=lead, stage_key="won", customer_name=PERSON, description="Signed in May."
        )
        if volume is None:
            volume = CustomField.objects.create(
                pipeline=opportunity.pipeline,
                name="Tests a day",
                field_type=FieldType.NUMBER,
                position=95,
            )
        Opportunity.objects.filter(pk=opportunity.pk).update(custom_fields={str(volume.pk): "300"})
        reassign(admin, {"lead": lead}, user_b)
        if state == "erased":
            privacy.erase(lead.pk, operator_id=admin.pk)
        if state == "held":
            LegalHold.objects.create(
                subject_type=HoldSubject.LEAD,
                subject_id=lead.pk,
                reference="C-1",
                placed_by=admin.pk,
            )
        deals[state] = Opportunity.objects.get(pk=opportunity.pk)
    payload = {"custom_fields": {str(volume.pk): "350"}} if change == "volume" else change
    client = signed_in(user_a)
    answers = []
    for state, opportunity in deals.items():
        before = (
            Opportunity.objects.filter(pk=opportunity.pk).values().get(),
            AuditEvent.objects.count(),
        )
        response = client.patch(
            f"/api/v1/workspaces/me/opportunities/{opportunity.pk}",
            {"version": opportunity.version, **payload},
            format="json",
        )
        assert response.status_code == 403, (state, response.content)
        assert response.json()["error"]["code"] == "customer_restricted"
        answers.append(without_request_id(response))
        after = (
            Opportunity.objects.filter(pk=opportunity.pk).values().get(),
            AuditEvent.objects.count(),
        )
        assert after == before, state
    assert answers[0] == answers[1] == answers[2]


# --- adversarial review of 73ff1e3, L6: one rule for the deal's free text (R118) -----------------
@pytest.fixture
def chatty_deal(user_a):
    """A deal whose free text names its customer, as dictated notes do: its description,
    work load, Expected CPT, agreed CPT (with its price history) and lost reason (with its
    stage history), and a free-text custom value."""
    from arkray.pipeline import services
    from tests.factories import default_stage

    lead = LeadFactory(owner=user_a, first_name="Ravi", last_name="Kumar", organization_name="")
    scope = AccessScope.own(user_a.pk)
    created = services.create_opportunity(
        actor=user_a,
        scope=scope,
        lead_id=lead.pk,
        fields={
            "value": Decimal("950000"),
            "account_name": "Apollo Labs",
            "customer_name": PERSON,
            "description": f"{PERSON} wants it before March.",
            "work_load": "Ravi's lab: 300 a day",
            "expected_cpt": "Rs 18 if Ravi signs",
        },
        stage_id=default_stage("negotiation").pk,
        negotiated_price=Decimal("900000"),
        agreed_cpt="Rs 17, agreed with Ravi",
    ).opportunity
    lost = services.move_opportunity(
        actor=user_a,
        scope=scope,
        opportunity_id=created.pk,
        version=created.version,
        stage_id=default_stage("lost").pk,
        lost_reason=f"{PERSON} chose another analyser.",
    )
    return {"lead": lead, "opportunity": lost}


def test_the_previous_owner_never_reads_the_deals_free_text(admin, user_a, user_b, chatty_deal):
    """Ask Arkray withheld the deal's text from its previous owner while the API returned it
    verbatim (description, lost reason, work load, Expected CPT, agreed CPT, and the lost
    reasons and agreed CPTs of its histories). One rule now (pipeline.customer.WITHHELD_TEXT),
    on every surface: the commercial record (amounts, stages, dates, who did what) stays."""
    reassign(admin, chatty_deal, user_b)
    client = signed_in(user_a)
    deal = chatty_deal["opportunity"]
    base = f"/api/v1/workspaces/me/opportunities/{deal.pk}"
    detail = client.get(base).json()
    for field in ("description", "lost_reason", "work_load", "expected_cpt", "agreed_cpt"):
        assert detail[field] == "", field
    assert (detail["value"], detail["negotiated_price"]) == ("950000.00", "900000.00")
    surfaces = {
        "detail": detail,
        "list": client.get("/api/v1/workspaces/me/opportunities?status=lost").json(),
        "board": client.get("/api/v1/workspaces/me/pipeline-board").json(),
        "history": client.get(f"{base}/history").json(),
        "prices": client.get(f"{base}/negotiated-prices").json(),
    }
    for name, payload in surfaces.items():
        assert not leaked(payload), (name, leaked(payload))
    history = surfaces["history"]["results"]
    assert [row["to_stage_name"] for row in history] == ["Lost", "Negotiation"]  # still there
    assert history[0]["lost_reason"] == ""
    prices = surfaces["prices"]["results"]
    assert [(p["price"], p["agreed_cpt"]) for p in prices] == [("900000.00", "")]


@pytest.mark.parametrize("tool", ["get_record", "get_negotiation_history"])
def test_ask_arkray_tools_withhold_the_deals_free_text(admin, user_a, user_b, chatty_deal, tool):
    """The same rule in Ask Arkray's tools (pipeline.customer.WITHHELD_TEXT): get_record sent a
    restricted deal's work load, Expected CPT and agreed CPTs, and get_negotiation_history its
    agreed CPTs, while the API blanked them (adversarial review of 73ff1e3, L6). The figures
    stay; an administrator's tools still read the text."""
    reassign(admin, chatty_deal, user_b)
    deal = chatty_deal["opportunity"]
    args = {"ref": f"opportunity:{deal.pk}"}
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    outcome = tools.execute(ctx, tool, args)
    assert not outcome.is_error, outcome.content
    content = json.loads(outcome.content)
    assert not leaked(content), leaked(content)
    assert content["customer_restricted"] is True
    history = content["negotiation_history" if tool == "get_record" else "history"]
    assert [row["price"] for row in history]
    assert all("agreed_cpt" not in row for row in history)
    if tool == "get_record":
        assert not {"work_load", "expected_cpt", "agreed_cpt"} & set(content)
    else:
        assert content["latest_agreed_cpt"] is None
    org = tools.ToolContext(scope=AccessScope.organization(admin.pk), now=timezone.now())
    full = json.loads(tools.execute(org, tool, args).content)
    assert "Rs 17, agreed with Ravi" in json.dumps(full, ensure_ascii=False)


def test_the_new_owner_and_an_administrator_still_read_it(admin, user_a, user_b, chatty_deal):
    reassign(admin, chatty_deal, user_b)
    deal = chatty_deal["opportunity"]
    for client, workspace in ((signed_in(user_b), "me"), (signed_in(admin), "all")):
        detail = client.get(f"/api/v1/workspaces/{workspace}/opportunities/{deal.pk}")
        if workspace == "me":  # a closed deal stays with its closer: not in B's workspace
            assert detail.status_code == 404
            continue
        body = detail.json()
        assert body["description"] == f"{PERSON} wants it before March."
        assert body["agreed_cpt"] == "Rs 17, agreed with Ravi"
        history = client.get(
            f"/api/v1/workspaces/{workspace}/opportunities/{deal.pk}/history"
        ).json()
        assert history["results"][0]["lost_reason"] == f"{PERSON} chose another analyser."


def test_the_previous_owner_never_reads_a_corrections_reason(admin, user_a, user_b, chatty_deal):
    deal = chatty_deal["opportunity"]
    corrected = signed_in(admin).post(
        f"/api/v1/workspaces/all/opportunities/{deal.pk}/correct",
        {"version": deal.version, "value": "940000.00", "reason": f"{PERSON} paid less."},
        format="json",
    )
    assert corrected.status_code == 200, corrected.content
    reassign(admin, chatty_deal, user_b)
    rows = (
        signed_in(user_a)
        .get(f"/api/v1/workspaces/me/opportunities/{deal.pk}/corrections")
        .json()["results"]
    )
    assert [(r["fields"], r["reason"]) for r in rows] == [(["value"], "")]
    assert not leaked(rows)
    admin_rows = (
        signed_in(admin)
        .get(f"/api/v1/workspaces/all/opportunities/{deal.pk}/corrections")
        .json()["results"]
    )
    assert admin_rows[0]["reason"] == f"{PERSON} paid less."
