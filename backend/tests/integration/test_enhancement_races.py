"""Races on the product enhancement phase's writes, run for real: each call in its own thread
and database connection, released together (docs/pipeline.md#configuration,
#negotiation; docs/activities.md#attachments; docs/admin-user-workspace.md#support-sessions).

Whatever the interleaving: of several changes made from one version exactly one wins and the
others get 409; no stage that was retired or deleted holds a current opportunity; no archived
pipeline holds an open one; negotiated prices, files and support sessions never exceed what
their rules allow; nothing deadlocks."""

from __future__ import annotations

import io
import threading
import time
from decimal import Decimal
from urllib.parse import quote

import pytest
from django.db import connections
from django.test import override_settings

from arkray.activities import services as activity_services
from arkray.activities.models import Attachment
from arkray.core.access import AccessScope
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    PermissionDeniedError,
)
from arkray.identity.models import SupportSession
from arkray.pipeline import configuration, services
from arkray.pipeline.models import NegotiationPrice, Opportunity, Pipeline, Stage
from tests.factories import AdminFactory, LeadFactory, NoteFactory, OpportunityFactory, UserFactory
from tests.helpers import run_concurrently, signed_in

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]

OWN = AccessScope.own
ORG = AccessScope.organization
EXPECTED = (ConflictError, BusinessRuleViolation, InvalidInputError, PermissionDeniedError)
ROUNDS = 6


def outcome(results):
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    for error in failed:
        assert "deadlock" not in str(error).lower(), error
        assert isinstance(error, EXPECTED), repr(error)
    return ok, failed


def stage_specs(pipeline):
    return [
        {
            "id": s.pk,
            "name": s.name,
            "type": s.stage_type,
            "probability": s.probability if s.category == "open" else None,
        }
        for s in pipeline.stages.filter(is_active=True).order_by("position")
    ]


def new_pipeline(owner, actor=None):
    actor = actor or owner
    scope = OWN(owner.pk) if actor == owner else AccessScope.for_user(actor.pk, owner.pk)
    return configuration.create_pipeline(
        actor=actor,
        scope=scope,
        name=f"P{Pipeline.objects.count()}",
        stages=[
            {"name": "A", "type": "open", "probability": Decimal("10")},
            {"name": "B", "type": "open", "probability": Decimal("30")},
            {"name": "N", "type": "negotiation", "probability": Decimal("70")},
            {"name": "Won", "type": "won"},
        ],
    )


def assert_configuration_holds():
    stale = Opportunity.objects.filter(archived_at__isnull=True, stage__is_active=False)
    assert not stale.exists(), list(stale.values_list("pk", "stage__name"))
    archived_open = Opportunity.objects.filter(
        archived_at__isnull=True, status="open", pipeline__is_active=False
    )
    assert not archived_open.exists()


def test_two_people_editing_stages_from_one_version():
    owner, admin = UserFactory(), AdminFactory()
    pipeline = new_pipeline(owner)
    for round_ in range(ROUNDS):
        pipeline.refresh_from_db()
        version = pipeline.version
        mine = stage_specs(pipeline)
        mine[0]["name"] = f"Owner {round_}"
        theirs = stage_specs(pipeline)
        theirs.reverse()  # a reorder (Won first is fine: an open stage remains)
        results = run_concurrently(
            lambda m=mine, v=version: configuration.replace_stages(
                actor=owner, scope=OWN(owner.pk), pipeline_id=pipeline.pk, version=v, stages=m
            ),
            lambda t=theirs, v=version: configuration.replace_stages(
                actor=admin,
                scope=AccessScope.for_user(admin.pk, owner.pk),
                pipeline_id=pipeline.pk,
                version=v,
                stages=t,
            ),
        )
        ok, failed = outcome(results)
        assert len(ok) == 1
        assert isinstance(failed[0], ConflictError)
        pipeline.refresh_from_db()
        assert pipeline.version == version + 1
        positions = list(
            Stage.objects.filter(pipeline=pipeline, is_active=True).values_list(
                "position", flat=True
            )
        )
        assert len(positions) == len(set(positions)) == 4


def test_a_stage_removed_while_a_deal_moves_into_it():
    owner = UserFactory()
    for _ in range(ROUNDS):
        pipeline = new_pipeline(owner)
        first = pipeline.stages.get(name="A")
        target = pipeline.stages.get(name="B")
        opp = OpportunityFactory(lead=LeadFactory(owner=owner), stage=first)
        without_b = [s for s in stage_specs(pipeline) if s["name"] != "B"]
        results = run_concurrently(
            lambda o=opp, t=target: services.move_opportunity(
                actor=owner, scope=OWN(owner.pk), opportunity_id=o.pk, version=1, stage_id=t.pk
            ),
            lambda p=pipeline, s=without_b: configuration.replace_stages(
                actor=owner,
                scope=OWN(owner.pk),
                pipeline_id=p.pk,
                version=p.version,
                stages=s,
            ),
        )
        ok, _ = outcome(results)
        assert len(ok) == 1, results  # one or the other, never both
        assert_configuration_holds()
        opp.refresh_from_db()
        if opp.stage_id == target.pk:  # the move won: the removal was refused
            assert Stage.objects.get(pk=target.pk).is_active
        else:  # the removal won: the move found no such stage
            assert not Stage.objects.filter(pk=target.pk, is_active=True).exists()


def test_a_pipeline_archived_while_a_deal_is_created_in_it():
    owner = UserFactory()
    for _ in range(ROUNDS):
        pipeline = new_pipeline(owner)
        lead = LeadFactory(owner=owner)
        results = run_concurrently(
            lambda p=pipeline, lead_=lead: services.create_opportunity(
                actor=owner,
                scope=OWN(owner.pk),
                lead_id=lead_.pk,
                fields={"value": Decimal("1")},
                pipeline_id=p.pk,
            ),
            lambda p=pipeline: configuration.archive_pipeline(
                actor=owner, scope=OWN(owner.pk), pipeline_id=p.pk, version=p.version
            ),
        )
        ok, _ = outcome(results)
        assert len(ok) == 1, results
        assert_configuration_holds()


def test_two_moves_into_negotiation_record_one_price():
    owner = UserFactory()
    pipeline = new_pipeline(owner)
    first, negotiation = pipeline.stages.get(name="A"), pipeline.stages.get(name="N")
    for n in range(ROUNDS):
        opp = OpportunityFactory(lead=LeadFactory(owner=owner), stage=first)
        results = run_concurrently(
            *(
                lambda o=opp, price=price: services.move_opportunity(
                    actor=owner,
                    scope=OWN(owner.pk),
                    opportunity_id=o.pk,
                    version=1,
                    stage_id=negotiation.pk,
                    negotiated_price=price,
                    agreed_cpt="Rs 18",
                )
                for price in (Decimal("100.00"), Decimal("90.00"), Decimal("80.00"))
            )
        )
        ok, _ = outcome(results)
        # One moved it; the others found it already there (a retry with a price: refused),
        # or at a newer version.
        assert len(ok) == 1, (n, results)
        rows = NegotiationPrice.objects.filter(opportunity_id=opp.pk)
        assert rows.count() == 1
        opp.refresh_from_db()
        assert opp.negotiated_price == rows.get().price


def test_concurrent_price_revisions_from_one_version():
    owner = UserFactory()
    pipeline = new_pipeline(owner)
    negotiation = pipeline.stages.get(name="N")
    opp = OpportunityFactory(lead=LeadFactory(owner=owner), stage=pipeline.stages.get(name="A"))
    services.move_opportunity(
        actor=owner,
        scope=OWN(owner.pk),
        opportunity_id=opp.pk,
        version=1,
        stage_id=negotiation.pk,
        negotiated_price=Decimal("1000"),
        agreed_cpt="Rs 18",
    )
    results = run_concurrently(
        *(
            lambda price=price: services.record_negotiated_price(
                actor=owner,
                scope=OWN(owner.pk),
                opportunity_id=opp.pk,
                version=2,
                price=price,
                agreed_cpt="Rs 18",
            )
            for price in (Decimal("900"), Decimal("800"))
        )
    )
    ok, failed = outcome(results)
    assert len(ok) == 1
    assert isinstance(failed[0], ConflictError)
    assert NegotiationPrice.objects.filter(opportunity_id=opp.pk).count() == 2


def test_custom_values_written_while_their_field_is_removed():
    owner = UserFactory()
    for _ in range(ROUNDS):
        pipeline = new_pipeline(owner)
        pipeline = configuration.replace_fields(
            actor=owner,
            scope=OWN(owner.pk),
            pipeline_id=pipeline.pk,
            version=pipeline.version,
            fields=[{"name": "Tender", "type": "text"}, {"name": "Lab", "type": "text"}],
        )
        tender = pipeline.fields.get(name="Tender")
        lab = pipeline.fields.get(name="Lab")
        opp = OpportunityFactory(lead=LeadFactory(owner=owner), stage=pipeline.stages.get(name="A"))
        results = run_concurrently(
            lambda o=opp, t=tender: services.update_opportunity(
                actor=owner,
                scope=OWN(owner.pk),
                opportunity_id=o.pk,
                version=1,
                changes={"custom_fields": {str(t.pk): "GEM/1"}},
            ),
            lambda p=pipeline, kept=lab: configuration.replace_fields(
                actor=owner,
                scope=OWN(owner.pk),
                pipeline_id=p.pk,
                version=p.version,
                fields=[{"id": kept.pk, "name": "Lab", "type": "text"}],
            ),
        )
        ok, failed = outcome(results)
        opp.refresh_from_db()
        tender.refresh_from_db()
        assert not tender.is_active
        if str(tender.pk) in opp.custom_fields:
            # Written before the removal (kept, hidden): never validated against a definition
            # that a removal had already replaced. The write happened first in time.
            assert len(ok) == 2
            assert opp.updated_at < tender.updated_at, (opp.updated_at, tender.updated_at)
        else:
            assert len(failed) == 1  # refused after it


def test_a_note_edited_by_its_author_and_an_administrator_at_once():
    owner, admin = UserFactory(), AdminFactory()
    note = NoteFactory(lead=LeadFactory(owner=owner), created_by=owner)
    results = run_concurrently(
        lambda: activity_services.update_activity(
            actor=owner,
            scope=OWN(owner.pk),
            activity_id=note.pk,
            version=1,
            changes={"description": "Author's words"},
        ),
        lambda: activity_services.update_activity(
            actor=admin,
            scope=AccessScope.for_user(admin.pk, owner.pk),
            activity_id=note.pk,
            version=1,
            changes={"description": "Admin's correction"},
        ),
    )
    ok, failed = outcome(results)
    assert len(ok) == 1
    assert isinstance(failed[0], ConflictError)
    note.refresh_from_db()
    assert note.edited_by_id == ok[0].edited_by_id
    assert note.version == 2


@override_settings(ATTACHMENT_MAX_PER_NOTE=3)
def test_uploads_at_a_notes_limit():
    owner = UserFactory()
    note = NoteFactory(lead=LeadFactory(owner=owner), created_by=owner)
    clients = [signed_in(owner) for _ in range(6)]

    def upload(client, n):
        return client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            b"file %d" % n,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=quote(f"f{n}.txt"),
        ).status_code

    statuses = run_concurrently(*(lambda c=c, n=n: upload(c, n) for n, c in enumerate(clients)))
    assert sorted(statuses) == [201, 201, 201, 422, 422, 422], statuses
    assert Attachment.objects.filter(note=note, state="stored").count() == 3


def test_an_administrator_starting_support_sessions_in_two_browsers_at_once():
    admin = AdminFactory()
    targets = [UserFactory(), UserFactory()]
    browsers = [signed_in(admin), signed_in(admin)]
    results = run_concurrently(
        *(
            lambda b=b, t=t: (
                b.post(
                    "/api/v1/admin/support-sessions", {"user": str(t.pk)}, format="json"
                ).status_code
            )
            for b, t in zip(browsers, targets, strict=True)
        )
    )
    assert set(results) <= {201, 409}, results
    assert SupportSession.objects.filter(admin=admin, ended_at__isnull=True).count() == 1


def test_a_support_write_and_the_users_own_write_from_one_version():
    owner, admin = UserFactory(), AdminFactory()
    lead = LeadFactory(owner=owner)
    opp = OpportunityFactory(lead=lead)
    support = signed_in(admin)
    assert (
        support.post("/api/v1/admin/support-sessions", {"user": str(owner.pk)}, format="json")
    ).status_code == 201
    mine = signed_in(owner)
    results = run_concurrently(
        lambda: (
            support.patch(
                f"/api/v1/workspaces/{owner.pk}/opportunities/{opp.pk}",
                {"version": 1, "work_load": "From support"},
                format="json",
            ).status_code
        ),
        lambda: (
            mine.patch(
                f"/api/v1/workspaces/me/opportunities/{opp.pk}",
                {"version": 1, "work_load": "From the user"},
                format="json",
            ).status_code
        ),
    )
    assert sorted(results) == [200, 409], results


def test_no_upload_leaves_an_object_without_a_row(tmp_path):
    """The storage key is recorded before the object is written: every object that might
    exist is known to the database (housekeeping can always find it)."""
    from django.core.files.storage import storages

    owner = UserFactory()
    note = NoteFactory(lead=LeadFactory(owner=owner), created_by=owner)
    client = signed_in(owner)
    for n in range(3):
        client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            io.BytesIO(b"x").getvalue(),
            content_type="application/octet-stream",
            HTTP_X_FILENAME=f"{n}.txt",
        )
    keys = set(Attachment.objects.values_list("storage_key", flat=True))
    for key in keys:
        assert storages["attachments"].exists(key)


@pytest.mark.parametrize("edit", ["remove_first", "add_first", "reorder"])
def test_a_stage_list_edit_that_shifts_positions_while_a_deal_moves_forward(monkeypatch, edit):
    """Enhancement review P0: a move holds KEY SHARE on its target stage and then inserts
    history (KEY SHARE on its source stage); an edit shifting positions updated the source
    (FOR UPDATE: position is a unique key) and waited for the target: deadlock, a 500. The
    edit now takes the pipeline FOR UPDATE first, which waits for the move's KEY SHARE on
    the pipeline. The move is paused just before its history insert, the edit started."""
    owner = UserFactory()
    pipeline = new_pipeline(owner)
    source, target = pipeline.stages.get(name="A"), pipeline.stages.get(name="B")
    opportunity = OpportunityFactory(lead=LeadFactory(owner=owner), stage=source)
    specs = stage_specs(pipeline)
    if edit == "remove_first":
        specs = [{"name": "Z", "type": "open", "probability": Decimal("5")}, *specs]
        pipeline = configuration.replace_stages(
            actor=owner,
            scope=OWN(owner.pk),
            pipeline_id=pipeline.pk,
            version=pipeline.version,
            stages=specs,
        )
        specs = stage_specs(pipeline)[1:]
    elif edit == "add_first":
        specs = [{"name": "Z", "type": "open", "probability": Decimal("5")}, *specs]
    else:
        specs[0], specs[1] = specs[1], specs[0]

    paused, release = threading.Event(), threading.Event()
    history = services._history

    def paused_history(*args, **kwargs):
        paused.set()
        release.wait(10)
        return history(*args, **kwargs)

    monkeypatch.setattr(services, "_history", paused_history)
    results = {}

    def run(name, call):
        try:
            results[name] = call()
        except BaseException as exc:
            results[name] = exc
        finally:
            connections.close_all()

    mover = threading.Thread(
        target=run,
        args=(
            "move",
            lambda: services.move_opportunity(
                actor=owner,
                scope=OWN(owner.pk),
                opportunity_id=opportunity.pk,
                version=opportunity.version,
                stage_id=target.pk,
            ),
        ),
    )
    mover.start()
    assert paused.wait(10)
    editor = threading.Thread(
        target=run,
        args=(
            "edit",
            lambda: configuration.replace_stages(
                actor=owner,
                scope=OWN(owner.pk),
                pipeline_id=pipeline.pk,
                version=pipeline.version,
                stages=specs,
            ),
        ),
    )
    editor.start()
    time.sleep(1)  # the edit runs as far as it can: to the pipeline's lock
    release.set()
    mover.join(30)
    editor.join(30)
    for result in results.values():
        assert not isinstance(result, BaseException), repr(result)
    moved = Opportunity.objects.get(pk=opportunity.pk)
    assert moved.stage_id == target.pk
    assert Stage.objects.get(pk=target.pk).is_active
