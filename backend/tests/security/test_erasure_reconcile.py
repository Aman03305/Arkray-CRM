"""Reconciling the database with the erasure ledger (final production audit ARCH-3, ARCH-6;
privacy.reconcile, privacy.replay).

ARCH-3: an erasure whose transaction fails after its ledger entry was written left the API
closed until an operator ran `replay_erasures`. The reconciliation applies the entry again
automatically, and the API reopens only once the database matches the ledger; a large gap
(a restore), a wrong ledger or an entry that can't be applied stays closed for an operator.

ARCH-6: a replay against an empty or wrong ledger (a container without the ledger volume)
reported success and moved the database's state backwards. It is refused, changing nothing.
"""

from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import transaction

from arkray.core import ledger
from arkray.core.ledger_gate import GATE
from arkray.leads.models import Lead
from arkray.privacy import reconcile, replay, services
from arkray.privacy.services import ERASED
from tests.factories import LeadFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

KEY = "ledger-test-key-" + "x" * 32


@pytest.fixture
def ledger_dir(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    settings.ERASURE_LEDGER_AUTO_RECONCILE_MAX = 10
    GATE.reset()
    yield directory
    GATE.reset()


class _Boom(Exception):
    pass


def failed_erasure(admin, owner):
    """An erasure whose transaction fails after its entry was written (killed before
    COMMIT, a constraint checked at COMMIT)."""
    lead = LeadFactory(owner=owner)

    def erase_then_fail():
        with transaction.atomic():
            services.erase(lead.pk, operator_id=admin.pk)
            raise _Boom

    with pytest.raises(_Boom):
        erase_then_fail()
    lead.refresh_from_db()
    assert lead.first_name != ERASED
    return lead


def api_status(user) -> int:
    GATE.reset()
    return signed_in(user).get("/api/v1/auth/me").status_code


def test_a_failed_erasure_commit_is_reconciled_and_the_api_reopens(ledger_dir, admin, user_a):
    lead = failed_erasure(admin, user_a)
    assert GATE.status() == "behind"
    assert api_status(user_a) == 503  # fail closed until the database matches
    assert reconcile.reconcile() == "reconciled"
    assert Lead.objects.get(pk=lead.pk).first_name == ERASED  # the erasure was asked for
    assert ledger.applied()[0] == 1
    assert api_status(user_a) == 200
    assert reconcile.reconcile() == "ok"


def test_only_the_missing_entries_are_applied(ledger_dir, admin, user_a):
    done = LeadFactory(owner=user_a)
    services.erase(done.pk, operator_id=admin.pk)
    failed_erasure(admin, user_a)
    applied = mock.Mock(wraps=replay._lead)
    with mock.patch.dict(replay._APPLY, {"lead_erased": applied}):
        assert reconcile.reconcile() == "reconciled"
    assert applied.call_count == 1  # entry 2 only; a restore's replay re-checks them all


def test_a_large_gap_waits_for_an_operator(ledger_dir, admin, user_a, settings, caplog):
    settings.ERASURE_LEDGER_AUTO_RECONCILE_MAX = 1
    first, second = failed_erasure(admin, user_a), failed_erasure(admin, user_a)
    with caplog.at_level(logging.ERROR, logger="arkray.privacy.reconcile"):
        assert reconcile.reconcile() == "operator"
    assert "erasure_ledger_reconciliation_required" in caplog.text
    assert api_status(user_a) == 503
    for lead in (first, second):
        assert Lead.objects.get(pk=lead.pk).first_name != ERASED
    out = Path(ledger_dir.parent / "report.json")
    call_command("replay_erasures", "--by", admin.email, "--report", str(out))
    assert api_status(user_a) == 200


def test_the_automation_can_be_turned_off(ledger_dir, admin, user_a, settings):
    settings.ERASURE_LEDGER_AUTO_RECONCILE_MAX = 0
    failed_erasure(admin, user_a)
    assert reconcile.reconcile() == "operator"
    assert api_status(user_a) == 503


def test_an_erasure_in_progress_is_left_alone(ledger_dir, admin, user_a):
    failed_erasure(admin, user_a)
    with mock.patch.object(ledger, "appending", return_value=True):
        assert reconcile.reconcile() == "settling"
    assert ledger.applied()[0] == 0


def test_an_entry_that_fails_keeps_the_api_closed(ledger_dir, admin, user_a, caplog):
    failed_erasure(admin, user_a)
    with (
        mock.patch.dict(replay._APPLY, {"lead_erased": mock.Mock(side_effect=RuntimeError)}),
        caplog.at_level(logging.ERROR, logger="arkray.privacy.reconcile"),
    ):
        assert reconcile.reconcile() == "failed"
    assert any("replay_erasures" in getattr(r, "reason", "") for r in caplog.records)
    assert ledger.applied()[0] == 0
    assert api_status(user_a) == 503


def test_a_hold_placed_since_waits_for_an_operator(ledger_dir, admin, user_a):
    from arkray.core.models import HoldSubject, LegalHold

    lead = failed_erasure(admin, user_a)
    LegalHold.objects.create(
        subject_type=HoldSubject.LEAD, subject_id=lead.pk, reference="CASE-9", placed_by=admin.pk
    )
    assert reconcile.reconcile() == "operator"
    assert Lead.objects.get(pk=lead.pk).first_name != ERASED
    assert api_status(user_a) == 503


def test_a_wrong_ledger_is_never_reconciled(ledger_dir, admin, user_a, tmp_path, settings):
    services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
    other = tmp_path / "another-ledger"
    other.mkdir()
    settings.ERASURE_LEDGER_URL = other.as_uri()
    for _ in range(2):  # another ledger, longer than what the database applied
        services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
    ledger.mark_applied(ledger.Entry(1, "lead_erased", "x", "t", "", "not-its-mac"))
    assert reconcile.reconcile() == "refused"
    assert ledger.applied() == (1, "not-its-mac")


def test_the_reconciliation_task_runs_while_the_gate_is_closed(ledger_dir, admin, user_a):
    from config.celery import app

    failed_erasure(admin, user_a)
    task = app.tasks["privacy.reconcile_ledger"]
    assert task.ledger_gate_exempt is True
    assert task.apply().get() == "reconciled"
    assert api_status(user_a) == 200


def test_the_reconciliation_runs_every_minute(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["privacy-reconcile-ledger"]
    assert entry["task"] == "privacy.reconcile_ledger"
    assert entry["schedule"].minute == set(range(60))  # every minute, wall clock


# --- ARCH-6: an empty or wrong ledger is never "replayed" -----------------------------------
def test_a_replay_against_an_empty_ledger_is_refused(ledger_dir, admin, user_a, tmp_path, settings):
    services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
    applied = ledger.applied()
    empty = tmp_path / "image-empty-ledger"
    empty.mkdir()
    settings.ERASURE_LEDGER_URL = empty.as_uri()
    with pytest.raises(replay.LedgerMismatch, match="applied 1 ledger entries but"):
        replay.replay(operator=admin)
    assert ledger.applied() == applied  # never moved backwards
    assert GATE.status() == "ahead"


def test_the_command_reports_an_empty_ledger_and_changes_nothing(
    ledger_dir, admin, user_a, tmp_path, settings
):
    services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
    empty = tmp_path / "image-empty-ledger"
    empty.mkdir()
    settings.ERASURE_LEDGER_URL = empty.as_uri()
    err = StringIO()
    with pytest.raises(SystemExit) as stopped:
        call_command(
            "replay_erasures", "--by", admin.email, "--report", str(tmp_path / "r.json"), stderr=err
        )
    assert stopped.value.code == 2
    assert "wrong ledger" in err.getvalue()
    assert "Nothing was changed" in err.getvalue()
    assert ledger.applied()[0] == 1


def test_a_ledger_whose_entries_differ_from_what_the_database_applied_is_refused(
    ledger_dir, admin, user_a
):
    services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
    services.erase(LeadFactory(owner=user_a).pk, operator_id=admin.pk)
    head = ledger.head()
    assert head is not None
    ledger.mark_applied(ledger.Entry(1, "lead_erased", "x", "t", "", "another-mac"))
    with pytest.raises(replay.LedgerMismatch, match="isn't the one this database applied"):
        replay.replay(operator=admin)
