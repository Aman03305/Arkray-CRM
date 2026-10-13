"""No administrator pseudonymises another (audit SEC-2; ADR-0030; privacy.staff;
docs/privacy.md#staff).

Pseudonymising replaces an account's email and password: done by one administrator to
another, it is the account change ADR-0030 reserves to the administrator themselves, and
nobody pseudonymises their own account. So the API refuses every account whose role is
administrator (active, invited or deactivated alike) with 403 and changes nothing; the
service refuses it too (on the locked row, under the user-administration lock), and the
only way is the operator's command with `--allow-administrator`. A replay of the erasure
ledger re-applies an administrator's pseudonymisation like any other.
"""

from __future__ import annotations

import json
import threading
import time
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction

from arkray.audit.models import AuditEvent
from arkray.core import ledger
from arkray.core.errors import BusinessRuleViolation, ConflictError, PermissionDeniedError
from arkray.core.ledger_gate import GATE
from arkray.core.models import LedgerState
from arkray.identity import services as identity_services
from arkray.identity.models import AccountToken, Role, User, UserStatus
from arkray.privacy import replay, staff
from tests.factories import AdminFactory, InvitedUserFactory, UserFactory
from tests.helpers import run_concurrently, signed_in

KEY = "ledger-test-key-" + "x" * 32


def url(user: User) -> str:
    return f"/api/v1/admin/privacy/users/{user.pk}/pseudonymise"


def snapshot(user: User) -> tuple[object, ...]:
    """What pseudonymising would change."""
    row = User.objects.get(pk=user.pk)
    tokens = sorted(AccountToken.objects.filter(user=row).values_list("id", "status"))
    return (
        row.first_name,
        row.last_name,
        row.email,
        row.password,
        row.role,
        row.status,
        row.session_epoch,
        row.version,
        tokens,
    )


def pseudonymised_events() -> int:
    return AuditEvent.objects.filter(action=staff.AUDIT_PSEUDONYMISED).count()


def former_administrator(admin, **extra):
    """Another administrator, deactivated (off-boarding is allowed, ADR-0030)."""
    other = AdminFactory(first_name="Bina", last_name="Admin", **extra)
    identity_services.deactivate_user(actor_id=admin.pk, user_id=other.pk)
    return User.objects.get(pk=other.pk)


@pytest.mark.django_db
class TestTheApi:
    @pytest.mark.parametrize("status", ["deactivated", "active", "invited"])
    def test_another_administrator_is_refused_with_403_and_nothing_changes(self, admin, status):
        if status == "deactivated":
            other = former_administrator(admin)
        elif status == "active":
            other = AdminFactory(first_name="Bina")
        else:
            other = InvitedUserFactory(role=Role.ADMIN, first_name="Bina")
        before = snapshot(other)
        response = signed_in(admin).post(url(other), {"confirm_email": other.email}, format="json")
        assert response.status_code == 403
        body = response.json()["error"]
        assert (body["code"], body["message"]) == ("permission_denied", staff.ADMINISTRATOR_REFUSED)
        assert snapshot(other) == before
        assert pseudonymised_events() == 0

    def test_the_audit_probe_scenario(self, admin):
        """The finding as reported: deactivate Bina, then pseudonymise her."""
        other = AdminFactory(first_name="Bina", last_name="Admin")
        client = signed_in(admin)
        assert client.post(f"/api/v1/admin/users/{other.pk}/deactivate").status_code == 200
        email = User.objects.get(pk=other.pk).email
        response = client.post(url(other), {"confirm_email": email}, format="json")
        assert response.status_code == 403
        other.refresh_from_db()
        assert other.email == email
        assert other.has_usable_password()

    def test_your_own_account_is_refused(self, admin):
        before = snapshot(admin)
        response = signed_in(admin).post(url(admin), {"confirm_email": admin.email}, format="json")
        assert response.status_code == 422
        assert response.json()["error"]["message"] == staff.NOT_YOURSELF
        assert snapshot(admin) == before

    def test_a_sales_user_cannot(self, admin, user_a, user_b):
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_b.pk)
        before = snapshot(user_b)
        response = signed_in(user_a).post(
            url(user_b), {"confirm_email": user_b.email}, format="json"
        )
        assert response.status_code == 403
        assert snapshot(user_b) == before

    def test_refused_inside_a_support_session(self, admin, user_a):
        other = former_administrator(admin)
        before = snapshot(other)
        client = signed_in(admin)
        started = client.post(
            "/api/v1/admin/support-sessions", {"user": str(user_a.pk)}, format="json"
        )
        assert started.status_code == 201, started.content
        response = client.post(url(other), {"confirm_email": other.email}, format="json")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "support_session_active"
        assert snapshot(other) == before

    def test_a_former_sales_user_is_still_pseudonymised(self, admin, user_b):
        identity_services.deactivate_user(actor_id=admin.pk, user_id=user_b.pk)
        response = signed_in(admin).post(
            url(user_b), {"confirm_email": user_b.email}, format="json"
        )
        assert response.status_code == 200, response.content
        user_b.refresh_from_db()
        assert staff.pseudonymised(user_b)
        assert not user_b.has_usable_password()
        event = AuditEvent.objects.get(action=staff.AUDIT_PSEUDONYMISED)
        assert event.actor_id == admin.pk
        assert "administrator_target" not in event.metadata

    def test_an_administrator_demoted_by_the_operator_is_a_former_user_like_any_other(self, admin):
        """The role is what the account is now: no administrator can demote another
        (ADR-0030), so only the operator's documented shell demotion gets here."""
        other = former_administrator(admin)
        User.objects.filter(pk=other.pk).update(role=Role.SALES_USER)
        response = signed_in(admin).post(url(other), {"confirm_email": other.email}, format="json")
        assert response.status_code == 200, response.content


@pytest.mark.django_db
class TestTheService:
    def test_another_administrator_is_refused(self, admin):
        other = former_administrator(admin)
        before = snapshot(other)
        with pytest.raises(PermissionDeniedError) as refused:
            staff.pseudonymise(other.pk, operator_id=admin.pk)
        assert refused.value.message == staff.ADMINISTRATOR_REFUSED
        with pytest.raises(PermissionDeniedError):
            staff.preview(other.pk, operator_id=admin.pk)
        assert snapshot(other) == before
        assert pseudonymised_events() == 0

    def test_yourself_is_refused_even_with_the_operator_flag(self, admin):
        with pytest.raises(BusinessRuleViolation):
            staff.pseudonymise(admin.pk, operator_id=admin.pk, allow_administrator=True)

    @pytest.mark.parametrize("operator", ["sales_user", "deactivated_admin", "unknown"])
    def test_only_an_administrator_with_privacy_manage_operates(self, admin, user_a, operator):
        target = UserFactory(first_name="Kiran")
        identity_services.deactivate_user(actor_id=admin.pk, user_id=target.pk)
        if operator == "sales_user":
            operator_id = user_a.pk
        elif operator == "deactivated_admin":
            operator_id = former_administrator(admin).pk
        else:
            operator_id = uuid4()
        before = snapshot(target)
        with pytest.raises(PermissionDeniedError):
            staff.pseudonymise(target.pk, operator_id=operator_id)
        with pytest.raises(PermissionDeniedError):
            staff.preview(target.pk, operator_id=operator_id)
        assert snapshot(target) == before


@pytest.mark.django_db
class TestTheOperatorCommand:
    def test_refused_without_the_flag(self, admin):
        other = former_administrator(admin)
        before = snapshot(other)
        for extra in ({}, {"yes": True}):
            with pytest.raises(CommandError, match="--allow-administrator"):
                call_command("pseudonymise_user", str(other.pk), by=admin.email, **extra)
        assert snapshot(other) == before

    def test_a_dry_run_with_the_flag_changes_nothing(self, admin):
        other = former_administrator(admin)
        before = snapshot(other)
        out = StringIO()
        call_command(
            "pseudonymise_user", str(other.pk), by=admin.email, allow_administrator=True, stdout=out
        )
        assert "Dry run" in out.getvalue()
        assert snapshot(other) == before

    def test_with_the_flag_it_is_done_and_audited(self, admin):
        other = former_administrator(admin)
        call_command(
            "pseudonymise_user",
            str(other.pk),
            "--allow-administrator",
            "--yes",
            by=admin.email,
            stdout=StringIO(),
        )
        other.refresh_from_db()
        assert staff.pseudonymised(other)
        assert not other.has_usable_password()
        assert other.role == Role.ADMIN  # the role stays, like every former user's
        event = AuditEvent.objects.get(action=staff.AUDIT_PSEUDONYMISED)
        assert (event.actor_id, event.subject_user_id) == (admin.pk, other.pk)
        assert event.metadata["administrator_target"] is True
        assert "bina" not in json.dumps(event.metadata).lower()

    def test_the_flag_does_not_lift_the_other_rules(self, admin):
        active = AdminFactory()
        with pytest.raises(CommandError, match="Deactivate the account first"):
            call_command(
                "pseudonymise_user",
                str(active.pk),
                by=admin.email,
                allow_administrator=True,
                yes=True,
            )
        assert not staff.pseudonymised(User.objects.get(pk=active.pk))


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


@pytest.mark.django_db
def test_replay_pseudonymises_a_restored_administrator_again(ledger_dir, admin):
    """The pseudonymisation was authorised when it happened (the operator's command): a
    restored backup that has the administrator back, active and named, is fixed again."""
    other = former_administrator(admin)
    before = ledger.applied()
    staff.pseudonymise(other.pk, operator_id=admin.pk, allow_administrator=True)
    with connection.cursor() as cursor:  # the backup had the account active, named
        # Taken before the pseudonymisation, so not marked either (review of 73ff1e3, L4: the
        # mark is kept by a trigger, which a restore doesn't run; nor does this simulation).
        cursor.execute("SET session_replication_role = replica")
        cursor.execute(
            "UPDATE identity_user SET first_name = 'Bina', last_name = 'Admin',"
            " email = 'bina.restored@example.test', status = 'active', is_active = true,"
            " deactivated_at = NULL, password = 'md5$restored$x', pseudonymised_at = NULL"
            " WHERE id = %s",
            [other.pk],
        )
        cursor.execute("SET session_replication_role = origin")
    LedgerState.objects.filter(pk=1).update(applied_seq=before[0], applied_mac=before[1])
    assert replay.replay(operator=admin).counts() == {"reapplied": 1}
    restored = User.objects.get(pk=other.pk)
    assert staff.pseudonymised(restored)
    assert (restored.role, restored.status, restored.has_usable_password()) == (
        Role.ADMIN,
        UserStatus.DEACTIVATED,
        False,
    )
    again = AuditEvent.objects.filter(action=staff.AUDIT_PSEUDONYMISED).order_by("-pk").first()
    assert again is not None
    assert again.metadata["replay"] is True
    assert again.metadata["administrator_target"] is True


@pytest.mark.django_db
def test_only_a_replay_runs_without_an_operator_and_it_is_audited_as_the_system(admin):
    """The automatic ledger reconciliation replays as the system (operator None)."""
    other = former_administrator(admin)
    before = snapshot(other)
    with pytest.raises(ValueError, match="without an operator"):
        staff.pseudonymise(other.pk, operator_id=None)
    with pytest.raises(ValueError, match="without an operator"):
        staff.pseudonymise(other.pk, operator_id=None, allow_administrator=True)
    assert snapshot(other) == before
    assert pseudonymised_events() == 0

    staff.pseudonymise(other.pk, operator_id=None, replay=True)
    assert staff.pseudonymised(User.objects.get(pk=other.pk))
    event = AuditEvent.objects.get(action=staff.AUDIT_PSEUDONYMISED)
    assert event.actor_id is None
    assert event.metadata["replay"] is True
    assert event.metadata["administrator_target"] is True


# --- races ----------------------------------------------------------------------------------
def _hold_then(first, then, hold_s: float = 0.6) -> list[object]:
    """Run `first` in a transaction kept open for `hold_s` after it returns, and start `then`
    in another connection while it is open: `then` must wait for the lock and see what
    `first` committed."""
    results: list[object] = [None, None]
    holding = threading.Event()

    def run_first() -> None:
        try:
            with transaction.atomic():
                results[0] = first()
                holding.set()
                time.sleep(hold_s)
        except BaseException as exc:
            results[0] = exc
        finally:
            holding.set()
            connections.close_all()

    def run_then() -> None:
        try:
            holding.wait(10)
            results[1] = then()
        except BaseException as exc:
            results[1] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run_first), threading.Thread(target=run_then)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


def _deactivated_sales_user(admin):
    user = UserFactory(first_name="Kiran")
    identity_services.deactivate_user(actor_id=admin.pk, user_id=user.pk)
    return User.objects.get(pk=user.pk)


def _promote(actor: User, user: User) -> Any:
    return identity_services.update_user(
        actor_id=actor.pk, user_id=user.pk, version=user.version, changes={"role": "admin"}
    )


@pytest.mark.django_db(transaction=True)
class TestRaces:
    def test_a_pseudonymisation_waiting_on_a_promotion_sees_the_administrator(self):
        """The role is read from the locked row, after the lock: a check made before it
        would have seen a sales user and pseudonymised a new administrator."""
        first, second = AdminFactory(), AdminFactory()
        c = _deactivated_sales_user(first)
        results = _hold_then(
            lambda: _promote(first, c),
            lambda: staff.pseudonymise(c.pk, operator_id=second.pk),
        )
        assert isinstance(results[0], User), results[0]
        assert isinstance(results[1], PermissionDeniedError), results[1]
        c.refresh_from_db()
        assert c.role == Role.ADMIN
        assert not staff.pseudonymised(c)
        assert c.has_usable_password()

    def test_a_promotion_waiting_on_a_pseudonymisation_finds_the_account_changed(self):
        first, second = AdminFactory(), AdminFactory()
        c = _deactivated_sales_user(first)
        results = _hold_then(
            lambda: staff.pseudonymise(c.pk, operator_id=second.pk),
            lambda: _promote(first, c),
        )
        assert isinstance(results[0], staff.StaffErasure), results[0]
        # Refused for what it now is, before the version (review of 73ff1e3, L4; it was the
        # version's 409 before).
        assert isinstance(results[1], identity_services.AccountPseudonymisedError), results[1]
        c.refresh_from_db()
        assert staff.pseudonymised(c)
        assert c.role == Role.SALES_USER

    def test_a_rolled_back_operator_demotion_leaves_the_administrator_protected(self):
        """The operator's shell demotion (security.md) holds the row; it is abandoned: the
        waiting pseudonymisation sees the role that was committed, administrator."""
        first, second = AdminFactory(), AdminFactory()
        b = former_administrator(first)
        before = snapshot(b)

        def demote_then_abandon() -> None:
            User.objects.select_for_update().get(pk=b.pk)
            User.objects.filter(pk=b.pk).update(role=Role.SALES_USER)

        holding = threading.Event()
        results: list[object] = [None, None]

        def operator() -> None:
            try:
                with transaction.atomic():
                    demote_then_abandon()
                    holding.set()
                    time.sleep(0.6)
                    raise RuntimeError("abandoned")
            except BaseException as exc:
                results[0] = exc
            finally:
                holding.set()
                connections.close_all()

        def administrator() -> None:
            try:
                holding.wait(10)
                results[1] = staff.pseudonymise(b.pk, operator_id=second.pk)
            except BaseException as exc:
                results[1] = exc
            finally:
                connections.close_all()

        threads = [threading.Thread(target=operator), threading.Thread(target=administrator)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert isinstance(results[0], RuntimeError), results[0]
        assert isinstance(results[1], PermissionDeniedError), results[1]
        assert snapshot(b) == before

    def test_racing_promotions_and_pseudonymisations_never_pseudonymise_an_administrator(self):
        for _ in range(4):
            first, second = AdminFactory(), AdminFactory()
            c = _deactivated_sales_user(first)
            results = run_concurrently(
                lambda c=c, a=first: _promote(a, c),
                lambda c=c, a=second: staff.pseudonymise(c.pk, operator_id=a.pk),
            )
            assert sum(isinstance(r, User | staff.StaffErasure) for r in results) == 1, results
            # A promotion after the pseudonymisation is refused for what the account now is
            # (review of 73ff1e3, L4: AccountPseudonymisedError; the version's 409 before).
            refusals = (
                PermissionDeniedError | ConflictError | identity_services.AccountPseudonymisedError
            )
            for result in results:
                assert isinstance(result, User | staff.StaffErasure | refusals), repr(result)
            c.refresh_from_db()
            assert not (staff.pseudonymised(c) and c.role == Role.ADMIN)
