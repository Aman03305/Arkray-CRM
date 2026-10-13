"""Pipeline configuration stays bounded (review of the 73ff1e3 remediation, DEALS-1 and DEALS-3;
docs/pipeline.md#configuration).

DEALS-1: only active pipelines counted towards an owner's 25, so a salesperson could create and
archive pipelines without limit (two requests each), and every pipelines list carried each
pipeline's whole configuration, select options included: one pipeline with 30 single-choice
fields of 50 hundred-character options is about 200 KB, and 150 archived ones made
`GET /workspaces/me/pipelines?archived=true` (and an administrator's organisation-wide list)
30 MB in about 10 s with a 200 MB peak, towards 100 MB at the list's 500. Now an owner keeps at
most MAX_ARCHIVED_PIPELINES_PER_OWNER archived pipelines (archiving one more is a 422), and a
pipelines list carries each pipeline's stages and flags only, never its custom fields (one
pipeline's own page has them): its size per pipeline is bounded whatever the configuration.

DEALS-3: removing custom fields archived them without limit (300 after 20 requests), and every
restricted view of a deal reads the pipeline's free-text field ids in any state. A pipeline now
keeps at most MAX_ARCHIVED_FIELDS_PER_PIPELINE removed fields (a 422 past it).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from arkray.pipeline import configuration
from arkray.pipeline.models import (
    MAX_ARCHIVED_FIELDS_PER_PIPELINE,
    MAX_ARCHIVED_PIPELINES_PER_OWNER,
    MAX_FIELD_OPTIONS,
    MAX_FIELDS_PER_PIPELINE,
    MAX_STAGES_PER_PIPELINE,
    CustomField,
    FieldType,
    Pipeline,
    Stage,
)
from tests.factories import UserFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

# What one pipeline may add to a list response, whatever its configuration: its flags, owner
# and at most MAX_STAGES_PER_PIPELINE active stages with 50-character names (about 5 KB).
LIST_BYTES_PER_PIPELINE = 6_000
STAGES = [
    {"name": "New", "type": "open", "probability": "10"},
    {"name": "Won", "type": "won"},
    {"name": "Lost", "type": "lost"},
]


def maxed_pipeline(owner: Any, number: int, *, active: bool) -> Pipeline:
    """A pipeline at every configuration limit (inserted directly: building them through the
    API takes seconds each): 20 stages with 50-character names, 30 single-choice fields of 50
    hundred-character options."""
    pipeline = Pipeline.objects.create(
        key=configuration.new_key("p"),
        name=f"Maxed {number}",
        owner=owner,
        created_by=owner,
        is_active=active,
    )
    Stage.objects.bulk_create(
        [
            Stage(
                pipeline=pipeline,
                key=configuration.new_key("s"),
                name=f"{index:02d} " + "s" * 47,
                position=(index + 1) * 10,
                probability=Decimal("100")
                if index == MAX_STAGES_PER_PIPELINE - 1
                else Decimal("10"),
                category="won" if index == MAX_STAGES_PER_PIPELINE - 1 else "open",
            )
            for index in range(MAX_STAGES_PER_PIPELINE)
        ]
    )
    CustomField.objects.bulk_create(
        [
            CustomField(
                pipeline=pipeline,
                name=f"Field {index:02d}",
                field_type=FieldType.SINGLE_SELECT,
                options=[
                    {"id": f"{index:02d}{option:08d}", "label": (f"{option:02d}" + "o" * 100)[:100]}
                    for option in range(MAX_FIELD_OPTIONS)
                ],
                position=index,
            )
            for index in range(MAX_FIELDS_PER_PIPELINE)
        ]
    )
    return pipeline


def archived_rows(owner: Any, count: int) -> None:
    Pipeline.objects.bulk_create(
        [
            Pipeline(
                key=configuration.new_key("p"),
                name=f"Old {index}",
                owner=owner,
                created_by=owner,
                is_active=False,
            )
            for index in range(count)
        ]
    )


def create_and_archive(client: Any) -> Any:
    created = client.post(
        "/api/v1/workspaces/me/pipelines", {"name": "One more", "stages": STAGES}, format="json"
    )
    assert created.status_code == 201, created.content
    body = created.json()
    return client.post(
        f"/api/v1/workspaces/me/pipelines/{body['id']}/archive",
        {"version": body["version"]},
        format="json",
    )


# --- DEALS-1: archived pipelines ------------------------------------------------------------------
def test_an_owner_at_the_archived_cap_cannot_archive_one_more(user_a):
    archived_rows(user_a, MAX_ARCHIVED_PIPELINES_PER_OWNER)
    response = create_and_archive(signed_in(user_a))
    assert response.status_code == 422, response.content
    assert str(MAX_ARCHIVED_PIPELINES_PER_OWNER) in response.json()["error"]["message"]
    assert Pipeline.objects.filter(owner=user_a, is_active=False).count() == (
        MAX_ARCHIVED_PIPELINES_PER_OWNER
    )
    assert Pipeline.objects.filter(owner=user_a, name="One more", is_active=True).exists()


def test_under_the_cap_archiving_goes_on_and_restoring_makes_room(user_a):
    archived_rows(user_a, MAX_ARCHIVED_PIPELINES_PER_OWNER - 1)
    client = signed_in(user_a)
    assert create_and_archive(client).status_code == 200
    old = Pipeline.objects.filter(owner=user_a, is_active=False, name="Old 0").get()
    restored = client.post(
        f"/api/v1/workspaces/me/pipelines/{old.pk}/restore",
        {"version": old.version},
        format="json",
    )
    assert restored.status_code == 200, restored.content
    assert create_and_archive(client).status_code == 200


def test_one_owners_archived_pipelines_never_count_against_another(user_a, user_b):
    archived_rows(user_b, MAX_ARCHIVED_PIPELINES_PER_OWNER)
    assert create_and_archive(signed_in(user_a)).status_code == 200


@pytest.mark.parametrize("archived", [True, False], ids=["archived", "active"])
def test_an_owners_pipeline_lists_stay_within_a_byte_budget(user_a, archived):
    count = 6
    for number in range(count):
        maxed_pipeline(user_a, number, active=not archived)
    response = signed_in(user_a).get(
        f"/api/v1/workspaces/me/pipelines{'?archived=true' if archived else ''}"
    )
    assert response.status_code == 200
    listed = response.json()["results"]
    mine = [p for p in listed if p["owner"] and p["owner"]["id"] == str(user_a.pk)]
    assert len(mine) == count
    budget = len(listed) * LIST_BYTES_PER_PIPELINE
    assert len(response.content) <= budget, (len(response.content), budget)
    assert all("custom_fields" not in p for p in listed)
    assert all(len(p["stages"]) == MAX_STAGES_PER_PIPELINE for p in mine)


def test_the_organisation_wide_lists_stay_within_a_byte_budget(admin):
    for owner in (UserFactory(), UserFactory()):
        for number in range(3):
            maxed_pipeline(owner, number, active=False)
            maxed_pipeline(owner, number + 10, active=True)
    client = signed_in(admin)
    for path in (
        "/api/v1/workspaces/all/pipelines?archived=true",
        "/api/v1/workspaces/all/pipelines",
        "/api/v1/config/pipelines",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        listed = response.json()["results"]
        assert len(listed) >= (6 if "all" in path else 1), path
        assert len(response.content) <= len(listed) * LIST_BYTES_PER_PIPELINE, (
            path,
            len(response.content),
        )


def test_one_pipeline_still_carries_its_whole_configuration(user_a):
    pipeline = maxed_pipeline(user_a, 0, active=True)
    body = signed_in(user_a).get(f"/api/v1/workspaces/me/pipelines/{pipeline.pk}").json()
    assert len(body["custom_fields"]) == MAX_FIELDS_PER_PIPELINE
    assert all(len(f["options"]) == MAX_FIELD_OPTIONS for f in body["custom_fields"])


# --- DEALS-3: archived custom fields --------------------------------------------------------------
def archived_fields(pipeline: Pipeline, count: int) -> None:
    CustomField.objects.bulk_create(
        [
            CustomField(
                pipeline=pipeline,
                name=f"Removed {index}",
                field_type=FieldType.LONG_TEXT,
                position=1000 + index,
                is_active=False,
            )
            for index in range(count)
        ]
    )


def field_pipeline(user: Any) -> tuple[Pipeline, list[dict[str, Any]]]:
    response = signed_in(user).post(
        "/api/v1/workspaces/me/pipelines",
        {
            "name": "Fields",
            "stages": STAGES,
            "custom_fields": [
                {"name": "Keep", "type": "text"},
                {"name": "Drop", "type": "long_text"},
            ],
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    return Pipeline.objects.get(pk=body["id"]), body["custom_fields"]


def replace_fields(user: Any, pipeline: Pipeline, fields: list[dict[str, Any]]) -> Any:
    pipeline.refresh_from_db()
    return signed_in(user).put(
        f"/api/v1/workspaces/me/pipelines/{pipeline.pk}/fields",
        {"version": pipeline.version, "custom_fields": fields},
        format="json",
    )


def test_a_pipeline_keeps_at_most_so_many_removed_fields(user_a):
    pipeline, fields = field_pipeline(user_a)
    archived_fields(pipeline, MAX_ARCHIVED_FIELDS_PER_PIPELINE)
    keep = next(f for f in fields if f["name"] == "Keep")
    before = list(CustomField.objects.filter(pipeline=pipeline).order_by("pk").values())
    response = replace_fields(
        user_a, pipeline, [{"id": keep["id"], "name": "Keep", "type": "text"}]
    )
    assert response.status_code == 422, response.content
    assert str(MAX_ARCHIVED_FIELDS_PER_PIPELINE) in response.json()["error"]["message"]
    assert list(CustomField.objects.filter(pipeline=pipeline).order_by("pk").values()) == before


def test_under_the_field_cap_removing_goes_on(user_a):
    pipeline, fields = field_pipeline(user_a)
    archived_fields(pipeline, MAX_ARCHIVED_FIELDS_PER_PIPELINE - 1)
    keep = next(f for f in fields if f["name"] == "Keep")
    response = replace_fields(
        user_a, pipeline, [{"id": keep["id"], "name": "Keep", "type": "text"}]
    )
    assert response.status_code == 200, response.content
    # Renaming, reordering and adding stay possible at the cap (nothing more is removed).
    archived_fields(pipeline, 1)
    again = replace_fields(
        user_a,
        pipeline,
        [{"id": keep["id"], "name": "Kept", "type": "text"}, {"name": "New", "type": "text"}],
    )
    assert again.status_code == 200, again.content
