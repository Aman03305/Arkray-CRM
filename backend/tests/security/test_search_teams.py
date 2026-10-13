"""Global search in a team administrator's workspace "all": the TEAM scope (ADR-0035).

Tara (a team administrator) manages Rahul and Mohan; Priya is in no team; Kavya is in Omar's.
Anita is a super administrator. Each of the six holds one unmistakably marked record of every
kind. Searched from Tara's "all", exactly Tara's, Rahul's and Mohan's records are found, every
kind of record, and no trace of Priya's, Kavya's or Omar's is in any response; from a
member's workspace only that member's; from Anita's "all" everyone's, as before. Tara can't
search anyone outside her team (404, before the query is even looked at). A closed deal whose
lead left the team is shown as one person's scope shows it: without its customer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from arkray.core.access import AccessScope
from arkray.identity.models import User
from arkray.leads import services as lead_services
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

GROUPS = ("leads", "opportunities", "tasks", "meetings", "notes")
PREFIXES = {
    "tara": "TARA-KAPPA",
    "rahul": "RAHUL-OMEGA",
    "mohan": "MOHAN-SIGMA",
    "priya": "PRIYA-ZETA",
    "kavya": "KAVYA-THETA",
    "omar": "OMAR-DELTA",
}
TEAM = {"tara", "rahul", "mohan"}


def build(owner: Any, prefix: str) -> dict[str, Any]:
    """One record of every kind, found by one shared word ("SHAREDWORD") and by its marker:
    PREFIX-LEAD, PREFIX-DEAL, PREFIX-TASK, PREFIX-MEETING, PREFIX-NOTE."""
    lead = LeadFactory(
        owner=owner,
        first_name=f"{prefix}-LEAD",
        last_name="SHAREDWORD",
        organization_name="",
        email="",
    )
    return {
        "leads": lead,
        "opportunities": OpportunityFactory(lead=lead, title=f"{prefix}-DEAL SHAREDWORD"),
        "tasks": TaskFactory(lead=lead, title=f"{prefix}-TASK SHAREDWORD"),
        "meetings": MeetingFactory(lead=lead, title=f"{prefix}-MEETING SHAREDWORD", location=""),
        "notes": NoteFactory(lead=lead, description=f"Remember {prefix}-NOTE SHAREDWORD today."),
    }


@pytest.fixture
def world(team_admin, team_member, team_member_2, user_b, other_team_member, other_team_admin):
    owners = {
        "tara": team_admin,
        "rahul": team_member,
        "mohan": team_member_2,
        "priya": user_b,
        "kavya": other_team_member,
        "omar": other_team_admin,
    }
    return {person: build(owner, PREFIXES[person]) for person, owner in owners.items()}


def search(client: Any, workspace: str, q: str) -> Any:
    response = client.get(f"/api/v1/workspaces/{workspace}/search", {"q": q})
    assert response.status_code == 200, response.content
    return response


def found(body: dict[str, Any]) -> dict[str, set[str]]:
    return {group: {row["id"] for row in body[group]["results"]} for group in GROUPS}


def expected(world: dict[str, Any], people: set[str]) -> dict[str, set[str]]:
    return {group: {str(world[p][group].pk) for p in people} for group in GROUPS}


def traces(text: str, world: dict[str, Any], people: set[str]) -> list[str]:
    """Anything of `people` in a response's bytes: a record id or a marker."""
    hits = [str(r.pk) for p in people for r in world[p].values() if str(r.pk) in text]
    return hits + [PREFIXES[p] for p in people if PREFIXES[p] in text]


def test_a_team_administrators_all_searches_exactly_the_team(world, team_admin):
    response = search(signed_in(team_admin), "all", "SHAREDWORD")
    assert found(response.json()) == expected(world, TEAM)
    outside = set(PREFIXES) - TEAM
    assert not traces(response.content.decode(), world, outside)


@pytest.mark.parametrize("person", sorted(PREFIXES))
def test_every_marker_from_the_team_workspace(world, team_admin, person):
    client = signed_in(team_admin)
    for kind, group in zip(("LEAD", "DEAL", "TASK", "MEETING", "NOTE"), GROUPS, strict=True):
        body = search(client, "all", f"{PREFIXES[person]}-{kind}").json()
        want = {g: set() for g in GROUPS}
        if person in TEAM:
            want[group] = {str(world[person][group].pk)}
        assert found(body) == want, (person, kind)
        assert not any(body[g]["has_more"] for g in GROUPS)


def test_a_members_workspace_searches_only_that_member(world, team_admin, team_member):
    response = search(signed_in(team_admin), str(team_member.pk), "SHAREDWORD")
    assert found(response.json()) == expected(world, {"rahul"})
    assert not traces(response.content.decode(), world, set(PREFIXES) - {"rahul"})


def test_the_super_administrators_all_is_still_everyone(world, admin):
    client = signed_in(admin)
    body = search(client, "all", "SHAREDWORD").json()
    everyone = expected(world, set(PREFIXES))
    for group in GROUPS:  # six people, five shown: the rest flagged
        assert found(body)[group] < everyone[group]
        assert len(found(body)[group]) == 5
        assert body[group]["has_more"] is True
    for person in sorted(set(PREFIXES) - TEAM):
        body = search(client, "all", f"{PREFIXES[person]}-NOTE").json()
        assert found(body)["notes"] == {str(world[person]["notes"].pk)}


def test_a_member_who_left_the_team_is_no_longer_searched(world, team_admin, team_member):
    User.objects.filter(pk=team_member.pk).update(manager=None)
    response = search(signed_in(team_admin), "all", "SHAREDWORD")
    assert found(response.json()) == expected(world, {"tara", "mohan"})
    assert not traces(response.content.decode(), world, set(PREFIXES) - {"tara", "mohan"})
    # Nor opened on its own: 404 like a user who doesn't exist.
    response = signed_in(team_admin).get(
        f"/api/v1/workspaces/{team_member.pk}/search", {"q": "SHAREDWORD"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("outsider", ["user_b", "other_team_member", "other_team_admin", "admin"])
@pytest.mark.parametrize("q", ["SHAREDWORD", "a", ""])
def test_nobody_outside_the_team_can_be_searched(world, request, team_admin, outsider, q):
    """404 whatever the query (an invalid one too: the workspace is resolved first), the
    same as for a user who doesn't exist."""
    target = request.getfixturevalue(outsider)
    response = signed_in(team_admin).get(f"/api/v1/workspaces/{target.pk}/search", {"q": q})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_closed_deal_whose_lead_left_the_team_is_shown_without_its_customer(
    team_admin, team_member, user_b, team_member_2, admin
):
    def won_deal(account: str) -> Any:
        lead = LeadFactory(owner=team_member, first_name="Ravi", last_name="Kumar")
        return OpportunityFactory(
            lead=lead,
            stage_key="won",
            title=f"Ravi Kumar {account}",
            account_name=account,
            customer_name="Ravi Kumar",
            value=Decimal("1000"),
        )

    away, inside = won_deal("Apollo Labs"), won_deal("Apollo Clinic")
    for deal, to in ((away, user_b), (inside, team_member_2)):
        deal.lead.refresh_from_db()
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=deal.lead.pk,
            version=deal.lead.version,
            owner_id=to.pk,
        )
    body = search(signed_in(team_admin), "all", "Apollo").json()
    rows = {row["id"]: row for row in body["opportunities"]["results"]}
    assert set(rows) == {str(away.pk), str(inside.pk)}
    assert rows[str(away.pk)]["customer_restricted"] is True
    assert rows[str(away.pk)]["customer_name"] == ""
    assert "Ravi" not in str(rows[str(away.pk)])
    assert rows[str(inside.pk)]["customer_restricted"] is False  # its lead is Mohan's: in
    assert rows[str(inside.pk)]["customer_name"] == "Ravi Kumar"
    # Searched by the customer's name, the deal whose lead left is not found.
    body = search(signed_in(team_admin), "all", "Ravi Kumar").json()
    assert {row["id"] for row in body["opportunities"]["results"]} == {str(inside.pk)}
