"""Phase 9 cursor hardening: a list's page link (keyset cursor) continues only the list it
was issued for: the same endpoint and record, the same signed-in user, the same workspace,
the same filters and ordering, and only for as long as a session can last. Replays of every
other kind are refused with the same 400 as a forged cursor; ordinary paging still works on
every list, including the board's columns continuing in the opportunities list."""

from __future__ import annotations

import base64
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from django.conf import settings

from arkray.core import keyset
from arkray.core.access import AccessScope
from arkray.core.keyset import INVALID_CURSOR
from arkray.leads.models import Lead
from arkray.pipeline import services as pipeline_services
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, TaskFactory, default_stage
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

W = "/api/v1/workspaces"


def cursor_of(response: Any) -> str:
    assert response.status_code == 200, response.content
    link = response.json()["next"]
    assert link, "expected a next page"
    cursor: str = parse_qs(urlparse(link).query)["cursor"][0]
    return cursor


def refused(response: Any) -> bool:
    if response.status_code != 400:
        return False
    error = response.json()["error"]
    return bool(error["details"].get("cursor") == [INVALID_CURSOR])


def with_history(owner: Any) -> Any:
    """An opportunity with two stage-history entries (created, then moved)."""
    scope = AccessScope.own(owner.pk)
    lead: Any = LeadFactory(owner=owner)
    opportunity = pipeline_services.create_opportunity(
        actor=owner,
        scope=scope,
        lead_id=lead.pk,
        fields={"value": Decimal("1000")},
    ).opportunity
    pipeline_services.move_opportunity(
        actor=owner,
        scope=scope,
        opportunity_id=opportunity.pk,
        version=opportunity.version,
        stage_id=default_stage("proposal").pk,
    )
    return opportunity


@pytest.fixture
def world(admin, user_a, user_b) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name, owner in (("a", user_a), ("b", user_b)):
        leads = LeadFactory.create_batch(3, owner=owner, status_id="new")
        LeadFactory.create_batch(2, owner=owner, status_id="contacted")
        OpportunityFactory.create_batch(3, lead=leads[0])
        TaskFactory.create_batch(2, lead=leads[0])
        NoteFactory.create_batch(2, lead=leads[0])
        data[name] = {"lead": leads[0], "opportunity": with_history(owner)}
    return data


def lists(world: dict[str, Any], who: str) -> dict[str, tuple[str, dict[str, str]]]:
    """name -> (path in workspace `me`, query) for every paginated list."""
    opportunity = world[who]["opportunity"]
    lead = opportunity.lead  # created through the services: its timeline has entries
    return {
        "leads": ("leads", {"page_size": "1"}),
        "opportunities": ("opportunities", {"page_size": "1"}),
        "activities": ("activities", {"page_size": "1"}),
        "lead timeline": (f"leads/{lead.pk}/timeline", {"page_size": "1"}),
        "opportunity timeline": (f"opportunities/{opportunity.pk}/timeline", {"page_size": "1"}),
        "opportunity history": (f"opportunities/{opportunity.pk}/history", {"page_size": "1"}),
    }


LISTS = [
    "leads",
    "opportunities",
    "activities",
    "lead timeline",
    "opportunity timeline",
    "opportunity history",
]


@pytest.mark.parametrize("name", LISTS)
def test_a_cursor_continues_its_own_list(name, world, user_a):
    client = signed_in(user_a)
    path, query = lists(world, "a")[name]
    first = client.get(f"{W}/me/{path}", query)
    second = client.get(f"{W}/me/{path}", {**query, "cursor": cursor_of(first)})
    assert second.status_code == 200, second.content
    assert second.json()["results"]
    assert second.json()["results"] != first.json()["results"]
    # `me` and one's own id are the same workspace; the page size may change between pages.
    same = client.get(
        f"{W}/{user_a.pk}/{path}", {**query, "page_size": "5", "cursor": cursor_of(first)}
    )
    assert same.status_code == 200, same.content


@pytest.mark.parametrize("name", LISTS)
def test_another_user_cannot_replay_a_cursor(name, world, user_a, user_b):
    path_a, query = lists(world, "a")[name]
    cursor = cursor_of(signed_in(user_a).get(f"{W}/me/{path_a}", query))
    # B on B's own corresponding list: the cursor is A's.
    path_b, _ = lists(world, "b")[name]
    assert refused(signed_in(user_b).get(f"{W}/me/{path_b}", {**query, "cursor": cursor}))


@pytest.mark.parametrize("name", LISTS)
def test_a_cursor_does_not_cross_workspaces(name, world, admin, user_a, user_b):
    client = signed_in(admin)
    path_a, query = lists(world, "a")[name]
    cursor = cursor_of(client.get(f"{W}/{user_a.pk}/{path_a}", query))
    for workspace in ("all", "me", str(user_b.pk)):
        response = client.get(f"{W}/{workspace}/{path_a}", {**query, "cursor": cursor})
        # The record may not exist in that workspace (404) — or the cursor is refused; never 200.
        assert response.status_code == 404 or refused(response), (workspace, response.content)


def test_a_cursor_does_not_cross_records(world, user_a):
    client = signed_in(user_a)
    other = with_history(user_a)
    opportunity = world["a"]["opportunity"]
    cursor = cursor_of(
        client.get(f"{W}/me/opportunities/{opportunity.pk}/history", {"page_size": "1"})
    )
    assert refused(
        client.get(f"{W}/me/opportunities/{other.pk}/history", {"page_size": "1", "cursor": cursor})
    )


@pytest.mark.parametrize(
    ("path", "issued_with", "replayed_with"),
    [
        ("leads", {"status": "new"}, {"status": "contacted"}),
        ("leads", {"status": "new"}, {}),
        ("leads", {}, {"q": "zebulon"}),
        ("leads", {"ordering": "name"}, {"ordering": "-created_at"}),
        ("opportunities", {"status": "open"}, {"status": "won"}),
        ("opportunities", {}, {"archived": "true"}),
        ("activities", {"type": "task"}, {"type": "note"}),
    ],
)
def test_a_cursor_does_not_cross_filters(path, issued_with, replayed_with, world, user_a):
    client = signed_in(user_a)
    first = client.get(f"{W}/me/{path}", {"page_size": "1", **issued_with})
    cursor = cursor_of(first)  # every issuing filter has several rows in the fixture
    replay = client.get(f"{W}/me/{path}", {"page_size": "1", **replayed_with, "cursor": cursor})
    assert refused(replay), replay.content
    # Empty values are no filter: a Leads cursor issued without a search continues with `q=`.
    if path == "leads" and not issued_with:
        same = client.get(f"{W}/me/{path}", {"page_size": "1", "q": "", "cursor": cursor})
        assert same.status_code == 200, same.content


def test_a_cursor_does_not_cross_lists(world, user_a):
    client = signed_in(user_a)
    cursor = cursor_of(client.get(f"{W}/me/leads", {"page_size": "1"}))
    for path in ("opportunities", "activities", f"leads/{world['a']['lead'].pk}/timeline"):
        assert refused(client.get(f"{W}/me/{path}", {"page_size": "1", "cursor": cursor})), path


def issued_at(payload: dict[str, Any], when: float) -> str:
    """The same cursor as if it had been issued at `when` (only the cursor's own clock
    moves: the session around the request is untouched)."""
    data = json.dumps(payload).encode()
    return keyset._fernet(settings.SECRET_KEY).encrypt_at_time(data, int(when)).decode()


def test_a_cursor_expires(world, user_a):
    client = signed_in(user_a)
    payload = keyset._open(cursor_of(client.get(f"{W}/me/leads", {"page_size": "1"})))
    max_age = settings.KEYSET_CURSOR_MAX_AGE_S
    expired = issued_at(payload, time.time() - max_age - 60)
    assert refused(client.get(f"{W}/me/leads", {"page_size": "1", "cursor": expired}))
    fresh = issued_at(payload, time.time() - max_age + 60)
    response = client.get(f"{W}/me/leads", {"page_size": "1", "cursor": fresh})
    assert response.status_code == 200, response.content


def test_cursors_expire_with_the_longest_session():
    assert settings.KEYSET_CURSOR_MAX_AGE_S == settings.SESSION_COOKIE_AGE


def test_a_tampered_cursor_is_refused(world, user_a):
    client = signed_in(user_a)
    cursor = cursor_of(client.get(f"{W}/me/leads", {"page_size": "1"}))
    payload = keyset._open(cursor)
    for change in ({"b": None}, {"b": "0" * 32}, {"d": "prev"}, {"o": "name"}):
        sealed = json.dumps({**payload, **change}).encode()
        forged = keyset._fernet("not-the-key").encrypt(sealed).decode()
        assert refused(client.get(f"{W}/me/leads", {"page_size": "1", "cursor": forged})), change
    flipped = cursor[:-2] + ("A" if cursor[-2] != "A" else "B") + cursor[-1]
    assert refused(client.get(f"{W}/me/leads", {"page_size": "1", "cursor": flipped}))


def test_a_cursor_holds_no_filter_values_or_names(world, user_a):
    client = signed_in(user_a)
    Lead.objects.filter(owner=user_a).update(first_name="Zebulon")
    cursor = cursor_of(
        client.get(f"{W}/me/leads", {"page_size": "1", "q": "Zebulon", "ordering": "name"})
    )
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    assert b"Zebulon" not in raw  # sealed: not even the ordering is readable
    assert b"name" not in raw
    decoded = str(keyset._open(cursor))
    assert "zebulon" not in decoded.lower()


def test_assignee_cursors_are_bound_to_the_admin(admin, user_a, user_b):
    from tests.factories import AdminFactory

    other_admin = AdminFactory()
    first = signed_in(admin).get("/api/v1/assignees", {"page_size": "1"})
    cursor = cursor_of(first)
    assert (
        signed_in(admin).get("/api/v1/assignees", {"page_size": "1", "cursor": cursor}).status_code
        == 200
    )
    assert refused(
        signed_in(other_admin).get("/api/v1/assignees", {"page_size": "1", "cursor": cursor})
    )


def test_the_boards_column_links_continue_in_the_opportunities_list(admin, user_a, user_b):
    lead = LeadFactory(owner=user_a)
    OpportunityFactory.create_batch(4, lead=lead)
    client = signed_in(user_a)
    board = client.get(f"{W}/me/pipeline-board", {"cards_per_stage": "1"})
    assert board.status_code == 200, board.content
    column = next(c for c in board.json()["columns"] if c["count"] > 1)
    followed = client.get(column["next"])  # the link exactly as given
    assert followed.status_code == 200, followed.content
    assert len(followed.json()["results"]) == 1
    # Someone else following it — or the same user with other filters — is refused.
    cursor = parse_qs(urlparse(column["next"]).query)["cursor"][0]
    assert refused(
        signed_in(user_b).get(f"{W}/me/opportunities", {**_query(column["next"]), "cursor": cursor})
    )
    changed = {**_query(column["next"]), "expected_close_from": "2020-01-01"}
    assert refused(client.get(f"{W}/me/opportunities", changed))


def _query(link: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}


def test_a_cursor_survives_a_secret_key_rotation(world, user_a, settings):
    """Phase 9 review P3: the binding was checked under the current key only, so rotating
    SECRET_KEY (old key kept as a fallback) broke every open page link."""
    client = signed_in(user_a)
    cursor = cursor_of(client.get(f"{W}/me/leads", {"page_size": "1"}))
    settings.SECRET_KEY_FALLBACKS = [settings.SECRET_KEY]
    settings.SECRET_KEY = "rotated-" + "k" * 60
    response = client.get(f"{W}/me/leads", {"page_size": "1", "cursor": cursor})
    assert response.status_code == 200, response.content
    # Without the old key among the fallbacks, it is invalid like any forged cursor.
    settings.SECRET_KEY_FALLBACKS = []
    assert refused(client.get(f"{W}/me/leads", {"page_size": "1", "cursor": cursor}))


def test_the_admin_user_list_binds_its_cursors(admin):
    """Phase 9 review P3: the user table kept DRF's readable, unbound, non-expiring cursor
    (a cursor issued with status=active replayed with role=sales_user returned 200)."""
    from tests.factories import AdminFactory, UserFactory

    UserFactory.create_batch(3)
    client = signed_in(admin)
    first = client.get("/api/v1/admin/users", {"page_size": "1", "status": "active"})
    cursor = cursor_of(first)
    same = client.get(
        "/api/v1/admin/users", {"page_size": "1", "status": "active", "cursor": cursor}
    )
    assert same.status_code == 200, same.content
    assert same.json()["results"] != first.json()["results"]
    other_filter = {"page_size": "1", "role": "sales_user", "cursor": cursor}
    assert refused(client.get("/api/v1/admin/users", other_filter))
    another_admin = signed_in(AdminFactory())
    replay = {"page_size": "1", "status": "active", "cursor": cursor}
    assert refused(another_admin.get("/api/v1/admin/users", replay))
