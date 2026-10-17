"""Deleting an account from user administration (ADR-0036; privacy.account_deletion).

"Delete" hands the account's work to someone else, deactivates it and erases its identity; the
CRM records, their attribution and the audit trail stay. A Super admin deletes Admins and
Users, an Admin only the users of their own team; nobody deletes themselves or a Super admin.
Every refusal is decided on the server (the tests call the API), changes nothing, and is the
same 404 for what a caller can't reach as for what doesn't exist.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.db import transaction
from django.utils import timezone

from arkray.activities.models import Activity
from arkray.ai.models import Conversation
from arkray.audit.models import AuditEvent
from arkray.core import ledger
from arkray.core.errors import BusinessRuleViolation
from arkray.core.ledger_gate import GATE
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity import mfa_services
from arkray.identity import services as identity_services
from arkray.identity.models import (
    AccountToken,
    Role,
    SupportSession,
    TokenPurpose,
    User,
    UserStatus,
)
from arkray.leads.models import Lead
from arkray.pipeline.models import Opportunity, Pipeline, Stage
from arkray.privacy import account_deletion
from tests.factories import (
    AdminFactory,
    LeadFactory,
    MeetingFactory,
    OpportunityFactory,
    TaskFactory,
    TeamAdminFactory,
    UserFactory,
)
from tests.helpers import signed_in
from tests.mfa_helpers import enrol

pytestmark = pytest.mark.django_db

KEY = "ledger-test-key-" + "x" * 32


def delete(client, target: User, **body: Any):
    payload = {"version": target.version, "confirm_email": target.email, **body}
    return client.post(f"/api/v1/admin/users/{target.pk}/delete", payload, format="json")


def preview(client, target: User):
    return client.get(f"/api/v1/admin/users/{target.pk}/deletion-preview")


def code(response) -> str:
    return str(response.json()["error"]["code"])


@pytest.fixture
def ledger_dir(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    yield directory
    GATE.reset()


@pytest.fixture
def team(db):
    boss = TeamAdminFactory(first_name="Tara", last_name="Boss")
    return boss, UserFactory(first_name="Mia", last_name="Member", manager=boss)


def personal_pipeline(owner: User, name: str) -> Pipeline:
    pipeline = Pipeline.objects.create(key=f"p{uuid4().hex[:12]}", name=name, owner=owner)
    Stage.objects.create(
        pipeline=pipeline, key="first", name="First", position=10, probability=10, category="open"
    )
    return pipeline


def snapshot() -> dict[str, int]:
    """Row counts that deletion must never reduce: the CRM and the audit trail."""
    return {
        "leads": Lead.objects.count(),
        "opportunities": Opportunity.objects.count(),
        "activities": Activity.objects.count(),
        "audit": AuditEvent.objects.count(),
        "users": User.objects.count(),
    }


# --- who may delete whom ------------------------------------------------------------------------
def test_a_super_admin_deletes_a_user(admin, user_a):
    victim = signed_in(user_a)
    assert victim.get("/api/v1/auth/me").status_code == 200
    before = snapshot()
    response = delete(signed_in(admin), user_a)
    assert (response.status_code, response.json()["completed"]) == (200, True)
    user_a.refresh_from_db()
    assert user_a.status == UserStatus.DEACTIVATED
    assert not user_a.is_active
    assert user_a.pseudonymised_at is not None
    assert user_a.first_name == "Former user"
    assert user_a.email.endswith("@pseudonymised.invalid")
    assert not user_a.has_usable_password()
    assert victim.get("/api/v1/auth/me").status_code == 401  # every session ended
    after = snapshot()
    assert after["users"] == before["users"]  # never a row delete
    assert after["audit"] > before["audit"]


def test_a_super_admin_deletes_an_admin_and_releases_the_team(admin, team):
    boss, member = team
    other_admin = TeamAdminFactory()
    pending = AccountToken.objects.create(
        user=boss,
        purpose=TokenPurpose.PASSWORD_RESET,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    response = delete(signed_in(admin), boss)
    assert response.status_code == 200
    boss.refresh_from_db()
    assert boss.pseudonymised_at is not None
    assert boss.status == UserStatus.DEACTIVATED
    member.refresh_from_db()
    assert member.manager_id is None  # answers to the super admins until reassigned
    assert member.status == UserStatus.ACTIVE  # still works
    pending.refresh_from_db()
    assert pending.status == "revoked"
    other_admin.refresh_from_db()
    assert other_admin.status == UserStatus.ACTIVE  # nobody else touched
    deactivated = AuditEvent.objects.get(action="user.deactivated", target_id=str(boss.pk))
    assert deactivated.metadata["reason"] == "deleted"
    assert deactivated.metadata["unassigned_members"] == 1


def test_an_admin_deletes_a_user_of_their_own_team(team):
    boss, member = team
    response = delete(signed_in(boss), member)
    assert response.status_code == 200
    member.refresh_from_db()
    assert member.pseudonymised_at is not None
    boss.refresh_from_db()
    assert boss.pseudonymised_at is None
    assert boss.status == UserStatus.ACTIVE


def test_an_admin_cannot_delete_another_admin_a_super_admin_or_themselves(admin, team):
    boss, _ = team
    rival = TeamAdminFactory()
    client = signed_in(boss)
    before = snapshot()
    assert delete(client, rival).status_code == 404  # out of reach: indistinguishable from none
    assert delete(client, admin).status_code == 404
    assert (delete(client, boss).status_code, code(delete(client, boss))) == (
        422,
        "business_rule_violation",
    )
    for user in (rival, admin, boss):
        user.refresh_from_db()
        assert user.pseudonymised_at is None
        assert user.status == UserStatus.ACTIVE
    assert snapshot() == before


def test_an_admin_cannot_delete_the_user_of_another_admin_or_an_unassigned_one(team):
    boss, _ = team
    other_team = UserFactory(manager=TeamAdminFactory())
    unassigned = UserFactory()
    client = signed_in(boss)
    for target in (other_team, unassigned):
        response = delete(client, target)
        assert response.status_code == 404
        assert preview(client, target).status_code == 404
        target.refresh_from_db()
        assert target.pseudonymised_at is None
        assert target.status == UserStatus.ACTIVE
    missing = client.post(
        "/api/v1/admin/users/00000000-0000-4000-8000-000000000001/delete",
        {"version": 1, "confirm_email": "x@example.test"},
        format="json",
    )
    assert missing.status_code == 404  # the same answer


def test_nobody_deletes_a_super_admin_not_even_a_super_admin(admin):
    peer = AdminFactory()
    client = signed_in(admin)
    response = delete(client, peer)
    assert (response.status_code, code(response)) == (403, "permission_denied")
    self_delete = delete(client, admin)
    assert (self_delete.status_code, code(self_delete)) == (422, "business_rule_violation")
    for user in (peer, admin):
        user.refresh_from_db()
        assert user.pseudonymised_at is None
        assert user.status == UserStatus.ACTIVE


def test_the_last_active_super_admin_is_never_removed(admin):
    """Defence in depth: the role rule already refuses every Super admin, and the removal
    itself still refuses to take the last active one."""
    with transaction.atomic():
        identity_services.serialise_user_administration()
        locked = User.objects.select_for_update().get(pk=admin.pk)
        with pytest.raises(BusinessRuleViolation, match="at least one active super admin"):
            identity_services.deactivate_for_removal(admin, locked, now=timezone.now())
    admin.refresh_from_db()
    assert admin.status == UserStatus.ACTIVE


def test_an_ordinary_user_and_anonymous_callers_are_refused(user_a, user_b, api_client):
    target = f"/api/v1/admin/users/{user_b.pk}"
    assert api_client.post(f"{target}/delete", {}, format="json").status_code == 401
    assert api_client.get(f"{target}/deletion-preview").status_code == 401
    client = signed_in(user_a)
    assert delete(client, user_b).status_code == 403
    assert preview(client, user_b).status_code == 403
    user_b.refresh_from_db()
    assert user_b.pseudonymised_at is None


def test_roles_and_teams_cannot_be_smuggled_into_the_request(admin, user_a):
    client = signed_in(admin)
    response = client.post(
        f"/api/v1/admin/users/{user_a.pk}/delete",
        {
            "version": user_a.version,
            "confirm_email": user_a.email,
            "role": Role.ADMIN,
            "is_staff": True,
            "manager": str(admin.pk),
        },
        format="json",
    )
    assert response.status_code == 400
    user_a.refresh_from_db()
    assert user_a.pseudonymised_at is None
    assert user_a.role == Role.SALES_USER


# --- confirmation, versions, MFA ------------------------------------------------------------
def test_the_account_is_only_deleted_when_its_email_is_typed_and_its_version_current(admin, user_a):
    client = signed_in(admin)
    wrong = delete(client, user_a, confirm_email="someone.else@example.test")
    assert (wrong.status_code, code(wrong)) == (422, "business_rule_violation")
    stale = delete(client, user_a, version=user_a.version + 5)
    assert (stale.status_code, code(stale)) == (409, "conflict")
    assert (
        client.post(
            f"/api/v1/admin/users/{user_a.pk}/delete", {"version": user_a.version}, format="json"
        ).status_code
        == 400
    )  # confirm_email is required
    user_a.refresh_from_db()
    assert user_a.pseudonymised_at is None
    # The typed email is case- and space-insensitive like every email in the product.
    again = delete(client, user_a, confirm_email=f"  {user_a.email.upper()} ")
    assert again.status_code == 200


def test_a_super_admin_needs_a_recent_second_factor(admin, user_a, settings):
    settings.MFA_REQUIRED_FOR_ADMINS = True
    enrol(admin)
    client = signed_in(admin)
    refused = delete(client, user_a)
    assert (refused.status_code, code(refused)) == (403, "mfa_required")
    user_a.refresh_from_db()
    assert user_a.pseudonymised_at is None
    session = client.session
    mfa_services.mark_verified(session)
    session.save()
    assert delete(client, user_a).status_code == 200


def test_deleting_twice_is_refused_the_second_time(admin, user_a):
    client = signed_in(admin)
    assert delete(client, user_a).status_code == 200
    user_a.refresh_from_db()
    again = delete(client, user_a)
    assert (again.status_code, code(again)) == (422, "account_pseudonymised")


# --- the work is handed over ----------------------------------------------------------------
@pytest.fixture
def busy(admin):
    """A user with a lead, an open deal, a task, a meeting, a closed deal and a closed lead."""
    owner = UserFactory(first_name="Busy", last_name="Owner")
    lead = LeadFactory(owner=owner)
    deal = OpportunityFactory(lead=lead, stage_key="new")
    task = TaskFactory(lead=lead, owner=owner, created_by=owner)
    meeting = MeetingFactory(lead=lead, owner=owner, created_by=owner)
    closed_lead = LeadFactory(owner=owner)
    won = OpportunityFactory(lead=closed_lead, stage_key="won")
    successor = UserFactory(first_name="Next", last_name="Owner")
    return {
        "owner": owner,
        "lead": lead,
        "deal": deal,
        "task": task,
        "meeting": meeting,
        "won": won,
        "closed_lead": closed_lead,
        "successor": successor,
    }


def test_a_user_with_work_needs_someone_to_take_it_over(admin, busy):
    client = signed_in(admin)
    owner = busy["owner"]
    gone = UserFactory(is_active=False)
    before = snapshot()
    missing = delete(client, owner)
    assert (missing.status_code, code(missing)) == (400, "validation_error")
    assert "reassign_to" in missing.json()["error"]["details"]
    for refused in (owner.pk, "00000000-0000-4000-8000-000000000009"):
        response = delete(client, owner, reassign_to=str(refused))
        assert response.status_code == 400
    assert delete(client, owner, reassign_to=str(gone.pk)).status_code == 400  # not active
    owner.refresh_from_db()
    assert owner.pseudonymised_at is None
    assert Lead.objects.get(pk=busy["lead"].pk).owner_id == owner.pk
    assert snapshot() == before


def test_the_work_moves_the_history_stays_and_every_move_is_audited(admin, busy):
    client = signed_in(admin)
    owner, successor = busy["owner"], busy["successor"]
    before = snapshot()
    response = delete(client, owner, reassign_to=str(successor.pk))
    assert response.status_code == 200
    assert response.json() == {"completed": True, "reassigned_leads": 2, "remaining_leads": 0}
    # Open work follows the lead to the new owner...
    assert Lead.objects.get(pk=busy["lead"].pk).owner_id == successor.pk
    assert Opportunity.objects.get(pk=busy["deal"].pk).owner_id == successor.pk
    assert Activity.objects.get(pk=busy["task"].pk).owner_id == successor.pk
    assert Activity.objects.get(pk=busy["meeting"].pk).owner_id == successor.pk
    # ...closed history keeps the person who did it, and so does every creator.
    assert Opportunity.objects.get(pk=busy["won"].pk).owner_id == owner.pk
    assert Activity.objects.get(pk=busy["task"].pk).created_by_id == owner.pk
    assert Lead.objects.get(pk=busy["lead"].pk).created_by_id == owner.pk
    # Nothing is deleted: counts are unchanged (plus audit rows); ownership constraints hold.
    after = snapshot()
    assert {k: after[k] for k in before if k != "audit"} == {
        k: before[k] for k in before if k != "audit"
    }
    reassigned = AuditEvent.objects.filter(action="lead.reassigned", actor_id=admin.pk)
    assert reassigned.count() == 2
    deleted = AuditEvent.objects.get(action="user.deleted")
    assert (deleted.actor_id, deleted.target_id) == (admin.pk, str(owner.pk))
    assert deleted.metadata["reassigned_to"] == str(successor.pk)
    assert owner.email not in json.dumps(deleted.metadata)
    # The successor sees the deal in their own workspace through the normal API.
    board = signed_in(successor).get("/api/v1/workspaces/me/opportunities")
    assert busy["deal"].title in board.content.decode()


def test_an_admin_hands_work_only_to_themselves_or_their_team(team):
    boss, member = team
    lead = LeadFactory(owner=member)
    OpportunityFactory(lead=lead, stage_key="new")
    outsider = UserFactory(manager=TeamAdminFactory())
    teammate = UserFactory(manager=boss)
    client = signed_in(boss)
    refused = delete(client, member, reassign_to=str(outsider.pk))
    assert (refused.status_code, code(refused)) == (400, "validation_error")
    assert Lead.objects.get(pk=lead.pk).owner_id == member.pk
    assert delete(client, member, reassign_to=str(teammate.pk)).status_code == 200
    assert Lead.objects.get(pk=lead.pk).owner_id == teammate.pk


def test_a_large_account_is_handed_over_in_batches_and_deleted_by_the_last(
    admin, busy, monkeypatch
):
    monkeypatch.setattr(account_deletion, "MAX_LEADS_PER_REQUEST", 2)
    owner, successor = busy["owner"], busy["successor"]
    for _ in range(3):
        LeadFactory(owner=owner)
    client = signed_in(admin)  # 5 unarchived leads in all
    first = delete(client, owner, reassign_to=str(successor.pk))
    assert (first.status_code, first.json()) == (
        202,
        {"completed": False, "reassigned_leads": 2, "remaining_leads": 3},
    )
    owner.refresh_from_db()
    assert owner.pseudonymised_at is None
    assert owner.status == UserStatus.ACTIVE  # still theirs
    second = delete(client, owner, reassign_to=str(successor.pk))
    assert (second.status_code, second.json()["remaining_leads"]) == (202, 1)
    last = delete(client, owner, reassign_to=str(successor.pk))
    assert (last.status_code, last.json()["completed"]) == (200, True)
    owner.refresh_from_db()
    assert owner.pseudonymised_at is not None
    assert not Lead.objects.filter(owner=owner, archived_at__isnull=True).exists()


def test_personal_pipelines_are_archived_unless_they_still_hold_a_deal(admin, busy):
    owner, successor = busy["owner"], busy["successor"]
    empty = personal_pipeline(owner, "Empty one")
    holding = personal_pipeline(owner, "Holding one")
    stage = holding.stages.get()
    OpportunityFactory(lead=busy["lead"], stage=stage)  # an open deal of the user's lead
    assert delete(signed_in(admin), owner, reassign_to=str(successor.pk)).status_code == 200
    empty.refresh_from_db()
    holding.refresh_from_db()
    assert empty.is_active is False  # archived, never deleted
    assert holding.is_active is True  # its deal (now the successor's) keeps its board
    assert Pipeline.objects.filter(pk__in=[empty.pk, holding.pk]).count() == 2
    event = AuditEvent.objects.get(action="user.deleted")
    assert event.metadata["archived_pipelines"] >= 1


# --- legal holds, retention, erasure ------------------------------------------------------------
def test_a_legal_hold_on_the_person_stops_the_deletion(admin, user_a):
    LegalHold.objects.create(
        subject_type=HoldSubject.USER,
        subject_id=user_a.pk,
        reference="MATTER-1",
        placed_by=admin.pk,
    )
    client = signed_in(admin)
    before = snapshot()
    response = delete(client, user_a)
    assert (response.status_code, code(response)) == (409, "legal_hold")
    user_a.refresh_from_db()
    assert user_a.pseudonymised_at is None
    assert user_a.status == UserStatus.ACTIVE
    assert snapshot() == before
    shown = preview(client, user_a).json()
    assert shown["legal_hold"] is True
    assert "legal hold" in shown["blocked_reason"]


def test_a_hold_on_a_lead_keeps_its_data_while_the_account_is_erased(admin, busy):
    lead = busy["lead"]
    hold = LegalHold.objects.create(
        subject_type=HoldSubject.LEAD, subject_id=lead.pk, reference="CASE-9", placed_by=admin.pk
    )
    first_name, email = lead.first_name, lead.email
    response = delete(signed_in(admin), busy["owner"], reassign_to=str(busy["successor"].pk))
    assert response.status_code == 200
    kept = Lead.objects.get(pk=lead.pk)
    assert (kept.first_name, kept.email) == (first_name, email)  # the customer's data is frozen
    hold.refresh_from_db()
    assert hold.released_at is None  # still held
    busy["owner"].refresh_from_db()
    assert busy["owner"].pseudonymised_at is not None


def test_their_personal_data_goes_and_the_erasure_is_recorded_for_restores(
    admin, user_a, ledger_dir
):
    Conversation.objects.create(actor=user_a, subject=user_a, workspace_kind="self")
    assert delete(signed_in(admin), user_a).status_code == 200
    assert not Conversation.objects.filter(actor=user_a).exists()
    kinds = [(e["kind"], e["subject"]) for e in map(json.loads, sorted_entries(ledger_dir))]
    assert kinds == [("user_pseudonymised", str(user_a.pk))]
    assert ledger.applied()[0] == 1
    erased = AuditEvent.objects.get(action="user.pseudonymised")
    assert erased.metadata["deleted"] is True
    assert erased.actor_id == admin.pk


def sorted_entries(directory: Path) -> list[str]:
    return [p.read_text() for p in sorted(directory.glob("*.json"))]


def test_a_support_session_with_the_deleted_user_ends(team):
    boss, member = team
    SupportSession.objects.create(
        admin=boss,
        target=member,
        session_digest="a" * 64,
        expires_at=timezone.now() + timedelta(minutes=20),
    )
    assert delete(signed_in(boss), member).status_code == 200
    ended = SupportSession.objects.get(target=member)
    assert ended.ended_at is not None


# --- the confirmation dialog's data ---------------------------------------------------------
def test_the_preview_counts_what_will_move_and_why_it_is_blocked(admin, busy, team):
    boss, _ = team
    LeadFactory(owner=boss)
    personal_pipeline(busy["owner"], "Mine")
    client = signed_in(admin)
    shown = preview(client, busy["owner"]).json()
    assert shown == {
        "leads": 2,
        "open_deals": 1,
        "open_activities": 2,
        "personal_pipelines": 1,
        "team_members": 0,
        "legal_hold": False,
        "needs_new_owner": True,
        "blocked_reason": None,
    }
    assert preview(client, boss).json()["team_members"] == 1
    self_preview = preview(client, admin).json()
    assert self_preview["blocked_reason"] is not None  # yourself
    assert preview(client, AdminFactory()).json()["blocked_reason"] is not None  # a super admin
    quiet = preview(client, UserFactory()).json()
    assert quiet["needs_new_owner"] is False
    assert quiet["blocked_reason"] is None
    # Nothing changed by looking.
    busy["owner"].refresh_from_db()
    assert busy["owner"].pseudonymised_at is None


def test_a_deleted_user_stays_listed_as_a_former_user_with_nothing_to_act_on(admin, user_a):
    client = signed_in(admin)
    assert delete(client, user_a).status_code == 200
    row = client.get(f"/api/v1/admin/users/{user_a.pk}").json()
    assert row["pseudonymised_at"] is not None
    assert row["status"] == "deactivated"
    assert user_a.email not in json.dumps(row)
    for action in ("reset-password", "activate", "resend-invitation"):
        refused = client.post(f"/api/v1/admin/users/{user_a.pk}/{action}", {}, format="json")
        assert refused.status_code == 422
        assert code(refused) == "account_pseudonymised"
