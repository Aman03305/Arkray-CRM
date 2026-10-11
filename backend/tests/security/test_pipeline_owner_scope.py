"""Configuring a pipeline is not authority over the deals in it (final production audit SEC-1,
SEC-1b; pipeline.configuration, pipeline.field_values).

A deal reassigned with its lead stays in its old owner's personal pipeline, out of their
sight. The old owner (or anyone working in their workspace: an administrator there, or in a
support session) may still configure the pipeline, but:

- deleting a removed field's values is refused, as a whole, while a deal outside their scope
  holds one (nothing archived, nothing queued, nothing ledgered);
- the job deletes only the workspace owner's values, so a deal reassigned between the
  request and the job keeps its value, and so does a replay of the job's ledger entry;
- refusals count only the deals the caller can see.

Organisation-wide, an administrator still deletes every deal's values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from arkray.audit.models import AuditEvent
from arkray.core import ledger
from arkray.core.access import AccessScope
from arkray.core.errors import PermissionDeniedError
from arkray.core.ledger_gate import GATE
from arkray.core.models import HoldSubject, LegalHold, OutboxEvent
from arkray.leads import services as lead_services
from arkray.pipeline import configuration, field_values
from arkray.pipeline.models import CustomField, Opportunity, Pipeline, Stage
from arkray.privacy import replay
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

PRIYAS = "Priya's private tender note 4471"
RAHULS = "Rahul's own note"
KEY = "ledger-test-key-" + "x" * 32


def _value(deal: Opportunity, field: CustomField) -> Any:
    return Opportunity.objects.get(pk=deal.pk).custom_fields.get(str(field.pk))


def _reassign(admin, lead, to) -> None:
    lead.refresh_from_db()
    lead_services.reassign_lead(
        actor=admin,
        scope=AccessScope.organization(admin.pk),
        lead_id=lead.pk,
        version=lead.version,
        owner_id=to.pk,
    )


@pytest.fixture
def tenders(user_a, user_b, admin):
    """Rahul's personal pipeline: his open deal, his closed (won) deal, and a deal an
    administrator has since reassigned, with its lead, to Priya. Each holds a value of the
    field "Contact notes"."""
    rahul = signed_in(user_a)
    created = rahul.post(
        "/api/v1/workspaces/me/pipelines",
        {
            "name": "Tenders",
            "stages": [
                {"name": "Qualify", "type": "open", "probability": "10"},
                {"name": "Bid", "type": "open", "probability": "40"},
                {"name": "Won", "type": "won"},
                {"name": "Lost", "type": "lost"},
            ],
            "custom_fields": [{"name": "Contact notes", "type": "text"}],
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    pipeline = Pipeline.objects.get(pk=created.json()["id"])
    field = CustomField.objects.get(pipeline=pipeline, name="Contact notes")
    qualify = Stage.objects.get(pipeline=pipeline, name="Qualify")
    won = Stage.objects.get(pipeline=pipeline, name="Won")

    def deal(stage, value):
        made = OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stage)
        Opportunity.objects.filter(pk=made.pk).update(custom_fields={str(field.pk): value})
        return made

    own, closed, moved = deal(qualify, RAHULS), deal(won, RAHULS), deal(qualify, PRIYAS)
    _reassign(admin, moved.lead, user_b)
    moved.refresh_from_db()
    assert moved.owner_id == user_b.pk
    assert moved.pipeline_id == pipeline.pk
    assert rahul.get(f"/api/v1/workspaces/me/opportunities/{moved.pk}").status_code == 404
    pipeline.refresh_from_db()
    return {
        "client": rahul,
        "pipeline": pipeline,
        "field": field,
        "qualify": qualify,
        "own": own,
        "closed": closed,
        "moved": moved,
    }


def _remove_fields(client, pipeline: Pipeline, **extra: Any) -> Any:
    pipeline.refresh_from_db()
    return client.put(
        f"/api/v1/workspaces/{extra.pop('workspace', 'me')}/pipelines/{pipeline.pk}/fields",
        {"version": pipeline.version, "custom_fields": [], **extra},
        format="json",
    )


def _unchanged(t: dict[str, Any], version: int) -> None:
    t["pipeline"].refresh_from_db()
    assert t["pipeline"].version == version
    assert CustomField.objects.get(pk=t["field"].pk).is_active
    assert not OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).exists()
    assert not AuditEvent.objects.filter(action=field_values.AUDIT_REQUESTED).exists()
    assert _value(t["moved"], t["field"]) == PRIYAS
    assert _value(t["own"], t["field"]) == RAHULS


# --- refused as a whole ---------------------------------------------------------------------
def test_the_old_owner_cannot_delete_values_of_a_deal_reassigned_away(tenders):
    version = tenders["pipeline"].version
    response = _remove_fields(tenders["client"], tenders["pipeline"], delete_removed_values=True)
    drain_outbox()
    assert response.status_code == 403, response.content
    assert "belong to other users" in response.json()["error"]["message"]
    _unchanged(tenders, version)


def test_nor_later_through_the_confirmed_deletion_of_a_removed_field(tenders):
    client, pipeline, field = tenders["client"], tenders["pipeline"], tenders["field"]
    assert _remove_fields(client, pipeline).status_code == 200  # archived, values kept
    response = client.post(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/fields/{field.pk}/delete-values",
        {"confirm_name": field.name},
        format="json",
    )
    drain_outbox()
    assert response.status_code == 403, response.content
    assert not OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES).exists()
    assert _value(tenders["moved"], field) == PRIYAS
    assert _value(tenders["own"], field) == RAHULS


def test_an_administrator_in_the_old_owners_workspace_is_refused_too(tenders, admin, user_a):
    version = tenders["pipeline"].version
    response = _remove_fields(
        signed_in(admin), tenders["pipeline"], workspace=user_a.pk, delete_removed_values=True
    )
    assert response.status_code == 403, response.content
    _unchanged(tenders, version)


def test_an_administrator_in_a_support_session_is_refused_too(tenders, admin, user_a):
    client = signed_in(admin)
    started = client.post("/api/v1/admin/support-sessions", {"user": str(user_a.pk)}, format="json")
    assert started.status_code in (200, 201), started.content
    version = tenders["pipeline"].version
    response = _remove_fields(
        client, tenders["pipeline"], workspace=user_a.pk, delete_removed_values=True
    )
    assert response.status_code == 403, response.content
    _unchanged(tenders, version)


# --- what stays allowed ---------------------------------------------------------------------
def test_removing_without_deleting_stays_allowed_and_keeps_every_value(tenders):
    assert _remove_fields(tenders["client"], tenders["pipeline"]).status_code == 200
    drain_outbox()
    assert not CustomField.objects.get(pk=tenders["field"].pk).is_active
    assert _value(tenders["moved"], tenders["field"]) == PRIYAS
    assert _value(tenders["own"], tenders["field"]) == RAHULS


def test_with_only_their_own_deals_the_owner_deletes_open_and_closed_values(tenders, admin, user_a):
    _reassign(admin, tenders["moved"].lead, user_a)  # back to Rahul: nothing foreign left
    response = _remove_fields(tenders["client"], tenders["pipeline"], delete_removed_values=True)
    assert response.status_code == 200, response.content
    drain_outbox()
    for deal in ("own", "closed", "moved"):
        assert _value(tenders[deal], tenders["field"]) is None


def test_organisation_wide_an_administrator_deletes_every_deals_values(tenders, admin):
    response = _remove_fields(
        signed_in(admin), tenders["pipeline"], workspace="all", delete_removed_values=True
    )
    assert response.status_code == 200, response.content
    drain_outbox()
    for deal in ("own", "closed", "moved"):
        assert _value(tenders[deal], tenders["field"]) is None


def test_a_legal_hold_still_keeps_its_deals_values(tenders, admin):
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD,
        subject_id=tenders["closed"].lead_id,
        reference="CASE-7",
        placed_by=admin.pk,
    )
    response = _remove_fields(
        signed_in(admin), tenders["pipeline"], workspace="all", delete_removed_values=True
    )
    assert response.status_code == 200
    drain_outbox()
    assert _value(tenders["closed"], tenders["field"]) == RAHULS
    assert _value(tenders["own"], tenders["field"]) is None


# --- races: the job only touches the requester's workspace ---------------------------------
@pytest.fixture
def ledger_dir(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    yield directory
    GATE.reset()


def test_a_deal_reassigned_after_the_request_keeps_its_value_and_replay_agrees(
    tenders, admin, user_a, user_b, ledger_dir: Path
):
    _reassign(admin, tenders["moved"].lead, user_a)  # the request is allowed...
    response = _remove_fields(tenders["client"], tenders["pipeline"], delete_removed_values=True)
    assert response.status_code == 200, response.content
    _reassign(admin, tenders["own"].lead, user_b)  # ...then a deal leaves before the job
    (event,) = OutboxEvent.objects.filter(topic=field_values.TOPIC_PURGE_VALUES)
    assert event.payload["owner_id"] == str(user_a.pk)
    drain_outbox()
    field = tenders["field"]
    assert _value(tenders["own"], field) == RAHULS  # Priya's now: kept
    assert _value(tenders["moved"], field) is None
    assert _value(tenders["closed"], field) is None
    (entry,) = [json.loads(p.read_text()) for p in sorted(ledger_dir.glob("*.json"))]
    assert (entry["kind"], entry["owner"]) == ("custom_values_deleted", str(user_a.pk))

    # A restore brings the deleted values back; replay deletes them on Rahul's deals only.
    for deal in ("moved", "closed"):
        Opportunity.objects.filter(pk=tenders[deal].pk).update(
            custom_fields={str(field.pk): RAHULS}
        )
    ledger.mark_applied(None)
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert _value(tenders["moved"], field) is None
    assert _value(tenders["closed"], field) is None
    assert _value(tenders["own"], field) == RAHULS


def test_a_job_queued_before_the_owner_filter_keeps_its_old_reach(tenders, admin):
    """Queued by an older release (no owner in the payload): organisation-wide, as before."""
    field = tenders["field"]
    CustomField.objects.filter(pk=field.pk).update(is_active=False)
    field_values.purge_values({"field_id": str(field.pk)})
    for deal in ("own", "closed", "moved"):
        assert _value(tenders[deal], field) is None


def test_the_service_refuses_before_changing_anything(tenders, user_a):
    pipeline = tenders["pipeline"]
    with pytest.raises(PermissionDeniedError):
        configuration.replace_fields(
            actor=user_a,
            scope=AccessScope.own(user_a.pk),
            pipeline_id=pipeline.pk,
            version=pipeline.version,
            fields=[],
            delete_removed_values=True,
        )
    _unchanged(tenders, pipeline.version)


# --- SEC-1b: refusals count only what the caller can see ------------------------------------
def _stages_without(pipeline: Pipeline, name: str) -> list[dict[str, Any]]:
    return [
        {"id": str(s.pk), "name": s.name, "type": s.stage_type, "probability": str(s.probability)}
        if s.category == "open"
        else {"id": str(s.pk), "name": s.name, "type": s.stage_type}
        for s in pipeline.stages.filter(is_active=True).order_by("position")
        if s.name != name
    ]


def test_removing_a_stage_holding_only_hidden_deals_is_refused_without_a_count(
    tenders, admin, user_b
):
    client, pipeline = tenders["client"], tenders["pipeline"]
    _reassign(admin, tenders["own"].lead, user_b)  # Qualify now holds only Priya's deals
    pipeline.refresh_from_db()
    response = client.put(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/stages",
        {"version": pipeline.version, "stages": _stages_without(pipeline, "Qualify")},
        format="json",
    )
    body = response.content.decode()
    assert response.status_code == 422, body
    assert "belong to other users" in body
    assert "2 opportunities" not in body
    assert "opportunity." not in body


def test_a_stage_refusal_counts_only_the_callers_deals(tenders):
    client, pipeline = tenders["client"], tenders["pipeline"]
    response = client.put(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/stages",
        {"version": pipeline.version, "stages": _stages_without(pipeline, "Qualify")},
        format="json",
    )
    body = response.content.decode()
    assert response.status_code == 422, body
    assert "has 1 opportunity." in body  # Rahul's own; Priya's isn't counted


def test_archiving_counts_only_the_callers_open_deals(tenders, admin, user_b):
    client, pipeline = tenders["client"], tenders["pipeline"]
    response = client.post(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/archive",
        {"version": pipeline.version},
        format="json",
    )
    assert response.status_code == 422
    assert "still has 1 open opportunity" in response.content.decode()
    _reassign(admin, tenders["own"].lead, user_b)
    pipeline.refresh_from_db()
    response = client.post(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/archive",
        {"version": pipeline.version},
        format="json",
    )
    body = response.content.decode()
    assert response.status_code == 422, body
    assert "belong to other users" in body
    assert "2 open" not in body


def test_organisation_wide_the_refusal_counts_every_deal(tenders, admin):
    pipeline = tenders["pipeline"]
    response = signed_in(admin).put(
        f"/api/v1/workspaces/all/pipelines/{pipeline.pk}/stages",
        {"version": pipeline.version, "stages": _stages_without(pipeline, "Qualify")},
        format="json",
    )
    assert response.status_code == 422
    assert "has 2 opportunities" in response.content.decode()


def test_the_replay_command_still_reads_old_entries_without_an_owner(ledger_dir, admin, tenders):
    """An entry written before the owner field existed replays organisation-wide."""
    field = tenders["field"]
    CustomField.objects.filter(pk=field.pk).update(is_active=False)
    from django.db import transaction

    with transaction.atomic():
        ledger.append("custom_values_deleted", field.pk)
    out = Path(ledger_dir.parent / "report.json")
    call_command("replay_erasures", "--by", admin.email, "--report", str(out))
    for deal in ("own", "closed", "moved"):
        assert _value(tenders[deal], field) is None
