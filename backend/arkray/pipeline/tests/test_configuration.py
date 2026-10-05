"""User-defined pipelines and editable stages (docs/pipeline.md#configuration), through the
API: ownership, who may configure what, stage editing and safe removal, and that won/lost
and negotiation behaviour follow a stage's type, never its name."""

from __future__ import annotations

from decimal import Decimal

import pytest

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.identity.models import Role
from arkray.identity.policy import ROLE_CAPABILITIES, Capability
from arkray.leads import services as lead_services
from arkray.pipeline import models as m
from arkray.pipeline.models import Opportunity, Pipeline, Stage, StageHistory
from tests.factories import LeadFactory, OpportunityFactory, UserFactory
from tests.helpers import signed_in

from .conftest import board_url, opportunities_url, opportunity_url

pytestmark = pytest.mark.django_db


def pipelines_url(workspace: str = "me") -> str:
    return f"/api/v1/workspaces/{workspace}/pipelines"


def pipeline_url(pipeline_id, workspace: str = "me", action: str = "") -> str:
    base = f"/api/v1/workspaces/{workspace}/pipelines/{pipeline_id}"
    return f"{base}/{action}" if action else base


def stage(name, kind="open", probability="10", stage_id=None):
    body = {"name": name, "type": kind}
    if kind in ("open", "negotiation"):
        body["probability"] = probability
    if stage_id is not None:
        body["id"] = str(stage_id)
    return body


DIAGNOSTICS = [
    stage("New", "open", "10"),
    stage("Qualified", "open", "25"),
    stage("Demo", "open", "40"),
    stage("Negotiation", "negotiation", "75"),
    stage("PO Received", "open", "90"),
    stage("Won", "won"),
    stage("Lost", "lost"),
]
SHORT = [
    stage("Lead", "open", "10"),
    stage("Discussion", "open", "40"),
    stage("Negotiation", "negotiation", "70"),
    stage("Closed", "won"),
]


def create(client, name, stages=DIAGNOSTICS, workspace="me", **extra):
    return client.post(
        pipelines_url(workspace), {"name": name, "stages": stages, **extra}, format="json"
    )


def created(client, name, stages=DIAGNOSTICS, workspace="me"):
    response = create(client, name, stages, workspace)
    assert response.status_code == 201, response.content
    return response.json()


def current_stages(body):
    return [
        {"id": s["id"], "name": s["name"], "type": s["type"], "probability": s["probability"]}
        for s in body["stages"]
        if s["is_active"]
    ]


def as_input(rows):
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "type": r["type"],
            **({"probability": r["probability"]} if r["type"] in ("open", "negotiation") else {}),
        }
        for r in rows
    ]


def put_stages(client, body, rows, workspace="me", version=None):
    return client.put(
        pipeline_url(body["id"], workspace, "stages"),
        {"version": body["version"] if version is None else version, "stages": rows},
        format="json",
    )


class TestOwnership:
    def test_a_user_creates_several_pipelines_of_their_own(self, user_a, user_a_client):
        for name in ("Diagnostics Sales", "Government Tender", "Replacement Business"):
            created(user_a_client, name)
        listed = user_a_client.get(pipelines_url()).json()["results"]
        names = [p["name"] for p in listed]
        assert names[0] == "Sales Pipeline"  # the shared default first
        assert set(names) == {
            "Sales Pipeline",
            "Diagnostics Sales",
            "Government Tender",
            "Replacement Business",
        }
        own = [p for p in listed if p["owner"] is not None]
        assert {p["owner"]["id"] for p in own} == {str(user_a.pk)}
        assert all(p["can_manage"] for p in own)
        shared = next(p for p in listed if p["owner"] is None)
        assert shared["can_manage"] is False

    def test_the_number_and_names_of_stages_are_the_creators_choice(self, user_a_client):
        seven = created(user_a_client, "Diagnostics Sales")
        four = created(user_a_client, "Small deals", SHORT)
        assert [s["name"] for s in seven["stages"]] == [s["name"] for s in DIAGNOSTICS]
        assert [s["type"] for s in four["stages"]] == ["open", "open", "negotiation", "won"]
        assert [s["position"] for s in four["stages"]] == [10, 20, 30, 40]

    def test_another_user_can_neither_see_nor_change_it(self, user_a_client, user_b):
        rahuls = created(user_a_client, "Government Tender")
        priya = signed_in(user_b)
        assert all(p["id"] != rahuls["id"] for p in priya.get(pipelines_url()).json()["results"])
        assert priya.get(pipeline_url(rahuls["id"])).status_code == 404
        patch = priya.patch(
            pipeline_url(rahuls["id"]), {"version": 1, "name": "Mine"}, format="json"
        )
        assert patch.status_code == 404
        assert put_stages(priya, rahuls, SHORT).status_code == 404
        assert (
            priya.post(pipeline_url(rahuls["id"], action="archive"), {"version": 1}, format="json")
        ).status_code == 404
        board = priya.get(board_url(), {"pipeline": rahuls["id"]})
        assert board.status_code == 400  # the same answer as for an id that doesn't exist
        # ...nor create one in Rahul's workspace
        assert create(priya, "Sneaky", workspace=str(rahuls["owner"]["id"])).status_code == 404
        assert Pipeline.objects.get(pk=rahuls["id"]).name == "Government Tender"

    def test_nobody_chooses_an_owner_or_a_key(self, user_a_client, user_b):
        for extra in ({"owner": str(user_b.pk)}, {"key": "mine"}, {"is_default": True}):
            assert create(user_a_client, "X", **extra).status_code == 400

    def test_an_administrator_manages_a_users_pipeline_in_their_workspace(
        self, user_a, user_a_client, admin_client, admin
    ):
        rahuls = created(user_a_client, "Diagnostics Sales")
        ws = str(user_a.pk)
        response = admin_client.patch(
            pipeline_url(rahuls["id"], ws), {"version": 1, "name": "Diagnostics"}, format="json"
        )
        assert response.status_code == 200, response.content
        event = AuditEvent.objects.get(action="pipeline.renamed")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)
        # organisation-wide too
        body = response.json()
        rows = current_stages(body)
        rows[0]["name"] = "Fresh"
        assert put_stages(admin_client, body, as_input(rows), workspace="all").status_code == 200

    def test_an_administrator_creates_a_pipeline_for_a_user(self, user_a, admin_client, admin):
        body = created(admin_client, "Reagent Contracts", SHORT, workspace=str(user_a.pk))
        pipeline = Pipeline.objects.get(pk=body["id"])
        assert (pipeline.owner_id, pipeline.created_by_id) == (user_a.pk, admin.pk)
        assert signed_in(user_a).get(pipeline_url(body["id"])).status_code == 200

    def test_shared_pipelines_are_made_and_configured_organisation_wide(
        self, admin_client, user_a_client, user_b
    ):
        shared = created(admin_client, "Tenders", SHORT, workspace="all")
        assert shared["owner"] is None
        for client in (user_a_client, signed_in(user_b)):
            listed = client.get(pipelines_url()).json()["results"]
            assert any(p["id"] == shared["id"] for p in listed)
        response = user_a_client.patch(
            pipeline_url(shared["id"]), {"version": 1, "name": "Mine"}, format="json"
        )
        assert response.status_code == 403
        # an administrator configures it from the organisation-wide workspace only
        in_own = admin_client.patch(
            pipeline_url(shared["id"]), {"version": 1, "name": "Tender bids"}, format="json"
        )
        assert in_own.status_code == 403
        in_org = admin_client.patch(
            pipeline_url(shared["id"], "all"), {"version": 1, "name": "Tender bids"}, format="json"
        )
        assert in_org.status_code == 200

    def test_a_sales_user_cant_create_a_shared_pipeline(self, user_a_client):
        assert create(user_a_client, "Org", workspace="all").status_code == 404

    def test_a_view_only_role_configures_nothing_outside_its_own_workspace(
        self, monkeypatch, admin, user_a, user_a_client
    ):
        rahuls = created(user_a_client, "Diagnostics Sales")
        monkeypatch.setitem(
            ROLE_CAPABILITIES,
            Role.ADMIN,
            frozenset(
                {Capability.CRM_ACCESS_OWN, Capability.CRM_VIEW_ALL, Capability.WORKSPACE_VIEW_ANY}
            ),
        )
        viewer = signed_in(admin)
        ws = str(user_a.pk)
        assert viewer.get(pipeline_url(rahuls["id"], ws)).status_code == 200
        assert viewer.get(pipeline_url(rahuls["id"], ws)).json()["can_manage"] is False
        response = viewer.patch(
            pipeline_url(rahuls["id"], ws), {"version": 1, "name": "X"}, format="json"
        )
        assert response.status_code == 403
        assert create(viewer, "X", workspace=ws).status_code == 403

    def test_names_are_unique_per_owner_case_insensitively(self, user_a_client, user_b):
        created(user_a_client, "Tender")
        duplicate = create(user_a_client, "tender")
        assert duplicate.status_code == 400
        assert "name" in duplicate.json()["error"]["details"]
        assert create(signed_in(user_b), "Tender").status_code == 201  # another owner may

    def test_each_owner_has_a_bounded_number_of_pipelines(self, user_a_client):
        for n in range(m.MAX_PIPELINES_PER_OWNER):
            created(user_a_client, f"P{n}", SHORT)
        assert create(user_a_client, "One too many", SHORT).status_code == 422

    def test_no_pipeline_for_a_deactivated_user(self, admin_client):
        gone = UserFactory(is_active=False)
        assert create(admin_client, "X", workspace=str(gone.pk)).status_code == 422


class TestStageValidation:
    @pytest.mark.parametrize(
        ("stages", "field"),
        [
            ([stage("Won", "won")], "stages"),  # no open stage
            ([stage("A"), stage("a")], "stages[1].name"),  # duplicate, case-insensitively
            ([stage("A"), {"name": "B", "type": "open"}], "stages[1].probability"),
            (
                [stage("A"), {"name": "W", "type": "won", "probability": "50"}],
                "stages[1].probability",
            ),
            ([stage("A"), stage("<b>B</b>")], "stages[1].name"),
            ([stage("A"), stage("   ")], "stages[1].name"),
            ([stage("A", probability="100.5")], "stages[0].probability"),
            ([stage("A"), {"name": "B", "type": "maybe"}], "type"),
        ],
    )
    def test_refused(self, user_a_client, stages, field):
        response = create(user_a_client, "X", stages)
        assert response.status_code == 400, response.content
        assert field in str(response.json()["error"]["details"])

    def test_at_most_twenty_stages(self, user_a_client):
        many = [stage(f"S{n}") for n in range(m.MAX_STAGES_PER_PIPELINE + 1)]
        assert create(user_a_client, "X", many).status_code == 400
        assert create(user_a_client, "Y", many[:-1]).status_code == 201

    def test_unknown_keys_are_refused(self, user_a_client):
        rows = [{**stage("A"), "category": "won"}]
        assert create(user_a_client, "X", rows).status_code == 400
        rows = [{**stage("A"), "is_active": False}]
        assert create(user_a_client, "Y", rows).status_code == 400


class TestStageEditing:
    def test_rename_reprobability_retype_reorder_and_add_in_one_versioned_change(
        self, user_a_client
    ):
        body = created(user_a_client, "Diagnostics Sales")
        before = current_stages(body)
        keys = {s["id"]: s["key"] for s in body["stages"]}
        rows = as_input(before)
        rows[1]["name"] = "Qualified lead"
        rows[2]["probability"] = "45.50"
        rows[4]["type"] = "negotiation"
        rows = [rows[1], rows[0], *rows[2:]]  # swap the first two
        rows.insert(3, stage("Trial", "open", "55"))
        response = put_stages(user_a_client, body, rows)
        assert response.status_code == 200, response.content
        after = response.json()
        assert after["version"] == body["version"] + 1
        active = [s for s in after["stages"] if s["is_active"]]
        assert [s["name"] for s in active] == [
            "Qualified lead",
            "New",
            "Demo",
            "Trial",
            "Negotiation",
            "PO Received",
            "Won",
            "Lost",
        ]
        assert [s["position"] for s in active] == [10, 20, 30, 40, 50, 60, 70, 80]
        demo = next(s for s in active if s["name"] == "Demo")
        assert (demo["probability"], demo["key"]) == ("45.50", keys[demo["id"]])  # key kept
        po = next(s for s in active if s["name"] == "PO Received")
        assert (po["type"], po["is_negotiation"]) == ("negotiation", True)
        event = AuditEvent.objects.get(action="pipeline.stages_changed")
        assert len(event.metadata["added"]) == 1
        assert "Qualified lead" not in str(event.metadata)  # keys, not names

    def test_names_can_be_swapped(self, user_a_client):
        body = created(user_a_client, "X", SHORT)
        rows = as_input(current_stages(body))
        rows[0]["name"], rows[1]["name"] = rows[1]["name"], rows[0]["name"]
        assert put_stages(user_a_client, body, rows).status_code == 200

    def test_a_stale_version_is_a_conflict(self, user_a_client):
        body = created(user_a_client, "X", SHORT)
        rows = as_input(current_stages(body))
        assert put_stages(user_a_client, body, rows).status_code == 200
        assert put_stages(user_a_client, body, rows).status_code == 409

    def test_a_stage_of_another_pipeline_is_unknown(self, user_a_client):
        one = created(user_a_client, "One", SHORT)
        two = created(user_a_client, "Two", SHORT)
        rows = as_input(current_stages(one))
        rows[0]["id"] = two["stages"][0]["id"]
        response = put_stages(user_a_client, one, rows)
        assert response.status_code == 400

    def test_an_unused_stage_is_deleted(self, user_a_client):
        body = created(user_a_client, "X", DIAGNOSTICS)
        rows = [r for r in as_input(current_stages(body)) if r["name"] != "Demo"]
        response = put_stages(user_a_client, body, rows)
        assert response.status_code == 200
        assert not Stage.objects.filter(pipeline_id=body["id"], name="Demo").exists()

    def test_a_stage_holding_opportunities_cant_be_removed(self, user_a, user_a_client):
        body = created(user_a_client, "X", DIAGNOSTICS)
        demo = Stage.objects.get(pipeline_id=body["id"], name="Demo")
        OpportunityFactory(lead=LeadFactory(owner=user_a), stage=demo)
        rows = [r for r in as_input(current_stages(body)) if r["name"] != "Demo"]
        response = put_stages(user_a_client, body, rows)
        assert response.status_code == 422
        assert "Demo" in response.json()["error"]["message"]
        assert "1 opportunity" in response.json()["error"]["message"]
        demo.refresh_from_db()
        assert demo.is_active

    def test_a_used_but_empty_stage_is_archived_and_history_survives(self, user_a, user_a_client):
        body = created(user_a_client, "X", DIAGNOSTICS)
        demo = Stage.objects.get(pipeline_id=body["id"], name="Demo")
        po = Stage.objects.get(pipeline_id=body["id"], name="PO Received")
        lead = LeadFactory(owner=user_a)
        opp = OpportunityFactory(lead=lead, stage=demo)
        moved = user_a_client.post(
            opportunity_url(opp.pk, action="move"),
            {"stage": str(po.pk), "version": 1},
            format="json",
        )
        assert moved.status_code == 200, moved.content
        archived = OpportunityFactory(lead=lead, stage=demo, archived_at="2026-01-01T00:00:00Z")
        rows = [r for r in as_input(current_stages(body)) if r["name"] != "Demo"]
        response = put_stages(user_a_client, body, rows)
        assert response.status_code == 200, response.content
        demo.refresh_from_db()
        assert (demo.is_active, demo.position >= 10000) == (False, True)
        # history keeps the name it recorded; the archived opportunity keeps its stage
        assert StageHistory.objects.filter(opportunity=opp, from_stage_name="Demo").exists()
        assert Opportunity.objects.get(pk=archived.pk).stage_id == demo.pk
        # the name can be used again by a new stage
        rows = as_input([s for s in response.json()["stages"] if s["is_active"]])
        rows.insert(1, stage("Demo", "open", "30"))
        again = put_stages(user_a_client, response.json(), rows)
        assert again.status_code == 200, again.content

    def test_a_stage_with_opportunities_cant_change_between_open_won_and_lost(
        self, user_a, user_a_client
    ):
        body = created(user_a_client, "X", DIAGNOSTICS)
        demo = Stage.objects.get(pipeline_id=body["id"], name="Demo")
        OpportunityFactory(lead=LeadFactory(owner=user_a), stage=demo)
        rows = as_input(current_stages(body))
        demo_row = next(r for r in rows if r["id"] == str(demo.pk))
        demo_row["type"] = "won"
        demo_row.pop("probability")
        response = put_stages(user_a_client, body, rows)
        assert response.status_code == 422
        # open <-> negotiation keeps the category: allowed
        demo_row["type"] = "negotiation"
        demo_row["probability"] = "50"
        assert put_stages(user_a_client, body, rows).status_code == 200

    def test_won_and_lost_follow_the_type_never_the_name(self, user_a, user_a_client):
        stages = [
            stage("Prospect", "open", "10"),
            stage("Won", "open", "90"),  # called Won, but an open stage
            stage("PO received", "won"),  # the real won stage
            stage("Dropped", "lost"),
        ]
        body = created(user_a_client, "Labels", stages)
        by_name = {s["name"]: s["id"] for s in body["stages"]}
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            opportunities_url(),
            {
                "lead": str(lead.pk),
                "value": "100000",
                "pipeline": body["id"],
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        opp = response.json()
        moved = user_a_client.post(
            opportunity_url(opp["id"], action="move"),
            {"stage": by_name["Won"], "version": opp["version"]},
            format="json",
        ).json()
        assert (moved["status"], moved["closed_at"]) == ("open", None)
        closed = user_a_client.post(
            opportunity_url(opp["id"], action="move"),
            {"stage": by_name["PO received"], "version": moved["version"]},
            format="json",
        ).json()
        assert (closed["status"], closed["probability"]) == ("won", "100.00")
        assert closed["closed_at"] is not None


class TestProbabilityAndTotals:
    def test_weighted_pipeline_uses_the_stage_probability_whatever_its_name(
        self, user_a, user_a_client
    ):
        stages = [
            stage("New", "open", "10"),
            stage("Commercial discussion", "negotiation", "60"),
            stage("Won", "won"),
        ]
        body = created(user_a_client, "Renamed", stages)
        commercial = next(s for s in body["stages"] if s["name"] == "Commercial discussion")
        lead = LeadFactory(owner=user_a)
        response = user_a_client.post(
            opportunities_url(),
            {
                "lead": str(lead.pk),
                "value": "100000",
                "pipeline": body["id"],
                "stage": commercial["id"],
                "negotiated_price": "95000",
            },
            format="json",
        )
        assert response.status_code == 201, response.content
        assert response.json()["weighted_value"] == "60000.00"
        OpportunityFactory(lead=lead, stage_key="proposal", value=Decimal("200000"))  # 50 %
        summary = user_a_client.get("/api/v1/workspaces/me/pipeline-summary").json()["totals"]
        # every authorized pipeline by default: 1,00,000 + 2,00,000; 60,000 + 1,00,000
        assert summary == {
            "pipeline_value": "300000.00",
            "weighted_pipeline": "160000.00",
            "open_count": 2,
        }
        only = user_a_client.get(
            "/api/v1/workspaces/me/pipeline-summary", {"pipeline": body["id"]}
        ).json()["totals"]
        assert only["pipeline_value"] == "100000.00"
        dashboard = user_a_client.get("/api/v1/workspaces/me/dashboard").json()
        assert dashboard["pipeline"]["pipeline_value"] == "300000.00"

    def test_a_new_default_probability_applies_to_deals_entering_later(self, user_a, user_a_client):
        body = created(user_a_client, "X", SHORT)
        lead_stage = Stage.objects.get(pipeline_id=body["id"], name="Lead")
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=lead_stage)
        rows = as_input(current_stages(body))
        rows[0]["probability"] = "20"
        assert put_stages(user_a_client, body, rows).status_code == 200
        opp.refresh_from_db()
        assert opp.probability == Decimal("10.00")  # adopted when it entered; not rewritten


class TestArchive:
    def test_archive_and_restore(self, user_a, user_a_client):
        body = created(user_a_client, "Old", SHORT)
        response = user_a_client.post(
            pipeline_url(body["id"], action="archive"), {"version": body["version"]}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        listed = user_a_client.get(pipelines_url()).json()["results"]
        assert all(p["id"] != body["id"] for p in listed)
        archived = user_a_client.get(pipelines_url(), {"archived": "true"}).json()["results"]
        assert [p["id"] for p in archived] == [body["id"]]
        lead = LeadFactory(owner=user_a)
        refused = user_a_client.post(
            opportunities_url(),
            {"lead": str(lead.pk), "value": "1", "pipeline": body["id"]},
            format="json",
        )
        assert refused.status_code == 400
        restored = user_a_client.post(
            pipeline_url(body["id"], action="restore"),
            {"version": response.json()["version"]},
            format="json",
        )
        assert restored.status_code == 200
        assert restored.json()["is_active"] is True

    def test_a_pipeline_with_open_deals_isnt_archived(self, user_a, user_a_client):
        body = created(user_a_client, "Busy", SHORT)
        first = Stage.objects.get(pipeline_id=body["id"], name="Lead")
        OpportunityFactory(lead=LeadFactory(owner=user_a), stage=first)
        response = user_a_client.post(
            pipeline_url(body["id"], action="archive"), {"version": 1}, format="json"
        )
        assert response.status_code == 422
        assert "1 open opportunity" in response.json()["error"]["message"]

    def test_the_default_pipeline_isnt_archived(self, admin_client, pipeline):
        response = admin_client.post(
            pipeline_url(pipeline.pk, "all", "archive"), {"version": 1}, format="json"
        )
        assert response.status_code == 422

    def test_a_closed_deal_in_an_archived_pipeline_cant_be_reopened(self, user_a, user_a_client):
        body = created(user_a_client, "Done", SHORT)
        won = Stage.objects.get(pipeline_id=body["id"], name="Closed")
        opp = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=won)
        user_a_client.post(
            pipeline_url(body["id"], action="archive"), {"version": 1}, format="json"
        )
        first = Stage.objects.get(pipeline_id=body["id"], name="Lead")
        response = user_a_client.post(
            opportunity_url(opp.pk, action="move"),
            {"stage": str(first.pk), "version": 1},
            format="json",
        )
        assert response.status_code == 422


class TestDealsAfterReassignment:
    def test_a_deal_in_its_previous_owners_pipeline_stays_visible_but_not_configurable(
        self, user_a, user_a_client, user_b, admin
    ):
        rahuls = created(user_a_client, "Diagnostics Sales", SHORT)
        first = Stage.objects.get(pipeline_id=rahuls["id"], name="Lead")
        lead = LeadFactory(owner=user_a)
        opp = OpportunityFactory(lead=lead, stage=first)
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        priya = signed_in(user_b)
        listed = {p["id"]: p for p in priya.get(pipelines_url()).json()["results"]}
        assert listed[rahuls["id"]]["can_manage"] is False
        board = priya.get(board_url(), {"pipeline": rahuls["id"]}).json()
        cards = [c["id"] for col in board["columns"] for c in col["cards"]]
        assert cards == [str(opp.pk)]
        second = Stage.objects.get(pipeline_id=rahuls["id"], name="Discussion")
        moved = priya.post(
            opportunity_url(opp.pk, action="move"),
            {"stage": str(second.pk), "version": Opportunity.objects.get(pk=opp.pk).version},
            format="json",
        )
        assert moved.status_code == 200, moved.content
        assert (
            priya.patch(
                pipeline_url(rahuls["id"]), {"version": 1, "name": "Mine"}, format="json"
            ).status_code
            == 403
        )

    def test_a_deal_is_created_only_in_a_shared_pipeline_or_its_owners_own(
        self, user_a, user_b, admin_client
    ):
        priyas = created(signed_in(user_b), "Priya's", SHORT)
        lead = LeadFactory(owner=user_a)
        response = admin_client.post(
            opportunities_url("all"),
            {"lead": str(lead.pk), "value": "1", "pipeline": priyas["id"]},
            format="json",
        )
        assert response.status_code == 400
        assert "pipeline" in response.json()["error"]["details"]


class TestReviewRegressions:
    """Enhancement review findings, each pinned."""

    def specs(self, pipeline):
        return [
            {
                "id": s.pk,
                "name": s.name,
                "type": s.stage_type,
                "probability": s.probability if s.category == "open" else None,
            }
            for s in pipeline.stages.filter(is_active=True).order_by("position")
        ]

    def pipeline(self, owner):
        from arkray.pipeline import configuration

        return configuration.create_pipeline(
            actor=owner,
            scope=AccessScope.own(owner.pk),
            name="Tenders",
            stages=[
                {"name": "A", "type": "open", "probability": Decimal("10")},
                {"name": "B", "type": "open", "probability": Decimal("30")},
                {"name": "N", "type": "negotiation", "probability": Decimal("70")},
                {"name": "Won", "type": "won"},
            ],
        )

    def test_an_archived_deal_whose_stage_was_removed_is_not_restored_into_it(self):
        """P2: restoring made a current deal of a retired stage (off the board)."""
        from arkray.core.errors import BusinessRuleViolation
        from arkray.pipeline import configuration, services

        owner = UserFactory()
        own = AccessScope.own(owner.pk)
        pipeline = self.pipeline(owner)
        b = pipeline.stages.get(name="B")
        opportunity = OpportunityFactory(lead=LeadFactory(owner=owner), stage=b)
        opportunity = services.archive_opportunity(
            actor=owner, scope=own, opportunity_id=opportunity.pk, version=opportunity.version
        )
        pipeline.refresh_from_db()
        configuration.replace_stages(
            actor=owner,
            scope=own,
            pipeline_id=pipeline.pk,
            version=pipeline.version,
            stages=[s for s in self.specs(pipeline) if s["name"] != "B"],
        )
        assert not Stage.objects.get(pk=b.pk).is_active  # retired: referenced
        with pytest.raises(BusinessRuleViolation, match="stage was removed"):
            services.restore_opportunity(
                actor=owner, scope=own, opportunity_id=opportunity.pk, version=opportunity.version
            )
        assert not Opportunity.objects.filter(
            archived_at__isnull=True, stage__is_active=False
        ).exists()

    def test_a_stage_retyped_to_negotiation_records_its_first_price_even_if_unchanged(self):
        """P3: the "same price as the latest is a retry" shortcut skipped the history row
        for a deal whose stage became a negotiation stage while it sat there."""
        from arkray.pipeline import configuration, services
        from arkray.pipeline.models import NegotiationPrice

        owner = UserFactory()
        own = AccessScope.own(owner.pk)
        pipeline = self.pipeline(owner)
        a, b, n = (pipeline.stages.get(name=x) for x in "ABN")
        opportunity = OpportunityFactory(lead=LeadFactory(owner=owner), stage=a)
        opportunity = services.move_opportunity(
            actor=owner,
            scope=own,
            opportunity_id=opportunity.pk,
            version=opportunity.version,
            stage_id=n.pk,
            negotiated_price=Decimal("100"),
        )
        opportunity = services.move_opportunity(
            actor=owner,
            scope=own,
            opportunity_id=opportunity.pk,
            version=opportunity.version,
            stage_id=b.pk,
        )
        pipeline.refresh_from_db()
        specs = self.specs(pipeline)
        for spec in specs:
            if spec["name"] == "B":
                spec["type"] = "negotiation"
        configuration.replace_stages(
            actor=owner, scope=own, pipeline_id=pipeline.pk, version=pipeline.version, stages=specs
        )
        services.record_negotiated_price(
            actor=owner,
            scope=own,
            opportunity_id=opportunity.pk,
            version=opportunity.version,
            price=Decimal("100"),
        )
        latest = NegotiationPrice.objects.filter(opportunity_id=opportunity.pk).latest("id")
        assert (latest.stage_id, latest.price) == (b.pk, Decimal("100.00"))
        # ...and a retry of that is still a harmless no-op.
        count = NegotiationPrice.objects.filter(opportunity_id=opportunity.pk).count()
        services.record_negotiated_price(
            actor=owner,
            scope=own,
            opportunity_id=opportunity.pk,
            version=opportunity.version,
            price=Decimal("100"),
        )
        assert NegotiationPrice.objects.filter(opportunity_id=opportunity.pk).count() == count
