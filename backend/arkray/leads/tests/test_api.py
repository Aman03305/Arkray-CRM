"""Leads API: create, read, edit, status, archive, options, list search/filters/sorting."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from arkray.leads.models import Lead, LeadSource, LeadStatus
from tests.factories import LeadFactory

from .conftest import lead_url, leads_url

pytestmark = pytest.mark.django_db

IST = ZoneInfo("Asia/Kolkata")


def create(client, workspace="me", **body):
    return client.post(leads_url(workspace), body, format="json")


def ids(response):
    return [row["id"] for row in response.json()["results"]]


class TestCreate:
    def test_a_minimal_lead_needs_only_a_name(self, user_a_client, user_a):
        response = create(user_a_client, first_name="Rahul")
        assert response.status_code == 201, response.content
        body = response.json()
        assert body["display_name"] == "Rahul"
        assert body["status"] == {"key": "new", "name": "New", "category": "open"}
        assert body["owner"]["id"] == str(user_a.pk)
        assert body["created_by"]["id"] == str(user_a.pk)
        assert body["version"] == 1
        assert response["Location"] == f"/api/v1/workspaces/me/leads/{body['id']}"

    def test_an_organisation_only_lead_is_allowed(self, user_a_client):
        response = create(user_a_client, organization_name="Apollo Diagnostics")
        assert response.status_code == 201
        assert response.json()["display_name"] == "Apollo Diagnostics"

    def test_a_lead_with_nobody_is_refused_on_the_name_field(self, user_a_client):
        response = create(user_a_client, email="someone@example.test")
        assert response.status_code == 400
        assert set(response.json()["error"]["details"]) == {"first_name"}
        assert not Lead.objects.exists()

    def test_every_field_round_trips(self, user_a_client):
        contacted = (timezone.now() - timedelta(days=2)).replace(microsecond=0)
        payload = {
            "first_name": "Priya",
            "last_name": "Patel",
            "organization_name": "Apollo Hospitals",
            "job_title": "Lab Director",
            "email": "Priya.Patel@apollo.example",
            "phone": "+91 22 2345 6789 ext. 12",
            "mobile": "+91 98765 43210",
            "alternate_phone": "022 2345 6700",
            "address_line_1": "Plot 7, Sector 5",
            "address_line_2": "CBD Belapur",
            "city": "Navi Mumbai",
            "state": "Maharashtra",
            "postal_code": "400614",
            "country": "IN",
            "source": "referral",
            "status": "contacted",
            "rating": "hot",
            "last_contacted_at": contacted.astimezone(IST).isoformat(),
            "description": "Evaluating analysers.\nBudget approved.",
        }
        body = create(user_a_client, **payload).json()
        for field, value in payload.items():
            if field in {"status", "source"}:
                assert body[field]["key"] == value
            elif field == "last_contacted_at":
                assert datetime.fromisoformat(body[field]) == contacted
                assert body[field].endswith("Z")  # UTC out
            else:
                assert body[field] == value, field

    def test_an_invisible_name_does_not_count_as_a_name(self, user_a_client):
        response = create(user_a_client, first_name=chr(0x200D), last_name=chr(0x200C))
        assert response.status_code == 400
        assert "first_name" in response.json()["error"]["details"]

    def test_unicode_names_are_stored_exactly(self, user_a_client):
        body = create(user_a_client, first_name="राजेश", last_name="कुमार").json()
        assert body["display_name"] == "राजेश कुमार"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("email", "not-an-email"),
            ("phone", "call me maybe"),
            ("country", "India"),
            ("rating", "lukewarm"),
            ("status", "won"),
            ("source", "tv"),
            ("last_contacted_at", "2026-09-01T10:00:00"),  # no offset
            ("last_contacted_at", "2999-01-01T00:00:00Z"),  # the future
            ("first_name", "Evil" + chr(0x202E) + "Name"),
            ("postal_code", "400/001"),
            ("first_name", "x" * 101),
        ],
    )
    def test_invalid_values_are_refused_with_the_field_named(self, user_a_client, field, value):
        body = {"first_name": "Rahul", field: value}
        response = create(user_a_client, **body)
        assert response.status_code == 400
        assert field in response.json()["error"]["details"]
        assert not Lead.objects.exists()

    def test_inactive_status_or_source_cannot_be_chosen(self, user_a_client):
        LeadStatus.objects.filter(key="unqualified").update(is_active=False)
        LeadSource.objects.filter(key="event").update(is_active=False)
        assert create(user_a_client, first_name="R", status="unqualified").status_code == 400
        assert create(user_a_client, first_name="R", source="event").status_code == 400

    @pytest.mark.parametrize(
        "extra",
        [
            {"created_by": "5a1e4d2c-0000-4000-8000-00000000abcd"},
            {"id": "5a1e4d2c-0000-4000-8000-00000000abcd"},
            {"version": 99},
            {"archived_at": "2026-01-01T00:00:00Z"},
            {"is_archived": True},
            {"organization": "5a1e4d2c-0000-4000-8000-00000000abcd"},
            {"tenant_id": "x"},
            {"created_at": "2020-01-01T00:00:00Z"},
            {"display_name": "Someone else"},
            {"phone_keys": ["+1"]},
            {"search_text": "X"},
        ],
    )
    def test_system_fields_in_the_payload_are_refused(self, user_a_client, extra):
        response = create(user_a_client, first_name="Rahul", **extra)
        assert response.status_code == 400
        assert "Unknown field" in str(response.json()["error"]["details"])
        assert not Lead.objects.exists()


class TestIdempotentCreate:
    KEY = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"

    def post(self, client, key=KEY, **body):
        return client.post(leads_url(), body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_a_retried_request_returns_the_original_lead(self, user_a_client):
        first = self.post(user_a_client, first_name="Rahul")
        second = self.post(user_a_client, first_name="Rahul")
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert second["Idempotent-Replayed"] == "true"
        assert Lead.objects.count() == 1

    def test_the_same_key_with_a_different_body_is_refused(self, user_a_client):
        self.post(user_a_client, first_name="Rahul")
        response = self.post(user_a_client, first_name="Priya")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "idempotency_key_reused"
        assert Lead.objects.count() == 1

    def test_keys_are_per_user(self, user_a_client, user_b_client):
        a = self.post(user_a_client, first_name="Rahul").json()["id"]
        b = self.post(user_b_client, first_name="Rahul").json()["id"]
        assert a != b
        assert Lead.objects.count() == 2

    def test_without_a_key_every_request_creates(self, user_a_client):
        create(user_a_client, first_name="Rahul")
        create(user_a_client, first_name="Rahul")
        assert Lead.objects.count() == 2

    @pytest.mark.parametrize("key", ["not-a-uuid", "", "{3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b}"])
    def test_a_malformed_key_is_refused(self, user_a_client, key):
        response = self.post(user_a_client, key=key, first_name="Rahul")
        assert response.status_code == 400
        assert "idempotency_key" in response.json()["error"]["details"]

    def test_a_failed_request_does_not_consume_the_key(self, user_a_client):
        assert self.post(user_a_client, email="bad").status_code == 400
        assert self.post(user_a_client, first_name="Rahul").status_code == 201


class TestRead:
    def test_detail_includes_everything_list_items_are_bounded(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, description="Long notes", city="Pune")
        detail = user_a_client.get(lead_url(lead.pk)).json()
        assert detail["description"] == "Long notes"
        assert detail["city"] == "Pune"
        item = user_a_client.get(leads_url()).json()["results"][0]
        assert "description" not in item
        assert "city" not in item
        assert "created_by" not in item
        assert "search_text" not in item
        assert "phone_keys" not in item

    def test_an_unknown_lead_is_404(self, user_a_client):
        response = user_a_client.get(lead_url(uuid.uuid4()))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_a_non_uuid_lead_id_is_404(self, user_a_client):
        assert user_a_client.get("/api/v1/workspaces/me/leads/not-a-uuid").status_code == 404

    def test_the_empty_workspace_is_an_empty_page(self, user_a_client):
        assert user_a_client.get(leads_url()).json() == {
            "results": [],
            "next": None,
            "previous": None,
        }

    def test_people_are_rendered_minimally(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a)
        owner = user_a_client.get(lead_url(lead.pk)).json()["owner"]
        assert owner == {"id": str(user_a.pk), "full_name": "Rahul Sharma", "is_active": True}


class TestEdit:
    def test_changes_are_saved_and_the_version_moves(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.patch(
            lead_url(lead.pk), {"version": 1, "city": "Pune", "rating": "warm"}, format="json"
        )
        assert response.status_code == 200
        assert (response.json()["city"], response.json()["version"]) == ("Pune", 2)

    def test_a_stale_version_is_a_conflict_and_changes_nothing(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, version=3)
        response = user_a_client.patch(
            lead_url(lead.pk), {"version": 2, "city": "Pune"}, format="json"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        lead.refresh_from_db()
        assert (lead.city, lead.version) == ("", 3)

    def test_the_version_is_required(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a)
        assert (
            user_a_client.patch(lead_url(lead.pk), {"city": "Pune"}, format="json").status_code
            == 400
        )

    def test_saving_identical_values_is_a_no_op(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, city="Pune")
        response = user_a_client.patch(
            lead_url(lead.pk), {"version": 1, "city": " Pune "}, format="json"
        )
        assert response.json()["version"] == 1

    def test_clearing_every_name_is_refused(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, first_name="R", last_name="", organization_name="")
        response = user_a_client.patch(
            lead_url(lead.pk), {"version": 1, "first_name": ""}, format="json"
        )
        assert response.status_code == 400
        assert "first_name" in response.json()["error"]["details"]

    @pytest.mark.parametrize(
        "field",
        [
            "owner",
            "owner_id",
            "status",
            "created_by",
            "created_at",
            "updated_at",
            "archived_at",
            "id",
            "display_name",
            "is_archived",
        ],
    )
    def test_system_fields_cannot_be_patched(self, user_a_client, user_a, user_b, field):
        lead = LeadFactory(owner=user_a)
        response = user_a_client.patch(
            lead_url(lead.pk), {"version": 1, field: str(user_b.pk)}, format="json"
        )
        assert response.status_code == 400
        lead.refresh_from_db()
        assert (lead.owner_id, lead.created_by_id, lead.version) == (user_a.pk, user_a.pk, 1)

    def test_archived_leads_are_read_only(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        response = user_a_client.patch(
            lead_url(lead.pk), {"version": 1, "city": "Pune"}, format="json"
        )
        assert response.status_code == 422
        assert "Restore" in response.json()["error"]["message"]


class TestStatus:
    def change(self, client, lead, status, version=1):
        return client.post(
            lead_url(lead.pk, action="status"),
            {"status": status, "version": version},
            format="json",
        )

    @pytest.mark.parametrize("target", ["contacted", "qualified", "unqualified", "new"])
    def test_any_active_status_can_be_chosen(self, user_a_client, user_a, target):
        lead = LeadFactory(owner=user_a, status_id="contacted")
        response = self.change(user_a_client, lead, target)
        assert response.status_code == 200
        assert response.json()["status"]["key"] == target

    def test_converted_needs_an_opportunity(self, user_a_client, user_a):
        """Phase 3: "Converted" means the lead has entered the opportunity process, so a
        lead without an opportunity can't simply be marked Converted (the pipeline module
        vetoes the change; arkray/pipeline/tests/test_conversion.py covers the rest)."""
        lead = LeadFactory(owner=user_a, status_id="qualified")
        response = self.change(user_a_client, lead, "converted")
        assert response.status_code == 422
        assert "opportunity" in response.json()["error"]["message"]
        lead.refresh_from_db()
        assert (lead.status_id, lead.version) == ("qualified", 1)

    def test_a_converted_lead_can_be_corrected(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, status_id="converted")
        assert self.change(user_a_client, lead, "qualified").status_code == 200

    def test_moving_to_the_current_status_is_a_no_op_even_with_an_old_version(
        self, user_a_client, user_a
    ):
        lead = LeadFactory(owner=user_a, status_id="contacted", version=4)
        response = self.change(user_a_client, lead, "contacted", version=1)
        assert (response.status_code, response.json()["version"]) == (200, 4)

    def test_stale_version_conflicts(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, version=2)
        assert self.change(user_a_client, lead, "contacted", version=1).status_code == 409

    def test_unknown_or_retired_statuses_are_refused(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a)
        assert self.change(user_a_client, lead, "won").status_code == 400
        LeadStatus.objects.filter(key="unqualified").update(is_active=False)
        assert self.change(user_a_client, lead, "unqualified").status_code == 400

    def test_a_lead_may_stay_in_a_retired_status(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, status_id="unqualified")
        LeadStatus.objects.filter(key="unqualified").update(is_active=False)
        body = user_a_client.get(lead_url(lead.pk)).json()
        assert body["status"]["key"] == "unqualified"
        edit = user_a_client.patch(lead_url(lead.pk), {"version": 1, "city": "Pune"}, format="json")
        assert edit.status_code == 200

    def test_archived_leads_keep_their_status(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now())
        assert self.change(user_a_client, lead, "contacted").status_code == 422


class TestArchive:
    def test_archive_hides_from_the_default_list_and_restore_brings_back(
        self, user_a_client, user_a
    ):
        lead = LeadFactory(owner=user_a)
        archived = user_a_client.post(
            lead_url(lead.pk, action="archive"), {"version": 1}, format="json"
        )
        assert archived.status_code == 200
        assert archived.json()["archived_at"]
        assert ids(user_a_client.get(leads_url())) == []
        assert ids(user_a_client.get(leads_url(suffix="?archived=true"))) == [str(lead.pk)]
        assert user_a_client.get(lead_url(lead.pk)).status_code == 200  # still readable
        restored = user_a_client.post(
            lead_url(lead.pk, action="restore"), {"version": 2}, format="json"
        )
        assert restored.status_code == 200
        assert restored.json()["archived_at"] is None
        assert ids(user_a_client.get(leads_url())) == [str(lead.pk)]
        assert Lead.objects.filter(pk=lead.pk).exists()  # never deleted

    def test_archive_and_restore_are_idempotent(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, archived_at=timezone.now(), version=5)
        again = user_a_client.post(
            lead_url(lead.pk, action="archive"), {"version": 1}, format="json"
        )
        assert (again.status_code, again.json()["version"]) == (200, 5)
        active = LeadFactory(owner=user_a, version=3)
        restore = user_a_client.post(
            lead_url(active.pk, action="restore"), {"version": 1}, format="json"
        )
        assert (restore.status_code, restore.json()["version"]) == (200, 3)

    def test_archive_requires_the_current_version(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, version=2)
        response = user_a_client.post(
            lead_url(lead.pk, action="archive"), {"version": 1}, format="json"
        )
        assert response.status_code == 409

    def test_there_is_no_delete(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a)
        assert user_a_client.delete(lead_url(lead.pk)).status_code == 405
        assert Lead.objects.filter(pk=lead.pk).exists()


class TestOptions:
    def test_options_list_configuration_for_forms(self, user_a_client):
        LeadSource.objects.filter(key="event").update(is_active=False)
        body = user_a_client.get("/api/v1/config/lead-options").json()
        assert [s["key"] for s in body["statuses"]] == [
            "new",
            "contacted",
            "qualified",
            "unqualified",
            "converted",
        ]
        assert next(s for s in body["sources"] if s["key"] == "event")["is_active"] is False
        assert body["ratings"] == [
            {"key": "hot", "name": "Hot"},
            {"key": "warm", "name": "Warm"},
            {"key": "cold", "name": "Cold"},
        ]
        assert "IN" in body["countries"]
        assert len(body["countries"]) == 249


class TestListFilters:
    @pytest.fixture
    def mix(self, user_a):
        return {
            "hot_new": LeadFactory(
                owner=user_a, rating="hot", status_id="new", source_id="website"
            ),
            "warm_contacted": LeadFactory(
                owner=user_a, rating="warm", status_id="contacted", source_id="referral"
            ),
            "cold_qualified": LeadFactory(owner=user_a, rating="cold", status_id="qualified"),
        }

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("status=contacted", ["warm_contacted"]),
            ("source=website", ["hot_new"]),
            ("rating=cold", ["cold_qualified"]),
            ("status=new&rating=warm", []),
        ],
    )
    def test_filters_combine(self, user_a_client, mix, query, expected):
        response = user_a_client.get(leads_url(suffix=f"?{query}"))
        assert response.status_code == 200
        assert sorted(ids(response)) == sorted(str(mix[k].pk) for k in expected)

    def test_created_dates_are_business_days_inclusive(self, user_a_client, user_a):
        def at(day, hour):  # an IST wall-clock time
            return datetime(2026, 9, day, hour, 0, tzinfo=IST)

        early = LeadFactory(owner=user_a)
        late = LeadFactory(owner=user_a)
        outside = LeadFactory(owner=user_a)
        # 00:30 IST on the 10th is still the 9th in UTC: it belongs to the 10th here.
        for lead, moment in [(early, at(10, 0)), (late, at(11, 23)), (outside, at(12, 0))]:
            Lead.objects.filter(pk=lead.pk).update(
                created_at=moment + timedelta(minutes=30), updated_at=timezone.now()
            )
        response = user_a_client.get(
            leads_url(suffix="?created_from=2026-09-10&created_to=2026-09-11")
        )
        assert sorted(ids(response)) == sorted([str(early.pk), str(late.pk)])

    def test_an_inverted_date_range_is_refused(self, user_a_client):
        response = user_a_client.get(
            leads_url(suffix="?created_from=2026-09-11&created_to=2026-09-10")
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "query",
        [
            "owner__email__contains=a",
            "email=a@b.c",
            "status__in=new",
            "ordering=email",
            "ordering=-id",
            "ordering=owner__password",
            "page_size=101",
            "page_size=0",
            "q=a",
            "q=" + "x" * 101,
            "rating=lukewarm",
            "archived=maybe",
            "created_from=yesterday",
        ],
    )
    def test_unknown_or_invalid_parameters_are_refused(self, user_a_client, query):
        response = user_a_client.get(leads_url(suffix=f"?{query}"))
        assert response.status_code == 400, query
        assert response.json()["error"]["code"] == "validation_error"

    def test_filtering_by_owner_is_for_the_organisation_workspace_only(self, user_a_client, user_a):
        response = user_a_client.get(leads_url(suffix=f"?owner={user_a.pk}"))
        assert response.status_code == 400
        assert "owner" in response.json()["error"]["details"]


class TestSearch:
    @pytest.fixture
    def people(self, user_a):
        return {
            "priya": LeadFactory(
                owner=user_a,
                first_name="Priya",
                last_name="Patel",
                organization_name="Apollo Hospitals",
                email="priya@apollo.example",
                phone="+91 98765 43210",
            ),
            "rajesh": LeadFactory(
                owner=user_a,
                first_name="राजेश",
                last_name="Kumar",
                organization_name="Metro Labs",
                email="rk@metro.example",
                mobile="(022) 2345-6789",
            ),
            "org": LeadFactory(
                owner=user_a,
                first_name="",
                last_name="",
                organization_name="Zoë's Clinic",
                email="",
            ),
        }

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("priya", ["priya"]),
            ("PATEL", ["priya"]),
            ("apollo", ["priya"]),
            ("priya patel", ["priya"]),
            ("priya metro", []),  # every term must match
            ("metro.example", ["rajesh"]),
            ("राजेश", ["rajesh"]),
            ("9876543210", ["priya"]),
            ("98765-43210", ["priya"]),
            ("+91 98765", ["priya"]),
            ("2345 6789", ["rajesh"]),
            ("zoë", ["org"]),
            ("clinic", ["org"]),
            ("%%", []),  # LIKE wildcards are literal
            ("__", []),
        ],
    )
    def test_search_matches_names_organisation_email_and_phone(
        self, user_a_client, people, q, expected
    ):
        response = user_a_client.get(leads_url(), {"q": q})
        assert response.status_code == 200, response.content
        assert sorted(ids(response)) == sorted(str(people[k].pk) for k in expected)

    def test_search_ignores_other_fields(self, user_a_client, user_a):
        LeadFactory(owner=user_a, description="secret project", city="Chennai")
        assert ids(user_a_client.get(leads_url(), {"q": "secret"})) == []
        assert ids(user_a_client.get(leads_url(), {"q": "chennai"})) == []

    def test_search_combines_with_filters_and_archive(self, user_a_client, people):
        Lead.objects.filter(pk=people["priya"].pk).update(archived_at=timezone.now())
        assert ids(user_a_client.get(leads_url(), {"q": "priya"})) == []
        assert ids(user_a_client.get(leads_url(), {"q": "priya", "archived": "true"})) == [
            str(people["priya"].pk)
        ]

    @pytest.mark.parametrize(
        "q",
        ["ab" + chr(0x202E) + "cd", "ab" + chr(7) + "cd", "ab" + chr(0) + "cd", "ab" + chr(0x2066)],
    )
    def test_control_characters_in_a_search_are_a_400_not_a_500(self, user_a_client, q):
        """Self-review finding: these reached clean_line() in the selector uncaught."""
        response = user_a_client.get(leads_url(), {"q": q})
        assert response.status_code == 400
        assert "q" in response.json()["error"]["details"]

    def test_the_selector_itself_refuses_them_too(self, user_a):
        from arkray.core.access import AccessScope
        from arkray.core.errors import InvalidInputError
        from arkray.leads.selectors import LeadFilters, lead_list

        with pytest.raises(InvalidInputError):
            list(lead_list(AccessScope.own(user_a.pk), LeadFilters(q="x" + chr(0x202E) + "y")))

    def test_at_most_five_terms_are_used(self, user_a_client, people):
        q = "priya patel apollo 98765 example nonsense-sixth-term"
        assert ids(user_a_client.get(leads_url(), {"q": q})) == [str(people["priya"].pk)]


class TestSortingAndPaging:
    @pytest.fixture
    def named(self, user_a):
        base = timezone.now() - timedelta(days=10)
        leads = {}
        for i, name in enumerate(["Charlie", "alpha", "Bravo", "Émile", "delta"]):
            lead = LeadFactory(
                owner=user_a,
                first_name=name,
                last_name="",
                last_contacted_at=None if i % 2 else base + timedelta(days=i),
            )
            Lead.objects.filter(pk=lead.pk).update(
                created_at=base + timedelta(hours=i), updated_at=base + timedelta(days=5 - i)
            )
            leads[name] = lead
        return leads

    def names(self, response):
        return [row["display_name"] for row in response.json()["results"]]

    @pytest.mark.parametrize(
        ("ordering", "expected"),
        [
            ("-created_at", ["delta", "Émile", "Bravo", "alpha", "Charlie"]),
            ("created_at", ["Charlie", "alpha", "Bravo", "Émile", "delta"]),
            ("name", ["alpha", "Bravo", "Charlie", "delta", "Émile"]),  # case- and accent-aware
            ("-updated_at", ["Charlie", "alpha", "Bravo", "Émile", "delta"]),
        ],
    )
    def test_every_allowed_ordering(self, user_a_client, named, ordering, expected):
        response = user_a_client.get(leads_url(), {"ordering": ordering})
        assert self.names(response) == expected

    def test_last_contacted_orderings_place_never_contacted_leads_deliberately(
        self, user_a_client, named
    ):
        never = sorted([named["alpha"], named["Émile"]], key=lambda lead: str(lead.pk))
        recent = self.names(user_a_client.get(leads_url(), {"ordering": "-last_contacted_at"}))
        assert recent[:3] == ["delta", "Bravo", "Charlie"]  # most recent first ...
        assert recent[3:] == [n.first_name for n in reversed(never)]  # ... never contacted last
        stale = self.names(user_a_client.get(leads_url(), {"ordering": "last_contacted_at"}))
        assert stale[:2] == [n.first_name for n in never]  # never contacted first ...
        assert stale[2:] == ["Charlie", "Bravo", "delta"]  # ... then longest ago

    def test_pages_follow_next_and_previous_links(self, user_a_client, named):
        first = user_a_client.get(leads_url(), {"ordering": "name", "page_size": 2}).json()
        assert [r["display_name"] for r in first["results"]] == ["alpha", "Bravo"]
        assert first["previous"] is None
        second = user_a_client.get(first["next"]).json()
        assert [r["display_name"] for r in second["results"]] == ["Charlie", "delta"]
        third = user_a_client.get(second["next"]).json()
        assert [r["display_name"] for r in third["results"]] == ["Émile"]
        assert third["next"] is None
        back = user_a_client.get(third["previous"]).json()
        assert [r["display_name"] for r in back["results"]] == ["Charlie", "delta"]

    def test_links_keep_the_filters_and_only_change_the_cursor(self, user_a_client, named):
        first = user_a_client.get(
            leads_url(), {"ordering": "name", "page_size": 1, "q": "apollo"}
        ).json()
        assert "q=apollo" in first["next"]
        assert "ordering=name" in first["next"]

    def test_a_cursor_from_another_ordering_is_refused(self, user_a_client, named):
        first = user_a_client.get(leads_url(), {"ordering": "name", "page_size": 1}).json()
        cursor = parse_qs(urlsplit(first["next"]).query)["cursor"][0]
        response = user_a_client.get(leads_url(), {"ordering": "-created_at", "cursor": cursor})
        assert response.status_code == 400
        assert "cursor" in response.json()["error"]["details"]

    @pytest.mark.parametrize("cursor", ["garbage", "a" * 1000, "%00", "eyJ9"])
    def test_malformed_cursors_are_400_never_500(self, user_a_client, named, cursor):
        response = user_a_client.get(leads_url(), {"cursor": cursor})
        assert response.status_code == 400

    def test_page_size_is_bounded_and_defaults_to_25(self, user_a_client, user_a):
        LeadFactory.create_batch(30, owner=user_a)
        assert len(ids(user_a_client.get(leads_url()))) == 25
        assert len(ids(user_a_client.get(leads_url(), {"page_size": 100}))) == 30
