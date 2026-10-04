"""The tools: exact figures from the owning modules, scope-bound, and every argument the
model sends treated as untrusted input."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from django.utils import timezone

from arkray.ai import tools
from arkray.ai.tools import ToolContext
from arkray.core.access import AccessScope
from tests.ai_fixtures import index, note, own
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, TaskFactory

pytestmark = pytest.mark.django_db


def run(scope: AccessScope, name: str, args: dict[str, Any] | Any) -> tuple[Any, ToolContext]:
    ctx = ToolContext(scope=scope, now=timezone.now())
    outcome = tools.execute(ctx, name, args)
    return (json.loads(outcome.content), outcome.is_error), ctx


def ok(scope: AccessScope, name: str, args: dict[str, Any] | None = None) -> Any:
    (body, is_error), _ = run(scope, name, args or {})
    assert not is_error, body
    return body


class TestGoldenFigures:
    """docs/rag-architecture.md#testing: exact, not approximate."""

    def test_pipeline(self, user_a, golden):
        body = ok(own(user_a), "get_pipeline_summary")
        assert body["pipeline_value"] == {
            "amount": "1000000.00",
            "currency": "INR",
            "display": "₹10,00,000",
        }
        assert body["weighted_pipeline"]["display"] == "₹5,00,000"
        assert body["open_opportunities"] == 2

    def test_leads_tasks_and_meetings(self, user_a, golden):
        assert ok(own(user_a), "get_lead_summary")["total_leads"] == 10
        summary = ok(own(user_a), "get_activity_summary")
        assert (summary["overdue_tasks"], summary["meetings_today"]) == (3, 2)
        assert ok(own(user_a), "list_tasks", {"filter": "overdue"})["total_matching"] == 3
        assert ok(own(user_a), "list_meetings", {"range": "today"})["total_matching"] == 2

    def test_facts_are_registered_by_the_server(self, user_a, golden):
        _, ctx = run(own(user_a), "get_pipeline_summary", {})
        assert [(f.label, f.value, f.raw) for f in ctx.facts] == [
            ("Pipeline value", "₹10,00,000", "1000000.00"),
            ("Weighted pipeline", "₹5,00,000", "500000.00"),
            ("Open opportunities", "2", "2"),
        ]

    def test_another_user_sees_none_of_it(self, user_b, golden):
        assert ok(own(user_b), "get_pipeline_summary")["open_opportunities"] == 0
        assert ok(own(user_b), "get_lead_summary")["total_leads"] == 0
        assert ok(own(user_b), "list_tasks", {"filter": "overdue"})["tasks"] == []

    def test_opportunities_in_a_stage_by_name(self, user_a, golden):
        body = ok(own(user_a), "list_opportunities", {"stage": "new"})
        assert body["total_matching"] == 2
        assert {row["value"]["display"] for row in body["opportunities"]} == {"₹5,00,000"}
        (error, is_error), _ = run(own(user_a), "list_opportunities", {"stage": "Nonexistent"})
        assert is_error
        assert "Stages:" in error["error"]


class TestNoContactData:
    def test_lead_rows_never_carry_email_or_phones(self, user_a):
        LeadFactory(owner=user_a, email="priv@example.com", phone="+919876543210")
        body = json.dumps(ok(own(user_a), "list_leads", {}))
        assert "priv@example.com" not in body
        assert "9876543210" not in body


class TestUntrustedArguments:
    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("list_leads", {"owner_id": "anything"}),  # no such parameter, ever
            ("list_leads", {"limit": 10_000}),
            ("list_leads", {"limit": 0}),
            ("list_leads", {"limit": True}),
            ("list_leads", {"sort": "password"}),
            ("list_leads", {"created": "1970"}),
            ("list_tasks", {}),
            ("list_tasks", {"filter": "everyone's"}),
            ("list_meetings", {"range": "all_time"}),
            ("get_record", {"ref": "user:" + str(uuid4())}),
            ("get_record", {"ref": "lead:not-a-uuid"}),
            ("get_record", {"ref": "lead:" + str(uuid4()), "workspace": "all"}),
            ("find_records", {"query": "a"}),
            ("find_records", {"query": "x" * 500}),
            ("search_notes", {"query": "q", "about": "task:" + str(uuid4())}),
            ("get_pipeline_summary", {"scope": "organization"}),
            ("execute_sql", {"sql": "SELECT * FROM identity_user"}),
            ("team_breakdown", {"metric": "pipeline"}),  # absent outside organisation scope
        ],
    )
    def test_refused_as_an_error_result(self, user_a, name, args):
        (body, is_error), _ = run(own(user_a), name, args)
        assert is_error
        assert set(body) == {"error"}

    def test_non_object_arguments_are_refused(self, user_a):
        (_body, is_error), _ = run(own(user_a), "list_leads", ["limit", 5])
        assert is_error

    def test_another_users_record_is_not_found_like_a_missing_one(self, user_a, user_b):
        priya_lead = LeadFactory(owner=user_b)
        priya_note = NoteFactory(lead=priya_lead, description="Private.")
        for ref in (f"lead:{priya_lead.pk}", f"note:{priya_note.pk}", f"lead:{uuid4()}"):
            (body, is_error), ctx = run(own(user_a), "get_record", {"ref": ref})
            assert is_error
            assert body == {"error": "No such record in this workspace."}
            assert ctx.records == {}
        (body, is_error), _ = run(
            own(user_a), "search_notes", {"query": "x", "about": f"lead:{priya_lead.pk}"}
        )
        assert is_error
        assert body == {"error": "No such record in this workspace."}

    def test_a_task_id_asked_for_as_a_note_is_not_found(self, user_a):
        task = TaskFactory(lead=LeadFactory(owner=user_a), description="x")
        (_, is_error), _ = run(own(user_a), "get_record", {"ref": f"note:{task.pk}"})
        assert is_error


class TestScopeBinding:
    def test_no_tool_offers_an_owner_or_workspace_parameter(self, admin):
        for scope in (own(admin), AccessScope.organization(admin.pk)):
            for definition in tools.definitions(scope):
                names = set(definition["input_schema"]["properties"])
                assert not names & {"owner", "owner_id", "user", "user_id", "workspace", "scope"}
                assert definition["strict"] is True
                assert definition["input_schema"]["additionalProperties"] is False

    def test_organisation_tools_exist_only_in_organisation_scope(self, admin, user_a):
        names = lambda scope: [d["name"] for d in tools.definitions(scope)]  # noqa: E731
        assert "team_breakdown" not in names(own(user_a))
        assert "team_breakdown" not in names(AccessScope.for_user(admin.pk, user_a.pk))
        assert "team_breakdown" in names(AccessScope.organization(admin.pk))

    def test_strict_schema_limits_hold(self, admin):
        definitions = tools.definitions(AccessScope.organization(admin.pk))
        optional = sum(
            len(set(d["input_schema"]["properties"]) - set(d["input_schema"]["required"]))
            for d in definitions
        )
        assert len(definitions) <= 20
        assert optional <= 24

    def test_team_breakdown_names_people_organisation_wide(self, admin, user_a, user_b, golden):
        body = ok(AccessScope.organization(admin.pk), "team_breakdown", {"metric": "pipeline"})
        assert body["users"][0]["user"] == user_a.full_name
        assert body["users"][0]["pipeline_value"]["display"] == "₹10,00,000"


class TestRecordsAndNotes:
    def test_get_record_returns_details_and_recent_activities(self, user_a):
        lead = LeadFactory(owner=user_a, description="Owns three clinics.")
        OpportunityFactory(lead=lead, title="Clinic analyzers")
        NoteFactory(lead=lead, description="Wants a quote by Friday.")
        body = ok(own(user_a), "get_record", {"ref": f"lead:{lead.pk}"})
        assert body["untrusted_text"]["text"] == "Owns three clinics."
        assert [o["title"] for o in body["opportunities"]] == ["Clinic analyzers"]
        assert body["recent_activities"][0]["untrusted_text"]["text"] == "Wants a quote by Friday."

    def test_search_notes_wraps_text_as_untrusted_and_cites(self, user_a):
        lead = LeadFactory(owner=user_a)
        created = note(user_a, lead, "The customer worried about the analyser price.")
        index()
        (body,) = [ok(own(user_a), "search_notes", {"query": "analyser price worry"})]
        (passage,) = body["passages"]
        assert passage["ref"] == f"note:{created.pk}"
        assert passage["untrusted_text"] == "Text: The customer worried about the analyser price."
        assert passage["lead"]["ref"] == f"lead:{lead.pk}"
