"""Teams in the CRM (ADR-0035): a team administrator's reach over leads, opportunities,
activities, pipelines and the dashboard (docs/authorization.md#the-team-scope).

Tara is a team administrator managing Rahul and Mohan. Priya is in no team; Omar administers
another team, Kavya's; Anita is a super administrator. Every record carries its owner's
marker (TARA-ONLY-*, RAHUL-ONLY-*, ...) and an amount unique to its owner, so any leak shows.

- Workspace `all` is Tara's team (scope TEAM: her records, Rahul's and Mohan's); a member's
  id is that member's workspace; anyone else's id is the same 404 as a user who doesn't
  exist, on every route.
- A record of anyone outside the team, through either workspace, answers exactly like a
  record that exists nowhere (reads 404, writes 404 or the same validation error) and
  changes nothing.
- Assignment, and creating a record for someone else, stays within the team: Tara or an
  active member, checked under a share lock on the member's row; anyone else is the same 400
  as an id that exists nowhere. A team move racing an assignment is ordered by that lock.
- Creating in the team's workspace names an owner of the team.
- Pipeline configuration is refused in the team's workspace (403); a member's personal
  pipeline is configured from the member's workspace, a shared one never by Tara.
- Correcting a closed deal: crm.manage_team for a deal of the team only.
- Owner filters in the team's workspace narrow within it; dashboard, board and summary
  figures count the team only. Anita's behaviour is unchanged.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from django.db import connection, transaction
from django.utils import timezone

from arkray.activities.models import Activity, Attachment
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.domain_events import subscribed
from arkray.core.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from arkray.identity import teams
from arkray.identity.workspaces import AUDIT_ACTION_WORKSPACE_ACCESSED
from arkray.leads import services as lead_services
from arkray.leads.events import LeadReassigned
from arkray.leads.models import Lead
from arkray.pipeline import configuration, field_values
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import (
    CustomField,
    Opportunity,
    OpportunityCorrection,
    Pipeline,
    Stage,
)
from tests.authz_matrix import AUTHZ_MATRIX
from tests.factories import (
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    TeamAdminFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import key_header, run_concurrently, signed_in
from tests.security.test_admin_workspace import (
    BODIES,
    OBJECT_ROUTES,
    RANDOM_USER,
    WORKSPACE_ROUTES,
    call,
    concrete,
    envelope,
    has_record,
    personal_pipeline,
    stored_attachment,
    substitute,
)

pytestmark = pytest.mark.django_db

API = "/api/v1/workspaces"
TEAM = "all"  # Tara's "all" is her team
VALUES = {
    "tara": Decimal("131313.00"),
    "rahul": Decimal("242424.00"),
    "mohan": Decimal("353535.00"),
    "priya": Decimal("464646.00"),
    "kavya": Decimal("575757.00"),
    "omar": Decimal("686868.00"),
    "anita": Decimal("797979.00"),
}
TEAM_NAMES = ("tara", "rahul", "mohan")
OUTSIDERS = ("priya", "kavya", "omar", "anita")
# Routes of other modules (global search, Ask Arkray): their team behaviour is tested with
# them (tests/security/test_teams_ai.py). Their workspace segment is checked here all the same.
NOT_CRM = ("api/v1/workspaces/<str:workspace>/ask", "api/v1/workspaces/<str:workspace>/search")
CRM_ROUTES = [
    (route, method) for route, method in WORKSPACE_ROUTES if not route.startswith(NOT_CRM)
]


# --- the world -----------------------------------------------------------------------------------
def records_of(owner: Any, mark: str, value: Decimal) -> dict[str, Any]:
    lead = LeadFactory(owner=owner, first_name=f"{mark}-LEAD", organization_name=mark)
    opportunity = OpportunityFactory(
        lead=lead, title=f"{mark}-OPPORTUNITY", value=value, stage=default_stage("proposal")
    )
    note = NoteFactory(lead=lead, opportunity=opportunity, description=f"{mark}-NOTE")
    return {
        "lead": lead,
        "opportunity": opportunity,
        "task": TaskFactory(
            lead=lead, title=f"{mark}-TASK", due_at=timezone.now() + timedelta(days=1)
        ),
        "meeting": MeetingFactory(lead=lead, title=f"{mark}-MEETING"),
        "note": note,
        "pipeline": personal_pipeline(owner, f"{mark}-PIPELINE"),
        "attachment": stored_attachment(note, f"{mark}-FILE.txt", mark.encode()),
    }


@pytest.fixture
def world(
    admin, team_admin, team_member, team_member_2, user_b, other_team_admin, other_team_member
) -> dict[str, Any]:
    people = {
        "anita": admin,
        "tara": team_admin,
        "rahul": team_member,
        "mohan": team_member_2,
        "priya": user_b,
        "omar": other_team_admin,
        "kavya": other_team_member,
    }
    out: dict[str, Any] = dict(people)
    for name, person in people.items():
        out[f"{name}_records"] = records_of(person, f"{name.upper()}-ONLY", VALUES[name])
    return out


def ws(user: Any) -> str:
    return str(user.pk)


def traces(world: dict[str, Any], name: str) -> list[str]:
    """What would show one owner's data in a response: their marker, their amount, their id
    and every one of their records' ids."""
    records = world[f"{name}_records"]
    return [
        f"{name.upper()}-ONLY",
        str(VALUES[name]).split(".")[0],
        ws(world[name]),
        *(str(r.pk) for r in records.values()),
    ]


def outsider_traces(world: dict[str, Any], *names: str) -> list[str]:
    return [t for name in (names or OUTSIDERS) for t in traces(world, name)]


def state() -> dict[str, Any]:
    """Everything a write could change (opening a workspace is audited: not counted)."""
    return {
        "leads": list(Lead.objects.values_list("id", "version", "owner_id").order_by("id")),
        "opportunities": list(
            Opportunity.objects.values_list("id", "version", "owner_id").order_by("id")
        ),
        "activities": list(
            Activity.objects.values_list("id", "version", "owner_id").order_by("id")
        ),
        "pipelines": list(Pipeline.objects.values_list("id", "version").order_by("id")),
        "stages": Stage.objects.count(),
        "fields": CustomField.objects.count(),
        "attachments": list(Attachment.objects.values_list("id", "deleted_at").order_by("id")),
        "corrections": OpportunityCorrection.objects.count(),
        "audit": AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED).count(),
    }


def workspaces(world: dict[str, Any]) -> list[str]:
    """The two kinds of workspace Tara writes in: her team's, and a member's (Rahul's)."""
    return [TEAM, ws(world["rahul"])]


# --- the route matrix ---------------------------------------------------------------------------
@pytest.mark.parametrize("where", ["team", "member"])
@pytest.mark.parametrize(("route", "method"), CRM_ROUTES)
def test_tara_reaches_every_crm_route_of_her_team(route, method, where, team_admin, team_member):
    workspace = TEAM if where == "team" else ws(team_member)
    response = call(signed_in(team_admin), method, concrete(route, workspace))
    assert response.status_code not in (401, 403, 500), response.content
    if not has_record(route):
        assert response.status_code != 404, response.content


@pytest.mark.parametrize(("route", "method"), WORKSPACE_ROUTES)
def test_an_outsiders_workspace_is_the_same_404_as_a_user_who_does_not_exist(
    route, method, team_admin, user_b, other_team_admin, other_team_member, admin
):
    """Priya (no team), Kavya (another team), Omar (another team's administrator) and Anita
    (a super administrator): Tara learns nothing, on every route, whatever its method."""
    client = signed_in(team_admin)
    missing = call(client, method, concrete(route, RANDOM_USER))
    assert missing.status_code == 404
    for outsider in (user_b, other_team_member, other_team_admin, admin):
        found = call(client, method, concrete(route, ws(outsider)))
        assert found.status_code == 404, (outsider.first_name, found.content)
        assert envelope(found) == envelope(missing)


@pytest.mark.parametrize("where", ["team", "member"])
@pytest.mark.parametrize(("route", "method"), OBJECT_ROUTES)
def test_outsiders_records_through_tara_s_workspaces_are_not_found(world, route, method, where):
    """Every record route and method, in the team's workspace and in Rahul's: an outsider's
    record answers exactly like a record that exists nowhere, and nothing changes."""
    client = signed_in(world["tara"])
    workspace = TEAM if where == "team" else ws(world["rahul"])
    before = state()
    nowhere = call(client, method, concrete(route, workspace), BODIES.get(method))
    for name in OUTSIDERS:
        path = substitute(route, workspace, world[f"{name}_records"])
        foreign = call(client, method, path, BODIES.get(method))
        if foreign.status_code == 400:  # strict input refuses the body first, for both
            assert nowhere.status_code == 400, (name, nowhere.content)
        else:
            assert foreign.status_code == nowhere.status_code == 404, (name, foreign.content)
            assert envelope(foreign) == envelope(nowhere)
        leaked = [t for t in outsider_traces(world) if t in foreign.content.decode()]
        assert leaked == [], (name, leaked)
    assert state() == before


def outsider_writes(world: dict[str, Any], name: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Valid writes on one outsider's records: each would succeed in their own workspace."""
    r = world[f"{name}_records"]
    lead, deal, task, pipeline = r["lead"], r["opportunity"], r["task"], r["pipeline"]
    rahul = ws(world["rahul"])
    return [
        ("patch", f"/leads/{lead.pk}", {"version": lead.version, "city": "Hacked"}),
        ("post", f"/leads/{lead.pk}/status", {"version": lead.version, "status": "contacted"}),
        ("post", f"/leads/{lead.pk}/assign", {"version": lead.version, "owner": rahul}),
        ("post", f"/leads/{lead.pk}/archive", {"version": lead.version}),
        ("post", f"/leads/{lead.pk}/correction", {"version": lead.version, "city": "Hacked"}),
        ("patch", f"/opportunities/{deal.pk}", {"version": deal.version, "description": "x"}),
        (
            "post",
            f"/opportunities/{deal.pk}/move",
            {"version": deal.version, "stage": str(default_stage("qualified").pk)},
        ),
        ("post", f"/opportunities/{deal.pk}/assign", {"version": deal.version, "owner": rahul}),
        ("post", f"/opportunities/{deal.pk}/archive", {"version": deal.version}),
        ("patch", f"/activities/{task.pk}", {"version": task.version, "title": "Hacked"}),
        ("post", f"/activities/{task.pk}/complete", {"version": task.version}),
        ("post", f"/activities/{task.pk}/archive", {"version": task.version}),
        ("post", "/activities", {"type": "task", "lead": str(lead.pk), "title": "Hacked"}),
        ("patch", f"/pipelines/{pipeline.pk}", {"version": pipeline.version, "name": "Hacked"}),
        ("post", f"/pipelines/{pipeline.pk}/archive", {"version": pipeline.version}),
        ("delete", f"/attachments/{r['attachment'].pk}", {}),
    ]


@pytest.mark.parametrize("where", ["team", "member"])
def test_valid_writes_on_outsiders_records_are_not_found_and_change_nothing(world, where):
    client = signed_in(world["tara"])
    workspace = TEAM if where == "team" else ws(world["rahul"])
    before = state()
    for name in OUTSIDERS:
        for method, path, body in outsider_writes(world, name):
            response = call(client, method, f"{API}/{workspace}{path}", body)
            assert response.status_code == 404, (name, method, path, response.content)
        lead = world[f"{name}_records"]["lead"]
        created = client.post(
            f"{API}/{workspace}/opportunities",
            {"lead": str(lead.pk), "value": "1"},
            format="json",
            headers=key_header(),
        )
        assert created.status_code == 404, (name, created.content)
    assert state() == before


# --- reading: the team, and nothing else ---------------------------------------------------------
def read_paths(world: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    paths = [
        "",
        "/dashboard",
        "/leads",
        "/leads?archived=true",
        "/leads/duplicates?email=x@example.test",
        "/pipeline-board",
        "/pipeline-summary",
        "/opportunities",
        "/activities",
        "/activities?type=note",
        "/activity-summary",
        "/pipelines",
        "/pipelines?archived=true",
    ]
    for name in names:
        r = world[f"{name}_records"]
        lead, deal = r["lead"].pk, r["opportunity"].pk
        paths += [
            f"/leads/{lead}",
            f"/leads/{lead}/timeline",
            f"/opportunities?lead={lead}",
            f"/opportunities/{deal}",
            f"/opportunities/{deal}/history",
            f"/opportunities/{deal}/timeline",
            f"/opportunities/{deal}/notes",
            f"/opportunities/{deal}/negotiated-prices",
            f"/opportunities/{deal}/corrections",
            f"/pipelines/{r['pipeline'].pk}",
            f"/pipeline-board?pipeline={r['pipeline'].pk}",
            f"/attachments/{r['attachment'].pk}/download",
            *(f"/activities/{r[k].pk}" for k in ("task", "meeting", "note")),
        ]
    return paths


def bodies(client: Any, workspace: str, paths: list[str]) -> dict[str, str]:
    out = {}
    for path in paths:
        response = client.get(f"{API}/{workspace}{path}")
        assert response.status_code == 200, (path, response.content)
        out[path] = response.getvalue().decode()
    return out


def test_the_team_workspace_shows_the_team_and_nothing_of_anyone_else(world):
    found = bodies(signed_in(world["tara"]), TEAM, read_paths(world, TEAM_NAMES))
    for path, body in found.items():
        leaked = [t for t in outsider_traces(world) if t in body]
        assert leaked == [], (path, leaked)
    for name in TEAM_NAMES:
        assert f"{name.upper()}-ONLY-LEAD" in found["/leads"]
        assert f"{name.upper()}-ONLY-OPPORTUNITY" in found["/opportunities"]
        assert f"{name.upper()}-ONLY-PIPELINE" in found["/pipelines"]


def test_a_members_workspace_shows_that_member_only(world):
    found = bodies(signed_in(world["tara"]), ws(world["rahul"]), read_paths(world, ("rahul",)))
    for path, body in found.items():
        leaked = [t for t in outsider_traces(world, *OUTSIDERS, "tara", "mohan") if t in body]
        assert leaked == [], (path, leaked)
    assert "RAHUL-ONLY-LEAD" in found["/leads"]


def test_a_team_members_record_through_another_members_workspace_is_not_found(world):
    """Mohan's records are in Tara's team, but not in Rahul's workspace."""
    client = signed_in(world["tara"])
    mohan = world["mohan_records"]
    for path in (
        f"/leads/{mohan['lead'].pk}",
        f"/opportunities/{mohan['opportunity'].pk}",
        f"/activities/{mohan['task'].pk}",
        f"/pipelines/{mohan['pipeline'].pk}",
    ):
        assert client.get(f"{API}/{ws(world['rahul'])}{path}").status_code == 404, path
        assert client.get(f"{API}/{TEAM}{path}").status_code == 200, path


def test_dashboard_board_and_summary_count_the_team_only(world):
    client = signed_in(world["tara"])
    team_value = f"{sum(VALUES[n] for n in TEAM_NAMES):.2f}"
    dashboard = client.get(f"{API}/{TEAM}/dashboard").json()
    assert dashboard["leads"]["total"] == len(TEAM_NAMES)
    assert dashboard["pipeline"]["pipeline_value"] == team_value
    assert dashboard["pipeline"]["open_count"] == len(TEAM_NAMES)
    assert dashboard["activities"]["open_tasks"] == len(TEAM_NAMES)
    assert dashboard["activities"]["upcoming_meetings"] == len(TEAM_NAMES)
    summary = client.get(f"{API}/{TEAM}/pipeline-summary").json()["totals"]
    assert (summary["pipeline_value"], summary["open_count"]) == (team_value, len(TEAM_NAMES))
    board = client.get(f"{API}/{TEAM}/pipeline-board").json()
    assert (board["totals"]["pipeline_value"], board["totals"]["open_count"]) == (
        team_value,
        len(TEAM_NAMES),
    )
    proposal = next(c for c in board["columns"] if c["stage"]["key"] == "proposal")
    assert (proposal["count"], proposal["total_value"]) == (len(TEAM_NAMES), team_value)


def test_a_members_dashboard_counts_that_member_only(world):
    client = signed_in(world["tara"])
    dashboard = client.get(f"{API}/{ws(world['rahul'])}/dashboard").json()
    assert dashboard["leads"]["total"] == 1
    assert dashboard["pipeline"]["pipeline_value"] == f"{VALUES['rahul']:.2f}"
    assert dashboard["activities"]["open_tasks"] == 1


def test_the_super_administrators_organisation_is_unchanged(world):
    client = signed_in(world["anita"])
    everyone = f"{sum(VALUES.values()):.2f}"
    dashboard = client.get(f"{API}/all/dashboard").json()
    assert dashboard["leads"]["total"] == len(VALUES)
    assert dashboard["pipeline"]["pipeline_value"] == everyone
    leads = client.get(f"{API}/all/leads").getvalue().decode()
    for name in VALUES:
        assert f"{name.upper()}-ONLY-LEAD" in leads
    # Organisation-wide, Anita still gives a team member's lead to anyone.
    lead = world["rahul_records"]["lead"]
    response = client.post(
        f"{API}/all/leads/{lead.pk}/assign",
        {"version": lead.version, "owner": ws(world["priya"])},
        format="json",
    )
    assert response.status_code == 200, response.content
    assert Lead.objects.get(pk=lead.pk).owner_id == world["priya"].pk


# --- owner filters ------------------------------------------------------------------------------
OWNER_FILTERED = ["/leads", "/opportunities", "/activities", "/pipeline-summary", "/pipeline-board"]


@pytest.mark.parametrize("path", OWNER_FILTERED)
def test_an_owner_filter_in_the_team_narrows_within_it(world, path):
    client = signed_in(world["tara"])
    rahul = client.get(f"{API}/{TEAM}{path}?owner={ws(world['rahul'])}")
    assert rahul.status_code == 200, rahul.content
    body = rahul.getvalue().decode()
    leaked = [t for t in outsider_traces(world, *OUTSIDERS, "tara", "mohan") if t in body]
    assert leaked == []
    if path in ("/leads", "/opportunities"):
        assert "RAHUL-ONLY" in body
    nowhere = client.get(f"{API}/{TEAM}{path}?owner={RANDOM_USER}")
    for name in OUTSIDERS:
        outside = client.get(f"{API}/{TEAM}{path}?owner={ws(world[name])}")
        assert outside.status_code == 200, outside.content
        # The same answer as for an owner who exists nowhere: nothing (the paging links
        # carry the filter's own value, so they are compared without it).
        assert outside.json() == _without_links(nowhere.json(), ws(world[name]), RANDOM_USER)
        assert not [t for t in outsider_traces(world) if t in outside.getvalue().decode()]


def _without_links(body: Any, replacement: str, original: str) -> Any:
    """`body` (the answer for `original`) as it reads for `replacement`: only the owner id in
    its links differs."""
    return json.loads(json.dumps(body).replace(original, replacement))


@pytest.mark.parametrize("path", OWNER_FILTERED)
def test_an_owner_filter_in_a_members_workspace_is_still_refused(world, path):
    client = signed_in(world["tara"])
    response = client.get(f"{API}/{ws(world['rahul'])}{path}?owner={ws(world['rahul'])}")
    assert response.status_code == 400
    assert "owner" in response.json()["error"]["details"]


# --- writing within the team -------------------------------------------------------------------
def test_tara_changes_team_records_as_herself(world):
    tara, rahul = world["tara"], world["rahul"]
    client = signed_in(tara)
    for workspace in workspaces(world):
        lead = Lead.objects.get(pk=world["rahul_records"]["lead"].pk)
        edited = client.patch(
            f"{API}/{workspace}/leads/{lead.pk}",
            {"version": lead.version, "city": f"Pune {workspace[:4]}"},
            format="json",
        )
        assert edited.status_code == 200, edited.content
        task = Activity.objects.get(pk=world["rahul_records"]["task"].pk)
        renamed = client.patch(
            f"{API}/{workspace}/activities/{task.pk}",
            {"version": task.version, "title": f"Call {workspace[:4]}"},
            format="json",
        )
        assert renamed.status_code == 200, renamed.content
    writes = AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED)
    assert set(writes.values_list("actor_id", flat=True)) == {tara.pk}
    assert set(writes.values_list("subject_user_id", flat=True)) == {rahul.pk}
    workspaces_audited = {e.metadata["workspace"] for e in writes}
    assert workspaces_audited == {"team", "user"}


def test_new_work_in_the_team_belongs_to_the_leads_owner(world):
    tara, mohan = world["tara"], world["mohan"]
    lead = world["mohan_records"]["lead"]
    task = signed_in(tara).post(
        f"{API}/{TEAM}/activities",
        {"type": "task", "lead": str(lead.pk), "title": "Follow up"},
        format="json",
    )
    assert task.status_code == 201, task.content
    assert (task.json()["owner"]["id"], task.json()["created_by"]["id"]) == (ws(mohan), ws(tara))


# --- creating for someone: within the team only ---------------------------------------------
def owner_error(response: Any) -> Any:
    return response.json()["error"]["details"]["owner"]


def test_creating_in_the_team_needs_an_owner(world):
    client = signed_in(world["tara"])
    lead = client.post(f"{API}/{TEAM}/leads", {"first_name": "Asha"}, format="json")
    assert lead.status_code == 400
    assert owner_error(lead) == [lead_services.OWNER_REQUIRED]
    deal = client.post(
        f"{API}/{TEAM}/opportunities",
        {"value": "1000", "customer_name": "Asha"},
        format="json",
        headers=key_header(),
    )
    assert deal.status_code == 400
    assert owner_error(deal) == [pipeline_services.OWNER_REQUIRED]


@pytest.mark.parametrize("who", ["tara", "rahul", "mohan"])
def test_creating_in_the_team_for_a_member_or_herself(world, who):
    client = signed_in(world["tara"])
    owner = ws(world[who])
    lead = client.post(f"{API}/{TEAM}/leads", {"first_name": "Asha", "owner": owner}, format="json")
    assert lead.status_code == 201, lead.content
    assert (lead.json()["owner"]["id"], lead.json()["created_by"]["id"]) == (
        owner,
        ws(world["tara"]),
    )
    deal = client.post(
        f"{API}/{TEAM}/opportunities",
        {"value": "1000", "customer_name": "Asha", "owner": owner},
        format="json",
        headers=key_header(),
    )
    assert deal.status_code == 201, deal.content
    assert deal.json()["owner"]["id"] == owner
    assert Lead.objects.get(pk=deal.json()["lead"]["id"]).owner_id == UUID(owner)


def test_creating_in_the_team_for_anyone_else_is_one_answer_and_writes_nothing(world):
    client = signed_in(world["tara"])
    deactivated = UserFactory(manager=world["tara"], is_active=False)
    before = state()
    answers = []
    for owner in [
        *(ws(world[n]) for n in OUTSIDERS),
        ws(deactivated),
        RANDOM_USER,
    ]:
        lead = client.post(
            f"{API}/{TEAM}/leads", {"first_name": "Asha", "owner": owner}, format="json"
        )
        deal = client.post(
            f"{API}/{TEAM}/opportunities",
            {"value": "1000", "customer_name": "Asha", "owner": owner},
            format="json",
            headers=key_header(),
        )
        for response in (lead, deal):
            assert response.status_code == 400, (owner, response.content)
            assert owner_error(response) == [lead_services.OWNER_NOT_ASSIGNABLE]
            answers.append(envelope(response))
    assert all(answer == answers[0] for answer in answers)
    assert state() == before


def test_creating_in_a_members_workspace_is_theirs(world):
    client = signed_in(world["tara"])
    rahul = ws(world["rahul"])
    lead = client.post(f"{API}/{rahul}/leads", {"first_name": "Asha"}, format="json")
    assert lead.status_code == 201, lead.content
    assert lead.json()["owner"]["id"] == rahul
    elsewhere = client.post(
        f"{API}/{rahul}/leads", {"first_name": "Asha", "owner": ws(world["mohan"])}, format="json"
    )
    assert elsewhere.status_code == 400
    assert owner_error(elsewhere) == [lead_services.OWNER_IS_SUBJECT_ONLY]


def test_a_retry_in_the_team_replays_the_first_create(world):
    """The idempotency digest of a team's workspace (no single subject) is stable."""
    client = signed_in(world["tara"])
    body = {"value": "1000", "customer_name": "Asha", "owner": ws(world["rahul"])}
    headers = key_header()
    first = client.post(f"{API}/{TEAM}/opportunities", body, format="json", headers=headers)
    again = client.post(f"{API}/{TEAM}/opportunities", body, format="json", headers=headers)
    assert first.status_code == 201, first.content
    assert again.status_code == 201, again.content
    assert again["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]
    lead_key = key_header()
    lead_body = {"first_name": "Asha", "owner": ws(world["mohan"])}
    made = client.post(f"{API}/{TEAM}/leads", lead_body, format="json", headers=lead_key)
    replay = client.post(f"{API}/{TEAM}/leads", lead_body, format="json", headers=lead_key)
    assert replay["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == made.json()["id"]
    task_key = key_header()
    task_body = {"type": "task", "lead": made.json()["id"], "title": "Call"}
    task = client.post(f"{API}/{TEAM}/activities", task_body, format="json", headers=task_key)
    retry = client.post(f"{API}/{TEAM}/activities", task_body, format="json", headers=task_key)
    assert retry["Idempotent-Replayed"] == "true"
    assert retry.json()["id"] == task.json()["id"]


# --- assignment: within the team only ---------------------------------------------------------
ASSIGNED = {
    "lead": lambda r: (f"/leads/{r['lead'].pk}/assign", Lead, r["lead"].pk),
    "opportunity": lambda r: (
        f"/opportunities/{r['opportunity'].pk}/assign",
        Opportunity,
        r["opportunity"].pk,
    ),
}


def _version(model: Any, pk: Any) -> int:
    return int(model.objects.values_list("version", flat=True).get(pk=pk))


@pytest.mark.parametrize("record", ["lead", "opportunity"])
@pytest.mark.parametrize("where", ["team", "member"])
@pytest.mark.parametrize("to", ["mohan", "tara"])
def test_assigning_within_the_team(world, record, where, to):
    path, model, pk = ASSIGNED[record](world["rahul_records"])
    workspace = TEAM if where == "team" else ws(world["rahul"])
    response = signed_in(world["tara"]).post(
        f"{API}/{workspace}{path}",
        {"version": _version(model, pk), "owner": ws(world[to])},
        format="json",
    )
    assert response.status_code == 200, response.content
    assert model.objects.get(pk=pk).owner_id == world[to].pk


@pytest.mark.parametrize("record", ["lead", "opportunity"])
@pytest.mark.parametrize("where", ["team", "member"])
def test_assigning_to_anyone_outside_the_team_is_one_answer_and_changes_nothing(
    world, record, where
):
    path, model, pk = ASSIGNED[record](world["rahul_records"])
    workspace = TEAM if where == "team" else ws(world["rahul"])
    client = signed_in(world["tara"])
    deactivated = UserFactory(manager=world["tara"], is_active=False)
    before = state()
    answers = []
    for owner in [*(ws(world[n]) for n in OUTSIDERS), ws(deactivated), RANDOM_USER]:
        response = client.post(
            f"{API}/{workspace}{path}",
            {"version": _version(model, pk), "owner": owner},
            format="json",
        )
        assert response.status_code == 400, (owner, response.content)
        assert owner_error(response) == [lead_services.OWNER_NOT_ASSIGNABLE]
        answers.append(envelope(response))
    assert all(answer == answers[0] for answer in answers)
    assert state() == before


def test_a_sales_user_still_assigns_nothing(world):
    path, model, pk = ASSIGNED["lead"](world["rahul_records"])
    response = signed_in(world["rahul"]).post(
        f"{API}/me{path}", {"version": _version(model, pk), "owner": ws(world["mohan"])}
    )
    assert response.status_code == 403


def test_the_assignment_service_checks_the_team_itself(world, monkeypatch):
    """A direct service call (an import, a tool) with a scope wider than the team, at the
    moment after authorize_write read the team (patched to agree): the new owner is checked
    against the team again, under the member's lock."""
    tara, rahul, priya = world["tara"], world["rahul"], world["priya"]
    monkeypatch.setattr(teams, "all_in_team", lambda admin_id, ids: True)
    wide = AccessScope.team(tara.pk, {rahul.pk, priya.pk})
    lead = world["rahul_records"]["lead"]
    with pytest.raises(InvalidInputError) as refused:
        lead_services.reassign_lead(
            actor=tara, scope=wide, lead_id=lead.pk, version=lead.version, owner_id=priya.pk
        )
    assert refused.value.details == {"owner": [lead_services.OWNER_NOT_ASSIGNABLE]}
    with pytest.raises(InvalidInputError):
        lead_services.create_lead(
            actor=tara, scope=wide, fields={"first_name": "Asha"}, owner_id=priya.pk
        )
    assert Lead.objects.get(pk=lead.pk).owner_id == rahul.pk


def test_a_record_created_in_a_teams_workspace_belongs_to_that_workspace(world):
    """A scope resolved before Mohan joined (it lists Tara and Rahul only): creating there for
    Mohan is refused, though he is in the team now; the record would land outside the very
    workspace it was created in."""
    tara, mohan = world["tara"], world["mohan"]
    earlier = AccessScope.team(tara.pk, {world["rahul"].pk})
    before = state()
    with pytest.raises(InvalidInputError) as refused:
        lead_services.create_lead(
            actor=tara, scope=earlier, fields={"first_name": "Asha"}, owner_id=mohan.pk
        )
    assert refused.value.details == {"owner": [lead_services.OWNER_NOT_ASSIGNABLE]}
    assert state() == before


def test_a_members_workspace_written_after_they_left_the_team_is_refused(world, monkeypatch):
    """Rahul leaves Tara's team after her request opened his workspace (authorize_write read
    the team before: patched to agree): creating for him is refused under his row's lock."""
    tara, rahul = world["tara"], world["rahul"]
    scope = AccessScope.for_user(tara.pk, rahul.pk)
    rahul.manager = None
    rahul.save(update_fields=["manager"])
    monkeypatch.setattr(teams, "all_in_team", lambda admin_id, ids: True)
    before = state()
    with pytest.raises(PermissionDeniedError):
        lead_services.create_lead(actor=tara, scope=scope, fields={"first_name": "Asha"})
    assert state() == before


def test_a_member_who_left_the_team_is_out_of_reach_at_the_next_request(world):
    client = signed_in(world["tara"])
    rahul = world["rahul"]
    lead = world["rahul_records"]["lead"]
    rahul.manager = None
    rahul.save(update_fields=["manager"])
    assert client.get(f"{API}/{TEAM}/leads/{lead.pk}").status_code == 404
    assert client.get(f"{API}/{ws(rahul)}/leads").status_code == 404
    mohans = world["mohan_records"]["lead"]
    response = client.post(
        f"{API}/{TEAM}/leads/{mohans.pk}/assign",
        {"version": mohans.version, "owner": ws(rahul)},
        format="json",
    )
    assert response.status_code == 400
    assert owner_error(response) == [lead_services.OWNER_NOT_ASSIGNABLE]


# --- pipeline configuration ---------------------------------------------------------------------
NEW_PIPELINE = {
    "name": "Tenders",
    "stages": [
        {"name": "Qualify", "type": "open", "probability": "10"},
        {"name": "Won", "type": "won"},
        {"name": "Lost", "type": "lost"},
    ],
    "custom_fields": [{"name": "Notes", "type": "text"}],
}


def config_writes(pipeline: Pipeline) -> list[tuple[str, str, dict[str, Any]]]:
    stages = [
        {"id": str(s.pk), "name": f"{s.name} 2", "type": s.stage_type, "probability": "20"}
        if s.category == "open"
        else {"id": str(s.pk), "name": f"{s.name} 2", "type": s.stage_type}
        for s in pipeline.stages.filter(is_active=True).order_by("position")
    ]
    base = f"/pipelines/{pipeline.pk}"
    return [
        ("patch", base, {"version": pipeline.version, "name": "Renamed"}),
        ("put", f"{base}/stages", {"version": pipeline.version, "stages": stages}),
        (
            "put",
            f"{base}/fields",
            {"version": pipeline.version, "custom_fields": [{"name": "Extra", "type": "text"}]},
        ),
        ("post", f"{base}/archive", {"version": pipeline.version}),
    ]


def test_no_pipeline_is_configured_from_the_team_workspace(world):
    client = signed_in(world["tara"])
    shared = Pipeline.objects.get(is_default=True)
    rahuls = world["rahul_records"]["pipeline"]
    archived = Pipeline.objects.create(
        key="pmohanarchived", name="MOHAN-ARCHIVED", owner=world["mohan"], is_active=False
    )
    removed = CustomField.objects.create(
        pipeline=rahuls, name="Old notes", field_type="text", is_active=False, position=9
    )
    before = state()
    created = client.post(f"{API}/{TEAM}/pipelines", NEW_PIPELINE, format="json")
    assert created.status_code == 403, created.content
    assert created.json()["error"]["message"] == configuration.TEAM_CONFIGURES_NOTHING
    for pipeline in (shared, rahuls):
        for method, path, body in config_writes(pipeline):
            response = call(client, method, f"{API}/{TEAM}{path}", body)
            assert response.status_code == 403, (pipeline.name, path, response.content)
            assert response.json()["error"]["message"] == configuration.TEAM_CONFIGURES_NOTHING
    restored = client.post(
        f"{API}/{TEAM}/pipelines/{archived.pk}/restore", {"version": 1}, format="json"
    )
    assert restored.status_code == 403
    values = client.post(
        f"{API}/{TEAM}/pipelines/{rahuls.pk}/fields/{removed.pk}/delete-values",
        {"confirm_name": "Old notes"},
        format="json",
    )
    assert values.status_code == 403
    assert state() == before
    listed = client.get(f"{API}/{TEAM}/pipelines").json()["results"]
    assert listed
    assert not any(p["can_manage"] for p in listed)


def test_a_members_personal_pipeline_is_configured_from_their_workspace(world):
    client = signed_in(world["tara"])
    rahul = ws(world["rahul"])
    created = client.post(f"{API}/{rahul}/pipelines", NEW_PIPELINE, format="json")
    assert created.status_code == 201, created.content
    assert created.json()["owner"]["id"] == rahul
    pipeline = world["rahul_records"]["pipeline"]
    for method, path, body in config_writes(pipeline):
        pipeline.refresh_from_db()
        body = {**body, "version": pipeline.version}
        response = call(client, method, f"{API}/{rahul}{path}", body)
        assert response.status_code == 200, (path, response.content)
    actors = AuditEvent.objects.filter(action__startswith="pipeline.").values_list(
        "actor_id", "subject_user_id"
    )
    assert set(actors) == {(world["tara"].pk, world["rahul"].pk)}


def test_a_shared_pipeline_is_never_configured_by_a_team_administrator(world):
    client = signed_in(world["tara"])
    shared = Pipeline.objects.get(is_default=True)
    before = state()
    for workspace in (ws(world["rahul"]), "me"):
        for method, path, body in config_writes(shared):
            response = call(client, method, f"{API}/{workspace}{path}", body)
            assert response.status_code == 403, (workspace, path, response.content)
    assert state() == before


def manageable(client: Any, workspace: str) -> dict[str, bool]:
    """`can_manage` of every pipeline a workspace lists, by name, and the same flag on each
    pipeline's own page (the board's Settings follow it: they must agree with the services)."""
    listed = client.get(f"{API}/{workspace}/pipelines").json()["results"]
    flags = {p["name"]: p["can_manage"] for p in listed}
    for p in listed:
        detail = client.get(f"{API}/{workspace}/pipelines/{p['id']}").json()
        assert detail["can_manage"] == p["can_manage"], (workspace, p["name"])
    return flags


def test_can_manage_agrees_with_the_services_in_every_workspace(world):
    """In the team's workspace nothing is manageable (configuration is refused there); in a
    member's, that member's personal pipelines are, shared ones and other people's never; in
    her own, her own. A super administrator's flags are unchanged."""
    tara, rahul = world["tara"], world["rahul"]
    # Rahul's deal in Mohan's personal pipeline (a deal stays in its pipeline when its lead
    # moves): Rahul's workspace lists that pipeline too, and must not offer to configure it.
    mohans = world["mohan_records"]["pipeline"]
    OpportunityFactory(lead=LeadFactory(owner=rahul), stage=mohans.stages.get())
    shared = Pipeline.objects.get(is_default=True).name
    client = signed_in(tara)

    team = manageable(client, TEAM)
    assert {shared, "TARA-ONLY-PIPELINE", "RAHUL-ONLY-PIPELINE", "MOHAN-ONLY-PIPELINE"} <= set(team)
    assert not any(team.values())

    member = manageable(client, ws(rahul))
    assert member == {shared: False, "RAHUL-ONLY-PIPELINE": True, "MOHAN-ONLY-PIPELINE": False}

    own = manageable(client, "me")
    assert own == {shared: False, "TARA-ONLY-PIPELINE": True}

    organisation = manageable(signed_in(world["anita"]), "all")
    assert organisation[shared] is True
    assert organisation["PRIYA-ONLY-PIPELINE"] is True
    assert all(organisation.values())


def test_no_team_scope_reaches_the_field_value_deletion(world):
    """Defence in depth: whatever a caller passes, a team's scope never becomes "every deal"
    (the deletion job's meaning of no owner), not even for a super administrator's call."""
    rahuls = world["rahul_records"]["pipeline"]
    field = CustomField.objects.create(
        pipeline=rahuls, name="Old notes", field_type="text", is_active=False, position=9
    )
    for actor in (world["tara"], world["anita"]):
        scope = AccessScope.team(actor.pk, {world["rahul"].pk})
        with pytest.raises(PermissionDeniedError):
            field_values.require_deletable(scope, rahuls.pk, [field.pk])
        with pytest.raises(PermissionDeniedError):
            field_values.request_for_fields(actor.pk, [field], scope)
        with pytest.raises(PermissionDeniedError):
            configuration.create_pipeline(
                actor=actor, scope=scope, name="Tenders", stages=NEW_PIPELINE["stages"]
            )
    assert state()["fields"] == CustomField.objects.count()
    assert not AuditEvent.objects.filter(action=field_values.AUDIT_REQUESTED).exists()


# --- correcting a closed deal ---------------------------------------------------------------------
def won_deal(owner: Any) -> Any:
    return OpportunityFactory(lead=LeadFactory(owner=owner), stage=default_stage("won"))


def correct(client: Any, workspace: str, deal: Opportunity, value: str = "1.00") -> Any:
    return client.post(
        f"{API}/{workspace}/opportunities/{deal.pk}/correct",
        {"version": _version(Opportunity, deal.pk), "value": value, "reason": "Agreed discount."},
        format="json",
    )


@pytest.mark.parametrize(("owner", "where"), [("rahul", TEAM), ("rahul", "member"), ("tara", "me")])
def test_tara_corrects_a_closed_deal_of_her_team(world, owner, where):
    deal = won_deal(world[owner])
    workspace = ws(world["rahul"]) if where == "member" else where
    response = correct(signed_in(world["tara"]), workspace, deal)
    assert response.status_code == 200, response.content
    assert Opportunity.objects.get(pk=deal.pk).value == Decimal("1.00")
    assert OpportunityCorrection.objects.get().actor_id == world["tara"].pk


def test_tara_never_corrects_an_outsiders_closed_deal(world):
    client = signed_in(world["tara"])
    for name in OUTSIDERS:
        deal = won_deal(world[name])
        for workspace in workspaces(world):
            response = correct(client, workspace, deal)
            assert response.status_code == 404, (name, workspace, response.content)
    assert not OpportunityCorrection.objects.exists()


def test_a_sales_user_still_corrects_nothing(world):
    deal = won_deal(world["rahul"])
    assert correct(signed_in(world["rahul"]), "me", deal).status_code == 403
    assert not OpportunityCorrection.objects.exists()


def test_the_correction_service_checks_the_deals_owner_itself(world, monkeypatch):
    """A scope wider than the team (a direct service call), at the moment after
    authorize_write read the team: the deal's owner is checked against the team again,
    under the owner's row lock, and a deal out of reach is not found."""
    tara, priya = world["tara"], world["priya"]
    monkeypatch.setattr(teams, "all_in_team", lambda admin_id, ids: True)
    deal = won_deal(priya)
    wide = AccessScope.team(tara.pk, {world["rahul"].pk, priya.pk})
    with pytest.raises(NotFoundError):
        pipeline_services.correct_closed_deal(
            actor=tara,
            scope=wide,
            opportunity_id=deal.pk,
            version=deal.version,
            reason="Agreed discount.",
            changes={"value": Decimal("1.00")},
        )
    assert not OpportunityCorrection.objects.exists()


# --- a closed deal whose customer left the team ------------------------------------------------
def test_reopening_a_deal_whose_customer_left_the_team_says_where(world):
    """Rahul's won deal stays his after its customer went to Priya: Tara sees it (customer
    hidden) but can't reopen it in her team, and is told to ask a super administrator."""
    lead = LeadFactory(owner=world["rahul"])
    deal = OpportunityFactory(lead=lead, stage=default_stage("won"))
    Lead.objects.filter(pk=lead.pk).update(owner=world["priya"])
    response = signed_in(world["tara"]).post(
        f"{API}/{TEAM}/opportunities/{deal.pk}/move",
        {"version": deal.version, "stage": str(default_stage("proposal").pk)},
        format="json",
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["message"] == pipeline_services.REOPEN_ELSEWHERE_TEAM
    assert Opportunity.objects.get(pk=deal.pk).status == "won"


def test_the_customer_left_messages_fit_where_the_team_admin_is(world):
    """Security review of ADR-0035: in her team's view the new owner is outside the team, so
    she is told to ask a super admin, never pointed at the view she is in; in a member's
    workspace the team's view may still reach the new owner (Mohan), so it says so."""
    from arkray.activities import services as activity_services

    lead = LeadFactory(owner=world["rahul"])
    deal = OpportunityFactory(lead=lead, stage=default_stage("won"))
    Lead.objects.filter(pk=lead.pk).update(owner=world["priya"])
    client = signed_in(world["tara"])
    rahul = ws(world["rahul"])
    move = {"version": deal.version, "stage": str(default_stage("proposal").pk)}
    task = {"type": "task", "opportunity": str(deal.pk), "title": "Follow up"}
    expected = [
        (TEAM, "move", move, pipeline_services.REOPEN_ELSEWHERE_TEAM),
        (rahul, "move", move, pipeline_services.REOPEN_ELSEWHERE_MEMBER),
        (TEAM, "task", task, activity_services.LEAD_ELSEWHERE_TEAM),
        (rahul, "task", task, activity_services.LEAD_ELSEWHERE_MEMBER),
    ]
    for workspace, action, body, message in expected:
        if action == "move":
            path = f"{API}/{workspace}/opportunities/{deal.pk}/move"
            response = client.post(path, body, format="json")
        else:
            path = f"{API}/{workspace}/activities"
            response = client.post(path, body, format="json", headers=key_header())
        assert response.status_code == 422, (workspace, action, response.content)
        assert response.json()["error"]["message"] == message, (workspace, action)
    assert Opportunity.objects.get(pk=deal.pk).status == "won"
    assert not Activity.objects.filter(opportunity=deal).exists()


# --- races: a team move against an assignment (real threads, separate connections) ----------------
def _race_world() -> dict[str, Any]:
    tara = TeamAdminFactory()
    rahul = UserFactory(manager=tara)
    mohan = UserFactory(manager=tara)
    return {"tara": tara, "rahul": rahul, "mohan": mohan, "lead": LeadFactory(owner=mohan)}


def _move_out_of_the_team(user_id: Any) -> None:
    """A super administrator's team move as identity.services makes it: the user's row is
    updated (its lock, FOR NO KEY UPDATE, conflicts with an assignment's FOR SHARE)."""
    with connection.cursor() as cursor:
        cursor.execute("UPDATE identity_user SET manager_id = NULL WHERE id = %s", [user_id])


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_team_move_first_the_assignment_waits_then_is_refused():
    w = _race_world()
    client = signed_in(w["tara"])
    moved = threading.Event()
    times: dict[str, float] = {}

    def move() -> None:
        with transaction.atomic():
            _move_out_of_the_team(w["rahul"].pk)
            times["locked"] = time.monotonic()
            moved.set()
            time.sleep(0.8)  # Rahul's row stays locked by the uncommitted move

    def assign() -> Any:
        assert moved.wait(10)
        response = client.post(
            f"{API}/{TEAM}/leads/{w['lead'].pk}/assign",
            {"version": w["lead"].version, "owner": str(w["rahul"].pk)},
            format="json",
        )
        times["assigned"] = time.monotonic()
        return response

    _, response = run_concurrently(move, assign)
    assert not isinstance(response, BaseException), response
    assert response.status_code == 400, response.content
    assert owner_error(response) == [lead_services.OWNER_NOT_ASSIGNABLE]
    assert times["assigned"] - times["locked"] >= 0.7  # it waited for the move to commit
    assert Lead.objects.get(pk=w["lead"].pk).owner_id == w["mohan"].pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
def test_assignment_first_the_team_move_waits_for_it():
    w = _race_world()
    client = signed_in(w["tara"])
    locked = threading.Event()
    times: dict[str, float] = {}

    def slow(_event: object) -> None:
        locked.set()
        time.sleep(0.8)  # inside the assignment's transaction, holding Rahul's row FOR SHARE

    def assign() -> Any:
        with subscribed(LeadReassigned, slow):
            return client.post(
                f"{API}/{TEAM}/leads/{w['lead'].pk}/assign",
                {"version": w["lead"].version, "owner": str(w["rahul"].pk)},
                format="json",
            )

    def move() -> None:
        assert locked.wait(10)
        started = time.monotonic()
        with transaction.atomic():
            _move_out_of_the_team(w["rahul"].pk)
        times["waited"] = time.monotonic() - started

    response, moved = run_concurrently(assign, move)
    assert not isinstance(moved, BaseException), moved
    assert response.status_code == 200, response.content
    assert times["waited"] >= 0.5  # the move waited for the assignment to commit
    assert Lead.objects.get(pk=w["lead"].pk).owner_id == w["rahul"].pk
    # The move took effect at Tara's next request: Rahul and his new lead are out of reach.
    assert client.get(f"{API}/{TEAM}/leads/{w['lead'].pk}").status_code == 404


def test_every_workspace_route_is_covered():
    """The matrix tests above run over every workspace route of the authorization matrix."""
    routes = {route for route in AUTHZ_MATRIX if "<str:workspace>" in route}
    assert {route for route, _ in WORKSPACE_ROUTES} == routes
    assert {route for route, _ in OBJECT_ROUTES} == {r for r in routes if has_record(r)}
