"""Retired stages are bounded (adversarial review of 73ff1e3, L10; docs/pipeline.md#stages).

A stage removed while anything references it (a deal, its stage history, an agreed price) is
retired, never deleted, and nothing ever stops referencing it (the histories are
append-only). The owner of a personal pipeline could loop "add a stage, move a deal through
it, remove it": every cycle left a retired row. Their positions climb from 10,000 in a
smallint column, so after about 22,700 cycles every stage edit of the pipeline failed with a
500; and every pipeline payload carried every retired stage (20,000 of them: 4 MB, 0.7 s for
an administrator's organisation-wide list).

Now a pipeline keeps at most MAX_RETIRED_STAGES_PER_PIPELINE retired stages: a removal that
would retire one more is refused (422, nothing changed; a stage nothing references is still
deleted), a retired position never leaves the smallint range, and the pipeline payloads list
the active stages only, so their size doesn't depend on how many stages were ever retired.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from arkray.core.access import AccessScope
from arkray.pipeline import configuration, services
from arkray.pipeline.models import MAX_RETIRED_STAGES_PER_PIPELINE, Pipeline, Stage
from tests.factories import LeadFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

SMALLINT_MAX = 32767


def personal_pipeline(user: Any) -> Pipeline:
    response = signed_in(user).post(
        "/api/v1/workspaces/me/pipelines",
        {
            "name": "Loop",
            "stages": [
                {"name": "Open", "type": "open", "probability": "10"},
                {"name": "Victim", "type": "open", "probability": "20"},
                {"name": "Won", "type": "won"},
                {"name": "Lost", "type": "lost"},
            ],
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    return Pipeline.objects.get(pk=response.json()["id"])


def referenced(user: Any, stage: Stage) -> None:
    """A deal created in `stage` (its stage history then references it), then archived, so
    the stage may be removed and must be retired."""
    lead: Any = LeadFactory(owner=user)
    deal = services.create_opportunity(
        actor=user,
        scope=AccessScope.own(user.pk),
        lead_id=lead.pk,
        fields={"value": Decimal("1000")},
        pipeline_id=stage.pipeline_id,
        stage_id=stage.pk,
    ).opportunity
    services.archive_opportunity(
        actor=user, scope=AccessScope.own(user.pk), opportunity_id=deal.pk, version=deal.version
    )


def retired_rows(pipeline: Pipeline, count: int, *, first_position: int) -> None:
    """`count` retired stages, as earlier removal cycles leave them (inserted directly)."""
    Stage.objects.bulk_create(
        [
            Stage(
                pipeline=pipeline,
                key=f"r{index:05d}",
                name=f"Retired {index}",
                position=first_position + index,
                probability=Decimal("10"),
                category="open",
                is_active=False,
            )
            for index in range(count)
        ]
    )


def remove_victim(user: Any, pipeline: Pipeline) -> Any:
    pipeline.refresh_from_db()
    kept = list(pipeline.stages.filter(is_active=True).order_by("position"))
    return signed_in(user).put(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/stages",
        {
            "version": pipeline.version,
            "stages": [
                {
                    "id": str(s.pk),
                    "name": s.name,
                    "type": s.stage_type,
                    "probability": str(s.probability),
                }
                for s in kept
                if s.name != "Victim"
            ],
        },
        format="json",
    )


def test_the_cap_refuses_one_more_retired_stage(user_a):
    pipeline = personal_pipeline(user_a)
    victim = pipeline.stages.get(name="Victim")
    referenced(user_a, victim)
    retired_rows(
        pipeline,
        MAX_RETIRED_STAGES_PER_PIPELINE,
        first_position=configuration.RETIRED_POSITION_BASE,
    )
    before = list(Stage.objects.filter(pipeline=pipeline).order_by("pk").values())
    response = remove_victim(user_a, pipeline)
    assert response.status_code == 422, response.content
    assert str(MAX_RETIRED_STAGES_PER_PIPELINE) in response.json()["error"]["message"]
    assert list(Stage.objects.filter(pipeline=pipeline).order_by("pk").values()) == before


def test_under_the_cap_a_referenced_stage_is_retired(user_a):
    pipeline = personal_pipeline(user_a)
    victim = pipeline.stages.get(name="Victim")
    referenced(user_a, victim)
    retired_rows(
        pipeline,
        MAX_RETIRED_STAGES_PER_PIPELINE - 1,
        first_position=configuration.RETIRED_POSITION_BASE,
    )
    response = remove_victim(user_a, pipeline)
    assert response.status_code == 200, response.content
    victim.refresh_from_db()
    assert victim.is_active is False


def test_an_unreferenced_stage_is_still_deleted_at_the_cap(user_a):
    pipeline = personal_pipeline(user_a)
    retired_rows(
        pipeline,
        MAX_RETIRED_STAGES_PER_PIPELINE,
        first_position=configuration.RETIRED_POSITION_BASE,
    )
    response = remove_victim(user_a, pipeline)
    assert response.status_code == 200, response.content
    assert not Stage.objects.filter(pipeline=pipeline, name="Victim").exists()


def test_a_retired_position_never_leaves_the_smallint_range(user_a):
    """Positions left by earlier releases (or a cap raised later) may already sit at the top
    of the column: one more retirement is a 422, never a 500."""
    pipeline = personal_pipeline(user_a)
    referenced(user_a, pipeline.stages.get(name="Victim"))
    retired_rows(pipeline, 1, first_position=SMALLINT_MAX)
    response = remove_victim(user_a, pipeline)
    assert response.status_code == 422, response.content
    assert Stage.objects.filter(pipeline=pipeline, name="Victim", is_active=True).exists()


@pytest.mark.parametrize("workspace", ["me", "all"])
def test_payloads_list_only_the_active_stages(admin, user_a, workspace):
    pipeline = personal_pipeline(user_a)
    retired_rows(pipeline, 50, first_position=configuration.RETIRED_POSITION_BASE)
    client = signed_in(user_a if workspace == "me" else admin)
    listed = client.get(f"/api/v1/workspaces/{workspace}/pipelines").json()["results"]
    found = next(p for p in listed if p["id"] == str(pipeline.pk))
    one = client.get(f"/api/v1/workspaces/{workspace}/pipelines/{pipeline.pk}").json()
    for payload in (found, one):
        assert [s["name"] for s in payload["stages"]] == ["Open", "Victim", "Won", "Lost"]
        assert all(s["is_active"] for s in payload["stages"])
