"""Phase 6: an administrator writing in Rahul's workspace while another administrator
deactivates Rahul, run for real (threads, separate connections; docs/admin-user-workspace.md).

Every write that would give Rahul new current work (a lead, an opportunity, a conversion, a
task, a meeting, a note, reopening a task or an opportunity) and the deactivation are ordered
by the lock on Rahul's user row (`lock_assignable_user`'s FOR SHARE against the
deactivation's FOR UPDATE): either the write commits first and the deactivation waits, or the
deactivation commits first and the write is refused with nothing written. Never a record
given to Rahul after his deactivation, never a partial write, never a deadlock.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from arkray.activities import events as activity_events
from arkray.activities.models import Activity, TimelineEntry
from arkray.audit.models import AuditEvent
from arkray.core.domain_events import subscribed
from arkray.identity import services as identity_services
from arkray.leads.events import LeadCreated
from arkray.leads.models import Lead
from arkray.pipeline import events as pipeline_events
from arkray.pipeline.models import Opportunity, StageHistory
from tests.factories import (
    AdminFactory,
    LeadFactory,
    OpportunityFactory,
    TaskFactory,
    UserFactory,
    default_stage,
)
from tests.helpers import run_concurrently, signed_in

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("crm_configuration"),
]

API = "/api/v1/workspaces"
WRITES = [
    "lead",
    "opportunity",
    "convert",
    "task",
    "meeting",
    "note",
    "reopen_task",
    "reopen_opportunity",
]


def counts() -> dict[str, int]:
    return {
        "leads": Lead.objects.count(),
        "opportunities": Opportunity.objects.count(),
        "activities": Activity.objects.count(),
        "timeline": TimelineEntry.objects.count(),
        "history": StageHistory.objects.count(),
        "audit": AuditEvent.objects.exclude(
            action__in=["workspace.accessed", "user.deactivated"]
        ).count(),
    }


def world() -> dict[str, Any]:
    rahul = UserFactory()
    lead = LeadFactory(owner=rahul)
    return {
        "anita": AdminFactory(),
        "bina": AdminFactory(),
        "rahul": rahul,
        "lead": lead,
        "to_convert": LeadFactory(owner=rahul),
        "done": TaskFactory(lead=lead, status="completed"),
        "won": OpportunityFactory(lead=lead, stage_key="won"),
    }


def writes(w: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any], type]]:
    """Each write in Rahul's workspace: (path, body, the domain event it emits in-transaction)."""
    base = f"{API}/{w['rahul'].pk}"
    lead = str(w["lead"].pk)
    start = timezone.now() + timedelta(days=1)
    return {
        "lead": (f"{base}/leads", {"first_name": "Race"}, LeadCreated),
        "opportunity": (
            f"{base}/opportunities",
            {"lead": lead, "value": "1"},
            pipeline_events.OpportunityCreated,
        ),
        "convert": (
            f"{base}/leads/{w['to_convert'].pk}/convert",
            {"version": w["to_convert"].version, "value": "1"},
            pipeline_events.OpportunityCreated,
        ),
        "task": (
            f"{base}/activities",
            {"type": "task", "lead": lead, "title": "Race"},
            activity_events.ActivityCreated,
        ),
        "meeting": (
            f"{base}/activities",
            {
                "type": "meeting",
                "lead": lead,
                "title": "Race",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
            },
            activity_events.ActivityCreated,
        ),
        "note": (
            f"{base}/activities",
            {"type": "note", "lead": lead, "description": "Race"},
            activity_events.ActivityCreated,
        ),
        "reopen_task": (
            f"{base}/activities/{w['done'].pk}/reopen",
            {"version": w["done"].version},
            activity_events.ActivityStatusChanged,
        ),
        "reopen_opportunity": (
            f"{base}/opportunities/{w['won'].pk}/move",
            {"stage": str(default_stage("proposal").pk), "version": w["won"].version},
            pipeline_events.OpportunityStageChanged,
        ),
    }


def nothing_given_to_rahul_after_his_deactivation(rahul) -> None:
    rahul.refresh_from_db()
    assert rahul.status == "deactivated"
    for model in (Lead, Opportunity, Activity):
        for row in model.objects.filter(owner=rahul).values("id", "updated_at"):
            assert row["updated_at"] <= rahul.deactivated_at, (model.__name__, row)


@pytest.mark.parametrize("write", WRITES)
def test_deactivation_first_the_write_waits_then_is_refused_with_nothing_written(
    write, monkeypatch
):
    w = world()
    path, body, _ = writes(w)[write]
    anita, bina = signed_in(w["anita"]), signed_in(w["bina"])
    anita.get(f"{API}/{w['rahul'].pk}")  # Anita has Rahul's workspace open
    before = counts()
    locked = threading.Event()
    finished: dict[str, float] = {}
    record = identity_services._audit_user

    def slow_audit(*args: Any, **kwargs: Any) -> None:
        record(*args, **kwargs)
        finished["locked"] = time.monotonic()
        locked.set()
        time.sleep(0.8)  # the deactivation holds Rahul's row FOR UPDATE meanwhile

    monkeypatch.setattr(identity_services, "_audit_user", slow_audit)

    def deactivate():
        response = bina.post(f"/api/v1/admin/users/{w['rahul'].pk}/deactivate", format="json")
        finished["deactivate"] = time.monotonic()
        return response

    def write_now():
        assert locked.wait(10)
        response = anita.post(path, body, format="json")
        finished["write"] = time.monotonic()
        return response

    deactivated, written = run_concurrently(deactivate, write_now)
    assert deactivated.status_code == 200, deactivated.content
    assert written.status_code in (400, 422), written.content
    # It waited for the deactivation to commit (the lock was held 0.8 s after it started).
    assert finished["write"] - finished["locked"] >= 0.7
    assert counts() == before
    nothing_given_to_rahul_after_his_deactivation(w["rahul"])


@pytest.mark.parametrize("write", WRITES)
def test_write_first_the_deactivation_waits_for_it(write):
    w = world()
    path, body, event = writes(w)[write]
    anita, bina = signed_in(w["anita"]), signed_in(w["bina"])
    anita.get(f"{API}/{w['rahul'].pk}")
    locked = threading.Event()

    def slow(_event: object) -> None:
        locked.set()
        time.sleep(0.8)  # inside the write's transaction, holding Rahul's row FOR SHARE

    def write_now():
        with subscribed(event, slow):
            return anita.post(path, body, format="json")

    def deactivate():
        assert locked.wait(10)
        return bina.post(f"/api/v1/admin/users/{w['rahul'].pk}/deactivate", format="json")

    written, deactivated = run_concurrently(write_now, deactivate)
    assert written.status_code in (200, 201), written.content
    assert deactivated.status_code == 200, deactivated.content
    nothing_given_to_rahul_after_his_deactivation(w["rahul"])


@pytest.mark.parametrize("seed", range(4))
def test_a_burst_of_every_write_around_a_deactivation(seed):
    w = world()
    ops = writes(w)
    bina = signed_in(w["bina"])
    signed_in(w["anita"]).get(f"{API}/{w['rahul'].pk}")
    rng = random.Random(seed)

    def writer(name: str):
        path, body, _ = ops[name]

        def call():
            time.sleep(rng.random() * 0.05)
            return name, signed_in(w["anita"]).post(path, body, format="json")

        return call

    def deactivate():
        time.sleep(rng.random() * 0.05)
        return "deactivate", bina.post(
            f"/api/v1/admin/users/{w['rahul'].pk}/deactivate", format="json"
        )

    calls = [writer(name) for name in WRITES] + [deactivate]
    rng.shuffle(calls)
    for result in run_concurrently(*calls):
        assert not isinstance(result, BaseException), result
        name, response = result
        assert response.status_code in (200, 201, 400, 409, 422), (name, response.content)
        assert "deadlock" not in response.content.decode().lower()
    nothing_given_to_rahul_after_his_deactivation(w["rahul"])
    # What was written was written whole: its timeline, stage history and audit with it.
    for activity in Activity.objects.filter(created_by=w["anita"]):
        assert TimelineEntry.objects.filter(activity_id=activity.pk).exists()
        assert AuditEvent.objects.filter(target_id=str(activity.pk)).exists()
    for opportunity in Opportunity.objects.filter(created_by=w["anita"]):
        assert StageHistory.objects.filter(opportunity=opportunity).exists()
