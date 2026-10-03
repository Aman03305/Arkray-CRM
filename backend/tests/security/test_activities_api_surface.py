"""The activities API's attack surface, route by route: for a sales user every route is a 404
under any workspace segment but `me` (another user's id however it is spelt, `all`, a
deactivated or unknown user, `Me`), no system field is writable, lifecycle actions take
exactly a version, malformed bodies, values and query parameters are refused (never a 500),
meeting links are https only, cursors carry no record text and are refused where they were
not issued, and list rows carry previews, never the whole text.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.core import signing
from django.utils import timezone

from arkray.activities.models import Activity
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

ME = "/api/v1/workspaces/me"
ACTIONS = ["complete", "cancel", "reopen", "archive", "restore"]


def every_route(activity, lead, opportunity):
    """Each activities route as (method, path below the workspace, body)."""
    one = f"activities/{activity.pk}"
    return [
        ("get", "activities", None),
        ("post", "activities", {"type": "task", "title": "x", "lead": str(lead.pk)}),
        ("get", "activity-summary", None),
        ("get", one, None),
        ("patch", one, {"version": 1, "title": "pwned"}),
        *(("post", f"{one}/{action}", {"version": 1}) for action in ACTIONS),
        ("get", f"leads/{lead.pk}/timeline", None),
        ("get", f"opportunities/{opportunity.pk}/timeline", None),
    ]


def cursor_of(page):
    return parse_qs(urlparse(page["next"]).query)["cursor"][0]


@pytest.fixture
def victims_records(user_b):
    lead = LeadFactory(owner=user_b)
    return lead, OpportunityFactory(lead=lead), TaskFactory(lead=lead, title="victim")


class TestWorkspaceSubstitution:
    @pytest.mark.parametrize(
        "segment",
        [
            "VICTIM",
            "VICTIM_UPPER",
            "VICTIM_HEX",
            "{VICTIM}",
            "urn:uuid:VICTIM",
            "%20VICTIM",
            "VICTIM%00",
            "all",
            "ALL",
            "Me",
            "me ",
            "DEACTIVATED",
            "RANDOM",
        ],
    )
    def test_every_route_is_not_found_outside_the_callers_own_workspace(
        self, user_a, user_b, victims_records, segment
    ):
        lead, opportunity, task = victims_records
        value = (
            segment.replace("VICTIM_UPPER", str(user_b.pk).upper())
            .replace("VICTIM_HEX", user_b.pk.hex)
            .replace("VICTIM", str(user_b.pk))
            .replace("DEACTIVATED", str(UserFactory(is_active=False).pk))
            .replace("RANDOM", str(uuid.uuid4()))
        )
        client = signed_in(user_a)
        for method, path, body in every_route(task, lead, opportunity):
            call = getattr(client, method)
            url = f"/api/v1/workspaces/{value}/{path}"
            response = call(url, body, format="json") if body else call(url)
            assert response.status_code == 404, (segment, method, path, response.status_code)
        task.refresh_from_db()
        assert task.version == 1


class TestWritableFields:
    @pytest.mark.parametrize(
        "extra",
        [
            {"owner": "VICTIM"},
            {"owner_id": "VICTIM"},
            {"created_by": "VICTIM"},
            {"completed_at": "NOW"},
            {"completed_by": "VICTIM"},
            {"status": "completed"},
            {"type": "note"},
            {"archived_at": "NOW"},
            {"lead": "OTHER_LEAD"},
            {"opportunity": "OTHER_OPPORTUNITY"},
            {"id": "RANDOM"},
            {"created_at": "NOW"},
            {"updated_at": "NOW"},
            {"current_owner_id": "VICTIM"},
            {"schedule_sort": "NOW"},
            {"is_superuser": True},
            {"capabilities": ["crm.view_all"]},
            {"lead__owner": "VICTIM"},
            {"__proto__": {"x": 1}},
        ],
    )
    def test_an_edit_naming_a_system_field_is_refused_and_changes_nothing(
        self, user_a, user_b, extra
    ):
        lead = LeadFactory(owner=user_a)
        other = LeadFactory(owner=user_a)
        task = TaskFactory(lead=lead, title="mine")
        placeholders = {
            "VICTIM": str(user_b.pk),
            "NOW": timezone.now().isoformat(),
            "OTHER_LEAD": str(other.pk),
            "OTHER_OPPORTUNITY": str(OpportunityFactory(lead=other).pk),
            "RANDOM": str(uuid.uuid4()),
        }
        payload = {
            key: placeholders.get(value, value) if isinstance(value, str) else value
            for key, value in extra.items()
        }
        response = signed_in(user_a).patch(
            f"{ME}/activities/{task.pk}", {"version": 1, **payload}, format="json"
        )
        assert response.status_code == 400, (extra, response.content)
        task.refresh_from_db()
        assert (task.version, task.owner_id, task.lead_id, task.status) == (
            1,
            user_a.pk,
            lead.pk,
            "open",
        )

    @pytest.mark.parametrize("action", ACTIONS)
    @pytest.mark.parametrize(
        "body",
        [
            {"version": 1, "status": "completed"},
            {"version": 1, "owner": "x"},
            {"version": 1, "completed_at": "2026-01-01T00:00:00Z"},
            {},
            [],
            {"version": "1; DROP"},
            {"version": [1]},
            {"version": {"$gt": 0}},
            {"version": True},
            {"version": 1.5},
            {"version": 0},
            {"version": -1},
        ],
    )
    def test_a_lifecycle_action_takes_a_positive_integer_version_and_nothing_else(
        self, user_a, action, body
    ):
        task = TaskFactory(lead=LeadFactory(owner=user_a))
        response = signed_in(user_a).post(
            f"{ME}/activities/{task.pk}/{action}", body, format="json"
        )
        assert response.status_code == 400, (action, body, response.status_code)
        task.refresh_from_db()
        assert task.version == 1

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", {"a": 1}),
            ("title", ["a"]),
            ("title", None),
            ("description", ["x"]),
            ("priority", "urgent"),
            ("priority", None),
            ("due_at", "2026-01-01T00:00:00"),
            ("due_at", 1700000000),
            ("due_at", "9999-12-31T23:59:59+14:00"),
            ("due_at", "0001-01-01T00:00:00-14:00"),
            ("due_at", "2026-01-01T00:00:00+25:00"),
            ("due_at", "1999-12-31T23:59:59Z"),
            ("title", "a\u202eb"),  # a right-to-left override
            ("title", "\u200b"),  # nothing visible
            ("title", "\ud800"),  # a lone surrogate
            pytest.param("title", "x" * 201, id="title-too-long"),
            # 6,000 characters as sent, 12,000 once NFC-normalised (the limit is 10,000)
            pytest.param("description", "\u0958" * 6000, id="description-too-long-once-nfc"),
            ("meeting_url", "https://x.example"),  # meeting fields on a task
            ("starts_at", "2026-01-01T00:00:00Z"),
        ],
    )
    def test_an_invalid_or_inapplicable_task_value_is_refused(self, user_a, field, value):
        task = TaskFactory(lead=LeadFactory(owner=user_a), title="mine")
        response = signed_in(user_a).patch(
            f"{ME}/activities/{task.pk}", {"version": 1, field: value}, format="json"
        )
        assert response.status_code == 400, (field, str(value)[:50], response.content[:300])

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "http://meet.example/x",
            "HTTPS://meet.example/x",
            "https://user:pw@meet.example/x",
            "https://user@meet.example/x",
            "https://:pw@meet.example/x",
            "data:text/html,<script>alert(1)</script>",
            " https://meet.example/x",
            'https://meet.example/"><script>alert(1)</script>',
            "https:/meet.example",
            "https:\\\\meet.example",
            "//meet.example",
            "https://meet.example\t.evil",
            "https://xn--80ak6aa92e.com/",
            "https://127.0.0.1/",
            "https://[::1]/",
            "https://localhost/",
        ],
    )
    def test_a_meeting_link_is_stored_as_https_or_refused(self, user_a, url):
        lead = LeadFactory(owner=user_a)
        start = timezone.now() + timedelta(days=1)
        response = signed_in(user_a).post(
            f"{ME}/activities",
            {
                "type": "meeting",
                "lead": str(lead.pk),
                "title": "m",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "meeting_url": url,
            },
            format="json",
        )
        assert response.status_code in (201, 400)
        if response.status_code == 201:
            assert response.json()["meeting_url"].startswith("https://")


class TestCreateBodies:
    @pytest.mark.parametrize(
        "body",
        [
            [],
            "string",
            123,
            None,
            {"type": "task", "title": "x", "lead": 12345},
            {"type": "task", "title": "x", "lead": -1},
            {"type": "task", "title": "x", "lead": 2**200},
            {"type": "task", "title": "x", "lead": "00000000-0000-0000-0000-000000000000"},
            {"type": "task", "title": "x", "lead": "LEAD", "opportunity": None},
            {"type": "task", "title": "x", "lead": None},
            {"type": "task", "title": "x", "lead": ["LEAD"]},
            {"type": "TASK", "title": "x", "lead": "LEAD"},
            {"type": "\ud800", "title": "x", "lead": "LEAD"},
            {"type": "note", "description": "x", "lead": "LEAD", "due_at": None},
            {
                "type": "meeting",
                "title": "x",
                "lead": "LEAD",
                "starts_at": "2026-01-01T10:00:00+05:30",
                "ends_at": "2026-01-01T10:00:00+05:30",
            },
            {
                "type": "meeting",
                "title": "x",
                "lead": "LEAD",
                "starts_at": "2026-01-01T10:00:00+05:30",
                "ends_at": "2026-01-03T10:00:00+05:30",
            },
            {"type": "meeting", "title": "x", "lead": "LEAD", "starts_at": None, "ends_at": None},
            {"\ud800": 1},
        ],
    )
    def test_a_malformed_create_body_is_refused(self, user_a, body):
        lead = LeadFactory(owner=user_a)

        def own_lead(value):
            if value == "LEAD":
                return str(lead.pk)
            if isinstance(value, list):
                return [own_lead(item) for item in value]
            return value

        if isinstance(body, dict):
            body = {key: own_lead(value) for key, value in body.items()}
        response = signed_in(user_a).post(
            f"{ME}/activities", json.dumps(body), content_type="application/json"
        )
        assert response.status_code in (400, 404), (body, response.status_code, response.content)


class TestListAndCursors:
    @pytest.mark.parametrize(
        "params",
        [
            {"page_size": 0},
            {"page_size": "1e2"},
            {"page_size": 101},
            {"date_from": "99999-01-01"},
            {"date_from": "1999-12-31"},
            {"lead": "not-a-uuid"},
            {"lead": "{00000000-0000-0000-0000-000000000000}"},
            {"cursor": ""},
            {"cursor": "x" * 1001},
            {"ordering": "title"},
            {"ordering": "owner__email"},
            {"type": "note", "status": "open"},
            {"overdue": "maybe"},
            {"search": "x"},
            {"owner": "x"},
        ],
    )
    def test_unusual_list_parameters_are_answered_or_refused_never_a_500(self, user_a, params):
        response = signed_in(user_a).get(f"{ME}/activities", params)
        assert response.status_code in (200, 400), (params, response.status_code)

    def test_cursors_carry_no_titles_or_text(self, user_a):
        lead = LeadFactory(owner=user_a)
        for i in range(3):
            TaskFactory(lead=lead, title=f"Secret title {i}", description="Secret body")
            NoteFactory(lead=lead, description="Secret note text")
        client = signed_in(user_a)
        for ordering in ("-created_at", "created_at", "scheduled", "-scheduled"):
            cursor = cursor_of(
                client.get(f"{ME}/activities", {"page_size": 1, "ordering": ordering}).json()
            )
            # Cursors are signed, not encrypted: anyone can read the payload.
            payload = cursor.split(":")[0]
            decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            assert b"Secret" not in decoded
            assert signing.loads(cursor, salt="arkray.core.keyset")["o"] == ordering

    def test_a_cursor_is_refused_by_another_endpoint_or_ordering(self, user_a):
        lead = LeadFactory(owner=user_a)
        opportunity = OpportunityFactory(lead=lead)
        for _ in range(3):
            TaskFactory(lead=lead)
            NoteFactory(lead=lead, opportunity=opportunity)
        client = signed_in(user_a)
        activities_cursor = cursor_of(client.get(f"{ME}/activities", {"page_size": 1}).json())
        signed_elsewhere = signing.dumps(
            {"o": "-occurred_at", "d": "next", "v": ["2026-01-01T00:00:00+00:00", "abc"]},
            salt="other-salt",
        )
        for url, cursor in [
            (f"{ME}/leads/{lead.pk}/timeline", activities_cursor),
            (f"{ME}/opportunities/{opportunity.pk}/timeline", activities_cursor),
            (f"{ME}/leads/{lead.pk}/timeline", signed_elsewhere),
        ]:
            response = client.get(url, {"cursor": cursor})
            assert response.status_code == 400, (url, response.status_code)
        response = client.get(
            f"{ME}/activities", {"cursor": activities_cursor, "ordering": "scheduled"}
        )
        assert response.status_code == 400

    def test_a_leads_list_cursor_replayed_on_activities_is_answered_or_refused(self, user_a):
        """The leads list signs its cursors with the same key and has an ordering of the same
        name, so the activities list may take one as a position."""
        for _ in range(3):
            LeadFactory(owner=user_a)
        client = signed_in(user_a)
        leads = client.get(f"{ME}/leads", {"page_size": 1}).json()
        assert leads["next"]
        response = client.get(f"{ME}/activities", {"cursor": cursor_of(leads)})
        assert response.status_code in (200, 400)


class TestWhatListsShow:
    def test_list_rows_carry_a_preview_never_the_whole_text_location_or_link(self, user_a):
        lead = LeadFactory(owner=user_a)
        long = "A" * 239 + "B" + "SECRET-TAIL" * 50
        NoteFactory(lead=lead, description=long)
        start = timezone.now() - timedelta(hours=2)
        MeetingFactory(
            lead=lead,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            description=long,
            location="Hidden room",
            meeting_url="https://meet.example/room",
        )
        body = signed_in(user_a).get(f"{ME}/activities").content.decode()
        assert "SECRET-TAIL" not in body
        assert "Hidden room" not in body
        assert "meet.example" not in body

    def test_the_summary_counts_only_the_callers_work_and_takes_no_parameters(self, user_a, user_b):
        TaskFactory(lead=LeadFactory(owner=user_b), due_at=timezone.now() - timedelta(days=1))
        client = signed_in(user_a)
        assert client.get(f"{ME}/activity-summary").json() == {
            "open_tasks": 0,
            "tasks_due_today": 0,
            "overdue_tasks": 0,
            "meetings_today": 0,
            "upcoming_meetings": 0,
        }
        response = client.get(f"{ME}/activity-summary", {"owner": str(user_b.pk)})
        assert response.status_code == 400

    def test_other_spellings_of_another_users_activity_id_are_not_found(self, user_a, user_b):
        task = TaskFactory(lead=LeadFactory(owner=user_b))
        client = signed_in(user_a)
        for spelling in (str(task.pk).upper(), task.pk.hex):
            assert client.get(f"{ME}/activities/{spelling}").status_code == 404
        assert Activity.objects.get(pk=task.pk).version == 1
