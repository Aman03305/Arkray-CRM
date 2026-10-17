"""The independent review of account deletion (ADR-0036), as regression tests.

- M1: deletion used to hold the account's row and then wait for CRM rows, the reverse of the
  project's lock order (lead, then user rows FOR SHARE): any write on the user's leads or
  pipelines at that moment was a deadlock. Now nothing is held across the hand-over.
- M2: the hand-over ran under the user-administration lock and the legal-hold barrier for the
  whole batch (longer than lock_timeout): other user administration and hold placement failed.
- M3: erasure reached other people's data when an Admin (no privacy.manage) deleted a user whose
  name they had chosen, and erased the Admin's own audit details about the account.
- M4: an archived lead with open work made the account undeletable.
- L1: a team Admin was told whether a legal hold existed (R148).
- L2/L6: the reset email, the owner's own reset budget and Retry-After.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.db import OperationalError, connection, connections, transaction
from django.utils import timezone

from arkray.activities import services as activity_services
from arkray.activities.models import Activity
from arkray.ai.models import Conversation, Question, QuestionStatus
from arkray.audit import services as audit
from arkray.audit.models import AuditDetail, AuditEvent
from arkray.core import holds
from arkray.core.access import AccessScope
from arkray.core.models import HoldSubject, LegalHold
from arkray.identity import services as identity_services
from arkray.identity.models import AccountToken, TokenPurpose, User
from arkray.leads import services as lead_services
from arkray.leads.models import Lead
from arkray.pipeline import configuration
from arkray.pipeline.models import Opportunity, Pipeline, Stage
from arkray.privacy import account_deletion, staff
from tests.factories import (
    AdminFactory,
    LeadFactory,
    OpportunityFactory,
    TaskFactory,
    TeamAdminFactory,
    UserFactory,
)
from tests.helpers import drain_outbox, signed_in

WAIT_S = 20.0
USERS = "/api/v1/admin/users"


def delete(client: Any, target: User, **body: Any) -> Any:
    payload = {"version": target.version, "confirm_email": target.email, **body}
    return client.post(f"{USERS}/{target.pk}/delete", payload, format="json")


class Running:
    """A call on a thread of its own (its own connection), waited for with `join`."""

    def __init__(self, target: Callable[[], Any]) -> None:
        self.result: Any = None
        self.pid = 0
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(target,), daemon=True)
        self._thread.start()
        assert self._connected.wait(WAIT_S), "the thread never connected"

    def _run(self, target: Callable[[], Any]) -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                self.pid = cursor.fetchone()[0]
            self._connected.set()
            self.result = target()
        except BaseException as exc:  # handed to the test
            self.result = exc
        finally:
            self._connected.set()
            connections.close_all()

    def join(self, timeout: float = WAIT_S * 2) -> Any:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the thread is still running"
        return self.result

    def waits_for_a_lock(self) -> bool:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline and self._thread.is_alive():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [self.pid]
                )
                row = cursor.fetchone()
            if row is not None and row[0] == "Lock":
                return True
            time.sleep(0.01)
        return False


def delete_call(actor: User, target: User, new_owner: User) -> Callable[[], Any]:
    def call() -> Any:
        return account_deletion.delete_account(
            actor_id=actor.pk,
            user_id=target.pk,
            version=target.version,
            confirm_email=target.email,
            reassign_to=new_owner.pk,
        )

    return call


def finished(first: Any, actor: User, target: User, new_owner: User) -> Any:
    """`first` if it completed the deletion; else the repeats a "not completed" asks for."""
    result = first
    for _ in range(5):
        assert isinstance(result, account_deletion.DeletionResult), result
        if result.completed:
            return result
        result = delete_call(actor, target, new_owner)()
    raise AssertionError(result)


# --- M1 and M2: no lock held across the hand-over ------------------------------------------------
@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("crm_configuration")
class TestTheHandOverHoldsNothingThatCrmWritesNeed:
    def test_a_task_added_to_the_users_lead_meanwhile_is_not_a_deadlock(self):
        """B (anyone in the user's workspace) adds a task to the user's lead: the lead's lock,
        then FOR SHARE on the owner. The deletion used to hold the owner's row and then ask for
        the lead: Postgres ended the cycle by aborting one of them (500)."""
        admin = AdminFactory()
        victim = UserFactory(first_name="Vic", last_name="Tim")
        successor = UserFactory(first_name="Suc", last_name="Cessor")
        lead = LeadFactory(owner=victim)
        b_holds_the_lead = threading.Event()
        release_b = threading.Event()
        original = activity_services.lock_assignable_user

        def paused(user_id: Any) -> bool:
            b_holds_the_lead.set()  # B has the lead; it stops right before the owner's row
            assert release_b.wait(WAIT_S)
            return bool(original(user_id))

        with mock.patch.object(activity_services, "lock_assignable_user", paused):
            b = Running(
                lambda: activity_services.create_activity(
                    actor=victim,
                    scope=AccessScope.own(victim.pk),
                    activity_type="task",
                    fields={"title": "Call back"},
                    lead_id=lead.pk,
                )
            )
            assert b_holds_the_lead.wait(WAIT_S)
            a = Running(delete_call(admin, victim, successor))
            assert a.waits_for_a_lock(), "the deletion did not wait for the lead"
            release_b.set()  # B now asks for the victim's row: the deletion holds none
            a_result, b_result = a.join(), b.join()

        assert not isinstance(b_result, BaseException), b_result
        assert not isinstance(a_result, OperationalError), a_result
        finished(a_result, admin, victim, successor)
        assert Lead.objects.get(pk=lead.pk).owner_id == successor.pk
        task = Activity.objects.get(title="Call back")
        assert task.owner_id == successor.pk  # the work that arrived meanwhile followed the lead
        assert User.objects.get(pk=victim.pk).pseudonymised_at is not None

    def test_a_pipeline_created_in_the_users_workspace_meanwhile_is_not_a_deadlock(self):
        """B creates a pipeline in the user's workspace: the owner's advisory lock, then FOR
        SHARE on the owner. The deletion used to hold the owner's row and then ask for the same
        advisory lock (archiving their pipelines)."""
        admin = AdminFactory()
        victim = UserFactory(first_name="Vic", last_name="Tim")
        successor = UserFactory(first_name="Suc", last_name="Cessor")
        pipeline = Pipeline.objects.create(key="pvictim0001", name="Mine", owner=victim)
        Stage.objects.create(
            pipeline=pipeline,
            key="first",
            name="First",
            position=10,
            probability=10,
            category="open",
        )
        b_holds_the_owner_lock = threading.Event()
        release_b = threading.Event()
        original = configuration.lock_assignable_user

        def paused(user_id: Any) -> bool:
            b_holds_the_owner_lock.set()
            assert release_b.wait(WAIT_S)
            return bool(original(user_id))

        stages = [
            {"name": "Open", "probability": 10, "type": "open"},
            {"name": "Won", "probability": 100, "type": "won"},
            {"name": "Lost", "probability": 0, "type": "lost"},
        ]
        scope = AccessScope.for_user(admin.pk, victim.pk)
        with mock.patch.object(configuration, "lock_assignable_user", paused):
            b = Running(
                lambda: configuration.create_pipeline(
                    actor=admin, scope=scope, name="Another", stages=stages
                )
            )
            assert b_holds_the_owner_lock.wait(WAIT_S)
            a = Running(delete_call(admin, victim, successor))
            assert a.waits_for_a_lock(), "the deletion did not wait for the advisory lock"
            release_b.set()
            a_result, b_result = a.join(), b.join()

        assert not isinstance(b_result, BaseException), b_result
        assert not isinstance(a_result, OperationalError), a_result
        assert finished(a_result, admin, victim, successor).completed
        pipeline.refresh_from_db()
        assert pipeline.is_active is False  # archived, kept

    def test_a_user_who_keeps_working_while_their_account_is_deleted_loses_nothing(self):
        """No patching: the user adds tasks to their own leads in a loop while an admin deletes
        the account. No deadlock for either, and nothing left on the erased account."""
        admin = AdminFactory()
        victim = UserFactory(first_name="Vic", last_name="Tim")
        successor = UserFactory(first_name="Suc", last_name="Cessor")
        leads = [LeadFactory(owner=victim) for _ in range(40)]
        stop = threading.Event()
        errors: list[str] = []

        def keep_working() -> None:
            while not stop.is_set():
                try:
                    activity_services.create_activity(
                        actor=victim,
                        scope=AccessScope.own(victim.pk),
                        activity_type="task",
                        fields={"title": "Call back"},
                        lead_id=random.choice(leads).pk,
                    )
                except BaseException as exc:  # refused once the lead moved or the user left
                    errors.append(f"{type(exc).__name__}: {exc}")
                time.sleep(0.005)

        worker = Running(keep_working)
        time.sleep(0.3)
        deleter = Running(
            lambda: finished(delete_call(admin, victim, successor)(), admin, victim, successor)
        )
        outcome = deleter.join()
        stop.set()
        worker.join()

        assert not isinstance(outcome, BaseException), outcome
        assert not [e for e in errors if "deadlock" in e.lower()], errors[:5]
        assert User.objects.get(pk=victim.pk).pseudonymised_at is not None
        assert staff.owned_work(User.objects.get(pk=victim.pk)) == (0, 0, 0)
        assert not Lead.objects.filter(owner=victim, archived_at__isnull=True).exists()

    def test_other_user_administration_and_hold_placement_go_on_during_the_hand_over(self):
        """The hand-over used to run under the user-administration lock and the legal-hold
        barrier: another administrator's `create_user` and a hold being placed waited for the
        whole batch (and past lock_timeout, failed)."""
        admin, other_admin = AdminFactory(), AdminFactory()
        victim, successor, bystander = UserFactory(), UserFactory(), UserFactory()
        LeadFactory(owner=victim)
        started, done = threading.Event(), threading.Event()
        original = lead_services.reassign_lead

        def paused(*args: Any, **kwargs: Any) -> Any:
            started.set()  # inside the hand-over, between its transactions
            assert done.wait(10), "the other administrator never got through"
            return original(*args, **kwargs)

        with mock.patch.object(lead_services, "reassign_lead", paused):
            a = Running(delete_call(admin, victim, successor))
            assert started.wait(WAIT_S)
            began = time.monotonic()
            created = identity_services.create_user(
                actor_id=other_admin.pk,
                email="newcomer@example.test",
                first_name="New",
                last_name="Comer",
                role="sales_user",
            )
            with transaction.atomic():
                holds.place(HoldSubject.USER, bystander.pk, reference="CASE-1", placed_by=admin.pk)
            took = time.monotonic() - began
            done.set()
            result = a.join()

        assert took < 3, f"another administrator waited {took:.1f}s for the hand-over"
        assert created.email == "newcomer@example.test"
        assert LegalHold.objects.filter(subject_id=bystander.pk).exists()
        assert isinstance(result, account_deletion.DeletionResult), result
        assert result.completed


# --- M3: an Admin's deletion erases the account's own traces, not other people's -----------------
def answered(
    actor: User, text: str, *, subject: User | None = None, kind: str = "self"
) -> Question:
    conversation = Conversation.objects.create(
        actor=actor, workspace_kind=kind, subject=subject or actor
    )
    now = timezone.now()
    return Question.objects.create(
        conversation=conversation,
        actor=actor,
        text=text,
        status=QuestionStatus.ANSWERED,
        mode="llm",
        answer={"blocks": [], "sources": []},
        created_at=now,
        finished_at=now,
        expires_at=now + timedelta(minutes=2),
    )


@pytest.mark.django_db
class TestAnAdminsDeletionErasesOnlyTheAccountsOwnTraces:
    @pytest.fixture
    def world(self):
        tara = TeamAdminFactory(first_name="Tara", last_name="Lead")
        anita = AdminFactory(first_name="Anita", last_name="Admin")
        # A user whose name an Admin chose to match something a Super admin asked about.
        decoy = UserFactory(first_name="Anita", last_name="Admin", manager=tara)
        return tara, anita, decoy

    def test_conversations_that_merely_name_them_are_left_alone(self, world):
        tara, anita, decoy = world
        theirs = answered(decoy, "My own question")
        others = answered(anita, "Which deals did Anita Admin reassign last week?")
        response = delete(signed_in(tara), decoy)
        assert response.status_code == 200, response.content
        assert not Question.objects.filter(pk=theirs.pk).exists()  # their own conversation goes
        assert Question.objects.filter(pk=others.pk).exists()  # a Super admin's stays
        deleted = AuditEvent.objects.get(action="user.deleted")
        assert deleted.metadata["erasure"] == "limited"
        assert AuditEvent.objects.get(action="user.pseudonymised").metadata["limited"] is True

    def test_a_super_admins_deletion_still_erases_everything_the_privacy_step_does(self, world):
        _, anita, decoy = world
        named = answered(anita, "Which deals did Anita Admin reassign last week?")
        response = delete(signed_in(AdminFactory()), decoy)
        assert response.status_code == 200, response.content
        assert not Question.objects.filter(pk=named.pk).exists()
        assert AuditEvent.objects.get(action="user.deleted").metadata["erasure"] == "full"

    def test_the_details_of_what_the_admin_did_about_the_account_are_kept(self, world):
        tara, _, decoy = world
        # Something Tara did about the member, with her address (an AuditDetail row), and
        # something the member did themselves.
        about_them = audit.record(
            "auth.password_set_by_admin",
            actor_id=tara.pk,
            target_type="user",
            target_id=decoy.pk,
            subject_user_id=decoy.pk,
            sensitive={"requested_from": "198.51.100.7"},
        )
        by_them = audit.record(
            "auth.login",
            actor_id=decoy.pk,
            target_type="user",
            target_id=decoy.pk,
            sensitive={"requested_from": "203.0.113.9"},
        )
        assert AuditDetail.objects.filter(event__in=[about_them, by_them]).count() == 2
        assert delete(signed_in(tara), decoy).status_code == 200
        assert AuditDetail.objects.filter(event=about_them).exists()  # follows its own retention
        assert not AuditDetail.objects.filter(event=by_them).exists()  # their own trace is gone

    def test_a_super_admins_deletion_removes_both(self, world):
        tara, _, decoy = world
        about_them = audit.record(
            "auth.password_set_by_admin",
            actor_id=tara.pk,
            target_type="user",
            target_id=decoy.pk,
            subject_user_id=decoy.pk,
            sensitive={"requested_from": "198.51.100.7"},
        )
        assert delete(signed_in(AdminFactory()), decoy).status_code == 200
        assert not AuditDetail.objects.filter(event=about_them).exists()


# --- M4: archived leads with open work -----------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.usefixtures("crm_configuration")
def test_an_archived_lead_with_open_work_is_handed_over_not_a_dead_end(admin):
    victim, successor = UserFactory(), UserFactory()
    live = LeadFactory(owner=victim)
    shelved = LeadFactory(owner=victim, archived_at=timezone.now())
    deal = OpportunityFactory(lead=shelved, stage_key="new")  # open work on an archived lead
    task = TaskFactory(lead=shelved, owner=victim, created_by=victim)
    quiet = LeadFactory(owner=victim, archived_at=timezone.now())  # archived, nothing open
    client = signed_in(admin)

    shown = client.get(f"{USERS}/{victim.pk}/deletion-preview").json()
    assert shown["leads"] == 2  # the live lead and the archived one that carries open work
    assert shown["open_deals"] == 1
    assert shown["open_activities"] == 1
    assert shown["needs_new_owner"] is True
    assert shown["blocked_reason"] is None

    response = delete(client, victim, reassign_to=str(successor.pk))
    assert response.status_code == 200, response.content
    assert response.json() == {"completed": True, "reassigned_leads": 2, "remaining_leads": 0}
    assert Lead.objects.get(pk=live.pk).owner_id == successor.pk
    assert Lead.objects.get(pk=shelved.pk).owner_id == successor.pk
    assert Lead.objects.get(pk=shelved.pk).archived_at is not None  # still archived
    assert Opportunity.objects.get(pk=deal.pk).owner_id == successor.pk
    assert Activity.objects.get(pk=task.pk).owner_id == successor.pk
    assert Lead.objects.get(pk=quiet.pk).owner_id == victim.pk  # history stays with them
    assert User.objects.get(pk=victim.pk).pseudonymised_at is not None


# --- L1: an Admin is not told about legal holds --------------------------------------------------
@pytest.mark.django_db
def test_an_admin_learns_nothing_about_a_legal_hold_on_a_member():
    tara = TeamAdminFactory()
    member = UserFactory(manager=tara)
    LegalHold.objects.create(
        subject_type=HoldSubject.USER, subject_id=member.pk, reference="MATTER-1", placed_by=tara.pk
    )
    client = signed_in(tara)

    shown = client.get(f"{USERS}/{member.pk}/deletion-preview").json()
    assert shown["legal_hold"] is False
    assert shown["blocked_reason"] == account_deletion.BLOCKED
    assert "hold" not in shown["blocked_reason"].lower()

    refused = delete(client, member)
    assert refused.status_code == 422  # not the 409 `legal_hold` a Super admin gets
    assert refused.json()["error"]["code"] == "business_rule_violation"
    assert refused.json()["error"]["message"] == account_deletion.BLOCKED
    assert User.objects.get(pk=member.pk).pseudonymised_at is None


# --- L2 and L6: the reset email, the owner's own budget, Retry-After -----------------------------
@pytest.mark.django_db
class TestTheResetAnAdministratorSends:
    def test_its_email_says_an_administrator_did_it_and_never_advises_ignoring_it(
        self, admin, user_a
    ):
        mail.outbox.clear()
        assert (
            signed_in(admin).post(f"{USERS}/{user_a.pk}/reset-password", {}, format="json")
        ).status_code == 200
        drain_outbox()
        link_mail = next(m for m in mail.outbox if "reset-password/" in str(m.body))
        assert "An administrator reset the password" in link_mail.body
        assert "no longer works" in link_mail.body
        assert "you can ignore this email" not in link_mail.body
        assert link_mail.to == [user_a.email]

    def test_the_owners_own_request_keeps_its_wording(self, user_a):
        mail.outbox.clear()
        identity_services.issue_password_reset(user_a.email)
        drain_outbox()
        link_mail = next(m for m in mail.outbox if "reset-password/" in str(m.body))
        assert "We received a request to reset the password" in link_mail.body
        assert "you can ignore this email" in link_mail.body

    def test_links_an_administrator_sent_do_not_use_up_the_owners_own_requests(self, admin, user_a):
        client = signed_in(admin)
        for _ in range(3):  # the administrators' limit
            assert (
                client.post(f"{USERS}/{user_a.pk}/reset-password", {}, format="json")
            ).status_code == 200
        identity_services.issue_password_reset(user_a.email)
        own = AccountToken.objects.filter(
            user=user_a, purpose=TokenPurpose.PASSWORD_RESET, created_by__isnull=True
        )
        assert own.count() == 1  # not suppressed (`auth.password_reset_suppressed`)
        assert not AuditEvent.objects.filter(action="auth.password_reset_suppressed").exists()

    def test_retry_after_is_when_the_oldest_counted_link_leaves_the_hour(self, admin, user_a):
        client = signed_in(admin)
        for _ in range(3):
            assert (
                client.post(f"{USERS}/{user_a.pk}/reset-password", {}, format="json")
            ).status_code == 200
        oldest = AccountToken.objects.filter(
            user=user_a, purpose=TokenPurpose.PASSWORD_RESET
        ).order_by("created_at")
        # Twenty minutes into the hour: the oldest of the three leaves it in forty.
        AccountToken.objects.filter(pk__in=[t.pk for t in oldest]).update(
            created_at=timezone.now() - timedelta(minutes=20)
        )
        limited = client.post(f"{USERS}/{user_a.pk}/reset-password", {}, format="json")
        assert limited.status_code == 429
        retry_after = int(limited["Retry-After"])
        assert 39 * 60 <= retry_after <= 41 * 60, retry_after

    def test_the_password_changed_date_moves_with_the_replaced_password(self, admin, user_a):
        assert user_a.password_changed_at is None
        signed_in(admin).post(f"{USERS}/{user_a.pk}/reset-password", {}, format="json")
        assert User.objects.get(pk=user_a.pk).password_changed_at is not None


# --- the gauge for a burst of deletions ----------------------------------------------------------
@pytest.mark.django_db
def test_deletions_are_counted_for_the_alert(admin):
    from arkray.identity import metrics

    victims = [UserFactory() for _ in range(2)]
    client = signed_in(admin)
    for victim in victims:
        assert delete(client, victim).status_code == 200
    gauge = next(g for g in metrics.gauges() if g.name == "arkray_account_deletions_last_hour")
    assert sum(value for _, value in gauge.samples) == 2
