"""Pseudonymising a former staff member (privacy remediation P2-7; privacy.staff;
docs/privacy.md#staff). Not lead erasure: the user's id keeps attributing their CRM history,
and financial and negotiated-price history stays exactly as it was.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from arkray.ai.models import Conversation
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail, AuditEvent
from arkray.core.access import AccessScope
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.errors import BusinessRuleViolation
from arkray.core.holds import UnderLegalHold
from arkray.core.models import HoldSubject, LegalHold, OutboxEvent
from arkray.identity import services as identity_services
from arkray.identity import throttling
from arkray.identity.models import AccountToken, SupportSession, TokenPurpose, User
from arkray.pipeline.models import NegotiationPrice, Opportunity
from arkray.privacy import staff
from tests.factories import LeadFactory, OpportunityFactory, UserFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def former(admin):
    user = UserFactory(first_name="Kiran", last_name="Desai", email="kiran.desai@arkray.example")
    lead = LeadFactory(owner=user)
    deal = OpportunityFactory(lead=lead, stage_key="won", value=Decimal("1500000"))
    NegotiationPrice.objects.create(
        opportunity=deal,
        price=Decimal("1400000"),
        currency="INR",
        stage=deal.stage,
        stage_name="Negotiation",
        source="stage_entry",
        opportunity_version=deal.version,
        actor=user,
    )
    lead.archived_at = timezone.now()
    lead.save()
    Conversation.objects.create(actor=user, subject=user, workspace_kind="self")
    token = bind_context(ExecutionContext(correlation_id="r", client_ip="198.51.100.4"))
    try:
        audit.record("auth.login", actor_id=user.pk, target_type="user", target_id=user.pk)
    finally:
        reset_context(token)
    identity_services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
    user.refresh_from_db()
    return {"user": user, "deal": deal}


def test_a_dry_run_counts_and_changes_nothing(admin, former):
    out = StringIO()
    call_command("pseudonymise_user", str(former["user"].pk), by=admin.email, stdout=out)
    assert "Dry run" in out.getvalue()
    assert User.objects.get(pk=former["user"].pk).first_name == "Kiran"


def test_identity_goes_attribution_and_history_stay(admin, former):
    user = former["user"]
    call_command("pseudonymise_user", str(user.pk), by=admin.email, yes=True, stdout=StringIO())
    user.refresh_from_db()
    assert user.first_name == "Former user"
    assert user.last_name == user.pk.hex[:8]
    assert user.email == f"former-{user.pk.hex}@pseudonymised.invalid"
    assert not user.has_usable_password()
    assert user.last_login is None
    deal = Opportunity.objects.get(pk=former["deal"].pk)
    assert deal.owner_id == user.pk  # still attributed
    assert deal.value == Decimal("1500000.00")  # financial history untouched
    price = NegotiationPrice.objects.get(opportunity=deal)
    assert (price.actor_id, price.price) == (user.pk, Decimal("1400000.00"))
    assert not Conversation.objects.filter(actor=user).exists()  # their own Q&A
    assert not AuditDetail.objects.filter(event__actor_id=user.pk).exists()  # their IPs
    assert AuditEvent.objects.filter(actor_id=user.pk, action="auth.login").exists()  # the event
    event = AuditEvent.objects.get(action="user.pseudonymised")
    assert "kiran" not in json.dumps(event.metadata).lower()
    # Admin screens show the pseudonym.
    page = signed_in(admin).get(f"/api/v1/admin/users/{user.pk}").json()
    assert page["first_name"] == "Former user"
    assert "kiran" not in json.dumps(page).lower()


def test_sessions_tokens_support_and_queued_mail_end(admin, former, user_a):
    user = former["user"]
    AccountToken.objects.create(
        user=user,
        purpose=TokenPurpose.PASSWORD_RESET,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    OutboxEvent.objects.create(
        topic="identity.password_reset_requested", payload={"email": user.email}, queue="email"
    )
    attempt = throttling.LoginThrottle(throttling.login_identifiers(user.email), "", "198.51.100.4")
    assert attempt.reserve() is None  # a failed sign-in's evidence is its reservation
    attempt.failed()
    result = staff.pseudonymise(user.pk, operator_id=admin.pk)
    assert result.tokens_revoked == 1
    assert result.queued_payloads == 1
    assert result.throttle_events >= 1
    assert not AccountToken.objects.filter(user=user, status="pending").exists()
    queued = OutboxEvent.objects.get(topic="identity.password_reset_requested")
    assert queued.payload == {}
    assert queued.status == "done"  # nothing left to send, never a dead event (review P3)


def test_support_reasons_naming_them_are_blanked(admin, former, user_a):
    user = former["user"]
    SupportSession.objects.create(
        admin=admin,
        target=user,
        reason="Kiran asked for help",
        session_digest="a" * 64,
        started_at=timezone.now() - timedelta(days=2),
        expires_at=timezone.now() - timedelta(days=1),
        ended_at=timezone.now() - timedelta(days=1),
        end_reason="exited",
    )
    staff.pseudonymise(user.pk, operator_id=admin.pk)
    assert SupportSession.objects.get(target=user).reason == ""


@pytest.mark.parametrize("problem", ["active", "owns_work", "self", "held"])
def test_refused_when_it_isnt_safe(admin, former, problem):
    user = former["user"]
    if problem == "active":
        identity_services.reactivate_user(actor_id=admin.pk, user_id=user.pk)
        expected = BusinessRuleViolation
    elif problem == "owns_work":
        LeadFactory(owner=user)
        expected = BusinessRuleViolation
    elif problem == "self":
        with pytest.raises(BusinessRuleViolation):
            staff.pseudonymise(admin.pk, operator_id=admin.pk)
        return
    else:
        LegalHold.objects.create(
            subject_type=HoldSubject.USER, subject_id=user.pk, reference="HR-1", placed_by=admin.pk
        )
        expected = UnderLegalHold
    with pytest.raises(expected):
        staff.pseudonymise(user.pk, operator_id=admin.pk)
    assert User.objects.get(pk=user.pk).first_name == "Kiran"


def test_idempotent_and_the_former_user_cannot_sign_in(admin, former, api_client):
    user = former["user"]
    staff.pseudonymise(user.pk, operator_id=admin.pk)
    with pytest.raises(staff.AlreadyPseudonymised):
        staff.pseudonymise(user.pk, operator_id=admin.pk)
    with pytest.raises(CommandError):
        call_command("pseudonymise_user", str(user.pk), by=admin.email, yes=True)
    for email in ("kiran.desai@arkray.example", user.email):
        response = api_client.post(
            "/api/v1/auth/login",
            {"email": email, "password": "anything-long-enough"},
            format="json",
        )
        assert response.status_code in (400, 401, 429)


def test_a_sales_user_cannot_pseudonymise(user_a, former):
    with pytest.raises(CommandError):
        call_command("pseudonymise_user", str(former["user"].pk), by=user_a.email, yes=True)


def test_it_is_not_lead_erasure(admin, former):
    """The leads the former user owned are untouched: erasing a customer is erase_lead."""
    lead_ids = list(
        Opportunity.objects.filter(owner=former["user"]).values_list("lead_id", flat=True)
    )
    staff.pseudonymise(former["user"].pk, operator_id=admin.pk)
    from arkray.leads.models import Lead

    assert all(Lead.objects.get(pk=pk).first_name != "[erased]" for pk in lead_ids)
    assert AccessScope.organization(admin.pk)


def test_the_api_answers_409_under_a_hold_and_503_without_a_ledger(
    admin, user_b, settings, tmp_path
):
    """Backend review P3: both came back as 500."""
    from arkray.core.ledger_gate import GATE
    from arkray.core.models import HoldSubject, LegalHold
    from arkray.identity import services as identity_services

    identity_services.deactivate_user(actor_id=admin.pk, user_id=user_b.pk)
    client = signed_in(admin)
    url = f"/api/v1/admin/privacy/users/{user_b.pk}/pseudonymise"
    hold = LegalHold.objects.create(
        subject_type=HoldSubject.USER,
        subject_id=user_b.pk,
        reference="MATTER-8",
        placed_by=admin.pk,
    )
    held = client.post(url, {"confirm_email": user_b.email}, format="json")
    assert (held.status_code, held.json()["error"]["code"]) == (409, "legal_hold")
    LegalHold.objects.filter(pk=hold.pk).update(released_at=hold.placed_at, released_by=admin.pk)

    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = "ledger-test-key-" + "x" * 32
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    assert GATE.status() == "ok"  # the gate verified it; now the store goes away
    settings.ERASURE_LEDGER_MAX_STALE_S = 3600
    directory.rmdir()
    try:
        down = client.post(url, {"confirm_email": user_b.email}, format="json")
    finally:
        GATE.reset()
    assert (down.status_code, down.json()["error"]["code"]) == (503, "erasure_ledger_unavailable")
    user_b.refresh_from_db()
    assert user_b.email
    assert not user_b.email.startswith("former-")
