"""Regression tests for the Phase 2 adversarial review: one per confirmed backend finding,
so none can silently return. Finding ids: S = API security review, D = domain/performance
review, SR = found in self-review during the same pass."""

from __future__ import annotations

import base64
import bz2
import json
import uuid
import zlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core import idempotency, keyset
from arkray.core.access import AccessScope
from arkray.core.errors import ConflictError, InvalidInputError
from arkray.core.models import IdempotencyRecord
from arkray.leads import selectors, services
from arkray.leads.models import Lead
from arkray.leads.phones import phone_key
from arkray.leads.validation import clean_fields
from tests.factories import AdminFactory, LeadFactory, UserFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

LEADS = "/api/v1/workspaces/me/leads"


def post_raw(client, path, body: bytes, content_type="application/json"):
    return client.generic("POST", path, body, content_type=content_type)


def cursor_payload(link: str):
    """The JSON inside a cursor. Since Phase 9 (R48) cursors are sealed, so only the server
    can read it: what anyone holding the URL sees is ciphertext (checked here too)."""
    token = parse_qs(urlsplit(link).query)["cursor"][0]
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert b'"v"' not in raw  # nothing readable in the URL
    return keyset._open(token)


class TestRequestParsing:
    """S-F1 (P1): the charset in Content-Type was used as a codec, so compression codecs
    inflated tiny bodies (a decompression bomb, even without signing in)."""

    @pytest.mark.parametrize(
        ("charset", "encode"),
        [
            ("zlib", zlib.compress),
            ("bz2", bz2.compress),
            ("rot13", lambda b: b),
            ("zlib", lambda b: b),
        ],
    )
    def test_non_utf8_charsets_are_refused_before_decoding(self, user_a_client, charset, encode):
        body = encode(b'{"first_name":"Zipped"}')
        response = post_raw(user_a_client, LEADS, body, f"application/json; charset={charset}")
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_media_type"
        assert not Lead.objects.exists()

    def test_the_bomb_is_refused_on_anonymous_endpoints_too(self):
        payload = b'{"email":"x@example.test","password":"' + b"p" * 1_000_000 + b'"}'
        response = post_raw(
            APIClient(),
            "/api/v1/auth/login",
            zlib.compress(payload, 9),
            "application/json; charset=zlib",
        )
        assert response.status_code == 415

    @pytest.mark.parametrize("charset", ["utf-8", "UTF-8", "utf8"])
    def test_utf8_in_any_spelling_still_works(self, user_a_client, charset):
        body = '{"first_name":"राजेश"}'.encode()
        response = post_raw(user_a_client, LEADS, body, f"application/json; charset={charset}")
        assert response.status_code == 201

    @pytest.mark.parametrize(
        "body",
        [b"[" * 100_000 + b"]" * 100_000, b'{"a":' * 50_000 + b"1" + b"}" * 50_000],
        ids=["arrays", "objects"],  # pytest puts param values in an env var otherwise
    )
    def test_deeply_nested_json_is_a_400_not_a_500(self, user_a_client, body):
        """S-F3."""
        response = post_raw(user_a_client, LEADS, body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_request"


class TestEchoedInput:
    """S-F2: an unknown key holding a lone surrogate was echoed into the error and crashed
    the renderer (500)."""

    def test_surrogate_keys_are_a_400(self, user_a_client, user_a):
        assert post_raw(user_a_client, LEADS, b'{"first_name":"x","\\ud800":1}').status_code == 400
        lead = LeadFactory(owner=user_a)
        response = user_a_client.generic(
            "PATCH",
            f"{LEADS}/{lead.pk}",
            b'{"version":1,"\\udfff":1}',
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_surrogates_in_choice_values_are_a_400(self, user_a_client):
        response = post_raw(user_a_client, LEADS, b'{"first_name":"x","rating":"\\ud800"}')
        assert response.status_code == 400


class TestQueryParameters:
    def test_extreme_dates_are_a_400_not_a_500(self, user_a_client):
        """S-F4 / D-F5: created_to=9999-12-31 overflowed date arithmetic."""
        for params in [{"created_to": "9999-12-31"}, {"created_from": "0001-01-01"}]:
            response = user_a_client.get(LEADS, params)
            assert response.status_code == 400

    @pytest.mark.parametrize(
        ("method", "suffix", "body"),
        [
            ("get", "/{id}", None),
            ("post", "", {"first_name": "X"}),
            ("patch", "/{id}", {"version": 1, "city": "Pune"}),
            ("post", "/{id}/archive", {"version": 1}),
        ],
    )
    def test_query_parameters_are_refused_where_none_are_read(
        self, user_a_client, user_a, method, suffix, body
    ):
        """S-info: '?owner=...' on a create was silently ignored instead of refused."""
        lead = LeadFactory(owner=user_a)
        path = f"{LEADS}{suffix.format(id=lead.pk)}?owner={user_a.pk}"
        response = getattr(user_a_client, method)(path, body, format="json")
        assert response.status_code == 400
        assert Lead.objects.count() == 1
        lead.refresh_from_db()
        assert (lead.city, lead.version) == ("", 1)

    def test_options_refuse_query_parameters(self, user_a_client):
        assert user_a_client.get("/api/v1/config/lead-options?x=1").status_code == 400

    def test_one_character_search_words_alone_are_refused_and_ignored_otherwise(
        self, user_a_client, user_a
    ):
        """D-info: 'Q Z' ran two one-character LIKE scans the trigram index can't serve."""
        LeadFactory(owner=user_a, first_name="Rahul", last_name="Sharma")
        assert user_a_client.get(LEADS, {"q": "Q Z"}).status_code == 400
        rows = user_a_client.get(LEADS, {"q": "Rahul S"}).json()["results"]
        assert [r["first_name"] for r in rows] == ["Rahul"]


class TestCursors:
    def test_name_cursors_never_carry_names(self, user_a_client, user_a):
        """S-F5: names travelled in cursors (URLs, proxy logs) in readable form."""
        for _ in range(2):
            LeadFactory(owner=user_a, first_name="Confidential", last_name="Patient")
        body = user_a_client.get(LEADS, {"ordering": "name", "page_size": 1}).json()
        payload = cursor_payload(body["next"])
        assert "Confidential" not in json.dumps(payload)
        assert payload["v"][0] is None  # the private key; only the id is carried
        second = user_a_client.get(body["next"]).json()
        assert len(second["results"]) == 1

    def test_assignee_cursors_never_carry_names(self, admin_client):
        for _ in range(3):
            UserFactory(first_name="Secret", last_name="Person")
        body = admin_client.get("/api/v1/assignees", {"page_size": 1}).json()
        assert "Secret" not in json.dumps(cursor_payload(body["next"]))

    @pytest.mark.parametrize(
        "name",
        [
            ("अपोलो डायग्नोस्टिक्स " * 20)[:200],  # Devanagari
            (chr(0x20000) * 200),  # CJK Extension B (astral plane)
        ],
        ids=["devanagari", "astral"],
    )
    def test_long_names_in_any_script_page_through_the_servers_own_links(
        self, user_a_client, user_a, name
    ):
        """S-F6 / D-F1: such names made cursors longer than the server accepts."""
        leads = {
            LeadFactory(owner=user_a, first_name="", last_name="", organization_name=name).pk
            for _ in range(4)
        }
        seen, link = [], None
        body = user_a_client.get(LEADS, {"ordering": "name", "page_size": 1}).json()
        while True:
            seen.extend(row["id"] for row in body["results"])
            link = body["next"]
            if not link:
                break
            assert len(parse_qs(urlsplit(link).query)["cursor"][0]) < 300
            response = user_a_client.get(link)
            assert response.status_code == 200, response.content
            body = response.json()
        assert {str(pk) for pk in leads} == set(seen)
        assert len(seen) == 4

    def test_a_renamed_boundary_row_continues_from_its_new_name(self, user_a_client, user_a):
        names = ["Anu", "Bala", "Chitra", "Deepa"]
        leads = {n: LeadFactory(owner=user_a, first_name=n, last_name="") for n in names}
        first = user_a_client.get(LEADS, {"ordering": "name", "page_size": 2}).json()
        assert [r["first_name"] for r in first["results"]] == ["Anu", "Bala"]
        Lead.objects.filter(pk=leads["Bala"].pk).update(first_name="Chaya")  # boundary renamed
        second = user_a_client.get(first["next"]).json()
        assert [r["first_name"] for r in second["results"]] == ["Chitra", "Deepa"]


class TestSearch:
    @pytest.mark.parametrize(
        ("q", "organization"),
        [("1-800", "1-800 Flowers"), ("2024-25", "Expo 2024-25"), ("3.14", "Pi 3.14 Labs")],
    )
    def test_phone_like_words_also_match_names_as_typed(
        self, user_a_client, user_a, q, organization
    ):
        """D-F7: phone-like terms were reduced to digits and missed names containing them."""
        lead = LeadFactory(owner=user_a, organization_name=organization)
        rows = user_a_client.get(LEADS, {"q": q}).json()["results"]
        assert [r["id"] for r in rows] == [str(lead.pk)]

    def test_phone_digits_still_match_formatted_numbers(self, user_a_client, user_a):
        lead = LeadFactory(owner=user_a, phone="+91 98765 43210")
        rows = user_a_client.get(LEADS, {"q": "98765-43210"}).json()["results"]
        assert [r["id"] for r in rows] == [str(lead.pk)]


class TestDuplicates:
    def test_email_case_folding_matches_postgresql(self, user_a):
        """D-F6: Python lower() and PostgreSQL lower() disagree on 'İ'; the same address
        typed twice didn't match itself."""
        lead = LeadFactory(owner=user_a, email="ali@İzmir.com.tr")
        found = selectors.possible_duplicates(AccessScope.own(user_a.pk), email="ali@İzmir.com.tr")
        assert [(d.pk, matched) for d, matched in found] == [(lead.pk, ["email"])]

    def test_invisible_characters_in_a_duplicate_check_match_nothing(self, user_a):
        LeadFactory(owner=user_a, email="a@b.example")
        assert (
            selectors.possible_duplicates(
                AccessScope.own(user_a.pk), email="a@b.example" + chr(0x202E)
            )
            == []
        )

    def test_many_matches_stay_bounded(self, user_a):
        """D-F10 (documented): with more than 50 matches, 5 recent ones are shown."""
        for _ in range(60):
            LeadFactory(owner=user_a, phone="+91 22 2345 6789")
        found = selectors.possible_duplicates(
            AccessScope.own(user_a.pk), phones=["+91 22 2345 6789"]
        )
        assert len(found) == 5


class TestTextAndPhones:
    @pytest.mark.parametrize(
        "name",
        [chr(0x200B), "Ra" + chr(0x200B) + "hul", "Rahul" + chr(0xE0041), "A" + chr(0x00AD) + "B"],
    )
    def test_invisible_characters_are_refused_in_names(self, user_a_client, name):
        """S-info / D-F8: zero-width spaces, soft hyphens and prompt-smuggling tag characters."""
        response = user_a_client.post(LEADS, {"first_name": name}, format="json")
        assert response.status_code == 400

    @pytest.mark.parametrize("phone", ["+91 98765 43210 ext ١٢", "+91 98765 43210 x १२"])
    def test_extensions_take_ascii_digits_only(self, phone):
        """D-F9: a Unicode-aware digit class accepted Arabic-Indic and Devanagari digits."""
        with pytest.raises(InvalidInputError) as caught:
            clean_fields({"phone": phone})
        assert set(caught.value.details) == {"phone"}

    def test_uk_trunk_zero_matches_the_international_form(self):
        assert phone_key("+44 (0)20 7946 0958") == phone_key("+44 20 7946 0958") == "+442079460958"

    def test_full_width_digits_are_converted_and_long_numbers_fit(self):
        assert clean_fields({"phone": "０３-１２３４-５６７８"}) == {"phone": "03-1234-5678"}  # noqa: RUF001 — full-width digits are the point
        assert clean_fields({"phone": "+1 (555) 010-9999 extension 12345"})


class TestPhoneKeysStayInSync:
    """D-F12: bulk ORM writes could leave phone_keys stale (duplicate checks then missed)."""

    def test_bulk_updates_of_numbers_are_refused(self, user_a):
        lead = LeadFactory(owner=user_a, phone="+91 98765 43210")
        with pytest.raises(ValueError, match="phone_keys"):
            Lead.objects.filter(pk=lead.pk).update(phone="+44 20 7946 0958")
        lead.phone = "+44 20 7946 0958"
        with pytest.raises(ValueError, match="phone_keys"):
            Lead.objects.bulk_update([lead], ["phone"])

    def test_bulk_create_computes_the_keys(self, user_a):
        Lead.objects.bulk_create(
            [
                Lead(
                    first_name="B",
                    phone="+91 98765 43210",
                    owner=user_a,
                    created_by=user_a,
                    status_id="new",
                )
            ]
        )
        assert Lead.objects.get(first_name="B").phone_keys == ["+919876543210"]

    def test_save_accepts_any_iterable_of_update_fields(self, user_a):
        lead = LeadFactory(owner=user_a)
        lead.job_title = "Director"
        lead.save(update_fields=(f for f in ["job_title"]))
        lead.refresh_from_db()
        assert lead.job_title == "Director"


class TestIdempotency:
    def test_a_replay_after_the_lead_moved_away_explains_itself(self, user_a, user_b, admin):
        """D-F11: the retry of a create that succeeded answered 404."""
        own = AccessScope.own(user_a.pk)
        key = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b"
        created = services.create_lead(
            actor=user_a, scope=own, fields={"first_name": "R"}, idempotency_key=key
        )
        services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=created.lead.pk,
            version=1,
            owner_id=user_b.pk,
        )
        with pytest.raises(ConflictError, match="already created"):
            services.create_lead(
                actor=user_a, scope=own, fields={"first_name": "R"}, idempotency_key=key
            )

    def test_expired_records_are_purged_for_everyone(self):
        """D-F11: records were only purged on the same actor's next keyed create."""
        from arkray.core.tasks import housekeeping

        idempotency.remember(UserFactory().pk, "op", uuid.uuid4(), "a" * 64, uuid.uuid4())
        IdempotencyRecord.objects.update(created_at=timezone.now() - timedelta(hours=25))
        assert housekeeping()["idempotency_records"] == 1
        assert not IdempotencyRecord.objects.exists()


def test_an_admin_handing_over_their_own_lead_shows_in_the_new_owners_audit_trail(admin, user_b):
    lead = LeadFactory(owner=admin)
    services.reassign_lead(
        actor=admin, scope=AccessScope.own(admin.pk), lead_id=lead.pk, version=1, owner_id=user_b.pk
    )
    assert AuditEvent.objects.get(action="lead.reassigned").subject_user_id == user_b.pk


def test_another_admin_can_page_a_users_long_named_leads(user_a):
    """S-F6: one user's long lead names blocked name-sorted paging for admins too."""
    for _ in range(3):
        LeadFactory(
            owner=user_a, first_name="", last_name="", organization_name=("அப்போலோ " * 30)[:200]
        )
    client = signed_in(AdminFactory())
    body = client.get(
        f"/api/v1/workspaces/{user_a.pk}/leads", {"ordering": "name", "page_size": 1}
    ).json()
    assert client.get(body["next"]).status_code == 200
