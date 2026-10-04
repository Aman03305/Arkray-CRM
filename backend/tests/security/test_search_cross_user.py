"""Cross-user suite for global search (Phase 7): authorisation before matching.

Rahul (A) and Priya (B) each own one unmistakably marked record of every kind. Each marker
is searched from Rahul's, Priya's and the administrator's workspaces (organisation-wide and
each selected user), and every response is checked EXACTLY: the right record in the right
group, nothing else, and no trace of the other user's records anywhere in the bytes. A
search for the other user's exact secret is indistinguishable from a search for something
nobody has: same status, same body, same size, same "more" flags.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

GROUPS = ("leads", "opportunities", "tasks", "meetings", "notes")
KINDS = {"LEAD": "leads", "DEAL": "opportunities", "TASK": "tasks", "MEETING": "meetings"}


def url(q, workspace="me"):
    return f"/api/v1/workspaces/{workspace}/search?q={q}"


def build(owner, prefix):
    """One record of every kind, each named only by its marker: PREFIX-LEAD, PREFIX-DEAL,
    PREFIX-TASK, PREFIX-MEETING and PREFIX-NOTE-SECRET."""
    lead = LeadFactory(
        owner=owner,
        first_name=f"{prefix}-LEAD",
        last_name="",
        organization_name="",
        email="",
    )
    return {
        "leads": lead,
        "opportunities": OpportunityFactory(lead=lead, title=f"{prefix}-DEAL"),
        "tasks": TaskFactory(lead=lead, title=f"{prefix}-TASK"),
        "meetings": MeetingFactory(lead=lead, title=f"{prefix}-MEETING", location=""),
        "notes": NoteFactory(lead=lead, description=f"Remember: {prefix}-NOTE-SECRET today."),
    }


@pytest.fixture
def world(user_a, user_b, admin):
    return {
        "rahul": build(user_a, "RAHUL-OMEGA"),
        "priya": build(user_b, "PRIYA-ZETA"),
        "clients": {
            "rahul": (signed_in(user_a), "me"),
            "priya": (signed_in(user_b), "me"),
            "admin-org": (signed_in(admin), "all"),
            "admin-rahul": (signed_in(admin), str(user_a.pk)),
            "admin-priya": (signed_in(admin), str(user_b.pk)),
        },
    }


# Who sees whose records.
SEES = {
    "rahul": {"rahul"},
    "priya": {"priya"},
    "admin-org": {"rahul", "priya"},
    "admin-rahul": {"rahul"},
    "admin-priya": {"priya"},
}
MARKERS = {
    "rahul": ["RAHUL-OMEGA-LEAD", "RAHUL-OMEGA-DEAL", "RAHUL-OMEGA-TASK", "RAHUL-OMEGA-MEETING"],
    "priya": ["PRIYA-ZETA-LEAD", "PRIYA-ZETA-DEAL", "PRIYA-ZETA-TASK", "PRIYA-ZETA-MEETING"],
}
SECRETS = {"rahul": "RAHUL-OMEGA-NOTE-SECRET", "priya": "PRIYA-ZETA-NOTE-SECRET"}


def results(world, who, q):
    client, workspace = world["clients"][who]
    response = client.get(url(q, workspace))
    assert response.status_code == 200, response.content
    return response


def found(body):
    return {g: [row["id"] for row in body[g]["results"]] for g in GROUPS}


@pytest.mark.parametrize("who", sorted(SEES))
@pytest.mark.parametrize("owner", ["rahul", "priya"])
def test_every_marker_from_every_workspace(world, who, owner):
    other = "priya" if owner == "rahul" else "rahul"
    for marker in [*MARKERS[owner], SECRETS[owner]]:
        group = "notes" if marker.endswith("SECRET") else KINDS[marker.rsplit("-", 1)[1]]
        body = results(world, who, marker).json()
        expected = {g: [] for g in GROUPS}
        if owner in SEES[who]:
            expected[group] = [str(world[owner][group].pk)]
        assert found(body) == expected, (who, marker)
        if owner not in SEES[who]:
            assert all(not body[g]["has_more"] for g in GROUPS)
    # Nothing of a user outside the workspace, in any form, in any response.
    for q in ("OMEGA", "ZETA", "SECRET", "NOTE", "LEAD", "DEAL", "TASK", "MEETING"):
        text = results(world, who, q).content.decode()
        for record in world[other].values():
            if other not in SEES[who]:
                assert str(record.pk) not in text, (who, q)
        if other not in SEES[who]:
            assert other.upper() not in text, (who, q)


@pytest.mark.parametrize(
    ("who", "secret"),
    [
        ("admin-rahul", "PRIYA-ZETA-NOTE-SECRET"),
        ("admin-priya", "RAHUL-OMEGA-NOTE-SECRET"),
        ("rahul", "PRIYA-ZETA-NOTE-SECRET"),
        ("priya", "RAHUL-OMEGA-NOTE-SECRET"),
    ],
)
def test_another_workspaces_exact_secret_looks_like_nothing_at_all(world, who, secret):
    """Not "permission denied", not a hidden or redacted result, not a count: the response
    is identical to one for a secret that exists nowhere (apart from the echoed query)."""
    real = results(world, who, secret)
    fake = results(world, who, secret.replace("SECRET", "NOSUCH"))
    assert real.status_code == fake.status_code == 200
    real_body, fake_body = real.json(), fake.json()
    for body in (real_body, fake_body):
        body.pop("query")
        body.pop("terms")
    assert real_body == fake_body == {g: {"results": [], "has_more": False} for g in GROUPS}
    assert len(real.content) - len(secret) * 2 == len(fake.content) - len(secret) * 2


def test_another_users_records_never_change_a_workspaces_results(world, user_a, user_b):
    """No count, "more" flag, ranking or window position moves because of records outside
    the scope: Priya adding 120 better matches (exact titles, newer) changes nothing for
    Rahul, in his own search or in the admin's view of his workspace."""
    lead = world["rahul"]["leads"]
    for n in range(3):
        TaskFactory(lead=lead, title=f"Quarterly review {n}")
    before = {who: results(world, who, "quarterly review").json() for who in ("rahul",)}
    before["admin-rahul"] = results(world, "admin-rahul", "quarterly review").json()
    priya_lead = world["priya"]["leads"]
    for _ in range(120):
        TaskFactory(lead=priya_lead, title="Quarterly review", created_at=timezone.now())
    for who, body in before.items():
        assert results(world, who, "quarterly review").json() == body, who
    assert results(world, "admin-org", "quarterly review").json()["tasks"]["has_more"] is True


def test_a_deactivated_users_workspace_is_searched_like_any_other(world, admin, user_a):
    user_a.is_active = False
    user_a.status = "deactivated"
    user_a.deactivated_at = timezone.now()
    user_a.save()
    body = results(world, "admin-rahul", "OMEGA").json()
    assert all(len(body[g]["results"]) == 1 for g in GROUPS)
    assert "ZETA" not in str(body)


def test_switching_workspaces_in_one_session_never_mixes_results(world):
    """Rahul, Priya, Rahul again from the same admin session: each answer is exactly that
    workspace's (nothing is remembered between searches)."""
    for who in ("admin-rahul", "admin-priya", "admin-rahul", "admin-org", "admin-priya"):
        for q in ("OMEGA", "ZETA", "LEAD"):
            body = results(world, who, q).content.decode()
            rahuls = "rahul" in SEES[who] and q in ("OMEGA", "LEAD")
            priyas = "priya" in SEES[who] and q in ("ZETA", "LEAD")
            assert ("OMEGA-" in body) == rahuls, (who, q)
            assert ("ZETA-" in body) == priyas, (who, q)


@pytest.mark.parametrize(
    "workspace",
    [
        "all",
        "ALL",
        "Me",
        "%2e%2e",
        "..%2fall",
        "00000000-0000-0000-0000-000000000000",
        "not-a-user",
    ],
)
def test_a_sales_user_cannot_widen_the_scope(world, workspace):
    client, _ = world["clients"]["rahul"]
    response = client.get(url("ZETA", workspace))
    assert response.status_code == 404
    assert "ZETA" not in response.content.decode()


def test_another_sales_users_workspace_is_404_like_a_missing_user(world, user_b):
    client, _ = world["clients"]["rahul"]
    missing = client.get(url("ZETA", "5a1e4d2c-0000-4000-8000-00000000abcd"))
    existing = client.get(url("ZETA", str(user_b.pk)))
    assert missing.status_code == existing.status_code == 404
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
    assert strip(missing) == strip(existing)


def test_an_admins_writes_in_a_workspace_are_found_there_not_attributed(world, admin, user_a):
    """A note the admin wrote in Rahul's workspace is Rahul's to find (it follows his lead);
    the result names no author, so it never claims Rahul wrote it."""
    note = NoteFactory(
        lead=world["rahul"]["leads"], created_by=admin, description="ADMIN-WROTE-THIS note"
    )
    body = results(world, "admin-rahul", "ADMIN-WROTE-THIS").json()
    assert [r["id"] for r in body["notes"]["results"]] == [str(note.pk)]
    assert "created_by" not in body["notes"]["results"][0]
    assert "Anita" not in str(body)
    assert "Rahul" not in str(body["notes"])
    assert results(world, "rahul", "ADMIN-WROTE-THIS").json()["notes"]["results"] != []
    assert results(world, "priya", "ADMIN-WROTE-THIS").json()["notes"]["results"] == []


def test_a_second_sales_user_is_isolated_too(world):
    """Not only Rahul and Priya: a third user sees neither."""
    third = signed_in(UserFactory())
    for q in ("OMEGA", "ZETA", "SECRET"):
        body = third.get(url(q)).json()
        assert all(body[g]["results"] == [] for g in GROUPS)
