"""The critical cross-user suite for Leads (docs/testing.md#critical-cross-user-security-suite).

World: Admin, User A (Rahul) and User B (Priya). A1 and A2 belong to A, B1 to B.
User A must never obtain B1, or learn that it exists, through any channel: list, detail,
guessed id, filters, sorting, search, pagination cursors, duplicate checks, edits, actions,
workspace substitution or crafted payloads. Every case also runs with the roles swapped.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone

from arkray.audit.models import AuditEvent
from arkray.core.keyset import INVALID_CURSOR
from arkray.leads.models import Lead
from tests.factories import LeadFactory, UserFactory
from tests.helpers import signed_in, without_request_id

pytestmark = pytest.mark.django_db

# B1 is deliberately easy to find: unique name, email and number an attacker might try.
SECRET = {
    "first_name": "Zenobia",
    "last_name": "Quillfeather",
    "organization_name": "Secret Pharma",
    "email": "zenobia@secret-pharma.example",
    "phone": "+91 99999 11111",
    "mobile": "+44 20 7946 0000",
    "status_id": "qualified",
    "source_id": "partner",
    "rating": "hot",
}


@pytest.fixture(params=["a_attacks_b", "b_attacks_a"])
def world(request, user_a, user_b):
    """(attacker, victim, attacker's leads, victim's lead)."""
    a1 = LeadFactory(owner=user_a, first_name="Arjun", organization_name="Alpha Labs")
    a2 = LeadFactory(owner=user_a, first_name="Anu", organization_name="Alpha Labs")
    b1 = LeadFactory(owner=user_b, **SECRET, last_contacted_at=timezone.now())
    if request.param == "a_attacks_b":
        return user_a, user_b, [a1, a2], b1
    Lead.objects.filter(pk=b1.pk).update(owner=user_a, created_by=user_a)
    Lead.objects.filter(pk__in=[a1.pk, a2.pk]).update(owner=user_b, created_by=user_b)
    return user_b, user_a, [a1, a2], Lead.objects.get(pk=b1.pk)


def cursor_of(link):
    return parse_qs(urlsplit(link).query)["cursor"][0]


def ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


def walk(client, url, params):
    """Every row reachable by following `next` links."""
    seen, response = [], client.get(url, params)
    while True:
        assert response.status_code == 200, response.content
        body = response.json()
        seen.extend(row["id"] for row in body["results"])
        if not body["next"]:
            return seen
        response = client.get(body["next"])


LIST_QUERIES = [
    {},
    {"archived": "true"},
    {"q": "zenobia"},
    {"q": "quillfeather"},
    {"q": "secret pharma"},
    {"q": "secret-pharma.example"},
    {"q": "9999911111"},
    {"q": "+91 99999"},
    {"q": "7946 0000"},
    {"status": "qualified"},
    {"source": "partner"},
    {"rating": "hot"},
    {"created_from": "2000-01-01", "created_to": "2100-01-01"},
    *({"ordering": o} for o in ["-created_at", "created_at", "name", "-updated_at"]),
    {"ordering": "-last_contacted_at"},
    {"ordering": "last_contacted_at"},
    {"page_size": 1},
    {"page_size": 100},
]


class TestReads:
    @pytest.mark.parametrize("params", LIST_QUERIES)
    def test_no_list_query_ever_returns_the_victims_lead(self, world, params):
        attacker, _, own, secret = world
        seen = walk(signed_in(attacker), "/api/v1/workspaces/me/leads", params)
        assert str(secret.pk) not in seen
        assert set(seen) <= {str(lead.pk) for lead in own}

    def test_the_attacker_sees_exactly_their_own_leads(self, world):
        attacker, _, own, _ = world
        seen = walk(signed_in(attacker), "/api/v1/workspaces/me/leads", {"page_size": 1})
        assert sorted(seen) == sorted(str(lead.pk) for lead in own)

    def test_pagination_metadata_does_not_reveal_other_users_leads(self, world, user_a, user_b):
        """Same pages, links and bodies whether or not the victim has leads at all."""
        attacker, victim, _, _ = world
        client = signed_in(attacker)
        with_victim = client.get("/api/v1/workspaces/me/leads", {"page_size": 2}).json()
        Lead.objects.filter(owner=victim).update(owner=UserFactory())  # victim now has none
        without_victim = client.get("/api/v1/workspaces/me/leads", {"page_size": 2}).json()
        assert with_victim == without_victim
        assert set(with_victim) == {"results", "next", "previous"}  # no counts or totals

    @pytest.mark.parametrize("action", ["", "/status", "/archive", "/restore"])
    def test_a_guessed_id_is_indistinguishable_from_a_missing_one(self, world, action):
        attacker, _, _, secret = world
        client = signed_in(attacker)
        method = client.get if action == "" else client.post
        body = {"": {}, "/status": {"version": 1, "status": "new"}}.get(action, {"version": 1})
        real = method(f"/api/v1/workspaces/me/leads/{secret.pk}{action}", body, format="json")
        ghost = method(f"/api/v1/workspaces/me/leads/{uuid.uuid4()}{action}", body, format="json")
        assert real.status_code == ghost.status_code == 404
        assert without_request_id(real) == without_request_id(ghost)

    def test_patch_on_a_guessed_id_is_indistinguishable_and_changes_nothing(self, world):
        attacker, _, _, secret = world
        client = signed_in(attacker)
        body = {"version": 1, "first_name": "Hacked"}
        real = client.patch(f"/api/v1/workspaces/me/leads/{secret.pk}", body, format="json")
        ghost = client.patch(f"/api/v1/workspaces/me/leads/{uuid.uuid4()}", body, format="json")
        assert real.status_code == ghost.status_code == 404
        assert without_request_id(real) == without_request_id(ghost)
        secret.refresh_from_db()
        assert (secret.first_name, secret.version) == ("Zenobia", 1)

    def test_the_duplicate_check_never_looks_outside_the_scope(self, world):
        attacker, _, _, _ = world
        response = signed_in(attacker).get(
            "/api/v1/workspaces/me/leads/duplicates",
            {"email": SECRET["email"], "phone": [SECRET["phone"], SECRET["mobile"]]},
        )
        assert response.json() == {"results": []}


class TestWorkspaceSubstitution:
    def paths(self, workspace, lead):
        base = f"/api/v1/workspaces/{workspace}/leads"
        return [
            ("get", base, {}),
            ("post", base, {"first_name": "Injected"}),
            ("get", f"{base}/duplicates", {"email": SECRET["email"]}),
            ("get", f"{base}/{lead.pk}", {}),
            ("patch", f"{base}/{lead.pk}", {"version": 1, "city": "Pune"}),
            ("post", f"{base}/{lead.pk}/status", {"version": 1, "status": "new"}),
            ("post", f"{base}/{lead.pk}/archive", {"version": 1}),
            ("post", f"{base}/{lead.pk}/restore", {"version": 1}),
        ]

    @pytest.mark.parametrize(
        "workspace_of",
        [
            lambda victim: str(victim.pk),
            lambda victim: str(victim.pk).upper(),
            lambda _: "all",
            lambda _: "ALL",
            lambda _: str(uuid.uuid4()),
            lambda victim: f"urn:uuid:{victim.pk}",
            lambda victim: str(victim.pk).replace("-", ""),
            lambda _: "..",
            lambda _: "me%00",
        ],
    )
    def test_no_other_workspace_opens_for_a_sales_user(self, world, workspace_of):
        attacker, victim, _, secret = world
        client = signed_in(attacker)
        for method, path, body in self.paths(workspace_of(victim), secret):
            response = getattr(client, method)(path, body, format="json")
            assert response.status_code == 404, (method, path, response.status_code)
        secret.refresh_from_db()
        assert (secret.version, secret.archived_at, secret.city) == (1, None, "")
        assert not Lead.objects.filter(first_name="Injected").exists()

    def test_the_attackers_own_uuid_is_their_own_workspace_only(self, world):
        attacker, _, own, secret = world
        client = signed_in(attacker)
        base = f"/api/v1/workspaces/{attacker.pk}/leads"
        assert ids(client.get(base)) == {str(lead.pk) for lead in own}
        assert client.get(f"{base}/{secret.pk}").status_code == 404


class TestWrites:
    def test_reassignment_is_refused_before_anything_is_looked_up(self, world):
        attacker, _, own, secret = world
        client = signed_in(attacker)
        for lead in (secret, own[0]):
            for workspace in ("me", "all", str(secret.owner_id)):
                response = client.post(
                    f"/api/v1/workspaces/{workspace}/leads/{lead.pk}/assign",
                    {"owner": str(attacker.pk), "version": 1},
                    format="json",
                )
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "permission_denied"
        secret.refresh_from_db()
        assert secret.owner_id != attacker.pk

    @pytest.mark.parametrize(
        "extra",
        [
            lambda victim: {"owner": str(victim.pk)},
            lambda victim: {"owner_id": str(victim.pk)},
            lambda victim: {"created_by": str(victim.pk)},
            lambda victim: {"created_by_id": str(victim.pk)},
            lambda _: {"organization": str(uuid.uuid4())},
            lambda _: {"tenant": "other"},
            lambda _: {"is_archived": True},
            lambda _: {"archived_at": "2026-01-01T00:00:00Z"},
            lambda _: {"version": 999},
            lambda _: {"status_key": "converted"},
            lambda _: {"audit": {"actor_id": str(uuid.uuid4())}},
            lambda _: {"metadata": {"x": 1}},
            lambda _: {"__class__": "Lead"},
        ],
    )
    def test_crafted_create_payloads_cannot_escape_the_rules(self, world, extra):
        attacker, victim, _, _ = world
        response = signed_in(attacker).post(
            "/api/v1/workspaces/me/leads", {"first_name": "Mallory", **extra(victim)}, format="json"
        )
        assert response.status_code == 400
        assert not Lead.objects.filter(first_name="Mallory").exists()
        assert not Lead.objects.filter(owner=victim, created_by=attacker).exists()

    @pytest.mark.parametrize(
        "extra",
        [
            lambda victim: {"owner": str(victim.pk)},
            lambda victim: {"created_by": str(victim.pk)},
            lambda _: {"status": "converted"},
            lambda _: {"archived_at": None},
            lambda _: {"is_archived": False},
            lambda _: {"created_at": "2001-01-01T00:00:00Z"},
            lambda _: {"id": str(uuid.uuid4())},
            lambda _: {"organization": "x"},
        ],
    )
    def test_crafted_patches_cannot_change_system_fields(self, world, extra):
        attacker, victim, own, _ = world
        lead = own[0]
        response = signed_in(attacker).patch(
            f"/api/v1/workspaces/me/leads/{lead.pk}",
            {"version": 1, "city": "Pune", **extra(victim)},
            format="json",
        )
        assert response.status_code == 400
        lead.refresh_from_db()
        assert (lead.owner_id, lead.city, lead.version) == (attacker.pk, "", 1)

    @pytest.mark.parametrize("action", ["status", "archive", "restore"])
    def test_action_payloads_are_strict(self, world, action):
        attacker, victim, own, _ = world
        response = signed_in(attacker).post(
            f"/api/v1/workspaces/me/leads/{own[0].pk}/{action}",
            {"version": 1, "status": "contacted", "owner": str(victim.pk)},
            format="json",
        )
        assert response.status_code == 400

    def test_nothing_the_attacker_tried_was_recorded_as_a_change_to_the_victims_lead(self, world):
        attacker, _, _, secret = world
        client = signed_in(attacker)
        client.patch(f"/api/v1/workspaces/me/leads/{secret.pk}", {"version": 1}, format="json")
        client.post(
            f"/api/v1/workspaces/me/leads/{secret.pk}/archive", {"version": 1}, format="json"
        )
        assert not AuditEvent.objects.filter(target_type="lead", target_id=str(secret.pk)).exists()


class TestCursors:
    def test_a_cursor_issued_to_the_victim_is_refused_for_the_attacker(self, world):
        """Since Phase 9 a cursor is bound to its user and workspace: the replay is a 400
        (before: a page of the attacker's own leads). Nothing of the victim's either way."""
        attacker, victim, _, _ = world
        LeadFactory.create_batch(3, owner=victim)
        victim_page = signed_in(victim).get("/api/v1/workspaces/me/leads", {"page_size": 1}).json()
        cursor = cursor_of(victim_page["next"])
        response = signed_in(attacker).get(
            "/api/v1/workspaces/me/leads", {"page_size": 100, "cursor": cursor}
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"] == {"cursor": [INVALID_CURSOR]}

    def test_a_cursor_cannot_carry_another_workspace(self, world):
        """Cursors hold sort positions only: the workspace always comes from the URL."""
        attacker, victim, _, _ = world
        admin_cursor_source = (
            signed_in(victim).get("/api/v1/workspaces/me/leads", {"page_size": 1}).json()
        )
        assert admin_cursor_source["next"] is None or "workspaces/me" in admin_cursor_source["next"]
        response = signed_in(attacker).get(
            f"/api/v1/workspaces/{victim.pk}/leads", {"cursor": "anything"}
        )
        assert response.status_code == 404  # the workspace is refused before the cursor is read


class TestAdministrators:
    def test_admin_access_follows_explicit_capabilities(self, admin_client, user_a, user_b):
        a1 = LeadFactory(owner=user_a)
        b1 = LeadFactory(owner=user_b)
        assert ids(admin_client.get("/api/v1/workspaces/all/leads")) >= {str(a1.pk), str(b1.pk)}
        assert ids(admin_client.get(f"/api/v1/workspaces/{user_b.pk}/leads")) == {str(b1.pk)}
        # B's lead is not in A's workspace, even for an admin.
        assert admin_client.get(f"/api/v1/workspaces/{user_a.pk}/leads/{b1.pk}").status_code == 404
        assert admin_client.get(f"/api/v1/workspaces/me/leads/{b1.pk}").status_code == 404
