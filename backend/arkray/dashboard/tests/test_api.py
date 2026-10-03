"""GET /api/v1/workspaces/{workspace}/dashboard: workspace resolution (the existing rules,
nothing dashboard-specific), strict input, no caching, audit of delegated viewing (once per
window, not per refresh) and no CRM data in the logs."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from tests.factories import LeadFactory, OpportunityFactory, TaskFactory, UserFactory
from tests.helpers import signed_in, without_request_id

from .conftest import dashboard_url

pytestmark = pytest.mark.django_db


def test_signed_out_is_401():
    response = APIClient().get(dashboard_url())
    assert response.status_code == 401
    assert response["WWW-Authenticate"].startswith("Session")


def test_own_dashboard(user_a_client):
    response = user_a_client.get(dashboard_url())
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]  # figures are personal data


@pytest.mark.parametrize(
    "workspace",
    [
        "all",
        "ALL",
        "Me",
        "everyone",
        str(uuid.uuid4()),
        "00000000-0000-0000-0000-000000000000",
        "urn:uuid:{other}",
        "{{{other}}}",
        "{other_hex}",
        "{other} ",
        "{other_upper}",
    ],
)
def test_a_sales_user_opens_no_other_workspace(user_a_client, user_b, workspace):
    """Another user's dashboard, the organisation's, and any odd spelling of an id: the same
    404 as a user id that doesn't exist, so nothing can be learnt from it."""
    other = str(user_b.pk)
    workspace = workspace.format(other=other, other_hex=user_b.pk.hex, other_upper=other.upper())
    response = user_a_client.get(dashboard_url(workspace))
    missing = user_a_client.get(dashboard_url(str(uuid.uuid4())))
    assert response.status_code == 404
    assert without_request_id(response) == without_request_id(missing)


def test_another_users_id_is_indistinguishable_from_a_missing_one(user_a_client, user_b):
    LeadFactory(owner=user_b)
    theirs = user_a_client.get(dashboard_url(str(user_b.pk)))
    missing = user_a_client.get(dashboard_url(str(uuid.uuid4())))
    assert theirs.status_code == missing.status_code == 404
    assert without_request_id(theirs) == without_request_id(missing)


def test_an_admin_opens_the_organisation_and_any_users_dashboard(admin_client, user_a):
    LeadFactory(owner=user_a)
    assert admin_client.get(dashboard_url("all")).json()["leads"]["total"] == 1
    assert admin_client.get(dashboard_url(str(user_a.pk))).json()["leads"]["total"] == 1
    assert admin_client.get(dashboard_url()).json()["leads"]["total"] == 0  # their own
    assert admin_client.get(dashboard_url(str(uuid.uuid4()))).status_code == 404


def test_a_deactivated_users_history_stays_viewable(admin_client):
    former = UserFactory(is_active=False)
    LeadFactory(owner=former)
    body = admin_client.get(dashboard_url(str(former.pk))).json()
    assert body["leads"]["total"] == 1


def test_the_admins_own_id_is_their_own_workspace(admin_client, admin):
    LeadFactory(owner=admin)
    assert admin_client.get(dashboard_url(str(admin.pk))).json()["leads"]["total"] == 1
    assert not AuditEvent.objects.filter(action="workspace.accessed").exists()


def test_query_parameters_are_refused(user_a_client):
    """Nothing is configurable: a parameter is a mistake or a probe, never silently ignored."""
    for params in ({"owner": "x"}, {"limit": "500"}, {"scope": "all"}, {"_": "1"}):
        response = user_a_client.get(dashboard_url(), params)
        assert response.status_code == 400, params


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_read_only(user_a_client, method):
    response = getattr(user_a_client, method)(dashboard_url(), {}, format="json")
    assert response.status_code == 405


def test_refreshing_never_floods_the_audit_trail(
    admin_client, admin, user_a, user_a_client, django_capture_on_commit_callbacks
):
    """Viewing your own dashboard writes nothing; an administrator viewing someone's (or the
    organisation's) is audited by workspace resolution once per window, however often the
    page reloads (docs/authorization.md). Each request commits, as it would in production,
    which is what records the window."""
    for _ in range(5):
        assert user_a_client.get(dashboard_url()).status_code == 200
    assert not AuditEvent.objects.exists()

    for _ in range(5):
        for workspace in (str(user_a.pk), "all"):
            with django_capture_on_commit_callbacks(execute=True):
                assert admin_client.get(dashboard_url(workspace)).status_code == 200
    accessed = AuditEvent.objects.filter(action="workspace.accessed", actor_id=admin.pk)
    assert sorted(accessed.values_list("target_id", flat=True)) == sorted([str(user_a.pk), "all"])
    assert accessed.get(target_id=str(user_a.pk)).subject_user_id == user_a.pk
    assert AuditEvent.objects.count() == 2  # nothing else: viewing is not a write


def test_no_crm_data_reaches_the_logs(user_a, caplog):
    """The access log records the request (path, status, timing, ids), never the figures,
    names or titles it returned."""
    lead = LeadFactory(owner=user_a, first_name="Zebulon", last_name="Quixote")
    OpportunityFactory(lead=lead, stage_key="proposal", value=Decimal("4242424.24"))
    TaskFactory(lead=lead, title="Fax the confidential quotation", due_at=timezone.now())
    caplog.set_level(logging.DEBUG)
    response = signed_in(user_a).get(dashboard_url())
    assert response.status_code == 200
    assert "Zebulon" in response.content.decode()  # it was in the response...
    logged = json.dumps(
        [{**record.__dict__, "msg": record.getMessage()} for record in caplog.records],
        default=str,
    )
    assert "http_request" in logged  # ...the request was logged...
    for secret in ("Zebulon", "Quixote", "4242424", "2121212", "confidential"):
        assert secret not in logged  # ...but none of what it returned


def test_the_clock_is_read_once_per_request(user_a, monkeypatch):
    """Figures, lists and every row's "overdue" flag use one `now`: a task can't be counted
    as not overdue and shown as overdue in the same response."""
    from arkray.dashboard.api import views

    calls = []
    real_now = timezone.now

    class Clock:
        @staticmethod
        def now():
            calls.append(1)
            return real_now()

    TaskFactory(lead=LeadFactory(owner=user_a), due_at=real_now() - timedelta(seconds=1))
    monkeypatch.setattr(views, "timezone", Clock)
    body = signed_in(user_a).get(dashboard_url()).json()
    assert len(calls) == 1
    assert body["activities"]["overdue_tasks"] == 1
    assert body["next_tasks"][0]["is_overdue"] is True
