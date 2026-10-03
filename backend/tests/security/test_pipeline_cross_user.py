"""The critical cross-user suite for the Pipeline
(docs/testing.md#critical-cross-user-security-suite).

World: Admin, User A (Rahul) and User B (Priya). Lead LA belongs to A with opportunities
A1 and A2; lead LB belongs to B with opportunity B1. The attacker must never obtain B1,
learn that it exists, change it, or have it counted in a total, through any channel: list,
detail, guessed id, lead relationship, pipeline board, filters, sorting, cursors, stage
transitions, edits, archive, conversion, aggregates, workspace substitution or crafted
payloads. Every case also runs with the roles swapped.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from arkray.audit.models import AuditEvent
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity, Pipeline, Stage, StageHistory
from tests.factories import LeadFactory, OpportunityFactory, UserFactory, default_stage
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

ME = "/api/v1/workspaces/me"
SECRET_TITLE = "Zenobia Secret Deal"
SECRET_VALUE = Decimal("987654321.99")


@pytest.fixture(params=["a_attacks_b", "b_attacks_a"])
def world(request, user_a, user_b):
    """(attacker, victim, attacker's lead, attacker's opportunities, victim's lead, B1)."""
    attacker, victim = (user_a, user_b) if request.param == "a_attacks_b" else (user_b, user_a)
    own_lead = LeadFactory(owner=attacker, first_name="Arjun")
    own = [
        OpportunityFactory(lead=own_lead, title="A1", value=Decimal("1000000")),
        OpportunityFactory(
            lead=own_lead, title="A2", value=Decimal("500000"), stage=default_stage("proposal")
        ),
    ]
    victim_lead = LeadFactory(owner=victim, first_name="Zenobia", organization_name="Secret Pharma")
    secret = OpportunityFactory(
        lead=victim_lead,
        title=SECRET_TITLE,
        value=SECRET_VALUE,
        stage=default_stage("negotiation"),
        description="Confidential pricing",
    )
    return attacker, victim, own_lead, own, victim_lead, secret


def ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


def walk(client, url, params):
    seen, response = [], client.get(url, params)
    while True:
        assert response.status_code == 200, response.content
        body = response.json()
        seen.extend(row["id"] for row in body["results"])
        if not body["next"]:
            return seen
        response = client.get(body["next"])


LIST_QUERIES = [
    {},
    {"archived": "true"},
    {"status": "open"},
    {"status": "won"},
    {"expected_close_from": "2000-01-01", "expected_close_to": "2099-12-31"},
    {"probability_min": "0", "probability_max": "100"},
    {"probability_min": "75"},
    *(
        {"ordering": o}
        for o in [
            "-created_at",
            "created_at",
            "-value",
            "value",
            "expected_close",
            "-updated_at",
            "-closed_at",
        ]
    ),
    {"page_size": 1},
    {"page_size": 100},
]


class TestReads:
    @pytest.mark.parametrize("params", LIST_QUERIES)
    def test_no_list_query_ever_returns_the_victims_opportunity(self, world, params):
        attacker, _, _, own, _, secret = world
        seen = walk(signed_in(attacker), f"{ME}/opportunities", params)
        assert str(secret.pk) not in seen
        assert set(seen) <= {str(o.pk) for o in own}

    def test_filtering_by_the_victims_lead_or_stage_finds_nothing(self, world):
        attacker, _, _, _, victim_lead, secret = world
        client = signed_in(attacker)
        assert ids(client.get(f"{ME}/opportunities", {"lead": str(victim_lead.pk)})) == set()
        assert str(secret.pk) not in ids(
            client.get(f"{ME}/opportunities", {"stage": str(secret.stage_id)})
        )

    @pytest.mark.parametrize("action", ["", "/history"])
    def test_a_guessed_id_is_indistinguishable_from_a_missing_one(self, world, action):
        attacker, _, _, _, _, secret = world
        client = signed_in(attacker)
        real = client.get(f"{ME}/opportunities/{secret.pk}{action}")
        missing = client.get(f"{ME}/opportunities/{uuid.uuid4()}{action}")
        assert real.status_code == missing.status_code == 404
        assert without_request_id(real) == without_request_id(missing)

    def test_the_board_shows_only_the_attackers_cards(self, world):
        attacker, _, _, own, _, _ = world
        body = signed_in(attacker).get(f"{ME}/pipeline-board", {"cards_per_stage": 50}).json()
        cards = {c["id"] for column in body["columns"] for c in column["cards"]}
        assert cards == {str(o.pk) for o in own}
        assert SECRET_TITLE not in str(body)
        negotiation = next(c for c in body["columns"] if c["stage"]["key"] == "negotiation")
        assert (negotiation["count"], negotiation["total_value"]) == (0, "0.00")

    def test_a_column_cursor_cant_be_replayed_into_another_workspace(self, world, user_a, user_b):
        """Cursors carry sort values only; the workspace always comes from the URL."""
        attacker, victim, *_ = world
        victim_lead = Lead.objects.filter(owner=victim).first()
        for i in range(3):
            OpportunityFactory(lead=victim_lead, title=f"Victim {i}")
        victims_page = signed_in(victim).get(f"{ME}/opportunities", {"page_size": 1}).json()
        replayed = signed_in(attacker).get(victims_page["next"])
        assert replayed.status_code == 200
        assert all(
            row["title"] not in {"Victim 0", "Victim 1", "Victim 2", SECRET_TITLE}
            for row in replayed.json()["results"]
        )


class TestAggregatesNeverLeak:
    """The totals must add up the attacker's authorized OPEN opportunities only."""

    def expected(self):
        # A1 1,000,000 at 10 % (new) + A2 500,000 at 50 % (proposal)
        return {"pipeline_value": "1500000.00", "weighted_pipeline": "350000.00", "open_count": 2}

    def test_summary(self, world):
        attacker, *_ = world
        body = signed_in(attacker).get(f"{ME}/pipeline-summary").json()
        assert body["totals"] == self.expected()

    def test_board_totals_and_column_values(self, world):
        attacker, *_ = world
        body = signed_in(attacker).get(f"{ME}/pipeline-board").json()
        assert body["totals"] == self.expected()
        assert sum(c["count"] for c in body["columns"]) == 2

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"probability_min": "75"},
            {"lead": "{victim_lead}"},
            {"expected_close_from": "2000-01-01"},
            {"pipeline": "{pipeline}"},
        ],
    )
    def test_no_filter_widens_a_total(self, world, params):
        attacker, _, _, _, victim_lead, secret = world
        query = {
            k: v.format(victim_lead=victim_lead.pk, pipeline=secret.pipeline_id)
            for k, v in params.items()
        }
        for path in ("pipeline-summary", "pipeline-board"):
            body = signed_in(attacker).get(f"{ME}/{path}", query).json()
            assert Decimal(body["totals"]["pipeline_value"]) <= Decimal("1500000.00")
            assert str(SECRET_VALUE) not in str(body)

    def test_totals_are_identical_whether_or_not_the_victim_has_opportunities(self, world):
        attacker, victim, *_ = world
        client = signed_in(attacker)
        before = (
            client.get(f"{ME}/pipeline-summary").json(),
            client.get(f"{ME}/pipeline-board").json(),
        )
        Opportunity.objects.filter(owner=victim).update(value=Decimal("1"))
        after = (
            client.get(f"{ME}/pipeline-summary").json(),
            client.get(f"{ME}/pipeline-board").json(),
        )
        assert before == after

    def test_an_admin_in_a_users_workspace_gets_that_users_totals_only(self, world, admin):
        attacker, victim, *_ = world
        client = signed_in(admin)
        own = client.get(f"/api/v1/workspaces/{attacker.pk}/pipeline-summary").json()
        assert own["totals"] == self.expected()
        theirs = client.get(f"/api/v1/workspaces/{victim.pk}/pipeline-summary").json()
        assert theirs["totals"] == {
            "pipeline_value": "987654321.99",
            "weighted_pipeline": "740740741.49",
            "open_count": 1,
        }
        everyone = client.get("/api/v1/workspaces/all/pipeline-summary").json()
        assert everyone["totals"]["open_count"] == 3
        assert everyone["totals"]["pipeline_value"] == "989154321.99"


class TestWrites:
    @pytest.mark.parametrize(
        ("method", "action", "body"),
        [
            ("patch", "", {"version": 1, "title": "Hacked"}),
            ("patch", "", {"version": 1, "value": "1"}),
            ("post", "/move", {"version": 1, "stage": "{won}"}),
            ("post", "/move", {"version": 1, "stage": "{lost}", "lost_reason": "x"}),
            ("post", "/archive", {"version": 1}),
            ("post", "/restore", {"version": 1}),
        ],
    )
    def test_every_write_to_the_victims_opportunity_is_a_404(self, world, method, action, body):
        attacker, _, _, _, _, secret = world
        payload = {
            k: v.format(won=default_stage("won").pk, lost=default_stage("lost").pk)
            if isinstance(v, str)
            else v
            for k, v in body.items()
        }
        response = getattr(signed_in(attacker), method)(
            f"{ME}/opportunities/{secret.pk}{action}", payload, format="json"
        )
        assert response.status_code == 404
        secret.refresh_from_db()
        assert (secret.title, secret.value, secret.status, secret.version) == (
            SECRET_TITLE,
            SECRET_VALUE,
            "open",
            1,
        )
        assert not AuditEvent.objects.filter(target_id=str(secret.pk)).exists()
        assert not StageHistory.objects.filter(opportunity=secret).exists()

    def test_no_opportunity_can_be_created_against_the_victims_lead(self, world):
        attacker, _, _, _, victim_lead, _ = world
        response = signed_in(attacker).post(
            f"{ME}/opportunities",
            {"lead": str(victim_lead.pk), "title": "Mine now", "value": "1"},
            format="json",
        )
        assert response.status_code == 404
        assert not Opportunity.objects.filter(title="Mine now").exists()

    def test_the_victims_lead_cant_be_converted(self, world):
        attacker, _, _, _, victim_lead, _ = world
        response = signed_in(attacker).post(
            f"{ME}/leads/{victim_lead.pk}/convert",
            {"version": 1, "title": "Mine now", "value": "1"},
            format="json",
        )
        assert response.status_code == 404
        victim_lead.refresh_from_db()
        assert victim_lead.status_id == "new"
        assert not Opportunity.objects.filter(title="Mine now").exists()

    @pytest.mark.parametrize(
        "extra",
        [
            lambda victim: {"owner": str(victim.pk)},
            lambda victim: {"owner_id": str(victim.pk)},
            lambda victim: {"created_by": str(victim.pk)},
            lambda _: {"status": "won"},
            lambda _: {"closed_at": "2026-01-01T00:00:00Z"},
            lambda _: {"category": "won"},
            lambda _: {"probability_overridden": False},
            lambda _: {"version": 99},
            lambda _: {"tenant": "other"},
            lambda _: {"organization": str(uuid.uuid4())},
            lambda _: {"pipeline_owner": str(uuid.uuid4())},
            lambda _: {"audit": {"actor_id": str(uuid.uuid4())}},
            lambda _: {"actor_id": str(uuid.uuid4())},
            lambda _: {"is_superuser": True},
            lambda _: {"capabilities": ["crm.view_all"]},
            lambda _: {"weighted_value": "1"},
        ],
    )
    def test_crafted_create_payloads_are_refused(self, world, extra):
        attacker, victim, own_lead, *_ = world
        response = signed_in(attacker).post(
            f"{ME}/opportunities",
            {"lead": str(own_lead.pk), "title": "Mallory", "value": "1", **extra(victim)},
            format="json",
        )
        assert response.status_code == 400
        assert not Opportunity.objects.filter(title="Mallory").exists()

    @pytest.mark.parametrize(
        "extra",
        [
            lambda victim: {"owner": str(victim.pk)},
            lambda _: {"status": "won"},
            lambda _: {"stage": str(default_stage("won").pk)},
            lambda _: {"lead": str(uuid.uuid4())},
            lambda _: {"closed_at": None},
            lambda _: {"created_at": "2001-01-01T00:00:00Z"},
            lambda _: {"archived_at": None},
            lambda _: {"id": str(uuid.uuid4())},
            lambda _: {"probability_overridden": True},
        ],
    )
    def test_crafted_patches_cant_change_system_fields(self, world, extra):
        attacker, victim, _, own, *_ = world
        target = own[0]
        response = signed_in(attacker).patch(
            f"{ME}/opportunities/{target.pk}", {"version": 1, **extra(victim)}, format="json"
        )
        assert response.status_code == 400
        target.refresh_from_db()
        assert (target.owner_id, target.status, target.version) == (attacker.pk, "open", 1)

    def test_a_probability_bypass_on_a_won_deal_is_refused(self, world):
        attacker, _, _, own, *_ = world
        won = OpportunityFactory(lead=own[0].lead, stage=default_stage("won"))
        response = signed_in(attacker).patch(
            f"{ME}/opportunities/{won.pk}", {"version": 1, "probability": "20"}, format="json"
        )
        assert response.status_code == 400
        won.refresh_from_db()
        assert won.probability == Decimal("100")

    def test_moving_into_another_pipelines_stage_is_refused(self, world):
        attacker, _, _, own, *_ = world
        other = Pipeline.objects.create(key="other", name="Other")
        foreign = Stage.objects.create(
            pipeline=other, key="x", name="X", position=1, probability=99, category="open"
        )
        response = signed_in(attacker).post(
            f"{ME}/opportunities/{own[0].pk}/move",
            {"stage": str(foreign.pk), "version": 1},
            format="json",
        )
        assert response.status_code == 400


class TestWorkspaceSubstitution:
    @pytest.mark.parametrize(
        "segment",
        ["{victim}", "all", "{victim_upper}", "urn:uuid:{victim}", "{{{victim}}}", "{victim_hex}"],
    )
    @pytest.mark.parametrize("path", ["pipeline-board", "pipeline-summary", "opportunities"])
    def test_a_sales_user_cant_open_someone_elses_or_the_organisations_pipeline(
        self, world, segment, path
    ):
        attacker, victim, *_ = world
        workspace = segment.format(
            victim=victim.pk, victim_upper=str(victim.pk).upper(), victim_hex=victim.pk.hex
        )
        response = signed_in(attacker).get(f"/api/v1/workspaces/{workspace}/{path}")
        assert response.status_code == 404
        assert SECRET_TITLE not in response.content.decode()

    def test_the_victims_opportunity_through_the_victims_workspace_segment(self, world):
        attacker, victim, _, _, _, secret = world
        response = signed_in(attacker).get(
            f"/api/v1/workspaces/{victim.pk}/opportunities/{secret.pk}"
        )
        assert response.status_code == 404


class TestClosedHistoryAfterReassignment:
    def test_the_previous_owner_keeps_their_won_deal_but_not_the_lead(self, admin, user_a, user_b):
        lead = LeadFactory(owner=user_a, first_name="Zenobia")
        won = OpportunityFactory(lead=lead, stage=default_stage("won"))
        signed_in(admin).post(
            f"/api/v1/workspaces/all/leads/{lead.pk}/assign",
            {"owner": str(user_b.pk), "version": 1},
            format="json",
        )
        body = signed_in(user_a).get(f"{ME}/opportunities/{won.pk}").json()
        assert body["owner"]["id"] == str(user_a.pk)
        assert body["lead"] == {"id": None, "restricted": True}
        assert "Zenobia" not in str(body)
        card = signed_in(user_a).get(f"{ME}/opportunities").json()["results"][0]
        assert card["lead"] == {"id": None, "restricted": True}


def test_sales_users_have_no_organisation_or_delegated_pipeline(user_a):
    client = signed_in(user_a)
    for path in ("pipeline-board", "pipeline-summary", "opportunities"):
        assert client.get(f"/api/v1/workspaces/all/{path}").status_code == 404
        assert client.get(f"/api/v1/workspaces/{UserFactory().pk}/{path}").status_code == 404
