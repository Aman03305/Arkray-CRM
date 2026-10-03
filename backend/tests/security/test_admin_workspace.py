"""Phase 6: the administrator's user workspace (docs/admin-user-workspace.md).

Admin Anita opens Rahul's CRM through /api/v1/workspaces/{rahul}/...: the same endpoints as
everyone's own workspace, authorised by resolve_workspace. Anita stays the actor of every
change; Rahul is only the subject (whose records they are). Every record below carries its
owner's marker (RAHUL-ONLY-*, PRIYA-ONLY-*, ₹111,111 / ₹888,888) so any leak is visible.
"""

from __future__ import annotations

import json
import re
import uuid
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.activities.models import Activity
from arkray.audit.models import AuditEvent
from arkray.identity import services as identity_services
from arkray.identity.workspaces import AUDIT_ACTION_WORKSPACE_ACCESSED
from arkray.leads.models import Lead
from arkray.leads.services import SUBJECT_NOT_ASSIGNABLE
from arkray.pipeline.models import Opportunity, StageHistory
from tests.authz_matrix import AUTHZ_MATRIX
from tests.factories import (
    AdminFactory,
    LeadFactory,
    MeetingFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    default_stage,
)
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

API = "/api/v1/workspaces"
RANDOM_USER = "0b9a4c6e-5d4f-4a3b-9c2d-1e0f2a3b4c5d"  # exists nowhere
MISSING_RECORD = "5a1e4d2c-0000-4000-8000-00000000abcd"
RECORD_PARAMS = ("<uuid:lead_id>", "<uuid:opportunity_id>", "<uuid:activity_id>")
WORKSPACE_ROUTES = [
    *sorted(
        (route, method)
        for route, rule in AUTHZ_MATRIX.items()
        if "<str:workspace>" in route and route != "api/v1/workspaces/<str:workspace>"
        for method in rule.methods
    ),
    ("api/v1/workspaces/<str:workspace>", "GET"),
]


def concrete(route: str, workspace: str, record: str = MISSING_RECORD) -> str:
    path = route.replace("<str:workspace>", workspace)
    for param in RECORD_PARAMS:
        path = path.replace(param, record)
    return "/" + path


def call(client: APIClient, method: str, path: str, body=None):
    return getattr(client, method.lower())(path, body or {}, format="json")


def has_record(route: str) -> bool:
    return any(param in route for param in RECORD_PARAMS)


def envelope(response) -> dict[str, Any]:
    """The error body without its per-request id: what tells two 404s apart, if anything."""
    body: dict[str, Any] = response.json()
    body["error"].pop("request_id", None)
    return body


# --- the world: two users' CRMs, every record marked ----------------------------------------------
@pytest.fixture
def world(admin, user_a, user_b):
    """user_a is Rahul, user_b is Priya (conftest)."""
    out = {"admin": admin, "rahul": user_a, "priya": user_b}
    for key, owner, mark, value in [
        ("rahul", user_a, "RAHUL-ONLY", Decimal("111111.00")),
        ("priya", user_b, "PRIYA-ONLY", Decimal("888888.00")),
    ]:
        lead = LeadFactory(owner=owner, first_name=f"{mark}-LEAD", organization_name=mark)
        opportunity = OpportunityFactory(
            lead=lead, title=f"{mark}-OPPORTUNITY", value=value, stage=default_stage("proposal")
        )
        soon = timezone.now() + timedelta(days=1)
        out[key + "_records"] = {
            "lead": lead,
            "opportunity": opportunity,
            "task": TaskFactory(lead=lead, title=f"{mark}-TASK", due_at=soon),
            "meeting": MeetingFactory(lead=lead, title=f"{mark}-MEETING"),
            "note": NoteFactory(lead=lead, description=f"{mark}-NOTE"),
        }
    return out


def ws(user) -> str:
    return str(user.pk)


def reads(world, workspace: str, each_request=nullcontext) -> dict[str, str]:
    """Every read of one workspace, by path. `each_request` wraps every request (e.g. to
    commit it on its own, as the server does)."""
    records = world["rahul_records"] if workspace == ws(world["rahul"]) else world["priya_records"]
    lead, opportunity = records["lead"].pk, records["opportunity"].pk
    paths = [
        "",
        "/dashboard",
        "/leads",
        "/leads?archived=true",
        f"/leads/{lead}",
        f"/leads/{lead}/timeline",
        "/leads/duplicates?email=x@example.test",
        "/pipeline-board",
        "/pipeline-summary",
        "/opportunities",
        f"/opportunities?lead={lead}",
        f"/opportunities/{opportunity}",
        f"/opportunities/{opportunity}/history",
        f"/opportunities/{opportunity}/timeline",
        "/activities",
        "/activities?type=note",
        f"/activities?lead={lead}",
        "/activity-summary",
    ] + [f"/activities/{records[k].pk}" for k in ("task", "meeting", "note")]
    client = signed_in(world["admin"])
    out = {}
    for path in paths:
        with each_request():
            response = client.get(f"{API}/{workspace}{path}")
        assert response.status_code == 200, (path, response.content)
        out[path] = response.content.decode()
    return out


def priya_traces(world) -> list[str]:
    records = world["priya_records"]
    return ["PRIYA-ONLY", "888888", str(world["priya"].pk)] + [str(r.pk) for r in records.values()]


def rahul_traces(world) -> list[str]:
    records = world["rahul_records"]
    return ["RAHUL-ONLY", "111111", str(world["rahul"].pk)] + [str(r.pk) for r in records.values()]


# --- the authorization matrix, for a selected user's workspace ------------------------------------
@pytest.mark.parametrize(("route", "method"), WORKSPACE_ROUTES)
def test_anonymous_callers_get_nothing_from_a_selected_workspace(route, method, user_b):
    response = call(APIClient(), method, concrete(route, ws(user_b)))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


@pytest.mark.parametrize(("route", "method"), WORKSPACE_ROUTES)
def test_another_sales_user_typing_the_url_learns_nothing(route, method, user_a, user_b):
    """User A on User B's workspace URL: the same answer as for a user who doesn't exist
    (capability routes refuse before the workspace is even looked at)."""
    client = signed_in(user_a)
    existing = call(client, method, concrete(route, ws(user_b)))
    missing = call(client, method, concrete(route, RANDOM_USER))
    if AUTHZ_MATRIX[route].access.startswith("capability:"):
        assert existing.status_code == missing.status_code == 403
    else:
        assert existing.status_code == missing.status_code == 404
    assert envelope(existing) == envelope(missing)


@pytest.mark.parametrize(("route", "method"), WORKSPACE_ROUTES)
def test_a_user_typing_their_own_id_gets_their_own_workspace(route, method, user_b):
    response = call(signed_in(user_b), method, concrete(route, ws(user_b)))
    if AUTHZ_MATRIX[route].access.startswith("capability:"):
        assert response.status_code == 403
    elif has_record(route):
        # The workspace opened; the (missing) record or the empty body is what's refused.
        assert response.status_code in (400, 404), response.content
    else:
        assert response.status_code not in (401, 403, 404), response.content


@pytest.mark.parametrize(("route", "method"), WORKSPACE_ROUTES)
def test_the_admin_reaches_every_route_of_a_selected_workspace(route, method, admin, user_b):
    response = call(signed_in(admin), method, concrete(route, ws(user_b)))
    assert response.status_code not in (401, 403), response.content
    if not has_record(route):
        assert response.status_code != 404, response.content


@pytest.mark.parametrize(("route", "method"), WORKSPACE_ROUTES)
def test_for_the_admin_a_missing_user_is_a_plain_404_on_every_route(route, method, admin):
    response = call(signed_in(admin), method, concrete(route, RANDOM_USER))
    assert response.status_code == 404
    assert envelope(response) == {
        "error": {"code": "not_found", "message": "Not found.", "details": None}
    }


@pytest.mark.parametrize(
    "segment",
    [
        "ME",
        "All",
        "not-a-uuid",
        "%7B" + RANDOM_USER + "%7D",
        RANDOM_USER.replace("-", ""),
        "urn:uuid:" + RANDOM_USER,
        "..%2F..%2Fadmin%2Fusers",
        RANDOM_USER + "%00",
        " " + RANDOM_USER,
    ],
)
def test_malformed_or_ambiguous_workspace_segments_are_404(admin, user_b, segment):
    client = signed_in(admin)
    for path in ("", "/leads", "/dashboard"):
        response = client.get(f"{API}/{segment}{path}")
        assert response.status_code == 404, (segment, path)


def test_an_upper_case_id_is_the_same_workspace(admin, user_b):
    client = signed_in(admin)
    assert client.get(f"{API}/{ws(user_b).upper()}").json()["subject"]["id"] == ws(user_b)


# --- cross-user fixtures: nothing crosses ---------------------------------------------------------
def test_rahuls_workspace_holds_nothing_of_priyas(world):
    for path, body in reads(world, ws(world["rahul"])).items():
        leaked = [t for t in priya_traces(world) if t in body]
        assert leaked == [], (path, leaked)


def test_priyas_workspace_holds_nothing_of_rahuls(world):
    for path, body in reads(world, ws(world["priya"])).items():
        leaked = [t for t in rahul_traces(world) if t in body]
        assert leaked == [], (path, leaked)


def test_rahuls_figures_are_exactly_his(world):
    client = signed_in(world["admin"])
    dashboard = client.get(f"{API}/{ws(world['rahul'])}/dashboard").json()
    assert dashboard["leads"]["total"] == 1
    assert dashboard["pipeline"]["pipeline_value"] == "111111.00"
    assert dashboard["pipeline"]["weighted_pipeline"] == "55555.50"
    assert dashboard["activities"]["open_tasks"] == 1
    assert dashboard["activities"]["upcoming_meetings"] == 1
    summary = client.get(f"{API}/{ws(world['rahul'])}/pipeline-summary").json()
    totals = summary["totals"]
    assert (totals["pipeline_value"], totals["open_count"]) == ("111111.00", 1)


# --- object substitution: Rahul's workspace, Priya's records --------------------------------------
OBJECT_ROUTES = sorted(
    (route, method)
    for route, rule in AUTHZ_MATRIX.items()
    if "<str:workspace>" in route and has_record(route)
    for method in rule.methods
)
BODIES = {
    "PATCH": {"title": "Hacked", "first_name": "Hacked", "description": "Hacked", "version": 1},
    "POST": {"version": 1, "status": "contacted", "stage": "", "owner": "", "lead": ""},
}


def substitute(route: str, workspace: str, records: dict[str, Any]) -> str:
    path = route.replace("<str:workspace>", workspace)
    path = path.replace("<uuid:lead_id>", str(records["lead"].pk))
    path = path.replace("<uuid:opportunity_id>", str(records["opportunity"].pk))
    return "/" + path.replace("<uuid:activity_id>", str(records["task"].pk))


@pytest.mark.parametrize(("route", "method"), OBJECT_ROUTES)
def test_priyas_records_through_rahuls_workspace_are_not_found(world, route, method):
    """Every record route and method: Rahul's URL + Priya's record answers exactly like Rahul's
    URL + a record that exists nowhere, and changes nothing."""
    client = signed_in(world["admin"])
    before = {
        "leads": list(Lead.objects.values_list("id", "version").order_by("id")),
        "opportunities": list(Opportunity.objects.values_list("id", "version").order_by("id")),
        "activities": list(Activity.objects.values_list("id", "version").order_by("id")),
        "audit": AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED).count(),
    }
    rahul = ws(world["rahul"])
    foreign = call(
        client, method, substitute(route, rahul, world["priya_records"]), BODIES.get(method)
    )
    nowhere = call(client, method, concrete(route, rahul), BODIES.get(method))
    if foreign.status_code == 400:
        # Strict input refuses the body first, identically for both: still no oracle.
        assert nowhere.status_code == 400
    else:
        assert foreign.status_code == nowhere.status_code == 404, foreign.content
        assert envelope(foreign) == envelope(nowhere)
    assert not any(t in foreign.content.decode() for t in priya_traces(world))
    assert before == {
        "leads": list(Lead.objects.values_list("id", "version").order_by("id")),
        "opportunities": list(Opportunity.objects.values_list("id", "version").order_by("id")),
        "activities": list(Activity.objects.values_list("id", "version").order_by("id")),
        "audit": AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED).count(),
    }


@pytest.mark.parametrize("kind", ["task", "meeting", "note"])
def test_each_kind_of_priyas_activity_through_rahuls_workspace_is_not_found(world, kind):
    client = signed_in(world["admin"])
    activity = world["priya_records"][kind]
    path = f"{API}/{ws(world['rahul'])}/activities/{activity.pk}"
    assert client.get(path).status_code == 404
    assert client.patch(path, {"title": "x", "version": 1}, format="json").status_code in (400, 404)
    for action in ("complete", "cancel", "reopen", "archive", "restore"):
        response = client.post(f"{path}/{action}", {"version": 1}, format="json")
        assert response.status_code == 404, (kind, action)
    activity.refresh_from_db()
    assert activity.version == 1


def test_linking_new_work_in_rahuls_workspace_to_priyas_records_is_refused(world):
    client = signed_in(world["admin"])
    rahul = ws(world["rahul"])
    priya_lead = world["priya_records"]["lead"]
    priya_opportunity = world["priya_records"]["opportunity"]
    count = (Opportunity.objects.count(), Activity.objects.count())
    responses = [
        client.post(
            f"{API}/{rahul}/opportunities",
            {"lead": str(priya_lead.pk), "title": "x", "value": "1"},
            format="json",
        ),
        client.post(
            f"{API}/{rahul}/activities",
            {"type": "task", "lead": str(priya_lead.pk), "title": "x"},
            format="json",
        ),
        client.post(
            f"{API}/{rahul}/activities",
            {"type": "note", "opportunity": str(priya_opportunity.pk), "description": "x"},
            format="json",
        ),
    ]
    for response in responses:
        assert response.status_code in (400, 404), response.content
        assert not any(t in response.content.decode() for t in priya_traces(world))
    assert (Opportunity.objects.count(), Activity.objects.count()) == count


# --- actor vs subject -----------------------------------------------------------------------------
def test_every_change_in_rahuls_workspace_is_anitas_and_none_is_rahuls(world):
    anita, rahul = world["admin"], world["rahul"]
    client = signed_in(anita)
    base = f"{API}/{ws(rahul)}"

    lead = client.post(f"{base}/leads", {"first_name": "Asha"}, format="json").json()
    assert (lead["owner"]["id"], lead["created_by"]["id"]) == (ws(rahul), str(anita.pk))
    edited = client.patch(
        f"{base}/leads/{lead['id']}", {"city": "Pune", "version": lead["version"]}, format="json"
    )
    assert edited.status_code == 200, edited.content

    opportunity = client.post(
        f"{base}/opportunities",
        {"lead": lead["id"], "title": "Lab", "value": "111111.00"},
        format="json",
    ).json()
    assert opportunity["owner"]["id"] == ws(rahul)
    assert opportunity["created_by"]["id"] == str(anita.pk)
    moved = client.post(
        f"{base}/opportunities/{opportunity['id']}/move",
        {"stage": str(default_stage("negotiation").pk), "version": opportunity["version"]},
        format="json",
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["value"] == "111111.00"  # exact money, untouched by the move
    assert set(
        StageHistory.objects.filter(opportunity_id=opportunity["id"]).values_list(
            "actor_id", flat=True
        )
    ) == {anita.pk}

    task = client.post(
        f"{base}/activities",
        {"type": "task", "lead": lead["id"], "title": "Call back"},
        format="json",
    ).json()
    assert (task["owner"]["id"], task["created_by"]["id"]) == (ws(rahul), str(anita.pk))
    completed = client.post(
        f"{base}/activities/{task['id']}/complete", {"version": task["version"]}, format="json"
    ).json()
    assert completed["completed_by"]["id"] == str(anita.pk)
    assert completed["owner"]["id"] == ws(rahul)

    start = timezone.now() - timedelta(hours=2)
    meeting = client.post(
        f"{base}/activities",
        {
            "type": "meeting",
            "lead": lead["id"],
            "title": "Demo",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
        },
        format="json",
    ).json()
    held = client.post(
        f"{base}/activities/{meeting['id']}/complete",
        {"version": meeting["version"]},
        format="json",
    ).json()
    assert (held["completed_by"]["id"], held["owner"]["id"]) == (str(anita.pk), ws(rahul))
    assert Lead.objects.get(pk=lead["id"]).last_contacted_at is not None  # Phase 4 service

    note = client.post(
        f"{base}/activities",
        {"type": "note", "lead": lead["id"], "description": "SECRET-NOTE-BODY prefers mornings"},
        format="json",
    ).json()
    assert (note["created_by"]["id"], note["owner"]["id"]) == (str(anita.pk), ws(rahul))

    writes = AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED)
    actions = set(writes.values_list("action", flat=True))
    assert {
        "lead.created",
        "lead.updated",
        "opportunity.created",
        "opportunity.stage_changed",
        "task.created",
        "task.completed",
        "meeting.created",
        "meeting.completed",
        "note.created",
    } <= actions
    # Anita did all of it, in Rahul's workspace; nothing claims Rahul acted.
    assert set(writes.values_list("actor_id", flat=True)) == {anita.pk}
    assert set(writes.values_list("subject_user_id", flat=True)) == {rahul.pk}
    assert not AuditEvent.objects.filter(actor_id=rahul.pk).exists()
    # The note's text never reaches the audit trail.
    assert "SECRET-NOTE-BODY" not in json.dumps(list(AuditEvent.objects.values("metadata")))


@pytest.mark.parametrize(
    "extra",
    [
        {"owner": "PRIYA"},
        {"created_by": "RAHUL"},
        {"workspace": "all"},
        {"user_id": "RAHUL"},
        {"actor": "RAHUL"},
        {"role": "admin"},
        {"capabilities": ["*"]},
        {"completed_by": "RAHUL"},
        {"version": 99},
        {"id": MISSING_RECORD},
    ],
)
def test_no_payload_field_moves_a_write_out_of_the_url_workspace(world, extra):
    """The workspace comes from the URL; the body can't name an owner, a creator, a
    workspace or an actor (strict input refuses unknown and read-only keys)."""
    ids = {"PRIYA": ws(world["priya"]), "RAHUL": ws(world["rahul"])}
    extra = {k: ids.get(v, v) if isinstance(v, str) else v for k, v in extra.items()}
    client = signed_in(world["admin"])
    base = f"{API}/{ws(world['rahul'])}"
    lead = world["rahul_records"]["lead"]
    counts = (Lead.objects.count(), Activity.objects.count(), Opportunity.objects.count())
    for path, body in [
        ("/leads", {"first_name": "X"}),
        ("/activities", {"type": "task", "lead": str(lead.pk), "title": "X"}),
        ("/opportunities", {"lead": str(lead.pk), "title": "X", "value": "1"}),
    ]:
        response = client.post(base + path, {**body, **extra}, format="json")
        if response.status_code == 201:
            # Only "owner" naming Rahul himself is legitimate in his workspace: never Priya.
            assert response.json()["owner"]["id"] == ws(world["rahul"])
            assert response.json()["created_by"]["id"] == str(world["admin"].pk)
        else:
            assert response.status_code == 400, (path, extra, response.content)
    if "owner" in extra:
        assert (
            not Lead.objects.filter(owner=world["priya"])
            .exclude(pk=world["priya_records"]["lead"].pk)
            .exists()
        )
    assert (Lead.objects.count(), Activity.objects.count(), Opportunity.objects.count()) == counts


def test_after_a_reassignment_the_admin_is_told_where_they_can_act_not_to_ask_an_admin(
    admin, user_a, user_b
):
    """Rahul keeps his closed work when his lead moves to Priya; reopening it (or adding work
    to his won deal) would give Priya current work, which Rahul's workspace can't do. The
    administrator in Rahul's workspace is told to use the organisation-wide view, and can
    there; a sales user is still told to ask an administrator (Phase 6 review, P3)."""
    lead = LeadFactory(owner=user_a)
    done = TaskFactory(lead=lead, status="completed")
    won = OpportunityFactory(lead=lead, stage_key="won")
    client = signed_in(admin)
    rahul = f"{API}/{ws(user_a)}"
    assigned = client.post(
        f"{rahul}/leads/{lead.pk}/assign", {"owner": ws(user_b), "version": 1}, format="json"
    )
    assert assigned.status_code == 200, assigned.content
    proposal = str(default_stage("proposal").pk)
    attempts = {
        "reopen task": client.post(
            f"{rahul}/activities/{done.pk}/reopen", {"version": 1}, format="json"
        ),
        "reopen deal": client.post(
            f"{rahul}/opportunities/{won.pk}/move",
            {"stage": proposal, "version": 1},
            format="json",
        ),
        "task on the deal": client.post(
            f"{rahul}/activities",
            {"type": "task", "opportunity": str(won.pk), "title": "x"},
            format="json",
        ),
    }
    for name, response in attempts.items():
        assert response.status_code == 422, (name, response.content)
        message = response.json()["error"]["message"]
        assert "belongs to someone else" in message, name
        assert "organisation-wide" in message, name
        assert "Ask an administrator" not in message, name
    # Where it points to, it works.
    everyone = f"{API}/all"
    reopened = client.post(f"{everyone}/activities/{done.pk}/reopen", {"version": 1}, format="json")
    assert reopened.status_code == 200, reopened.content
    assert reopened.json()["owner"]["id"] == ws(user_b)
    moved = client.post(
        f"{everyone}/opportunities/{won.pk}/move", {"stage": proposal, "version": 1}, format="json"
    )
    assert moved.status_code == 200, moved.content
    # In one's own workspace the advice is unchanged.
    kept = TaskFactory(lead=LeadFactory(owner=user_a), status="completed")
    signed_in(admin).post(
        f"{API}/all/leads/{kept.lead_id}/assign", {"owner": ws(user_b), "version": 1}, format="json"
    )
    own = signed_in(user_a).post(
        f"{API}/me/activities/{kept.pk}/reopen", {"version": 1}, format="json"
    )
    assert own.status_code == 422
    assert "Ask an administrator" in own.json()["error"]["message"]


# --- deactivated users ----------------------------------------------------------------------------
def test_a_deactivated_users_history_stays_readable_and_receives_no_new_work(world):
    """Anita has Rahul's workspace open; another administrator deactivates Rahul; Anita's next
    writes are refused cleanly (nothing partial), and her reads still work."""
    anita, rahul = world["admin"], world["rahul"]
    client = signed_in(anita)
    base = f"{API}/{ws(rahul)}"
    assert client.get(base).json()["subject"]["status"] == "active"

    other_admin = AdminFactory()
    identity_services.deactivate_user(actor_id=other_admin.pk, user_id=rahul.pk)

    lead = world["rahul_records"]["lead"]
    before = (
        Lead.objects.count(),
        Opportunity.objects.count(),
        Activity.objects.count(),
        AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED).count(),
    )
    start = timezone.now() + timedelta(days=1)
    attempts = {
        "lead": client.post(f"{base}/leads", {"first_name": "New"}, format="json"),
        "opportunity": client.post(
            f"{base}/opportunities",
            {"lead": str(lead.pk), "title": "New", "value": "1"},
            format="json",
        ),
        "task": client.post(
            f"{base}/activities",
            {"type": "task", "lead": str(lead.pk), "title": "New"},
            format="json",
        ),
        "meeting": client.post(
            f"{base}/activities",
            {
                "type": "meeting",
                "lead": str(lead.pk),
                "title": "New",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
            },
            format="json",
        ),
    }
    assert attempts["lead"].status_code == 400
    assert attempts["lead"].json()["error"]["details"] == {"owner": [SUBJECT_NOT_ASSIGNABLE]}
    for kind in ("opportunity", "task", "meeting"):
        assert attempts[kind].status_code == 422, (kind, attempts[kind].content)
        assert "deactivated" in attempts[kind].json()["error"]["message"]
    assert before == (
        Lead.objects.count(),
        Opportunity.objects.count(),
        Activity.objects.count(),
        AuditEvent.objects.exclude(action=AUDIT_ACTION_WORKSPACE_ACCESSED).count(),
    )

    # History stays readable, with its status shown; nothing was reassigned or reactivated.
    assert client.get(base).json()["subject"]["status"] == "deactivated"
    assert client.get(f"{base}/leads").json()["results"][0]["id"] == str(lead.pk)
    assert client.get(f"{base}/dashboard").json()["pipeline"]["pipeline_value"] == "111111.00"
    rahul.refresh_from_db()
    assert (rahul.status, rahul.is_active) == ("deactivated", False)
    assert Lead.objects.get(pk=lead.pk).owner_id == rahul.pk


# --- delegated-access audit: one row per user per window, not per request -------------------------
def test_a_whole_workspace_visit_is_audited_once_per_user_per_window(
    world, django_capture_on_commit_callbacks
):
    anita = world["admin"]
    accessed = AuditEvent.objects.filter(action=AUDIT_ACTION_WORKSPACE_ACCESSED, actor_id=anita.pk)

    def committed():
        return django_capture_on_commit_callbacks(execute=True)

    for workspace in (ws(world["rahul"]), ws(world["priya"]), ws(world["rahul"])):
        reads(world, workspace, committed)  # 21 requests across all four modules each time
    assert accessed.count() == 2
    assert set(accessed.values_list("subject_user_id", flat=True)) == {
        world["rahul"].pk,
        world["priya"].pk,
    }
    assert settings.WORKSPACE_ACCESS_AUDIT_WINDOW_S == 15 * 60


# --- no impersonation -----------------------------------------------------------------------------
def test_no_impersonation_the_session_stays_the_admins_throughout(world):
    anita = world["admin"]
    client = signed_in(anita)
    session_key = client.session.session_key
    reads(world, ws(world["rahul"]))
    client.post(f"{API}/{ws(world['rahul'])}/leads", {"first_name": "Asha"}, format="json")
    me = client.get("/api/v1/auth/me").json()
    assert (me["id"], me["email"]) == (str(anita.pk), anita.email)
    assert client.session.session_key == session_key
    assert str(client.session["_auth_user_id"]) == str(anita.pk)


def test_there_is_no_route_to_act_as_another_user():
    patterns = []

    def walk(resolver, prefix=""):
        for entry in resolver.url_patterns:
            if hasattr(entry, "url_patterns"):
                walk(entry, prefix + str(entry.pattern))
            else:
                patterns.append(prefix + str(entry.pattern))

    walk(get_resolver())
    risky = re.compile(r"impersonat|login[-_]?as|become|switch[-_]?user|sudo|act[-_]?as", re.I)
    assert [p for p in patterns if risky.search(p)] == []


# --- query counts ---------------------------------------------------------------------------------
# Each request of an admin's visit to Rahul's workspace (the audit window already open, as for
# every request after the first): session + user (2), the workspace check (1), then the
# module's own reads: the same as in the user's own workspace plus that one check.
EXPECTED_QUERIES = {
    "": 4,  # + the subject's id, name and status
    "/dashboard": 10,  # + the read-only snapshot and its six figures and three lists
    "/leads": 4,  # + one keyset page
    "/pipeline-board": 8,  # + pipeline, stages, totals, columns, cards
    "/activities": 4,  # + one keyset page
    "/activity-summary": 5,  # + the counts
}


def test_selected_workspace_requests_cost_a_bounded_constant(
    world, django_capture_on_commit_callbacks
):
    client = signed_in(world["admin"])
    base = f"{API}/{ws(world['rahul'])}"
    with django_capture_on_commit_callbacks(execute=True):
        client.get(base)  # opens the audit window
    counts = {}
    for path in EXPECTED_QUERIES:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(base + path).status_code == 200
        counts[path] = len(queries)
    assert counts == EXPECTED_QUERIES
    # More records never cost more queries.
    for _ in range(5):
        lead = LeadFactory(owner=world["rahul"])
        OpportunityFactory(lead=lead)
        TaskFactory(lead=lead)
    for path, expected in EXPECTED_QUERIES.items():
        with CaptureQueriesContext(connection) as queries:
            client.get(base + path)
        assert len(queries) == expected, path


def test_the_users_list_stays_constant_with_workspace_links(admin):
    client = signed_in(admin)
    for _ in range(8):
        TaskFactory(lead=LeadFactory())  # users who own CRM records
    AdminFactory()
    with CaptureQueriesContext(connection) as queries:
        response = client.get("/api/v1/admin/users")
    assert response.status_code == 200
    assert len(queries) == 3
    # The list carries what the name link needs (id, name) and no per-user CRM figures.
    row = response.json()["results"][0]
    assert {"id", "full_name"} <= set(row)
    assert not {"leads", "pipeline_value", "open_tasks"} & set(row)


def test_the_workspace_description_carries_only_id_name_and_status(admin, user_b):
    body = signed_in(admin).get(f"{API}/{ws(user_b)}").json()
    assert body == {
        "kind": "user",
        "subject": {"id": ws(user_b), "full_name": "Priya Patel", "status": "active"},
    }


def test_the_uuid_type_is_what_the_segment_check_accepts():
    """Guard for RANDOM_USER above: it must be a well-formed id that names nobody."""
    assert str(uuid.UUID(RANDOM_USER)) == RANDOM_USER
