"""Phase 9 review regressions: each test reproduces a finding of the Phase 9 security review
(whole application) and pins its fix."""

from __future__ import annotations

import json
from typing import Any

import pytest

from arkray.identity import authentication, services
from arkray.identity.services import SESSION_ENDED
from tests.factories import DEFAULT_PASSWORD
from tests.helpers import drain_outbox, last_secret, signed_in

pytestmark = pytest.mark.django_db

ME = "/api/v1/auth/me"
RECOVERED = "owner-recovered-passphrase-77"
ATTACKERS = "attacker-chosen-passphrase-99"


def reset_link_for(user: Any) -> str:
    services.issue_password_reset(user.email)
    drain_outbox()
    return last_secret("reset-password")


def reset_while_verifying(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    """The owner confirms a reset link right after this request's password check passed
    (the window is one password hash, ~100 ms in production)."""
    verify = authentication.verify_current_password

    def verify_then_reset(request: Any, password: str) -> None:
        verify(request, password)
        services.confirm_password_reset(secret, RECOVERED)

    monkeypatch.setattr(authentication, "verify_current_password", verify_then_reset)


class TestAChangeInFlightCannotUndoAReset:
    """P1: a password (or own email) change from a session a reset just ended set the
    attacker's password and kept the attacker signed in; the reset silently lost."""

    def test_password_change(self, user_a, monkeypatch):
        attacker = signed_in(user_a)
        reset_while_verifying(monkeypatch, reset_link_for(user_a))
        response = attacker.post(
            "/api/v1/auth/password/change",
            {"current_password": DEFAULT_PASSWORD, "new_password": ATTACKERS},
            format="json",
        )
        assert response.status_code == 403
        assert response.json()["error"]["message"] == SESSION_ENDED
        user_a.refresh_from_db()
        assert user_a.check_password(RECOVERED)
        assert not user_a.check_password(ATTACKERS)
        assert attacker.get(ME).status_code == 401

    def test_own_email_change(self, admin, monkeypatch):
        attacker = signed_in(admin)
        reset_while_verifying(monkeypatch, reset_link_for(admin))
        original_email = admin.email
        response = attacker.post(
            f"/api/v1/admin/users/{admin.pk}/change-email",
            {
                "email": "attacker@example.com",
                "version": admin.version,
                "current_password": DEFAULT_PASSWORD,
            },
            format="json",
        )
        assert response.status_code == 403
        assert response.json()["error"]["message"] == SESSION_ENDED
        admin.refresh_from_db()
        assert admin.email == original_email
        assert admin.check_password(RECOVERED)
        assert attacker.get(ME).status_code == 401

    def test_an_ordinary_change_still_keeps_this_session(self, user_a):
        client = signed_in(user_a)
        response = client.post(
            "/api/v1/auth/password/change",
            {"current_password": DEFAULT_PASSWORD, "new_password": ATTACKERS},
            format="json",
        )
        assert response.status_code == 204
        assert client.get(ME).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_visits_write_one_audit_row(admin, user_a):
    """Phase 9 review (and R58): with the window in PostgreSQL, requests opening it at once
    write exactly one `workspace.accessed` row (the cache let up to three through)."""
    from arkray.audit.models import AuditEvent
    from arkray.identity.workspaces import resolve_workspace
    from tests.helpers import run_concurrently

    def visit() -> None:
        resolve_workspace(admin, str(user_a.pk))

    run_concurrently(*(visit for _ in range(6)))
    rows = AuditEvent.objects.filter(action="workspace.accessed", subject_user_id=user_a.pk)
    assert rows.count() == 1


class TestUserNamesFollowTheTextRules:
    """Phase 9 review: user names skipped the rules every CRM field follows; a right-to-left
    override spoofed names in pickers and banners, and line breaks reached email bodies."""

    URL = "/api/v1/admin/users"

    def create(self, admin: Any, first_name: str) -> Any:
        return signed_in(admin).post(
            self.URL,
            {
                "email": "new.person@arkray.test",
                "first_name": first_name,
                "last_name": "Sharma",
                "role": "sales_user",
            },
            format="json",
        )

    @pytest.mark.parametrize(
        "name",
        [
            "Rahul\u202eamrahS",  # right-to-left override
            "Ra\u200bhul",  # zero-width space
            "Rahul\x1b[31m",  # terminal escape
            "Rahul\U000e0041",  # tag character
        ],
    )
    def test_invisible_and_control_characters_are_refused(self, admin, name):
        response = self.create(admin, name)
        assert response.status_code == 400
        assert "first_name" in response.json()["error"]["details"]

    def test_line_breaks_cannot_add_lines_to_emails(self, admin):
        response = self.create(admin, "Rahul\r\nBcc: spy@evil.example")
        assert response.status_code == 201
        assert response.json()["first_name"] == "Rahul Bcc: spy@evil.example"

    @pytest.mark.parametrize("name", ["राहुल", "Zoë", "O'Brien", "Anne-Marie", "क्षत्रिय"])
    def test_real_names_are_kept(self, admin, name):
        response = self.create(admin, name)
        assert response.status_code == 201, response.content
        assert response.json()["first_name"] == name

    def test_renames_follow_the_same_rules(self, admin, user_a):
        client = signed_in(admin)
        response = client.patch(
            f"{self.URL}/{user_a.pk}",
            {"version": user_a.version, "last_name": "Sharma\u202e"},
            format="json",
        )
        assert response.status_code == 400
        assert "last_name" in response.json()["error"]["details"]


class TestAuditMetadata:
    """Phase 9 review: sanitize_metadata missed secret keys and inline secrets, let a long
    key bypass the size cap, crashed on deep nesting, NaN and NUL, and kept ANSI and bidi
    characters. (No current caller passes user-controlled keys; this is defence in depth.)"""

    def test_secret_keys_and_values_are_redacted(self):
        from arkray.audit.services import REDACTED, sanitize_metadata

        cleaned = sanitize_metadata(
            {
                "private_key": "k",
                "pwd": "p",
                "otp": "1",
                "bearer": "b",
                "auth": "a",
                "dsn": "d",
                "signature": "s",
                "note": "password=hunter2 then token: abc123",
                "author": "kept",
            }
        )
        for key in ("private_key", "pwd", "otp", "bearer", "auth", "dsn", "signature"):
            assert cleaned[key] == REDACTED, key
        assert "hunter2" not in cleaned["note"]
        assert "abc123" not in cleaned["note"]
        assert cleaned["author"] == "kept"

    def test_size_depth_and_odd_values_are_bounded(self):
        from arkray.audit.services import MAX_METADATA_BYTES, sanitize_metadata

        long_key = sanitize_metadata({"k" * 20_000: 1})
        assert len(json.dumps(long_key).encode()) <= MAX_METADATA_BYTES
        deep: dict[str, Any] = {}
        node = deep
        for _ in range(2_000):
            node["x"] = {}
            node = node["x"]
        assert len(json.dumps(sanitize_metadata(deep)).encode()) <= MAX_METADATA_BYTES
        odd = sanitize_metadata({"nan": float("nan"), "text": "a\x00b\x1b[31mc\u202ed"})
        assert odd["nan"] is None
        assert odd["text"] == "ab[31mcd"

    def test_odd_values_no_longer_abort_the_change(self, user_a):
        from arkray.audit import services as audit
        from arkray.audit.models import AuditEvent

        audit.record(
            "test.metadata",
            actor_id=user_a.pk,
            metadata={"nan": float("inf"), "text": "a\x00b"},
        )
        assert AuditEvent.objects.get(action="test.metadata").metadata == {
            "nan": None,
            "text": "ab",
        }


def test_append_only_rows_have_opaque_ids_and_sealed_cursors(user_a):
    """R48 (Phase 4, fixed in Phase 9): timeline and stage-history rows showed ids from one
    global sequence, in responses and readable cursors, so the gap between two of one's own
    entries counted every event written anywhere meanwhile."""
    import base64
    from decimal import Decimal
    from urllib.parse import parse_qs, urlparse

    from arkray.core.access import AccessScope
    from arkray.pipeline import services as pipeline_services
    from tests.factories import LeadFactory, default_stage

    scope = AccessScope.own(user_a.pk)
    lead = LeadFactory(owner=user_a)
    opportunity = pipeline_services.create_opportunity(
        actor=user_a,
        scope=scope,
        lead_id=lead.pk,
        fields={"value": Decimal("1000")},
    ).opportunity
    pipeline_services.move_opportunity(
        actor=user_a,
        scope=scope,
        opportunity_id=opportunity.pk,
        version=opportunity.version,
        stage_id=default_stage("proposal").pk,
    )
    client = signed_in(user_a)
    base = f"/api/v1/workspaces/me/opportunities/{opportunity.pk}"
    for path in ("history", "timeline"):
        first = client.get(f"{base}/{path}", {"page_size": "1"}).json()
        again = client.get(f"{base}/{path}", {"page_size": "1"}).json()
        ids = [row["id"] for row in first["results"]]
        assert all(isinstance(i, str) and len(i) == 20 and not i.isdigit() for i in ids)
        assert ids == [row["id"] for row in again["results"]]  # stable keys for the UI
        cursor = parse_qs(urlparse(first["next"]).query)["cursor"][0]
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        assert b"occurred_at" not in raw
        assert b'"v"' not in raw
